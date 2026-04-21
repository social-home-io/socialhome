"""Outbound federation for space-scoped tasks (§15 / §13).

Subscribes to :class:`TaskCreated` / :class:`TaskUpdated` /
:class:`TaskDeleted` domain events (via the
:class:`SpaceTaskService`). When the event carries a ``space_id``, we
fan out ``SPACE_TASK_*`` federation events to every peer instance
that's a member of the space. This complements the snapshot-sync
scheduler — co-members see edits within the same second, not the next
sync tick.

Household-scoped tasks (no ``space_id``) stay local.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from ..domain.events import TaskCreated, TaskDeleted, TaskUpdated
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class TaskFederationOutbound:
    """Publish space-scoped task mutations to paired peer instances."""

    __slots__ = ("_bus", "_federation", "_space_repo")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._space_repo = space_repo

    def wire(self) -> None:
        self._bus.subscribe(TaskCreated, self._on_created)
        self._bus.subscribe(TaskUpdated, self._on_updated)
        self._bus.subscribe(TaskDeleted, self._on_deleted)

    async def _on_created(self, event: TaskCreated) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_TASK_CREATED,
            _task_payload(event.task, event.space_id),
        )

    async def _on_updated(self, event: TaskUpdated) -> None:
        if event.space_id is None:
            return
        await self._fan_out(
            event.space_id,
            FederationEventType.SPACE_TASK_UPDATED,
            _task_payload(event.task, event.space_id),
        )

    async def _on_deleted(self, event: TaskDeleted) -> None:
        if event.space_id is None:
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

    async def _fan_out(
        self,
        space_id: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        try:
            peers = await self._space_repo.list_member_instances(space_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("task-outbound: list peers failed: %s", exc)
            return
        own = getattr(self._federation, "_own_instance_id", "")
        for instance_id in peers:
            if instance_id == own or not instance_id:
                continue
            try:
                await self._federation.send_event(
                    to_instance_id=instance_id,
                    event_type=event_type,
                    payload=payload,
                    space_id=space_id,
                )
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "task-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                )


def _task_payload(task, space_id: str) -> dict:
    d = asdict(task)
    # Flatten datetime / date → iso so JSON serialisation survives the
    # envelope encoder downstream.
    for k in ("created_at", "updated_at", "due_date"):
        v = d.get(k)
        if v is not None and hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    if d.get("status") is not None and hasattr(d["status"], "value"):
        d["status"] = d["status"].value
    # ``RecurrenceRule`` is the only nested dataclass — shallow-serialise it.
    rec = d.get("recurrence")
    if rec and hasattr(rec, "rrule"):
        d["recurrence"] = {
            "rrule": rec.rrule,
            "last_spawned_at": (
                rec.last_spawned_at.isoformat() if rec.last_spawned_at else None
            ),
        }
    d["space_id"] = space_id
    return d
