"""The space moderation queue for every feature (§4.3 ``MODERATED``).

Under a ``MODERATED`` access level a plain member's NEW item, and their
edit / delete of SOMEBODY ELSE's item, waits here until content authority
(owner / admin / moderator) approves it. Own edits / deletes, layout moves,
RSVPs, comments, reactions and content authority's own writes never queue
(:meth:`SpaceFeatures.access_decision`).

Every household that may review an item holds its own copy of it
(``space_moderation_queue``, the same id everywhere): the submitter's own
household (the author's ``…/moderation/mine``), the space's host, and every
household holding a live admin / moderator seat (v_43 federated moderation,
:class:`ModerationFederation` — ``SPACE_MODERATION_SUBMITTED``, sent per
target, never to a plain member household). Any of them may approve or
reject: the approving household applies the item itself through the
feature's normal persist path inside a :func:`release_scope`, so the
released content federates as the SUBMITTER's with the approval block, and
tells the others (``SPACE_MODERATION_DECIDED``). A decision received from
another household is applied with approve-beats-reject, so every copy
converges on what was published. Expiry stays local and deterministic (the
same ``expires_at`` everywhere).

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
submitter and content authority before approval, and nothing reaches a
household that holds no content-authority seat.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol

from ..domain.events import (
    SpaceConfigChanged,
    SpaceMemberLeft,
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
    ModerationPayloadTooLargeError,
    ModerationQueueFullError,
    ModerationStatus,
    ModerationTargetGoneError,
    ModerationUnavailableError,
    Space,
    SpaceModerationItem,
    SpacePermissionError,
    SpaceRole,
    WRITER_ROLES,
)
from ..domain.federation import FederationEventType
from ..repositories.base import dump_json
from .bus_publisher import BusPublisherMixin
from .moderation_release import release_scope

if TYPE_CHECKING:
    from ..domain.outbox import OutboxEntry
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
#: Contentless early-decision tombstones live this long, and at most this
#: many per deciding household.
TOMBSTONE_RETENTION = timedelta(days=14)
MAX_TOMBSTONES_PER_HOUSEHOLD = 200
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
    #: True on a reviewer household: the approval went to the host, which
    #: publishes the item (v_43) — nothing is persisted here yet.
    publishing: bool = False


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
    """The narrow face content services submit through."""

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


class ModerationFederation(Protocol):
    """Where items and decisions travel between households (v_43,
    :class:`~socialhome.services.space_moderation_federation.SpaceModerationFederation`)."""

    async def submission_targets(self, space: Space) -> list[str]:
        """The households a new item goes to; raises
        :class:`~socialhome.domain.space.HostTooOldError` when the host
        cannot hold it."""
        ...

    async def send_submitted(
        self, space: Space, item: SpaceModerationItem, targets: list[str]
    ) -> None: ...

    async def send_decided(
        self,
        space: Space,
        item: SpaceModerationItem,
        *,
        decision: ModerationStatus,
        decided_by: str,
        reason: str | None,
    ) -> None: ...

    async def send_release_request(
        self, space: Space, item: SpaceModerationItem, *, decided_by: str
    ) -> None:
        """A reviewer household's approval, to the host only."""
        ...

    async def reviewer_households(self, space: Space) -> list[str]:
        """The other households that review ``space`` right now."""
        ...

    async def display_name(self, space_id: str, user_id: str) -> str | None:
        """A remote submitter's name as the roster mirror holds it."""
        ...

    async def is_remote_writer(self, space_id: str, user_id: str) -> bool:
        """A member of another household with a live writer seat."""
        ...

    async def remote_role(self, space_id: str, user_id: str) -> str | None:
        """The role the roster mirror holds for a member of another household."""
        ...


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
        "_federated",
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
        #: Items + decisions to and from the other households (v_43).
        #: ``None`` (unit tests, no federation): only the host holds items.
        self._federated: ModerationFederation | None = None

    def attach_federation(self, federated: ModerationFederation) -> None:
        """Wire federated moderation (``app._build_space_moderation``)."""
        self._federated = federated

    # ── Registry ─────────────────────────────────────────────────────────

    def register(
        self, feature: str, action: ContentAction, handler: ModerationHandler
    ) -> None:
        """Register the handler for ``feature``'s ``action`` items."""
        key = (feature, action.value)
        if key in self._handlers:
            raise ValueError(f"moderation handler for {key!r} already registered")
        self._handlers[key] = handler

    def validate_payload(
        self, space: Space, feature: str, action: ContentAction, payload: dict
    ) -> dict:
        """``payload`` normalised by the ``(feature, action)`` handler — the
        live path's codecs and caps (a remote submission, v_43)."""
        return self._handler(feature, action.value).validate(space, dict(payload))

    async def local_snapshot(
        self, space_id: str, feature: str, action: ContentAction, payload: dict
    ) -> str | None:
        """The stored ``current_snapshot`` for a received item, from THIS
        household's copy of the target (v_43): every field of it for a
        delete, the edited fields for an edit, nothing for a create or a
        target not held here."""
        target = payload.get("target_id")
        if action is ContentAction.CREATE or not isinstance(target, str):
            return None
        live = await self._handler(feature, action.value).snapshot(space_id, target)
        if live is None:
            return None
        if action is ContentAction.EDIT:
            if payload.get("op") == "resolve_conflict":
                keys: list[str] = ["content"]
            else:
                changed = payload.get("proposed") or payload.get("patch") or {}
                keys = [k for k in changed if k in live]
            live = {k: live.get(k) for k in keys}
        return dump_json(live)

    def _handler(self, feature: str, action: str) -> ModerationHandler:
        handler = self._handlers.get((feature, action))
        if handler is None:
            # Fail closed: an item nothing knows how to apply can't queue.
            raise SpacePermissionError(f"{feature} {action} cannot be reviewed here")
        return handler

    # ── Households ───────────────────────────────────────────────────────

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
        """Queue one submission here, send it to the households that may
        review it, and announce it to local content authority.

        A member household (not the host) can only submit through the
        federation (fail closed without it); a host below v_43 refuses
        (:class:`~socialhome.domain.space.HostTooOldError`, 409) before
        anything is stored."""
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
        if self._federated is not None:
            targets = await self._federated.submission_targets(space)
        elif self.is_host(space):
            targets = []
        else:
            # Only the host could review it, and nothing can reach the host.
            raise SpacePermissionError(f"{feature} here needs review, which is off")
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
        if targets and self._federated is not None:
            await self._federated.send_submitted(space, item, targets)
        return item

    async def store_received(self, item: SpaceModerationItem) -> bool:
        """Hold an item another household submitted (v_43) — its inbound
        handler checked it. Stored once (the same id everywhere): True when
        this call stored it, which announces it to local content authority."""
        if not await self._spaces.insert_moderation_item_if_absent(item):
            # A decision overtook it (a tombstone): the content fills in, the
            # decision stands — nothing new to review.
            await self._spaces.fill_moderation_tombstone(item)
            return False
        await self._emit(SpaceModerationQueued(item=item))
        return True

    async def apply_decision(
        self,
        item: SpaceModerationItem,
        *,
        decision: ModerationStatus,
        decided_by: str,
        reason: str | None,
    ) -> bool:
        """Record another household's decision on a held item (v_43). The
        first decision wins — except that an approval beats a rejection
        (the content was published, so every copy says so). Content is
        never applied from here: the approving household published it, and
        it arrives as its own ``SPACE_*`` write. True when the row moved."""
        if decision is ModerationStatus.APPROVED:
            moved = await self._spaces.claim_moderation_item(
                item.id,
                status=ModerationStatus.APPROVED,
                reviewed_by=decided_by,
                from_statuses=(ModerationStatus.PENDING, ModerationStatus.REJECTED),
            )
            if moved:
                await self._emit(
                    SpaceModerationApproved(
                        item=_decided(item, ModerationStatus.APPROVED, decided_by)
                    )
                )
            return moved
        moved = await self._spaces.claim_moderation_item(
            item.id,
            status=ModerationStatus.REJECTED,
            reviewed_by=decided_by,
            reason=reason,
        )
        if moved:
            await self._emit(
                SpaceModerationRejected(
                    item=replace(
                        _decided(item, ModerationStatus.REJECTED, decided_by),
                        rejection_reason=reason,
                    )
                )
            )
        return moved

    async def held_row(
        self, space_id: str, feature: str, target_id: str
    ) -> dict | None:
        """Our own copy of ``target_id`` in ``feature`` (the handler's
        snapshot) — what a release of an edit is checked against."""
        handler = self._handlers.get(
            (feature, ContentAction.EDIT.value)
        ) or self._handlers.get((feature, ContentAction.CREATE.value))
        if handler is None:
            return None
        # A handler may hold more than its reviewer-facing snapshot (the
        # task's recurrence, the event's feed flag): what a release of an
        # edit must leave unchanged.
        held = getattr(handler, "held", None)
        if held is not None:
            row: dict | None = await held(space_id, target_id)
            return row
        return await handler.snapshot(space_id, target_id)

    async def note_release(self, item: SpaceModerationItem, approved_by: str) -> None:
        """A release of an item we hold arrived from the host and passed
        every check: the row reads approved now (approve beats reject),
        before — or without — the host's decision."""
        if await self._spaces.claim_moderation_item(
            item.id,
            status=ModerationStatus.APPROVED,
            reviewed_by=approved_by,
            from_statuses=(ModerationStatus.PENDING, ModerationStatus.REJECTED),
        ):
            await self._emit(
                SpaceModerationApproved(
                    item=_decided(item, ModerationStatus.APPROVED, approved_by)
                )
            )

    async def record_early_decision(
        self,
        *,
        item_id: str,
        space_id: str,
        decision: ModerationStatus,
        decided_by: str,
        reason: str | None,
        from_instance: str = "",
    ) -> bool:
        """A decision for an item not held here yet (it overtook the
        submission): keep a contentless tombstone under its id, so a late
        ``SPACE_MODERATION_SUBMITTED`` cannot store it as pending. At most
        :data:`MAX_TOMBSTONES_PER_HOUSEHOLD` live per deciding household
        (beyond it the decision is dropped, WARNING)."""
        if (
            await self._spaces.count_moderation_tombstones(from_instance)
            >= MAX_TOMBSTONES_PER_HOUSEHOLD
        ):
            log.warning(
                "moderation: %s holds %d early-decision tombstones here — "
                "dropping its decision on unknown item %s",
                from_instance,
                MAX_TOMBSTONES_PER_HOUSEHOLD,
                item_id,
            )
            return False
        now = datetime.now(timezone.utc)
        return await self._spaces.insert_moderation_item_if_absent(
            SpaceModerationItem(
                id=item_id,
                space_id=space_id,
                feature="",
                action="",
                submitted_by="",
                payload={},
                current_snapshot=dump_json({"tombstone_from": from_instance}),
                submitted_at=now,
                expires_at=now,
                status=decision,
                reviewed_by=decided_by,
                reviewed_at=now,
                rejection_reason=reason,
            )
        )

    async def drop_held_for_others(self, space_id: str) -> int:
        """This household lost its last content-authority seat in the
        space: the pending items it holds for OTHER households' members go
        (their content NULLed, the rows expired). Its own members' items —
        their pending strip — stay."""
        space = await self._spaces.get(space_id)
        if space is None or self.is_host(space):
            return 0
        members = await self._spaces.list_members(space_id)
        if any(m.role in CONTENT_AUTHORITY_ROLES for m in members):
            return 0
        return await self._spaces.drop_pending_from_others(
            space_id, keep_submitters=frozenset(m.user_id for m in members)
        )

    def watch_seats(self, bus: "EventBus") -> None:
        """Re-check this household's content authority whenever a local
        seat changes (a role change, a member leaving or removed)."""
        bus.subscribe(SpaceConfigChanged, self._on_seat_change)
        bus.subscribe(SpaceMemberLeft, self._on_seat_change)

    async def _on_seat_change(
        self, event: SpaceConfigChanged | SpaceMemberLeft
    ) -> None:
        dropped = await self.drop_held_for_others(event.space_id)
        if dropped:
            log.info(
                "moderation: no content authority left in space %s — dropped "
                "%d pending item(s) of other households",
                event.space_id,
                dropped,
            )

    async def outbox_entry_wanted(self, entry: "OutboxEntry") -> bool:
        """May a queued envelope still go out? A retried
        ``SPACE_MODERATION_SUBMITTED`` only to a household that still
        reviews the space (the host, or a live admin / moderator seat)."""
        if entry.event_type is not FederationEventType.SPACE_MODERATION_SUBMITTED:
            return True
        if self._federated is None:
            return False
        try:
            space_id = str(json.loads(entry.payload_json).get("space_id") or "")
        except TypeError, ValueError:
            return False
        space = await self._spaces.get(space_id) if space_id else None
        wanted = space is not None and entry.instance_id in (
            await self._federated.reviewer_households(space)
        )
        if not wanted:
            log.warning(
                "moderation: dropping a queued submission to %s — no longer a "
                "reviewer of space %s",
                entry.instance_id,
                space_id,
            )
        return wanted

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
        if not item.feature:
            # A tombstone: decided elsewhere before it ever reached us.
            raise ModerationAlreadyDecidedError(
                f"item {item_id!r} was decided before this household received it"
            )
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
        display = user.display_name or user.username if user else None
        if display is None and self._federated is not None:
            display = await self._federated.display_name(
                item.space_id, item.submitted_by
            )
        return {
            "id": item.id,
            "space_id": item.space_id,
            "feature": item.feature,
            "action": item.action,
            "entity": payload.get("entity"),
            "op": payload.get("op"),
            "target_id": target_id,
            "submitted_by": item.submitted_by,
            "submitted_by_display": display or item.submitted_by,
            "submitted_at": _iso(item.submitted_at),
            "expires_at": _iso(item.expires_at),
            "status": item.status.value,
            "reviewed_by": item.reviewed_by,
            "reviewed_at": _iso(item.reviewed_at),
            "rejection_reason": item.rejection_reason,
            # Approved on this reviewer household, waiting for the host to
            # publish it (v_43).
            "publishing": (
                item.status is ModerationStatus.PENDING and bool(item.reviewed_by)
            ),
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
        role = await self._require_content_authority(space_id, actor_user_id)
        if not self.is_host(space):
            # Only the host applies an item, from its own stored copy (v_43):
            # here the approval is checked and handed to the host.
            return await self._request_release(space, role, item_id, actor_user_id)
        return await self._guarded_approve(
            space, role, item_id, actor_user_id, force, allow_rejected=True
        )

    async def release_remote(
        self, item: SpaceModerationItem, *, approved_by: str, role: str
    ) -> ApplyResult:
        """The host applies an item a moderator on ANOTHER household approved
        (their ``SPACE_MODERATION_DECIDED``; the inbound handler verified
        that seat). Every gate of a local approve runs for that approver,
        from this household's own copy; approve beats reject."""
        space = await self._space(item.space_id)
        if not self.is_host(space):
            raise SpacePermissionError("only the space's host releases an item")
        return await self._guarded_approve(
            space, role, item.id, approved_by, False, allow_rejected=True
        )

    async def _guarded_approve(
        self,
        space: Space,
        role: str,
        item_id: str,
        actor_user_id: str,
        force: bool,
        *,
        allow_rejected: bool = False,
    ) -> ApplyResult:
        if item_id in self._inflight:
            raise ModerationInProgressError(f"item {item_id!r} is being approved")
        self._inflight.add(item_id)
        try:
            return await self._approve(
                space,
                role,
                item_id,
                actor_user_id,
                force,
                allow_rejected=allow_rejected,
            )
        finally:
            self._inflight.discard(item_id)

    async def _request_release(
        self, space: Space, role: str, item_id: str, actor_user_id: str
    ) -> ApplyResult:
        """A reviewer household's approve: the same gates as on the host,
        then ``SPACE_MODERATION_DECIDED{approved}`` to the host, which
        applies the item. The row stays pending here — marked as sent for
        publishing — until the host's decision arrives."""
        item = await self.get_item(space.id, item_id)
        self._handler(item.feature, item.action)
        if item.status is not ModerationStatus.PENDING:
            raise ModerationAlreadyDecidedError(
                f"item {item_id!r} is already {item.status.value}"
            )
        if _aware(item.expires_at) <= datetime.now(timezone.utc):
            await self._expire(item, actor_user_id)
            raise ModerationExpiredError("this submission's review window has passed")
        if space.archived or not self._feature_on(space, item):
            raise ModerationUnavailableError(
                f"{item.feature} is not available in this space right now"
            )
        if (
            space.features.access_decision(
                item.feature,
                role=role,
                action=ContentAction(item.action),
                owns_target=False,
            )
            is AccessDecision.DENY
        ):
            raise AccessAdminOnlyError(item.feature)
        if not await self._submitter_writes(space.id, item.submitted_by):
            raise ModerationTargetGoneError("the author is no longer a member")
        if self._federated is None:
            raise SpacePermissionError("the space's host can't be reached from here")
        await self._federated.send_release_request(
            space, item, decided_by=actor_user_id
        )
        await self._spaces.mark_release_requested(item.id, reviewed_by=actor_user_id)
        target = (item.payload or {}).get("target_id")
        return ApplyResult(
            target_id=target if isinstance(target, str) else None,
            post_id=target if item.feature == "posts" else None,
            publishing=True,
        )

    async def _approve(
        self,
        space: Space,
        role: str,
        item_id: str,
        actor_user_id: str,
        force: bool,
        *,
        allow_rejected: bool = False,
    ) -> ApplyResult:
        space_id = space.id
        item = await self.get_item(space_id, item_id)
        handler = self._handler(item.feature, item.action)
        open_statuses = (
            (ModerationStatus.PENDING, ModerationStatus.REJECTED)
            if allow_rejected
            else (ModerationStatus.PENDING,)
        )
        if item.status not in (*open_statuses, ModerationStatus.APPROVED):
            raise ModerationAlreadyDecidedError(
                f"item {item_id!r} is already {item.status.value}"
            )
        if item.status is ModerationStatus.REJECTED and not await self._may_overturn(
            space, role, item
        ):
            raise ModerationAlreadyDecidedError(
                f"item {item_id!r} was rejected by a higher role — only an equal "
                "or higher role can overturn it"
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
        if not await self._submitter_writes(space_id, item.submitted_by):
            # The author left, was removed or demoted: their words are not
            # published on their behalf any more (a published item keeps
            # its status — only the rest of it is withheld).
            if not resuming:
                await self._expire(item, actor_user_id)
            raise ModerationTargetGoneError("the author is no longer a member")
        if resuming:
            with release_scope(item.id, actor_user_id, approver_role=role):
                return await self._resume(item, handler, approved_by=actor_user_id)
        if not await self._spaces.claim_moderation_item(
            item_id,
            status=ModerationStatus.APPROVED,
            reviewed_by=actor_user_id,
            from_statuses=open_statuses,
        ):
            raise ModerationAlreadyDecidedError(f"item {item_id!r} was just decided")
        try:
            # Everything the apply federates is this item's release (v_43).
            with release_scope(item.id, actor_user_id, approver_role=role):
                result = await handler.apply(
                    item, approved_by=actor_user_id, force=force
                )
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
                await self._announce_decision(
                    space, item, ModerationStatus.APPROVED, actor_user_id, None
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
        await self._announce_decision(
            space, item, ModerationStatus.APPROVED, actor_user_id, None
        )
        return result

    async def _announce_decision(
        self,
        space: Space,
        item: SpaceModerationItem,
        decision: ModerationStatus,
        decided_by: str,
        reason: str | None,
    ) -> None:
        """Tell the other households holding the item (v_43). Fail-soft: the
        decision stands here (and its content is already published); a
        household that misses it expires its copy on schedule."""
        if self._federated is None:
            return
        try:
            await self._federated.send_decided(
                space, item, decision=decision, decided_by=decided_by, reason=reason
            )
        except Exception as exc:
            log.warning(
                "moderation: could not announce the %s of item %s: %r",
                decision.value,
                item.id,
                exc,
            )

    async def _may_overturn(
        self, space: Space, role: str, item: SpaceModerationItem
    ) -> bool:
        """Approve beats reject only from an equal or higher role than the
        rejecter's (owner > admin > moderator) — the seats the host holds
        for both; a rejecter whose role is unknown counts as a moderator."""
        rejecter = item.reviewed_by or ""
        member = await self._spaces.get_member(space.id, rejecter) if rejecter else None
        if member is not None:
            theirs = str(member.role)
        elif self._federated is not None and rejecter:
            theirs = await self._federated.remote_role(space.id, rejecter) or ""
        else:
            theirs = ""
        return _rank(role) >= (_rank(theirs) or _rank(SpaceRole.MODERATOR.value))

    async def _submitter_writes(self, space_id: str, user_id: str) -> bool:
        """The submitter still holds a writer seat and is not banned — their
        local seat here, or (a member of another household, v_43) their live
        mirrored seat."""
        if await self._spaces.is_banned(space_id, user_id):
            return False
        member = await self._spaces.get_member(space_id, user_id)
        if member is not None:
            return member.role in WRITER_ROLES
        if self._federated is None:
            return False
        return await self._federated.is_remote_writer(space_id, user_id)

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
        space = await self._space(space_id)
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
        await self._announce_decision(
            space, item, ModerationStatus.REJECTED, actor_user_id, clean
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
        at = now or datetime.now(timezone.utc)
        await self._spaces.delete_stale_tombstones(at - TOMBSTONE_RETENTION)
        return await self._spaces.purge_payloads(at - MODERATION_PAYLOAD_RETENTION)

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _feature_on(space: Space, item: SpaceModerationItem) -> bool:
        return feature_on(space, item.feature, item.payload or {})


def feature_on(space: Space, feature: str, payload: dict) -> bool:
    """Is ``feature`` switched on in ``space`` for an item carrying
    ``payload`` (a post's type must be allowed)?"""
    toggle = _FEATURE_TOGGLE.get(feature)
    if toggle is not None:
        return bool(getattr(space.features, toggle, True))
    if feature == "posts":
        post_type = str(payload.get("type") or "text")
        if post_type == "bazaar" and not space.features.bazaar:
            return False
        return space.features.allows(post_type)
    return False


_RANKS = {
    SpaceRole.OWNER.value: 3,
    SpaceRole.ADMIN.value: 2,
    SpaceRole.MODERATOR.value: 1,
}


def _rank(role: str) -> int:
    return _RANKS.get(str(role), 0)


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
