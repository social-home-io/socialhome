"""The board view models — what Home and a person page show, built once
for both the HTML templates (:mod:`.views`) and the wall-panel JSON
(:mod:`.display_json`).

Everything a board decides — which tabs show, which mentions sit on a
banner (and so leave the message card), what the timeline, the day
list, the picture view and the map hold — is computed here from the read
model (:class:`~socialhome.domain.household_dashboard.HomeSnapshot` /
:class:`~socialhome.domain.household_dashboard.PersonSnapshot`) and the
pure presenters (:mod:`.presenter`, :mod:`.messages`, :mod:`.picture`,
:mod:`.map_view`, :mod:`.tabs`). The HTML page and the panel JSON are
two spellings of these models, so the wall and the panel can't drift
apart in what they show.

Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import datetime

from ..domain.household_dashboard import (
    HERE_SHOWN,
    SECTION_TIMETABLE,
    DashboardAgendaItem,
    DashboardEvent,
    DashboardLesson,
    DashboardPerson,
    DashboardSticky,
    HomeSnapshot,
    PersonSnapshot,
    here_alerts,
    mentions_for,
)
from .map_view import MapView, map_view
from .messages import (
    ChatDay,
    MentionCard,
    MessageRow,
    card_rows,
    chat_days,
    chat_window,
    mention_cards,
    message_rows,
    new_count,
)
from .picture import PictureView, picture_view
from .presenter import (
    MAX_LANES,
    SHOPPING_SHOWN,
    STICKIES_SHOWN,
    ColorBook,
    Fmt,
    PresenceGroups,
    SchoolSpan,
    Timeline,
    build_timeline,
    day_list,
    night_item,
    presence_groups,
    school_changes,
    school_now,
)
from .tabs import (
    TAB_MAP,
    TAB_MESSAGES,
    TAB_NOTES,
    TAB_OVERVIEW,
    HomeTab,
    home_tabs,
)

__all__ = [
    "TOMORROW_SHOWN",
    "BoardBase",
    "HomeBoard",
    "Overview",
    "PersonBoard",
    "board_base",
    "home_board",
    "model_fields",
    "person_board",
    "tomorrow_list",
]

#: Events listed under "Tomorrow" on a person page.
TOMORROW_SHOWN = 3


def model_fields(model: object) -> dict[str, object]:
    """A model's fields by name, values as they are (no deep copy —
    unlike :func:`dataclasses.asdict`): what a template context takes."""
    return {f.name: getattr(model, f.name) for f in fields(model)}  # type: ignore[arg-type]


@dataclass(slots=True, frozen=True)
class BoardBase:
    """What every board shares: the formatter, the rail and night mode."""

    fmt: Fmt
    people: tuple[DashboardPerson, ...]
    #: A rail (or face strip) only for a household of more than one.
    show_rail: bool
    #: More people than lanes fit: the rail is denser, Home lists the day.
    rail_many: bool
    night: bool
    #: What night mode shows under the clock (``night_item``).
    night_item: tuple[str, DashboardAgendaItem] | None
    #: Every person mention waiting (never at night, only with a rail):
    #: the rail's badges.
    badged: tuple[MentionCard, ...]

    @property
    def badges(self) -> frozenset[str]:
        return frozenset(c.person_id for c in self.badged if c.person_id)


def board_base(
    snapshot: HomeSnapshot | PersonSnapshot,
    *,
    people: tuple[DashboardPerson, ...],
    colors: ColorBook,
    t: Callable[..., str],
    own_color: str | None = None,
) -> BoardBase:
    fmt = Fmt(
        now=snapshot.now,
        today=snapshot.today,
        t=t,
        people=people,
        colors=colors,
        own_color=own_color,
    )
    night = snapshot.is_night
    show_rail = len(people) > 1
    badged = (
        ()
        if night or not show_rail
        else tuple(c for c in mention_cards(snapshot.mentions, fmt) if c.person_id)
    )
    return BoardBase(
        fmt=fmt,
        people=people,
        show_rail=show_rail,
        rail_many=len(people) > MAX_LANES,
        night=night,
        night_item=night_item(snapshot) if night else None,
        badged=badged,
    )


# ─── Home ────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Overview:
    """Home's Overview tab. Field names are the template's context keys
    (``messages`` is the card: the newest few, minus any on a banner)."""

    timeline: Timeline | None
    day_rows: list[DashboardEvent | SchoolSpan]
    presence: PresenceGroups | None
    at_school: list[DashboardLesson]
    changes: list[DashboardLesson]
    shopping_shown: tuple[object, ...]
    shopping_more: int
    stickies: tuple[DashboardSticky, ...]
    #: "See all ›" only where the Notes tab holds more than the card.
    notes_more: bool
    open_chores: int
    messages: list[MessageRow]
    messages_more: bool


@dataclass(slots=True, frozen=True)
class HomeBoard:
    """Home on one tab — the tab bar, its badge, the ``@here`` banners and
    that tab's own content (the other tabs' fields stay empty)."""

    snap: HomeSnapshot
    tab: str
    #: The tab bar: empty with only Overview showing.
    tabs: tuple[HomeTab, ...]
    #: Messages under 15 min old the Messages tab lists (the badge).
    tab_new: int
    here_cards: list[MentionCard]
    overview: Overview | None = None
    chat_days: list[ChatDay] | None = None
    notes: tuple[DashboardSticky, ...] | None = None
    notes_beyond: int = 0
    map: MapView | None = None


def home_board(
    snap: HomeSnapshot, fmt: Fmt, tab: str = TAB_OVERVIEW
) -> HomeBoard | None:
    """Home on ``tab`` — ``None`` when that tab doesn't show (:mod:`.tabs`).

    Every tab carries the tab bar (with the Messages badge: messages under
    :data:`~.messages.NEW_FOR` old that the Messages tab lists) and the
    ``@here`` banners; a message on a banner is left out of the Overview
    card, the Messages list and the badge until someone taps "Got it"."""
    shown = home_tabs(snap)
    if tab not in {t.key for t in shown}:
        return None
    here_cards = (
        []
        if snap.is_night
        else mention_cards(here_alerts(snap.mentions), fmt)[:HERE_SHOWN]
    )
    bannered = frozenset(c.id for c in here_cards)
    # The tab's window again here (the read model bounds it already).
    rows = (
        [] if snap.is_night else chat_window(message_rows(snap.messages, fmt), snap.now)
    )
    listed = [r for r in rows if r.id not in bannered]
    base = HomeBoard(
        snap=snap,
        tab=tab,
        tabs=shown if len(shown) > 1 else (),
        tab_new=new_count(listed),
        here_cards=here_cards,
    )
    if tab == TAB_MESSAGES:
        return _with(base, chat_days=chat_days(listed, fmt))
    if tab == TAB_NOTES:
        return _with(
            base,
            notes=snap.stickies,
            notes_beyond=max(0, snap.stickies_total - len(snap.stickies)),
        )
    if tab == TAB_MAP:
        return _with(base, map=map_view(snap, fmt))
    card = card_rows(rows, snap.now, hide=bannered)
    timetable = SECTION_TIMETABLE in snap.sections
    overview = Overview(
        timeline=build_timeline(snap, fmt),
        day_rows=day_list(snap),
        presence=presence_groups(snap),
        at_school=school_now(snap.school, snap.now) if timetable else [],
        changes=school_changes(snap.school) if timetable else [],
        shopping_shown=snap.shopping[:SHOPPING_SHOWN],
        shopping_more=max(0, snap.shopping_total - SHOPPING_SHOWN),
        stickies=snap.stickies[:STICKIES_SHOWN],
        notes_more=snap.stickies_total > STICKIES_SHOWN,
        open_chores=sum(1 for c in snap.chores_today if not c.done),
        messages=card,
        messages_more=snap.messages_on and len(listed) > len(card),
    )
    return _with(base, overview=overview)


def _with(board: HomeBoard, **changes: object) -> HomeBoard:
    values = model_fields(board)
    values.update(changes)
    return HomeBoard(**values)  # type: ignore[arg-type]


# ─── A person ────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class PersonBoard:
    """A person page. Field names are the template's context keys."""

    page: PersonSnapshot
    school_lessons: tuple[DashboardLesson, ...]
    today_list: tuple[DashboardAgendaItem, ...]
    tomorrow_list: list[DashboardAgendaItem]
    #: Mentions naming this person (never at night).
    my_mentions: list[MentionCard]
    #: The picture view's model, for a picture-view person.
    picture: PictureView | None
    #: Household-chat rows: the picture view only, never at night.
    messages: list[MessageRow]


def person_board(page: PersonSnapshot, fmt: Fmt) -> PersonBoard:
    school_lessons = page.school.lessons if page.school is not None else ()
    events_today = (
        tuple(i for i in page.today_items if isinstance(i, DashboardEvent))
        if page.school is not None
        else page.today_items
    )
    tomorrow = tomorrow_list(page)
    mine = (
        []
        if page.is_night
        else mention_cards(mentions_for(page.mentions, page.person.user_id), fmt)
    )
    picture = (
        picture_view(page, fmt, tomorrow=tomorrow) if page.person.picture_view else None
    )
    messages = (
        message_rows(page.messages, fmt)
        if picture is not None and not page.is_night
        else []
    )
    return PersonBoard(
        page=page,
        school_lessons=school_lessons,
        today_list=events_today,
        tomorrow_list=tomorrow,
        my_mentions=mine,
        picture=picture,
        messages=messages,
    )


def tomorrow_list(page: PersonSnapshot) -> list[DashboardAgendaItem]:
    """Tomorrow on a person page: the first few events, plus the first
    lesson (school is said once, not lesson by lesson), in time order."""
    events = [i for i in page.tomorrow_items if isinstance(i, DashboardEvent)]
    items: list[DashboardAgendaItem] = list(events[:TOMORROW_SHOWN])
    if page.school_tomorrow_first is not None:
        items.append(page.school_tomorrow_first)
    items.sort(key=_tomorrow_order)
    return items


def _tomorrow_order(item: DashboardAgendaItem) -> tuple[int, datetime]:
    return (0 if isinstance(item, DashboardEvent) and item.standing else 1, item.start)
