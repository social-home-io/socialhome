"""CallSignalingService — backend signalling relay for voice/video (§26).

The backend is **never** in the media path: it only relays SDP offers /
answers and trickle ICE candidates between callers. All audio/video is
peer-to-peer via WebRTC, mandatorily DTLS-SRTP encrypted.

Two transport paths converge here:

* **Local calls** — both parties on the same household instance. The
  service stores the call state and forwards events through the
  in-process WebSocket manager (when available).
* **Federated calls** — caller and callee live on different instances.
  ``CALL_OFFER`` / ``CALL_ANSWER`` / ``CALL_ICE_CANDIDATE`` /
  ``CALL_HANGUP`` events arrive via :class:`FederationService` and land
  in :meth:`handle_federated_signal`, which routes them to the local
  user's WS session (or to a stored ringing record if the user is not
  yet connected).

State is persisted to ``call_sessions`` via an injected
:class:`~social_home.repositories.call_repo.AbstractCallRepo`. The
in-memory :class:`CallRecord` remains the hot path for SDP routing.
Missed calls (ringing > 90 s) leave a ``type='call_event'`` system
message in the DM thread via the conversation repo (§26.8 line 26445).

SDP integrity: every outbound SDP is signed with the instance's Ed25519
identity key via :func:`federation.sdp_signing.sign_rtc_offer`. Inbound
SDPs from federation are verified against the sender's identity key
*before* being forwarded to the local browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..domain.call import CallQualitySample, CallSession
from ..domain.conversation import ConversationMessage
from ..domain.federation import FederationEventType
from ..federation.sdp_signing import (
    sign_rtc_offer,
    signed_sdp_from_dict,
    signed_sdp_to_dict,
    verify_rtc_offer,
)
from ..repositories.call_repo import AbstractCallRepo
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


# ─── Constants ────────────────────────────────────────────────────────────

#: Maximum lifetime of a "ringing" call before it is auto-missed (§26.8).
RINGING_TTL_SECONDS: int = 90

#: Hard cap on simultaneous in-flight calls per user (DoS guard).
MAX_CALLS_PER_USER: int = 16


# ─── Exceptions ──────────────────────────────────────────────────────────


class CallNotFoundError(KeyError):
    """Raised when a call_id does not match a live or persisted call."""


class CallConversationError(ValueError):
    """Raised when the conversation is missing / unknown / unsupported."""


@dataclass(slots=True)
class CallRecord:
    """Hot-path server-side bookkeeping for a single call.

    The authoritative *persisted* state lives in ``call_sessions``; this
    is only the SDP-routing scratchpad kept for sub-millisecond lookups
    during a live call.
    """

    call_id: str
    conversation_id: str
    caller_user_id: str
    callee_user_id: str | None
    callee_instance_id: str | None
    call_type: str  # "audio" | "video"
    status: str = "ringing"  # ringing | in_progress | ended
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    pending_signals: list[dict] = field(default_factory=list)
    # Group-call mesh participants (§26.4). 1:1 calls keep this at
    # {caller, callee}; group calls grow as members accept.
    participants: set[str] = field(default_factory=set)


# ─── Service ──────────────────────────────────────────────────────────────


class CallSignalingService:
    """Relay backend for WebRTC calls.

    The service is intentionally minimal: it does not negotiate SDP,
    decode media, or track quality. Everything flows through. The hot
    signalling state lives in memory (``_calls`` dict) and the cold
    history lives in the ``call_sessions`` table via *call_repo*.

    Parameters
    ----------
    call_repo:
        Persists the call lifecycle to ``call_sessions`` + quality
        samples to ``call_quality_samples``.
    conversation_repo:
        Resolves conversation membership + writes missed/ended/declined
        ``call_event`` system messages to the DM thread.
    user_repo:
        Maps ``username`` ↔ ``user_id`` so membership checks work with
        the conversation-member schema (keyed on ``username``).
    federation_service:
        Used to ship CALL_* events to a remote instance for federated
        calls. May be ``None`` for local-only deployments.
    own_identity_seed:
        Ed25519 private seed used to sign outbound SDPs.
    ws_manager:
        Optional :class:`WebSocketManager` for delivering signals to the
        local browser. May be attached after construction.
    """

    __slots__ = (
        "_call_repo",
        "_conv_repo",
        "_user_repo",
        "_federation",
        "_own_seed",
        "_ws_manager",
        "_calls",
        "_per_user",
        "_push",
    )

    def __init__(
        self,
        *,
        call_repo: AbstractCallRepo,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        own_identity_seed: bytes,
        federation_service=None,
        ws_manager=None,
    ) -> None:
        self._call_repo = call_repo
        self._conv_repo = conversation_repo
        self._user_repo = user_repo
        self._federation = federation_service
        self._own_seed = own_identity_seed
        self._ws_manager = ws_manager
        self._calls: dict[str, CallRecord] = {}
        self._per_user: dict[str, set[str]] = {}
        # Optional push service for missed-call notifications (Phase CH).
        self._push = None

    def attach_federation(self, federation_service) -> None:
        self._federation = federation_service

    def attach_ws_manager(self, ws_manager) -> None:
        self._ws_manager = ws_manager

    def attach_push_service(self, push_service) -> None:
        """Wire :class:`PushService` for missed-call notifications (§26.8)."""
        self._push = push_service

    # ─── Public lifecycle ─────────────────────────────────────────────────

    async def initiate_call(
        self,
        *,
        caller_user_id: str,
        conversation_id: str,
        call_type: str,
        sdp_offer: str,
    ) -> dict:
        """Begin a call inside *conversation_id* (§26.2).

        * Verifies the caller is a member of the conversation.
        * For 1:1 DMs resolves the single callee; group DMs fan out to
          every other member (and leave ``callee_user_id=None`` on the
          record so the group mesh handles individual ring events).
        * Persists the call row; emits a ``call_event`` system message
          in the DM thread (``event="started"``).
        * Ships ``CALL_OFFER`` to each remote participant and pushes
          ``call.ringing`` on the WS channel for each local one.
        """
        if call_type not in ("audio", "video"):
            raise ValueError(f"Invalid call_type: {call_type!r}")
        if not sdp_offer:
            raise ValueError("Empty SDP offer")
        self._enforce_user_cap(caller_user_id)

        caller_user = await self._user_repo.get_by_user_id(caller_user_id)
        if caller_user is None:
            raise PermissionError(
                f"Unknown caller user_id {caller_user_id!r}",
            )
        caller_username = caller_user.username

        local_callees, remote_callees = await self._resolve_conversation_peers(
            conversation_id,
            exclude_username=caller_username,
        )
        if not local_callees and not remote_callees:
            raise CallConversationError(
                "Conversation has no other participants to call",
            )

        call_id = "call-" + secrets.token_urlsafe(16)
        participants = {caller_user_id}
        participants.update(u.user_id for u in local_callees)
        participants.update(user_id for _inst, user_id in remote_callees)

        # For 1:1 DMs there is a single callee; for groups we leave it
        # None (the mesh tracks each peer individually).
        is_one_to_one = len(participants) == 2
        primary_callee_id: str | None = None
        primary_callee_instance: str | None = None
        if is_one_to_one:
            if local_callees:
                primary_callee_id = local_callees[0].user_id
            else:
                primary_callee_instance, primary_callee_id = remote_callees[0]

        record = CallRecord(
            call_id=call_id,
            conversation_id=conversation_id,
            caller_user_id=caller_user_id,
            callee_user_id=primary_callee_id,
            callee_instance_id=primary_callee_instance,
            call_type=call_type,
            participants=set(participants),
        )
        self._calls[call_id] = record
        self._per_user.setdefault(caller_user_id, set()).add(call_id)

        session = CallSession(
            id=call_id,
            conversation_id=conversation_id,
            initiator_user_id=caller_user_id,
            callee_user_id=primary_callee_id,
            call_type=call_type,
            status="ringing",
            participant_user_ids=tuple(sorted(participants)),
        )
        await self._call_repo.save_call(session)
        await self._emit_call_event_message(session, event="started")

        signed = sign_rtc_offer(sdp_offer, "offer", identity_seed=self._own_seed)
        signed_dict = signed_sdp_to_dict(signed)

        # Ring every local callee.
        for u in local_callees:
            self._per_user.setdefault(u.user_id, set()).add(call_id)
            await self._fanout_to_user(
                u.user_id,
                {
                    "type": "call.ringing",
                    "call_id": call_id,
                    "conversation_id": conversation_id,
                    "from_user": caller_user_id,
                    "call_type": call_type,
                    "signed_sdp": signed_dict,
                },
            )
        # Federate each remote callee.
        if self._federation is not None:
            for remote_instance, remote_user_id in remote_callees:
                try:
                    await self._federation.send_event(
                        to_instance_id=remote_instance,
                        event_type=FederationEventType.CALL_OFFER,
                        payload={
                            "call_id": call_id,
                            "conversation_id": conversation_id,
                            "from_user": caller_user_id,
                            "to_user": remote_user_id,
                            "call_type": call_type,
                            "signed_sdp": signed_dict,
                        },
                    )
                except Exception as exc:  # pragma: no cover
                    log.warning(
                        "CALL_OFFER to %s failed: %s",
                        remote_instance,
                        exc,
                    )

        return {
            "call_id": call_id,
            "status": record.status,
            "conversation_id": conversation_id,
            "callee_user_id": primary_callee_id,
            "callee_instance_id": primary_callee_instance,
        }

    async def answer_call(
        self,
        *,
        call_id: str,
        answerer_user_id: str,
        sdp_answer: str,
    ) -> dict:
        """Submit the SDP answer for a ringing call."""
        record = self._calls.get(call_id)
        if record is None:
            raise CallNotFoundError(call_id)
        if (
            answerer_user_id not in record.participants
            or answerer_user_id == record.caller_user_id
        ):
            raise PermissionError("Only a callee may answer this call")
        record.status = "in_progress"
        record.last_activity = time.time()

        await self._call_repo.transition(
            call_id,
            status="active",
            connected_at=_now_iso(),
        )

        signed = sign_rtc_offer(sdp_answer, "answer", identity_seed=self._own_seed)
        signed_dict = signed_sdp_to_dict(signed)

        caller_instance = await self._instance_of(record.caller_user_id)
        if (
            caller_instance
            and self._federation is not None
            and caller_instance != self._federation.own_instance_id
        ):
            await self._federation.send_event(
                to_instance_id=caller_instance,
                event_type=FederationEventType.CALL_ANSWER,
                payload={
                    "call_id": call_id,
                    "signed_sdp": signed_dict,
                },
            )
        await self._fanout_to_user(
            record.caller_user_id,
            {
                "type": "call.answered",
                "call_id": call_id,
                "signed_sdp": signed_dict,
            },
        )
        return {"call_id": call_id, "status": record.status}

    async def add_ice_candidate(
        self,
        *,
        call_id: str,
        from_user_id: str,
        candidate: dict,
    ) -> None:
        """Trickle a single ICE candidate to the other side."""
        record = self._calls.get(call_id)
        if record is None:
            raise CallNotFoundError(call_id)
        record.last_activity = time.time()

        # For 1:1 calls deliver to the one other participant; for group
        # calls fan out to every peer except the sender.
        others = [u for u in record.participants if u != from_user_id]
        for other in others:
            other_instance = await self._instance_of(other)
            if (
                other_instance
                and self._federation is not None
                and other_instance != self._federation.own_instance_id
            ):
                await self._federation.send_event(
                    to_instance_id=other_instance,
                    event_type=FederationEventType.CALL_ICE_CANDIDATE,
                    payload={
                        "call_id": call_id,
                        "from_user": from_user_id,
                        "candidate": candidate,
                    },
                )
            else:
                await self._fanout_to_user(
                    other,
                    {
                        "type": "call.ice_candidate",
                        "call_id": call_id,
                        "from_user": from_user_id,
                        "candidate": candidate,
                    },
                )

    async def decline(self, *, call_id: str, decliner_user_id: str) -> None:
        """A callee refuses a ringing call (§26.8). Emits
        ``CALL_DECLINE`` + a ``call_event`` row in the DM thread.
        """
        record = self._calls.get(call_id)
        if record is None:
            return
        if (
            decliner_user_id not in record.participants
            or decliner_user_id == record.caller_user_id
        ):
            raise PermissionError("Only a callee may decline")
        record.status = "declined"

        session = await self._call_repo.transition(
            call_id,
            status="declined",
            ended_at=_now_iso(),
        )
        if session is not None:
            await self._emit_call_event_message(session, event="declined")

        caller = record.caller_user_id
        caller_instance = await self._instance_of(caller)
        if (
            caller_instance
            and self._federation is not None
            and caller_instance != self._federation.own_instance_id
        ):
            await self._federation.send_event(
                to_instance_id=caller_instance,
                event_type=FederationEventType.CALL_DECLINE,
                payload={"call_id": call_id, "decliner_user": decliner_user_id},
            )
        await self._fanout_to_user(
            caller or "",
            {
                "type": "call.declined",
                "call_id": call_id,
                "by": decliner_user_id,
            },
        )
        self._cleanup_call(call_id)

    async def hangup(self, *, call_id: str, hanger_user_id: str) -> None:
        """Terminate a call. Writes ``ended`` with a duration + fires
        ``CALL_HANGUP`` to every other peer."""
        record = self._calls.get(call_id)
        if record is None:
            return
        if hanger_user_id not in record.participants:
            raise PermissionError("Not a participant in this call")
        record.status = "ended"

        # Duration = now − connected_at if we have it, else 0.
        persisted = await self._call_repo.get_call(call_id)
        duration = None
        if persisted is not None and persisted.connected_at:
            try:
                start = datetime.fromisoformat(persisted.connected_at)
                duration = int(
                    (datetime.now(timezone.utc) - start).total_seconds(),
                )
            except ValueError:  # pragma: no cover
                duration = 0
        session = await self._call_repo.transition(
            call_id,
            status="ended",
            ended_at=_now_iso(),
            duration_seconds=duration,
        )
        if session is not None:
            await self._emit_call_event_message(session, event="ended")

        others = [u for u in record.participants if u != hanger_user_id]
        for other in others:
            other_instance = await self._instance_of(other)
            if (
                other_instance
                and self._federation is not None
                and other_instance != self._federation.own_instance_id
            ):
                await self._federation.send_event(
                    to_instance_id=other_instance,
                    event_type=FederationEventType.CALL_HANGUP,
                    payload={"call_id": call_id, "hanger_user": hanger_user_id},
                )
            else:
                await self._fanout_to_user(
                    other,
                    {
                        "type": "call.ended",
                        "call_id": call_id,
                        "by": hanger_user_id,
                    },
                )
        self._cleanup_call(call_id)

    async def join_call(
        self,
        *,
        call_id: str,
        joiner_user_id: str,
        sdp_offers: dict[str, str],
    ) -> dict:
        """Late-join an in-progress group call (spec §26.8 lines 26532-26605).

        Membership-verified: the joiner must be a member of the call's
        conversation. Fans one ``CALL_OFFER`` per existing participant.
        """
        session = await self._call_repo.get_call(call_id)
        if session is None:
            raise CallNotFoundError(call_id)
        joiner = await self._user_repo.get_by_user_id(joiner_user_id)
        if joiner is None:
            raise PermissionError("Unknown joiner user_id")
        members = await self._conv_repo.list_members(session.conversation_id)
        if not any(
            m.username == joiner.username and m.deleted_at is None for m in members
        ):
            raise PermissionError(
                "User is not a member of this conversation",
            )

        participants = set(session.participant_user_ids) | {joiner_user_id}
        joined: list[str] = []
        record = self._calls.get(call_id)
        if record is not None:
            record.participants = participants
            record.last_activity = time.time()
            self._per_user.setdefault(joiner_user_id, set()).add(call_id)
        await self._call_repo.transition(
            call_id,
            status=session.status,
            participant_user_ids=tuple(sorted(participants)),
        )

        for participant_id, sdp_offer in sdp_offers.items():
            if participant_id == joiner_user_id:
                continue
            if participant_id not in session.participant_user_ids:
                continue
            signed = sign_rtc_offer(
                sdp_offer,
                "offer",
                identity_seed=self._own_seed,
            )
            signed_dict = signed_sdp_to_dict(signed)
            peer_instance = await self._instance_of(participant_id)
            if (
                peer_instance
                and self._federation is not None
                and peer_instance != self._federation.own_instance_id
            ):
                await self._federation.send_event(
                    to_instance_id=peer_instance,
                    event_type=FederationEventType.CALL_OFFER,
                    payload={
                        "call_id": call_id,
                        "conversation_id": session.conversation_id,
                        "from_user": joiner_user_id,
                        "to_user": participant_id,
                        "call_type": session.call_type,
                        "signed_sdp": signed_dict,
                        "late_join": True,
                    },
                )
            else:
                await self._fanout_to_user(
                    participant_id,
                    {
                        "type": "call.peer_join",
                        "call_id": call_id,
                        "joiner_user_id": joiner_user_id,
                        "signed_sdp": signed_dict,
                    },
                )
            joined.append(participant_id)
        return {"call_id": call_id, "joined": joined}

    # ─── Federation inbound ───────────────────────────────────────────────

    async def handle_federated_signal(self, event) -> None:
        """Dispatch an incoming CALL_* federation event.

        Verifies signed SDPs against the sender's identity key, then
        fans the matching ``call.*`` WS event to the local user.
        """
        et = event.event_type
        payload = event.payload or {}
        call_id = payload.get("call_id") or ""
        if not call_id:
            return

        # Pull the sender's public key for SDP verification.
        sender_pk: bytes | None = None
        if self._federation is not None:
            try:
                inst = await self._federation._federation_repo.get_instance(
                    event.from_instance,
                )
                if inst is not None:
                    sender_pk = bytes.fromhex(inst.remote_identity_pk)
            except Exception:
                sender_pk = None

        signed_dict = payload.get("signed_sdp")
        if signed_dict and sender_pk is not None:
            try:
                signed = signed_sdp_from_dict(signed_dict)
                if not verify_rtc_offer(signed, remote_public_key=sender_pk):
                    log.warning("call signal: SDP signature failed (%s)", call_id)
                    return
            except Exception as exc:
                log.warning("call signal: bad signed_sdp (%s): %s", call_id, exc)
                return

        match et:
            case FederationEventType.CALL_OFFER:
                callee_user = payload.get("to_user") or ""
                caller = payload.get("from_user") or ""
                conversation_id = payload.get("conversation_id") or ""
                new_record = CallRecord(
                    call_id=call_id,
                    conversation_id=conversation_id,
                    caller_user_id=caller,
                    callee_user_id=callee_user,
                    callee_instance_id=event.from_instance,
                    call_type=str(payload.get("call_type", "audio")),
                    participants={caller, callee_user},
                )
                self._calls[call_id] = new_record
                self._per_user.setdefault(callee_user, set()).add(call_id)
                await self._fanout_to_user(
                    callee_user,
                    {
                        "type": "call.ringing",
                        "call_id": call_id,
                        "conversation_id": conversation_id,
                        "from_user": caller,
                        "call_type": payload.get("call_type"),
                        "signed_sdp": signed_dict,
                    },
                )
            case FederationEventType.CALL_ANSWER:
                record = self._calls.get(call_id)
                if record is not None:
                    record.status = "in_progress"
                    record.last_activity = time.time()
                    await self._fanout_to_user(
                        record.caller_user_id,
                        {
                            "type": "call.answered",
                            "call_id": call_id,
                            "signed_sdp": signed_dict,
                        },
                    )
            case FederationEventType.CALL_ICE_CANDIDATE | FederationEventType.CALL_ICE:
                record = self._calls.get(call_id)
                if record is not None:
                    record.last_activity = time.time()
                    target = (
                        record.callee_user_id
                        if payload.get("from_user") == record.caller_user_id
                        else record.caller_user_id
                    )
                    await self._fanout_to_user(
                        target or "",
                        {
                            "type": "call.ice_candidate",
                            "call_id": call_id,
                            "candidate": payload.get("candidate"),
                        },
                    )
            case (
                FederationEventType.CALL_HANGUP
                | FederationEventType.CALL_END
                | FederationEventType.CALL_DECLINE
                | FederationEventType.CALL_BUSY
            ):
                record = self._calls.get(call_id)
                if record is not None:
                    target = (
                        record.callee_user_id
                        if payload.get("hanger_user") == record.caller_user_id
                        else record.caller_user_id
                    )
                    await self._fanout_to_user(
                        target or "",
                        {
                            "type": "call.ended",
                            "call_id": call_id,
                        },
                    )
                    self._cleanup_call(call_id)
            case FederationEventType.CALL_QUALITY:
                # Phase CF — persist remote-reported WebRTC quality sample.
                try:
                    sample = CallQualitySample(
                        call_id=call_id,
                        reporter_user_id=str(payload.get("reporter_user") or ""),
                        sampled_at=int(payload.get("sampled_at") or time.time()),
                        rtt_ms=payload.get("rtt_ms"),
                        jitter_ms=payload.get("jitter_ms"),
                        loss_pct=payload.get("loss_pct"),
                        audio_bitrate=payload.get("audio_bitrate"),
                        video_bitrate=payload.get("video_bitrate"),
                    )
                    await self._call_repo.save_quality_sample(sample)
                except Exception as exc:
                    log.debug("CALL_QUALITY ingest failed (%s): %s", call_id, exc)

    # ─── Quality sampling (local) ─────────────────────────────────────────

    async def record_quality_sample(self, sample: CallQualitySample) -> None:
        """Persist a locally-collected WebRTC getStats() sample.

        Called by ``POST /api/calls/{id}/quality``. Forwards to federated
        peers so each side has a full view of the call's health.
        """
        await self._call_repo.save_quality_sample(sample)
        # Best-effort federation — peers only. Skip self-loop.
        if self._federation is None:
            return
        record = self._calls.get(sample.call_id)
        if record is None:
            return
        for peer in record.participants - {sample.reporter_user_id}:
            peer_instance = await self._instance_of(peer)
            if peer_instance and peer_instance != self._federation.own_instance_id:
                try:
                    await self._federation.send_event(
                        to_instance_id=peer_instance,
                        event_type=FederationEventType.CALL_QUALITY,
                        payload={
                            "call_id": sample.call_id,
                            "reporter_user": sample.reporter_user_id,
                            "sampled_at": sample.sampled_at,
                            "rtt_ms": sample.rtt_ms,
                            "jitter_ms": sample.jitter_ms,
                            "loss_pct": sample.loss_pct,
                            "audio_bitrate": sample.audio_bitrate,
                            "video_bitrate": sample.video_bitrate,
                        },
                    )
                except Exception as exc:  # pragma: no cover
                    log.debug("CALL_QUALITY fanout failed: %s", exc)

    # ─── Inspection (for routes / tests) ──────────────────────────────────

    def get_call(self, call_id: str) -> CallRecord | None:
        return self._calls.get(call_id)

    def list_calls_for_user(self, user_id: str) -> list[CallRecord]:
        return [
            self._calls[c]
            for c in self._per_user.get(user_id, set())
            if c in self._calls
        ]

    async def gc_expired(self) -> int:
        """Mark ringing calls older than :data:`RINGING_TTL_SECONDS`
        as ``missed`` (not deleted) and emit a ``call_event`` system
        message in each call's DM thread. Returns the count missed.

        Called from :class:`StaleCallCleanupScheduler`; runs every 30 s.
        """
        missed = await self._call_repo.end_stale_calls(
            older_than_seconds=RINGING_TTL_SECONDS,
        )
        for session in missed:
            await self._emit_call_event_message(session, event="missed")
            await self._emit_missed_call_push(session)
            # Drop the in-memory hot-path record if still present.
            self._cleanup_call(session.id)
        return len(missed)

    # ─── Group-call participants (sync helpers for tests) ────────────────

    def add_participant(self, call_id: str, user_id: str) -> bool:
        """Add a user to an active call's in-memory participant set."""
        rec = self._calls.get(call_id)
        if rec is None:
            return False
        if user_id in rec.participants:
            return False
        rec.participants.add(user_id)
        rec.last_activity = time.time()
        self._per_user.setdefault(user_id, set()).add(call_id)
        return True

    def remove_participant(self, call_id: str, user_id: str) -> bool:
        rec = self._calls.get(call_id)
        if rec is None:
            return False
        if user_id not in rec.participants:
            return False
        rec.participants.discard(user_id)
        if user_id in self._per_user:
            self._per_user[user_id].discard(call_id)
            if not self._per_user[user_id]:
                self._per_user.pop(user_id, None)
        if not rec.participants:
            self._cleanup_call(call_id)
        return True

    def participants_for(self, call_id: str) -> set[str]:
        rec = self._calls.get(call_id)
        return set(rec.participants) if rec else set()

    # ─── Internals ────────────────────────────────────────────────────────

    def _enforce_user_cap(self, user_id: str) -> None:
        if len(self._per_user.get(user_id, set())) >= MAX_CALLS_PER_USER:
            raise RuntimeError("Too many concurrent calls for user")

    def _cleanup_call(self, call_id: str) -> None:
        rec = self._calls.pop(call_id, None)
        if rec is None:
            return
        for u in rec.participants:
            if u in self._per_user:
                self._per_user[u].discard(call_id)
                if not self._per_user[u]:
                    self._per_user.pop(u, None)

    async def _resolve_conversation_peers(
        self,
        conversation_id: str,
        *,
        exclude_username: str,
    ) -> tuple[list, list]:
        """Return ``(local_callees: list[User], remote_callees: [(inst, uid)])``.

        Raises ``PermissionError`` if *exclude_username* isn't a member.
        """
        members = await self._conv_repo.list_members(conversation_id)
        if not any(
            m.username == exclude_username and m.deleted_at is None for m in members
        ):
            raise PermissionError(
                "User is not a member of this conversation",
            )
        local_callees = []
        for m in members:
            if m.deleted_at is not None or m.username == exclude_username:
                continue
            u = await self._user_repo.get(m.username)
            if u is not None:
                local_callees.append(u)
        remote_callees: list[tuple[str, str]] = []
        remotes = await self._conv_repo.list_remote_members(conversation_id)
        for r in remotes:
            remote_user = await self._find_remote_user(
                r.instance_id,
                r.remote_username,
            )
            if remote_user is not None:
                remote_callees.append((r.instance_id, remote_user.user_id))
        return local_callees, remote_callees

    async def _find_remote_user(self, instance_id: str, username: str):
        """Look up a ``RemoteUser`` in the ``remote_users`` table.

        The conversation repo holds ``(instance_id, remote_username)``
        but the call record needs ``user_id``. We resolve via the user
        repo's ``list_remote_for_instance`` helper (cheap — one query
        per federated-group participant).
        """
        for ru in await self._user_repo.list_remote_for_instance(instance_id):
            if ru.remote_username == username:
                return ru
        return None

    async def _instance_of(self, user_id: str | None) -> str | None:
        if not user_id:
            return None
        try:
            return await self._user_repo.get_instance_for_user(user_id)
        except Exception:
            return None

    async def _emit_call_event_message(
        self,
        session: CallSession,
        *,
        event: str,
    ) -> None:
        """Write a ``type='call_event'`` system message in the DM thread.

        Event values: ``'started'``, ``'missed'``, ``'ended'``,
        ``'declined'``. Frontend renders a compact centered row per
        Phase CG4.
        """
        if not session.conversation_id:
            return
        content = json.dumps(
            {
                "event": event,
                "call_id": session.id,
                "call_type": session.call_type,
                "caller_user_id": session.initiator_user_id,
                "callee_user_id": session.callee_user_id,
                "duration_seconds": session.duration_seconds,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        await self._conv_repo.save_message(
            ConversationMessage(
                id="msg-" + secrets.token_urlsafe(12),
                conversation_id=session.conversation_id,
                sender_user_id=session.initiator_user_id,
                content=content,
                created_at=datetime.now(timezone.utc),
                type="call_event",
            )
        )
        try:
            await self._conv_repo.touch_last_message(session.conversation_id)
        except Exception:  # pragma: no cover
            pass

    async def _emit_missed_call_push(self, session: CallSession) -> None:
        """Fire a missed-call push notification (§25.3 — title only)."""
        if self._push is None:
            return
        # Callees = every participant except the initiator; for 1:1 DMs
        # that's a single user_id.
        recipients = [
            uid
            for uid in session.participant_user_ids
            if uid != session.initiator_user_id
        ]
        if not recipients and session.callee_user_id:
            recipients = [session.callee_user_id]
        try:
            await self._push.notify_missed_call(
                recipient_user_ids=recipients,
                caller_user_id=session.initiator_user_id,
                call_id=session.id,
                conversation_id=session.conversation_id,
            )
        except Exception as exc:  # pragma: no cover
            log.debug("missed-call push failed: %s", exc)

    async def _fanout_to_user(self, user_id: str, payload: dict[str, Any]) -> None:
        """Forward an event to all of *user_id*'s WS sessions, if any."""
        if not user_id or self._ws_manager is None:
            return
        try:
            await self._ws_manager.broadcast_to_user(user_id, payload)
        except Exception as exc:
            log.debug("call ws fanout failed for %s: %s", user_id, exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Stale-call cleanup scheduler ────────────────────────────────────────


class StaleCallCleanupScheduler:
    """Background task that marks ringing calls past TTL as ``missed``
    (§26.8). Follows the ``_stop: asyncio.Event`` pattern (CLAUDE.md
    "Schedulers" invariant; reference template:
    :mod:`~social_home.infrastructure.replay_cache_scheduler`).
    """

    __slots__ = ("_service", "_interval", "_task", "_stop")

    def __init__(
        self,
        service: CallSignalingService,
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        self._service = service
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start the background loop. Idempotent — a second call is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="StaleCallCleanup")

    async def stop(self) -> None:
        """Signal exit and wait for the task to drain."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                missed = await self._service.gc_expired()
                if missed:
                    log.debug("stale-call cleanup: marked %d missed", missed)
            except Exception:  # pragma: no cover
                log.exception("stale-call cleanup tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue
