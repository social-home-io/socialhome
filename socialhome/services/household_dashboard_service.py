"""Household dashboard snapshots — the wall tablet's read model.

:class:`HouseholdDashboardService` assembles the household-safe
:class:`~socialhome.domain.household_dashboard.HomeSnapshot` (the
overview) and :class:`~socialhome.domain.household_dashboard.PersonSnapshot`
(one member's page) that the dashboard's server-rendered views display.

House rules:

* **Read-only, but for one write.** Only plain reads of the owning
  services — nothing that reconciles or creates rows (so never the
  household-chat summary). The one write is :meth:`acknowledge`: a
  "Got it" tapped on a mention, which re-derives from scratch that the
  wall shows that mention before it reacts ✅ to the chat message.
* **Household-safe.** Presence is home / away / a zone name, plus — for
  Home's Map tab — a member's shared 4-dp GPS point, when it last
  changed, and the household's own zones (never federated); calendar
  events come from members' personal calendars;
  chores, the shopping list, today's timetables and household stickies.
  Never DMs, notifications, space chats or polls.
* **Household chat, only when asked.** With the admin's
  ``show_household_messages`` on (and ``feat_household_chat`` on), Home
  carries the household-chat messages of the last :data:`CHAT_MAX_AGE`
  (at most :data:`CHAT_SHOWN`, for its Messages tab) and picture-view
  pages the newest :data:`MESSAGES_SHOWN` of the last
  :data:`MESSAGE_MAX_AGE` — never at
  night, never on a reader page, never a location, a deleted message,
  one stamped in the future or one from a hidden person. The read
  (:meth:`HouseholdChatService.recent_messages`) never creates the chat
  and never moves anyone's read watermark. Guardian blocks (§CP.F2) hold
  on the shared screen, failing closed: a blocked counterpart's messages
  never show, nor do a protected member's own while anyone they are
  blocked from is shown — the chat hides them from each other, and
  either could be standing at the wall.
* **Mentions, on the same opt-in.** Household-chat ``@here`` and
  ``@name`` mentions of the last :data:`MENTION_MAX_AGE` that nobody has
  acknowledged (a ✅ reaction) ride along on every page — ``@here`` from
  anyone but its sender, ``@name`` from that person — under the same
  rules as the messages (shown senders, guardian blocks, never at night);
  a mention of a hidden person, or of the sender themself, is skipped.
* **Assigned timetables only.** School is the timetables a member is
  assigned to; space timetables pinned to someone's Home are ignored on
  purpose — a pin is a personal Home choice (a parent pinning a child's
  class plan) and here would show that member as being at school.
* **Hidden people are gone.** A member the admin hid
  (``DashboardPersonSettings.show=False``) has no page and is removed
  from every slice: their presence, their events (an event left with no
  shown person is dropped, and a shared event never takes its row id or
  colour from a hidden member's copy), their chores (a task assigned only
  to hidden people is dropped) and their timetables; stickies they wrote
  are left out too.
* **Household time.** "Today" is the household-local date of ``now``
  (``HouseholdPreferences.tz``), and every datetime in a snapshot is in
  that zone — the server renders, so this is the tz boundary. Timetables
  are the exception that proves the rule: like My Corner, each one shows
  its lessons for "today" in *its own* zone (then converted for display).
* **Toggles.** A section whose household ``feat_*`` toggle is off is
  empty and its service is not called (shopping has no toggle). Person
  colours follow members' calendar colours only while the calendar
  section is on; when it's off they fall back to the palette.
* **Fail soft.** Each slice — and each calendar / task list inside one —
  that fails is logged at WARNING and comes back empty; one broken slice
  never breaks the page. The settings and household preferences are not
  slices — without them the page can't tell who is hidden, so their
  errors propagate.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ..domain import timetable as td
from ..domain.calendar import Calendar, CalendarEvent, all_day_covers
from ..domain.conversation import ConversationMessage, MentionGlance
from ..domain.household_dashboard import (
    CHAT_MAX_AGE,
    CHAT_SHOWN,
    HERE_SHOWN,
    MENTION_HERE,
    MENTION_MAX_AGE,
    MENTION_PERSON,
    MENTIONS_SCANNED,
    MESSAGE_MAX_AGE,
    MESSAGES_SHOWN,
    PERSON_PALETTE,
    SECTION_CALENDAR,
    SECTION_PRESENCE,
    SECTION_SHOPPING,
    SECTION_STICKIES,
    SECTION_TASKS,
    SECTION_TIMETABLE,
    DashboardAgendaItem,
    DashboardEvent,
    DashboardLesson,
    DashboardMention,
    DashboardMessage,
    DashboardPerson,
    DashboardSchoolDay,
    DashboardSettings,
    DashboardShoppingItem,
    DashboardSticky,
    DashboardTask,
    DashboardZone,
    HomeSnapshot,
    PersonSnapshot,
    in_night_window,
    initials,
    is_pure_emoji,
    lesson_color_token,
    picture_effective,
    subject_color_tokens,
    token_hex,
)
from ..domain.household_zone import HouseholdZone, zone_color
from ..domain.preferences import HouseholdPreferences
from ..domain.presence import PersonPresence
from ..domain.task import Task, TaskList, TaskStatus
from ..domain.timetable import Timetable
from ..domain.user import DASHBOARD_AUTHOR, User
from ..utils.datetime import as_utc
from ..utils.timezones import local_date, local_day_bounds, zone_of
from .calendar_service import CalendarService
from .child_protection_service import ChildProtectionService
from .dashboard_settings_service import DashboardSettingsService
from .dm_mentions import MENTIONABLE_TYPES
from .dm_service import DmService
from .household_chat_service import HouseholdChatService
from .household_zone_service import HouseholdZoneService
from .preferences_service import PreferencesService
from .presence_service import PresenceService
from .shopping_service import ShoppingService
from .sticky_service import StickyService
from .system_chat_policy import HOUSEHOLD_CHAT_SECTION
from .task_service import TaskService
from .timetable_service import TimetableService
from .user_service import UserService

log = logging.getLogger(__name__)

__all__ = ["HouseholdDashboardService"]

#: Per-slice caps — a wall tablet shows a glance, not an archive.
MAX_EVENTS = 30
MAX_CHORES = 30
MAX_SHOPPING = 20
#: A hard upper bound on the stickies a snapshot carries — the Notes
#: tab shows them all up to this; ``stickies_total`` keeps the true
#: count (the household board is read whole, as the app reads it).
MAX_STICKIES = 100
MAX_TOMORROW_ITEMS = 5
#: A person's page lists chores due within this many days; done chores
#: due that far ahead still count when they were finished today.
CHORE_LOOKAHEAD_DAYS = 7
#: Done chores are read back this far by due date; only those completed
#: today are kept (bounds the read of a long, never-archived done list).
DONE_LOOKBACK_DAYS = 14

#: Household-chat message ``type`` → :attr:`DashboardMessage.kind`.
_MESSAGE_KIND = {
    "text": "text",
    "transcript": "text",
    "image": "photo",
    "video": "video",
    "file": "file",
    "audio": "voice",
}

#: A message stamped this far past "now" (a sender's clock a little
#: ahead) is still read; anything later is not.
MESSAGE_CLOCK_SKEW = timedelta(minutes=2)

#: ``PersonPresence.state`` → the dashboard's presence word.
_PRESENCE = {"home": "home", "away": "away", "zone": "zone"}

_TOGGLED_SECTIONS = (
    SECTION_CALENDAR,
    SECTION_TASKS,
    SECTION_STICKIES,
    SECTION_PRESENCE,
    SECTION_TIMETABLE,
)


@dataclass(slots=True, frozen=True)
class _Context:
    """One snapshot's inputs (service-internal, never leaves)."""

    now: datetime  # household zone
    tz: str
    today: date
    prefs: HouseholdPreferences
    settings: DashboardSettings
    sections: frozenset[str]
    #: Every active member (hidden ones too), in directory order — the
    #: calendar-owner lookup uses it.
    members: tuple[User, ...]
    #: The shown members.
    shown: tuple[User, ...]
    shown_ids: frozenset[str]
    hidden_ids: frozenset[str]
    by_username: Mapping[str, str] = field(default_factory=dict)

    def on(self, section: str) -> bool:
        return section in self.sections

    def keep(self, user_ids: Iterable[str]) -> tuple[str, ...]:
        """``user_ids`` reduced to shown members, order kept, deduped."""
        return tuple(dict.fromkeys(u for u in user_ids if u in self.shown_ids))


@dataclass(slots=True, frozen=True)
class _Days:
    """Per-person timetable days: today's and tomorrow's school."""

    today: DashboardSchoolDay | None = None
    tomorrow: DashboardSchoolDay | None = None


@dataclass(slots=True, frozen=True)
class _Copy:
    """One calendar's row of a (possibly shared) event."""

    event: CalendarEvent
    owner_id: str
    color: str


async def _safe[T](label: str, aw: Awaitable[T], default: T) -> T:
    """Await one slice; on any error log at WARNING and use ``default``."""
    try:
        return await aw
    except Exception:
        log.warning("household dashboard: %s failed", label, exc_info=True)
        return default


def _located_at(raw: str | None, ctx: _Context) -> datetime | None:
    """A presence row's ``updated_at`` in the household zone — ``None``
    when missing or unreadable. Both stored UTC shapes are read: aware
    ISO 8601 and SQLite's naive ``datetime('now')``."""
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("household dashboard: unreadable presence time %r", raw)
        return None
    return as_utc(stamp).astimezone(ctx.now.tzinfo)


def _agenda_key(item: DashboardAgendaItem) -> tuple[int, datetime, datetime]:
    all_day = isinstance(item, DashboardEvent) and item.all_day
    return (0 if all_day else 1, item.start, item.end)


def _timed(items: Iterable[DashboardAgendaItem]) -> list[DashboardAgendaItem]:
    """Items with a real start time: timed events and lessons that take
    place (no all-day events, breaks or cancelled lessons)."""
    out: list[DashboardAgendaItem] = []
    for item in items:
        if isinstance(item, DashboardEvent):
            if not item.all_day:
                out.append(item)
        elif item.takes_place:
            out.append(item)
    return out


def _next_after(
    now: datetime, items: Iterable[DashboardAgendaItem]
) -> DashboardAgendaItem | None:
    upcoming = [i for i in _timed(items) if i.start > now]
    return min(upcoming, key=_agenda_key, default=None)


def _running_at(
    now: datetime, items: Iterable[DashboardAgendaItem]
) -> DashboardAgendaItem | None:
    """The item running at ``now`` — the latest-started when they overlap."""
    running = [i for i in _timed(items) if i.start <= now < i.end]
    return max(running, key=lambda i: (i.start, i.end), default=None)


def _first_lesson(day: DashboardSchoolDay | None) -> DashboardLesson | None:
    if day is None:
        return None
    return next((ls for ls in day.lessons if ls.takes_place), None)


def _merge_key(ev: CalendarEvent) -> str:
    """Which rows are copies of one shared event — the SPA's
    ``groupSharedEvents`` (``client/src/utils/calendar.ts``): the
    client-stamped ``client_event_uuid`` when present, else the content
    key (summary, start, end, creator, description, location, cover).
    The start is part of the uuid key too, so each occurrence of a
    recurring shared event stays its own item."""
    if ev.client_event_uuid:
        return f"uuid\x1f{ev.client_event_uuid}\x1f{ev.start.isoformat()}"
    return "\x1f".join(
        (
            "content",
            ev.summary,
            ev.start.isoformat(),
            ev.end.isoformat(),
            ev.created_by,
            ev.description or "",
            ev.location or "",
            ev.cover_url or "",
        )
    )


class HouseholdDashboardService:
    """Build the household dashboard's snapshots (read-only)."""

    __slots__ = (
        "_acks",
        "_calendar",
        "_chat",
        "_cp",
        "_preferences",
        "_presence",
        "_settings",
        "_shopping",
        "_stickies",
        "_tasks",
        "_timetables",
        "_users",
        "_zones",
    )

    def __init__(
        self,
        *,
        user_service: UserService,
        presence_service: PresenceService,
        household_zones: HouseholdZoneService,
        calendar_service: CalendarService,
        task_service: TaskService,
        shopping_service: ShoppingService,
        timetable_service: TimetableService,
        sticky_service: StickyService,
        preferences_service: PreferencesService,
        dashboard_settings: DashboardSettingsService,
        household_chat: HouseholdChatService,
        child_protection: ChildProtectionService,
        acknowledgements: DmService,
    ) -> None:
        self._users = user_service
        self._presence = presence_service
        self._zones = household_zones
        self._calendar = calendar_service
        self._tasks = task_service
        self._shopping = shopping_service
        self._timetables = timetable_service
        self._stickies = sticky_service
        self._preferences = preferences_service
        self._settings = dashboard_settings
        self._chat = household_chat
        self._cp = child_protection
        self._acks = acknowledgements

    # ── Public API ───────────────────────────────────────────────────────

    async def home(self, *, now: datetime | None = None) -> HomeSnapshot:
        """The household overview at ``now`` (default: the current time)."""
        ctx = await self._context(now)
        tomorrow = ctx.today + timedelta(days=1)
        calendars, timetables = await asyncio.gather(
            self._calendars(ctx), self._timetable_rows(ctx)
        )
        colors = self._person_colors(ctx, calendars)
        senders = await self._chat_senders(ctx)
        located, events, chores, shopping, notes, chat = await asyncio.gather(
            asyncio.gather(
                self._people(ctx, colors, located=True), self._zone_slice(ctx)
            ),
            self._events(ctx, calendars, colors),
            self._chores(ctx, due_to=ctx.today),
            self._shopping_slice(),
            self._sticky_slice(ctx),
            asyncio.gather(
                self._messages(ctx, senders, max_age=CHAT_MAX_AGE, limit=CHAT_SHOWN),
                self._mentions(ctx, senders),
            ),
        )
        people, zones = located
        messages, mentions = chat
        school = [self._school(ctx, user.user_id, timetables) for user in ctx.shown]
        shopping_items, shopping_total = shopping
        stickies, stickies_total = notes
        all_today = events.get(ctx.today, [])
        school_today = tuple(d.today for d in school if d.today is not None)
        school_tomorrow = tuple(
            ls for ls in (_first_lesson(d.tomorrow) for d in school) if ls is not None
        )
        lessons_today = [ls for day in school_today for ls in day.lessons]
        return HomeSnapshot(
            now=ctx.now,
            today=ctx.today,
            household_name=ctx.prefs.household_name,
            sections=ctx.sections,
            is_night=self._is_night(ctx),
            people=people,
            events_today=tuple(all_today[:MAX_EVENTS]),
            # From the whole day, not just the displayed (capped) events.
            next_up=_next_after(ctx.now, [*all_today, *lessons_today]),
            chores_today=chores,
            shopping=shopping_items,
            shopping_total=shopping_total,
            stickies=stickies,
            stickies_total=stickies_total,
            school=school_today,
            tomorrow_first=_next_after(
                ctx.now, [*events.get(tomorrow, []), *school_tomorrow]
            ),
            messages=messages,
            mentions=mentions,
            messages_on=self._messages_on(ctx),
            zones=zones,
        )

    async def person(
        self, user_id: str, *, now: datetime | None = None
    ) -> PersonSnapshot | None:
        """``user_id``'s page at ``now`` — ``None`` for an unknown,
        inactive or hidden member."""
        ctx = await self._context(now)
        if user_id not in ctx.shown_ids:
            return None
        tomorrow = ctx.today + timedelta(days=1)
        calendars, timetables = await asyncio.gather(
            self._calendars(ctx), self._timetable_rows(ctx)
        )
        colors = self._person_colors(ctx, calendars)
        senders = await self._chat_senders(ctx)
        people, events, chores, mentions = await asyncio.gather(
            self._people(ctx, colors),
            self._events(ctx, calendars, colors),
            self._chores(
                ctx,
                due_to=ctx.today + timedelta(days=CHORE_LOOKAHEAD_DAYS),
                user_id=user_id,
            ),
            # Every page: the rail badges everyone with a mention waiting.
            self._mentions(ctx, senders),
        )
        school = self._school(ctx, user_id, timetables)
        person = next(p for p in people if p.user_id == user_id)
        # Only the picture view shows messages; a reader page never reads them.
        messages = await self._messages(ctx, senders) if person.picture_view else ()

        def mine(day: date) -> list[DashboardEvent]:
            return [e for e in events.get(day, []) if user_id in e.person_ids]

        today_items: list[DashboardAgendaItem] = [*mine(ctx.today)]
        if school.today is not None:
            today_items.extend(school.today.lessons)
        today_items.sort(key=_agenda_key)
        # Events and lessons are capped apart: a full school day must not
        # push the after-school events (piano at 15:00) off the list.
        tomorrow_items: list[DashboardAgendaItem] = [
            *sorted(mine(tomorrow), key=_agenda_key)[:MAX_TOMORROW_ITEMS]
        ]
        if school.tomorrow is not None:
            tomorrow_items.extend(
                [ls for ls in school.tomorrow.lessons if ls.takes_place][
                    :MAX_TOMORROW_ITEMS
                ]
            )
        tomorrow_items.sort(key=_agenda_key)
        return PersonSnapshot(
            person=person,
            now=ctx.now,
            today=ctx.today,
            sections=ctx.sections,
            is_night=self._is_night(ctx),
            now_item=_running_at(ctx.now, today_items),
            next_item=_next_after(ctx.now, today_items),
            today_items=tuple(today_items),
            tomorrow_items=tuple(tomorrow_items),
            chores=chores,
            school=school.today,
            school_tomorrow_first=_first_lesson(school.tomorrow),
            people=people,
            messages=messages,
            mentions=mentions,
        )

    async def acknowledge(
        self,
        message_id: str,
        *,
        person_id: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """A "Got it" tapped on the wall: ``True`` once the mention is
        acknowledged, ``False`` (nothing written) when the wall doesn't
        show it — never saying why.

        Nothing the tablet sends is trusted: the mentions are read again
        under every rule of the read model (opt-in, chat on, not night,
        shown and unblocked sender, within :data:`MENTION_MAX_AGE`), and
        ``message_id`` must be one of them — an ``@here`` when no
        ``person_id`` is given (written as
        :data:`~socialhome.domain.user.DASHBOARD_AUTHOR`), else a mention of
        that shown person (credited to them: it is their page). An
        ``@here`` must be one of the newest :data:`HERE_SHOWN` waiting —
        the banners Home shows — so what can be written is what is
        visible. Already acknowledged → ``True`` without a second write.
        """
        ctx = await self._context(now)
        if person_id is not None and person_id not in ctx.shown_ids:
            return False
        senders = await self._chat_senders(ctx)
        kind = MENTION_HERE if person_id is None else MENTION_PERSON
        rows = await self._mention_rows(ctx, senders)
        # Only the @here banners Home shows: the newest waiting ones (and
        # any already seen, which just answer yes).
        waiting_here = [m for m, seen in rows if m.kind == MENTION_HERE and not seen]
        banners = {m.message_id for m in waiting_here[:HERE_SHOWN]}
        match = next(
            (
                (mention, seen)
                for mention, seen in rows
                if (
                    seen
                    or mention.kind != MENTION_HERE
                    or mention.message_id in banners
                )
                and mention.message_id == message_id
                and mention.kind == kind
                and mention.person_id == person_id
            ),
            None,
        )
        if match is None:
            return False
        if match[1]:
            return True
        try:
            await self._acks.acknowledge_on_dashboard(
                message_id, as_user_id=person_id or DASHBOARD_AUTHOR
            )
        except KeyError:  # deleted (or gone) since the read
            return False
        return True

    async def avatar(self, user_id: str) -> tuple[bytes, str] | None:
        """``(image bytes, picture hash)`` of a *shown*, active member's
        profile picture — ``None`` for a hidden, inactive or unknown member
        (whose picture is never read) or one without a picture. Fails soft
        like a slice."""
        settings = await self._settings.get()
        # The directory is read either way, so a hidden member and an
        # unknown id take the same work (no timing tells them apart).
        members = await _safe("users", self._users.list_active(), list[User]())
        if not settings.person(user_id).show:
            return None
        if not any(u.user_id == user_id for u in members):
            return None
        return await _safe("picture", self._users.get_picture(user_id), None)

    # ── Context ──────────────────────────────────────────────────────────

    async def _context(self, now: datetime | None) -> _Context:
        settings = await self._settings.get()
        prefs = await self._preferences.get_household()
        members = tuple(await _safe("users", self._users.list_active(), list[User]()))
        hidden = frozenset(uid for uid, s in settings.people if not s.show)
        shown = tuple(u for u in members if u.user_id not in hidden)
        local_now = as_utc(now or datetime.now(UTC)).astimezone(zone_of(prefs.tz))
        sections = frozenset(
            {s for s in _TOGGLED_SECTIONS if prefs.is_enabled(s)} | {SECTION_SHOPPING}
        )
        return _Context(
            now=local_now,
            tz=prefs.tz,
            today=local_now.date(),
            prefs=prefs,
            settings=settings,
            sections=sections,
            members=members,
            shown=shown,
            shown_ids=frozenset(u.user_id for u in shown),
            hidden_ids=hidden,
            by_username={u.username: u.user_id for u in members},
        )

    @staticmethod
    def _is_night(ctx: _Context) -> bool:
        return in_night_window(
            ctx.now.time(), ctx.settings.night_start, ctx.settings.night_end
        )

    # ── People ───────────────────────────────────────────────────────────

    async def _calendars(self, ctx: _Context) -> list[Calendar]:
        """Every member's personal calendar — ``[]`` without calling the
        calendar service when the calendar section is off (person colours
        then fall back to the palette). The username is ignored for
        ``scope="household"``."""
        if not ctx.on(SECTION_CALENDAR):
            return []
        rows = await _safe(
            "calendars",
            self._calendar.list_calendars("", scope="household"),
            list[Calendar](),
        )
        return [
            c
            for c in rows
            if c.calendar_type == "personal" and c.owner_username in ctx.by_username
        ]

    @staticmethod
    def _person_colors(ctx: _Context, calendars: Sequence[Calendar]) -> dict[str, str]:
        """Each shown member's colour: their first personal calendar's
        (only while the calendar section is on — otherwise ``calendars`` is
        empty), else a palette colour by their position among the shown members."""
        from_calendars: dict[str, str] = {}
        for c in calendars:
            from_calendars.setdefault(ctx.by_username[c.owner_username], c.color)
        return {
            user.user_id: from_calendars.get(
                user.user_id, PERSON_PALETTE[index % len(PERSON_PALETTE)]
            )
            for index, user in enumerate(ctx.shown)
        }

    async def _presence_rows(self, ctx: _Context) -> dict[str, PersonPresence]:
        if not ctx.on(SECTION_PRESENCE):
            return {}
        rows = await _safe(
            "presence", self._presence.list_presence(), list[PersonPresence]()
        )
        return {r.user_id: r for r in rows}

    async def _people(
        self, ctx: _Context, colors: Mapping[str, str], *, located: bool = False
    ) -> tuple[DashboardPerson, ...]:
        """The shown members. Only Home (``located``) carries where each
        one is — the Map tab's point and time; a person page never does."""
        presence = await self._presence_rows(ctx)
        out: list[DashboardPerson] = []
        for user in ctx.shown:
            row = presence.get(user.user_id)
            state = _PRESENCE.get(row.state, "unknown") if row else "unknown"
            where = row if located else None
            status = user.status
            live = status.is_set and not status.is_expired(ctx.now)
            out.append(
                DashboardPerson(
                    user_id=user.user_id,
                    display_name=user.display_name,
                    initials=initials(user.display_name),
                    color=colors[user.user_id],
                    picture_hash=user.picture_hash,
                    picture_view=picture_effective(
                        ctx.settings.person(user.user_id), user
                    ),
                    presence=state,
                    zone_name=row.zone_name if row and state == "zone" else None,
                    # Only a real emoji: the field accepts short text too,
                    # which the wall shows as an icon, never as words.
                    status_emoji=(
                        status.emoji
                        if live and is_pure_emoji(status.emoji or "")
                        else None
                    ),
                    status_text=(
                        (status.text or None)
                        if live and ctx.settings.show_status_text
                        else None
                    ),
                    latitude=where.latitude if where else None,
                    longitude=where.longitude if where else None,
                    located_at=_located_at(where.updated_at, ctx) if where else None,
                )
            )
        return tuple(out)

    async def _zone_slice(self, ctx: _Context) -> tuple[DashboardZone, ...]:
        """The household zones for the Map tab — Home first, then by name;
        ``()`` without calling the zone service while presence is off."""
        if not ctx.on(SECTION_PRESENCE):
            return ()
        rows = await _safe("zones", self._zones.list_zones(), list[HouseholdZone]())
        rows.sort(key=lambda z: (not z.is_home, z.name.casefold()))
        return tuple(
            DashboardZone(
                id=z.id,
                name=z.name,
                latitude=z.latitude,
                longitude=z.longitude,
                radius_m=z.radius_m,
                color=zone_color(z),
                icon=z.icon,
                is_home=z.is_home,
            )
            for z in rows
        )

    # ── Calendar ─────────────────────────────────────────────────────────

    async def _events(
        self,
        ctx: _Context,
        calendars: Sequence[Calendar],
        colors: Mapping[str, str],
    ) -> dict[date, list[DashboardEvent]]:
        """Shown members' events overlapping household-local today and
        tomorrow, keyed by that date (an event spanning both is in both).

        Recurring events arrive as their occurrences (the calendar repo
        expands them in range). The copies of a shared event (one row per
        member's calendar, see :func:`_merge_key`) become one event whose
        id, text and colour come from a *shown* member's copy — the
        creator's when shown. When only a hidden member's copy exists (a
        shown member is just an attendee), the event gets a derived id and
        the first shown person's colour, never the hidden calendar's.
        """
        if not calendars:  # calendar section off, or no member calendars
            return {}
        days = (ctx.today, ctx.today + timedelta(days=1))
        start = local_day_bounds(days[0], ctx.tz)[0]
        end = local_day_bounds(days[1], ctx.tz)[1]
        reads = await asyncio.gather(
            *(
                _safe(
                    f"calendar {c.id}",
                    self._calendar.list_events_in_range(
                        c.id, start=start.isoformat(), end=end.isoformat()
                    ),
                    list[CalendarEvent](),
                )
                for c in calendars
            )
        )
        groups: dict[str, list[_Copy]] = {}
        for c, rows in zip(calendars, reads, strict=True):
            owner = ctx.by_username[c.owner_username]
            for ev in rows:
                groups.setdefault(_merge_key(ev), []).append(_Copy(ev, owner, c.color))
        zone = zone_of(ctx.tz)
        out: dict[date, list[DashboardEvent]] = {}
        for key, copies in groups.items():
            person_ids = ctx.keep(
                uid for cp in copies for uid in (cp.owner_id, *cp.event.attendees)
            )
            if not person_ids:
                continue
            shown_copies = [cp for cp in copies if cp.owner_id in ctx.shown_ids]
            if shown_copies:
                primary = next(
                    (cp for cp in shown_copies if cp.owner_id == cp.event.created_by),
                    shown_copies[0],
                )
                ev, event_id, color = primary.event, primary.event.id, primary.color
            else:
                ev = copies[0].event
                event_id = "event-" + hashlib.sha256(key.encode()).hexdigest()[:16]
                color = colors[person_ids[0]]
            item = DashboardEvent(
                id=event_id,
                title=ev.summary,
                start=ev.start.astimezone(zone),
                end=ev.end.astimezone(zone),
                all_day=ev.all_day,
                person_ids=person_ids,
                color=color,
                location=ev.location,
            )
            for day in days:
                if self._on_day(ctx, ev, day):
                    out.setdefault(day, []).append(item)
        for items in out.values():
            items.sort(key=lambda e: (*_agenda_key(e), e.title))
        return out

    @staticmethod
    def _on_day(ctx: _Context, ev: CalendarEvent, day: date) -> bool:
        if ev.all_day:
            return all_day_covers(ev, day, ctx.tz)
        lo, hi = local_day_bounds(day, ctx.tz)
        if ev.end <= ev.start:  # zero-length: a point in time
            return lo <= ev.start < hi
        return ev.start < hi and ev.end > lo

    # ── Tasks ────────────────────────────────────────────────────────────

    async def _chores(
        self, ctx: _Context, *, due_to: date, user_id: str | None = None
    ) -> tuple[DashboardTask, ...]:
        """Open tasks due by ``due_to`` (overdue included) plus those
        completed today — only ``user_id``'s when given, filtered before
        the :data:`MAX_CHORES` cap so a busy household can't push a
        person's own chores off their page."""
        if not ctx.on(SECTION_TASKS):
            return ()
        lists = await _safe("tasks", self._tasks.list_lists(), list[TaskList]())
        per_list = await asyncio.gather(
            *(
                _safe(
                    f"task list {lst.id}",
                    self._list_chores(ctx, lst, due_to),
                    list[DashboardTask](),
                )
                for lst in lists
            )
        )
        out = [c for chores in per_list for c in chores]
        if user_id is not None:
            out = [c for c in out if user_id in c.person_ids]
        out.sort(key=lambda c: (c.done, c.due or date.max, c.title))
        return tuple(out[:MAX_CHORES])

    async def _list_chores(
        self, ctx: _Context, lst: TaskList, due_to: date
    ) -> list[DashboardTask]:
        """One list's chores. :class:`Task` has no completion stamp, so
        "completed today" is a done task whose ``updated_at`` falls on
        household-local today (a later edit to a done task re-dates it);
        done tasks due up to :data:`CHORE_LOOKAHEAD_DAYS` ahead count, so a
        chore finished a day early still shows as done today. Tasks
        without a due date aren't chores and never show."""
        open_ = await self._tasks.list_tasks(
            lst.id, include_done=False, due_to=due_to.isoformat()
        )
        done = await self._tasks.list_tasks(
            lst.id,
            status=TaskStatus.DONE.value,
            due_from=(ctx.today - timedelta(days=DONE_LOOKBACK_DAYS)).isoformat(),
            due_to=(ctx.today + timedelta(days=CHORE_LOOKAHEAD_DAYS)).isoformat(),
        )
        out: list[DashboardTask] = []
        for t in (*open_, *done):
            chore = self._chore(ctx, t)
            if chore is not None:
                out.append(chore)
        return out

    @staticmethod
    def _chore(ctx: _Context, t: Task) -> DashboardTask | None:
        if t.archived_at is not None or t.due_date is None:
            return None
        done = t.status == TaskStatus.DONE
        if done and local_date(as_utc(t.updated_at), ctx.tz) != ctx.today:
            return None
        person_ids = ctx.keep(t.assignees)
        if t.assignees and not person_ids:
            return None  # assigned only to hidden (or departed) people
        return DashboardTask(
            id=t.id,
            title=t.title,
            due=t.due_date,
            overdue_days=0 if done else max(0, (ctx.today - t.due_date).days),
            done=done,
            person_ids=person_ids,
        )

    # ── Shopping + stickies ──────────────────────────────────────────────

    async def _shopping_slice(
        self,
    ) -> tuple[tuple[DashboardShoppingItem, ...], int]:
        items = await _safe(
            "shopping", self._shopping.list_items(include_completed=False), []
        )
        shown = tuple(
            DashboardShoppingItem(id=i.id, text=i.text, store=i.store)
            for i in items[:MAX_SHOPPING]
        )
        return shown, len(items)

    async def _sticky_slice(
        self, ctx: _Context
    ) -> tuple[tuple[DashboardSticky, ...], int]:
        """The household stickies (hidden authors left out), at most
        :data:`MAX_STICKIES` of them, and how many there are in all."""
        if not ctx.on(SECTION_STICKIES):
            return (), 0
        notes = await _safe("stickies", self._stickies.list(space_id=None), [])
        shown = [
            DashboardSticky(id=n.id, content=n.content, color=n.color)
            for n in notes
            if n.author not in ctx.hidden_ids
        ]
        return tuple(shown[:MAX_STICKIES]), len(shown)

    # ── Household chat ───────────────────────────────────────────────────

    @staticmethod
    def _messages_on(ctx: _Context) -> bool:
        """The admin turned messages on and the household chat is on."""
        return ctx.settings.show_household_messages and ctx.prefs.is_enabled(
            HOUSEHOLD_CHAT_SECTION
        )

    async def _chat_senders(self, ctx: _Context) -> frozenset[str]:
        """Whose household-chat messages and mentions the wall may show:
        the shown members no guardian block withholds — empty (nothing is
        read) while the admin hasn't turned messages on, the household
        chat is off, or it is night."""
        if not self._messages_on(ctx) or self._is_night(ctx) or not ctx.shown_ids:
            return frozenset()
        try:
            withheld = await self._guardian_withheld(ctx)
        except Exception:
            # Fail closed: without the blocks nothing is safe to show.
            log.warning("household dashboard: guardian blocks failed", exc_info=True)
            return frozenset()
        return ctx.shown_ids - withheld

    async def _messages(
        self,
        ctx: _Context,
        senders: frozenset[str],
        *,
        max_age: timedelta = MESSAGE_MAX_AGE,
        limit: int = MESSAGES_SHOWN,
    ) -> tuple[DashboardMessage, ...]:
        """The newest ``limit`` household-chat messages of the last
        ``max_age`` from ``senders`` (:meth:`_chat_senders`) — ``()``
        without reading anything when there are none."""
        if not senders:
            return ()
        rows = await _safe(
            "household chat",
            self._chat.recent_messages(
                since=ctx.now - max_age,
                until=ctx.now + MESSAGE_CLOCK_SKEW,
                senders=senders,
                limit=limit,
            ),
            list[ConversationMessage](),
        )
        zone = zone_of(ctx.tz)
        out: list[DashboardMessage] = []
        for m in rows:
            kind = _MESSAGE_KIND.get(m.type)
            if (
                kind is None
                or m.deleted
                or m.sender_user_id not in senders
                or as_utc(m.created_at) > ctx.now
            ):
                continue  # the read filters these already; never trust it alone
            text = m.content.strip() if kind in ("text", "voice") else ""
            if kind == "text" and not text:
                continue
            out.append(
                DashboardMessage(
                    id=m.id,
                    sender_id=m.sender_user_id,
                    kind=kind,
                    text=text,
                    sent_at=as_utc(m.created_at).astimezone(zone),
                )
            )
        return tuple(out)

    async def _mentions(
        self, ctx: _Context, senders: frozenset[str]
    ) -> tuple[DashboardMention, ...]:
        """The unacknowledged ``@here`` / ``@name`` mentions, newest first."""
        return tuple(
            m for m, seen in await self._mention_rows(ctx, senders) if not seen
        )

    async def _mention_rows(
        self, ctx: _Context, senders: frozenset[str]
    ) -> list[tuple[DashboardMention, bool]]:
        """Every mention the wall may show from ``senders`` in the last
        :data:`MENTION_MAX_AGE`, each with whether it is acknowledged:
        an ``@here`` by anyone but its sender, an ``@name`` by that
        person. ``[]`` without reading anything when there are no
        senders; a failed read fails soft like a slice."""
        if not senders:
            return []
        rows = await _safe(
            "household chat mentions",
            self._chat.recent_mentions(
                since=ctx.now - MENTION_MAX_AGE,
                until=ctx.now + MESSAGE_CLOCK_SKEW,
                senders=senders,
                limit=MENTIONS_SCANNED,
            ),
            list[MentionGlance](),
        )
        zone = zone_of(ctx.tz)
        oldest = ctx.now - MENTION_MAX_AGE
        out: list[tuple[DashboardMention, bool]] = []
        for row in rows:
            m = row.message
            sent = as_utc(m.created_at)
            if (
                m.deleted
                or m.type not in MENTIONABLE_TYPES
                or m.sender_user_id not in senders
                or sent > ctx.now
                or sent < oldest
            ):
                continue  # the read filters these already; never trust it alone
            text = m.content.strip()
            local = sent.astimezone(zone)
            if row.here:
                seen = bool(row.acked_by - {m.sender_user_id})
                out.append(
                    (
                        DashboardMention(
                            message_id=m.id,
                            sender_id=m.sender_user_id,
                            text=text,
                            sent_at=local,
                            kind=MENTION_HERE,
                        ),
                        seen,
                    )
                )
            for uid in dict.fromkeys(row.user_ids):
                if uid not in ctx.shown_ids or uid == m.sender_user_id:
                    continue
                out.append(
                    (
                        DashboardMention(
                            message_id=m.id,
                            sender_id=m.sender_user_id,
                            text=text,
                            sent_at=local,
                            kind=MENTION_PERSON,
                            person_id=uid,
                        ),
                        uid in row.acked_by,
                    )
                )
        return out

    async def _guardian_withheld(self, ctx: _Context) -> frozenset[str]:
        """Whose messages a guardian block (§CP.F2) keeps off the wall.

        For every protected active member (hidden ones too — they can
        still stand at the tablet): everyone a block separates them from,
        and the member themself while one of those is a household member —
        shown or hidden, anyone local can stand in front of the wall."""
        out: set[str] = set()
        local = {u.user_id for u in ctx.members}
        for user in ctx.members:
            if not user.child_protection_enabled:
                continue
            counterparts = await self._cp.guardian_block_counterparts(user.user_id)
            out |= counterparts
            if counterparts & local:
                out.add(user.user_id)
        return frozenset(out)

    # ── Timetables ───────────────────────────────────────────────────────

    async def _timetable_rows(self, ctx: _Context) -> list[Timetable]:
        """Every household timetable, read once per snapshot (``[]`` when
        the timetable section is off); people and days filter in memory."""
        if not ctx.on(SECTION_TIMETABLE):
            return []
        return await _safe("timetables", self._timetables.list_all(), list[Timetable]())

    def _school(
        self, ctx: _Context, user_id: str, timetables: Sequence[Timetable]
    ) -> _Days:
        """``user_id``'s school today and tomorrow.

        Only timetables the person is *assigned* to count. Space
        timetables pinned to a member's Home are deliberately ignored
        (unlike My Corner): a pin is a personal "show this on my Home"
        choice — a parent pinning their child's class plan — and on the
        shared household screen it would mislabel that member as being at
        school.
        """
        return _Days(
            today=self._school_day(ctx, user_id, timetables, offset=0),
            tomorrow=self._school_day(ctx, user_id, timetables, offset=1),
        )

    def _school_day(
        self,
        ctx: _Context,
        user_id: str,
        timetables: Sequence[Timetable],
        *,
        offset: int,
    ) -> DashboardSchoolDay | None:
        """The lessons ``offset`` days after "today".

        Each plan's lessons come from *its own* local day
        (``local_date(now, tt.tz) + offset``), consistent with
        :meth:`TimetableService.today_for_user` — a Berlin plan seen from
        New York at 20:00 already shows Berlin's tomorrow morning. Lesson
        times are converted to household time for display, and ``date`` on
        the result is the household day (``today + offset``), so when a
        plan's zone differs from the household's, ``date`` and the
        lessons' own date can differ by one."""
        zone = zone_of(ctx.tz)
        names: list[str] = []
        lessons: list[DashboardLesson] = []
        for tt in timetables:
            try:
                day = local_date(ctx.now, tt.tz) + timedelta(days=offset)
                if not TimetableService.in_effect_for(tt, user_id, day):
                    continue
                resolved = td.today_timetable(tt, day)
            except Exception:  # one bad row never empties the slice
                log.warning(
                    "household dashboard: timetable %s failed", tt.id, exc_info=True
                )
                continue
            if resolved is None or not td.has_lessons(resolved):
                continue
            names.append(tt.name)
            subjects = subject_color_tokens(tt)
            for tl in resolved.lessons:
                ls = tl.lesson
                token = (
                    lesson_color_token(ls.color, ls.title, subjects)
                    if ls.kind is td.EntryKind.LESSON
                    else None
                )
                lessons.append(
                    DashboardLesson(
                        person_id=user_id,
                        start=tl.start_at.astimezone(zone),
                        end=tl.end_at.astimezone(zone),
                        kind=ls.kind.value,
                        status=ls.status.value,
                        title=ls.title,
                        label=ls.label,
                        icon=ls.icon,
                        room=ls.room,
                        teacher=ls.teacher,
                        note=ls.note,
                        color=token,
                        color_hex=token_hex(token),
                    )
                )
        if not lessons:
            return None
        lessons.sort(key=lambda ls: (ls.start, ls.end))
        taking_place = [ls for ls in lessons if ls.takes_place]
        return DashboardSchoolDay(
            person_id=user_id,
            date=ctx.today + timedelta(days=offset),
            timetable_name=" · ".join(names),
            lessons=tuple(lessons),
            first_start=taking_place[0].start if taking_place else None,
            last_end=max((ls.end for ls in taking_place), default=None),
            changes=tuple(ls for ls in lessons if ls.status != "normal"),
        )
