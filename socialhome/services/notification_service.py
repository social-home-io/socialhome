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
| CommentAdded         | Post author (if not the commenter)    | "{commenter} commented on your post"           |
| TaskAssigned         | Each assignee (not the assigner)      | "You were assigned: {task title}"              |
| TaskDeadlineDue      | All assignees                         | "Task due today: {task title}"                 |
| SpacePostCreated     | Space members with notifications on   | "{author} posted in {space}"                   |
| SpaceModerationQueued| Space admins                          | "New content pending review in {space}"        |

Body is intentionally omitted for privacy-sensitive events (DMs,
location, UGC content) per §25.3.
"""

from __future__ import annotations

import logging

from ..domain.events import (
    BazaarBidPlaced,
    BazaarListingExpired,
    BazaarOfferAccepted,
    BazaarOfferRejected,
    CalendarEventCreated,
    CalendarEventDeleted,
    CalendarEventUpdated,
    CommentAdded,
    EventReminderDue,
    DmContactRequested,
    DmMessageCreated,
    NotificationCreated,
    PostCreated,
    RemoteSpaceInviteAccepted,
    RemoteSpaceInviteDeclined,
    SpaceJoinApproved,
    SpaceJoinDenied,
    SpaceJoinRequested,
    SpaceMemberJoined,
    SpaceModerationQueued,
    SpacePostCreated,
    SpacePostModerated,
    TaskAssigned,
    TaskCompleted,
    TaskDeadlineDue,
)
from ..i18n import Catalog
from ..infrastructure.event_bus import EventBus
from ..repositories.notification_repo import (
    AbstractNotificationRepo,
    new_notification,
)
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .push_service import PushPayload


log = logging.getLogger(__name__)


class NotificationService:
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
        "_notifs",
        "_users",
        "_spaces",
        "_bus",
        "_i18n",
        "_push",
        "_adapter",
        "_calendar_repo",
    )

    def __init__(
        self,
        notification_repo: AbstractNotificationRepo,
        user_repo: AbstractUserRepo,
        space_repo: AbstractSpaceRepo,
        bus: EventBus,
        *,
        i18n: Catalog | None = None,
    ) -> None:
        self._notifs = notification_repo
        self._users = user_repo
        self._spaces = space_repo
        self._bus = bus
        self._i18n = i18n
        self._push = None  # attach_push_service(PushService)
        self._adapter = None  # attach_platform_adapter(PlatformAdapter)
        self._calendar_repo = None  # attach_calendar_repo(...) Phase D

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

    def attach_platform_adapter(self, adapter) -> None:
        """Attach the :class:`PlatformAdapter` so push notifications also
        reach HA mobile apps (`notify.mobile_app_<user>`) or the
        standalone ``notify_endpoint``. Optional — if both this adapter
        and the Web Push ``PushService`` are wired, we fan out to both
        so the user gets the notification on every registered surface.
        """
        self._adapter = adapter

    async def _save_notif(self, note):
        """Persist + publish ``NotificationCreated`` + fire title-only
        pushes to every registered surface (Web Push + HA mobile app).

        Per §25.3 we never put the body on the wire; subscribers
        translate the title and tap-open the app to see the full row.
        """
        saved = await self._notifs.save(note)
        await self._bus.publish(
            NotificationCreated(
                user_id=saved.user_id,
                notification_id=saved.id,
                type=saved.type,
                title=saved.title,
            )
        )
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
        return self._i18n.gettext(key, locale=locale, **fmt)

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
        self._bus.subscribe(DmMessageCreated, self.on_dm_message_created)
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

    # ── Handlers ───────────────────────────────────────────────────────

    async def on_post_created(self, event: PostCreated) -> None:
        """Notify every active household member except the author."""
        author_id = event.post.author
        users = await self._users.list_active()
        author = await self._users.get_by_user_id(author_id)
        name = author.display_name if author else "Someone"
        for user in users:
            if user.user_id == author_id:
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
        """Notify the post author when someone else comments."""
        # Resolve post author from the post_id → feed_posts.author
        commenter_id = event.comment.author
        # The event only carries post_id, not the post author. To resolve
        # properly we'd query the post. For v1 we keep this simple:
        # notify everyone except the commenter. This is slightly noisy but
        # ensures the post author always gets notified.
        commenter = await self._users.get_by_user_id(commenter_id)
        name = commenter.display_name if commenter else "Someone"
        users = await self._users.list_active()
        for user in users:
            if user.user_id == commenter_id:
                continue
            await self._save_notif(
                new_notification(
                    user_id=user.user_id,
                    type="comment_added",
                    title=f"{name} commented on a post",
                    link_url=f"/post/{event.post_id}",
                )
            )

    async def on_task_assigned(self, event: TaskAssigned) -> None:
        """Notify the assignee (unless they assigned themselves)."""
        if event.task.created_by == event.assigned_to:
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
        """Push only — the in-app notification is conversation-list-based.

        §25.3: the payload carries no body. Recipients see only the
        sender's display name; they have to open the app to read the
        message.
        """
        if not event.recipient_user_ids:
            return
        title = f"{event.sender_display_name} messaged you"
        await self._fan_push(
            event.recipient_user_ids,
            title=title,
            tag=f"dm:{event.conversation_id}",
            click_url=f"/dms/{event.conversation_id}",
        )

    async def on_dm_contact_requested(self, event: DmContactRequested) -> None:
        """A user wants to start a DM — notify the recipient + push."""
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

        Honours per-member :table:`space_notif_prefs`: ``muted`` skips the
        member entirely, ``mentions`` only fires if the member's user_id
        is in ``event.mentions``.
        """
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        author_id = event.post.author
        author = await self._users.get_by_user_id(author_id)
        name = author.display_name if author else "Someone"
        mentioned = {m.user_id for m in event.mentions if m.user_id}
        members = await self._spaces.list_members(event.space_id)
        for member in members:
            if member.user_id == author_id:
                continue
            level = await self._notifs.get_space_notif_level(
                user_id=member.user_id,
                space_id=event.space_id,
            )
            if level == "muted":
                continue
            if level == "mentions" and member.user_id not in mentioned:
                continue
            recipient = await self._users.get_by_user_id(member.user_id)
            await self._save_notif(
                new_notification(
                    user_id=member.user_id,
                    type="space_post_created",
                    title=self._t(
                        "notification.space.post.created",
                        locale=self._locale(recipient),
                        fallback="{author} posted in {space_name}",
                        author=name,
                        space_name=space.name,
                    ),
                    link_url=f"/spaces/{event.space_id}",
                )
            )

    async def on_moderation_queued(self, event: SpaceModerationQueued) -> None:
        """Notify space admins/owners that content is pending review."""
        space = await self._spaces.get(event.item.space_id)
        if space is None:
            return
        members = await self._spaces.list_members(event.item.space_id)
        for member in members:
            if member.role in ("owner", "admin"):
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
                        link_url=f"/spaces/{event.item.space_id}/moderation",
                    )
                )

    async def on_calendar_event_created(
        self,
        event: CalendarEventCreated,
    ) -> None:
        """Notify household members about a new calendar event."""
        cal_event = event.event
        users = await self._users.list_active()
        for user in users:
            if user.user_id == cal_event.created_by:
                continue
            await self._save_notif(
                new_notification(
                    user_id=user.user_id,
                    type="calendar_event_created",
                    title=self._t(
                        "notification.calendar.created",
                        locale=self._locale(user),
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
            localized = self._t(
                "notification.calendar.cancelled",
                locale=self._locale(recipient),
                fallback="Event cancelled: {summary}",
                summary=event.summary or "(removed)",
            ) or title
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
            if r.status in (
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
        """Notify task assignees when a task is completed."""
        task = event.task
        completed_by = event.completed_by
        completer = await self._users.get_by_user_id(completed_by)
        name = completer.display_name if completer else "Someone"
        for uid in getattr(task, "assignees", ()):
            if uid == completed_by:
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
                    link_url=f"/tasks/{task.list_id}",
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
            if member.role not in ("owner", "admin"):
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
            if m.role not in ("owner", "admin"):
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
            if m.role not in ("owner", "admin"):
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
