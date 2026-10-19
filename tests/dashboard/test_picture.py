"""Tests for the picture view's presenter: clock, wedge, day parts, tiles."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.picture import (
    DAY_PARTS,
    FREE_HOME_ICON,
    FREE_ICON,
    WEDGE_FULL_MINUTES,
    analog_clock,
    day_part,
    hand_angles,
    item_icon,
    minutes_until,
    picture_view,
    rotate,
    sector_path,
    wedge,
)
from socialhome.dashboard.presenter import Fmt, person_colors
from socialhome.dashboard.render import DashboardRenderer
from socialhome.domain.calendar import DaySpan
from socialhome.domain.household_dashboard import DashboardTask
from socialhome.i18n import Catalog

from .factories import (
    ANNA,
    ART,
    BREAK,
    DINNER,
    LEO,
    MATHS,
    MIA,
    PASCAL,
    TODAY,
    TOMORROW,
    at,
    event,
    lesson,
    mia_page,
    person,
)

BERLIN = ZoneInfo("Europe/Berlin")
RENDERER = DashboardRenderer(
    Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")
)
T = RENDERER.translator("en")


def _fmt(page):
    return Fmt(
        now=page.now,
        today=page.today,
        t=T,
        people=page.people,
        colors=person_colors(page),
        own_color=page.person.color,
    )


def _view(page, tomorrow=()):
    return picture_view(page, _fmt(page), tomorrow=tomorrow)


# ─── Clock ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hh", "mm", "hour", "minute"),
    [
        (0, 0, 0, 0),
        (12, 0, 0, 0),
        (3, 0, 90, 0),
        (14, 30, 75, 180),
        (9, 45, 292.5, 270),
        (23, 59, 359.5, 354),
    ],
)
def test_hand_angles(hh, mm, hour, minute):
    assert hand_angles(at(hh, mm)) == (hour, minute)


def test_hand_angles_ignore_seconds():
    assert hand_angles(at(14, 30).replace(second=59)) == (75, 180)


def test_rotate_spells_whole_and_half_degrees_like_the_script():
    assert rotate(75) == "rotate(75 100 100)"
    assert rotate(292.5) == "rotate(292.5 100 100)"
    assert rotate(0.0, 50, 50) == "rotate(0 50 50)"


def test_minutes_until_rounds_up_and_never_goes_negative():
    now = at(14, 32)
    assert minutes_until(now, at(15)) == 28
    assert minutes_until(now, now + timedelta(seconds=20)) == 1
    assert minutes_until(now, now) == 0
    assert minutes_until(now, at(14)) == 0


def test_minutes_until_counts_real_minutes_across_dst():
    # 25 Oct 2026: Berlin's clocks go back at 03:00 CEST → 02:00 CET.
    before = datetime(2026, 10, 25, 2, 50, tzinfo=BERLIN)  # CEST
    after = datetime(2026, 10, 25, 2, 10, tzinfo=BERLIN, fold=1)  # CET
    assert after.astimezone(UTC) > before.astimezone(UTC)
    assert minutes_until(before, after) == 20
    # Spring forward: 29 Mar 2026 02:00 CET → 03:00 CEST.
    early = datetime(2026, 3, 29, 1, 50, tzinfo=BERLIN)
    late = datetime(2026, 3, 29, 3, 10, tzinfo=BERLIN)
    assert minutes_until(early, late) == 20


def test_sector_path_quarter_and_large_arc():
    assert sector_path(100, 100, 50, 0, 90) == "M100 100L100 50A50 50 0 0 1 150 100Z"
    big = sector_path(100, 100, 50, 0, 270)
    assert big.startswith("M100 100L100 50A50 50 0 1 1 ") and big.endswith("Z")
    assert sector_path(100, 100, 50, 90, 90) == "M100 100L150 100A50 50 0 0 1 100 150Z"


def test_wedge_runs_from_the_minute_hand_to_the_next_start():
    w = wedge(at(14, 30), at(14, 45), start_deg=180, cx=100, cy=100, r=50)
    assert w is not None
    assert w.minutes == 15 and not w.full
    assert w.path == sector_path(100, 100, 50, 180, 90)


def test_wedge_is_a_full_disc_from_an_hour_out():
    w = wedge(at(9), at(10), start_deg=0, cx=100, cy=100, r=50)
    assert w is not None and w.full and w.minutes == WEDGE_FULL_MINUTES
    assert w.path == ""
    far = wedge(at(9), at(13), start_deg=0, cx=100, cy=100, r=50)
    assert far is not None and far.full and far.minutes == 60


def test_no_wedge_once_it_started():
    assert wedge(at(9), at(9), start_deg=0, cx=100, cy=100, r=50) is None
    assert wedge(at(9), at(8), start_deg=0, cx=100, cy=100, r=50) is None


def test_analog_clock_hands_and_wedge_follow_the_household_time():
    clock = analog_clock(at(14, 32), label="It's 14:32", next_start=at(15), color="k1")
    assert clock.hour == rotate(76) and clock.minute == rotate(192)
    assert clock.wedge is not None and clock.wedge.minutes == 28
    assert clock.wedge.path == sector_path(100, 100, 88, 192, 168)
    assert clock.color == "k1" and clock.label == "It's 14:32"
    bare = analog_clock(at(22, 5), label="x")
    assert bare.wedge is None and bare.color == ""


def test_analog_clock_across_dst_shows_wall_time_and_real_minutes():
    now = datetime(2026, 10, 25, 2, 50, tzinfo=BERLIN)
    start = datetime(2026, 10, 25, 2, 10, tzinfo=BERLIN, fold=1)
    clock = analog_clock(now, label="x", next_start=start)
    assert clock.minute == rotate(300)
    assert clock.wedge is not None and clock.wedge.minutes == 20


# ─── Day parts ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hh", "key"),
    [
        (0, "morning"),
        (6, "morning"),
        (11, "morning"),
        (12, "afternoon"),
        (16, "afternoon"),
        (17, "evening"),
        (19, "evening"),
        (20, "night"),
        (23, "night"),
    ],
)
def test_day_part(hh, key):
    assert day_part(at(hh, 59)).key == key


def test_day_parts_are_the_four_icons_in_order():
    assert [p.icon for p in DAY_PARTS] == ["🌅", "☀️", "🌇", "🌙"]


def test_day_part_uses_wall_clock_time_in_the_household_zone():
    utc_noon = datetime(2026, 10, 12, 10, 30, tzinfo=UTC)  # 12:30 in Berlin
    assert day_part(utc_noon.astimezone(BERLIN)).key == "afternoon"


# ─── Icons ───────────────────────────────────────────────────────────────


def test_item_icon():
    assert item_icon(MATHS) == "📐"  # the timetable's own icon
    assert item_icon(lesson("Mathe", at(8), at(9))) == "🔢"  # guessed
    assert item_icon(lesson("Quidditch", at(8), at(9))) == "🔹"
    assert item_icon(BREAK) == "🍎"  # a break without icon or label
    assert (
        item_icon(lesson(None, at(9), at(9, 20), kind="break", label="Pause")) == "🧸"
    )
    assert item_icon(lesson(None, at(9), at(9, 20), kind="break", icon="🚌")) == "🚌"
    assert item_icon(lesson(None, at(9), at(10))) == "🔹"
    assert item_icon(DINNER) == "🍝"
    assert item_icon(event("x", "🎻 Concert", at(9), at(10))) == "🎻"
    assert item_icon(event("x", "Taxes", at(9), at(10))) == "🔹"


# ─── The whole picture page ──────────────────────────────────────────────


def test_now_and_next_cards():
    page = mia_page(now=at(8, 30))
    v = _view(page)
    assert v.now is not None and v.now.icon == "📐"
    assert v.now.label == "Now: Maths"
    assert v.next is not None and v.next.icon == "🎨"
    assert v.next.label == "Next: Art, in 5 h"
    # The NEXT card's own little timer: a full disc (more than an hour).
    assert v.next_pie is not None and v.next_pie.full
    # The clock's wedge in the NEXT item's colour.
    assert v.clock.wedge is not None and v.clock.color
    assert v.clock.color == _fmt(page).kc(ART.color_hex)


def test_next_pie_shrinks_within_the_last_hour():
    page = mia_page(now=at(13, 45))
    v = _view(page)
    assert v.next_pie is not None and not v.next_pie.full
    assert v.next_pie.minutes == 15
    assert v.next_pie.path == sector_path(50, 50, 46, 0, 90)


def test_free_time_card_when_nothing_runs_now():
    page = person(LEO, now_item=None, next_item=DINNER, today_items=(DINNER,))
    v = _view(page)
    # Leo is away → a toy, not a house.
    assert v.now is not None and v.now.icon == FREE_ICON == "🧸"
    assert v.now.label == "Free time"
    home = person(replace(LEO, presence="home"), now_item=None, next_item=DINNER)
    assert _view(home).now.icon == FREE_HOME_ICON == "🏠"
    no_presence = person(
        replace(LEO, presence="home"),
        now_item=None,
        next_item=DINNER,
        sections=frozenset({"calendar"}),
    )
    assert _view(no_presence).now.icon == FREE_ICON


def test_no_cards_without_now_or_next():
    v = _view(person(LEO, now_item=None, next_item=None))
    assert v.now is None and v.next is None and v.next_pie is None
    assert v.clock.wedge is None


def test_my_day_groups_by_day_part_with_past_current_and_cancelled():
    page = mia_page(now=at(10, 30))
    v = _view(page)
    parts = {p.key: p for p in v.parts}
    assert [p.key for p in v.parts] == ["morning", "afternoon", "evening", "night"]
    assert parts["morning"].state == "now"
    assert parts["afternoon"].state == "later" and parts["night"].state == "later"
    morning = parts["morning"].tiles
    assert [t.icon for t in morning] == ["📐", "🍎", "⚽"]
    maths, brk, sport = morning
    assert maths.past and not maths.current
    assert brk.past
    # Sport (10:15–11:00) runs now — but it's cancelled: ✕, never framed.
    assert sport.cancelled and not sport.current
    assert sport.label == "10:15–11:00 Sport, cancelled"
    assert [t.icon for t in parts["afternoon"].tiles] == ["🎨", "🇬🇧"]
    assert [t.icon for t in parts["evening"].tiles] == ["🍝"]
    assert parts["night"].tiles == ()
    assert maths.label == "08:20–09:05 Maths"
    assert not v.empty


def test_a_trip_begun_on_an_earlier_day_is_the_days_standing_context():
    """A trip that began yesterday evening isn't "this evening": it sits
    first (with the all-day things) and says its days, like every
    calendar in the app — never yesterday's start time."""
    trip = event(
        "trip",
        "Camp",
        at(18, day=TODAY - timedelta(days=1)),
        at(10, day=TOMORROW),
        "u-mia",
        days=DaySpan(TODAY - timedelta(days=1), TOMORROW),
    )
    page = replace(mia_page(now=at(10, 30)), today_items=(trip, MATHS))
    parts = {p.key: p for p in _view(page).parts}
    first = parts["morning"].tiles[0]
    assert first.caption == "Camp"
    assert first.label == "Sun 11 – Tue 13 · 3 days Camp"
    assert parts["evening"].tiles == ()


def test_the_running_item_is_framed():
    page = mia_page(now=at(14, 30))
    v = _view(page)
    afternoon = next(p for p in v.parts if p.key == "afternoon")
    art = afternoon.tiles[0]
    assert art.current and not art.past
    assert next(p for p in v.parts if p.key == "morning").state == "past"


def test_all_day_items_open_the_morning_and_never_dim():
    bday = event(
        "b", "Mia's birthday", at(0), at(0, day=TOMORROW), "u-mia", all_day=True
    )
    page = replace(mia_page(now=at(21)), today_items=(bday, MATHS))
    v = _view(page)
    morning = next(p for p in v.parts if p.key == "morning")
    assert [t.icon for t in morning.tiles] == ["🎂", "📐"]
    assert not morning.tiles[0].past and not morning.tiles[0].current
    assert morning.tiles[0].label == "All day Mia's birthday"
    assert morning.tiles[1].past


def test_empty_day_is_one_balloon():
    v = _view(person(LEO, now_item=None, next_item=None, today_items=()))
    assert v.empty and v.parts == ()


def test_jobs_today_overdue_and_done_today_but_not_later_ones():
    def task(tid, title, due, *, done=False, overdue=0):
        return DashboardTask(
            id=tid,
            title=title,
            due=due,
            overdue_days=overdue,
            done=done,
            person_ids=("u-leo",),
        )

    chores = (
        task("a", "Feed the cat", TODAY),
        task("b", "Brush teeth", TODAY, done=True),
        task("c", "Tidy room", TODAY - timedelta(days=1), overdue=1),
        task("d", "Water plants", TODAY + timedelta(days=2)),
        task("e", "Taxes", TODAY),
        task("f", "No date", None),
    )
    v = _view(person(LEO, chores=chores))
    assert [(j.icon, j.done) for j in v.jobs] == [
        ("🐱", False),
        ("🦷", True),
        ("🧹", False),
        ("🔹", False),
    ]
    assert v.jobs[0].label == "To do: Feed the cat"
    assert v.jobs[1].label == "Done: Brush teeth"
    assert v.jobs[2].label == "Overdue: Tidy room"


def test_no_jobs_when_tasks_are_off():
    page = person(LEO, sections=frozenset({"calendar"}))
    assert _view(page).jobs == ()


def test_home_and_away_faces_leave_out_the_page_owner():
    page = person(LEO)  # people: Pascal (home), Anna (zone), Mia (home), Leo
    v = _view(page)
    assert v.home == (PASCAL, MIA)
    assert v.away == (ANNA,)
    off = _view(person(LEO, sections=frozenset({"calendar"})))
    assert off.home == () and off.away == ()
    alone = _view(person(LEO, people=(LEO,)))
    assert alone.home == () and alone.away == ()
    unknown = person(LEO, people=(replace(PASCAL, presence="unknown"), LEO))
    assert _view(unknown).home == () and _view(unknown).away == ()


def test_tomorrow_tiles_and_sleeps():
    first = lesson("Maths", at(7, 30, TOMORROW), at(8, 15, TOMORROW))
    swim = event("s", "Swimming", at(15, day=TOMORROW), at(16, day=TOMORROW), "u-leo")
    v = _view(person(LEO), tomorrow=(first, swim))
    assert [t.icon for t in v.tomorrow] == ["🎒", "🏊"]
    assert v.tomorrow[0].label == "07:30 School"
    assert v.tomorrow[1].label == "15:00–16:00 Swimming"
    assert v.sleeps == 1
    assert _view(person(LEO)).sleeps == 0 and _view(person(LEO)).tomorrow == ()


@pytest.mark.parametrize(
    "started",
    [
        # A holiday Mon–Fri: all-day, started yesterday, sorts first.
        event(
            "trip",
            "Urlaub",
            at(0, day=TODAY - timedelta(days=1)),
            at(0, day=TOMORROW + timedelta(days=3)),
            "u-leo",
            all_day=True,
        ),
        # A sleepover: 18:00 today until 10:00 tomorrow — running now.
        event("so", "Sleepover", at(18), at(10, day=TOMORROW), "u-leo"),
    ],
)
def test_tomorrow_items_that_started_earlier_are_still_one_sleep_away(started):
    page = person(LEO, now=at(19), now_item=None, next_item=None)
    swim = event("s", "Swimming", at(15, day=TOMORROW), at(16, day=TOMORROW), "u-leo")
    v = _view(page, tomorrow=(started, swim))
    # Everything in the list is tomorrow's: one sleep, never 0.
    assert v.sleeps == 1
    # A tomorrow tile is never "now" or "over" today.
    assert [(t.current, t.past) for t in v.tomorrow] == [(False, False)] * 2


def test_clock_label_and_current_day_part():
    v = _view(mia_page(now=at(18, 5)))
    assert v.clock.label == "It's 18:05"
    assert v.part_now.key == "evening"


def test_night_item_icon():
    first = lesson("Maths", at(7, 30, TOMORROW), at(8, 15, TOMORROW))
    page = replace(mia_page(now=at(22, 30), is_night=True), school_tomorrow_first=first)
    v = _view(page)
    assert v.night is not None
    assert v.night.when == "tomorrow" and v.night.icon == "🎒"
    assert v.night.label == "Tomorrow 07:30: School"
    gym = event("g", "Gym", at(7, day=TOMORROW), at(8, day=TOMORROW), "u-mia")
    v2 = _view(replace(page, tomorrow_items=(gym,)))
    assert v2.night is not None and v2.night.icon == "🏋️"
    assert v2.night.label == "Tomorrow 07:00: Gym"
    day = _view(mia_page(now=at(9)))
    assert day.night is None
    nothing = _view(replace(page, tomorrow_items=(), school_tomorrow_first=None))
    assert nothing.night is None


def test_night_before_noon_shows_the_morning_item():
    page = replace(mia_page(now=at(5), is_night=True), next_item=MATHS)
    v = _view(page)
    assert v.night is not None and v.night.when == "today"
    # A lesson is "school" at night, as on the reader page.
    assert v.night.icon == "🎒"
    assert v.night.label == "Today 08:20: School"


# ─── Helper captions (for a grown-up reading along) ──────────────────────


def test_tiles_carry_their_title_as_a_caption():
    page = mia_page(now=at(10, 30))
    parts = {p.key: p for p in _view(page).parts}
    maths, brk, sport = parts["morning"].tiles
    assert (maths.caption, brk.caption, sport.caption) == ("Maths", "Break", "Sport")
    assert sport.cancelled  # the template strikes the caption through
    assert [t.caption for t in parts["evening"].tiles] == ["Dinner with Gran"]


def test_now_and_next_captions_carry_the_time():
    v = _view(mia_page(now=at(8, 30)))
    assert v.now is not None and v.now.caption == "Maths · 08:20"
    assert v.next is not None and v.next.caption == "Art · 14:00"


def test_an_all_day_next_card_says_all_day_not_midnight():
    bday = event(
        "b", "Mia's birthday", at(0), at(0, day=TOMORROW), "u-mia", all_day=True
    )
    v = _view(replace(mia_page(now=at(8, 30)), next_item=bday))
    assert v.next is not None and v.next.caption == "Mia's birthday · All day"


def test_free_time_caption():
    page = person(LEO, now_item=None, next_item=DINNER, today_items=(DINNER,))
    v = _view(page)
    assert v.now is not None and v.now.caption == "Free time"


def test_job_and_tomorrow_captions():
    first = lesson("Maths", at(7, 30, TOMORROW), at(8, 15, TOMORROW))
    swim = event("s", "Swimming", at(15, day=TOMORROW), at(16, day=TOMORROW), "u-leo")
    chores = (
        DashboardTask(
            id="a",
            title="Feed the cat",
            due=TODAY,
            overdue_days=0,
            done=False,
            person_ids=("u-leo",),
        ),
    )
    v = _view(person(LEO, chores=chores), tomorrow=(first, swim))
    assert [t.caption for t in v.tomorrow] == ["School", "Swimming"]
    assert [j.caption for j in v.jobs] == ["Feed the cat"]


def test_night_caption():
    first = lesson("Maths", at(7, 30, TOMORROW), at(8, 15, TOMORROW))
    page = replace(mia_page(now=at(22, 30), is_night=True), school_tomorrow_first=first)
    v = _view(page)
    assert v.night is not None and v.night.caption == "Tomorrow · School 07:30"
    morning = _view(replace(mia_page(now=at(5), is_night=True), next_item=MATHS))
    assert morning.night is not None
    assert morning.night.caption == "Today · School 08:20"
