"""Tests for :class:`SpaceSummaryService` — per-section counts for More's
section rows, read at request time from in-memory repo stubs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from socialhome.domain.space import SpaceFeatures, SpacePermissionError, SpaceRole
from socialhome.domain.task import TaskStatus
from socialhome.services.space_summary_service import (
    UPCOMING_DAYS,
    SpaceSummaryService,
)

_SID = "sp-1"
_ME = "uid-me"
_NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


class _Spaces:
    def __init__(self, features: SpaceFeatures, seats: dict[str, SpaceRole]):
        self.space = SimpleNamespace(id=_SID, features=features)
        self.seats = seats
        self.banned: set[str] = set()
        self.exists = True

    async def get_member(self, space_id, user_id):
        role = self.seats.get(user_id) if space_id == _SID else None
        return SimpleNamespace(role=role) if role else None

    async def is_banned(self, space_id, user_id):
        return user_id in self.banned

    async def get(self, space_id):
        return self.space if self.exists and space_id == _SID else None


def _task(status: TaskStatus, *, assignees=(), archived=False):
    return SimpleNamespace(
        status=status,
        assignees=tuple(assignees),
        archived_at=_NOW if archived else None,
    )


class _Tasks:
    async def list_by_space(self, space_id):
        return [
            _task(TaskStatus.TODO, assignees=[_ME]),
            _task(TaskStatus.IN_PROGRESS, assignees=["uid-other", _ME]),
            _task(TaskStatus.TODO),
            _task(TaskStatus.DONE, assignees=[_ME]),
            _task(TaskStatus.TODO, assignees=[_ME], archived=True),
        ]


class _Rows:
    """``list(space_id=…)`` for pages and stickies."""

    def __init__(self, n: int):
        self.n = n

    async def list(self, *, space_id=None):
        assert space_id == _SID
        return [object()] * self.n


class _Gallery:
    def __init__(self):
        self.limit = None

    async def list_albums(self, space_id, *, limit=30):
        self.limit = limit
        return [SimpleNamespace(item_count=3), SimpleNamespace(item_count=2)]


class _Bazaar:
    async def list_active_in_spaces(self, space_ids):
        assert space_ids == (_SID,)
        return [object()]


class _Calendar:
    def __init__(self):
        self.window = None

    async def list_events_in_range(self, space_id, *, start, end):
        self.window = (start, end)
        return [object()] * 4


def _svc(features: SpaceFeatures | None = None, seats=None):
    features = features or SpaceFeatures()
    spaces = _Spaces(features, seats if seats is not None else {_ME: SpaceRole.MEMBER})
    gallery, calendar = _Gallery(), _Calendar()
    svc = SpaceSummaryService(
        space_repo=spaces,
        task_repo=_Tasks(),
        page_repo=_Rows(2),
        gallery_repo=gallery,
        sticky_repo=_Rows(6),
        bazaar_repo=_Bazaar(),
        calendar_repo=calendar,
    )
    return svc, spaces, gallery, calendar


async def test_counts_every_section_that_is_on():
    svc, _, gallery, calendar = _svc()
    out = await svc.summary(_SID, user_id=_ME, now=_NOW)
    assert out == {
        # done and archived tasks are not open; "mine" = assigned to me.
        "tasks": {"open": 3, "mine": 2},
        "pages": {"count": 2},
        "gallery": {"albums": 2, "items": 5},
        "stickies": {"count": 6},
        "bazaar": {"active": 1},
        "calendar": {"upcoming": 4, "days": UPCOMING_DAYS},
    }
    assert gallery.limit == 200
    assert calendar.window == (_NOW, _NOW + timedelta(days=UPCOMING_DAYS))


async def test_a_section_turned_off_has_no_count():
    features = SpaceFeatures(
        todo=False,
        pages=False,
        gallery=False,
        stickies=False,
        bazaar=False,
        calendar=False,
    )
    svc, *_ = _svc(features)
    out = await svc.summary(_SID, user_id=_ME, now=_NOW)
    assert out == {
        "tasks": None,
        "pages": None,
        "gallery": None,
        "stickies": None,
        "bazaar": None,
        "calendar": None,
    }


async def test_a_follower_gets_the_counts_they_may_read():
    svc, *_ = _svc(seats={_ME: SpaceRole.SUBSCRIBER})
    out = await svc.summary(_SID, user_id=_ME, now=_NOW)
    assert out["pages"] == {"count": 2}
    # Nothing in the reply is about the map or the chat.
    assert set(out) == {"tasks", "pages", "gallery", "stickies", "bazaar", "calendar"}


async def test_no_seat_is_refused_before_the_space_is_read():
    svc, spaces, *_ = _svc(seats={})
    spaces.exists = False  # an unknown id answers the same
    with pytest.raises(SpacePermissionError):
        await svc.summary(_SID, user_id=_ME)


async def test_a_banned_seat_is_no_seat():
    svc, spaces, *_ = _svc()
    spaces.banned.add(_ME)
    with pytest.raises(SpacePermissionError):
        await svc.summary(_SID, user_id=_ME)


async def test_a_seat_left_in_a_dissolved_space_is_not_found():
    svc, spaces, *_ = _svc()
    spaces.exists = False
    with pytest.raises(KeyError):
        await svc.summary(_SID, user_id=_ME)


async def test_now_defaults_to_the_current_time():
    svc, _, _, calendar = _svc()
    before = datetime.now(UTC)
    await svc.summary(_SID, user_id=_ME)
    start, end = calendar.window
    assert before <= start <= datetime.now(UTC)
    assert end - start == timedelta(days=UPCOMING_DAYS)
