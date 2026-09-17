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

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from socialhome.crypto import b64url_encode, sign_ed25519
from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
    gfs_federation_key,
    gfs_ws_registry_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.repositories import SqliteGfsFederationRepo
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
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _hello(instance_id: str, seed: bytes, *, ts: int | None = None) -> dict:
    if ts is None:
        ts = int(time.time())
    msg = f"{instance_id}|{ts}".encode("utf-8")
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
                inbox_url="http://peer.home/wh",
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


async def test_ws_push_via_fanout_reaches_client(ws_client):
    """publish_event → _fan_out → registry.send → SH receives ``relay``."""
    async with ws_client.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_hello("peer.home", ws_client._seed))

        registry = ws_client._app[gfs_ws_registry_key]
        for _ in range(50):
            if registry.is_connected("peer.home"):
                break
            await asyncio.sleep(0.01)

        # Subscribe peer.home to a space, then publish from another instance.
        federation = ws_client._app[gfs_federation_key]
        other_seed, other_pub = _gen_ed25519()
        await ws_client._app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id="other.home",
                display_name="Other",
                public_key=other_pub,
                inbox_url="http://other.home/wh",
                status="active",
            )
        )
        # other.home publishes space-1 so it's a real subscribe target.
        pub_args = {
            "space_id": "space-1",
            "owning_instance": "other.home",
            "name": "Space One",
            "description": "",
            "about_markdown": "",
            "cover_url": "",
            "icon_url": "",
            "min_age": 0,
            "category": "general",
            "accent_color": "#D2542A",
            "primary_color": "#D2542A",
            "identity_public_key": "",
        }
        pub_canonical = json.dumps(
            pub_args, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        await federation.publish_space(
            space_id="space-1",
            owning_instance="other.home",
            name="Space One",
            signature=b64url_encode(sign_ed25519(other_seed, pub_canonical)),
        )
        sub_ts = _now_iso()
        sub_canonical = json.dumps(
            {
                "action": "subscribe",
                "instance_id": "peer.home",
                "space_id": "space-1",
                "ts": sub_ts,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        await federation.subscribe(
            "peer.home",
            "space-1",
            sub_ts,
            b64url_encode(sign_ed25519(ws_client._seed, sub_canonical)),
        )
        ev_canonical = json.dumps(
            {
                "space_id": "space-1",
                "event_type": "post.created",
                "payload": {"text": "hello"},
                "from_instance": "other.home",
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        delivered = await federation.publish_event(
            "space-1",
            "post.created",
            {"text": "hello"},
            "other.home",
            signature=b64url_encode(sign_ed25519(other_seed, ev_canonical)),
        )
        assert delivered == ["peer.home"]

        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        frame = json.loads(msg.data)
        assert frame["type"] == "relay"
        assert frame["space_id"] == "space-1"
        assert frame["event_type"] == "post.created"
        assert frame["from_instance"] == "other.home"
        assert frame["payload"] == {"text": "hello"}


async def test_ws_rejects_bad_signature(ws_client):
    async with ws_client.ws_connect("/gfs/ws") as ws:
        bad = _hello("peer.home", ws_client._seed)
        bad["sig"] = b64url_encode(b"\x00" * 64)
        await ws.send_json(bad)

        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
        assert msg.type.name == "CLOSE"
        assert ws.close_code == 4401


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


async def test_ws_hello_timeout_closes_4408(ws_client):
    """Connecting and never sending a hello → server closes 4408 within ~5 s."""
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


async def _publish_and_subscribe_peer(
    app, subscriber_seed: bytes, *, owner_id: str, owner_seed: bytes
) -> None:
    """Publish ``space-1`` under *owner_id* and subscribe ``peer.home`` — the
    exact production shape whose 5b-b handoff is lost when the subscriber's own
    socket is down at subscribe time."""
    federation = app[gfs_federation_key]
    pub_args = {
        "space_id": "space-1",
        "owning_instance": owner_id,
        "name": "Space One",
        "description": "",
        "about_markdown": "",
        "cover_url": "",
        "icon_url": "",
        "min_age": 0,
        "category": "general",
        "accent_color": "#D2542A",
        "primary_color": "#D2542A",
        "identity_public_key": "",
    }
    pub_canonical = json.dumps(pub_args, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    await federation.publish_space(
        space_id="space-1",
        owning_instance=owner_id,
        name="Space One",
        signature=b64url_encode(sign_ed25519(owner_seed, pub_canonical)),
    )
    sub_ts = _now_iso()
    sub_canonical = json.dumps(
        {
            "action": "subscribe",
            "instance_id": "peer.home",
            "space_id": "space-1",
            "ts": sub_ts,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    await federation.subscribe(
        "peer.home",
        "space-1",
        sub_ts,
        b64url_encode(sign_ed25519(subscriber_seed, sub_canonical)),
    )


async def test_ws_connect_renotifies_owner_of_existing_subscriptions(ws_client):
    """REGRESSION: the subscriber subscribed while its socket was DOWN (the
    5b-b handoff never reached it). When its socket connects, the owner gets
    the ``new_subscriber`` frame again over the owner's live socket."""
    app = ws_client._app
    owner_seed, owner_pub = _gen_ed25519()
    await app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="owner.home",
            display_name="Owner",
            public_key=owner_pub,
            inbox_url="http://owner.home/wh",
            status="active",
        )
    )
    await _publish_and_subscribe_peer(
        app, ws_client._seed, owner_id="owner.home", owner_seed=owner_seed
    )

    async with ws_client.ws_connect("/gfs/ws") as owner_ws:
        await owner_ws.send_json(_hello("owner.home", owner_seed))
        registry = app[gfs_ws_registry_key]
        for _ in range(100):
            if registry.is_connected("owner.home"):
                break
            await asyncio.sleep(0.01)

        sub_ws = await ws_client.ws_connect("/gfs/ws")
        await sub_ws.send_json(_hello("peer.home", ws_client._seed))

        msg = await asyncio.wait_for(owner_ws.receive(), timeout=5.0)
        frame = json.loads(msg.data)
        assert frame["type"] == "new_subscriber"
        assert frame["space_id"] == "space-1"
        assert frame["subscriber"]["instance_id"] == "peer.home"
        await sub_ws.close()


async def test_ws_connect_survives_reconnect_notify_failure(ws_client, monkeypatch):
    """A repo failure in the reconnect notify must not break the handshake —
    the socket still registers and the hello still succeeds."""

    async def _boom(self, instance_id: str):
        raise RuntimeError("db gone")

    monkeypatch.setattr(
        SqliteGfsFederationRepo,
        "list_subscribed_spaces",
        _boom,
    )
    async with ws_client.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_hello("peer.home", ws_client._seed))
        registry = ws_client._app[gfs_ws_registry_key]
        for _ in range(100):
            if registry.is_connected("peer.home"):
                break
            await asyncio.sleep(0.01)
        assert registry.is_connected("peer.home")
        assert not ws.closed
