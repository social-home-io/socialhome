"""HaBridgeService — forward domain events to Home Assistant for automations.

Only active when running as a HA App (``platform_adapter`` is the HA
adapter). Events are namespaced ``socialhome.*`` so HA users can write
automations like::

    automation:
      - alias: "Notify on new household post"
        trigger:
          - platform: event
            event_type: socialhome.post_created
        action:
          - service: notify.family
            data:
              message: "New post in the family feed"

The bridge intentionally publishes a *narrow* set of events: the ones a
user might reasonably automate on. Bursty internal events (reaction
changes, every comment) are skipped — they would flood the HA event bus
and add noise. If a user later needs more, the list can be extended.

``socialhome.notification_new`` is personal (D14): it fires only for a
person who opted in (``preferences_json["ha_automation_events"]``, Settings
→ Notifications), never for a protected account, and carries no title or
text — ``{user_id, username, notification_id, type, link_url}``. HA keeps
fired events in its database and shows them to anyone with admin,
logbook or automation access (HA REST API, ``POST /api/events/<type>``:
"Fires an event. The optional body is the event data … Events are stored
in the database, so be mindful of what you fire" —
https://contributors.home-assistant.io/core/internals/rest-api/).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..domain.events import (
    BazaarBidPlaced,
    CalendarEventCreated,
    CalendarEventDeleted,
    DmMessageCreated,
    NotificationCreated,
    PostCreated,
    PresenceUpdated,
    SpaceConfigChanged,
    SpacePostCreated,
    SpacePostModerated,
    TaskAssigned,
    TaskCompleted,
    TaskDeadlineDue,
    UserStatusChanged,
)
from ..infrastructure.event_bus import EventBus
from .user_preferences import parse_ha_automation_events

if TYPE_CHECKING:
    from ..repositories.user_repo import AbstractUserRepo
    from .child_protection_service import ChildProtectionService

log = logging.getLogger(__name__)


class HaBridgeService:
    """Subscribes to domain events and fires HA events on the HA bus.

    Parameters
    ----------
    bus:
        The shared in-process event bus.
    ha_adapter:
        Any object that exposes ``async fire_event(event_type, data)``.
        ``HomeAssistantAdapter`` matches that shape; other adapters can
        too if they want HA compatibility.
    users:
        Looks up a notification's recipient for their opt-in. Without it
        no ``socialhome.notification_new`` fires (fail closed).
    child_protection:
        A protected account never reaches HA's event bus, whatever its
        preferences say.
    """

    __slots__ = ("_adapter", "_bus", "_child_protection", "_users")

    def __init__(
        self,
        bus: EventBus,
        ha_adapter,
        *,
        users: AbstractUserRepo | None = None,
        child_protection: ChildProtectionService | None = None,
    ) -> None:
        self._bus = bus
        self._adapter = ha_adapter
        self._users = users
        self._child_protection = child_protection

    def wire(self) -> None:
        self._bus.subscribe(PostCreated, self._on_post_created)
        self._bus.subscribe(SpacePostCreated, self._on_space_post_created)
        self._bus.subscribe(TaskAssigned, self._on_task_assigned)
        self._bus.subscribe(TaskCompleted, self._on_task_completed)
        self._bus.subscribe(TaskDeadlineDue, self._on_task_deadline)
        self._bus.subscribe(UserStatusChanged, self._on_user_status)
        self._bus.subscribe(DmMessageCreated, self._on_dm_message_created)
        self._bus.subscribe(CalendarEventCreated, self._on_calendar_event_created)
        self._bus.subscribe(CalendarEventDeleted, self._on_calendar_event_deleted)
        self._bus.subscribe(SpacePostModerated, self._on_space_post_moderated)
        self._bus.subscribe(SpaceConfigChanged, self._on_space_config_changed)
        self._bus.subscribe(PresenceUpdated, self._on_presence_updated)
        self._bus.subscribe(BazaarBidPlaced, self._on_bazaar_bid_placed)
        self._bus.subscribe(NotificationCreated, self._on_notification_created)

    # ─── Event handlers ───────────────────────────────────────────────────

    async def _on_post_created(self, event: PostCreated) -> None:
        await self._fire(
            "socialhome.post_created",
            {
                "post_id": event.post.id,
                "author": event.post.author,
                "type": str(event.post.type),
            },
        )

    async def _on_space_post_created(self, event: SpacePostCreated) -> None:
        await self._fire(
            "socialhome.space_post_created",
            {
                "post_id": event.post.id,
                "space_id": event.space_id,
                "author": event.post.author,
            },
        )

    async def _on_task_assigned(self, event: TaskAssigned) -> None:
        await self._fire(
            "socialhome.task_assigned",
            {
                "task_id": event.task.id,
                "assigned_to": event.assigned_to,
                "title": event.task.title,
            },
        )

    async def _on_task_completed(self, event: TaskCompleted) -> None:
        await self._fire(
            "socialhome.task_completed",
            {
                "task_id": event.task.id,
                "completed_by": event.completed_by,
            },
        )

    async def _on_task_deadline(self, event: TaskDeadlineDue) -> None:
        await self._fire(
            "socialhome.task_deadline_due",
            {
                "task_id": event.task.id,
                "due_date": event.due_date.isoformat(),
            },
        )

    async def _on_user_status(self, event: UserStatusChanged) -> None:
        await self._fire(
            "socialhome.user_status_changed",
            {
                "user_id": event.user_id,
                "emoji": event.status.emoji if event.status else None,
                "text": event.status.text if event.status else None,
            },
        )

    async def _on_dm_message_created(self, event: DmMessageCreated) -> None:
        if event.system_scope is not None:
            # The household / a space chat is not a DM.
            return
        # §25.3 privacy: NO message content in HA events.
        await self._fire(
            "socialhome.dm_received",
            {
                "conversation_id": event.conversation_id,
                "sender_display_name": event.sender_display_name,
                "is_group": len(event.recipient_user_ids) > 1,
            },
        )

    async def _on_calendar_event_created(self, event: CalendarEventCreated) -> None:
        await self._fire(
            "socialhome.calendar_event_created",
            {
                "event_id": event.event.id,
                "summary": event.event.summary,
                "start": event.event.start.isoformat() if event.event.start else None,
                "end": event.event.end.isoformat() if event.event.end else None,
                "created_by": event.event.created_by,
            },
        )

    async def _on_calendar_event_deleted(self, event: CalendarEventDeleted) -> None:
        await self._fire(
            "socialhome.calendar_event_deleted",
            {
                "event_id": event.event_id,
            },
        )

    async def _on_space_post_moderated(self, event: SpacePostModerated) -> None:
        await self._fire(
            "socialhome.space_post_moderated",
            {
                "space_id": event.space_id,
                "post_id": event.post.id,
                "moderated_by": event.moderated_by,
            },
        )

    async def _on_space_config_changed(self, event: SpaceConfigChanged) -> None:
        await self._fire(
            "socialhome.space_config_changed",
            {
                "space_id": event.space_id,
                "event_type": event.event_type,
                "sequence": event.sequence,
            },
        )

    async def _on_presence_updated(self, event: PresenceUpdated) -> None:
        # §25 privacy: NO GPS coordinates in HA events.
        await self._fire(
            "socialhome.presence_updated",
            {
                "username": event.username,
                "state": event.state,
                "zone_name": event.zone_name,
            },
        )

    async def _on_bazaar_bid_placed(self, event: BazaarBidPlaced) -> None:
        await self._fire(
            "socialhome.bazaar_bid_placed",
            {
                "listing_post_id": event.listing_post_id,
                "bidder_user_id": event.bidder_user_id,
                "amount": event.amount,
            },
        )

    async def _on_notification_created(self, event: NotificationCreated) -> None:
        if event.bumped:
            return  # the same unread row again: automations heard it once
        user = await self._opted_in_user(event.user_id)
        if user is None:
            return
        # D14: no title, no text — they name people and carry chat, task
        # and event content. An automation gets what it needs to react
        # (who, what kind, which row) and the link to open.
        await self._fire(
            "socialhome.notification_new",
            {
                "user_id": event.user_id,
                "username": user.username,
                "notification_id": event.notification_id,
                "type": event.type,
                "link_url": event.link_url,
            },
        )

    # ─── Internals ────────────────────────────────────────────────────────

    async def _opted_in_user(self, user_id: str):
        """The recipient when they opted into HA automation events and are
        not a protected account; ``None`` otherwise (D14)."""
        if self._users is None:
            return None
        user = await self._users.get_by_user_id(user_id)
        if user is None or not parse_ha_automation_events(user.preferences_json):
            return None
        if self._child_protection is not None and (
            await self._child_protection.is_protected(user_id)
        ):
            return None
        return user

    async def _fire(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            ok = await self._adapter.fire_event(event_type, data)
        except Exception as exc:  # defensive
            log.warning(
                "ha_bridge: fire_event %s failed: %s", event_type, exc, exc_info=True
            )
            return
        if ok is False:
            # POST /api/events/<type> answered non-2xx or was unreachable.
            log.warning("ha_bridge: Home Assistant did not accept event %s", event_type)
