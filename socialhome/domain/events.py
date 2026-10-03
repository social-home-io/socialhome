"""Domain events (§5.2 pattern ①).

Services persist state, publish a :class:`DomainEvent`, and return. The
``EventBus`` delivers events to subscribers — notification service, WebSocket
manager, federation broadcaster — which react *synchronously* under the same
asyncio event loop.

Events are frozen dataclasses. They carry enough context for any subscriber
to act without reaching back into repositories. When a subscriber needs more
than the event carries, the correct answer is to extend the event, not to
add a repository reference to the subscriber.

Only the widely-used events are defined here. UI-specific or operational
events live next to the service that publishes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .calendar import CalendarEvent
    from .mention import Mention
    from .post import Comment, Post
    from .space import SpaceModerationItem
    from .task import Task
    from .timetable import Timetable
    from .user import UserStatus


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DomainEvent:
    """Marker base class. All events are ``@dataclass(slots=True, frozen=True)``
    subclasses; this class carries no fields of its own.
    """


# ─── Posts + comments ─────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class PostCreated(DomainEvent):
    post: "Post"
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PostEdited(DomainEvent):
    post: "Post"
    occurred_at: datetime = field(default_factory=_now)
    #: ``None`` when the post lives in the household feed; the
    #: containing ``space_id`` when it's a space post. The outbound
    #: federation bridge in :mod:`socialhome.services.space_post_outbound`
    #: gates broadcast on this field — without it we couldn't tell
    #: whether to fan SPACE_POST_UPDATED to space members or do
    #: nothing (household-only edits stay local).
    space_id: str | None = None
    #: ``None`` on local origination; set to the originating peer's
    #: instance_id when ``federation_inbound_service`` re-publishes
    #: after receiving SPACE_POST_UPDATED. See ``SpacePostCreated``
    #: for the loop-prevention rationale.
    origin_instance_id: str | None = None
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    #: Space posts only: the @-mentions this edit newly ADDS (in the new
    #: content, not in the old — :func:`~socialhome.domain.mention.mentions_added`),
    #: resolved on this household's own member view. Only these people are
    #: notified about an edit. Never on the wire.
    new_mentions: tuple["Mention", ...] = ()


@dataclass(slots=True, frozen=True)
class PostDeleted(DomainEvent):
    """Soft-delete — content cleared, node retained."""

    post_id: str
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    occurred_at: datetime = field(default_factory=_now)
    #: ``None`` for household-feed deletes, ``space_id`` for space
    #: post deletes — same gate as :class:`PostEdited`.
    space_id: str | None = None
    origin_instance_id: str | None = None


@dataclass(slots=True, frozen=True)
class PostReactionChanged(DomainEvent):
    post: "Post"
    occurred_at: datetime = field(default_factory=_now)
    space_id: str | None = None
    origin_instance_id: str | None = None


@dataclass(slots=True, frozen=True)
class CommentAdded(DomainEvent):
    post_id: str
    comment: "Comment"
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)
    #: ``None`` when local-origination; set on inbound replay — see
    #: :class:`SpacePostCreated` for the loop-prevention rationale.
    origin_instance_id: str | None = None
    #: Space comments only: @-mentions resolved against the space's members
    #: by :class:`~socialhome.services.space_mentions.SpaceMentionResolver`.
    #: Empty for household-feed comments.
    mentions: tuple["Mention", ...] = ()


@dataclass(slots=True, frozen=True)
class CommentUpdated(DomainEvent):
    """Comment body edited. Broadcast as ``comment.updated`` WS frame."""

    post_id: str
    comment: "Comment"
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)
    origin_instance_id: str | None = None
    #: Space comments only: the @-mentions this edit newly adds (see
    #: :attr:`PostEdited.new_mentions`).
    new_mentions: tuple["Mention", ...] = ()


@dataclass(slots=True, frozen=True)
class CommentDeleted(DomainEvent):
    """Comment removed. Broadcast as ``comment.deleted`` WS frame."""

    post_id: str
    comment_id: str
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)
    origin_instance_id: str | None = None


# ─── Spaces ───────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class SpacePostCreated(DomainEvent):
    post: "Post"
    space_id: str
    #: @-mentions in ``post.content``, resolved against the space's members
    #: by each household on its own member view
    #: (:class:`~socialhome.services.space_mentions.SpaceMentionResolver`).
    #: Never on the wire. May include one ``MentionType.HERE`` entry — only
    #: when the space allows @here and the author is owner/admin by this
    #: household's roster (the resolver drops it otherwise).
    mentions: tuple["Mention", ...] = ()
    approved_by: str | None = None
    occurred_at: datetime = field(default_factory=_now)
    #: ``None`` when the post was created locally (via the SPA's
    #: ``POST /api/spaces/{id}/posts``). Set to the originating
    #: peer's instance_id when the post arrived via federation —
    #: outbound federation bridges check this so an inbound-driven
    #: publish doesn't fan back out as a loop. Existing local
    #: subscribers (realtime WS, search index, HA bridge) ignore
    #: this field and broadcast for every event regardless.
    origin_instance_id: str | None = None
    #: For a public/global space post received from another member household,
    #: the author-household's pre-signed Phase-5a inner payload (see
    #: space_public_author.build_signed_author_inner). A seed-holding receiver
    #: relays it verbatim to the GFS so the post reaches subscribers even when
    #: the owner is offline. None for local posts and private-space posts.
    public_relay: dict | None = None


@dataclass(slots=True, frozen=True)
class SpacePostModerated(DomainEvent):
    """An admin removed a post as a moderation action.

    Triggers federation broadcast (``SPACE_POST_DELETED``) to member
    instances. The ``post`` value carries ``moderated=True`` and ``content``
    cleared.
    """

    space_id: str
    post: "Post"
    moderated_by: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceModerationQueued(DomainEvent):
    item: "SpaceModerationItem"
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceModerationApproved(DomainEvent):
    item: "SpaceModerationItem"
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceModerationRejected(DomainEvent):
    item: "SpaceModerationItem"
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceModerationExpired(DomainEvent):
    """A queued item nobody decided within its review window (7 days)."""

    item: "SpaceModerationItem"
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ReportFiled(DomainEvent):
    """A user filed a report (``ReportTargetType`` value as ``target_type``).

    ``space_id`` is set for a space-scoped report — its content authority
    triages it — and ``None`` for a household-level one.
    """

    report_id: str
    target_type: str
    target_id: str
    category: str
    reporter_user_id: str
    space_id: str | None = None
    #: Whom the report is about — the reported member, or the item's
    #: author — so they are never asked to triage it (``None``: unknown).
    subject_user_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ReportResolved(DomainEvent):
    report_id: str
    resolved_by: str
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceConfigChanged(DomainEvent):
    space_id: str
    event_type: str
    payload: dict
    sequence: int
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceProposalUpdated(DomainEvent):
    """A critical-action approval proposal was opened / voted / resolved.

    Drives the local realtime ``space.proposal.updated`` WS frame and, on
    the host, the ``SPACE_ADMIN_PROPOSAL_UPDATED`` federation broadcast that
    mirrors the proposal + tally onto admin households so their SPA can
    render it and vote. ``view`` is the SPA-facing snapshot built by
    :class:`SpaceApprovalService` (id, action, tally, status, …).
    """

    space_id: str
    proposal_id: str
    view: dict
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceMemberLocationOptedIn(DomainEvent):
    """A member just enabled location_share_enabled for a space.

    :class:`SpaceLocationOutbound` subscribes and re-fires the
    member's current household presence for this one space so a
    fresh opt-in produces an immediate pin instead of waiting for
    the next HA push (could be minutes).
    """

    space_id: str
    user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceLocationModeChanged(DomainEvent):
    """Admin flipped a space's ``features.location_mode`` (§23.8.6).

    Picked up by :class:`SpaceLocationOutbound` to refire the latest
    presence for every opted-in member of *this one space* — so
    receivers see the new mode (GPS pin → zone label, or vice versa)
    within seconds, rather than waiting for the next HA push.
    """

    space_id: str
    new_mode: str  # "gps" | "zone_only"
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class RemoteSpaceLocationUpdated(DomainEvent):
    """A remote member's space-map pin was stored (§23.8.6).

    Published by the inbound ``SPACE_LOCATION_UPDATED`` handler after the
    ``space_remote_member_locations`` upsert, with coordinates already
    truncated to 4 decimals. :class:`SpaceLocationOutbound` turns it into
    the same local ``space_location_updated`` frame a local member's move
    produces, so remote pins move live instead of on the Map tab's poll.
    Local-only: never re-federated.
    """

    space_id: str
    instance_id: str
    user_id: str
    mode: str  # "gps" | "zone_only"
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None
    zone_id: str | None = None
    zone_name: str | None = None
    updated_at: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceLocationFeatureEnabled(DomainEvent):
    """Admin flipped ``feature_location`` from OFF to ON (§23.8.6).

    Published by :class:`SpaceService.update_config` only on the
    OFF→ON transition (not on idempotent re-enables or other feature
    toggles). :class:`NotificationService` subscribes and nudges
    every space member — except the actor — to visit Personal
    Settings → Privacy → Space location sharing to opt in.
    """

    space_id: str
    space_name: str
    actor_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceZoneUpserted(DomainEvent):
    """A per-space display zone was created or modified (§23.8.7).

    Picked up by ``SpaceZoneOutboundService`` (federation fan-out)
    and ``RealtimeService`` (local WS ``space_zone_changed`` frame).
    """

    space_id: str
    zone_id: str
    name: str
    latitude: float
    longitude: float
    radius_m: int
    color: str | None
    created_by: str
    updated_at: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceZoneDeleted(DomainEvent):
    """A per-space display zone was deleted (§23.8.7)."""

    space_id: str
    zone_id: str
    deleted_by: str
    occurred_at: datetime = field(default_factory=_now)


# ─── Tasks ────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class TaskAssigned(DomainEvent):
    task: "Task"
    assigned_to: str  # user_id
    #: Set for a space task — consumers gate delivery on membership.
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskCompleted(DomainEvent):
    task: "Task"
    completed_by: str  # user_id
    spawned_next: "Task | None" = None
    #: Set for a space task — fans out to the space, not the household.
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskDeadlineDue(DomainEvent):
    """Published at 08:00 local time on a task's due-date. One event per
    (task, due_date) — the notification service fans out to all assignees.
    """

    task: "Task"
    due_date: date
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskCreated(DomainEvent):
    """Any new task is created. :class:`RealtimeService` broadcasts
    this as ``task.created`` so co-members see the row appear live
    (household scope — space scope fan-out is tighter).

    ``origin_instance_id`` is set by federation inbound so the outbound
    bridge can skip echoing a peer's own edit back to it."""

    task: "Task"
    space_id: str | None = None
    origin_instance_id: str | None = None
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskUpdated(DomainEvent):
    """Title / description / due / status / position / assignees
    / priority / labels change. Broadcast as ``task.updated``.
    ``origin_instance_id`` as on :class:`TaskCreated`."""

    task: "Task"
    space_id: str | None = None
    origin_instance_id: str | None = None
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskDeleted(DomainEvent):
    """Task row removed. Broadcast as ``task.deleted``.
    ``origin_instance_id`` as on :class:`TaskCreated`."""

    task_id: str
    list_id: str
    space_id: str | None = None
    origin_instance_id: str | None = None
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TimetableSaved(DomainEvent):
    """A timetable was created or edited (the full new state).

    Broadcast as ``timetable.changed`` — household scope when
    ``space_id`` is ``None``, else to the space's local members.
    ``origin_instance_id`` is set by federation inbound so the outbound
    bridge can skip echoing a peer's own edit back to it.
    """

    timetable: "Timetable"
    space_id: str | None = None
    origin_instance_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TimetableDeleted(DomainEvent):
    """A timetable was deleted. Broadcast as ``timetable.deleted``.

    ``deleted_by`` names the space admin who deleted a space timetable and
    ``created_by`` its creator — the federated delete carries both, so a
    receiver binds the admin to the sending household and tombstones an id
    it never held only when that id commits to ``created_by`` in the space.
    """

    timetable_id: str
    space_id: str | None = None
    origin_instance_id: str | None = None
    deleted_by: str | None = None
    created_by: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskListCreated(DomainEvent):
    """New task list. Broadcast as ``task_list.created`` so sidebars
    refresh live when another tab adds a list.

    ``created_by`` names the creator (a space list's id is owner-bound to
    it); ``origin_instance_id`` is set by federation inbound so the
    outbound bridge skips echoing a peer's own change back."""

    list_id: str
    name: str
    space_id: str | None = None
    created_by: str = ""
    origin_instance_id: str | None = None
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskListUpdated(DomainEvent):
    """Task-list rename / colour / emoji. ``origin_instance_id`` as on
    :class:`TaskListCreated`."""

    list_id: str
    name: str
    space_id: str | None = None
    origin_instance_id: str | None = None
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskListDeleted(DomainEvent):
    """Task-list removed (cascades to tasks via DB FK).
    ``origin_instance_id`` as on :class:`TaskListCreated`."""

    list_id: str
    space_id: str | None = None
    origin_instance_id: str | None = None
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    occurred_at: datetime = field(default_factory=_now)


# ─── Schedule polls (§9) ─────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class SchedulePollCreated(DomainEvent):
    """A schedule poll (slot definitions + title + deadline) was just
    persisted alongside its wrapper :class:`PostType.SCHEDULE` post.

    Picked up by :class:`ScheduleFederationOutbound` (F5) so the slot
    definitions reach remote member households — without this the
    wrapper post federates as text-only and the remote SPA renders
    an empty slot picker.
    """

    post_id: str
    title: str
    deadline: str | None
    #: Frozen tuple of slot dicts ``{"id", "slot_date", "start_time",
    #: "end_time", "position"}`` — same shape the repo's
    #: ``create_schedule_poll`` accepts.
    slots: tuple[dict, ...]
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SchedulePollResponded(DomainEvent):
    """A member voted / changed / retracted their availability.

    ``response`` is ``"yes"`` / ``"maybe"`` / ``"no"`` / ``"retracted"``
    so consumers can update aggregate counts without a full summary
    fetch.
    """

    post_id: str
    slot_id: str
    user_id: str
    response: str
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PollCreated(DomainEvent):
    """A reply poll was attached to a post (§9)."""

    post_id: str
    question: str
    allow_multiple: bool
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PollVoted(DomainEvent):
    """A user cast or retracted a vote. ``option_ids`` is the full set
    after the change (empty = retracted)."""

    post_id: str
    voter_user_id: str
    option_ids: tuple[str, ...]
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PollClosed(DomainEvent):
    """Author closed the poll — no more votes accepted."""

    post_id: str
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SchedulePollFinalized(DomainEvent):
    """Author locked in the winning slot.

    Space-scoped polls trigger the calendar auto-create (§17.2 /
    §23.53) when the space's ``calendar`` feature is enabled.
    """

    post_id: str
    slot_id: str
    slot_date: str
    start_time: str | None
    end_time: str | None
    title: str
    finalized_by: str
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


# ─── Calendar ─────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class CalendarEventCreated(DomainEvent):
    event: "CalendarEvent"
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class CalendarEventUpdated(DomainEvent):
    event: "CalendarEvent"
    #: Phase D: names of *material* fields that changed in this update
    #: (start / end / summary / capacity-down). Empty tuple means the
    #: update was cosmetic-only (e.g. attendees list reorder) — push
    #: notifications skip it.
    material_changes: tuple[str, ...] = ()
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class CalendarEventDeleted(DomainEvent):
    event_id: str
    #: Phase D: snapshot the event so the cancellation push handler can
    #: still produce a meaningful title even though the row is now gone.
    #: ``None`` when the deletion path doesn't have the prior event in
    #: hand (e.g. inbound federation only carries the id).
    summary: str | None = None
    space_id: str | None = None
    #: Phase D: user_ids to notify (RSVPed `going` / `waitlist` /
    #: `requested` at deletion time). Captured pre-delete since the
    #: ``ON DELETE CASCADE`` FK on `space_calendar_rsvps` wipes the rows
    #: before subscribers run.
    notify_user_ids: tuple[str, ...] = ()
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceRsvpChanged(DomainEvent):
    """A user's RSVP on a space calendar event was set, changed, or
    cleared (§23.7). Carries the new effective status; ``None`` means
    "RSVP removed". Used by :class:`SpaceRsvpMirrorBridge` to keep a
    mirror of going-events on the user's personal calendar in sync —
    accepting an RSVP drops the event onto your own calendar so it
    shows up alongside household events without flipping back to the
    space surface.
    """

    event_id: str
    space_id: str
    user_id: str
    occurrence_at: str
    status: str | None  # None ⇒ removed
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class EventReminderDue(DomainEvent):
    """Phase D: scheduler emits this when a reminder window comes due.

    The notification service subscribes and produces a push + an in-app
    notification row. ``minutes_before`` is the user's configured offset
    (carried for analytics + the UI badge — "in 1 hour: …").
    """

    event_id: str
    user_id: str
    occurrence_at: str
    minutes_before: int
    summary: str
    space_id: str
    occurred_at: datetime = field(default_factory=_now)


# ─── Users ────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class UserStatusChanged(DomainEvent):
    user_id: str
    status: "UserStatus | None"  # None = status cleared
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class UserProvisioned(DomainEvent):
    user_id: str
    username: str
    is_admin: bool
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class UserDeprovisioned(DomainEvent):
    user_id: str
    username: str
    occurred_at: datetime = field(default_factory=_now)


# ─── Online status (session presence) ─────────────────────────────────────
#
# Orthogonal to physical :class:`PresenceUpdated`: a user can be ``home``
# but offline (no WS session) or ``away`` but online (using the app on the
# go). These events are local-only — never federated — and drive the green
# / amber dot on member avatars.


@dataclass(slots=True, frozen=True)
class UserCameOnline(DomainEvent):
    """First WS session for ``user_id`` opened (was offline → online)."""

    user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class UserWentIdle(DomainEvent):
    """Every open session has been idle ≥ ``IDLE_AFTER`` (online → idle)."""

    user_id: str
    last_active_at: datetime
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class UserResumedActive(DomainEvent):
    """At least one session became active again (idle → online)."""

    user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class UserWentOffline(DomainEvent):
    """Last WS session for ``user_id`` closed (online/idle → offline).

    ``last_seen_at`` is the timestamp persisted to ``users.last_seen_at``
    so the UI can render "Last seen 2 h ago" after a server restart.
    """

    user_id: str
    last_seen_at: datetime
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class UserProfileUpdated(DomainEvent):
    """Display-name / bio / picture edit on a local user (§23 profile).

    ``picture_hash`` is the new cache-busting digest (None when the
    picture was cleared). ``picture_webp`` carries the bytes so the
    federation-outbound layer can fan them to paired peers; WS
    broadcasts drop it and send only the hash so the frame stays small.

    ``handle`` carries the public @handle so a ``set_handle`` edit can
    federate via USER_UPDATED like any other profile change. Defaulted
    ``None`` so the other publishers (display_name/bio/picture edits,
    renames) that don't touch the handle still construct unchanged.
    """

    user_id: str
    username: str
    display_name: str
    bio: str | None
    picture_hash: str | None
    picture_webp: bytes | None = None
    handle: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceMemberProfileUpdated(DomainEvent):
    """Per-space override changed (display_name or picture; §4.1.6).

    Same ``picture_webp`` discipline as :class:`UserProfileUpdated`.
    """

    space_id: str
    user_id: str
    space_display_name: str | None
    picture_hash: str | None
    picture_webp: bytes | None = None
    occurred_at: datetime = field(default_factory=_now)


# ─── Gallery (§23.119) ────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class GalleryAlbumCreated(DomainEvent):
    album_id: str
    space_id: str | None
    owner_id: str
    occurred_at: datetime = field(default_factory=_now)
    #: ``None`` on local origination; the sending household's instance
    #: id when a federation inbound handler re-publishes a change it
    #: applied — the outbound bridge never federates those back.
    origin_instance_id: str | None = None


@dataclass(slots=True, frozen=True)
class GalleryAlbumUpdated(DomainEvent):
    """An album's name / description / cover changed."""

    album_id: str
    space_id: str | None
    occurred_at: datetime = field(default_factory=_now)
    #: ``None`` on local origination; the sending household's instance
    #: id when a federation inbound handler re-publishes a change it
    #: applied — the outbound bridge never federates those back.
    origin_instance_id: str | None = None


@dataclass(slots=True, frozen=True)
class GalleryAlbumDeleted(DomainEvent):
    album_id: str
    space_id: str | None
    occurred_at: datetime = field(default_factory=_now)
    #: The deleted album's owner — lets a receiver that does not hold the
    #: album yet check an owner-bound id's delete came from its owner.
    owner_id: str | None = None
    #: ``None`` on local origination; the sending household's instance
    #: id when a federation inbound handler re-publishes a change it
    #: applied — the outbound bridge never federates those back.
    origin_instance_id: str | None = None


@dataclass(slots=True, frozen=True)
class GalleryItemUploaded(DomainEvent):
    item_id: str
    album_id: str
    item_type: str  # 'photo' | 'video'
    uploader: str
    #: Scope for realtime fan-out: the space id for a space album, else
    #: None (household album). Lets the WS layer route to space members
    #: only, never the whole household.
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)
    #: ``None`` on local origination; the sending household's instance
    #: id when a federation inbound handler re-publishes a change it
    #: applied — the outbound bridge never federates those back.
    origin_instance_id: str | None = None


@dataclass(slots=True, frozen=True)
class GalleryItemDeleted(DomainEvent):
    item_id: str
    album_id: str
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)
    #: ``None`` on local origination; the sending household's instance
    #: id when a federation inbound handler re-publishes a change it
    #: applied — the outbound bridge never federates those back.
    origin_instance_id: str | None = None


# ─── Bazaar events (§9, §23.15) ──────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class BazaarBidPlaced(DomainEvent):
    """A bidder placed (or updated) a bid on a listing.

    ``new_end_time`` is the listing's ``end_time`` after any anti-snipe
    extension has been applied — the WS broadcast carries it so the
    countdown UI updates immediately.
    """

    listing_post_id: str
    seller_user_id: str
    bidder_user_id: str
    amount: int
    new_end_time: str
    #: Bid row id — minted by the bidder's instance and federated as-is
    #: so all members converge on a single canonical id per bid (F7).
    bid_id: str = ""
    space_id: str | None = None
    message: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class BazaarOfferAccepted(DomainEvent):
    """A seller accepted an offer (or auction closed with a winner)."""

    listing_post_id: str
    seller_user_id: str
    buyer_user_id: str
    price: int
    #: Bid row id that was accepted (F7) — receivers mirror the same
    #: accepted=True flag onto their local row.
    bid_id: str = ""
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class BazaarListingExpired(DomainEvent):
    """An auction passed its ``end_time`` and was closed."""

    listing_post_id: str
    seller_user_id: str
    final_status: str  # "sold" | "expired"
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class BazaarListingCreated(DomainEvent):
    """A new listing + parent space post were just persisted together."""

    listing_post_id: str
    space_id: str
    seller_user_id: str
    mode: str
    title: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class BazaarListingUpdated(DomainEvent):
    """Seller edited a mutable field (title, description, end_time, …)."""

    listing_post_id: str
    seller_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class BazaarListingCancelled(DomainEvent):
    """Seller pulled the listing before any terminal resolution."""

    listing_post_id: str
    seller_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class BazaarOfferRejected(DomainEvent):
    """Seller explicitly rejected an OFFER-mode bid."""

    listing_post_id: str
    seller_user_id: str
    bidder_user_id: str
    bid_id: str
    reason: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class BazaarBidWithdrawn(DomainEvent):
    """Bidder withdrew a pending OFFER (or non-winning auction bid)."""

    listing_post_id: str
    seller_user_id: str
    bidder_user_id: str
    bid_id: str
    occurred_at: datetime = field(default_factory=_now)


# ─── DM contact request (§12) ────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class DmContactRequested(DomainEvent):
    """A user asked to start a DM with another user."""

    requester_user_id: str
    requester_display_name: str
    recipient_user_id: str
    occurred_at: datetime = field(default_factory=_now)


# ─── Shopping list (§23.120) — local household only, no federation ─────
#
# The shopping list is intentionally a local-household feature:
# short-lived, low-signal items that don't benefit from cross-household
# sync. These events feed the WebSocket fan-out only.


@dataclass(slots=True, frozen=True)
class ShoppingItemAdded(DomainEvent):
    """Someone added a new item to the household shopping list."""

    item_id: str
    text: str
    created_by: str
    created_at: str
    store: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ShoppingItemToggled(DomainEvent):
    """An item's completed state flipped (check / uncheck)."""

    item_id: str
    completed: bool
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ShoppingItemUpdated(DomainEvent):
    """An item's text or store was edited (inline-edit / wizard).

    Carries the full post-edit values so the SPA / paired tabs can
    patch their cached row without re-querying.
    """

    item_id: str
    text: str
    store: str | None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ShoppingItemRemoved(DomainEvent):
    """An item was deleted from the list."""

    item_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ShoppingItemsCleared(DomainEvent):
    """All completed items were bulk-cleared; carries the count removed."""

    count: int
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ShoppingStoresReordered(DomainEvent):
    """The household drag-reordered the shopping-store catalogue.

    ``order`` is the canonical sequence of store names — every other
    SPA tab patches its local store ordering to match.
    """

    order: tuple[str, ...]
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ShoppingStoreRenamed(DomainEvent):
    """A store row was renamed in the catalogue.

    The rename cascades to every item that referenced ``old_name``;
    receivers should patch both their catalogue and their items'
    ``store`` field in one pass.
    """

    old_name: str
    new_name: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ShoppingStoreDeleted(DomainEvent):
    """A store row was removed from the catalogue.

    All items that referenced this store have already had their
    ``store`` field cleared to ``NULL``; the SPA should mirror that
    locally so the row drops into the "No store" bucket without
    waiting for a re-fetch.
    """

    name: str
    occurred_at: datetime = field(default_factory=_now)


# ─── Presence + notification real-time events (§21, §22) ────────────────


@dataclass(slots=True, frozen=True)
class PresenceUpdated(DomainEvent):
    """A household member's presence changed (state / zone / location).

    Carries only the fields the WS layer needs to fan out — coordinates
    are already 4-dp-truncated by :class:`PresenceService` per §25 GPS
    rule before this event is published.

    ``user_id`` and ``gps_accuracy_m`` are carried alongside the
    household-only ``zone_name`` so the per-space outbound service
    (:mod:`space_location_outbound`) can build a GPS-only payload
    without a second DB hit. Subscribers that target the space-bound
    channel must drop ``zone_name`` — HA zones are household-only data
    (§7.3, §23.8.6).
    """

    username: str
    state: str  # "home" | "away" | "zone" | "unavailable"
    zone_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    user_id: str | None = None
    gps_accuracy_m: float | None = None
    updated_at: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class NotificationCreated(DomainEvent):
    """A new notification row exists for ``user_id``.

    The frontend bell uses this to bump its unread badge without
    re-polling. ``link_url`` rides along so the WS frame can render a
    clickable item in the bell panel without a follow-up fetch.
    """

    user_id: str
    notification_id: str
    type: str
    title: str
    link_url: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class NotificationReadChanged(DomainEvent):
    """``user_id``'s unread count changed (read / mark-read / dismiss)."""

    user_id: str
    unread_count: int
    occurred_at: datetime = field(default_factory=_now)


# ─── Pairing events (§11.9) ──────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class PairingIntroRelayReceived(DomainEvent):
    """A paired peer asked us to introduce them to ``target_instance_id``."""

    from_instance: str
    target_instance_id: str
    message: str = ""
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceSyncComplete(DomainEvent):
    """A direct-peer sync session finished streaming (§25.6)."""

    space_id: str
    from_instance: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class DmHistorySyncComplete(DomainEvent):
    """A peer finished streaming missed DM history for one conversation."""

    conversation_id: str
    from_instance: str
    chunks_received: int = 0
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ConnectionReachable(DomainEvent):
    """A previously-unreachable peer is answering again.

    Emitted by :class:`AbstractFederationRepo.mark_reachable` only on the
    transition from unreachable → reachable — no noise on every successful
    send.
    """

    instance_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ConnectionUnreachable(DomainEvent):
    """A previously-reachable peer just failed to answer.

    Published on the reachable → unreachable transition only (mirrors
    :class:`ConnectionReachable`), so the SPA can flip the peer's dot
    red live instead of waiting for a manual refresh.
    """

    instance_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PeerTransportChanged(DomainEvent):
    """A paired peer's federation transport flipped between WebRTC
    DataChannel and HTTPS inbox.

    Published from :class:`socialhome.federation.transport._RtcPeer`
    on every open/close edge. The realtime service re-emits this as
    the ``peer.transport_changed`` WS frame so the Connections page
    can patch the row in place without a refetch.

    ``transport`` is the new effective transport from the perspective
    of outbound delivery — ``"rtc"`` when the DataChannel just opened,
    ``"https"`` when it just closed (the peer remains reachable via
    HTTPS inbox).
    """

    instance_id: str
    transport: Literal["rtc", "https"]
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class LocalHomeLocationUpdated(DomainEvent):
    """The local instance's home GPS coordinates changed.

    Published by :class:`HaAdapter` / :class:`HaosAdapter` on
    startup when the value read from HA Core's ``/api/config``
    differs from the previously-stored value (or on first boot of
    a fresh instance). The federation service subscribes and fans
    out :data:`FederationEventType.LOCAL_HOME_LOCATION_CHANGED` to
    every confirmed peer (gated on
    :data:`FederationCapability.MIN_FOR_HOME_LOCATION_BROADCAST`).
    """

    latitude: float | None
    longitude: float | None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PeerHomeChanged(DomainEvent):
    """A confirmed peer's home GPS coordinates changed.

    Published by the inbound handler for
    :data:`FederationEventType.LOCAL_HOME_LOCATION_CHANGED` after
    the :class:`RemoteInstance` row has been updated. The realtime
    service subscribes and re-emits as the ``peer.home_changed`` WS
    frame so the SPA's federation map can patch the pin in place.

    Both ``latitude`` and ``longitude`` are ``None`` when the peer
    has revoked its location (sent a both-null payload) — the SPA
    should clear the pin for that peer in this case.
    """

    instance_id: str
    latitude: float | None
    longitude: float | None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PairingIntroReceived(DomainEvent):
    """Target side of §11.9 — a peer has introduced us to a new instance."""

    from_instance: str  # the introducer
    via_instance_id: str  # intermediary
    message: str = ""
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PairingAcceptReceived(DomainEvent):
    """Initiator side of §11 — peer accepted our QR invite."""

    from_instance: str
    token: str
    verification_code: str = ""
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PairingConfirmed(DomainEvent):
    """Either side — peer confirmed the SAS; pair is live."""

    instance_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PairingAborted(DomainEvent):
    """Either side — peer aborted an in-progress handshake."""

    instance_id: str
    reason: str = ""
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class AutoPairRequestIncoming(DomainEvent):
    """C side of the transitive auto-pair flow (§11 extension).

    The vouching peer's signature has been verified and the envelope
    is queued in :class:`AutoPairInbox`. Admin clicks approve/decline
    — approve completes the pair instantly (no QR/SAS) because B's
    vouch replaces the out-of-band verification step.
    """

    request_id: str
    from_a_id: str
    via_b_id: str
    from_a_display: str
    via_b_display: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PeerUnpaired(DomainEvent):
    """A pairing was torn down — by the peer (inbound ``UNPAIR``) or by
    our own admin (``DELETE /api/pairing/connections/{id}``)."""

    instance_id: str
    occurred_at: datetime = field(default_factory=_now)


# ─── Remote space-membership events (drive admin UI) ─────────────────────


@dataclass(slots=True, frozen=True)
class RemoteSpaceCreated(DomainEvent):
    """A paired peer created a space; mirrors locally (§13)."""

    space_id: str
    from_instance: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class RemoteSpaceDissolved(DomainEvent):
    space_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class RemoteSpaceMemberBanned(DomainEvent):
    space_id: str
    user_id: str
    banned_by: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class RemoteSpaceInviteReceived(DomainEvent):
    """A peer invited a local user to a remote space (§11.2)."""

    space_id: str
    inviter_user_id: str
    invitee_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class LocalSpaceInviteCreated(DomainEvent):
    """An admin invited a same-household user to a space — they have
    to accept before they're seated. Pascal asked for parity with the
    cross-household flow ("they should receive a join request like
    all others"); this is the bus signal :class:`RealtimeService`
    listens on to push a ``space.local_invite_received`` WS frame to
    the invitee's session so the inbox banner appears immediately."""

    space_id: str
    invitation_id: str
    invited_user_id: str
    invited_by: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class RemoteSpaceJoinRequestReceived(DomainEvent):
    space_id: str
    requester_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class RemoteJoinRequestApproved(DomainEvent):
    """§D2 — applicant side: our remote join-request was approved. The
    applicant-side federation handler publishes this so the space
    service can auto-consume the attached invite token + seat the user.
    """

    request_id: str
    space_id: str
    invite_token: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class RemoteJoinRequestDenied(DomainEvent):
    request_id: str
    space_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class RemoteSpaceInviteAccepted(DomainEvent):
    """Local record that a remote user accepted our private-space invite
    (§D1b). Drives notifications + UI refresh on the host."""

    space_id: str
    instance_id: str
    invitee_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PeerProtoVersionRaised(DomainEvent):
    """A paired household advertised a higher ``proto_version`` than the
    one we had on file (``INSTANCE_CAPABILITIES_UPDATED``). Lets senders
    catch that household up on what it could not receive before — e.g. the
    host's roster snapshot (v_32)."""

    instance_id: str
    old_version: int
    new_version: int
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceRemoteSeatLive(DomainEvent):
    """A remote member's seat just became live in this household's roster
    mirror (``space_remote_members``) — seated by an accept / redeem, or
    learned from authority-signed roster gossip or snapshot.

    Releases the space writes held for that user or that household while
    their seat had not reached us (:class:`~socialhome.federation
    .pending_seat_buffer.PendingSeatBuffer`)."""

    space_id: str
    instance_id: str
    user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceAdminAuthorityRevoked(DomainEvent):
    """On the household that HOSTS a space: someone's ability to sign for
    the space as an admin may just have ended (v_44).

    ``instance_id`` names a household whose admin seat was just demoted,
    removed, banned or tombstoned by roster gossip — the rotation service
    re-checks whether it still holds any admin seat and, if not (and
    delegation is on), rotates the space authority key. ``None`` means
    ``delegated_admin_authority`` was just turned OFF: every seed ever
    shared is now unauthorized, so the key rotates unconditionally."""

    space_id: str
    instance_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceAdminSeedsRetiredAfterRestore(DomainEvent):
    """The owner household was restored from a backup or Recovery Kit and a
    space it hosts rotated its authority key WITHOUT re-sharing the new
    seed (v_44). The restored roster may name admins that were revoked since
    the backup, so delegated authority comes back only when the owner
    re-confirms its admins (the next promote or delegation toggle shares)."""

    space_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceAuthorityEchoDue(DomainEvent):
    """A member household just learned something its space owner must hear
    now rather than on the next periodic sync (v_46): the owner's
    post-restore bundle replaced an epoch BELOW the one this household held,
    so the owner forgot a rotation. The sync scheduler answers with a
    ``SPACE_SYNC_BEGIN`` to the owner carrying the authority epoch echo."""

    space_id: str
    owner_instance_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class RemoteSpaceInviteDeclined(DomainEvent):
    """Mirror of :class:`RemoteSpaceInviteAccepted` for the decline path."""

    space_id: str
    instance_id: str
    invitee_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class RemoteSpaceMemberRemoved(DomainEvent):
    """The host removed us from a remote private space (§D1b)."""

    space_id: str
    instance_id: str
    user_id: str
    occurred_at: datetime = field(default_factory=_now)


# ─── DM events (drive push notifications + WS fan-out) ──────────────────


@dataclass(slots=True, frozen=True)
class DmMessageCreated(DomainEvent):
    """A new DM landed in a conversation.

    ``recipient_user_ids`` lists every participant except the sender —
    the push service iterates over them, applying the §25.3 redaction
    rule (title only, body omitted).

    ``content`` is the plaintext body — local-only (§25.3 only applies
    to push payloads and federation envelopes; the in-process event bus
    is trusted). The search service uses it for FTS5 indexing; the
    push service ignores it.
    """

    conversation_id: str
    message_id: str
    sender_user_id: str
    sender_display_name: str
    recipient_user_ids: tuple[str, ...]
    content: str = ""
    message_type: str = "text"
    media_url: str | None = None
    #: v_3 media metadata. Populated for ``image`` / ``video`` /
    #: ``file`` messages; ``None`` everywhere else. The realtime
    #: layer surfaces these on the ``dm.message`` WS frame so the
    #: optimistic-bubble reconcile keeps the filename + size on
    #: the receiver's bubble (the bubble's render branches on
    #: ``mime_type`` / ``file_name``).
    file_name: str | None = None
    mime_type: str | None = None
    file_size_bytes: int | None = None
    reply_to_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)
    #: @-mentions in ``content`` resolved against the conversation's seats
    #: on this household
    #: (:class:`~socialhome.services.dm_mentions.DmMentionResolver`).
    #: Drives the ``dm_mention`` bell and the group ``mentions`` level.
    #: Never on the wire; never contains ``@here``.
    mentions: tuple["Mention", ...] = ()


@dataclass(slots=True, frozen=True)
class DmMessageUpdated(DomainEvent):
    """A DM's ``content`` was updated in place.

    Fires for voice-note transcripts and for a sender's text edit
    (:meth:`DmService.edit_message` locally, a re-fanned ``DM_MESSAGE``
    carrying ``edited_at`` inbound). The transcript paths:

    1. **Sender-side STT patch.** Right after the audio bubble is
       persisted with empty ``content``, the dm service runs the
       sender's ``adapter.stt`` on the blob; when the transcript is
       ready it patches the row and emits this event. The recipient's
       open thread tab swaps the "Transcribing…" placeholder for the
       transcript line.
    2. **Receiver-side fallback.** If a remote sender shipped audio
       without a transcript (their STT failed or wasn't configured),
       the local ``AudioTranscriptScheduler`` runs the recipient's own
       STT and patches the row the same way.

    ``recipient_user_ids`` covers every other participant in the
    conversation — the realtime layer fans the
    ``dm.message_updated`` WS frame out to all of them.
    """

    conversation_id: str
    message_id: str
    sender_user_id: str
    recipient_user_ids: tuple[str, ...]
    content: str
    edited_at: datetime
    occurred_at: datetime = field(default_factory=_now)
    #: A sender's edit: the @-mentions it newly adds (see
    #: :attr:`PostEdited.new_mentions`). Empty for transcript patches.
    new_mentions: tuple["Mention", ...] = ()
    #: The sender's name for a mention bell ("{name} mentioned you …").
    sender_display_name: str | None = None


@dataclass(slots=True, frozen=True)
class DmMessageReactionChanged(DomainEvent):
    """A reaction was added or removed on a DM message.

    Fans out a ``dm.message_reaction`` WS frame to every member of
    the conversation so open threads update their reaction strip
    in lockstep. The federation outbound path is handled separately
    in :meth:`DmService.add_reaction` / :meth:`DmService.remove_reaction`
    via ``DM_MESSAGE_REACTION`` envelopes.
    """

    conversation_id: str
    message_id: str
    user_id: str
    emoji: str
    action: str  # "add" | "remove"
    recipient_user_ids: tuple[str, ...]
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class DmConversationCreated(DomainEvent):
    """A new DM / group DM was created.

    Drives WS fan-out so every participant's open inbox tab refreshes
    immediately — without it, a brand-new DM stays invisible to the
    recipient until they reload the page.

    ``member_user_ids`` covers everyone, *including* the creator: their
    other open sessions need the frame too (mobile + desktop on the
    same account is a routine pattern in the household OS).
    """

    conversation_id: str
    conversation_type: str  # "dm" | "group_dm"
    name: str | None
    creator_user_id: str
    member_user_ids: tuple[str, ...]
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class DmGroupRosterChanged(DomainEvent):
    """A group conversation's member list or name changed here (v_37).

    Emitted after a membership snapshot was applied — locally by the
    authority household, or from its ``DM_GROUP_ROSTER``. Drives a
    ``dm.group.updated`` WS frame so open threads refresh their roster.
    ``notify_user_ids`` holds the local members before *and* after the
    change, so a removed member's open tab learns it is out.
    """

    conversation_id: str
    name: str | None
    notify_user_ids: tuple[str, ...]
    occurred_at: datetime = field(default_factory=_now)


# ─── Page events (drive FTS5 indexing + conflict bookkeeping) ────────────


@dataclass(slots=True, frozen=True)
class PageCreated(DomainEvent):
    page_id: str
    space_id: str | None
    title: str
    content: str
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    #: v_48: the host's canonical-version fields (``seq``, ``version_hash``,
    #: ``conflict``, ``sequenced`` …) — set only by the space's host; the
    #: outbound broadcasts them to v_48 members.
    canonical: dict | None = None
    #: v_48: a member's local draft, sent to the host as a proposal by the
    #: forwarder — never broadcast.
    proposal: bool = False
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PageUpdated(DomainEvent):
    page_id: str
    space_id: str | None
    title: str
    content: str
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    #: v_48: the host's canonical-version fields (``seq``, ``version_hash``,
    #: ``conflict``, ``sequenced`` …) — set only by the space's host; the
    #: outbound broadcasts them to v_48 members.
    canonical: dict | None = None
    #: v_48: a member's local draft, sent to the host as a proposal by the
    #: forwarder — never broadcast.
    proposal: bool = False
    #: v_48: a last-write-wins update from a household that does not (yet)
    #: see its host as v_48 still names the version it was made from
    #: (``base_seq`` / ``base_hash``), so a v_48 host merges it instead of
    #: letting a delayed copy overwrite newer edits.
    base: dict | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PageProposalSettled(DomainEvent):
    """The host answered one of this household's page proposals (v_48):
    ``applied`` (sequenced — merged, fast-forwarded or kept as a conflict
    side) or ``refused`` with a ``reason`` (``access`` / ``archived`` /
    ``gone`` / ``rate_limited`` / ``bad_base``). The forwarder sends the
    next draft; the WS ``page.sequenced`` frame updates an open viewer."""

    page_id: str
    space_id: str
    proposal_hash: str
    outcome: str
    reason: str | None = None
    #: The host's ``seq`` that answered it (0: unknown).
    seq: int = 0
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PageDeleted(DomainEvent):
    page_id: str
    space_id: str | None = None
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PageEditLockAcquired(DomainEvent):
    """Fired when an editor takes an edit lock (§23.72).

    ``RealtimeService`` broadcasts this to the household (or space
    members, if the page is space-scoped) as a ``page.editing`` WS
    event so every open Pages viewer can disable its "Edit" button.
    """

    page_id: str
    space_id: str | None
    locked_by: str
    lock_expires_at: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PageEditLockReleased(DomainEvent):
    """Fired when the lock is released or expires (§23.72)."""

    page_id: str
    space_id: str | None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PageConflictEmitted(DomainEvent):
    """Fired when a save collides with a concurrent edit (§4.4.4.1).

    Broadcast as a ``page.conflict`` WS event so the editor that's
    about to save sees the opposing body in real time rather than on
    the next PATCH round-trip.
    """

    page_id: str
    space_id: str | None
    theirs: str
    theirs_by: str
    #: Recorded from another household's concurrent edit (v_48) — the page
    #: now holds an open conflict; an open viewer refetches it.
    federated: bool = False
    occurred_at: datetime = field(default_factory=_now)


# ─── Sticky notes (§19) ──────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class StickyCreated(DomainEvent):
    """A new sticky note was added (household or space-scoped).

    :class:`RealtimeService` broadcasts this as a ``sticky.created`` WS
    event — scoped to ``space_id`` members when set, household-wide
    otherwise.
    """

    sticky_id: str
    space_id: str | None
    author: str
    content: str
    color: str
    position_x: float
    position_y: float
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class StickyUpdated(DomainEvent):
    """Content / position / color change on a sticky."""

    sticky_id: str
    space_id: str | None
    content: str
    color: str
    position_x: float
    position_y: float
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class StickyDeleted(DomainEvent):
    """A sticky was removed. Only ``sticky_id`` + ``space_id`` travel —
    peers clear the row locally."""

    sticky_id: str
    space_id: str | None
    #: The user who made this write (v_42) — federated inside the sealed
    #: payload so receivers judge it against the space's access level.
    #: Empty for a write applied from a peer, or one nobody made.
    actor_user_id: str = ""
    occurred_at: datetime = field(default_factory=_now)


# ─── Space membership (§23.48 / §23.52) ──────────────────────────────────


@dataclass(slots=True, frozen=True)
class SpaceMemberJoined(DomainEvent):
    """A user is now a member (via invite accept, join approval, or add)."""

    space_id: str
    user_id: str
    role: str = "member"
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceMemberLeft(DomainEvent):
    """A user left the space or was removed (not banned)."""

    space_id: str
    user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceContentKeyImported(DomainEvent):
    """A new space content key landed in ``space_keys`` (#122).

    Fires from :meth:`SpaceContentEncryption.import_key` so the
    :class:`PendingDecryptsCache` can drain any sync chunks /
    SEALED-sender envelopes that arrived ahead of their key. The
    classic case is a §25.6 sync chunk that landed while the §D1b
    accept was still in flight — the chunk's epoch isn't known yet,
    decrypt fails, the chunk gets stashed; this event fires when
    the matching ``apply_space_content_key_from_metadata`` runs and
    the stashed chunk is replayed.
    """

    space_id: str
    epoch: int
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceJoinRequested(DomainEvent):
    """A user submitted a request to join a ``join_mode='request'`` space."""

    space_id: str
    user_id: str
    request_id: str
    message: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceJoinApproved(DomainEvent):
    """An admin approved a pending join request."""

    space_id: str
    user_id: str
    request_id: str
    approved_by: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceJoinDenied(DomainEvent):
    space_id: str
    user_id: str
    request_id: str
    denied_by: str
    occurred_at: datetime = field(default_factory=_now)


# ─── Child Protection (§CP / §23.107) ────────────────────────────────────


@dataclass(slots=True, frozen=True)
class CpProtectionEnabled(DomainEvent):
    minor_username: str
    declared_age: int
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class CpProtectionDisabled(DomainEvent):
    minor_username: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class CpGuardianAdded(DomainEvent):
    minor_user_id: str
    guardian_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class CpGuardianRemoved(DomainEvent):
    minor_user_id: str
    guardian_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class CpBlockAdded(DomainEvent):
    minor_user_id: str
    blocked_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class CpBlockRemoved(DomainEvent):
    minor_user_id: str
    blocked_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class CpSpaceAgeGateChanged(DomainEvent):
    space_id: str
    min_age: int
    occurred_at: datetime = field(default_factory=_now)


# ─── Highlights (§Highlights) ──────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class HighlightFrameAdded(DomainEvent):
    """A frame was created or appended on the author's instance.

    Carries enough for federation outbound to fan out the encrypted
    payload (audience, frame body) and for :class:`RealtimeService` to
    push a ``highlight.frame_added`` WS event to local viewers in the
    audience.
    """

    highlight_id: str
    frame_id: str
    author_user_id: str
    highlight_date: str
    sequence: int
    is_first_frame: bool
    audience_kind: str  # 'all_paired' | 'households' | 'users'
    audience: tuple[str, ...]  # peer instance_ids or user_ids
    frame_type: str  # 'image' | 'video'
    media_url: str
    caption_text: str | None
    caption_emoji: str | None
    duration_ms: int | None
    expires_at: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class HighlightFrameRemoved(DomainEvent):
    highlight_id: str
    frame_id: str
    author_user_id: str
    audience_kind: str
    audience: tuple[str, ...]
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class HighlightRemoved(DomainEvent):
    highlight_id: str
    author_user_id: str
    audience_kind: str
    audience: tuple[str, ...]
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class HighlightFrameViewed(DomainEvent):
    """A viewer marked a frame as seen — federates back to the author."""

    highlight_id: str
    frame_id: str
    viewer_user_id: str
    author_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class HighlightFrameReactionChanged(DomainEvent):
    """Reaction set, changed, or cleared.

    ``emoji is None`` ⇒ cleared. Federates back to the author so they
    see the reaction-counter update.
    """

    highlight_id: str
    frame_id: str
    reactor_user_id: str
    author_user_id: str
    emoji: str | None
    occurred_at: datetime = field(default_factory=_now)


# ─── Household feature toggles (§18 / §23.13) ────────────────────────────


@dataclass(slots=True, frozen=True)
class HouseholdConfigChanged(DomainEvent):
    """Emitted when an admin edits household toggles / name.

    ``changed`` is a sparse ``{key: new_value}`` dict — only fields that
    actually changed are included. Subscribers fan this out to every WS
    session so the client can refresh its nav + post-type allowlist
    without a page reload.
    """

    changed: dict
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class UserPreferencesChanged(DomainEvent):
    """A user's row in ``preferences`` was mutated.

    ``changed`` is a sparse ``{key: new_value}`` dict — only fields that
    actually changed are included. Fired only to that user's WS session(s)
    so they can refresh surfaces they have personalised (highlights tab,
    momentum strip, bazaar pill).
    """

    user_id: str
    changed: dict
    occurred_at: datetime = field(default_factory=_now)


# ─── Personal user blocks (§Privacy) ──────────────────────────────────────


@dataclass(slots=True, frozen=True)
class UserBlocked(DomainEvent):
    """A local user added another user to their personal block list.

    Distinct from the parent-driven :class:`CpBlockAdded` (CP/§child-protection):
    this is the adult-to-adult voluntary mute that hides highlights, posts,
    DMs, presence and notifications surfacing the blocked user.
    """

    blocker_user_id: str
    blocked_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class UserUnblocked(DomainEvent):
    """A local user removed another user from their personal block list."""

    blocker_user_id: str
    blocked_user_id: str
    occurred_at: datetime = field(default_factory=_now)


# ─── Apps (§Social Home Apps) ────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class AppInstalled(DomainEvent):
    """An app bundle was downloaded, verified, unpacked, and recorded.

    Published by :class:`AppService.install` after the repo row is committed
    so subscribers can react to a new app being available.
    """

    app_id: str
    name: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class AppUninstalled(DomainEvent):
    """An installed app was removed from disk and the registry.

    Published by :class:`AppService.uninstall` after the repo row and bundle
    directory are gone.
    """

    app_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class AppUpdated(DomainEvent):
    """An installed app was updated to a newer version from the catalog.

    Published by :class:`AppService.update_app` after the new bundle is
    unpacked, the repo row is updated, and the old bundle directory is removed.
    """

    app_id: str
    name: str
    old_version: str
    new_version: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class AppChallengeReceived(DomainEvent):
    """A Social Home App session invite ("challenge") arrived for a local user.

    Published by :class:`AppFederationService` when an ``APP_SESSION`` *open*
    resolves to a specific local recipient (local loopback or remote-routed).
    Consumed by the notification service to raise a bell row + push.
    ``from_display`` is a human label only — never a stable cross-household id.
    """

    app_id: str
    session_id: str
    to_user_id: str
    from_display: str


# ─── Momentum (§Momentum) ─────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class MomentCreated(DomainEvent):
    """A moment was created — locally posted or federated in.

    Carries enough for federation outbound to fan to peers and for
    :class:`RealtimeService` to broadcast the ``moment.created`` WS
    frame. Replies set ``parent_moment_id`` to the parent moment id;
    otherwise it's ``None``.
    """

    moment_id: str
    author_user_id: str
    content: str
    media_url: str | None
    media_type: str | None
    duration_ms: int | None
    parent_moment_id: str | None
    #: The author of the parent moment when this is a reply, otherwise
    #: ``None``. Carried on the event so :class:`NotificationService`
    #: can ping the parent author without a repo lookup.
    parent_author_user_id: str | None
    origin_instance_id: str
    expires_at: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class MomentDeleted(DomainEvent):
    """A moment was deleted (by author or admin)."""

    moment_id: str
    author_user_id: str
    origin_instance_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class MomentReactionChanged(DomainEvent):
    """Reaction set, changed, or cleared.

    ``emoji is None`` ⇒ cleared. The reactor's home instance federates
    this back to the author's instance for the live counter update.
    """

    moment_id: str
    reactor_user_id: str
    author_user_id: str
    emoji: str | None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class UserFollowed(DomainEvent):
    follower_user_id: str
    followed_user_id: str
    occurred_at: datetime = field(default_factory=_now)


# ─── Media transcode (async video transcode) ─────────────────────────────


@dataclass(slots=True, frozen=True)
class MediaTranscodeReady(DomainEvent):
    """A background video transcode finished — the ``.webm`` + poster
    are now on disk under the media root.

    Published by :class:`MediaTranscodeService.flush_once` on success
    only (a failed-frame event is a documented follow-up). Subscribers
    (realtime WS fan-out) can surface a "your video is ready" signal so
    the SPA swaps its "processing" placeholder for the playable clip.
    ``owner_user_id`` is ``None`` when the source row carried no owner.
    """

    output_filename: str
    thumbnail_filename: str
    owner_user_id: str | None


@dataclass(slots=True, frozen=True)
class MediaTranscodeFailed(DomainEvent):
    """A background video transcode permanently failed — the worker hit
    :data:`MediaTranscodeService.MAX_ATTEMPTS` and flipped the row to
    ``status='failed'``.

    Published by :class:`MediaTranscodeService._fail` on the terminal /
    attempt-cap branch only (a reschedule never emits it). The realtime
    WS fan-out re-emits it as a ``media.failed`` frame to the uploader so
    the SPA flips its "Processing…" placeholder to the failed state
    immediately, instead of surfacing ``media_status:'failed'`` only on
    the next list fetch. ``owner_user_id`` is ``None`` when the source
    row carried no owner.
    """

    output_filename: str
    owner_user_id: str | None


@dataclass(slots=True, frozen=True)
class UserUnfollowed(DomainEvent):
    follower_user_id: str
    followed_user_id: str
    occurred_at: datetime = field(default_factory=_now)
