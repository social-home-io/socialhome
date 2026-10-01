"""Outbound federation for space-scoped tasks (§15 / §13).

Subscribes to :class:`TaskCreated` / :class:`TaskUpdated` /
:class:`TaskDeleted` domain events (via the
:class:`SpaceTaskService`). When the event carries a ``space_id``, we
fan out ``SPACE_TASK_*`` federation events to every peer instance
that's a member of the space. This complements the snapshot-sync
scheduler — co-members see edits within the same second, not the next
sync tick.

Household-scoped tasks (no ``space_id``) stay local, and an event that
carries ``origin_instance_id`` was applied from a peer's federation event
— re-broadcasting it would echo the peer's own edit back to the space.

The payload is the shared wire form
(:func:`socialhome.domain.task.task_to_wire_dict`). ``priority`` and
``labels`` (v_40) ride ungated inside the sealed payload: a v_39 receiver
ignores the unknown keys.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    TaskCreated,
    TaskDeleted,
    TaskListCreated,
    TaskListDeleted,
    TaskListUpdated,
    TaskUpdated,
)
from ..domain.federation import FederationEventType
from ..domain.task import TaskList, task_list_to_wire_dict, task_to_wire_dict
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)


class TaskFederationOutbound:
    """Publish space-scoped task mutations to paired peer instances."""

    __slots__ = ("_bus", "_federation")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
    ) -> None:
        self._bus = bus
        self._federation = federation_service

    def wire(self) -> None:
        self._bus.subscribe(TaskCreated, self._on_created)
        self._bus.subscribe(TaskUpdated, self._on_updated)
        self._bus.subscribe(TaskDeleted, self._on_deleted)
        self._bus.subscribe(TaskListCreated, self._on_list_created)
        self._bus.subscribe(TaskListUpdated, self._on_list_updated)
        self._bus.subscribe(TaskListDeleted, self._on_list_deleted)

    async def _on_created(self, event: TaskCreated) -> None:
        if event.space_id is None or event.origin_instance_id:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_TASK_CREATED,
            task_to_wire_dict(event.task, event.space_id),
        )

    async def _on_updated(self, event: TaskUpdated) -> None:
        if event.space_id is None or event.origin_instance_id:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_TASK_UPDATED,
            task_to_wire_dict(event.task, event.space_id),
        )

    async def _on_deleted(self, event: TaskDeleted) -> None:
        if event.space_id is None or event.origin_instance_id:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_TASK_DELETED,
            {
                "id": event.task_id,
                "list_id": event.list_id,
                "space_id": event.space_id,
            },
        )

    async def _on_list_created(self, event: TaskListCreated) -> None:
        if event.space_id is None or event.origin_instance_id:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_TASK_LIST_CREATED,
            task_list_to_wire_dict(
                TaskList(
                    id=event.list_id, name=event.name, created_by=event.created_by
                ),
                event.space_id,
            ),
        )

    async def _on_list_updated(self, event: TaskListUpdated) -> None:
        if event.space_id is None or event.origin_instance_id:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_TASK_LIST_UPDATED,
            {"id": event.list_id, "space_id": event.space_id, "name": event.name},
        )

    async def _on_list_deleted(self, event: TaskListDeleted) -> None:
        if event.space_id is None or event.origin_instance_id:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_TASK_LIST_DELETED,
            {"id": event.list_id, "space_id": event.space_id},
        )

    async def _fan_out(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        try:
            await self._federation.broadcast_to_space_members(
                space_id,
                event_type,
                payload,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "task-outbound: broadcast failed for space=%s: %s",
                space_id,
                exc,
            )
