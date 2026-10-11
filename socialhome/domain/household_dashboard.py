"""Household dashboard — settings shape, limits and coded refusals.

The household dashboard is a wall-tablet view served on its own port and
unlocked with a household-wide dashboard password. These types carry the
admin-editable settings; persistence and validation live in
:mod:`socialhome.services.dashboard_settings_service`.

The second half of the module is the dashboard's *read model*: the
"household-safe" snapshot types :class:`HomeSnapshot` /
:class:`PersonSnapshot` that
:class:`~socialhome.services.household_dashboard_service.HouseholdDashboardService`
assembles and the server-rendered views display, plus the small pure
helpers they share (picture-view default, night window, timetable colour
tokens). Every text field in a snapshot is plain text — escaping is the
template's job — and every datetime is tz-aware in the household zone.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, ClassVar, Final

from .errors import CodedError
from .timetable import EntryKind

if TYPE_CHECKING:
    from .timetable import Timetable
    from .user import User

__all__ = [
    "DEFAULT_DASHBOARD_PORT",
    "IDLE_RETURN_CHOICES",
    "MAX_ADDRESS_LENGTH",
    "MAX_PIN_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "MIN_PIN_LENGTH",
    "PERSON_PALETTE",
    "SECTION_CALENDAR",
    "SECTION_PRESENCE",
    "SECTION_SHOPPING",
    "SECTION_STICKIES",
    "SECTION_TASKS",
    "SECTION_TIMETABLE",
    "SETUP_TOKEN_TTL_SECONDS",
    "SUPPORTED_DASHBOARD_LOCALES",
    "TIMETABLE_COLOR_HEX",
    "DashboardAgendaItem",
    "DashboardEvent",
    "DashboardLesson",
    "DashboardNotRunningError",
    "DashboardPasswordRequiredError",
    "DashboardPasswordTooShortError",
    "DashboardPerson",
    "DashboardPersonSettings",
    "DashboardPinInvalidError",
    "DashboardPortFixedError",
    "DashboardPortInUseError",
    "DashboardSchoolDay",
    "DashboardSettings",
    "DashboardSettingsInvalidError",
    "DashboardShoppingItem",
    "DashboardSticky",
    "DashboardTask",
    "HomeSnapshot",
    "PersonSnapshot",
    "hash_color",
    "in_night_window",
    "initials",
    "is_pure_emoji",
    "leading_emoji",
    "lesson_color_token",
    "normalize_token_key",
    "picture_effective",
    "subject_color_tokens",
    "token_hex",
]

#: Port the dashboard listens on unless an admin picks another one (and
#: the only port in the Home Assistant App, whose host mapping HA
#: Supervisor owns).
DEFAULT_DASHBOARD_PORT = 8100

#: Minutes of inactivity before the tablet returns to the overview
#: (``0`` = never).
IDLE_RETURN_CHOICES = frozenset({0, 1, 3, 10})

SUPPORTED_DASHBOARD_LOCALES = frozenset({"en", "de", "es", "fr", "nl"})

MIN_PASSWORD_LENGTH = 8

#: The privacy-lock PIN is this many ASCII digits (inclusive).
MIN_PIN_LENGTH = 4
MAX_PIN_LENGTH = 6

#: Longest admin-set dashboard address (``DashboardSettings.address``).
MAX_ADDRESS_LENGTH = 200

#: Lifetime of a one-time tablet setup token (QR / link pairing).
SETUP_TOKEN_TTL_SECONDS = 600


@dataclass(slots=True, frozen=True)
class DashboardPersonSettings:
    """Per-person dashboard display choices.

    ``picture=None`` means "use the default" — resolved by the caller
    (pictures are on by default for child-protected minors), not here.
    """

    show: bool = True
    picture: bool | None = None


_DEFAULT_PERSON = DashboardPersonSettings()


@dataclass(slots=True, frozen=True)
class DashboardSettings:
    """Admin-editable household dashboard settings.

    ``people`` is a tuple of ``(user_id, settings)`` pairs so the whole
    value stays frozen and hashable; use :meth:`person` for a lookup.
    """

    enabled: bool = False
    port: int = DEFAULT_DASHBOARD_PORT
    locale: str = "en"
    idle_return_minutes: int = 3
    night_start: str = "22:00"
    night_end: str = "06:00"
    keep_screen_on: bool = True
    show_status_text: bool = False
    #: Admin-set base URL tablets use (``http(s)://host[:port]``), shown
    #: in the admin UI and setup links instead of a guessed address.
    address: str | None = None
    people: tuple[tuple[str, DashboardPersonSettings], ...] = ()
    password_set_at: str | None = None
    #: A privacy-lock PIN exists (tablets show the lock button and a
    #: locked tablet asks for it). Never the PIN or its hash.
    pin_set: bool = False

    def person(self, user_id: str) -> DashboardPersonSettings:
        """The stored settings for ``user_id``, or the defaults."""
        for uid, settings in self.people:
            if uid == user_id:
                return settings
        return _DEFAULT_PERSON

    def people_map(self) -> dict[str, DashboardPersonSettings]:
        """A fresh ``{user_id: settings}`` dict of the stored entries."""
        return dict(self.people)


class DashboardPasswordRequiredError(CodedError):
    """The dashboard can't be enabled (or paired) without a password."""

    status = 422
    code = "DASHBOARD_PASSWORD_REQUIRED"
    detail = "Set a dashboard password before enabling the dashboard."


class DashboardPortFixedError(CodedError):
    """The platform owns the dashboard port; it can't be changed here."""

    status = 422
    code = "DASHBOARD_PORT_FIXED"
    detail = "The dashboard port is fixed on this installation."


class DashboardPortInUseError(CodedError):
    """Another process already listens on the dashboard port."""

    status = 409
    code = "DASHBOARD_PORT_IN_USE"
    detail = "The dashboard port is already in use."

    def __init__(self, port: int) -> None:
        super().__init__(params={"port": port})


class DashboardNotRunningError(CodedError):
    """A tablet sign-in link only works while the dashboard listens."""

    status = 409
    code = "DASHBOARD_NOT_RUNNING"
    detail = "Turn the dashboard on before creating a sign-in link."


class DashboardSettingsInvalidError(CodedError, ValueError):
    """A dashboard setting failed validation. ``params.field`` names the
    setting — never the submitted value."""

    status = 422
    code = "DASHBOARD_SETTINGS_INVALID"
    detail = "A dashboard setting is invalid."

    def __init__(self, field: str) -> None:
        super().__init__(
            f"Invalid dashboard setting: {field}",
            params={"field": field},
        )


class DashboardPasswordTooShortError(CodedError, ValueError):
    """The dashboard password is shorter than :data:`MIN_PASSWORD_LENGTH`."""

    status = 422
    code = "DASHBOARD_PASSWORD_TOO_SHORT"
    detail = "The dashboard password is too short."

    def __init__(self) -> None:
        super().__init__(params={"min": MIN_PASSWORD_LENGTH})


# ─── Read model: helpers ─────────────────────────────────────────────────


def picture_effective(stored: DashboardPersonSettings, user: User) -> bool:
    """Whether the dashboard shows ``user``'s pages in picture view.

    The one rule for the "default" (``stored.picture is None``): picture
    view is on for child-protected minors. An explicit admin choice
    always wins. Shared by the admin API and the snapshot service so the
    two can never disagree.
    """
    if stored.picture is not None:
        return stored.picture
    return bool(user.is_minor and user.child_protection_enabled)


#: Timetable colour token → hex, for the server-rendered dashboard (no
#: theme CSS variables there). Mirrors the light-theme
#: ``--sh-tt-<token>-edge`` values in ``client/src/styles/app.css``; the
#: tokens themselves are ``client/src/utils/tokenColor.ts`` TOKEN_COLORS,
#: in that order (which :func:`hash_color` and :data:`PERSON_PALETTE`
#: rely on).
TIMETABLE_COLOR_HEX: Final[Mapping[str, str]] = {
    "terracotta": "#C8562E",
    "amber": "#AD780C",
    "olive": "#847D22",
    "moss": "#3F7D57",
    "teal": "#26877F",
    "sky": "#357BBB",
    "indigo": "#5462C4",
    "violet": "#8757B8",
    "rose": "#C44D72",
    "slate": "#5C6D7D",
    "sand": "#96783F",
    "coral": "#CF5439",
}

_TOKEN_ORDER: Final[tuple[str, ...]] = tuple(TIMETABLE_COLOR_HEX)

#: Fallback person colours (by position in the household) for a member
#: without a personal calendar to borrow a colour from.
PERSON_PALETTE: Final[tuple[str, ...]] = tuple(TIMETABLE_COLOR_HEX.values())

#: Section names in :attr:`HomeSnapshot.sections` — the household
#: ``feat_*`` toggle names, plus ``shopping`` (which has no toggle).
SECTION_CALENDAR: Final = "calendar"
SECTION_TASKS: Final = "tasks"
SECTION_STICKIES: Final = "stickies"
SECTION_PRESENCE: Final = "presence"
SECTION_TIMETABLE: Final = "timetable"
SECTION_SHOPPING: Final = "shopping"


def token_hex(token: str | None) -> str | None:
    """The hex colour of a timetable colour token (``None`` if unknown)."""
    if token is None:
        return None
    return TIMETABLE_COLOR_HEX.get(token)


def normalize_token_key(name: str) -> str:
    """Lowercase, trimmed, diacritics stripped, inner spaces collapsed —
    the SPA's ``normalizeTokenKey`` ("Música" ≡ "musica")."""
    decomposed = unicodedata.normalize("NFD", name)
    stripped = "".join(
        ch for ch in decomposed if not unicodedata.category(ch).startswith("M")
    )
    return " ".join(stripped.lower().split())


def _utf16_units(text: str) -> tuple[int, ...]:
    """``text`` as UTF-16 code units — what JS ``charCodeAt`` walks and
    JS string comparison orders by."""
    raw = text.encode("utf-16-be")
    return tuple(int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2))


def hash_color(name: str) -> str:
    """FNV-1a over the normalised name → one palette token; the SPA's
    ``hashColor`` (``client/src/utils/tokenColor.ts``)."""
    h = 0x811C9DC5
    for unit in _utf16_units(normalize_token_key(name)):
        h ^= unit
        h = (h * 0x01000193) & 0xFFFFFFFF
    return _TOKEN_ORDER[h % len(_TOKEN_ORDER)]


def subject_color_tokens(tt: Timetable) -> dict[str, str]:
    """Automatic colours for ``tt``'s uncoloured subjects, keyed by the
    normalised title — the SPA's ``subjectColors``
    (``client/src/features/timetable/colors.ts``): the distinct titles,
    sorted in code-unit order, take the palette tokens no subject uses
    explicitly, in palette order; titles beyond the palette fall back to
    :func:`hash_color`."""
    explicit: set[str] = set()
    titles: set[str] = set()
    for entry in tt.entries:
        if entry.kind is not EntryKind.LESSON:
            continue
        key = normalize_token_key(entry.title or "")
        if entry.color:
            explicit.add(entry.color)
        elif key:
            titles.add(key)
    free = [c for c in _TOKEN_ORDER if c not in explicit]
    out: dict[str, str] = {}
    for i, key in enumerate(sorted(titles, key=_utf16_units)):
        out[key] = free[i] if i < len(free) else hash_color(key)
    return out


def lesson_color_token(
    color: str | None, title: str | None, subjects: Mapping[str, str]
) -> str | None:
    """The token a lesson renders with — its own, else its subject's
    automatic one; ``None`` (neutral) for an untitled slot. The SPA's
    ``entryColor``."""
    if color:
        return color
    key = normalize_token_key(title or "")
    if not key:
        return None
    return subjects.get(key) or hash_color(key)


def initials(display_name: str) -> str:
    """Up to two initials for an avatar placeholder (``"?"`` if blank)."""
    words = display_name.split()
    if not words:
        return "?"
    return "".join(w[0] for w in words[:2]).upper()


def leading_emoji(text: str) -> str | None:
    """The emoji a title starts with ("🏊 Swimming" → "🏊"), with its
    modifiers (variation selectors, skin tones, ZWJ parts, flags);
    ``None`` when the title doesn't start with one."""
    if not text or unicodedata.category(text[0]) != "So":
        return None
    end = 1
    if len(text) > 1 and _is_flag_half(text[0]) and _is_flag_half(text[1]):
        end = 2  # a flag is two regional-indicator letters
    while end < len(text):
        ch = text[end]
        modifier = ch in _JOINERS or 0x1F3FB <= ord(ch) <= 0x1F3FF
        # A zero-width joiner joins another emoji (👨‍👩‍👧), never letters —
        # "🏥‍K‍l‍i‍n‍i‍k" must not pass as one emoji.
        joined = text[end - 1] == _ZWJ and unicodedata.category(ch) == "So"
        if not (modifier or joined):
            break
        end += 1
    return text[:end]


def is_pure_emoji(text: str) -> bool:
    """Is ``text`` exactly one emoji (with its modifiers) and nothing else?"""
    return bool(text) and leading_emoji(text) == text


_ZWJ = "\u200d"
#: Zero-width joiner + variation selector 16 (emoji presentation).
_JOINERS = frozenset({_ZWJ, "\ufe0f"})


def _is_flag_half(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _parse_hhmm(value: str) -> time | None:
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except ValueError:
        return None


def in_night_window(now: time, start: str, end: str) -> bool:
    """Whether the wall-clock ``now`` falls in the ``start``..``end``
    night window (``"HH:MM"``; start inclusive, end exclusive). The
    window may wrap midnight (``22:00``–``06:00``); an empty window
    (start == end) or an unparsable bound is never night."""
    lo, hi = _parse_hhmm(start), _parse_hhmm(end)
    if lo is None or hi is None or lo == hi:
        return False
    if lo < hi:
        return lo <= now < hi
    return now >= lo or now < hi


# ─── Read model: snapshot types ──────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class DashboardPerson:
    """One shown household member, household-safe.

    ``presence`` is ``"home"`` / ``"away"`` / ``"zone"`` (then
    ``zone_name`` names it) / ``"unknown"`` — never coordinates.
    ``picture_hash`` is for an avatar URL the view builds (``None`` = use
    ``initials`` on ``color``). ``status_text`` is ``None`` unless the
    admin turned on ``show_status_text``.
    """

    user_id: str
    display_name: str
    initials: str
    color: str  # hex
    picture_hash: str | None
    picture_view: bool
    presence: str = "unknown"
    zone_name: str | None = None
    status_emoji: str | None = None
    status_text: str | None = None


@dataclass(slots=True, frozen=True)
class DashboardEvent:
    """A calendar event (or one occurrence of a recurring one).

    A household event shared with several members (one row per member's
    calendar) appears once, with every shown member in ``person_ids``
    (calendar owners and attendees; hidden people removed).
    ``start`` / ``end`` are in the household zone.

    ``id`` is for de-duplication only: an event known solely from a hidden
    member's copy gets a hashed ``event-…`` id that matches no stored row,
    so views must never link to or look up an event by ``id``.
    """

    item_type: ClassVar[str] = "event"

    id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool
    person_ids: tuple[str, ...]
    color: str | None  # hex, the calendar's colour
    location: str | None = None


@dataclass(slots=True, frozen=True)
class DashboardLesson:
    """One slot of a person's timetable on one day.

    ``kind`` is ``"lesson"`` / ``"break"``; ``status`` is ``"normal"`` /
    ``"changed"`` / ``"cancelled"`` / ``"added"``. ``color`` is the
    timetable colour token (``None`` = neutral), ``color_hex`` its hex.
    ``start`` / ``end`` are in the household zone.
    """

    item_type: ClassVar[str] = "lesson"

    person_id: str
    start: datetime
    end: datetime
    kind: str
    status: str
    title: str | None = None
    label: str | None = None
    icon: str | None = None
    room: str | None = None
    teacher: str | None = None
    note: str | None = None
    color: str | None = None
    color_hex: str | None = None

    @property
    def takes_place(self) -> bool:
        """A real lesson that happens (not a break, not cancelled)."""
        return self.kind == EntryKind.LESSON and self.status != "cancelled"


#: Anything on a day's agenda: a calendar event or a timetable slot
#: (tell them apart by ``item_type``).
DashboardAgendaItem = DashboardEvent | DashboardLesson


@dataclass(slots=True, frozen=True)
class DashboardSchoolDay:
    """A person's school day (every timetable in effect, merged).

    ``first_start`` / ``last_end`` span the lessons that take place
    (``None`` when all are cancelled); ``changes`` are the slots whose
    status isn't ``"normal"``.

    ``date`` is the *household* day. Each plan's lessons come from the
    plan's own local day (consistent with
    ``TimetableService.today_for_user``), with times shown in household
    time — so when a plan's zone differs from the household's, the
    lessons may belong to the plan's next or previous day.
    """

    person_id: str
    date: date
    timetable_name: str
    lessons: tuple[DashboardLesson, ...]
    first_start: datetime | None
    last_end: datetime | None
    changes: tuple[DashboardLesson, ...] = ()


@dataclass(slots=True, frozen=True)
class DashboardTask:
    """A chore: a household task with a due date.

    ``overdue_days`` is ``0`` unless an open task's due date is before
    today. ``done`` tasks are only those completed today.
    """

    id: str
    title: str
    due: date | None
    overdue_days: int
    done: bool
    person_ids: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class DashboardShoppingItem:
    """An open item on the household shopping list."""

    id: str
    text: str
    store: str | None = None


@dataclass(slots=True, frozen=True)
class DashboardSticky:
    """A household sticky note (``color`` is ``#RRGGBB``)."""

    id: str
    content: str
    color: str


@dataclass(slots=True, frozen=True)
class HomeSnapshot:
    """Everything the dashboard's household overview renders.

    ``sections`` names the enabled sections (``SECTION_*``); a slice whose
    section is off is empty. ``next_up`` is the soonest timed event or
    lesson still to start today. ``tomorrow_first`` is the earliest timed
    event or first lesson tomorrow — for night mode.
    """

    now: datetime
    today: date
    household_name: str
    sections: frozenset[str]
    is_night: bool
    people: tuple[DashboardPerson, ...] = ()
    events_today: tuple[DashboardEvent, ...] = ()
    next_up: DashboardAgendaItem | None = None
    chores_today: tuple[DashboardTask, ...] = ()
    shopping: tuple[DashboardShoppingItem, ...] = ()
    shopping_total: int = 0
    stickies: tuple[DashboardSticky, ...] = ()
    school: tuple[DashboardSchoolDay, ...] = ()
    tomorrow_first: DashboardAgendaItem | None = None


@dataclass(slots=True, frozen=True)
class PersonSnapshot:
    """One person's dashboard page.

    ``today_items`` merges the person's events and timetable slots in time
    order (all-day events first); ``now_item`` / ``next_item`` are the
    timed event or lesson running now / starting next today.
    ``tomorrow_items`` holds tomorrow's first few events and first few
    lessons that take place (capped apart, then merged in time order).
    ``chores`` are tasks assigned to the person: open ones due within a
    week (overdue included) and those completed today. ``people`` is
    every shown member (``person`` among them), in household order — the
    dashboard's rail.
    """

    person: DashboardPerson
    now: datetime
    today: date
    sections: frozenset[str]
    is_night: bool
    now_item: DashboardAgendaItem | None = None
    next_item: DashboardAgendaItem | None = None
    today_items: tuple[DashboardAgendaItem, ...] = ()
    tomorrow_items: tuple[DashboardAgendaItem, ...] = ()
    chores: tuple[DashboardTask, ...] = ()
    school: DashboardSchoolDay | None = None
    school_tomorrow_first: DashboardLesson | None = None
    people: tuple[DashboardPerson, ...] = ()


class DashboardPinInvalidError(CodedError, ValueError):
    """The privacy-lock PIN is not 4–6 ASCII digits."""

    status = 422
    code = "DASHBOARD_PIN_INVALID"
    detail = "The dashboard PIN must be 4 to 6 digits."

    def __init__(self) -> None:
        super().__init__(params={"min": MIN_PIN_LENGTH, "max": MAX_PIN_LENGTH})
