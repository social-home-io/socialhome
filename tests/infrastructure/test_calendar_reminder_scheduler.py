"""CalendarReminderScheduler — in-window event → notification."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.user import User
from socialhome.infrastructure.calendar_reminder_scheduler import (
    CalendarReminderScheduler,
)


class _FakeCalendarRepo:
    def __init__(self, events_by_user: dict[str, list[CalendarEvent]]) -> None:
        self._events = events_by_user

    async def list_events_for_user_in_range(
        self,
        username,
        *,
        start,
        end,
    ):
        return [
            e for e in self._events.get(username, []) if e.start < end and e.end > start
        ]


class _FakeUserRepo:
    def __init__(self, users: list[User]) -> None:
        self._users = users

    async def list_active(self) -> list[User]:
        return list(self._users)


class _FakeNotifService:
    def __init__(self) -> None:
        self.saved = []

    async def _save_notif(self, note):
        self.saved.append(note)
        return note


def _user(username: str) -> User:
    return User(
        username=username,
        user_id=f"{username}-id",
        display_name=username.title(),
    )


def _event(eid: str, offset_minutes: int, duration_min: int = 60) -> CalendarEvent:
    now = datetime.now(timezone.utc)
    return CalendarEvent(
        id=eid,
        calendar_id="c1",
        summary=f"Event {eid}",
        start=now + timedelta(minutes=offset_minutes),
        end=now + timedelta(minutes=offset_minutes + duration_min),
        created_by="admin",
    )


@pytest.fixture
def scheduler():
    users = [_user("alice")]
    events = {
        "alice": [
            _event("in-window", offset_minutes=5),  # fires
            _event("past", offset_minutes=-30),  # already started
            _event("far", offset_minutes=60),  # outside window
        ],
    }
    cal_repo = _FakeCalendarRepo(events)
    user_repo = _FakeUserRepo(users)
    notif = _FakeNotifService()
    sched = CalendarReminderScheduler(
        calendar_repo=cal_repo,
        user_repo=user_repo,
        notif_service=notif,
        reminder_window_minutes=10,
    )
    return sched, notif


async def test_tick_fires_in_window_event_only(scheduler):
    sched, notif = scheduler
    fired = await sched.tick_once()
    assert fired == 1
    assert len(notif.saved) == 1
    note = notif.saved[0]
    assert note.type == "calendar_reminder"
    assert "Event in-window" in note.title
    assert note.link_url == "/calendar?event=in-window"
    assert note.user_id == "alice-id"


async def test_tick_is_idempotent_per_event(scheduler):
    sched, notif = scheduler
    await sched.tick_once()
    await sched.tick_once()
    # Only one notification — dedupe kicks in on the second tick.
    assert len(notif.saved) == 1


async def test_nothing_fires_when_no_upcoming(scheduler):
    sched, notif = scheduler
    sched._calendar_repo = _FakeCalendarRepo({"alice": []})
    fired = await sched.tick_once()
    assert fired == 0
    assert notif.saved == []
