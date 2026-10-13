"""Tests for Home's tabs (Overview · Messages · Notes · Map): which show
(pure)."""

from __future__ import annotations

import pytest

from socialhome.dashboard.tabs import (
    HOME_TABS,
    TAB_MAP,
    TAB_MESSAGES,
    TAB_NOTES,
    TAB_OVERVIEW,
    home_tab,
    home_tabs,
)

from .factories import ALL_SECTIONS, PASCAL, home, located, map_home


def _keys(snap) -> list[str]:
    return [t.key for t in home_tabs(snap)]


def test_routes_and_fragments():
    assert [(t.key, t.href, t.fragment) for t in HOME_TABS] == [
        (TAB_OVERVIEW, "/", "/fragment/home"),
        (TAB_MESSAGES, "/messages", "/fragment/home/messages"),
        (TAB_NOTES, "/notes", "/fragment/home/notes"),
        (TAB_MAP, "/map", "/fragment/home/map"),
    ]
    assert home_tab(TAB_NOTES).href == "/notes"


def test_every_tab_with_messages_on_and_notes():
    assert _keys(home(messages_on=True)) == ["overview", "messages", "notes"]


def test_messages_tab_stays_with_nothing_to_show():
    snap = home(messages_on=True, messages=(), stickies=())
    assert _keys(snap) == ["overview", "messages", "notes"]


def test_no_messages_tab_without_the_opt_in():
    assert _keys(home(messages_on=False)) == ["overview", "notes"]


def test_no_notes_tab_while_stickies_are_off():
    snap = home(messages_on=True, sections=ALL_SECTIONS - {"stickies"})
    assert _keys(snap) == ["overview", "messages"]


def test_only_overview_when_nothing_else_qualifies():
    snap = home(messages_on=False, sections=ALL_SECTIONS - {"stickies"})
    assert _keys(snap) == ["overview"]


def test_unknown_tab_is_an_error():
    with pytest.raises(KeyError):
        home_tab("chores")


def test_map_tab_while_someone_is_located():
    assert _keys(map_home(messages_on=True)) == ["overview", "messages", "notes", "map"]


def test_no_map_tab_when_nobody_shares_or_the_last_fix_is_a_day_old():
    assert _keys(home(messages_on=True)) == ["overview", "messages", "notes"]
    old = map_home(people=(located(PASCAL, 24 * 60, presence="home"),))
    assert "map" not in _keys(old)


def test_map_alone_still_gets_a_bar():
    snap = map_home(messages_on=False, sections=ALL_SECTIONS - {"stickies"})
    assert _keys(snap) == ["overview", "map"]
