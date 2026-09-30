"""Timetable service — household school timetables (*Stundenplan*).

Thin orchestration around :class:`AbstractTimetableRepo` and the pure
:mod:`socialhome.domain.timetable` module. Every edit goes through
:meth:`TimetableEditorMixin._mutate`: load → compare the client's
``version`` → apply a domain mutator → prune old overrides → validate →
compare-and-swap save → emit :class:`TimetableSaved`. The same mixin
backs the space-scoped service later, which only swaps the load / store
hooks.

Errors surface as domain exceptions that ``BaseView._iter`` maps:

* ``KeyError``                  → 404 (timetable / entry / override missing)
* ``TimetableValidationError``  → 422 (a ``ValueError``)
* ``TimetableConflictError``    → 409 ``TIMETABLE_CONFLICT`` (stale version)
* ``TimetableOrphanError``      → 409 ``DAY_HAS_ENTRIES`` / ``DAYS_ORPHAN_ENTRIES``
* ``TimetableLimitError``       → 409 ``TIMETABLE_LIMIT``
* ``FeatureDisabledError``      → 403 (``feat_timetable`` off)
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any, Final, Literal
from zoneinfo import ZoneInfo

from ..domain import timetable as td
from ..domain.events import TimetableDeleted, TimetableSaved
from ..domain.timetable import (
    UNSET,
    ResolvedDay,
    ResolvedWeek,
    Timetable,
    TimetableConflictError,
    TimetableLimitError,
    TimetableValidationError,
    TimetableValidity,
)
from ..repositories.timetable_repo import AbstractTimetableRepo
from ..repositories.user_repo import AbstractUserRepo
from ..utils.timezones import DEFAULT_TZ, is_valid_tz
from .bus_publisher import BusPublisherMixin

if TYPE_CHECKING:
    from ..domain.events import DomainEvent
    from ..domain.preferences import HouseholdPreferences
    from ..infrastructure.event_bus import EventBus
    from .preferences_service import PreferencesService

log = logging.getLogger(__name__)

#: Household-wide cap on timetables (one per child is the common case).
MAX_TIMETABLES: Final = 30
#: Upper bound on a single shift, before the domain's midnight check.
_MAX_SHIFT_MINUTES: Final = 24 * 60 - 1
_SECTION: Final = "timetable"
_COPY_SUFFIX: Final = " (copy)"
_DEFAULTS_KEYS: Final = frozenset(td.defaults_to_dict(td.TimetableDefaults()))

Template = Literal["school", "empty"]
_TEMPLATES: Final[frozenset[str]] = frozenset({"school", "empty"})

#: A domain mutator bound to its arguments: ``(tt, now) -> new tt``.
Mutation = Callable[[Timetable, datetime], Timetable]


def _utcnow() -> datetime:
    """The service clock — one seam for "now" and "today" (tests shift it)."""
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def _check_opt_str(value: object, what: str) -> None:
    """Type guard for header strings before they reach the domain."""
    if value is not None and not isinstance(value, str):
        raise TimetableValidationError(f"{what} must be a string")


def _find[T: (td.TimetableEntry, td.TimetableOverride)](
    items: Iterable[T], item_id: str, what: str
) -> T:
    for item in items:
        if item.id == item_id:
            return item
    raise KeyError(f"{what} not found")


class TimetableEditorMixin:
    """Behaviour-only mixin: the compare-and-swap edit loop.

    The consumer supplies the scope-specific hooks — ``_load`` (raise
    ``KeyError`` when missing), ``_store`` (CAS write, ``False`` on a
    lost race) and ``_space_of`` (the ``space_id`` for the emitted
    event, ``None`` for the household) — and ``_emit`` from
    :class:`~socialhome.services.bus_publisher.BusPublisherMixin`. It
    carries no slots so it composes with other slotted mixins.
    """

    __slots__ = ()

    if TYPE_CHECKING:

        async def _emit(self, event: DomainEvent) -> None: ...

        async def _load(self, timetable_id: str) -> Timetable: ...

        async def _store(self, tt: Timetable, *, expected_version: int) -> bool: ...

        async def _space_of(self, timetable_id: str) -> str | None: ...

    @staticmethod
    def today_in(tz: str) -> date:
        """Today's date in ``tz`` (UTC when the zone is unknown)."""
        zone = ZoneInfo(tz) if is_valid_tz(tz) else timezone.utc
        return _utcnow().astimezone(zone).date()

    def _check_not_expired(self, tt: Timetable, ov: td.TimetableOverride) -> None:
        """Refuse an override the next save would prune (422, not silence)."""
        cutoff = self.today_in(tt.tz) - timedelta(days=td.OVERRIDE_RETENTION_DAYS)
        if ov.date < cutoff:
            raise TimetableValidationError(
                f"override date is more than {td.OVERRIDE_RETENTION_DAYS} days "
                "in the past"
            )

    async def _mutate(
        self,
        timetable_id: str,
        fn: Mutation,
        *,
        expected_version: int,
        by: str | None,
    ) -> Timetable:
        tt = await self._load(timetable_id)
        if tt.version != expected_version:
            raise TimetableConflictError(current_version=tt.version)
        new = fn(tt, _utcnow())
        if new.version == tt.version:
            # The mutator found nothing to change; leave the row (and any
            # prunable overrides) for the next real edit.
            return tt
        new = td.prune_overrides(new, self.today_in(new.tz))
        td.validate(new)
        if not await self._store(new, expected_version=expected_version):
            current = await self._load(timetable_id)
            raise TimetableConflictError(current_version=current.version)
        await self._emit(
            TimetableSaved(timetable=new, space_id=await self._space_of(timetable_id))
        )
        return new


class TimetableService(BusPublisherMixin, TimetableEditorMixin):
    """Household timetable operations."""

    __slots__ = ("_repo", "_bus", "_household", "_users")

    def __init__(
        self,
        timetable_repo: AbstractTimetableRepo,
        bus: EventBus | None = None,
        *,
        user_repo: AbstractUserRepo | None = None,
    ) -> None:
        self._repo = timetable_repo
        self._bus = bus
        self._household: PreferencesService | None = None
        self._users = user_repo

    def attach_household_features(self, svc: PreferencesService) -> None:
        """Wire :class:`PreferencesService`: the ``feat_timetable`` gate
        (403 on every call when off) and the household tz default."""
        self._household = svc

    async def _require_enabled(self) -> HouseholdPreferences | None:
        if self._household is None:
            return None
        return await self._household.require_enabled(_SECTION)

    # ── TimetableEditorMixin hooks ───────────────────────────────────────

    async def _load(self, timetable_id: str) -> Timetable:
        tt = await self._repo.get(timetable_id)
        if tt is None:
            raise KeyError("timetable not found")
        return tt

    async def _store(self, tt: Timetable, *, expected_version: int) -> bool:
        return await self._repo.save(tt, expected_version=expected_version)

    async def _space_of(self, timetable_id: str) -> str | None:
        return None

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _active_users(self, user_ids: Sequence[str]) -> set[str]:
        """The subset of ``user_ids`` that are active local users (all of
        them when no user directory is wired)."""
        if self._users is None or not user_ids:
            return set(user_ids)
        found = await self._users.list_by_ids(set(user_ids))
        return {u.user_id for u in found if u.is_active()}

    async def _check_assignees(self, assignees: Sequence[str]) -> None:
        """Every assignee must be an active local user."""
        if set(assignees) - await self._active_users(assignees):
            raise TimetableValidationError("unknown assignee")

    async def _check_capacity(self) -> None:
        # Count-then-insert is not atomic: two concurrent creates at 29 can
        # both pass and land 31. Accepted — the cap bounds a household's
        # clutter, not a security boundary, and one extra row is harmless.
        if await self._repo.count() >= MAX_TIMETABLES:
            raise TimetableLimitError(f"at most {MAX_TIMETABLES} timetables")

    async def _insert(self, tt: Timetable) -> Timetable:
        tt = td.prune_overrides(tt, self.today_in(tt.tz))
        td.validate(tt)
        await self._repo.insert(tt)
        await self._emit(TimetableSaved(timetable=tt))
        return tt

    async def household_today(self) -> date:
        """Today in the household tz — the default date for ``/day``."""
        prefs = await self._require_enabled()
        return self.today_in(prefs.tz if prefs is not None else DEFAULT_TZ)

    # ── Timetables ───────────────────────────────────────────────────────

    async def list_all(self) -> list[Timetable]:
        await self._require_enabled()
        return await self._repo.list_all()

    async def get(self, timetable_id: str) -> Timetable:
        await self._require_enabled()
        return await self._load(timetable_id)

    async def create(
        self,
        *,
        name: str,
        created_by: str,
        template: Template = "school",
        week_start: int = td.WEEK_START_MONDAY,
        days: Sequence[int] = (0, 1, 2, 3, 4),
        tz: str | None = None,
        assignees: Sequence[str] | None = None,
        color: str | None = None,
    ) -> Timetable:
        prefs = await self._require_enabled()
        if not isinstance(template, str) or template not in _TEMPLATES:
            raise TimetableValidationError(f"unknown template {template!r:.40}")
        name = td.normalize_name(name)
        _check_opt_str(color, "color")
        _check_opt_str(tz, "tz")
        await self._check_capacity()
        people = (created_by,) if assignees is None else tuple(dict.fromkeys(assignees))
        await self._check_assignees(people)
        if tz is None:
            tz = prefs.tz if prefs is not None else DEFAULT_TZ
        defaults = td.TimetableDefaults()
        day_set = tuple(sorted(set(days)))
        entries: tuple[td.TimetableEntry, ...] = ()
        if template == "school":
            entries = tuple(
                e
                for wd in day_set
                for e in td.school_template_day(wd, defaults, _new_id)
            )
        now = _utcnow()
        tt = Timetable(
            id=_new_id(),
            name=name,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            week_start=week_start,
            tz=tz,
            color=color,
            days=day_set,
            defaults=defaults,
            entries=entries,
            assignees=people,
            updated_by=created_by,
        )
        return await self._insert(tt)

    async def update(
        self,
        timetable_id: str,
        *,
        version: int,
        by: str,
        name: str | td.Unset = UNSET,
        color: str | None | td.Unset = UNSET,
        week_start: int | td.Unset = UNSET,
        tz: str | td.Unset = UNSET,
        days: Sequence[int] | td.Unset = UNSET,
        assignees: Sequence[str] | td.Unset = UNSET,
        defaults: Mapping[str, Any] | td.Unset = UNSET,
        drop_orphans: bool = False,
    ) -> Timetable:
        """Header edit. ``defaults`` is a partial wire dict merged onto
        the current defaults."""
        await self._require_enabled()
        if not isinstance(color, td.Unset):
            _check_opt_str(color, "color")
        if not isinstance(tz, td.Unset) and not isinstance(tz, str):
            raise TimetableValidationError("tz must be a string")
        if not isinstance(assignees, td.Unset):
            assignees = tuple(dict.fromkeys(assignees))
            await self._check_assignees(assignees)
        if not isinstance(defaults, td.Unset):
            if not isinstance(defaults, Mapping):
                raise TimetableValidationError("defaults must be an object")
            unknown = set(defaults) - _DEFAULTS_KEYS
            if unknown:
                raise TimetableValidationError("defaults: unknown field")
        patch = defaults

        def apply(tt: Timetable, now: datetime) -> Timetable:
            merged: td.TimetableDefaults | td.Unset = UNSET
            if not isinstance(patch, td.Unset):
                merged = td.defaults_from_dict(
                    {**td.defaults_to_dict(tt.defaults), **patch}
                )
            return td.with_header(
                tt,
                now=now,
                by=by,
                name=name,
                color=color,
                week_start=week_start,
                tz=tz,
                days=days,
                defaults=merged,
                assignees=assignees,
                drop_orphans=drop_orphans,
            )

        return await self._mutate(timetable_id, apply, expected_version=version, by=by)

    async def delete(self, timetable_id: str, *, by: str) -> None:
        await self._require_enabled()
        await self._load(timetable_id)
        if not await self._repo.delete(timetable_id):
            raise KeyError("timetable not found")
        log.debug("timetable %s deleted by %s", timetable_id, by)
        await self._emit(TimetableDeleted(timetable_id=timetable_id))

    async def duplicate(
        self, timetable_id: str, *, name: str | None, by: str
    ) -> Timetable:
        """Deep copy with fresh ids; overrides follow their remapped entries."""
        await self._require_enabled()
        src = await self._load(timetable_id)
        if name is None:
            name = src.name[: td.MAX_NAME - len(_COPY_SUFFIX)].rstrip() + _COPY_SUFFIX
        name = td.normalize_name(name)
        await self._check_capacity()
        active = await self._active_users(src.assignees)
        id_map = {e.id: _new_id() for e in src.entries}
        now = _utcnow()
        dup = copy.replace(
            src,
            id=_new_id(),
            name=name,
            created_by=by,
            created_at=now,
            updated_at=now,
            updated_by=by,
            version=1,
            # People who left since the source was made are dropped, not fatal.
            assignees=tuple(a for a in src.assignees if a in active),
            entries=tuple(copy.replace(e, id=id_map[e.id]) for e in src.entries),
            overrides=tuple(
                copy.replace(
                    o,
                    id=_new_id(),
                    entry_id=None if o.entry_id is None else id_map.get(o.entry_id),
                )
                for o in src.overrides
            ),
        )
        return await self._insert(dup)

    # ── Entries ──────────────────────────────────────────────────────────

    async def add_entry(
        self, timetable_id: str, *, version: int, by: str, fields: Mapping[str, Any]
    ) -> Timetable:
        await self._require_enabled()
        td.reject_unknown(fields, td.ENTRY_FIELDS - {"id"}, "entry")
        entry = td.entry_from_dict(fields, id=_new_id())
        return await self._mutate(
            timetable_id,
            lambda tt, now: td.with_entry(tt, entry, now=now, by=by),
            expected_version=version,
            by=by,
        )

    async def update_entry(
        self,
        timetable_id: str,
        entry_id: str,
        *,
        version: int,
        by: str,
        fields: Mapping[str, Any],
    ) -> Timetable:
        await self._require_enabled()

        def apply(tt: Timetable, now: datetime) -> Timetable:
            existing = _find(tt.entries, entry_id, "entry")
            return td.with_entry(tt, td.entry_patch(existing, fields), now=now, by=by)

        return await self._mutate(timetable_id, apply, expected_version=version, by=by)

    async def delete_entry(
        self, timetable_id: str, entry_id: str, *, version: int, by: str
    ) -> Timetable:
        await self._require_enabled()

        def apply(tt: Timetable, now: datetime) -> Timetable:
            _find(tt.entries, entry_id, "entry")
            return td.without_entry(tt, entry_id, now=now, by=by)

        return await self._mutate(timetable_id, apply, expected_version=version, by=by)

    async def replace_entries(
        self, timetable_id: str, *, version: int, by: str, entries: Any
    ) -> Timetable:
        """Replace every entry. Elements carrying the ``id`` of an entry the
        timetable already has keep it (and its overrides); every other
        element gets a minted id — a client can't choose new ids."""
        await self._require_enabled()

        def apply(tt: Timetable, now: datetime) -> Timetable:
            parsed = td.entries_from_list(
                entries,
                id_factory=_new_id,
                keep_ids=frozenset(e.id for e in tt.entries),
            )
            return td.with_entries(tt, parsed, now=now, by=by)

        return await self._mutate(timetable_id, apply, expected_version=version, by=by)

    async def generate_day(
        self,
        timetable_id: str,
        weekday: int,
        *,
        version: int,
        by: str,
        slots: Any,
        replace: bool = False,
    ) -> Timetable:
        await self._require_enabled()
        parsed = td.entries_from_list(
            slots,
            id_factory=_new_id,
            weekday=weekday,
            allow_id=False,
            max_len=td.MAX_ENTRIES_PER_DAY,
        )
        return await self._mutate(
            timetable_id,
            lambda tt, now: td.generate_day(
                tt, weekday, parsed, replace=replace, now=now, by=by
            ),
            expected_version=version,
            by=by,
        )

    async def copy_day(
        self,
        timetable_id: str,
        from_weekday: int,
        *,
        to_weekdays: Sequence[int],
        with_subjects: bool = True,
        replace: bool = False,
        version: int,
        by: str,
    ) -> Timetable:
        await self._require_enabled()
        return await self._mutate(
            timetable_id,
            lambda tt, now: td.copy_day(
                tt,
                from_weekday,
                to_weekdays,
                with_subjects=with_subjects,
                replace=replace,
                id_factory=_new_id,
                now=now,
                by=by,
            ),
            expected_version=version,
            by=by,
        )

    async def shift_after(
        self,
        timetable_id: str,
        weekday: int,
        *,
        from_time: time,
        minutes: int,
        version: int,
        by: str,
    ) -> Timetable:
        await self._require_enabled()
        if abs(minutes) > _MAX_SHIFT_MINUTES:
            raise TimetableValidationError("shift is longer than a day")
        return await self._mutate(
            timetable_id,
            lambda tt, now: td.shift_after(
                tt, weekday, from_time, minutes, now=now, by=by
            ),
            expected_version=version,
            by=by,
        )

    # ── Validity ─────────────────────────────────────────────────────────

    async def set_validity(
        self,
        timetable_id: str,
        *,
        version: int,
        by: str,
        valid_from: date | None,
        valid_until: date | None,
        excluded_weeks: Sequence[date],
    ) -> Timetable:
        await self._require_enabled()
        validity = TimetableValidity(
            valid_from=valid_from,
            valid_until=valid_until,
            excluded_weeks=tuple(excluded_weeks),
        )
        return await self._mutate(
            timetable_id,
            lambda tt, now: td.with_validity(tt, validity, now=now, by=by),
            expected_version=version,
            by=by,
        )

    # ── Overrides ────────────────────────────────────────────────────────

    async def add_override(
        self, timetable_id: str, *, version: int, by: str, fields: Mapping[str, Any]
    ) -> Timetable:
        await self._require_enabled()
        td.reject_unknown(fields, td.OVERRIDE_FIELDS - {"id"}, "override")
        ov = td.override_from_dict(fields, id=_new_id())

        def apply(tt: Timetable, now: datetime) -> Timetable:
            self._check_not_expired(tt, ov)
            return td.with_override(tt, ov, now=now, by=by)

        return await self._mutate(timetable_id, apply, expected_version=version, by=by)

    async def update_override(
        self,
        timetable_id: str,
        override_id: str,
        *,
        version: int,
        by: str,
        fields: Mapping[str, Any],
    ) -> Timetable:
        await self._require_enabled()

        def apply(tt: Timetable, now: datetime) -> Timetable:
            existing = _find(tt.overrides, override_id, "override")
            patched = td.override_patch(existing, fields)
            self._check_not_expired(tt, patched)
            return td.with_override(tt, patched, now=now, by=by)

        return await self._mutate(timetable_id, apply, expected_version=version, by=by)

    async def delete_override(
        self, timetable_id: str, override_id: str, *, version: int, by: str
    ) -> Timetable:
        await self._require_enabled()

        def apply(tt: Timetable, now: datetime) -> Timetable:
            _find(tt.overrides, override_id, "override")
            return td.without_override(tt, override_id, now=now, by=by)

        return await self._mutate(timetable_id, apply, expected_version=version, by=by)

    async def clear_week(
        self, timetable_id: str, any_date: date, *, version: int, by: str
    ) -> Timetable:
        await self._require_enabled()
        return await self._mutate(
            timetable_id,
            lambda tt, now: td.clear_week(tt, any_date, now=now, by=by),
            expected_version=version,
            by=by,
        )

    # ── Resolved views ───────────────────────────────────────────────────

    async def resolve_week(self, timetable_id: str, any_date: date) -> ResolvedWeek:
        return td.resolve_week(await self.get(timetable_id), any_date)

    async def resolve_day(self, timetable_id: str, d: date) -> ResolvedDay:
        return td.resolve_day(await self.get(timetable_id), d)

    async def active_for_user(self, user_id: str, on: date) -> list[Timetable]:
        """Timetables assigned to ``user_id`` that are in effect on ``on``."""
        return [
            tt
            for tt in await self.list_all()
            if user_id in tt.assignees and td.resolve_day(tt, on).valid
        ]
