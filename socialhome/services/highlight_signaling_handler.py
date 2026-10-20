"""Author-side signalling + DataChannel streamer for §highlights_public.

Receives ``{type:"highlight_signal", ...}`` frames from
:class:`GfsWsClient` (the existing SH↔GFS WebSocket). On a fresh
``offer`` it spins up an :mod:`aiolibdatachannel` answerer
PeerConnection, posts the SDP answer back to the GFS via the signed
``/gfs/highlight_rtc/answer`` endpoint, applies remote ICE candidates as
they arrive, and once the DataChannel opens it streams the highlight
metadata + frame bytes using the framing protocol in
:mod:`socialhome.services.highlight_public_framing` — every frame sealed
end to end to the viewer's key and the link secret
(:mod:`socialhome.services.highlight_public_seal`), on both transports.

The PeerConnection itself is built by an injected factory so tests can
swap in a stub without dragging the real
:mod:`aiolibdatachannel` runtime through the unit-test path. The
default factory matches :class:`SyncRtcSession`'s configuration so we
get the same ICE / SCTP plumbing we already exercise for §4.2.3 sync.

Lifecycle is the standard ``_stop: asyncio.Event`` pattern from the
project's other schedulers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pathlib
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import aiofiles
import aiofiles.os
import aiohttp
import aiolibdatachannel as rtc
from cryptography.hazmat.primitives.asymmetric import ec

from ..crypto import b64url_encode, sign_ed25519
from ..media.profile_picture import public_profile_picture
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.highlight_repo import AbstractHighlightRepo
from ..repositories.profile_picture_repo import AbstractProfilePictureRepo
from ..repositories.user_repo import AbstractUserRepo
from ..webrtc_ice import rtc_configuration
from . import highlight_public_framing as framing
from .highlight_public_profile import parse_viewer_key, seal_profile
from .highlight_public_seal import StreamSealer, link_secret
from .inbound_media_store import MEDIA_REF_PREFIX, local_media_ref, media_file_path
from .protection_gate import ProtectionGateMixin

log = logging.getLogger(__name__)


#: Per-author cap on concurrent public viewer sessions. The
#: 11th + viewer receives an :func:`error_frame` and the channel
#: closes immediately.
MAX_CONCURRENT_VIEWERS_PER_INSTANCE: int = 50

#: Per-highlight cap, applied on top of the per-instance cap. A single
#: viral highlight can't drown out other publications.
MAX_CONCURRENT_VIEWERS_PER_HIGHLIGHT: int = 10


# ─── Peer abstraction ──────────────────────────────────────────────────


@runtime_checkable
class _AnswererPeer(Protocol):
    """Protocol the signalling handler expects from a PeerConnection.

    Tests inject a stub that records calls and exposes hooks; the real
    implementation in :func:`_default_peer_factory` wraps an
    ``aiolibdatachannel.PeerConnection`` with the answerer role.
    """

    async def set_remote_offer(self, sdp: str) -> None: ...
    async def create_answer(self) -> str: ...
    async def add_ice_candidate(self, candidate: dict) -> None: ...
    async def wait_open(self) -> None: ...
    async def send(self, frame_bytes: bytes) -> None: ...
    async def close(self) -> None: ...


PeerFactory = Callable[[list[dict[str, Any]]], _AnswererPeer]


# ─── Session state ─────────────────────────────────────────────────────


@dataclass(slots=True)
class _Session:
    session_id: str
    highlight_id: str
    peer: _AnswererPeer
    #: The viewer's ephemeral key: the whole stream is sealed to it.
    viewer_key: ec.EllipticCurvePublicKey | None = None
    #: The share token the viewer opened (the link secret derives from it).
    token: str = ""
    task: asyncio.Task[None] | None = None
    pending_ice: list[dict] = field(default_factory=list)


# ─── Handler ───────────────────────────────────────────────────────────


class HighlightSignalingHandler(ProtectionGateMixin):
    """Drives the author-side half of §highlights_public WebRTC."""

    __slots__ = (
        "_child_protection",
        "_gfs_repo",
        "_highlights",
        "_http_client",
        "_ice_servers",
        "_lock",
        "_media_dir",
        "_own_instance_id",
        "_peer_factory",
        "_pictures",
        "_relay_tasks",
        "_sessions",
        "_signing_key",
        "_users",
    )

    def __init__(
        self,
        highlight_repo: AbstractHighlightRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        *,
        media_dir: str,
        peer_factory: PeerFactory | None = None,
        ice_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        self._highlights = highlight_repo
        self._gfs_repo = gfs_repo
        self._http_client: aiohttp.ClientSession | None = None
        self._signing_key: bytes | None = None
        self._own_instance_id: str = ""
        self._peer_factory = peer_factory or _default_peer_factory
        self._ice_servers: list[dict[str, Any]] = list(ice_servers or [])
        self._media_dir = media_dir
        self._sessions: dict[str, _Session] = {}
        #: In-flight GFS-relay fallback streams (no PeerConnection — the
        #: framed bytes go out over a signed streaming POST instead).
        self._relay_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._child_protection = None
        #: Where the author's name, bio and picture come from (``None``:
        #: no ``author_profile`` frame is sent).
        self._users: AbstractUserRepo | None = None
        self._pictures: AbstractProfilePictureRepo | None = None

    # ── Late-bound wiring ────────────────────────────────────────────────

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    def attach_identity(
        self,
        *,
        own_instance_id: str,
        signing_key: bytes,
    ) -> None:
        self._own_instance_id = own_instance_id
        self._signing_key = signing_key

    def attach_ice_servers(self, servers: list[dict[str, Any]]) -> None:
        self._ice_servers = list(servers)

    def attach_author_profiles(
        self,
        *,
        user_repo: AbstractUserRepo,
        picture_repo: AbstractProfilePictureRepo,
    ) -> None:
        """Wire where the sealed ``author_profile`` frame reads from."""
        self._users = user_repo
        self._pictures = picture_repo

    # ── Frame entry point ───────────────────────────────────────────────

    async def handle_signal(self, frame: dict) -> None:
        """Dispatch a single ``highlight_signal`` frame from the WS."""
        kind = str(frame.get("kind") or "")
        if kind == "offer":
            await self._on_offer(frame)
        elif kind == "ice":
            await self._on_ice(frame)
        elif kind == "relay_offer":
            await self._on_relay_offer(frame)
        else:
            log.debug("highlight_signal: unknown kind %r — dropped", kind)

    # ── Offer path ──────────────────────────────────────────────────────

    async def _on_offer(self, frame: dict) -> None:
        session_id = str(frame.get("session_id") or "")
        highlight_id = str(frame.get("highlight_id") or "")
        sdp_offer = str(frame.get("sdp") or "")
        if not (session_id and highlight_id and sdp_offer):
            log.debug("highlight_signal offer: missing fields — dropped")
            return
        if session_id in self._sessions:
            log.debug("highlight_signal offer: duplicate session_id — dropped")
            return

        # Reject viewer if we've hit the caps. We still need a peer to
        # send the error frame, but we open it just long enough.
        per_highlight = sum(
            1 for s in self._sessions.values() if s.highlight_id == highlight_id
        )
        if (
            len(self._sessions) >= MAX_CONCURRENT_VIEWERS_PER_INSTANCE
            or per_highlight >= MAX_CONCURRENT_VIEWERS_PER_HIGHLIGHT
        ):
            log.warning(
                "highlight_signal: rejecting offer for highlight=%s (cap reached)",
                highlight_id,
            )
            await self._reject_with_error(session_id, sdp_offer, "backpressure")
            return

        highlight = await self._highlights.get_highlight(highlight_id)
        if highlight is None or highlight.public_gfs_id is None:
            await self._reject_with_error(session_id, sdp_offer, "expired")
            return
        viewer_key = parse_viewer_key(frame.get("vk"))
        token = str(frame.get("token") or "")
        if viewer_key is None or not token:
            # Nothing to seal to — never stream a highlight in the clear.
            await self._reject_with_error(session_id, sdp_offer, "unsupported")
            return

        peer = self._peer_factory(self._ice_servers)
        async with self._lock:
            self._sessions[session_id] = _Session(
                session_id=session_id,
                highlight_id=highlight_id,
                peer=peer,
                viewer_key=viewer_key,
                token=token,
            )
        task = asyncio.create_task(
            self._serve(session_id, sdp_offer),
            name=f"highlight-signal[{session_id}]",
        )
        self._sessions[session_id].task = task

    async def _on_ice(self, frame: dict) -> None:
        session_id = str(frame.get("session_id") or "")
        candidate = frame.get("candidate")
        if not session_id or not isinstance(candidate, dict):
            return
        sess = self._sessions.get(session_id)
        if sess is None:
            return
        try:
            await sess.peer.add_ice_candidate(candidate)
        except Exception as exc:  # defensive — bad candidates shouldn't kill
            log.debug("highlight_signal ice: %s", exc, exc_info=True)

    # ── Streaming ──────────────────────────────────────────────────────

    async def _serve(self, session_id: str, sdp_offer: str) -> None:
        sess = self._sessions[session_id]
        peer = sess.peer
        try:
            await peer.set_remote_offer(sdp_offer)
            answer_sdp = await peer.create_answer()
            await self._post_answer(session_id, answer_sdp)
            await peer.wait_open()
            await self._stream_highlight(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # defensive
            log.warning("highlight_signal serve %s: %s", session_id, exc, exc_info=True)
        finally:
            try:
                await peer.close()
            except Exception:  # defensive
                log.debug("highlight_signal: peer close failed", exc_info=True)
            async with self._lock:
                self._sessions.pop(session_id, None)

    async def _stream_highlight(self, session_id: str) -> None:
        """WebRTC sink: drive the shared frame generator onto the peer."""
        sess = self._sessions[session_id]
        async for chunk in self._iter_sealed_stream(
            sess.highlight_id, token=sess.token, viewer_key=sess.viewer_key
        ):
            await sess.peer.send(chunk)

    async def _iter_sealed_stream(
        self,
        highlight_id: str,
        *,
        token: str,
        viewer_key: ec.EllipticCurvePublicKey | None,
    ) -> AsyncIterator[bytes]:
        """What either transport sends: ``stream_seal``, then every frame of
        :meth:`_iter_highlight_frames` sealed to the viewer's key and the
        link's secret. Without a key, a token or our identity: one
        plaintext ``unsupported`` error and nothing else."""
        if viewer_key is None or not token or self._signing_key is None:
            yield framing.error_frame("unsupported")
            return
        sealer = StreamSealer(
            viewer_key,
            secret=link_secret(
                self._signing_key, highlight_id=highlight_id, token=token
            ),
            highlight_id=highlight_id,
        )
        async for chunk in sealer.wrap(
            self._iter_highlight_frames(highlight_id, viewer_key=viewer_key)
        ):
            yield chunk

    async def _iter_highlight_frames(
        self,
        highlight_id: str,
        *,
        viewer_key: ec.EllipticCurvePublicKey | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield the inner highlight frames (meta → chunks → end), before
        sealing.

        Single source of truth for both transports (through
        :meth:`_iter_sealed_stream`): the WebRTC path sends each sealed
        frame on the DataChannel; the GFS-relay fallback pipes the
        identical bytes into the signed streaming POST, so the viewer's
        decoder is transport-blind.
        """
        highlight = await self._highlights.get_highlight(highlight_id)
        if highlight is None:
            yield framing.error_frame("expired")
            return
        frames = await self._highlights.list_frames(highlight_id)
        manifest = []
        for f in frames:
            byte_length = await self._media_size(f.media_url)
            manifest.append(
                {
                    "frame_id": f.id,
                    "sequence": f.sequence,
                    "content_type": _content_type_for(f.media_url),
                    "byte_length": byte_length,
                    "caption_text": f.caption_text,
                    "caption_emoji": f.caption_emoji,
                    "duration_ms": f.duration_ms,
                }
            )
        highlight_dict = {
            "id": highlight.id,
            "author_user_id": highlight.author_user_id,
            "highlight_date": highlight.highlight_date,
            "expires_at": highlight.expires_at,
        }
        yield framing.highlight_meta(highlight_dict, manifest)
        if viewer_key is not None:
            profile = await self._author_profile(highlight.author_user_id)
            if profile is not None:
                fields, ciphertext = seal_profile(
                    viewer_key, highlight_id=highlight.id, profile=profile
                )
                yield framing.author_profile(fields, ciphertext)
        for f in frames:
            async for chunk in self._iter_frame_chunks(f):
                yield chunk
        yield framing.stream_end()

    async def _author_profile(self, user_id: str) -> dict | None:
        """The author's public profile for the page: name, bio and a small
        picture (base64 WebP) — the name only for a protected account
        (§CP.R; it can't publish a highlight, but never leak one that
        slipped through). ``None`` when unwired or unknown."""
        if self._users is None:
            return None
        user = await self._users.get_by_user_id(user_id)
        if user is None:
            return None
        name = user.display_name or user.username
        if await self._is_protected(user_id):
            return {"display_name": name, "bio": None, "picture": None}
        picture = None
        if self._pictures is not None and user.picture_hash:
            got = await self._pictures.get_user_picture(user_id)
            if got is not None:
                small = await public_profile_picture(got[0])
                if small is not None:
                    picture = base64.b64encode(small).decode("ascii")
        return {"display_name": name, "bio": user.bio or None, "picture": picture}

    async def _iter_frame_chunks(self, frame_row) -> AsyncIterator[bytes]:
        path = self._resolve_media_path(frame_row.media_url)
        if path is None or not await aiofiles.os.path.isfile(path):
            log.warning(
                "highlight_signal: missing media for frame %s, sending error",
                frame_row.id,
            )
            yield framing.error_frame("expired")
            return
        size = (await aiofiles.os.stat(path)).st_size
        chunk_index = 0
        sent = 0
        async with aiofiles.open(path, "rb") as fh:
            while True:
                buf = await fh.read(framing.CHUNK_SIZE)
                if not buf:
                    break
                sent += len(buf)
                is_last = sent >= size
                yield framing.frame_chunk(
                    frame_id=frame_row.id,
                    sequence=frame_row.sequence,
                    chunk_index=chunk_index,
                    byte_length=size,
                    is_last_chunk=is_last,
                    payload=buf,
                )
                chunk_index += 1

    # ── GFS-relay fallback ──────────────────────────────────────────────

    async def _on_relay_offer(self, frame: dict) -> None:
        """Stream a published highlight to the GFS for proxy delivery.

        The guest couldn't reach us over WebRTC; the GFS pushed this
        ``relay_offer`` so we stream the framed bytes back over a signed
        POST and the GFS pipes them to the waiting guest. Same publish
        guard as the WebRTC offer — never relay an unpublished highlight.
        """
        relay_id = str(frame.get("relay_id") or "")
        highlight_id = str(frame.get("highlight_id") or "")
        if not (relay_id and highlight_id):
            log.debug("highlight_signal relay_offer: missing fields — dropped")
            return
        highlight = await self._highlights.get_highlight(highlight_id)
        if highlight is None or highlight.public_gfs_id is None:
            log.debug(
                "highlight_signal relay_offer: %s not published — dropped",
                highlight_id,
            )
            return
        task = asyncio.create_task(
            self._serve_relay(
                relay_id,
                highlight_id,
                token=str(frame.get("token") or ""),
                viewer_key=parse_viewer_key(frame.get("vk")),
            ),
            name=f"highlight-relay[{relay_id}]",
        )
        self._relay_tasks.add(task)
        task.add_done_callback(self._relay_tasks.discard)

    async def _serve_relay(
        self,
        relay_id: str,
        highlight_id: str,
        *,
        token: str = "",
        viewer_key: ec.EllipticCurvePublicKey | None = None,
    ) -> None:
        url = await self._gfs_url(
            f"/gfs/highlight_rtc/relay-stream/{relay_id}",
            highlight_id,
        )
        if url is None or self._http_client is None:
            return
        headers = self._relay_auth_headers(relay_id)
        try:
            async with self._http_client.post(
                url,
                allow_redirects=False,
                data=self._iter_sealed_stream(
                    highlight_id, token=token, viewer_key=viewer_key
                ),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=600, sock_connect=15),
            ) as resp:
                if resp.status >= 300:
                    log.warning(
                        "highlight relay POST %s returned HTTP %s",
                        url,
                        resp.status,
                    )
        except aiohttp.ClientError as exc:
            log.warning("highlight relay POST %s failed: %s", url, exc)

    def _relay_auth_headers(self, relay_id: str) -> dict[str, str]:
        """Header-based Ed25519 auth for the binary relay upload.

        The body is the raw framed stream, so the signature can't ride
        inside it — sign the canonical ``{instance_id, relay_id, ts}`` dict
        (same scheme as :meth:`_sign`) and carry it in headers the GFS
        verifies via ``authenticate_relay_stream``. The ``ts`` binds the
        signature to a short time window so a captured header can't be
        replayed against a future relay id.
        """
        if self._signing_key is None:
            raise RuntimeError(
                "HighlightSignalingHandler used before attach_identity",
            )
        instance_id = self._require_instance_id()
        ts = int(time.time())
        body = {"instance_id": instance_id, "relay_id": relay_id, "ts": ts}
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        sig = b64url_encode(sign_ed25519(self._signing_key, canonical))
        return {
            "X-SH-Instance": instance_id,
            "X-SH-Timestamp": str(ts),
            "X-SH-Signature": sig,
        }

    def _resolve_media_path(self, media_url: str | None) -> pathlib.Path | None:
        """Resolve a local media ref (``api/media/{filename}``, leading ``/``
        tolerated) to the on-disk path.

        Defends against path traversal — ``filename`` must not contain
        a slash or start with a dot, same guard
        :class:`MediaServeView` applies on the HTTP path.
        """
        # Uploads are stored as ``api/media/<name>`` (no leading slash) —
        # the old ``/api/media/``-only check read every one as missing.
        ref = local_media_ref(media_url)
        if ref is None:
            return None
        return media_file_path(
            pathlib.Path(self._media_dir), ref[len(MEDIA_REF_PREFIX) :]
        )

    async def _media_size(self, media_url: str | None) -> int:
        path = self._resolve_media_path(media_url)
        if path is None:
            return 0
        try:
            return (await aiofiles.os.stat(path)).st_size
        except OSError:
            return 0

    # ── HTTP back-channel to GFS ────────────────────────────────────────

    async def _post_answer(self, session_id: str, sdp: str) -> None:
        sess = self._sessions.get(session_id)
        if sess is None:
            return
        url = await self._gfs_url("/gfs/highlight_rtc/answer", sess.highlight_id)
        if url is None:
            return
        body = self._sign(
            {
                "instance_id": self._require_instance_id(),
                "session_id": session_id,
                "sdp": sdp,
            }
        )
        await self._post(url, body)

    async def _reject_with_error(
        self,
        session_id: str,
        sdp_offer: str,
        reason: str,
    ) -> None:
        """Best-effort error-frame delivery without spinning a session.

        We need a PeerConnection to actually send bytes; for cap /
        expired rejections we don't keep one around. The viewer will
        time out on its session poll, which the SPA bootstrap surfaces
        as a "the host is busy, try again" message. The reason string
        is logged so operators can spot abuse patterns.
        """
        log.info(
            "highlight_signal: rejecting session %s (%s) — viewer will time out",
            session_id,
            reason,
        )

    async def _gfs_url(self, path: str, highlight_id: str) -> str | None:
        """Resolve the inbox URL of the GFS the highlight is published to.

        Reads ``highlight.public_gfs_id`` (set during publish) so we always
        post the answer to the same GFS that pushed us the offer —
        important when the SH is paired with multiple GFSes.
        """
        if self._http_client is None:
            return None
        highlight = await self._highlights.get_highlight(highlight_id)
        if highlight is None or highlight.public_gfs_id is None:
            return None
        conn = await self._gfs_repo.get(highlight.public_gfs_id)
        if conn is None or conn.status != "active":
            return None
        return f"{conn.inbox_url}{path}"

    async def _post(self, url: str, body: dict) -> None:
        if self._http_client is None:
            return
        try:
            async with self._http_client.post(
                url,
                allow_redirects=False,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 300:
                    log.warning(
                        "highlight_signal POST %s returned HTTP %s",
                        url,
                        resp.status,
                    )
        except aiohttp.ClientError as exc:
            log.warning("highlight_signal POST %s failed: %s", url, exc)

    # ── Helpers ────────────────────────────────────────────────────────

    def _require_instance_id(self) -> str:
        if not self._own_instance_id:
            raise RuntimeError(
                "HighlightSignalingHandler used before attach_identity",
            )
        return self._own_instance_id

    def _sign(self, body: dict) -> dict:
        if self._signing_key is None:
            raise RuntimeError(
                "HighlightSignalingHandler used before attach_identity",
            )
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        signed = dict(body)
        signed["signature"] = b64url_encode(sign_ed25519(self._signing_key, canonical))
        return signed

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def stop(self) -> None:
        """Cancel every in-flight session + relay. Called from app cleanup."""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for sess in sessions:
            if sess.task is not None and not sess.task.done():
                sess.task.cancel()
            try:
                await sess.peer.close()
            except Exception:  # defensive
                log.debug("highlight_signal: peer close failed", exc_info=True)
        for task in list(self._relay_tasks):
            if not task.done():
                task.cancel()
        self._relay_tasks.clear()


# ─── Defaults ──────────────────────────────────────────────────────────


def _content_type_for(media_url: str | None) -> str:
    """Derive a Content-Type hint from the media URL extension.

    Used for the manifest, so the JS viewer knows whether to render an
    ``<img>`` or a ``<video>`` for each chunked frame.
    """
    if not media_url:
        return "application/octet-stream"
    ext = os.path.splitext(media_url)[1].lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }.get(ext, "application/octet-stream")


class _AiolibAnswererPeer:
    """Production :class:`_AnswererPeer` backed by
    :class:`aiolibdatachannel.PeerConnection`.

    The viewer side is the **offerer** — it sends the SDP offer and
    opens the DataChannel. We answer with a non-trickle SDP
    (``pc.create_answer()`` waits for ICE gathering to finish before
    returning, so candidates are inlined). That keeps the wire
    protocol simple — only an offer + answer + the eventual bytes,
    no separate ICE-trickling channel needs to exist between the SH
    and the GFS.

    Once the channel opens we proxy ``send`` straight through and
    ``close()`` tears down the PC (the spawned drain task is
    auto-cancelled via :meth:`pc.spawn_task` ownership).
    """

    __slots__ = ("_channel", "_channel_event", "_pc")

    def __init__(self, ice_servers: list[dict[str, Any]]) -> None:
        # ``urls`` is a list in the shape ``build_ice_servers`` makes:
        # ``str(["stun:…"])`` used to reach libdatachannel as the URL, which
        # refused the whole configuration (``rtcCreatePeerConnection:
        # invalid argument``) — every public viewer fell back to the relay.
        self._pc: rtc.PeerConnection = rtc.PeerConnection(
            rtc_configuration(ice_servers)
        )
        self._channel: rtc.DataChannel | None = None
        # Set when the viewer-opened DataChannel arrives so ``wait_open``
        # can return — replaces the missing
        # ``asyncio.Event``-style hook on incoming_data_channels.
        self._channel_event: asyncio.Event = asyncio.Event()
        self._pc.spawn_task(self._drain_incoming_channel())

    async def _drain_incoming_channel(self) -> None:
        """Wait for the viewer's DataChannel and latch it on the peer."""
        try:
            async for ch in self._pc.incoming_data_channels():
                self._channel = ch
                self._channel_event.set()
                return
        except (rtc.RTCError, rtc.ConnectionClosedError) as exc:
            log.debug("highlight_signal incoming-channel drain ended: %s", exc)

    async def set_remote_offer(self, sdp: str) -> None:
        await self._pc.set_remote_description(sdp, "offer")

    async def create_answer(self) -> str:
        # Non-trickle: ``create_answer`` waits for ICE gathering to
        # finish and returns the SDP with all candidates inlined. The
        # signalling endpoint between SH and GFS only ferries the
        # answer SDP — there's no ICE-trickle channel to drain into.
        local = await self._pc.create_answer()
        return local.sdp

    async def add_ice_candidate(self, candidate: dict) -> None:
        cand = str(candidate.get("candidate") or "")
        mid = str(candidate.get("sdpMid") or candidate.get("sdp_mid") or "0")
        if not cand:
            return
        await self._pc.add_remote_candidate(cand, mid)

    async def wait_open(self) -> None:
        # Wait for the channel to arrive (viewer opens it), then wait
        # for libdatachannel to transition it to OPEN.
        await self._channel_event.wait()
        assert self._channel is not None  # set alongside the event
        await self._channel.wait_open()

    async def send(self, frame_bytes: bytes) -> None:
        if self._channel is None:
            raise rtc.ConnectionClosedError("channel not yet open")
        await self._channel.send(frame_bytes)

    async def close(self) -> None:
        # ``aclose`` drains the spawn_task we registered, releases the
        # native handle, and ensures the channel goes away with the PC.
        try:
            await self._pc.aclose()
        except rtc.RTCError as exc:  # defensive on already-closed paths
            log.debug("highlight_signal peer close error: %s", exc)


def _default_peer_factory(ice: list[dict[str, Any]]) -> _AnswererPeer:
    """Production peer factory — wraps an
    :class:`aiolibdatachannel.PeerConnection` to satisfy the
    :class:`_AnswererPeer` Protocol. The constructor injects this by
    default; tests pass a stub via the ``peer_factory`` keyword to
    avoid spinning a real native handle.
    """
    return _AiolibAnswererPeer(ice)
