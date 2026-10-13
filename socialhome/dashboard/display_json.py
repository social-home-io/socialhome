"""The wall-panel JSON (``/api/display/v1/…``, ``api_version`` 1).

A wall panel (an ESPHome LVGL display) draws the same boards as the
browser dashboard, from JSON. This module spells the board view models
(:mod:`.boards`) as JSON — the HTML templates' twin — with every
user-readable string pre-formatted on the server: times in the
household's zone, relative phrases ("in 20 min"), translated labels from
the dashboard catalog. The firmware stays locale-free and layout-only.

What a payload carries is what the matching HTML board shows — nothing
more (no event locations, lesson notes, shopping stores, raw presence
rows): the read model has already applied every visibility rule
(hidden people, child protection, night, the chat opt-in).

* People are listed once per payload (``people``) and referenced by id.
* An emoji is ``{"text", "code"}``: ``code`` is the Twemoji code for
  ``GET /api/display/v1/emoji/{code}`` (:func:`~.emoji.emoji_code`),
  ``null`` when the text is no emoji (a zone pin may be a letter).
* Colours are validated ``#rrggbb`` (or ``null``), each with the black /
  white ``ink`` that reads on it where text sits on it.

The contract is ``docs/display-api/v1/*.schema.json``. Additive changes
keep ``api_version`` 1; anything breaking moves to ``/api/display/v2/``.

Pure: no I/O.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import tzinfo
from typing import Final

from ..domain.household_dashboard import (
    SECTION_PRESENCE,
    SECTION_STICKIES,
    SECTION_TASKS,
    SECTION_TIMETABLE,
    SECTION_CALENDAR,
    DashboardAgendaItem,
    DashboardLesson,
    DashboardPerson,
    DashboardSettings,
    DashboardSticky,
    DashboardTask,
)
from ..domain.theme import HouseholdTheme
from .boards import BoardBase, HomeBoard, PersonBoard
from .clock import WallClock, household_clock
from .contrast import hex_color, ink_on, note_colors, readable_on
from .emoji import emoji_code
from .map_view import HOME_ICON, PLACE_ICON, AWAY_ICON, MapView
from .messages import ChatDay, MentionCard, MessageRow
from .picture import (
    PictureJob,
    PictureTile,
    PictureView,
    Wedge,
    hand_angles,
)
from .presenter import (
    DARK_SURFACES,
    LIGHT_SURFACES,
    SCHOOL_ICON,
    Fmt,
    SchoolSpan,
    Timeline,
)
from .render import text_direction
from .tabs import TAB_MESSAGES, TAB_OVERVIEW, HomeTab

__all__ = [
    "API_VERSION",
    "API_PATH",
    "CSS_PALETTES",
    "EMPTY_DAY_ICON",
    "STRING_KEYS",
    "board_header",
    "css_palettes",
    "display_clock",
    "emoji",
    "home_payload",
    "lock_payload",
    "map_payload",
    "messages_payload",
    "notes_payload",
    "person_payload",
    "theme_payload",
]

API_VERSION: Final = 1
API_PATH: Final = "/api/display/v1"
EMPTY_DAY_ICON: Final = "🎈"
MESSAGES_EMPTY_ICON: Final = "💬"
NOTES_EMPTY_ICON: Final = "📝"
NIGHT_TOMORROW_ICON: Final = "🌙"
NIGHT_MORNING_ICON: Final = "🌅"
#: A lesson status that gets a tag (and the catalog key that names it).
_TAGGED_STATUSES: Final = frozenset({"cancelled", "changed", "added"})

T = Callable[..., str]
Json = dict[str, object]


# ─── Small spellings ─────────────────────────────────────────────────────


def emoji(text: str | None) -> Json | None:
    """``{"text", "code"}`` for a glyph the panel draws; ``None`` for none."""
    if not text:
        return None
    return {"text": text, "code": emoji_code(text)}


def _color(value: object) -> str | None:
    return hex_color(value)


def _ink(value: object) -> str | None:
    color = hex_color(value)
    return None if color is None else ink_on(color)


def _cls_color(fmt: Fmt, cls: str) -> str | None:
    return fmt.colors.color_of(cls)


def display_clock(now: float, zone: tzinfo, t: T) -> Json:
    """The household clock, truncated to the minute — so a payload (and
    its ETag) changes at most once a minute; the panel ticks its own
    clock (SNTP) and shows it in ``tz`` / ``utc_offset_minutes``."""
    reading: WallClock = household_clock(float(int(now) // 60 * 60), zone, t)
    offset = _offset_minutes(reading.iso)
    return {
        "time": reading.time,
        "date": reading.date,
        "day": reading.day,
        "iso": reading.iso,
        "unix": reading.unix,
        "tz": reading.tz,
        "utc_offset_minutes": offset,
    }


_OFFSET = re.compile(r"([+-])(\d\d):(\d\d)$")


def _offset_minutes(iso: str) -> int:
    found = _OFFSET.search(iso)
    if found is None:
        return 0
    sign = -1 if found.group(1) == "-" else 1
    return sign * (int(found.group(2)) * 60 + int(found.group(3)))


def avatar_path(person: DashboardPerson) -> str | None:
    """``/api/display/v1/avatar/<id>?v=<hash>`` (the panel adds
    ``&size=N``); ``None`` without a picture (the face shows initials)."""
    if not person.picture_hash:
        return None
    return f"{API_PATH}/avatar/{_q(person.user_id)}?v={_q(person.picture_hash)}"


def _q(value: str) -> str:
    # Local import-free quoting: ids and hashes are URL-safe tokens; any
    # other byte is percent-encoded.
    return "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "-._~") else _pct(ch)
        for ch in value
    )


def _pct(ch: str) -> str:
    return "".join(f"%{b:02X}" for b in ch.encode("utf-8"))


def _where(person: DashboardPerson, t: T) -> Json | None:
    """Where someone is, as the person page says it — never coordinates."""
    if person.presence == "home":
        return {"icon": emoji(HOME_ICON), "label": t("dashboard.presence.home")}
    if person.presence == "zone" and person.zone_name:
        return {"icon": emoji(PLACE_ICON), "label": person.zone_name}
    if person.presence in ("zone", "away"):
        return {"icon": emoji(AWAY_ICON), "label": t("dashboard.presence.away")}
    return None


def _person(
    person: DashboardPerson,
    *,
    t: T,
    presence_on: bool,
    badges: Mapping[str, int],
) -> Json:
    badge = badges.get(person.user_id, 0)
    return {
        "id": person.user_id,
        "name": person.display_name,
        "initials": person.initials,
        "color": _color(person.color),
        "ink": _ink(person.color),
        "avatar": avatar_path(person),
        "picture_hash": person.picture_hash,
        "picture_view": person.picture_view,
        "where": _where(person, t) if presence_on else None,
        "badge": badge,
        "badge_label": (
            t("dashboard.mentions.badge", name=person.display_name) if badge else None
        ),
    }


def _ids(people: Iterable[DashboardPerson]) -> list[str]:
    return [p.user_id for p in people]


# ─── Shared header ───────────────────────────────────────────────────────


def board_header(
    *,
    view: str,
    base: BoardBase,
    sections: frozenset[str],
    settings: DashboardSettings,
    household: str,
    clock: Json,
    view_key: str,
    current: str | None = None,
) -> Json:
    """What every board payload starts with: the household, clock,
    settings the panel needs, the rail's people and night mode."""
    t = base.fmt.t
    badges = Counter(c.person_id for c in base.badged if c.person_id)
    return {
        "api_version": API_VERSION,
        "view": view,
        "view_key": view_key,
        "household": household,
        "locale": settings.locale,
        "dir": text_direction(settings.locale),
        "clock": clock,
        "settings": {
            "idle_return_minutes": settings.idle_return_minutes,
            "keep_screen_on": settings.keep_screen_on,
            "mention_sound": settings.mention_sound,
            "night_start": settings.night_start,
            "night_end": settings.night_end,
            "pin_set": settings.pin_set,
        },
        "rail": {"shown": base.show_rail, "many": base.rail_many, "current": current},
        "people": [
            _person(
                p,
                t=t,
                presence_on=SECTION_PRESENCE in sections,
                badges=badges,
            )
            for p in base.people
        ],
        "is_night": base.night,
        "night": None,
    }


def _night(base: BoardBase, clock: Json, *, with_people: bool) -> Json:
    """Night mode (non-picture): the clock, the date and the morning's
    first thing — ``people`` only on Home (a person page is theirs)."""
    f, t = base.fmt, base.fmt.t
    item: Json | None = None
    if base.night_item is not None:
        when, it = base.night_item
        lesson = isinstance(it, DashboardLesson)
        item = {
            "when": when,
            "when_label": t(f"dashboard.night.{when}"),
            "time": f.hhmm(it.start),
            "title": t("dashboard.school") if lesson else f.title(it),
            "icon": emoji(SCHOOL_ICON) if lesson else None,
            "people": _ids(f.people(it)) if with_people else [],
        }
    return {
        "time": clock["time"],
        "date": clock["date"],
        "item": item,
        "wake_hint": t("dashboard.night.wake"),
    }


# ─── Rows ────────────────────────────────────────────────────────────────


def _agenda_row(item: DashboardAgendaItem, f: Fmt, *, with_people: bool) -> Json:
    """One line of a day list (``agenda_row``): time, (icon) title,
    timetable state, room / teacher."""
    status: str | None = None
    is_break = False
    if isinstance(item, DashboardLesson):
        status = item.status if item.status in _TAGGED_STATUSES else None
        is_break = item.kind == "break"
    return {
        "kind": item.item_type,
        "is_break": is_break,
        "time": f.span(item),
        "title": f.title(item),
        "icon": emoji(f.icon(item)),
        "status": status,
        "status_label": t_status(f.t, status),
        "now": f.is_now(item),
        "past": f.is_past(item),
        "meta": f.lesson_meta(item) or None,
        "color": _color(f.item_color(item)),
        "people": _ids(f.people(item)) if with_people else [],
    }


def t_status(t: T, status: str | None) -> str | None:
    return None if status is None else t(f"dashboard.lesson.{status}")


def _school_row(span: SchoolSpan, f: Fmt) -> Json:
    """A child's school day as one row of Home's day list."""
    return {
        "kind": "school",
        "is_break": False,
        "time": f"{f.hhmm(span.start)}–{f.hhmm(span.end)}",
        "title": f.t("dashboard.school"),
        "icon": emoji(SCHOOL_ICON),
        "status": None,
        "status_label": None,
        "now": False,
        "past": False,
        "meta": None,
        "color": None,
        "people": [span.person_id] if f.people((span.person_id,)) else [],
    }


def _chore(c: DashboardTask, f: Fmt, *, show_people: bool, show_due: bool) -> Json:
    if c.done:
        state = "done"
    elif c.overdue_days:
        state = "overdue"
    else:
        state = "open"
    late = f.late(c.overdue_days) if c.overdue_days and not c.done else None
    due = (
        f.due(c.due)
        if late is None and show_due and not c.done and f.due(c.due)
        else None
    )
    return {
        "id": c.id,
        "title": c.title,
        "state": state,
        "state_label": f.t(f"dashboard.chore.{state}"),
        "late": late,
        "due": due,
        "people": _ids(f.people(c.person_ids)) if show_people else [],
    }


def _message(m: MessageRow, t: T) -> Json:
    return {
        "id": m.id,
        "person": m.person.user_id,
        "name": m.person.display_name,
        "from_label": t("dashboard.messages.from", name=m.person.display_name),
        "icon": emoji(m.icon),
        "text": m.text,
        "caption": m.caption,
        "say": m.say,
        "new": m.new,
        "when": m.when,
        "iso": m.iso,
    }


def _mention(m: MentionCard, label: str, t: T) -> Json:
    return {
        "id": m.id,
        "key": m.key,
        "label": label,
        "person": m.person.user_id,
        "name": m.person.display_name,
        "from_label": t(
            "dashboard.mentions.from_for_you"
            if m.person_id
            else "dashboard.messages.from",
            name=m.person.display_name,
        ),
        "text": m.text,
        "caption": m.caption,
        "say": m.say,
        "when": m.when,
        "iso": m.iso,
        "ack": {"message_id": m.id, "person_id": m.person_id},
    }


def _note(s: DashboardSticky) -> Json:
    color = hex_color(s.color)
    colors = note_colors(color) if color is not None else None
    return {
        "id": s.id,
        "text": s.content,
        "color": color,
        "light": (
            None if colors is None else {"bg": colors.light_bg, "ink": colors.light_ink}
        ),
        "dark": (
            None if colors is None else {"bg": colors.dark_bg, "ink": colors.dark_ink}
        ),
    }


# ─── Home ────────────────────────────────────────────────────────────────


def _tabs(board: HomeBoard, t: T) -> list[Json]:
    out: list[Json] = []
    for tab in board.tabs:
        badge = (
            board.tab_new
            if tab.key == TAB_MESSAGES and board.tab != TAB_MESSAGES
            else 0
        )
        out.append(
            {
                "key": tab.key,
                "label": t(f"dashboard.tabs.{tab.key}"),
                "path": _tab_path(tab),
                "current": tab.key == board.tab,
                "badge": badge,
                "badge_label": t("dashboard.tabs.new", n=badge) if badge else None,
            }
        )
    return out


def _tab_path(tab: HomeTab) -> str:
    return f"{API_PATH}/home" if tab.key == TAB_OVERVIEW else f"{API_PATH}/{tab.key}"


def _home_common(payload: Json, board: HomeBoard, base: BoardBase, clock: Json) -> Json:
    t = base.fmt.t
    payload["tab"] = board.tab
    payload["tabs"] = _tabs(board, t)
    everyone = t("dashboard.mentions.everyone")
    payload["alerts"] = [_mention(m, everyone, t) for m in board.here_cards]
    if base.night:
        payload["night"] = _night(base, clock, with_people=True)
    return payload


def _timeline(tl: Timeline, f: Fmt) -> Json:
    return {
        "start_hour": tl.start_hour,
        "end_hour": tl.end_hour,
        "now_x": tl.now_x,
        "now_label": f.t("dashboard.timeline.now"),
        "label": f.t("dashboard.timeline.label"),
        "ticks": [{"label": k.label, "x": k.x, "minor": k.minor} for k in tl.ticks],
        "lanes": [
            {
                "person": lane.person.user_id,
                "color": _color(lane.person.color),
                "ink": _ink(lane.person.color),
                "all_day": list(lane.all_day),
                "rows": lane.rows,
                "blocks": [
                    {
                        "kind": b.kind,
                        "title": b.title,
                        "label": b.label,
                        "x": b.x,
                        "w": b.w,
                        "row": b.row,
                        "past": b.past,
                        "narrow": b.narrow,
                        "icon": emoji(b.icon),
                        "more": b.more,
                    }
                    for b in lane.blocks
                ],
            }
            for lane in tl.lanes
        ],
    }


def _overview(board: HomeBoard, base: BoardBase) -> Json:
    f, t, snap = base.fmt, base.fmt.t, board.snap
    ov = board.overview
    assert ov is not None
    presence: Json | None = None
    if ov.presence is not None:
        presence = {
            "label": t("dashboard.home.whos_home"),
            "home": (
                {
                    "icon": emoji(HOME_ICON),
                    "label": t("dashboard.presence.home"),
                    "people": _ids(ov.presence.home),
                }
                if ov.presence.home
                else None
            ),
            "away": (
                {
                    "icon": emoji(AWAY_ICON),
                    "label": t("dashboard.presence.away"),
                    "people": [
                        {"id": p.user_id, "zone": p.zone_name or None}
                        for p in ov.presence.away
                    ],
                }
                if ov.presence.away
                else None
            ),
        }
    nxt: Json | None = None
    if snap.next_up is not None:
        it = snap.next_up
        nxt = {
            "kind": "next",
            "label": t("dashboard.home.next"),
            "time": f.hhmm(it.start),
            "icon": emoji(f.icon(it)),
            "title": f.title(it),
            "rel": f.rel(it.start),
            "people": _ids(f.people(it)),
            "color": _color(f.item_color(it)),
        }
    elif snap.tomorrow_first is not None:
        it = snap.tomorrow_first
        lesson = isinstance(it, DashboardLesson)
        nxt = {
            "kind": "tomorrow",
            "label": t("dashboard.person.tomorrow"),
            "time": f.hhmm(it.start),
            "icon": emoji(SCHOOL_ICON) if lesson else None,
            "title": t("dashboard.school") if lesson else f.title(it),
            "rel": None,
            "people": _ids(f.people(it)),
            "color": _color(f.item_color(it)),
        }
    messages: Json | None = None
    if ov.messages:
        messages = {
            "label": t("dashboard.messages.heading"),
            "see_all": ov.messages_more,
            "items": [_message(m, t) for m in ov.messages],
        }
    today: Json | None = None
    if SECTION_CALENDAR in snap.sections or SECTION_TIMETABLE in snap.sections:
        if ov.timeline is not None:
            mode = "timeline"
        elif base.rail_many and ov.day_rows:
            mode = "list"
        else:
            mode = "empty"
        today = {
            "label": t("dashboard.home.today"),
            "mode": mode,
            "timeline": None if ov.timeline is None else _timeline(ov.timeline, f),
            "list": [
                _school_row(r, f)
                if isinstance(r, SchoolSpan)
                else _agenda_row(r, f, with_people=True)
                for r in ov.day_rows
            ],
            "empty": t("dashboard.empty_day") if mode == "empty" else None,
            "empty_icon": emoji(EMPTY_DAY_ICON) if mode == "empty" else None,
            "at_school": (
                {
                    "label": t("dashboard.home.at_school_now"),
                    "items": [
                        {
                            "people": _ids(f.people(ls)),
                            "icon": emoji(f.icon(ls)),
                            "title": f.title(ls),
                            "until": f.until(ls.end),
                        }
                        for ls in ov.at_school
                    ],
                }
                if ov.at_school
                else None
            ),
            "changes": (
                {
                    "label": t("dashboard.home.school_changes"),
                    "items": [_change(ls, f) for ls in ov.changes],
                }
                if ov.changes
                else None
            ),
        }
    chores: Json | None = None
    if SECTION_TASKS in snap.sections and snap.chores_today:
        label = t("dashboard.home.chores")
        if ov.open_chores:
            label += " · " + t("dashboard.home.chores_open", n=ov.open_chores)
        chores = {
            "label": label,
            "items": [
                _chore(c, f, show_people=True, show_due=False)
                for c in snap.chores_today
            ],
        }
    shopping: Json | None = None
    if snap.shopping:
        shopping = {
            "label": f"{t('dashboard.home.shopping')} · {snap.shopping_total}",
            "items": [item.text for item in ov.shopping_shown],  # type: ignore[attr-defined]
            "more": (
                t("dashboard.home.shopping_more", n=ov.shopping_more)
                if ov.shopping_more
                else None
            ),
        }
    notes: Json | None = None
    if SECTION_STICKIES in snap.sections and ov.stickies:
        notes = {
            "label": f"{t('dashboard.home.notes')} · {snap.stickies_total}",
            "see_all": ov.notes_more,
            "items": [_note(s) for s in ov.stickies],
        }
    return {
        "presence": presence,
        "next": nxt,
        "messages": messages,
        "today": today,
        "chores": chores,
        "shopping": shopping,
        "notes": notes,
    }


def _change(ls: DashboardLesson, f: Fmt) -> Json:
    who = f.people(ls)
    meta = f.lesson_meta(ls) if ls.status != "cancelled" else ""
    return {
        "status": ls.status,
        "status_label": f.t(f"dashboard.lesson.{ls.status}"),
        "who": who[0].display_name if who else None,
        "title": f"{f.hhmm(ls.start)} {f.title(ls)}",
        "meta": meta or None,
        "people": _ids(who),
    }


def home_payload(header: Json, board: HomeBoard, base: BoardBase, clock: Json) -> Json:
    """``GET /api/display/v1/home`` — Home's Overview."""
    payload = _home_common(header, board, base, clock)
    payload["overview"] = _overview(board, base)
    return payload


def _chat_days(days: list[ChatDay], t: T) -> list[Json]:
    return [
        {
            "key": day.key,
            "label": day.label,
            "rows": [
                {
                    **_message(r.message, t),
                    "text": r.text,
                    "rest": r.rest,
                    "time": r.time,
                    "playing": r.playing,
                }
                for r in day.rows
            ],
        }
        for day in days
    ]


def _tab_empty(icon: str, title: str, text: str) -> Json:
    return {"icon": emoji(icon), "title": title, "text": text}


def messages_payload(
    header: Json, board: HomeBoard, base: BoardBase, clock: Json
) -> Json:
    """``GET /api/display/v1/messages`` — the last day of the household chat."""
    t = base.fmt.t
    payload = _home_common(header, board, base, clock)
    days = board.chat_days or []
    payload["messages"] = {
        "days": _chat_days(days, t),
        # Nothing to list — unless an @here banner shows the only message.
        "empty": (
            _tab_empty(
                MESSAGES_EMPTY_ICON,
                t("dashboard.messages.empty"),
                t("dashboard.messages.empty_hint"),
            )
            if not days and not board.here_cards
            else None
        ),
    }
    return payload


def notes_payload(header: Json, board: HomeBoard, base: BoardBase, clock: Json) -> Json:
    """``GET /api/display/v1/notes`` — every household sticky."""
    t = base.fmt.t
    payload = _home_common(header, board, base, clock)
    notes = board.notes or ()
    payload["notes"] = {
        "items": [_note(s) for s in notes],
        "more": (
            t("dashboard.home.shopping_more", n=board.notes_beyond)
            if notes and board.notes_beyond
            else None
        ),
        "empty": (
            None
            if notes
            else _tab_empty(
                NOTES_EMPTY_ICON,
                t("dashboard.notes.empty"),
                t("dashboard.notes.empty_hint"),
            )
        ),
    }
    return payload


def map_payload(
    header: Json, board: HomeBoard, base: BoardBase, clock: Json, attribution: str
) -> Json:
    """``GET /api/display/v1/map`` — who's where (no image yet)."""
    t = base.fmt.t
    payload = _home_common(header, board, base, clock)
    view: MapView | None = board.map
    assert view is not None
    payload["map"] = {
        "title": t("dashboard.map.list"),
        "label": view.label,
        "attribution": attribution,
        "spots": [
            {
                "person": s.person.user_id,
                "name": s.person.display_name,
                "icon": emoji(s.icon),
                "place": s.place,
                "when": s.when,
                "age_label": (
                    t("dashboard.map.last_seen", rel=s.when)
                    if s.stale
                    else f"{t('dashboard.map.updated')} {s.when}"
                ),
                "iso": s.iso,
                "stale": s.stale,
                "zone": None if s.zone is None else s.zone.id,
                "lat": s.latitude,
                "lon": s.longitude,
            }
            for s in view.spots
        ],
        "bubbles": [
            {
                "key": b.key,
                "lat": b.latitude,
                "lon": b.longitude,
                "zone": None if b.zone is None else b.zone.id,
                "members": [s.person.user_id for s in b.spots],
                "more": b.more,
                "caption": b.caption,
                "label": b.label,
                "stale": b.stale,
                "age": b.age,
                "away": b.away,
            }
            for b in view.bubbles
        ],
        "pins": [
            {
                "id": p.zone.id,
                "name": p.zone.name,
                "icon": emoji(p.icon),
                "lat": p.zone.latitude,
                "lon": p.zone.longitude,
                "radius_m": p.zone.radius_m,
                "color": _color(p.zone.color),
                "is_home": p.zone.is_home,
                "occupied": p.occupied,
            }
            for p in view.pins
        ],
    }
    return payload


# ─── A person ────────────────────────────────────────────────────────────


def _reader(board: PersonBoard, f: Fmt) -> Json:
    t, page = f.t, board.page
    now_item: Json | None = None
    if page.now_item is not None:
        it = page.now_item
        now_item = {
            "label": t("dashboard.person.now"),
            "icon": emoji(f.icon(it)),
            "title": f.title(it),
            "meta": f.lesson_meta(it) or None,
            "until": f.until(it.end),
            "color": _color(f.item_color(it)),
        }
    next_item: Json | None = None
    if page.next_item is not None:
        it = page.next_item
        next_item = {
            "label": t("dashboard.person.next"),
            "icon": emoji(f.icon(it)),
            "title": f.title(it),
            "meta": f.lesson_meta(it) or None,
            "time": f.hhmm(it.start),
            "rel": f.rel(it.start),
            "color": _color(f.item_color(it)),
        }
    school: Json | None = None
    if board.school_lessons:
        school = {
            "label": t("dashboard.person.school_today"),
            "rows": [
                _agenda_row(ls, f, with_people=False) for ls in board.school_lessons
            ],
        }
    day: Json | None = None
    if board.today_list or board.tomorrow_list or not board.school_lessons:
        empty = not board.today_list and not board.school_lessons
        tomorrow: Json | None = None
        if board.tomorrow_list:
            tomorrow = {
                "label": t("dashboard.person.tomorrow"),
                "rows": [_tomorrow_row(i, f) for i in board.tomorrow_list],
            }
        day = {
            "label": t("dashboard.person.today"),
            "rows": [_agenda_row(i, f, with_people=False) for i in board.today_list],
            "empty": t("dashboard.empty_day") if empty else None,
            "empty_icon": emoji(EMPTY_DAY_ICON) if empty else None,
            "tomorrow": tomorrow,
        }
    chores: Json | None = None
    if SECTION_TASKS in page.sections and page.chores:
        chores = {
            "label": t("dashboard.person.chores"),
            "items": [
                _chore(c, f, show_people=False, show_due=True) for c in page.chores
            ],
        }
    return {
        "now": now_item,
        "next": next_item,
        "school": school,
        "day": day,
        "chores": chores,
    }


def _tomorrow_row(item: DashboardAgendaItem, f: Fmt) -> Json:
    if isinstance(item, DashboardLesson):
        return {
            "kind": "school",
            "is_break": False,
            "time": f.hhmm(item.start),
            "title": f.t("dashboard.person.first_lesson"),
            "icon": emoji(SCHOOL_ICON),
            "status": None,
            "status_label": None,
            "now": False,
            "past": False,
            "meta": None,
            "color": _color(item.color_hex),
            "people": [],
        }
    return _agenda_row(item, f, with_people=False)


def _tile(tile: PictureTile, f: Fmt) -> Json:
    return {
        "icon": emoji(tile.icon),
        "label": tile.label,
        "caption": tile.caption or None,
        "color": _cls_color(f, tile.color),
        "past": tile.past,
        "current": tile.current,
        "cancelled": tile.cancelled,
    }


def _wedge(w: Wedge | None) -> Json | None:
    return None if w is None else {"minutes": w.minutes, "full": w.full}


def _job(job: PictureJob) -> Json:
    return {
        "icon": emoji(job.icon),
        "label": job.label,
        "caption": job.caption or None,
        "done": job.done,
    }


def _picture(board: PersonBoard, pv: PictureView, f: Fmt) -> Json:
    t, page = f.t, board.page
    hour, minute = hand_angles(page.now)
    wedge = _wedge(pv.clock.wedge)
    if wedge is not None:
        wedge["color"] = _cls_color(f, pv.clock.color)
    night: Json | None = None
    if pv.night is not None:
        night = {
            "when": pv.night.when,
            "moon": emoji(
                NIGHT_TOMORROW_ICON
                if pv.night.when == "tomorrow"
                else NIGHT_MORNING_ICON
            ),
            "icon": emoji(pv.night.icon),
            "label": pv.night.label,
            "caption": pv.night.caption or None,
        }
    where: Json | None = None
    if pv.home or pv.away:
        where = {
            "label": t("dashboard.home.whos_home"),
            "home": (
                {
                    "icon": emoji(HOME_ICON),
                    "label": t("dashboard.presence.home"),
                    "people": _ids(pv.home),
                }
                if pv.home
                else None
            ),
            "away": (
                {
                    "icon": emoji(AWAY_ICON),
                    "label": t("dashboard.presence.away"),
                    "people": _ids(pv.away),
                }
                if pv.away
                else None
            ),
        }
    return {
        "clock": {
            "hour_deg": hour,
            "minute_deg": minute,
            "label": pv.clock.label,
            "wedge": wedge,
        },
        "part_now": {
            "key": pv.part_now.key,
            "icon": emoji(pv.part_now.icon),
            "label": t(f"dashboard.picture.part.{pv.part_now.key}"),
        },
        "now_next_label": t("dashboard.picture.now_next"),
        "now_label": t("dashboard.person.now"),
        "next_label": t("dashboard.person.next"),
        "now": None if pv.now is None else _tile(pv.now, f),
        "next": None if pv.next is None else _tile(pv.next, f),
        "next_pie": _wedge(pv.next_pie),
        "day": {
            "label": t("dashboard.picture.day"),
            "empty": pv.empty,
            "empty_label": t("dashboard.empty_day") if pv.empty else None,
            "empty_icon": emoji(EMPTY_DAY_ICON) if pv.empty else None,
            "parts": [
                {
                    "key": part.key,
                    "icon": emoji(part.icon),
                    "label": part.label,
                    "state": part.state,
                    "tiles": [_tile(x, f) for x in part.tiles],
                }
                for part in pv.parts
            ],
        },
        "tomorrow": (
            {
                "label": t("dashboard.person.tomorrow"),
                "sleeps": pv.sleeps,
                "sleeps_label": t("dashboard.picture.sleeps", n=pv.sleeps),
                "tiles": [_tile(x, f) for x in pv.tomorrow],
            }
            if pv.tomorrow
            else None
        ),
        "jobs": (
            {
                "label": t("dashboard.picture.jobs"),
                "items": [_job(j) for j in pv.jobs],
            }
            if pv.jobs
            else None
        ),
        "where": where,
        "messages": [_message(m, t) for m in board.messages],
        "night": night,
    }


def person_payload(
    header: Json, board: PersonBoard, base: BoardBase, clock: Json
) -> Json:
    """``GET /api/display/v1/person/{id}`` — the reader page, or the
    picture view for someone who can't read yet."""
    f, t, page = base.fmt, base.fmt.t, board.page
    p = page.person
    presence_on = SECTION_PRESENCE in page.sections and p.presence != "unknown"
    header["person"] = {
        "id": p.user_id,
        "name": p.display_name,
        "initials": p.initials,
        "color": _color(p.color),
        "ink": _ink(p.color),
        "avatar": avatar_path(p),
        "picture_hash": p.picture_hash,
        "status_emoji": emoji(p.status_emoji),
        "status_text": p.status_text or None,
        "where": _where(p, t) if presence_on else None,
    }
    for_you = t("dashboard.mentions.for_you")
    header["mentions"] = [_mention(m, for_you, t) for m in board.my_mentions]
    if board.picture is not None:
        header["kind"] = "picture"
        header["picture"] = _picture(board, board.picture, f)
        header["reader"] = None
    else:
        header["kind"] = "reader"
        header["picture"] = None
        header["reader"] = _reader(board, f)
    if base.night and board.picture is None:
        header["night"] = _night(base, clock, with_people=False)
    return header


# ─── Lock + theme ────────────────────────────────────────────────────────


def lock_payload(*, household: str, locale: str, clock: Json, t: T) -> Json:
    """What a privacy-locked panel gets (``423``): the household name, the
    clock and the PIN strings — never a person (the lock screen's rule)."""
    return {
        "api_version": API_VERSION,
        "locked": True,
        "household": household,
        "locale": locale,
        "dir": text_direction(locale),
        "clock": clock,
        "strings": {
            "locked": t("dashboard.lock.locked"),
            "pin_button": t("dashboard.lock.pin_button"),
            "enter_pin": t("dashboard.lock.enter_pin"),
            "unlock": t("dashboard.lock.unlock"),
            "delete": t("dashboard.lock.delete"),
            "wrong_pin": t("dashboard.lock.wrong_pin"),
        },
    }


#: The static UI strings a panel needs, by the name the firmware uses →
#: the dashboard catalog key. Placeholders stay in braces (``{time}``) for
#: the panel to fill.
STRING_KEYS: Final[Mapping[str, str]] = {
    "tab_overview": "dashboard.tabs.overview",
    "tab_messages": "dashboard.tabs.messages",
    "tab_notes": "dashboard.tabs.notes",
    "tab_map": "dashboard.tabs.map",
    "tabs_label": "dashboard.tabs.label",
    "nav_home": "dashboard.nav.home",
    "nav_label": "dashboard.nav.label",
    "lock": "dashboard.lock.lock",
    "locked": "dashboard.lock.locked",
    "pin_button": "dashboard.lock.pin_button",
    "enter_pin": "dashboard.lock.enter_pin",
    "unlock": "dashboard.lock.unlock",
    "delete": "dashboard.lock.delete",
    "wrong_pin": "dashboard.lock.wrong_pin",
    "got_it": "dashboard.mentions.got_it",
    "message_for_everyone": "dashboard.mentions.everyone",
    "message_for_you": "dashboard.mentions.for_you",
    "night_wake": "dashboard.night.wake",
    "night_today": "dashboard.night.today",
    "night_tomorrow": "dashboard.night.tomorrow",
    "offline": "dashboard.offline",
    "retry": "dashboard.offline.retry",
    "see_all": "dashboard.home.see_all",
    "new": "dashboard.messages.new",
    "read_aloud": "dashboard.messages.read_aloud",
    "hear_rest": "dashboard.messages.hear_rest",
    "timeline_now": "dashboard.timeline.now",
    "empty_day": "dashboard.empty_day",
    "map_title": "dashboard.map.list",
    "map_show_all": "dashboard.map.show_all",
    "not_found": "dashboard.not_found",
    "back_home": "dashboard.not_found.back",
    "pair_title": "dashboard.display.pair_title",
    "pair_hint": "dashboard.display.pair_hint",
    "pair_expired": "dashboard.display.pair_expired",
    "unpaired": "dashboard.display.unpaired",
    "connecting": "dashboard.display.connecting",
}
#: Placeholders a string keeps for the panel to fill.
_PLACEHOLDERS: Final = {"dashboard.offline": {"time": "{time}"}}

#: The ``--dash-*`` colour tokens a panel needs.
_TOKENS: Final = (
    "bg",
    "surface",
    "text",
    "muted",
    "accent",
    "accent-text",
    "border",
    "error-bg",
    "error-text",
    "focus",
)
_DECL = re.compile(r"--dash-([a-z-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;")
#: The three palettes in ``dashboard.css``: ``:root`` (light), the
#: ``prefers-color-scheme: dark`` override, and ``.dash-night``.
CSS_PALETTES: Final = ("light", "dark", "night")


def css_palettes(css: str) -> dict[str, dict[str, str]]:
    """The light, dark and night ``--dash-*`` colours from
    ``dashboard.css`` (the one place they are defined): the first
    ``:root`` block, the first ``prefers-color-scheme: dark`` block and
    the first ``.dash-night`` block. A token a block leaves out keeps the
    light value (dark), resp. the dark one (night)."""

    def block(start: str) -> dict[str, str]:
        at = css.find(start)
        if at < 0:
            return {}
        end = css.find("}", at)
        return {
            name.replace("-", "_"): value.lower()
            for name, value in _DECL.findall(css[at:end])
            if name in _TOKENS
        }

    light = block(":root {")
    dark = {**light, **block("@media (prefers-color-scheme: dark)")}
    night = {**dark, **block(".dash-night {")}
    return {"light": light, "dark": dark, "night": night}


def theme_payload(
    *,
    css: str,
    theme: HouseholdTheme | None,
    locale: str,
    t: T,
) -> Json:
    """``GET /api/display/v1/theme`` — colours, font, locale and strings.

    The household theme colour becomes the accent per scheme exactly as
    on the wall (:func:`~.presenter.theme_css`): only where it reads as
    text on that scheme's surfaces."""
    palettes = css_palettes(css)
    primary = hex_color(theme.primary_color) if theme is not None else None
    if primary is not None:
        for name, surfaces in (("light", LIGHT_SURFACES), ("dark", DARK_SURFACES)):
            if readable_on(primary, surfaces):
                palettes[name]["accent"] = primary
                palettes[name]["accent_text"] = ink_on(primary)
    strings = {
        name: t(key, **_PLACEHOLDERS.get(key, {})) for name, key in STRING_KEYS.items()
    }
    return {
        "api_version": API_VERSION,
        "locale": locale,
        "dir": text_direction(locale),
        "font": {
            # The dashboard draws with the system UI font; the panel maps
            # this to the font it bundles.
            "family": "system-ui",
            "household": theme.font_family if theme is not None else None,
        },
        "mode": theme.mode if theme is not None else "auto",
        "household_colors": {
            "primary": primary,
            "accent": hex_color(theme.accent_color) if theme is not None else None,
        },
        "colors": palettes,
        "strings": strings,
    }
