"""Tests for the public-moments WebRTC signalling routes (§Momentum-public).

Mirrors ``test_highlight_rtc_routes.py``. The offer/poll/ice-viewer
routes are anonymous (gated by the user's live directory registration);
the answer/ice-author routes are Ed25519-signed by the author SH. Both
round-trip through the same :class:`GfsRtcSession` table the SH↔SH sync
flow uses, so this test mostly checks the registration-gate, the WS push
to the author, and the GFS-relay byte passthrough.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import socialhome.global_server.routes.moment_rtc as mr
from socialhome.global_server import relay_bridge
from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
    gfs_moment_public_registry_key,
    gfs_relay_bridge_key,
    gfs_rtc_key,
    gfs_ws_registry_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app


# ─── Helpers ─────────────────────────────────────────────────────────────


def _config(tmp_dir):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
    )


def _make_keypair() -> tuple[bytes, str]:
    sk = Ed25519PrivateKey.generate()
    seed = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pk_hex = (
        sk.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    return seed, pk_hex


def _sign(seed: bytes, body: dict) -> dict:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    import base64

    sig = base64.urlsafe_b64encode(sk.sign(canonical)).rstrip(b"=").decode("ascii")
    return {**body, "signature": sig}


def _relay_headers(
    seed: bytes, instance_id: str, relay_id: str, *, ts: int | None = None
) -> dict[str, str]:
    if ts is None:
        ts = int(time.time())
    body = {"instance_id": instance_id, "relay_id": relay_id, "ts": ts}
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    import base64

    sig = base64.urlsafe_b64encode(sk.sign(canonical)).rstrip(b"=").decode("ascii")
    return {
        "X-SH-Instance": instance_id,
        "X-SH-Timestamp": str(ts),
        "X-SH-Signature": sig,
    }


async def _await_relay_offer(client, timeout: float = 2.0) -> str:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for frame in client._author_ws.sent:
            if frame.get("kind") == "relay_offer":
                return frame["relay_id"]
        await asyncio.sleep(0.01)
    raise AssertionError("no relay_offer frame pushed to author")


class _StubWs:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send_str(self, msg: str) -> None:
        self.sent.append(json.loads(msg))


@pytest.fixture
async def keypair():
    return _make_keypair()


@pytest.fixture
async def client(tmp_dir, keypair):
    seed, pk_hex = keypair
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        tc._seed = seed
        await app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id="inst-author",
                display_name="Author",
                public_key=pk_hex,
                status="active",
            )
        )
        # Author "online" — the offer route checks the WS registry.
        ws = _StubWs()
        app[gfs_ws_registry_key]._by_instance["inst-author"] = ws
        tc._author_ws = ws
        # Register the user in the public-moments directory so the offer
        # route's gate resolves.
        registry = app[gfs_moment_public_registry_key]
        await registry.register_user(
            user_id="u-1",
            instance_id="inst-author",
            username="alice",
            display_name="Alice",
            home_instance_pk=pk_hex,
        )
        yield tc


# ─── /gfs/moment_rtc/offer ───────────────────────────────────────────────


async def test_offer_creates_session_and_pushes_to_author(client):
    resp = await client.post(
        "/gfs/moment_rtc/offer",
        json={"user_id": "u-1", "sdp": "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\n"},
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["session_id"]
    sent = client._author_ws.sent
    assert sent and sent[0]["type"] == "moment_signal"
    assert sent[0]["kind"] == "offer"
    assert sent[0]["session_id"] == data["session_id"]
    assert sent[0]["user_id"] == "u-1"
    assert sent[0]["gfs_id"] == "gfs-test"


def _shape(resp, body: bytes) -> tuple:
    """Status + body + every header except the per-request ``Date``."""
    headers = sorted(
        (k.lower(), v) for k, v in resp.headers.items() if k.lower() != "date"
    )
    return resp.status, body, headers


async def _offline(client) -> None:
    client._app[gfs_ws_registry_key]._by_instance.pop("inst-author", None)


async def _deregister(client) -> None:
    await client._app[gfs_moment_public_registry_key].deregister_user("u-1")


#: Every non-success state of the anonymous viewer routes, as
#: ``(label, setup, user_id)``. They MUST be indistinguishable — an
#: outsider must not learn whether the author's household is connected.
_FAILURE_STATES = [
    ("unknown_user", None, "u-nope"),
    ("deregistered", _deregister, "u-1"),
    ("author_offline", _offline, "u-1"),
]


@pytest.mark.security
@pytest.mark.parametrize(("label", "setup", "user_id"), _FAILURE_STATES)
async def test_offer_failure_is_uniform(client, label, setup, user_id):
    """Each failure state returns the one uniform ``503 unavailable``,
    byte-identical (status, body, headers) to the author-offline reply."""
    if setup is not None:
        await setup(client)
    resp = await client.post(
        "/gfs/moment_rtc/offer", json={"user_id": user_id, "sdp": "v=0"}
    )
    got = _shape(resp, await resp.read())
    assert not [f for f in client._author_ws.sent if f.get("kind") == "offer"]

    # Reference: registered user, author offline.
    await _offline(client)
    await client._app[gfs_moment_public_registry_key].register_user(
        user_id="u-ref",
        instance_id="inst-author",
        username="ref",
        display_name="Ref",
        home_instance_pk="00",
    )
    ref = await client.post(
        "/gfs/moment_rtc/offer", json={"user_id": "u-ref", "sdp": "v=0"}
    )
    want = _shape(ref, await ref.read())
    assert got == want, label
    assert want[0] == 503
    assert json.loads(want[1]) == {"error": "unavailable"}


async def test_offer_missing_fields_returns_422(client):
    resp = await client.post("/gfs/moment_rtc/offer", json={"sdp": "v=0"})
    assert resp.status == 422


# ─── session polling ─────────────────────────────────────────────────────


async def test_session_poll_returns_answer_after_author_responds(client):
    offer = await (
        await client.post(
            "/gfs/moment_rtc/offer",
            json={"user_id": "u-1", "sdp": "v=0"},
        )
    ).json()
    session_id = offer["session_id"]

    ans = _sign(
        client._seed,
        {
            "instance_id": "inst-author",
            "session_id": session_id,
            "sdp": "v=0\r\no=- ans",
        },
    )
    r = await client.post("/gfs/moment_rtc/answer", json=ans)
    assert r.status == 200

    poll = await (await client.get(f"/gfs/moment_rtc/session/{session_id}")).json()
    assert poll["answer_sdp"] == "v=0\r\no=- ans"


async def test_session_poll_unknown_session_returns_404(client):
    resp = await client.get("/gfs/moment_rtc/session/missing")
    assert resp.status == 404


# ─── ICE plumbing ────────────────────────────────────────────────────────


async def test_viewer_ice_relays_to_author_ws(client):
    offer = await (
        await client.post(
            "/gfs/moment_rtc/offer",
            json={"user_id": "u-1", "sdp": "v=0"},
        )
    ).json()
    session_id = offer["session_id"]

    r = await client.post(
        "/gfs/moment_rtc/ice/viewer",
        json={"session_id": session_id, "candidate": {"candidate": "x"}},
    )
    assert r.status == 200
    sent = client._author_ws.sent
    assert sent[-1]["type"] == "moment_signal"
    assert sent[-1]["kind"] == "ice"
    assert sent[-1]["candidate"] == {"candidate": "x"}


async def test_viewer_ice_unknown_session_returns_404(client):
    r = await client.post(
        "/gfs/moment_rtc/ice/viewer",
        json={"session_id": "missing", "candidate": {"candidate": "x"}},
    )
    assert r.status == 404


async def test_viewer_ice_invalid_payload_returns_422(client):
    r = await client.post("/gfs/moment_rtc/ice/viewer", json={})
    assert r.status == 422
    r = await client.post(
        "/gfs/moment_rtc/ice/viewer",
        json={"session_id": "x", "candidate": "not-a-dict"},
    )
    assert r.status == 422


async def test_author_ice_signed_appends_candidate(client):
    offer = await (
        await client.post(
            "/gfs/moment_rtc/offer",
            json={"user_id": "u-1", "sdp": "v=0"},
        )
    ).json()
    session_id = offer["session_id"]

    body = _sign(
        client._seed,
        {
            "instance_id": "inst-author",
            "session_id": session_id,
            "candidate": {"candidate": "y"},
        },
    )
    r = await client.post("/gfs/moment_rtc/ice/author", json=body)
    assert r.status == 200
    rtc = client._app[gfs_rtc_key]
    session = rtc.get_session(session_id)
    assert session is not None
    assert {"candidate": "y"} in session.ice_candidates


async def test_author_ice_unknown_session_returns_404(client):
    body = _sign(
        client._seed,
        {
            "instance_id": "inst-author",
            "session_id": "missing",
            "candidate": {"candidate": "x"},
        },
    )
    r = await client.post("/gfs/moment_rtc/ice/author", json=body)
    assert r.status == 404


# ─── Author authority ────────────────────────────────────────────────────


async def test_answer_from_wrong_instance_returns_403(client):
    offer = await (
        await client.post(
            "/gfs/moment_rtc/offer",
            json={"user_id": "u-1", "sdp": "v=0"},
        )
    ).json()
    session_id = offer["session_id"]

    other_seed, other_pk = _make_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="inst-other",
            display_name="Other",
            public_key=other_pk,
            status="active",
        )
    )
    body = _sign(
        other_seed,
        {"instance_id": "inst-other", "session_id": session_id, "sdp": "v=0"},
    )
    r = await client.post("/gfs/moment_rtc/answer", json=body)
    assert r.status == 403


async def test_answer_missing_session_id_returns_422(client):
    body = _sign(client._seed, {"instance_id": "inst-author", "sdp": "v=0"})
    r = await client.post("/gfs/moment_rtc/answer", json=body)
    assert r.status == 422


async def test_answer_unknown_session_returns_404(client):
    body = _sign(
        client._seed,
        {"instance_id": "inst-author", "session_id": "missing", "sdp": "v=0"},
    )
    r = await client.post("/gfs/moment_rtc/answer", json=body)
    assert r.status == 404


# ─── GFS-relay fallback ──────────────────────────────────────────────────


async def test_relay_round_trip_pipes_author_bytes_to_guest(client):
    """Guest GET ⇄ signed author upload pipes the framed bytes verbatim."""
    framed = b"\x00\x00\x00\x04meta" + b"x" * 5000  # opaque to the bridge
    get_task = asyncio.create_task(client.get("/gfs/moment_rtc/relay/u-1"))
    relay_id = await _await_relay_offer(client)
    up = await client.post(
        f"/gfs/moment_rtc/relay-stream/{relay_id}",
        data=framed,
        headers=_relay_headers(client._seed, "inst-author", relay_id),
    )
    assert up.status == 200
    resp = await get_task
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/octet-stream"
    body = await resp.read()
    assert body == framed


@pytest.mark.security
@pytest.mark.parametrize(("label", "setup", "user_id"), _FAILURE_STATES)
async def test_relay_failure_is_uniform_and_immediate(
    client, monkeypatch, label, setup, user_id
):
    """Every relay failure answers the same ``503 unavailable`` as the
    author-online-but-never-streams branch, and answers it at once: no
    failure is held open for the author-connect budget (a floor would hide
    nothing — the matching offer already says 201 vs 503 instantly — while
    letting anonymous callers pin a task for 30 s)."""
    budget = 2.0
    monkeypatch.setattr(mr, "RELAY_AUTHOR_CONNECT_TIMEOUT_SECONDS", budget)
    if setup is not None:
        await setup(client)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    resp = await client.get(f"/gfs/moment_rtc/relay/{user_id}")
    elapsed = loop.time() - t0
    got = _shape(resp, await resp.read())

    # Reference: author online + registered, never starts streaming.
    client._app[gfs_ws_registry_key]._by_instance["inst-author"] = client._author_ws
    await client._app[gfs_moment_public_registry_key].register_user(
        user_id="u-ref",
        instance_id="inst-author",
        username="ref",
        display_name="Ref",
        home_instance_pk="00",
    )
    t1 = loop.time()
    ref = await client.get("/gfs/moment_rtc/relay/u-ref")
    ref_elapsed = loop.time() - t1
    want = _shape(ref, await ref.read())

    assert got == want, label
    assert want[0] == 503
    assert json.loads(want[1]) == {"error": "unavailable"}
    assert elapsed < budget / 2, (label, elapsed)
    assert ref_elapsed >= budget


@pytest.mark.security
async def test_relay_bridge_full_is_uniform(client, monkeypatch):
    """The live-channel ceiling ("could not be brokered") is the same
    uniform response, sent at once."""
    monkeypatch.setattr(mr, "RELAY_AUTHOR_CONNECT_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(relay_bridge, "MAX_LIVE_CHANNELS", 0)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    resp = await client.get("/gfs/moment_rtc/relay/u-1")
    assert loop.time() - t0 < 1.0
    assert resp.status == 503
    assert await resp.json() == {"error": "unavailable"}


async def test_relay_upload_unknown_relay_id_returns_404(client):
    resp = await client.post(
        "/gfs/moment_rtc/relay-stream/missing",
        data=b"bytes",
        headers=_relay_headers(client._seed, "inst-author", "missing"),
    )
    assert resp.status == 404


async def test_relay_upload_wrong_instance_returns_403(client):
    relay_id = client._app[gfs_relay_bridge_key].create(
        target_instance_id="inst-author", scope="u-1"
    )
    other_seed, other_pk = _make_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="inst-other",
            display_name="Other",
            public_key=other_pk,
            status="active",
        )
    )
    resp = await client.post(
        f"/gfs/moment_rtc/relay-stream/{relay_id}",
        data=b"bytes",
        headers=_relay_headers(other_seed, "inst-other", relay_id),
    )
    assert resp.status == 403


async def test_relay_upload_bad_signature_returns_401(client):
    relay_id = client._app[gfs_relay_bridge_key].create(
        target_instance_id="inst-author", scope="u-1"
    )
    headers = _relay_headers(client._seed, "inst-author", relay_id)
    headers["X-SH-Signature"] = "tampered"
    resp = await client.post(
        f"/gfs/moment_rtc/relay-stream/{relay_id}",
        data=b"bytes",
        headers=headers,
    )
    assert resp.status == 401


async def test_relay_upload_missing_auth_headers_returns_422(client):
    relay_id = client._app[gfs_relay_bridge_key].create(
        target_instance_id="inst-author", scope="u-1"
    )
    resp = await client.post(f"/gfs/moment_rtc/relay-stream/{relay_id}", data=b"bytes")
    assert resp.status == 422


async def test_author_ice_invalid_payload_returns_422(client):
    body = _sign(client._seed, {"instance_id": "inst-author"})
    r = await client.post("/gfs/moment_rtc/ice/author", json=body)
    assert r.status == 422


async def test_author_ice_wrong_instance_returns_403(client):
    offer = await (
        await client.post(
            "/gfs/moment_rtc/offer",
            json={"user_id": "u-1", "sdp": "v=0"},
        )
    ).json()
    other_seed, other_pk = _make_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id="inst-other",
            display_name="Other",
            public_key=other_pk,
            status="active",
        )
    )
    body = _sign(
        other_seed,
        {
            "instance_id": "inst-other",
            "session_id": offer["session_id"],
            "candidate": {"candidate": "x"},
        },
    )
    r = await client.post("/gfs/moment_rtc/ice/author", json=body)
    assert r.status == 403
