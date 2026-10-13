"""Tests for the shared board view models (:mod:`socialhome.dashboard.boards`)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from socialhome.dashboard.boards import (
    TOMORROW_SHOWN,
    board_base,
    home_board,
    model_fields,
    person_board,
    tomorrow_list,
)
from socialhome.dashboard.presenter import (
    SHOPPING_SHOWN,
    STICKIES_SHOWN,
    home_colors,
    person_colors,
)
from socialhome.dashboard.tabs import TAB_MAP, TAB_MESSAGES, TAB_NOTES, TAB_OVERVIEW

from .factories import (
    ANNA,
    FOR_LEO,
    FOR_MIA,
    HERE,
    LEO,
    MESSAGES,
    PEOPLE,
    TOMORROW,
    at,
    event,
    home,
    leo_page,
    map_home,
    message,
    person,
)


def _t(key: str, **fmt: object) -> str:
    return key + (repr(sorted(fmt.items())) if fmt else "")


def _home_fmt(snap):
    return board_base(snap, people=snap.people, colors=home_colors(snap), t=_t).fmt


def test_model_fields_keeps_values_as_they_are():
    snap = home()
    board = home_board(snap, _home_fmt(snap))
    out = model_fields(board)
    assert out["snap"] is snap
    assert out["overview"] is board.overview


def test_base_rail_and_badges():
    snap = home(mentions=(HERE, FOR_LEO, FOR_MIA))
    base = board_base(snap, people=snap.people, colors=home_colors(snap), t=_t)
    assert base.show_rail and not base.rail_many
    assert base.badges == frozenset({"u-leo", "u-mia"})
    assert base.night_item is None


def test_base_no_badges_at_night_or_without_rail():
    snap = home(mentions=(FOR_LEO,), is_night=True)
    base = board_base(snap, people=snap.people, colors=home_colors(snap), t=_t)
    assert base.badged == () and base.night_item is not None
    alone = home(mentions=(FOR_LEO,), people=(LEO,))
    base = board_base(alone, people=alone.people, colors=home_colors(alone), t=_t)
    assert not base.show_rail and base.badged == ()


def test_overview_content():
    snap = home(stickies_total=STICKIES_SHOWN + 2)
    board = home_board(snap, _home_fmt(snap))
    assert board is not None and board.tab == TAB_OVERVIEW
    ov = board.overview
    assert ov is not None and ov.timeline is not None
    assert len(ov.shopping_shown) == SHOPPING_SHOWN
    assert ov.shopping_more == snap.shopping_total - SHOPPING_SHOWN
    assert ov.notes_more
    assert ov.open_chores == sum(1 for c in snap.chores_today if not c.done)
    assert board.chat_days is None and board.map is None


def test_banner_leaves_the_card_and_the_badge():
    msgs = (message("h1", "u-anna", 3, text="@here dinner"), *MESSAGES)
    snap = home(messages=msgs, mentions=(HERE,), messages_on=True)
    board = home_board(snap, _home_fmt(snap))
    assert [c.id for c in board.here_cards] == ["h1"]
    assert "h1" not in [r.id for r in board.overview.messages]
    # m1 (4 min) is new; h1 (3 min) would be too, but it is on the banner.
    assert board.tab_new == 1


def test_tabs_and_tab_content():
    snap = map_home(messages=MESSAGES, messages_on=True)
    fmt = _home_fmt(snap)
    msgs = home_board(snap, fmt, TAB_MESSAGES)
    assert [t.key for t in msgs.tabs] == [
        TAB_OVERVIEW,
        TAB_MESSAGES,
        TAB_NOTES,
        TAB_MAP,
    ]
    assert msgs.chat_days and msgs.overview is None
    notes = home_board(snap, fmt, TAB_NOTES)
    assert notes.notes == snap.stickies and notes.notes_beyond == 0
    assert home_board(snap, fmt, TAB_MAP).map is not None


def test_tab_off_is_none_and_only_overview_has_no_bar():
    snap = home(sections=frozenset({"calendar"}))
    fmt = _home_fmt(snap)
    assert home_board(snap, fmt, TAB_NOTES) is None
    assert home_board(snap, fmt, TAB_MAP) is None
    assert home_board(snap, fmt).tabs == ()


def test_night_home_has_no_banners_or_rows():
    snap = home(is_night=True, mentions=(HERE,), messages=MESSAGES, messages_on=True)
    board = home_board(snap, _home_fmt(snap))
    assert board.here_cards == [] and board.overview.messages == []


def _person_fmt(page):
    return board_base(
        page,
        people=page.people,
        colors=person_colors(page),
        t=_t,
        own_color=page.person.color,
    ).fmt


def test_person_reader_board():
    page = person(mentions=(FOR_MIA, replace(FOR_LEO, person_id="u-anna")))
    board = person_board(page, _person_fmt(page))
    assert board.picture is None and board.messages == []
    assert [m.person_id for m in board.my_mentions] == ["u-anna"]
    assert board.school_lessons == ()
    assert board.today_list == page.today_items


def test_person_picture_board_and_night():
    page = leo_page(messages=MESSAGES, mentions=(FOR_LEO,))
    board = person_board(page, _person_fmt(page))
    assert board.picture is not None and board.messages
    assert len(board.my_mentions) == 1
    night = person_board(replace(page, is_night=True), _person_fmt(page))
    assert night.messages == [] and night.my_mentions == []


def test_school_day_lists_only_events():
    page = leo_page()
    board = person_board(page, _person_fmt(page))
    assert all(i.item_type == "event" for i in board.today_list)
    assert board.school_lessons == page.school.lessons


def test_tomorrow_list_caps_events_and_adds_first_lesson():
    events = tuple(
        event(
            f"t{i}",
            f"E{i}",
            at(9 + i, day=TOMORROW),
            at(10 + i, day=TOMORROW),
            "u-anna",
        )
        for i in range(TOMORROW_SHOWN + 2)
    )
    first = leo_page().school_tomorrow_first
    page = person(ANNA, tomorrow_items=events, school_tomorrow_first=first)
    items = tomorrow_list(page)
    assert len(items) == TOMORROW_SHOWN + 1
    assert items[0] is first  # 08:00 sorts before the 09:00 event
    assert [i.start for i in items] == sorted(i.start for i in items)


def test_tomorrow_all_day_first():
    allday = event(
        "a",
        "Trip",
        at(0, day=TOMORROW),
        at(0, day=TOMORROW + timedelta(days=1)),
        "u-anna",
        all_day=True,
    )
    early = event("b", "Early", at(6, day=TOMORROW), at(7, day=TOMORROW), "u-anna")
    page = person(ANNA, tomorrow_items=(early, allday), people=PEOPLE)
    assert [i.id for i in tomorrow_list(page)] == ["a", "b"]
