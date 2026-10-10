"""Author-side signalling + DataChannel streamer for §Momentum-public.

The public-moments counterpart to
:class:`socialhome.services.highlight_signaling_handler.HighlightSignalingHandler`.
A guest visiting ``GET /moments/{user_id}`` on the GFS reads that user's
CURRENT PUBLIC moments, delivered live from the author's SH over a single
ordered WebRTC DataChannel (with a GFS-relay fallback when WebRTC fails).
The GFS stores ZERO moment bytes — it only brokers the signalling and, on
the fallback path, pipes the framed bytes straight through.

Receives ``{type:"moment_signal", ...}`` frames from the SH↔GFS
WebSocket. On a fresh ``offer`` it spins up an :mod:`aiolibdatachannel`
answerer PeerConnection, posts the SDP answer back to the GFS via the
signed ``/gfs/moment_rtc/answer`` endpoint, applies remote ICE
candidates as they arrive, and once the DataChannel opens streams the
public-moment index + media bytes via the framing protocol in
:mod:`socialhome.services.highlight_public_framing`.

**Privacy invariant (critical):** only ``is_public=1``, non-expired
moments are ever streamed. The guard lives in
:meth:`AbstractMomentRepo.list_public_for` (the SQL ``WHERE`` clause), so
a private / household moment can never reach the public stream.

The content-agnostic peer abstraction
(``_AnswererPeer`` / ``_AiolibAnswererPeer`` / ``_default_peer_factory``)
and the ``_content_type_for`` helper are imported from the highlight
handler rather than copy-pasted — only the content streamer and the
signalling frame field names differ.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import aiofiles
import aiofiles.os
import aiohttp

from ..crypto import b64url_encode, sign_ed25519
from ..domain.child_protection import ProtectedCapability
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.moment_repo import AbstractMomentRepo
from . import highlight_public_framing as framing
from .protection_gate import ProtectionGateMixin
from .highlight_signaling_handler import (
    PeerFactory,
    _AnswererPeer,
    _content_type_for,
    _default_peer_factory,
)

log = logging.getLogger(__name__)


#: Per-instance cap on concurrent public-moment viewer sessions.
MAX_CONCURRENT_VIEWERS_PER_INSTANCE: int = 50

#: Per-author cap, applied on top of the per-instance cap, so a single
#: viral author can't drown out the others on this instance.
MAX_CONCURRENT_VIEWERS_PER_USER: int = 10


# ─── Session state ─────────────────────────────────────────────────────


@dataclass(slots=True)
class _Session:
    session_id: str
    user_id: str
    #: The LOCAL ``gfs_connections`` row id the answer goes back through.
    gfs_id: str
    peer: _AnswererPeer
    task: asyncio.Task[None] | None = None
    pending_ice: list[dict] = field(default_factory=list)


# ─── Handler ───────────────────────────────────────────────────────────


class MomentPublicSignalingHandler(ProtectionGateMixin):
    """Drives the author-side half of §Momentum-public WebRTC.

    A protected account's public moments are never served (§CP.R) — the
    stream is an empty index, whatever the connection server still lists.
    """

    __slots__ = (
        "_child_protection",
        "_moments",
        "_gfs_repo",
        "_http_client",
        "_signing_key",
        "_own_instance_id",
        "_peer_factory",
        "_ice_servers",
        "_media_dir",
        "_sessions",
        "_relay_tasks",
        "_lock",
    )

    def __init__(
        self,
        moment_repo: AbstractMomentRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        *,
        media_dir: str,
        peer_factory: PeerFactory | None = None,
        ice_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        self._moments = moment_repo
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

    # ── Frame entry point ───────────────────────────────────────────────

    async def handle_signal(self, frame: dict, *, gfs_id: str | None = None) -> None:
        """Dispatch a single ``moment_signal`` frame from the WS.

        *gfs_id* is the LOCAL connection row id the frame arrived on — bound
        per WS client by the supervisor — and is where every answer goes.
        The frame's own ``gfs_id`` is the server's PUBLIC id (whatever it
        currently serves, or an alias), never a row id; it is only resolved,
        against the active connections, when no connection is bound.
        """
        kind = str(frame.get("kind") or "")
        if kind == "ice":
            await self._on_ice(frame)
            return
        if kind not in ("offer", "relay_offer"):
            log.debug("moment_signal: unknown kind %r — dropped", kind)
            return
        conn_id = gfs_id or await self._conn_for_server(str(frame.get("gfs_id") or ""))
        if kind == "offer":
            await self._on_offer(frame, conn_id)
        else:
            await self._on_relay_offer(frame, conn_id)

    async def _conn_for_server(self, server_id: str) -> str:
        """The active connection pinned to public id *server_id*, or ``""``."""
        if not server_id:
            return ""
        for conn in await self._gfs_repo.list_active():
            if conn.gfs_instance_id == server_id:
                return conn.id
        return ""

    # ── Offer path ──────────────────────────────────────────────────────

    async def _on_offer(self, frame: dict, gfs_id: str) -> None:
        session_id = str(frame.get("session_id") or "")
        user_id = str(frame.get("user_id") or "")
        sdp_offer = str(frame.get("sdp") or "")
        if not (session_id and user_id and gfs_id and sdp_offer):
            log.debug("moment_signal offer: missing fields — dropped")
            return
        if session_id in self._sessions:
            log.debug("moment_signal offer: duplicate session_id — dropped")
            return

        # Reject the viewer if we've hit the caps.
        per_user = sum(1 for s in self._sessions.values() if s.user_id == user_id)
        if (
            len(self._sessions) >= MAX_CONCURRENT_VIEWERS_PER_INSTANCE
            or per_user >= MAX_CONCURRENT_VIEWERS_PER_USER
        ):
            log.warning(
                "moment_signal: rejecting offer for user=%s (cap reached)",
                user_id,
            )
            await self._reject_with_error(session_id, sdp_offer, "backpressure")
            return

        peer = self._peer_factory(self._ice_servers)
        async with self._lock:
            self._sessions[session_id] = _Session(
                session_id=session_id,
                user_id=user_id,
                gfs_id=gfs_id,
                peer=peer,
            )
        task = asyncio.create_task(
            self._serve(session_id, sdp_offer),
            name=f"moment-signal[{session_id}]",
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
            log.debug("moment_signal ice: %s", exc)

    # ── Streaming ──────────────────────────────────────────────────────

    async def _serve(self, session_id: str, sdp_offer: str) -> None:
        sess = self._sessions[session_id]
        peer = sess.peer
        try:
            await peer.set_remote_offer(sdp_offer)
            answer_sdp = await peer.create_answer()
            await self._post_answer(session_id, answer_sdp)
            await peer.wait_open()
            await self._stream(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # defensive
            log.warning("moment_signal serve %s: %s", session_id, exc)
        finally:
            try:
                await peer.close()
            except Exception:  # defensive
                pass
            async with self._lock:
                self._sessions.pop(session_id, None)

    async def _stream(self, session_id: str) -> None:
        """WebRTC sink: drive the shared frame generator onto the peer."""
        sess = self._sessions[session_id]
        async for chunk in self._iter_moment_frames(sess.user_id):
            await sess.peer.send(chunk)

    async def _iter_moment_frames(self, user_id: str) -> AsyncIterator[bytes]:
        """Yield the framed public-moment stream (meta → chunks → end).

        Single source of truth for both transports: the WebRTC path sends
        each chunk on the DataChannel; the GFS-relay fallback pipes the
        identical bytes into the signed streaming POST. The privacy
        invariant is enforced by ``list_public_for`` (``is_public=1`` and
        non-expired only), so a private / household moment can never enter
        this stream. An empty index still emits ``moment_index_meta([])``
        + ``stream_end`` so the guest sees an empty index, not an error.
        """
        moments = await self._moments.list_public_for(user_id)
        if await self._is_restricted(user_id, ProtectedCapability.PUBLIC_MOMENTS):
            # §CP.R: shared before protection, never streamed after it.
            moments = []
        meta: list[dict] = []
        for m in moments:
            has_media = bool(m.media_url)
            entry: dict[str, Any] = {
                "id": m.id,
                "content": m.content,
                "created_at": m.created_at,
                "media_type": m.media_type,
                "has_media": has_media,
                # Synthetic frame id == moment id so chunks correlate to
                # this manifest entry on the viewer side.
                "media_frame_id": m.id,
            }
            if has_media:
                entry["byte_length"] = await self._media_size(m.media_url)
                entry["content_type"] = _content_type_for(m.media_url)
            meta.append(entry)
        yield framing.moment_index_meta(meta)
        for index, m in enumerate(moments):
            if not m.media_url:
                continue
            async for chunk in self._iter_media_chunks(
                media_url=m.media_url,
                frame_id=m.id,
                sequence=index,
            ):
                yield chunk
        yield framing.stream_end()

    async def _iter_media_chunks(
        self,
        *,
        media_url: str,
        frame_id: str,
        sequence: int,
    ) -> AsyncIterator[bytes]:
        path = self._resolve_media_path(media_url)
        if path is None or not await aiofiles.os.path.isfile(path):
            log.warning(
                "moment_signal: missing media for moment %s, sending error",
                frame_id,
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
                    frame_id=frame_id,
                    sequence=sequence,
                    chunk_index=chunk_index,
                    byte_length=size,
                    is_last_chunk=is_last,
                    payload=buf,
                )
                chunk_index += 1

    # ── GFS-relay fallback ──────────────────────────────────────────────

    async def _on_relay_offer(self, frame: dict, gfs_id: str) -> None:
        """Stream the author's public moments to the GFS for proxy delivery.

        The guest couldn't reach us over WebRTC; the GFS pushed this
        ``relay_offer`` so we stream the framed bytes back over a signed
        POST and the GFS pipes them to the waiting guest. Same privacy
        guard as the WebRTC offer — only public, non-expired moments.
        """
        relay_id = str(frame.get("relay_id") or "")
        user_id = str(frame.get("user_id") or "")
        if not (relay_id and user_id and gfs_id):
            log.debug("moment_signal relay_offer: missing fields — dropped")
            return
        task = asyncio.create_task(
            self._serve_relay(relay_id, user_id, gfs_id),
            name=f"moment-relay[{relay_id}]",
        )
        self._relay_tasks.add(task)
        task.add_done_callback(self._relay_tasks.discard)

    async def _serve_relay(self, relay_id: str, user_id: str, gfs_id: str) -> None:
        url = await self._gfs_url(
            f"/gfs/moment_rtc/relay-stream/{relay_id}",
            gfs_id,
        )
        if url is None or self._http_client is None:
            return
        headers = self._relay_auth_headers(relay_id)
        try:
            async with self._http_client.post(
                url,
                allow_redirects=False,
                data=self._iter_moment_frames(user_id),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=600, sock_connect=15),
            ) as resp:
                if resp.status >= 300:
                    log.warning(
                        "moment relay POST %s returned HTTP %s",
                        url,
                        resp.status,
                    )
        except aiohttp.ClientError as exc:
            log.warning("moment relay POST %s failed: %s", url, exc)

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
                "MomentPublicSignalingHandler used before attach_identity",
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
        """Resolve ``/api/media/{filename}`` to the on-disk path.

        Defends against path traversal — ``filename`` must not contain a
        slash or start with a dot, same guard :class:`MediaServeView`
        applies on the HTTP path.
        """
        if not media_url:
            return None
        prefix = "/api/media/"
        if not media_url.startswith(prefix):
            return None
        filename = media_url[len(prefix) :]
        if "/" in filename or "\\" in filename or filename.startswith("."):
            return None
        return pathlib.Path(self._media_dir) / filename

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
        url = await self._gfs_url("/gfs/moment_rtc/answer", sess.gfs_id)
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
        """Best-effort rejection without spinning a session.

        We need a PeerConnection to actually send bytes; for cap
        rejections we don't keep one around. The viewer times out on its
        session poll, which the public viewer surfaces as a "the host is
        busy, try again" message. The reason string is logged so
        operators can spot abuse patterns.
        """
        log.info(
            "moment_signal: rejecting session %s (%s) — viewer will time out",
            session_id,
            reason,
        )

    async def _gfs_url(self, path: str, gfs_id: str) -> str | None:
        """Resolve the inbox URL of the GFS that pushed us this frame.

        *gfs_id* is the LOCAL connection row id (see :meth:`handle_signal`),
        so we always reply to the same GFS — important when the SH is
        paired with multiple GFSes.
        """
        if self._http_client is None:
            return None
        conn = await self._gfs_repo.get(gfs_id)
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
                        "moment_signal POST %s returned HTTP %s",
                        url,
                        resp.status,
                    )
        except aiohttp.ClientError as exc:
            log.warning("moment_signal POST %s failed: %s", url, exc)

    # ── Helpers ────────────────────────────────────────────────────────

    def _require_instance_id(self) -> str:
        if not self._own_instance_id:
            raise RuntimeError(
                "MomentPublicSignalingHandler used before attach_identity",
            )
        return self._own_instance_id

    def _sign(self, body: dict) -> dict:
        if self._signing_key is None:
            raise RuntimeError(
                "MomentPublicSignalingHandler used before attach_identity",
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
                pass
        for task in list(self._relay_tasks):
            if not task.done():
                task.cancel()
        self._relay_tasks.clear()
