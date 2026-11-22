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

import logging
from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web

from socialhome.crypto import generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.gfs_client_status import (
    parse_status_request,
    sign_client_status,
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
    swapped in, ``during`` runs while the question is out."""

    def __init__(self, status: str = "pending") -> None:
        self.status = status
        self.signer = GFS_KEY
        self.reply = None
        self.http_status = 200
        self.swap_question = False
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
        instance_id, q_sig, _suite = parse_status_request(body["client"])
        if self.swap_question:
            q_sig = "A" + q_sig[1:] if q_sig[0] != "A" else "B" + q_sig[1:]
        sig, suite = sign_client_status(
            self.signer.private_key, instance_id, nonce, q_sig, self.status
        )
        answer = {
            "nonce": nonce,
            "client_status": self.status,
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
    instance_id, sig, suite = parse_status_request(question["client"])
    assert instance_id == HOME_ID
    # Signed for THIS GFS (the key pinned for the connection) only.
    assert verify_status_request(
        HOME.public_key.hex(),
        GFS_KEY.public_key.hex(),
        HOME_ID,
        question["nonce"],
        sig,
        suite,
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
    old_sig, suite = sign_client_status(
        GFS_KEY.private_key, HOME_ID, old_nonce, "old-question", "active"
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


@pytest.mark.parametrize("http_status", [404, 429, 503])
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
