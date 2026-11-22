"""A household waiting for a GFS operator's approval connects on its own.

Regression (#1413 follow-up): a household registered ``pending`` (the GFS
operator reviews new households) stayed pending forever after the operator
approved it — nothing asked the GFS again, the GFS has no way to tell, and a
restart did not help. The household now asks over ``POST /gfs/proof`` with a
signed question (:mod:`socialhome.gfs_client_status`) on a bounded backoff,
verifies the answer against the GFS key it PINNED, and on ``active`` flips
the connection and runs the normal on-connected hooks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web

from socialhome.crypto import b64url_encode, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.gfs_client_status import (
    StatusRequest,
    parse_status_request,
    sign_client_status,
    status_request_ts,
    verify_status_request,
)
from socialhome.gfs_proof import PROOF_ROUTE
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services import gfs_connection_service as svc_mod
from socialhome.services.gfs_connection_service import GfsConnectionService

HOME = generate_identity_keypair()
HOME_ID = "home-alpha"
GFS_KEY = generate_identity_keypair()


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "household.db", batch_timeout_ms=10)
    await db.startup()
    yield SqliteGfsConnectionRepo(db)
    await db.shutdown()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


class _Gfs:
    """A stub GFS answering ``/gfs/proof`` with a chosen status.

    ``signer`` signs the answer (the real GFS key by default), ``reply``
    rewrites the answer before it goes out (to forge / strip / replay),
    ``swap_question`` makes the GFS answer a question someone on the path
    swapped in, ``tamper_suite`` rewrites the question's suite on the path
    and has the GFS sign ``unknown`` for the tampered question (the
    pre-review behaviour), ``during`` runs while the question is out."""

    def __init__(self, status: str = "pending") -> None:
        self.status = status
        self.signer = GFS_KEY
        self.reply = None
        self.http_status = 200
        self.swap_question = False
        self.tamper_suite = False
        self.during = None
        self.questions: list[dict] = []

    async def handler(self, request: web.Request) -> web.Response:
        assert request.path == PROOF_ROUTE
        body = await request.json()
        self.questions.append(body)
        if self.http_status != 200:
            return web.json_response({"error": "x"}, status=self.http_status)
        if self.during is not None:
            await self.during()
        nonce = body["nonce"]
        block = body["client"]
        if self.tamper_suite:
            status = "unknown"
            request = StatusRequest(
                instance_id=block["instance_id"],
                sig=block["sig"],
                suite="ed448",
                ts=block["ts"],
            )
        else:
            status = self.status
            request = parse_status_request(block, now=time.time())
        if self.swap_question:
            request = replace(request, sig=b64url_encode(b"w" * 64))
        sig, suite = sign_client_status(self.signer.private_key, nonce, request, status)
        answer = {
            "nonce": nonce,
            "client_status": status,
            "client_status_sig": sig,
            "client_status_sig_suite": suite,
        }
        if self.reply is not None:
            answer = self.reply(answer)
        return web.json_response(answer)


@pytest.fixture
async def gfs(aiohttp_server):
    stub = _Gfs()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", stub.handler)
    server = await aiohttp_server(app)
    stub.url = str(server.make_url("")).rstrip("/")
    return stub


async def _pending(repo, gfs, *, status: str = "pending") -> GfsConnection:
    conn = GfsConnection(
        id="c1",
        gfs_instance_id="gfs-1",
        display_name="GFS",
        public_key=GFS_KEY.public_key.hex(),
        inbox_url=gfs.url,
        status=status,
        paired_at="2026-11-22T00:00:00+00:00",
    )
    await repo.save(conn)
    return conn


def _svc(repo, session) -> tuple[GfsConnectionService, list[GfsConnection]]:
    svc = GfsConnectionService(repo, http_client=session)
    svc.attach_publish_context(
        space_repo=None, own_instance_id=HOME_ID, own_signing_key=HOME.private_key
    )
    connected: list[GfsConnection] = []

    async def hook(conn: GfsConnection) -> None:
        connected.append(conn)

    svc.attach_on_connected(hook)
    return svc, connected


def test_backoff_is_one_minute_doubling_to_one_hour():
    assert svc_mod.APPROVAL_RETRY_DELAYS_S == (
        60.0,
        120.0,
        240.0,
        480.0,
        960.0,
        1920.0,
        3600.0,
    )


async def test_an_approved_connection_turns_active_and_runs_the_hooks(
    repo, session, gfs
):
    await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    gfs.status = "active"
    assert await svc.check_approval("c1") == "done"
    assert (await repo.get("c1")).status == "active"
    assert [c.status for c in connected] == ["active"]
    assert connected[0].id == "c1"


async def test_the_question_is_signed_by_the_household(repo, session, gfs):
    await _pending(repo, gfs)
    svc, _ = _svc(repo, session)
    await svc.check_approval("c1")
    (question,) = gfs.questions
    request = parse_status_request(question["client"], now=time.time())
    assert request.instance_id == HOME_ID
    # A coarse, current time: minute resolution only.
    assert request.ts == status_request_ts(request.ts)
    assert abs(request.ts - time.time()) <= 120
    # Signed for THIS GFS (the key pinned for the connection) only.
    assert verify_status_request(
        HOME.public_key.hex(), GFS_KEY.public_key.hex(), question["nonce"], request
    )


async def test_still_pending_keeps_waiting(repo, session, gfs):
    await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "pending"
    assert connected == []


@pytest.mark.parametrize("status", ["unknown", "banned"])
async def test_a_rejected_removed_or_banned_household_is_suspended(
    repo, session, gfs, status, caplog
):
    """Suspended — and still asked, so it is never a dead end."""
    await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    gfs.status = status
    with caplog.at_level(logging.WARNING):
        assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "suspended"
    assert connected == []
    assert "suspended" in caplog.text
    # Asked again: no second WARNING for the same verdict.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert await svc.check_approval("c1") == "retry"
    assert "suspended" not in caplog.text
    assert len(gfs.questions) == 2


async def test_an_unbanned_then_approved_household_recovers(repo, session, gfs):
    """The operator unbans (the GFS row goes back to pending), then accepts:
    the suspended connection follows without a new code."""
    await _pending(repo, gfs, status="suspended")
    svc, connected = _svc(repo, session)
    gfs.status = "pending"
    assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "pending"
    gfs.status = "active"
    assert await svc.check_approval("c1") == "done"
    assert (await repo.get("c1")).status == "active"
    assert [c.id for c in connected] == ["c1"]


async def test_a_swapped_question_cannot_get_a_household_suspended(
    repo, session, gfs, caplog
):
    """Someone on the path breaks the question's signature; the real GFS
    signs ``unknown`` for it. That answer is bound to the broken question,
    not the household's, so it is refused and nothing changes."""
    await _pending(repo, gfs)
    svc, _ = _svc(repo, session)
    gfs.status = "unknown"
    gfs.swap_question = True
    with caplog.at_level(logging.WARNING):
        assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "pending"
    assert "does not verify" in caplog.text


async def test_a_connection_removed_while_asking_is_not_revived(repo, session, gfs):
    await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    gfs.status = "active"

    async def remove() -> None:
        await repo.delete("c1")

    gfs.during = remove
    assert await svc.check_approval("c1") == "skip"
    assert await repo.get("c1") is None
    assert connected == []


async def test_a_forged_approval_is_refused(repo, session, gfs, caplog):
    """Signed by any key but the one pinned for this connection."""
    await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    gfs.status = "active"
    gfs.signer = generate_identity_keypair()
    with caplog.at_level(logging.WARNING):
        assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "pending"
    assert connected == []
    assert "does not verify" in caplog.text


async def test_a_relabelled_answer_is_refused(repo, session, gfs):
    """A genuine ``pending`` answer re-labelled ``active`` on the way."""
    await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    gfs.reply = lambda a: {**a, "client_status": "active"}
    assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "pending"
    assert connected == []


async def test_a_replayed_approval_is_refused(repo, session, gfs):
    """An approval signed for an earlier question (another nonce)."""
    await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    old_nonce = "A" * 43
    old_question = StatusRequest(
        instance_id=HOME_ID,
        sig=b64url_encode(b"o" * 64),
        suite="ed25519",
        ts=status_request_ts(time.time()),
    )
    old_sig, suite = sign_client_status(
        GFS_KEY.private_key, old_nonce, old_question, "active"
    )
    gfs.reply = lambda a: {
        **a,
        "client_status": "active",
        "client_status_sig": old_sig,
        "client_status_sig_suite": suite,
    }
    assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "pending"
    assert connected == []


@pytest.mark.parametrize(
    "reply",
    [
        # An older GFS: a plain possession proof, no status at all.
        lambda a: {"nonce": a["nonce"]},
        # Unsigned.
        lambda a: {**a, "client_status_sig": ""},
        lambda a: {k: v for k, v in a.items() if k != "client_status_sig_suite"},
        # Unknown suite — never a default fallback.
        lambda a: {**a, "client_status_sig_suite": "ed448"},
        # Not a status.
        lambda a: {**a, "client_status": 7},
        lambda a: ["not", "an", "object"],
    ],
)
async def test_an_unsigned_or_malformed_answer_keeps_waiting(repo, session, gfs, reply):
    await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    gfs.status = "active"
    gfs.reply = reply
    assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "pending"
    assert connected == []


@pytest.mark.parametrize("http_status", [400, 404, 429, 503])
async def test_an_unavailable_gfs_keeps_waiting(repo, session, gfs, http_status):
    await _pending(repo, gfs)
    svc, _ = _svc(repo, session)
    gfs.http_status = http_status
    assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "pending"


async def test_an_unreachable_gfs_keeps_waiting(repo, session, gfs):
    conn = await _pending(repo, gfs)
    await repo.save(replace(conn, inbox_url="http://127.0.0.1:9"))
    svc, _ = _svc(repo, session)
    assert await svc.check_approval("c1") == "retry"


async def test_nothing_is_asked_for_an_active_connection(repo, session, gfs):
    await _pending(repo, gfs, status="active")
    svc, connected = _svc(repo, session)
    assert await svc.check_approval("c1") == "skip"
    assert await svc.check_approval("gone") == "skip"
    assert gfs.questions == []
    assert connected == []


async def test_without_an_own_identity_it_waits(repo, session, gfs):
    await _pending(repo, gfs)
    svc = GfsConnectionService(repo, http_client=session)
    assert await svc.check_approval("c1") == "retry"
    assert gfs.questions == []


@pytest.mark.parametrize("status", ["pending", "suspended"])
async def test_start_asks_for_every_waiting_connection(
    repo, session, gfs, monkeypatch, status
):
    """Event-driven at app start: a restart asks again, on the backoff."""
    monkeypatch.setattr(svc_mod, "APPROVAL_RETRY_DELAYS_S", (0.01,))
    await _pending(repo, gfs, status=status)
    svc, connected = _svc(repo, session)
    gfs.status = "active"
    await svc.start()
    try:
        await svc.wait_approvals_idle()
        assert (await repo.get("c1")).status == "active"
        assert [c.id for c in connected] == ["c1"]
    finally:
        await svc.stop()


async def test_with_no_pending_connection_nothing_runs(repo, session, gfs):
    await _pending(repo, gfs, status="active")
    svc, _ = _svc(repo, session)
    await svc.start()
    try:
        assert svc.approvals_pending() == set()
        assert await svc.check_pending_approvals() == 0
        assert gfs.questions == []
    finally:
        await svc.stop()


async def test_after_the_backoff_the_hourly_pass_keeps_asking(
    repo, session, gfs, monkeypatch
):
    monkeypatch.setattr(svc_mod, "APPROVAL_RETRY_DELAYS_S", (0.01, 0.01))
    await _pending(repo, gfs)
    svc, _ = _svc(repo, session)
    await svc.start()
    try:
        await svc.wait_approvals_idle()
        # Bounded: one question per delay, then nothing until the hourly pass.
        assert len(gfs.questions) == 2
        assert svc.approvals_pending() == {"c1"}
        gfs.status = "active"
        assert await svc.check_pending_approvals() == 1
        assert len(gfs.questions) == 3
        assert svc.approvals_pending() == set()
    finally:
        await svc.stop()


async def test_disconnecting_stops_asking(repo, session, gfs, monkeypatch):
    monkeypatch.setattr(svc_mod, "APPROVAL_RETRY_DELAYS_S", (30.0,))
    await _pending(repo, gfs)
    svc, _ = _svc(repo, session)
    await svc.start()
    try:
        assert svc.approvals_pending() == {"c1"}
        await svc.disconnect("c1")
        assert svc.approvals_pending() == set()
    finally:
        await svc.stop()


# ── Security review (#1414) ───────────────────────────────────────────────


async def test_a_tampered_question_suite_never_suspends(repo, session, gfs, caplog):
    """Finding 1: someone on the path rewrites the question's ``sig_suite``;
    a GFS signs ``unknown`` for that question over the household's REAL id,
    nonce and signature. The answer binds the whole question, so it does not
    verify for the household's own one — refused, nothing changes."""
    await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    gfs.tamper_suite = True
    with caplog.at_level(logging.WARNING):
        assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "pending"
    assert connected == []
    assert "does not verify" in caplog.text


async def test_a_refused_question_is_asked_again_not_a_verdict(repo, session, gfs):
    """The GFS answers a stale or unsupported question with an unsigned 400
    (a skewed clock, say): the household asks again later, nothing changes."""
    await _pending(repo, gfs, status="suspended")
    svc, connected = _svc(repo, session)
    gfs.http_status = 400
    assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "suspended"
    assert connected == []


async def test_concurrent_checks_run_the_hooks_once(repo, session, gfs):
    """Finding 3: the hourly pass and a backoff task answering together
    flip the row once and open one push socket, not two."""
    await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    gfs.status = "active"
    both_out = asyncio.Barrier(2)

    async def wait_for_both() -> None:
        await both_out.wait()

    gfs.during = wait_for_both
    results = await asyncio.gather(svc.check_approval("c1"), svc.check_approval("c1"))
    assert sorted(results) == ["done", "skip"]
    assert (await repo.get("c1")).status == "active"
    assert [c.id for c in connected] == ["c1"]


async def test_a_disconnect_right_before_the_write_opens_no_socket(
    repo, session, gfs, monkeypatch
):
    """Finding 3: the flip is compare-and-set — a disconnect landing between
    the answer and the status write leaves no row to flip, so no hook runs
    (a blind ``UPDATE`` + hooks opened a push socket for a removed GFS)."""
    await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    gfs.status = "active"

    def removed_first(write):
        async def wrapped(self, gfs_id, *args):
            await self.delete(gfs_id)
            return await write(self, gfs_id, *args)

        return wrapped

    cls = SqliteGfsConnectionRepo
    monkeypatch.setattr(cls, "update_status", removed_first(cls.update_status))
    monkeypatch.setattr(cls, "transition_status", removed_first(cls.transition_status))
    assert await svc.check_approval("c1") == "skip"
    assert await repo.get("c1") is None
    assert connected == []


async def test_disconnect_cancels_the_approval_check_first(
    repo, session, gfs, monkeypatch
):
    """Finding 3: ``disconnect`` stops the approval check BEFORE anything
    else, so a check in flight never lands on a connection being removed —
    and no push socket opens."""
    monkeypatch.setattr(svc_mod, "APPROVAL_RETRY_DELAYS_S", (0.01,))
    await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    gfs.status = "active"
    asked = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        asked.set()
        await release.wait()

    gfs.during = hold
    seen_by_hook: list[set[str]] = []

    async def on_disconnect(_conn) -> None:
        seen_by_hook.append(svc.approvals_pending())

    svc.attach_on_disconnect(on_disconnect)
    await svc.start()
    try:
        await asyncio.wait_for(asked.wait(), timeout=5)
        await svc.disconnect("c1")
        release.set()
        await svc.wait_approvals_idle()
        assert seen_by_hook == [set()]
        assert connected == []
        assert await repo.get("c1") is None
    finally:
        release.set()
        await svc.stop()


async def test_an_already_active_connection_never_runs_the_hooks_again(
    repo, session, gfs
):
    """Finding 3: a check whose answer lands after the connection turned
    active another way (registration answered ``active``) runs no hook."""
    conn = await _pending(repo, gfs)
    svc, connected = _svc(repo, session)
    gfs.status = "active"

    async def activate() -> None:
        await repo.save(replace(conn, status="active"))

    gfs.during = activate
    assert await svc.check_approval("c1") == "skip"
    assert connected == []


def test_suspended_connections_are_asked_for_seven_days():
    assert svc_mod.SUSPENDED_ASK_FOR_S == 7 * 24 * 3600.0


async def test_a_suspended_connection_stops_asking_after_the_window(
    repo, session, gfs, monkeypatch
):
    """Finding 4: a suspended household does not ask forever — after
    :data:`SUSPENDED_ASK_FOR_S` (7 days, counted from when it became
    suspended) it stops, and the backoff forgets it."""
    monkeypatch.setattr(svc_mod, "SUSPENDED_ASK_FOR_S", 0.2)
    await _pending(repo, gfs)
    svc, _ = _svc(repo, session)
    gfs.status = "banned"
    assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "suspended"
    # Inside the window: still asked.
    assert await svc.check_approval("c1") == "retry"
    assert len(gfs.questions) == 2
    await asyncio.sleep(0.25)
    assert await svc.check_approval("c1") == "skip"
    assert len(gfs.questions) == 2
    assert (await repo.get("c1")).status == "suspended"


async def test_the_hourly_pass_drops_a_suspended_connection_past_the_window(
    repo, session, gfs, monkeypatch
):
    monkeypatch.setattr(svc_mod, "APPROVAL_RETRY_DELAYS_S", (0.01,))
    monkeypatch.setattr(svc_mod, "SUSPENDED_ASK_FOR_S", 0.0)
    await _pending(repo, gfs)
    svc, _ = _svc(repo, session)
    gfs.status = "unknown"
    await svc.start()
    try:
        await svc.wait_approvals_idle()
        assert (await repo.get("c1")).status == "suspended"
        assert svc.approvals_pending() == {"c1"}
        assert await svc.check_pending_approvals() == 1
        assert svc.approvals_pending() == set()
        assert len(gfs.questions) == 1
    finally:
        await svc.stop()


async def test_a_pending_connection_keeps_asking_past_the_window(
    repo, session, gfs, monkeypatch
):
    """The 7-day stop is for ``suspended`` only: a pending household keeps
    asking on the backoff until the operator decides."""
    monkeypatch.setattr(svc_mod, "SUSPENDED_ASK_FOR_S", 0.0)
    await _pending(repo, gfs)
    svc, _ = _svc(repo, session)
    for _ in range(3):
        assert await svc.check_approval("c1") == "retry"
    assert len(gfs.questions) == 3


async def test_an_unban_restarts_the_window(repo, session, gfs, monkeypatch):
    """Suspended → pending (unbanned) → suspended again counts afresh."""
    monkeypatch.setattr(svc_mod, "SUSPENDED_ASK_FOR_S", 0.2)
    await _pending(repo, gfs)
    svc, _ = _svc(repo, session)
    gfs.status = "banned"
    assert await svc.check_approval("c1") == "retry"
    await asyncio.sleep(0.15)
    gfs.status = "pending"
    assert await svc.check_approval("c1") == "retry"
    assert (await repo.get("c1")).status == "pending"
    gfs.status = "banned"
    assert await svc.check_approval("c1") == "retry"
    await asyncio.sleep(0.1)
    # 0.25 s since the first suspension, 0.1 s since this one: still asked.
    assert await svc.check_approval("c1") == "retry"
    assert len(gfs.questions) == 4


async def test_removing_and_reconnecting_starts_over(repo, session, gfs, monkeypatch):
    """A suspended connection past its window, removed and connected again
    (same id here, the worst case), is asked again."""
    monkeypatch.setattr(svc_mod, "SUSPENDED_ASK_FOR_S", 0.1)
    conn = await _pending(repo, gfs)
    svc, _ = _svc(repo, session)
    gfs.status = "banned"
    assert await svc.check_approval("c1") == "retry"
    await asyncio.sleep(0.15)
    assert await svc.check_approval("c1") == "skip"
    await svc.disconnect("c1")
    await repo.save(replace(conn, status="suspended"))
    assert await svc.check_approval("c1") == "retry"
    assert len(gfs.questions) == 2
