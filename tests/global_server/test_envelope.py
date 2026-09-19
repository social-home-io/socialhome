"""Integration tests for ``POST /gfs/envelope`` (§D2b opaque relay).

Real GFS app, real SQLite, real aiohttp client + WebSocket. The
security-marked tests pin the two properties the endpoint exists for: the
response is a byte-identical ``202`` no matter who (or whether) the
recipient is, and the sealed blob never reaches a log line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from socialhome.capabilities_sig import verify_capabilities
from socialhome.crypto import b64url_encode, sign_ed25519
from socialhome.global_server.app_keys import (
    gfs_db_key,
    gfs_envelope_queue_repo_key,
    gfs_fed_repo_key,
    gfs_ws_registry_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.envelope_relay import (
    ENVELOPE_MAX_BODY_BYTES,
    ENVELOPE_MAX_PER_MINUTE,
)
from socialhome.global_server.server import create_gfs_app

CIPHERTEXT = "bm9uY2U:Y2lwaGVydGV4dC1ieXRlcw"
EPH_PK = "ZXBoZW1lcmFsLXB1YmxpYy1rZXktYnl0ZXM"


def _sealed(marker: str = CIPHERTEXT) -> dict[str, str]:
    return {"kem_suite": "x25519", "eph_pk": EPH_PK, "ciphertext": marker}


def _envelope(to_instance: str, marker: str = CIPHERTEXT) -> dict:
    return {"to_instance": to_instance, "sealed": _sealed(marker)}


def _gen_ed25519() -> tuple[bytes, str]:
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


def _hello(instance_id: str, seed: bytes) -> dict:
    ts = int(time.time())
    return {
        "type": "hello",
        "instance_id": instance_id,
        "ts": ts,
        "sig": b64url_encode(sign_ed25519(seed, f"{instance_id}|{ts}".encode("utf-8"))),
    }


@pytest.fixture
async def gfs(tmp_dir):
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
                instance_id="recipient2home2222222222222222aa",
                display_name="Recipient",
                public_key=pub_hex,
                inbox_url="http://recipient.home/wh",
                status="active",
            )
        )
        tc._seed = seed
        tc._app = app
        yield tc


async def _wait_connected(app, instance_id: str) -> None:
    registry = app[gfs_ws_registry_key]
    for _ in range(200):
        if registry.is_connected(instance_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{instance_id} never connected")


# ── Delivery ─────────────────────────────────────────────────────────────


async def test_online_recipient_receives_exactly_type_and_sealed(gfs):
    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_hello("recipient2home2222222222222222aa", gfs._seed))
        await _wait_connected(gfs._app, "recipient2home2222222222222222aa")

        resp = await gfs.post(
            "/gfs/envelope", json=_envelope("recipient2home2222222222222222aa")
        )
        assert resp.status == 202

        frame = await asyncio.wait_for(ws.receive_json(), timeout=5)

    assert frame == {"type": "envelope", "sealed": _sealed()}


async def test_offline_recipient_is_queued_then_drained_in_order_on_hello(gfs):
    for i in range(3):
        resp = await gfs.post(
            "/gfs/envelope",
            json=_envelope("recipient2home2222222222222222aa", f"ct-{i}"),
        )
        assert resp.status == 202

    queue_repo = gfs._app[gfs_envelope_queue_repo_key]
    assert await queue_repo.count_for("recipient2home2222222222222222aa") == 3

    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_hello("recipient2home2222222222222222aa", gfs._seed))
        frames = [
            await asyncio.wait_for(ws.receive_json(), timeout=5) for _ in range(3)
        ]

    assert [f["sealed"]["ciphertext"] for f in frames] == ["ct-0", "ct-1", "ct-2"]
    assert all(set(f) == {"type", "sealed"} for f in frames)
    assert await queue_repo.count_for("recipient2home2222222222222222aa") == 0


@pytest.mark.security
async def test_unknown_recipient_is_accepted_and_stores_nothing(gfs):
    resp = await gfs.post(
        "/gfs/envelope", json=_envelope("stranger2home2222222222222222abc")
    )
    assert resp.status == 202

    rows = await gfs._app[gfs_db_key].fetchall("SELECT * FROM gfs_envelope_queue", ())
    assert rows == []


# ── Uniform response ─────────────────────────────────────────────────────


@pytest.mark.security
async def test_response_is_byte_identical_online_offline_and_unknown(gfs):
    """Any variation here is a presence/existence oracle: an anonymous caller
    could walk instance ids and learn which households use this server and
    which are awake."""
    offline = await gfs.post(
        "/gfs/envelope", json=_envelope("recipient2home2222222222222222aa")
    )
    offline_body = await offline.read()

    unknown = await gfs.post(
        "/gfs/envelope", json=_envelope("stranger2home2222222222222222abc")
    )
    unknown_body = await unknown.read()

    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(_hello("recipient2home2222222222222222aa", gfs._seed))
        await _wait_connected(gfs._app, "recipient2home2222222222222222aa")
        online = await gfs.post(
            "/gfs/envelope", json=_envelope("recipient2home2222222222222222aa")
        )
        online_body = await online.read()

    assert online.status == offline.status == unknown.status == 202
    assert online_body == offline_body == unknown_body
    assert json.loads(online_body) == {"status": "accepted"}


# ── Rejections ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"to_instance": "recipient2home2222222222222222aa"},
        {"sealed": _sealed()},
        {"to_instance": "", "sealed": _sealed()},
        {"to_instance": "x" * 129, "sealed": _sealed()},
        {"to_instance": ["recipient2home2222222222222222aa"], "sealed": _sealed()},
        {"to_instance": "recipient2home2222222222222222aa", "sealed": "opaque"},
        {
            "to_instance": "recipient2home2222222222222222aa",
            "sealed": {"kem_suite": "x25519"},
        },
        {
            "to_instance": "recipient2home2222222222222222aa",
            "sealed": {**_sealed(), "from_instance": "sender.home"},
        },
    ],
)
async def test_malformed_body_is_400(gfs, body):
    resp = await gfs.post("/gfs/envelope", json=body)
    assert resp.status == 400


async def test_non_json_body_is_400(gfs):
    resp = await gfs.post("/gfs/envelope", data=b"{not json")
    assert resp.status == 400


async def test_json_array_body_is_400(gfs):
    resp = await gfs.post("/gfs/envelope", data=b"[1,2,3]")
    assert resp.status == 400


async def test_oversize_body_is_413(gfs):
    oversized = json.dumps(
        {
            "to_instance": "recipient2home2222222222222222aa",
            "sealed": {
                "kem_suite": "x25519",
                "eph_pk": EPH_PK,
                "ciphertext": "A" * (ENVELOPE_MAX_BODY_BYTES + 1),
            },
        }
    ).encode()
    resp = await gfs.post(
        "/gfs/envelope",
        data=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 413


async def test_rate_limiter_sheds_a_flood_from_one_ip(gfs):
    statuses = []
    for _ in range(ENVELOPE_MAX_PER_MINUTE + 1):
        resp = await gfs.post(
            "/gfs/envelope", json=_envelope("recipient2home2222222222222222aa")
        )
        statuses.append(resp.status)
    assert statuses[:ENVELOPE_MAX_PER_MINUTE] == [202] * ENVELOPE_MAX_PER_MINUTE
    assert statuses[-1] == 429


# ── Logging discipline ───────────────────────────────────────────────────


@pytest.mark.security
async def test_no_log_record_carries_the_sealed_material(gfs, caplog):
    with caplog.at_level(logging.DEBUG):
        await gfs.post(
            "/gfs/envelope", json=_envelope("recipient2home2222222222222222aa")
        )
        await gfs.post(
            "/gfs/envelope", json=_envelope("stranger2home2222222222222222abc")
        )
        async with gfs.ws_connect("/gfs/ws") as ws:
            await ws.send_json(_hello("recipient2home2222222222222222aa", gfs._seed))
            await asyncio.wait_for(ws.receive_json(), timeout=5)

    for record in caplog.records:
        rendered = record.getMessage()
        assert CIPHERTEXT not in rendered
        assert EPH_PK not in rendered


# ── Capability advertisement ─────────────────────────────────────────────


@pytest.mark.security
async def test_gfs_info_advertises_envelope_relay_inside_the_signed_block(gfs):
    resp = await gfs.get("/gfs/info")
    assert resp.status == 200
    body = await resp.json()

    assert body["capabilities"]["envelope_relay"] is True
    assert verify_capabilities(
        body["public_key"],
        body["gfs_instance_id"],
        body["capabilities"],
        body["capabilities_sig"],
        body["capabilities_sig_suite"],
    )


# ── to_instance shape (log forgery) ──────────────────────────────────────


FORGED_ID = (
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    "2026-09-18 00:00:00 WARNING gfs.envelope: ACCEPTED forged line"
)


@pytest.mark.security
async def test_a_newline_in_to_instance_is_a_400_and_forges_no_log_line(gfs, caplog):
    """The relay is anonymous by design, so ``to_instance`` is attacker-
    controlled on every request — and the server writes it into its own log
    lines. A length-only bound let a caller author a second, entirely
    fabricated log record."""
    with caplog.at_level(logging.DEBUG):
        resp = await gfs.post(
            "/gfs/envelope",
            json={"to_instance": FORGED_ID, "sealed": _sealed()},
        )
    assert resp.status == 400
    for record in caplog.records:
        assert "forged line" not in record.getMessage()

    rows = await gfs._app[gfs_db_key].fetchall("SELECT * FROM gfs_envelope_queue", ())
    assert rows == []
