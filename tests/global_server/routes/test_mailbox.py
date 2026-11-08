"""``routes/mailbox.py`` — the HTTP / WebSocket surface of GFS mailboxes.
Behaviour end to end: ``tests/global_server/test_mailboxes.py``."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.global_server.config import GfsConfig
from socialhome.global_server.routes.mailbox import REGISTER_MAX_BODY_BYTES
from socialhome.global_server.server import create_gfs_app


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
    async with TestClient(TestServer(create_gfs_app(cfg))) as tc:
        yield tc


async def test_register_non_json_is_400(gfs):
    resp = await gfs.post("/gfs/mailbox/register", data=b"{nope")
    assert resp.status == 400
    assert await resp.json() == {"error": "invalid_json"}


async def test_register_non_object_is_400(gfs):
    resp = await gfs.post("/gfs/mailbox/register", json=[1, 2])
    assert resp.status == 400
    assert await resp.json() == {"error": "invalid_body"}


async def test_register_oversize_is_413(gfs):
    resp = await gfs.post(
        "/gfs/mailbox/register", data=b"x" * (REGISTER_MAX_BODY_BYTES + 1)
    )
    assert resp.status == 413


async def test_challenge_frame_shape(gfs):
    ws = await gfs.ws_connect("/gfs/mailbox/ws")
    hello = await ws.receive_json(timeout=2)
    assert set(hello) == {"type", "challenge", "gfs_instance_id", "sig_suites"}
    assert hello["type"] == "mailbox_challenge"
    assert hello["sig_suites"] == ["ed25519"]
    assert len(hello["challenge"]) == 43  # 32 random bytes, b64url
    await ws.close()


async def test_a_binary_claim_is_a_protocol_close(gfs):
    ws = await gfs.ws_connect("/gfs/mailbox/ws")
    await ws.receive_json(timeout=2)
    await ws.send_bytes(b"\x00")
    closed = await ws.receive(timeout=2)
    assert ws.close_code == 4400
    assert closed.extra == "claim-required"
