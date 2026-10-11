"""Turn dashboard snapshots into what the templates render.

The read model (:class:`~socialhome.domain.household_dashboard.HomeSnapshot`
/ :class:`~socialhome.domain.household_dashboard.PersonSnapshot`) says
*what* is going on; this module decides *how it looks* on the wall:

* **Colours** — every colour (person, calendar, timetable, sticky note,
  household theme) is validated (:func:`~.contrast.hex_color`) and
  lands in the page only as a class in one hash-admitted inline
  ``<style>`` (:class:`ColorBook`, :func:`theme_css`). There are no
  ``style=`` attributes: the CSP forbids them.
* **Geometry** — the swim-lane timeline (:func:`build_timeline`) places
  blocks on whole-percent steps; ``dashboard.css`` carries one class per
  step (``.x-N`` / ``.w-N``), so positions need no inline style either.
* **Phrases** — :class:`Fmt` is the template's formatter: 24-hour times,
  localised relative phrases (:mod:`.clock`), titles of lessons, the
  people of an item.

Pure: no I/O, so it is tested without a server.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import quote

from ..domain.household_dashboard import (
    SECTION_CALENDAR,
    SECTION_PRESENCE,
    SECTION_TIMETABLE,
    DashboardAgendaItem,
    DashboardEvent,
    DashboardLesson,
    DashboardPerson,
    DashboardSchoolDay,
    HomeSnapshot,
    PersonSnapshot,
)
from .clock import days_late, due_label, hhmm, relative_in, until
from .contrast import hex_color, ink_on, readable_on

__all__ = [
    "DARK_SURFACES",
    "LIGHT_SURFACES",
    "MAX_LANES",
    "SHOPPING_SHOWN",
    "STICKIES_SHOWN",
    "ColorBook",
    "Fmt",
    "Lane",
    "LaneBlock",
    "Tick",
    "Timeline",
    "avatar_url",
    "build_timeline",
    "home_colors",
    "night_item",
    "person_colors",
    "presence_groups",
    "school_changes",
    "school_now",
    "theme_css",
    "view_key",
]

#: The page backgrounds text sits on (``--dash-bg`` / ``--dash-surface``
#: in ``dashboard.css``), light and dark.
LIGHT_SURFACES = ("#ffffff", "#f4f2ee")
DARK_SURFACES = ("#22201d", "#151412")

#: More people than this: no swim-lanes (too thin to read from across
#: the room) — Home lists the day in time order instead.
MAX_LANES = 6
#: Overlapping blocks in one lane stack into at most this many rows.
MAX_LANE_ROWS = 3
#: The timeline's default span (hours); stretched to fit earlier / later
#: items.
DAY_START_HOUR = 8
DAY_END_HOUR = 22
#: Hour labels closer than this (percent) to "now" are left out — the now
#: label sits there.
NOW_LABEL_CLEARANCE = 12
SHOPPING_SHOWN = 6
STICKIES_SHOWN = 4


class ColorBook:
    """Validated colours → short CSS classes (``k0``, ``k1``…).

    Each class sets ``--kc`` (the colour) and ``--kt`` (black or white
    ink that reads on it). Register every colour *before* rendering
    (:meth:`add`); the template asks :meth:`cls`. Anything that isn't a
    plain hex colour gets no class — the element keeps its neutral look.
    """

    __slots__ = ("_classes",)

    def __init__(self) -> None:
        self._classes: dict[str, str] = {}

    def add(self, value: object) -> None:
        color = hex_color(value)
        if color is not None and color not in self._classes:
            self._classes[color] = f"k{len(self._classes)}"

    def cls(self, value: object) -> str:
        color = hex_color(value)
        return "" if color is None else self._classes.get(color, "")

    def css(self) -> str:
        return "".join(
            f".{name}{{--kc:{color};--kt:{ink_on(color)}}}"
            for color, name in self._classes.items()
        )


def _agenda_colors(items: Iterable[DashboardAgendaItem]) -> Iterable[object]:
    for item in items:
        yield item.color if isinstance(item, DashboardEvent) else item.color_hex


def home_colors(snapshot: HomeSnapshot) -> ColorBook:
    """Every colour Home can show, registered in a stable order."""
    book = ColorBook()
    for value in (
        *(p.color for p in snapshot.people),
        *_agenda_colors(snapshot.events_today),
        *_agenda_colors(ls for day in snapshot.school for ls in day.lessons),
        *_agenda_colors(x for x in (snapshot.next_up, snapshot.tomorrow_first) if x),
        *(s.color for s in snapshot.stickies),
    ):
        book.add(value)
    return book


def person_colors(page: PersonSnapshot) -> ColorBook:
    """Every colour a person page can show, registered in a stable order."""
    book = ColorBook()
    school = page.school.lessons if page.school else ()
    extra = (page.now_item, page.next_item, page.school_tomorrow_first)
    for value in (
        *(p.color for p in page.people),
        page.person.color,
        *_agenda_colors(page.today_items),
        *_agenda_colors(school),
        *_agenda_colors(page.tomorrow_items),
        *_agenda_colors(x for x in extra if x),
    ):
        book.add(value)
    return book


def theme_css(primary: object) -> str:
    """The household theme colour as the dashboard accent — per colour
    scheme, and only where it reads as text on the page (4.5:1 on both
    backgrounds); otherwise that scheme keeps the built-in high-contrast
    accent."""
    color = hex_color(primary)
    if color is None:
        return ""
    rule = f":root{{--dash-accent:{color};--dash-accent-text:{ink_on(color)}}}"
    out = []
    if readable_on(color, LIGHT_SURFACES):
        out.append(f"@media (prefers-color-scheme:light){{{rule}}}")
    if readable_on(color, DARK_SURFACES):
        out.append(f"@media (prefers-color-scheme:dark){{{rule}}}")
    return "".join(out)


def view_key(*parts: object) -> str:
    """A short digest of what a ``/fragment/…`` swap can't change — the
    inline style, night mode, the rail. When a fresh fragment's key
    differs from the page's, ``dashboard.js`` reloads the whole page."""
    raw = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def avatar_url(person: DashboardPerson) -> str | None:
    """``/avatar/<id>?v=<hash>`` — ``None`` when the person has no picture
    (the face shows initials)."""
    if not person.picture_hash:
        return None
    return f"/avatar/{quote(person.user_id, safe='')}?v={quote(person.picture_hash, safe='')}"


# ─── Formatting ──────────────────────────────────────────────────────────


class Fmt:
    """What templates call to spell times, titles and people."""

    __slots__ = ("now", "today", "t", "colors", "_people")

    def __init__(
        self,
        *,
        now: datetime,
        today: date,
        t: Callable[..., str],
        people: Sequence[DashboardPerson],
        colors: ColorBook,
    ) -> None:
        self.now = now
        self.today = today
        self.t = t
        self.colors = colors
        self._people: Mapping[str, DashboardPerson] = {p.user_id: p for p in people}

    def hhmm(self, value: datetime) -> str:
        return hhmm(value)

    def span(self, item: DashboardAgendaItem) -> str:
        """``"09:00–17:00"``, ``"09:00"`` (a point in time) or "All day"."""
        if isinstance(item, DashboardEvent) and item.all_day:
            return self.t("dashboard.all_day")
        if item.end <= item.start:
            return hhmm(item.start)
        return f"{hhmm(item.start)}–{hhmm(item.end)}"

    def rel(self, then: datetime) -> str:
        return relative_in(self.now, then, self.t)

    def until(self, end: datetime) -> str:
        return until(end, self.t)

    def late(self, days: int) -> str:
        return days_late(days, self.t)

    def due(self, due: date | None) -> str | None:
        return due_label(due, self.today, self.t)

    def title(self, item: DashboardAgendaItem) -> str:
        """An event's title; a lesson's subject (or label, or "Break")."""
        if isinstance(item, DashboardEvent):
            return item.title
        if item.title:
            return item.title
        if item.label:
            return item.label
        return self.t("dashboard.break") if item.kind == "break" else ""

    def people(
        self, item: DashboardAgendaItem | Iterable[str]
    ) -> list[DashboardPerson]:
        """The shown people of an item (or of a list of ids), in order."""
        if isinstance(item, DashboardEvent):
            ids: Iterable[str] = item.person_ids
        elif isinstance(item, DashboardLesson):
            ids = (item.person_id,)
        else:
            ids = item
        return [self._people[i] for i in ids if i in self._people]

    def kc(self, color: object) -> str:
        return self.colors.cls(color)

    def item_color(self, item: DashboardAgendaItem) -> object:
        return item.color if isinstance(item, DashboardEvent) else item.color_hex

    def is_past(self, item: DashboardAgendaItem) -> bool:
        """Over already (all-day events never are, today)."""
        if isinstance(item, DashboardEvent) and item.all_day:
            return False
        if item.end > item.start:
            return item.end <= self.now
        return item.start < self.now  # a point in time

    def is_now(self, item: DashboardAgendaItem) -> bool:
        if isinstance(item, DashboardEvent) and item.all_day:
            return False
        return item.start <= self.now < item.end

    def avatar(self, person: DashboardPerson) -> str | None:
        return avatar_url(person)

    def icon(self, item: DashboardAgendaItem) -> str:
        """A lesson's own timetable icon; events have none (yet)."""
        if isinstance(item, DashboardLesson):
            return item.icon or ""
        return ""

    def lesson_meta(self, item: DashboardAgendaItem) -> str:
        """``"Room B12 · Keller"`` for a lesson; ``""`` otherwise."""
        if not isinstance(item, DashboardLesson):
            return ""
        parts = []
        if item.room:
            parts.append(self.t("dashboard.lesson.room", room=item.room))
        if item.teacher:
            parts.append(item.teacher)
        return " · ".join(parts)


# ─── Home: who's home, school ────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class PresenceGroups:
    home: tuple[DashboardPerson, ...]
    away: tuple[DashboardPerson, ...]


def presence_groups(snapshot: HomeSnapshot) -> PresenceGroups | None:
    """Who's home vs away — ``None`` when it isn't shown: presence off,
    a household of one, or nobody's whereabouts known."""
    if SECTION_PRESENCE not in snapshot.sections or len(snapshot.people) < 2:
        return None
    home = tuple(p for p in snapshot.people if p.presence == "home")
    away = tuple(p for p in snapshot.people if p.presence in ("away", "zone"))
    if not home and not away:
        return None
    return PresenceGroups(home=home, away=away)


def school_now(
    days: Iterable[DashboardSchoolDay], now: datetime
) -> list[DashboardLesson]:
    """Each child's lesson running right now (one per child)."""
    out: list[DashboardLesson] = []
    for day in days:
        running = [
            ls for ls in day.lessons if ls.takes_place and ls.start <= now < ls.end
        ]
        if running:
            out.append(max(running, key=lambda ls: ls.start))
    return out


def school_changes(days: Iterable[DashboardSchoolDay]) -> list[DashboardLesson]:
    """Today's timetable changes (cancelled / changed / added), in time
    order."""
    return sorted(
        (ls for day in days for ls in day.changes), key=lambda ls: (ls.start, ls.end)
    )


# ─── Home: swim-lane timeline ────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class LaneBlock:
    """One bar in a person's lane. ``x`` / ``w`` are whole percent of
    the timeline; ``row`` stacks overlapping bars."""

    title: str
    label: str
    x: int
    w: int
    row: int
    color: str
    kind: str  # "event" | "school"
    past: bool


@dataclass(slots=True, frozen=True)
class Lane:
    person: DashboardPerson
    blocks: tuple[LaneBlock, ...]
    all_day: tuple[str, ...]
    rows: int


@dataclass(slots=True, frozen=True)
class Tick:
    """An hour label on the timeline; ``minor`` ones are dropped on a
    phone (every 4 h instead of every 2 h)."""

    label: str
    x: int
    minor: bool = False


@dataclass(slots=True, frozen=True)
class Timeline:
    start_hour: int
    end_hour: int
    ticks: tuple[Tick, ...]
    now_x: int | None
    lanes: tuple[Lane, ...]


def _minute_of_day(value: datetime, today: date) -> int:
    """Wall-clock minutes since today's midnight, clamped to today."""
    if value.date() < today:
        return 0
    if value.date() > today:
        return 24 * 60
    return value.hour * 60 + value.minute


def _rows(spans: Sequence[tuple[int, int]]) -> list[int]:
    """Greedy rows for ``spans`` (sorted by start): each goes into the
    first row whose last bar has ended; at most :data:`MAX_LANE_ROWS`."""
    ends: list[int] = []
    out: list[int] = []
    for start, end in spans:
        for i, last in enumerate(ends):
            if last <= start:
                ends[i] = end
                out.append(i)
                break
        else:
            if len(ends) < MAX_LANE_ROWS:
                ends.append(end)
                out.append(len(ends) - 1)
            else:
                out.append(MAX_LANE_ROWS - 1)
    return out


def build_timeline(snapshot: HomeSnapshot, fmt: Fmt) -> Timeline | None:
    """Home's TODAY swim-lanes: one lane per person, their timed events
    and their school block (first to last lesson that takes place) laid
    on 08–22, stretched to cover earlier or later items.

    ``None`` when lanes don't fit: more than :data:`MAX_LANES` people
    (Home lists the day instead), or nothing timed or all-day today.
    """
    people = snapshot.people
    if not people or len(people) > MAX_LANES:
        return None
    today = snapshot.today
    spans: dict[str, list[tuple[int, int, str, str, str, str]]] = {
        p.user_id: [] for p in people
    }
    all_day: dict[str, list[str]] = {p.user_id: [] for p in people}
    if SECTION_CALENDAR in snapshot.sections:
        for ev in snapshot.events_today:
            for uid in ev.person_ids:
                if uid not in spans:
                    continue
                if ev.all_day:
                    all_day[uid].append(ev.title)
                    continue
                s = _minute_of_day(ev.start, today)
                e = max(_minute_of_day(ev.end, today), s)
                spans[uid].append(
                    (s, e, ev.title, f"{fmt.span(ev)} {ev.title}", "event", uid)
                )
    if SECTION_TIMETABLE in snapshot.sections:
        school = fmt.t("dashboard.school")
        for day in snapshot.school:
            if day.person_id not in spans or day.first_start is None:
                continue
            assert day.last_end is not None
            s = _minute_of_day(day.first_start, today)
            e = _minute_of_day(day.last_end, today)
            label = f"{hhmm(day.first_start)}–{hhmm(day.last_end)} {school}"
            spans[day.person_id].append((s, e, school, label, "school", day.person_id))
    timed = [x for rows in spans.values() for x in rows]
    if not timed and not any(all_day.values()):
        return None
    start_h = min([DAY_START_HOUR, *(s // 60 for s, *_ in timed)])
    end_h = max([DAY_END_HOUR, *(-(-e // 60) for _, e, *_ in timed)])
    end_h = min(end_h, 24)
    lo, total = start_h * 60, (end_h - start_h) * 60

    def pct(minute: int) -> int:
        return max(0, min(100, round((minute - lo) * 100 / total)))

    now_minute = _minute_of_day(snapshot.now, today)
    now_x = pct(now_minute) if lo <= now_minute <= end_h * 60 else None
    by_id = {p.user_id: p for p in people}
    lanes: list[Lane] = []
    for p in people:
        mine = sorted(spans[p.user_id], key=lambda x: (x[0], x[1]))
        rows = _rows([(s, e) for s, e, *_ in mine])
        blocks = []
        for (s, e, title, label, kind, uid), row in zip(mine, rows, strict=True):
            x = min(pct(s), 99)
            w = max(1, min(pct(e) - x, 100 - x))
            blocks.append(
                LaneBlock(
                    title=title,
                    label=label,
                    x=x,
                    w=w,
                    row=row,
                    color=fmt.kc(by_id[uid].color),
                    kind=kind,
                    past=e <= now_minute if e > s else s < now_minute,
                )
            )
        lanes.append(
            Lane(
                person=p,
                blocks=tuple(blocks),
                all_day=tuple(all_day[p.user_id]),
                rows=max([0, *rows]) + 1,
            )
        )
    # Every second hour — except right under the "now" label.
    ticks = tuple(
        Tick(label=f"{h:02d}", x=pct(h * 60), minor=(h - start_h) % 4 == 2)
        for h in range(start_h, end_h + 1)
        if (h - start_h) % 2 == 0
        and (now_x is None or abs(pct(h * 60) - now_x) >= NOW_LABEL_CLEARANCE)
    )
    return Timeline(
        start_hour=start_h,
        end_hour=end_h,
        ticks=ticks,
        now_x=now_x,
        lanes=tuple(lanes),
    )


# ─── Night ───────────────────────────────────────────────────────────────


def night_item(
    snapshot: HomeSnapshot | PersonSnapshot,
) -> tuple[str, DashboardAgendaItem] | None:
    """What night mode shows under the clock: ``(when, item)`` where
    ``when`` is ``"today"`` or ``"tomorrow"``.

    Before noon (the small hours of a night window that wrapped
    midnight) "the morning" is *today's* first item still to come;
    from noon on it is tomorrow's first timed item.
    """
    morning = snapshot.now.hour < 12
    if isinstance(snapshot, HomeSnapshot):
        if morning:
            return ("today", snapshot.next_up) if snapshot.next_up else None
        first = snapshot.tomorrow_first
        return ("tomorrow", first) if first else None
    if morning:
        return ("today", snapshot.next_item) if snapshot.next_item else None
    timed = [
        i
        for i in snapshot.tomorrow_items
        if not (isinstance(i, DashboardEvent) and i.all_day)
    ]
    first = timed[0] if timed else snapshot.school_tomorrow_first
    return ("tomorrow", first) if first else None
