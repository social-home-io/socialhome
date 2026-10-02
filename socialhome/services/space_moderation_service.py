"""The space moderation queue for every feature (§4.3 ``MODERATED``).

Under a ``MODERATED`` access level a plain member's NEW item, and their
edit / delete of SOMEBODY ELSE's item, waits here until content authority
(owner / admin / moderator) approves it. Own edits / deletes, layout moves,
RSVPs, comments, reactions and content authority's own writes never queue
(:meth:`SpaceFeatures.access_decision`).

The queue is the HOST household's (``space_moderation_queue``). Until
federated moderation lands it holds a feature other than ``posts`` only
for a space with no remote member households: the config API refuses
``MODERATED`` for those features elsewhere
(:class:`ModerationNotFederatedError`), and a QUEUE answer on a household
that cannot hold the queue is a refusal, never a silent write. Posts keep
their older behaviour — they queue on the host, and a member household
sends its member's post straight on for the host to judge.

Each content service owns the shape of its own items through a
:class:`ModerationHandler` registered per ``(feature, action)`` in
``app._build_services`` (registry pattern). The content service submits
through the narrow :class:`ModerationSubmitter` protocol — so neither side
imports the other — and approval replays the write through that service's
normal persist path, gated as the APPROVER (``approved_by``), so the result
federates and notifies exactly like a direct write while staying
attributed to the submitter.

Pending content lives only in this table: nothing reaches a feed, list,
search index, notification body or realtime frame of anybody but the
submitter and content authority before approval.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol

from ..domain.events import (
    SpaceModerationApproved,
    SpaceModerationExpired,
    SpaceModerationQueued,
    SpaceModerationRejected,
)
from ..domain.space import (
    CONTENT_AUTHORITY_ROLES,
    AccessAdminOnlyError,
    AccessDecision,
    ContentAction,
    ModerationAlreadyDecidedError,
    ModerationExpiredError,
    ModerationInProgressError,
    ModerationNotFederatedError,
    ModerationNotHostError,
    ModerationPayloadTooLargeError,
    ModerationQueueFullError,
    ModerationStatus,
    ModerationTargetGoneError,
    ModerationUnavailableError,
    Space,
    SpaceFeatureAccess,
    SpaceModerationItem,
    SpacePermissionError,
    WRITER_ROLES,
)
from ..repositories.base import dump_json
from .bus_publisher import BusPublisherMixin

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

#: How long an item waits for a decision before it expires.
MODERATION_TTL = timedelta(days=7)
#: Decided / expired items keep their content this long, then the scheduler
#: NULLs it (the row stays for audit).
MODERATION_PAYLOAD_RETENTION = timedelta(days=7)
#: DoS caps: pending items per (space, submitter) and per space.
MAX_PENDING_PER_SUBMITTER = 20
MAX_PENDING_PER_SPACE = 500
#: Serialised payload + snapshot cap.
MAX_PAYLOAD_BYTES = 256 * 1024
#: Rejection reason cap (characters).
MAX_REJECT_REASON = 500
#: Decided / expired items ``…/moderation/mine`` returns besides every
#: pending one.
MINE_RECENT_LIMIT = 100

#: The per-space on/off toggle behind each access-gated feature (``tasks``
#: rides the ``todo`` tab). Posts have no toggle of their own.
_FEATURE_TOGGLE: dict[str, str] = {
    "pages": "pages",
    "stickies": "stickies",
    "calendar": "calendar",
    "tasks": "todo",
}


@dataclass(slots=True, frozen=True)
class ApplyResult:
    """What an approval produced: the persisted item's id (``post_id`` too
    for a post, the legacy approve-response alias)."""

    target_id: str | None
    post_id: str | None = None
    #: False when the content landed but a later side effect (a poll, a
    #: listing, a publish) failed — a repeat approve resumes it.
    complete: bool = True


class ModerationHandler(Protocol):
    """One feature's queue items (registered per ``(feature, action)``)."""

    def validate(self, space: Space, payload: dict) -> dict:
        """The payload normalised with the live REST path's codecs/caps."""
        ...

    async def snapshot(self, space_id: str, target_id: str) -> dict | None:
        """The live row as a plain dict, ``None`` when it no longer exists."""
        ...

    async def apply(
        self, item: SpaceModerationItem, *, approved_by: str, force: bool
    ) -> ApplyResult:
        """Replay the write through the feature's normal persist path,
        gated as ``approved_by``. Raises :class:`ModerationTargetGoneError`
        for an edit whose target is gone, ``ModerationStaleError`` when it
        changed underneath (unless ``force``)."""
        ...

    def preview(self, item: SpaceModerationItem) -> dict:
        """The proposed state, shaped for the SPA's preview card."""
        ...


class ModerationSubmitter(Protocol):
    """The narrow face content services submit through (and the space
    config gate asks before it allows ``MODERATED``)."""

    async def require_moderation_supported(
        self, space: Space, features: Any
    ) -> None: ...

    async def submit(
        self,
        space: Space,
        *,
        feature: str,
        action: ContentAction,
        submitted_by: str,
        payload: dict,
        snapshot: dict | None = None,
    ) -> SpaceModerationItem: ...


def item_payload_snapshot(item: SpaceModerationItem) -> dict | None:
    """``current_snapshot`` decoded (``None`` once purged / for a create)."""
    if not item.current_snapshot:
        return None
    try:
        value = json.loads(item.current_snapshot)
    except TypeError, ValueError:
        return None
    return value if isinstance(value, dict) else None


class SpaceModerationService(BusPublisherMixin):
    """Submit, list, approve, reject and expire queued space content."""

    __slots__ = (
        "_spaces",
        "_users",
        "_federation_repo",
        "_bus",
        "_own_instance_id",
        "_handlers",
        "_inflight",
    )

    def __init__(
        self,
        space_repo: "AbstractSpaceRepo",
        *,
        user_repo: "AbstractUserRepo",
        bus: "EventBus | None" = None,
        federation_repo: "AbstractFederationRepo | None" = None,
        own_instance_id: str | None = None,
    ) -> None:
        self._spaces = space_repo
        self._users = user_repo
        self._bus = bus
        self._federation_repo = federation_repo
        self._own_instance_id = own_instance_id
        self._handlers: dict[tuple[str, str], ModerationHandler] = {}
        #: Items an approve / resume is applying right now. A household is
        #: one process with one database writer, so this check-and-add (no
        #: ``await`` between them) serialises every apply of an item without
        #: a schema change; the create-once repo writes are the backstop.
        self._inflight: set[str] = set()

    # ── Registry ─────────────────────────────────────────────────────────

    def register(
        self, feature: str, action: ContentAction, handler: ModerationHandler
    ) -> None:
        """Register the handler for ``feature``'s ``action`` items."""
        key = (feature, action.value)
        if key in self._handlers:
            raise ValueError(f"moderation handler for {key!r} already registered")
        self._handlers[key] = handler

    def _handler(self, feature: str, action: str) -> ModerationHandler:
        handler = self._handlers.get((feature, action))
        if handler is None:
            # Fail closed: an item nothing knows how to apply can't queue.
            raise SpacePermissionError(f"{feature} {action} cannot be reviewed here")
        return handler

    # ── Where the queue may live ─────────────────────────────────────────

    def is_host(self, space: Space) -> bool:
        return (
            self._own_instance_id is not None
            and space.owner_instance_id == self._own_instance_id
        )

    async def has_remote_households(self, space_id: str) -> bool:
        """Does any household other than this one hold a seat in the space?"""
        if self._federation_repo is None:
            return False
        ids = await self._federation_repo.list_member_instance_ids(space_id)
        return any(i != self._own_instance_id for i in ids)

    async def queue_holds(self, space: Space, feature: str) -> bool:
        """May ``feature``'s items queue here? Posts: on the host. Every
        other feature: on the host of a space without remote households."""
        if not self.is_host(space):
            return False
        if feature == "posts":
            return True
        return not await self.has_remote_households(space.id)

    async def require_moderation_supported(self, space: Space, features: Any) -> None:
        """Config gate: refuse a non-post feature newly set to MODERATED on
        a space whose queue can't hold it (422 ``MODERATION_NOT_FEDERATED``).
        ``features`` is the proposed :class:`SpaceFeatures`."""
        for feature in ("pages", "stickies", "calendar", "tasks"):
            if features.access_level(feature) is not SpaceFeatureAccess.MODERATED:
                continue
            if space.features.access_level(feature) is SpaceFeatureAccess.MODERATED:
                continue  # unchanged — never strand an existing setting
            if not await self.queue_holds(space, feature):
                raise ModerationNotFederatedError(feature)

    # ── Submit ───────────────────────────────────────────────────────────

    async def submit(
        self,
        space: Space,
        *,
        feature: str,
        action: ContentAction,
        submitted_by: str,
        payload: dict,
        snapshot: dict | None = None,
    ) -> SpaceModerationItem:
        """Queue one submission and announce it to content authority."""
        if not await self.queue_holds(space, feature):
            raise ModerationNotFederatedError(feature, http_status=403)
        handler = self._handler(feature, action.value)
        clean = handler.validate(space, dict(payload))
        snapshot_json = dump_json(snapshot) if snapshot is not None else None
        # The cap is on what is STORED: the exact text the repo writes,
        # in UTF-8 bytes.
        size = len(dump_json(clean).encode()) + len((snapshot_json or "").encode())
        if size > MAX_PAYLOAD_BYTES:
            raise ModerationPayloadTooLargeError(
                f"submission is too large to review ({size} bytes)"
            )
        if (
            await self._spaces.count_pending(space.id, submitted_by=submitted_by)
            >= MAX_PENDING_PER_SUBMITTER
        ):
            raise ModerationQueueFullError(
                "you already have the maximum number of items waiting for review"
            )
        if await self._spaces.count_pending(space.id) >= MAX_PENDING_PER_SPACE:
            raise ModerationQueueFullError("this space's review queue is full")
        now = datetime.now(timezone.utc)
        item = SpaceModerationItem(
            id=uuid.uuid4().hex,
            space_id=space.id,
            feature=feature,
            action=action.value,
            submitted_by=submitted_by,
            payload=clean,
            current_snapshot=snapshot_json,
            submitted_at=now,
            expires_at=now + MODERATION_TTL,
            status=ModerationStatus.PENDING,
        )
        await self._spaces.insert_moderation_item(item)
        await self._emit(SpaceModerationQueued(item=item))
        return item

    # ── Reads ────────────────────────────────────────────────────────────

    async def _space(self, space_id: str) -> Space:
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        return space

    async def _require_content_authority(self, space_id: str, user_id: str) -> str:
        member = await self._spaces.get_member(space_id, user_id)
        if member is None or member.role not in CONTENT_AUTHORITY_ROLES:
            raise SpacePermissionError(
                "only the space owner, admins and moderators can review content"
            )
        return str(member.role)

    async def list_items(
        self, space_id: str, *, actor_user_id: str, include_decided: bool = False
    ) -> list[SpaceModerationItem]:
        """The queue (content authority): pending, or every status."""
        await self._space(space_id)
        await self._require_content_authority(space_id, actor_user_id)
        # Every pending item (a space holds at most MAX_PENDING_PER_SPACE);
        # "all" adds the most recent decided ones up to the same bound.
        return await self._spaces.list_moderation_queue(
            space_id,
            status=None if include_decided else ModerationStatus.PENDING,
            limit=MAX_PENDING_PER_SPACE,
        )

    async def list_mine(
        self, space_id: str, *, user_id: str
    ) -> list[SpaceModerationItem]:
        """The caller's own submissions, any status (any member)."""
        await self._space(space_id)
        if await self._spaces.get_member(space_id, user_id) is None:
            raise SpacePermissionError("not a member of this space")
        # All of the caller's pending items (at most MAX_PENDING_PER_SUBMITTER)
        # — never crowded out by newer decided ones — then recent outcomes.
        pending = await self._spaces.list_moderation_for_submitter(
            space_id,
            user_id,
            status=ModerationStatus.PENDING,
            limit=MAX_PENDING_PER_SUBMITTER,
        )
        recent = await self._spaces.list_moderation_for_submitter(
            space_id, user_id, limit=MINE_RECENT_LIMIT
        )
        seen = {i.id for i in pending}
        merged = pending + [i for i in recent if i.id not in seen]
        return sorted(merged, key=lambda i: _aware(i.submitted_at), reverse=True)

    async def get_item(self, space_id: str, item_id: str) -> SpaceModerationItem:
        item = await self._spaces.get_moderation_item(item_id)
        if item is None or item.space_id != space_id:
            raise KeyError(f"moderation item {item_id!r} not found")
        return item

    async def describe(
        self, item: SpaceModerationItem, *, with_current: bool = False
    ) -> dict:
        """The API shape of an item (``GET …/moderation`` and ``…/mine``)."""
        payload = item.payload or {}
        handler = self._handlers.get((item.feature, item.action))
        preview: dict = {}
        if handler is not None and payload:
            try:
                preview = handler.preview(item)
            except Exception:  # a corrupt row must not break the whole list
                log.warning("moderation: preview failed for item %s", item.id)
        snapshot = item_payload_snapshot(item)
        current: dict | None = None
        target_id = payload.get("target_id")
        if (
            with_current
            and handler is not None
            and item.status is ModerationStatus.PENDING
            and item.action == ContentAction.EDIT.value
            and isinstance(target_id, str)
        ):
            live = await handler.snapshot(item.space_id, target_id)
            if live is not None:
                keys = snapshot.keys() if snapshot else live.keys()
                current = {k: live.get(k) for k in keys}
        user = await self._users.get_by_user_id(item.submitted_by)
        return {
            "id": item.id,
            "space_id": item.space_id,
            "feature": item.feature,
            "action": item.action,
            "entity": payload.get("entity"),
            "op": payload.get("op"),
            "target_id": target_id,
            "submitted_by": item.submitted_by,
            "submitted_by_display": (
                user.display_name or user.username if user else item.submitted_by
            ),
            "submitted_at": _iso(item.submitted_at),
            "expires_at": _iso(item.expires_at),
            "status": item.status.value,
            "reviewed_by": item.reviewed_by,
            "reviewed_at": _iso(item.reviewed_at),
            "rejection_reason": item.rejection_reason,
            "preview": preview,
            "snapshot": snapshot,
            "current": current,
            "payload": payload,
        }

    async def find_pending(
        self, space_id: str, submitted_by: str, **match: object
    ) -> SpaceModerationItem | None:
        """The submitter's pending item whose payload carries ``match``."""
        for item in await self._spaces.list_moderation_for_submitter(
            space_id,
            submitted_by,
            status=ModerationStatus.PENDING,
            limit=MAX_PENDING_PER_SUBMITTER,
        ):
            if all(item.payload.get(k) == v for k, v in match.items()):
                return item
        return None

    # ── Decisions ────────────────────────────────────────────────────────

    async def approve(
        self,
        space_id: str,
        item_id: str,
        *,
        actor_user_id: str,
        force: bool = False,
    ) -> ApplyResult:
        """Approve one pending item: persist it through the feature's
        normal path as ``actor_user_id``'s release, exactly once.

        Approving an item that is already APPROVED resumes it: a handler
        that can (a post's poll / schedule / listing) creates whatever part
        of it is still missing; otherwise :class:`ModerationAlreadyDecidedError`.
        """
        space = await self._space(space_id)
        if not self.is_host(space):
            # The queue lives on the host; nothing here may release it.
            raise ModerationNotHostError("only the space's host reviews its queue")
        role = await self._require_content_authority(space_id, actor_user_id)
        if item_id in self._inflight:
            raise ModerationInProgressError(f"item {item_id!r} is being approved")
        self._inflight.add(item_id)
        try:
            return await self._approve(space, role, item_id, actor_user_id, force)
        finally:
            self._inflight.discard(item_id)

    async def _approve(
        self,
        space: Space,
        role: str,
        item_id: str,
        actor_user_id: str,
        force: bool,
    ) -> ApplyResult:
        space_id = space.id
        item = await self.get_item(space_id, item_id)
        handler = self._handler(item.feature, item.action)
        if item.status not in (ModerationStatus.PENDING, ModerationStatus.APPROVED):
            raise ModerationAlreadyDecidedError(
                f"item {item_id!r} is already {item.status.value}"
            )
        resuming = item.status is ModerationStatus.APPROVED
        # The same checks guard a resume: it releases more of the item.
        if _aware(item.expires_at) <= datetime.now(timezone.utc):
            if not resuming:
                await self._expire(item, actor_user_id)
            raise ModerationExpiredError("this submission's review window has passed")
        if space.archived or not self._feature_on(space, item):
            raise ModerationUnavailableError(
                f"{item.feature} is not available in this space right now"
            )
        # Releasing an item IS making the write: the feature's level applies
        # to the approver (ADMIN_ONLY takes it from an admin only).
        decision = space.features.access_decision(
            item.feature,
            role=role,
            action=ContentAction(item.action),
            owns_target=False,
        )
        if decision is AccessDecision.DENY:
            raise AccessAdminOnlyError(item.feature)
        submitter = await self._spaces.get_member(space_id, item.submitted_by)
        if submitter is None or submitter.role not in WRITER_ROLES:
            # The author left, was removed or demoted: their words are not
            # published on their behalf any more (a published item keeps
            # its status — only the rest of it is withheld).
            if not resuming:
                await self._expire(item, actor_user_id)
            raise ModerationTargetGoneError("the author is no longer a member")
        if resuming:
            return await self._resume(item, handler, approved_by=actor_user_id)
        if not await self._spaces.claim_moderation_item(
            item_id, status=ModerationStatus.APPROVED, reviewed_by=actor_user_id
        ):
            raise ModerationAlreadyDecidedError(f"item {item_id!r} was just decided")
        try:
            result = await handler.apply(item, approved_by=actor_user_id, force=force)
        except ModerationTargetGoneError:
            # The edit has nothing left to change: the item expires.
            if await self._spaces.release_moderation_item(
                item_id, claimed_status=ModerationStatus.APPROVED
            ):
                await self._expire(item, actor_user_id)
            raise
        except Exception as exc:
            # (A cancellation is not caught: the item stays APPROVED — the
            # side that can never leave published content reopenable.)
            if await self._landed(item, handler):
                # The content is live; only a later side effect failed (a
                # bus publish, a poll / listing, a federation enqueue). The
                # item stays APPROVED — it must never read as pending or be
                # rejectable while its content is published. A repeat
                # approve resumes what is resumable.
                log.warning(
                    "moderation: item %s (%s %s) landed but a side effect "
                    "failed: %r — kept approved",
                    item_id,
                    item.feature,
                    item.action,
                    exc,
                )
                await self._emit(
                    SpaceModerationApproved(
                        item=_decided(item, ModerationStatus.APPROVED, actor_user_id)
                    )
                )
                target = (item.payload or {}).get("target_id")
                return ApplyResult(
                    target_id=target if isinstance(target, str) else None,
                    post_id=target if item.feature == "posts" else None,
                    complete=False,
                )
            # Nothing was persisted (STALE, a validation refusal, a crash):
            # the item goes back to pending for another try — unless it
            # was decided meanwhile (the release is conditional).
            await self._spaces.release_moderation_item(
                item_id, claimed_status=ModerationStatus.APPROVED
            )
            raise
        await self._emit(
            SpaceModerationApproved(
                item=_decided(item, ModerationStatus.APPROVED, actor_user_id)
            )
        )
        return result

    async def _expire(self, item: SpaceModerationItem, actor_user_id: str) -> None:
        if await self._spaces.claim_moderation_item(
            item.id, status=ModerationStatus.EXPIRED, reviewed_by=actor_user_id
        ):
            await self._emit(
                SpaceModerationExpired(
                    item=_decided(item, ModerationStatus.EXPIRED, actor_user_id)
                )
            )

    async def _resume(
        self,
        item: SpaceModerationItem,
        handler: ModerationHandler,
        *,
        approved_by: str,
    ) -> ApplyResult:
        """Finish an APPROVED item whose apply stopped part-way."""
        resume = getattr(handler, "resume", None)
        if resume is None or not await resume(item, approved_by=approved_by):
            raise ModerationAlreadyDecidedError(f"item {item.id!r} is already approved")
        log.info("moderation: resumed approved item %s", item.id)
        target = (item.payload or {}).get("target_id")
        return ApplyResult(
            target_id=target if isinstance(target, str) else None,
            post_id=target if item.feature == "posts" else None,
        )

    async def _landed(
        self, item: SpaceModerationItem, handler: ModerationHandler
    ) -> bool:
        """Did the item's primary write reach its table before the failure?
        A create: the target now exists. A delete: it is gone. An edit:
        every proposed field reads back as proposed."""
        payload = item.payload or {}
        target = payload.get("target_id")
        if not isinstance(target, str):
            return False
        try:
            live = await handler.snapshot(item.space_id, target)
        except Exception:  # can't tell → assume nothing landed
            log.warning("moderation: could not re-read %s after a failure", target)
            return False
        match item.action:
            case ContentAction.CREATE.value:
                return live is not None
            case ContentAction.DELETE.value:
                if payload.get("op") in ("archive", "unarchive"):
                    return live is not None and bool(live.get("archived")) is (
                        payload.get("op") == "archive"
                    )
                return live is None
        proposed = payload.get("proposed") or payload.get("patch")
        if live is None or not isinstance(proposed, dict) or not proposed:
            return False
        return all(live.get(k) == v for k, v in proposed.items() if k in live)

    async def reject(
        self,
        space_id: str,
        item_id: str,
        *,
        actor_user_id: str,
        reason: str | None = None,
    ) -> None:
        """Reject one pending item (works on an archived space / disabled
        feature too). The reason reaches the submitter, never a push."""
        clean = (reason or "").strip() or None
        if clean is not None and len(clean) > MAX_REJECT_REASON:
            raise ValueError(
                f"reason must be at most {MAX_REJECT_REASON} characters",
            )
        await self._space(space_id)
        await self._require_content_authority(space_id, actor_user_id)
        item = await self.get_item(space_id, item_id)
        if item.status is not ModerationStatus.PENDING or not (
            await self._spaces.claim_moderation_item(
                item_id,
                status=ModerationStatus.REJECTED,
                reviewed_by=actor_user_id,
                reason=clean,
            )
        ):
            raise ModerationAlreadyDecidedError(
                f"item {item_id!r} is already decided",
            )
        await self._emit(
            SpaceModerationRejected(
                item=replace(
                    _decided(item, ModerationStatus.REJECTED, actor_user_id),
                    rejection_reason=clean,
                )
            )
        )

    # ── Expiry (scheduler) ───────────────────────────────────────────────

    async def expire_due(self, now: datetime | None = None) -> int:
        """Expire every overdue pending item; returns how many moved."""
        moved = await self._spaces.expire_due(now or datetime.now(timezone.utc))
        for item in moved:
            await self._emit(SpaceModerationExpired(item=item))
        return len(moved)

    async def purge_decided(self, now: datetime | None = None) -> int:
        """NULL the content of items decided more than the retention ago."""
        before = (now or datetime.now(timezone.utc)) - MODERATION_PAYLOAD_RETENTION
        return await self._spaces.purge_payloads(before)

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _feature_on(space: Space, item: SpaceModerationItem) -> bool:
        toggle = _FEATURE_TOGGLE.get(item.feature)
        if toggle is not None:
            return bool(getattr(space.features, toggle, True))
        if item.feature == "posts":
            post_type = str((item.payload or {}).get("type") or "text")
            if post_type == "bazaar" and not space.features.bazaar:
                return False
            return space.features.allows(post_type)
        return False


def _decided(
    item: SpaceModerationItem, status: ModerationStatus, reviewer: str | None
) -> SpaceModerationItem:
    return replace(
        item,
        status=status,
        reviewed_by=reviewer,
        reviewed_at=datetime.now(timezone.utc),
    )


def _aware(value: datetime) -> datetime:
    """``value`` as tz-aware UTC (a naive stored timestamp is UTC)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
