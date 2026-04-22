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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .calendar import CalendarEvent
    from .mention import Mention
    from .post import Comment, Post
    from .space import SpaceModerationItem
    from .task import Task
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


@dataclass(slots=True, frozen=True)
class PostDeleted(DomainEvent):
    """Soft-delete — content cleared, node retained."""

    post_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PostReactionChanged(DomainEvent):
    post: "Post"
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class CommentAdded(DomainEvent):
    post_id: str
    comment: "Comment"
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class CommentUpdated(DomainEvent):
    """Comment body edited. Broadcast as ``comment.updated`` WS frame."""

    post_id: str
    comment: "Comment"
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class CommentDeleted(DomainEvent):
    """Comment removed. Broadcast as ``comment.deleted`` WS frame."""

    post_id: str
    comment_id: str
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


# ─── Spaces ───────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class SpacePostCreated(DomainEvent):
    post: "Post"
    space_id: str
    mentions: tuple["Mention", ...] = ()
    approved_by: str | None = None
    occurred_at: datetime = field(default_factory=_now)


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
class ReportFiled(DomainEvent):
    """A user filed a report on a post / comment / user / space."""

    report_id: str
    target_type: str  # 'post' | 'comment' | 'user' | 'space'
    target_id: str
    category: str
    reporter_user_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ReportResolved(DomainEvent):
    report_id: str
    resolved_by: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceConfigChanged(DomainEvent):
    space_id: str
    event_type: str
    payload: dict
    sequence: int
    occurred_at: datetime = field(default_factory=_now)


# ─── Tasks ────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class TaskAssigned(DomainEvent):
    task: "Task"
    assigned_to: str  # user_id
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskCompleted(DomainEvent):
    task: "Task"
    completed_by: str  # user_id
    spawned_next: "Task | None" = None
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
    (household scope — space scope fan-out is tighter)."""

    task: "Task"
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskUpdated(DomainEvent):
    """Title / description / due / status / position / assignees
    change. Broadcast as ``task.updated``."""

    task: "Task"
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskDeleted(DomainEvent):
    """Task row removed. Broadcast as ``task.deleted``."""

    task_id: str
    list_id: str
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskListCreated(DomainEvent):
    """New task list. Broadcast as ``task_list.created`` so sidebars
    refresh live when another tab adds a list."""

    list_id: str
    name: str
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskListUpdated(DomainEvent):
    """Task-list rename / colour / emoji."""

    list_id: str
    name: str
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class TaskListDeleted(DomainEvent):
    """Task-list removed (cascades to tasks via DB FK)."""

    list_id: str
    space_id: str | None = None
    occurred_at: datetime = field(default_factory=_now)


# ─── Schedule polls (§9) ─────────────────────────────────────────────────


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
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class CalendarEventDeleted(DomainEvent):
    event_id: str
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


@dataclass(slots=True, frozen=True)
class UserProfileUpdated(DomainEvent):
    """Display-name / bio / picture edit on a local user (§23 profile).

    ``picture_hash`` is the new cache-busting digest (None when the
    picture was cleared). ``picture_webp`` carries the bytes so the
    federation-outbound layer can fan them to paired peers; WS
    broadcasts drop it and send only the hash so the frame stays small.
    """

    user_id: str
    username: str
    display_name: str
    bio: str | None
    picture_hash: str | None
    picture_webp: bytes | None = None
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


@dataclass(slots=True, frozen=True)
class GalleryAlbumDeleted(DomainEvent):
    album_id: str
    space_id: str | None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class GalleryItemUploaded(DomainEvent):
    item_id: str
    album_id: str
    item_type: str  # 'photo' | 'video'
    uploader: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class GalleryItemDeleted(DomainEvent):
    item_id: str
    album_id: str
    occurred_at: datetime = field(default_factory=_now)


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
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class BazaarOfferAccepted(DomainEvent):
    """A seller accepted an offer (or auction closed with a winner)."""

    listing_post_id: str
    seller_user_id: str
    buyer_user_id: str
    price: int
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
    """A new listing + parent feed post were just persisted together."""

    listing_post_id: str
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
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class ShoppingItemToggled(DomainEvent):
    """An item's completed state flipped (check / uncheck)."""

    item_id: str
    completed: bool
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


# ─── Presence + notification real-time events (§21, §22) ────────────────


@dataclass(slots=True, frozen=True)
class PresenceUpdated(DomainEvent):
    """A household member's presence changed (state / zone / location).

    Carries only the fields the WS layer needs to fan out — coordinates
    are already 4-dp-truncated by :class:`PresenceService` per §25 GPS
    rule before this event is published.
    """

    username: str
    state: str  # "home" | "away" | "zone" | "unavailable"
    zone_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class NotificationCreated(DomainEvent):
    """A new notification row exists for ``user_id``.

    The frontend bell uses this to bump its unread badge without
    re-polling.
    """

    user_id: str
    notification_id: str
    type: str
    title: str
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
    """A confirmed peer tore down the pairing."""

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
    occurred_at: datetime = field(default_factory=_now)


# ─── Page events (drive FTS5 indexing + conflict bookkeeping) ────────────


@dataclass(slots=True, frozen=True)
class PageCreated(DomainEvent):
    page_id: str
    space_id: str | None
    title: str
    content: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PageUpdated(DomainEvent):
    page_id: str
    space_id: str | None
    title: str
    content: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class PageDeleted(DomainEvent):
    page_id: str
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
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class StickyDeleted(DomainEvent):
    """A sticky was removed. Only ``sticky_id`` + ``space_id`` travel —
    peers clear the row locally."""

    sticky_id: str
    space_id: str | None
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
    target_audience: str
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
