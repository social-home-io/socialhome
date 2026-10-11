"""Crafted dashboard snapshots + a fake snapshot service for view tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from socialhome.domain.household_dashboard import (
    DashboardEvent,
    DashboardLesson,
    DashboardMessage,
    DashboardPerson,
    DashboardSchoolDay,
    DashboardShoppingItem,
    DashboardSticky,
    DashboardTask,
    HomeSnapshot,
    PersonSnapshot,
)

BERLIN = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 10, 12)  # a Monday
TOMORROW = TODAY + timedelta(days=1)
ALL_SECTIONS = frozenset(
    {"calendar", "tasks", "stickies", "presence", "timetable", "shopping"}
)


def at(hh: int, mm: int = 0, day: date = TODAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=BERLIN)


NOW = at(14, 32)

ANNA = DashboardPerson(
    user_id="u-anna",
    display_name="Anna",
    initials="A",
    color="#C44D72",
    picture_hash="pic1",
    picture_view=False,
    presence="zone",
    zone_name="Work",
    status_emoji="😴",
)
PASCAL = DashboardPerson(
    user_id="u-pascal",
    display_name="Pascal",
    initials="P",
    color="#357BBB",
    picture_hash=None,
    picture_view=False,
    presence="home",
)
MIA = DashboardPerson(
    user_id="u-mia",
    display_name="Mia",
    initials="M",
    color="#3F7D57",
    picture_hash=None,
    picture_view=False,
    presence="home",
)
LEO = DashboardPerson(
    user_id="u-leo",
    display_name="Leo",
    initials="L",
    color="#AD780C",
    picture_hash=None,
    picture_view=True,
    presence="away",
)
PEOPLE = (PASCAL, ANNA, MIA, LEO)


def event(
    eid: str,
    title: str,
    start: datetime,
    end: datetime,
    *people: str,
    all_day: bool = False,
    color: str | None = "#357BBB",
) -> DashboardEvent:
    return DashboardEvent(
        id=eid,
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        person_ids=tuple(people),
        color=color,
        location="Secret Street 1",
    )


def lesson(
    title: str | None,
    start: datetime,
    end: datetime,
    *,
    person: str = "u-mia",
    status: str = "normal",
    kind: str = "lesson",
    **kw,
) -> DashboardLesson:
    return DashboardLesson(
        person_id=person,
        start=start,
        end=end,
        kind=kind,
        status=status,
        title=title,
        color_hex=kw.pop("color_hex", "#5462C4"),
        **kw,
    )


MATHS = lesson("Maths", at(8, 20), at(9, 5), icon="📐", room="B12", teacher="Keller")
SPORT = lesson("Sport", at(10, 15), at(11, 0), status="cancelled", icon="⚽")
ART = lesson("Art", at(14, 0), at(15, 0), status="changed", room="B7", icon="🎨")
ENGLISH = lesson("English", at(15, 5), at(15, 50), status="added", icon="🇬🇧")
BREAK = lesson(None, at(9, 5), at(9, 20), kind="break")

MIA_SCHOOL = DashboardSchoolDay(
    person_id="u-mia",
    date=TODAY,
    timetable_name="5b",
    lessons=(MATHS, BREAK, SPORT, ART, ENGLISH),
    first_start=at(8, 20),
    last_end=at(15, 50),
    changes=(SPORT, ART, ENGLISH),
)

DENTIST = event("e1", "Dentist", at(10), at(11), "u-pascal")
WORK = event("e2", "Work", at(9), at(17), "u-anna", color="#C44D72")
SWIM = event("e3", "Swimming", at(15), at(16), "u-leo")
DINNER = event(
    "e4", "Dinner with Gran", at(18, 30), at(20), *[p.user_id for p in PEOPLE]
)
BIRTHDAY = event(
    "e5", "Mia's birthday", at(0), at(0, day=TOMORROW), "u-mia", all_day=True
)

CHORES = (
    DashboardTask(
        id="t1",
        title="Empty dishwasher",
        due=TODAY,
        overdue_days=0,
        done=False,
        person_ids=("u-mia",),
    ),
    DashboardTask(
        id="t2",
        title="Taxes",
        due=TODAY - timedelta(days=2),
        overdue_days=2,
        done=False,
        person_ids=("u-pascal",),
    ),
    DashboardTask(
        id="t3",
        title="Laundry",
        due=TODAY,
        overdue_days=0,
        done=True,
        person_ids=("u-anna",),
    ),
)

SHOPPING = tuple(
    DashboardShoppingItem(id=f"s{i}", text=text)
    for i, text in enumerate(["Milk", "Bread", "Eggs", "Apples", "Rice", "Tea", "Oats"])
)

STICKIES = (
    DashboardSticky(id="n1", content="Plumber Tue 9am", color="#FFEE88"),
    DashboardSticky(id="n2", content="Bad colour", color="red;}body{x:y"),
)


def message(
    mid: str, sender: str, minutes_ago: float, kind: str = "text", text: str = ""
) -> DashboardMessage:
    return DashboardMessage(
        id=mid,
        sender_id=sender,
        kind=kind,
        text=text,
        sent_at=NOW - timedelta(minutes=minutes_ago),
    )


#: Anna 4 min ago (new), a photo from Pascal 40 min ago, a voice note
#: from Mia with its transcript two hours ago.
MESSAGES = (
    message("m1", "u-anna", 4, text='Dinner at 7 <b>"sharp"</b> @Pascal'),
    message("m2", "u-pascal", 40, kind="photo"),
    message("m3", "u-mia", 125, kind="voice", text="Can I go to Lena's?"),
)


def home(**kw) -> HomeSnapshot:
    base = HomeSnapshot(
        now=NOW,
        today=TODAY,
        household_name="The Vizeli Home",
        sections=ALL_SECTIONS,
        is_night=False,
        people=PEOPLE,
        events_today=(BIRTHDAY, WORK, DENTIST, SWIM, DINNER),
        next_up=SWIM,
        chores_today=CHORES,
        shopping=SHOPPING,
        shopping_total=11,
        stickies=STICKIES,
        school=(MIA_SCHOOL,),
        tomorrow_first=lesson("Maths", at(7, 30, TOMORROW), at(8, 15, TOMORROW)),
    )
    return replace(base, **kw)


def person(who: DashboardPerson = ANNA, **kw) -> PersonSnapshot:
    base = PersonSnapshot(
        person=who,
        now=NOW,
        today=TODAY,
        sections=ALL_SECTIONS,
        is_night=False,
        now_item=WORK,
        next_item=DINNER,
        today_items=(WORK, DINNER),
        tomorrow_items=(
            event(
                "e9",
                "Mia's birthday",
                at(0, day=TOMORROW),
                at(0, day=TOMORROW + timedelta(days=1)),
                "u-anna",
                all_day=True,
            ),
        ),
        chores=(CHORES[2],),
        people=PEOPLE,
    )
    return replace(base, **kw)


def mia_page(**kw) -> PersonSnapshot:
    kw.setdefault("now", at(8, 30))
    return person(
        MIA,
        now_item=MATHS,
        next_item=ART,
        today_items=(MATHS, BREAK, SPORT, ART, ENGLISH, DINNER),
        tomorrow_items=(lesson("Maths", at(7, 30, TOMORROW), at(8, 15, TOMORROW)),),
        chores=(CHORES[0],),
        school=MIA_SCHOOL,
        school_tomorrow_first=lesson("Maths", at(7, 30, TOMORROW), at(8, 15, TOMORROW)),
        **kw,
    )


class FakeDashboardService:
    """Stands in for :class:`HouseholdDashboardService` in view tests."""

    def __init__(self) -> None:
        self.home_snapshot = home()
        self.pages: dict[str, PersonSnapshot] = {
            "u-anna": person(),
            "u-mia": mia_page(),
        }
        self.pictures: dict[str, tuple[bytes, str]] = {}
        self.calls: list[str] = []

    async def home(self, *, now: datetime | None = None) -> HomeSnapshot:
        self.calls.append("home")
        return self.home_snapshot

    async def person(
        self, user_id: str, *, now: datetime | None = None
    ) -> PersonSnapshot | None:
        self.calls.append(f"person:{user_id}")
        return self.pages.get(user_id)

    async def avatar(self, user_id: str) -> tuple[bytes, str] | None:
        self.calls.append(f"avatar:{user_id}")
        return self.pictures.get(user_id)


LEO_READING = lesson("Lesen", at(8), at(8, 45), person="u-leo")
LEO_BREAK = lesson(None, at(8, 45), at(9), person="u-leo", kind="break", label="Pause")
LEO_GYM = lesson("Turnen", at(9), at(9, 45), person="u-leo", status="cancelled")
LEO_ART = lesson("Kunst", at(10), at(11), person="u-leo", icon="🎨")
LEO_SCHOOL = DashboardSchoolDay(
    person_id="u-leo",
    date=TODAY,
    timetable_name="1a",
    lessons=(LEO_READING, LEO_BREAK, LEO_GYM, LEO_ART),
    first_start=at(8),
    last_end=at(11),
    changes=(LEO_GYM,),
)
LEO_CHORES = (
    DashboardTask(
        id="t-cat",
        title="Katze füttern",
        due=TODAY,
        overdue_days=0,
        done=False,
        person_ids=("u-leo",),
    ),
    DashboardTask(
        id="t-teeth",
        title="Zähneputzen",
        due=TODAY,
        overdue_days=0,
        done=True,
        person_ids=("u-leo",),
    ),
)


def leo_page(**kw) -> PersonSnapshot:
    """Leo (picture view) at 10:30 on a school day: Art runs, gym was
    cancelled, swimming is next."""
    first = lesson(
        "Lesen", at(8, day=TOMORROW), at(8, 45, day=TOMORROW), person="u-leo"
    )
    base = person(
        LEO,
        now=at(10, 30),
        now_item=LEO_ART,
        next_item=SWIM,
        today_items=(LEO_READING, LEO_BREAK, LEO_GYM, LEO_ART, SWIM, DINNER),
        tomorrow_items=(first,),
        chores=LEO_CHORES,
        school=LEO_SCHOOL,
        school_tomorrow_first=first,
    )
    return replace(base, **kw)
