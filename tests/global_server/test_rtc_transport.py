"""Tests for GfsRtcSession + /gfs/rtc/* signalling endpoints (spec §24.12)."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from socialhome.crypto import b64url_encode, sign_ed25519
from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
    gfs_rtc_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.rtc_transport import GfsRtcSession
from socialhome.global_server.server import create_gfs_app


@pytest.fixture
def rtc():
    """A fresh GfsRtcSession instance."""
    return GfsRtcSession()


async def test_offer_returns_session_id(rtc):
    """offer() returns a non-empty string session_id."""
    session_id = await rtc.offer("inst-a", "v=0\r\no=...\r\n")
    assert isinstance(session_id, str)
    assert len(session_id) > 0


async def test_offer_returns_unique_ids(rtc):
    """Multiple offer() calls return distinct session_ids."""
    id1 = await rtc.offer("inst-a", "sdp1")
    id2 = await rtc.offer("inst-a", "sdp2")
    assert id1 != id2


async def test_offer_stores_session(rtc):
    """offer() stores session state retrievable via get_session()."""
    session_id = await rtc.offer("inst-x", "sdp-body")
    session = rtc.get_session(session_id)
    assert session is not None
    assert session.session_id == session_id
    assert session.initiator_id == "inst-x"
    assert session.offer_sdp == "sdp-body"
    assert session.answer_sdp is None


async def test_answer_stores_answer_sdp(rtc):
    """answer() stores the answer SDP for the given session."""
    session_id = await rtc.offer("inst-a", "offer-sdp")
    await rtc.answer(session_id, "answer-sdp")
    session = rtc.get_session(session_id)
    assert session is not None
    assert session.answer_sdp == "answer-sdp"


async def test_answer_unknown_session_raises_key_error(rtc):
    """answer() with an unknown session_id raises KeyError."""
    with pytest.raises(KeyError):
        await rtc.answer("nonexistent-session-id", "sdp")


async def test_ice_candidate_accumulates_candidates(rtc):
    """ice_candidate() appends candidates to the session."""
    session_id = await rtc.offer("inst-b", "sdp")
    candidate1 = {"candidate": "candidate:0 1 UDP ...", "sdpMid": "0"}
    candidate2 = {"candidate": "candidate:1 1 TCP ...", "sdpMid": "0"}
    await rtc.ice_candidate(session_id, candidate1)
    await rtc.ice_candidate(session_id, candidate2)
    session = rtc.get_session(session_id)
    assert session is not None
    assert len(session.ice_candidates) == 2
    assert session.ice_candidates[0] == candidate1
    assert session.ice_candidates[1] == candidate2


async def test_ice_candidate_unknown_session_raises_key_error(rtc):
    """ice_candidate() with an unknown session_id raises KeyError."""
    with pytest.raises(KeyError):
        await rtc.ice_candidate("no-such-session", {"candidate": "x"})


async def test_get_session_returns_none_for_unknown(rtc):
    """get_session() returns None for an unknown session_id."""
    result = rtc.get_session("does-not-exist")
    assert result is None


async def test_multiple_sessions_are_independent(rtc):
    """Two concurrent sessions do not interfere with each other."""
    sid1 = await rtc.offer("inst-1", "sdp-1")
    sid2 = await rtc.offer("inst-2", "sdp-2")
    await rtc.answer(sid1, "answer-1")
    s1 = rtc.get_session(sid1)
    s2 = rtc.get_session(sid2)
    assert s1.answer_sdp == "answer-1"
    assert s2.answer_sdp is None


async def test_offer_with_empty_sdp_is_accepted(rtc):
    """offer() accepts an empty SDP string without raising."""
    session_id = await rtc.offer("inst-empty", "")
    session = rtc.get_session(session_id)
    assert session is not None
    assert session.offer_sdp == ""


# ─── Integration tests — /gfs/rtc/* signalling endpoints ─────────────


def _gen_ed25519():
    """Return (seed_bytes, public_key_hex) for a fresh Ed25519 keypair."""
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


def _sign(body: dict, seed: bytes) -> dict:
    """Attach a base64url-encoded Ed25519 ``signature`` to *body*."""
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return {**body, "signature": b64url_encode(sign_ed25519(seed, canonical))}


@pytest.fixture
async def rtc_client(tmp_dir):
    """GFS app with one active registered peer wired in."""
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
        fed_repo = app[gfs_fed_repo_key]
        await fed_repo.upsert_instance(
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


async def test_rtc_offer_creates_session(rtc_client):
    body = _sign(
        {"instance_id": "peer.home", "sdp": "v=0\r\no=- 123..."},
        rtc_client._seed,
    )
    resp = await rtc_client.post("/gfs/rtc/offer", json=body)
    assert resp.status == 200
    data = await resp.json()
    assert data["session_id"]
    rtc: GfsRtcSession = rtc_client._app[gfs_rtc_key]
    session = rtc.get_session(data["session_id"])
    assert session is not None
    assert session.offer_sdp.startswith("v=0")
    # Transport column flipped to 'webrtc'.
    row = await rtc_client._app[gfs_fed_repo_key].get_rtc_connection("peer.home")
    assert row is not None
    assert row.transport == "webrtc"


async def test_rtc_offer_rejects_unknown_instance(rtc_client):
    seed, _pub = _gen_ed25519()
    body = _sign({"instance_id": "ghost.home", "sdp": "x"}, seed)
    resp = await rtc_client.post("/gfs/rtc/offer", json=body)
    assert resp.status == 403


async def test_rtc_offer_rejects_bad_signature(rtc_client):
    other_seed, _pub = _gen_ed25519()  # wrong key
    body = _sign({"instance_id": "peer.home", "sdp": "x"}, other_seed)
    resp = await rtc_client.post("/gfs/rtc/offer", json=body)
    assert resp.status == 401


async def test_rtc_answer_attaches_sdp(rtc_client):
    body = _sign(
        {"instance_id": "peer.home", "sdp": "offer-body"},
        rtc_client._seed,
    )
    resp = await rtc_client.post("/gfs/rtc/offer", json=body)
    session_id = (await resp.json())["session_id"]
    answer = _sign(
        {"instance_id": "peer.home", "session_id": session_id, "sdp": "answer-body"},
        rtc_client._seed,
    )
    resp = await rtc_client.post("/gfs/rtc/answer", json=answer)
    assert resp.status == 200
    rtc: GfsRtcSession = rtc_client._app[gfs_rtc_key]
    assert rtc.get_session(session_id).answer_sdp == "answer-body"


async def test_rtc_answer_unknown_session_404(rtc_client):
    body = _sign(
        {"instance_id": "peer.home", "session_id": "missing", "sdp": "x"},
        rtc_client._seed,
    )
    resp = await rtc_client.post("/gfs/rtc/answer", json=body)
    assert resp.status == 404


async def test_rtc_ice_relays_candidate(rtc_client):
    body = _sign(
        {"instance_id": "peer.home", "sdp": "o"},
        rtc_client._seed,
    )
    session_id = (await (await rtc_client.post("/gfs/rtc/offer", json=body)).json())[
        "session_id"
    ]
    ice = _sign(
        {
            "instance_id": "peer.home",
            "session_id": session_id,
            "candidate": {"candidate": "candidate:0 1 UDP ...", "sdpMid": "0"},
        },
        rtc_client._seed,
    )
    resp = await rtc_client.post("/gfs/rtc/ice", json=ice)
    assert resp.status == 200
    rtc: GfsRtcSession = rtc_client._app[gfs_rtc_key]
    candidates = rtc.get_session(session_id).ice_candidates
    assert candidates[0]["candidate"].startswith("candidate:0")


async def test_rtc_session_poll_returns_state(rtc_client):
    body = _sign(
        {"instance_id": "peer.home", "sdp": "initiator-offer"},
        rtc_client._seed,
    )
    session_id = (await (await rtc_client.post("/gfs/rtc/offer", json=body)).json())[
        "session_id"
    ]
    resp = await rtc_client.get(f"/gfs/rtc/session/{session_id}")
    assert resp.status == 200
    data = await resp.json()
    assert data["initiator_id"] == "peer.home"
    assert data["offer_sdp"] == "initiator-offer"
    assert data["answer_sdp"] is None
    assert data["ice_candidates"] == []


async def test_rtc_session_poll_404_on_unknown(rtc_client):
    resp = await rtc_client.get("/gfs/rtc/session/nope")
    assert resp.status == 404


async def test_rtc_ping_updates_transport(rtc_client):
    body = _sign(
        {"instance_id": "peer.home", "transport": "https"},
        rtc_client._seed,
    )
    resp = await rtc_client.post("/gfs/rtc/ping", json=body)
    assert resp.status == 200
    row = await rtc_client._app[gfs_fed_repo_key].get_rtc_connection("peer.home")
    assert row.transport == "https"


async def test_rtc_ping_rejects_invalid_transport(rtc_client):
    body = _sign(
        {"instance_id": "peer.home", "transport": "smoke-signals"},
        rtc_client._seed,
    )
    resp = await rtc_client.post("/gfs/rtc/ping", json=body)
    assert resp.status == 422


async def test_rtc_offer_missing_instance_id_422(rtc_client):
    # No instance_id + no signature: caller can't be authenticated.
    resp = await rtc_client.post("/gfs/rtc/offer", json={"sdp": "x"})
    assert resp.status == 422
