"""Tests for HaBridgeService — domain events → HA event bus."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from socialhome.domain.events import (
    DmMessageCreated,
    NotificationCreated,
    PostCreated,
    SpacePostCreated,
    TaskAssigned,
    TaskCompleted,
    TaskDeadlineDue,
    UserStatusChanged,
)
from socialhome.domain.post import Post, PostType
from socialhome.domain.task import Task, TaskStatus
from socialhome.domain.user import User, UserStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.ha_bridge_service import HaBridgeService


class _FakeAdapter:
    def __init__(self, *, fail: bool = False, refuse: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self.refuse = refuse

    async def fire_event(self, event_type, data):
        if self.fail:
            raise RuntimeError("boom")
        if self.refuse:
            return False
        self.calls.append((event_type, data))
        return True


class _FakeUsers:
    """``get_by_user_id`` over a fixed set of users."""

    def __init__(self, *users: User) -> None:
        self._by_id = {u.user_id: u for u in users}

    async def get_by_user_id(self, user_id):
        return self._by_id.get(user_id)


class _FakeProtection:
    def __init__(self, protected: set[str] | None = None) -> None:
        self.protected = protected or set()

    async def is_protected(self, user_id):
        return user_id in self.protected


def _user(user_id: str, username: str, *, opted_in: bool) -> User:
    prefs = '{"ha_automation_events": true}' if opted_in else "{}"
    return User(
        user_id=user_id,
        username=username,
        display_name=username.title(),
        preferences_json=prefs,
    )


ANNA = _user("u-anna", "anna", opted_in=True)
BEN = _user("u-ben", "ben", opted_in=False)
KID = _user("u-kid", "kid", opted_in=True)


@pytest.fixture
def env():
    bus = EventBus()
    adapter = _FakeAdapter()
    svc = HaBridgeService(
        bus,
        adapter,
        users=_FakeUsers(ANNA, BEN, KID),
        child_protection=_FakeProtection({KID.user_id}),
    )
    svc.wire()
    return bus, adapter


def _notif(user_id: str, **kw) -> NotificationCreated:
    return NotificationCreated(
        user_id=user_id,
        notification_id=kw.pop("notification_id", "n1"),
        type=kw.pop("type", "dm_message"),
        title=kw.pop("title", "Carla: see you at 6"),
        link_url=kw.pop("link_url", "/dms/c1"),
        **kw,
    )


def _post():
    return Post(
        id="p1",
        author="u1",
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 4, 15, tzinfo=UTC),
    )


def _task():
    now = datetime(2026, 4, 15, tzinfo=UTC)
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
    assert et == "socialhome.post_created"
    assert data["post_id"] == "p1"
    assert data["author"] == "u1"


async def test_space_post_created_includes_space_id(env):
    bus, adapter = env
    await bus.publish(SpacePostCreated(post=_post(), space_id="sp-1"))
    et, data = adapter.calls[0]
    assert et == "socialhome.space_post_created"
    assert data["space_id"] == "sp-1"


async def test_task_assigned_includes_assignee(env):
    bus, adapter = env
    await bus.publish(TaskAssigned(task=_task(), assigned_to="alice"))
    et, data = adapter.calls[0]
    assert et == "socialhome.task_assigned"
    assert data["assigned_to"] == "alice"
    assert data["title"] == "Buy milk"


async def test_task_completed_fires(env):
    bus, adapter = env
    await bus.publish(TaskCompleted(task=_task(), completed_by="alice"))
    et, _ = adapter.calls[0]
    assert et == "socialhome.task_completed"


async def test_task_deadline_due_fires(env):
    bus, adapter = env
    await bus.publish(TaskDeadlineDue(task=_task(), due_date=date(2026, 4, 15)))
    et, data = adapter.calls[0]
    assert et == "socialhome.task_deadline_due"
    assert data["due_date"] == "2026-04-15"


async def test_user_status_changed_fires(env):
    bus, adapter = env
    status = UserStatus(emoji="👍", text="busy")
    await bus.publish(UserStatusChanged(user_id="u1", status=status))
    et, data = adapter.calls[0]
    assert et == "socialhome.user_status_changed"
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


async def test_dm_message_fires_dm_received_without_content(env):
    bus, adapter = env
    await bus.publish(
        DmMessageCreated(
            conversation_id="c1",
            message_id="m1",
            sender_user_id="u1",
            sender_display_name="Anna",
            recipient_user_ids=("u2",),
            content="secret",
        )
    )
    assert adapter.calls == [
        (
            "socialhome.dm_received",
            {"conversation_id": "c1", "sender_display_name": "Anna", "is_group": False},
        )
    ]


async def test_system_chat_message_is_not_a_dm_for_ha(env):
    """A household-chat message is no DM: no ``socialhome.dm_received``."""
    bus, adapter = env
    await bus.publish(
        DmMessageCreated(
            conversation_id="hh",
            message_id="m1",
            sender_user_id="u1",
            sender_display_name="Anna",
            recipient_user_ids=("u2", "u3"),
            system_scope="household",
        )
    )
    assert adapter.calls == []


async def test_notification_new_fires_once_not_on_a_bumped_row(env):
    """A chat burst bumps one unread row; automations used to hear
    ``socialhome.notification_new`` for every message of the burst."""
    bus, adapter = env
    for bumped in (False, True):
        await bus.publish(_notif(ANNA.user_id, bumped=bumped))
    assert [et for et, _ in adapter.calls] == ["socialhome.notification_new"]


async def test_notification_new_carries_no_title_or_text(env):
    """D14: type, ids and link only - never the title (it names people and
    carries chat / task / event text) and never a body."""
    bus, adapter = env
    await bus.publish(_notif(ANNA.user_id))
    assert adapter.calls == [
        (
            "socialhome.notification_new",
            {
                "user_id": "u-anna",
                "username": "anna",
                "notification_id": "n1",
                "type": "dm_message",
                "link_url": "/dms/c1",
            },
        )
    ]


async def test_notification_new_only_for_people_who_opted_in(env):
    """D14: off by default - a person who never opted in is not on HA's bus."""
    bus, adapter = env
    await bus.publish(_notif(BEN.user_id))
    assert adapter.calls == []


async def test_notification_new_never_for_a_protected_account(env):
    """D14: a protected account is skipped even with an opt-in on record
    (e.g. set before protection, or written around the API)."""
    bus, adapter = env
    await bus.publish(_notif(KID.user_id))
    assert adapter.calls == []


async def test_notification_new_unknown_user_is_skipped(env):
    bus, adapter = env
    await bus.publish(_notif("u-gone"))
    assert adapter.calls == []


async def test_notification_new_without_user_lookup_fails_closed():
    """A bridge built without a user directory can't check the opt-in, so
    it fires nothing rather than everything."""
    bus = EventBus()
    adapter = _FakeAdapter()
    HaBridgeService(bus, adapter).wire()
    await bus.publish(_notif(ANNA.user_id))
    assert adapter.calls == []


async def test_refused_fire_is_logged_at_warning(caplog):
    """CLAUDE.md: an HA call failure is diagnosable, not silent."""
    bus = EventBus()
    HaBridgeService(bus, _FakeAdapter(refuse=True)).wire()
    with caplog.at_level("WARNING", logger="socialhome.services.ha_bridge_service"):
        await bus.publish(PostCreated(post=_post()))
    assert any(
        "socialhome.post_created" in r.getMessage() and r.levelname == "WARNING"
        for r in caplog.records
    )
