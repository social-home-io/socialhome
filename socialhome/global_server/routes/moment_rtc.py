"""Public-viewer WebRTC signalling for the public-moments live index.

The §Momentum-public counterpart to :mod:`.highlight_rtc`. A guest
visiting ``GET /moments/{user_id}`` reads that user's CURRENT PUBLIC
moments, streamed live from the author's SH over a WebRTC DataChannel
(with a GFS-relay fallback). These endpoints are anonymous on purpose —
a non-Social-Home browser can hit them without any Ed25519 key.

Unlike the highlight flow there is no share token: the public-moments
directory IS the authorisation surface. A guest may stream any user who
holds a live registration in :class:`MomentPublicRegistry` — the same
opt-in that already lists them in the public directory.

The offer is stored in the same :class:`GfsRtcSession` table used by
SH↔SH sync (so the SDP plumbing stays shared) and is *also* pushed to
the author's SH over the existing GFS↔SH WebSocket as a
``moment_signal`` frame. The author's SH replies with an SDP answer via
the signed :class:`MomentRtcAnswerView`; the browser polls
:class:`MomentRtcSessionView` until the answer + ICE list arrive.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from .. import app_keys as K
from ..public_unavailable import unavailable_at, unavailable_response
from .base import GfsBaseView
from .rtc import _rtc_authenticate, authenticate_relay_stream

log = logging.getLogger(__name__)

#: How long the guest's relay GET waits for the author SH to start
#: streaming before giving up with the uniform 503. Matches the viewer's
#: WebRTC poll budget so the fallback doesn't hang far longer than the
#: primary. It is also the latency of EVERY relay failure (unknown, offline,
#: never streamed, bridge full), so timing can't reveal author presence.
RELAY_AUTHOR_CONNECT_TIMEOUT_SECONDS: float = 30.0

#: Author body is read in chunks this size and piped straight to the
#: guest — independent of the framing chunk size (pure byte passthrough).
RELAY_READ_CHUNK_BYTES: int = 64 * 1024


async def _servable_author(view: GfsBaseView, user_id: str) -> str | None:
    """The author instance a guest may stream ``user_id`` from, or ``None``.

    ``None`` folds every non-success state — unknown user, inactive
    registration, author's household not connected — into one answer the
    callers map to the uniform reply. The registry lookup AND the
    connection check run on every branch (an unknown user checks the
    empty id), so no branch skips work the others do.
    """
    registry = view.svc(K.gfs_moment_public_registry_key)
    reg = await registry.get_registration(user_id)
    active = reg is not None and reg.status == "active"
    author_instance_id = reg.instance_id if reg is not None and active else ""
    online = view.svc(K.gfs_ws_registry_key).is_connected(author_instance_id)
    if not (active and online and author_instance_id):
        return None
    return author_instance_id


# ─── Public viewer surface (anonymous) ───────────────────────────────────


class MomentRtcOfferView(GfsBaseView):
    """``POST /gfs/moment_rtc/offer`` — viewer initiates signalling.

    Body: ``{user_id, sdp}``. No signature — the user's live directory
    registration is the auth surface. We push a ``moment_signal`` frame
    to the author's WS so its :class:`MomentPublicSignalingHandler` knows
    to answer.
    """

    async def post(self) -> web.Response:
        body = await self.body_or_400()
        user_id = str(body.get("user_id") or "")
        sdp = str(body.get("sdp") or "")
        if not (user_id and sdp):
            return web.json_response({"error": "missing_fields"}, status=422)
        author_instance_id = await _servable_author(self, user_id)
        if author_instance_id is None:
            return unavailable_response()

        ws_registry = self.svc(K.gfs_ws_registry_key)
        rtc = self.svc(K.gfs_rtc_key)
        session_id = await rtc.offer(author_instance_id, sdp)
        gfs_id = self.svc(K.gfs_config_key).instance_id
        await ws_registry.send(
            author_instance_id,
            {
                "type": "moment_signal",
                "kind": "offer",
                "session_id": session_id,
                "user_id": user_id,
                "gfs_id": gfs_id,
                "sdp": sdp,
            },
        )
        return web.json_response({"session_id": session_id}, status=201)


class MomentRtcSessionView(GfsBaseView):
    """``GET /gfs/moment_rtc/session/{session_id}`` — viewer polls for answer."""

    async def get(self) -> web.Response:
        session_id = self.match("session_id")
        rtc = self.svc(K.gfs_rtc_key)
        session = rtc.get_session(session_id)
        if session is None:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response(
            {
                "session_id": session.session_id,
                "answer_sdp": session.answer_sdp,
                "ice_candidates": session.ice_candidates,
            }
        )


class MomentRtcViewerIceView(GfsBaseView):
    """``POST /gfs/moment_rtc/ice/viewer`` — viewer pushes an ICE candidate."""

    async def post(self) -> web.Response:
        body = await self.body_or_400()
        session_id = str(body.get("session_id") or "")
        candidate = body.get("candidate") or {}
        if not session_id or not isinstance(candidate, dict):
            return web.json_response({"error": "invalid"}, status=422)
        rtc = self.svc(K.gfs_rtc_key)
        try:
            await rtc.ice_candidate(session_id, candidate)
        except KeyError:
            return web.json_response({"error": "not_found"}, status=404)
        # Forward to the author so the answerer peer learns the remote ICE
        # list as it streams in.
        session = rtc.get_session(session_id)
        if session is not None:
            ws_registry = self.svc(K.gfs_ws_registry_key)
            await ws_registry.send(
                session.initiator_id,
                {
                    "type": "moment_signal",
                    "kind": "ice",
                    "session_id": session_id,
                    "candidate": candidate,
                },
            )
        return web.json_response({"status": "ok"})


# ─── GFS-relay fallback (anon guest GET ⇄ signed author stream) ───────────


class MomentRelayStreamView(GfsBaseView):
    """``GET /gfs/moment_rtc/relay/{user_id}`` — anon.

    Fallback for when the guest can't open a direct WebRTC DataChannel.
    Auth is the same live directory registration the offer flow uses. We
    register a transient relay channel, push a ``relay_offer`` to the
    author over the WS, wait for the author to start streaming, then pipe
    the author's framed bytes straight to this chunked response. The GFS
    stores nothing — it is a pure passthrough for already-public content.
    """

    async def get(self) -> web.StreamResponse:
        # Every failure answers only at this deadline — the same budget the
        # online-but-never-streams branch waits — so latency can't tell an
        # offline author from an online one (see :mod:`..public_unavailable`).
        deadline = (
            asyncio.get_running_loop().time() + RELAY_AUTHOR_CONNECT_TIMEOUT_SECONDS
        )
        user_id = self.match("user_id")
        author_instance_id = await _servable_author(self, user_id)
        if author_instance_id is None:
            return await unavailable_at(deadline)

        bridge = self.svc(K.gfs_relay_bridge_key)
        relay_id = bridge.create(target_instance_id=author_instance_id, scope=user_id)
        if relay_id is None:
            return await unavailable_at(deadline)
        channel = bridge.get(relay_id)
        assert channel is not None  # just created

        ws_registry = self.svc(K.gfs_ws_registry_key)
        gfs_id = self.svc(K.gfs_config_key).instance_id
        await ws_registry.send(
            author_instance_id,
            {
                "type": "moment_signal",
                "kind": "relay_offer",
                "relay_id": relay_id,
                "user_id": user_id,
                "gfs_id": gfs_id,
            },
        )

        # Hold the request until the author actually starts streaming, so a
        # stalled author yields a clean 503 rather than an empty 200 body.
        try:
            await asyncio.wait_for(
                channel.connected.wait(),
                timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
            )
        except asyncio.TimeoutError, TimeoutError:
            bridge.close(relay_id)
            return await unavailable_at(deadline)

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "application/octet-stream",
                "Cache-Control": "no-store",
            },
        )
        await resp.prepare(self.request)
        try:
            async for chunk in bridge.consume(relay_id):
                await resp.write(chunk)
        finally:
            bridge.close(relay_id)
        await resp.write_eof()
        return resp


class MomentRelayUploadView(GfsBaseView):
    """``POST /gfs/moment_rtc/relay-stream/{relay_id}`` — author streams.

    Signed via the header-based :func:`authenticate_relay_stream`; the
    request body is the raw framed byte stream. We feed it chunk-by-chunk
    into the bridge channel the guest GET is draining. Authority guard:
    the relay's target instance must equal the authenticated signer.
    """

    async def post(self) -> web.Response:
        result = await authenticate_relay_stream(self)
        if isinstance(result, web.Response):
            return result
        instance_id = result
        relay_id = self.match("relay_id")
        bridge = self.svc(K.gfs_relay_bridge_key)
        channel = bridge.get(relay_id)
        if channel is None:
            return web.json_response({"error": "not_found"}, status=404)
        if channel.target_instance_id != instance_id:
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            async for chunk in self.request.content.iter_chunked(
                RELAY_READ_CHUNK_BYTES
            ):
                if not await bridge.feed(relay_id, chunk):
                    # Guest hung up — stop reading the upload early.
                    break
        finally:
            await bridge.finish(relay_id)
        return web.json_response({"status": "ok"})


# ─── Author surface (signed) ─────────────────────────────────────────────


class MomentRtcAnswerView(GfsBaseView):
    """``POST /gfs/moment_rtc/answer`` — author returns the SDP answer.

    Body: ``{instance_id, session_id, sdp, signature}`` — signed with the
    author SH's Ed25519 key, verified by the shared
    :func:`_rtc_authenticate` middleware.
    """

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        session_id = str(body.get("session_id") or "")
        sdp = str(body.get("sdp") or "")
        if not session_id or not sdp:
            return web.json_response({"error": "missing_fields"}, status=422)
        rtc = self.svc(K.gfs_rtc_key)
        session = rtc.get_session(session_id)
        if session is None:
            return web.json_response({"error": "not_found"}, status=404)
        # Authority guard: only the instance the offer was pushed to may
        # answer this session.
        if session.initiator_id != instance_id:
            return web.json_response({"error": "forbidden"}, status=403)
        await rtc.answer(session_id, sdp)
        return web.json_response({"status": "ok"})


class MomentRtcAuthorIceView(GfsBaseView):
    """``POST /gfs/moment_rtc/ice/author`` — author pushes an ICE candidate
    back to the GFS so the viewer's poll picks it up."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        session_id = str(body.get("session_id") or "")
        candidate = body.get("candidate") or {}
        if not session_id or not isinstance(candidate, dict):
            return web.json_response({"error": "invalid"}, status=422)
        rtc = self.svc(K.gfs_rtc_key)
        session = rtc.get_session(session_id)
        if session is None:
            return web.json_response({"error": "not_found"}, status=404)
        if session.initiator_id != instance_id:
            return web.json_response({"error": "forbidden"}, status=403)
        await rtc.ice_candidate(session_id, candidate)
        return web.json_response({"status": "ok"})
