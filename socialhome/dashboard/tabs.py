"""Home's tabs on the wall: Overview · Messages · Notes · Map.

Overview is the household glance (``/``); Messages lists the last day of
the household chat; Notes shows every sticky; Map shows who is where
(:mod:`.map_view`). The tabs are views *inside*
Home — the rail keeps Home current on all three, and an idle tablet goes
back to Overview.

* **Overview** always shows.
* **Messages** shows while the admin's ``show_household_messages`` is on
  and the household chat is enabled (:attr:`HomeSnapshot.messages_on`) —
  even with nothing in it (the tab shows its empty state).
* **Notes** shows while the stickies section is on — even with no notes.
* **Map** shows while at least one shown member has a fresh location
  (:func:`~.map_view.has_map`) — sharing flips at runtime, so a ``/map``
  without it answers ``303`` to Overview rather than ``404``.

With only Overview there is no tab bar at all. Messages or Notes when it
doesn't show answers ``404`` and nothing links to it. Pure: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..domain.household_dashboard import SECTION_STICKIES, HomeSnapshot
from .map_view import has_map

__all__ = [
    "HOME_TABS",
    "TAB_MAP",
    "TAB_MESSAGES",
    "TAB_NOTES",
    "TAB_OVERVIEW",
    "HomeTab",
    "home_tab",
    "home_tabs",
]

TAB_OVERVIEW: Final = "overview"
TAB_MESSAGES: Final = "messages"
TAB_NOTES: Final = "notes"
TAB_MAP: Final = "map"


@dataclass(slots=True, frozen=True)
class HomeTab:
    """One of Home's tabs: its key (the ``dashboard.tabs.<key>`` label),
    its page and the ``/fragment/…`` its live refresh fetches."""

    key: str
    href: str
    fragment: str


#: Every tab, in bar order.
HOME_TABS: Final = (
    HomeTab(TAB_OVERVIEW, "/", "/fragment/home"),
    HomeTab(TAB_MESSAGES, "/messages", "/fragment/home/messages"),
    HomeTab(TAB_NOTES, "/notes", "/fragment/home/notes"),
    HomeTab(TAB_MAP, "/map", "/fragment/home/map"),
)


def home_tab(key: str) -> HomeTab:
    """The tab named ``key`` (``KeyError`` for none)."""
    for tab in HOME_TABS:
        if tab.key == key:
            return tab
    raise KeyError(key)


def home_tabs(snap: HomeSnapshot) -> tuple[HomeTab, ...]:
    """The tabs Home shows for ``snap``, in bar order."""
    shown = {
        TAB_OVERVIEW: True,
        TAB_MESSAGES: snap.messages_on,
        TAB_NOTES: SECTION_STICKIES in snap.sections,
        TAB_MAP: has_map(snap),
    }
    return tuple(t for t in HOME_TABS if shown[t.key])
