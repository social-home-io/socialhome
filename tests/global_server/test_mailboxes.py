"""GFS mailboxes (redesign PR 1): anonymous registration with proof of
possession, the anonymous claim socket, and envelope / media routing to a
mailbox id. Real GFS app, real SQLite, real aiohttp client + WebSockets."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from socialhome.capabilities_sig import verify_capabilities
from socialhome.crypto import b64url_encode, ed25519_public_key, sign_ed25519
from socialhome.domain.gfs_mailbox import (
    claim_signed_bytes,
    derive_mailbox_id,
    register_signed_bytes,
)
from socialhome.domain.relay_media_frame import (
    VALID_SEALED_LENGTHS,
    build_relay_media_body,
)
from socialhome.global_server.app_keys import (
    gfs_db_key,
    gfs_envelope_queue_repo_key,
    gfs_envelope_relay_key,
    gfs_fed_repo_key,
    gfs_mailbox_repo_key,
    gfs_mailbox_service_key,
    gfs_mailbox_ws_registry_key,
)
from socialhome.global_server.config import EnvelopeMailboxConfig, GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.mailboxes import GfsMailboxService
from socialhome.global_server.server import create_gfs_app

GFS_ID = "gfs-node-a"
INSTANCE = "recipient2home2222222222222222aa"
SEALED = {"kem_suite": "x25519", "eph_pk": "ZXBo", "ciphertext": "Y2lwaGVy"}


def _cfg(tmp_dir, **kw) -> GfsConfig:
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id=GFS_ID,
        cluster_enabled=False,
        cluster_node_id=GFS_ID,
        cluster_peers=(),
        **kw,
    )


@pytest.fixture
async def gfs(tmp_dir):
    app = create_gfs_app(_cfg(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        await app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id=INSTANCE,
                display_name="Recipient",
                public_key=ed25519_public_key(b"\x07" * 32).hex(),
                status="active",
            )
        )
        tc._app = app
        yield tc


class Box:
    """A mailbox key the test holds."""

    def __init__(self, seed: bytes | None = None) -> None:
        self.seed = seed if seed is not None else secrets.token_bytes(32)
        self.pk = ed25519_public_key(self.seed)
        self.id = derive_mailbox_id(self.pk)

    def register_body(
        self,
        *,
        at: int | None = None,
        nonce: str | None = None,
        gfs_id: str = GFS_ID,
        **override,
    ) -> dict:
        ts = int(time.time()) if at is None else at
        nonce = nonce or b64url_encode(secrets.token_bytes(16))
        body = {
            "sig_suite": "ed25519",
            "sign_pk": b64url_encode(self.pk),
            "mailbox_id": self.id,
            "ts": ts,
            "nonce": nonce,
            "sig": b64url_encode(
                sign_ed25519(
                    self.seed, register_signed_bytes(gfs_id, self.id, ts, nonce)
                )
            ),
        }
        body.update(override)
        return body

    def claim(self, challenge: str, *, accepts=("envelope_media",), **override) -> dict:
        body = {
            "type": "mailbox_claim",
            "sig_suite": "ed25519",
            "mailbox_id": self.id,
            "sig": b64url_encode(
                sign_ed25519(self.seed, claim_signed_bytes(GFS_ID, self.id, challenge))
            ),
            "accepts": list(accepts),
        }
        body.update(override)
        return body


async def _register(gfs, box: Box, **kw):
    return await gfs.post("/gfs/mailbox/register", json=box.register_body(**kw))


async def _claim(gfs, box: Box, **kw):
    ws = await gfs.ws_connect("/gfs/mailbox/ws")
    hello = await ws.receive_json(timeout=2)
    assert hello["type"] == "mailbox_challenge"
    assert hello["gfs_instance_id"] == GFS_ID
    await ws.send_json(box.claim(hello["challenge"], **kw))
    return ws


async def _settle(app) -> None:
    relay = app[gfs_envelope_relay_key]
    for _ in range(500):
        if relay.in_flight == 0 and not relay._media_inflight:
            await relay.settle_media()
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background envelope work never settled")


def _media(to: str, fill: bytes = b"\xab") -> bytes:
    size = min(VALID_SEALED_LENGTHS)
    return build_relay_media_body(to, (fill * size)[:size])


# ── Registration ─────────────────────────────────────────────────────────


async def test_register_pins_the_key_and_is_idempotent(gfs):
    box = Box()
    first = await _register(gfs, box)
    assert first.status == 200
    assert await first.json() == {"status": "registered", "mailbox_id": box.id}
    again = await _register(gfs, box)
    assert again.status == 200
    stored = await gfs._app[gfs_mailbox_repo_key].get(box.id)
    assert stored is not None and stored.sign_pk == box.pk


@pytest.mark.parametrize(
    "override",
    [
        {"mailbox_id": "b" * 32},  # not the hash of the key
        {"sig": b64url_encode(b"\x00" * 64)},  # not the key's signature
        {"sign_pk": b64url_encode(b"\x00" * 31)},  # not a key
        {"sign_pk": b64url_encode(b"\x01" + b"\x00" * 31)},  # small-order key
        {"nonce": "short"},
        {"ts": "soon"},
        {"sig_suite": None},
    ],
)
async def test_register_refuses_a_bad_proof(gfs, override):
    box = Box()
    resp = await _register(gfs, box, **override)
    assert resp.status in (400, 403)
    assert await gfs._app[gfs_mailbox_repo_key].get(box.id) is None


@pytest.mark.security
async def test_register_refuses_an_unknown_sig_suite(gfs):
    box = Box()
    resp = await _register(gfs, box, sig_suite="ed25519+mldsa65")
    assert resp.status == 400
    assert (await resp.json())["error"] == "unsupported_sig_suite"


@pytest.mark.security
async def test_register_replay_is_refused(gfs):
    """The same signed registration twice: the nonce is spent."""
    box = Box()
    body = box.register_body()
    assert (await gfs.post("/gfs/mailbox/register", json=body)).status == 200
    replay = await gfs.post("/gfs/mailbox/register", json=body)
    assert replay.status == 409
    assert (await replay.json())["error"] == "replay"


@pytest.mark.security
async def test_register_outside_the_ts_window_is_refused(gfs):
    box = Box()
    resp = await _register(gfs, box, at=int(time.time()) - 301)
    assert resp.status == 400
    assert await gfs._app[gfs_mailbox_repo_key].get(box.id) is None


@pytest.mark.security
async def test_a_proof_made_for_another_gfs_is_refused(gfs):
    box = Box()
    resp = await _register(gfs, box, gfs_id="gfs-elsewhere")
    assert resp.status == 403


@pytest.mark.security
async def test_an_id_equal_to_a_registered_instance_is_refused(gfs, monkeypatch):
    """A mailbox can never shadow a household: the id is refused when an
    instance holds it (forced here — a real collision needs a preimage)."""
    box = Box()
    await gfs._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id=box.id,
            display_name="Household",
            public_key=ed25519_public_key(b"\x08" * 32).hex(),
            status="active",
        )
    )
    resp = await _register(gfs, box)
    assert resp.status == 409
    assert (await resp.json())["error"] == "id_in_use"
    assert await gfs._app[gfs_mailbox_repo_key].get(box.id) is None


async def test_per_ip_mailbox_cap(tmp_dir):
    app = create_gfs_app(
        _cfg(tmp_dir, envelope_mailbox=EnvelopeMailboxConfig(mailbox_max_per_ip=2))
    )
    async with TestClient(TestServer(app)) as tc:
        boxes = [Box() for _ in range(3)]
        assert (await _register(tc, boxes[0])).status == 200
        assert (await _register(tc, boxes[1])).status == 200
        # Re-registering an existing mailbox costs nothing.
        assert (await _register(tc, boxes[0])).status == 200
        capped = await _register(tc, boxes[2])
        assert capped.status == 429
        assert await app[gfs_mailbox_repo_key].get(boxes[2].id) is None


async def test_mailboxes_off_refuses_both_routes_and_drops_the_capability(tmp_dir):
    app = create_gfs_app(
        _cfg(tmp_dir, envelope_mailbox=EnvelopeMailboxConfig(enabled=False))
    )
    async with TestClient(TestServer(app)) as tc:
        assert (await _register(tc, Box())).status == 404
        assert (await tc.get("/gfs/mailbox/ws")).status == 404
        info = await (await tc.get("/gfs/info")).json()
        assert "envelope_mailbox" not in info["capabilities"]


async def test_gfs_info_signs_envelope_mailbox(gfs):
    body = await (await gfs.get("/gfs/info")).json()
    cap = body["capabilities"]["envelope_mailbox"]
    assert cap == {"sig_suites": ["ed25519"], "idle_days": 30}
    assert verify_capabilities(
        body["public_key"],
        body["gfs_instance_id"],
        body["capabilities"],
        body["capabilities_sig"],
        body["capabilities_sig_suite"],
    )


# ── Claim + delivery ─────────────────────────────────────────────────────


async def test_queued_json_and_media_drain_on_claim_byte_identical(gfs):
    box = Box()
    assert (await _register(gfs, box)).status == 200
    media = _media(box.id)
    resp = await gfs.post(
        "/gfs/envelope", json={"to_instance": box.id, "sealed": SEALED}
    )
    assert resp.status == 202
    resp = await gfs.post("/gfs/envelope", data=media)
    assert resp.status == 202
    await _settle(gfs._app)
    queue = gfs._app[gfs_envelope_queue_repo_key]
    assert await queue.count_for(box.id) == 1

    ws = await _claim(gfs, box)
    claimed = await ws.receive_json(timeout=2)
    assert claimed == {"type": "mailbox_claimed"}
    first = await ws.receive(timeout=2)
    assert first.type == WSMsgType.TEXT
    assert json.loads(first.data) == {"type": "envelope", "sealed": SEALED}
    second = await ws.receive(timeout=5)
    assert second.type == WSMsgType.BINARY
    assert second.data == media
    stored = await gfs._app[gfs_mailbox_repo_key].get(box.id)
    assert stored is not None and stored.last_claim_at is not None
    await ws.close()


async def test_live_json_and_media_reach_a_claimed_socket(gfs):
    box = Box()
    await _register(gfs, box)
    ws = await _claim(gfs, box)
    assert (await ws.receive_json(timeout=2))["type"] == "mailbox_claimed"
    await gfs.post("/gfs/envelope", json={"to_instance": box.id, "sealed": SEALED})
    got = await ws.receive_json(timeout=2)
    assert got == {"type": "envelope", "sealed": SEALED}
    media = _media(box.id, b"\xcd")
    await gfs.post("/gfs/envelope", data=media)
    got = await ws.receive(timeout=5)
    assert got.type == WSMsgType.BINARY and got.data == media
    await ws.close()


@pytest.mark.security
async def test_a_mailbox_cannot_be_claimed_without_its_key(gfs):
    """A wrong key and an unknown mailbox close the same way."""
    box = Box()
    await _register(gfs, box)
    await gfs.post("/gfs/envelope", json={"to_instance": box.id, "sealed": SEALED})
    await _settle(gfs._app)

    thief = Box()
    ws = await gfs.ws_connect("/gfs/mailbox/ws")
    hello = await ws.receive_json(timeout=2)
    forged = thief.claim(hello["challenge"], mailbox_id=box.id)
    await ws.send_json(forged)
    closed = await ws.receive(timeout=2)
    assert closed.type == WSMsgType.CLOSE
    wrong_key = (ws.close_code, closed.extra)

    ws = await gfs.ws_connect("/gfs/mailbox/ws")
    hello = await ws.receive_json(timeout=2)
    await ws.send_json(Box().claim(hello["challenge"]))  # never registered
    closed = await ws.receive(timeout=2)
    unknown = (ws.close_code, closed.extra)
    assert wrong_key == unknown == (4401, "auth-failed")
    # The queued envelope is still there for the owner.
    assert await gfs._app[gfs_envelope_queue_repo_key].count_for(box.id) == 1


@pytest.mark.security
async def test_a_claim_signed_over_another_challenge_is_refused(gfs):
    box = Box()
    await _register(gfs, box)
    ws = await gfs.ws_connect("/gfs/mailbox/ws")
    await ws.receive_json(timeout=2)
    await ws.send_json(box.claim(b64url_encode(secrets.token_bytes(32))))
    closed = await ws.receive(timeout=2)
    assert closed.type == WSMsgType.CLOSE and ws.close_code == 4401


async def test_a_claim_with_an_unknown_sig_suite_is_refused(gfs):
    box = Box()
    await _register(gfs, box)
    ws = await gfs.ws_connect("/gfs/mailbox/ws")
    hello = await ws.receive_json(timeout=2)
    await ws.send_json(box.claim(hello["challenge"], sig_suite="rsa"))
    closed = await ws.receive(timeout=2)
    assert closed.type == WSMsgType.CLOSE and ws.close_code == 4401
    assert closed.extra == "unsupported-suite"


async def test_one_mailbox_per_socket(gfs):
    """A second claim on a claimed socket is ignored: the socket keeps
    serving only its first mailbox."""
    one, two = Box(), Box()
    await _register(gfs, one)
    await _register(gfs, two)
    ws = await _claim(gfs, one)
    hello_two = await ws.receive_json(timeout=2)
    assert hello_two == {"type": "mailbox_claimed"}
    await ws.send_json(two.claim("x" * 43))
    await asyncio.sleep(0.05)
    registry = gfs._app[gfs_mailbox_ws_registry_key]
    assert registry.is_connected(one.id)
    assert not registry.is_connected(two.id)
    await ws.close()


async def test_a_newer_claim_replaces_the_older_socket(gfs):
    box = Box()
    await _register(gfs, box)
    old = await _claim(gfs, box)
    await old.receive_json(timeout=2)
    new = await _claim(gfs, box)
    await new.receive_json(timeout=2)
    closed = await old.receive(timeout=2)
    assert closed.type == WSMsgType.CLOSE
    await new.close()


async def test_claim_timeout_closes_4408(gfs, monkeypatch):
    monkeypatch.setattr(
        "socialhome.global_server.routes.mailbox.CLAIM_TIMEOUT_SECONDS", 0.1
    )
    ws = await gfs.ws_connect("/gfs/mailbox/ws")
    await ws.receive_json(timeout=2)
    closed = await ws.receive(timeout=2)
    assert closed.type == WSMsgType.CLOSE and ws.close_code == 4408


@pytest.mark.parametrize("frame", ["not json", "[1]", '{"type":"hello"}'])
async def test_a_malformed_claim_is_a_protocol_close(gfs, frame):
    ws = await gfs.ws_connect("/gfs/mailbox/ws")
    await ws.receive_json(timeout=2)
    await ws.send_str(frame)
    closed = await ws.receive(timeout=2)
    assert closed.type == WSMsgType.CLOSE and ws.close_code in (4400, 4401)


# ── Uniform answers + privacy ────────────────────────────────────────────


@pytest.mark.security
async def test_envelope_to_unknown_and_known_mailbox_is_the_same_202(gfs, monkeypatch):
    """Status, body and timing: the answer goes out before the lookup."""
    box = Box()
    await _register(gfs, box)
    unknown = Box()
    bodies = []
    for target in (box.id, unknown.id, INSTANCE):
        resp = await gfs.post(
            "/gfs/envelope", json={"to_instance": target, "sealed": SEALED}
        )
        bodies.append((resp.status, await resp.read()))
    for target in (box.id, unknown.id):
        resp = await gfs.post("/gfs/envelope", data=_media(target))
        bodies.append((resp.status, await resp.read()))
    assert len(set(bodies)) == 1
    assert bodies[0][0] == 202
    await _settle(gfs._app)
    queue = gfs._app[gfs_envelope_queue_repo_key]
    assert await queue.count_for(box.id) == 1
    assert await queue.count_for(unknown.id) == 0
    assert await queue.media_bytes_for(unknown.id, now=0) == 0

    # Timing: the route never awaits the recipient lookup.
    release = asyncio.Event()

    async def _slow_is_mailbox(_self, _mid):
        await release.wait()
        return True

    monkeypatch.setattr(
        type(gfs._app[gfs_mailbox_repo_key]), "is_mailbox", _slow_is_mailbox
    )
    resp = await asyncio.wait_for(
        gfs.post("/gfs/envelope", json={"to_instance": unknown.id, "sealed": SEALED}),
        timeout=2,
    )
    assert resp.status == 202
    release.set()
    await _settle(gfs._app)


@pytest.mark.security
async def test_nothing_in_the_db_or_logs_ties_a_mailbox_to_an_ip_or_instance(
    gfs, caplog
):
    caplog.set_level(logging.DEBUG)
    box = Box()
    await _register(gfs, box)
    await gfs.post("/gfs/envelope", json={"to_instance": box.id, "sealed": SEALED})
    await gfs.post("/gfs/envelope", data=_media(box.id))
    await _settle(gfs._app)
    ws = await _claim(gfs, box)
    await ws.receive_json(timeout=2)
    await ws.receive(timeout=2)
    await ws.receive(timeout=5)
    await gfs.post("/gfs/envelope", json={"to_instance": box.id, "sealed": SEALED})
    await ws.receive(timeout=2)
    await ws.close()
    await asyncio.sleep(0.05)

    # Logs: the mailbox id never appears in any record, so no record can
    # sit next to (or be correlated by content with) an address.
    for record in caplog.records:
        assert box.id not in record.getMessage(), record.getMessage()

    # DB: the mailbox id appears only in the mailbox table and the queues,
    # and no row naming it holds an address or an instance id.
    db = gfs._app[gfs_db_key]
    tables = [
        r["name"]
        for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    allowed = {"gfs_mailboxes", "gfs_envelope_queue", "gfs_media_queue"}
    for table in tables:
        rows = await db.fetchall(f"SELECT * FROM {table}")
        for row in rows:
            text = " ".join(
                v.decode("latin-1") if isinstance(v, bytes) else str(v)
                for v in tuple(row)
            )
            if box.id in text:
                assert table in allowed, table
                assert "127.0.0.1" not in text
                assert INSTANCE not in text
    cols = [r[1] for r in await db.fetchall("PRAGMA table_info(gfs_mailboxes)")]
    assert cols == ["mailbox_id", "sign_pk", "created_at", "last_claim_at"]


# ── Idle sweep ──────────────────────────────────────────────────────────


async def test_idle_mailboxes_are_swept_with_their_queue(gfs):
    box = Box()
    await _register(gfs, box)
    await gfs.post("/gfs/envelope", json={"to_instance": box.id, "sealed": SEALED})
    await _settle(gfs._app)
    svc: GfsMailboxService = gfs._app[gfs_mailbox_service_key]
    assert await svc.sweep(now=int(time.time())) == 0
    assert await svc.sweep(now=int(time.time()) + 31 * 86400) == 1
    assert await gfs._app[gfs_mailbox_repo_key].get(box.id) is None
    assert await gfs._app[gfs_envelope_queue_repo_key].count_for(box.id) == 0


async def test_rate_limit_on_register(gfs, monkeypatch):
    statuses = []
    for _ in range(40):
        resp = await gfs.post("/gfs/mailbox/register", json={})
        statuses.append(resp.status)
    assert 429 in statuses


# ── In-memory limits (unit) ─────────────────────────────────────────────


def _svc(**cfg) -> GfsMailboxService:
    return GfsMailboxService(
        repo=None,  # type: ignore[arg-type]
        fed_repo=None,  # type: ignore[arg-type]
        gfs_instance_id=GFS_ID,
        config=EnvelopeMailboxConfig(**cfg),
    )


def test_the_per_address_cap_resets_with_the_next_window():
    svc = _svc(mailbox_max_per_ip=2)
    day = 86_400.0
    assert svc._count_new("ip", 2, 10.0)
    assert svc._count_new("ip", 2, 20.0)
    assert not svc._count_new("ip", 2, 30.0)
    assert svc._count_new("other", 2, 30.0)
    assert svc._count_new("ip", 2, day + 1)


def test_the_per_address_cap_holds_a_bounded_number_of_buckets(monkeypatch):
    monkeypatch.setattr("socialhome.global_server.mailboxes.MAILBOX_CAP_MAX_KEYS", 3)
    svc = _svc()
    for n in range(10):
        svc._count_new(f"ip-{n}", 5, 1.0)
    assert len(svc._new_per_bucket) == 3
    assert list(svc._new_per_bucket) == ["ip-7", "ip-8", "ip-9"]


def test_spent_nonces_expire_and_are_bounded(monkeypatch):
    svc = _svc()
    assert svc._spend_nonce("a", 0.0)
    assert not svc._spend_nonce("a", 1.0)
    # Past the TTL the old entry is dropped (its ts would fail the window).
    assert svc._spend_nonce("b", 10_000.0)
    assert "a" not in svc._spent_nonces
    monkeypatch.setattr("socialhome.global_server.mailboxes.MAILBOX_NONCE_MAX", 2)
    for n in range(5):
        svc._spend_nonce(f"n{n}", 10_001.0)
    assert len(svc._spent_nonces) <= 2
