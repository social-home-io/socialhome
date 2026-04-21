"""TaskFederationOutbound — per-event SPACE_TASK_* fan-out."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from social_home.domain.events import TaskCreated, TaskDeleted, TaskUpdated
from social_home.domain.federation import FederationEventType
from social_home.domain.task import Task, TaskStatus
from social_home.infrastructure.event_bus import EventBus
from social_home.services.task_federation_outbound import (
    TaskFederationOutbound,
)


class _FakeFed:
    def __init__(self, own: str = "own-inst") -> None:
        self._own_instance_id = own
        self.sent: list[tuple[str, FederationEventType, dict]] = []

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append((to_instance_id, event_type, payload))


class _FakeSpaceRepo:
    def __init__(self, members: dict[str, list[str]]) -> None:
        self._m = members

    async def list_member_instances(self, space_id):
        return list(self._m.get(space_id, []))


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
    repo = _FakeSpaceRepo(
        {
            "sp-A": ["own-inst", "peer-1", "peer-2"],
            "sp-B": ["peer-3"],
        }
    )
    out = TaskFederationOutbound(
        bus=bus,
        federation_service=fed,
        space_repo=repo,
    )
    out.wire()
    return bus, fed


async def test_household_task_is_not_federated(env):
    bus, fed = env
    await bus.publish(TaskCreated(task=_task("t1"), space_id=None))
    assert fed.sent == []


async def test_space_task_created_fanouts_to_peers_excluding_self(env):
    bus, fed = env
    await bus.publish(TaskCreated(task=_task("t1"), space_id="sp-A"))
    recipients = [r[0] for r in fed.sent]
    assert recipients == ["peer-1", "peer-2"]
    assert {r[1] for r in fed.sent} == {FederationEventType.SPACE_TASK_CREATED}
    payload = fed.sent[0][2]
    assert payload["id"] == "t1"
    assert payload["space_id"] == "sp-A"
    assert payload["status"] == "todo"


async def test_space_task_deleted_minimal_payload(env):
    bus, fed = env
    await bus.publish(
        TaskDeleted(
            task_id="t1",
            list_id="L",
            space_id="sp-A",
        )
    )
    assert len(fed.sent) == 2
    for _to, event_type, payload in fed.sent:
        assert event_type is FederationEventType.SPACE_TASK_DELETED
        assert payload == {"id": "t1", "list_id": "L", "space_id": "sp-A"}


async def test_space_task_updated_event_type(env):
    bus, fed = env
    await bus.publish(TaskUpdated(task=_task("t1"), space_id="sp-B"))
    assert [r[1] for r in fed.sent] == [FederationEventType.SPACE_TASK_UPDATED]
    assert fed.sent[0][0] == "peer-3"
