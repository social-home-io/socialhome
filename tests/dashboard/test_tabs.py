"""Tests for Home's tabs (Overview · Messages · Notes): which show (pure)."""

from __future__ import annotations

import pytest

from socialhome.dashboard.tabs import (
    HOME_TABS,
    TAB_MESSAGES,
    TAB_NOTES,
    TAB_OVERVIEW,
    home_tab,
    home_tabs,
)

from .factories import ALL_SECTIONS, home


def _keys(snap) -> list[str]:
    return [t.key for t in home_tabs(snap)]


def test_routes_and_fragments():
    assert [(t.key, t.href, t.fragment) for t in HOME_TABS] == [
        (TAB_OVERVIEW, "/", "/fragment/home"),
        (TAB_MESSAGES, "/messages", "/fragment/home/messages"),
        (TAB_NOTES, "/notes", "/fragment/home/notes"),
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
