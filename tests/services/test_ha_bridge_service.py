"""Tests for HaBridgeService — domain events → HA event bus."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from social_home.domain.events import (
    PostCreated,
    SpacePostCreated,
    TaskAssigned,
    TaskCompleted,
    TaskDeadlineDue,
    UserStatusChanged,
)
from social_home.domain.post import Post, PostType
from social_home.domain.task import Task, TaskStatus
from social_home.domain.user import UserStatus
from social_home.infrastructure.event_bus import EventBus
from social_home.services.ha_bridge_service import HaBridgeService


class _FakeAdapter:
    def __init__(self, *, fail: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def fire_event(self, event_type, data):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append((event_type, data))
        return True


@pytest.fixture
def env():
    bus = EventBus()
    adapter = _FakeAdapter()
    svc = HaBridgeService(bus, adapter)
    svc.wire()
    return bus, adapter


def _post():
    return Post(
        id="p1",
        author="u1",
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
    )


def _task():
    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    return Task(
        id="t1",
        list_id="l1",
        title="Buy milk",
        status=TaskStatus.TODO,
        position=0,
        created_by="me",
        created_at=now,
        updated_at=now,
    )


# ─── Event handlers ──────────────────────────────────────────────────────


async def test_post_created_fires_namespaced_event(env):
    bus, adapter = env
    await bus.publish(PostCreated(post=_post()))
    assert adapter.calls
    et, data = adapter.calls[0]
    assert et == "social_home.post_created"
    assert data["post_id"] == "p1"
    assert data["author"] == "u1"


async def test_space_post_created_includes_space_id(env):
    bus, adapter = env
    await bus.publish(SpacePostCreated(post=_post(), space_id="sp-1"))
    et, data = adapter.calls[0]
    assert et == "social_home.space_post_created"
    assert data["space_id"] == "sp-1"


async def test_task_assigned_includes_assignee(env):
    bus, adapter = env
    await bus.publish(TaskAssigned(task=_task(), assigned_to="alice"))
    et, data = adapter.calls[0]
    assert et == "social_home.task_assigned"
    assert data["assigned_to"] == "alice"
    assert data["title"] == "Buy milk"


async def test_task_completed_fires(env):
    bus, adapter = env
    await bus.publish(TaskCompleted(task=_task(), completed_by="alice"))
    et, _ = adapter.calls[0]
    assert et == "social_home.task_completed"


async def test_task_deadline_due_fires(env):
    bus, adapter = env
    await bus.publish(TaskDeadlineDue(task=_task(), due_date=date(2026, 4, 15)))
    et, data = adapter.calls[0]
    assert et == "social_home.task_deadline_due"
    assert data["due_date"] == "2026-04-15"


async def test_user_status_changed_fires(env):
    bus, adapter = env
    status = UserStatus(emoji="👍", text="busy")
    await bus.publish(UserStatusChanged(user_id="u1", status=status))
    et, data = adapter.calls[0]
    assert et == "social_home.user_status_changed"
    assert data["emoji"] == "👍"


async def test_user_status_cleared_carries_null_emoji(env):
    bus, adapter = env
    await bus.publish(UserStatusChanged(user_id="u1", status=None))
    _, data = adapter.calls[0]
    assert data["emoji"] is None
    assert data["text"] is None


async def test_adapter_failure_does_not_propagate():
    """A failing fire_event must not crash the publisher."""
    bus = EventBus()
    bridge = HaBridgeService(bus, _FakeAdapter(fail=True))
    bridge.wire()
    # Should not raise.
    await bus.publish(PostCreated(post=_post()))
