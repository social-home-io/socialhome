"""Tests for :class:`SpaceContentInboundHandlers` (§13)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.federation_inbound import SpaceContentInboundHandlers


class _FakeRegistry:
    def __init__(self) -> None:
        self.registered = []

    def register(self, t, h):
        self.registered.append((t, h))


class _FakeFederationService:
    def __init__(self) -> None:
        self._event_registry = _FakeRegistry()


class _FakePageRepo:
    def __init__(self) -> None:
        self.saved = []
        self.deleted = []

    async def save(self, page):
        self.saved.append(page)

    async def delete(self, page_id):
        self.deleted.append(page_id)


class _FakeStickyRepo:
    def __init__(self) -> None:
        self.saved = []
        self.deleted = []

    async def save(self, sticky):
        self.saved.append(sticky)

    async def delete(self, sticky_id):
        self.deleted.append(sticky_id)


class _FakeSpaceTaskRepo:
    def __init__(self) -> None:
        self.saved = []
        self.deleted = []

    async def save(self, space_id, task):
        self.saved.append((space_id, task))
        return task

    async def delete(self, task_id):
        self.deleted.append(task_id)


class _FakeSpaceCalendarRepo:
    def __init__(self) -> None:
        self.saved = []
        self.deleted = []

    async def save_event(self, space_id, event):
        self.saved.append((space_id, event))
        return event

    async def delete_event(self, event_id):
        self.deleted.append(event_id)


class _FakePollRepo:
    def __init__(self) -> None:
        self.valid_options: set[tuple[str, str]] = set()
        self.cleared: list[tuple[str, str]] = []
        self.inserted: list[tuple[str, str]] = []
        self.closed: list[str] = []

    async def option_belongs_to_post(self, *, option_id, post_id):
        return (post_id, option_id) in self.valid_options

    async def clear_user_votes(self, *, post_id, voter_user_id):
        self.cleared.append((post_id, voter_user_id))

    async def insert_vote(self, *, option_id, voter_user_id):
        self.inserted.append((option_id, voter_user_id))

    async def close(self, post_id):
        self.closed.append(post_id)


def _event(event_type, payload, *, from_instance="peer-a", space_id=None):
    return FederationEvent(
        msg_id="m",
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
    )


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def repos():
    return {
        "page": _FakePageRepo(),
        "sticky": _FakeStickyRepo(),
        "task": _FakeSpaceTaskRepo(),
        "calendar": _FakeSpaceCalendarRepo(),
        "poll": _FakePollRepo(),
    }


@pytest.fixture
def handlers(bus, repos):
    h = SpaceContentInboundHandlers(
        bus=bus,
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        poll_repo=repos["poll"],
    )
    h.attach_to(_FakeFederationService())
    return h


async def test_attach_registers_all_content_event_types(bus, repos):
    h = SpaceContentInboundHandlers(
        bus=bus,
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
        poll_repo=repos["poll"],
    )
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    # 15 events total: 3 task + 3 page + 3 sticky + 3 calendar + 3 poll.
    for t in (
        FederationEventType.SPACE_TASK_CREATED,
        FederationEventType.SPACE_TASK_UPDATED,
        FederationEventType.SPACE_TASK_DELETED,
        FederationEventType.SPACE_PAGE_CREATED,
        FederationEventType.SPACE_PAGE_UPDATED,
        FederationEventType.SPACE_PAGE_DELETED,
        FederationEventType.SPACE_STICKY_CREATED,
        FederationEventType.SPACE_STICKY_UPDATED,
        FederationEventType.SPACE_STICKY_DELETED,
        FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
        FederationEventType.SPACE_CALENDAR_EVENT_UPDATED,
        FederationEventType.SPACE_CALENDAR_EVENT_DELETED,
        FederationEventType.SPACE_POLL_CREATED,
        FederationEventType.SPACE_POLL_VOTE_CAST,
        FederationEventType.SPACE_POLL_CLOSED,
    ):
        assert t in types


# ─── Tasks ──────────────────────────────────────────────────────────


async def test_task_saved_happy_path(repos, handlers):
    await handlers._on_task_saved(
        _event(
            FederationEventType.SPACE_TASK_CREATED,
            {
                "id": "t-1",
                "list_id": "list-1",
                "title": "Fix the sink",
                "status": "todo",
                "created_by": "u-1",
                "assignees": ["u-2"],
            },
            space_id="sp-1",
        )
    )
    assert len(repos["task"].saved) == 1
    sp, task = repos["task"].saved[0]
    assert sp == "sp-1"
    assert task.id == "t-1"
    assert task.assignees == ("u-2",)


async def test_task_saved_missing_fields_drops(repos, handlers):
    await handlers._on_task_saved(
        _event(
            FederationEventType.SPACE_TASK_CREATED,
            {},
            space_id="sp-1",
        )
    )
    assert repos["task"].saved == []


async def test_task_deleted(repos, handlers):
    await handlers._on_task_deleted(
        _event(
            FederationEventType.SPACE_TASK_DELETED,
            {"id": "t-1"},
        )
    )
    assert repos["task"].deleted == ["t-1"]


# ─── Pages ──────────────────────────────────────────────────────────


async def test_page_saved_happy_path(repos, handlers):
    await handlers._on_page_saved(
        _event(
            FederationEventType.SPACE_PAGE_CREATED,
            {
                "id": "p-1",
                "title": "Shopping tips",
                "content": "Buy local",
                "created_by": "u-1",
                "created_at": "2026-04-18T00:00:00+00:00",
                "updated_at": "2026-04-18T00:00:00+00:00",
            },
            space_id="sp-1",
        )
    )
    assert len(repos["page"].saved) == 1
    assert repos["page"].saved[0].id == "p-1"
    assert repos["page"].saved[0].space_id == "sp-1"


async def test_page_saved_missing_title_drops(repos, handlers):
    await handlers._on_page_saved(
        _event(
            FederationEventType.SPACE_PAGE_CREATED,
            {"id": "p-1"},
            space_id="sp-1",
        )
    )
    assert repos["page"].saved == []


async def test_page_deleted(repos, handlers):
    await handlers._on_page_deleted(
        _event(
            FederationEventType.SPACE_PAGE_DELETED,
            {"id": "p-1"},
        )
    )
    assert repos["page"].deleted == ["p-1"]


# ─── Stickies ───────────────────────────────────────────────────────


async def test_sticky_saved_happy_path(repos, handlers):
    await handlers._on_sticky_saved(
        _event(
            FederationEventType.SPACE_STICKY_CREATED,
            {
                "id": "s-1",
                "author": "u-1",
                "content": "Remember to water plants",
                "color": "pink",
                "position_x": 100.0,
                "position_y": 50.0,
            },
            space_id="sp-1",
        )
    )
    assert len(repos["sticky"].saved) == 1
    assert repos["sticky"].saved[0].id == "s-1"


async def test_sticky_saved_missing_content_drops(repos, handlers):
    await handlers._on_sticky_saved(
        _event(
            FederationEventType.SPACE_STICKY_CREATED,
            {"id": "s-1", "author": "u-1"},
            space_id="sp-1",
        )
    )
    assert repos["sticky"].saved == []


async def test_sticky_deleted(repos, handlers):
    await handlers._on_sticky_deleted(
        _event(
            FederationEventType.SPACE_STICKY_DELETED,
            {"id": "s-1"},
        )
    )
    assert repos["sticky"].deleted == ["s-1"]


# ─── Calendar events ────────────────────────────────────────────────


async def test_calendar_saved_happy_path(repos, handlers):
    await handlers._on_calendar_saved(
        _event(
            FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
            {
                "id": "e-1",
                "calendar_id": "cal-1",
                "summary": "Weekly sync",
                "created_by": "u-1",
                "start": "2026-04-18T10:00:00+00:00",
                "end": "2026-04-18T11:00:00+00:00",
            },
            space_id="sp-1",
        )
    )
    assert len(repos["calendar"].saved) == 1
    sp, ev = repos["calendar"].saved[0]
    assert sp == "sp-1"
    assert ev.id == "e-1"


async def test_calendar_saved_missing_end_drops(repos, handlers):
    await handlers._on_calendar_saved(
        _event(
            FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
            {
                "id": "e-1",
                "calendar_id": "cal-1",
                "summary": "X",
                "created_by": "u-1",
                "start": "2026-04-18T10:00:00+00:00",
            },
            space_id="sp-1",
        )
    )
    assert repos["calendar"].saved == []


async def test_calendar_deleted(repos, handlers):
    await handlers._on_calendar_deleted(
        _event(
            FederationEventType.SPACE_CALENDAR_EVENT_DELETED,
            {"id": "e-1"},
        )
    )
    assert repos["calendar"].deleted == ["e-1"]


# ─── Polls ──────────────────────────────────────────────────────────


async def test_poll_created_logs_noop(repos, handlers):
    """POLL_CREATED is a signal hook; no persistence side effect."""
    await handlers._on_poll_created(
        _event(
            FederationEventType.SPACE_POLL_CREATED,
            {"post_id": "p-1"},
        )
    )
    assert repos["poll"].inserted == []
    assert repos["poll"].closed == []


async def test_poll_vote_clears_and_inserts(repos, handlers):
    """Single-choice invariant — prior vote cleared before new one inserts."""
    repos["poll"].valid_options.add(("p-1", "opt-a"))
    await handlers._on_poll_vote(
        _event(
            FederationEventType.SPACE_POLL_VOTE_CAST,
            {"post_id": "p-1", "option_id": "opt-a", "voter_user_id": "u-1"},
        )
    )
    assert repos["poll"].cleared == [("p-1", "u-1")]
    assert repos["poll"].inserted == [("opt-a", "u-1")]


async def test_poll_vote_option_not_on_post_drops(repos, handlers):
    """Can't corrupt a tally with an option id that belongs elsewhere."""
    # No options registered — every lookup returns False.
    await handlers._on_poll_vote(
        _event(
            FederationEventType.SPACE_POLL_VOTE_CAST,
            {"post_id": "p-1", "option_id": "stolen", "voter_user_id": "u-1"},
        )
    )
    assert repos["poll"].inserted == []


async def test_poll_vote_missing_field_drops(repos, handlers):
    await handlers._on_poll_vote(
        _event(
            FederationEventType.SPACE_POLL_VOTE_CAST,
            {"post_id": "p-1"},
        )
    )
    assert repos["poll"].inserted == []


async def test_poll_closed(repos, handlers):
    await handlers._on_poll_closed(
        _event(
            FederationEventType.SPACE_POLL_CLOSED,
            {"post_id": "p-1"},
        )
    )
    assert repos["poll"].closed == ["p-1"]


async def test_poll_handlers_not_registered_without_poll_repo(bus, repos):
    """Deployments without polls skip those events cleanly."""
    h = SpaceContentInboundHandlers(
        bus=bus,
        page_repo=repos["page"],
        sticky_repo=repos["sticky"],
        task_repo=repos["task"],
        calendar_repo=repos["calendar"],
    )  # no poll_repo
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert FederationEventType.SPACE_POLL_CREATED not in types
    assert FederationEventType.SPACE_POLL_VOTE_CAST not in types
    assert FederationEventType.SPACE_POLL_CLOSED not in types
