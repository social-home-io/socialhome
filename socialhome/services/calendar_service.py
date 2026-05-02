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
from datetime import datetime, timedelta, timezone

from ..domain.calendar import (
    Calendar,
    CalendarEvent,
    CalendarRSVP,
    EventReminder,
    RSVPStatus,
)
from ..domain.events import (
    CalendarEventCreated,
    CalendarEventDeleted,
    CalendarEventUpdated,
    SpaceMemberLeft,
)
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from ..repositories.calendar_repo import AbstractCalendarRepo, AbstractSpaceCalendarRepo
from ..utils.rrule import expand_rrule


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

    async def list_calendars(
        self,
        username: str,
        *,
        scope: str = "user",
    ) -> list[Calendar]:
        """List calendars visible to *username*.

        ``scope='user'`` (default) returns only the caller's own
        calendars — the historical behaviour. ``scope='household'``
        returns every calendar on the instance so the SPA's calendar
        picker can let a member peek at another member's calendar.
        """
        if scope == "household":
            return await self._repo.list_all_calendars()
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
        rsvp_enabled: bool = False,
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
            rsvp_enabled=rsvp_enabled,
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
        rsvp_enabled: bool | None = None,
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
            rsvp_enabled=(
                rsvp_enabled if rsvp_enabled is not None else existing.rsvp_enabled
            ),
        )
        await self._repo.save_event(updated)
        if self._bus is not None:
            await self._bus.publish(CalendarEventUpdated(event=updated))
        return updated


class SpaceCalendarService:
    """Space calendar event operations."""

    __slots__ = ("_repo", "_bus", "_federation")

    def __init__(
        self,
        space_calendar_repo: AbstractSpaceCalendarRepo,
        bus: EventBus | None = None,
    ) -> None:
        self._repo = space_calendar_repo
        self._bus = bus
        self._federation = None

    def attach_federation(self, federation_service) -> None:
        """Wire outbound federation for RSVPs.

        ``federation_service`` is the :class:`FederationService`. RSVP
        propagation rides
        :meth:`FederationService.broadcast_to_space_members` so we
        automatically reach every peer co-hosting the space.
        """
        self._federation = federation_service

    def wire(self) -> None:
        """Phase E: subscribe to SpaceMemberLeft so a user leaving a
        space cleans up their RSVPs on its events.

        Idempotent. Call once at app startup; the bus de-duplicates
        subscribers internally."""
        if self._bus is None:
            return
        self._bus.subscribe(SpaceMemberLeft, self._on_member_left)

    async def _on_member_left(self, event: SpaceMemberLeft) -> None:
        """When a member leaves a space, drop their RSVPs on its events.

        We list the space's events for the next-year window (matches the
        lifetime of typical space content) and remove the user's RSVP
        for each. RSVPs on far-future recurring events outside the
        window are left in place — they expire naturally via
        ``ON DELETE CASCADE`` when the event is eventually deleted.
        """
        events = await self._repo.list_events_in_range(
            event.space_id,
            start=datetime.now(timezone.utc) - timedelta(days=1),
            end=datetime.now(timezone.utc) + timedelta(days=365),
        )
        # `list_events_in_range` may return virtual occurrences (id
        # suffixed with ``@<iso>``) for recurring events; canonical
        # rows only here.
        seen: set[str] = set()
        for ev in events:
            base_id = ev.id.split("@", 1)[0]
            if base_id in seen:
                continue
            seen.add(base_id)
            # Drop every per-occurrence row for this user — fetch their
            # rows for the event and delete each.
            user_rows = [
                r
                for r in await self._repo.list_rsvps(base_id)
                if r.user_id == event.user_id
            ]
            for r in user_rows:
                await self._repo.remove_rsvp(
                    base_id,
                    event.user_id,
                    occurrence_at=r.occurrence_at,
                )

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
        capacity: int | None = None,
    ) -> CalendarEvent:
        """Create a space-scoped calendar event.

        ``capacity`` (Phase C): when set, "going" RSVPs require host
        approval and overflow lands on a waitlist. The creator is
        auto-RSVP'd as ``going`` for the first occurrence and counts
        toward capacity.
        """
        summary = (summary or "").strip()
        if not summary:
            raise ValueError("summary must not be empty")
        if capacity is not None and capacity < 0:
            raise ValueError("capacity must be >= 0")
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
            capacity=capacity,
        )
        saved = await self._repo.save_event(space_id, event)
        if self._bus is not None:
            await self._bus.publish(CalendarEventCreated(event=saved))
        # Phase C: auto-RSVP the creator as going for the first
        # occurrence — they're implicitly going, even on a capped event
        # (skips the approval flow for self).
        await self._repo.upsert_rsvp(
            CalendarRSVP(
                event_id=saved.id,
                user_id=created_by,
                status=RSVPStatus.GOING,
                updated_at=datetime.now(timezone.utc).isoformat(),
                occurrence_at=start_dt.isoformat(),
            )
        )
        return saved

    async def delete_event(self, event_id: str) -> None:
        # Snapshot the event + the cohort of "still attending"-ish RSVPs
        # before deletion so the push handler can produce a meaningful
        # title and reach affected members. The RSVP rows themselves
        # CASCADE-delete with the event.
        result = await self._repo.get_event(event_id)
        snapshot_summary = result[1].summary if result is not None else None
        snapshot_space = result[0] if result is not None else None
        notify: tuple[str, ...] = ()
        if result is not None:
            rsvps = await self._repo.list_rsvps(event_id)
            notify = tuple(
                {
                    r.user_id
                    for r in rsvps
                    if r.status
                    in (
                        RSVPStatus.GOING,
                        RSVPStatus.WAITLIST,
                        RSVPStatus.REQUESTED,
                    )
                }
            )
        await self._repo.delete_event(event_id)
        if self._bus is not None:
            await self._bus.publish(
                CalendarEventDeleted(
                    event_id=event_id,
                    summary=snapshot_summary,
                    space_id=snapshot_space,
                    notify_user_ids=notify,
                )
            )

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
        capacity: int | None = None,
        clear_capacity: bool = False,
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

        if capacity is not None and capacity < 0:
            raise ValueError("capacity must be >= 0")
        new_capacity = (
            None
            if clear_capacity
            else (capacity if capacity is not None else existing.capacity)
        )
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
            capacity=new_capacity,
        )
        await self._repo.save_event(space_id, updated)
        # Compute *material* field changes — Phase D: only these
        # trigger update push notifications. Cosmetic changes (description,
        # attendees, rrule, all_day) are silent.
        changes: list[str] = []
        if existing.summary != updated.summary:
            changes.append("summary")
        if existing.start != updated.start:
            changes.append("start")
        if existing.end != updated.end:
            changes.append("end")
        # Capacity going *down* is material (it kicks people out / changes
        # waitlist semantics). Capacity going up is silent — promotion
        # happens automatically.
        if (
            existing.capacity is not None
            and new_capacity is not None
            and new_capacity < existing.capacity
        ):
            changes.append("capacity_down")
        if self._bus is not None:
            await self._bus.publish(
                CalendarEventUpdated(
                    event=updated,
                    material_changes=tuple(changes),
                )
            )
        # Capacity raised — promote from waitlist to fill new seats.
        if (
            existing.capacity is not None
            and new_capacity is not None
            and new_capacity > existing.capacity
        ):
            await self._auto_promote_waitlist(
                space_id=space_id,
                event=updated,
                occ_iso=updated.start.isoformat(),
            )
        return updated

    async def rsvp(
        self,
        *,
        event_id: str,
        user_id: str,
        status: str,
        occurrence_at: datetime | str | None = None,
    ) -> None:
        """Set ``user_id``'s RSVP for an occurrence of ``event_id``.

        For non-recurring events, ``occurrence_at`` may be omitted —
        defaults to ``event.start``. For recurring events
        (``rrule != None``) it must be provided and must reach a real
        occurrence under the event's rrule (validated via
        :func:`expand_rrule`).
        """
        if status not in RSVPStatus.USER_SETTABLE:
            raise ValueError(
                f"RSVP status must be one of {sorted(RSVPStatus.USER_SETTABLE)}, "
                f"got {status!r}"
            )
        result = await self._repo.get_event(event_id)
        if result is None:
            raise KeyError(f"event {event_id!r} not found")
        space_id, event = result
        occ_dt = self._resolve_occurrence(event, occurrence_at)
        # Phase E: past-event RSVP lock. Once an occurrence has *ended*
        # (its window is fully in the past), responding to it is a
        # write into the past — reject. We compare against
        # ``occurrence_end = occ_dt + (event.end - event.start)`` so
        # recurring events get the correct per-occurrence window.
        # The creator's auto-RSVP at create_event time goes through a
        # different code path so this guard doesn't block it.
        duration = event.end - event.start
        if occ_dt + duration < datetime.now(timezone.utc):
            raise ValueError(
                "cannot RSVP to an occurrence that has already ended",
            )
        occ_iso = occ_dt.isoformat()
        now_iso = datetime.now(timezone.utc).isoformat()
        # Phase C: capacity-aware "going" routing. Only "going" is
        # affected — "maybe" never counts toward capacity, "declined"
        # frees a slot (which then auto-promotes the waitlist below).
        effective_status = status
        if (
            event.capacity is not None
            and status == RSVPStatus.GOING
            and user_id != event.created_by
        ):
            existing = await self._existing_rsvp(event_id, user_id, occ_iso)
            if existing != RSVPStatus.GOING:
                effective_status = await self._route_capped_going(
                    event=event,
                    user_id=user_id,
                    occ_iso=occ_iso,
                )
        await self._repo.upsert_rsvp(
            CalendarRSVP(
                event_id=event_id,
                user_id=user_id,
                status=effective_status,
                updated_at=now_iso,
                occurrence_at=occ_iso,
            )
        )
        await self._publish_federation_rsvp(
            space_id=space_id,
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_iso,
            status=effective_status,
            updated_at=now_iso,
        )
        # If this RSVP frees a seat (declined / maybe replacing a
        # previous "going"), promote the oldest waitlist row.
        if status != RSVPStatus.GOING and event.capacity is not None:
            await self._auto_promote_waitlist(
                space_id=space_id,
                event=event,
                occ_iso=occ_iso,
            )

    async def remove_rsvp(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: datetime | str | None = None,
    ) -> None:
        """Clear ``user_id``'s RSVP for an occurrence of ``event_id``."""
        result = await self._repo.get_event(event_id)
        if result is None:
            return
        space_id, event = result
        occ_dt = self._resolve_occurrence(event, occurrence_at)
        occ_iso = occ_dt.isoformat()
        await self._repo.remove_rsvp(
            event_id,
            user_id,
            occurrence_at=occ_iso,
        )
        await self._publish_federation_rsvp(
            space_id=space_id,
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_iso,
            status=None,  # signals delete
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        # Removing a "going" RSVP frees a seat — promote from waitlist.
        if event.capacity is not None:
            await self._auto_promote_waitlist(
                space_id=space_id,
                event=event,
                occ_iso=occ_iso,
            )

    async def list_rsvps(
        self,
        event_id: str,
        *,
        occurrence_at: datetime | str | None = None,
    ) -> list[CalendarRSVP]:
        """List RSVPs for ``event_id``.

        If ``occurrence_at`` is provided, returns only the RSVPs for
        that single occurrence. Otherwise returns the rows across all
        occurrences (callers wanting per-occurrence aggregates should
        group on ``rsvp.occurrence_at``).
        """
        if occurrence_at is None:
            return await self._repo.list_rsvps(event_id)
        occ_iso = (
            occurrence_at.isoformat()
            if isinstance(occurrence_at, datetime)
            else str(occurrence_at)
        )
        return await self._repo.list_rsvps(event_id, occurrence_at=occ_iso)

    # ── Reminders (Phase D) ──────────────────────────────────────────────

    async def add_reminder(
        self,
        *,
        event_id: str,
        user_id: str,
        minutes_before: int,
        occurrence_at: datetime | str | None = None,
    ) -> EventReminder:
        """Schedule a reminder for ``user_id`` on a specific occurrence.

        ``minutes_before`` is the offset; ``fire_at`` is computed as
        ``occurrence - minutes_before``. The reminder lives in the
        scheduler's queue until either delivered or removed.
        """
        if minutes_before < 0:
            raise ValueError("minutes_before must be >= 0")
        result = await self._repo.get_event(event_id)
        if result is None:
            raise KeyError(f"event {event_id!r} not found")
        _space_id, event = result
        occ_dt = self._resolve_occurrence(event, occurrence_at)
        fire_at_dt = occ_dt - timedelta(minutes=minutes_before)
        reminder = EventReminder(
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_dt.isoformat(),
            minutes_before=int(minutes_before),
            fire_at=fire_at_dt.isoformat(),
        )
        await self._repo.upsert_reminder(reminder)
        return reminder

    async def remove_reminder(
        self,
        *,
        event_id: str,
        user_id: str,
        minutes_before: int,
        occurrence_at: datetime | str | None = None,
    ) -> None:
        result = await self._repo.get_event(event_id)
        if result is None:
            return
        _space_id, event = result
        occ_dt = self._resolve_occurrence(event, occurrence_at)
        await self._repo.remove_reminder(
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_dt.isoformat(),
            minutes_before=int(minutes_before),
        )

    async def list_reminders(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: datetime | str | None = None,
    ) -> list[EventReminder]:
        occ_iso: str | None
        if occurrence_at is None:
            occ_iso = None
        else:
            result = await self._repo.get_event(event_id)
            if result is None:
                return []
            _space_id, event = result
            occ_dt = self._resolve_occurrence(event, occurrence_at)
            occ_iso = occ_dt.isoformat()
        return await self._repo.list_reminders(
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_iso,
        )

    # ── Capacity / request-to-join (Phase C) ─────────────────────────────

    async def list_pending(
        self,
        event_id: str,
        *,
        occurrence_at: datetime | str | None = None,
    ) -> list[CalendarRSVP]:
        """List `requested` RSVPs awaiting host approval for an event.

        Approver-side helper — the route gates this on actor approver-
        status before invoking. With ``occurrence_at`` returns just that
        instance; without, returns all pending across occurrences.
        """
        rsvps = await self.list_rsvps(event_id, occurrence_at=occurrence_at)
        return [r for r in rsvps if r.status == RSVPStatus.REQUESTED]

    async def approve_rsvp(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: datetime | str | None = None,
    ) -> str:
        """Approve a pending request-to-join.

        Promotes the user to ``going`` if a seat is free, otherwise to
        ``waitlist``. Returns the resulting status. Raises
        :class:`KeyError` if the event or RSVP doesn't exist.

        The approver gate (event creator OR space admin) is enforced at
        the route layer because it needs the actor's space-membership
        which the calendar service doesn't depend on.
        """
        result = await self._repo.get_event(event_id)
        if result is None:
            raise KeyError(f"event {event_id!r} not found")
        space_id, event = result
        occ_dt = self._resolve_occurrence(event, occurrence_at)
        occ_iso = occ_dt.isoformat()
        existing = await self._existing_rsvp(event_id, user_id, occ_iso)
        if existing != RSVPStatus.REQUESTED:
            raise KeyError(
                f"no pending request for user {user_id!r} on this occurrence",
            )
        new_status = (
            RSVPStatus.GOING
            if event.capacity is None
            or await self._going_count(event_id, occ_iso) < event.capacity
            else RSVPStatus.WAITLIST
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._repo.upsert_rsvp(
            CalendarRSVP(
                event_id=event_id,
                user_id=user_id,
                status=new_status,
                updated_at=now_iso,
                occurrence_at=occ_iso,
            )
        )
        await self._publish_federation_rsvp(
            space_id=space_id,
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_iso,
            status=new_status,
            updated_at=now_iso,
        )
        return new_status

    async def deny_rsvp(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: datetime | str | None = None,
    ) -> None:
        """Deny a pending request-to-join — removes the row entirely."""
        result = await self._repo.get_event(event_id)
        if result is None:
            raise KeyError(f"event {event_id!r} not found")
        _space_id, event = result
        occ_dt = self._resolve_occurrence(event, occurrence_at)
        occ_iso = occ_dt.isoformat()
        existing = await self._existing_rsvp(event_id, user_id, occ_iso)
        if existing != RSVPStatus.REQUESTED:
            raise KeyError(
                f"no pending request for user {user_id!r} on this occurrence",
            )
        # Reuse remove_rsvp so federation propagation + waitlist
        # promotion (no-op for a denied REQUESTED row) all happen.
        await self.remove_rsvp(
            event_id=event_id,
            user_id=user_id,
            occurrence_at=occ_dt,
        )

    async def _existing_rsvp(
        self,
        event_id: str,
        user_id: str,
        occ_iso: str,
    ) -> str | None:
        for r in await self._repo.list_rsvps(
            event_id,
            occurrence_at=occ_iso,
        ):
            if r.user_id == user_id:
                return r.status
        return None

    async def _going_count(self, event_id: str, occ_iso: str) -> int:
        return sum(
            1
            for r in await self._repo.list_rsvps(
                event_id,
                occurrence_at=occ_iso,
            )
            if r.status == RSVPStatus.GOING
        )

    async def _route_capped_going(
        self,
        *,
        event: CalendarEvent,
        user_id: str,
        occ_iso: str,
    ) -> str:
        """For a capped event, return the effective status when a member
        attempts to set ``going``: always REQUESTED first (host
        approval). Capacity-vs-waitlist routing happens at approval
        time."""
        return RSVPStatus.REQUESTED

    async def _auto_promote_waitlist(
        self,
        *,
        space_id: str,
        event: CalendarEvent,
        occ_iso: str,
    ) -> None:
        """If an occurrence has free capacity, promote the oldest
        ``waitlist`` row to ``going``."""
        if event.capacity is None:
            return
        going = await self._going_count(event.id, occ_iso)
        if going >= event.capacity:
            return
        rows = await self._repo.list_rsvps(event.id, occurrence_at=occ_iso)
        candidates = [r for r in rows if r.status == RSVPStatus.WAITLIST]
        if not candidates:
            return
        candidates.sort(key=lambda r: r.updated_at)
        promoted = candidates[0]
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._repo.upsert_rsvp(
            CalendarRSVP(
                event_id=promoted.event_id,
                user_id=promoted.user_id,
                status=RSVPStatus.GOING,
                updated_at=now_iso,
                occurrence_at=occ_iso,
            )
        )
        await self._publish_federation_rsvp(
            space_id=space_id,
            event_id=promoted.event_id,
            user_id=promoted.user_id,
            occurrence_at=occ_iso,
            status=RSVPStatus.GOING,
            updated_at=now_iso,
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_occurrence(
        event: CalendarEvent,
        occurrence_at: datetime | str | None,
    ) -> datetime:
        """Validate / default ``occurrence_at`` against ``event``.

        * Non-recurring + omitted → ``event.start``.
        * Non-recurring + given matching ``event.start`` → accepted.
        * Recurring + omitted → :class:`ValueError`.
        * Recurring + given → must match one of the rrule's expanded
          occurrences within a 1-year window from the seed; otherwise
          :class:`ValueError`.
        """
        is_recurring = bool(event.rrule)
        if occurrence_at is None:
            if is_recurring:
                raise ValueError(
                    "recurring events require occurrence_at on RSVP",
                )
            return event.start
        occ_dt = (
            occurrence_at
            if isinstance(occurrence_at, datetime)
            else datetime.fromisoformat(str(occurrence_at).replace("Z", "+00:00"))
        )
        if not is_recurring:
            if occ_dt != event.start:
                raise ValueError(
                    "non-recurring events: occurrence_at must equal event.start",
                )
            return occ_dt
        # Recurring — check the rrule actually emits this occurrence.
        window_end = max(
            occ_dt + timedelta(seconds=1),
            event.start + timedelta(days=365 * 5),
        )
        starts = {
            s
            for s, _ in expand_rrule(
                event.start,
                event.end,
                event.rrule,
                window_start=event.start,
                window_end=window_end,
            )
        }
        if occ_dt not in starts:
            raise ValueError(
                f"occurrence_at {occ_dt.isoformat()} is not a valid occurrence "
                f"of event {event.id}",
            )
        return occ_dt

    async def _publish_federation_rsvp(
        self,
        *,
        space_id: str,
        event_id: str,
        user_id: str,
        occurrence_at: str,
        status: str | None,
        updated_at: str,
    ) -> None:
        """Broadcast a SPACE_RSVP_UPDATED (or _DELETED) to every peer
        household co-hosting this space. No-op when federation isn't
        wired (unit tests / standalone mode)."""
        if self._federation is None:
            return
        evt_type = (
            FederationEventType.SPACE_RSVP_UPDATED
            if status is not None
            else FederationEventType.SPACE_RSVP_DELETED
        )
        payload: dict = {
            "event_id": event_id,
            "user_id": user_id,
            "occurrence_at": occurrence_at,
            "updated_at": updated_at,
        }
        if status is not None:
            payload["status"] = status
        await self._federation.broadcast_to_space_members(
            space_id,
            evt_type,
            payload,
        )


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 string, tolerating the trailing ``Z`` form."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid datetime: {value!r}") from exc
