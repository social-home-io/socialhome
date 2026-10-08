"""Notification service — event-driven notification dispatch (§17.2).

Subscribes to :class:`DomainEvent` types via the :class:`EventBus` and
creates :class:`Notification` entries in the notification repo for the
relevant users. The route layer reads these via ``GET /api/notifications``
and the bell-badge via ``GET /api/notifications/unread-count``.

Push delivery (HA mobile notifications, ntfy, etc.) is out of scope for
this first service slice — it will ride on top of this same bus wiring
when the platform adapter's push API is ready. The notification row
itself is the persistence layer; push is fire-and-forget on top.

**Which events produce notifications:**

| Event                 | Who is notified                       | Title pattern                                  |
|----------------------|---------------------------------------|-------------------------------------------------|
| PostCreated          | All active household members          | "{author} posted"                              |
| CommentAdded         | Household users (feed) / space members (space comment, level + mentions as below) | "{commenter} commented on a post" / "{author} commented in {space}" |
| TaskAssigned         | Each assignee (not the assigner)      | "You were assigned: {task title}"              |
| TaskDeadlineDue      | All assignees                         | "Task due today: {task title}"                 |
| SpacePostCreated     | Space members with notifications on   | "{author} posted in {space}"                   |
| … mentioning a member | That member (level all / mentions)   | "{author} mentioned you in {space}"            |
| PostEdited / CommentUpdated (space) | Only members the edit newly mentions | "{author} mentioned you in {space}" |
| DmMessageCreated     | Other members (group level all / mentions, not muted) | "{sender} messaged you" / "{sender} mentioned you in {chat}" |
| DmMessageUpdated     | Only members the edit newly mentions (groups) | "{sender} mentioned you in {chat}"   |
| SpaceModerationQueued| Space admins                          | "New content pending review in {space}"        |
| SpaceModeration{Approved,Rejected,Expired} | The submitter     | "Your submission in {space} was approved"…      |

Body is intentionally omitted for privacy-sensitive events (DMs,
location, UGC content) per §25.3.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from ..domain.conversation import ConversationType, SystemChatScope, mute_active
from ..domain.events import (
    AppChallengeReceived,
    BazaarBidPlaced,
    BazaarListingExpired,
    BazaarOfferAccepted,
    BazaarOfferRejected,
    CalendarEventCreated,
    CalendarEventDeleted,
    CalendarEventUpdated,
    CommentAdded,
    CommentUpdated,
    EventReminderDue,
    DmContactRequested,
    DmMessageCreated,
    DmMessageDeleted,
    DmMessageUpdated,
    MomentCreated,
    MomentReactionChanged,
    NotificationCreated,
    PostCreated,
    PostEdited,
    RemoteSpaceDissolved,
    ReportFiled,
    SpaceAdminSeedsRetiredAfterRestore,
    RemoteSpaceInviteAccepted,
    RemoteSpaceInviteDeclined,
    SpaceJoinApproved,
    SpaceJoinDenied,
    SpaceJoinRequested,
    SpaceLocationFeatureEnabled,
    SpaceMemberJoined,
    SpaceModerationApproved,
    SpaceModerationExpired,
    SpaceModerationQueued,
    SpaceModerationRejected,
    SpaceRemoteSeatLive,
    SpacePostCreated,
    SpacePostModerated,
    TaskAssigned,
    TaskCompleted,
    TaskDeadlineDue,
    UserFollowed,
)
from ..domain.federation_capabilities import FederationCapability
from ..domain.mention import Mention, MentionType
from ..repositories._spec import Spec
from ..domain.space import (
    CONTENT_AUTHORITY_ROLES,
    SETTINGS_AUTHORITY_ROLES,
    SpaceFeatureAccess,
    SpaceRole,
)
from ..i18n import Catalog
from ..infrastructure.event_bus import EventBus
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.notification_repo import (
    AbstractNotificationRepo,
    new_notification,
)
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .protection_gate import ProtectionGateMixin
from .push_service import PushPayload


log = logging.getLogger(__name__)

#: One @here per author per space per this many seconds pages anyone.
HERE_COOLDOWN_SECONDS = 600
#: Cap on remembered (space, author) @here timestamps before pruning.
_HERE_TRACK_MAX = 10_000


@dataclass(slots=True, frozen=True)
class _SeatPrefs:
    """Service-local: one conversation's per-member bell settings."""

    muted: frozenset[str] = frozenset()
    mentions_only: frozenset[str] = frozenset()
    group: bool = False
    name: str | None = None
    #: Set for a system chat (household / space chat).
    system_scope: SystemChatScope | None = None
    #: A space chat's space and its name (the bell reads "… in {space}").
    space_id: str | None = None
    space_name: str | None = None


#: Where a household-chat bell / push leads: the feed's Chat tab.
HOUSEHOLD_CHAT_LINK = "/?tab=chat"


def _conversation_link(
    conversation_id: str,
    scope: SystemChatScope | None,
    space_id: str | None = None,
) -> str:
    """The SPA path a conversation's bell / push opens: the feed's Chat
    tab for the household chat, the space's Feed | Chat switch for a space
    chat, the DM thread otherwise."""
    if scope is SystemChatScope.HOUSEHOLD:
        return HOUSEHOLD_CHAT_LINK
    if scope is SystemChatScope.SPACE and space_id:
        return f"/spaces/{space_id}?view=chat"
    return f"/dms/{conversation_id}"


#: English names of the access-levelled features (catalog fallback).
_FEATURE_FALLBACK: dict[str, str] = {
    "pages": "Pages",
    "tasks": "Tasks",
    "stickies": "Sticky notes",
    "calendar": "Calendar events",
}


class NotificationService(ProtectionGateMixin):
    """Creates notification-centre entries in response to domain events.

    Call :meth:`wire` once during app startup to bind the handlers to the
    event bus. The handler methods are public so integration tests can
    invoke them directly without going through the bus.

    When constructed with ``i18n`` (a :class:`~socialhome.i18n.Catalog`),
    notification titles are translated to each recipient's locale; when
    ``i18n`` is ``None`` the legacy English-only titles are used so
    existing tests stay green.
    """

    __slots__ = (
        "_child_protection",
        "_notifs",
        "_users",
        "_spaces",
        "_bus",
        "_i18n",
        "_push",
        "_adapter",
        "_calendar_repo",
        "_personal_calendar_repo",
        "_ws_manager",
        "_clock",
        "_here_last",
        "_convos",
        "_remote_members",
        "_instances",
    )

    def __init__(
        self,
        notification_repo: AbstractNotificationRepo,
        user_repo: AbstractUserRepo,
        space_repo: AbstractSpaceRepo,
        bus: EventBus,
        *,
        i18n: Catalog | None = None,
        conversation_repo: AbstractConversationRepo | None = None,
    ) -> None:
        self._notifs = notification_repo
        #: Per-member conversation mutes (``on_dm_message_created``).
        #: Without it no conversation reads as muted.
        self._convos = conversation_repo
        self._users = user_repo
        #: §CP.F2 — nobody hears from someone a guardian block separates
        #: them from (mentions, @here, DMs, reactions).
        self._child_protection = None
        self._spaces = space_repo
        self._bus = bus
        self._i18n = i18n
        #: Monotonic clock for the @here cooldown (tests swap it).
        self._clock: Callable[[], float] = time.monotonic
        self._here_last: dict[tuple[str, str], float] = {}
        self._push = None  # attach_push_service(PushService)
        self._adapter = None  # attach_platform_adapter(PlatformAdapter)
        self._calendar_repo = None  # attach_calendar_repo(...) Phase D
        # Personal calendars (per-user). Used by ``on_calendar_event_created``
        # to dispatch the notification audience: a personal-calendar event
        # only notifies the calendar's owner (and not the creator
        # themselves). Space calendar events fall through to
        # ``_spaces.list_members`` instead. Optional — without it the
        # handler degrades to "no personal notifications", which is safer
        # than the old "fan to every household member" behavior.
        self._personal_calendar_repo = None
        # WebSocketManager — optional. Used by :meth:`on_dm_message_created`
        # to skip the bell row + push when the recipient has the DM
        # thread open in any of their tabs. Without it the service
        # degrades to the pre-fix behaviour (always notify).
        self._ws_manager = None
        # Remote seats per household — optional; without it the
        # "Reviewed isn't available here any more" warning is skipped.
        self._remote_members = None
        # Paired households' advertised ``proto_version`` — optional; it
        # limits that warning to households below v_43 (federated
        # moderation). Without it every new household warns.
        self._instances = None

    def attach_remote_member_repo(self, remote_member_repo) -> None:
        """Wire the remote-seat mirror (``on_remote_seat_live``)."""
        self._remote_members = remote_member_repo

    def attach_federation_repo(self, federation_repo) -> None:
        """Wire the paired households' versions (``on_remote_seat_live``)."""
        self._instances = federation_repo

    def attach_push_service(self, push_service) -> None:
        """Attach a :class:`PushService` to fan out Web Push alongside the
        in-app notification rows. Safe to call once; subsequent calls
        replace the previous reference.
        """
        self._push = push_service

    def attach_calendar_repo(self, calendar_repo) -> None:
        """Wire :class:`AbstractSpaceCalendarRepo` so update-push handlers
        can resolve the affected RSVP cohort. Optional — without it the
        update-push handler is a no-op (Phase D)."""
        self._calendar_repo = calendar_repo

    def attach_personal_calendar_repo(self, calendar_repo) -> None:
        """Wire :class:`AbstractCalendarRepo` (the personal-calendar repo)
        so ``on_calendar_event_created`` can resolve the owning user of
        the event's calendar. Without it personal-calendar events emit
        no notifications (the broader space-member branch still works
        via ``_spaces``).
        """
        self._personal_calendar_repo = calendar_repo

    def attach_platform_adapter(self, adapter) -> None:
        """Attach the :class:`PlatformAdapter` so push notifications also
        reach HA mobile apps (`notify.mobile_app_<user>`) or the
        standalone ``notify_endpoint``. Optional — if both this adapter
        and the Web Push ``PushService`` are wired, we fan out to both
        so the user gets the notification on every registered surface.
        """
        self._adapter = adapter

    def attach_ws_manager(self, ws_manager) -> None:
        """Attach the :class:`WebSocketManager` so DM notifications can
        skip recipients who have the thread open right now. See the
        docstring on :meth:`on_dm_message_created` for the rationale.
        """
        self._ws_manager = ws_manager

    async def _save_notif(
        self, note, *, dedupe_by_link: bool = False, push: bool = True
    ):
        """Persist + publish ``NotificationCreated`` + fire title-only
        pushes to every registered surface (Web Push + HA mobile app).

        Per §25.3 we never put the body on the wire; subscribers
        translate the title and tap-open the app to see the full row.

        ``dedupe_by_link=True`` collapses bursts: if an unread row
        already exists for the same ``(user_id, type, link_url)`` it's
        bumped in place rather than duplicated. Used today by DMs so a
        five-message burst from one peer shows up as one bell entry
        until the recipient opens the thread.
        """
        if dedupe_by_link:
            saved = await self._notifs.save_or_bump_unread(note)
        else:
            saved = await self._notifs.save(note)
        await self._bus.publish(
            NotificationCreated(
                user_id=saved.user_id,
                notification_id=saved.id,
                type=saved.type,
                title=saved.title,
                link_url=saved.link_url,
            )
        )
        if not push:
            return saved  # in-app only (a repeat the user was already told)
        # Web Push (browsers that registered via pywebpush).
        if self._push is not None:
            try:
                await self._push.push_to_user(
                    saved.user_id,
                    PushPayload(
                        title=saved.title,
                        click_url=saved.link_url,
                        tag=saved.type,
                    ),
                )
            except Exception as exc:
                log.debug("web push fan-out failed: %s", exc)
        # Platform adapter (HA mobile app / standalone inbox).
        if self._adapter is not None:
            try:
                user = await self._users.get_by_user_id(saved.user_id)
                if user is not None:
                    await self._adapter.send_push(
                        user,
                        saved.title,
                        "",
                        data={"type": saved.type, "url": saved.link_url},
                    )
            except Exception as exc:
                log.debug("platform push fan-out failed: %s", exc)
        return saved

    async def _fan_push(
        self,
        user_ids,
        *,
        title: str,
        click_url: str | None = None,
        tag: str | None = None,
        space_id: str | None = None,
    ) -> None:
        """Send a minimal Web Push payload to each user in *user_ids*.

        §25.3: only the title travels in the payload — body is always
        omitted. We treat push failures as best-effort and never raise.
        """
        if self._push is None:
            return
        payload = PushPayload(
            title=title,
            click_url=click_url,
            tag=tag,
            space_id=space_id,
        )
        try:
            await self._push.push_to_users(list(user_ids), payload)
        except Exception as exc:
            log.debug("push fan-out failed: %s", exc)

    def _t(self, key: str, *, locale: str | None, fallback: str, **fmt) -> str:
        if self._i18n is None:
            try:
                return fallback.format(**fmt)
            except KeyError, IndexError:
                return fallback
        translated = self._i18n.gettext(key, locale=locale, **fmt)
        # ``Catalog.gettext`` returns the raw key when the locale catalog
        # is missing the translation.  That looked like
        # ``notification.calendar.updated`` in the inbox UI — clearly
        # not the warm copy we wanted.  Detect the miss and fall back
        # to the formatted fallback so the row reads naturally even
        # before the catalog catches up.
        if translated == key:
            try:
                return fallback.format(**fmt)
            except KeyError, IndexError:
                return fallback
        return translated

    @staticmethod
    def _locale(user) -> str | None:
        return getattr(user, "locale", None) or None

    def wire(self) -> None:
        """Register all event handlers on the bus. Idempotent (but
        calling twice subscribes twice — callers should call once).
        """
        self._bus.subscribe(PostCreated, self.on_post_created)
        self._bus.subscribe(CommentAdded, self.on_comment_added)
        self._bus.subscribe(TaskAssigned, self.on_task_assigned)
        self._bus.subscribe(TaskDeadlineDue, self.on_task_deadline_due)
        self._bus.subscribe(SpacePostCreated, self.on_space_post_created)
        self._bus.subscribe(SpaceModerationQueued, self.on_moderation_queued)
        self._bus.subscribe(ReportFiled, self.on_report_filed)
        self._bus.subscribe(SpaceModerationApproved, self.on_moderation_decided)
        self._bus.subscribe(SpaceModerationRejected, self.on_moderation_decided)
        self._bus.subscribe(SpaceModerationExpired, self.on_moderation_decided)
        self._bus.subscribe(SpaceRemoteSeatLive, self.on_remote_seat_live)
        self._bus.subscribe(DmMessageCreated, self.on_dm_message_created)
        self._bus.subscribe(DmMessageDeleted, self.on_dm_message_deleted)
        self._bus.subscribe(DmMessageUpdated, self.on_dm_message_updated)
        self._bus.subscribe(PostEdited, self.on_post_edited)
        self._bus.subscribe(CommentUpdated, self.on_comment_updated)
        self._bus.subscribe(BazaarBidPlaced, self.on_bazaar_bid_placed)
        self._bus.subscribe(BazaarOfferAccepted, self.on_bazaar_offer_accepted)
        self._bus.subscribe(BazaarOfferRejected, self.on_bazaar_offer_rejected)
        self._bus.subscribe(BazaarListingExpired, self.on_bazaar_listing_expired)
        self._bus.subscribe(DmContactRequested, self.on_dm_contact_requested)
        self._bus.subscribe(CalendarEventCreated, self.on_calendar_event_created)
        self._bus.subscribe(CalendarEventDeleted, self.on_calendar_event_deleted)
        self._bus.subscribe(CalendarEventUpdated, self.on_calendar_event_updated)
        self._bus.subscribe(EventReminderDue, self.on_event_reminder_due)
        self._bus.subscribe(TaskCompleted, self.on_task_completed)
        self._bus.subscribe(SpacePostModerated, self.on_space_post_moderated)
        self._bus.subscribe(SpaceMemberJoined, self.on_space_member_joined)
        self._bus.subscribe(SpaceJoinRequested, self.on_space_join_requested)
        self._bus.subscribe(SpaceJoinApproved, self.on_space_join_approved)
        self._bus.subscribe(SpaceJoinDenied, self.on_space_join_denied)
        self._bus.subscribe(
            RemoteSpaceInviteAccepted,
            self.on_remote_invite_accepted,
        )
        self._bus.subscribe(
            RemoteSpaceInviteDeclined,
            self.on_remote_invite_declined,
        )
        self._bus.subscribe(RemoteSpaceDissolved, self.on_remote_space_dissolved)
        self._bus.subscribe(
            SpaceAdminSeedsRetiredAfterRestore, self.on_admin_seeds_retired
        )
        # Momentum (§Momentum) — reactions, replies, and new follows.
        self._bus.subscribe(MomentReactionChanged, self.on_moment_reaction_changed)
        self._bus.subscribe(MomentCreated, self.on_moment_created)
        self._bus.subscribe(UserFollowed, self.on_user_followed)
        # Space location feature enabled — nudge members to opt in.
        self._bus.subscribe(
            SpaceLocationFeatureEnabled,
            self.on_space_location_feature_enabled,
        )
        # Social Home Apps — a challenge (APP_SESSION open) arrived.
        self._bus.subscribe(
            AppChallengeReceived,
            self.on_app_challenge_received,
        )

    # ── Handlers ───────────────────────────────────────────────────────

    async def on_post_created(self, event: PostCreated) -> None:
        """Notify every active household member except the author."""
        author_id = event.post.author
        users = await self._users.list_active()
        author = await self._users.get_by_user_id(author_id)
        name = author.display_name if author else "Someone"
        blocked = await self._guardian_block_counterparts(author_id)
        for user in users:
            if user.user_id == author_id or user.user_id in blocked:
                continue
            await self._save_notif(
                new_notification(
                    user_id=user.user_id,
                    type="post_created",
                    title=self._t(
                        "notification.post.created",
                        locale=self._locale(user),
                        fallback="{author} posted",
                        author=name,
                    ),
                    link_url=f"/post/{event.post.id}",
                )
            )

    async def on_comment_added(self, event: CommentAdded) -> None:
        """Household-feed comment → notify every other active household
        user. Space comment → the space's members only, honouring their
        notification level and ``event.mentions`` exactly like a space post
        (a household user outside the space must not hear about it)."""
        commenter_id = event.comment.author
        if event.space_id is not None:
            await self._notify_space_members(
                space_id=event.space_id,
                author_id=commenter_id,
                mentions=event.mentions,
                generic_type="space_comment_added",
                generic_key="notification.space.comment.added",
                generic_fallback="{author} commented in {space_name}",
                mention_key="notification.space.comment.mention",
                mention_fallback="{author} mentioned you in a comment in {space_name}",
            )
            return
        # The event only carries post_id, not the post author. For v1 we
        # keep this simple: notify everyone except the commenter. Slightly
        # noisy, but the post author is always notified.
        commenter = await self._users.get_by_user_id(commenter_id)
        name = commenter.display_name if commenter else "Someone"
        users = await self._users.list_active()
        blocked = await self._guardian_block_counterparts(commenter_id)
        for user in users:
            if user.user_id == commenter_id or user.user_id in blocked:
                continue
            await self._save_notif(
                new_notification(
                    user_id=user.user_id,
                    type="comment_added",
                    title=f"{name} commented on a post",
                    link_url=f"/post/{event.post_id}",
                )
            )

    async def on_post_edited(self, event: PostEdited) -> None:
        """A space post edit rings only the members it newly @-mentions
        (``event.new_mentions`` — never those already mentioned, never a
        generic "posted" bell). A newly added ``@here`` pages like on
        create, still under the per-author cooldown."""
        if event.space_id is None or not event.new_mentions:
            return
        await self._notify_space_members(
            space_id=event.space_id,
            author_id=event.post.author,
            mentions=event.new_mentions,
            generic_type=None,
            generic_key="notification.space.post.created",
            generic_fallback="{author} posted in {space_name}",
            mention_key="notification.space.mention",
            mention_fallback="{author} mentioned you in {space_name}",
        )

    async def on_comment_updated(self, event: CommentUpdated) -> None:
        """Space comment edit — same rule as :meth:`on_post_edited`."""
        if event.space_id is None or not event.new_mentions:
            return
        await self._notify_space_members(
            space_id=event.space_id,
            author_id=event.comment.author,
            mentions=event.new_mentions,
            generic_type=None,
            generic_key="notification.space.comment.added",
            generic_fallback="{author} commented in {space_name}",
            mention_key="notification.space.comment.mention",
            mention_fallback="{author} mentioned you in a comment in {space_name}",
        )

    async def on_task_assigned(self, event: TaskAssigned) -> None:
        """Notify the assignee (unless they assigned themselves).

        A space task only notifies a current local member of that space —
        the title must never reach a non-member (defence in depth behind
        the service-level assignee check)."""
        if event.task.created_by == event.assigned_to:
            return
        if (
            event.space_id is not None
            and await self._spaces.get_member(event.space_id, event.assigned_to) is None
        ):
            return
        recipient = await self._users.get_by_user_id(event.assigned_to)
        await self._save_notif(
            new_notification(
                user_id=event.assigned_to,
                type="task_assigned",
                title=self._t(
                    "notification.task.assigned",
                    locale=self._locale(recipient),
                    fallback="You were assigned: {title}",
                    title=event.task.title,
                ),
            )
        )

    async def on_task_deadline_due(self, event: TaskDeadlineDue) -> None:
        """Notify every assignee that a task is due today."""
        for assignee_id in event.task.assignees:
            recipient = await self._users.get_by_user_id(assignee_id)
            title = self._t(
                "notification.task.deadline_due",
                locale=self._locale(recipient),
                fallback="Task due today: {title}",
                title=event.task.title,
            )
            await self._save_notif(
                new_notification(
                    user_id=assignee_id,
                    type="task_deadline",
                    title=title,
                )
            )
        if event.task.assignees:
            await self._fan_push(
                event.task.assignees,
                title=(f"Task due today: {event.task.title}"),
                tag="task_deadline",
                click_url=f"/tasks/{event.task.id}",
            )

    async def on_dm_message_created(self, event: DmMessageCreated) -> None:
        """Create an in-app notification row + push for each recipient.

        §25.3: titles only — body never on the wire. ``_save_notif``
        already drives the push fan-out (Web Push + platform adapter)
        so we don't need to call ``_fan_push`` separately.

        Bell rows are **collapsed per conversation** via
        ``dedupe_by_link=True``: a burst of N messages from one peer
        bumps a single existing unread row rather than spamming the
        bell with N entries. The companion :meth:`mark_read_for_dm`
        clears that row when the recipient opens the thread, so the
        next message after opening starts a fresh row.

        Active-viewer suppression: when the recipient already has the
        DM thread open in any of their tabs (the SPA emits
        ``{type: 'dm.active', data: {conversation_id}}`` over WS on
        mount), we skip the bell row AND the push for that recipient.
        The message itself still renders via the regular DM broadcast
        path; only the notification noise is gone. Without an attached
        ``WebSocketManager`` we degrade to the pre-fix behaviour
        (always notify) so unit tests that don't wire the manager
        don't have to mock it.

        Muted conversations: a recipient who muted this conversation
        (``conversation_members.muted_until`` still in the future) gets
        neither the bell row nor the push. The message and its unread
        count are untouched; the mute is theirs alone and never federated.

        Group mentions (``event.mentions``, resolved against the
        conversation's seats): a mentioned member gets one distinct
        ``dm_mention`` bell ("{sender} mentioned you in {chat}") instead of
        the ``dm_message`` one; a member at level ``mentions``
        (``conversation_members.notif_level``) is rung only when mentioned.
        A mute wins over both. In a 1:1 a mention is just a message.

        The household chat (a system chat) reads "{sender} in Household
        chat" (localised; no location / content hint) and opens the feed's
        Chat tab (:data:`HOUSEHOLD_CHAT_LINK`) instead of ``/dms/{id}``.
        """
        if not event.recipient_user_ids:
            return
        # A shared location says so in the title — never where (§25.3:
        # no coordinates, no label, no body).
        if event.message_type == "location":
            title = f"{event.sender_display_name} shared a location"
        else:
            title = f"{event.sender_display_name} messaged you"
        prefs = await self._seat_prefs(event.conversation_id)
        link = _conversation_link(
            event.conversation_id, prefs.system_scope, prefs.space_id
        )
        system_chat = prefs.system_scope is not None
        muted = prefs.muted
        mentioned = (
            {m.user_id for m in event.mentions if m.user_id} if prefs.group else set()
        )
        blocked = await self._guardian_block_counterparts(event.sender_user_id)
        for recipient_id in event.recipient_user_ids:
            if recipient_id in blocked:
                continue
            # Notifications.user_id FK's into ``users`` (local accounts
            # only). Remote recipients' rows live in ``remote_users``;
            # they get notified on their *own* household via the
            # inbound DM federation handler. Skipping them here keeps
            # the sender-side ``DmMessageCreated`` handler from blowing
            # up with a FOREIGN KEY constraint failure for every
            # cross-household DM.
            local = await self._users.get_by_user_id(recipient_id)
            if local is None:
                continue
            if local.username in muted:
                # The recipient muted this conversation: the message still
                # lands and counts unread, but no bell row and no push.
                continue
            is_mentioned = recipient_id in mentioned
            if local.username in prefs.mentions_only and not is_mentioned:
                continue
            if self._ws_manager is not None and (
                self._ws_manager.is_user_active_in_conversation(
                    recipient_id,
                    event.conversation_id,
                )
            ):
                continue
            if is_mentioned:
                await self._save_dm_mention(
                    local,
                    sender_name=event.sender_display_name,
                    chat_name=self._chat_name(prefs, local),
                    link=link,
                )
                continue
            await self._save_notif(
                new_notification(
                    user_id=recipient_id,
                    type="dm_message",
                    title=(
                        self._t(
                            "notification.household_chat.message",
                            locale=self._locale(local),
                            fallback="{author} in {chat_name}",
                            author=event.sender_display_name,
                            chat_name=self._chat_name(prefs, local),
                        )
                        if system_chat
                        else title
                    ),
                    link_url=link,
                ),
                dedupe_by_link=True,
            )

    async def on_dm_message_deleted(self, event: DmMessageDeleted) -> None:
        """A deleted message no longer rings: for each local member it went
        to, once nothing in the conversation is unread for them any more,
        the conversation's ``dm_message`` / ``dm_mention`` bell rows are
        marked read (bell rows are collapsed per conversation, so this is
        the row the deleted message bumped)."""
        if self._convos is None:
            return
        for user_id in dict.fromkeys(event.recipient_user_ids):
            local = await self._users.get_by_user_id(user_id)
            if local is None:
                continue
            if await self._convos.count_unread(event.conversation_id, local.username):
                continue
            await self.mark_read_for_dm(user_id, event.conversation_id)

    def _chat_name(self, prefs: "_SeatPrefs", recipient) -> str | None:
        """The name a bell gives the conversation: the group's own, or for
        the household chat its localised label, for a space chat the space's
        name."""
        if prefs.system_scope is SystemChatScope.SPACE:
            return prefs.space_name or self._t(
                "notification.space_chat.name",
                locale=self._locale(recipient),
                fallback="a space",
            )
        if prefs.system_scope is SystemChatScope.HOUSEHOLD:
            return self._t(
                "notification.household_chat.name",
                locale=self._locale(recipient),
                fallback="Household chat",
            )
        return prefs.name

    async def on_dm_message_updated(self, event: DmMessageUpdated) -> None:
        """A sender's edit rings only the group members it newly @-mentions
        (``event.new_mentions``) with a ``dm_mention`` bell — whatever
        their level, never while muted, never the sender, never someone
        already mentioned, never across a guardian block. Transcript patches
        carry no new mentions."""
        if not event.new_mentions:
            return
        prefs = await self._seat_prefs(event.conversation_id)
        if not prefs.group:
            return
        link = _conversation_link(
            event.conversation_id, prefs.system_scope, prefs.space_id
        )
        allowed = set(event.recipient_user_ids)
        blocked = await self._guardian_block_counterparts(event.sender_user_id)
        name = event.sender_display_name or await self._display_name(
            event.sender_user_id
        )
        for user_id in {m.user_id for m in event.new_mentions if m.user_id}:
            if (
                user_id == event.sender_user_id
                or user_id not in allowed
                or user_id in blocked
            ):
                continue
            local = await self._users.get_by_user_id(user_id)
            if local is None or local.username in prefs.muted:
                continue
            if self._ws_manager is not None and (
                self._ws_manager.is_user_active_in_conversation(
                    user_id, event.conversation_id
                )
            ):
                continue
            await self._save_dm_mention(
                local,
                sender_name=name,
                chat_name=self._chat_name(prefs, local),
                link=link,
            )

    async def _save_dm_mention(
        self, recipient, *, sender_name: str, chat_name: str | None, link: str
    ) -> None:
        if chat_name:
            title = self._t(
                "notification.dm.mention",
                locale=self._locale(recipient),
                fallback="{author} mentioned you in {chat_name}",
                author=sender_name,
                chat_name=chat_name,
            )
        else:
            title = self._t(
                "notification.dm.mention_unnamed",
                locale=self._locale(recipient),
                fallback="{author} mentioned you in a group chat",
                author=sender_name,
            )
        await self._save_notif(
            new_notification(
                user_id=recipient.user_id,
                type="dm_mention",
                title=title,
                link_url=link,
            ),
            dedupe_by_link=True,
        )

    async def _display_name(self, user_id: str) -> str:
        local = await self._users.get_by_user_id(user_id)
        if local is not None:
            return local.display_name
        remote = await self._users.get_remote(user_id)
        return remote.display_name if remote and remote.display_name else "Someone"

    async def _seat_prefs(self, conversation_id: str) -> "_SeatPrefs":
        """This conversation's per-member bell settings, read once per
        message: who muted it right now, who is at level ``mentions``, and
        whether it is a group (only groups have mentions / levels)."""
        if self._convos is None:
            return _SeatPrefs()
        now = datetime.now(timezone.utc)
        members = await self._convos.list_members(conversation_id)
        conv = await self._convos.get(conversation_id)
        group = conv is not None and conv.type is ConversationType.GROUP_DM
        space_id = conv.space_id if conv is not None else None
        space = await self._spaces.get(space_id) if space_id else None
        return _SeatPrefs(
            muted=frozenset(
                m.username for m in members if mute_active(m.muted_until, now=now)
            ),
            mentions_only=frozenset(
                m.username for m in members if group and m.notif_level == "mentions"
            ),
            group=group,
            name=conv.name if conv is not None and group else None,
            system_scope=conv.system_scope if conv is not None else None,
            space_id=space_id,
            space_name=space.name if space is not None else None,
        )

    async def mark_read_for_dm(
        self,
        user_id: str,
        conversation_id: str,
    ) -> int:
        """Mark every unread ``dm_message`` / ``dm_mention`` notification
        pointing at a conversation as read for one user. Returns the number flipped.

        Called from ``POST /api/conversations/{id}/read`` so the bell
        clears in step with the thread's read-receipt state — opening
        the thread is the natural "I've seen these" signal, no
        separate UI gesture needed.
        """
        conv = await self._convos.get(conversation_id) if self._convos else None
        link = _conversation_link(
            conversation_id,
            conv.system_scope if conv is not None else None,
            conv.space_id if conv is not None else None,
        )
        flipped = 0
        for ntype in ("dm_message", "dm_mention"):
            flipped += await self._notifs.mark_read_by_link(
                user_id=user_id, link_url=link, type=ntype
            )
        return flipped

    async def on_dm_contact_requested(self, event: DmContactRequested) -> None:
        """A user wants to start a DM — notify the recipient + push."""
        if await self._guardian_blocked(
            event.recipient_user_id, event.requester_user_id
        ):
            return
        recipient = await self._users.get_by_user_id(event.recipient_user_id)
        title = self._t(
            "notification.dm.contact_requested",
            locale=self._locale(recipient),
            fallback="{name} wants to message you",
            name=event.requester_display_name,
        )
        await self._save_notif(
            new_notification(
                user_id=event.recipient_user_id,
                type="dm_contact_requested",
                title=title,
                link_url="/dms",
            )
        )
        await self._fan_push(
            [event.recipient_user_id],
            title=title,
            tag=f"dm-contact:{event.requester_user_id}",
            click_url="/dms",
        )

    async def on_app_challenge_received(
        self,
        event: AppChallengeReceived,
    ) -> None:
        """A Social Home App challenge arrived for a local user — bell + push.

        §25.3: title only — the challenger's display name is a human label
        (not user-generated content) and rides the title; no body on the wire.
        Chess is the only app today, so the chess wording is fine. The deep
        link points at the app surface so a tap opens it; the SPA's open frame
        (delivered separately over WS) seats the live invite.
        """
        recipient = await self._users.get_by_user_id(event.to_user_id)
        title = self._t(
            "notification.app.challenge",
            locale=self._locale(recipient),
            fallback="♟ {name} challenged you to chess",
            name=event.from_display,
        )
        await self._save_notif(
            new_notification(
                user_id=event.to_user_id,
                type="app_challenge",
                title=title,
                link_url=f"/apps/{event.app_id}",
            )
        )
        await self._fan_push(
            [event.to_user_id],
            title=title,
            tag=f"app-challenge:{event.app_id}:{event.session_id}",
            click_url=f"/apps/{event.app_id}",
        )

    async def on_bazaar_bid_placed(self, event: BazaarBidPlaced) -> None:
        """Notify the seller that a bid landed (or was raised)."""
        if event.bidder_user_id == event.seller_user_id:
            return
        recipient = await self._users.get_by_user_id(event.seller_user_id)
        title = self._t(
            "notification.bazaar.bid_placed",
            locale=self._locale(recipient),
            fallback="New bid on your listing",
        )
        await self._save_notif(
            new_notification(
                user_id=event.seller_user_id,
                type="bazaar_bid_placed",
                title=title,
                link_url=f"/bazaar/{event.listing_post_id}",
            )
        )
        await self._fan_push(
            [event.seller_user_id],
            title=title,
            tag=f"bazaar-bid:{event.listing_post_id}",
            click_url=f"/bazaar/{event.listing_post_id}",
        )

    async def on_bazaar_offer_accepted(
        self,
        event: BazaarOfferAccepted,
    ) -> None:
        """Notify the buyer that the seller accepted their offer."""
        recipient = await self._users.get_by_user_id(event.buyer_user_id)
        title = self._t(
            "notification.bazaar.offer_accepted",
            locale=self._locale(recipient),
            fallback="Your offer was accepted",
        )
        await self._save_notif(
            new_notification(
                user_id=event.buyer_user_id,
                type="bazaar_offer_accepted",
                title=title,
                link_url=f"/bazaar/{event.listing_post_id}",
            )
        )
        await self._fan_push(
            [event.buyer_user_id],
            title=title,
            tag=f"bazaar-accept:{event.listing_post_id}",
            click_url=f"/bazaar/{event.listing_post_id}",
        )

    async def on_bazaar_offer_rejected(
        self,
        event: BazaarOfferRejected,
    ) -> None:
        recipient = await self._users.get_by_user_id(event.bidder_user_id)
        title = self._t(
            "notification.bazaar.offer_rejected",
            locale=self._locale(recipient),
            fallback="Your offer was declined",
        )
        await self._save_notif(
            new_notification(
                user_id=event.bidder_user_id,
                type="bazaar_offer_rejected",
                title=title,
                link_url=f"/bazaar/{event.listing_post_id}",
            )
        )
        await self._fan_push(
            [event.bidder_user_id],
            title=title,
            tag=f"bazaar-reject:{event.listing_post_id}",
            click_url=f"/bazaar/{event.listing_post_id}",
        )

    async def on_bazaar_listing_expired(
        self,
        event: BazaarListingExpired,
    ) -> None:
        """Notify the seller whenever a listing transitions to sold/expired."""
        recipient = await self._users.get_by_user_id(event.seller_user_id)
        if event.final_status == "sold":
            title = self._t(
                "notification.bazaar.sold",
                locale=self._locale(recipient),
                fallback="Your listing sold",
            )
        else:
            title = self._t(
                "notification.bazaar.expired",
                locale=self._locale(recipient),
                fallback="Your listing expired without a buyer",
            )
        await self._save_notif(
            new_notification(
                user_id=event.seller_user_id,
                type=f"bazaar_listing_{event.final_status}",
                title=title,
                link_url=f"/bazaar/{event.listing_post_id}",
            )
        )
        await self._fan_push(
            [event.seller_user_id],
            title=title,
            tag=f"bazaar-closed:{event.listing_post_id}",
            click_url=f"/bazaar/{event.listing_post_id}",
        )

    async def on_space_post_created(self, event: SpacePostCreated) -> None:
        """Notify space members (except the author). Space name is included
        in the title for context. Body is omitted per §25.3.

        See :meth:`_notify_space_members` for how the per-member
        :table:`space_notif_prefs` level and ``event.mentions`` combine.
        """
        await self._notify_space_members(
            space_id=event.space_id,
            author_id=event.post.author,
            mentions=event.mentions,
            generic_type="space_post_created",
            generic_key="notification.space.post.created",
            generic_fallback="{author} posted in {space_name}",
            mention_key="notification.space.mention",
            mention_fallback="{author} mentioned you in {space_name}",
        )

    async def _notify_space_members(
        self,
        *,
        space_id: str,
        author_id: str,
        mentions: tuple[Mention, ...],
        generic_type: str | None,
        generic_key: str,
        generic_fallback: str,
        mention_key: str,
        mention_fallback: str,
    ) -> None:
        """Fan a space-content bell out to this household's space members.

        Per member (never the author, so a self-mention is silent):

        * ``muted`` → nothing, mentioned or not.
        * mentioned (a resolved ``MentionType.USER`` in *mentions*) → one
          ``space_mention`` bell ("{author} mentioned you in …") instead of
          the generic one — never both.
        * ``mentions`` level and not mentioned → nothing.
        * otherwise (``all``) → the generic bell.

        ``@here`` (a ``MentionType.HERE`` entry — the resolver keeps it only
        when the space allows it and the author is owner/admin by this
        household's roster): every member at ``all`` / ``mentions`` gets one
        ``space_here`` bell ("{author} notified everyone in …") instead of the
        generic one; a member also mentioned directly gets the mention bell
        only. At most one @here per ``(space, author)`` per
        :data:`HERE_COOLDOWN_SECONDS` pages anyone — a later one inside the
        window notifies like a plain post (per-process, spam brake).

        ``generic_type=None`` (an edit, whose *mentions* are only the newly
        added ones) → no generic bell for anyone: only the mentioned (and a
        newly added, still-allowed, not-rate-limited ``@here``) are rung.

        The member list is the local ``space_members`` roster, so a mention can
        only ever reach a member — the resolver never resolves outsiders.
        """
        space = await self._spaces.get(space_id)
        if space is None:
            return
        author = await self._users.get_by_user_id(author_id)
        # A post federated from another household names a remote author.
        remote_author = (
            await self._users.get_remote(author_id) if author is None else None
        )
        name = (
            author.display_name
            if author
            else remote_author.display_name
            if remote_author and remote_author.display_name
            else "Someone"
        )
        mentioned = {m.user_id for m in mentions if m.user_id}
        here = any(m.type is MentionType.HERE for m in mentions) and (
            self._take_here_slot(space_id, author_id)
        )
        members = await self._spaces.list_members(space_id)
        blocked = await self._guardian_block_counterparts(author_id)
        for member in members:
            if member.user_id == author_id or member.user_id in blocked:
                continue
            level = await self._notifs.get_space_notif_level(
                user_id=member.user_id,
                space_id=space_id,
            )
            if level == "muted":
                continue
            is_mentioned = member.user_id in mentioned
            if level == "mentions" and not (is_mentioned or here):
                continue
            if is_mentioned:
                ntype, key, fallback = "space_mention", mention_key, mention_fallback
            elif here:
                ntype, key, fallback = (
                    "space_here",
                    "notification.space.here",
                    "{author} notified everyone in {space_name}",
                )
            elif generic_type is None:
                continue
            else:
                ntype, key, fallback = generic_type, generic_key, generic_fallback
            recipient = await self._users.get_by_user_id(member.user_id)
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type=ntype,
                    title=self._t(
                        key,
                        locale=self._locale(recipient),
                        fallback=fallback,
                        author=name,
                        space_name=space.name,
                    ),
                    link_url=f"/spaces/{space_id}",
                )
            )

    def _take_here_slot(self, space_id: str, author_id: str) -> bool:
        """Claim this author's @here slot in *space_id*; ``False`` while the
        previous @here is younger than :data:`HERE_COOLDOWN_SECONDS`."""
        now = self._clock()
        key = (space_id, author_id)
        last = self._here_last.get(key)
        if last is not None and now - last < HERE_COOLDOWN_SECONDS:
            return False
        self._here_last[key] = now
        if len(self._here_last) > _HERE_TRACK_MAX:
            # Bounded: drop entries whose window has passed.
            self._here_last = {
                k: t
                for k, t in self._here_last.items()
                if now - t < HERE_COOLDOWN_SECONDS
            }
        return True

    async def on_moderation_queued(self, event: SpaceModerationQueued) -> None:
        """Notify everyone who works the queue (owner / admin / moderator)
        that content is pending review."""
        space = await self._spaces.get(event.item.space_id)
        if space is None:
            return
        members = await self._spaces.list_members(event.item.space_id)
        for member in members:
            if member.role in CONTENT_AUTHORITY_ROLES:
                recipient = await self._users.get_by_user_id(member.user_id)
                await self._save_notif(
                    new_notification(
                        user_id=member.user_id,
                        type="moderation_pending",
                        title=self._t(
                            "notification.space.moderation.queued",
                            locale=self._locale(recipient),
                            fallback="New content pending review in {space_name}",
                            space_name=space.name,
                        ),
                        link_url=f"/spaces/{event.item.space_id}?tab=moderation",
                    )
                )

    async def on_report_filed(self, event: ReportFiled) -> None:
        """A space-scoped report: tell the space's content authority
        (owner / admin / moderator) on this household — except the reporter
        and the report's subject (the reported member / the item's author,
        unless they are the space's sole authority) — title only, never the category, notes or
        reporter (§25.3). Household-level reports
        notify nobody (household admins look in the admin page)."""
        if not event.space_id:
            return
        space = await self._spaces.get(event.space_id)
        if space is None or space.dissolved:
            return
        members = await self._spaces.list_members(event.space_id)
        authority = [m for m in members if m.role in CONTENT_AUTHORITY_ROLES]
        subject = event.subject_user_id
        # The subject reviews a report about themself only as the space's
        # sole content authority (its owner, nobody else anywhere) — the
        # ``ReportService.review_space`` rule.
        subject_reviews = (
            subject is not None
            and len(authority) == 1
            and authority[0].user_id == subject
            and authority[0].role == SpaceRole.OWNER
            and not (
                self._remote_members is not None
                and await self._remote_members.list_instances_with_roles(
                    event.space_id,
                    frozenset({SpaceRole.ADMIN.value, SpaceRole.MODERATOR.value}),
                )
            )
        )
        for member in authority:
            if member.user_id == event.reporter_user_id:
                continue
            if member.user_id == subject and not subject_reviews:
                continue  # never told about a report on themself
            recipient = await self._users.get_by_user_id(member.user_id)
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="space_report",
                    title=self._t(
                        "notification.space.report.filed",
                        locale=self._locale(recipient),
                        fallback="New report in {space_name}",
                        space_name=space.name,
                    ),
                    link_url=f"/spaces/{event.space_id}?tab=moderation",
                )
            )

    async def on_remote_seat_live(self, event: SpaceRemoteSeatLive) -> None:
        """A household's FIRST member just took a seat in a space that keeps
        a feature other than posts "Reviewed", and that household is too old
        to submit its members' changes for review (below v_43, federated
        moderation): every receiver refuses those changes. Tell the space's
        local owner / admins once, so they ask that household to update or
        pick another level. A v_43 household needs no warning."""
        if self._remote_members is None:
            return
        if self._instances is not None:
            peer = await self._instances.get_instance(event.instance_id)
            if (
                peer is not None
                and peer.proto_version
                >= FederationCapability.MIN_FOR_FEDERATED_MODERATION
            ):
                return
        space = await self._spaces.get(event.space_id)
        if space is None or space.dissolved:
            return
        reviewed = [
            f
            for f in ("pages", "tasks", "stickies", "calendar")
            if space.features.access_level(f) is SpaceFeatureAccess.MODERATED
        ]
        if not reviewed:
            return
        seats = await self._remote_members.list_for_instance(
            event.space_id, event.instance_id, include_tombstoned=False
        )
        if len(seats) > 1:
            return  # not the household's first member — already told
        for member in await self._spaces.list_members(event.space_id):
            if member.role not in SETTINGS_AUTHORITY_ROLES:
                continue
            recipient = await self._users.get_by_user_id(member.user_id)
            locale = self._locale(recipient)
            names = ", ".join(
                self._t(
                    f"notification.space.feature.{f}",
                    locale=locale,
                    fallback=_FEATURE_FALLBACK[f],
                )
                for f in reviewed
            )
            # One push per (space, household): the link names the household,
            # so its rejoin bumps the existing bell in-app (read or not) and
            # only a household that is new here pushes again.
            link = f"/spaces/{event.space_id}/settings?household={event.instance_id}"
            told = await self._notifs.find(
                Spec(
                    where=[
                        ("user_id", "=", member.user_id),
                        ("type", "=", "moderation_unavailable"),
                        ("link_url", "=", link),
                    ],
                    limit=1,
                )
            )
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="moderation_unavailable",
                    title=self._t(
                        "notification.space.moderation.unavailable",
                        locale=locale,
                        fallback=(
                            "Reviewed isn't available for {features} in "
                            "{space_name} for members of a household that "
                            "needs an update — their changes are refused until "
                            "it updates or you choose another level"
                        ),
                        features=names,
                        space_name=space.name,
                    ),
                    link_url=link,
                ),
                dedupe_by_link=True,
                push=not told,
            )

    async def on_moderation_decided(
        self,
        event: SpaceModerationApproved
        | SpaceModerationRejected
        | SpaceModerationExpired,
    ) -> None:
        """Tell the submitter what became of their queued item
        (``moderation_decided``). Title only — never the content, never a
        moderator's rejection reason (§25.3); the app shows those."""
        item = event.item
        space = await self._spaces.get(item.space_id)
        if space is None:
            return
        if await self._spaces.get_member(item.space_id, item.submitted_by) is None:
            return  # they left: nothing of theirs to come back to
        recipient = await self._users.get_by_user_id(item.submitted_by)
        if recipient is None:
            return
        outcome = item.status.value  # approved | rejected | expired
        fallback = {
            "approved": "Your submission in {space_name} was approved",
            "rejected": "Your submission in {space_name} was not approved",
        }.get(outcome, "Your submission in {space_name} expired without a review")
        await self._save_notif(
            new_notification(
                user_id=item.submitted_by,
                type="moderation_decided",
                title=self._t(
                    f"notification.space.moderation.{outcome}",
                    locale=self._locale(recipient),
                    fallback=fallback,
                    space_name=space.name,
                ),
                link_url=f"/spaces/{item.space_id}",
            )
        )

    async def on_calendar_event_created(
        self,
        event: CalendarEventCreated,
    ) -> None:
        """Notify only the people the event is actually for.

        Two paths depending on which calendar the event lives on:

        * **Personal calendar.** The ``calendars`` table holds one row
          per user's personal calendar; the event's ``calendar_id``
          points at one of those rows. Only the calendar's owner needs
          a bell, and only when *someone else* added the event — a
          user adding a reminder to their own calendar should never
          notify themselves.

        * **Space calendar.** Space events live in
          ``space_calendar_events`` and don't have a row in
          ``calendars``; their ``calendar_id`` is the
          :class:`Space` id directly. Members of that space (minus the
          creator) get the bell — same shape as ``SpacePostCreated``.

        The old behavior fanned out to every active household member
        regardless of whose calendar the event lived on, which spammed
        users about events they had nothing to do with. The user
        report that drove the rewrite is on file in the PR notes.
        """
        cal_event = event.event
        # Personal-calendar branch — only fires if the calendar row
        # exists in ``calendars`` (i.e. the event lives on a personal
        # calendar, not a space one).
        if self._personal_calendar_repo is not None:
            try:
                cal = await self._personal_calendar_repo.get_calendar(
                    cal_event.calendar_id,
                )
            except Exception:
                cal = None
            if cal is not None:
                owner = await self._users.get(cal.owner_username)
                if owner is None:
                    return
                if owner.user_id == cal_event.created_by:
                    # The owner added the event to their own calendar —
                    # no self-notification.
                    return
                await self._save_notif(
                    new_notification(
                        user_id=owner.user_id,
                        type="calendar_event_created",
                        title=self._t(
                            "notification.calendar.created",
                            locale=self._locale(owner),
                            fallback="New event: {summary}",
                            summary=cal_event.summary,
                        ),
                        link_url="/calendar",
                    )
                )
                return
        # Space-calendar branch — ``calendar_id`` is the space_id.
        # Notify members of that space (except the creator). If the
        # space doesn't exist either (e.g., a misrouted federation
        # event), bail silently rather than reverting to the
        # household-wide fanout.
        space = await self._spaces.get(cal_event.calendar_id)
        if space is None:
            return
        members = await self._spaces.list_members(cal_event.calendar_id)
        for member in members:
            if member.user_id == cal_event.created_by:
                continue
            recipient = await self._users.get_by_user_id(member.user_id)
            if recipient is None:
                continue
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="calendar_event_created",
                    title=self._t(
                        "notification.calendar.created",
                        locale=self._locale(recipient),
                        fallback="New event: {summary}",
                        summary=cal_event.summary,
                    ),
                    link_url="/calendar",
                )
            )

    async def on_calendar_event_deleted(
        self,
        event: CalendarEventDeleted,
    ) -> None:
        """Phase D: cancellation push to RSVPed members.

        Receives the pre-delete snapshot from
        :meth:`SpaceCalendarService.delete_event`. The cohort
        (``notify_user_ids``) was captured before the FK CASCADE
        wiped the RSVP rows.
        """
        if not event.notify_user_ids:
            return
        title = self._t(
            "notification.calendar.cancelled",
            locale=None,
            fallback="Event cancelled: {summary}",
            summary=event.summary or "(removed)",
        )
        for uid in event.notify_user_ids:
            recipient = await self._users.get_by_user_id(uid)
            if recipient is None:
                continue
            localized = (
                self._t(
                    "notification.calendar.cancelled",
                    locale=self._locale(recipient),
                    fallback="Event cancelled: {summary}",
                    summary=event.summary or "(removed)",
                )
                or title
            )
            await self._save_notif(
                new_notification(
                    user_id=uid,
                    type="calendar_event_cancelled",
                    title=localized,
                    link_url=(
                        f"/spaces/{event.space_id}/calendar"
                        if event.space_id
                        else "/calendar"
                    ),
                )
            )

    async def on_calendar_event_updated(
        self,
        event: CalendarEventUpdated,
    ) -> None:
        """Phase D: push only when material fields change.

        Material = start / end / summary / capacity-down. Cosmetic
        updates (description, attendees, rrule, all_day) stay silent so
        members don't get notification spam from incidental edits.
        """
        if not event.material_changes:
            return
        if self._calendar_repo is None:
            return
        cal_event = event.event
        try:
            rsvps = await self._calendar_repo.list_rsvps(cal_event.id)
        except Exception:
            return
        cohort = {
            r.user_id
            for r in rsvps
            if r.status
            in (
                "going",
                "waitlist",
                "requested",
                "maybe",
            )
        }
        if not cohort:
            return
        for uid in cohort:
            recipient = await self._users.get_by_user_id(uid)
            if recipient is None:
                continue
            await self._save_notif(
                new_notification(
                    user_id=uid,
                    type="calendar_event_updated",
                    title=self._t(
                        "notification.calendar.updated",
                        locale=self._locale(recipient),
                        fallback="Event updated: {summary}",
                        summary=cal_event.summary,
                    ),
                    link_url=f"/spaces/{cal_event.calendar_id}/calendar",
                )
            )

    async def on_event_reminder_due(self, event: EventReminderDue) -> None:
        """Phase D: deliver the user's chosen reminder."""
        recipient = await self._users.get_by_user_id(event.user_id)
        if recipient is None:
            return
        await self._save_notif(
            new_notification(
                user_id=event.user_id,
                type="calendar_reminder",
                title=self._t(
                    "notification.calendar.reminder",
                    locale=self._locale(recipient),
                    fallback="Reminder: {summary}",
                    summary=event.summary,
                ),
                link_url=f"/spaces/{event.space_id}/calendar",
            )
        )

    async def on_task_completed(self, event: TaskCompleted) -> None:
        """Notify task assignees when a task is completed.

        A space task notifies only assignees who are still local members
        of the space and links to the space's tasks tab."""
        task = event.task
        completed_by = event.completed_by
        completer = await self._users.get_by_user_id(completed_by)
        name = completer.display_name if completer else "Someone"
        if event.space_id is None:
            link_url = f"/tasks/{task.list_id}"
            members: set[str] | None = None
        else:
            link_url = f"/spaces/{event.space_id}?tab=tasks"
            members = set(await self._spaces.list_local_member_user_ids(event.space_id))
        for uid in getattr(task, "assignees", ()):
            if uid == completed_by:
                continue
            if members is not None and uid not in members:
                continue
            recipient = await self._users.get_by_user_id(uid)
            await self._save_notif(
                new_notification(
                    user_id=uid,
                    type="task_completed",
                    title=self._t(
                        "notification.task.completed",
                        locale=self._locale(recipient),
                        fallback="{name} completed: {title}",
                        name=name,
                        title=task.title,
                    ),
                    link_url=link_url,
                )
            )

    async def on_space_post_moderated(
        self,
        event: SpacePostModerated,
    ) -> None:
        """Notify the post author that their post was moderated."""
        post = event.post
        await self._save_notif(
            new_notification(
                user_id=post.author,
                type="post_moderated",
                title="Your post was moderated",
                link_url=f"/spaces/{event.space_id}",
            )
        )

    async def on_space_member_joined(self, event: SpaceMemberJoined) -> None:
        """Tell existing members that a new person joined (§23.52)."""
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        joiner = await self._users.get_by_user_id(event.user_id)
        name = joiner.display_name if joiner else event.user_id
        members = await self._spaces.list_members(event.space_id)
        for member in members:
            if member.user_id == event.user_id:
                continue
            recipient = await self._users.get_by_user_id(member.user_id)
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="space_member_joined",
                    title=self._t(
                        "notification.space.member.joined",
                        locale=self._locale(recipient),
                        fallback="{name} joined {space_name}",
                        name=name,
                        space_name=space.name,
                    ),
                    link_url=f"/spaces/{event.space_id}",
                )
            )

    async def on_remote_space_dissolved(self, event: RemoteSpaceDissolved) -> None:
        """The owner host ended our membership in a space; the local copy was
        archived read-only. Tell each local member once that their copy is now
        a read-only archive (the space is still viewable).

        Reason-aware: both ``_on_dissolved`` (reason='dissolved') and
        ``_on_sync_rejected`` (reason='dissolved' | 'removed') publish this
        event, and the space row's ``archived_reason`` is already set by the
        time we run. 'removed' means the space still exists on the host but we
        were dropped from it; 'dissolved' (or anything else) keeps the
        original 'was dissolved' wording."""
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        if space.archived_reason == "removed":
            title = f"You're no longer a member of “{space.name}”"
            body = "Your copy is now a read-only archive."
        else:
            title = f"“{space.name}” was dissolved"
            body = (
                "The space's owner dissolved it. Your copy is now a read-only archive."
            )
        members = await self._spaces.list_members(event.space_id)
        for member in members:
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="space_dissolved",
                    title=title,
                    body=body,
                    link_url=f"/spaces/{event.space_id}",
                ),
                # A re-broadcast of SPACE_DISSOLVED (fresh msg_id) must not
                # re-notify every member; the (user_id, type, link_url) tuple
                # is stable for this one-time notice.
                dedupe_by_link=True,
            )

    async def on_admin_seeds_retired(
        self, event: SpaceAdminSeedsRetiredAfterRestore
    ) -> None:
        """v_44: a restore rotated a space's authority key, shared the new
        seed with nobody and turned delegated admin OFF. Tell the space's
        OWNER (only) to review its admins; turning delegation back on shares
        the seed with the admins seated then."""
        for member in await self._spaces.list_members(event.space_id):
            if member.role != SpaceRole.OWNER:
                continue
            owner = await self._users.get_by_user_id(member.user_id)
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="space_admins_reconfirm",
                    title=self._t(
                        "notification.space.admins_reconfirm",
                        locale=self._locale(owner),
                        fallback=(
                            "Restored from backup: review space admins, then "
                            "turn delegated admin back on"
                        ),
                    ),
                    body=None,
                    link_url=f"/spaces/{event.space_id}",
                ),
                dedupe_by_link=True,
            )

    async def on_space_join_requested(
        self,
        event: SpaceJoinRequested,
    ) -> None:
        """Notify space admins + owner that a new join request is pending."""
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        requester = await self._users.get_by_user_id(event.user_id)
        name = requester.display_name if requester else event.user_id
        members = await self._spaces.list_members(event.space_id)
        for member in members:
            if member.role not in SETTINGS_AUTHORITY_ROLES:
                continue
            recipient = await self._users.get_by_user_id(member.user_id)
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="space_join_requested",
                    title=self._t(
                        "notification.space.join.requested",
                        locale=self._locale(recipient),
                        fallback="{name} wants to join {space_name}",
                        name=name,
                        space_name=space.name,
                    ),
                    link_url=f"/spaces/{event.space_id}#join-requests",
                )
            )

    async def on_space_join_approved(
        self,
        event: SpaceJoinApproved,
    ) -> None:
        """Tell the requester their join was approved."""
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        recipient = await self._users.get_by_user_id(event.user_id)
        if recipient is None:
            # A §D2 cross-household applicant (or an admin/mod elevation of
            # one): the approved user lives on another household, so there
            # is no LOCAL user to notify — and a notification row for a
            # non-local user_id would fail the users FK. The applicant is
            # told over federation, not by a local notification here.
            return
        await self._save_notif(
            new_notification(
                user_id=event.user_id,
                type="space_join_approved",
                title=self._t(
                    "notification.space.join.approved",
                    locale=self._locale(recipient),
                    fallback="You're in: {space_name}",
                    space_name=space.name,
                ),
                link_url=f"/spaces/{event.space_id}",
            )
        )

    async def on_space_join_denied(
        self,
        event: SpaceJoinDenied,
    ) -> None:
        """§D2 — tell the requester their join was declined.

        Per §25.3 title-only rule: space name is omitted from the body
        because the notification may surface on a lock screen.
        """
        recipient = await self._users.get_by_user_id(event.user_id)
        if recipient is None:
            # Cross-household applicant — no local user to notify, and a row
            # for a non-local user_id would fail the users FK.
            return
        await self._save_notif(
            new_notification(
                user_id=event.user_id,
                type="space_join_denied",
                title=self._t(
                    "notification.space.join.denied",
                    locale=self._locale(recipient),
                    fallback="Your join request was declined.",
                ),
                link_url=None,
            )
        )

    async def on_remote_invite_accepted(
        self,
        event: RemoteSpaceInviteAccepted,
    ) -> None:
        """§D1b — inviter learns the remote user accepted. Title-only."""
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        members = await self._spaces.list_members(event.space_id)
        # Narrow to space admins: members with role admin|owner.
        for m in members:
            if m.role not in SETTINGS_AUTHORITY_ROLES:
                continue
            recipient = await self._users.get_by_user_id(m.user_id)
            await self._save_notif(
                new_notification(
                    user_id=m.user_id,
                    type="space_remote_invite_accepted",
                    title=self._t(
                        "notification.space.remote_invite.accepted",
                        locale=self._locale(recipient),
                        fallback="Your invite was accepted.",
                    ),
                    link_url=f"/spaces/{event.space_id}",
                )
            )

    async def on_remote_invite_declined(
        self,
        event: RemoteSpaceInviteDeclined,
    ) -> None:
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        members = await self._spaces.list_members(event.space_id)
        for m in members:
            if m.role not in SETTINGS_AUTHORITY_ROLES:
                continue
            recipient = await self._users.get_by_user_id(m.user_id)
            await self._save_notif(
                new_notification(
                    user_id=m.user_id,
                    type="space_remote_invite_declined",
                    title=self._t(
                        "notification.space.remote_invite.declined",
                        locale=self._locale(recipient),
                        fallback="Your invite was declined.",
                    ),
                    link_url=None,
                )
            )

    # ── Momentum (§Momentum) ──────────────────────────────────────────

    async def on_moment_reaction_changed(
        self,
        event: MomentReactionChanged,
    ) -> None:
        """Notify the moment author when someone reacts.

        Cleared reactions (``emoji is None``) intentionally don't fire
        a notification — the author already saw the original. Self-
        reactions are silent. The recipient must live on this instance.
        """
        if event.emoji is None:
            return
        if event.reactor_user_id == event.author_user_id:
            return
        if await self._guardian_blocked(event.author_user_id, event.reactor_user_id):
            return
        author = await self._users.get_by_user_id(event.author_user_id)
        if author is None:
            return  # remote author — their instance fires the local notif
        reactor = await self._users.get_by_user_id(event.reactor_user_id)
        name = reactor.display_name if reactor is not None else event.reactor_user_id
        await self._save_notif(
            new_notification(
                user_id=event.author_user_id,
                type="moment_reacted",
                title=f"{name} reacted {event.emoji} to your moment",
                link_url=f"/momentum/{event.moment_id}",
            )
        )

    async def on_moment_created(self, event: MomentCreated) -> None:
        """Notify the parent author when this moment is a reply.

        Top-level moments don't fire a notification per recipient —
        broadcast posts are picked up via the WS frame and the inbox
        refresh; pinging every household member would be too noisy.
        """
        if event.parent_moment_id is None:
            return
        if not event.parent_author_user_id:
            return
        if event.parent_author_user_id == event.author_user_id:
            return  # author replied to their own thread
        if await self._guardian_blocked(
            event.parent_author_user_id, event.author_user_id
        ):
            return
        recipient = await self._users.get_by_user_id(event.parent_author_user_id)
        if recipient is None:
            return  # parent author lives on a peer instance
        replier = await self._users.get_by_user_id(event.author_user_id)
        name = replier.display_name if replier is not None else event.author_user_id
        await self._save_notif(
            new_notification(
                user_id=event.parent_author_user_id,
                type="moment_replied",
                title=f"{name} replied to your moment",
                link_url=f"/momentum/{event.parent_moment_id}",
            )
        )

    async def on_user_followed(self, event: UserFollowed) -> None:
        """Notify the followed user when someone starts following them."""
        if event.follower_user_id == event.followed_user_id:
            return
        if await self._guardian_blocked(event.followed_user_id, event.follower_user_id):
            return
        recipient = await self._users.get_by_user_id(event.followed_user_id)
        if recipient is None:
            return
        follower = await self._users.get_by_user_id(event.follower_user_id)
        name = follower.display_name if follower is not None else event.follower_user_id
        await self._save_notif(
            new_notification(
                user_id=event.followed_user_id,
                type="user_followed",
                title=f"{name} started following you",
                link_url="/momentum",
            )
        )

    # ── Space location feature (§23.8.6) ──────────────────────────────

    async def on_space_location_feature_enabled(
        self,
        event: SpaceLocationFeatureEnabled,
    ) -> None:
        """Nudge every space member (except the actor) to opt in.

        Fired only on the OFF→ON transition — see
        :class:`SpaceService.update_config`. The link points at the
        Personal Settings → Privacy → 'Space location sharing' panel
        so members can discover and manage their per-space sharing in
        one place.
        """
        members = await self._spaces.list_members(event.space_id)
        user_ids = [m.user_id for m in members if m.user_id != event.actor_user_id]
        for uid in user_ids:
            await self._save_notif(
                new_notification(
                    user_id=uid,
                    type="space_location_enabled",
                    title=f"Location sharing turned on in {event.space_name}",
                    link_url="/settings#privacy",
                )
            )
        await self._fan_push(
            user_ids,
            title=f"Location sharing turned on in {event.space_name}",
            click_url="/settings#privacy",
            space_id=event.space_id,
        )
