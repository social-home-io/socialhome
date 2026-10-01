"""Timetable routes — ``/api/timetables/*`` (household school timetables)
and ``/api/spaces/{space_id}/timetables/*`` (a space's shared timetables).

Thin :class:`BaseView` handlers over :class:`TimetableService` and, for a
space, :class:`SpaceTimetableScope`. The space views reuse every
household handler and only swap :meth:`_TimetableView._svc`: membership
first, then the space's ``timetable`` feature, then the space-scoped
service (which re-checks both, and refuses a non-admin write with 403).
There is no space ``/day`` view — that is the household's "my lessons".

Every
mutation except create / delete carries the client's ``version`` (body,
or ``?version=N`` on DELETE) for the service's compare-and-swap, and
answers with the full timetable: the wire dict plus ``active_this_week``
and ``valid_today`` computed in the timetable's own tz.

Errors are mapped centrally by ``BaseView._iter``: 404 unknown id,
422 ``TimetableValidationError``, 409 ``TIMETABLE_CONFLICT`` (with
``current_version``) / ``DAY_HAS_ENTRIES`` / ``DAYS_ORPHAN_ENTRIES``
(with ``count``) / ``TIMETABLE_LIMIT``, 403 when ``feat_timetable`` is
off; for a space, 403 for a non-member / non-admin write.
"""

from __future__ import annotations

import re
from typing import Any

from aiohttp import web

from ..app_keys import (
    space_repo_key,
    space_timetable_service_key,
    timetable_service_key,
)
from ..domain import timetable as td
from ..domain.space import SpacePermissionError
from ..domain.timetable import Timetable, TimetableValidationError
from ..services.timetable_service import (
    SpaceTimetableScope,
    TimetableEditorMixin,
    TimetableService,
)
from .base import BaseView

#: What a handler edits through: the household service or a space scope.
#: Both expose the same editing surface (:class:`TimetableEditorMixin`)
#: plus ``list_all`` / ``create`` / ``delete`` / ``duplicate``.
Editor = TimetableService | SpaceTimetableScope

_SECTION = "timetable"
#: ``?version=N`` — ASCII digits only (``str.isdigit`` admits "١" / "²").
_VERSION_QUERY_RE = re.compile(r"[0-9]{1,10}")

#: Header fields ``PATCH /api/timetables/{id}`` accepts (besides ``version``).
_HEADER_FIELDS = frozenset(
    {
        "name",
        "color",
        "week_start",
        "tz",
        "days",
        "assignees",
        "defaults",
        "drop_orphans",
    }
)


# ─── Parsing helpers (request → typed values; errors are 422) ────────────


def _version(value: Any) -> int:
    version = td.parse_int(value, "version")
    if version < 1:
        raise TimetableValidationError("version must be a positive integer")
    return version


def _flag(body: dict, key: str, default: bool) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise TimetableValidationError(f"{key} must be a boolean")
    return value


def _fields(body: dict) -> dict:
    """The body minus the CAS ``version`` — an entry / override patch."""
    return {k: v for k, v in body.items() if k != "version"}


def _view(svc: TimetableEditorMixin, tt: Timetable) -> dict:
    return td.timetable_view_dict(tt, svc.today_in(tt.tz))


class _TimetableView(BaseView):
    """Shared plumbing: feature gate, service, body/version, response."""

    async def _household(self) -> TimetableService:
        self.user  # auth check
        await self.require_household_feature(_SECTION)
        return self.svc(timetable_service_key)

    async def _svc(self) -> Editor:
        return await self._household()

    async def _body(self, *, optional: bool = False) -> dict:
        if optional and not self.request.can_read_body:
            return {}
        body = await self.body()
        if not isinstance(body, dict):
            raise TimetableValidationError("body must be a JSON object")
        return body

    async def _delete_version(self) -> int:
        """``?version=N`` (preferred) or ``{"version": N}`` in the body."""
        raw = self.request.query.get("version")
        if raw is not None:
            if _VERSION_QUERY_RE.fullmatch(raw) is None:
                raise TimetableValidationError("version must be a positive integer")
            return _version(int(raw))
        body = await self._body(optional=True)
        return _version(body.get("version"))

    def _weekday(self) -> int:
        return int(self.match("weekday"))  # the route regex pins 0..6

    def _tt(self, svc: Editor, tt: Timetable, status: int = 200):
        return self._json({"timetable": _view(svc, tt)}, status=status)


# ─── Collection + today ──────────────────────────────────────────────────


class TimetableCollectionView(_TimetableView):
    """``GET /api/timetables`` — list; ``POST`` — create (201)."""

    async def get(self) -> web.Response:
        svc = await self._svc()
        items = await svc.list_all()
        return self._json({"timetables": [_view(svc, tt) for tt in items]})

    async def post(self) -> web.Response:
        svc = await self._svc()
        body = await self._body()
        kwargs: dict[str, Any] = {}
        if "week_start" in body:
            kwargs["week_start"] = td.parse_int(body["week_start"], "week_start")
        if "days" in body:
            kwargs["days"] = td.parse_weekdays(body["days"], "days")
        if body.get("assignees") is not None:
            kwargs["assignees"] = td.parse_user_ids(body["assignees"], "assignees")
        tt = await svc.create(
            name=body.get("name", ""),
            created_by=self.user.user_id,
            template=body.get("template", "school"),
            tz=body.get("tz"),
            color=body.get("color"),
            **kwargs,
        )
        return self._tt(svc, tt, status=201)


class TimetableDayView(_TimetableView):
    """``GET /api/timetables/day?date=YYYY-MM-DD`` — the caller's lessons.

    Only timetables the caller is assigned to and that are in effect that
    day; ``date`` defaults to today in the household tz.
    """

    async def get(self) -> web.Response:
        svc = await self._household()
        raw = self.request.query.get("date")
        on = td.parse_date(raw, "date") if raw else await svc.household_today()
        active = await svc.active_for_user(self.user.user_id, on)
        return self._json(
            {
                "date": on.isoformat(),
                "timetables": [
                    {
                        "timetable_id": tt.id,
                        "name": tt.name,
                        "color": tt.color,
                        "lessons": td.resolved_day_to_dict(td.resolve_day(tt, on))[
                            "lessons"
                        ],
                    }
                    for tt in active
                ],
            }
        )


# ─── One timetable ───────────────────────────────────────────────────────


class TimetableDetailView(_TimetableView):
    """``GET`` / ``PATCH`` (header) / ``DELETE /api/timetables/{id}``."""

    async def get(self) -> web.Response:
        svc = await self._svc()
        return self._tt(svc, await svc.get(self.match("id")))

    async def patch(self) -> web.Response:
        svc = await self._svc()
        body = await self._body()
        unknown = set(body) - _HEADER_FIELDS - {"version"}
        if unknown:
            raise TimetableValidationError("unknown field in timetable patch")
        kwargs: dict[str, Any] = {}
        for key in ("name", "color", "tz", "defaults"):
            if key in body:
                kwargs[key] = body[key]
        if "week_start" in body:
            kwargs["week_start"] = td.parse_int(body["week_start"], "week_start")
        if "days" in body:
            kwargs["days"] = td.parse_weekdays(body["days"], "days")
        if "assignees" in body:
            kwargs["assignees"] = td.parse_user_ids(body["assignees"], "assignees")
        tt = await svc.update(
            self.match("id"),
            version=_version(body.get("version")),
            by=self.user.user_id,
            drop_orphans=_flag(body, "drop_orphans", False),
            **kwargs,
        )
        return self._tt(svc, tt)

    async def delete(self) -> web.Response:
        svc = await self._svc()
        await svc.delete(self.match("id"), by=self.user.user_id)
        return self._json({"ok": True})


class TimetableDuplicateView(_TimetableView):
    """``POST /api/timetables/{id}/duplicate`` — ``{name?}`` (body optional) → 201."""

    async def post(self) -> web.Response:
        svc = await self._svc()
        body = await self._body(optional=True)
        tt = await svc.duplicate(
            self.match("id"), name=body.get("name"), by=self.user.user_id
        )
        return self._tt(svc, tt, status=201)


# ─── Day tools ───────────────────────────────────────────────────────────


class TimetableDayGenerateView(_TimetableView):
    """``POST …/days/{weekday}/generate`` — ``{version, slots, replace?}``."""

    async def post(self) -> web.Response:
        svc = await self._svc()
        body = await self._body()
        tt = await svc.generate_day(
            self.match("id"),
            self._weekday(),
            version=_version(body.get("version")),
            by=self.user.user_id,
            slots=body.get("slots"),
            replace=_flag(body, "replace", False),
        )
        return self._tt(svc, tt)


class TimetableDayCopyView(_TimetableView):
    """``POST …/days/{weekday}/copy`` — ``{version, to, with_subjects?, replace?}``."""

    async def post(self) -> web.Response:
        svc = await self._svc()
        body = await self._body()
        tt = await svc.copy_day(
            self.match("id"),
            self._weekday(),
            to_weekdays=td.parse_weekdays(body.get("to"), "to"),
            with_subjects=_flag(body, "with_subjects", True),
            replace=_flag(body, "replace", False),
            version=_version(body.get("version")),
            by=self.user.user_id,
        )
        return self._tt(svc, tt)


class TimetableDayShiftView(_TimetableView):
    """``POST …/days/{weekday}/shift`` — ``{version, from: "HH:MM", minutes}``."""

    async def post(self) -> web.Response:
        svc = await self._svc()
        body = await self._body()
        tt = await svc.shift_after(
            self.match("id"),
            self._weekday(),
            from_time=td.parse_hhmm(body.get("from"), "from"),
            minutes=td.parse_int(body.get("minutes"), "minutes"),
            version=_version(body.get("version")),
            by=self.user.user_id,
        )
        return self._tt(svc, tt)


# ─── Entries ─────────────────────────────────────────────────────────────


class TimetableEntryCollectionView(_TimetableView):
    """``POST …/entries`` — add one; ``PUT`` — replace all ``{version, entries}``."""

    async def post(self) -> web.Response:
        svc = await self._svc()
        body = await self._body()
        tt = await svc.add_entry(
            self.match("id"),
            version=_version(body.get("version")),
            by=self.user.user_id,
            fields=_fields(body),
        )
        return self._tt(svc, tt)

    async def put(self) -> web.Response:
        svc = await self._svc()
        body = await self._body()
        tt = await svc.replace_entries(
            self.match("id"),
            version=_version(body.get("version")),
            by=self.user.user_id,
            entries=body.get("entries"),
        )
        return self._tt(svc, tt)


class TimetableEntryDetailView(_TimetableView):
    """``PATCH`` / ``DELETE …/entries/{entry_id}``."""

    async def patch(self) -> web.Response:
        svc = await self._svc()
        body = await self._body()
        tt = await svc.update_entry(
            self.match("id"),
            self.match("entry_id"),
            version=_version(body.get("version")),
            by=self.user.user_id,
            fields=_fields(body),
        )
        return self._tt(svc, tt)

    async def delete(self) -> web.Response:
        svc = await self._svc()
        tt = await svc.delete_entry(
            self.match("id"),
            self.match("entry_id"),
            version=await self._delete_version(),
            by=self.user.user_id,
        )
        return self._tt(svc, tt)


# ─── Validity + weeks ────────────────────────────────────────────────────


class TimetableValidityView(_TimetableView):
    """``PUT …/validity`` — ``{version, valid_from, valid_until, excluded_weeks}``."""

    async def put(self) -> web.Response:
        svc = await self._svc()
        body = await self._body()
        validity = td.validity_from_dict(body)
        tt = await svc.set_validity(
            self.match("id"),
            version=_version(body.get("version")),
            by=self.user.user_id,
            valid_from=validity.valid_from,
            valid_until=validity.valid_until,
            excluded_weeks=validity.excluded_weeks,
        )
        return self._tt(svc, tt)


class TimetableWeekView(_TimetableView):
    """``GET …/weeks/{date}`` — the resolved week containing ``date``."""

    async def get(self) -> web.Response:
        svc = await self._svc()
        week = await svc.resolve_week(
            self.match("id"), td.parse_date(self.match("date"), "date")
        )
        return self._json({"week": td.resolved_week_to_dict(week)})


class TimetableWeekOverridesView(_TimetableView):
    """``DELETE …/weeks/{date}/overrides?version=N`` — clear that week."""

    async def delete(self) -> web.Response:
        svc = await self._svc()
        tt = await svc.clear_week(
            self.match("id"),
            td.parse_date(self.match("date"), "date"),
            version=await self._delete_version(),
            by=self.user.user_id,
        )
        return self._tt(svc, tt)


# ─── Overrides ───────────────────────────────────────────────────────────


class TimetableOverrideCollectionView(_TimetableView):
    """``POST …/overrides`` — add a cancel / replace / add override."""

    async def post(self) -> web.Response:
        svc = await self._svc()
        body = await self._body()
        tt = await svc.add_override(
            self.match("id"),
            version=_version(body.get("version")),
            by=self.user.user_id,
            fields=_fields(body),
        )
        return self._tt(svc, tt)


class TimetableOverrideDetailView(_TimetableView):
    """``PATCH`` / ``DELETE …/overrides/{override_id}``."""

    async def patch(self) -> web.Response:
        svc = await self._svc()
        body = await self._body()
        tt = await svc.update_override(
            self.match("id"),
            self.match("override_id"),
            version=_version(body.get("version")),
            by=self.user.user_id,
            fields=_fields(body),
        )
        return self._tt(svc, tt)

    async def delete(self) -> web.Response:
        svc = await self._svc()
        tt = await svc.delete_override(
            self.match("id"),
            self.match("override_id"),
            version=await self._delete_version(),
            by=self.user.user_id,
        )
        return self._tt(svc, tt)


# ─── Space timetables ────────────────────────────────────────────────────


class _SpaceTimetablesBase(_TimetableView):
    """``/api/spaces/{space_id}/timetables…`` — the household handlers,
    bound to the space's timetables.

    The member check and the feature check run here, in that order, so a
    non-member learns nothing about the space's features; the scope the
    handler then uses re-checks both on every call and adds the owner /
    admin gate for writes.
    """

    async def _svc(self) -> Editor:
        user = self.user
        space_id = self.match("space_id")
        if await self.svc(space_repo_key).get_member(space_id, user.user_id) is None:
            raise SpacePermissionError("not a member of this space")
        await self.require_space_feature(space_id, _SECTION)
        return self.svc(space_timetable_service_key).scope(space_id, user.user_id)


class SpaceTimetableCollectionView(_SpaceTimetablesBase, TimetableCollectionView):
    """``GET`` / ``POST /api/spaces/{space_id}/timetables``."""


class SpaceTimetableDetailView(_SpaceTimetablesBase, TimetableDetailView):
    """``GET`` / ``PATCH`` / ``DELETE …/timetables/{id}`` (delete tombstones)."""


class SpaceTimetableDuplicateView(_SpaceTimetablesBase, TimetableDuplicateView):
    """``POST …/timetables/{id}/duplicate``."""


class SpaceTimetableDayGenerateView(_SpaceTimetablesBase, TimetableDayGenerateView):
    """``POST …/timetables/{id}/days/{weekday}/generate``."""


class SpaceTimetableDayCopyView(_SpaceTimetablesBase, TimetableDayCopyView):
    """``POST …/timetables/{id}/days/{weekday}/copy``."""


class SpaceTimetableDayShiftView(_SpaceTimetablesBase, TimetableDayShiftView):
    """``POST …/timetables/{id}/days/{weekday}/shift``."""


class SpaceTimetableEntryCollectionView(
    _SpaceTimetablesBase, TimetableEntryCollectionView
):
    """``POST`` / ``PUT …/timetables/{id}/entries``."""


class SpaceTimetableEntryDetailView(_SpaceTimetablesBase, TimetableEntryDetailView):
    """``PATCH`` / ``DELETE …/timetables/{id}/entries/{entry_id}``."""


class SpaceTimetableValidityView(_SpaceTimetablesBase, TimetableValidityView):
    """``PUT …/timetables/{id}/validity``."""


class SpaceTimetableWeekView(_SpaceTimetablesBase, TimetableWeekView):
    """``GET …/timetables/{id}/weeks/{date}``."""


class SpaceTimetableWeekOverridesView(_SpaceTimetablesBase, TimetableWeekOverridesView):
    """``DELETE …/timetables/{id}/weeks/{date}/overrides?version=N``."""


class SpaceTimetableOverrideCollectionView(
    _SpaceTimetablesBase, TimetableOverrideCollectionView
):
    """``POST …/timetables/{id}/overrides``."""


class SpaceTimetableOverrideDetailView(
    _SpaceTimetablesBase, TimetableOverrideDetailView
):
    """``PATCH`` / ``DELETE …/timetables/{id}/overrides/{override_id}``."""
