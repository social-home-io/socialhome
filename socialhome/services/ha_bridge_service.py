"""HaBridgeService — forward domain events to Home Assistant for automations.

Only active when running as a HA App (``platform_adapter`` is the HA
adapter). Events are namespaced ``socialhome.*`` so HA users can write
automations like::

    automation:
      - alias: "Anna has a new direct message"
        trigger:
          - platform: event
            event_type: socialhome.dm_received
            event_data:
              username: anna
        action:
          - service: light.turn_on
            target:
              entity_id: light.desk_lamp

The bridge intentionally publishes a *narrow* set of events: the ones a
user might reasonably automate on. Bursty internal events (reaction
changes, every comment) are skipped — they would flood the HA event bus
and add noise.

Why personal events are opt-in
------------------------------
HA keeps every fired event in its database and shows it to anyone with
admin, logbook or automation access (HA REST API, ``POST
/api/events/<type>``: "Fires an event. The optional body is the event
data … Events are stored in the database, so be mindful of what you
fire" — https://contributors.home-assistant.io/core/internals/rest-api/).
So an event that concerns or names a person, or carries what someone
wrote, is **personal** (D14, owner decision 2026-10-28):

* it fires only for the person it is about, and only when that person
  opted in (``preferences_json["ha_automation_events"]``, Settings →
  Notifications);
* it never fires for a protected account
  (:attr:`ProtectedCapability.HA_AUTOMATIONS`), whatever is stored;
* a person who is not a local account (a remote author, a remote DM
  participant) is never the subject — there is no opt-in to read;
* it carries ids, a kind and an in-app link only, plus the subject's own
  ``user_id`` / ``username`` (what automations match on): never a
  display name, a title, a summary, a message, a status text, an
  amount, a zone, or anyone else's id.

A **household** event concerns a space, not a person, carries no
user-written content, and fires as before.

Every fired event is listed in :data:`HA_EVENT_SCOPES`; firing an
unlisted event type, or a personal one through the household path, is
refused at runtime (fail closed) and caught by a test.

========================================  =========  ==========================================
Event type                                Scope      Who "the person" is (one event each)
========================================  =========  ==========================================
``socialhome.notification_new``           personal   the notification's recipient
``socialhome.dm_received``                personal   each local recipient (never the sender)
``socialhome.post_created``               personal   the author (household feed post)
``socialhome.space_post_created``         personal   the author (a local author only)
``socialhome.space_post_moderated``       personal   the author whose post was removed
``socialhome.task_assigned``              personal   the assignee
``socialhome.task_completed``             personal   the person who completed it
``socialhome.task_deadline_due``          personal   each assignee (no assignee: nobody)
``socialhome.calendar_event_created``     personal   the event's creator
``socialhome.calendar_event_deleted``     personal   the person who deleted it (unknown: nobody)
``socialhome.bazaar_bid_placed``          personal   the seller (the bid is on their listing)
``socialhome.user_status_changed``        personal   the person whose status changed
``socialhome.presence_updated``           personal   the person whose presence changed
``socialhome.space_config_changed``       household  — (a space's settings; no person, no text)
========================================  =========  ==========================================
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

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
from ..domain.task import Task
from ..domain.user import User
from ..infrastructure.event_bus import EventBus
from .notification_links import (
    bazaar_listing_link,
    household_post_link,
    space_post_link,
    task_link,
)
from .user_preferences import parse_ha_automation_events

if TYPE_CHECKING:
    from ..repositories.user_repo import AbstractUserRepo
    from .child_protection_service import ChildProtectionService

log = logging.getLogger(__name__)


class HaEventScope(StrEnum):
    """Who an HA event may fire for — see the module docstring."""

    #: Concerns or names a person: only for that person, only when they
    #: opted in, never for a protected account; ids / kind / link only.
    PERSONAL = "personal"
    #: Concerns a space, names nobody, carries no user content.
    HOUSEHOLD = "household"


#: Every event type the bridge fires, classified. A new event must be
#: added here (and to the module docstring's table) or it never fires.
HA_EVENT_SCOPES: Final[Mapping[str, HaEventScope]] = MappingProxyType(
    {
        "socialhome.notification_new": HaEventScope.PERSONAL,
        "socialhome.dm_received": HaEventScope.PERSONAL,
        "socialhome.post_created": HaEventScope.PERSONAL,
        "socialhome.space_post_created": HaEventScope.PERSONAL,
        "socialhome.space_post_moderated": HaEventScope.PERSONAL,
        "socialhome.task_assigned": HaEventScope.PERSONAL,
        "socialhome.task_completed": HaEventScope.PERSONAL,
        "socialhome.task_deadline_due": HaEventScope.PERSONAL,
        "socialhome.calendar_event_created": HaEventScope.PERSONAL,
        "socialhome.calendar_event_deleted": HaEventScope.PERSONAL,
        "socialhome.bazaar_bid_placed": HaEventScope.PERSONAL,
        "socialhome.user_status_changed": HaEventScope.PERSONAL,
        "socialhome.presence_updated": HaEventScope.PERSONAL,
        "socialhome.space_config_changed": HaEventScope.HOUSEHOLD,
    }
)


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
        Looks up the person an event is about, for their opt-in. Without
        it no personal event fires (fail closed).
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

    # ─── Personal events ──────────────────────────────────────────────────

    async def _on_post_created(self, event: PostCreated) -> None:
        post = event.post
        await self._fire_personal(
            "socialhome.post_created",
            (post.author,),
            {
                "post_id": post.id,
                "type": str(post.type),
                "link_url": household_post_link(post.id),
            },
        )

    async def _on_space_post_created(self, event: SpacePostCreated) -> None:
        post = event.post
        await self._fire_personal(
            "socialhome.space_post_created",
            (post.author,),
            {
                "post_id": post.id,
                "space_id": event.space_id,
                "type": str(post.type),
                "link_url": space_post_link(event.space_id, post.id),
            },
        )

    async def _on_space_post_moderated(self, event: SpacePostModerated) -> None:
        # The outcome is about the author whose post was removed; the
        # moderator is not named.
        await self._fire_personal(
            "socialhome.space_post_moderated",
            (event.post.author,),
            {"space_id": event.space_id, "post_id": event.post.id},
        )

    async def _on_task_assigned(self, event: TaskAssigned) -> None:
        await self._fire_personal(
            "socialhome.task_assigned",
            (event.assigned_to,),
            self._task_data(event.task, event.space_id),
        )

    async def _on_task_completed(self, event: TaskCompleted) -> None:
        await self._fire_personal(
            "socialhome.task_completed",
            (event.completed_by,),
            self._task_data(event.task, event.space_id),
        )

    async def _on_task_deadline(self, event: TaskDeadlineDue) -> None:
        await self._fire_personal(
            "socialhome.task_deadline_due",
            event.task.assignees,
            {
                **self._task_data(event.task, None),
                # The day this event fires on — no more than the event says.
                "due_date": event.due_date.isoformat(),
            },
        )

    async def _on_user_status(self, event: UserStatusChanged) -> None:
        # No emoji, no text: both are what the person wrote.
        await self._fire_personal(
            "socialhome.user_status_changed",
            (event.user_id,),
            {"status_set": event.status is not None},
        )

    async def _on_dm_message_created(self, event: DmMessageCreated) -> None:
        if event.system_scope is not None:
            # The household / a space chat is not a DM.
            return
        # One event per local recipient who opted in; never the sender's
        # name, never the content (§25.3).
        await self._fire_personal(
            "socialhome.dm_received",
            event.recipient_user_ids,
            {
                "conversation_id": event.conversation_id,
                "message_id": event.message_id,
                "message_type": event.message_type,
                "is_group": len(event.recipient_user_ids) > 1,
                # The DM thread (notification_service's _conversation_link).
                "link_url": f"/dms/{event.conversation_id}",
            },
        )

    async def _on_calendar_event_created(self, event: CalendarEventCreated) -> None:
        cal = event.event
        await self._fire_personal(
            "socialhome.calendar_event_created",
            (cal.created_by,),
            {"event_id": cal.id, "calendar_id": cal.calendar_id},
        )

    async def _on_calendar_event_deleted(self, event: CalendarEventDeleted) -> None:
        if event.actor_user_id is None:
            return  # nobody known to be the subject: fire for nobody
        await self._fire_personal(
            "socialhome.calendar_event_deleted",
            (event.actor_user_id,),
            {"event_id": event.event_id, "space_id": event.space_id},
        )

    async def _on_bazaar_bid_placed(self, event: BazaarBidPlaced) -> None:
        # The seller hears a bid landed on their listing — not who bid,
        # not how much.
        await self._fire_personal(
            "socialhome.bazaar_bid_placed",
            (event.seller_user_id,),
            {
                "listing_post_id": event.listing_post_id,
                "bid_id": event.bid_id,
                "space_id": event.space_id,
                "link_url": bazaar_listing_link(event.listing_post_id),
            },
        )

    async def _on_presence_updated(self, event: PresenceUpdated) -> None:
        # §25: no coordinates; no zone name either — it says where the
        # person is. ``state`` (home / away / zone / unavailable) only.
        data = {"state": event.state}
        if event.user_id is not None:
            await self._fire_personal(
                "socialhome.presence_updated", (event.user_id,), data
            )
            return
        if self._users is None:
            return
        user = await self._eligible(await self._users.get(event.username))
        if user is not None:
            await self._fire_for(user, "socialhome.presence_updated", data)

    async def _on_notification_created(self, event: NotificationCreated) -> None:
        if event.bumped:
            return  # the same unread row again: automations heard it once
        # D14: no title, no text — they name people and carry chat, task
        # and event content. An automation gets what it needs to react
        # (who, what kind, which row) and the link to open.
        await self._fire_personal(
            "socialhome.notification_new",
            (event.user_id,),
            {
                "notification_id": event.notification_id,
                "type": event.type,
                "link_url": event.link_url,
            },
        )

    # ─── Household events ─────────────────────────────────────────────────

    async def _on_space_config_changed(self, event: SpaceConfigChanged) -> None:
        # The change's payload (which can name members) stays out.
        await self._fire_household(
            "socialhome.space_config_changed",
            {
                "space_id": event.space_id,
                "event_type": event.event_type,
                "sequence": event.sequence,
            },
        )

    # ─── Internals ────────────────────────────────────────────────────────

    @staticmethod
    def _task_data(task: Task, space_id: str | None) -> dict[str, Any]:
        return {
            "task_id": task.id,
            "list_id": task.list_id,
            "space_id": space_id,
            "link_url": task_link(task, space_id),
        }

    async def _fire_personal(
        self, event_type: str, user_ids: Iterable[str], data: dict[str, Any]
    ) -> None:
        """Fire ``event_type`` once for each of ``user_ids`` who opted in
        and is not protected, with their own ``user_id`` / ``username``."""
        if HA_EVENT_SCOPES.get(event_type) is not HaEventScope.PERSONAL:
            log.error("ha_bridge: %s is not a classified personal event", event_type)
            return
        seen: set[str] = set()
        for user_id in user_ids:
            if user_id in seen:
                continue
            seen.add(user_id)
            user = await self._opted_in_user(user_id)
            if user is not None:
                await self._fire_for(user, event_type, data)

    async def _fire_for(
        self, user: User, event_type: str, data: dict[str, Any]
    ) -> None:
        await self._fire(
            event_type, {"user_id": user.user_id, "username": user.username, **data}
        )

    async def _fire_household(self, event_type: str, data: dict[str, Any]) -> None:
        if HA_EVENT_SCOPES.get(event_type) is not HaEventScope.HOUSEHOLD:
            log.error("ha_bridge: %s is not a classified household event", event_type)
            return
        await self._fire(event_type, data)

    async def _opted_in_user(self, user_id: str) -> User | None:
        """The local person ``user_id`` when they opted into HA automation
        events and are not a protected account; ``None`` otherwise (D14)."""
        if self._users is None:
            return None
        return await self._eligible(await self._users.get_by_user_id(user_id))

    async def _eligible(self, user: User | None) -> User | None:
        if user is None or not parse_ha_automation_events(user.preferences_json):
            return None
        if self._child_protection is not None and (
            await self._child_protection.is_protected(user.user_id)
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
