"""Calendar repository — personal calendars + space calendars + RSVPs.

Two DB-level concepts:

* Personal calendars (``calendars`` + ``calendar_events``) — one per user.
* Space calendars (``space_calendar_events`` + ``space_calendar_rsvps``) —
  one per space, rows scoped by ``space_id``.

Exposed as two repo classes to mirror the table split; helpers are shared.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Protocol, runtime_checkable

from dataclasses import replace

from ..db import AsyncDatabase
from ..domain.calendar import (
    Calendar,
    CalendarEvent,
    CalendarRSVP,
    RSVPStatus,
)
from ..utils.rrule import expand_rrule
from .base import bool_col, dump_json, load_json, row_to_dict, rows_to_dicts


def _expand_window(
    events: list[CalendarEvent],
    *,
    start: datetime,
    end: datetime,
) -> list[CalendarEvent]:
    """Expand recurring events into their virtual occurrences.

    Non-recurring events are returned as-is (one per row). Recurring
    events are cloned per-occurrence with adjusted ``start`` / ``end``;
    the ``id`` is suffixed with ``@<iso>`` so consumers can tell
    virtuals apart from stored rows.
    """
    out: list[CalendarEvent] = []
    for ev in events:
        if not ev.rrule:
            out.append(ev)
            continue
        occs = expand_rrule(
            ev.start,
            ev.end,
            ev.rrule,
            window_start=start,
            window_end=end,
        )
        for s, e in occs:
            if s == ev.start and e == ev.end:
                out.append(ev)
            else:
                out.append(replace(ev, start=s, end=e, id=f"{ev.id}@{s.isoformat()}"))
    out.sort(key=lambda x: x.start)
    return out


# ─── Shared helpers ───────────────────────────────────────────────────────


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ─── Personal calendars ───────────────────────────────────────────────────


@runtime_checkable
class AbstractCalendarRepo(Protocol):
    async def save_calendar(self, calendar: Calendar) -> Calendar: ...
    async def get_calendar(self, calendar_id: str) -> Calendar | None: ...
    async def list_calendars_for_user(self, username: str) -> list[Calendar]: ...
    async def delete_calendar(self, calendar_id: str) -> None: ...

    async def save_event(self, event: CalendarEvent) -> CalendarEvent: ...
    async def get_event(self, event_id: str) -> CalendarEvent | None: ...
    async def list_events_in_range(
        self,
        calendar_id: str,
        *,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]: ...
    async def list_events_for_user_in_range(
        self,
        username: str,
        *,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]: ...
    async def delete_event(self, event_id: str) -> None: ...


class SqliteCalendarRepo:
    """SQLite-backed :class:`AbstractCalendarRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Calendars ──────────────────────────────────────────────────────

    async def save_calendar(self, calendar: Calendar) -> Calendar:
        await self._db.enqueue(
            """
            INSERT INTO calendars(id, name, color, owner_username, calendar_type)
            VALUES(?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                color=excluded.color,
                calendar_type=excluded.calendar_type
            """,
            (
                calendar.id,
                calendar.name,
                calendar.color,
                calendar.owner_username,
                calendar.calendar_type,
            ),
        )
        return calendar

    async def get_calendar(self, calendar_id: str) -> Calendar | None:
        row = await self._db.fetchone(
            "SELECT * FROM calendars WHERE id=?",
            (calendar_id,),
        )
        return _row_to_calendar(row_to_dict(row))

    async def list_calendars_for_user(self, username: str) -> list[Calendar]:
        rows = await self._db.fetchall(
            "SELECT * FROM calendars WHERE owner_username=? ORDER BY name",
            (username,),
        )
        return [c for c in (_row_to_calendar(d) for d in rows_to_dicts(rows)) if c]

    async def delete_calendar(self, calendar_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM calendars WHERE id=?",
            (calendar_id,),
        )

    # ── Events ─────────────────────────────────────────────────────────

    async def save_event(self, event: CalendarEvent) -> CalendarEvent:
        await self._db.enqueue(
            """
            INSERT INTO calendar_events(
                id, calendar_id, summary, description, start_dt, end_dt,
                all_day, attendees_json, mirrored_from, rrule,
                created_by, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,
                     COALESCE(?, datetime('now')),
                     COALESCE(?, datetime('now')))
            ON CONFLICT(id) DO UPDATE SET
                summary=excluded.summary,
                description=excluded.description,
                start_dt=excluded.start_dt,
                end_dt=excluded.end_dt,
                all_day=excluded.all_day,
                attendees_json=excluded.attendees_json,
                mirrored_from=excluded.mirrored_from,
                rrule=excluded.rrule,
                updated_at=datetime('now')
            """,
            (
                event.id,
                event.calendar_id,
                event.summary,
                event.description,
                _iso(event.start),
                _iso(event.end),
                int(event.all_day),
                dump_json(list(event.attendees)),
                event.mirrored_from,
                event.rrule,
                event.created_by,
                None,
                None,
            ),
        )
        return event

    async def get_event(self, event_id: str) -> CalendarEvent | None:
        row = await self._db.fetchone(
            "SELECT * FROM calendar_events WHERE id=?",
            (event_id,),
        )
        return _row_to_event(row_to_dict(row))

    async def list_events_in_range(
        self,
        calendar_id: str,
        *,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]:
        # Pull one-off events that overlap the window, plus every
        # recurring event whose seed starts before ``end`` — recurring
        # instances inside the window may originate from a seed that
        # started years ago, so the seed itself doesn't need to overlap.
        rows = await self._db.fetchall(
            """
            SELECT * FROM calendar_events
             WHERE calendar_id=?
               AND (
                    (rrule IS NULL AND start_dt < ? AND end_dt > ?)
                 OR (rrule IS NOT NULL AND start_dt < ?)
               )
             ORDER BY start_dt
            """,
            (calendar_id, _iso(end), _iso(start), _iso(end)),
        )
        events = [e for e in (_row_to_event(d) for d in rows_to_dicts(rows)) if e]
        return _expand_window(events, start=start, end=end)

    async def list_events_for_user_in_range(
        self,
        username: str,
        *,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]:
        rows = await self._db.fetchall(
            """
            SELECT e.* FROM calendar_events e
              JOIN calendars c ON c.id = e.calendar_id
             WHERE c.owner_username=?
               AND (
                    (e.rrule IS NULL AND e.start_dt < ? AND e.end_dt > ?)
                 OR (e.rrule IS NOT NULL AND e.start_dt < ?)
               )
             ORDER BY e.start_dt
            """,
            (username, _iso(end), _iso(start), _iso(end)),
        )
        events = [e for e in (_row_to_event(d) for d in rows_to_dicts(rows)) if e]
        return _expand_window(events, start=start, end=end)

    async def delete_event(self, event_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM calendar_events WHERE id=?",
            (event_id,),
        )


# ─── Space calendars ──────────────────────────────────────────────────────


@runtime_checkable
class AbstractSpaceCalendarRepo(Protocol):
    async def save_event(
        self,
        space_id: str,
        event: CalendarEvent,
    ) -> CalendarEvent: ...
    async def get_event(
        self,
        event_id: str,
    ) -> tuple[str, CalendarEvent] | None: ...
    async def list_events_in_range(
        self,
        space_id: str,
        *,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]: ...
    async def list_events_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[CalendarEvent]: ...
    async def delete_event(self, event_id: str) -> None: ...

    async def upsert_rsvp(self, rsvp: CalendarRSVP) -> None: ...
    async def remove_rsvp(
        self,
        event_id: str,
        user_id: str,
        *,
        occurrence_at: str,
    ) -> None: ...
    async def list_rsvps(
        self,
        event_id: str,
        *,
        occurrence_at: str | None = None,
    ) -> list[CalendarRSVP]: ...

    # ── Federation buffer (§Phase A out-of-order RSVPs) ───────────────
    async def buffer_pending_rsvp(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: str,
        status: str,
        updated_at: str,
    ) -> None: ...
    async def flush_pending_rsvps(
        self,
        event_id: str,
    ) -> list[CalendarRSVP]: ...
    async def gc_pending_rsvps(self, *, older_than_iso: str) -> int: ...


class SqliteSpaceCalendarRepo:
    """SQLite-backed :class:`AbstractSpaceCalendarRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save_event(
        self,
        space_id: str,
        event: CalendarEvent,
    ) -> CalendarEvent:
        await self._db.enqueue(
            """
            INSERT INTO space_calendar_events(
                id, space_id, summary, description, start_dt, end_dt,
                all_day, attendees_json, rrule, capacity,
                created_by, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,
                     COALESCE(?, datetime('now')),
                     COALESCE(?, datetime('now')))
            ON CONFLICT(id) DO UPDATE SET
                summary=excluded.summary,
                description=excluded.description,
                start_dt=excluded.start_dt,
                end_dt=excluded.end_dt,
                all_day=excluded.all_day,
                attendees_json=excluded.attendees_json,
                rrule=excluded.rrule,
                capacity=excluded.capacity,
                updated_at=datetime('now')
            """,
            (
                event.id,
                space_id,
                event.summary,
                event.description,
                _iso(event.start),
                _iso(event.end),
                int(event.all_day),
                dump_json(list(event.attendees)),
                event.rrule,
                event.capacity,
                event.created_by,
                None,
                None,
            ),
        )
        return event

    async def get_event(
        self,
        event_id: str,
    ) -> tuple[str, CalendarEvent] | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_calendar_events WHERE id=?",
            (event_id,),
        )
        d = row_to_dict(row)
        if d is None:
            return None
        return d["space_id"], _row_to_space_event(d)

    async def list_events_in_range(
        self,
        space_id: str,
        *,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM space_calendar_events
             WHERE space_id=?
               AND (
                    (rrule IS NULL AND start_dt < ? AND end_dt > ?)
                 OR (rrule IS NOT NULL AND start_dt < ?)
               )
             ORDER BY start_dt
            """,
            (space_id, _iso(end), _iso(start), _iso(end)),
        )
        events = [_row_to_space_event(d) for d in rows_to_dicts(rows)]
        return _expand_window(events, start=start, end=end)

    async def list_events_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[CalendarEvent]:
        """Calendar events with ``updated_at > since``, oldest-first.

        Used by ``SpaceSyncResumeProvider`` to replay missed
        ``SPACE_CALENDAR_EVENT_*`` events on long-offline catch-up.
        Recurring events are emitted once with their RRULE — the
        receiver's existing inbound handler stores them as a single row
        and the per-occurrence expansion runs on read.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM space_calendar_events "
            "WHERE space_id=? AND updated_at > ? "
            "ORDER BY updated_at ASC LIMIT ?",
            (space_id, since, int(limit)),
        )
        return [_row_to_space_event(d) for d in rows_to_dicts(rows)]

    async def delete_event(self, event_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_calendar_events WHERE id=?",
            (event_id,),
        )

    # ── RSVPs ──────────────────────────────────────────────────────────

    async def upsert_rsvp(self, rsvp: CalendarRSVP) -> None:
        if rsvp.status not in RSVPStatus.ALL:
            raise ValueError(f"invalid RSVP status {rsvp.status!r}")
        if not rsvp.occurrence_at:
            raise ValueError("CalendarRSVP.occurrence_at must be set")
        await self._db.enqueue(
            """
            INSERT INTO space_calendar_rsvps(
                event_id, user_id, occurrence_at, status, updated_at
            ) VALUES(?, ?, ?, ?, COALESCE(?, datetime('now')))
            ON CONFLICT(event_id, user_id, occurrence_at) DO UPDATE SET
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                rsvp.event_id,
                rsvp.user_id,
                rsvp.occurrence_at,
                rsvp.status,
                rsvp.updated_at,
            ),
        )

    async def remove_rsvp(
        self,
        event_id: str,
        user_id: str,
        *,
        occurrence_at: str,
    ) -> None:
        await self._db.enqueue(
            """
            DELETE FROM space_calendar_rsvps
             WHERE event_id=? AND user_id=? AND occurrence_at=?
            """,
            (event_id, user_id, occurrence_at),
        )

    async def list_rsvps(
        self,
        event_id: str,
        *,
        occurrence_at: str | None = None,
    ) -> list[CalendarRSVP]:
        if occurrence_at is None:
            rows = await self._db.fetchall(
                "SELECT * FROM space_calendar_rsvps "
                "WHERE event_id=? ORDER BY occurrence_at, updated_at",
                (event_id,),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM space_calendar_rsvps "
                "WHERE event_id=? AND occurrence_at=? ORDER BY updated_at",
                (event_id, occurrence_at),
            )
        return [
            CalendarRSVP(
                event_id=r["event_id"],
                user_id=r["user_id"],
                status=r["status"],
                updated_at=r["updated_at"],
                occurrence_at=r["occurrence_at"],
            )
            for r in rows
        ]

    # ── Federation out-of-order buffer ─────────────────────────────────

    async def buffer_pending_rsvp(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: str,
        status: str,
        updated_at: str,
    ) -> None:
        """Buffer an inbound RSVP whose event hasn't propagated yet.

        Idempotent: last-write-wins on (event_id, user_id, occurrence_at).
        Status ``"removed"`` represents a DELETE that arrived before its
        event — so when the event lands and we flush, the deletion is
        honoured (rather than the buffer resurrecting a stale RSVP).
        """
        await self._db.enqueue(
            """
            INSERT INTO pending_federated_rsvps(
                event_id, user_id, occurrence_at, status, updated_at
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(event_id, user_id, occurrence_at) DO UPDATE SET
                status=excluded.status,
                updated_at=excluded.updated_at,
                received_at=datetime('now')
            """,
            (event_id, user_id, occurrence_at, status, updated_at),
        )

    async def flush_pending_rsvps(self, event_id: str) -> list[CalendarRSVP]:
        """Drain buffered RSVPs for ``event_id`` and apply them.

        Called when an event lands locally (either local create or
        inbound federation). Returns the list of applied RSVPs (excluding
        ``removed`` rows which result in a delete). The buffer rows are
        always cleared regardless of whether the apply succeeded —
        callers shouldn't see the same buffered RSVP twice.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM pending_federated_rsvps WHERE event_id=?",
            (event_id,),
        )
        applied: list[CalendarRSVP] = []
        for r in rows:
            status = r["status"]
            occurrence_at = r["occurrence_at"]
            if status == "removed":
                await self.remove_rsvp(
                    event_id, r["user_id"], occurrence_at=occurrence_at,
                )
            elif status in RSVPStatus.ALL:
                rsvp = CalendarRSVP(
                    event_id=event_id,
                    user_id=r["user_id"],
                    status=status,
                    updated_at=r["updated_at"],
                    occurrence_at=occurrence_at,
                )
                await self.upsert_rsvp(rsvp)
                applied.append(rsvp)
        if rows:
            await self._db.enqueue(
                "DELETE FROM pending_federated_rsvps WHERE event_id=?",
                (event_id,),
            )
        return applied

    async def gc_pending_rsvps(self, *, older_than_iso: str) -> int:
        """Drop buffered RSVPs older than ``older_than_iso``.

        Returns the row count that was purged. Called periodically by a
        scheduler (Phase E) to bound the buffer when an event never
        arrives (e.g. cancelled upstream before propagating).
        """
        cur = await self._db.fetchall(
            "SELECT COUNT(*) AS n FROM pending_federated_rsvps WHERE received_at<?",
            (older_than_iso,),
        )
        n = int(cur[0]["n"]) if cur else 0
        if n:
            await self._db.enqueue(
                "DELETE FROM pending_federated_rsvps WHERE received_at<?",
                (older_than_iso,),
            )
        return n


# ─── Row → domain ─────────────────────────────────────────────────────────


def _row_to_calendar(row: dict | None) -> Calendar | None:
    if row is None:
        return None
    return Calendar(
        id=row["id"],
        name=row["name"],
        color=row.get("color", "#4A90E2"),
        owner_username=row["owner_username"],
        calendar_type=row.get("calendar_type", "personal"),
    )


def _row_to_event(row: dict | None) -> CalendarEvent | None:
    if row is None:
        return None
    return CalendarEvent(
        id=row["id"],
        calendar_id=row["calendar_id"],
        summary=row["summary"],
        start=_parse(row["start_dt"]) or datetime.now(timezone.utc),
        end=_parse(row["end_dt"]) or datetime.now(timezone.utc),
        created_by=row["created_by"],
        description=row.get("description"),
        all_day=bool_col(row.get("all_day", 0)),
        attendees=tuple(load_json(row.get("attendees_json"), [])),
        mirrored_from=row.get("mirrored_from"),
        rrule=row.get("rrule"),
    )


def _row_to_space_event(row: dict) -> CalendarEvent:
    # Space events have no calendar_id column — use space_id as the
    # effective container so the domain object is still populated.
    cap = row.get("capacity")
    return CalendarEvent(
        id=row["id"],
        calendar_id=row["space_id"],
        summary=row["summary"],
        start=_parse(row["start_dt"]) or datetime.now(timezone.utc),
        end=_parse(row["end_dt"]) or datetime.now(timezone.utc),
        created_by=row["created_by"],
        description=row.get("description"),
        all_day=bool_col(row.get("all_day", 0)),
        attendees=tuple(load_json(row.get("attendees_json"), [])),
        rrule=row.get("rrule"),
        capacity=int(cap) if cap is not None else None,
    )
