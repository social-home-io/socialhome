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
:class:`~socialhome.repositories.call_repo.AbstractCallRepo`. The
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
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import orjson

from ..domain.call import CallQualitySample, CallSession
from ..domain.conversation import ConversationMessage
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..federation.sdp_signing import (
    sign_rtc_offer,
    signed_sdp_from_dict,
    signed_sdp_to_dict,
    verify_rtc_offer,
)
from ..repositories.call_repo import AbstractCallRepo
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.user_repo import AbstractUserRepo
from .protection_gate import ProtectionGateMixin

log = logging.getLogger(__name__)


# ─── Constants ────────────────────────────────────────────────────────────

#: Maximum lifetime of a "ringing" call before it is auto-missed (§26.8).
RINGING_TTL_SECONDS: int = 90

#: Hard cap on simultaneous in-flight calls per user (DoS guard).
MAX_CALLS_PER_USER: int = 16

#: Most people in one call, caller included. Group calls are a full
#: WebRTC mesh — every browser sends its own audio/video to each of the
#: others — so each extra person costs every participant one more upload.
#: Six keeps a video mesh usable on a home uplink.
MAX_CALL_PARTICIPANTS: int = 6


# ─── Exceptions ──────────────────────────────────────────────────────────


class CallNotFoundError(KeyError):
    """Raised when a call_id does not match a live or persisted call."""


class CallConversationError(ValueError):
    """Raised when the conversation is missing / unknown / unsupported."""


class CallAlreadyAnsweredError(RuntimeError):
    """Raised when a callee who already answered answers again (another
    of their devices)."""


class CallTooLargeError(ValueError):
    """Raised when a call would have more than
    :data:`MAX_CALL_PARTICIPANTS` people in it."""


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
    # Group-call mesh participants (§26.4): everyone invited. 1:1 calls
    # keep this at {caller, callee}.
    participants: set[str] = field(default_factory=set)
    # Who is in the call: the caller plus every callee that answered it.
    answered: set[str] = field(default_factory=set)
    # Who hung up / declined / had their invite withdrawn. The call is
    # over once fewer than two participants are left.
    left: set[str] = field(default_factory=set)

    @property
    def present(self) -> set[str]:
        return self.participants - self.left


# ─── Service ──────────────────────────────────────────────────────────────


#: A call a guardian block (§CP.F2) stands in the way of, as the protected
#: account hears it. The person it blocks hears a personal block's words.
GUARDIAN_BLOCK_CALL_DETAIL = "You can't call this person."
BLOCKED_CALL_DETAIL = "Recipient has you blocked."


class CallSignalingService(ProtectionGateMixin):
    """Relay backend for WebRTC calls.

    Guardian blocks (§CP.F2) hold for calls as for messages: nobody rings,
    joins or is rung across one, local or federated.

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
        "_child_protection",
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
        self._child_protection = None

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
        sdp_offer: str | None = None,
        sdp_offers: dict[str, str] | None = None,
    ) -> dict:
        """Begin a call inside *conversation_id* (§26.2).

        * Verifies the caller is a member of the conversation.
        * For 1:1 DMs resolves the single callee; group DMs fan out to
          every other member (and leave ``callee_user_id=None`` on the
          record so the group mesh handles individual ring events).
        * Group calls are a mesh: *sdp_offers* carries one offer per
          callee (``{user_id: sdp}``); a callee missing from it gets
          *sdp_offer*, and a callee with neither is not rung. The ring
          frame lists every ``participants`` so callees can open their
          legs to each other (see :meth:`join_call`).
        * Persists the call row; emits a ``call_event`` system message
          in the DM thread (``event="started"``).
        * Ships ``CALL_OFFER`` to each remote participant and pushes
          ``call.ringing`` on the WS channel for each local one.
        """
        if call_type not in ("audio", "video"):
            raise ValueError(f"Invalid call_type: {call_type!r}")
        offers = {k: v for k, v in (sdp_offers or {}).items() if v}
        if not sdp_offer and not offers:
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
        # §CP.F2: nobody a guardian block separates from the caller is rung;
        # a protected caller can't call into a conversation seating one.
        blocked = await self._guardian_block_counterparts(caller_user_id)
        if (
            blocked
            and ({u.user_id for u in local_callees} | {r[1] for r in remote_callees})
            & blocked
        ):
            protected = await self._is_protected(caller_user_id)
            if len(local_callees) + len(remote_callees) == 1 or protected:
                raise PermissionError(
                    GUARDIAN_BLOCK_CALL_DETAIL if protected else BLOCKED_CALL_DETAIL
                )
            local_callees = [u for u in local_callees if u.user_id not in blocked]
            remote_callees = [r for r in remote_callees if r[1] not in blocked]
        # A callee we hold no offer for can't be connected — don't ring them.
        local_callees = [u for u in local_callees if offers.get(u.user_id) or sdp_offer]
        remote_callees = [r for r in remote_callees if offers.get(r[1]) or sdp_offer]
        if not local_callees and not remote_callees:
            raise CallConversationError(
                "Conversation has no other participants to call",
            )
        if 1 + len(local_callees) + len(remote_callees) > MAX_CALL_PARTICIPANTS:
            raise CallTooLargeError(
                f"Calls are limited to {MAX_CALL_PARTICIPANTS} people",
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
            answered={caller_user_id},
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

        roster = sorted(participants)

        def signed_offer_for(user_id: str) -> dict:
            sdp = offers.get(user_id) or sdp_offer or ""
            return signed_sdp_to_dict(
                sign_rtc_offer(sdp, "offer", identity_seed=self._own_seed)
            )

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
                    "signed_sdp": signed_offer_for(u.user_id),
                    "participants": roster,
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
                            "signed_sdp": signed_offer_for(remote_user_id),
                            # v_37: every invitee, so a callee on another
                            # household opens its legs to the others.
                            # Older receivers ignore it.
                            "participants": roster,
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
            "participants": roster,
        }

    async def answer_call(
        self,
        *,
        call_id: str,
        answerer_user_id: str,
        sdp_answer: str,
        to_user_id: str | None = None,
    ) -> dict:
        """Submit an SDP answer.

        Without *to_user_id* (or with the caller) this answers the ring:
        the answerer joins the call and the caller gets the answer. In a
        group call every callee answers the caller once — a second answer
        from the same callee (another of their devices) is refused with
        :class:`CallAlreadyAnsweredError`.

        With another participant as *to_user_id* it answers that
        participant's mesh-leg offer (``call.peer_join``, see
        :meth:`join_call`) and is relayed to them only.
        """
        record = self._calls.get(call_id)
        if record is None:
            raise CallNotFoundError(call_id)
        target = to_user_id or record.caller_user_id
        if (
            answerer_user_id not in record.participants
            or target not in record.participants
            or target == answerer_user_id
            or answerer_user_id in record.left
        ):
            raise PermissionError("Only a participant may answer this call")
        if await self._guardian_blocked(answerer_user_id, target):
            # §CP.F2 — no leg across a guardian block, answered either way.
            raise PermissionError(GUARDIAN_BLOCK_CALL_DETAIL)
        record.last_activity = time.time()
        signed = sign_rtc_offer(sdp_answer, "answer", identity_seed=self._own_seed)
        signed_dict = signed_sdp_to_dict(signed)

        if target != record.caller_user_id:
            await self._relay_mesh_answer(record, answerer_user_id, target, signed_dict)
            return {"call_id": call_id, "status": record.status}

        if answerer_user_id in record.answered:
            raise CallAlreadyAnsweredError(call_id)
        record.answered.add(answerer_user_id)
        if record.status == "ringing":
            record.status = "in_progress"
            await self._call_repo.transition(
                call_id,
                status="active",
                connected_at=_now_iso(),
            )

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
                    # Informational for older receivers (they route every
                    # answer to the caller anyway); lets a newer caller
                    # household tell which callee answered.
                    "from_user": answerer_user_id,
                    "signed_sdp": signed_dict,
                },
            )
        await self._fanout_to_user(
            record.caller_user_id,
            {
                "type": "call.answered",
                "call_id": call_id,
                "from_user": answerer_user_id,
                "signed_sdp": signed_dict,
            },
        )
        # Stop the ring on the answerer's other devices (the frame carries
        # no SDP — only the offering side applies an answer).
        await self._fanout_to_user(
            answerer_user_id,
            {"type": "call.answered", "call_id": call_id},
        )
        return {"call_id": call_id, "status": record.status}

    async def _relay_mesh_answer(
        self,
        record: CallRecord,
        answerer_user_id: str,
        target: str,
        signed_dict: dict,
    ) -> None:
        """Deliver a callee-to-callee mesh-leg answer to *target*.

        A target on another household gets ``CALL_ANSWER`` naming it in
        ``to_user`` (v_37). A household below v_37 would hand an answer
        addressed to another callee to the caller instead, so it gets
        none — that one leg stays unconnected.
        """
        target_instance = await self._instance_of(target)
        if (
            target_instance
            and self._federation is not None
            and target_instance != self._federation.own_instance_id
        ):
            if not await self._federation.peer_supports(
                target_instance,
                min_version=FederationCapability.MIN_FOR_CROSS_HOUSEHOLD_GROUP_DM,
            ):
                log.warning(
                    "call %s: mesh-leg answer %s -> %s not sent — household %s "
                    "is below v_%d and would route it to the caller",
                    record.call_id,
                    answerer_user_id,
                    target,
                    target_instance,
                    FederationCapability.MIN_FOR_CROSS_HOUSEHOLD_GROUP_DM,
                )
                return
            await self._federation.send_event(
                to_instance_id=target_instance,
                event_type=FederationEventType.CALL_ANSWER,
                payload={
                    "call_id": record.call_id,
                    "from_user": answerer_user_id,
                    "to_user": target,
                    "signed_sdp": signed_dict,
                },
            )
            return
        await self._fanout_to_user(
            target,
            {
                "type": "call.answered",
                "call_id": record.call_id,
                "from_user": answerer_user_id,
                "signed_sdp": signed_dict,
            },
        )

    async def add_ice_candidate(
        self,
        *,
        call_id: str,
        from_user_id: str,
        candidate: dict,
        to_user_id: str | None = None,
    ) -> None:
        """Trickle a single ICE candidate.

        With *to_user_id* the candidate goes to that participant only —
        one mesh leg. Without it (older clients) it fans out to every
        other participant.
        """
        record = self._calls.get(call_id)
        if record is None:
            raise CallNotFoundError(call_id)
        if from_user_id not in record.participants:
            raise PermissionError("Not a participant in this call")
        if to_user_id is not None:
            if to_user_id not in record.participants or to_user_id == from_user_id:
                raise PermissionError("ICE target is not another participant")
            others = [to_user_id]
        else:
            others = [u for u in record.participants if u != from_user_id]
        record.last_activity = time.time()

        for other in others:
            other_instance = await self._instance_of(other)
            if (
                other_instance
                and self._federation is not None
                and other_instance != self._federation.own_instance_id
            ):
                payload: dict[str, Any] = {
                    "call_id": call_id,
                    "from_user": from_user_id,
                    "candidate": candidate,
                }
                if to_user_id is not None:
                    # v_37: one mesh leg — name its end. An older household
                    # routes by caller/callee only, so a callee-to-callee
                    # candidate would reach the wrong person there.
                    if other != record.caller_user_id and not (
                        await self._federation.peer_supports(
                            other_instance,
                            min_version=(
                                FederationCapability.MIN_FOR_CROSS_HOUSEHOLD_GROUP_DM
                            ),
                        )
                    ):
                        continue
                    payload["to_user"] = other
                await self._federation.send_event(
                    to_instance_id=other_instance,
                    event_type=FederationEventType.CALL_ICE_CANDIDATE,
                    payload=payload,
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
        """A callee refuses a ringing call (§26.8). In a 1:1 call that
        ends it (``declined`` row + ``call_event``); in a group call only
        the decliner leaves. Emits ``CALL_DECLINE`` to a remote caller."""
        record = self._calls.get(call_id)
        if record is None:
            return
        if (
            decliner_user_id not in record.participants
            or decliner_user_id == record.caller_user_id
        ):
            raise PermissionError("Only a callee may decline")
        if decliner_user_id in record.answered or decliner_user_id in record.left:
            return  # already in the call (hang up instead) / already gone

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
        await self._leave(record, decliner_user_id, declined=True)

    async def hangup(self, *, call_id: str, hanger_user_id: str) -> None:
        """Leave a call. A 1:1 call ends (``ended`` with a duration); a
        group call ends once fewer than two people are left. Fires
        ``CALL_HANGUP`` once to each other participant's household."""
        record = self._calls.get(call_id)
        if record is None:
            return
        if hanger_user_id not in record.participants:
            raise PermissionError("Not a participant in this call")
        if hanger_user_id in record.left:
            return

        notified: set[str] = set()
        for other in sorted(record.present - {hanger_user_id}):
            other_instance = await self._instance_of(other)
            if (
                other_instance
                and self._federation is not None
                and other_instance != self._federation.own_instance_id
                and other_instance not in notified
            ):
                notified.add(other_instance)
                await self._federation.send_event(
                    to_instance_id=other_instance,
                    event_type=FederationEventType.CALL_HANGUP,
                    payload={"call_id": call_id, "hanger_user": hanger_user_id},
                )
        await self._leave(record, hanger_user_id, declined=False)

    async def _leave(self, record: CallRecord, user_id: str, *, declined: bool) -> None:
        """*user_id* hung up or declined: tell the others, and close the
        call once fewer than two participants are left.

        When the caller leaves, invites nobody has answered yet are
        withdrawn — a ringing callee stops ringing instead of joining an
        empty call. Each remaining local participant gets ``call.ended``
        / ``call.declined`` with ``by`` (so a mesh client can drop just
        that leg) and ``over`` (the call is finished for them).
        """
        before = record.present
        record.left.add(user_id)
        if user_id == record.caller_user_id:
            record.left |= record.participants - record.answered
        over = len(record.present) < 2
        frame_type = "call.declined" if declined else "call.ended"
        for other in sorted(before - {user_id}):
            await self._fanout_to_user(
                other,
                # ``over``: the whole call is finished for the receiver
                # (as opposed to one participant leaving it).
                {
                    "type": frame_type,
                    "call_id": record.call_id,
                    "by": user_id,
                    "over": over,
                },
            )
        if not over:
            return
        record.status = "ended"
        never_answered = record.answered <= {record.caller_user_id}
        persisted = await self._call_repo.get_call(record.call_id)
        if declined and never_answered:
            session = await self._call_repo.transition(
                record.call_id,
                status="declined",
                ended_at=_now_iso(),
            )
            event = "declined"
        else:
            session = await self._call_repo.transition(
                record.call_id,
                status="ended",
                ended_at=_now_iso(),
                duration_seconds=_duration_since(
                    persisted.connected_at if persisted else None
                ),
            )
            event = "ended"
        if session is not None:
            await self._emit_call_event_message(session, event=event)
        self._cleanup_call(record.call_id)

    async def join_call(
        self,
        *,
        call_id: str,
        joiner_user_id: str,
        sdp_offers: dict[str, str],
    ) -> dict:
        """Offer mesh legs into a group call (spec §26.8 lines 26532-26605).

        Used both by a late joiner and by a callee opening its legs to the
        other callees right after answering the caller (the lower
        ``user_id`` of each callee pair offers — see
        ``docs/protocol/calls.md``). Membership-verified: the joiner must
        be a member of the call's conversation. Fans one offer per named
        participant still in the call (``call.peer_join`` locally,
        ``CALL_OFFER`` with ``late_join`` to a remote one); the receiver
        answers it with ``POST /answer {to_user: joiner}``.
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

        already = joiner_user_id in session.participant_user_ids
        # §CP.F2: no leg ever opens across a guardian block. A protected
        # account can't late-join a call seating a blocked person; a callee
        # (rung by someone else) or anyone else just skips those legs.
        blocked = await self._guardian_block_counterparts(joiner_user_id)
        if blocked & set(session.participant_user_ids):
            if not already and await self._is_protected(joiner_user_id):
                raise PermissionError(GUARDIAN_BLOCK_CALL_DETAIL)
            sdp_offers = {u: o for u, o in sdp_offers.items() if u not in blocked}
        participants = set(session.participant_user_ids) | {joiner_user_id}
        if len(participants) > MAX_CALL_PARTICIPANTS:
            raise CallTooLargeError(
                f"Calls are limited to {MAX_CALL_PARTICIPANTS} people",
            )
        joined: list[str] = []
        record = self._calls.get(call_id)
        if record is not None:
            record.participants |= participants
            record.answered.add(joiner_user_id)
            record.left.discard(joiner_user_id)
            record.last_activity = time.time()
            self._per_user.setdefault(joiner_user_id, set()).add(call_id)
        if not already:
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
            if record is not None and participant_id in record.left:
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
                await self._on_federated_offer(event, call_id, payload, signed_dict)
            case FederationEventType.CALL_ANSWER:
                record = self._calls.get(call_id)
                if record is None or not await self._hosts_participant(
                    event.from_instance, record.participants
                ):
                    return
                to_user = payload.get("to_user")
                if to_user and to_user != record.caller_user_id:
                    await self._on_mesh_leg_answer(event, record, payload, signed_dict)
                    return
                if not await self._is_local_user(record.caller_user_id):
                    # An answer to the ring only means something on the
                    # caller's household.
                    return
                answerer = await self._inbound_answerer(event, record, payload)
                if answerer is None or answerer in record.answered:
                    return  # unattributable / a second answer (other device)
                record.answered.add(answerer)
                record.last_activity = time.time()
                if record.status == "ringing":
                    record.status = "in_progress"
                    # Caller-side row: without this it stays ``ringing`` and
                    # the stale-call sweep marks the live call missed at 90 s.
                    await self._call_repo.transition(
                        call_id,
                        status="active",
                        connected_at=_now_iso(),
                    )
                await self._fanout_to_user(
                    record.caller_user_id,
                    {
                        "type": "call.answered",
                        "call_id": call_id,
                        "from_user": answerer,
                        "signed_sdp": signed_dict,
                    },
                )
            case FederationEventType.CALL_ICE_CANDIDATE | FederationEventType.CALL_ICE:
                record = self._calls.get(call_id)
                if record is None:
                    return
                sender = payload.get("from_user")
                if not await self._is_sender_participant(
                    event, call_id, sender, record.participants, field="from_user"
                ):
                    return
                record.last_activity = time.time()
                named = payload.get("to_user")
                if named:
                    # v_37: one mesh leg — only to one of our participants.
                    if named not in record.participants or not (
                        await self._is_local_user(str(named))
                    ):
                        log.warning(
                            "CALL_ICE_CANDIDATE refused: from=%s call_id=%s — "
                            "to_user is not a participant here",
                            event.from_instance,
                            call_id,
                        )
                        return
                    target: str | None = str(named)
                else:
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
                        "from_user": sender,
                        "candidate": payload.get("candidate"),
                    },
                )
            case (
                FederationEventType.CALL_HANGUP
                | FederationEventType.CALL_END
                | FederationEventType.CALL_DECLINE
                | FederationEventType.CALL_BUSY
            ):
                await self._on_federated_end(event, call_id, payload)
            case FederationEventType.CALL_QUALITY:
                # Phase CF — persist remote-reported WebRTC quality sample.
                # Bound to the sender: the call must exist here and the
                # reporter must be one of its participants, homed on the
                # sending household.
                reporter = str(payload.get("reporter_user") or "")
                _record, _persisted, participants = await self._known_call(call_id)
                if not await self._is_sender_participant(
                    event, call_id, reporter, participants, field="reporter_user"
                ):
                    return
                try:
                    sample = CallQualitySample(
                        call_id=call_id,
                        reporter_user_id=reporter,
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

    async def _on_federated_offer(
        self,
        event,
        call_id: str,
        payload: dict,
        signed_dict: dict | None,
    ) -> None:
        """Ring a local callee for an inbound ``CALL_OFFER``.

        Persists the callee-side ``call_sessions`` row: every per-call
        route (answer / ice / decline / hangup) authorises against it and
        the stale-call sweep marks it missed. A repeat ``call_id`` (a
        second local callee of a group call, or a late joiner) is merged
        into the existing call — never allowed to reset it.
        """
        callee_user = payload.get("to_user") or ""
        caller = payload.get("from_user") or ""
        conversation_id = payload.get("conversation_id") or ""
        call_type = str(payload.get("call_type") or "audio")
        reject = await self._reject_inbound_offer(
            from_instance=event.from_instance,
            caller_user_id=caller,
            callee_user_id=callee_user,
            conversation_id=conversation_id,
            call_type=call_type,
        )
        if reject is None:
            existing = await self._call_repo.get_call(call_id)
            if existing is not None and existing.conversation_id != conversation_id:
                reject = "call_id already used by another conversation"
        if reject is None and existing is None:
            if len(self._per_user.get(callee_user, set())) >= MAX_CALLS_PER_USER:
                reject = "callee has too many concurrent calls"
        if reject is not None:
            log.warning(
                "CALL_OFFER %s from %s dropped: %s",
                call_id,
                event.from_instance,
                reject,
            )
            return

        if existing is not None:
            participants = set(existing.participant_user_ids) | {caller, callee_user}
            await self._call_repo.transition(
                call_id,
                status=existing.status,
                participant_user_ids=tuple(sorted(participants)),
            )
            record = self._calls.get(call_id)
            if record is not None:
                record.participants |= {caller, callee_user}
                if payload.get("late_join"):
                    record.answered.add(caller)
                record.last_activity = time.time()
        else:
            roster = {caller, callee_user} | await self._offered_roster(
                conversation_id, payload.get("participants")
            )
            await self._call_repo.save_call(
                CallSession(
                    id=call_id,
                    conversation_id=conversation_id,
                    initiator_user_id=caller,
                    callee_user_id=callee_user,
                    call_type=call_type,
                    status="ringing",
                    participant_user_ids=tuple(sorted(roster)),
                )
            )
            self._calls[call_id] = CallRecord(
                call_id=call_id,
                conversation_id=conversation_id,
                caller_user_id=caller,
                callee_user_id=callee_user,
                callee_instance_id=event.from_instance,
                call_type=call_type,
                participants=roster,
                answered={caller},
            )
        self._per_user.setdefault(callee_user, set()).add(call_id)

        if payload.get("late_join") and existing is not None:
            # Mirrors the local ``join_call`` fan-out.
            await self._fanout_to_user(
                callee_user,
                {
                    "type": "call.peer_join",
                    "call_id": call_id,
                    "joiner_user_id": caller,
                    "signed_sdp": signed_dict,
                },
            )
            return
        ring: dict[str, Any] = {
            "type": "call.ringing",
            "call_id": call_id,
            "conversation_id": conversation_id,
            "from_user": caller,
            "call_type": call_type,
            "signed_sdp": signed_dict,
        }
        record = self._calls.get(call_id)
        if record is not None and len(record.participants) > 2:
            # Group call: the callee's client opens its legs to the others.
            ring["participants"] = sorted(record.participants)
        await self._fanout_to_user(callee_user, ring)

    async def _offered_roster(
        self,
        conversation_id: str,
        offered: object,
    ) -> set[str]:
        """The ring's ``participants`` that are members of the conversation.

        A caller names every invitee so a callee here can open legs to the
        others; nobody outside the conversation's own seats is taken.
        """
        if not isinstance(offered, list) or len(offered) > MAX_CALL_PARTICIPANTS:
            return set()
        members = await self._conversation_user_ids(conversation_id)
        return {u for u in offered if isinstance(u, str) and u in members}

    async def _conversation_user_ids(self, conversation_id: str) -> set[str]:
        """``user_id`` of everyone seated in the conversation, here or remote."""
        ids: set[str] = set()
        for m in await self._conv_repo.list_members(conversation_id):
            if m.deleted_at is not None:
                continue
            u = await self._user_repo.get(m.username)
            if u is not None:
                ids.add(u.user_id)
        for r in await self._conv_repo.list_remote_members(conversation_id):
            seat_uid = r.user_id
            if seat_uid:
                ids.add(seat_uid)
                continue
            ru = await self._find_remote_user(r.instance_id, r.remote_username)
            if ru is not None:
                ids.add(ru.user_id)
        return ids

    async def _on_mesh_leg_answer(
        self,
        event,
        record: CallRecord,
        payload: dict,
        signed_dict: dict | None,
    ) -> None:
        """A callee on another household answers our callee's mesh-leg offer.

        The answerer must be a participant homed on the sending household,
        the target a participant homed here. It only connects that one leg
        — the call's answered state is the caller's business.
        """
        to_user = str(payload.get("to_user") or "")
        answerer = payload.get("from_user")
        if not await self._is_sender_participant(
            event, record.call_id, answerer, record.participants, field="from_user"
        ):
            return
        if to_user not in record.participants or not await self._is_local_user(to_user):
            log.warning(
                "CALL_ANSWER refused: from=%s call_id=%s — to_user is not a "
                "participant here",
                event.from_instance,
                record.call_id,
            )
            return
        record.last_activity = time.time()
        await self._fanout_to_user(
            to_user,
            {
                "type": "call.answered",
                "call_id": record.call_id,
                "from_user": answerer,
                "signed_sdp": signed_dict,
            },
        )

    async def _is_local_user(self, user_id: str) -> bool:
        return await self._user_repo.get_by_user_id(user_id) is not None

    async def _on_federated_end(self, event, call_id: str, payload: dict) -> None:
        """Inbound ``CALL_HANGUP`` / ``CALL_END`` / ``CALL_DECLINE`` /
        ``CALL_BUSY``: release the local user and close the persisted row
        (otherwise it stays ``active`` forever, or ``ringing`` until the
        sweep reports a declined call as missed)."""
        record, persisted, participants = await self._known_call(call_id)
        # The ender named in the payload is bound to the signing household:
        # a household can only end a call on behalf of its own participant
        # (the same "any participant hangs up → the call ends" rule as a
        # local hangup), never name somebody else's user.
        ender = payload.get("hanger_user") or payload.get("decliner_user")
        if not await self._is_sender_participant(
            event, call_id, ender, participants, field="hanger_user/decliner_user"
        ):
            return
        group = record is not None and len(record.participants) > 2
        if (
            not group
            and persisted is not None
            and persisted.status in ("ringing", "active")
        ):
            declined = event.event_type in (
                FederationEventType.CALL_DECLINE,
                FederationEventType.CALL_BUSY,
            )
            await self._call_repo.transition(
                call_id,
                status="declined" if declined else "ended",
                ended_at=_now_iso(),
                duration_seconds=_duration_since(persisted.connected_at),
            )
        if record is None:
            return
        if len(record.participants) > 2:
            # Group call: one participant left — the others stay in it
            # (the local ``hangup`` / ``decline`` rule).
            await self._leave(
                record,
                str(ender),
                declined=event.event_type
                in (FederationEventType.CALL_DECLINE, FederationEventType.CALL_BUSY),
            )
            return
        target = (
            record.callee_user_id
            if ender == record.caller_user_id
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

    async def _inbound_answerer(
        self,
        event,
        record: CallRecord,
        payload: dict,
    ) -> str | None:
        """Which callee a ``CALL_ANSWER`` is from.

        Newer households name it (``from_user``, bound to the sending
        household); an older one doesn't, and the answer is attributed to
        the only not-yet-answered callee that household hosts — ``None``
        (dropped) when that is ambiguous.
        """
        named = payload.get("from_user")
        if named:
            if not await self._is_sender_participant(
                event, record.call_id, named, record.participants, field="from_user"
            ):
                return None
            return str(named)
        candidates = [
            u
            for u in sorted(record.participants - record.answered)
            if await self._instance_of(u) == event.from_instance
        ]
        if len(candidates) != 1:
            log.warning(
                "CALL_ANSWER refused: from=%s call_id=%s — no from_user and %d "
                "unanswered callees on that household",
                event.from_instance,
                record.call_id,
                len(candidates),
            )
            return None
        return candidates[0]

    async def _known_call(
        self,
        call_id: str,
    ) -> tuple[CallRecord | None, CallSession | None, set[str]]:
        """The in-memory record, the persisted row and the participant set
        (from whichever of the two exists) for *call_id*."""
        record = self._calls.get(call_id)
        persisted = await self._call_repo.get_call(call_id)
        participants = (
            set(record.participants)
            if record is not None
            else set(persisted.participant_user_ids)
            if persisted is not None
            else set()
        )
        return record, persisted, participants

    async def _is_sender_participant(
        self,
        event,
        call_id: str,
        user_id: object,
        participants: set[str],
        *,
        field: str,
    ) -> bool:
        """``True`` when the payload-named *user_id* is a participant of the
        call AND homed on the signing household. Refusals log a WARNING."""
        if not isinstance(user_id, str) or not user_id:
            reason = f"missing {field}"
        elif user_id not in participants:
            reason = f"{field} is not a participant of the call"
        elif await self._instance_of(user_id) != event.from_instance:
            reason = f"{field} is not a user of the sending household"
        else:
            return True
        log.warning(
            "%s refused: from=%s call_id=%s user=%s — %s",
            event.event_type.value,
            event.from_instance,
            call_id,
            user_id if isinstance(user_id, str) else None,
            reason,
        )
        return False

    async def _hosts_participant(
        self,
        instance_id: str,
        participants: set[str],
    ) -> bool:
        """``True`` when *instance_id* hosts one of the call's participants
        — only those households may signal into the call."""
        for user_id in participants:
            if await self._instance_of(user_id) == instance_id:
                return True
        return False

    async def _reject_inbound_offer(
        self,
        *,
        from_instance: str,
        caller_user_id: str,
        callee_user_id: str,
        conversation_id: str,
        call_type: str,
    ) -> str | None:
        """Why an inbound ``CALL_OFFER`` must not ring, or ``None`` if it may.

        The offer is persisted as a ``call_sessions`` row, so it has to be
        a call this household would accept from its own members: a known
        call type, from a user the sending household hosts, for a user
        this household hosts, inside a conversation both of them are in.
        """
        if call_type not in ("audio", "video"):
            return f"invalid call_type {call_type!r}"
        callee = await self._user_repo.get_by_user_id(callee_user_id)
        if callee is None:
            return "callee is not a local user"
        caller = None
        for ru in await self._user_repo.list_remote_for_instance(from_instance):
            if ru.user_id == caller_user_id:
                caller = ru
                break
        if caller is None:
            return "caller is not a user of the sending household"
        if await self._guardian_blocked(callee_user_id, caller_user_id):
            return "guardian block"
        if not conversation_id:
            return "missing conversation_id"
        members = await self._conv_repo.list_members(conversation_id)
        if not any(
            m.username == callee.username and m.deleted_at is None for m in members
        ):
            return "callee is not in the conversation"
        remotes = await self._conv_repo.list_remote_members(conversation_id)
        if not any(
            r.instance_id == from_instance
            and r.remote_username == caller.remote_username
            and r.user_id in (None, caller_user_id)
            for r in remotes
        ):
            return "caller is not in the conversation"
        return None

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
        content = orjson.dumps(
            {
                "event": event,
                "call_id": session.id,
                "call_type": session.call_type,
                "caller_user_id": session.initiator_user_id,
                "callee_user_id": session.callee_user_id,
                "duration_seconds": session.duration_seconds,
            },
            option=orjson.OPT_SORT_KEYS,
        ).decode()
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


def _duration_since(connected_at: str | None) -> int | None:
    """Whole seconds since *connected_at* (ISO-8601), ``None`` if never
    connected."""
    if not connected_at:
        return None
    try:
        start = datetime.fromisoformat(connected_at)
    except ValueError:
        return 0
    return int((datetime.now(timezone.utc) - start).total_seconds())


# ─── Stale-call cleanup scheduler ────────────────────────────────────────


class StaleCallCleanupScheduler:
    """Background task that marks ringing calls past TTL as ``missed``
    (§26.8). Follows the ``_stop: asyncio.Event`` pattern (CLAUDE.md
    "Schedulers" invariant; reference template:
    :mod:`~socialhome.infrastructure.replay_cache_scheduler`).
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
