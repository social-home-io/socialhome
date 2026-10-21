"""SH↔SH WebRTC signalling rendezvous routes (``/gfs/rtc/*``, spec §4.2.3).

The GFS is a public meeting point where two household instances can drop
their SDP offer / answer / ICE candidates so they can bring up a direct
WebRTC DataChannel between themselves for §4.2.3 sync. The GFS holds no
PeerConnection — it just stores and forwards the signalling artefacts.

This is **not** the SH↔GFS transport. That is a `wss://` WebSocket on
``/gfs/ws`` (spec §24.12); see :mod:`.ws` and :mod:`..ws_registry`.

Every POST carries an Ed25519 signature over the canonical body minus
the ``signature`` field, same scheme as ``/gfs/report``.
"""

from __future__ import annotations

import logging
import time

from aiohttp import web

from .. import app_keys as K
from ..admin_service import verify_report_signature
from .base import GfsBaseView

log = logging.getLogger(__name__)

#: Accepted clock skew on the relay-stream auth timestamp. Matches the
#: §24.11 inbound-envelope window so a captured ``X-SH-Signature`` header
#: can't be replayed once the relay id is reused / forgotten.
RELAY_AUTH_MAX_SKEW_SECONDS: int = 300


async def _rtc_authenticate(
    view: GfsBaseView,
    *,
    keep_signature: bool = False,
    max_bytes: int | None = None,
) -> tuple[dict, str] | web.Response:
    """Shared signature check for ``/gfs/rtc/*`` POST bodies.

    Returns the parsed + verified payload dict + sender instance_id on
    success, or a ready-to-return error Response. The returned body has
    no ``signature`` unless ``keep_signature`` — a body the GFS forwards
    verbatim (a public moment envelope) keeps it, because the receiver
    verifies that same signature itself. With *max_bytes* the body is
    read bounded (413 past it) before anything is parsed.
    """
    fed_repo = view.svc(K.gfs_fed_repo_key)
    body = (
        await view.bounded_json(max_bytes)
        if max_bytes is not None
        else await view.body_or_400()
    )
    instance_id = str(body.get("instance_id") or "")
    if not instance_id:
        return web.json_response(
            {"error": "missing_fields", "required": ["instance_id"]},
            status=422,
        )
    sender = await fed_repo.get_instance(instance_id)
    if sender is None or sender.status != "active":
        return web.json_response({"error": "forbidden"}, status=403)
    signature = body.pop("signature", "")
    if not verify_report_signature(body, signature, sender.public_key):
        return web.json_response({"error": "invalid_signature"}, status=401)
    if keep_signature:
        body["signature"] = signature
    return body, instance_id


async def authenticate_relay_stream(view: GfsBaseView) -> str | web.Response:
    """Header-based Ed25519 check for the author's relay-stream upload.

    The relay ``POST .../relay-stream/{relay_id}`` body is the raw framed
    byte stream, so the signature can't ride inside it. Instead the author
    signs the canonical ``{"instance_id", "relay_id", "ts"}`` dict (same
    scheme as :func:`_rtc_authenticate`) and ships ``instance_id`` /
    ``ts`` / signature in ``X-SH-Instance`` / ``X-SH-Timestamp`` /
    ``X-SH-Signature`` headers. The ``ts`` must be within
    :data:`RELAY_AUTH_MAX_SKEW_SECONDS` of now, so a captured header can't
    be replayed later. Returns the verified sender instance_id or a
    ready-to-return error Response.
    """
    fed_repo = view.svc(K.gfs_fed_repo_key)
    instance_id = view.request.headers.get("X-SH-Instance", "")
    signature = view.request.headers.get("X-SH-Signature", "")
    ts_raw = view.request.headers.get("X-SH-Timestamp", "")
    relay_id = view.match("relay_id")
    if not instance_id or not signature or not ts_raw:
        return web.json_response({"error": "missing_auth_headers"}, status=422)
    try:
        ts = int(ts_raw)
    except ValueError:
        return web.json_response({"error": "invalid_timestamp"}, status=422)
    if abs(int(time.time()) - ts) > RELAY_AUTH_MAX_SKEW_SECONDS:
        return web.json_response({"error": "stale_timestamp"}, status=401)
    sender = await fed_repo.get_instance(instance_id)
    if sender is None or sender.status != "active":
        return web.json_response({"error": "forbidden"}, status=403)
    body = {"instance_id": instance_id, "relay_id": relay_id, "ts": ts}
    if not verify_report_signature(body, signature, sender.public_key):
        return web.json_response({"error": "invalid_signature"}, status=401)
    return instance_id


class RtcOfferView(GfsBaseView):
    """``POST /gfs/rtc/offer`` — create a new signalling session."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        sdp = str(body.get("sdp") or "")
        rtc = self.svc(K.gfs_rtc_key)
        session_id = await rtc.offer(instance_id, sdp)
        if session_id is None:
            return web.json_response({"error": "busy"}, status=503)
        return web.json_response({"session_id": session_id})


class RtcAnswerView(GfsBaseView):
    """``POST /gfs/rtc/answer`` — attach an SDP answer to a session."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, _instance_id = result
        session_id = str(body.get("session_id") or "")
        sdp = str(body.get("sdp") or "")
        rtc = self.svc(K.gfs_rtc_key)
        try:
            await rtc.answer(session_id, sdp)
        except KeyError:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response({"status": "ok"})


class RtcIceView(GfsBaseView):
    """``POST /gfs/rtc/ice`` — relay an ICE candidate."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, _instance_id = result
        session_id = str(body.get("session_id") or "")
        candidate = body.get("candidate") or {}
        if not isinstance(candidate, dict):
            return web.json_response(
                {"error": "invalid_candidate"},
                status=422,
            )
        rtc = self.svc(K.gfs_rtc_key)
        try:
            stored = await rtc.ice_candidate(session_id, candidate)
        except KeyError:
            return web.json_response({"error": "not_found"}, status=404)
        if not stored:
            return web.json_response({"error": "too_many_candidates"}, status=429)
        return web.json_response({"status": "ok"})


class RtcSessionView(GfsBaseView):
    """``GET /gfs/rtc/session/{session_id}`` — poll signalling state."""

    async def get(self) -> web.Response:
        session_id = self.match("session_id")
        rtc = self.svc(K.gfs_rtc_key)
        session = rtc.get_session(session_id)
        if session is None:
            return web.json_response({"error": "not_found"}, status=404)
        return web.json_response(
            {
                "session_id": session.session_id,
                "initiator_id": session.initiator_id,
                "offer_sdp": session.offer_sdp,
                "answer_sdp": session.answer_sdp,
                "ice_candidates": session.ice_candidates,
            }
        )


class RtcPingView(GfsBaseView):
    """``POST /gfs/rtc/ping`` — HTTPS-fallback keepalive; bumps ``last_ping_at``.

    Instances with an open ``/gfs/ws`` WebSocket do not need to call this —
    the WS heartbeat keeps ``last_ping_at`` fresh. This endpoint exists for
    instances on the HTTPS-inbox fallback path (spec §24.12).
    """

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        transport = str(body.get("transport") or "https")
        if transport not in ("websocket", "https"):
            return web.json_response(
                {"error": "invalid_transport"},
                status=422,
            )
        fed_repo = self.svc(K.gfs_fed_repo_key)
        await fed_repo.upsert_rtc_connection(instance_id, transport=transport)
        return web.json_response(
            {"status": "ok", "transport": transport},
        )
