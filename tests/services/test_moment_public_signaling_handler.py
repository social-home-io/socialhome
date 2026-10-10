"""Tests for :class:`MomentPublicSignalingHandler` (§Momentum-public).

Mirrors ``test_highlight_signaling_handler.py``: a stub peer factory
exercises the framing + orchestration without an
:mod:`aiolibdatachannel` runtime. The privacy invariant — only
``is_public=1`` moments ever reach the stream — has a dedicated test.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.moment import Moment
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.services import highlight_public_framing as framing
from socialhome.services.moment_public_signaling_handler import (
    MAX_CONCURRENT_VIEWERS_PER_USER,
    MomentPublicSignalingHandler,
)


# ─── Stub peer ───────────────────────────────────────────────────────────


class _StubPeer:
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
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _moment(
    *,
    id: str,
    author: str = "u1",
    is_public: bool = True,
    media_url: str | None = None,
    media_type: str | None = None,
    created_offset_min: int = 0,
) -> Moment:
    return Moment(
        id=id,
        author_user_id=author,
        content=f"content-{id}",
        media_url=media_url,
        media_type=media_type,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id="self",
        created_at=(
            datetime.now(timezone.utc) + timedelta(minutes=created_offset_min)
        ).isoformat(),
        expires_at=_expires(),
        is_public=is_public,
    )


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
        "moments": SqliteMomentRepo(db),
        "gfs": SqliteGfsConnectionRepo(db),
        "media_dir": str(media_dir),
    }


@pytest.fixture
async def public_moments(repos):
    """Two public moments: one text-only, one with image media."""
    media_dir = repos["media_dir"]
    open(f"{media_dir}/a.jpg", "wb").write(b"A" * 200_000)
    await repos["moments"].save(_moment(id="m-text", created_offset_min=-1))
    await repos["moments"].save(
        _moment(
            id="m-media",
            media_url="/api/media/a.jpg",
            media_type="image",
            created_offset_min=-2,
        )
    )
    return "m-text", "m-media"


def _make_handler(repos, *, peer_factory=None):
    last_peer: list[_StubPeer] = []

    def _factory(_ice):
        peer = _StubPeer()
        last_peer.append(peer)
        return peer

    handler = MomentPublicSignalingHandler(
        repos["moments"],
        repos["gfs"],
        media_dir=repos["media_dir"],
        peer_factory=peer_factory or _factory,
    )
    handler.attach_session(_StubSession())
    handler.attach_identity(own_instance_id="inst-self", signing_key=b"\x00" * 32)
    handler.attach_ice_servers([{"urls": ["stun:stun.example"]}])
    return handler, last_peer


async def _drain(handler) -> None:
    tasks = [s.task for s in handler._sessions.values() if s.task is not None]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _drain_relay(handler) -> None:
    tasks = list(handler._relay_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ─── Happy path ─────────────────────────────────────────────────────────


async def test_offer_streams_meta_then_chunks_then_stream_end(repos, public_moments):
    m_text, m_media = public_moments
    handler, peers = _make_handler(repos)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    assert peers, "peer factory wasn't called"
    peer = peers[0]
    assert peer.remote_offer == "v=0"
    assert peer.closed
    decoded = [framing.decode(b) for b in peer.frames]
    kinds = [f.header["kind"] for f in decoded]
    assert kinds[0] == framing.KIND_MOMENT_INDEX_META
    assert kinds[-1] == framing.KIND_STREAM_END
    # Manifest lists both moments, newest-first.
    meta = decoded[0].header["moments"]
    assert [m["id"] for m in meta] == [m_text, m_media]
    # Only the media moment yields chunks; the text moment doesn't.
    chunks = [f for f in decoded if f.header["kind"] == framing.KIND_FRAME_CHUNK]
    assert {c.header["frame_id"] for c in chunks} == {m_media}


async def test_meta_carries_byte_length_and_content_type_for_media(
    repos, public_moments
):
    m_text, m_media = public_moments
    handler, peers = _make_handler(repos)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    meta = framing.decode(peers[0].frames[0]).header["moments"]
    by_id = {m["id"]: m for m in meta}
    assert by_id[m_text]["has_media"] is False
    assert "byte_length" not in by_id[m_text]
    assert by_id[m_media]["has_media"] is True
    assert by_id[m_media]["byte_length"] == 200_000
    assert by_id[m_media]["content_type"] == "image/jpeg"
    assert by_id[m_media]["media_frame_id"] == m_media


async def test_empty_index_still_streams_meta_and_end(repos):
    """A user with no public moments yields meta([]) + stream_end — never
    an error frame — so the guest sees an empty index."""
    handler, peers = _make_handler(repos)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    decoded = [framing.decode(b) for b in peers[0].frames]
    kinds = [f.header["kind"] for f in decoded]
    assert kinds == [framing.KIND_MOMENT_INDEX_META, framing.KIND_STREAM_END]
    assert decoded[0].header["moments"] == []


async def test_private_moment_is_never_in_the_stream(repos):
    """PRIVACY INVARIANT: an is_public=0 moment is never streamed."""
    await repos["moments"].save(_moment(id="m-pub", is_public=True))
    await repos["moments"].save(_moment(id="m-priv", is_public=False))
    handler, peers = _make_handler(repos)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    decoded = [framing.decode(b) for b in peers[0].frames]
    meta = decoded[0].header["moments"]
    ids = {m["id"] for m in meta}
    assert "m-pub" in ids
    assert "m-priv" not in ids
    # And no chunk carries the private moment's id either.
    chunk_ids = {
        f.header["frame_id"]
        for f in decoded
        if f.header["kind"] == framing.KIND_FRAME_CHUNK
    }
    assert "m-priv" not in chunk_ids


async def test_handler_posts_signed_answer_to_correct_gfs(repos, public_moments):
    handler, _peers = _make_handler(repos)
    sess = _StubSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    assert sess.posts
    url, body = sess.posts[0]
    assert url == "https://gfs.example/gfs/moment_rtc/answer"
    assert body["session_id"] == "s-1"
    assert body["instance_id"] == "inst-self"
    assert "signature" in body


# ─── GFS-relay fallback ──────────────────────────────────────────────────


async def test_relay_offer_posts_framed_stream_with_signed_headers(
    repos, public_moments
):
    m_text, m_media = public_moments
    handler, _peers = _make_handler(repos)
    sess = _RecordingSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "relay_offer",
            "relay_id": "r-1",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
        }
    )
    await _drain_relay(handler)

    assert len(sess.relay_calls) == 1
    url, headers, body = sess.relay_calls[0]
    assert url == "https://gfs.example/gfs/moment_rtc/relay-stream/r-1"
    assert headers["X-SH-Instance"] == "inst-self"
    assert headers["X-SH-Signature"]

    import struct

    rest = body
    decoded = []
    while rest:
        hlen = struct.unpack(">I", rest[:4])[0]
        plen = struct.unpack(">I", rest[4 + hlen : 4 + hlen + 4])[0]
        total = 4 + hlen + 4 + plen
        decoded.append(framing.decode(rest[:total]))
        rest = rest[total:]
    kinds = [d.header["kind"] for d in decoded]
    assert kinds[0] == framing.KIND_MOMENT_INDEX_META
    assert kinds[-1] == framing.KIND_STREAM_END


async def test_relay_offer_signature_verifies_against_instance_key(repos):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from socialhome.global_server.admin_service import verify_report_signature

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
    assert not verify_report_signature(
        {"instance_id": "inst-self", "relay_id": "r-OTHER", "ts": ts},
        headers["X-SH-Signature"],
        pk_hex,
    )


async def test_relay_offer_missing_fields_is_no_op(repos):
    handler, _ = _make_handler(repos)
    sess = _RecordingSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal({"kind": "relay_offer", "relay_id": ""})
    await _drain_relay(handler)
    assert sess.relay_calls == []


async def test_relay_offer_inactive_gfs_posts_nothing(repos, public_moments):
    """If the GFS connection is suspended, the relay URL won't resolve."""
    await repos["gfs"]._db.enqueue(
        "UPDATE gfs_connections SET status='suspended' WHERE id='gfs-abc'"
    )
    handler, _ = _make_handler(repos)
    sess = _RecordingSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "relay_offer",
            "relay_id": "r-1",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
        }
    )
    await _drain_relay(handler)
    assert sess.relay_calls == []


async def test_stop_cancels_in_flight_relay_tasks(repos, public_moments):
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
            await release.wait()
            return _StubResp(200, {"status": "ok"})

        async def __aexit__(self, *a):
            return False

    handler._http_client = _BlockingSession()  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "relay_offer",
            "relay_id": "r-1",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
        }
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert handler._relay_tasks
    await handler.stop()
    assert handler._relay_tasks == set()
    release.set()


# ─── ICE plumbing ───────────────────────────────────────────────────────


async def test_ice_frame_is_forwarded_to_peer(repos, public_moments):
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
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    await asyncio.sleep(0)
    await handler.handle_signal(
        {
            "kind": "ice",
            "session_id": "s-1",
            "candidate": {"candidate": "x", "sdpMid": "0"},
        }
    )
    assert peers[0].candidates == [{"candidate": "x", "sdpMid": "0"}]
    open_event.set()
    await _drain(handler)


async def test_ice_for_unknown_session_is_no_op(repos):
    handler, _ = _make_handler(repos)
    await handler.handle_signal(
        {"kind": "ice", "session_id": "missing", "candidate": {"x": 1}}
    )


async def test_ice_with_non_dict_candidate_is_dropped(repos):
    handler, _ = _make_handler(repos)
    await handler.handle_signal(
        {"kind": "ice", "session_id": "s-1", "candidate": "not-a-dict"}
    )


# ─── Stop / cleanup + caps ────────────────────────────────────────────────


async def test_stop_cancels_in_flight_sessions(repos, public_moments):
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
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    await asyncio.sleep(0)
    await handler.stop()
    assert peers[0].closed
    assert "s-1" not in handler._sessions


async def test_per_user_cap_rejects_extra_offer(repos, public_moments):
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
    for i in range(MAX_CONCURRENT_VIEWERS_PER_USER):
        await handler.handle_signal(
            {
                "kind": "offer",
                "session_id": f"s-{i}",
                "user_id": "u1",
                "gfs_id": "gfs-abc",
                "sdp": "v=0",
            }
        )
    assert len(peers) == MAX_CONCURRENT_VIEWERS_PER_USER
    before = len(peers)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-overflow",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    assert len(peers) == before
    open_event.set()
    await _drain(handler)


async def test_per_instance_cap_takes_priority(repos, public_moments):
    from socialhome.services import moment_public_signaling_handler as mod

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
    saved = mod.MAX_CONCURRENT_VIEWERS_PER_INSTANCE
    mod.MAX_CONCURRENT_VIEWERS_PER_INSTANCE = 1
    try:
        await handler.handle_signal(
            {
                "kind": "offer",
                "session_id": "a",
                "user_id": "u1",
                "gfs_id": "gfs-abc",
                "sdp": "v=0",
            }
        )
        before = len(peers)
        await handler.handle_signal(
            {
                "kind": "offer",
                "session_id": "b",
                "user_id": "u1",
                "gfs_id": "gfs-abc",
                "sdp": "v=0",
            }
        )
        assert len(peers) == before
    finally:
        mod.MAX_CONCURRENT_VIEWERS_PER_INSTANCE = saved
        open_event.set()
        await _drain(handler)


# ─── Extra branch coverage ───────────────────────────────────────────────


async def test_handle_signal_unknown_kind_is_no_op(repos):
    handler, peers = _make_handler(repos)
    await handler.handle_signal({"kind": "weather", "session_id": "x"})
    assert peers == []


async def test_offer_missing_fields_is_no_op(repos):
    handler, peers = _make_handler(repos)
    await handler.handle_signal({"kind": "offer", "session_id": ""})
    assert peers == []


async def test_duplicate_session_id_is_dropped(repos, public_moments):
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
    for _ in range(2):
        await handler.handle_signal(
            {
                "kind": "offer",
                "session_id": "s-1",
                "user_id": "u1",
                "gfs_id": "gfs-abc",
                "sdp": "v=0",
            }
        )
    assert len(peers) == 1
    open_event.set()
    await _drain(handler)


async def test_post_answer_fails_when_gfs_inactive(repos, public_moments):
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
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    assert sess.posts == []


async def test_handler_requires_identity_attach_to_sign(repos):
    handler = MomentPublicSignalingHandler(
        repos["moments"],
        repos["gfs"],
        media_dir="/tmp",
    )
    with pytest.raises(RuntimeError):
        handler._sign({"a": 1})
    with pytest.raises(RuntimeError):
        handler._require_instance_id()
    with pytest.raises(RuntimeError):
        handler._relay_auth_headers("r-1")


async def test_resolve_media_path_rejects_traversal(repos):
    handler, _ = _make_handler(repos)
    assert handler._resolve_media_path("/api/media/../etc/passwd") is None
    assert handler._resolve_media_path("/api/media/.hidden") is None
    assert handler._resolve_media_path("not-a-media-url") is None
    assert handler._resolve_media_path(None) is None


async def test_missing_media_file_yields_error_frame(repos):
    """A public moment whose media bytes are gone yields an error frame
    in place of its chunks (the meta + stream_end still bracket it)."""
    await repos["moments"].save(
        _moment(id="m-gone", media_url="/api/media/missing.jpg", media_type="image")
    )
    handler, peers = _make_handler(repos)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    decoded = [framing.decode(b) for b in peers[0].frames]
    kinds = [f.header["kind"] for f in decoded]
    assert framing.KIND_ERROR in kinds
    assert kinds[0] == framing.KIND_MOMENT_INDEX_META
    assert kinds[-1] == framing.KIND_STREAM_END


# ─── Error / edge branch coverage ─────────────────────────────────────────


class _ErrSession(_StubSession):
    """Answer POST returns HTTP 500 so the error-log branch is hit."""

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(500, {"error": "down"})


class _ErrRelayPost:
    def __init__(self, data):
        self._data = data

    async def __aenter__(self):
        if hasattr(self._data, "__aiter__"):
            async for _chunk in self._data:
                pass
        return _StubResp(500, {"error": "down"})

    async def __aexit__(self, *a):
        return False


class _ErrRelaySession:
    """Relay streaming POST returns HTTP 500 → error-log branch."""

    def post(self, url, *, data=None, headers=None, **_kw):
        return _ErrRelayPost(data)


async def test_post_answer_logs_on_http_error(repos, public_moments):
    handler, _ = _make_handler(repos)
    handler._http_client = _ErrSession()  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    assert handler._sessions == {}


async def test_relay_offer_logs_on_http_error(repos, public_moments):
    handler, _ = _make_handler(repos)
    handler._http_client = _ErrRelaySession()  # type: ignore[assignment]
    await handler.handle_signal(
        {"kind": "relay_offer", "relay_id": "r-1", "user_id": "u1", "gfs_id": "gfs-abc"}
    )
    await _drain_relay(handler)
    # No exception escaped; relay task drained cleanly.
    assert handler._relay_tasks == set()


async def test_relay_offer_unknown_gfs_posts_nothing(repos, public_moments):
    handler, _ = _make_handler(repos)
    sess = _RecordingSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {"kind": "relay_offer", "relay_id": "r-1", "user_id": "u1", "gfs_id": "nope"}
    )
    await _drain_relay(handler)
    assert sess.relay_calls == []


async def test_ice_with_failing_peer_is_swallowed(repos, public_moments):
    open_event = asyncio.Event()

    class _SlowPeer(_StubPeer):
        async def wait_open(self) -> None:
            await open_event.wait()

        async def add_ice_candidate(self, candidate: dict) -> None:
            raise RuntimeError("bad candidate")

    def _factory(_ice):
        return _SlowPeer()

    handler, _ = _make_handler(repos, peer_factory=_factory)
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    await asyncio.sleep(0)
    # Must not raise despite the peer rejecting the candidate.
    await handler.handle_signal(
        {"kind": "ice", "session_id": "s-1", "candidate": {"candidate": "x"}}
    )
    open_event.set()
    await _drain(handler)


class _ProtectedCp:
    """§CP.R stub: ``u1`` is a protected account."""

    def register_gate(self, gate):
        pass

    async def is_restricted(self, user_id, capability):
        return user_id == "u1"


async def test_protected_accounts_public_moments_are_never_streamed(
    repos, public_moments
):
    """Shared publicly before protection — never served after it, whatever
    the connection server still lists."""
    handler, peers = _make_handler(repos)
    handler.attach_child_protection(_ProtectedCp())
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-1",
            "user_id": "u1",
            "gfs_id": "gfs-abc",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    decoded = [framing.decode(b) for b in peers[0].frames]
    assert [f.header["kind"] for f in decoded] == [
        framing.KIND_MOMENT_INDEX_META,
        framing.KIND_STREAM_END,
    ]
    assert decoded[0].header["moments"] == []


# ─── Which GFS to answer: the connection the frame arrived on ────────────


async def _second_gfs(repos, *, conn_id: str, server_id: str, url: str) -> None:
    """A connection whose local row id (a uuid in production) differs from
    the server's public id — the shape every real pairing has."""
    await repos["gfs"]._db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at)"
        " VALUES(?, ?, 'GFS', 'ee', ?, 'active', datetime('now'))",
        (conn_id, server_id, url),
    )


async def test_the_answer_goes_to_the_connection_the_frame_arrived_on(
    repos, public_moments
):
    """Regression: the GFS stamps its PUBLIC id into ``gfs_id``, which is
    not the household's connection row id — the old lookup by row id found
    nothing and the answer was never sent. The supervisor binds the
    connection the frame arrived on; that is where the answer goes, whatever
    id (an alias, a pre-rebind id) the frame names."""
    await _second_gfs(
        repos, conn_id="conn-uuid", server_id="gfs-social-home", url="https://g2.test"
    )
    handler, _peers = _make_handler(repos)
    sess = _StubSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-bound",
            "user_id": "u1",
            "gfs_id": "gfs-1",  # an alias the server still answers to
            "sdp": "v=0",
        },
        gfs_id="conn-uuid",
    )
    await _drain(handler)
    assert [url for url, _ in sess.posts] == ["https://g2.test/gfs/moment_rtc/answer"]


async def test_an_unbound_frame_resolves_the_servers_public_id(repos, public_moments):
    await _second_gfs(
        repos, conn_id="conn-uuid", server_id="gfs-social-home", url="https://g2.test"
    )
    handler, _peers = _make_handler(repos)
    sess = _StubSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-unbound",
            "user_id": "u1",
            "gfs_id": "gfs-social-home",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    assert [url for url, _ in sess.posts] == ["https://g2.test/gfs/moment_rtc/answer"]


async def test_a_relay_offer_streams_to_the_bound_connection(repos, public_moments):
    await _second_gfs(
        repos, conn_id="conn-uuid", server_id="gfs-social-home", url="https://g2.test"
    )
    handler, _ = _make_handler(repos)
    sess = _RecordingSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {"kind": "relay_offer", "relay_id": "r-9", "user_id": "u1", "gfs_id": "x"},
        gfs_id="conn-uuid",
    )
    await _drain_relay(handler)
    assert [c[0] for c in sess.relay_calls] == [
        "https://g2.test/gfs/moment_rtc/relay-stream/r-9"
    ]


async def test_a_frame_naming_no_known_server_is_dropped(repos, public_moments):
    handler, _ = _make_handler(repos)
    sess = _StubSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-x",
            "user_id": "u1",
            "gfs_id": "gfs-unknown",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    assert sess.posts == []
    assert "s-x" not in handler._sessions


async def test_an_unbound_frame_naming_a_replaced_id_still_resolves(
    repos, public_moments
):
    """L5: a frame naming an id the server signed it ``replaces`` (a household
    not yet rebound, or an alias) resolves through ``known_instance_ids``."""
    await _second_gfs(
        repos, conn_id="conn-uuid", server_id="gfs-social-home", url="https://g2.test"
    )
    handler, _peers = _make_handler(repos)
    handler.attach_known_ids(
        lambda c: (
            frozenset({c.gfs_instance_id})
            | ({"gfs-1"} if c.id == "conn-uuid" else set())
        )
    )
    sess = _StubSession()
    handler._http_client = sess  # type: ignore[assignment]
    await handler.handle_signal(
        {
            "kind": "offer",
            "session_id": "s-alias",
            "user_id": "u1",
            "gfs_id": "gfs-1",
            "sdp": "v=0",
        }
    )
    await _drain(handler)
    assert [url for url, _ in sess.posts] == ["https://g2.test/gfs/moment_rtc/answer"]
