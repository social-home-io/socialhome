"""Call signalling routes (spec §26).

* ``POST   /api/calls``                            — initiate a call inside a conversation
* ``POST   /api/calls/{call_id}/answer``           — submit SDP answer
* ``POST   /api/calls/{call_id}/ice``              — trickle ICE candidate
* ``POST   /api/calls/{call_id}/hangup``           — end the call
* ``POST   /api/calls/{call_id}/decline``          — callee refuses the call
* ``POST   /api/calls/{call_id}/join``             — late-join an in-progress group call
* ``POST   /api/calls/{call_id}/quality``          — push a WebRTC getStats() sample
* ``GET    /api/calls/active``                     — list this user's active calls
* ``GET    /api/calls/{call_id}/quality``          — list persisted quality samples (admin)
* ``GET    /api/conversations/{id}/calls``         — call history for a conversation
* ``GET    /api/calls/ice-servers`` / ``/api/webrtc/ice_servers`` — STUN / TURN config

Every mutating route verifies the caller is a member of the call's
conversation — an authenticated user cannot signal into a conversation
they are not part of (spec §26.8 / §23.83 security note).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

from aiohttp import web

from .. import app_keys as K
from ..domain.call import CallQualitySample
from ..services.call_service import (
    CallConversationError,
    CallNotFoundError,
)
from .base import BaseView

log = logging.getLogger(__name__)


def _make_turn_credential(
    secret: str,
    user_id: str,
    *,
    ttl_seconds: int = 3600,
) -> tuple[str, str]:
    """coturn-style time-limited TURN credential (section 26.7).

    Returns ``(username, password)`` where ``username = "<expiry>:<user>"``
    and ``password = base64(HMAC-SHA1(secret, username))``. The TURN
    server validates by recomputing the HMAC and checking ``expiry``
    is in the future.
    """
    expiry = int(time.time()) + max(60, int(ttl_seconds))
    username = f"{expiry}:{user_id}"
    digest = hmac.new(
        secret.encode("utf-8"),
        username.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    credential = base64.b64encode(digest).decode("ascii")
    return username, credential


async def _require_call_participant(view, call_id: str) -> web.Response | None:
    """Return a 403 Response if ``view.user`` isn't a member of *call_id*'s
    conversation, else ``None``.

    Used by every per-call mutating route. The membership check prefers
    the persisted row (``call_repo.get_call``) because the in-memory
    record can be absent for a call that has transitioned to ended /
    missed but the row is still queryable.
    """
    ctx = view.user
    if ctx is None or ctx.user_id is None:
        return web.json_response({"error": "unauthenticated"}, status=401)
    call_repo = view.svc(K.call_repo_key)
    conv_repo = view.svc(K.conversation_repo_key)
    session = await call_repo.get_call(call_id)
    if session is None:
        return web.json_response({"error": "call_not_found"}, status=404)
    if not session.conversation_id:
        # Federated-inbound calls may lack a conversation binding; in
        # that case fall back to the call-participant set.
        if ctx.user_id not in session.participant_user_ids:
            return web.json_response({"error": "forbidden"}, status=403)
        return None
    members = await conv_repo.list_members(session.conversation_id)
    username = ctx.username
    if not any(m.username == username and m.deleted_at is None for m in members):
        return web.json_response({"error": "forbidden"}, status=403)
    return None


class CallCollectionView(BaseView):
    """``POST /api/calls`` — initiate a call inside a conversation (§26.2)."""

    async def post(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        body = await self.body()
        conversation_id = body.get("conversation_id")
        sdp_offer = body.get("sdp_offer")
        call_type = body.get("call_type", "audio")
        if not conversation_id or not sdp_offer:
            return web.json_response(
                {
                    "error": "missing_fields",
                    "required": ["conversation_id", "sdp_offer"],
                },
                status=422,
            )
        svc = self.svc(K.call_signaling_service_key)
        try:
            result = await svc.initiate_call(
                caller_user_id=ctx.user_id,
                conversation_id=conversation_id,
                call_type=call_type,
                sdp_offer=sdp_offer,
            )
        except PermissionError as exc:
            return web.json_response(
                {"error": "forbidden", "detail": str(exc)}, status=403
            )
        except CallConversationError as exc:
            return web.json_response(
                {"error": "invalid_conversation", "detail": str(exc)}, status=422
            )
        except ValueError as exc:
            return web.json_response(
                {"error": "invalid_request", "detail": str(exc)}, status=422
            )
        return web.json_response(result, status=201)


class CallAnswerView(BaseView):
    """``POST /api/calls/{call_id}/answer`` — submit SDP answer."""

    async def post(self) -> web.Response:
        call_id = self.match("call_id")
        guard = await _require_call_participant(self, call_id)
        if guard is not None:
            return guard
        body = await self.body()
        sdp_answer = body.get("sdp_answer")
        if not sdp_answer:
            return web.json_response({"error": "missing sdp_answer"}, status=422)
        svc = self.svc(K.call_signaling_service_key)
        try:
            result = await svc.answer_call(
                call_id=call_id,
                answerer_user_id=self.user.user_id,
                sdp_answer=sdp_answer,
            )
        except CallNotFoundError:
            return web.json_response({"error": "call_not_found"}, status=404)
        except PermissionError as exc:
            return web.json_response(
                {"error": "forbidden", "detail": str(exc)}, status=403
            )
        return web.json_response(result)


class CallIceView(BaseView):
    """``POST /api/calls/{call_id}/ice`` — trickle ICE candidate."""

    async def post(self) -> web.Response:
        call_id = self.match("call_id")
        guard = await _require_call_participant(self, call_id)
        if guard is not None:
            return guard
        body = await self.body()
        candidate = body.get("candidate")
        if candidate is None:
            return web.json_response({"error": "missing candidate"}, status=422)
        svc = self.svc(K.call_signaling_service_key)
        try:
            await svc.add_ice_candidate(
                call_id=call_id,
                from_user_id=self.user.user_id,
                candidate=candidate,
            )
        except CallNotFoundError:
            return web.json_response({"error": "call_not_found"}, status=404)
        return web.Response(status=204)


class CallHangupView(BaseView):
    """``POST /api/calls/{call_id}/hangup`` — end the call."""

    async def post(self) -> web.Response:
        call_id = self.match("call_id")
        guard = await _require_call_participant(self, call_id)
        if guard is not None:
            return guard
        svc = self.svc(K.call_signaling_service_key)
        try:
            await svc.hangup(call_id=call_id, hanger_user_id=self.user.user_id)
        except PermissionError as exc:
            return web.json_response(
                {"error": "forbidden", "detail": str(exc)}, status=403
            )
        return web.Response(status=204)


class CallDeclineView(BaseView):
    """``POST /api/calls/{call_id}/decline`` — callee refuses a ringing call."""

    async def post(self) -> web.Response:
        call_id = self.match("call_id")
        guard = await _require_call_participant(self, call_id)
        if guard is not None:
            return guard
        svc = self.svc(K.call_signaling_service_key)
        try:
            await svc.decline(
                call_id=call_id,
                decliner_user_id=self.user.user_id,
            )
        except PermissionError as exc:
            return web.json_response(
                {"error": "forbidden", "detail": str(exc)}, status=403
            )
        return web.Response(status=204)


class CallJoinView(BaseView):
    """``POST /api/calls/{call_id}/join`` — late-join a group call (§26.8)."""

    async def post(self) -> web.Response:
        call_id = self.match("call_id")
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        # Membership is checked inside the service for this route since
        # the joiner is not yet a participant.
        body = await self.body()
        raw_offers = body.get("sdp_offers") or {}
        if not isinstance(raw_offers, dict):
            return web.json_response(
                {"error": "sdp_offers must be an object"},
                status=422,
            )
        sdp_offers = {str(k): str(v) for k, v in raw_offers.items()}
        svc = self.svc(K.call_signaling_service_key)
        try:
            result = await svc.join_call(
                call_id=call_id,
                joiner_user_id=ctx.user_id,
                sdp_offers=sdp_offers,
            )
        except CallNotFoundError:
            return web.json_response({"error": "call_not_found"}, status=404)
        except PermissionError as exc:
            return web.json_response(
                {"error": "forbidden", "detail": str(exc)}, status=403
            )
        return web.json_response(result, status=200)


class CallQualityView(BaseView):
    """``POST /api/calls/{call_id}/quality`` — record a WebRTC stats sample.

    ``GET /api/calls/{call_id}/quality`` — list persisted samples (admin
    drill-down; non-admins get their own samples filtered server-side).
    """

    async def post(self) -> web.Response:
        call_id = self.match("call_id")
        guard = await _require_call_participant(self, call_id)
        if guard is not None:
            return guard
        body = await self.body()
        svc = self.svc(K.call_signaling_service_key)
        sample = CallQualitySample(
            call_id=call_id,
            reporter_user_id=self.user.user_id,
            sampled_at=int(body.get("sampled_at") or time.time()),
            rtt_ms=_coerce_int(body.get("rtt_ms")),
            jitter_ms=_coerce_int(body.get("jitter_ms")),
            loss_pct=_coerce_float(body.get("loss_pct")),
            audio_bitrate=_coerce_int(body.get("audio_bitrate")),
            video_bitrate=_coerce_int(body.get("video_bitrate")),
        )
        await svc.record_quality_sample(sample)
        return web.Response(status=204)

    async def get(self) -> web.Response:
        call_id = self.match("call_id")
        guard = await _require_call_participant(self, call_id)
        if guard is not None:
            return guard
        call_repo = self.svc(K.call_repo_key)
        samples = await call_repo.list_quality_samples(call_id)
        return web.json_response(
            {
                "call_id": call_id,
                "samples": [
                    {
                        "reporter_user_id": s.reporter_user_id,
                        "sampled_at": s.sampled_at,
                        "rtt_ms": s.rtt_ms,
                        "jitter_ms": s.jitter_ms,
                        "loss_pct": s.loss_pct,
                        "audio_bitrate": s.audio_bitrate,
                        "video_bitrate": s.video_bitrate,
                    }
                    for s in samples
                ],
            }
        )


class CallActiveView(BaseView):
    """``GET /api/calls/active`` — list this user's active calls.

    Merges the in-memory hot-path records (ringing + in-progress) with
    any persisted ``active``/``ringing`` rows so a page reload during a
    live call still surfaces the call.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        svc = self.svc(K.call_signaling_service_key)
        call_repo = self.svc(K.call_repo_key)
        # Hot path.
        hot = {c.call_id: _hot_dict(c) for c in svc.list_calls_for_user(ctx.user_id)}
        # Cold path.
        cold = await call_repo.list_active(user_id=ctx.user_id)
        for c in cold:
            hot.setdefault(c.id, _cold_dict(c))
        return web.json_response(list(hot.values()))


class ConversationCallHistoryView(BaseView):
    """``GET /api/conversations/{id}/calls`` — call history for a DM."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        conversation_id = self.match("id")
        conv_repo = self.svc(K.conversation_repo_key)
        members = await conv_repo.list_members(conversation_id)
        if not any(
            m.username == ctx.username and m.deleted_at is None for m in members
        ):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            limit = int(self.request.query.get("limit", "50"))
        except ValueError:
            limit = 50
        limit = max(1, min(200, limit))
        call_repo = self.svc(K.call_repo_key)
        rows = await call_repo.list_history_for_conversation(
            conversation_id,
            limit=limit,
        )
        out = []
        for r in rows:
            samples = await call_repo.list_quality_samples(r.id)
            avg_rtt = _avg(s.rtt_ms for s in samples)
            avg_loss = _avg(s.loss_pct for s in samples)
            out.append(
                {
                    "call_id": r.id,
                    "conversation_id": r.conversation_id,
                    "initiator_user_id": r.initiator_user_id,
                    "callee_user_id": r.callee_user_id,
                    "call_type": r.call_type,
                    "status": r.status,
                    "participant_user_ids": list(r.participant_user_ids),
                    "started_at": r.started_at,
                    "connected_at": r.connected_at,
                    "ended_at": r.ended_at,
                    "duration_seconds": r.duration_seconds,
                    "avg_rtt_ms": avg_rtt,
                    "avg_loss_pct": avg_loss,
                }
            )
        return web.json_response({"calls": out})


class IceServersView(BaseView):
    """``GET /api/calls/ice-servers`` and ``/api/webrtc/ice_servers`` — ICE config.

    Public-by-design (no sensitive data leak) but still requires
    auth so bots can't enumerate the operator's TURN credentials.
    When ``webrtc_turn_secret`` is configured we issue HMAC-derived
    time-limited credentials per coturn's REST API spec instead of
    a static username/password — strongly preferred for production.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None:
            return web.json_response({"error": "unauthenticated"}, status=401)
        cfg = self.svc(K.config_key)
        servers: list[dict] = []
        if cfg.webrtc_stun_url:
            servers.append({"urls": [cfg.webrtc_stun_url]})
        if cfg.webrtc_turn_url:
            entry: dict = {"urls": [cfg.webrtc_turn_url]}
            if getattr(cfg, "webrtc_turn_secret", "") and ctx.user_id:
                username, credential = _make_turn_credential(
                    cfg.webrtc_turn_secret,
                    ctx.user_id,
                    ttl_seconds=getattr(cfg, "webrtc_turn_ttl_seconds", 3600),
                )
                entry["username"] = username
                entry["credential"] = credential
            else:
                if cfg.webrtc_turn_user:
                    entry["username"] = cfg.webrtc_turn_user
                if cfg.webrtc_turn_cred:
                    entry["credential"] = cfg.webrtc_turn_cred
            servers.append(entry)
        return web.json_response({"ice_servers": servers})


# ─── Helpers ───────────────────────────────────────────────────────────


def _coerce_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _coerce_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _avg(values) -> float | None:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _hot_dict(c) -> dict:
    return {
        "call_id": c.call_id,
        "status": c.status,
        "caller": c.caller_user_id,
        "callee": c.callee_user_id,
        "call_type": c.call_type,
        "created_at": c.created_at,
        "conversation_id": c.conversation_id,
    }


def _cold_dict(c) -> dict:
    return {
        "call_id": c.id,
        "status": c.status,
        "caller": c.initiator_user_id,
        "callee": c.callee_user_id,
        "call_type": c.call_type,
        "created_at": c.started_at,
        "conversation_id": c.conversation_id,
    }
