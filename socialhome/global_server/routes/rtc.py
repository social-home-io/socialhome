"""WebRTC DataChannel signalling routes (``/gfs/rtc/*``, spec §24.12).

The GFS acts as the signalling rendezvous between two paired household
instances so they can bring up a DataChannel without a public IP.
Every POST carries an Ed25519 signature over the canonical body minus
the ``signature`` field, same scheme as ``/gfs/report``.
"""

from __future__ import annotations

import logging

from aiohttp import web

from .. import app_keys as K
from ..admin_service import verify_report_signature
from .base import GfsBaseView

log = logging.getLogger(__name__)


async def _rtc_authenticate(view: GfsBaseView) -> tuple[dict, str] | web.Response:
    """Shared signature check for ``/gfs/rtc/*`` POST bodies.

    Returns the parsed + verified payload dict + sender instance_id on
    success, or a ready-to-return error Response.
    """
    fed_repo = view.svc(K.gfs_fed_repo_key)
    body = await view.body_or_400()
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
    return body, instance_id


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
        fed_repo = self.svc(K.gfs_fed_repo_key)
        await fed_repo.upsert_rtc_connection(instance_id, transport="webrtc")
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
            await rtc.ice_candidate(session_id, candidate)
        except KeyError:
            return web.json_response({"error": "not_found"}, status=404)
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
    """``POST /gfs/rtc/ping`` — keepalive; bumps ``last_ping_at``."""

    async def post(self) -> web.Response:
        result = await _rtc_authenticate(self)
        if isinstance(result, web.Response):
            return result
        body, instance_id = result
        transport = str(body.get("transport") or "webrtc")
        if transport not in ("webrtc", "https"):
            return web.json_response(
                {"error": "invalid_transport"},
                status=422,
            )
        fed_repo = self.svc(K.gfs_fed_repo_key)
        await fed_repo.upsert_rtc_connection(instance_id, transport=transport)
        return web.json_response(
            {"status": "ok", "transport": transport},
        )
