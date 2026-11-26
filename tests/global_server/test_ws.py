"""Integration tests for ``GET /gfs/ws`` (spec §24.12).

End-to-end: spin up the real GFS aiohttp app, register a peer with a
known Ed25519 keypair, open a WebSocket from a real client, and assert
the connection lifecycle, hello-frame validation, and push delivery via
``GfsFederationService._fan_out``.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from socialhome import crypto as crypto_mod
from socialhome.crypto import b64url_encode, sign_ed25519
from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
    gfs_federation_key,
    gfs_ws_registry_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.routes import ws as ws_routes
from socialhome.global_server.server import create_gfs_app

# ── Helpers ────────────────────────────────────────────────────────────────────


def _gen_ed25519() -> tuple[bytes, str]:
    """Return ``(seed_bytes, public_key_hex)`` for a fresh keypair."""
    priv = ed25519.Ed25519PrivateKey.generate()
    seed = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_hex = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    return seed, pub_hex


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()


def _hello(instance_id: str, seed: bytes, *, ts: int | None = None) -> dict:
    if ts is None:
        ts = int(time.time())
    msg = f"{instance_id}|{ts}".encode()
    return {
        "type": "hello",
        "instance_id": instance_id,
        "ts": ts,
        "sig": b64url_encode(sign_ed25519(seed, msg)),
    }


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
async def ws_client(tmp_dir):
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-node-a",
        cluster_enabled=False,
        cluster_node_id="gfs-node-a",
        cluster_peers=(),
    )
    app = create_gfs_app(cfg)
    seed, pub_hex = _gen_ed25519()
    async with TestClient(TestServer(app)) as tc:
        await app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id="peer.home",
                display_name="Peer",
                public_key=pub_hex,
                status="active",
            )
        )
        tc._seed = seed
        tc._app = app
        yield tc


# ── Tests ──────────────────────────────────────────────────────────────────────


async def test_ws_happy_path_registers_and_marks_transport(ws_client):
    async with ws_client.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_hello("peer.home", ws_client._seed))

        # Wait for the in-memory registry AND the queued DB write
        # (``AsyncDatabase.enqueue`` batches; the row appears once the
        # batch flushes — 10 ms in tests).
        registry = ws_client._app[gfs_ws_registry_key]
        fed_repo = ws_client._app[gfs_fed_repo_key]
        for _ in range(100):
            row = await fed_repo.get_rtc_connection("peer.home")
            if registry.is_connected("peer.home") and row is not None:
                break
            await asyncio.sleep(0.02)

        assert registry.is_connected("peer.home")
        assert row is not None
        assert row.transport == "websocket"


async def test_ws_rejects_bad_signature(ws_client):
    async with ws_client.ws_connect("/gfs/ws") as ws:
        bad = _hello("peer.home", ws_client._seed)
        bad["sig"] = b64url_encode(b"\x00" * 64)
        await ws.send_json(bad)

        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
        assert msg.type.name == "CLOSE"
        assert ws.close_code == 4401
        assert msg.extra == "auth-failed"


@pytest.mark.security
async def test_ws_rejects_an_undecodable_signature_as_auth_failed(ws_client):
    async with ws_client.ws_connect("/gfs/ws") as ws:
        bad = _hello("peer.home", ws_client._seed)
        bad["sig"] = "!!not-base64!!"
        await ws.send_json(bad)

        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
        assert ws.close_code == 4401
        assert msg.extra == "auth-failed"


async def test_ws_rejects_skewed_timestamp(ws_client):
    async with ws_client.ws_connect("/gfs/ws") as ws:
        # 10 minutes in the past — outside the ±300 s window.
        await ws.send_json(
            _hello("peer.home", ws_client._seed, ts=int(time.time()) - 600),
        )
        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
        assert msg.type.name == "CLOSE"
        assert ws.close_code == 4401


async def test_ws_rejects_unknown_instance(ws_client):
    """Hello from an unregistered ``instance_id`` is closed 4401."""
    seed, _pub_hex = _gen_ed25519()
    async with ws_client.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_hello("ghost.home", seed))
        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
        assert msg.type.name == "CLOSE"
        assert ws.close_code == 4401
        assert msg.extra == "auth-failed"


@pytest.mark.security
async def test_ws_unknown_and_bad_signature_are_indistinguishable(
    ws_client, monkeypatch
):
    """An unregistered id must not be told apart from a registered one with
    a wrong signature — not by close reason, and not by skipping the Ed25519
    verify (timing): both branches run exactly one verification."""
    verifies: list[bytes] = []
    real_verify = crypto_mod.verify_ed25519

    def _counting(pub, message, sig):
        verifies.append(message)
        return real_verify(pub, message, sig)

    monkeypatch.setattr(crypto_mod, "verify_ed25519", _counting)
    stranger_seed, _pub = _gen_ed25519()
    reasons = []
    for instance_id, seed in (
        ("ghost.home", stranger_seed),
        ("peer.home", stranger_seed),  # registered, wrong key
    ):
        async with ws_client.ws_connect("/gfs/ws") as ws:
            await ws.send_json(_hello(instance_id, seed))
            msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
            reasons.append((ws.close_code, msg.extra))
    assert reasons == [(4401, "auth-failed"), (4401, "auth-failed")]
    assert len(verifies) == 2
    assert verifies[0].startswith(b"ghost.home|")


async def test_ws_hello_timeout_closes_4408(ws_client, monkeypatch):
    """Connecting and never sending a hello → server closes 4408 once the
    hello window (5 s in production, shrunk here) runs out."""
    monkeypatch.setattr(ws_routes, "HELLO_TIMEOUT_SECONDS", 0.2)
    async with ws_client.ws_connect("/gfs/ws") as ws:
        msg = await asyncio.wait_for(ws.receive(), timeout=8.0)
        assert msg.type.name == "CLOSE"
        assert ws.close_code == 4408


async def test_ws_reconnect_evicts_previous(ws_client):
    """A second connect for the same instance evicts the first with 4409."""
    first = await ws_client.ws_connect("/gfs/ws")
    await first.send_json(_hello("peer.home", ws_client._seed))

    registry = ws_client._app[gfs_ws_registry_key]
    for _ in range(50):
        if registry.is_connected("peer.home"):
            break
        await asyncio.sleep(0.01)

    second = await ws_client.ws_connect("/gfs/ws")
    await second.send_json(_hello("peer.home", ws_client._seed))

    msg = await asyncio.wait_for(first.receive(), timeout=2.0)
    assert msg.type.name == "CLOSE"
    assert first.close_code == 4409
    await second.close()


async def test_a_failed_push_closes_the_socket_and_the_envelope_drains_on_reconnect(
    ws_client,
):
    """A live push that fails must close the household's socket (1013): it
    used to be only dropped from the registry, so the household stayed
    connected, received nothing more and never reconnected. The envelope
    went to the queue and drains after the next hello."""
    sealed = {"kem_suite": "x25519", "eph_pk": "ZXBo", "ciphertext": "Y2lwaGVy"}
    # ``POST /gfs/envelope`` takes only a well-formed instance id.
    peer = "recipient2home2222222222222222aa"
    fed_repo = ws_client._app[gfs_fed_repo_key]
    existing = await fed_repo.get_instance("peer.home")
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id=peer,
            display_name="Peer",
            public_key=existing.public_key,
            status="active",
        )
    )
    registry = ws_client._app[gfs_ws_registry_key]
    async with ws_client.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_hello(peer, ws_client._seed))
        for _ in range(50):
            if registry.is_connected(peer):
                break
            await asyncio.sleep(0.01)
        server_ws = registry._by_instance[peer]

        async def _reset(*_args, **_kwargs):
            raise ConnectionResetError("simulated reset")

        server_ws.send_str = _reset
        resp = await ws_client.post(
            "/gfs/envelope", json={"to_instance": peer, "sealed": sealed}
        )
        assert resp.status == 202
        closed = await asyncio.wait_for(ws.receive(), timeout=5)
        assert closed.type.name == "CLOSE"
        assert ws.close_code == 1013

    async with ws_client.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_hello(peer, ws_client._seed))
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            frame = json.loads(msg.data)
            if frame.get("type") == "envelope":
                break
        assert frame == {"type": "envelope", "sealed": sealed}


async def test_ws_unregisters_on_clean_disconnect(ws_client):
    async with ws_client.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_hello("peer.home", ws_client._seed))
        registry = ws_client._app[gfs_ws_registry_key]
        for _ in range(50):
            if registry.is_connected("peer.home"):
                break
            await asyncio.sleep(0.01)
        assert registry.is_connected("peer.home")

    # Allow the server-side `finally` block to run + the queued
    # ``upsert_rtc_connection(transport="https")`` write to flush.
    fed_repo = ws_client._app[gfs_fed_repo_key]
    for _ in range(100):
        row = await fed_repo.get_rtc_connection("peer.home")
        if (
            not registry.is_connected("peer.home")
            and row is not None
            and row.transport == "https"
        ):
            break
        await asyncio.sleep(0.02)
    assert registry.is_connected("peer.home") is False
    assert row is not None
    assert row.transport == "https"


# ── Subscriber reconnect → owner re-notify (Phase 5b-d) ───────────────────────


async def test_the_seen_mark_refresh_survives_a_failed_write(monkeypatch, caplog):
    """A failed ``relay_seen_at`` write is logged and retried on the next
    tick; the loop ends once the socket is closed."""
    monkeypatch.setattr(ws_routes, "RELAY_SEEN_MIN_SESSION_S", 0.0)
    monkeypatch.setattr(ws_routes, "RELAY_SEEN_REFRESH_S", 0.0)

    class _Socket:
        closed = False

    sock = _Socket()

    class _Repo:
        def __init__(self) -> None:
            self.calls = 0

        async def mark_relay_seen(self, instance_id: str, *, at: int) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("db busy")
            if self.calls == 3:
                sock.closed = True

    repo = _Repo()
    with caplog.at_level("WARNING"):
        await asyncio.wait_for(
            ws_routes._mark_seen_while_open(repo, "home", sock), timeout=2
        )
    assert repo.calls == 3
    assert "relay-seen mark failed for home" in caplog.text


async def test_a_hello_triggers_no_space_keyed_notice(ws_client):
    """v_96 (P7) — the space-keyed follower seats are gone, so a hello asks
    no owner for a key repair (no ``new_subscriber``), whatever it lists."""
    hello = _hello("peer.home", ws_client._seed)
    hello["accepts"] = ["space_key_pull", "ws_binary1"]
    async with ws_client.ws_connect("/gfs/ws") as ws:
        await ws.send_json(hello)
        registry = ws_client._app[gfs_ws_registry_key]
        for _ in range(100):
            if registry.is_connected("peer.home"):
                break
            await asyncio.sleep(0.01)
        assert registry.is_connected("peer.home")
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(ws.receive(), timeout=0.3)
    assert not hasattr(ws_client._app[gfs_federation_key], "on_subscriber_connected")
