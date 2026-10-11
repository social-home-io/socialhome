"""Unit tests for :class:`HouseholdDashboardService` — the wall tablet's
household-safe read model. In-memory fakes for every injected service
(the timetable service is the real one over an in-memory repo, and the
calendar fake expands recurrences with the repository's own expander)."""

from __future__ import annotations

import dataclasses
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pytest

from socialhome.domain.calendar import Calendar, CalendarEvent
from socialhome.domain.household_dashboard import (
    PERSON_PALETTE,
    TIMETABLE_COLOR_HEX,
    DashboardEvent,
    DashboardLesson,
    DashboardPersonSettings,
    DashboardSettings,
    HomeSnapshot,
    PersonSnapshot,
    hash_color,
)
from socialhome.domain.preferences import HouseholdPreferences
from socialhome.domain.presence import PersonPresence
from socialhome.domain.shopping import ShoppingItem
from socialhome.domain.sticky import Sticky
from socialhome.domain.task import Task, TaskList, TaskStatus
from socialhome.domain.timetable import (
    EntryKind,
    OverrideKind,
    Timetable,
    TimetableEntry,
    TimetableOverride,
)
from socialhome.domain.user import User, UserStatus
from socialhome.repositories.calendar_repo import _expand_window
from socialhome.services import household_dashboard_service as dashboard_module
from socialhome.services.household_dashboard_service import (
    HouseholdDashboardService,
)
from socialhome.services.timetable_service import TimetableService
from socialhome.utils.timezones import local_instant, zone_of

BERLIN = "Europe/Berlin"
MONDAY = date(2026, 10, 12)  # a Monday, CEST (UTC+2)
TUESDAY = MONDAY + timedelta(days=1)
SATURDAY = date(2026, 10, 17)


def at(d: date, hh: int, mm: int = 0, tz: str = BERLIN) -> datetime:
    """The UTC instant of wall-clock ``hh:mm`` on ``d`` in ``tz``."""
    return local_instant(d, time(hh, mm), tz)


# ─── Fakes ───────────────────────────────────────────────────────────────


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: set[str] = set()

    def hit(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"{name} exploded")


class FakeUsers(Recorder):
    def __init__(self, users: list[User]) -> None:
        super().__init__()
        self.users = users
        self.pictures: dict[str, tuple[bytes, str]] = {}

    async def list_active(self) -> list[User]:
        self.hit("list_active")
        return list(self.users)

    async def get_picture(self, user_id: str) -> tuple[bytes, str] | None:
        self.hit("get_picture")
        return self.pictures.get(user_id)


class FakePresence(Recorder):
    def __init__(self, rows: list[PersonPresence]) -> None:
        super().__init__()
        self.rows = rows

    async def list_presence(self) -> list[PersonPresence]:
        self.hit("list_presence")
        return list(self.rows)


class FakeCalendar(Recorder):
    def __init__(self, calendars: list[Calendar], events: list[CalendarEvent]):
        super().__init__()
        self.calendars = calendars
        self.events = events
        self.bad_calendars: set[str] = set()

    async def list_calendars(self, username: str, *, scope: str = "user"):
        self.hit("list_calendars")
        assert scope == "household"
        return list(self.calendars)

    async def list_events_in_range(self, calendar_id: str, *, start: str, end: str):
        self.hit("list_events_in_range")
        if calendar_id in self.bad_calendars:
            raise RuntimeError("bad calendar")
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
        rows = [
            ev
            for ev in self.events
            if ev.calendar_id == calendar_id
            and (
                (ev.rrule is None and ev.start < e and ev.end > s)
                or (ev.rrule is not None and ev.start < e)
            )
        ]
        return _expand_window(rows, start=s, end=e)


class FakeTasks(Recorder):
    def __init__(self, lists: list[TaskList], tasks: list[Task]) -> None:
        super().__init__()
        self.lists = lists
        self.tasks = tasks
        self.bad_lists: set[str] = set()

    async def list_lists(self) -> list[TaskList]:
        self.hit("list_lists")
        return list(self.lists)

    async def list_tasks(
        self,
        list_id: str,
        *,
        include_done: bool = True,
        status: str | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
        **_: Any,
    ) -> list[Task]:
        self.hit("list_tasks")
        if list_id in self.bad_lists:
            raise RuntimeError("bad list")
        out = []
        for t in self.tasks:
            if t.list_id != list_id:
                continue
            if status is not None:
                if t.status != status:
                    continue
            elif not include_done and t.status == TaskStatus.DONE:
                continue
            if due_from and (t.due_date is None or t.due_date.isoformat() < due_from):
                continue
            if due_to and (t.due_date is None or t.due_date.isoformat() > due_to):
                continue
            out.append(t)
        return out


class FakeShopping(Recorder):
    def __init__(self, items: list[ShoppingItem]) -> None:
        super().__init__()
        self.items = items

    async def list_items(self, *, include_completed: bool = False):
        self.hit("list_items")
        assert include_completed is False
        return [i for i in self.items if not i.completed]


class FakeStickies(Recorder):
    def __init__(self, notes: list[Sticky]) -> None:
        super().__init__()
        self.notes = notes

    async def list(self, *, space_id: str | None):
        self.hit("list_stickies")
        assert space_id is None
        return list(self.notes)


class FakeTimetableRepo:
    def __init__(self, tts: list[Timetable]) -> None:
        self.tts = tts

    async def list_all(self) -> list[Timetable]:
        return list(self.tts)


class SpyTimetables(TimetableService):
    """The real service, counting calls (and able to fail)."""

    __slots__ = ("rec",)

    def __init__(self, tts: list[Timetable]) -> None:
        super().__init__(FakeTimetableRepo(tts))  # type: ignore[arg-type]
        self.rec = Recorder()

    async def list_all(self) -> list[Timetable]:
        self.rec.hit("list_all")
        return await super().list_all()


class FakePreferences(Recorder):
    def __init__(self, prefs: HouseholdPreferences) -> None:
        super().__init__()
        self.prefs = prefs

    async def get_household(self) -> HouseholdPreferences:
        self.hit("get_household")
        return self.prefs


class FakeSettings(Recorder):
    def __init__(self, settings: DashboardSettings) -> None:
        super().__init__()
        self.settings = settings

    async def get(self) -> DashboardSettings:
        self.hit("get_settings")
        return self.settings


# ─── Fixture data ────────────────────────────────────────────────────────

ANNA = User(
    user_id="u-anna",
    username="anna",
    display_name="Anna Maria",
    picture_hash="abc",
    status=UserStatus(emoji="🎉", text="Birthday!"),
)
BEN = User(
    user_id="u-ben",
    username="ben",
    display_name="Ben",
    is_minor=True,
    child_protection_enabled=True,
    preferences_json='{"timetable_home_pins": ["space-tt"]}',
)
CARL = User(user_id="u-carl", username="carl", display_name="Carl")  # hidden
DORA = User(
    user_id="u-dora",
    username="dora",
    display_name="Dora",
    status=UserStatus(emoji="🌴", text="old", expires_at="2026-01-01T00:00:00+00:00"),
)


def cal(cid: str, owner: str, color: str, kind: str = "personal") -> Calendar:
    return Calendar(
        id=cid, name=cid, color=color, owner_username=owner, calendar_type=kind
    )


def event(
    eid: str,
    calendar_id: str,
    title: str,
    start: datetime,
    end: datetime,
    **kw: Any,
) -> CalendarEvent:
    kw.setdefault("tz", BERLIN)
    return CalendarEvent(
        id=eid,
        calendar_id=calendar_id,
        summary=title,
        start=start,
        end=end,
        created_by="u-anna",
        **kw,
    )


def calendars() -> list[Calendar]:
    return [
        cal("c-anna", "anna", "#112233"),
        cal("c-ben", "ben", "#445566"),
        cal("c-carl", "carl", "#778899"),
        cal("c-space", "anna", "#000000", kind="space"),
        cal("c-ghost", "ghost", "#999999"),  # owner is no active user
    ]


def events() -> list[CalendarEvent]:
    return [
        event("dentist", "c-anna", "Dentist", at(MONDAY, 15), at(MONDAY, 16)),
        # A household event shared with Anna and Ben: one row per calendar.
        event(
            "dinner-a",
            "c-anna",
            "Dinner at Oma",
            at(MONDAY, 18),
            at(MONDAY, 20),
            client_event_uuid="dinner",
            location="Oma's",
        ),
        event(
            "dinner-b",
            "c-ben",
            "Dinner at Oma",
            at(MONDAY, 18),
            at(MONDAY, 20),
            client_event_uuid="dinner",
        ),
        # Weekly on Mondays since September — today is an occurrence.
        event(
            "piano",
            "c-ben",
            "Piano",
            at(date(2026, 9, 7), 16),
            at(date(2026, 9, 7), 17),
            rrule="FREQ=WEEKLY;BYDAY=MO",
        ),
        # Hidden Carl's calendar: Ben attends → shows for Ben only.
        event(
            "zoo",
            "c-carl",
            "Zoo",
            at(MONDAY, 12),
            at(MONDAY, 14),
            attendees=("u-ben", "u-carl", "u-remote"),
        ),
        # Hidden Carl's own event → dropped.
        event("secret", "c-carl", "Secret", at(MONDAY, 10), at(MONDAY, 11)),
        event(
            "holiday",
            "c-anna",
            "Holiday",
            at(MONDAY, 0),
            at(TUESDAY, 0),
            all_day=True,
        ),
        event("gym", "c-anna", "Gym", at(TUESDAY, 7), at(TUESDAY, 8)),
        event("late", "c-anna", "Late", at(TUESDAY, 23), at(TUESDAY, 23, 30)),
        event("space-ev", "c-space", "Space thing", at(MONDAY, 9), at(MONDAY, 10)),
        event("ghost-ev", "c-ghost", "Ghost", at(MONDAY, 9), at(MONDAY, 10)),
    ]


def entry(eid: str, weekday: int, start: time, end: time, title, **kw):
    return TimetableEntry(
        id=eid, weekday=weekday, start=start, end=end, title=title, **kw
    )


def ben_timetable() -> Timetable:
    stamp = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return Timetable(
        id="tt-ben",
        name="5b",
        created_by="u-anna",
        created_at=stamp,
        updated_at=stamp,
        tz=BERLIN,
        assignees=("u-ben",),
        entries=(
            entry("m1", 0, time(8, 0), time(8, 45), "Mathe", room="101"),
            entry("m2", 0, time(8, 50), time(9, 35), "Deutsch", color="sky"),
            entry("m3", 0, time(9, 35), time(9, 55), "Pause", kind=EntryKind.BREAK),
            entry("m4", 0, time(9, 55), time(10, 40), "Sport", icon="⚽"),
            entry("t1", 1, time(7, 45), time(8, 30), "Englisch", teacher="Ms. K"),
            entry("t2", 1, time(8, 35), time(9, 20), "Mathe"),
        ),
        overrides=(
            TimetableOverride(
                id="o1", date=MONDAY, kind=OverrideKind.CANCEL, entry_id="m2"
            ),
            TimetableOverride(
                id="o2",
                date=MONDAY,
                kind=OverrideKind.REPLACE,
                entry_id="m4",
                room="Gym 2",
            ),
            TimetableOverride(
                id="o3",
                date=MONDAY,
                kind=OverrideKind.ADD,
                start=time(11, 0),
                end=time(11, 45),
                title="Kunst",
            ),
        ),
    )


def carl_timetable() -> Timetable:
    stamp = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return Timetable(
        id="tt-carl",
        name="Uni",
        created_by="u-carl",
        created_at=stamp,
        updated_at=stamp,
        tz=BERLIN,
        assignees=("u-carl",),
        entries=(entry("c1", 0, time(7, 0), time(8, 0), "Lecture"),),
    )


def music_timetable() -> Timetable:
    """A second plan Ben is assigned to (only some tests use it)."""
    stamp = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return Timetable(
        id="tt-music",
        name="Music school",
        created_by="u-anna",
        created_at=stamp,
        updated_at=stamp,
        tz=BERLIN,
        assignees=("u-ben",),
        entries=(entry("s1", 0, time(14, 0), time(15, 0), "Choir"),),
    )


def task(tid: str, title: str, due: date | None, **kw: Any) -> Task:
    stamp = kw.pop("updated_at", at(MONDAY, 6))
    return Task(
        id=tid,
        list_id=kw.pop("list_id", "l1"),
        title=title,
        status=kw.pop("status", TaskStatus.TODO),
        position=kw.pop("position", 0),
        created_by="u-anna",
        created_at=stamp,
        updated_at=stamp,
        due_date=due,
        **kw,
    )


def tasks() -> list[Task]:
    return [
        task("trash", "Take out trash", date(2026, 10, 10), assignees=("u-anna",)),
        task(
            "cat",
            "Feed cat",
            MONDAY,
            status=TaskStatus.DONE,
            updated_at=at(MONDAY, 7),
            assignees=("u-ben",),
        ),
        task(
            "plants",
            "Water plants",
            date(2026, 10, 11),
            status=TaskStatus.DONE,
            updated_at=at(date(2026, 10, 11), 9),
        ),
        task("room", "Clean room", MONDAY, assignees=("u-carl",)),
        task(
            "homework",
            "Homework",
            date(2026, 10, 15),
            assignees=("u-ben", "u-carl"),
        ),
        task("groceries", "Groceries", MONDAY, list_id="l2"),
        task(
            "archived",
            "Old",
            MONDAY,
            archived_at=at(MONDAY, 5),
            assignees=("u-anna",),
        ),
        task("someday", "Someday", None, assignees=("u-anna",)),
        task("far", "Far away", date(2026, 10, 30), assignees=("u-ben",)),
    ]


def presence() -> list[PersonPresence]:
    return [
        PersonPresence(
            username="anna",
            user_id="u-anna",
            display_name="Anna Maria",
            entity_id="person.anna",
            state="home",
            latitude=52.5201,
            longitude=13.4049,
            gps_accuracy_m=12.0,
        ),
        PersonPresence(
            username="ben",
            user_id="u-ben",
            display_name="Ben",
            entity_id="person.ben",
            state="zone",
            zone_name="School",
            latitude=52.5301,
            longitude=13.4149,
        ),
        PersonPresence(
            username="carl",
            user_id="u-carl",
            display_name="Carl",
            entity_id="person.carl",
            state="home",
        ),
        PersonPresence(
            username="dora",
            user_id="u-dora",
            display_name="Dora",
            entity_id="person.dora",
            state="unavailable",
        ),
    ]


class Harness:
    def __init__(
        self,
        *,
        prefs: HouseholdPreferences | None = None,
        settings: DashboardSettings | None = None,
        users: list[User] | None = None,
    ) -> None:
        self.users = FakeUsers(users if users is not None else [ANNA, BEN, CARL, DORA])
        self.presence = FakePresence(presence())
        self.calendar = FakeCalendar(calendars(), events())
        self.tasks = FakeTasks(
            [
                TaskList(id="l1", name="Chores", created_by="u-anna"),
                TaskList(id="l2", name="Errands", created_by="u-anna"),
            ],
            tasks(),
        )
        self.shopping = FakeShopping(
            [
                ShoppingItem(
                    id="s1",
                    text="Milk",
                    completed=False,
                    created_by="u-anna",
                    created_at="2026-10-12T06:00:00+00:00",
                    store="Aldi",
                ),
                ShoppingItem(
                    id="s2",
                    text="Bread",
                    completed=False,
                    created_by="u-carl",
                    created_at="2026-10-12T06:00:00+00:00",
                ),
                ShoppingItem(
                    id="s3",
                    text="Eggs",
                    completed=True,
                    created_by="u-anna",
                    created_at="2026-10-12T06:00:00+00:00",
                ),
            ]
        )
        self.stickies = FakeStickies(
            [
                Sticky(
                    id="n1",
                    author="u-anna",
                    content="Call plumber",
                    color="#FFEE88",
                    position_x=0,
                    position_y=0,
                    created_at="",
                    updated_at="",
                ),
                Sticky(
                    id="n2",
                    author="u-carl",
                    content="Carl's note",
                    color="#FFEE88",
                    position_x=0,
                    position_y=0,
                    created_at="",
                    updated_at="",
                ),
            ]
        )
        self.timetables = SpyTimetables([ben_timetable(), carl_timetable()])
        self.prefs = FakePreferences(
            prefs or HouseholdPreferences(household_name="Villa", tz=BERLIN)
        )
        self.settings = FakeSettings(
            settings
            or DashboardSettings(
                people=(("u-carl", DashboardPersonSettings(show=False)),)
            )
        )
        self.service = HouseholdDashboardService(
            user_service=self.users,  # type: ignore[arg-type]
            presence_service=self.presence,  # type: ignore[arg-type]
            calendar_service=self.calendar,  # type: ignore[arg-type]
            task_service=self.tasks,  # type: ignore[arg-type]
            shopping_service=self.shopping,  # type: ignore[arg-type]
            timetable_service=self.timetables,
            sticky_service=self.stickies,  # type: ignore[arg-type]
            preferences_service=self.prefs,  # type: ignore[arg-type]
            dashboard_settings=self.settings,  # type: ignore[arg-type]
        )

    async def home(self, now: datetime) -> HomeSnapshot:
        return await self.service.home(now=now)

    async def person(self, uid: str, now: datetime) -> PersonSnapshot | None:
        return await self.service.person(uid, now=now)


def ids(items) -> list[str]:
    return [i.id for i in items]


def titles(items) -> list[str | None]:
    return [i.title for i in items]


# ─── Home ────────────────────────────────────────────────────────────────


async def test_home_basics():
    h = Harness()
    snap = await h.home(at(MONDAY, 15, 30))
    assert snap.now == at(MONDAY, 15, 30)
    assert snap.now.tzinfo is not None and snap.now.utcoffset() == timedelta(hours=2)
    assert snap.today == MONDAY
    assert snap.household_name == "Villa"
    assert snap.is_night is False
    assert snap.sections == frozenset(
        {"calendar", "tasks", "stickies", "presence", "timetable", "shopping"}
    )


async def test_home_people_are_household_safe():
    h = Harness()
    snap = await h.home(at(MONDAY, 15, 30))
    assert [p.user_id for p in snap.people] == ["u-anna", "u-ben", "u-dora"]
    anna, ben, dora = snap.people
    assert anna.display_name == "Anna Maria"
    assert anna.initials == "AM"
    assert anna.color == "#112233"  # first personal calendar
    assert anna.picture_hash == "abc"
    assert anna.picture_view is False
    assert anna.presence == "home" and anna.zone_name is None
    assert anna.status_emoji == "🎉"
    assert anna.status_text is None  # show_status_text is off
    assert ben.presence == "zone" and ben.zone_name == "School"
    assert ben.picture_view is True  # child-protected minor default
    assert ben.color == "#445566"
    assert dora.presence == "unknown"
    assert dora.color == PERSON_PALETTE[2]  # no calendar → palette by position
    assert dora.status_emoji is None  # expired status


async def test_status_text_shown_when_enabled():
    h = Harness(
        settings=DashboardSettings(
            show_status_text=True,
            people=(("u-ben", DashboardPersonSettings(picture=False)),),
        )
    )
    snap = await h.home(at(MONDAY, 15, 30))
    anna = snap.people[0]
    assert anna.status_text == "Birthday!"
    assert [p.user_id for p in snap.people] == ["u-anna", "u-ben", "u-carl", "u-dora"]
    assert snap.people[1].picture_view is False  # explicit choice wins


async def test_home_events_today():
    h = Harness()
    snap = await h.home(at(MONDAY, 15, 30))
    assert titles(snap.events_today) == [
        "Holiday",  # all-day first
        "Zoo",
        "Dentist",
        "Piano",  # recurring occurrence today
        "Dinner at Oma",  # shared event appears once
    ]
    by_title = {e.title: e for e in snap.events_today}
    assert by_title["Dinner at Oma"].person_ids == ("u-anna", "u-ben")
    assert by_title["Dinner at Oma"].location == "Oma's"
    assert by_title["Dinner at Oma"].color == "#112233"
    assert by_title["Zoo"].person_ids == ("u-ben",)  # hidden / remote removed
    assert by_title["Holiday"].all_day is True
    piano = by_title["Piano"]
    assert piano.start == at(MONDAY, 16)
    assert piano.start.utcoffset() == timedelta(hours=2)  # household tz
    assert piano.person_ids == ("u-ben",)
    assert "Secret" not in by_title  # only hidden Carl
    assert "Space thing" not in by_title  # space calendars are not personal
    assert "Ghost" not in by_title  # owner isn't a household member


async def test_home_tz_boundary_uses_household_date():
    ny = "America/New_York"
    h = Harness(prefs=HouseholdPreferences(tz=ny))
    h.calendar.events = [
        event(
            "tonight", "c-anna", "Tonight", at(MONDAY, 22, tz=ny), at(MONDAY, 23, tz=ny)
        ),
        event(
            "after-midnight",
            "c-anna",
            "After midnight",
            at(TUESDAY, 0, 15, tz=ny),
            at(TUESDAY, 1, tz=ny),
        ),
    ]
    now = at(MONDAY, 23, 30, tz=ny)
    assert now.astimezone(timezone.utc).date() == TUESDAY  # UTC is already tomorrow
    snap = await h.home(now)
    assert snap.today == MONDAY
    assert titles(snap.events_today) == ["Tonight"]
    assert snap.tomorrow_first is not None
    assert snap.tomorrow_first.title == "After midnight"


async def test_home_next_up_is_the_soonest_event():
    h = Harness()
    snap = await h.home(at(MONDAY, 15, 30))
    assert isinstance(snap.next_up, DashboardEvent)
    assert snap.next_up.title == "Piano"


async def test_home_next_up_can_be_a_lesson():
    h = Harness()
    snap = await h.home(at(MONDAY, 10, 50))
    assert isinstance(snap.next_up, DashboardLesson)
    assert snap.next_up.title == "Kunst"
    assert snap.next_up.person_id == "u-ben"


async def test_home_next_up_none_when_the_day_is_done():
    h = Harness()
    snap = await h.home(at(MONDAY, 21, 0))
    assert snap.next_up is None


async def test_home_chores():
    h = Harness()
    snap = await h.home(at(MONDAY, 15, 30))
    assert ids(snap.chores_today) == ["trash", "groceries", "cat"]
    trash, groceries, cat = snap.chores_today
    assert trash.overdue_days == 2 and trash.done is False
    assert trash.list_name == "Chores"
    assert trash.person_ids == ("u-anna",)
    assert groceries.overdue_days == 0 and groceries.person_ids == ()
    assert groceries.list_name == "Errands"
    assert cat.done is True and cat.overdue_days == 0


async def test_home_shopping_and_stickies():
    h = Harness()
    snap = await h.home(at(MONDAY, 15, 30))
    assert [i.text for i in snap.shopping] == ["Milk", "Bread"]
    assert snap.shopping[0].store == "Aldi"
    assert snap.shopping_total == 2
    assert [s.content for s in snap.stickies] == ["Call plumber"]  # Carl hidden
    assert snap.stickies[0].color == "#FFEE88"


async def test_home_shopping_is_capped_but_counted():
    h = Harness()
    h.shopping.items = [
        ShoppingItem(
            id=f"i{n}", text=f"Item {n}", completed=False, created_by="u", created_at=""
        )
        for n in range(30)
    ]
    snap = await h.home(at(MONDAY, 15, 30))
    assert len(snap.shopping) == 20
    assert snap.shopping_total == 30


async def test_home_school():
    h = Harness()
    snap = await h.home(at(MONDAY, 8, 10))
    assert [d.person_id for d in snap.school] == ["u-ben"]  # Carl hidden
    day = snap.school[0]
    assert day.date == MONDAY
    assert day.timetable_name == "5b"
    assert titles(day.lessons) == ["Mathe", "Deutsch", "Pause", "Sport", "Kunst"]
    mathe, deutsch, pause, sport, kunst = day.lessons
    assert mathe.start == at(MONDAY, 8) and mathe.end == at(MONDAY, 8, 45)
    assert mathe.start.utcoffset() == timedelta(hours=2)
    assert mathe.room == "101"
    assert mathe.status == "normal" and mathe.kind == "lesson"
    # Uncoloured subject → the SPA's automatic subject colour.
    assert mathe.color is not None
    assert mathe.color_hex == TIMETABLE_COLOR_HEX[mathe.color]
    assert deutsch.status == "cancelled"
    assert deutsch.color == "sky" and deutsch.color_hex == "#357BBB"
    assert pause.kind == "break" and pause.color is None and pause.color_hex is None
    assert sport.status == "changed" and sport.room == "Gym 2" and sport.icon == "⚽"
    assert kunst.status == "added"
    assert kunst.color == hash_color("Kunst")  # not a subject of the plan
    assert kunst.timetable_name == "5b"
    assert [c.title for c in day.changes] == ["Deutsch", "Sport", "Kunst"]
    assert day.first_start == at(MONDAY, 8)
    assert day.last_end == at(MONDAY, 11, 45)


async def test_two_assigned_timetables_merge_into_one_school_day():
    h = Harness()
    h.service._timetables = SpyTimetables(  # type: ignore[attr-defined]
        [ben_timetable(), music_timetable()]
    )
    snap = await h.home(at(MONDAY, 8, 10))
    day = snap.school[0]
    assert day.timetable_name == "5b · Music school"
    assert titles(day.lessons)[-1] == "Choir"
    assert day.lessons[-1].timetable_name == "Music school"
    assert day.last_end == at(MONDAY, 15)


async def test_home_school_tomorrow_for_night_mode():
    h = Harness()
    snap = await h.home(at(MONDAY, 22, 30))
    assert snap.is_night is True
    assert [(ls.person_id, ls.title) for ls in snap.school_tomorrow] == [
        ("u-ben", "Englisch")
    ]
    assert snap.school_tomorrow[0].start == at(TUESDAY, 7, 45)
    assert snap.school_tomorrow[0].teacher == "Ms. K"
    # Gym at 07:00 beats Ben's 07:45 lesson.
    assert snap.tomorrow_first is not None
    assert snap.tomorrow_first.title == "Gym"


async def test_home_tomorrow_first_can_be_a_lesson():
    h = Harness()
    h.calendar.events = []
    snap = await h.home(at(MONDAY, 22, 30))
    assert isinstance(snap.tomorrow_first, DashboardLesson)
    assert snap.tomorrow_first.title == "Englisch"


async def test_no_school_on_the_weekend():
    h = Harness()
    snap = await h.home(at(SATURDAY, 9))
    assert snap.school == ()
    assert snap.school_tomorrow == ()
    page = await h.person("u-ben", at(SATURDAY, 9))
    assert page is not None
    assert page.school is None
    assert page.school_tomorrow_first is None


async def test_zero_length_event_is_a_point_in_time():
    h = Harness()
    h.calendar.events = [
        event("ping", "c-anna", "Ping", at(MONDAY, 9), at(MONDAY, 9)),
        event("edge", "c-anna", "Edge", at(TUESDAY, 0), at(TUESDAY, 0)),
    ]
    snap = await h.home(at(MONDAY, 8))
    assert titles(snap.events_today) == ["Ping"]  # midnight belongs to tomorrow
    assert snap.tomorrow_first is not None and snap.tomorrow_first.title == "Edge"


async def test_school_day_with_every_lesson_cancelled():
    h = Harness()
    tt = ben_timetable()
    h.timetables = SpyTimetables(
        [
            dataclasses.replace(
                tt,
                entries=(tt.entries[0],),
                overrides=(
                    TimetableOverride(
                        id="x", date=MONDAY, kind=OverrideKind.CANCEL, entry_id="m1"
                    ),
                ),
            )
        ]
    )
    h.service._timetables = h.timetables  # type: ignore[attr-defined]
    snap = await h.home(at(MONDAY, 8, 10))
    day = snap.school[0]
    assert day.first_start is None and day.last_end is None
    assert [c.status for c in day.changes] == ["cancelled"]


async def test_a_broken_timetable_is_skipped(monkeypatch, caplog):
    h = Harness()
    h.service._timetables = SpyTimetables(  # type: ignore[attr-defined]
        [ben_timetable(), music_timetable()]
    )

    def boom(tt, d, **kw):
        if tt.id == "tt-music":
            raise ValueError("bad row")
        return real(tt, d, **kw)

    real = dashboard_module.td.today_timetable
    monkeypatch.setattr(dashboard_module.td, "today_timetable", boom)
    with caplog.at_level(logging.WARNING):
        snap = await h.home(at(MONDAY, 8, 10))
    assert snap.school[0].timetable_name == "5b"
    assert "tt-music" in caplog.text


async def test_night_window_wraps_midnight():
    h = Harness()
    assert (await h.home(at(MONDAY, 23))).is_night is True
    assert (await h.home(at(MONDAY, 3))).is_night is True
    assert (await h.home(at(MONDAY, 6))).is_night is False
    assert (await h.home(at(MONDAY, 21, 59))).is_night is False


async def test_naive_now_is_utc_and_default_now_works():
    h = Harness()
    snap = await h.home(datetime(2026, 10, 12, 13, 30))  # naive → UTC
    assert snap.now == at(MONDAY, 15, 30)
    live = await h.service.home()
    assert live.now.tzinfo is not None


# ─── Section toggles ─────────────────────────────────────────────────────


async def test_disabled_sections_are_empty_and_not_read():
    h = Harness(
        prefs=HouseholdPreferences(
            tz=BERLIN,
            feat_calendar=False,
            feat_tasks=False,
            feat_stickies=False,
            feat_presence=False,
            feat_timetable=False,
        )
    )
    snap = await h.home(at(MONDAY, 8, 10))
    assert snap.sections == frozenset({"shopping"})
    assert snap.events_today == () and snap.next_up is None
    assert snap.chores_today == ()
    assert snap.stickies == ()
    assert snap.school == () and snap.school_tomorrow == ()
    assert snap.tomorrow_first is None
    assert all(p.presence == "unknown" for p in snap.people)
    # Calendar off → the calendar service isn't called at all, so person
    # colours fall back to the palette (by position in the household).
    assert [p.color for p in snap.people] == [
        PERSON_PALETTE[0],
        PERSON_PALETTE[1],
        PERSON_PALETTE[2],
    ]
    assert h.calendar.calls == []
    assert h.tasks.calls == []
    assert h.stickies.calls == []
    assert h.presence.calls == []
    assert h.timetables.rec.calls == []
    assert snap.shopping_total == 2

    page = await h.person("u-ben", at(MONDAY, 8, 10))
    assert page is not None
    assert page.today_items == () and page.chores == () and page.school is None
    assert h.calendar.calls == []
    assert h.timetables.rec.calls == []


# ─── Degradation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("target", "method"),
    [
        ("presence", "list_presence"),
        ("calendar", "list_calendars"),
        ("tasks", "list_lists"),
        ("shopping", "list_items"),
        ("stickies", "list_stickies"),
    ],
)
async def test_a_failing_slice_degrades_alone(target, method, caplog):
    h = Harness()
    getattr(h, target).fail.add(method)
    with caplog.at_level(logging.WARNING):
        snap = await h.home(at(MONDAY, 8, 10))
    assert "exploded" in caplog.text
    assert len(snap.people) == 3
    if target != "shopping":
        assert snap.shopping_total == 2
    if target != "tasks":
        assert snap.chores_today
    if target != "calendar":
        assert snap.events_today
    if target != "stickies":
        assert snap.stickies
    assert snap.school


async def test_failing_slices_leave_the_rest():
    h = Harness()
    h.calendar.fail.add("list_calendars")
    snap = await h.home(at(MONDAY, 15, 30))
    assert snap.events_today == ()
    assert snap.people[0].color == PERSON_PALETTE[0]
    h.shopping.fail.add("list_items")
    snap = await h.home(at(MONDAY, 15, 30))
    assert snap.shopping == () and snap.shopping_total == 0


async def test_a_failing_timetable_read_empties_only_school():
    h = Harness()
    h.timetables.rec.fail.add("list_all")
    snap = await h.home(at(MONDAY, 8, 10))
    assert snap.school == ()
    assert snap.events_today


async def test_one_bad_calendar_is_skipped():
    h = Harness()
    h.calendar.bad_calendars.add("c-ben")
    snap = await h.home(at(MONDAY, 15, 30))
    assert "Piano" not in titles(snap.events_today)
    assert "Dentist" in titles(snap.events_today)


async def test_a_failing_user_list_leaves_an_empty_household():
    h = Harness()
    h.users.fail.add("list_active")
    snap = await h.home(at(MONDAY, 15, 30))
    assert snap.people == ()
    assert snap.events_today == ()  # nobody to attribute them to
    assert ids(snap.chores_today) == ["groceries"]  # unassigned chores stay
    assert await h.person("u-anna", at(MONDAY, 15, 30)) is None


# ─── Person page ─────────────────────────────────────────────────────────


async def test_person_unknown_or_hidden_is_none():
    h = Harness()
    assert await h.person("u-nobody", at(MONDAY, 15, 30)) is None
    assert await h.person("u-carl", at(MONDAY, 15, 30)) is None


async def test_person_page_for_an_adult():
    h = Harness()
    page = await h.person("u-anna", at(MONDAY, 15, 30))
    assert page is not None
    assert page.person.user_id == "u-anna"
    assert page.today == MONDAY and page.is_night is False
    assert titles(page.today_items) == ["Holiday", "Dentist", "Dinner at Oma"]
    assert page.now_item is not None and page.now_item.title == "Dentist"
    assert page.next_item is not None and page.next_item.title == "Dinner at Oma"
    assert titles(page.tomorrow_items) == ["Gym", "Late"]
    assert ids(page.chores) == ["trash"]
    assert page.school is None


async def test_person_page_for_a_pupil():
    h = Harness()
    page = await h.person("u-ben", at(MONDAY, 8, 10))
    assert page is not None
    assert page.person.picture_view is True
    assert page.now_item is not None and page.now_item.title == "Mathe"
    # Deutsch is cancelled, the break is no "next" — Sport is.
    assert page.next_item is not None and page.next_item.title == "Sport"
    assert page.school is not None and page.school.person_id == "u-ben"
    assert titles(page.today_items) == [
        "Mathe",
        "Deutsch",
        "Pause",
        "Sport",
        "Kunst",
        "Zoo",
        "Piano",
        "Dinner at Oma",
    ]
    assert titles(page.tomorrow_items) == ["Englisch", "Mathe"]
    assert page.school_tomorrow_first is not None
    assert page.school_tomorrow_first.title == "Englisch"
    # Open within a week (the 30th is too far) + done today; Carl removed.
    assert ids(page.chores) == ["homework", "cat"]
    assert page.chores[0].person_ids == ("u-ben",)


async def test_person_now_item_prefers_the_latest_start():
    h = Harness()
    h.calendar.events = [
        event("long", "c-ben", "Long", at(MONDAY, 8), at(MONDAY, 12)),
    ]
    page = await h.person("u-ben", at(MONDAY, 10, 0))
    assert page is not None
    assert page.now_item is not None and page.now_item.title == "Sport"


async def test_person_tomorrow_items_are_capped():
    h = Harness()
    h.calendar.events = [
        event(f"e{n}", "c-anna", f"E{n}", at(TUESDAY, 9 + n), at(TUESDAY, 9 + n, 30))
        for n in range(8)
    ]
    page = await h.person("u-anna", at(MONDAY, 15, 30))
    assert page is not None
    assert titles(page.tomorrow_items) == ["E0", "E1", "E2", "E3", "E4"]


async def test_many_lessons_tomorrow_never_hide_an_after_school_event():
    """Regression (mockup D2): six lessons tomorrow used to fill the cap,
    dropping the 15:00 piano lesson. Events and lessons are capped apart."""
    h = Harness()
    tt = ben_timetable()
    lessons = tuple(
        entry(f"x{n}", 1, time(8 + n, 0), time(8 + n, 45), f"L{n}") for n in range(6)
    )
    h.timetables = SpyTimetables([dataclasses.replace(tt, entries=lessons)])
    h.calendar.events = [
        event("piano", "c-ben", "Piano", at(TUESDAY, 15), at(TUESDAY, 16)),
    ]
    h.service = HouseholdDashboardService(
        user_service=h.users,  # type: ignore[arg-type]
        presence_service=h.presence,  # type: ignore[arg-type]
        calendar_service=h.calendar,  # type: ignore[arg-type]
        task_service=h.tasks,  # type: ignore[arg-type]
        shopping_service=h.shopping,  # type: ignore[arg-type]
        timetable_service=h.timetables,
        sticky_service=h.stickies,  # type: ignore[arg-type]
        preferences_service=h.prefs,  # type: ignore[arg-type]
        dashboard_settings=h.settings,  # type: ignore[arg-type]
    )
    page = await h.person("u-ben", at(MONDAY, 15, 30))
    assert page is not None
    assert "Piano" in titles(page.tomorrow_items)
    lessons_tomorrow = [i for i in page.tomorrow_items if i.item_type == "lesson"]
    assert len(lessons_tomorrow) == 5  # lessons are capped on their own


# ─── Privacy ─────────────────────────────────────────────────────────────


def _keys(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(str(k))
            out |= _keys(v)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            out |= _keys(v)
    return out


def _values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [x for v in value.values() for x in _values(v)]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [x for v in value for x in _values(v)]
    return [value]


async def test_no_location_and_no_hidden_person_anywhere():
    h = Harness()
    home = dataclasses.asdict(await h.home(at(MONDAY, 8, 10)))
    page = await h.person("u-ben", at(MONDAY, 8, 10))
    assert page is not None
    person = dataclasses.asdict(page)
    for blob in (home, person):
        keys = _keys(blob)
        assert not keys & {"latitude", "longitude", "gps", "gps_accuracy_m"}
        values = _values(blob)
        assert 52.5201 not in values and 13.4049 not in values
        assert "u-carl" not in values
        assert "Secret" not in values and "Lecture" not in values
        assert "Clean room" not in values and "Carl's note" not in values


async def test_zero_people():
    h = Harness(users=[])
    snap = await h.home(at(MONDAY, 15, 30))
    assert snap.people == ()
    assert snap.school == ()


async def test_one_person():
    h = Harness(users=[ANNA])
    snap = await h.home(at(MONDAY, 15, 30))
    assert [p.user_id for p in snap.people] == ["u-anna"]
    # The shared dinner keeps only the shown member.
    dinner = next(e for e in snap.events_today if e.title == "Dinner at Oma")
    assert dinner.person_ids == ("u-anna",)
    assert "Piano" not in titles(snap.events_today)


def test_zone_of_berlin_is_cest_on_the_test_day():
    assert at(MONDAY, 12).astimezone(zone_of(BERLIN)).utcoffset() == timedelta(hours=2)


async def test_pinned_space_timetables_never_count_as_school():
    """Regression: a Home pin is a personal choice, not "goes to school" —
    Ben pinned the "Choir" space timetable, yet only his assigned plan
    shows on the shared dashboard."""
    h = Harness()
    assert "space-tt" in BEN.preferences_json  # the pin is really there
    snap = await h.home(at(MONDAY, 8, 10))
    assert [d.timetable_name for d in snap.school] == ["5b"]
    assert "Choir" not in titles(snap.school[0].lessons)
    page = await h.person("u-ben", at(MONDAY, 8, 10))
    assert page is not None
    assert page.school is not None and page.school.timetable_name == "5b"
    assert "Choir" not in titles(page.today_items)


# ─── Review follow-ups ───────────────────────────────────────────────────


async def test_hidden_owner_copy_never_lends_its_colour_or_id():
    """Carl (hidden) owns the only copy of "Zoo"; Ben attends. The event
    shows for Ben in Ben's colour and without Carl's row id."""
    h = Harness()
    snap = await h.home(at(MONDAY, 15, 30))
    zoo = next(e for e in snap.events_today if e.title == "Zoo")
    assert zoo.person_ids == ("u-ben",)
    assert zoo.color == "#445566"  # Ben's colour, not Carl's #778899
    assert zoo.id != "zoo"
    assert "#778899" not in _values(dataclasses.asdict(snap))


async def test_hidden_copy_listed_first_never_wins_the_merge():
    h = Harness()
    h.calendar.calendars = [cal("c-carl", "carl", "#778899"), *calendars()]
    h.calendar.events = [
        event(
            "trip-c",
            "c-carl",
            "Trip",
            at(MONDAY, 10),
            at(MONDAY, 12),
            client_event_uuid="trip",
            location="Carl's copy",
        ),
        event(
            "trip-b",
            "c-ben",
            "Trip",
            at(MONDAY, 10),
            at(MONDAY, 12),
            client_event_uuid="trip",
            location="Ben's copy",
        ),
    ]
    snap = await h.home(at(MONDAY, 8))
    (trip,) = snap.events_today
    assert trip.id == "trip-b"
    assert trip.color == "#445566"
    assert trip.location == "Ben's copy"
    assert trip.person_ids == ("u-ben",)


async def test_the_creators_shown_copy_is_preferred():
    h = Harness()
    # Ben's calendar is read first, so his copy is seen first.
    h.calendar.calendars = [cal("c-ben", "ben", "#445566"), *calendars()]
    h.calendar.events = [
        event(
            f"party-{owner}",
            f"c-{owner}",
            "Party",
            at(MONDAY, 19),
            at(MONDAY, 22),
            client_event_uuid="party",
        )
        for owner in ("ben", "anna")  # Anna (the creator) listed second
    ]
    snap = await h.home(at(MONDAY, 8))
    (party,) = snap.events_today
    assert party.id == "party-anna" and party.color == "#112233"
    assert party.person_ids == ("u-ben", "u-anna")


async def test_shared_event_without_uuid_merges_by_content():
    h = Harness()
    h.calendar.events = [
        event(
            f"legacy-{owner}",
            f"c-{owner}",
            "Legacy",
            at(MONDAY, 19),
            at(MONDAY, 20),
            description="same",
            location="Home",
        )
        for owner in ("anna", "ben")
    ] + [
        # Same title and time but another description → a different event.
        event("other", "c-ben", "Legacy", at(MONDAY, 19), at(MONDAY, 20)),
    ]
    snap = await h.home(at(MONDAY, 8))
    assert len(snap.events_today) == 2
    merged = next(e for e in snap.events_today if e.id == "legacy-anna")
    assert merged.person_ids == ("u-anna", "u-ben")


async def test_next_up_looks_past_the_display_cap():
    h = Harness()
    h.calendar.events = [
        event(
            f"d{n}",
            "c-anna",
            f"Day {n:02d}",
            at(MONDAY, 0),
            at(TUESDAY, 0),
            all_day=True,
        )
        for n in range(31)
    ] + [event("later", "c-anna", "Later", at(MONDAY, 17), at(MONDAY, 18))]
    h.timetables = SpyTimetables([])
    h.service._timetables = h.timetables  # type: ignore[attr-defined]
    snap = await h.home(at(MONDAY, 15, 30))
    assert len(snap.events_today) == 30
    assert "Later" not in titles(snap.events_today)
    assert snap.next_up is not None and snap.next_up.title == "Later"


async def test_person_chores_are_not_lost_to_the_home_cap():
    h = Harness()
    h.tasks.tasks = [
        task(f"a{n}", f"Anna {n:02d}", date(2026, 10, 1), assignees=("u-anna",))
        for n in range(35)
    ] + [task("bens", "Ben's chore", MONDAY, assignees=("u-ben",))]
    page = await h.person("u-ben", at(MONDAY, 15, 30))
    assert page is not None
    assert ids(page.chores) == ["bens"]
    snap = await h.home(at(MONDAY, 15, 30))
    assert len(snap.chores_today) == 30  # home stays capped


async def test_chore_due_tomorrow_but_done_today_counts_as_done_today():
    h = Harness()
    h.tasks.tasks = [
        task(
            "early",
            "Done early",
            TUESDAY,
            status=TaskStatus.DONE,
            updated_at=at(MONDAY, 9),
        ),
        task("open-tomorrow", "Not yet", TUESDAY),
    ]
    snap = await h.home(at(MONDAY, 15, 30))
    assert ids(snap.chores_today) == ["early"]
    assert snap.chores_today[0].done is True


async def test_one_bad_task_list_is_skipped(caplog):
    h = Harness()
    h.tasks.bad_lists.add("l2")
    with caplog.at_level(logging.WARNING):
        snap = await h.home(at(MONDAY, 15, 30))
    assert ids(snap.chores_today) == ["trash", "cat"]
    assert "bad list" in caplog.text


async def test_timetables_are_read_once_per_snapshot():
    h = Harness()
    h.timetables._repo.tts.append(  # type: ignore[attr-defined]
        dataclasses.replace(ben_timetable(), id="tt-anna", assignees=("u-anna",))
    )
    await h.home(at(MONDAY, 8, 10))
    assert h.timetables.rec.calls == ["list_all"]
    h.timetables.rec.calls.clear()
    await h.person("u-ben", at(MONDAY, 8, 10))
    assert h.timetables.rec.calls == ["list_all"]


async def test_a_timetable_resolves_in_its_own_zone():
    """Like My Corner: Monday 20:00 in New York is already Tuesday 02:00
    in Berlin, so Ben's Berlin plan shows Tuesday's lessons."""
    ny = "America/New_York"
    h = Harness(prefs=HouseholdPreferences(tz=ny))
    snap = await h.home(at(MONDAY, 20, tz=ny))
    assert snap.today == MONDAY
    (day,) = snap.school
    assert titles(day.lessons) == ["Englisch", "Mathe"]
    assert day.lessons[0].start == at(TUESDAY, 7, 45)
    assert day.lessons[0].start.utcoffset() == timedelta(hours=-4)  # NY zone


async def test_person_colours_follow_calendars_only_while_the_calendar_is_on():
    on = await Harness().home(at(MONDAY, 15, 30))
    assert [p.color for p in on.people] == ["#112233", "#445566", PERSON_PALETTE[2]]
    h = Harness(prefs=HouseholdPreferences(tz=BERLIN, feat_calendar=False))
    off = await h.home(at(MONDAY, 15, 30))
    assert [p.color for p in off.people] == [
        PERSON_PALETTE[0],
        PERSON_PALETTE[1],
        PERSON_PALETTE[2],
    ]
    assert off.events_today == ()
    assert h.calendar.calls == []


# ─── Rail + avatars ──────────────────────────────────────────────────────


async def test_person_page_carries_every_shown_person_for_the_rail():
    h = Harness()
    page = await h.person("u-ben", at(MONDAY, 8, 10))
    assert page is not None
    assert [p.user_id for p in page.people] == ["u-anna", "u-ben", "u-dora"]
    assert page.person in page.people


async def test_avatar_only_for_shown_active_members():
    h = Harness()
    h.users.pictures = {
        "u-anna": (b"RIFF0000WEBPVP8 ", "abc"),
        "u-carl": (b"RIFF0000WEBPVP8 ", "def"),
    }
    assert await h.service.avatar("u-anna") == (b"RIFF0000WEBPVP8 ", "abc")
    assert await h.service.avatar("u-carl") is None  # hidden
    assert await h.service.avatar("u-nobody") is None  # unknown / inactive
    assert await h.service.avatar("u-ben") is None  # shown, no picture
    # Hidden and unknown people never reach the picture store.
    assert h.users.calls.count("get_picture") == 2


async def test_avatar_fails_soft():
    h = Harness()
    h.users.fail.add("get_picture")
    assert await h.service.avatar("u-anna") is None


# ─── Review round ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("emoji", "shown"),
    [
        ("🎉", "🎉"),
        ("👍🏽", "👍🏽"),
        ("👩‍👧", "👩‍👧"),
        ("Ärzt·Termin", None),  # text in the emoji field never reaches the wall
        ("🎉 party", None),
        ("", None),
    ],
)
async def test_status_emoji_only_when_purely_emoji(emoji, shown):
    anna = dataclasses.replace(ANNA, status=UserStatus(emoji=emoji, text="x"))
    h = Harness(users=[anna])
    snap = await h.home(at(MONDAY, 15, 30))
    assert snap.people[0].status_emoji == shown


async def test_avatar_reads_the_directory_for_hidden_people_too():
    """Uniform work for hidden vs unknown: no timing tells them apart."""
    h = Harness()
    h.users.pictures = {"u-carl": (b"RIFF0000WEBPVP8 ", "def")}
    assert await h.service.avatar("u-carl") is None
    assert h.users.calls == ["list_active"]
    h.users.calls.clear()
    assert await h.service.avatar("u-nobody") is None
    assert h.users.calls == ["list_active"]


async def test_palette_colours_follow_the_shown_order():
    """Hiding someone doesn't leave a gap in the fallback palette."""
    h = Harness(prefs=HouseholdPreferences(household_name="V", tz=BERLIN))
    h.calendar.calendars = []
    snap = await h.home(at(MONDAY, 15, 30))
    assert [p.color for p in snap.people] == list(PERSON_PALETTE[:3])
