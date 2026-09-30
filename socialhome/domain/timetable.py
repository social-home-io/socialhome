"""Timetable domain types (school *Stundenplan*).

A :class:`Timetable` is a named weekly plan covering a set of weekdays
(``days``, 0=Mon … 6=Sun, same numbering as :meth:`datetime.date.weekday`).
Each weekday carries its own list of :class:`TimetableEntry` rows with free
start/end times, so Monday can start with a 07:15 music lesson while the
other days start at 08:00. Per-date changes (a *Vertretungsplan*) are
:class:`TimetableOverride` rows: cancel a lesson, replace some of its
fields, or add an extra slot.

``week_start`` (Monday or Sunday) decides what "a week" is for this
timetable — excluded holiday weeks are keyed by their :func:`week_anchor`.

Everything here is pure: the mutators return a new, already-validated
:class:`Timetable` with ``version + 1`` so the service layer can
compare-and-swap on the version, and :func:`to_wire_dict` /
:func:`from_wire_dict` define the one JSON shape shared by the REST body,
the repository's JSON columns and (later) federation.

The identifier is always ``timetable`` — ``schedule`` belongs to the
Doodle-like schedule-poll feature.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Final

import orjson

from ..utils.timezones import is_valid_tz, local_instant


class EntryKind(StrEnum):
    LESSON = "lesson"
    BREAK = "break"


class OverrideKind(StrEnum):
    CANCEL = "cancel"
    REPLACE = "replace"
    ADD = "add"


class LessonStatus(StrEnum):
    NORMAL = "normal"
    CANCELLED = "cancelled"
    CHANGED = "changed"
    ADDED = "added"


WEEK_START_MONDAY: Final = 0
WEEK_START_SUNDAY: Final = 6
_WEEK_STARTS: Final = frozenset({WEEK_START_MONDAY, WEEK_START_SUNDAY})

#: Theme colour tokens — the SPA maps them onto the palette, so a timetable
#: never carries a raw hex value that clashes with light/dark mode.
TIMETABLE_COLORS: Final[frozenset[str]] = frozenset(
    {
        "terracotta",
        "amber",
        "olive",
        "moss",
        "teal",
        "sky",
        "indigo",
        "violet",
        "rose",
        "slate",
        "sand",
        "coral",
    }
)

MAX_ENTRIES_PER_DAY = 24
#: Lessons per timetable in :func:`today_timetable` (the home screen).
MAX_TODAY_LESSONS = 24
MAX_ENTRIES = 200
MAX_OVERRIDES = 200
MAX_EXCLUDED_WEEKS = 110
MAX_ASSIGNEES = 20
MIN_ENTRY_MINUTES = 5
MAX_NAME = 60
MAX_TITLE = 60
MAX_ROOM = 30
MAX_TEACHER = 60
MAX_NOTE = 200
MAX_LABEL = 8
MAX_ICON = 16  # code points — room for ZWJ sequences and tag flags
MAX_ID = 64  # timetable / entry / override ids and user ids
MAX_TZ = 64
#: Fits SQLite's INTEGER and orjson; stops a peer from pinning LWW forever
#: with an absurd version.
MAX_VERSION = 2**31 - 1
MAX_WIRE_BYTES = 128 * 1024
OVERRIDE_RETENTION_DAYS = 14
WIRE_SCHEMA = 1

_LESSON_MINUTES_RANGE: Final = (5, 240)
_GAP_MINUTES_RANGE: Final = (0, 120)
_LAST_MINUTE: Final = 23 * 60 + 59  # 23:59 — there is no 24:00
_HHMM_RE: Final = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
#: Every id (timetable / entry / override, and user ids — base32 derived
#: or ``uuid4().hex`` in practice) is URL-path and log safe.
_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: Dates a timetable may mention. Far enough out for any school year, and
#: keeps week arithmetic (anchor − 1 day, anchor + 7) clear of
#: ``date.min`` / ``date.max`` so it can never raise ``OverflowError``.
MIN_DATE: Final = date(1900, 1, 1)
MAX_DATE: Final = date(2199, 12, 31)

#: Optional string fields shared by entries and overrides, with their caps.
_TEXT_LIMITS: Final[tuple[tuple[str, int], ...]] = (
    ("label", MAX_LABEL),
    ("title", MAX_TITLE),
    ("room", MAX_ROOM),
    ("teacher", MAX_TEACHER),
    ("note", MAX_NOTE),
)
_TEXT_FIELDS: Final = tuple(name for name, _ in _TEXT_LIMITS) + ("color", "icon")
#: Fields a ``replace`` override may change on its entry. ``label`` is
#: replaceable too (e.g. a moved lesson renumbered "1." → "0.").
_REPLACEABLE: Final = (
    "icon",
    "label",
    "title",
    "room",
    "teacher",
    "note",
    "color",
    "start",
    "end",
)
#: Fields a ``cancel`` override must leave unset — it carries no payload.
_CANCEL_EMPTY: Final = (*_TEXT_FIELDS, "start", "end")
#: Lesson fields cleared by ``copy_day(with_subjects=False)``.
_SUBJECT_FIELDS: Final = ("title", "room", "teacher", "note", "color", "icon")
#: Non-symbol code points an emoji sequence may still contain: the
#: zero-width joiner (👩‍🔬) and the tag characters of subdivision flags.
_ZWJ: Final = 0x200D
_ICON_JOINERS: Final = frozenset({_ZWJ, *range(0xE0020, 0xE0080)})
_REGIONAL_INDICATORS: Final = range(0x1F1E6, 0x1F200)
_SKIN_TONES: Final = range(0x1F3FB, 0x1F400)
#: Keycaps (1️⃣ #️⃣ *⃣) are the one emoji built on an ASCII base.
_KEYCAP_RE: Final = re.compile("[0-9#*]\ufe0f?\u20e3")


# ─── Exceptions ──────────────────────────────────────────────────────────


class TimetableValidationError(ValueError):
    """The timetable (or an input to a mutator) breaks an invariant (422)."""


class TimetableConflictError(Exception):
    """The compare-and-swap version check failed."""

    def __init__(self, current_version: int) -> None:
        super().__init__(f"timetable changed concurrently (now v{current_version})")
        self.current_version = current_version


class TimetableOrphanError(Exception):
    """The operation would silently drop entries; the caller must confirm.

    ``code`` is ``"DAY_HAS_ENTRIES"`` (generate/copy onto a non-empty day)
    or ``"DAYS_ORPHAN_ENTRIES"`` (removing weekdays that still hold
    entries); ``count`` is how many entries would go.
    """

    def __init__(self, code: str, count: int) -> None:
        super().__init__(f"{code}: {count} entries would be removed")
        self.code = code
        self.count = count


class TimetableLimitError(Exception):
    """Too many timetables."""


# ─── Value types ─────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class TimetableDefaults:
    """Defaults the UI uses to generate a day (not applied retroactively)."""

    lesson_minutes: int = 45
    gap_minutes: int = 5
    day_start: time = time(8, 0)


@dataclass(slots=True, frozen=True)
class TimetableEntry:
    """One recurring slot on a weekday — a lesson or a break.

    An untitled lesson is an empty slot the UI "brush" fills in later.
    """

    id: str
    weekday: int
    start: time
    end: time
    kind: EntryKind = EntryKind.LESSON
    label: str | None = None  # e.g. "1.", "0."
    title: str | None = None
    room: str | None = None
    teacher: str | None = None
    note: str | None = None
    color: str | None = None
    icon: str | None = None  # an emoji, for children who can't read yet


@dataclass(slots=True, frozen=True)
class TimetableOverride:
    """A per-date change: cancel / replace an entry, or add an extra slot."""

    id: str
    date: date
    kind: OverrideKind
    entry_id: str | None = None  # cancel / replace
    start: time | None = None  # replace (optional) / add (required)
    end: time | None = None
    entry_kind: EntryKind = EntryKind.LESSON  # add only
    label: str | None = None
    title: str | None = None
    room: str | None = None
    teacher: str | None = None
    note: str | None = None
    color: str | None = None
    icon: str | None = None


@dataclass(slots=True, frozen=True)
class TimetableValidity:
    """Inclusive date range plus excluded (holiday) weeks, keyed by anchor."""

    valid_from: date | None = None
    valid_until: date | None = None
    excluded_weeks: tuple[date, ...] = ()

    def is_valid_on(self, d: date, week_start: int) -> bool:
        if self.valid_from is not None and d < self.valid_from:
            return False
        if self.valid_until is not None and d > self.valid_until:
            return False
        return week_anchor(d, week_start) not in self.excluded_weeks


@dataclass(slots=True, frozen=True)
class Timetable:
    id: str
    name: str
    created_by: str  # user_id
    created_at: datetime
    updated_at: datetime
    week_start: int = WEEK_START_MONDAY
    tz: str = "UTC"
    color: str | None = None
    days: tuple[int, ...] = (0, 1, 2, 3, 4)
    defaults: TimetableDefaults = field(default_factory=TimetableDefaults)
    entries: tuple[TimetableEntry, ...] = ()
    overrides: tuple[TimetableOverride, ...] = ()
    validity: TimetableValidity = field(default_factory=TimetableValidity)
    assignees: tuple[str, ...] = ()  # user_ids; always () for space timetables
    version: int = 1
    updated_by: str | None = None


@dataclass(slots=True, frozen=True)
class EffectiveLesson:
    """A slot as it actually happens on one date, overrides applied."""

    source_id: str  # entry id, or the override id for ADDED
    date: date
    start: time
    end: time
    kind: EntryKind
    label: str | None
    title: str | None
    room: str | None
    teacher: str | None
    note: str | None
    color: str | None
    status: LessonStatus
    override_id: str | None = None
    original: TimetableEntry | None = None  # set for CHANGED / CANCELLED
    icon: str | None = None


@dataclass(slots=True, frozen=True)
class ResolvedDay:
    date: date
    valid: bool
    lessons: tuple[EffectiveLesson, ...]


@dataclass(slots=True, frozen=True)
class ResolvedWeek:
    anchor: date
    valid: bool
    days: tuple[ResolvedDay, ...]


@dataclass(slots=True, frozen=True)
class TodayLesson:
    """An :class:`EffectiveLesson` placed on the absolute time line.

    ``start_at`` / ``end_at`` are tz-aware UTC instants of the lesson's
    wall-clock times in the timetable's zone (see :func:`~socialhome.utils.timezones.local_instant`),
    so the home screen can merge lessons with calendar events.
    """

    lesson: EffectiveLesson
    start_at: datetime
    end_at: datetime


@dataclass(slots=True, frozen=True)
class TodayTimetable:
    """One timetable's lessons for one local date (the home-screen card)."""

    timetable_id: str
    name: str
    color: str | None
    tz: str
    date: date  # the local date in ``tz``
    lessons: tuple[TodayLesson, ...]


# ─── Small helpers ───────────────────────────────────────────────────────


def week_anchor(d: date, week_start: int) -> date:
    """First day of the week containing ``d`` (a Monday or a Sunday)."""
    _check_date(d, "date")
    if week_start == WEEK_START_MONDAY:
        return d - timedelta(days=d.weekday())
    if week_start == WEEK_START_SUNDAY:
        return d - timedelta(days=(d.weekday() + 1) % 7)
    raise TimetableValidationError(f"week_start must be 0 or 6, got {_r(week_start)}")


def normalize_name(s: str) -> str:
    """Strip ``s``; it must be 1..:data:`MAX_NAME` characters."""
    if not isinstance(s, str):
        raise TimetableValidationError("name must be a string")
    name = s.strip()
    if not name:
        raise TimetableValidationError("name must not be empty")
    if len(name) > MAX_NAME:
        raise TimetableValidationError(f"name exceeds {MAX_NAME} characters")
    return name


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _r(value: object) -> str:
    """``repr`` for error messages, truncated so hostile input can't bloat them."""
    return repr(value)[:40]


def _check_id(value: object, what: str) -> None:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise TimetableValidationError(
            f"{what} must be 1..{MAX_ID} characters of A-Z a-z 0-9 _ -"
        )


def _check_date(d: date, what: str) -> None:
    if not MIN_DATE <= d <= MAX_DATE:
        raise TimetableValidationError(
            f"{what} must be between {MIN_DATE.year} and {MAX_DATE.year}"
        )


def _from_minutes(m: int) -> time:
    return time(m // 60, m % 60)


def _is_hhmm(t: time) -> bool:
    return t.second == 0 and t.microsecond == 0 and t.tzinfo is None


def _clean_text(value: str | None) -> str | None:
    """Mutator-side normalisation: strip, and empty → ``None``."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _clean_fields[T: (TimetableEntry, TimetableOverride)](obj: T) -> T:
    changes = {
        name: _clean_text(getattr(obj, name)) for name in _TEXT_FIELDS if name != "icon"
    }
    # An icon is not stripped (whitespace is invalid, not decoration);
    # only "" means "no icon".
    changes["icon"] = obj.icon or None
    return copy.replace(obj, **changes)


def _check_color(value: str | None, where: str) -> None:
    if value is not None and value not in TIMETABLE_COLORS:
        raise TimetableValidationError(f"{where}: unknown color {_r(value)}")


def _pictographs(value: str) -> int:
    """Count visible pictographs, grapheme-ish.

    A symbol starts a new one unless it follows a ZWJ (👩‍🔬 is one);
    two regional indicators make one flag (🇬🇧); skin tones, variation
    selectors, keycap marks and tag characters never start one.
    """
    count = 0
    prev = 0
    open_flag = False
    for c in value:
        o = ord(c)
        if o in _REGIONAL_INDICATORS:
            if open_flag:
                open_flag = False  # second half of the flag
            else:
                count += prev != _ZWJ
                open_flag = True
        else:
            open_flag = False
            if unicodedata.category(c)[0] == "S" and o not in _SKIN_TONES:
                count += prev != _ZWJ
        prev = o
    return count


def _check_icon(value: object, where: str) -> None:
    """``None`` or exactly one emoji of at most :data:`MAX_ICON` code points.

    A keycap (1️⃣ #️⃣ *⃣) is matched explicitly. Anything else must start
    with a symbol (``S*``, regional indicators included), consist only of
    non-ASCII symbols, combining marks (``M*`` — variation selectors,
    keycap marks) and ZWJ / tag joiners, and draw a single pictograph.
    That admits 🔢 ✏️ 👩‍🔬 👍🏽 🇬🇧 but refuses words in any script, markup,
    whitespace, invisible controls, bare joiners and ★★.
    """
    if value is None:
        return
    if isinstance(value, str) and _KEYCAP_RE.fullmatch(value):
        return
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_ICON
        or unicodedata.category(value[0])[0] != "S"
        or not all(
            ord(c) > 0x7F
            and (ord(c) in _ICON_JOINERS or unicodedata.category(c)[0] in ("S", "M"))
            for c in value
        )
        or _pictographs(value) != 1
    ):
        raise TimetableValidationError(f"{where}: icon must be a single emoji")


def _check_texts(obj: TimetableEntry | TimetableOverride, where: str) -> None:
    for name, limit in _TEXT_LIMITS:
        value = getattr(obj, name)
        if value is not None and len(value) > limit:
            raise TimetableValidationError(
                f"{where}: {name} exceeds {limit} characters"
            )
    _check_color(obj.color, where)
    _check_icon(obj.icon, where)


def _check_order(start: time, end: time, where: str) -> None:
    if not (_is_hhmm(start) and _is_hhmm(end)):
        raise TimetableValidationError(f"{where}: times must be whole HH:MM")
    if start >= end:
        raise TimetableValidationError(f"{where}: start must be before end")


def _check_interval(start: time, end: time, where: str) -> None:
    """A base slot (entry or ``add``): ordered and ≥ MIN_ENTRY_MINUTES long."""
    _check_order(start, end, where)
    if _minutes(end) - _minutes(start) < MIN_ENTRY_MINUTES:
        raise TimetableValidationError(
            f"{where}: must last at least {MIN_ENTRY_MINUTES} minutes"
        )


def _check_no_overlap(
    intervals: Iterable[tuple[time, time]],
    where: str,
) -> None:
    """Half-open ``[start, end)`` intervals — touching is fine."""
    ordered = sorted(intervals)
    for (_, prev_end), (next_start, _) in zip(ordered, ordered[1:]):
        if next_start < prev_end:
            raise TimetableValidationError(f"{where}: slots overlap")


# ─── validate ────────────────────────────────────────────────────────────


def validate(tt: Timetable) -> None:
    """Raise :class:`TimetableValidationError` unless ``tt`` is consistent."""
    _validate_header(tt)
    entries_by_id = _validate_entries(tt)
    _validate_overrides(tt, entries_by_id)
    _validate_validity(tt)
    try:
        wire = orjson.dumps(to_wire_dict(tt))
    except TypeError as exc:  # orjson.JSONEncodeError: e.g. a lone surrogate
        raise TimetableValidationError("text must be valid UTF-8") from exc
    if len(wire) > MAX_WIRE_BYTES:
        raise TimetableValidationError(
            f"timetable exceeds {MAX_WIRE_BYTES} bytes on the wire"
        )


def _validate_header(tt: Timetable) -> None:
    _check_id(tt.id, "id")
    _check_id(tt.created_by, "created_by")
    if tt.updated_by is not None:
        _check_id(tt.updated_by, "updated_by")
    if normalize_name(tt.name) != tt.name:
        raise TimetableValidationError("name must be trimmed")
    if tt.week_start not in _WEEK_STARTS:
        raise TimetableValidationError("week_start must be 0 or 6")
    # Length cap first: the zone lookup maps the name onto a file path.
    if not isinstance(tt.tz, str) or len(tt.tz) > MAX_TZ or not is_valid_tz(tt.tz):
        raise TimetableValidationError(f"unknown tz {_r(tt.tz)}")
    _check_color(tt.color, "timetable")
    if not 1 <= tt.version <= MAX_VERSION:
        raise TimetableValidationError(f"version must be 1..{MAX_VERSION}")
    days = tt.days
    if (
        not days
        or any(d not in range(7) for d in days)
        or list(days) != sorted(set(days))
    ):
        raise TimetableValidationError(
            "days must be a non-empty, sorted, unique subset of 0..6"
        )
    dft = tt.defaults
    lo, hi = _LESSON_MINUTES_RANGE
    if not lo <= dft.lesson_minutes <= hi:
        raise TimetableValidationError(f"defaults: lesson_minutes must be {lo}..{hi}")
    lo, hi = _GAP_MINUTES_RANGE
    if not lo <= dft.gap_minutes <= hi:
        raise TimetableValidationError(f"defaults: gap_minutes must be {lo}..{hi}")
    if not _is_hhmm(dft.day_start):
        raise TimetableValidationError("defaults: day_start must be whole HH:MM")
    if len(tt.assignees) > MAX_ASSIGNEES:
        raise TimetableValidationError(f"at most {MAX_ASSIGNEES} assignees")
    if len(set(tt.assignees)) != len(tt.assignees):
        raise TimetableValidationError("assignees must be unique")
    for a in tt.assignees:
        _check_id(a, "assignee")


def _validate_entries(tt: Timetable) -> dict[str, TimetableEntry]:
    if len(tt.entries) > MAX_ENTRIES:
        raise TimetableValidationError(f"at most {MAX_ENTRIES} entries")
    by_id: dict[str, TimetableEntry] = {}
    per_day: dict[int, list[tuple[time, time]]] = {}
    for e in tt.entries:
        _check_id(e.id, "entry id")
        where = f"entry {e.id!r}"
        if e.id in by_id:
            raise TimetableValidationError(f"duplicate entry id {e.id!r}")
        by_id[e.id] = e
        if e.weekday not in tt.days:
            raise TimetableValidationError(f"{where}: weekday not in days")
        _check_interval(e.start, e.end, where)
        _check_texts(e, where)
        per_day.setdefault(e.weekday, []).append((e.start, e.end))
    for weekday, intervals in per_day.items():
        if len(intervals) > MAX_ENTRIES_PER_DAY:
            raise TimetableValidationError(
                f"weekday {weekday}: at most {MAX_ENTRIES_PER_DAY} entries per day"
            )
        _check_no_overlap(intervals, f"weekday {weekday}")
    return by_id


def _validate_overrides(
    tt: Timetable,
    entries_by_id: Mapping[str, TimetableEntry],
) -> None:
    if len(tt.overrides) > MAX_OVERRIDES:
        raise TimetableValidationError(f"at most {MAX_OVERRIDES} overrides")
    seen_ids: set[str] = set()
    seen_targets: set[tuple[date, str]] = set()
    dates: set[date] = set()
    for ov in tt.overrides:
        _check_id(ov.id, "override id")
        where = f"override {ov.id!r}"
        if ov.id in seen_ids:
            raise TimetableValidationError(f"duplicate override id {ov.id!r}")
        seen_ids.add(ov.id)
        _check_date(ov.date, f"{where}: date")
        if ov.date.weekday() not in tt.days:
            raise TimetableValidationError(f"{where}: date's weekday not in days")
        _check_texts(ov, where)
        if ov.kind is OverrideKind.ADD:
            if ov.entry_id is not None:
                raise TimetableValidationError(f"{where}: add takes no entry_id")
            if ov.start is None or ov.end is None:
                raise TimetableValidationError(f"{where}: add needs start and end")
            _check_interval(ov.start, ov.end, where)
        else:
            if ov.entry_id is None:
                raise TimetableValidationError(f"{where}: {ov.kind} needs entry_id")
            _check_id(ov.entry_id, f"{where}: entry_id")
            if ov.entry_kind is not EntryKind.LESSON:
                raise TimetableValidationError(
                    f"{where}: entry_kind only applies to add ({ov.kind})"
                )
            if ov.kind is OverrideKind.CANCEL and any(
                getattr(ov, name) is not None for name in _CANCEL_EMPTY
            ):
                raise TimetableValidationError(
                    f"{where}: cancel carries no fields besides entry_id"
                )
            entry = entries_by_id.get(ov.entry_id)
            if entry is None:
                raise TimetableValidationError(f"{where}: unknown entry")
            if entry.weekday != ov.date.weekday():
                raise TimetableValidationError(
                    f"{where}: date's weekday does not match the entry's weekday"
                )
            target = (ov.date, ov.entry_id)
            if target in seen_targets:
                raise TimetableValidationError(
                    f"{where}: at most one cancel/replace per date and entry"
                )
            seen_targets.add(target)
            if ov.kind is OverrideKind.REPLACE:
                start = ov.start if ov.start is not None else entry.start
                end = ov.end if ov.end is not None else entry.end
                _check_order(start, end, where)
        dates.add(ov.date)
    for d in sorted(dates):
        live = [
            (ls.start, ls.end)
            for ls in _effective_lessons(tt, d)
            if ls.status is not LessonStatus.CANCELLED
        ]
        _check_no_overlap(live, f"{d.isoformat()} after overrides")


def _validate_validity(tt: Timetable) -> None:
    v = tt.validity
    for d in (v.valid_from, v.valid_until, *v.excluded_weeks):
        if d is not None:
            _check_date(d, "validity date")
    if (
        v.valid_from is not None
        and v.valid_until is not None
        and v.valid_from > v.valid_until
    ):
        raise TimetableValidationError("valid_from must not be after valid_until")
    weeks = v.excluded_weeks
    if len(weeks) > MAX_EXCLUDED_WEEKS:
        raise TimetableValidationError(f"at most {MAX_EXCLUDED_WEEKS} excluded weeks")
    if list(weeks) != sorted(set(weeks)):
        raise TimetableValidationError("excluded_weeks must be sorted and unique")
    for w in weeks:
        if week_anchor(w, tt.week_start) != w:
            raise TimetableValidationError(
                f"excluded week {w.isoformat()} is not a week anchor"
            )


# ─── resolve ─────────────────────────────────────────────────────────────


def _lesson_from_entry(
    e: TimetableEntry,
    d: date,
    status: LessonStatus,
    *,
    override_id: str | None = None,
    original: TimetableEntry | None = None,
) -> EffectiveLesson:
    return EffectiveLesson(
        source_id=e.id,
        date=d,
        start=e.start,
        end=e.end,
        kind=e.kind,
        label=e.label,
        title=e.title,
        room=e.room,
        teacher=e.teacher,
        note=e.note,
        color=e.color,
        status=status,
        override_id=override_id,
        original=original,
        icon=e.icon,
    )


def _effective_lessons(tt: Timetable, d: date) -> tuple[EffectiveLesson, ...]:
    """The day's slots with overrides applied, ignoring validity."""
    todays = [ov for ov in tt.overrides if ov.date == d]
    by_entry: dict[str, TimetableOverride] = {}
    for ov in todays:
        if ov.kind is not OverrideKind.ADD and ov.entry_id is not None:
            by_entry.setdefault(ov.entry_id, ov)
    out: list[EffectiveLesson] = []
    for e in tt.entries:
        if e.weekday != d.weekday():
            continue
        hit = by_entry.get(e.id)
        if hit is None:
            out.append(_lesson_from_entry(e, d, LessonStatus.NORMAL))
        elif hit.kind is OverrideKind.CANCEL:
            out.append(
                _lesson_from_entry(
                    e, d, LessonStatus.CANCELLED, override_id=hit.id, original=e
                )
            )
        else:
            merged = copy.replace(
                e,
                **{
                    name: getattr(hit, name)
                    for name in _REPLACEABLE
                    if getattr(hit, name) is not None
                },
            )
            out.append(
                _lesson_from_entry(
                    merged, d, LessonStatus.CHANGED, override_id=hit.id, original=e
                )
            )
    for ov in todays:
        if ov.kind is not OverrideKind.ADD:
            continue
        if ov.start is None or ov.end is None:
            # validate() rejects this; resolve_day also runs on unvalidated
            # input (and mypy needs the narrowing), so skip rather than crash.
            continue
        out.append(
            EffectiveLesson(
                source_id=ov.id,
                date=d,
                start=ov.start,
                end=ov.end,
                kind=ov.entry_kind,
                label=ov.label,
                title=ov.title,
                room=ov.room,
                teacher=ov.teacher,
                note=ov.note,
                color=ov.color,
                status=LessonStatus.ADDED,
                override_id=ov.id,
                icon=ov.icon,
            )
        )
    out.sort(key=lambda ls: (ls.start, ls.end))
    return tuple(out)


def resolve_day(tt: Timetable, d: date) -> ResolvedDay:
    _check_date(d, "date")
    valid = tt.validity.is_valid_on(d, tt.week_start) and d.weekday() in tt.days
    if not valid:
        return ResolvedDay(date=d, valid=False, lessons=())
    return ResolvedDay(date=d, valid=True, lessons=_effective_lessons(tt, d))


def resolve_week(tt: Timetable, any_date: date) -> ResolvedWeek:
    """The timetable's days of the week containing ``any_date``.

    ``valid`` is true when at least one of those days is in effect — an
    excluded week, or one wholly outside the validity range, is invalid.
    """
    anchor = week_anchor(any_date, tt.week_start)
    days = tuple(
        resolve_day(tt, anchor + timedelta(days=i))
        for i in range(7)
        if (anchor + timedelta(days=i)).weekday() in tt.days
    )
    return ResolvedWeek(
        anchor=anchor,
        valid=any(day.valid for day in days),
        days=days,
    )


def is_filled(ls: EffectiveLesson) -> bool:
    """A break, or a lesson with a title or an icon — not one of the
    template's untitled slots the UI brush fills in later. Mirrors the
    home card's ``isShown`` (``client/src/features/welcome/schedule.ts``)."""
    return ls.kind is EntryKind.BREAK or bool((ls.title or "").strip() or ls.icon)


def has_lessons(today: TodayTimetable) -> bool:
    """At least one filled lesson (breaks alone don't make a school day)."""
    return any(ls.lesson.kind is EntryKind.LESSON for ls in today.lessons)


def today_timetable(
    tt: Timetable, d: date, *, max_lessons: int = MAX_TODAY_LESSONS
) -> TodayTimetable | None:
    """``tt``'s lessons on ``d`` with UTC instants, or ``None`` when the
    timetable isn't in effect that day. Cancelled lessons are kept (the
    card strikes them through); untitled slots are dropped (see
    :func:`is_filled`) before at most ``max_lessons`` are taken."""
    day = resolve_day(tt, d)
    if not day.valid:
        return None
    lessons = tuple(
        TodayLesson(
            lesson=ls,
            start_at=local_instant(d, ls.start, tt.tz),
            end_at=local_instant(d, ls.end, tt.tz),
        )
        for ls in [ls for ls in day.lessons if is_filled(ls)][:max_lessons]
    )
    return TodayTimetable(
        timetable_id=tt.id,
        name=tt.name,
        color=tt.color,
        tz=tt.tz,
        date=d,
        lessons=lessons,
    )


def prune_overrides(tt: Timetable, today: date) -> Timetable:
    """Drop overrides older than :data:`OVERRIDE_RETENTION_DAYS`.

    Housekeeping rather than an edit, so the version is left alone; the
    same instance comes back when nothing is old enough to drop.
    """
    cutoff = today - timedelta(days=OVERRIDE_RETENTION_DAYS)
    kept = tuple(ov for ov in tt.overrides if ov.date >= cutoff)
    if len(kept) == len(tt.overrides):
        return tt
    return copy.replace(tt, overrides=kept)


# ─── Mutators ────────────────────────────────────────────────────────────


class _Unset:
    """Sentinel type for "leave this header field unchanged"."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = _Unset()
#: Public name of the sentinel's type, for callers' type hints.
Unset = _Unset


def _commit(
    tt: Timetable, *, now: datetime, by: str | None, **changes: Any
) -> Timetable:
    out = copy.replace(
        tt,
        version=tt.version + 1,
        updated_at=now,
        updated_by=by,
        **changes,
    )
    validate(out)
    return out


def _live_overrides(
    overrides: Iterable[TimetableOverride],
    entries: Sequence[TimetableEntry],
) -> tuple[TimetableOverride, ...]:
    """Drop cancel/replace overrides whose entry is gone or changed weekday."""
    weekday_of = {e.id: e.weekday for e in entries}
    return tuple(
        ov
        for ov in overrides
        if ov.kind is OverrideKind.ADD
        or (ov.entry_id in weekday_of and weekday_of[ov.entry_id] == ov.date.weekday())
    )


def _with_entry_set(
    tt: Timetable,
    entries: Sequence[TimetableEntry],
    *,
    now: datetime,
    by: str | None,
) -> Timetable:
    entries = tuple(entries)
    return _commit(
        tt,
        now=now,
        by=by,
        entries=entries,
        overrides=_live_overrides(tt.overrides, entries),
    )


def with_header(
    tt: Timetable,
    *,
    now: datetime,
    by: str | None = None,
    name: str | _Unset = UNSET,
    color: str | None | _Unset = UNSET,
    week_start: int | _Unset = UNSET,
    tz: str | _Unset = UNSET,
    days: Iterable[int] | _Unset = UNSET,
    defaults: TimetableDefaults | _Unset = UNSET,
    assignees: Iterable[str] | _Unset = UNSET,
    drop_orphans: bool = False,
) -> Timetable:
    changes: dict[str, Any] = {}
    if not isinstance(name, _Unset):
        changes["name"] = normalize_name(name)
    if not isinstance(color, _Unset):
        changes["color"] = _clean_text(color)
    if not isinstance(tz, _Unset):
        changes["tz"] = tz
    if not isinstance(defaults, _Unset):
        changes["defaults"] = defaults
    if not isinstance(assignees, _Unset):
        changes["assignees"] = tuple(dict.fromkeys(assignees))
    if not isinstance(week_start, _Unset) and week_start != tt.week_start:
        if week_start not in _WEEK_STARTS:
            raise TimetableValidationError("week_start must be 0 or 6")
        shift = -1 if week_start == WEEK_START_SUNDAY else 1
        changes["week_start"] = week_start
        changes["validity"] = copy.replace(
            tt.validity,
            excluded_weeks=tuple(
                w + timedelta(days=shift) for w in tt.validity.excluded_weeks
            ),
        )
    if not isinstance(days, _Unset):
        new_days = tuple(sorted(set(days)))
        removed = set(tt.days) - set(new_days)
        orphans = [e for e in tt.entries if e.weekday in removed]
        if orphans and not drop_orphans:
            raise TimetableOrphanError("DAYS_ORPHAN_ENTRIES", len(orphans))
        changes["days"] = new_days
        if removed:
            changes["entries"] = tuple(
                e for e in tt.entries if e.weekday not in removed
            )
            changes["overrides"] = tuple(
                ov for ov in tt.overrides if ov.date.weekday() not in removed
            )
    return _commit(tt, now=now, by=by, **changes)


def with_entry(
    tt: Timetable,
    entry: TimetableEntry,
    *,
    now: datetime,
    by: str | None = None,
) -> Timetable:
    """Add ``entry``, or replace the entry with the same id."""
    entry = _clean_fields(entry)
    entries = [e for e in tt.entries if e.id != entry.id]
    entries.append(entry)
    return _with_entry_set(tt, entries, now=now, by=by)


def without_entry(
    tt: Timetable,
    entry_id: str,
    *,
    now: datetime,
    by: str | None = None,
) -> Timetable:
    """Remove an entry and every override that references it."""
    if not any(e.id == entry_id for e in tt.entries):
        raise TimetableValidationError(f"unknown entry {_r(entry_id)}")
    entries = [e for e in tt.entries if e.id != entry_id]
    return _with_entry_set(tt, entries, now=now, by=by)


def with_entries(
    tt: Timetable,
    entries: Iterable[TimetableEntry],
    *,
    now: datetime,
    by: str | None = None,
) -> Timetable:
    """Replace every entry; overrides of vanished entries are dropped."""
    return _with_entry_set(tt, [_clean_fields(e) for e in entries], now=now, by=by)


def generate_day(
    tt: Timetable,
    weekday: int,
    slots: Sequence[TimetableEntry],
    *,
    replace: bool = False,
    now: datetime,
    by: str | None = None,
) -> Timetable:
    """Set ``weekday``'s entries to ``slots``.

    A day that already has entries raises :class:`TimetableOrphanError`
    (``DAY_HAS_ENTRIES``) unless ``replace`` confirms dropping them.
    """
    if any(s.weekday != weekday for s in slots):
        raise TimetableValidationError("every slot must be on the generated weekday")
    existing = sum(1 for e in tt.entries if e.weekday == weekday)
    if existing and not replace:
        raise TimetableOrphanError("DAY_HAS_ENTRIES", existing)
    entries = [e for e in tt.entries if e.weekday != weekday]
    entries.extend(_clean_fields(s) for s in slots)
    return _with_entry_set(tt, entries, now=now, by=by)


def copy_day(
    tt: Timetable,
    from_weekday: int,
    to_weekdays: Iterable[int],
    *,
    with_subjects: bool = True,
    replace: bool = False,
    id_factory: Callable[[], str],
    now: datetime,
    by: str | None = None,
) -> Timetable:
    """Copy ``from_weekday``'s entries onto each target weekday (new ids).

    ``with_subjects=False`` copies only the time grid: lessons lose their
    title/room/teacher/note/color, breaks are copied as-is.
    """
    targets = tuple(dict.fromkeys(to_weekdays))
    if not targets:
        return tt  # nothing to do — no version bump
    if from_weekday not in tt.days:
        raise TimetableValidationError("source weekday not in days")
    for t in targets:
        if t not in tt.days:
            raise TimetableValidationError(f"target weekday {t} not in days")
        if t == from_weekday:
            raise TimetableValidationError("cannot copy a day onto itself")
    occupied = sum(1 for e in tt.entries if e.weekday in targets)
    if occupied and not replace:
        raise TimetableOrphanError("DAY_HAS_ENTRIES", occupied)
    source = [e for e in tt.entries if e.weekday == from_weekday]
    entries = [e for e in tt.entries if e.weekday not in targets]
    for t in targets:
        for e in source:
            clone = copy.replace(e, id=id_factory(), weekday=t)
            if not with_subjects and e.kind is EntryKind.LESSON:
                clone = copy.replace(clone, **dict.fromkeys(_SUBJECT_FIELDS))
            entries.append(clone)
    return _with_entry_set(tt, entries, now=now, by=by)


def shift_after(
    tt: Timetable,
    weekday: int,
    from_time: time,
    minutes: int,
    *,
    now: datetime,
    by: str | None = None,
) -> Timetable:
    """Move every entry on ``weekday`` starting at/after ``from_time``.

    Nothing to move (``minutes == 0`` or no matching entry) returns ``tt``
    unchanged, without a version bump.
    """
    if minutes == 0:
        return tt
    entries: list[TimetableEntry] = []
    moved = False
    for e in tt.entries:
        if e.weekday != weekday or e.start < from_time:
            entries.append(e)
            continue
        moved = True
        start = _minutes(e.start) + minutes
        end = _minutes(e.end) + minutes
        if start < 0 or end > _LAST_MINUTE:
            raise TimetableValidationError(
                f"entry {e.id!r}: shift would cross midnight"
            )
        entries.append(
            copy.replace(e, start=_from_minutes(start), end=_from_minutes(end))
        )
    if not moved:
        return tt
    return _with_entry_set(tt, entries, now=now, by=by)


def with_validity(
    tt: Timetable,
    validity: TimetableValidity,
    *,
    now: datetime,
    by: str | None = None,
) -> Timetable:
    """Set the validity; excluded weeks are snapped to anchors, sorted, deduped."""
    weeks = sorted({week_anchor(w, tt.week_start) for w in validity.excluded_weeks})
    return _commit(
        tt,
        now=now,
        by=by,
        validity=copy.replace(validity, excluded_weeks=tuple(weeks)),
    )


def with_override(
    tt: Timetable,
    ov: TimetableOverride,
    *,
    now: datetime,
    by: str | None = None,
) -> Timetable:
    """Add ``ov``, or replace the override with the same id."""
    ov = _clean_fields(ov)
    overrides = [o for o in tt.overrides if o.id != ov.id]
    overrides.append(ov)
    return _commit(tt, now=now, by=by, overrides=tuple(overrides))


def without_override(
    tt: Timetable,
    override_id: str,
    *,
    now: datetime,
    by: str | None = None,
) -> Timetable:
    if not any(o.id == override_id for o in tt.overrides):
        raise TimetableValidationError(f"unknown override {_r(override_id)}")
    return _commit(
        tt,
        now=now,
        by=by,
        overrides=tuple(o for o in tt.overrides if o.id != override_id),
    )


def clear_week(
    tt: Timetable,
    any_date: date,
    *,
    now: datetime,
    by: str | None = None,
) -> Timetable:
    """Drop every override dated inside the week containing ``any_date``."""
    start = week_anchor(any_date, tt.week_start)
    end = start + timedelta(days=7)
    return _commit(
        tt,
        now=now,
        by=by,
        overrides=tuple(o for o in tt.overrides if not start <= o.date < end),
    )


# ─── Templates ───────────────────────────────────────────────────────────

#: School template: six lessons, a long break after the 2nd and a short
#: one after the 4th (minutes). Breaks replace the gap after their lesson.
_SCHOOL_LESSONS: Final = 6
_SCHOOL_BREAKS: Final[Mapping[int, int]] = {2: 20, 4: 15}
_SCHOOL_BREAK_TITLE: Final = "Pause"


def school_template_day(
    weekday: int,
    defaults: TimetableDefaults,
    id_factory: Callable[[], str],
) -> tuple[TimetableEntry, ...]:
    """A typical school day on ``weekday``, timed from ``defaults``.

    Untitled lessons labelled "1."–"6." (empty slots for the UI brush),
    ``gap_minutes`` apart, with a 20-minute break after lesson 2 and a
    15-minute break after lesson 4. With the stock defaults that is
    08:00–13:20.
    """
    out: list[TimetableEntry] = []
    t = _minutes(defaults.day_start)

    def slot(start: int, end: int, **kw: Any) -> TimetableEntry:
        if end > _LAST_MINUTE:
            raise TimetableValidationError("school template would cross midnight")
        return TimetableEntry(
            id=id_factory(),
            weekday=weekday,
            start=_from_minutes(start),
            end=_from_minutes(end),
            **kw,
        )

    for n in range(1, _SCHOOL_LESSONS + 1):
        end = t + defaults.lesson_minutes
        out.append(slot(t, end, label=f"{n}."))
        pause = _SCHOOL_BREAKS.get(n)
        if pause is None:
            t = end + defaults.gap_minutes
        else:
            out.append(
                slot(end, end + pause, kind=EntryKind.BREAK, title=_SCHOOL_BREAK_TITLE)
            )
            t = end + pause
    return tuple(out)


# ─── Wire format ─────────────────────────────────────────────────────────
#
# One JSON shape for the REST body, the repository's JSON columns and
# federation. The element helpers are public so the repository reuses
# them and the two can't drift.


def _hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _opt_hhmm(t: time | None) -> str | None:
    return None if t is None else _hhmm(t)


def _opt_iso(d: date | None) -> str | None:
    return None if d is None else d.isoformat()


def defaults_to_dict(d: TimetableDefaults) -> dict[str, Any]:
    return {
        "lesson_minutes": d.lesson_minutes,
        "gap_minutes": d.gap_minutes,
        "day_start": _hhmm(d.day_start),
    }


def entry_to_dict(e: TimetableEntry) -> dict[str, Any]:
    return {
        "id": e.id,
        "weekday": e.weekday,
        "start": _hhmm(e.start),
        "end": _hhmm(e.end),
        "kind": e.kind.value,
        "label": e.label,
        "title": e.title,
        "room": e.room,
        "teacher": e.teacher,
        "note": e.note,
        "color": e.color,
        "icon": e.icon,
    }


def override_to_dict(o: TimetableOverride) -> dict[str, Any]:
    return {
        "id": o.id,
        "date": o.date.isoformat(),
        "kind": o.kind.value,
        "entry_id": o.entry_id,
        "start": _opt_hhmm(o.start),
        "end": _opt_hhmm(o.end),
        "entry_kind": o.entry_kind.value,
        "label": o.label,
        "title": o.title,
        "room": o.room,
        "teacher": o.teacher,
        "note": o.note,
        "color": o.color,
        "icon": o.icon,
    }


#: Keys of an entry / override on the wire (and in REST bodies).
ENTRY_FIELDS: Final = frozenset(
    entry_to_dict(TimetableEntry(id="x", weekday=0, start=time(0), end=time(0, 5)))
)
OVERRIDE_FIELDS: Final = frozenset(
    override_to_dict(TimetableOverride(id="x", date=MIN_DATE, kind=OverrideKind.CANCEL))
)


def validity_to_dict(v: TimetableValidity) -> dict[str, Any]:
    return {
        "valid_from": _opt_iso(v.valid_from),
        "valid_until": _opt_iso(v.valid_until),
        "excluded_weeks": [w.isoformat() for w in v.excluded_weeks],
    }


def to_wire_dict(tt: Timetable) -> dict[str, Any]:
    return {
        "schema": WIRE_SCHEMA,
        "id": tt.id,
        "name": tt.name,
        "created_by": tt.created_by,
        "created_at": tt.created_at.isoformat(),
        "updated_at": tt.updated_at.isoformat(),
        "updated_by": tt.updated_by,
        "version": tt.version,
        "week_start": tt.week_start,
        "tz": tt.tz,
        "color": tt.color,
        "days": list(tt.days),
        "defaults": defaults_to_dict(tt.defaults),
        "entries": [entry_to_dict(e) for e in tt.entries],
        "overrides": [override_to_dict(o) for o in tt.overrides],
        "validity": validity_to_dict(tt.validity),
        "assignees": list(tt.assignees),
    }


# Strict parsers: every malformed value surfaces as a
# TimetableValidationError, never a KeyError / TypeError.


def _field(d: Mapping[str, Any], key: str) -> Any:
    """A required field — absent raises instead of ``KeyError``."""
    if key not in d:
        raise TimetableValidationError(f"missing field {key!r}")
    return d[key]


def _as_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TimetableValidationError(f"{what} must be an object")
    return value


def _as_list(value: Any, what: str, *, max_len: int) -> list[Any]:
    """A list, length-capped *before* any element is parsed."""
    if not isinstance(value, list):
        raise TimetableValidationError(f"{what} must be a list")
    if len(value) > max_len:
        raise TimetableValidationError(f"{what}: at most {max_len} items")
    return value


def _as_str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise TimetableValidationError(f"{what} must be a string")
    return value


def _as_opt_str(value: Any, what: str) -> str | None:
    return None if value is None else _as_str(value, what)


def _as_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TimetableValidationError(f"{what} must be an integer")
    return value


def _parse_hhmm(value: Any, what: str) -> time:
    m = _HHMM_RE.match(_as_str(value, what))
    if m is None:
        raise TimetableValidationError(f"{what} must be HH:MM")
    return time(int(m.group(1)), int(m.group(2)))


def _parse_opt_hhmm(value: Any, what: str) -> time | None:
    return None if value is None else _parse_hhmm(value, what)


def _parse_date(value: Any, what: str) -> date:
    try:
        d = date.fromisoformat(_as_str(value, what))
    except ValueError as exc:
        raise TimetableValidationError(f"{what} must be an ISO date") from exc
    _check_date(d, what)
    return d


def _parse_opt_date(value: Any, what: str) -> date | None:
    return None if value is None else _parse_date(value, what)


def _parse_datetime(value: Any, what: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_as_str(value, what))
    except ValueError as exc:
        raise TimetableValidationError(f"{what} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise TimetableValidationError(f"{what} must carry a timezone")
    return parsed


def _parse_enum[E: StrEnum](cls: type[E], value: Any, what: str) -> E:
    try:
        return cls(_as_str(value, what))
    except ValueError as exc:
        raise TimetableValidationError(f"{what}: unknown value {_r(value)}") from exc


def _texts(d: Mapping[str, Any], what: str) -> dict[str, str | None]:
    return {name: _as_opt_str(d.get(name), f"{what}.{name}") for name in _TEXT_FIELDS}


def defaults_from_dict(value: Any) -> TimetableDefaults:
    d = _as_mapping(value, "defaults")
    base = TimetableDefaults()
    return TimetableDefaults(
        lesson_minutes=_as_int(
            d.get("lesson_minutes", base.lesson_minutes), "defaults.lesson_minutes"
        ),
        gap_minutes=_as_int(
            d.get("gap_minutes", base.gap_minutes), "defaults.gap_minutes"
        ),
        day_start=(
            _parse_hhmm(d["day_start"], "defaults.day_start")
            if "day_start" in d
            else base.day_start
        ),
    )


def entry_from_dict(value: Any, *, id: str | None = None) -> TimetableEntry:
    """Parse one entry. A given ``id`` (server-minted) wins over ``d["id"]``."""
    d = _as_mapping(value, "entry")
    return TimetableEntry(
        id=id if id is not None else _as_str(_field(d, "id"), "entry.id"),
        weekday=_as_int(_field(d, "weekday"), "entry.weekday"),
        start=_parse_hhmm(_field(d, "start"), "entry.start"),
        end=_parse_hhmm(_field(d, "end"), "entry.end"),
        kind=_parse_enum(EntryKind, d.get("kind", "lesson"), "entry.kind"),
        **_texts(d, "entry"),
    )


def override_from_dict(value: Any, *, id: str | None = None) -> TimetableOverride:
    """Parse one override. A given ``id`` (server-minted) wins over ``d["id"]``."""
    d = _as_mapping(value, "override")
    return TimetableOverride(
        id=id if id is not None else _as_str(_field(d, "id"), "override.id"),
        date=_parse_date(_field(d, "date"), "override.date"),
        kind=_parse_enum(OverrideKind, _field(d, "kind"), "override.kind"),
        entry_id=_as_opt_str(d.get("entry_id"), "override.entry_id"),
        start=_parse_opt_hhmm(d.get("start"), "override.start"),
        end=_parse_opt_hhmm(d.get("end"), "override.end"),
        entry_kind=_parse_enum(
            EntryKind, d.get("entry_kind", "lesson"), "override.entry_kind"
        ),
        **_texts(d, "override"),
    )


def validity_from_dict(value: Any) -> TimetableValidity:
    d = _as_mapping(value, "validity")
    return TimetableValidity(
        valid_from=_parse_opt_date(d.get("valid_from"), "validity.valid_from"),
        valid_until=_parse_opt_date(d.get("valid_until"), "validity.valid_until"),
        excluded_weeks=tuple(
            _parse_date(w, "validity.excluded_weeks[]")
            for w in _as_list(
                d.get("excluded_weeks", []),
                "validity.excluded_weeks",
                max_len=MAX_EXCLUDED_WEEKS,
            )
        ),
    )


def from_wire_dict(d: Mapping[str, Any]) -> Timetable:
    """Parse :func:`to_wire_dict` output.

    Structural only: a malformed or wrong-schema dict raises
    :class:`TimetableValidationError`, but the invariants are checked by
    :func:`validate` — callers ingesting untrusted input call it next.
    List lengths and the version range are bounded here already, so a
    hostile payload can't make the parser do unbounded work or hand the
    database / orjson an integer they can't hold.
    """
    d = _as_mapping(d, "timetable")
    schema = d.get("schema")
    if type(schema) is not int or schema != WIRE_SCHEMA:
        raise TimetableValidationError(f"unsupported timetable schema {_r(schema)}")
    version = _as_int(_field(d, "version"), "version")
    if not 1 <= version <= MAX_VERSION:
        raise TimetableValidationError(f"version must be 1..{MAX_VERSION}")
    return Timetable(
        id=_as_str(_field(d, "id"), "id"),
        name=_as_str(_field(d, "name"), "name"),
        created_by=_as_str(_field(d, "created_by"), "created_by"),
        created_at=_parse_datetime(_field(d, "created_at"), "created_at"),
        updated_at=_parse_datetime(_field(d, "updated_at"), "updated_at"),
        updated_by=_as_opt_str(d.get("updated_by"), "updated_by"),
        version=version,
        week_start=_as_int(_field(d, "week_start"), "week_start"),
        tz=_as_str(_field(d, "tz"), "tz"),
        color=_as_opt_str(d.get("color"), "color"),
        days=tuple(
            _as_int(x, "days[]") for x in _as_list(_field(d, "days"), "days", max_len=7)
        ),
        defaults=defaults_from_dict(_field(d, "defaults")),
        entries=tuple(
            entry_from_dict(e)
            for e in _as_list(_field(d, "entries"), "entries", max_len=MAX_ENTRIES)
        ),
        overrides=tuple(
            override_from_dict(o)
            for o in _as_list(
                d.get("overrides", []), "overrides", max_len=MAX_OVERRIDES
            )
        ),
        validity=validity_from_dict(_field(d, "validity")),
        assignees=tuple(
            _as_str(a, "assignees[]")
            for a in _as_list(
                d.get("assignees", []), "assignees", max_len=MAX_ASSIGNEES
            )
        ),
    )


# Public wrappers for the route / service layer, so path and query
# strings are parsed with the same rules (and errors) as wire fields.


def parse_date(value: Any, what: str) -> date:
    """An ISO ``YYYY-MM-DD`` string, else :class:`TimetableValidationError`."""
    return _parse_date(value, what)


def parse_hhmm(value: Any, what: str) -> time:
    """A ``"HH:MM"`` string, else :class:`TimetableValidationError`."""
    return _parse_hhmm(value, what)


def parse_int(value: Any, what: str) -> int:
    """A JSON integer (``bool`` refused), else :class:`TimetableValidationError`."""
    return _as_int(value, what)


def parse_weekdays(value: Any, what: str) -> tuple[int, ...]:
    """A non-empty list of weekdays 0..6 → sorted, deduplicated tuple."""
    items = _as_list(value, what, max_len=14)
    days = {_as_int(x, f"{what}[]") for x in items}
    if not days or any(d not in range(7) for d in days):
        raise TimetableValidationError(f"{what} must be a non-empty subset of 0..6")
    return tuple(sorted(days))


def parse_user_ids(value: Any, what: str) -> tuple[str, ...]:
    """A list of user ids (≤ :data:`MAX_ASSIGNEES`) → deduplicated tuple."""
    items = _as_list(value, what, max_len=MAX_ASSIGNEES * 2)
    ids: list[str] = []
    for x in items:
        _check_id(x, f"{what}[]")
        ids.append(x)
    out = tuple(dict.fromkeys(ids))
    if len(out) > MAX_ASSIGNEES:
        raise TimetableValidationError(f"{what}: at most {MAX_ASSIGNEES} items")
    return out


def reject_unknown(d: Any, allowed: Collection[str], what: str) -> None:
    """REST strictness: refuse keys outside ``allowed``.

    The wire parsers (:func:`entry_from_dict`, :func:`from_wire_dict`)
    stay lenient so a newer federation peer's extra fields don't break
    an older receiver; request bodies from our own SPA get no such slack.
    """
    unknown = sorted(k for k in _as_mapping(d, what) if k not in allowed)
    if unknown:
        raise TimetableValidationError(f"{what}: unknown field {_r(unknown[0])}")


def entries_from_list(
    value: Any,
    *,
    id_factory: Callable[[], str],
    weekday: int | None = None,
    allow_id: bool = True,
    keep_ids: Collection[str] | None = None,
    max_len: int = MAX_ENTRIES,
) -> tuple[TimetableEntry, ...]:
    """Parse a request's entry list (length-capped before parsing).

    An element's ``id`` is kept when ``keep_ids`` is ``None`` or contains
    it — so a replace-all keeps the ids, and the overrides, of existing
    entries the client sent back — and minted otherwise. ``allow_id=False``
    refuses an ``id`` key outright (every id is minted). ``weekday``
    forces every element onto that day (generating a day from slots).
    Unknown keys are refused.
    """
    allowed = ENTRY_FIELDS if allow_id else ENTRY_FIELDS - {"id"}
    out: list[TimetableEntry] = []
    for item in _as_list(value, "entries", max_len=max_len):
        reject_unknown(item, allowed, "entry")
        d = dict(item)
        if weekday is not None:
            d["weekday"] = weekday
        given = d.get("id")
        keep = given is not None and (keep_ids is None or given in keep_ids)
        out.append(entry_from_dict(d, id=None if keep else id_factory()))
    return tuple(out)


# ─── Resolved views (read-only REST shapes) ──────────────────────────────


def effective_lesson_to_dict(ls: EffectiveLesson) -> dict[str, Any]:
    return {
        "source_id": ls.source_id,
        "date": ls.date.isoformat(),
        "start": _hhmm(ls.start),
        "end": _hhmm(ls.end),
        "kind": ls.kind.value,
        "label": ls.label,
        "title": ls.title,
        "room": ls.room,
        "teacher": ls.teacher,
        "note": ls.note,
        "color": ls.color,
        "icon": ls.icon,
        "status": ls.status.value,
        "override_id": ls.override_id,
        "original": None if ls.original is None else entry_to_dict(ls.original),
    }


def resolved_day_to_dict(day: ResolvedDay) -> dict[str, Any]:
    return {
        "date": day.date.isoformat(),
        "valid": day.valid,
        "lessons": [effective_lesson_to_dict(ls) for ls in day.lessons],
    }


def today_lesson_to_dict(ls: TodayLesson) -> dict[str, Any]:
    """:func:`effective_lesson_to_dict` plus ISO ``start_at`` / ``end_at``."""
    return {
        **effective_lesson_to_dict(ls.lesson),
        "start_at": ls.start_at.isoformat(),
        "end_at": ls.end_at.isoformat(),
    }


def today_timetable_to_dict(today: TodayTimetable) -> dict[str, Any]:
    return {
        "timetable_id": today.timetable_id,
        "name": today.name,
        "color": today.color,
        "tz": today.tz,
        "date": today.date.isoformat(),
        "lessons": [today_lesson_to_dict(ls) for ls in today.lessons],
    }


def resolved_week_to_dict(week: ResolvedWeek) -> dict[str, Any]:
    return {
        "anchor": week.anchor.isoformat(),
        "valid": week.valid,
        "days": [resolved_day_to_dict(d) for d in week.days],
    }


def timetable_view_dict(tt: Timetable, today: date) -> dict[str, Any]:
    """The REST body: the wire dict plus flags computed for ``today``.

    ``today`` is the date in the timetable's own tz (the caller resolves
    it — this module does no clock reads).
    """
    return {
        **to_wire_dict(tt),
        "active_this_week": resolve_week(tt, today).valid,
        "valid_today": resolve_day(tt, today).valid,
    }


# ─── Partial updates (PATCH) ─────────────────────────────────────────────


def _patched(
    current: dict[str, Any],
    fields: Any,
    what: str,
) -> dict[str, Any]:
    """``{**current, **fields}`` — unknown keys refused, ``id`` never changed."""
    patch = _as_mapping(fields, f"{what} patch")
    unknown = sorted(k for k in patch if k not in current)
    if unknown:
        raise TimetableValidationError(f"{what}: unknown field {_r(unknown[0])}")
    return {**current, **patch, "id": current["id"]}


def entry_patch(existing: TimetableEntry, fields: Mapping[str, Any]) -> TimetableEntry:
    """Apply a partial wire-shaped update to ``existing`` (``None`` clears)."""
    return entry_from_dict(_patched(entry_to_dict(existing), fields, "entry"))


def override_patch(
    existing: TimetableOverride,
    fields: Mapping[str, Any],
) -> TimetableOverride:
    """Apply a partial wire-shaped update to ``existing`` (``None`` clears)."""
    return override_from_dict(_patched(override_to_dict(existing), fields, "override"))
