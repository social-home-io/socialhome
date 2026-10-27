"""Tests for HaBridgeService — domain events → HA event bus.

Every personal event (D14) is checked four ways: it fires for the
opted-in person it is about, not for someone who didn't opt in, not for
a protected account, and its payload carries no name or user content.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, date, datetime

import pytest

from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.events import (
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
from socialhome.domain.post import Post, PostType
from socialhome.domain.task import Task, TaskStatus
from socialhome.domain.user import User, UserStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services import ha_bridge_service
from socialhome.services.ha_bridge_service import (
    HA_EVENT_SCOPES,
    HaBridgeService,
    HaEventScope,
)


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
    """``get`` / ``get_by_user_id`` over a fixed set of users."""

    def __init__(self, *users: User) -> None:
        self._by_id = {u.user_id: u for u in users}
        self._by_name = {u.username: u for u in users}

    async def get_by_user_id(self, user_id):
        return self._by_id.get(user_id)

    async def get(self, username):
        return self._by_name.get(username)


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
CARLA = _user("u-carla", "carla", opted_in=True)

#: Keys that would carry a name, a title or what someone wrote.
FORBIDDEN_KEYS = frozenset(
    {
        "title",
        "text",
        "body",
        "summary",
        "description",
        "content",
        "message",
        "emoji",
        "display_name",
        "sender_display_name",
        "author",
        "zone_name",
        "latitude",
        "longitude",
        "amount",
        "start",
        "end",
        "assigned_to",
        "completed_by",
        "created_by",
        "moderated_by",
        "bidder_user_id",
        "seller_user_id",
        "sender_user_id",
    }
)


@pytest.fixture
def env():
    bus = EventBus()
    adapter = _FakeAdapter()
    svc = HaBridgeService(
        bus,
        adapter,
        users=_FakeUsers(ANNA, BEN, KID, CARLA),
        child_protection=_FakeProtection({KID.user_id}),
    )
    svc.wire()
    return bus, adapter


def _post(author: str = ANNA.user_id) -> Post:
    return Post(
        id="p1",
        author=author,
        type=PostType.TEXT,
        content="hi",
        created_at=datetime(2026, 4, 15, tzinfo=UTC),
    )


def _task(assignees: tuple[str, ...] = ()) -> Task:
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
        assignees=assignees,
    )


def _cal(created_by: str) -> CalendarEvent:
    return CalendarEvent(
        id="e1",
        calendar_id="cal1",
        summary="Dentist with Carla",
        start=datetime(2026, 4, 15, 9, tzinfo=UTC),
        end=datetime(2026, 4, 15, 10, tzinfo=UTC),
        created_by=created_by,
    )


def _notif(user_id: str, **kw) -> NotificationCreated:
    return NotificationCreated(
        user_id=user_id,
        notification_id=kw.pop("notification_id", "n1"),
        type=kw.pop("type", "dm_message"),
        title=kw.pop("title", "Carla: see you at 6"),
        link_url=kw.pop("link_url", "/dms/c1"),
        **kw,
    )


def _dm(recipients: tuple[str, ...], **kw) -> DmMessageCreated:
    return DmMessageCreated(
        conversation_id="c1",
        message_id="m1",
        sender_user_id=kw.pop("sender", "u-remote"),
        sender_display_name="Dora",
        recipient_user_ids=recipients,
        content="secret",
        **kw,
    )


# Each personal event: (event_type, a factory taking the subject's user_id,
# the exact payload expected for ANNA).
PERSONAL_CASES = {
    "socialhome.post_created": (
        lambda uid: PostCreated(post=_post(uid)),
        {"post_id": "p1", "type": "text", "link_url": "/post/p1"},
    ),
    "socialhome.space_post_created": (
        lambda uid: SpacePostCreated(post=_post(uid), space_id="sp1"),
        {
            "post_id": "p1",
            "space_id": "sp1",
            "type": "text",
            "link_url": "/spaces/sp1?post=p1",
        },
    ),
    "socialhome.space_post_moderated": (
        lambda uid: SpacePostModerated(
            space_id="sp1", post=_post(uid), moderated_by="u-mod"
        ),
        {"space_id": "sp1", "post_id": "p1"},
    ),
    "socialhome.task_assigned": (
        lambda uid: TaskAssigned(task=_task(), assigned_to=uid, space_id="sp1"),
        {
            "task_id": "t1",
            "list_id": "l1",
            "space_id": "sp1",
            "link_url": "/spaces/sp1/tasks?list=l1&task=t1",
        },
    ),
    "socialhome.task_completed": (
        lambda uid: TaskCompleted(task=_task(), completed_by=uid),
        {
            "task_id": "t1",
            "list_id": "l1",
            "space_id": None,
            "link_url": "/organize?list=l1&task=t1",
        },
    ),
    "socialhome.task_deadline_due": (
        lambda uid: TaskDeadlineDue(task=_task((uid,)), due_date=date(2026, 4, 15)),
        {
            "task_id": "t1",
            "list_id": "l1",
            "space_id": None,
            "link_url": "/organize?list=l1&task=t1",
            "due_date": "2026-04-15",
        },
    ),
    "socialhome.calendar_event_created": (
        lambda uid: CalendarEventCreated(event=_cal(uid)),
        {"event_id": "e1", "calendar_id": "cal1"},
    ),
    "socialhome.calendar_event_deleted": (
        lambda uid: CalendarEventDeleted(
            event_id="e1", summary="Dentist", space_id="sp1", actor_user_id=uid
        ),
        {"event_id": "e1", "space_id": "sp1"},
    ),
    "socialhome.bazaar_bid_placed": (
        lambda uid: BazaarBidPlaced(
            listing_post_id="l9",
            seller_user_id=uid,
            bidder_user_id="u-bidder",
            amount=500,
            new_end_time="2026-04-16T00:00:00+00:00",
            bid_id="b1",
            message="I'll pick it up today",
        ),
        {
            "listing_post_id": "l9",
            "bid_id": "b1",
            "space_id": None,
            "link_url": "/bazaar?listing=l9",
        },
    ),
    "socialhome.user_status_changed": (
        lambda uid: UserStatusChanged(
            user_id=uid, status=UserStatus(emoji="🤒", text="sick today")
        ),
        {"status_set": True},
    ),
    "socialhome.presence_updated": (
        lambda uid: PresenceUpdated(
            username="",
            user_id=uid,
            state="zone",
            zone_name="Doctor",
            latitude=47.1234,
            longitude=8.1234,
        ),
        {"state": "zone"},
    ),
    "socialhome.dm_received": (
        lambda uid: _dm((uid,)),
        {
            "conversation_id": "c1",
            "message_id": "m1",
            "message_type": "text",
            "is_group": False,
            "link_url": "/dms/c1",
        },
    ),
    "socialhome.notification_new": (
        lambda uid: _notif(uid),
        {"notification_id": "n1", "type": "dm_message", "link_url": "/dms/c1"},
    ),
}


def test_every_personal_event_has_a_case():
    personal = {
        et for et, scope in HA_EVENT_SCOPES.items() if scope is HaEventScope.PERSONAL
    }
    assert personal == set(PERSONAL_CASES)


@pytest.mark.parametrize("event_type", sorted(PERSONAL_CASES))
async def test_personal_event_fires_for_the_opted_in_person(env, event_type):
    bus, adapter = env
    make, payload = PERSONAL_CASES[event_type]
    await bus.publish(make(ANNA.user_id))
    assert adapter.calls == [
        (event_type, {"user_id": "u-anna", "username": "anna", **payload})
    ]


@pytest.mark.parametrize("event_type", sorted(PERSONAL_CASES))
async def test_personal_event_not_for_someone_who_did_not_opt_in(env, event_type):
    bus, adapter = env
    make, _ = PERSONAL_CASES[event_type]
    await bus.publish(make(BEN.user_id))
    assert adapter.calls == []


@pytest.mark.parametrize("event_type", sorted(PERSONAL_CASES))
async def test_personal_event_never_for_a_protected_account(env, event_type):
    """Skipped even with an opt-in on record (set before protection, or
    written around the API)."""
    bus, adapter = env
    make, _ = PERSONAL_CASES[event_type]
    await bus.publish(make(KID.user_id))
    assert adapter.calls == []


@pytest.mark.parametrize("event_type", sorted(PERSONAL_CASES))
async def test_personal_event_not_for_an_unknown_or_remote_person(env, event_type):
    bus, adapter = env
    make, _ = PERSONAL_CASES[event_type]
    await bus.publish(make("u-remote-person"))
    assert adapter.calls == []


@pytest.mark.parametrize("event_type", sorted(PERSONAL_CASES))
async def test_personal_event_carries_no_names_or_text(env, event_type):
    bus, adapter = env
    make, _ = PERSONAL_CASES[event_type]
    await bus.publish(make(ANNA.user_id))
    ((_, data),) = adapter.calls
    assert not (set(data) & FORBIDDEN_KEYS)
    values = {str(v) for v in data.values()}
    for leaked in ("Buy milk", "Dentist", "secret", "Dora", "Anna", "sick", "🤒"):
        assert not any(leaked in v for v in values)


@pytest.mark.parametrize("event_type", sorted(PERSONAL_CASES))
async def test_personal_event_without_user_lookup_fails_closed(event_type):
    """A bridge built without a user directory can't check the opt-in, so
    it fires nothing rather than everything."""
    bus = EventBus()
    adapter = _FakeAdapter()
    HaBridgeService(bus, adapter).wire()
    make, _ = PERSONAL_CASES[event_type]
    await bus.publish(make(ANNA.user_id))
    assert adapter.calls == []


# ─── Classification guard ────────────────────────────────────────────────


def test_every_fired_event_type_is_classified():
    """A new ``socialhome.*`` event in the bridge must be classified in
    ``HA_EVENT_SCOPES`` (and the docstring table), or this fails."""
    source = inspect.getsource(ha_bridge_service)
    body = source.split('"""', 2)[2]  # skip the module docstring
    body = (
        body.split("HA_EVENT_SCOPES: Final", 1)[0] + body.split("class HaBridge", 1)[1]
    )
    fired = set(re.findall(r'"(socialhome\.[a-z_]+)"', body))
    assert fired, "the scan found no fired events"
    assert fired == set(HA_EVENT_SCOPES)
    docstring = ha_bridge_service.__doc__ or ""
    for event_type, scope in HA_EVENT_SCOPES.items():
        assert re.search(rf"``{re.escape(event_type)}``\s+{scope.value}", docstring), (
            f"{event_type} missing from the module docstring table"
        )


def test_every_subscribed_domain_event_fires_something():
    """Each handler wired on the bus belongs to a classified event: the
    count of subscriptions matches the classified events."""
    bus = EventBus()
    HaBridgeService(bus, _FakeAdapter()).wire()
    handlers = [
        name
        for name, _ in inspect.getmembers(HaBridgeService, inspect.isfunction)
        if name.startswith("_on_")
    ]
    assert len(handlers) == len(HA_EVENT_SCOPES)


async def test_unclassified_event_is_refused(env, caplog):
    """Fail closed at runtime too: an unlisted or mis-scoped type never fires."""
    bus = EventBus()
    adapter = _FakeAdapter()
    svc = HaBridgeService(bus, adapter, users=_FakeUsers(ANNA))
    with caplog.at_level("ERROR", logger="socialhome.services.ha_bridge_service"):
        await svc._fire_personal("socialhome.new_thing", (ANNA.user_id,), {})
        await svc._fire_household("socialhome.new_thing", {})
        await svc._fire_household("socialhome.dm_received", {})
        await svc._fire_personal("socialhome.space_config_changed", ("u",), {})
    assert adapter.calls == []
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 4


# ─── Per-event specifics ─────────────────────────────────────────────────


async def test_dm_fires_once_per_opted_in_recipient(env):
    """A group DM: each local recipient who opted in gets their own event;
    the others, a protected kid and remote seats get nothing."""
    bus, adapter = env
    await bus.publish(
        _dm((ANNA.user_id, BEN.user_id, KID.user_id, CARLA.user_id, "u-remote"))
    )
    assert [(et, d["username"], d["is_group"]) for et, d in adapter.calls] == [
        ("socialhome.dm_received", "anna", True),
        ("socialhome.dm_received", "carla", True),
    ]


async def test_dm_never_fires_for_the_sender(env):
    bus, adapter = env
    await bus.publish(_dm((BEN.user_id,), sender=ANNA.user_id))
    assert adapter.calls == []


async def test_system_chat_message_is_not_a_dm_for_ha(env):
    """A household-chat message is no DM: no ``socialhome.dm_received``."""
    bus, adapter = env
    await bus.publish(_dm((ANNA.user_id, CARLA.user_id), system_scope="household"))
    assert adapter.calls == []


async def test_task_deadline_fires_per_assignee_once(env):
    bus, adapter = env
    task = _task((ANNA.user_id, BEN.user_id, ANNA.user_id, CARLA.user_id))
    await bus.publish(TaskDeadlineDue(task=task, due_date=date(2026, 4, 15)))
    assert [d["username"] for _, d in adapter.calls] == ["anna", "carla"]


async def test_task_deadline_without_assignees_fires_nothing(env):
    bus, adapter = env
    await bus.publish(TaskDeadlineDue(task=_task(), due_date=date(2026, 4, 15)))
    assert adapter.calls == []


async def test_calendar_event_deleted_without_actor_fires_nothing(env):
    bus, adapter = env
    await bus.publish(CalendarEventDeleted(event_id="e1"))
    assert adapter.calls == []


async def test_user_status_cleared(env):
    bus, adapter = env
    await bus.publish(UserStatusChanged(user_id=ANNA.user_id, status=None))
    assert adapter.calls == [
        (
            "socialhome.user_status_changed",
            {"user_id": "u-anna", "username": "anna", "status_set": False},
        )
    ]


async def test_presence_resolves_the_person_by_username(env):
    """Presence without a user_id still finds its person by username."""
    bus, adapter = env
    for user in (ANNA, BEN, KID):
        await bus.publish(PresenceUpdated(username=user.username, state="home"))
    await bus.publish(PresenceUpdated(username="nobody", state="home"))
    assert adapter.calls == [
        (
            "socialhome.presence_updated",
            {"user_id": "u-anna", "username": "anna", "state": "home"},
        )
    ]


async def test_presence_by_username_without_user_lookup_fails_closed():
    bus = EventBus()
    adapter = _FakeAdapter()
    HaBridgeService(bus, adapter).wire()
    await bus.publish(PresenceUpdated(username="anna", state="home"))
    assert adapter.calls == []


async def test_notification_new_fires_once_not_on_a_silent_repeat(env):
    """A chat burst bumps one unread row silently; automations used to hear
    ``socialhome.notification_new`` for every message of the burst. A bump
    that still pushes (a second comment on a post) or a re-opened read row
    is news again."""
    bus, adapter = env
    for bumped, silent in ((False, False), (True, True), (True, False)):
        await bus.publish(_notif(ANNA.user_id, bumped=bumped, silent=silent))
    assert [et for et, _ in adapter.calls] == ["socialhome.notification_new"] * 2


# ─── Household events ────────────────────────────────────────────────────


async def test_space_config_changed_is_household_wide(env):
    """A space's settings changed: no person, no payload content - fires
    without any opt-in, as before."""
    bus, adapter = env
    await bus.publish(
        SpaceConfigChanged(
            space_id="sp1",
            event_type="member_banned",
            payload={"user_id": "u-x", "reason": "spam"},
            sequence=7,
        )
    )
    assert adapter.calls == [
        (
            "socialhome.space_config_changed",
            {"space_id": "sp1", "event_type": "member_banned", "sequence": 7},
        )
    ]


# ─── Failure handling ────────────────────────────────────────────────────


async def test_adapter_failure_does_not_propagate(caplog):
    """A failing fire_event must not crash the publisher."""
    bus = EventBus()
    HaBridgeService(bus, _FakeAdapter(fail=True), users=_FakeUsers(ANNA)).wire()
    with caplog.at_level("WARNING", logger="socialhome.services.ha_bridge_service"):
        await bus.publish(PostCreated(post=_post()))
    assert any("failed" in r.getMessage() for r in caplog.records)


async def test_refused_fire_is_logged_at_warning(caplog):
    """CLAUDE.md: an HA call failure is diagnosable, not silent."""
    bus = EventBus()
    HaBridgeService(bus, _FakeAdapter(refuse=True)).wire()
    with caplog.at_level("WARNING", logger="socialhome.services.ha_bridge_service"):
        await bus.publish(
            SpaceConfigChanged(space_id="s", event_type="x", payload={}, sequence=1)
        )
    assert any(
        "socialhome.space_config_changed" in r.getMessage() and r.levelname == "WARNING"
        for r in caplog.records
    )
