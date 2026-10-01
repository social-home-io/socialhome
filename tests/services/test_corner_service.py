"""Tests for socialhome.services.corner_service — the "My Corner" bundle.

Unit tests: every repo is an in-memory stub. The timetable slice runs
the real :class:`TimetableService` over a stub repo so the tz / validity
rules are the production ones.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.preferences import HouseholdPreferences
from socialhome.domain.timetable import (
    EntryKind,
    LessonStatus,
    OverrideKind,
    Timetable,
    TimetableEntry,
    TimetableOverride,
    TimetableValidity,
    week_anchor,
)
from socialhome.services import corner_service as cs
from socialhome.services.corner_service import CornerService
from socialhome.services.timetable_service import (
    MAX_TODAY_TIMETABLES,
    TimetableService,
)

#: A Monday in CEST (Berlin = UTC+2).
MON = date(2026, 9, 28)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _utc(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc)


# ─── Stubs ───────────────────────────────────────────────────────────────


class _Nothing:
    """Answers every repo call with an empty result."""

    async def count_unread(self, *a, **kw):
        return 0

    async def list_for_user(self, *a, **kw):
        return []

    async def list_presence(self):
        return []

    async def list_by_assignee(self, *a, **kw):
        return []

    async def list_by_seller(self, *a, **kw):
        return []

    async def get(self, *a, **kw):
        return None


class _Calendar:
    """Returns the events overlapping ``[start, end)`` — like the SQL."""

    def __init__(self, events=()):
        self.events = list(events)
        self.calls: list[tuple[datetime, datetime]] = []

    async def list_events_for_user_in_range(self, username, *, start, end):
        self.calls.append((start, end))
        return [e for e in self.events if e.start < end and e.end > start]


class _TimetableRepo:
    def __init__(self, tts=()):
        self.tts = list(tts)

    async def list_all(self):
        return list(self.tts)


class _Prefs:
    def __init__(self, *, tz="UTC", enabled=True):
        self.tz = tz
        self.enabled = enabled

    async def get_household(self):
        return HouseholdPreferences(tz=self.tz, feat_timetable=self.enabled)

    async def require_enabled(self, section):
        prefs = await self.get_household()
        prefs.require_enabled(section)
        return prefs


class _Boom:
    async def today_for_user(self, *a, **kw):
        raise RuntimeError("boom")


def _event(eid, start, end, **kw) -> CalendarEvent:
    return CalendarEvent(
        id=eid,
        calendar_id="cal",
        summary=kw.pop("summary", eid),
        start=start,
        end=end,
        created_by="u-anna",
        **kw,
    )


def _entry(eid, weekday, start, end, **kw) -> TimetableEntry:
    return TimetableEntry(id=eid, weekday=weekday, start=start, end=end, **kw)


def _tt(tid="tt-1", *, assignees=("u-anna",), **kw) -> Timetable:
    base = dict(
        id=tid,
        name=f"Anna {tid}",
        created_by="u-anna",
        created_at=T0,
        updated_at=T0,
        tz="Europe/Berlin",
        assignees=assignees,
        entries=(
            _entry("m1", 0, time(8, 0), time(8, 45), title="Mathe"),
            _entry("m2", 0, time(8, 50), time(9, 35), title="Deutsch"),
            _entry(
                "mb", 0, time(9, 35), time(9, 55), kind=EntryKind.BREAK, title="Pause"
            ),
        ),
    )
    base.update(kw)
    return Timetable(**base)


class _Users:
    def __init__(self, preferences_json="{}"):
        self.preferences_json = preferences_json

    async def get_by_user_id(self, user_id):
        return SimpleNamespace(user_id=user_id, preferences_json=self.preferences_json)


class _SpaceTimetables:
    """Stand-in for SpaceTimetableService.pinned_for_user."""

    def __init__(self, visible=(), *, boom=False):
        self.visible = {tt.id: tt for tt in visible}
        self.boom = boom
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def pinned_for_user(self, user_id, pins):
        self.calls.append((user_id, tuple(pins)))
        if self.boom:
            raise RuntimeError("boom")
        return [self.visible[p] for p in pins if p in self.visible]


def _svc(
    *,
    tts=(),
    events=(),
    prefs=None,
    timetable_service="real",
    users=None,
    space_timetables=None,
):
    prefs = prefs or _Prefs()
    nothing = _Nothing()
    if timetable_service == "real":
        timetable_service = TimetableService(_TimetableRepo(tts))
        timetable_service.attach_household_features(prefs)
    calendar = _Calendar(events)
    svc = CornerService(
        notification_repo=nothing,
        conversation_repo=nothing,
        calendar_repo=calendar,
        presence_service=nothing,
        task_repo=nothing,
        bazaar_repo=nothing,
        user_repo=users or nothing,
        space_repo=nothing,
        space_post_repo=nothing,
        timetable_service=timetable_service,
        preferences_service=prefs,
        space_timetable_service=space_timetables,
    )
    return svc, calendar


async def _build(svc, now):
    return await svc.build(user_id="u-anna", username="anna", now=now)


# ─── today_timetable ─────────────────────────────────────────────────────


async def test_timetable_slice_only_when_assigned_and_valid():
    mine = _tt("tt-mine")
    other = _tt("tt-ben", assignees=("u-ben",))
    svc, _ = _svc(tts=[mine, other])
    bundle = await _build(svc, _utc(MON, 5))
    assert [t.timetable_id for t in bundle.today_timetable] == ["tt-mine"]
    # Saturday: not a school day → nothing.
    bundle = await _build(svc, _utc(MON + timedelta(days=5), 5))
    assert bundle.today_timetable == ()


async def test_timetable_slice_empty_when_feature_off():
    svc, _ = _svc(tts=[_tt()], prefs=_Prefs(enabled=False))
    bundle = await _build(svc, _utc(MON, 5))
    assert bundle.today_timetable == ()


async def test_timetable_slice_empty_without_a_timetable_service():
    svc, _ = _svc(tts=[_tt()], timetable_service=None)
    assert (await _build(svc, _utc(MON, 5))).today_timetable == ()


async def test_holiday_week_is_excluded():
    tt = _tt(validity=TimetableValidity(excluded_weeks=(week_anchor(MON, 0),)))
    svc, _ = _svc(tts=[tt])
    assert (await _build(svc, _utc(MON, 5))).today_timetable == ()


async def test_berlin_timetable_seen_by_a_utc_household():
    svc, _ = _svc(tts=[_tt()], prefs=_Prefs(tz="UTC"))
    # 22:30 UTC Sunday is 00:30 Monday in Berlin → Monday's lessons.
    bundle = await _build(svc, _utc(MON - timedelta(days=1), 22, 30))
    [today] = bundle.today_timetable
    assert today.date == MON
    first = today.lessons[0]
    assert first.lesson.start == time(8, 0)
    assert first.start_at == _utc(MON, 6)  # 08:00 CEST
    assert first.end_at == _utc(MON, 6, 45)


async def test_dst_day_lessons_are_placed_deterministically():
    # Sunday 2026-03-29, Berlin springs forward 02:00 → 03:00.
    dst = date(2026, 3, 29)
    tt = _tt(
        days=(6,),
        entries=(
            _entry("s1", 6, time(1, 30), time(2, 30), title="Nacht"),
            _entry("s2", 6, time(8, 0), time(8, 45), title="Mathe"),
        ),
    )
    svc, _ = _svc(tts=[tt])
    [today] = (await _build(svc, _utc(dst, 5))).today_timetable
    night, maths = today.lessons
    assert night.start_at == _utc(dst, 0, 30)  # 01:30 CET
    assert night.end_at == _utc(dst, 1, 30)  # 02:30 doesn't exist → 03:30 CEST
    assert maths.start_at == _utc(dst, 6)  # 08:00 CEST


async def test_cancelled_lessons_are_included():
    tt = _tt(
        overrides=(
            TimetableOverride(
                id="o1", date=MON, kind=OverrideKind.CANCEL, entry_id="m2"
            ),
        )
    )
    svc, _ = _svc(tts=[tt])
    [today] = (await _build(svc, _utc(MON, 5))).today_timetable
    status = {ls.lesson.source_id: ls.lesson.status for ls in today.lessons}
    assert status == {
        "m1": LessonStatus.NORMAL,
        "m2": LessonStatus.CANCELLED,
        "mb": LessonStatus.NORMAL,
    }


async def test_timetable_and_lesson_caps():
    many = tuple(
        _entry(
            f"e{i}",
            0,
            time(6 + i // 2, 30 * (i % 2)),
            time(6 + i // 2, 25 + 30 * (i % 2)),
            title=f"Fach {i}",
        )
        for i in range(24)
    )
    extra = tuple(
        TimetableOverride(
            id=f"x{i}",
            date=MON,
            kind=OverrideKind.ADD,
            start=time(19, 10 * i),
            end=time(19, 10 * i + 5),
            title="Extra",
        )
        for i in range(3)
    )
    tts = [
        _tt(f"tt-{i}", entries=many, overrides=extra)
        for i in range(MAX_TODAY_TIMETABLES + 2)
    ]
    svc, _ = _svc(tts=tts)
    bundle = await _build(svc, _utc(MON, 5))
    assert len(bundle.today_timetable) == MAX_TODAY_TIMETABLES
    assert all(len(t.lessons) == 24 for t in bundle.today_timetable)


async def test_timetable_slice_is_fail_soft():
    svc, _ = _svc(timetable_service=_Boom())
    bundle = await _build(svc, _utc(MON, 5))
    assert bundle.today_timetable == ()


# ─── today_events ────────────────────────────────────────────────────────


async def test_today_events_include_a_morning_event_that_already_ended():
    early = _event("dentist", _utc(MON, 7), _utc(MON, 8))
    later = _event("tea", _utc(MON, 15), _utc(MON, 16))
    tomorrow = _event(
        "gym", _utc(MON + timedelta(days=1), 7), _utc(MON + timedelta(days=1), 8)
    )
    svc, _ = _svc(events=[later, early, tomorrow])
    bundle = await _build(svc, _utc(MON, 12))
    assert [e.id for e in bundle.today_events] == ["dentist", "tea"]
    # upcoming_events still starts at now.
    assert [e.id for e in bundle.upcoming_events] == ["tea", "gym"]


async def test_today_events_use_the_household_day():
    # Berlin day of Monday = Sun 22:00Z … Mon 22:00Z.
    late_sunday_utc = _event(
        "late", _utc(MON - timedelta(days=1), 22, 30), _utc(MON - timedelta(days=1), 23)
    )
    svc, calendar = _svc(events=[late_sunday_utc], prefs=_Prefs(tz="Europe/Berlin"))
    bundle = await _build(svc, _utc(MON, 5))
    assert [e.id for e in bundle.today_events] == ["late"]
    start, end = calendar.calls[-1]
    assert (start, end) == (_utc(MON - timedelta(days=1), 22), _utc(MON, 22))


async def test_today_events_capped():
    events = [
        _event(f"e{i:02}", _utc(MON, 1) + timedelta(minutes=i), _utc(MON, 2))
        for i in range(cs.MAX_TODAY_EVENTS + 5)
    ]
    svc, _ = _svc(events=events)
    bundle = await _build(svc, _utc(MON, 0, 30))
    assert len(bundle.today_events) == cs.MAX_TODAY_EVENTS
    assert bundle.today_events[0].id == "e00"


async def test_today_events_fail_soft():
    class _Broken(_Calendar):
        async def list_events_for_user_in_range(self, *a, **kw):
            raise RuntimeError("db gone")

    svc, _ = _svc()
    svc._calendar = _Broken()
    bundle = await _build(svc, _utc(MON, 5))
    assert bundle.today_events == ()
    assert bundle.upcoming_events == ()


@pytest.mark.parametrize("prefs", [None, "broken"])
async def test_household_tz_falls_back_to_utc(prefs):
    class _BrokenPrefs(_Prefs):
        async def get_household(self):
            raise RuntimeError("no prefs")

    svc, calendar = _svc()
    svc._preferences = None if prefs is None else _BrokenPrefs()
    await _build(svc, _utc(MON, 5))
    assert calendar.calls[-1] == (_utc(MON, 0), _utc(MON + timedelta(days=1), 0))


async def test_today_events_drop_all_day_events_of_other_days():
    yesterday = _event(
        "yesterday",
        _utc(MON - timedelta(days=1), 0),
        _utc(MON, 0),
        all_day=True,
    )
    today = _event(
        "today", _utc(MON, 0), _utc(MON + timedelta(days=1), 0), all_day=True
    )
    svc, _ = _svc(events=[yesterday, today], prefs=_Prefs(tz="Europe/Berlin"))
    bundle = await _build(svc, _utc(MON, 5))
    assert [e.id for e in bundle.today_events] == ["today"]
    # West of UTC, tomorrow's all-day event overlaps the local evening.
    tomorrow = _event(
        "tomorrow",
        _utc(MON + timedelta(days=1), 0),
        _utc(MON + timedelta(days=2), 0),
        all_day=True,
    )
    svc, _ = _svc(events=[tomorrow, today], prefs=_Prefs(tz="America/Bogota"))
    bundle = await _build(svc, _utc(MON, 20))  # 15:00 Monday in Bogotá
    assert [e.id for e in bundle.today_events] == ["today"]


# ─── Pinned space timetables ─────────────────────────────────────────────


async def test_pinned_space_timetables_join_the_today_card():
    mine = _tt("tt-mine")
    pinned = _tt("tt-class", assignees=())
    space = _SpaceTimetables([pinned])
    users = _Users('{"timetable_home_pins": ["tt-class", "tt-left-space"]}')
    svc, _ = _svc(tts=[mine], users=users, space_timetables=space)
    bundle = await _build(svc, _utc(MON, 5))
    assert [t.timetable_id for t in bundle.today_timetable] == ["tt-mine", "tt-class"]
    # The service is asked with the parsed pins; it drops the ones the
    # user may no longer read (a space they left, a feature turned off).
    assert space.calls == [("u-anna", ("tt-class", "tt-left-space"))]


async def test_no_pins_means_no_space_lookup():
    space = _SpaceTimetables([_tt("tt-class", assignees=())])
    svc, _ = _svc(users=_Users("{}"), space_timetables=space)
    assert (await _build(svc, _utc(MON, 5))).today_timetable == ()
    assert space.calls == []


async def test_pinned_space_timetables_are_fail_soft():
    mine = _tt("tt-mine")
    users = _Users('{"timetable_home_pins": ["tt-class"]}')
    svc, _ = _svc(tts=[mine], users=users, space_timetables=_SpaceTimetables(boom=True))
    bundle = await _build(svc, _utc(MON, 5))
    assert [t.timetable_id for t in bundle.today_timetable] == ["tt-mine"]


async def test_pins_respect_the_today_cap():
    pins = [_tt(f"tt-p{i}", assignees=()) for i in range(MAX_TODAY_TIMETABLES + 2)]
    users = _Users(
        '{"timetable_home_pins": [' + ",".join(f'"{t.id}"' for t in pins) + "]}"
    )
    svc, _ = _svc(users=users, space_timetables=_SpaceTimetables(pins))
    bundle = await _build(svc, _utc(MON, 5))
    assert len(bundle.today_timetable) == MAX_TODAY_TIMETABLES
