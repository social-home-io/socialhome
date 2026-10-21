"""Tests for :class:`HighlightSignalingHandler`.

Uses a stub peer factory so the framing + orchestration logic can be
exercised without an :mod:`aiolibdatachannel` runtime. The real
factory is wired in production at ``app._on_startup`` time and is
covered indirectly by the integration suite (PR3 → real-peer).
"""

from __future__ import annotations

import asyncio
import base64
import io
from datetime import UTC, datetime, timedelta

import aiofiles
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from PIL import Image

from socialhome.crypto import b64url_encode
from socialhome.domain.highlight import HighlightAudience, HighlightFrameType
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.highlight_repo import SqliteHighlightRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services import highlight_public_framing as framing
from socialhome.services.highlight_public_profile import open_profile, parse_viewer_key
from socialhome.services.highlight_public_seal import (
    MAX_TOKEN_LEN,
    StreamOpener,
    link_secret,
    token_digest,
)
from socialhome.services.highlight_signaling_handler import (
    MAX_CONCURRENT_VIEWERS_PER_HIGHLIGHT,
    HighlightSignalingHandler,
    _default_peer_factory,
)
from socialhome.webrtc_ice import build_ice_servers

#: The share token the viewer opened, and its ephemeral key (sent as ``vk``).
TOKEN = "tok-1"
VIEWER_SK = ec.generate_private_key(ec.SECP256R1())
VK = b64url_encode(
    VIEWER_SK.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
)
#: ``_make_handler``'s identity seed.
SEED = b"\x00" * 32


def _open_stream(raw_frames, highlight_id, *, token=TOKEN, sk=VIEWER_SK):
    """Open a sealed stream as the viewer: the decoded inner frames."""
    first, *rest = [framing.decode(b) for b in raw_frames]
    assert first.header["kind"] == framing.KIND_STREAM_SEAL
    opener = StreamOpener(
        sk,
        first.header,
        secret=link_secret(SEED, highlight_id=highlight_id, token=token),
        highlight_id=highlight_id,
    )
    out = []
    for f in rest:
        assert f.header == {"kind": framing.KIND_SEALED}
        out.append(framing.decode(opener.open(f.payload)))
    return out


def _split(body: bytes) -> list[bytes]:
    out = []
    rest = body
    while rest:
        hlen = int.from_bytes(rest[:4], "big")
        plen = int.from_bytes(rest[4 + hlen : 8 + hlen], "big")
        out.append(rest[: 8 + hlen + plen])
        rest = rest[8 + hlen + plen :]
    return out


# ─── Stub peer ───────────────────────────────────────────────────────────


class _StubPeer:
    """Records every call. ``wait_open`` resolves immediately so the
    handler proceeds straight to streaming; tests inspect ``frames``
    after the serve task drains."""

    def __init__(self):
        self.remote_offer: str | None = None
        self.answer_sdp: str = "v=0\r\no=- ans"
        self.candidates: list[dict] = []
        self.frames: list[bytes] = []
        self.closed = False

    async def set_remote_offer(self, sdp: str) -> None:
        self.remote_offer = sdp

    async def create_answer(self) -> str:
        return self.answer_sdp

    async def add_ice_candidate(self, candidate: dict) -> None:
        self.candidates.append(candidate)

    async def wait_open(self) -> None:
        return None

    async def send(self, frame_bytes: bytes) -> None:
        self.frames.append(frame_bytes)

    async def close(self) -> None:
        self.closed = True


class _StubResp:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status = status
        self._body = body or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return ""


class _StubSession:
    def __init__(self):
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(200, {"status": "ok"})


class _RecordingRelayPost:
    """Async-CM that drains the streamed ``data`` body into bytes."""

    def __init__(self, sess, url, data, headers):
        self._sess = sess
        self._url = url
        self._data = data
        self._headers = headers

    async def __aenter__(self):
        body = b""
        if hasattr(self._data, "__aiter__"):
            async for chunk in self._data:
                body += chunk
        self._sess.relay_calls.append((self._url, dict(self._headers), body))
        return _StubResp(200, {"status": "ok"})

    async def __aexit__(self, *a):
        return False


class _RecordingSession:
    """Captures the relay streaming POST (url, headers, drained body)."""

    def __init__(self):
        self.relay_calls: list[tuple[str, dict, bytes]] = []

    def post(self, url, *, data=None, headers=None, **_kw):
        return _RecordingRelayPost(self, url, data, headers or {})


# ─── Fixtures ────────────────────────────────────────────────────────────


def _expires(days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


@pytest.fixture
async def repos(db, tmp_dir):
    media_dir = tmp_dir / "media"
    media_dir.mkdir()
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u1', 'alice', 'Alice', 'active')",
    )
    await db.enqueue(
        """
        INSERT INTO gfs_connections(
            id, gfs_instance_id, display_name, public_key, inbox_url,
            status, paired_at
        ) VALUES('gfs-abc','gfs-abc','GFS','ff','https://gfs.example',
                 'active', datetime('now'))
        """,
    )
    return {
        "highlights": SqliteHighlightRepo(db),
        "gfs": SqliteGfsConnectionRepo(db),
        "media_dir": str(media_dir),
    }


@pytest.fixture
async def published_highlight(repos):
    """Highlight with two image frames, marked as publicly published."""
    highlight = await repos["highlights"].find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-05-03",
        expires_at=_expires(),
    )
    f1 = await repos["highlights"].append_frame(
        highlight_id=highlight.id,
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/a.jpg",
    )
    f2 = await repos["highlights"].append_frame(
        highlight_id=highlight.id,
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/b.jpg",
    )
    # Drop the bytes for both frames into the test media dir.
    media_dir = repos["media_dir"]
    async with aiofiles.open(f"{media_dir}/a.jpg", "wb") as f:
        await f.write(b"A" * 1000)
    async with aiofiles.open(f"{media_dir}/b.jpg", "wb") as f:
        await f.write(b"B" * 200_000)  # big-ish
    await repos["highlights"].mark_published(
        highlight.id,
        gfs_id="gfs-abc",
        published_at=datetime.now(UTC).isoformat(),
    )
    # The share token this household minted for the link.
    await repos["highlights"].add_public_token(
        highlight.id, gfs_id="gfs-abc", token_hash=token_digest(TOKEN)
    )
    return highlight.id, f1.id, f2.id


def _make_handler(repos, *, peer_factory=None):
    last_peer: list[_StubPeer] = []

    def _factory(_ice):
        peer = _StubPeer()
        last_peer.append(peer)
        return peer

    handler = HighlightSignalingHandler(
        repos["highlights"],
        repos["gfs"],
        media_dir=repos["media_dir"],
        peer_factory=peer_factory or _factory,
    )
    handler.attach_session(_StubSession())
    handler.attach_identity(own_instance_id="inst-self", signing_key=b"\x00" * 32)
    handler.attach_ice_servers([{"urls": ["stun:stun.example"]}])
    return handler, last_peer


async def _drain(handler) -> None:
    """Wait for every in-flight serve task to drain."""
    tasks = [s.task for s in handler._sessions.values() if s.task is not None]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _drain_relay(handler) -> None:
    """Wait for every in-flight relay-stream task to drain."""
    tasks = list(handler._relay_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ─── Happy path ─────────────────────────────────────────────────────────


async def test_offer_streams_metadata_then_chunks_then_stream_end(
    repos,
    published_highlight,
):
    highlight_id, f1, f2 = published_highlight
    handler, peers = _make_handler(repos)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "highlight_id": highlight_id,
            "sdp": "v=0",
            "token": TOKEN,
            "vk": VK,
        }
    )
    await _drain(handler)
    assert peers, "peer factory wasn't called"
    peer = peers[0]
    assert peer.remote_offer == "v=0"
    assert peer.closed
    # Frame stream: meta, then 1 chunk for the small file, then 4 chunks
    # for the 200KB file (200000 / 65536 = 3 full + 1 partial), then
    # stream_end.
    decoded = _open_stream(peer.frames, highlight_id)
    kinds = [f.header["kind"] for f in decoded]
    assert kinds[0] == framing.KIND_HIGHLIGHT_META
    assert kinds[-1] == framing.KIND_STREAM_END
    chunks = [f for f in decoded if f.header["kind"] == framing.KIND_FRAME_CHUNK]
    # Both frames represented.
    assert {c.header["frame_id"] for c in chunks} == {f1, f2}
    # The last chunk per frame carries is_last_chunk=True.
    last_per_frame = {}
    for c in chunks:
        if c.header["is_last_chunk"]:
            last_per_frame[c.header["frame_id"]] = c
    assert set(last_per_frame.keys()) == {f1, f2}


async def test_meta_manifest_carries_byte_length_and_content_type(
    repos,
    published_highlight,
):
    highlight_id, f1, f2 = published_highlight
    handler, peers = _make_handler(repos)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "highlight_id": highlight_id,
            "sdp": "v=0",
            "token": TOKEN,
            "vk": VK,
        }
    )
    await _drain(handler)
    meta = _open_stream(peers[0].frames, highlight_id)[0].header
    by_id = {f["frame_id"]: f for f in meta["frames"]}
    assert by_id[f1]["byte_length"] == 1000
    assert by_id[f1]["content_type"] == "image/jpeg"
    assert by_id[f2]["byte_length"] == 200_000


async def test_handler_posts_signed_answer_to_correct_gfs(repos, published_highlight):
    highlight_id, *_ = published_highlight
    handler, _peers = _make_handler(repos)
    sess = _StubSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "highlight_id": highlight_id,
            "sdp": "v=0",
            "token": TOKEN,
            "vk": VK,
        }
    )
    await _drain(handler)
    assert sess.posts
    url, body = sess.posts[0]
    assert url == "https://gfs.example/gfs/highlight_rtc/answer"
    assert body["session_id"] == "s-1"
    assert body["instance_id"] == "inst-self"
    assert "signature" in body


# ─── GFS-relay fallback ──────────────────────────────────────────────────


async def test_relay_offer_posts_framed_stream_with_signed_headers(
    repos,
    published_highlight,
):
    """relay_offer → the identical framed stream is POSTed to the GFS
    relay-stream URL with header-based Ed25519 auth."""
    highlight_id, f1, f2 = published_highlight
    handler, _peers = _make_handler(repos)
    sess = _RecordingSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "relay_offer",
            "relay_id": "r-1",
            "highlight_id": highlight_id,
            "token": TOKEN,
            "vk": VK,
        }
    )
    await _drain_relay(handler)

    assert len(sess.relay_calls) == 1
    url, headers, body = sess.relay_calls[0]
    assert url == "https://gfs.example/gfs/highlight_rtc/relay-stream/r-1"
    assert headers["X-SH-Instance"] == "inst-self"
    assert headers["X-SH-Signature"]

    # The streamed body is the same sealed framing as the WebRTC path.
    decoded = _open_stream(_split(body), highlight_id)
    kinds = [d.header["kind"] for d in decoded]
    assert kinds[0] == framing.KIND_HIGHLIGHT_META
    assert kinds[-1] == framing.KIND_STREAM_END
    chunk_ids = {
        d.header["frame_id"]
        for d in decoded
        if d.header["kind"] == framing.KIND_FRAME_CHUNK
    }
    assert chunk_ids == {f1, f2}


async def test_relay_offer_signature_verifies_against_instance_key(
    repos,
    published_highlight,
):
    """The relay auth headers verify under the handler's own public key,
    using the same canonical-dict scheme the GFS checks."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from socialhome.global_server.admin_service import verify_report_signature

    _highlight_id, *_ = published_highlight
    handler, _ = _make_handler(repos)
    headers = handler._relay_auth_headers("r-42")

    pk_hex = (
        Ed25519PrivateKey.from_private_bytes(b"\x00" * 32)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    ts = int(headers["X-SH-Timestamp"])
    body = {"instance_id": "inst-self", "relay_id": "r-42", "ts": ts}
    assert verify_report_signature(body, headers["X-SH-Signature"], pk_hex)
    # A different relay_id must not verify under the same signature.
    assert not verify_report_signature(
        {"instance_id": "inst-self", "relay_id": "r-OTHER", "ts": ts},
        headers["X-SH-Signature"],
        pk_hex,
    )


async def test_relay_offers_beyond_the_cap_are_dropped(
    repos, published_highlight, monkeypatch
):
    """#1087 review: the GFS can ask for relays at will; the uploads in
    flight are bounded like the WebRTC viewers."""
    highlight_id, *_ = published_highlight
    monkeypatch.setattr(
        "socialhome.services.highlight_signaling_handler."
        "MAX_CONCURRENT_VIEWERS_PER_INSTANCE",
        1,
    )
    handler, _ = _make_handler(repos)
    handler._http_client = _RecordingSession()  # type: ignore[assignment]
    gate = asyncio.Event()

    async def _slow(*_a, **_kw):
        await gate.wait()

    monkeypatch.setattr(HighlightSignalingHandler, "_serve_relay", _slow)
    for n in range(3):
        await handler.handle_signal(
            {
                "kind": "relay_offer",
                "relay_id": f"r-{n}",
                "highlight_id": highlight_id,
                "token": TOKEN,
                "vk": VK,
            }
        )
    assert len(handler._relay_tasks) == 1
    gate.set()
    await _drain_relay(handler)


async def test_relay_offer_unpublished_highlight_posts_nothing(repos):
    handler, _ = _make_handler(repos)
    sess = _RecordingSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {"kind": "relay_offer", "relay_id": "r-1", "highlight_id": "missing"}
    )
    await _drain_relay(handler)
    assert sess.relay_calls == []


async def test_relay_offer_missing_fields_is_no_op(repos):
    handler, _ = _make_handler(repos)
    sess = _RecordingSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal({"kind": "relay_offer", "relay_id": ""})
    await _drain_relay(handler)
    assert sess.relay_calls == []


async def test_stop_cancels_in_flight_relay_tasks(repos, published_highlight):
    """A relay stream in flight is cancelled by stop()."""
    highlight_id, *_ = published_highlight
    handler, _ = _make_handler(repos)

    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingSession:
        def post(self, url, *, data=None, headers=None, **_kw):
            return _BlockingPost(data)

    class _BlockingPost:
        def __init__(self, data):
            self._data = data

        async def __aenter__(self):
            started.set()
            await release.wait()  # hold the stream open
            return _StubResp(200, {"status": "ok"})

        async def __aexit__(self, *a):
            return False

    handler._http_client = _BlockingSession()  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "relay_offer",
            "relay_id": "r-1",
            "highlight_id": highlight_id,
            "token": TOKEN,
            "vk": VK,
        }
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert handler._relay_tasks
    await handler.stop()
    assert handler._relay_tasks == set()
    release.set()


# ─── ICE plumbing ───────────────────────────────────────────────────────


async def test_ice_frame_is_forwarded_to_peer(repos, published_highlight):
    highlight_id, *_ = published_highlight

    open_event = asyncio.Event()

    class _SlowPeer(_StubPeer):
        async def wait_open(self) -> None:
            # Block until the test signals ready.
            await open_event.wait()

    peers: list[_StubPeer] = []

    def _factory(_ice):
        p = _SlowPeer()
        peers.append(p)
        return p

    handler, _ = _make_handler(repos, peer_factory=_factory)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "highlight_id": highlight_id,
            "sdp": "v=0",
            "token": TOKEN,
            "vk": VK,
        }
    )
    # Hand off to the loop so the serve task can spin up.
    await asyncio.sleep(0)
    await handler.handle_signal(
        {
            "kind": "ice",
            "session_id": "s-1",
            "candidate": {"candidate": "x", "sdpMid": "0"},
        }
    )
    assert peers[0].candidates == [{"candidate": "x", "sdpMid": "0"}]
    open_event.set()  # let the serve task finish so cleanup is clean
    await _drain(handler)


async def test_ice_for_unknown_session_is_no_op(repos, published_highlight):
    handler, _ = _make_handler(repos)
    await handler.handle_signal(
        {"kind": "ice", "session_id": "missing", "candidate": {"x": 1}}
    )
    # No peers created, no exceptions.


# ─── Failure paths ──────────────────────────────────────────────────────


async def test_offer_for_unpublished_highlight_skips_peer(repos):
    """Unknown / unpublished highlight => no peer is opened, session
    is dropped silently (viewer times out)."""
    handler, peers = _make_handler(repos)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-x",
            "highlight_id": "missing",
            "sdp": "v=0",
            "token": TOKEN,
            "vk": VK,
        }
    )
    await _drain(handler)
    assert peers == []


async def test_per_highlight_cap_rejects_eleventh_offer(repos, published_highlight):
    highlight_id, *_ = published_highlight

    open_event = asyncio.Event()

    class _SlowPeer(_StubPeer):
        async def wait_open(self) -> None:
            await open_event.wait()

    peers: list[_StubPeer] = []

    def _factory(_ice):
        p = _SlowPeer()
        peers.append(p)
        return p

    handler, _ = _make_handler(repos, peer_factory=_factory)
    for i in range(MAX_CONCURRENT_VIEWERS_PER_HIGHLIGHT):
        await handler.handle_signal(
            {
                "kind": "offer",
                "session_id": f"s-{i}",
                "highlight_id": highlight_id,
                "sdp": "v=0",
                "token": TOKEN,
                "vk": VK,
            }
        )
    # 10 sessions hold open peers.
    assert len(peers) == MAX_CONCURRENT_VIEWERS_PER_HIGHLIGHT

    # The 11th must be rejected — no new peer.
    before = len(peers)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-overflow",
            "highlight_id": highlight_id,
            "sdp": "v=0",
            "token": TOKEN,
            "vk": VK,
        }
    )
    assert len(peers) == before
    open_event.set()
    await _drain(handler)


async def test_duplicate_session_id_is_dropped(repos, published_highlight):
    highlight_id, *_ = published_highlight

    open_event = asyncio.Event()

    class _SlowPeer(_StubPeer):
        async def wait_open(self) -> None:
            await open_event.wait()

    peers: list[_StubPeer] = []

    def _factory(_ice):
        p = _SlowPeer()
        peers.append(p)
        return p

    handler, _ = _make_handler(repos, peer_factory=_factory)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "highlight_id": highlight_id,
            "sdp": "v=0",
            "token": TOKEN,
            "vk": VK,
        }
    )
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "highlight_id": highlight_id,
            "sdp": "v=0",
            "token": TOKEN,
            "vk": VK,
        }
    )
    assert len(peers) == 1
    open_event.set()
    await _drain(handler)


# ─── Stop / cleanup ─────────────────────────────────────────────────────


async def test_stop_cancels_in_flight_sessions(repos, published_highlight):
    highlight_id, *_ = published_highlight

    open_event = asyncio.Event()

    class _SlowPeer(_StubPeer):
        async def wait_open(self) -> None:
            await open_event.wait()

    peers: list[_StubPeer] = []

    def _factory(_ice):
        p = _SlowPeer()
        peers.append(p)
        return p

    handler, _ = _make_handler(repos, peer_factory=_factory)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "highlight_id": highlight_id,
            "sdp": "v=0",
            "token": TOKEN,
            "vk": VK,
        }
    )
    await asyncio.sleep(0)  # allow the serve task to start
    await handler.stop()
    assert peers[0].closed
    assert "s-1" not in handler._sessions


# ─── Extra branch coverage ───────────────────────────────────────────────


async def test_handle_signal_unknown_kind_is_no_op(repos):
    handler, peers = _make_handler(repos)
    await handler.handle_signal({"kind": "weather", "session_id": "x"})
    assert peers == []


async def test_offer_missing_fields_is_no_op(repos):
    handler, peers = _make_handler(repos)
    await handler.handle_signal({"kind": "offer", "session_id": ""})
    assert peers == []


async def test_ice_with_non_dict_candidate_is_dropped(repos, published_highlight):
    _highlight_id, *_ = published_highlight
    handler, _ = _make_handler(repos)
    # No exception, no crash.
    await handler.handle_signal(
        {"kind": "ice", "session_id": "s-1", "candidate": "not-a-dict"}
    )


async def test_post_answer_fails_when_gfs_inactive(repos, published_highlight):
    """If the publication's GFS is suspended, no answer is posted (peer
    still gets cleaned up, viewer times out)."""
    highlight_id, *_ = published_highlight
    # Suspend the GFS.
    await repos["gfs"]._db.enqueue(
        "UPDATE gfs_connections SET status='suspended' WHERE id='gfs-abc'"
    )
    sess = _StubSession()
    handler, _peers = _make_handler(repos)
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "highlight_id": highlight_id,
            "sdp": "v=0",
            "token": TOKEN,
            "vk": VK,
        }
    )
    await _drain(handler)
    # No answer ever went out.
    assert sess.posts == []


async def test_post_answer_logs_on_http_error(repos, published_highlight):
    """A 500 from the GFS is logged but doesn't crash the serve task."""
    highlight_id, *_ = published_highlight

    class _ErrSession(_StubSession):
        def post(self, url, *, json=None, **_kw):
            self.posts.append((url, json or {}))
            return _StubResp(500, {"error": "down"})

    handler, _ = _make_handler(repos)
    handler._http_client = _ErrSession()  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "highlight_id": highlight_id,
            "sdp": "v=0",
            "token": TOKEN,
            "vk": VK,
        }
    )
    await _drain(handler)
    # No exception escaped — sessions cleared.
    assert handler._sessions == {}


async def test_per_instance_cap_takes_priority(repos, published_highlight):
    """If MAX_CONCURRENT_VIEWERS_PER_INSTANCE is reached the next offer
    is rejected before the per-highlight cap is even consulted."""
    from socialhome.services import highlight_signaling_handler as mod

    highlight_id, *_ = published_highlight

    open_event = asyncio.Event()

    class _SlowPeer(_StubPeer):
        async def wait_open(self) -> None:
            await open_event.wait()

    peers: list[_StubPeer] = []

    def _factory(_ice):
        p = _SlowPeer()
        peers.append(p)
        return p

    handler, _ = _make_handler(repos, peer_factory=_factory)
    # Tighten the cap so the test runs cheaply.
    saved = mod.MAX_CONCURRENT_VIEWERS_PER_INSTANCE
    mod.MAX_CONCURRENT_VIEWERS_PER_INSTANCE = 1
    try:
        await handler.handle_signal(
            {
                "kind": "offer",
                "session_id": "a",
                "highlight_id": highlight_id,
                "sdp": "v=0",
                "token": TOKEN,
                "vk": VK,
            }
        )
        before = len(peers)
        await handler.handle_signal(
            {
                "kind": "offer",
                "session_id": "b",
                "highlight_id": highlight_id,
                "sdp": "v=0",
                "token": TOKEN,
                "vk": VK,
            }
        )
        assert len(peers) == before
    finally:
        mod.MAX_CONCURRENT_VIEWERS_PER_INSTANCE = saved
        open_event.set()
        await _drain(handler)


async def test_handler_requires_identity_attach_to_sign():
    handler = HighlightSignalingHandler.__new__(HighlightSignalingHandler)
    # Call private _sign through the handler's protocol — but easier:
    # construct a real handler and skip attach_identity.
    from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
    from socialhome.repositories.highlight_repo import SqliteHighlightRepo

    handler = HighlightSignalingHandler(
        SqliteHighlightRepo.__new__(SqliteHighlightRepo),
        SqliteGfsConnectionRepo.__new__(SqliteGfsConnectionRepo),
        media_dir="/tmp",
    )
    with pytest.raises(RuntimeError):
        handler._sign({"a": 1})
    with pytest.raises(RuntimeError):
        handler._require_instance_id()


async def test_resolve_media_path_rejects_traversal(repos):
    handler, _ = _make_handler(repos)
    assert handler._resolve_media_path("/api/media/../etc/passwd") is None
    assert handler._resolve_media_path("/api/media/.hidden") is None
    assert handler._resolve_media_path("not-a-media-url") is None
    assert handler._resolve_media_path(None) is None


async def test_default_peer_factory_returns_a_real_aiolib_peer():
    """The default factory wraps ``aiolibdatachannel.PeerConnection``; an
    earlier revision raised ``NotImplementedError`` until wired in at
    startup — public highlight viewing was silently broken because the
    handler was instantiated without ``peer_factory=``. Now the default
    constructs a working peer."""

    from socialhome.services.highlight_signaling_handler import (
        _AiolibAnswererPeer,
        _default_peer_factory,
    )

    peer = _default_peer_factory([])
    assert isinstance(peer, _AiolibAnswererPeer)
    # Tear down so we don't leave a native handle / spawn task lying
    # around in the test loop.
    await peer.close()


async def test_content_type_helper_picks_extensions():
    from socialhome.services.highlight_signaling_handler import _content_type_for

    assert _content_type_for("/api/media/a.jpg") == "image/jpeg"
    assert _content_type_for("/api/media/a.mp4") == "video/mp4"
    assert _content_type_for("/api/media/a.unknown") == "application/octet-stream"
    assert _content_type_for(None) == "application/octet-stream"


# ── Production peer-factory adapter ────────────────────────────────────


async def test_aiolib_answerer_peer_set_remote_offer_and_create_answer():
    """The ``_AiolibAnswererPeer`` proxies the SDP exchange through the
    underlying ``PeerConnection``. ``create_answer`` returns inline-ICE
    SDP (the production wire protocol between SH and GFS doesn't
    trickle ICE separately)."""
    from socialhome.services.highlight_signaling_handler import (
        _AiolibAnswererPeer,
    )

    peer = _AiolibAnswererPeer([])
    await peer.set_remote_offer("v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\na=offer\r\n")
    sdp = await peer.create_answer()
    assert sdp  # non-empty SDP string
    assert "v=0" in sdp
    await peer.close()


async def test_aiolib_answerer_peer_add_ice_candidate_accepts_both_keys():
    """Production peer accepts both ``sdpMid`` (standard WebRTC key) and
    ``sdp_mid`` (snake-case from older signalling payloads)."""
    from socialhome.services.highlight_signaling_handler import (
        _AiolibAnswererPeer,
    )

    peer = _AiolibAnswererPeer([])
    # Either key shape is accepted; absence-of-error is the success
    # criterion (the stubbed PC just records the call).
    await peer.add_ice_candidate({"candidate": "candidate:1", "sdpMid": "0"})
    await peer.add_ice_candidate({"candidate": "candidate:2", "sdp_mid": "1"})
    # Empty candidate is a silent no-op rather than a crash.
    await peer.add_ice_candidate({"candidate": "", "sdpMid": "0"})
    await peer.close()


async def test_aiolib_answerer_peer_send_before_channel_raises():
    """``send`` before the viewer-opened DataChannel arrives raises
    ConnectionClosedError instead of silently dropping the frame."""
    import aiolibdatachannel as _rtc

    from socialhome.services.highlight_signaling_handler import (
        _AiolibAnswererPeer,
    )

    peer = _AiolibAnswererPeer([])
    with pytest.raises(_rtc.ConnectionClosedError):
        await peer.send(b"frame")
    await peer.close()


async def test_aiolib_answerer_peer_wait_open_and_send_round_trip():
    """When the viewer's DataChannel arrives, ``wait_open`` resolves and
    ``send`` proxies to it. Drives the test by manually pushing a
    channel into the stub PC's incoming-channels queue."""
    from socialhome.services.highlight_signaling_handler import (
        _AiolibAnswererPeer,
    )

    peer = _AiolibAnswererPeer([])
    # Stub PC exposes ``_incoming_queue`` — push a fake DC onto it so
    # ``_drain_incoming_channel`` latches it.
    fake_dc = await peer._pc.create_data_channel("public-viewer")  # type: ignore[union-attr]
    await peer._pc._incoming_queue.put(fake_dc)  # type: ignore[attr-defined]
    # Mark it open so wait_open can return.
    fake_dc.is_open = True
    fake_dc._open.set()
    await asyncio.wait_for(peer.wait_open(), timeout=1.0)
    await peer.send(b"frame")
    # The fake DC records sent frames on its ``sent`` list.
    assert b"frame" in fake_dc.sent
    await peer.close()


async def test_aiolib_answerer_peer_close_is_idempotent():
    """``close()`` swallows :class:`RTCError` from an already-closed PC
    so callers can safely ``await peer.close()`` multiple times — the
    handler does this in ``_serve``'s finally even when the PC was
    already torn down by the drain task."""
    from socialhome.services.highlight_signaling_handler import (
        _AiolibAnswererPeer,
    )

    peer = _AiolibAnswererPeer([])
    await peer.close()
    # Second close is a no-op, no exception escapes.
    await peer.close()


async def test_the_answerer_takes_the_ice_list_the_app_builds():
    """Regression: ``build_ice_servers`` makes ``{"urls": [...]}`` entries;
    the answerer passed ``str(["stun:…"])`` as the URL, libdatachannel
    refused the configuration (``rtcCreatePeerConnection: invalid
    argument``) and every public viewer fell back to the GFS relay."""
    ice = build_ice_servers(
        stun_url="stun:stun.example.org:3478",
        turn_url="turn:turn.example.org:3478",
        turn_user="u",
        turn_cred="c",
        turn_secret=None,
        turn_ttl_seconds=3600,
        hmac_user_id=None,
    )
    peer = _default_peer_factory(ice)
    try:
        config = peer._pc._config  # the fake records what it was given
        assert [(s.url, s.username, s.credential) for s in config.ice_servers] == [
            ("stun:stun.example.org:3478", None, None),
            ("turn:turn.example.org:3478", "u", "c"),
        ]
    finally:
        await peer.close()


# ─── Who shared it: the sealed author_profile frame ─────────────────────


def _viewer_key():
    sk = ec.generate_private_key(ec.SECP256R1())
    raw = sk.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return sk, b64url_encode(raw)


class _Pictures:
    async def get_user_picture(self, user_id):
        buf = io.BytesIO()
        Image.new("RGB", (300, 300), (40, 90, 160)).save(buf, "WEBP")
        return buf.getvalue(), "0123456789abcdef"


class _Protected:
    def __init__(self, ids) -> None:
        self.ids = ids

    def register_gate(self, gate):
        pass

    async def is_protected(self, user_id):
        return user_id in self.ids


async def _profile_frames(handler, highlight_id, vk):
    frames = [
        framing.decode(b)
        async for b in handler._iter_highlight_frames(
            highlight_id, viewer_key=parse_viewer_key(vk)
        )
    ]
    return frames, [f for f in frames if f.header["kind"] == "author_profile"]


async def test_a_viewer_with_a_key_gets_who_shared_it_sealed(
    repos, published_highlight, db
):
    highlight_id, _f1, _f2 = published_highlight
    await db.enqueue(
        "UPDATE users SET bio='Bread and bees.', picture_hash='0123456789abcdef'"
        " WHERE user_id='u1'"
    )
    handler, _ = _make_handler(repos)
    handler.attach_author_profiles(
        user_repo=SqliteUserRepo(db),
        picture_repo=_Pictures(),  # type: ignore[arg-type]
    )
    sk, vk = _viewer_key()
    frames, [prof] = await _profile_frames(handler, highlight_id, vk)
    # Right after highlight_meta, before any media.
    assert [f.header["kind"] for f in frames[:2]] == [
        "highlight_meta",
        "author_profile",
    ]
    assert b"Alice" not in prof.payload and b"Bread" not in prof.payload
    opened = open_profile(sk, prof.header, prof.payload, highlight_id=highlight_id)
    assert (opened["display_name"], opened["bio"]) == ("Alice", "Bread and bees.")
    pic = base64.b64decode(opened["picture"])
    assert pic[:4] == b"RIFF" and len(pic) <= 24 * 1024


async def test_no_key_no_profile_frame(repos, published_highlight, db):
    highlight_id, _f1, _f2 = published_highlight
    handler, _ = _make_handler(repos)
    handler.attach_author_profiles(
        user_repo=SqliteUserRepo(db),
        picture_repo=_Pictures(),  # type: ignore[arg-type]
    )
    _frames, profiles = await _profile_frames(handler, highlight_id, None)
    assert profiles == []


async def test_a_protected_author_is_named_only(repos, published_highlight, db):
    highlight_id, _f1, _f2 = published_highlight
    await db.enqueue("UPDATE users SET bio='x', picture_hash='0123456789abcdef'")
    handler, _ = _make_handler(repos)
    handler.attach_author_profiles(
        user_repo=SqliteUserRepo(db),
        picture_repo=_Pictures(),  # type: ignore[arg-type]
    )
    handler.attach_child_protection(_Protected({"u1"}))  # type: ignore[arg-type]
    sk, vk = _viewer_key()
    _frames, [prof] = await _profile_frames(handler, highlight_id, vk)
    opened = open_profile(sk, prof.header, prof.payload, highlight_id=highlight_id)
    assert opened == {"display_name": "Alice", "bio": None, "picture": None}


async def test_the_relay_carries_the_sealed_frame_for_the_relay_viewer(
    repos, published_highlight, db
):
    """The GFS pipes the relay bytes: it sees the sealed frame only."""
    highlight_id, _f1, _f2 = published_highlight
    handler, _ = _make_handler(repos)
    handler.attach_author_profiles(
        user_repo=SqliteUserRepo(db),
        picture_repo=_Pictures(),  # type: ignore[arg-type]
    )
    sess = _RecordingSession()
    handler._http_client = sess  # type: ignore[assignment]
    sk, vk = _viewer_key()
    await handler.handle_signal(
        {
            "kind": "relay_offer",
            "relay_id": "r-1",
            "highlight_id": highlight_id,
            "token": TOKEN,
            "vk": vk,
        }
    )
    await _drain_relay(handler)
    [(_url, _headers, body)] = sess.relay_calls
    assert b"Alice" not in body
    frames = _open_stream(_split(body), highlight_id, sk=sk)
    [prof] = [f for f in frames if f.header["kind"] == "author_profile"]
    assert (
        open_profile(sk, prof.header, prof.payload, highlight_id=highlight_id)[
            "display_name"
        ]
        == "Alice"
    )


async def test_resolve_media_path_accepts_the_stored_relative_form(repos):
    """Regression: uploads are stored as ``api/media/<name>`` (no leading
    slash); the ``/api/media/``-only check read every one as missing, so
    the public page answered "ended" for every new upload."""
    handler, _ = _make_handler(repos)
    got = handler._resolve_media_path("api/media/a.jpg")
    assert got is not None and got.name == "a.jpg"
    assert handler._resolve_media_path("/api/media/a.jpg") == got
    assert handler._resolve_media_path("api/media/../a.jpg") is None


# ─── End-to-end sealed stream (the GFS never sees the highlight) ─────────


async def test_the_direct_stream_is_sealed_end_to_end(repos, published_highlight):
    """Every frame after ``stream_seal`` is ciphertext: no frame id, caption
    or media byte is readable without the viewer's key and the link secret."""
    highlight_id, f1, f2 = published_highlight
    handler, peers = _make_handler(repos)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "highlight_id": highlight_id,
            "sdp": "v=0",
            "token": TOKEN,
            "vk": VK,
        }
    )
    await _drain(handler)
    wire = b"".join(peers[0].frames)
    for clear in (f1.encode(), f2.encode(), b"highlight_meta", b"frame_chunk"):
        assert clear not in wire
    kinds = {framing.decode(b).header["kind"] for b in peers[0].frames}
    assert kinds == {framing.KIND_STREAM_SEAL, framing.KIND_SEALED}


async def test_the_relay_stream_does_not_open_without_the_link_secret(
    repos, published_highlight
):
    """The GFS minted the token and pipes the relay, but the secret is
    derived from our identity and rides only in the link's fragment."""
    highlight_id, *_ = published_highlight
    handler, _ = _make_handler(repos)
    sess = _RecordingSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "relay_offer",
            "relay_id": "r-1",
            "highlight_id": highlight_id,
            "token": TOKEN,
            "vk": VK,
        }
    )
    await _drain_relay(handler)
    [(_url, _headers, body)] = sess.relay_calls
    first, second, *_ = [framing.decode(b) for b in _split(body)]
    guess = StreamOpener(
        VIEWER_SK, first.header, secret=bytes(32), highlight_id=highlight_id
    )
    with pytest.raises(InvalidTag):
        guess.open(second.payload)
    # Another token's secret doesn't open it either.
    other = StreamOpener(
        VIEWER_SK,
        first.header,
        secret=link_secret(SEED, highlight_id=highlight_id, token="tok-2"),
        highlight_id=highlight_id,
    )
    with pytest.raises(InvalidTag):
        other.open(second.payload)


@pytest.mark.parametrize("missing", ["vk", "token"])
async def test_an_offer_without_a_key_or_token_gets_no_stream(
    repos, published_highlight, missing
):
    highlight_id, *_ = published_highlight
    handler, peers = _make_handler(repos)
    frame = {
        "kind": "offer",
        "session_id": "s-1",
        "highlight_id": highlight_id,
        "sdp": "v=0",
        "token": TOKEN,
        "vk": VK,
    }
    del frame[missing]
    await handler.handle_signal(frame)
    await _drain(handler)
    assert peers == []


@pytest.mark.parametrize("missing", ["vk", "token"])
async def test_a_relay_without_a_key_or_token_streams_only_an_error(
    repos, published_highlight, missing
):
    highlight_id, *_ = published_highlight
    handler, _ = _make_handler(repos)
    sess = _RecordingSession()
    handler._http_client = sess  # type: ignore[assignment]
    frame = {
        "kind": "relay_offer",
        "relay_id": "r-1",
        "highlight_id": highlight_id,
        "token": TOKEN,
        "vk": VK,
    }
    del frame[missing]
    await handler.handle_signal(frame)
    await _drain_relay(handler)
    [(_url, _headers, body)] = sess.relay_calls
    assert [framing.decode(b).header for b in _split(body)] == [
        {"kind": "error", "error": "unsupported"}
    ]


# ─── Only our own live links are streamed (not any token a GFS forwards) ──


def _offer(highlight_id, token):
    return {
        "kind": "offer",
        "session_id": "s-1",
        "highlight_id": highlight_id,
        "sdp": "v=0",
        "token": token,
        "vk": VK,
    }


def _relay(highlight_id, token):
    return {
        "kind": "relay_offer",
        "relay_id": "r-1",
        "highlight_id": highlight_id,
        "token": token,
        "vk": VK,
    }


async def _relay_headers(handler, frame):
    sess = _RecordingSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(frame)
    await _drain_relay(handler)
    [(_url, _headers, body)] = sess.relay_calls
    return [framing.decode(b).header for b in _split(body)]


async def _revoke(repos, highlight_id, token=TOKEN):
    await repos["highlights"].remove_public_token(highlight_id, token_digest(token))


@pytest.mark.parametrize("case", ["unknown", "revoked"])
async def test_an_offer_for_a_token_we_did_not_mint_or_revoked_gets_no_stream(
    repos, published_highlight, case
):
    """Revocation is ours to enforce: a GFS that forwards a token we never
    minted, or one we revoked, gets no peer and no stream."""
    highlight_id, *_ = published_highlight
    handler, peers = _make_handler(repos)
    token = "tok-forged" if case == "unknown" else TOKEN
    if case == "revoked":
        await _revoke(repos, highlight_id)
    await handler.handle_signal(_offer(highlight_id, token))
    await _drain(handler)
    assert peers == []


@pytest.mark.parametrize("case", ["unknown", "revoked"])
async def test_a_relay_for_a_token_we_did_not_mint_or_revoked_gets_only_an_error(
    repos, published_highlight, case
):
    highlight_id, *_ = published_highlight
    handler, _ = _make_handler(repos)
    token = "tok-forged" if case == "unknown" else TOKEN
    if case == "revoked":
        await _revoke(repos, highlight_id)
    assert await _relay_headers(handler, _relay(highlight_id, token)) == [
        {"kind": "error", "error": "expired"}
    ]


async def test_an_oversized_token_is_refused_before_any_lookup(
    repos, published_highlight
):
    highlight_id, *_ = published_highlight
    handler, peers = _make_handler(repos)
    huge = "x" * (MAX_TOKEN_LEN + 1)
    await handler.handle_signal(_offer(highlight_id, huge))
    await _drain(handler)
    assert peers == []
    assert await _relay_headers(handler, _relay(highlight_id, huge)) == [
        {"kind": "error", "error": "unsupported"}
    ]


async def test_an_expired_highlight_is_not_streamed_even_while_published(
    repos, published_highlight, db
):
    """The retention scheduler may not have pruned it yet."""
    highlight_id, *_ = published_highlight
    await db.enqueue(
        "UPDATE highlights SET expires_at=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), highlight_id),
    )
    handler, peers = _make_handler(repos)
    await handler.handle_signal(_offer(highlight_id, TOKEN))
    await _drain(handler)
    assert peers == []
    assert await _relay_headers(handler, _relay(highlight_id, TOKEN)) == [
        {"kind": "error", "error": "expired"}
    ]


async def test_a_token_revoked_mid_stream_setup_is_rechecked_before_streaming(
    repos, published_highlight
):
    """The sealed stream re-checks the token itself, so a revoke between
    the offer and the open channel still sends nothing."""
    highlight_id, *_ = published_highlight
    handler, _ = _make_handler(repos)
    await _revoke(repos, highlight_id)
    frames = [
        f
        async for f in handler._iter_sealed_stream(
            highlight_id, token=TOKEN, viewer_key=parse_viewer_key(VK)
        )
    ]
    assert [framing.decode(b).header for b in frames] == [
        {"kind": "error", "error": "expired"}
    ]
