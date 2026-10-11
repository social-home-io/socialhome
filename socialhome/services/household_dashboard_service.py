"""Household dashboard snapshots — the wall tablet's read model.

:class:`HouseholdDashboardService` assembles the household-safe
:class:`~socialhome.domain.household_dashboard.HomeSnapshot` (the
overview) and :class:`~socialhome.domain.household_dashboard.PersonSnapshot`
(one member's page) that the dashboard's server-rendered views display.

House rules:

* **Read-only.** Only plain reads of the owning services — nothing that
  reconciles or creates rows (so never the household-chat summary).
* **Household-safe.** Presence is home / away / a zone name, never
  coordinates; calendar events come from members' personal calendars;
  chores, the shopping list, today's timetables and household stickies.
  Never DMs, notifications, chat or polls.
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
  empty and its service is not called (shopping has no toggle). The
  calendar list is still read once for person colours, so colours don't
  change when the calendar toggle flips.
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
from datetime import date, datetime, timedelta, timezone

from ..domain import timetable as td
from ..domain.calendar import Calendar, CalendarEvent, all_day_covers
from ..domain.household_dashboard import (
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
    DashboardPerson,
    DashboardSchoolDay,
    DashboardSettings,
    DashboardShoppingItem,
    DashboardSticky,
    DashboardTask,
    HomeSnapshot,
    PersonSnapshot,
    in_night_window,
    initials,
    lesson_color_token,
    picture_effective,
    subject_color_tokens,
    token_hex,
)
from ..domain.preferences import HouseholdPreferences
from ..domain.presence import PersonPresence
from ..domain.task import Task, TaskList, TaskStatus
from ..domain.timetable import Timetable
from ..domain.user import User
from ..utils.timezones import local_date, local_day_bounds, zone_of
from .calendar_service import CalendarService
from .dashboard_settings_service import DashboardSettingsService
from .preferences_service import PreferencesService
from .presence_service import PresenceService
from .shopping_service import ShoppingService
from .sticky_service import StickyService
from .task_service import TaskService
from .timetable_service import TimetableService
from .user_service import UserService

log = logging.getLogger(__name__)

__all__ = ["HouseholdDashboardService"]

#: Per-slice caps — a wall tablet shows a glance, not an archive.
MAX_EVENTS = 30
MAX_CHORES = 30
MAX_SHOPPING = 20
MAX_STICKIES = 20
MAX_TOMORROW_ITEMS = 5
#: A person's page lists chores due within this many days; done chores
#: due that far ahead still count when they were finished today.
CHORE_LOOKAHEAD_DAYS = 7
#: Done chores are read back this far by due date; only those completed
#: today are kept (bounds the read of a long, never-archived done list).
DONE_LOOKBACK_DAYS = 14

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
    #: palette index and the calendar-owner lookup use it.
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


def _aware(value: datetime) -> datetime:
    """Treat a naive datetime as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


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
        "_users",
        "_presence",
        "_calendar",
        "_tasks",
        "_shopping",
        "_timetables",
        "_stickies",
        "_preferences",
        "_settings",
    )

    def __init__(
        self,
        *,
        user_service: UserService,
        presence_service: PresenceService,
        calendar_service: CalendarService,
        task_service: TaskService,
        shopping_service: ShoppingService,
        timetable_service: TimetableService,
        sticky_service: StickyService,
        preferences_service: PreferencesService,
        dashboard_settings: DashboardSettingsService,
    ) -> None:
        self._users = user_service
        self._presence = presence_service
        self._calendar = calendar_service
        self._tasks = task_service
        self._shopping = shopping_service
        self._timetables = timetable_service
        self._stickies = sticky_service
        self._preferences = preferences_service
        self._settings = dashboard_settings

    # ── Public API ───────────────────────────────────────────────────────

    async def home(self, *, now: datetime | None = None) -> HomeSnapshot:
        """The household overview at ``now`` (default: the current time)."""
        ctx = await self._context(now)
        tomorrow = ctx.today + timedelta(days=1)
        calendars, timetables = await asyncio.gather(
            self._calendars(ctx), self._timetable_rows(ctx)
        )
        colors = self._person_colors(ctx, calendars)
        people, events, chores, shopping, stickies = await asyncio.gather(
            self._people(ctx, colors),
            self._events(ctx, calendars, colors),
            self._chores(ctx, due_to=ctx.today),
            self._shopping_slice(),
            self._sticky_slice(ctx),
        )
        school = [self._school(ctx, user.user_id, timetables) for user in ctx.shown]
        shopping_items, shopping_total = shopping
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
            school=school_today,
            school_tomorrow=school_tomorrow,
            tomorrow_first=_next_after(
                ctx.now, [*events.get(tomorrow, []), *school_tomorrow]
            ),
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
        people, events, chores = await asyncio.gather(
            self._people(ctx, colors),
            self._events(ctx, calendars, colors),
            self._chores(
                ctx,
                due_to=ctx.today + timedelta(days=CHORE_LOOKAHEAD_DAYS),
                user_id=user_id,
            ),
        )
        school = self._school(ctx, user_id, timetables)
        person = next(p for p in people if p.user_id == user_id)

        def mine(day: date) -> list[DashboardEvent]:
            return [e for e in events.get(day, []) if user_id in e.person_ids]

        today_items: list[DashboardAgendaItem] = [*mine(ctx.today)]
        if school.today is not None:
            today_items.extend(school.today.lessons)
        today_items.sort(key=_agenda_key)
        tomorrow_items: list[DashboardAgendaItem] = [*mine(tomorrow)]
        if school.tomorrow is not None:
            tomorrow_items.extend(
                ls for ls in school.tomorrow.lessons if ls.takes_place
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
            tomorrow_items=tuple(tomorrow_items[:MAX_TOMORROW_ITEMS]),
            chores=chores,
            school=school.today,
            school_tomorrow_first=_first_lesson(school.tomorrow),
        )

    # ── Context ──────────────────────────────────────────────────────────

    async def _context(self, now: datetime | None) -> _Context:
        settings = await self._settings.get()
        prefs = await self._preferences.get_household()
        members = tuple(await _safe("users", self._users.list_active(), list[User]()))
        hidden = frozenset(uid for uid, s in settings.people if not s.show)
        shown = tuple(u for u in members if u.user_id not in hidden)
        local_now = _aware(now or datetime.now(timezone.utc)).astimezone(
            zone_of(prefs.tz)
        )
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
        """Every member's personal calendar. Read even when the calendar
        section is off — person colours come from it and must not change
        with the toggle (events are only read when it's on). The username
        is ignored for ``scope="household"``."""
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
        """Each shown member's colour: their first personal calendar's,
        else a palette colour by their position in the household."""
        from_calendars: dict[str, str] = {}
        for c in calendars:
            from_calendars.setdefault(ctx.by_username[c.owner_username], c.color)
        return {
            user.user_id: from_calendars.get(
                user.user_id, PERSON_PALETTE[index % len(PERSON_PALETTE)]
            )
            for index, user in enumerate(ctx.members)
            if user.user_id in ctx.shown_ids
        }

    async def _presence_rows(self, ctx: _Context) -> dict[str, PersonPresence]:
        if not ctx.on(SECTION_PRESENCE):
            return {}
        rows = await _safe(
            "presence", self._presence.list_presence(), list[PersonPresence]()
        )
        return {r.user_id: r for r in rows}

    async def _people(
        self, ctx: _Context, colors: Mapping[str, str]
    ) -> tuple[DashboardPerson, ...]:
        presence = await self._presence_rows(ctx)
        out: list[DashboardPerson] = []
        for user in ctx.shown:
            row = presence.get(user.user_id)
            state = _PRESENCE.get(row.state, "unknown") if row else "unknown"
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
                    status_emoji=(status.emoji or None) if live else None,
                    status_text=(
                        (status.text or None)
                        if live and ctx.settings.show_status_text
                        else None
                    ),
                )
            )
        return tuple(out)

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
        if not ctx.on(SECTION_CALENDAR) or not calendars:
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
            chore = self._chore(ctx, t, lst.name)
            if chore is not None:
                out.append(chore)
        return out

    @staticmethod
    def _chore(ctx: _Context, t: Task, list_name: str) -> DashboardTask | None:
        if t.archived_at is not None or t.due_date is None:
            return None
        done = t.status == TaskStatus.DONE
        if done and local_date(_aware(t.updated_at), ctx.tz) != ctx.today:
            return None
        person_ids = ctx.keep(t.assignees)
        if t.assignees and not person_ids:
            return None  # assigned only to hidden (or departed) people
        return DashboardTask(
            id=t.id,
            title=t.title,
            list_name=list_name,
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

    async def _sticky_slice(self, ctx: _Context) -> tuple[DashboardSticky, ...]:
        if not ctx.on(SECTION_STICKIES):
            return ()
        notes = await _safe("stickies", self._stickies.list(space_id=None), [])
        return tuple(
            DashboardSticky(id=n.id, content=n.content, color=n.color)
            for n in notes
            if n.author not in ctx.hidden_ids
        )[:MAX_STICKIES]

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
        """The lessons ``offset`` days after "today" — today in each
        timetable's own zone, like :meth:`TimetableService.today_for_user`
        (a Berlin plan seen from New York at 20:00 already shows Berlin's
        tomorrow morning). ``date`` on the result is the household day."""
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
                        timetable_name=tt.name,
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
