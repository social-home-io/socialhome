"""SyncSessionManager — owns active SyncRtcSessions + sync-protocol logic.

Implements the security audit fixes from §25.6.2:

* **S-6** — sync flood rate limit: 5 ``SPACE_SYNC_BEGIN`` per
  (instance, space) per hour and a 3 concurrent session cap per
  remote instance.
* **S-7** — :func:`validate_ice_candidate` (2 KB max, must start with
  ``"candidate:"``).
* **S-8** — :data:`MAX_SIGNALING_SESSIONS` cap.
* **S-12** — ``SPACE_SYNC_REQUEST_MORE`` clamps ``before_seq`` to the
  current ``space_max_seq`` and validates the resource against
  :data:`ALLOWED_RESOURCES`.
* **S-15** — :meth:`SyncSessionManager.trigger_relay_sync` provides the
  relay fallback that the dispatch handlers were calling but was never
  implemented.
* **S-17** — ``INSTANCE_SYNC_STATUS`` requires the sender to be a
  known active paired instance and caps the message to ≤ 100 spaces.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from ..domain.federation import FederationEventType, PairingStatus
from ..repositories.federation_repo import AbstractFederationRepo
from .sync_rtc import (
    MAX_SIGNALING_SESSIONS,
    SyncRtcSession,
    SyncSessionRecord,
)

log = logging.getLogger(__name__)


# ─── Constants from §25.6.2 audit ─────────────────────────────────────────

#: Maximum SPACE_SYNC_BEGIN events per (from_instance, space_id) per hour
#: (S-6 part 1).
SYNC_BEGIN_RATE_LIMIT_PER_HOUR: int = 5

#: Maximum concurrent active sync sessions per remote instance
#: (S-6 part 2).
MAX_ACTIVE_SESSIONS_PER_INSTANCE: int = 3

#: Maximum size of an ICE candidate string in bytes (S-7).
MAX_ICE_CANDIDATE_BYTES: int = 2048

#: How long a ``SPACE_SYNC_BEGIN`` we issued stays claimable by the
#: provider's ``SPACE_SYNC_OFFER``. Generous — a provider may be offline
#: when we ask and answer on its next boot — but finite, so a request the
#: provider never answers does not sit there for the process's lifetime.
PENDING_REQUEST_TTL_SECONDS: float = 3600.0

#: Cap on outstanding requests, so a retry storm cannot grow the table
#: without end. Oldest-first eviction.
MAX_PENDING_REQUESTS: int = 256

#: Allowed resource types for SPACE_SYNC_REQUEST_MORE (S-12).  Anything
#: else is silently dropped — never an unknown surface that an attacker
#: can probe.
ALLOWED_RESOURCES: frozenset[str] = frozenset(
    {
        "posts",
        "comments",
        "page_body",
        "calendar_events_past",
        "tasks_completed",
        "stickies_archived",
        "gallery_album",
        "gallery_item_full",
    }
)

#: Maximum number of space ids accepted in a single
#: ``INSTANCE_SYNC_STATUS`` payload (S-17).
MAX_INSTANCE_SYNC_STATUS_SPACES: int = 100


# ─── Dispatch outcomes ────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class PendingSyncRequest:
    """A ``SPACE_SYNC_BEGIN`` we sent and have not yet been answered.

    ``sync_id`` is the only thing addressing a sync session, and until a
    provider answers there is no session — :meth:`SyncSessionManager
    .apply_offer` MINTS one for whatever id the OFFER carries. Recording
    what we asked for, and of whom, is what makes an unsolicited offer
    recognisable: an offer is only ever an answer to our own request.
    """

    sync_id: str
    space_id: str
    provider_instance_id: str
    created_at: float


@dataclass(slots=True, frozen=True)
class SyncDecision:
    """Result of a SyncSessionManager dispatch call.

    Returned to the caller (typically ``FederationService._dispatch_event``)
    so it can fan out the appropriate response events.

    ``next_event`` and ``next_payload`` describe a single follow-up
    federation event the manager wants to send (e.g.
    ``SPACE_SYNC_DIRECT_FAILED`` after a rate-limit hit).  When both are
    ``None`` no further action is needed.
    """

    accepted: bool
    reason: str = ""
    next_event: FederationEventType | None = None
    next_payload: dict | None = None


# ─── Manager ──────────────────────────────────────────────────────────────


class SyncSessionManager:
    """In-memory registry + protocol logic for direct-sync sessions.

    Lifetime of a session:

    1. ``SPACE_SYNC_BEGIN`` arrives → :meth:`begin_session` is called.
       Manager runs S-6 rate checks, S-8 capacity check, member auth.
    2. ``SPACE_SYNC_OFFER`` ``ANSWER`` ``ICE`` events flow through
       :meth:`apply_offer` / :meth:`apply_answer` / :meth:`apply_ice`.
    3. ``SPACE_SYNC_DIRECT_READY`` → :meth:`mark_ready`.
    4. ``SPACE_SYNC_DIRECT_FAILED`` or 15 s ICE timeout →
       :meth:`trigger_relay_sync` (S-15) then :meth:`close_session`.
    """

    __slots__ = (
        "_federation_repo",
        "_sessions",
        "_rate_buckets",
        "_get_max_seq",
        "_check_member",
        "_reject_reason",
        "_on_local_ice",
        "_requests",
    )

    def __init__(
        self,
        federation_repo: AbstractFederationRepo,
        *,
        get_max_seq=None,
        check_member=None,
        reject_reason=None,
        on_local_ice=None,
    ) -> None:
        self._federation_repo = federation_repo
        self._sessions: dict[str, SyncSessionRecord] = {}

        # Sliding window of timestamps per (instance, space) for S-6.
        # ``deque`` is bounded by pruning entries older than 1 hour
        # before each lookup — no background cleaner needed.
        self._rate_buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)

        # Optional injected callbacks (kept generic so the manager has
        # no hard dependency on space_repo internals).  When ``None``
        # the corresponding check is skipped.
        self._get_max_seq = get_max_seq
        self._check_member = check_member
        # Classifies WHY a non-member's SPACE_SYNC_BEGIN is being rejected
        # so the host can reply with a signed SPACE_SYNC_REJECTED (S-1
        # backstop) instead of the silent drop. Signature:
        # ``async (space_id) -> "dissolved" | "removed"`` — "removed" if
        # the space row still exists (requester was dropped), "dissolved"
        # if it's gone. Kept generic so the manager has no hard
        # space_repo dependency; when ``None`` the silent drop is kept.
        self._reject_reason = reject_reason
        # Forwards each session's locally-gathered ICE candidate to the
        # opposite peer via ``SPACE_SYNC_ICE``. Signature:
        # ``async (sync_id, candidate, sdp_mid) -> None``. Injected by
        # ``FederationService`` so the manager stays free of routing
        # knowledge.
        self._on_local_ice = on_local_ice
        #: ``sync_id`` → the ``SPACE_SYNC_BEGIN`` we sent, until the
        #: provider answers it with an OFFER. See
        #: :class:`PendingSyncRequest`.
        self._requests: dict[str, PendingSyncRequest] = {}

    # ─── Requester-side: the syncs WE asked for ───────────────────────────

    def record_sync_request(
        self,
        *,
        sync_id: str,
        space_id: str,
        provider_instance_id: str,
    ) -> None:
        """Remember a ``SPACE_SYNC_BEGIN`` we are about to send.

        Every requester-side send site calls this, because
        :meth:`pending_sync_request` refusing is what stops an
        unsolicited ``SPACE_SYNC_OFFER`` from minting a session.
        """
        self._prune_requests()
        while len(self._requests) >= MAX_PENDING_REQUESTS:
            self._requests.pop(next(iter(self._requests)))
        self._requests[sync_id] = PendingSyncRequest(
            sync_id=sync_id,
            space_id=space_id,
            provider_instance_id=provider_instance_id,
            created_at=time.time(),
        )

    def pending_sync_request(self, sync_id: str) -> PendingSyncRequest | None:
        """The request we issued under ``sync_id``, or ``None``.

        ``None`` means we never asked (or asked too long ago), which is
        the whole point: an OFFER is an ANSWER, so there is no such thing
        as a legitimate one for an id we did not mint.
        """
        self._prune_requests()
        return self._requests.get(sync_id)

    def _prune_requests(self) -> None:
        cutoff = time.time() - PENDING_REQUEST_TTL_SECONDS
        stale = [k for k, v in self._requests.items() if v.created_at < cutoff]
        for key in stale:
            del self._requests[key]

    # ─── Public registry methods ──────────────────────────────────────────

    def has_session(self, sync_id: str) -> bool:
        return sync_id in self._sessions

    def get_session(self, sync_id: str) -> SyncSessionRecord | None:
        return self._sessions.get(sync_id)

    def active_session_count(self) -> int:
        return len(self._sessions)

    def active_sessions_for_instance(self, instance_id: str) -> int:
        return sum(
            1 for s in self._sessions.values() if s.requester_instance_id == instance_id
        )

    def close_session(self, sync_id: str) -> None:
        sess = self._sessions.pop(sync_id, None)
        if sess is None:
            return
        if sess.rtc_watcher is not None:
            # Stop the requester-side wait_ready / chunk-drain task
            # before tearing down the RTC handle so the loop doesn't
            # observe a torn-down PeerConnection mid-iteration.
            sess.rtc_watcher.cancel()
            sess.rtc_watcher = None
        if sess.rtc is not None:
            sess.rtc.close()

    def reap_stale(self, ttl_seconds: float, *, now: float | None = None) -> int:
        """Close sessions older than ``ttl_seconds`` (by ``created_at``).

        A safety backstop for sessions that never reach an explicit
        close — a sync stalls mid-stream, the peer vanishes, or a
        completion signal is missed. ``ttl_seconds`` is generous (well
        beyond any real sync duration), so this only reaps genuinely
        abandoned sessions. Returns the number reaped. Uses
        :meth:`close_session` for each so rtc handles / watcher tasks are
        torn down, not just dropped from the dict.
        """
        current = now if now is not None else time.time()
        # Snapshot before iterating — close_session mutates _sessions.
        reaped = 0
        for sync_id, record in list(self._sessions.items()):
            if current - record.created_at > ttl_seconds:
                self.close_session(sync_id)
                reaped += 1
        if reaped:
            log.info("sync-manager: reaped %d stale session(s)", reaped)
        return reaped

    # ─── S-6: rate limit ──────────────────────────────────────────────────

    def check_sync_begin_rate(
        self,
        from_instance: str,
        space_id: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Return True if this peer may start another sync for *space_id*.

        Implements the 5-per-hour bucket from S-6 part 1.  Increments
        the bucket on ``True``; ``False`` means the caller must reply
        with ``SPACE_SYNC_DIRECT_FAILED {reason: "rate_limited"}``.
        """
        now = now if now is not None else time.monotonic()
        key = (from_instance, space_id)
        bucket = self._rate_buckets[key]
        cutoff = now - 3600
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= SYNC_BEGIN_RATE_LIMIT_PER_HOUR:
            return False
        bucket.append(now)
        return True

    # ─── S-7: ICE candidate validation ────────────────────────────────────

    @staticmethod
    def validate_ice_candidate(candidate: str) -> bool:
        """RFC-8839 sanity check + 2 KB cap (S-7)."""
        if not isinstance(candidate, str):
            return False
        if len(candidate.encode("utf-8")) > MAX_ICE_CANDIDATE_BYTES:
            return False
        return candidate.strip().startswith("candidate:")

    # ─── S-12: request_more bounds ────────────────────────────────────────

    async def clamp_request_more(self, payload: dict) -> dict | None:
        """Clamp + validate a ``SPACE_SYNC_REQUEST_MORE`` payload.

        Returns the cleaned payload, or ``None`` if the request must be
        silently dropped (unknown resource or malformed).
        """
        space_id = payload.get("space_id")
        resource = payload.get("resource")
        if not isinstance(space_id, str) or not space_id:
            return None
        if resource not in ALLOWED_RESOURCES:
            return None

        try:
            before_seq = int(payload.get("before_seq", 0) or 0)
            limit = int(payload.get("limit", 50) or 50)
        except TypeError, ValueError:
            return None
        limit = max(1, min(limit, 200))

        if self._get_max_seq is not None:
            try:
                space_max = int(await self._get_max_seq(space_id))
            except Exception:
                space_max = before_seq
            before_seq = min(before_seq, space_max) if before_seq > 0 else space_max

        return {
            "space_id": space_id,
            "resource": resource,
            "before_seq": before_seq,
            "limit": limit,
            "page_id": payload.get("page_id"),
        }

    # ─── S-8 / S-6: session admission ─────────────────────────────────────

    async def begin_session(
        self,
        *,
        sync_id: str,
        space_id: str,
        requester_instance_id: str,
        provider_instance_id: str,
        sync_mode: str = "initial",
        ice_servers: list[dict] | None = None,
    ) -> SyncDecision:
        """Admit a new sync session.

        Runs the S-6 rate limit (5/h + 3 concurrent) and the S-8 cap.
        On rejection returns a :class:`SyncDecision` with
        ``accepted=False`` and a ``next_event`` describing the
        ``SPACE_SYNC_DIRECT_FAILED`` event the caller should send back
        to the requester.
        """
        if sync_id in self._sessions:
            return SyncDecision(accepted=False, reason="duplicate_sync_id")

        if not self.check_sync_begin_rate(requester_instance_id, space_id):
            return SyncDecision(
                accepted=False,
                reason="rate_limited",
                next_event=FederationEventType.SPACE_SYNC_DIRECT_FAILED,
                next_payload={"sync_id": sync_id, "reason": "rate_limited"},
            )

        if (
            self.active_sessions_for_instance(requester_instance_id)
            >= MAX_ACTIVE_SESSIONS_PER_INSTANCE
        ):
            return SyncDecision(
                accepted=False,
                reason="too_many_sessions",
                next_event=FederationEventType.SPACE_SYNC_DIRECT_FAILED,
                next_payload={"sync_id": sync_id, "reason": "too_many_sessions"},
            )

        if len(self._sessions) >= MAX_SIGNALING_SESSIONS:
            return SyncDecision(
                accepted=False,
                reason="node_capacity",
                next_event=FederationEventType.SPACE_SYNC_DIRECT_FAILED,
                next_payload={"sync_id": sync_id, "reason": "node_capacity"},
            )

        # Optional space-membership check (S-1).
        if self._check_member is not None:
            try:
                ok = await self._check_member(space_id, requester_instance_id)
            except Exception:
                ok = False
            if not ok:
                if self._reject_reason is None:
                    # No classifier wired — preserve the S-1 silent drop.
                    return SyncDecision(accepted=False, reason="not_a_member")
                try:
                    reason = await self._reject_reason(space_id)
                except Exception:
                    reason = "dissolved"
                return SyncDecision(
                    accepted=False,
                    reason="not_a_member",
                    next_event=FederationEventType.SPACE_SYNC_REJECTED,
                    next_payload={
                        "sync_id": sync_id,
                        "space_id": space_id,
                        "reason": reason,
                    },
                )

        rtc = SyncRtcSession(
            sync_id=sync_id,
            space_id=space_id,
            requester_instance_id=requester_instance_id,
            provider_instance_id=provider_instance_id,
            sync_mode=sync_mode,
            role="provider",
            ice_servers=ice_servers,
            on_local_ice=self._on_local_ice,
        )
        record = SyncSessionRecord(
            sync_id=sync_id,
            space_id=space_id,
            requester_instance_id=requester_instance_id,
            provider_instance_id=provider_instance_id,
            sync_mode=sync_mode,
            rtc=rtc,
            created_at=time.time(),
        )
        self._sessions[sync_id] = record
        return SyncDecision(accepted=True)

    def register_requester_https_session(
        self,
        *,
        sync_id: str,
        space_id: str,
        requester_instance_id: str,
        provider_instance_id: str,
        sync_mode: str = "initial",
    ) -> bool:
        """Register a requester-role receive session for an HTTPS/mesh sync.

        A mesh member (not directly paired with the host) cannot do the RTC
        handshake — ICE can't traverse a relay — so it never gets a session
        via :meth:`apply_offer`. It must register one up front before sending
        SPACE_SYNC_BEGIN(prefer_direct=False), so the inbound (routed)
        SPACE_SYNC_CHUNK handler finds it instead of dropping the chunks.

        Sets ``transport_mode="https"`` and ``provider_instance_id`` = the host
        so the chunk handler's provider-origin guard matches. No rtc handle, no
        admission checks (those are the provider's job in ``begin_session``).
        Returns False (no-op) if a session for ``sync_id`` already exists.
        """
        if sync_id in self._sessions:
            return False
        self._sessions[sync_id] = SyncSessionRecord(
            sync_id=sync_id,
            space_id=space_id,
            requester_instance_id=requester_instance_id,
            provider_instance_id=provider_instance_id,
            sync_mode=sync_mode,
            rtc=None,
            created_at=time.time(),
            transport_mode="https",
        )
        return True

    # ─── S-13/S-14: offer / answer / ice apply ────────────────────────────

    async def apply_offer(
        self,
        *,
        sync_id: str,
        sdp_offer: str,
        requester_instance_id: str,
        space_id: str,
        provider_instance_id: str = "",
        sync_mode: str = "initial",
        ice_servers: list[dict] | None = None,
    ) -> str:
        """Requester-side handling of ``SPACE_SYNC_OFFER``.

        Creates a *requester*-role session if one does not yet exist
        (the requester didn't allocate one in begin_session), generates
        an SDP answer via :meth:`SyncRtcSession.create_answer`, and
        returns the answer string for the caller to embed in
        ``SPACE_SYNC_ANSWER``.

        ``provider_instance_id`` PINS the session to the household that
        may stream into it. It used to be left empty here ("unknown at
        requester side"), and the chunk handler's origin check skipped on
        that empty string — so the session accepted chunks from anyone.
        The caller knows the answer: it is the authenticated sender of
        the OFFER, matched against the request we recorded.
        """
        record = self._sessions.get(sync_id)
        if record is None:
            rtc = SyncRtcSession(
                sync_id=sync_id,
                space_id=space_id,
                requester_instance_id=requester_instance_id,
                provider_instance_id=provider_instance_id,
                sync_mode=sync_mode,
                role="requester",
                ice_servers=ice_servers,
                on_local_ice=self._on_local_ice,
            )
            record = SyncSessionRecord(
                sync_id=sync_id,
                space_id=space_id,
                requester_instance_id=requester_instance_id,
                provider_instance_id=provider_instance_id,
                sync_mode=sync_mode,
                rtc=rtc,
                created_at=time.time(),
            )
            self._sessions[sync_id] = record

        if record.rtc is None:  # defensive
            raise RuntimeError("sync record missing rtc handle")

        return await record.rtc.create_answer(sdp_offer)  # S-13

    async def apply_answer(
        self,
        *,
        sync_id: str,
        sdp_answer: str,
        from_instance: str,
    ) -> bool:
        """Provider-side handling of ``SPACE_SYNC_ANSWER`` (S-14 origin guard)."""
        record = self._sessions.get(sync_id)
        if record is None:
            log.debug("apply_answer: unknown sync_id=%s", sync_id)
            return False
        # S-14: answer must come from the original requester.
        if record.requester_instance_id != from_instance:
            log.warning(
                "apply_answer: rejected — sync_id=%s from=%s expected=%s",
                sync_id,
                from_instance,
                record.requester_instance_id,
            )
            return False
        if record.rtc is None:
            return False
        await record.rtc.set_answer(sdp_answer)
        return True

    async def apply_ice(
        self,
        *,
        sync_id: str,
        candidate: str,
        sdp_mid: str = "0",
    ) -> bool:
        """Apply a single ICE candidate after S-7 validation."""
        if not self.validate_ice_candidate(candidate):
            log.debug("apply_ice: dropped invalid candidate (sync=%s)", sync_id)
            return False
        record = self._sessions.get(sync_id)
        if record is None or record.rtc is None:
            return False
        await record.rtc.add_ice_candidate(candidate, sdp_mid)
        return True

    # ─── S-15: relay fallback ─────────────────────────────────────────────

    async def trigger_relay_sync(self, sync_id: str) -> SyncDecision:
        """Build a relay-mode ``SPACE_SYNC_BEGIN`` to retry without WebRTC.

        Per **§25.8.18**: Tier 3 (``sync_mode="full"``) MUST NOT fall
        back to relay — for those sessions the manager closes and
        returns ``accepted=False`` so the caller can surface the
        failure to the user.
        """
        record = self._sessions.get(sync_id)
        if record is None:
            return SyncDecision(accepted=False, reason="unknown_sync_id")

        # Tier 3 abort per §25.8.18.
        if record.sync_mode == "full":
            self.close_session(sync_id)
            return SyncDecision(
                accepted=False,
                reason="tier3_abort",
                next_event=FederationEventType.SPACE_SYNC_DIRECT_FAILED,
                next_payload={"sync_id": sync_id, "reason": "tier3_abort"},
            )

        new_sync_id = secrets.token_urlsafe(16)
        self.close_session(sync_id)
        return SyncDecision(
            accepted=True,
            reason="relay_fallback",
            next_event=FederationEventType.SPACE_SYNC_BEGIN,
            next_payload={
                "space_id": record.space_id,
                "sync_id": new_sync_id,
                "sync_mode": record.sync_mode,
                "prefer_direct": False,
                "tier1_only": record.sync_mode == "initial",
            },
        )

    # ─── S-17: instance_sync_status guard ─────────────────────────────────

    async def validate_instance_sync_status(
        self,
        *,
        from_instance: str,
        payload: dict,
    ) -> list[str]:
        """Return the list of space ids the receiver should resume.

        Returns an empty list when:

        * the sender is not a known active paired instance (S-17);
        * the payload carries more than 100 spaces (S-17 cap).
        """
        instance = await self._federation_repo.get_instance(from_instance)
        if instance is None or instance.status != PairingStatus.CONFIRMED:
            log.debug(
                "INSTANCE_SYNC_STATUS rejected: unknown instance=%s",
                from_instance,
            )
            return []

        spaces = payload.get("spaces") or []
        if not isinstance(spaces, list):
            return []
        if len(spaces) > MAX_INSTANCE_SYNC_STATUS_SPACES:
            log.warning(
                "INSTANCE_SYNC_STATUS rejected: %d spaces from %s exceeds cap=%d",
                len(spaces),
                from_instance,
                MAX_INSTANCE_SYNC_STATUS_SPACES,
            )
            return []

        # Per-space membership filter is left to the caller (it has the
        # space repo handy); we just validate shape here.
        out: list[str] = []
        for entry in spaces:
            sid = (
                entry
                if isinstance(entry, str)
                else entry.get("space_id")
                if isinstance(entry, dict)
                else None
            )
            if isinstance(sid, str) and sid:
                out.append(sid)
        return out


# ─── Helpers exposed for tests ────────────────────────────────────────────


def new_sync_id() -> str:
    """Return a fresh 128-bit URL-safe sync id (S-2)."""
    return secrets.token_urlsafe(16)
