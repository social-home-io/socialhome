"""The picture view — a person page for someone who can't read yet.

Mockup D: pictures first. What a five-year-old reads:

* **an analog clock** showing the real household time, its hands set
  server-side (``dashboard.js`` re-checks them every second and
  writes the hands when they change);
* **NOW → NEXT**: two big emoji cards. NEXT carries a *time-timer
  wedge* — see :func:`wedge`;
* **MY DAY**: the day's events and lessons as emoji tiles under the
  four day parts 🌅 ☀️ 🌇 🌙 — past tiles dimmed, the running one
  framed, a cancelled lesson greyed with a big ✕;
* **tomorrow**: 🌙 per sleep, then tomorrow's first things;
* **MY JOBS** (emoji + ☐ / ✓), **HOME** (faces under 🏠 / 🚗) and, on
  an empty day, one big 🎈.

**Helper captions.** Under every tile sits a small, muted caption — the
title (a chore's name; NOW / NEXT add the time, "Swimming · 15:00") —
and each section has a tiny label ("Now", "My day", "My jobs" …), so a
grown-up or an older sibling standing next to the child can explain
what a picture means. They are small print: the emoji stays what the
child reads. Every tile's full title and time still goes to screen
readers through its ``aria-label``; the caption repeats part of it, so
the template hides it from them (``aria-hidden``).

**The wedge.** Children know the "Time Timer": a red disc that shrinks
as time runs out. The wedge is that disc, drawn on the clock face from
the minute hand clockwise to where the minute hand will be when NEXT
starts — so the hand "eats" the wedge and NEXT begins when it is gone.
It is capped at one full turn: from an hour out the whole face is
tinted (a lot of time), and only the last hour shrinks. The NEXT card
repeats it as a small pie from twelve o'clock. Minutes are *real*
minutes (UTC arithmetic), so a DST switch never stretches it.

Pure: no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from ..domain.household_dashboard import (
    SECTION_PRESENCE,
    SECTION_TASKS,
    DashboardAgendaItem,
    DashboardEvent,
    DashboardLesson,
    DashboardPerson,
    DashboardTask,
    PersonSnapshot,
)
from .icons import BREAK_ICON, suggest_icon, title_icon
from .presenter import SCHOOL_ICON, Fmt, night_item

__all__ = [
    "CLOCK_CENTER",
    "DAY_PARTS",
    "FREE_HOME_ICON",
    "FREE_ICON",
    "PIE_CENTER",
    "WEDGE_FULL_MINUTES",
    "AnalogClock",
    "DayPart",
    "PictureJob",
    "PictureNight",
    "PicturePart",
    "PictureTile",
    "PictureView",
    "Wedge",
    "analog_clock",
    "day_part",
    "hand_angles",
    "item_icon",
    "minutes_until",
    "picture_view",
    "rotate",
    "sector_path",
    "wedge",
]

#: The clock is drawn in a 200 × 200 SVG box around this centre.
CLOCK_CENTER: Final = 100
#: The wedge sits inside the hour ticks.
CLOCK_WEDGE_RADIUS: Final = 88
#: The NEXT card's little pie: a 100 × 100 box.
PIE_CENTER: Final = 50
PIE_RADIUS: Final = 46
#: From this many minutes out the wedge is a full disc.
WEDGE_FULL_MINUTES: Final = 60

#: Nothing on now: free time — at home a house, else a toy.
FREE_ICON: Final = "🧸"
FREE_HOME_ICON: Final = "🏠"


# ─── Clock + wedge ───────────────────────────────────────────────────────


def hand_angles(now: datetime) -> tuple[float, float]:
    """Hour and minute hand, in degrees clockwise from twelve, for
    ``now``'s wall-clock time (in its own zone — the household's).
    Whole minutes only: ``dashboard.js`` re-checks every second with
    the same formula and writes the hands when they change."""
    hour = (now.hour % 12) * 30 + now.minute * 0.5
    return hour, now.minute * 6.0


def rotate(deg: float, cx: int = CLOCK_CENTER, cy: int = CLOCK_CENTER) -> str:
    """An SVG ``transform`` attribute (a presentation attribute, not a
    ``style`` — the CSP allows it), spelled like ``dashboard.js`` does."""
    return f"rotate({deg:g} {cx} {cy})"


def minutes_until(now: datetime, then: datetime) -> int:
    """Whole minutes from ``now`` to ``then``, rounded up; ``0`` once it
    is due. Real elapsed time — compared in UTC, because subtracting two
    datetimes of the same zone ignores a DST switch in between."""
    seconds = (then.astimezone(UTC) - now.astimezone(UTC)).total_seconds()
    return max(0, math.ceil(seconds / 60))


def _point(cx: float, cy: float, r: float, deg: float) -> str:
    rad = math.radians(deg)
    x = round(cx + r * math.sin(rad), 2)
    y = round(cy - r * math.cos(rad), 2)
    return f"{x:g} {y:g}"


def sector_path(cx: float, cy: float, r: float, start_deg: float, sweep: float) -> str:
    """An SVG path for a pie slice: from the centre out to ``start_deg``
    (clockwise from twelve), along the rim for ``sweep`` degrees (less
    than a full turn), back to the centre."""
    large = 1 if sweep > 180 else 0
    start = _point(cx, cy, r, start_deg)
    end = _point(cx, cy, r, start_deg + sweep)
    return f"M{cx:g} {cy:g}L{start}A{r:g} {r:g} 0 {large} 1 {end}Z"


@dataclass(slots=True, frozen=True)
class Wedge:
    """Time left until something starts, as a pie. ``full`` — an hour
    or more: draw the whole disc (``path`` is empty then)."""

    minutes: int
    full: bool
    path: str


def wedge(
    now: datetime, start: datetime, *, start_deg: float, cx: float, cy: float, r: float
) -> Wedge | None:
    """The time-timer wedge from ``start_deg`` for the minutes until
    ``start`` (capped at :data:`WEDGE_FULL_MINUTES`); ``None`` once it
    started."""
    minutes = minutes_until(now, start)
    if minutes <= 0:
        return None
    if minutes >= WEDGE_FULL_MINUTES:
        return Wedge(minutes=WEDGE_FULL_MINUTES, full=True, path="")
    return Wedge(
        minutes=minutes,
        full=False,
        path=sector_path(cx, cy, r, start_deg, minutes * 6),
    )


@dataclass(slots=True, frozen=True)
class AnalogClock:
    """What the clock macro draws: hand ``transform`` attributes, the
    optional wedge (in colour class ``color``) and the spoken time."""

    hour: str
    minute: str
    label: str
    wedge: Wedge | None = None
    color: str = ""


def analog_clock(
    now: datetime, *, label: str, next_start: datetime | None = None, color: str = ""
) -> AnalogClock:
    hour, minute = hand_angles(now)
    w = (
        wedge(
            now,
            next_start,
            start_deg=minute,
            cx=CLOCK_CENTER,
            cy=CLOCK_CENTER,
            r=CLOCK_WEDGE_RADIUS,
        )
        if next_start is not None
        else None
    )
    return AnalogClock(
        hour=rotate(hour),
        minute=rotate(minute),
        label=label,
        wedge=w,
        color=color if w is not None else "",
    )


# ─── Day parts ───────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class DayPart:
    key: str
    icon: str
    #: First wall-clock hour of the part.
    starts: int


DAY_PARTS: Final[tuple[DayPart, ...]] = (
    DayPart("morning", "🌅", 0),
    DayPart("afternoon", "☀️", 12),
    DayPart("evening", "🌇", 17),
    DayPart("night", "🌙", 20),
)


def day_part(value: datetime) -> DayPart:
    """The part of the day ``value``'s wall-clock hour falls in."""
    return next(p for p in reversed(DAY_PARTS) if value.hour >= p.starts)


# ─── Tiles ───────────────────────────────────────────────────────────────


def item_icon(item: DashboardAgendaItem) -> str:
    """A lesson's own timetable icon, else a guess from its subject (a
    break: from its label, else 🍎); an event's leading emoji or a guess
    from its title; else the neutral 🔹."""
    if isinstance(item, DashboardEvent):
        return title_icon(item.title)
    if item.icon:
        return item.icon
    if item.kind == "break":
        return suggest_icon(item.label or item.title, school=True) or BREAK_ICON
    return title_icon(item.title or item.label, school=True)


@dataclass(slots=True, frozen=True)
class PictureTile:
    """One emoji tile. ``label`` is what screen readers say; ``caption``
    the small print under the emoji (screen readers skip it — the label
    already says it)."""

    icon: str
    label: str
    caption: str = ""
    color: str = ""
    past: bool = False
    current: bool = False
    cancelled: bool = False


@dataclass(slots=True, frozen=True)
class PicturePart:
    """A day part's column: ``state`` is ``past`` / ``now`` / ``later``."""

    key: str
    icon: str
    label: str
    state: str
    tiles: tuple[PictureTile, ...]


@dataclass(slots=True, frozen=True)
class PictureJob:
    icon: str
    label: str
    done: bool
    caption: str = ""


@dataclass(slots=True, frozen=True)
class PictureNight:
    """Night mode's one item: ``when`` is ``today`` / ``tomorrow``."""

    when: str
    icon: str
    label: str
    #: "Tomorrow · School 07:30".
    caption: str = ""


@dataclass(slots=True, frozen=True)
class PictureView:
    clock: AnalogClock
    part_now: DayPart
    parts: tuple[PicturePart, ...]
    empty: bool
    now: PictureTile | None = None
    next: PictureTile | None = None
    next_pie: Wedge | None = None
    tomorrow: tuple[PictureTile, ...] = ()
    sleeps: int = 0
    jobs: tuple[PictureJob, ...] = ()
    home: tuple[DashboardPerson, ...] = ()
    away: tuple[DashboardPerson, ...] = ()
    night: PictureNight | None = None


def _is_cancelled(item: DashboardAgendaItem) -> bool:
    return isinstance(item, DashboardLesson) and item.status == "cancelled"


def _time(item: DashboardAgendaItem, f: Fmt) -> str:
    """When ``item`` starts, as the reader page spells it — "All day"
    for an all-day event (never a midnight "00:00")."""
    if isinstance(item, DashboardEvent) and item.all_day:
        return f.span(item)
    return f.hhmm(item.start)


def _card_caption(item: DashboardAgendaItem, f: Fmt) -> str:
    """A NOW / NEXT card's caption: "Swimming · 15:00"."""
    return f.t("dashboard.picture.at", title=f.title(item), time=_time(item, f))


def _tile(item: DashboardAgendaItem, f: Fmt, *, today: bool = True) -> PictureTile:
    """An item's tile. ``today=False`` (tomorrow's list): never past or
    running — an event that started earlier (a holiday, a sleepover)
    still shows as tomorrow's."""
    cancelled = _is_cancelled(item)
    key = "dashboard.picture.cancelled" if cancelled else "dashboard.picture.item"
    return PictureTile(
        icon=item_icon(item),
        label=f.t(key, when=f.span(item), title=f.title(item)),
        caption=f.title(item),
        color=f.kc(f.item_color(item)),
        past=today and f.is_past(item),
        current=today and f.is_now(item) and not cancelled,
        cancelled=cancelled,
    )


def _school_tile(item: DashboardLesson, f: Fmt) -> PictureTile:
    """Tomorrow's school day, said once: 🎒 at the first lesson."""
    school = f.t("dashboard.school")
    return PictureTile(
        icon=SCHOOL_ICON,
        label=f.t("dashboard.picture.item", when=f.hhmm(item.start), title=school),
        caption=school,
        color=f.kc(item.color_hex),
    )


def _parts(page: PersonSnapshot, f: Fmt) -> tuple[PicturePart, ...]:
    current = day_part(page.now)
    buckets: dict[str, list[PictureTile]] = {p.key: [] for p in DAY_PARTS}
    for item in page.today_items:
        all_day = isinstance(item, DashboardEvent) and item.all_day
        part = DAY_PARTS[0] if all_day else day_part(item.start)
        buckets[part.key].append(_tile(item, f))
    out = []
    for p in DAY_PARTS:
        if p.starts < current.starts:
            state = "past"
        elif p == current:
            state = "now"
        else:
            state = "later"
        out.append(
            PicturePart(
                key=p.key,
                icon=p.icon,
                label=f.t(f"dashboard.picture.part.{p.key}"),
                state=state,
                tiles=tuple(buckets[p.key]),
            )
        )
    return tuple(out)


def _jobs(page: PersonSnapshot, f: Fmt) -> tuple[PictureJob, ...]:
    """Today's jobs: done today, due today or overdue — not the week's."""
    if SECTION_TASKS not in page.sections:
        return ()
    return tuple(
        _job(c, f)
        for c in page.chores
        if c.done or (c.due is not None and c.due <= page.today)
    )


def _job(c: DashboardTask, f: Fmt) -> PictureJob:
    if c.done:
        state = f.t("dashboard.chore.done")
    elif c.overdue_days:
        state = f.t("dashboard.chore.overdue")
    else:
        state = f.t("dashboard.chore.open")
    return PictureJob(
        icon=title_icon(c.title),
        label=f.t("dashboard.picture.job", state=state, title=c.title),
        done=c.done,
        caption=c.title,
    )


def _whereabouts(
    page: PersonSnapshot,
) -> tuple[tuple[DashboardPerson, ...], tuple[DashboardPerson, ...]]:
    if SECTION_PRESENCE not in page.sections:
        return (), ()
    others = [p for p in page.people if p.user_id != page.person.user_id]
    home = tuple(p for p in others if p.presence == "home")
    away = tuple(p for p in others if p.presence in ("away", "zone"))
    return home, away


def _night(page: PersonSnapshot, f: Fmt) -> PictureNight | None:
    if not page.is_night:
        return None
    found = night_item(page)
    if found is None:
        return None
    when, item = found
    if isinstance(item, DashboardLesson):
        icon, title = SCHOOL_ICON, f.t("dashboard.school")
    else:
        icon, title = item_icon(item), f.title(item)
    day, time = f.t(f"dashboard.night.{when}"), f.hhmm(item.start)
    return PictureNight(
        when=when,
        icon=icon,
        label=f.t("dashboard.picture.night", when=day, time=time, title=title),
        caption=f.t(
            "dashboard.picture.night_caption", when=day, time=time, title=title
        ),
    )


def picture_view(
    page: PersonSnapshot, f: Fmt, *, tomorrow: Sequence[DashboardAgendaItem]
) -> PictureView:
    """Everything the picture page draws. ``tomorrow`` is the person
    page's "tomorrow" list (first few events + the first lesson)."""
    now_item, next_item = page.now_item, page.next_item
    time_label = f.t("dashboard.picture.clock", time=f.hhmm(page.now))
    clock = analog_clock(
        page.now,
        label=time_label,
        next_start=next_item.start if next_item else None,
        color=f.kc(f.item_color(next_item)) if next_item else "",
    )
    now_tile = next_tile = pie = None
    if now_item is not None:
        now_tile = PictureTile(
            icon=item_icon(now_item),
            label=f.t("dashboard.picture.now", title=f.title(now_item)),
            caption=_card_caption(now_item, f),
            color=f.kc(f.item_color(now_item)),
            current=True,
        )
    elif next_item is not None:
        at_home = SECTION_PRESENCE in page.sections and page.person.presence == "home"
        free = f.t("dashboard.picture.free")
        now_tile = PictureTile(
            icon=FREE_HOME_ICON if at_home else FREE_ICON, label=free, caption=free
        )
    if next_item is not None:
        next_tile = PictureTile(
            icon=item_icon(next_item),
            label=f.t(
                "dashboard.picture.next",
                title=f.title(next_item),
                when=f.rel(next_item.start),
            ),
            caption=_card_caption(next_item, f),
            color=f.kc(f.item_color(next_item)),
        )
        pie = wedge(
            page.now,
            next_item.start,
            start_deg=0,
            cx=PIE_CENTER,
            cy=PIE_CENTER,
            r=PIE_RADIUS,
        )
    tomorrow_tiles = tuple(
        _school_tile(i, f)
        if isinstance(i, DashboardLesson)
        else _tile(i, f, today=False)
        for i in tomorrow
    )
    home, away = _whereabouts(page)
    return PictureView(
        clock=clock,
        part_now=day_part(page.now),
        parts=_parts(page, f) if page.today_items else (),
        empty=not page.today_items,
        now=now_tile,
        next=next_tile,
        next_pie=pie,
        tomorrow=tomorrow_tiles,
        # The list holds only tomorrow's things (some may have started
        # earlier — a holiday, a sleepover): always one sleep away.
        sleeps=1 if tomorrow else 0,
        jobs=_jobs(page, f),
        home=home,
        away=away,
        night=_night(page, f),
    )
