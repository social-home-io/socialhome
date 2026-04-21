"""Calendar service — thin orchestration wrapper around calendar repos.

Provides service-layer entry points for personal calendars and space
calendar events. Route handlers call these methods; no SQL in routes.

Raises the usual domain exceptions:

* ``KeyError``   → 404 (calendar or event not found)
* ``ValueError`` → 422 (validation failure)
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from ..domain.calendar import Calendar, CalendarEvent, CalendarRSVP, RSVPStatus
from ..domain.events import (
    CalendarEventCreated,
    CalendarEventDeleted,
    CalendarEventUpdated,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.calendar_repo import AbstractCalendarRepo, AbstractSpaceCalendarRepo


class CalendarService:
    """Personal calendar operations."""

    __slots__ = ("_repo", "_bus", "_household")

    def __init__(
        self,
        calendar_repo: AbstractCalendarRepo,
        bus: EventBus | None = None,
    ) -> None:
        self._repo = calendar_repo
        self._bus = bus
        self._household = None

    def attach_household_features(self, svc) -> None:
        """Wire :class:`HouseholdFeaturesService` for toggle enforcement (§18)."""
        self._household = svc

    async def _require_calendar_enabled(self) -> None:
        if self._household is not None:
            await self._household.require_enabled("calendar")

    # ── Calendars ────────────────────────────────────────────────────────

    async def create_calendar(
        self,
        *,
        name: str,
        owner_username: str,
        color: str | None = None,
    ) -> Calendar:
        await self._require_calendar_enabled()
        name = name.strip()
        if not name:
            raise ValueError("calendar name must not be empty")
        calendar = Calendar(
            id=uuid.uuid4().hex,
            name=name,
            owner_username=owner_username,
            color=color or "#2196F3",
        )
        return await self._repo.save_calendar(calendar)

    async def get_calendar(self, calendar_id: str) -> Calendar:
        result = await self._repo.get_calendar(calendar_id)
        if result is None:
            raise KeyError(f"calendar {calendar_id!r} not found")
        return result

    async def list_calendars(self, username: str) -> list[Calendar]:
        return await self._repo.list_calendars_for_user(username)

    async def delete_calendar(self, calendar_id: str) -> None:
        result = await self._repo.get_calendar(calendar_id)
        if result is None:
            raise KeyError(f"calendar {calendar_id!r} not found")
        await self._repo.delete_calendar(calendar_id)

    # ── Events ────────────────────────────────────────────────────────────

    async def create_event(
        self,
        *,
        calendar_id: str,
        summary: str,
        start: str,
        end: str,
        created_by: str,
        all_day: bool = False,
        description: str | None = None,
        attendees: list[str] | None = None,
        rrule: str | None = None,
    ) -> CalendarEvent:
        await self._require_calendar_enabled()
        summary = summary.strip()
        if not summary:
            raise ValueError("event summary must not be empty")

        # Validate calendar exists
        cal = await self._repo.get_calendar(calendar_id)
        if cal is None:
            raise KeyError(f"calendar {calendar_id!r} not found")

        def _parse_dt(value: str) -> datetime:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"invalid datetime: {value!r}") from exc

        start_dt = _parse_dt(start)
        end_dt = _parse_dt(end)
        if end_dt < start_dt:
            raise ValueError("event end must not be before start")

        event = CalendarEvent(
            id=uuid.uuid4().hex,
            calendar_id=calendar_id,
            summary=summary,
            start=start_dt,
            end=end_dt,
            created_by=created_by,
            all_day=all_day,
            description=description,
            attendees=tuple(attendees or []),
            rrule=rrule,
        )
        saved = await self._repo.save_event(event)
        if self._bus is not None:
            await self._bus.publish(CalendarEventCreated(event=saved))
        return saved

    async def get_event(self, event_id: str) -> CalendarEvent:
        result = await self._repo.get_event(event_id)
        if result is None:
            raise KeyError(f"calendar event {event_id!r} not found")
        return result

    async def list_events_in_range(
        self,
        calendar_id: str,
        *,
        start: str,
        end: str,
    ) -> list[CalendarEvent]:
        def _parse_dt(value: str) -> datetime:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"invalid datetime: {value!r}") from exc

        return await self._repo.list_events_in_range(
            calendar_id,
            start=_parse_dt(start),
            end=_parse_dt(end),
        )

    async def delete_event(self, event_id: str) -> None:
        result = await self._repo.get_event(event_id)
        if result is None:
            raise KeyError(f"calendar event {event_id!r} not found")
        await self._repo.delete_event(event_id)
        if self._bus is not None:
            await self._bus.publish(CalendarEventDeleted(event_id=event_id))

    async def update_event(
        self,
        event_id: str,
        *,
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        all_day: bool | None = None,
        description: str | None = None,
        attendees: list[str] | None = None,
        rrule: str | None = None,
    ) -> CalendarEvent:
        """Partial-update an existing event. Only fields the caller
        supplies are overwritten; the rest retain their current values.
        """
        existing = await self._repo.get_event(event_id)
        if existing is None:
            raise KeyError(f"calendar event {event_id!r} not found")
        new_summary = (summary or existing.summary).strip()
        if not new_summary:
            raise ValueError("event summary must not be empty")
        new_start = _parse_iso(start) if start else existing.start
        new_end = _parse_iso(end) if end else existing.end
        if new_end < new_start:
            raise ValueError("event end must not be before start")
        updated = replace(
            existing,
            summary=new_summary,
            start=new_start,
            end=new_end,
            all_day=bool(all_day) if all_day is not None else existing.all_day,
            description=description
            if description is not None
            else existing.description,
            attendees=tuple(attendees) if attendees is not None else existing.attendees,
            rrule=rrule if rrule is not None else existing.rrule,
        )
        await self._repo.save_event(updated)
        if self._bus is not None:
            await self._bus.publish(CalendarEventUpdated(event=updated))
        return updated


class SpaceCalendarService:
    """Space calendar event operations."""

    __slots__ = ("_repo", "_bus")

    def __init__(
        self,
        space_calendar_repo: AbstractSpaceCalendarRepo,
        bus: EventBus | None = None,
    ) -> None:
        self._repo = space_calendar_repo
        self._bus = bus

    async def list_events_in_range(
        self,
        space_id: str,
        *,
        start: str,
        end: str,
    ) -> list[CalendarEvent]:
        def _parse_dt(value: str) -> datetime:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"invalid datetime: {value!r}") from exc

        return await self._repo.list_events_in_range(
            space_id,
            start=_parse_dt(start),
            end=_parse_dt(end),
        )

    async def create_event(
        self,
        *,
        space_id: str,
        summary: str,
        start: str,
        end: str,
        created_by: str,
        description: str | None = None,
        all_day: bool = False,
        attendees: tuple[str, ...] = (),
        rrule: str | None = None,
    ) -> CalendarEvent:
        """Create a space-scoped calendar event."""
        summary = (summary or "").strip()
        if not summary:
            raise ValueError("summary must not be empty")
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid datetime: {exc}") from exc
        if end_dt < start_dt:
            raise ValueError("end must be at or after start")
        event = CalendarEvent(
            id=uuid.uuid4().hex,
            calendar_id=space_id,  # space-scoped events use space_id as calendar_id
            summary=summary,
            description=description,
            start=start_dt,
            end=end_dt,
            all_day=all_day,
            attendees=tuple(attendees),
            created_by=created_by,
            rrule=rrule,
        )
        saved = await self._repo.save_event(space_id, event)
        if self._bus is not None:
            await self._bus.publish(CalendarEventCreated(event=saved))
        return saved

    async def delete_event(self, event_id: str) -> None:
        await self._repo.delete_event(event_id)
        if self._bus is not None:
            await self._bus.publish(CalendarEventDeleted(event_id=event_id))

    async def resolve_space_id(self, event_id: str) -> str | None:
        """Return the ``space_id`` that owns ``event_id`` or None.

        Used by the RSVP route to gate voters on space membership and
        to scope WS broadcasts to co-members.
        """
        result = await self._repo.get_event(event_id)
        if result is None:
            return None
        space_id, _event = result
        return space_id

    async def update_event(
        self,
        event_id: str,
        *,
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        all_day: bool | None = None,
        description: str | None = None,
        attendees: tuple[str, ...] | None = None,
        rrule: str | None = None,
    ) -> CalendarEvent:
        """Partial-update a space event. Emits CalendarEventUpdated."""
        result = await self._repo.get_event(event_id)
        if result is None:
            raise KeyError(f"space calendar event {event_id!r} not found")
        space_id, existing = result

        new_summary = (summary if summary is not None else existing.summary).strip()
        if not new_summary:
            raise ValueError("event summary must not be empty")
        new_start = _parse_iso(start) if start else existing.start
        new_end = _parse_iso(end) if end else existing.end
        if new_end < new_start:
            raise ValueError("event end must not be before start")

        updated = replace(
            existing,
            summary=new_summary,
            start=new_start,
            end=new_end,
            all_day=bool(all_day) if all_day is not None else existing.all_day,
            description=description
            if description is not None
            else existing.description,
            attendees=tuple(attendees) if attendees is not None else existing.attendees,
            rrule=rrule if rrule is not None else existing.rrule,
        )
        await self._repo.save_event(space_id, updated)
        if self._bus is not None:
            await self._bus.publish(CalendarEventUpdated(event=updated))
        return updated

    async def rsvp(
        self,
        *,
        event_id: str,
        user_id: str,
        status: str,
    ) -> None:
        if status not in RSVPStatus.ALL:
            raise ValueError(f"invalid RSVP status: {status!r}")
        await self._repo.upsert_rsvp(
            CalendarRSVP(
                event_id=event_id,
                user_id=user_id,
                status=status,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    async def remove_rsvp(self, *, event_id: str, user_id: str) -> None:
        await self._repo.remove_rsvp(event_id, user_id)

    async def list_rsvps(self, event_id: str):
        return await self._repo.list_rsvps(event_id)


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 string, tolerating the trailing ``Z`` form."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid datetime: {value!r}") from exc
