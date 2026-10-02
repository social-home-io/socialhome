"""TaskFederationOutbound — per-event SPACE_TASK_* fan-out."""

from __future__ import annotations

import copy
from datetime import date, datetime, timezone

import pytest

from socialhome.domain.events import (
    TaskCreated,
    TaskDeleted,
    TaskListCreated,
    TaskListDeleted,
    TaskListUpdated,
    TaskUpdated,
)
from socialhome.domain.federation import FederationEventType
from socialhome.domain.task import Task, TaskPriority, TaskStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.moderation_release import release_scope
from socialhome.services.task_federation_outbound import (
    TaskFederationOutbound,
)


class _FakeFed:
    """Records `broadcast_to_space_members` calls.

    F2a switched task outbound from per-peer `send_event` to mesh-
    routed broadcast so members behind a relay receive task mutations
    too. The broadcast helper handles the member-list lookup + the
    per-peer `send_with_mesh_fallback` internally.
    """

    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, FederationEventType, dict]] = []

    async def broadcast_to_space_members(
        self,
        space_id,
        event_type,
        payload,
        **kwargs,
    ):
        self.broadcasts.append((space_id, event_type, payload))


def _task(tid: str) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=tid,
        list_id="L",
        title=f"t{tid}",
        status=TaskStatus.TODO,
        position=0,
        created_by="u",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFed()
    out = TaskFederationOutbound(bus=bus, federation_service=fed)
    out.wire()
    return bus, fed


async def test_household_task_is_not_federated(env):
    bus, fed = env
    await bus.publish(TaskCreated(task=_task("t1"), space_id=None))
    assert fed.broadcasts == []


async def test_space_task_created_broadcasts_with_payload(env):
    bus, fed = env
    await bus.publish(TaskCreated(task=_task("t1"), space_id="sp-A"))
    assert len(fed.broadcasts) == 1
    space_id, event_type, payload = fed.broadcasts[0]
    assert space_id == "sp-A"
    assert event_type is FederationEventType.SPACE_TASK_CREATED
    assert payload["id"] == "t1"
    assert payload["space_id"] == "sp-A"
    assert payload["status"] == "todo"


async def test_space_task_deleted_minimal_payload(env):
    bus, fed = env
    await bus.publish(
        TaskDeleted(task_id="t1", list_id="L", space_id="sp-A"),
    )
    assert len(fed.broadcasts) == 1
    space_id, event_type, payload = fed.broadcasts[0]
    assert space_id == "sp-A"
    assert event_type is FederationEventType.SPACE_TASK_DELETED
    assert payload == {"id": "t1", "list_id": "L", "space_id": "sp-A"}


async def test_space_task_updated_event_type(env):
    bus, fed = env
    await bus.publish(TaskUpdated(task=_task("t1"), space_id="sp-B"))
    assert len(fed.broadcasts) == 1
    space_id, event_type, _payload = fed.broadcasts[0]
    assert space_id == "sp-B"
    assert event_type is FederationEventType.SPACE_TASK_UPDATED


async def test_payload_carries_priority_labels_and_due_date(env):
    bus, fed = env
    t = copy.replace(
        _task("t1"),
        priority=TaskPriority.HIGH,
        labels=("Garden",),
        due_date=date(2026, 10, 3),
        archived_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    await bus.publish(TaskUpdated(task=t, space_id="sp-A"))
    payload = fed.broadcasts[0][2]
    assert payload["priority"] == "high"
    assert payload["labels"] == ["Garden"]
    assert payload["due_date"] == "2026-10-03"
    assert payload["archived_at"] == "2026-09-01T00:00:00+00:00"


async def test_payload_always_has_priority_key(env):
    bus, fed = env
    await bus.publish(TaskCreated(task=_task("t1"), space_id="sp-A"))
    payload = fed.broadcasts[0][2]
    assert "priority" in payload and payload["priority"] is None
    assert payload["labels"] == []


@pytest.mark.parametrize(
    "event",
    [
        TaskCreated(task=_task("t1"), space_id="sp-A", origin_instance_id="peer"),
        TaskUpdated(task=_task("t1"), space_id="sp-A", origin_instance_id="peer"),
        TaskDeleted(
            task_id="t1", list_id="L", space_id="sp-A", origin_instance_id="peer"
        ),
    ],
)
async def test_events_applied_from_a_peer_are_not_echoed(env, event):
    bus, fed = env
    await bus.publish(event)
    assert fed.broadcasts == []


# ─── Task lists (v_40) ───────────────────────────────────────────────────


async def test_space_list_events_broadcast_sealed_payloads(env):
    bus, fed = env
    await bus.publish(
        TaskListCreated(list_id="L1", name="Chores", space_id="sp-A", created_by="u")
    )
    await bus.publish(TaskListUpdated(list_id="L1", name="Jobs", space_id="sp-A"))
    await bus.publish(TaskListDeleted(list_id="L1", space_id="sp-A"))
    assert [(s, t) for s, t, _ in fed.broadcasts] == [
        ("sp-A", FederationEventType.SPACE_TASK_LIST_CREATED),
        ("sp-A", FederationEventType.SPACE_TASK_LIST_UPDATED),
        ("sp-A", FederationEventType.SPACE_TASK_LIST_DELETED),
    ]
    assert fed.broadcasts[0][2] == {
        "id": "L1",
        "space_id": "sp-A",
        "name": "Chores",
        "created_by": "u",
    }
    assert fed.broadcasts[1][2] == {"id": "L1", "space_id": "sp-A", "name": "Jobs"}
    assert fed.broadcasts[2][2] == {"id": "L1", "space_id": "sp-A"}


@pytest.mark.parametrize(
    "event",
    [
        TaskListCreated(list_id="L1", name="x"),
        TaskListUpdated(list_id="L1", name="x"),
        TaskListDeleted(list_id="L1"),
        TaskListCreated(
            list_id="L1", name="x", space_id="sp-A", origin_instance_id="peer"
        ),
        TaskListUpdated(
            list_id="L1", name="x", space_id="sp-A", origin_instance_id="peer"
        ),
        TaskListDeleted(list_id="L1", space_id="sp-A", origin_instance_id="peer"),
    ],
)
async def test_household_or_peer_applied_list_events_stay_local(env, event):
    bus, fed = env
    await bus.publish(event)
    assert fed.broadcasts == []


async def test_every_write_carries_its_actor(env):
    """v_42: receivers judge a write against the space's ``tasks`` access
    level by who made it, so the actor rides inside the sealed payload."""
    bus, fed = env
    await bus.publish(
        TaskCreated(task=_task("t1"), space_id="sp-A", actor_user_id="u-a")
    )
    await bus.publish(
        TaskUpdated(task=_task("t1"), space_id="sp-A", actor_user_id="u-b")
    )
    await bus.publish(
        TaskDeleted(task_id="t1", list_id="L", space_id="sp-A", actor_user_id="u-c")
    )
    await bus.publish(
        TaskListCreated(
            list_id="L",
            name="n",
            space_id="sp-A",
            created_by="u-d",
            actor_user_id="u-d",
        )
    )
    await bus.publish(
        TaskListUpdated(list_id="L", name="n2", space_id="sp-A", actor_user_id="u-e")
    )
    await bus.publish(
        TaskListDeleted(list_id="L", space_id="sp-A", actor_user_id="u-f")
    )
    assert [p["actor_user_id"] for _s, _t, p in fed.broadcasts] == [
        "u-a",
        "u-b",
        "u-c",
        "u-d",
        "u-e",
        "u-f",
    ]


async def test_a_moderation_release_carries_the_approval_block(env):
    """v_43: every write a release emits names the item + approver inside
    the sealed payload; a plain write never does."""
    bus, fed = env
    await bus.publish(TaskCreated(task=_task("t0"), space_id="sp-A"))
    with release_scope("item-1", "u-mod"):
        await bus.publish(TaskCreated(task=_task("t1"), space_id="sp-A"))
        await bus.publish(TaskUpdated(task=_task("t1"), space_id="sp-A"))
        await bus.publish(TaskDeleted(task_id="t1", list_id="L", space_id="sp-A"))
        await bus.publish(
            TaskListCreated(list_id="L2", name="n", space_id="sp-A", created_by="u")
        )
        await bus.publish(TaskListUpdated(list_id="L2", name="m", space_id="sp-A"))
        await bus.publish(TaskListDeleted(list_id="L2", space_id="sp-A"))
    assert "moderation" not in fed.broadcasts[0][2]
    block = {"item_id": "item-1", "approved_by": "u-mod"}
    assert [p.get("moderation") for _s, _t, p in fed.broadcasts[1:]] == [block] * 6
