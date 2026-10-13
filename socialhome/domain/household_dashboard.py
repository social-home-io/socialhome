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
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, ClassVar, Final

from .errors import CodedError
from .timetable import EntryKind

if TYPE_CHECKING:
    from .timetable import Timetable
    from .user import User

__all__ = [
    "ACK_EMOJI",
    "DEFAULT_DASHBOARD_PORT",
    "CHAT_MAX_AGE",
    "CHAT_SHOWN",
    "HERE_SHOWN",
    "IDLE_RETURN_CHOICES",
    "MENTION_HERE",
    "MENTION_MAX_AGE",
    "MENTION_PERSON",
    "MENTIONS_SCANNED",
    "MESSAGE_KINDS",
    "MESSAGE_MAX_AGE",
    "MESSAGES_SHOWN",
    "MAP_DROP_AFTER",
    "MAP_STALE_AFTER",
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
    "DashboardMention",
    "DashboardMessage",
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
    "DashboardZone",
    "HomeSnapshot",
    "PersonSnapshot",
    "hash_color",
    "here_alerts",
    "in_night_window",
    "initials",
    "is_pure_emoji",
    "leading_emoji",
    "lesson_color_token",
    "mentioned_ids",
    "mentions_for",
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

#: Household-chat messages on the wall (``show_household_messages``): the
#: newest few, and none older than this (wall-clock age).
MESSAGES_SHOWN = 3
MESSAGE_MAX_AGE = timedelta(hours=6)

#: Home's Messages tab lists the household chat of the last day — at most
#: this many, newest first (Home's messages card shows the newest
#: :data:`MESSAGES_SHOWN` of the last :data:`MESSAGE_MAX_AGE` of them).
CHAT_MAX_AGE = timedelta(hours=24)

#: Home's Map tab: a location older than this is "stale" (the marker
#: fades and says how long ago it was seen)…
MAP_STALE_AFTER = timedelta(hours=1)
#: …and one older than this is gone — the person leaves the map, and
#: with nobody left the tab goes too.
MAP_DROP_AFTER = timedelta(hours=24)
CHAT_SHOWN = 30

#: The reaction a "Got it" on the wall writes to a household-chat message
#: — and the one that marks a mention as seen (the SPA picker's ✅).
ACK_EMOJI: Final = "✅"

#: A household-chat ``@here`` / ``@name`` shows on the wall (banner,
#: badge) until acknowledged or this old; at most this many of the
#: newest messages of that hour are scanned for mentions.
MENTION_MAX_AGE = timedelta(hours=1)
MENTIONS_SCANNED = 100

#: ``@here`` banners Home shows at once (the newest) — and so the only
#: ``@here`` mentions a "Got it" can acknowledge.
HERE_SHOWN = 3

#: :attr:`DashboardMention.kind` values: ``@here`` (a banner on Home) or
#: ``@name`` (a badge on that person's face and a card on their page).
MENTION_HERE: Final = "here"
MENTION_PERSON: Final = "person"

#: :attr:`DashboardMessage.kind` values.
MESSAGE_KINDS = frozenset({"text", "photo", "video", "file", "voice"})


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
    #: Home (and picture-view pages) show the last few household-chat
    #: messages. Off by default: anyone at the tablet can read them.
    show_household_messages: bool = False
    #: Play a short on-device tone when a new @here / @name mention of
    #: the household chat appears (part of the messages opt-in above).
    mention_sound: bool = True
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
    ``None`` when the title doesn't start with one.

    Only *pictures* count (:func:`_is_picture`): letterlike and enclosed
    alphanumeric symbols (©, ™, Ⓜ️, 🅰️, 🔟, 🈁 …) show letters or digits,
    which the picture view must not; half a flag is a boxed letter."""
    if not text:
        return None
    if _is_flag_half(text[0]):
        if len(text) < 2 or not _is_flag_half(text[1]):
            return None
        end = 2  # a flag is two regional-indicator letters
    elif _is_picture(text[0]):
        end = 1
    else:
        return None
    while end < len(text):
        ch = text[end]
        modifier = ch in _JOINERS or 0x1F3FB <= ord(ch) <= 0x1F3FF
        # A zero-width joiner joins another picture (👨‍👩‍👧), never letters —
        # "🏥‍K‍l‍i‍n‍i‍k" must not pass as one emoji.
        joined = text[end - 1] == _ZWJ and _is_picture(ch)
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

#: Code point ranges of pictographic emoji (an approximation of Unicode's
#: ``Extended_Pictographic``, which Python's ``unicodedata`` doesn't
#: expose): watches / hourglasses / media keys, misc symbols + dingbats,
#: a few arrows and shapes, mahjong + cards, and the big pictograph
#: blocks. Left out on purpose: letterlike symbols (©, ®, ™), enclosed
#: alphanumerics (Ⓜ️, 🅰️, 🅿️) and enclosed ideographs (🈁, ㊗️).
_PICTURE_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x231A, 0x231B),
    (0x2328, 0x2328),
    (0x23CF, 0x23CF),
    (0x23E9, 0x23F3),
    (0x23F8, 0x23FA),
    (0x25AA, 0x25AB),
    (0x25B6, 0x25B6),
    (0x25C0, 0x25C0),
    (0x25FB, 0x25FE),
    (0x2600, 0x27BF),
    (0x2934, 0x2935),
    (0x2B05, 0x2B07),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x1F000, 0x1F0FF),
    (0x1F300, 0x1FAFF),
)
#: Inside those blocks, pictures of letters and digits: 🔟 (keycap ten),
#: 🔠 🔡 🔤 (Latin letters).
_LETTER_PICTURES: Final = frozenset({0x1F51F, 0x1F520, 0x1F521, 0x1F524})


def _is_picture(ch: str) -> bool:
    cp = ord(ch)
    if cp in _LETTER_PICTURES or unicodedata.category(ch) != "So":
        return False
    return any(lo <= cp <= hi for lo, hi in _PICTURE_RANGES)


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
    """One shown household member.

    ``presence`` is ``"home"`` / ``"away"`` / ``"zone"`` (then
    ``zone_name`` names it) / ``"unknown"``. ``latitude`` / ``longitude``
    are the member's shared GPS point (4 dp, as stored — only Home's Map
    tab draws it, and only for someone outside every household zone) and
    ``located_at`` when their presence last changed (household zone);
    all three are ``None`` without a presence row. ``picture_hash`` is
    for an avatar URL the view builds (``None`` = use ``initials`` on
    ``color``). ``status_text`` is ``None`` unless the admin turned on
    ``show_status_text``.
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
    latitude: float | None = None
    longitude: float | None = None
    located_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class DashboardZone:
    """A household zone as Home's Map tab draws it: the household's own
    place (never federated). ``color`` is the resolved ``#rrggbb``
    (:func:`~socialhome.domain.household_zone.zone_color`, as the app
    paints it); ``icon`` one emoji or ``None``."""

    id: str
    name: str
    latitude: float
    longitude: float
    radius_m: int
    color: str
    icon: str | None = None
    is_home: bool = False


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
class DashboardMessage:
    """A recent household-chat message, as the wall may show it.

    ``kind`` is one of :data:`MESSAGE_KINDS`: ``"text"`` (typed or
    dictated), ``"photo"``, ``"video"``, ``"file"`` or ``"voice"`` (a voice
    note). ``text`` is plain text — the message (its latest edit), or a
    voice note's transcript — and ``""`` for media and for a voice note
    not transcribed yet. ``sent_at`` is in the household zone. Never a
    location, never a reaction, never who has read it.
    """

    id: str
    sender_id: str
    kind: str
    text: str
    sent_at: datetime


@dataclass(slots=True, frozen=True)
class DashboardMention:
    """An unacknowledged household-chat mention the wall shows.

    ``kind`` is :data:`MENTION_HERE` (``person_id`` is ``None``) or
    :data:`MENTION_PERSON` (``person_id`` is the shown member it
    @-mentions). ``text`` is the message's plain text (its latest edit);
    ``sent_at`` is in the household zone. One message may give both an
    ``@here`` and several person mentions.
    """

    message_id: str
    sender_id: str
    text: str
    sent_at: datetime
    kind: str
    person_id: str | None = None


def here_alerts(
    mentions: tuple[DashboardMention, ...],
) -> tuple[DashboardMention, ...]:
    """The ``@here`` mentions among ``mentions`` (order kept)."""
    return tuple(m for m in mentions if m.kind == MENTION_HERE)


def mentions_for(
    mentions: tuple[DashboardMention, ...], person_id: str
) -> tuple[DashboardMention, ...]:
    """``person_id``'s own ``@name`` mentions among ``mentions``."""
    return tuple(
        m for m in mentions if m.kind == MENTION_PERSON and m.person_id == person_id
    )


def mentioned_ids(mentions: tuple[DashboardMention, ...]) -> frozenset[str]:
    """Everyone with an ``@name`` mention waiting — the rail's badges."""
    return frozenset(
        m.person_id
        for m in mentions
        if m.kind == MENTION_PERSON and m.person_id is not None
    )


@dataclass(slots=True, frozen=True)
class HomeSnapshot:
    """Everything the dashboard's household overview renders.

    ``sections`` names the enabled sections (``SECTION_*``); a slice whose
    section is off is empty. ``next_up`` is the soonest timed event or
    lesson still to start today. ``tomorrow_first`` is the earliest timed
    event or first lesson tomorrow — for night mode. ``messages`` are the
    household-chat messages of the last :data:`CHAT_MAX_AGE` (at most
    :data:`CHAT_SHOWN`), newest first — empty unless the admin turned them
    on, and always at night. ``messages_on`` says whether they are on at
    all (the admin's opt-in and the household chat) — Home then has a
    Messages tab, even with nothing in it. ``mentions`` are the
    unacknowledged household-chat ``@here`` / ``@name`` mentions of the
    last :data:`MENTION_MAX_AGE`, newest first — on the same opt-in.
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
    messages: tuple[DashboardMessage, ...] = ()
    mentions: tuple[DashboardMention, ...] = ()
    messages_on: bool = False
    #: Every household sticky the wall may show (hidden authors left out)
    #: — ``stickies`` holds at most a hard bound of them.
    stickies_total: int = 0
    #: The household zones (Home first), for the Map tab — empty while
    #: the presence section is off.
    zones: tuple[DashboardZone, ...] = ()


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
    dashboard's rail. ``messages`` (as on Home) only for a picture-view
    person — a reader page never shows them. ``mentions`` are all of
    them, as on Home (the rail badges everyone mentioned); the page shows
    only the person's own (:func:`mentions_for`).
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
    messages: tuple[DashboardMessage, ...] = ()
    mentions: tuple[DashboardMention, ...] = ()


class DashboardPinInvalidError(CodedError, ValueError):
    """The privacy-lock PIN is not 4–6 ASCII digits."""

    status = 422
    code = "DASHBOARD_PIN_INVALID"
    detail = "The dashboard PIN must be 4 to 6 digits."

    def __init__(self) -> None:
        super().__init__(params={"min": MIN_PIN_LENGTH, "max": MAX_PIN_LENGTH})
