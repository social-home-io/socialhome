"""Timetable service — school timetables (*Stundenplan*).

Thin orchestration around the timetable repos and the pure
:mod:`socialhome.domain.timetable` module. Every edit goes through
:meth:`TimetableEditorMixin._mutate`: load → compare the client's
``version`` → apply a domain mutator → prune old overrides → validate →
compare-and-swap save → emit :class:`TimetableSaved`.

Two scopes share that editing surface (header, entries, day tools,
validity, overrides, week clear, resolved views) through the mixin, and
differ only in their hooks:

* :class:`TimetableService` — the household's timetables, gated on
  ``feat_timetable``, with per-person assignees.
* :class:`SpaceTimetableService` — a space's shared timetables (a class
  plan). :meth:`SpaceTimetableService.scope` hands out a
  :class:`SpaceTimetableScope` bound to one space and one caller; every
  call on it re-checks that the caller is a member (reads) or an
  owner / admin (writes) and that the space has its ``timetable``
  feature on. No assignees; ids are owner-bound (they federate).

Errors surface as domain exceptions that ``BaseView._iter`` maps:

* ``KeyError``                  → 404 (timetable / entry / override missing)
* ``TimetableValidationError``  → 422 (a ``ValueError``)
* ``TimetableConflictError``    → 409 ``TIMETABLE_CONFLICT`` (stale version)
* ``TimetableOrphanError``      → 409 ``DAY_HAS_ENTRIES`` / ``DAYS_ORPHAN_ENTRIES``
* ``TimetableLimitError``       → 409 ``TIMETABLE_LIMIT``
* ``FeatureDisabledError``      → 403 (``feat_timetable`` / space feature off)
* ``SpacePermissionError``      → 403 (not a member / not a space admin)
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any, Final, Literal

from ..domain import timetable as td
from ..domain.events import TimetableDeleted, TimetableSaved
from ..domain.preferences import FeatureDisabledError
from ..domain.space import SETTINGS_AUTHORITY_ROLES, SpacePermissionError
from ..domain.timetable import (
    UNSET,
    ResolvedDay,
    ResolvedWeek,
    Timetable,
    TimetableConflictError,
    TimetableLimitError,
    TimetableValidationError,
    TimetableValidity,
    TodayTimetable,
)
from ..federation.owner_bound_id import SPACE_TIMETABLE_KIND, mint_owner_bound_id
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.timetable_repo import (
    AbstractSpaceTimetableRepo,
    AbstractTimetableRepo,
)
from ..repositories.user_repo import AbstractUserRepo
from ..utils.timezones import DEFAULT_TZ, is_valid_tz, local_date
from .bus_publisher import BusPublisherMixin
from .space_member_guard import SpaceMemberGuardMixin
from .user_preferences import MAX_TIMETABLE_HOME_PINS

if TYPE_CHECKING:
    from ..domain.events import DomainEvent
    from ..domain.preferences import HouseholdPreferences
    from ..domain.space import Space
    from ..infrastructure.event_bus import EventBus
    from .preferences_service import PreferencesService

log = logging.getLogger(__name__)

#: Household-wide cap on timetables (one per child is the common case).
MAX_TIMETABLES: Final = 30
#: Per-space cap (a class space holds one plan, maybe a few variants).
MAX_SPACE_TIMETABLES: Final = 10
#: Timetables on the home screen's "today" card (one per child).
MAX_TODAY_TIMETABLES: Final = 3
#: Upper bound on a single shift, before the domain's midnight check.
_MAX_SHIFT_MINUTES: Final = 24 * 60 - 1
_SECTION: Final = "timetable"
#: The :class:`~socialhome.domain.space.SpaceFeatures` field gating spaces.
_SPACE_FEATURE: Final = "timetable"
_SPACE_EDITORS: Final = SETTINGS_AUTHORITY_ROLES
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


def _check_template(template: object) -> None:
    if not isinstance(template, str) or template not in _TEMPLATES:
        raise TimetableValidationError(f"unknown template {template!r:.40}")


def _new_timetable(
    *,
    id: str,
    name: str,
    created_by: str,
    template: Template,
    week_start: int,
    days: Sequence[int],
    tz: str,
    color: str | None,
    assignees: tuple[str, ...],
) -> Timetable:
    """A fresh version-1 timetable, school-templated or empty."""
    defaults = td.TimetableDefaults()
    day_set = tuple(sorted(set(days)))
    entries: tuple[td.TimetableEntry, ...] = ()
    if template == "school":
        entries = tuple(
            e for wd in day_set for e in td.school_template_day(wd, defaults, _new_id)
        )
    now = _utcnow()
    return Timetable(
        id=id,
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
        assignees=assignees,
        updated_by=created_by,
    )


def _copy_of(
    src: Timetable,
    *,
    id: str,
    name: str,
    by: str,
    assignees: tuple[str, ...],
) -> Timetable:
    """Deep copy with fresh entry / override ids; overrides follow their
    remapped entries."""
    id_map = {e.id: _new_id() for e in src.entries}
    now = _utcnow()
    return copy.replace(
        src,
        id=id,
        name=name,
        created_by=by,
        created_at=now,
        updated_at=now,
        updated_by=by,
        version=1,
        assignees=assignees,
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


def _copy_name(src: Timetable, name: str | None) -> str:
    if name is None:
        name = src.name[: td.MAX_NAME - len(_COPY_SUFFIX)].rstrip() + _COPY_SUFFIX
    return td.normalize_name(name)


def _find[T: (td.TimetableEntry, td.TimetableOverride)](
    items: Iterable[T], item_id: str, what: str
) -> T:
    for item in items:
        if item.id == item_id:
            return item
    raise KeyError(f"{what} not found")


class TimetableEditorMixin:
    """Behaviour-only mixin: the editing surface both scopes share.

    Owns the compare-and-swap edit loop (:meth:`_mutate`) and every edit
    that runs through it — header, entries, day tools, validity,
    overrides, week clear — plus ``get`` and the resolved views. The
    consumer supplies the scope-specific hooks:

    * ``_guard(write=…)`` — the scope's gate, awaited first on every
      call (the household feature toggle; the space membership / admin /
      feature check);
    * ``_check_assignees`` — which assignees the scope accepts;
    * ``_load`` (raise ``KeyError`` when missing or out of scope),
      ``_store`` (CAS write, ``False`` on a lost race) and ``_space_of``
      (the ``space_id`` for the emitted event, ``None`` for the household);
    * ``_emit`` from :class:`~socialhome.services.bus_publisher.BusPublisherMixin`.

    It carries no slots so it composes with other slotted mixins.
    """

    __slots__ = ()

    if TYPE_CHECKING:

        async def _emit(self, event: DomainEvent) -> None: ...

        async def _guard(self, *, write: bool) -> object: ...

        async def _check_assignees(self, assignees: Sequence[str]) -> None: ...

        async def _load(self, timetable_id: str) -> Timetable: ...

        async def _store(self, tt: Timetable, *, expected_version: int) -> bool: ...

        async def _space_of(self, timetable_id: str) -> str | None: ...

    @staticmethod
    def today_in(tz: str) -> date:
        """Today's date in ``tz`` (UTC when the zone is unknown)."""
        return local_date(_utcnow(), tz)

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

    # ── Reads ────────────────────────────────────────────────────────────

    async def get(self, timetable_id: str) -> Timetable:
        await self._guard(write=False)
        return await self._load(timetable_id)

    async def resolve_week(self, timetable_id: str, any_date: date) -> ResolvedWeek:
        return td.resolve_week(await self.get(timetable_id), any_date)

    async def resolve_day(self, timetable_id: str, d: date) -> ResolvedDay:
        return td.resolve_day(await self.get(timetable_id), d)

    # ── Header ───────────────────────────────────────────────────────────

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
        await self._guard(write=True)
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

    # ── Entries ──────────────────────────────────────────────────────────

    async def add_entry(
        self, timetable_id: str, *, version: int, by: str, fields: Mapping[str, Any]
    ) -> Timetable:
        await self._guard(write=True)
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
        await self._guard(write=True)

        def apply(tt: Timetable, now: datetime) -> Timetable:
            existing = _find(tt.entries, entry_id, "entry")
            return td.with_entry(tt, td.entry_patch(existing, fields), now=now, by=by)

        return await self._mutate(timetable_id, apply, expected_version=version, by=by)

    async def delete_entry(
        self, timetable_id: str, entry_id: str, *, version: int, by: str
    ) -> Timetable:
        await self._guard(write=True)

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
        await self._guard(write=True)

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
        await self._guard(write=True)
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
        await self._guard(write=True)
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
        await self._guard(write=True)
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
        await self._guard(write=True)
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
        await self._guard(write=True)
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
        await self._guard(write=True)

        def apply(tt: Timetable, now: datetime) -> Timetable:
            existing = _find(tt.overrides, override_id, "override")
            patched = td.override_patch(existing, fields)
            self._check_not_expired(tt, patched)
            return td.with_override(tt, patched, now=now, by=by)

        return await self._mutate(timetable_id, apply, expected_version=version, by=by)

    async def delete_override(
        self, timetable_id: str, override_id: str, *, version: int, by: str
    ) -> Timetable:
        await self._guard(write=True)

        def apply(tt: Timetable, now: datetime) -> Timetable:
            _find(tt.overrides, override_id, "override")
            return td.without_override(tt, override_id, now=now, by=by)

        return await self._mutate(timetable_id, apply, expected_version=version, by=by)

    async def clear_week(
        self, timetable_id: str, any_date: date, *, version: int, by: str
    ) -> Timetable:
        await self._guard(write=True)
        return await self._mutate(
            timetable_id,
            lambda tt, now: td.clear_week(tt, any_date, now=now, by=by),
            expected_version=version,
            by=by,
        )


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

    async def _guard(self, *, write: bool) -> HouseholdPreferences | None:
        return await self._require_enabled()

    async def _load(self, timetable_id: str) -> Timetable:
        tt = await self._repo.get(timetable_id)
        if tt is None:
            raise KeyError("timetable not found")
        return tt

    async def _store(self, tt: Timetable, *, expected_version: int) -> bool:
        return await self._repo.save(tt, expected_version=expected_version)

    async def _space_of(self, timetable_id: str) -> str | None:
        return None

    async def _check_assignees(self, assignees: Sequence[str]) -> None:
        """Every assignee must be an active local user."""
        if set(assignees) - await self._active_users(assignees):
            raise TimetableValidationError("unknown assignee")

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _active_users(self, user_ids: Sequence[str]) -> set[str]:
        """The subset of ``user_ids`` that are active local users (all of
        them when no user directory is wired)."""
        if self._users is None or not user_ids:
            return set(user_ids)
        found = await self._users.list_by_ids(set(user_ids))
        return {u.user_id for u in found if u.is_active()}

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
        _check_template(template)
        name = td.normalize_name(name)
        _check_opt_str(color, "color")
        _check_opt_str(tz, "tz")
        await self._check_capacity()
        people = (created_by,) if assignees is None else tuple(dict.fromkeys(assignees))
        await self._check_assignees(people)
        if tz is None:
            tz = prefs.tz if prefs is not None else DEFAULT_TZ
        tt = _new_timetable(
            id=_new_id(),
            name=name,
            created_by=created_by,
            template=template,
            week_start=week_start,
            days=days,
            tz=tz,
            color=color,
            assignees=people,
        )
        return await self._insert(tt)

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
        name = _copy_name(src, name)
        await self._check_capacity()
        active = await self._active_users(src.assignees)
        dup = _copy_of(
            src,
            id=_new_id(),
            name=name,
            by=by,
            # People who left since the source was made are dropped, not fatal.
            assignees=tuple(a for a in src.assignees if a in active),
        )
        return await self._insert(dup)

    # ── Resolved views ───────────────────────────────────────────────────

    async def active_for_user(self, user_id: str, on: date) -> list[Timetable]:
        """Timetables assigned to ``user_id`` that are in effect on ``on``."""
        return [
            tt
            for tt in await self.list_all()
            if user_id in tt.assignees and td.resolve_day(tt, on).valid
        ]

    async def today_for_user(
        self,
        user_id: str,
        now: datetime | None = None,
        *,
        extra: Sequence[Timetable] = (),
    ) -> tuple[TodayTimetable, ...]:
        """Today's lessons of every timetable assigned to ``user_id`` that
        is in effect today — "today" in each timetable's own zone. A
        timetable without a filled lesson today (only untitled template
        slots, or none at all) is left out before the cap is applied, and
        one that fails to resolve is logged and skipped.

        The home screen's read: an empty tuple (never
        :class:`FeatureDisabledError`) when ``feat_timetable`` is off.
        ``extra`` appends timetables the caller found elsewhere (the
        user's pinned space timetables) after the assigned ones;
        duplicates by id are dropped. At most :data:`MAX_TODAY_TIMETABLES`.
        """
        if self._household is not None:
            prefs = await self._household.get_household()
            if not prefs.is_enabled(_SECTION):
                return ()
        now = now or _utcnow()
        assigned = [tt for tt in await self._repo.list_all() if user_id in tt.assignees]
        out: list[TodayTimetable] = []
        seen: set[str] = set()
        for tt in (*assigned, *extra):
            if tt.id in seen:
                continue
            seen.add(tt.id)
            try:
                today = td.today_timetable(tt, local_date(now, tt.tz))
            except Exception as exc:  # one bad row never empties the slice
                log.warning("timetable %s: today's lessons failed: %s", tt.id, exc)
                continue
            if today is None or not td.has_lessons(today):
                continue  # not in effect, or no filled lesson today
            out.append(today)
            if len(out) >= MAX_TODAY_TIMETABLES:
                break
        return tuple(out)


# ─── Space timetables ────────────────────────────────────────────────────


class SpaceTimetableService(SpaceMemberGuardMixin):
    """A space's shared timetables — members read, owners / admins edit.

    Stateless apart from its repos: :meth:`scope` binds one space and one
    caller into a :class:`SpaceTimetableScope`, which carries the same
    editing surface as the household :class:`TimetableService` (through
    :class:`TimetableEditorMixin`) and re-checks access on every call.
    """

    __slots__ = ("_repo", "_spaces", "_bus")

    def __init__(
        self,
        space_timetable_repo: AbstractSpaceTimetableRepo,
        space_repo: AbstractSpaceRepo,
        bus: EventBus | None = None,
    ) -> None:
        self._repo = space_timetable_repo
        self._spaces = space_repo
        self._bus = bus

    def scope(self, space_id: str, actor_user_id: str) -> SpaceTimetableScope:
        """The timetables of ``space_id`` as seen by ``actor_user_id``."""
        return SpaceTimetableScope(self, space_id, actor_user_id)

    async def require_access(
        self, space_id: str, user_id: str, *, write: bool
    ) -> Space:
        """Membership (reads: any role, followers included — they read
        space content; writes: owner / admin), then the space's
        ``timetable`` feature. Raises :class:`SpacePermissionError` /
        :class:`FeatureDisabledError`."""
        if write:
            await self._role_or_raise(
                space_id, user_id, _SPACE_EDITORS, message="admin or owner required"
            )
        else:
            await self._member_or_raise(space_id, user_id)
        space = await self._spaces.get(space_id)
        if space is None or not space.features.timetable:
            raise FeatureDisabledError(f"space:{_SPACE_FEATURE}")
        return space

    async def pinned_for_user(
        self, user_id: str, pins: Sequence[str]
    ) -> list[Timetable]:
        """The user's home-pinned space timetables, in pin order.

        A pin into a space the user no longer belongs to, or whose
        timetable feature is off, is silently ignored — as are unknown and
        deleted ids. At most :data:`MAX_TIMETABLE_HOME_PINS` pins are read.
        """
        wanted = list(dict.fromkeys(pins))[:MAX_TIMETABLE_HOME_PINS]
        if not wanted:
            return []
        found = {tt.id: (sid, tt) for sid, tt in await self._repo.list_by_ids(wanted)}
        allowed: dict[str, bool] = {}
        out: list[Timetable] = []
        for tid in wanted:
            hit = found.get(tid)
            if hit is None:
                continue
            space_id, tt = hit
            if space_id not in allowed:
                allowed[space_id] = await self._may_read(space_id, user_id)
            if allowed[space_id]:
                out.append(tt)
        return out

    async def _may_read(self, space_id: str, user_id: str) -> bool:
        if await self._spaces.get_member(space_id, user_id) is None:
            return False
        space = await self._spaces.get(space_id)
        return space is not None and space.features.timetable


class SpaceTimetableScope(BusPublisherMixin, TimetableEditorMixin):
    """One space's timetables, for one caller.

    Every call re-runs :meth:`SpaceTimetableService.require_access` (reads
    need a membership, writes an owner / admin seat, both the space
    feature), and an edit may only be recorded as the caller's own
    (``by`` / ``created_by`` must be the caller). Rows of another space
    are "not found".
    """

    __slots__ = ("_svc", "_space_id", "_actor", "_bus")

    def __init__(
        self, svc: SpaceTimetableService, space_id: str, actor_user_id: str
    ) -> None:
        self._svc = svc
        self._space_id = space_id
        self._actor = actor_user_id
        self._bus = svc._bus

    # ── TimetableEditorMixin hooks ───────────────────────────────────────

    async def _guard(self, *, write: bool) -> Space:
        return await self._svc.require_access(self._space_id, self._actor, write=write)

    def _check_actor(self, by: str | None) -> None:
        if by != self._actor:
            raise SpacePermissionError("an edit is recorded as its caller")

    async def _check_assignees(self, assignees: Sequence[str]) -> None:
        if assignees:
            raise TimetableValidationError("space timetables have no assignees")

    async def _load(self, timetable_id: str) -> Timetable:
        got = await self._svc._repo.get(timetable_id)
        if got is None or got[0] != self._space_id:
            raise KeyError("timetable not found")
        return got[1]

    async def _store(self, tt: Timetable, *, expected_version: int) -> bool:
        return await self._svc._repo.save(
            tt, space_id=self._space_id, expected_version=expected_version
        )

    async def _space_of(self, timetable_id: str) -> str | None:
        return self._space_id

    async def _mutate(
        self,
        timetable_id: str,
        fn: Mutation,
        *,
        expected_version: int,
        by: str | None,
    ) -> Timetable:
        self._check_actor(by)
        return await TimetableEditorMixin._mutate(
            self, timetable_id, fn, expected_version=expected_version, by=by
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _check_capacity(self) -> None:
        # Same non-atomic count-then-insert as the household cap: a race
        # can land one extra row, which bounds clutter, not security.
        if await self._svc._repo.count_in_space(self._space_id) >= (
            MAX_SPACE_TIMETABLES
        ):
            raise TimetableLimitError(f"at most {MAX_SPACE_TIMETABLES} timetables")

    def _mint_id(self, owner_user_id: str) -> str:
        return mint_owner_bound_id(
            SPACE_TIMETABLE_KIND, space_id=self._space_id, owner_user_id=owner_user_id
        )

    async def _insert(self, tt: Timetable) -> Timetable:
        tt = td.prune_overrides(tt, self.today_in(tt.tz))
        td.validate(tt)
        if not await self._svc._repo.insert(tt, space_id=self._space_id):
            raise KeyError("space not found")
        await self._emit(TimetableSaved(timetable=tt, space_id=self._space_id))
        return tt

    # ── Timetables ───────────────────────────────────────────────────────

    async def list_all(self) -> list[Timetable]:
        await self._guard(write=False)
        return await self._svc._repo.list_by_space(self._space_id)

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
        space = await self._guard(write=True)
        self._check_actor(created_by)
        _check_template(template)
        name = td.normalize_name(name)
        _check_opt_str(color, "color")
        _check_opt_str(tz, "tz")
        await self._check_assignees(tuple(assignees or ()))
        await self._check_capacity()
        if tz is None:
            tz = space.tz if space.tz and is_valid_tz(space.tz) else DEFAULT_TZ
        tt = _new_timetable(
            id=self._mint_id(created_by),
            name=name,
            created_by=created_by,
            template=template,
            week_start=week_start,
            days=days,
            tz=tz,
            color=color,
            assignees=(),
        )
        return await self._insert(tt)

    async def delete(self, timetable_id: str, *, by: str) -> None:
        """Tombstone the timetable — federated deletes can't resurrect it."""
        await self._guard(write=True)
        self._check_actor(by)
        tt = await self._load(timetable_id)
        if not await self._svc._repo.soft_delete(
            timetable_id, space_id=self._space_id, at=_utcnow()
        ):
            raise KeyError("timetable not found")
        log.debug(
            "space %s: timetable %s deleted by %s", self._space_id, timetable_id, by
        )
        await self._emit(
            TimetableDeleted(
                timetable_id=timetable_id,
                space_id=self._space_id,
                deleted_by=by,
                created_by=tt.created_by,
            )
        )

    async def duplicate(
        self, timetable_id: str, *, name: str | None, by: str
    ) -> Timetable:
        """Deep copy into the same space, owned (and id-bound) to ``by``."""
        await self._guard(write=True)
        self._check_actor(by)
        src = await self._load(timetable_id)
        name = _copy_name(src, name)
        await self._check_capacity()
        dup = _copy_of(src, id=self._mint_id(by), name=name, by=by, assignees=())
        return await self._insert(dup)
