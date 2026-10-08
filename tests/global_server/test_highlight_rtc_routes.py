"""Tests for the public-viewer WebRTC signalling routes (§highlights_public).

The offer/poll/ice-viewer routes are anonymous; the answer/ice-author
routes are Ed25519-signed by the author SH. Both round-trip through
the same :class:`GfsRtcSession` table the SH↔SH sync flow uses, so
this test mostly checks the auth + token-gate paths and the WS push
to the author.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import socialhome.global_server.routes.highlight_rtc as hr
from socialhome.global_server import relay_bridge
from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
    gfs_relay_bridge_key,
    gfs_rtc_key,
    gfs_highlight_pub_service_key,
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
    sig = base64.urlsafe_b64encode(sk.sign(canonical)).rstrip(b"=").decode("ascii")
    return {**body, "signature": sig}


def _relay_headers(
    seed: bytes, instance_id: str, relay_id: str, *, ts: int | None = None
) -> dict[str, str]:
    """Header-based auth the author uses for the relay byte-stream upload."""
    if ts is None:
        ts = int(time.time())
    body = {"instance_id": instance_id, "relay_id": relay_id, "ts": ts}
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    sig = base64.urlsafe_b64encode(sk.sign(canonical)).rstrip(b"=").decode("ascii")
    return {
        "X-SH-Instance": instance_id,
        "X-SH-Timestamp": str(ts),
        "X-SH-Signature": sig,
    }


async def _await_relay_offer(client, timeout: float = 2.0) -> str:
    """Poll the author's stub WS for the pushed ``relay_offer`` frame."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for frame in client._author_ws.sent:
            if frame.get("kind") == "relay_offer":
                return frame["relay_id"]
        await asyncio.sleep(0.01)
    raise AssertionError("no relay_offer frame pushed to author")


class _StubWs:
    """Just enough surface for the WS registry's send-then-evict flow."""

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
        # Author "online" — offer route checks WS registry.
        ws = _StubWs()
        app[gfs_ws_registry_key]._by_instance["inst-author"] = ws
        tc._author_ws = ws
        # Pre-publish a highlight so the offer's token resolves.
        registry = app[gfs_highlight_pub_service_key]
        tok, _url = await registry.record_publish(
            highlight_id="s-1",
            instance_id="inst-author",
            expires_at=10_000_000_000,
            publish_signature="",
        )
        tc._token = tok.token
        yield tc


# ─── /gfs/highlight_rtc/offer ────────────────────────────────────────────────


async def test_offer_creates_session_and_pushes_to_author(client):
    body = {
        "instance_id": "inst-author",
        "highlight_id": "s-1",
        "token": client._token,
        "sdp": "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\n",
    }
    resp = await client.post("/gfs/highlight_rtc/offer", json=body)
    assert resp.status == 201
    data = await resp.json()
    assert data["session_id"]
    # WS frame pushed to author.
    sent = client._author_ws.sent
    assert sent and sent[0]["type"] == "highlight_signal"
    assert sent[0]["kind"] == "offer"
    assert sent[0]["session_id"] == data["session_id"]
    assert sent[0]["highlight_id"] == "s-1"


def _shape(resp, body: bytes) -> tuple:
    """Status + body + every header except the per-request ``Date``."""
    headers = sorted(
        (k.lower(), v) for k, v in resp.headers.items() if k.lower() != "date"
    )
    return resp.status, body, headers


async def _offline(client) -> None:
    client._app[gfs_ws_registry_key]._by_instance.pop("inst-author", None)


async def _revoke(client) -> None:
    await client._app[gfs_highlight_pub_service_key].revoke_token(
        client._token, "inst-author"
    )


async def _unpublish(client) -> None:
    await client._app[gfs_highlight_pub_service_key].remove_publish(
        "s-1", "inst-author"
    )


#: Every non-success state the anonymous viewer routes can hit, as
#: ``(label, setup, overrides)``. They MUST be indistinguishable — an
#: outsider must not learn whether the author's household is connected,
#: nor whether the item exists.
_FAILURE_STATES = [
    ("unknown_token", None, {"token": "bogus"}),
    ("unknown_author", None, {"instance_id": "inst-nope"}),
    ("unknown_item", None, {"highlight_id": "s-OTHER"}),
    ("revoked", _revoke, {}),
    ("unpublished", _unpublish, {}),
    ("author_offline", _offline, {}),
]


@pytest.mark.security
@pytest.mark.parametrize(("label", "setup", "override"), _FAILURE_STATES)
async def test_offer_failure_is_uniform(client, label, setup, override):
    """Each failure state returns the one uniform ``503 unavailable``,
    byte-identical to the author-offline response (status, body, headers)."""
    base = {
        "instance_id": "inst-author",
        "highlight_id": "s-1",
        "token": client._token,
        "sdp": "v=0",
    }
    if setup is not None:
        await setup(client)
    resp = await client.post("/gfs/highlight_rtc/offer", json={**base, **override})
    got = _shape(resp, await resp.read())
    # Nothing reached the author's WS on any failure branch.
    assert not [f for f in client._author_ws.sent if f.get("kind") == "offer"]

    # Reference: a never-issued token while the author is offline.
    await _offline(client)
    ref = await client.post(
        "/gfs/highlight_rtc/offer", json={**base, "token": "never-issued"}
    )
    want = _shape(ref, await ref.read())
    assert got == want, label
    assert want[0] == 503
    assert json.loads(want[1]) == {"error": "unavailable"}


async def test_offer_missing_fields_returns_422(client):
    resp = await client.post("/gfs/highlight_rtc/offer", json={"sdp": "v=0"})
    assert resp.status == 422


# ─── /gfs/highlight_rtc/session/{id} polling ─────────────────────────────────


async def test_session_poll_returns_answer_after_author_responds(client):
    offer = await (
        await client.post(
            "/gfs/highlight_rtc/offer",
            json={
                "instance_id": "inst-author",
                "highlight_id": "s-1",
                "token": client._token,
                "sdp": "v=0",
            },
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
    r = await client.post("/gfs/highlight_rtc/answer", json=ans)
    assert r.status == 200

    poll = await (await client.get(f"/gfs/highlight_rtc/session/{session_id}")).json()
    assert poll["answer_sdp"] == "v=0\r\no=- ans"


async def test_session_poll_unknown_session_returns_404(client):
    resp = await client.get("/gfs/highlight_rtc/session/missing")
    assert resp.status == 404


# ─── ICE candidate plumbing ──────────────────────────────────────────────


async def test_viewer_ice_relays_to_author_ws(client):
    offer = await (
        await client.post(
            "/gfs/highlight_rtc/offer",
            json={
                "instance_id": "inst-author",
                "highlight_id": "s-1",
                "token": client._token,
                "sdp": "v=0",
            },
        )
    ).json()
    session_id = offer["session_id"]

    r = await client.post(
        "/gfs/highlight_rtc/ice/viewer",
        json={"session_id": session_id, "candidate": {"candidate": "x"}},
    )
    assert r.status == 200
    # WS now holds the offer frame + the ICE forward.
    sent = client._author_ws.sent
    assert sent[-1]["kind"] == "ice"
    assert sent[-1]["candidate"] == {"candidate": "x"}


async def test_viewer_ice_unknown_session_returns_404(client):
    r = await client.post(
        "/gfs/highlight_rtc/ice/viewer",
        json={"session_id": "missing", "candidate": {"candidate": "x"}},
    )
    assert r.status == 404


async def test_author_ice_signed_appends_candidate(client):
    offer = await (
        await client.post(
            "/gfs/highlight_rtc/offer",
            json={
                "instance_id": "inst-author",
                "highlight_id": "s-1",
                "token": client._token,
                "sdp": "v=0",
            },
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
    r = await client.post("/gfs/highlight_rtc/ice/author", json=body)
    assert r.status == 200
    rtc = client._app[gfs_rtc_key]
    session = rtc.get_session(session_id)
    assert session is not None
    assert {"candidate": "y"} in session.ice_candidates


# ─── Author authority ────────────────────────────────────────────────────


async def test_answer_from_wrong_instance_returns_403(client, tmp_dir):
    """A different signed instance can't answer someone else's session."""
    offer = await (
        await client.post(
            "/gfs/highlight_rtc/offer",
            json={
                "instance_id": "inst-author",
                "highlight_id": "s-1",
                "token": client._token,
                "sdp": "v=0",
            },
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
        {
            "instance_id": "inst-other",
            "session_id": session_id,
            "sdp": "v=0",
        },
    )
    r = await client.post("/gfs/highlight_rtc/answer", json=body)
    assert r.status == 403


# ─── ICE servers ─────────────────────────────────────────────────────────


async def test_ice_servers_returns_stun(client):
    resp = await client.get("/gfs/highlights/ice-servers")
    assert resp.status == 200
    data = await resp.json()
    assert data["servers"]
    assert any("stun:" in s["urls"][0] for s in data["servers"])


# ─── Validation edges ────────────────────────────────────────────────────


async def test_viewer_ice_invalid_payload_returns_422(client):
    r = await client.post("/gfs/highlight_rtc/ice/viewer", json={})
    assert r.status == 422
    r = await client.post(
        "/gfs/highlight_rtc/ice/viewer",
        json={"session_id": "x", "candidate": "not-a-dict"},
    )
    assert r.status == 422


async def test_answer_missing_session_id_returns_422(client):
    body = _sign(client._seed, {"instance_id": "inst-author", "sdp": "v=0"})
    r = await client.post("/gfs/highlight_rtc/answer", json=body)
    assert r.status == 422


async def test_answer_unknown_session_returns_404(client):
    body = _sign(
        client._seed,
        {"instance_id": "inst-author", "session_id": "missing", "sdp": "v=0"},
    )
    r = await client.post("/gfs/highlight_rtc/answer", json=body)
    assert r.status == 404


async def test_author_ice_invalid_payload_returns_422(client):
    body = _sign(client._seed, {"instance_id": "inst-author"})
    r = await client.post("/gfs/highlight_rtc/ice/author", json=body)
    assert r.status == 422


async def test_author_ice_unknown_session_returns_404(client):
    body = _sign(
        client._seed,
        {
            "instance_id": "inst-author",
            "session_id": "missing",
            "candidate": {"candidate": "x"},
        },
    )
    r = await client.post("/gfs/highlight_rtc/ice/author", json=body)
    assert r.status == 404


async def test_author_ice_wrong_instance_returns_403(client):
    """Even with a valid session, only the instance the offer was
    pushed to may push ICE for it."""
    offer = await (
        await client.post(
            "/gfs/highlight_rtc/offer",
            json={
                "instance_id": "inst-author",
                "highlight_id": "s-1",
                "token": client._token,
                "sdp": "v=0",
            },
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
    r = await client.post("/gfs/highlight_rtc/ice/author", json=body)
    assert r.status == 403


async def test_ice_servers_includes_turn_when_configured(client):
    """The /gfs/highlights/ice-servers helper passes a TURN server through
    when the operator set it on the GFS config — GfsConfig is a frozen
    dataclass so we swap in a tiny ``SimpleNamespace`` that satisfies
    the same ``getattr``-based shape the route expects."""
    from types import SimpleNamespace

    from socialhome.global_server.app_keys import gfs_config_key

    client._app[gfs_config_key] = SimpleNamespace(  # type: ignore[assignment]
        ice_stun_url="stun:stun.l.google.com:19302",
        ice_turn_url="turn:turn.example:3478",
        ice_turn_user="alice",
        ice_turn_credential="secret",
    )
    resp = await client.get("/gfs/highlights/ice-servers")
    data = await resp.json()
    urls = [s["urls"][0] for s in data["servers"]]
    assert any("turn:" in u for u in urls)


# ─── GFS-relay fallback ──────────────────────────────────────────────────


async def test_relay_round_trip_pipes_author_bytes_to_guest(client):
    """Guest GET ⇄ signed author upload pipes the framed bytes verbatim."""
    framed = b"\x00\x00\x00\x04meta" + b"x" * 5000  # opaque to the bridge
    get_task = asyncio.create_task(
        client.get(
            f"/gfs/highlight_rtc/relay/inst-author/s-1?token={client._token}",
        )
    )
    relay_id = await _await_relay_offer(client)
    # Author streams the framed bytes back over the signed upload.
    up = await client.post(
        f"/gfs/highlight_rtc/relay-stream/{relay_id}",
        data=framed,
        headers=_relay_headers(client._seed, "inst-author", relay_id),
    )
    assert up.status == 200
    resp = await get_task
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/octet-stream"
    body = await resp.read()
    assert body == framed


async def test_relay_missing_token_returns_422(client):
    resp = await client.get("/gfs/highlight_rtc/relay/inst-author/s-1")
    assert resp.status == 422


@pytest.mark.security
@pytest.mark.parametrize(("label", "setup", "override"), _FAILURE_STATES)
async def test_relay_failure_is_uniform_in_shape_and_latency(
    client, monkeypatch, label, setup, override
):
    """Every relay failure — including the author-online-but-never-streams
    branch — answers the same ``503 unavailable`` only after the same
    author-connect budget, so neither the bytes nor the latency say
    whether the author's household is connected."""

    budget = 0.3
    monkeypatch.setattr(hr, "RELAY_AUTHOR_CONNECT_TIMEOUT_SECONDS", budget)
    instance_id = override.get("instance_id", "inst-author")
    highlight_id = override.get("highlight_id", "s-1")
    token = override.get("token", client._token)
    if setup is not None:
        await setup(client)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    resp = await client.get(
        f"/gfs/highlight_rtc/relay/{instance_id}/{highlight_id}?token={token}",
    )
    elapsed = loop.time() - t0
    got = _shape(resp, await resp.read())

    # Reference: author online, valid token, author never starts streaming.
    client._app[gfs_ws_registry_key]._by_instance["inst-author"] = client._author_ws
    tok, _ = await client._app[gfs_highlight_pub_service_key].record_publish(
        highlight_id="s-ref",
        instance_id="inst-author",
        expires_at=10_000_000_000,
        publish_signature="",
    )
    t1 = loop.time()
    ref = await client.get(
        f"/gfs/highlight_rtc/relay/inst-author/s-ref?token={tok.token}"
    )
    ref_elapsed = loop.time() - t1
    want = _shape(ref, await ref.read())

    assert got == want, label
    assert want[0] == 503
    assert json.loads(want[1]) == {"error": "unavailable"}
    assert elapsed >= budget, (label, elapsed)
    assert ref_elapsed >= budget


@pytest.mark.security
async def test_relay_bridge_full_is_uniform(client, monkeypatch):
    """The live-channel ceiling ("could not be brokered") is the same
    response after the same budget."""

    monkeypatch.setattr(hr, "RELAY_AUTHOR_CONNECT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(relay_bridge, "MAX_LIVE_CHANNELS", 0)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    resp = await client.get(
        f"/gfs/highlight_rtc/relay/inst-author/s-1?token={client._token}",
    )
    assert loop.time() - t0 >= 0.2
    assert resp.status == 503
    assert await resp.json() == {"error": "unavailable"}


async def test_relay_upload_unknown_relay_id_returns_404(client):
    resp = await client.post(
        "/gfs/highlight_rtc/relay-stream/missing",
        data=b"bytes",
        headers=_relay_headers(client._seed, "inst-author", "missing"),
    )
    assert resp.status == 404


async def test_relay_upload_wrong_instance_returns_403(client):
    # A live relay targeting inst-author; a different signed instance can't
    # feed it.
    relay_id = client._app[gfs_relay_bridge_key].create(
        target_instance_id="inst-author", scope="s-1"
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
        f"/gfs/highlight_rtc/relay-stream/{relay_id}",
        data=b"bytes",
        headers=_relay_headers(other_seed, "inst-other", relay_id),
    )
    assert resp.status == 403


async def test_relay_upload_bad_signature_returns_401(client):
    relay_id = client._app[gfs_relay_bridge_key].create(
        target_instance_id="inst-author", scope="s-1"
    )
    headers = _relay_headers(client._seed, "inst-author", relay_id)
    headers["X-SH-Signature"] = "tampered"
    resp = await client.post(
        f"/gfs/highlight_rtc/relay-stream/{relay_id}",
        data=b"bytes",
        headers=headers,
    )
    assert resp.status == 401


async def test_relay_upload_missing_auth_headers_returns_422(client):
    relay_id = client._app[gfs_relay_bridge_key].create(
        target_instance_id="inst-author", scope="s-1"
    )
    resp = await client.post(
        f"/gfs/highlight_rtc/relay-stream/{relay_id}", data=b"bytes"
    )
    assert resp.status == 422


async def test_relay_upload_stale_timestamp_returns_401(client):
    relay_id = client._app[gfs_relay_bridge_key].create(
        target_instance_id="inst-author", scope="s-1"
    )
    headers = _relay_headers(
        client._seed, "inst-author", relay_id, ts=int(time.time()) - 1000
    )
    resp = await client.post(
        f"/gfs/highlight_rtc/relay-stream/{relay_id}", data=b"bytes", headers=headers
    )
    assert resp.status == 401


async def test_relay_upload_missing_timestamp_returns_422(client):
    relay_id = client._app[gfs_relay_bridge_key].create(
        target_instance_id="inst-author", scope="s-1"
    )
    headers = _relay_headers(client._seed, "inst-author", relay_id)
    del headers["X-SH-Timestamp"]
    resp = await client.post(
        f"/gfs/highlight_rtc/relay-stream/{relay_id}", data=b"bytes", headers=headers
    )
    assert resp.status == 422


async def test_offer_endpoint_is_rate_limited(client):
    """The anonymous offer entry point sheds a per-IP flood with 429."""
    body = {
        "instance_id": "inst-author",
        "highlight_id": "s-1",
        "token": client._token,
        "sdp": "v=0",
    }
    saw_429 = False
    for _ in range(40):
        r = await client.post("/gfs/highlight_rtc/offer", json=body)
        if r.status == 429:
            saw_429 = True
            assert r.headers.get("Retry-After") == "60"
            break
    assert saw_429, "offer endpoint was never rate-limited"
