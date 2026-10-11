"""Tests for the dashboard presenter: colours, timeline geometry, phrases."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import socialhome.i18n as i18n_pkg
from socialhome.dashboard.contrast import contrast_ratio
from socialhome.dashboard.presenter import (
    MAX_LANE_ROWS,
    MAX_LANES,
    ColorBook,
    Fmt,
    avatar_url,
    SchoolSpan,
    build_timeline,
    day_list,
    home_colors,
    leading_emoji,
    night_item,
    person_colors,
    presence_groups,
    school_changes,
    school_now,
    theme_css,
    view_key,
)
from socialhome.dashboard.render import STATIC_DIR
from socialhome.i18n import Catalog

from .factories import (
    ANNA,
    ART,
    BIRTHDAY,
    BREAK,
    DENTIST,
    DINNER,
    ENGLISH,
    LEO,
    MATHS,
    MIA,
    MIA_SCHOOL,
    PASCAL,
    PEOPLE,
    SPORT,
    SWIM,
    TODAY,
    TOMORROW,
    WORK,
    at,
    event,
    home,
    lesson,
    mia_page,
    person,
)

CATALOG = Catalog.from_directory(Path(i18n_pkg.__file__).parent / "messages")


def _t(locale: str = "en"):
    def t(key: str, **fmt: object) -> str:
        return CATALOG.gettext(key, locale=locale, **fmt)

    return t


def _fmt(snapshot=None, locale="en") -> Fmt:
    snap = snapshot or home()
    colors = ColorBook()
    for p in PEOPLE:
        colors.add(p.color)
    return Fmt(
        now=snap.now, today=snap.today, t=_t(locale), people=PEOPLE, colors=colors
    )


# ─── ColorBook / theme ───────────────────────────────────────────────────


def test_color_book_classes_and_css():
    book = ColorBook()
    book.add("#FFEE88")
    book.add("#ffee88")  # same colour, one class
    book.add("#123")
    book.add("red;}body{x:y")
    book.add(None)
    assert book.cls("#ffee88") == "k0"
    assert book.cls("#112233") == "k1"
    assert book.cls("red;}body{x:y") == ""
    assert book.cls("#abcdef") == ""  # never registered
    assert book.css() == (
        ".k0{--kc:#ffee88;--kt:#000000}.k1{--kc:#112233;--kt:#ffffff}"
    )


def test_theme_css_uses_a_readable_colour_in_both_schemes():
    # A mid-tone teal: readable on light, not on dark.
    css = theme_css("#2F6F5E")
    assert css == (
        "@media (prefers-color-scheme:light){:root{--dash-accent:#2f6f5e;"
        "--dash-accent-text:#ffffff}}"
    )


def test_theme_css_dark_only_for_a_light_colour():
    css = theme_css("#ffee88")
    assert "prefers-color-scheme:light" not in css
    assert "@media (prefers-color-scheme:dark){:root{--dash-accent:#ffee88;" in css


def test_theme_css_falls_back_when_contrast_fails_everywhere():
    # The default brand terracotta is 4.06:1 on white, 3.9:1 on dark.
    assert theme_css("#D2542A") == ""


@pytest.mark.parametrize("bad", [None, "", "blue", "#12;}x{", 5])
def test_theme_css_ignores_invalid(bad):
    assert theme_css(bad) == ""


def test_view_key_is_stable_and_sensitive():
    assert view_key("a", True) == view_key("a", True)
    assert view_key("a", True) != view_key("a", False)
    assert len(view_key()) == 16


def test_avatar_url():
    assert avatar_url(ANNA) == "/avatar/u-anna?v=pic1"
    assert avatar_url(PASCAL) is None
    odd = replace(ANNA, user_id="a/b c", picture_hash="x&y")
    assert avatar_url(odd) == "/avatar/a%2Fb%20c?v=x%26y"


# ─── Fmt ─────────────────────────────────────────────────────────────────


def test_fmt_span_and_titles():
    f = _fmt()
    assert f.span(WORK) == "09:00–17:00"
    assert f.span(BIRTHDAY) == "All day"
    point = event("p", "Call", at(12), at(12), "u-anna")
    assert f.span(point) == "12:00"
    assert f.title(WORK) == "Work"
    assert f.title(MATHS) == "Maths"
    assert f.title(BREAK) == "Break"
    labelled = lesson(None, at(12), at(13), label="Lunch")
    assert f.title(labelled) == "Lunch"
    assert f.title(lesson(None, at(12), at(13))) == ""


def test_fmt_people():
    f = _fmt()
    assert f.people(DINNER) == list(PEOPLE)
    assert f.people(MATHS) == [MIA]
    assert f.people(["u-leo", "u-gone", "u-anna"]) == [LEO, ANNA]


def test_fmt_past_and_now():
    f = _fmt()  # 14:32
    assert f.is_past(DENTIST) and not f.is_now(DENTIST)
    assert f.is_now(WORK) and not f.is_past(WORK)
    assert not f.is_past(SWIM) and not f.is_now(SWIM)
    assert not f.is_past(BIRTHDAY) and not f.is_now(BIRTHDAY)
    assert f.is_past(event("p", "Call", at(12), at(12), "u-anna"))
    assert not f.is_past(event("p", "Call", at(16), at(16), "u-anna"))


def test_fmt_phrases():
    f = _fmt()
    assert f.hhmm(at(7, 5)) == "07:05"
    assert f.rel(at(15)) == "in 28 min"
    assert f.until(at(17)) == "until 17:00"
    assert f.late(2) == "2 days late"
    assert f.due(TOMORROW) == "tomorrow"
    assert f.due(None) is None
    assert f.kc("#357BBB") == "k0"
    assert f.item_color(WORK) == "#C44D72"
    assert f.item_color(MATHS) == "#5462C4"
    assert f.avatar(ANNA) == "/avatar/u-anna?v=pic1"


def test_fmt_localised():
    f = _fmt(locale="de")
    assert f.rel(at(15)) == "in 28 Min."
    assert f.span(BIRTHDAY) == "Ganztägig"


# ─── Who's home / school ─────────────────────────────────────────────────


def test_presence_groups():
    groups = presence_groups(home())
    assert groups is not None
    assert groups.home == (PASCAL, MIA)
    assert groups.away == (ANNA, LEO)


def test_presence_groups_hidden_when_off_alone_or_unknown():
    assert presence_groups(home(sections=frozenset({"calendar"}))) is None
    assert presence_groups(home(people=(PASCAL,))) is None
    unknown = tuple(replace(p, presence="unknown") for p in PEOPLE)
    assert presence_groups(home(people=unknown)) is None


def test_school_now_and_changes():
    assert school_now([MIA_SCHOOL], at(8, 30)) == [MATHS]
    assert school_now([MIA_SCHOOL], at(9, 10)) == []  # break
    assert school_now([MIA_SCHOOL], at(10, 30)) == []  # cancelled
    assert school_changes([MIA_SCHOOL]) == [SPORT, ART, ENGLISH]


# ─── Timeline ────────────────────────────────────────────────────────────


def test_timeline_default_window_and_ticks():
    tl = build_timeline(home(), _fmt())
    assert tl is not None
    assert (tl.start_hour, tl.end_hour) == (8, 22)
    # 14:32 on 08–22 → 46.7 %; the 14 and 16 labels give way to "now".
    assert tl.now_x == 47
    assert [t.label for t in tl.ticks] == ["08", "10", "12", "18", "20", "22"]
    assert [t.label for t in tl.ticks if not t.minor] == ["08", "12", "20"]
    assert tl.ticks[0].x == 0 and tl.ticks[-1].x == 100


def test_timeline_lanes():
    tl = build_timeline(home(), _fmt())
    assert tl is not None
    lanes = {lane.person.user_id: lane for lane in tl.lanes}
    assert [lane.person for lane in tl.lanes] == list(PEOPLE)
    pascal = lanes["u-pascal"]
    titles = [b.title for b in pascal.blocks]
    assert titles == ["Dentist", "Dinner with Gran"]
    dentist = pascal.blocks[0]
    assert (dentist.x, dentist.w) == (14, 7)  # 10:00–11:00
    assert dentist.label == "10:00–11:00 Dentist"
    assert dentist.past is True and dentist.kind == "event"
    assert dentist.color == "k0"
    mia = lanes["u-mia"]
    assert mia.all_day == ("Mia's birthday",)
    school = next(b for b in mia.blocks if b.kind == "school")
    assert school.title == "School"
    assert school.label == "08:20–15:50 School"
    assert school.past is False


def test_timeline_overlaps_stack_into_rows():
    snap = home(
        events_today=(
            event("a", "A", at(9), at(12), "u-anna"),
            event("b", "B", at(10), at(11), "u-anna"),
            event("c", "C", at(10), at(11), "u-anna"),
            event("d", "D", at(10), at(11), "u-anna"),
            event("e", "E", at(12), at(13), "u-anna"),
        ),
        school=(),
    )
    tl = build_timeline(snap, _fmt(snap))
    assert tl is not None
    anna = next(lane for lane in tl.lanes if lane.person == ANNA)
    # D doesn't fit any row: it folds into C as "+1" (and C's spoken label
    # names it), instead of being drawn on top of C.
    assert [b.title for b in anna.blocks] == ["A", "B", "C", "E"]
    assert [b.row for b in anna.blocks] == [0, 1, 2, 0]
    assert [b.more for b in anna.blocks] == [0, 0, 1, 0]
    assert anna.blocks[2].label == "10:00–11:00 C; 10:00–11:00 D"
    assert anna.rows == 3


def test_timeline_overflow_counts_every_hidden_block():
    snap = home(
        events_today=tuple(
            event(f"e{n}", f"E{n}", at(10), at(11), "u-anna") for n in range(6)
        ),
        school=(),
    )
    tl = build_timeline(snap, _fmt(snap))
    assert tl is not None
    anna = next(lane for lane in tl.lanes if lane.person == ANNA)
    assert [b.more for b in anna.blocks] == [0, 0, 3]


def test_timeline_ignores_the_small_hours():
    """An item over before 06:00 doesn't stretch the day to midnight."""
    snap = home(
        events_today=(
            event("n", "Night feed", at(1), at(2), "u-anna"),
            event("w", "Work", at(9), at(17), "u-anna"),
        ),
        school=(),
    )
    tl = build_timeline(snap, _fmt(snap))
    assert tl is not None
    assert (tl.start_hour, tl.end_hour) == (8, 22)
    anna = next(lane for lane in tl.lanes if lane.person == ANNA)
    assert [b.title for b in anna.blocks] == ["Work"]


def test_timeline_stretches_for_early_and_late_items():
    snap = home(
        events_today=(
            event("a", "Flight", at(5, 30), at(7), "u-anna"),
            event("b", "Party", at(21), at(23, 30), "u-anna"),
        ),
        school=(),
    )
    tl = build_timeline(snap, _fmt(snap))
    assert tl is not None
    assert (tl.start_hour, tl.end_hour) == (6, 24)


def test_timeline_clamps_items_from_other_days():
    snap = home(
        now=at(23, 30),
        events_today=(
            event(
                "a", "Overnight", at(22, day=TODAY - timedelta(days=1)), at(9), "u-anna"
            ),
            event("b", "Late", at(23), at(2, day=TOMORROW), "u-anna"),
        ),
        school=(),
    )
    tl = build_timeline(snap, _fmt(snap))
    assert tl is not None
    assert (tl.start_hour, tl.end_hour) == (6, 24)
    anna = next(lane for lane in tl.lanes if lane.person == ANNA)
    assert anna.blocks[0].x == 0
    assert anna.blocks[1].x + anna.blocks[1].w == 100
    assert tl.now_x == 97


def test_timeline_point_events_have_a_width():
    snap = home(
        events_today=(event("a", "Call", at(22), at(22), "u-anna"),),
        school=(),
        now=at(23),
    )
    tl = build_timeline(snap, _fmt(snap))
    assert tl is not None
    block = next(lane for lane in tl.lanes if lane.person == ANNA).blocks[0]
    assert (block.x, block.w) == (99, 1)
    assert block.past is True
    assert tl.now_x is None  # 23:00 is past the 22:00 end


def test_timeline_none_without_anything_or_with_too_many_people():
    assert build_timeline(home(events_today=(), school=()), _fmt()) is None
    many = tuple(
        replace(PASCAL, user_id=f"u-{i}", display_name=f"P{i}")
        for i in range(MAX_LANES + 1)
    )
    assert build_timeline(home(people=many), _fmt()) is None
    assert build_timeline(home(people=()), _fmt()) is None


def test_timeline_respects_sections_and_unknown_people():
    snap = home(
        sections=frozenset({"tasks"}),
    )
    assert build_timeline(snap, _fmt(snap)) is None
    stray = home(
        events_today=(event("x", "Ghost", at(10), at(11), "u-ghost"),),
        school=(replace(MIA_SCHOOL, person_id="u-ghost"),),
    )
    assert build_timeline(stray, _fmt(stray)) is None


def test_timeline_skips_a_school_day_with_everything_cancelled():
    day = replace(MIA_SCHOOL, first_start=None, last_end=None)
    snap = home(events_today=(), school=(day,))
    assert build_timeline(snap, _fmt(snap)) is None


def test_timeline_all_day_only_still_shows_lanes():
    snap = home(events_today=(BIRTHDAY,), school=())
    tl = build_timeline(snap, _fmt(snap))
    assert tl is not None
    assert all(not lane.blocks for lane in tl.lanes)


# ─── Night ───────────────────────────────────────────────────────────────


def test_night_item_home_evening_is_tomorrow():
    snap = home(now=at(22, 47), is_night=True)
    assert night_item(snap) == ("tomorrow", snap.tomorrow_first)
    assert night_item(home(now=at(22, 47), tomorrow_first=None)) is None


def test_night_item_home_small_hours_is_today():
    snap = home(now=at(5, 10), next_up=SWIM)
    assert night_item(snap) == ("today", SWIM)
    assert night_item(home(now=at(5, 10), next_up=None)) is None


def test_night_item_person():
    page = mia_page(now=at(22, 30))
    when, item = night_item(page)
    assert when == "tomorrow" and item.title == "Maths"
    # All-day tomorrow items are skipped; nothing timed → nothing.
    assert night_item(person(now=at(22, 30))) is None
    first = lesson("Maths", at(7, 30, TOMORROW), at(8, 15, TOMORROW))
    only_school = person(now=at(22, 30), school_tomorrow_first=first)
    assert night_item(only_school) == ("tomorrow", first)
    assert night_item(person(now=at(5))) == ("today", DINNER)
    assert night_item(person(now=at(5), next_item=None)) is None


def test_contrast_of_ink_in_css_is_aa():
    book = ColorBook()
    for color in ("#FFEE88", "#357BBB", "#3F7D57", "#AD780C"):
        book.add(color)
        cls = book.cls(color)
        ink = book.css().split(f".{cls}{{")[1].split("--kt:")[1][:7]
        assert contrast_ratio(ink, color.lower()) >= 4.5


def test_home_colors_cover_people_items_and_stickies():
    book = home_colors(home())
    for color in ("#357BBB", "#C44D72", "#5462C4", "#FFEE88"):
        assert book.cls(color), color
    assert ".k" in book.css()
    assert "body" not in book.css()  # the bad sticky colour never lands


def test_person_colors():
    book = person_colors(mia_page())
    assert book.cls(MIA.color) and book.cls("#5462C4") and book.cls(ANNA.color)
    assert person_colors(person(people=())).cls(ANNA.color)


def test_fmt_icon_and_lesson_meta():
    f = _fmt()
    assert f.icon(MATHS) == "📐"
    assert f.icon(BREAK) == ""
    assert f.icon(WORK) == ""
    assert f.lesson_meta(MATHS) == "Room B12 · Keller"
    assert f.lesson_meta(ART) == "Room B7"
    assert f.lesson_meta(lesson("X", at(9), at(10), teacher="Ms Q")) == "Ms Q"
    assert f.lesson_meta(BREAK) == ""
    assert f.lesson_meta(WORK) == ""


# ─── UX round: narrow blocks, own colour, emoji ──────────────────────────


def test_narrow_blocks_show_an_icon_instead_of_text():
    tl = build_timeline(home(), _fmt())
    assert tl is not None
    lanes = {lane.person.user_id: lane for lane in tl.lanes}
    dentist = lanes["u-pascal"].blocks[0]  # 1 h of 14 → 7 %
    # No leading emoji: the keyword guess ("Dentist" → 🦷).
    assert dentist.narrow is True and dentist.icon == "🦷"
    work = lanes["u-anna"].blocks[0]
    assert work.narrow is False
    school = next(b for b in lanes["u-mia"].blocks if b.kind == "school")
    assert school.icon == "🎒"


def test_narrow_event_uses_its_leading_emoji():
    snap = home(
        events_today=(event("s", "🏊 Swimming", at(15), at(16), "u-leo"),), school=()
    )
    tl = build_timeline(snap, _fmt(snap))
    assert tl is not None
    block = next(lane for lane in tl.lanes if lane.person == LEO).blocks[0]
    assert block.narrow and block.icon == "🏊"


def test_narrow_event_without_a_known_word_gets_the_neutral_icon():
    snap = home(events_today=(event("s", "Taxes", at(15), at(16), "u-leo"),), school=())
    tl = build_timeline(snap, _fmt(snap))
    assert tl is not None
    block = next(lane for lane in tl.lanes if lane.person == LEO).blocks[0]
    assert block.narrow and block.icon == "🔹"


@pytest.mark.parametrize(
    ("title", "emoji"),
    [
        ("🏊 Swimming", "🏊"),
        ("🇬🇧 English", "🇬🇧"),
        ("Swim 🏊", None),
        ("", None),
        ("1 Meeting", None),
        ("❤️ Date", "❤️"),
    ],
)
def test_leading_emoji(title, emoji):
    assert leading_emoji(title) == emoji


def test_fmt_own_colour_paints_events_in_the_page_persons_colour():
    colors = ColorBook()
    f = Fmt(
        now=at(12),
        today=TODAY,
        t=_t(),
        people=PEOPLE,
        colors=colors,
        own_color=ANNA.color,
    )
    assert f.item_color(DINNER) == ANNA.color  # not the merged copy's colour
    assert f.item_color(MATHS) == "#5462C4"  # lessons keep the timetable's
    assert _fmt().item_color(DINNER) == DINNER.color


@pytest.mark.parametrize(
    ("title", "emoji"),
    [("👩‍👧 Kids", "👩‍👧"), ("👍🏽 ok", "👍🏽"), ("🇩🇪", "🇩🇪")],
)
def test_leading_emoji_sequences(title, emoji):
    assert leading_emoji(title) == emoji


def test_day_list_is_chronological_with_school_spans():
    rows = day_list(home())
    assert [getattr(r, "title", "school") for r in rows] == [
        "Mia's birthday",  # all-day first
        "school",  # 08:20
        "Work",  # 09:00
        "Dentist",
        "Swimming",
        "Dinner with Gran",
    ]
    span = rows[1]
    assert isinstance(span, SchoolSpan)
    assert (span.person_id, span.start, span.end) == ("u-mia", at(8, 20), at(15, 50))
    assert span.item_type == "school"


def test_day_list_respects_sections_and_cancelled_days():
    assert day_list(home(sections=frozenset({"tasks"}))) == []
    no_school = home(school=(replace(MIA_SCHOOL, first_start=None, last_end=None),))
    assert not any(isinstance(r, SchoolSpan) for r in day_list(no_school))
    events_only = day_list(home(sections=frozenset({"calendar"})))
    assert not any(isinstance(r, SchoolSpan) for r in events_only)


def test_stylesheet_has_every_step_the_presenter_can_emit():
    """Q10: an .x-N / .w-N class without a rule would put a bar at 0."""
    css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
    for n in range(101):
        assert f".x-{n} {{ inset-inline-start: {n}%; }}" in css, n
    for n in range(1, 101):
        assert f".w-{n} {{ inline-size: {n}%; }}" in css, n
    for row in range(MAX_LANE_ROWS):
        assert f".dash-r-{row} {{" in css
    for rows in range(2, MAX_LANE_ROWS + 1):
        assert f".dash-rows-{rows} {{" in css
