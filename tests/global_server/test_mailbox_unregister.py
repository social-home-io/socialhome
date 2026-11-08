"""GFS mailboxes (redesign PR 2): the anonymous, proof-of-possession
unregister a household sends when it leaves a space. Real GFS app, real
SQLite, real aiohttp client + WebSockets."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import b64url_encode, sign_ed25519
from socialhome.domain.gfs_mailbox import unregister_signed_bytes
from socialhome.global_server.app_keys import (
    gfs_envelope_queue_repo_key,
    gfs_mailbox_repo_key,
    gfs_mailbox_service_key,
    gfs_mailbox_ws_registry_key,
)
from socialhome.global_server.config import EnvelopeMailboxConfig
from socialhome.global_server.envelope_relay import (
    MAILBOX_UNREGISTER_MAX_PER_MINUTE,
)
from socialhome.global_server.mailboxes import MailboxRefused
from socialhome.global_server.server import create_gfs_app
from tests.global_server.test_mailboxes import (  # noqa: F401 — fixture
    GFS_ID,
    SEALED,
    Box,
    _cfg,
    _claim,
    _media,
    _register,
    _settle,
    gfs,
)

ROUTE = "/gfs/mailbox/unregister"


def _unregister_body(
    box: Box,
    *,
    at: int | None = None,
    nonce: str | None = None,
    gfs_id: str = GFS_ID,
    seed: bytes | None = None,
    **override,
) -> dict:
    ts = int(time.time()) if at is None else at
    nonce = nonce or b64url_encode(secrets.token_bytes(16))
    body = {
        "sig_suite": "ed25519",
        "mailbox_id": box.id,
        "gfs_instance_id": gfs_id,
        "ts": ts,
        "nonce": nonce,
        "sig": b64url_encode(
            sign_ed25519(
                seed if seed is not None else box.seed,
                unregister_signed_bytes(gfs_id, box.id, ts, nonce),
            )
        ),
    }
    body.update(override)
    return body


async def _queue_both(gfs, box: Box) -> None:  # noqa: F811 — the fixture name
    """One JSON envelope and one media frame waiting for *box*."""
    await gfs.post("/gfs/envelope", json={"to_instance": box.id, "sealed": SEALED})
    await gfs.post("/gfs/envelope", data=_media(box.id))
    await _settle(gfs._app)
    queue = gfs._app[gfs_envelope_queue_repo_key]
    assert await queue.count_for(box.id) == 1
    assert await queue.media_bytes_for(box.id, now=0) > 0


# ── Route ────────────────────────────────────────────────────────────────


async def test_a_verified_unregister_drops_the_mailbox_and_its_queues(gfs):  # noqa: F811
    box, other = Box(), Box()
    assert (await _register(gfs, box)).status == 200
    assert (await _register(gfs, other)).status == 200
    await _queue_both(gfs, box)
    await _queue_both(gfs, other)

    resp = await gfs.post(ROUTE, json=_unregister_body(box))
    assert resp.status == 200
    assert await resp.json() == {"status": "unregistered"}

    repo = gfs._app[gfs_mailbox_repo_key]
    queue = gfs._app[gfs_envelope_queue_repo_key]
    assert await repo.get(box.id) is None
    assert await queue.count_for(box.id) == 0
    assert await queue.media_bytes_for(box.id, now=0) == 0
    # Another mailbox is untouched.
    assert await repo.get(other.id) is not None
    assert await queue.count_for(other.id) == 1
    assert await queue.media_bytes_for(other.id, now=0) > 0


@pytest.mark.security
async def test_unknown_mailbox_and_bad_signature_answer_like_success(gfs):  # noqa: F811
    """No oracle: an unknown mailbox, a wrong key and a real unregister all
    get the same 200 — and only the real one deletes anything."""
    box = Box()
    await _register(gfs, box)
    await _queue_both(gfs, box)

    answers = []
    unknown = await gfs.post(ROUTE, json=_unregister_body(Box()))
    answers.append((unknown.status, await unknown.read()))
    forged = await gfs.post(
        ROUTE, json=_unregister_body(box, seed=secrets.token_bytes(32))
    )
    answers.append((forged.status, await forged.read()))
    # The forged one changed nothing.
    assert await gfs._app[gfs_mailbox_repo_key].get(box.id) is not None
    assert await gfs._app[gfs_envelope_queue_repo_key].count_for(box.id) == 1
    real = await gfs.post(ROUTE, json=_unregister_body(box))
    answers.append((real.status, await real.read()))
    assert len(set(answers)) == 1
    assert answers[0][0] == 200
    assert await gfs._app[gfs_mailbox_repo_key].get(box.id) is None


@pytest.mark.security
async def test_unregister_outside_the_ts_window_is_refused(gfs):  # noqa: F811
    box = Box()
    await _register(gfs, box)
    resp = await gfs.post(ROUTE, json=_unregister_body(box, at=int(time.time()) - 301))
    assert resp.status == 400
    assert (await resp.json())["error"] == "stale_ts"
    assert await gfs._app[gfs_mailbox_repo_key].get(box.id) is not None


@pytest.mark.security
async def test_unregister_made_for_another_gfs_is_refused(gfs):  # noqa: F811
    box = Box()
    await _register(gfs, box)
    resp = await gfs.post(ROUTE, json=_unregister_body(box, gfs_id="gfs-elsewhere"))
    assert resp.status == 403
    assert (await resp.json())["error"] == "wrong_gfs"
    assert await gfs._app[gfs_mailbox_repo_key].get(box.id) is not None


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"sig_suite": "ed25519+mldsa65"}, "unsupported_sig_suite"),
        ({"mailbox_id": "not an id"}, "invalid_body"),
        ({"nonce": "short"}, "invalid_nonce"),
        ({"ts": "soon"}, "invalid_ts"),
    ],
)
async def test_a_malformed_unregister_is_refused(gfs, override, error):  # noqa: F811
    box = Box()
    await _register(gfs, box)
    resp = await gfs.post(ROUTE, json=_unregister_body(box, **override))
    assert resp.status == 400
    assert (await resp.json())["error"] == error
    assert await gfs._app[gfs_mailbox_repo_key].get(box.id) is not None


async def test_invalid_json_is_400(gfs):  # noqa: F811
    resp = await gfs.post(ROUTE, data=b"{not json")
    assert resp.status == 400
    assert await resp.json() == {"error": "invalid_json"}


@pytest.mark.security
async def test_a_replayed_unregister_is_an_idempotent_200(gfs):  # noqa: F811
    """The same signed unregister twice: the second finds nothing to do,
    and even after the mailbox is registered again a replay of the old
    request cannot drop it (the nonce is spent)."""
    box = Box()
    await _register(gfs, box)
    body = _unregister_body(box)
    assert (await gfs.post(ROUTE, json=body)).status == 200
    assert await gfs._app[gfs_mailbox_repo_key].get(box.id) is None
    assert (await _register(gfs, box)).status == 200
    replay = await gfs.post(ROUTE, json=body)
    assert replay.status == 200
    assert await replay.json() == {"status": "unregistered"}
    assert await gfs._app[gfs_mailbox_repo_key].get(box.id) is not None


async def test_unregister_closes_the_live_claim_socket(gfs):  # noqa: F811
    box = Box()
    await _register(gfs, box)
    ws = await _claim(gfs, box)
    assert (await ws.receive_json(timeout=2))["type"] == "mailbox_claimed"
    registry = gfs._app[gfs_mailbox_ws_registry_key]
    assert registry.is_connected(box.id)

    assert (await gfs.post(ROUTE, json=_unregister_body(box))).status == 200
    closed = await ws.receive(timeout=2)
    assert closed.type == WSMsgType.CLOSE
    for _ in range(100):
        if not registry.is_connected(box.id):
            break
        await asyncio.sleep(0.01)
    assert not registry.is_connected(box.id)


async def test_a_forged_unregister_leaves_the_claim_socket_open(gfs):  # noqa: F811
    box = Box()
    await _register(gfs, box)
    ws = await _claim(gfs, box)
    assert (await ws.receive_json(timeout=2))["type"] == "mailbox_claimed"
    forged = _unregister_body(box, seed=secrets.token_bytes(32))
    assert (await gfs.post(ROUTE, json=forged)).status == 200
    await asyncio.sleep(0.05)
    assert gfs._app[gfs_mailbox_ws_registry_key].is_connected(box.id)
    await gfs.post("/gfs/envelope", json={"to_instance": box.id, "sealed": SEALED})
    assert await ws.receive_json(timeout=2) == {"type": "envelope", "sealed": SEALED}
    await ws.close()


@pytest.mark.security
async def test_no_log_record_names_the_mailbox(gfs, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    box = Box()
    await _register(gfs, box)
    await _queue_both(gfs, box)
    ws = await _claim(gfs, box)
    await ws.receive_json(timeout=2)
    await gfs.post(ROUTE, json=_unregister_body(box, seed=secrets.token_bytes(32)))
    await gfs.post(ROUTE, json=_unregister_body(box, at=0))
    await gfs.post(ROUTE, json=_unregister_body(box))
    await gfs.post(ROUTE, json=_unregister_body(box))
    await ws.receive(timeout=2)
    await asyncio.sleep(0.05)
    assert caplog.records
    for record in caplog.records:
        assert box.id not in record.getMessage(), record.getMessage()


async def test_mailboxes_off_refuses_unregister(tmp_dir):
    app = create_gfs_app(
        _cfg(tmp_dir, envelope_mailbox=EnvelopeMailboxConfig(enabled=False))
    )
    async with TestClient(TestServer(app)) as tc:
        assert (await tc.post(ROUTE, json=_unregister_body(Box()))).status == 404


async def test_rate_limit_on_unregister(gfs):  # noqa: F811
    statuses = []
    for _ in range(MAILBOX_UNREGISTER_MAX_PER_MINUTE + 10):
        statuses.append((await gfs.post(ROUTE, json={})).status)
    assert 429 in statuses


# ── Service (unit) ──────────────────────────────────────────────────────


async def test_service_unregister_reports_whether_it_deleted(gfs):  # noqa: F811
    svc = gfs._app[gfs_mailbox_service_key]
    box = Box()
    await _register(gfs, box)
    assert await svc.unregister(_unregister_body(Box())) is False
    assert (
        await svc.unregister(_unregister_body(box, seed=secrets.token_bytes(32)))
        is False
    )
    body = _unregister_body(box)
    assert await svc.unregister(body) is True
    assert await svc.unregister(body) is False  # replay: nothing to do
    with pytest.raises(MailboxRefused) as err:
        await svc.unregister(["not", "a", "dict"])
    assert (err.value.status, err.value.error) == (400, "invalid_body")
