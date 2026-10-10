"""Calendar repository — personal calendars + space calendars + RSVPs.

Two DB-level concepts:

* Personal calendars (``calendars`` + ``calendar_events``) — one per user.
* Space calendars (``space_calendar_events`` + ``space_calendar_rsvps``) —
  one per space, rows scoped by ``space_id``.

Exposed as two repo classes to mirror the table split; helpers are shared.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date, datetime, timezone
from typing import Protocol, runtime_checkable

from dataclasses import replace

from ..auth import sha256_token_hash
from ..db import AsyncDatabase
from ..domain.calendar import (
    OCCURRENCE_ID_SEPARATOR,
    Calendar,
    CalendarEvent,
    CalendarEventCopy,
    CalendarRSVP,
    EventReminder,
    RSVPStatus,
)
from ..domain.tombstone import SpaceRowTombstone
from ..utils.rrule import expand_rrule
from .base import (
    bool_col,
    changed_since_sql,
    dump_json,
    load_json,
    row_to_dict,
    rows_to_dicts,
    sync_page_cursor,
)


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

    When ``ev.tz`` is set, recurrence expansion happens in the event's
    wall-clock zone so DST transitions don't drift the recurring
    occurrences by an hour (see :func:`expand_rrule`). UTC stays the
    storage shape; the conversion is only used internally during the
    expansion step.
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
            tz=ev.tz,
        )
        for s, e in occs:
            if s == ev.start and e == ev.end:
                out.append(ev)
            else:
                out.append(
                    replace(
                        ev,
                        start=s,
                        end=e,
                        id=f"{ev.id}{OCCURRENCE_ID_SEPARATOR}{s.isoformat()}",
                    )
                )
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


#: SQLite's default ``SQLITE_MAX_VARIABLE_NUMBER`` is 999 host
#: parameters per statement; 500 leaves headroom for any extra bound
#: values a caller adds around the ``IN`` list.
_MAX_SQL_PARAMS = 500


def _chunks(items: Sequence[str], size: int = _MAX_SQL_PARAMS) -> Iterator[list[str]]:
    """Yield ``items`` in slices of at most ``size`` elements.

    Used to keep an ``IN (...)`` placeholder list under SQLite's
    host-parameter ceiling without degrading into an N+1 query.
    """
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


# ─── Personal calendars ───────────────────────────────────────────────────


@runtime_checkable
class AbstractCalendarRepo(Protocol):
    async def save_calendar(self, calendar: Calendar) -> Calendar: ...
    async def get_calendar(self, calendar_id: str) -> Calendar | None: ...
    async def list_calendars_for_user(self, username: str) -> list[Calendar]: ...
    async def list_all_calendars(self) -> list[Calendar]: ...
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
    # Personal-calendar RSVPs (cross-household invites only). Local
    # household members never RSVP — invitee scope is enforced at the
    # service layer.
    async def upsert_rsvp(self, rsvp: CalendarRSVP) -> None: ...
    async def remove_rsvp(
        self,
        event_id: str,
        user_id: str,
        *,
        occurrence_at: str | None = None,
    ) -> None: ...
    async def list_rsvps(
        self,
        event_id: str,
        *,
        occurrence_at: str | None = None,
    ) -> list[CalendarRSVP]: ...
    async def get_event_by_remote(
        self,
        *,
        remote_instance_id: str,
        remote_event_id: str,
    ) -> CalendarEvent | None: ...
    # Find personal-calendar rows that mirror a given source event
    # (e.g. a space calendar event whose RSVP was accepted by the
    # owner). Used by :class:`SpaceRsvpMirrorBridge` to refresh /
    # delete mirrors when the source changes.
    async def list_mirrors_of(
        self,
        source_event_id: str,
    ) -> list[CalendarEvent]: ...
    # Resolve the full sibling set of a household fan-out by its
    # client-minted ``client_event_uuid`` — the server is the authority
    # on which rows belong to a shared event.
    async def list_copies_for_client_event_uuids(
        self,
        uuids: Sequence[str],
    ) -> dict[str, list[CalendarEventCopy]]: ...
    async def find_by_client_event_uuid(
        self,
        calendar_id: str,
        client_event_uuid: str,
    ) -> CalendarEvent | None: ...


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

    async def list_all_calendars(self) -> list[Calendar]:
        """Every personal calendar on the instance, ordered owner-then-name.

        Used by the household-wide calendar picker in the SPA so a
        member can switch between their own calendar and another
        member's. The household = the instance, so an unfiltered
        ``SELECT`` is the right scope.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM calendars ORDER BY owner_username, name",
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
                rsvp_enabled, cover_url, location, tz, origin,
                remote_event_id, remote_instance_id, created_by,
                client_event_uuid,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
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
                rsvp_enabled=excluded.rsvp_enabled,
                cover_url=excluded.cover_url,
                location=excluded.location,
                tz=excluded.tz,
                origin=excluded.origin,
                remote_event_id=excluded.remote_event_id,
                remote_instance_id=excluded.remote_instance_id,
                client_event_uuid=excluded.client_event_uuid,
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
                int(event.rsvp_enabled),
                event.cover_url,
                event.location,
                event.tz,
                event.origin,
                event.remote_event_id,
                event.remote_instance_id,
                event.created_by,
                event.client_event_uuid,
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

    # ── Personal-calendar RSVPs ───────────────────────────────────────

    async def upsert_rsvp(self, rsvp: CalendarRSVP) -> None:
        if not rsvp.occurrence_at:
            raise ValueError("CalendarRSVP.occurrence_at must be set")
        await self._db.enqueue(
            """
            INSERT INTO calendar_event_rsvps(
                event_id, user_id, occurrence_at, status, updated_at
            ) VALUES(?,?,?,?,?)
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
        occurrence_at: str | None = None,
    ) -> None:
        if occurrence_at is None:
            await self._db.enqueue(
                "DELETE FROM calendar_event_rsvps WHERE event_id=? AND user_id=?",
                (event_id, user_id),
            )
        else:
            await self._db.enqueue(
                "DELETE FROM calendar_event_rsvps "
                "WHERE event_id=? AND user_id=? AND occurrence_at=?",
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
                "SELECT * FROM calendar_event_rsvps WHERE event_id=?",
                (event_id,),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM calendar_event_rsvps "
                "WHERE event_id=? AND occurrence_at=?",
                (event_id, occurrence_at),
            )
        return [
            CalendarRSVP(
                event_id=r["event_id"],
                user_id=r["user_id"],
                status=r["status"],
                updated_at=r["updated_at"],
                occurrence_at=r["occurrence_at"] or "",
            )
            for r in rows_to_dicts(rows)
        ]

    async def get_event_by_remote(
        self,
        *,
        remote_instance_id: str,
        remote_event_id: str,
    ) -> CalendarEvent | None:
        """Find a previously-mirrored remote invite. Used by inbound
        federation to apply UPDATED / DELETED to the existing row."""
        row = await self._db.fetchone(
            "SELECT * FROM calendar_events "
            "WHERE remote_instance_id=? AND remote_event_id=?",
            (remote_instance_id, remote_event_id),
        )
        return _row_to_event(row_to_dict(row))

    async def list_mirrors_of(
        self,
        source_event_id: str,
    ) -> list[CalendarEvent]:
        """List personal-calendar rows mirroring a given source event.

        Used by :class:`SpaceRsvpMirrorBridge` when a space event is
        edited or deleted — every personal mirror with
        ``mirrored_from = source_event_id`` is refreshed or dropped.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM calendar_events WHERE mirrored_from=?",
            (source_event_id,),
        )
        return [e for e in (_row_to_event(d) for d in rows_to_dicts(rows)) if e]

    async def list_copies_for_client_event_uuids(
        self,
        uuids: Sequence[str],
    ) -> dict[str, list[CalendarEventCopy]]:
        """Resolve every local sibling row of a household fan-out.

        A household event shared with N members is stored as one
        ``calendar_events`` row per member's personal calendar, all
        stamped with the same client-minted ``client_event_uuid``. The
        server is the authority on that set — a caller must never
        reconstruct it from whichever calendars happen to be visible.

        The ``origin='local' AND mirrored_from IS NULL AND
        c.calendar_type='personal'`` filter is required, not defensive:
        :mod:`socialhome.services.federation_inbound.personal_calendar`
        persists inbound peer events carrying the *peer's*
        ``client_event_uuid`` with ``origin='remote_invite'``. Those
        rows are not ours to edit or delete, and neither are mirrors of
        a source event (``mirrored_from``) or space-calendar rows.

        This reader is DELIBERATELY NARROWER than the shared
        ``client_event_uuid IS NOT NULL AND origin='local' AND
        mirrored_from IS NULL`` predicate that scopes 0047's de-dup,
        the ``ux_calendar_events_fanout`` index and
        :meth:`find_by_client_event_uuid`: it adds
        ``c.calendar_type='personal'`` on the JOINed ``calendars`` row.
        That is not a lock-step violation — narrower is the safe
        direction. A partial index cannot carry a predicate on another
        table, so the constraint could not express it even if we wanted
        it to; and a "copy" is a household personal-calendar fan-out by
        definition, so a row on a non-personal calendar is not one and
        must never be offered to the SPA as a PATCH target. The
        dangerous direction is the opposite one — a row the DB
        constrains but no reader can see — and that stays impossible.

        Rows are ordered by ``created_at, id`` so the fan-out's copy
        list is deterministic. There is at most one local copy per
        calendar — that invariant is enforced in the schema by the
        partial unique index ``ux_calendar_events_fanout``
        (``0047_calendar_fanout_dedupe.sql``), not by this ordering.

        Queries are chunked at :data:`_MAX_SQL_PARAMS` uuids per
        statement (SQLite host-parameter ceiling); one statement per
        chunk, never one per uuid.
        """
        wanted = list(dict.fromkeys(u.strip() for u in uuids if u and u.strip()))
        if not wanted:
            return {}
        out: dict[str, list[CalendarEventCopy]] = {}
        for chunk in _chunks(wanted):
            placeholders = ",".join("?" for _ in chunk)
            rows = await self._db.fetchall(
                f"""
                SELECT e.id, e.calendar_id, e.client_event_uuid, c.owner_username
                  FROM calendar_events e
                  JOIN calendars c ON c.id = e.calendar_id
                 WHERE e.client_event_uuid IN ({placeholders})
                   AND e.origin = 'local'
                   AND e.mirrored_from IS NULL
                   AND c.calendar_type = 'personal'
                 ORDER BY e.created_at, e.id
                """,
                tuple(chunk),
            )
            for row in rows_to_dicts(rows):
                out.setdefault(row["client_event_uuid"], []).append(
                    CalendarEventCopy(
                        event_id=row["id"],
                        calendar_id=row["calendar_id"],
                        owner_username=row["owner_username"],
                    )
                )
        return out

    async def find_by_client_event_uuid(
        self,
        calendar_id: str,
        client_event_uuid: str,
    ) -> CalendarEvent | None:
        """Find the local row on ``calendar_id`` for a fan-out uuid.

        At most one such row exists: the partial unique index
        ``ux_calendar_events_fanout``
        (``0047_calendar_fanout_dedupe.sql``) enforces one local copy
        per ``(calendar_id, client_event_uuid)``. The
        ``ORDER BY created_at, id LIMIT 1`` costs nothing and keeps the
        answer deterministic should that guard ever be absent (e.g. a
        restored pre-0047 backup). ``remote_invite`` rows are excluded:
        they carry the *peer's* uuid and are not ours to edit. So are
        ``mirrored_from``-bearing rows — the personal mirror of a space
        event (``SpaceRsvpMirrorBridge``) is ``origin='local'`` but is
        not a fan-out copy, and a PATCH can stamp a uuid onto it.

        The predicate here, the ``ux_calendar_events_fanout`` index
        predicate and 0047's de-dup scope are deliberately identical
        (``client_event_uuid IS NOT NULL AND origin='local' AND
        mirrored_from IS NULL``) and must stay in lock-step — see the
        header of ``0047_calendar_fanout_dedupe.sql``.
        :meth:`list_copies_for_client_event_uuids` is the one
        deliberate exception: it adds ``calendar_type='personal'`` on
        the joined calendar row, which is narrower — see its docstring
        for why that direction is safe.
        """
        row = await self._db.fetchone(
            "SELECT * FROM calendar_events "
            "WHERE calendar_id=? AND client_event_uuid=? AND origin='local' "
            "AND mirrored_from IS NULL "
            "ORDER BY created_at, id LIMIT 1",
            (calendar_id, client_event_uuid),
        )
        return _row_to_event(row_to_dict(row))


# ─── Space calendars ──────────────────────────────────────────────────────


@runtime_checkable
class AbstractSpaceCalendarRepo(Protocol):
    async def save_event(
        self,
        event: CalendarEvent,
        *,
        space_id: str,
    ) -> bool: ...
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
    ) -> list[CalendarEvent]:
        """The space's live events overlapping ``[start, end)`` (recurring
        ones expanded)."""
        ...

    async def list_events_sync_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[CalendarEvent], int | None]:
        """One page of the space's live events **as stored** for a §25.6
        sync — a recurring event once, with its rule (never expanded into
        occurrences), whatever its date: nothing prunes calendar events, so
        a window would hide rows the space still shows. ``since``: only
        those whose change stamp is above it (migration 0086). ``(rows,
        next_cursor)`` paging, keyset on the row id."""
        ...

    async def list_events_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[CalendarEvent]: ...
    async def delete_event(
        self, event_id: str, *, space_id: str, deleted_by: str = ""
    ) -> bool: ...
    async def is_event_deleted(self, event_id: str, *, space_id: str) -> bool: ...
    async def tombstone_event(
        self,
        event_id: str,
        *,
        space_id: str,
        created_by: str,
        created_at: str = "",
        deleted_by: str = "",
    ) -> bool: ...
    async def list_event_tombstones_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[SpaceRowTombstone], int | None]:
        """One page of the space's event tombstones for the §25.6
        ``calendar_deleted`` resource, keyset on the row id."""
        ...

    async def upsert_rsvp(self, rsvp: CalendarRSVP, *, space_id: str) -> bool: ...
    async def remove_rsvp(
        self,
        event_id: str,
        user_id: str,
        *,
        occurrence_at: str,
        space_id: str,
    ) -> bool: ...
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
        space_id: str,
    ) -> None: ...
    async def flush_pending_rsvps(
        self,
        event_id: str,
        *,
        space_id: str,
    ) -> list[CalendarRSVP]: ...
    async def gc_pending_rsvps(self, *, older_than_iso: str) -> int: ...

    # ── Phase D: reminders ─────────────────────────────────────────────
    async def upsert_reminder(self, reminder: EventReminder) -> None: ...
    async def remove_reminder(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: str,
        minutes_before: int,
    ) -> None: ...
    async def list_reminders(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: str | None = None,
    ) -> list[EventReminder]: ...
    async def list_due_reminders(
        self,
        *,
        now_iso: str,
        limit: int = 100,
    ) -> list[EventReminder]: ...
    async def mark_reminder_sent(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: str,
        minutes_before: int,
        sent_at: str,
    ) -> None: ...

    # ── Phase F: iCal feed tokens ─────────────────────────────────────
    async def upsert_feed_token(
        self,
        *,
        user_id: str,
        space_id: str,
        token: str,
    ) -> None: ...
    async def get_feed_token(
        self,
        *,
        user_id: str,
        space_id: str,
    ) -> str | None: ...
    async def get_user_for_feed_token(
        self,
        token: str,
    ) -> tuple[str, str] | None: ...
    async def revoke_feed_token(
        self,
        *,
        user_id: str,
        space_id: str,
    ) -> None: ...


class SqliteSpaceCalendarRepo:
    """SQLite-backed :class:`AbstractSpaceCalendarRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save_event(
        self,
        event: CalendarEvent,
        *,
        space_id: str,
    ) -> bool:
        """Upsert a space calendar event into ``space_id``.

        ``space_id`` is authoritative (§24.11): a conflict on an id
        that already belongs to another space is refused and reported
        as ``False`` — the gated space decides where the write lands,
        never the bare row id in the payload.
        """
        n = await self._db.enqueue_rowcount(
            """
            INSERT INTO space_calendar_events(
                id, space_id, summary, description, start_dt, end_dt,
                all_day, attendees_json, rrule, capacity, cover_url,
                location, tz, announce_in_feed, created_by, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
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
                cover_url=excluded.cover_url,
                location=excluded.location,
                tz=excluded.tz,
                announce_in_feed=excluded.announce_in_feed,
                updated_at=datetime('now')
            WHERE space_calendar_events.space_id = excluded.space_id
              AND space_calendar_events.deleted_at IS NULL
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
                event.cover_url,
                event.location,
                event.tz,
                int(event.announce_in_feed),
                event.created_by,
                None,
                None,
            ),
        )
        return n > 0

    async def get_event(
        self,
        event_id: str,
    ) -> tuple[str, CalendarEvent] | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_calendar_events WHERE id=? AND deleted_at IS NULL",
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
            "SELECT * FROM space_calendar_events"
            " WHERE space_id=? AND deleted_at IS NULL"
            " AND ((rrule IS NULL AND start_dt < ? AND end_dt > ?)"
            " OR (rrule IS NOT NULL AND start_dt < ?))"
            " ORDER BY start_dt",
            (space_id, _iso(end), _iso(start), _iso(end)),
        )
        events = [_row_to_space_event(d) for d in rows_to_dicts(rows)]
        return _expand_window(events, start=start, end=end)

    async def list_events_sync_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[CalendarEvent], int | None]:
        changed, changed_params = changed_since_sql("sync_seq", since)
        rows = rows_to_dicts(
            await self._db.fetchall(
                "SELECT rowid AS sync_rowid, * FROM space_calendar_events"
                " WHERE space_id=? AND deleted_at IS NULL"
                + changed
                + " AND rowid > ? ORDER BY rowid LIMIT ?",
                (space_id, *changed_params, cursor or 0, int(limit)),
            )
        )
        return [_row_to_space_event(r) for r in rows], sync_page_cursor(rows, limit)

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

        Shape invariant: ``space_calendar_events.updated_at`` is always
        SQLite's naive ``datetime('now')`` (``save_event`` never passes a
        Python value through its ``COALESCE``), and the only ``since``
        supplied today is the epoch cursor. A caller mixing in a tz-aware
        ISO cursor must wrap both sides in ``datetime()`` — a raw TEXT
        compare sorts "T" (0x54) above " " (0x20) and would skip same-day
        rows on resume.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM space_calendar_events "
            "WHERE space_id=? AND updated_at > ? AND deleted_at IS NULL "
            "ORDER BY updated_at ASC LIMIT ?",
            (space_id, since, int(limit)),
        )
        return [_row_to_space_event(d) for d in rows_to_dicts(rows)]

    async def delete_event(
        self, event_id: str, *, space_id: str, deleted_by: str = ""
    ) -> bool:
        """Delete an event of ``space_id``. ``False`` = not live there.

        The row stays as a tombstone (migration 0085): content blanked,
        ``deleted_at`` / ``deleted_by`` set — the §25.6 ``calendar_deleted``
        resource streams it to a household that missed the delete, and no
        upsert brings the id back. Its RSVPs and reminders go with it (the
        0085 trigger).
        """
        n = await self._db.enqueue_rowcount(
            """
            UPDATE space_calendar_events
               SET deleted_at=datetime('now'), deleted_by=?,
                   summary='', description=NULL, start_dt='', end_dt='',
                   attendees_json='[]', rrule=NULL, capacity=NULL,
                   notify_before_minutes=NULL, cover_url=NULL, location=NULL
             WHERE id=? AND space_id=? AND deleted_at IS NULL
            """,
            (deleted_by or None, event_id, space_id),
        )
        return n > 0

    async def is_event_deleted(self, event_id: str, *, space_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM space_calendar_events WHERE id=? AND space_id=?"
            " AND deleted_at IS NOT NULL",
            (event_id, space_id),
        )
        return row is not None

    async def tombstone_event(
        self,
        event_id: str,
        *,
        space_id: str,
        created_by: str,
        created_at: str = "",
        deleted_by: str = "",
    ) -> bool:
        """Record a delete of an event never held here: a content-free stub
        row, so a stale copy streamed later cannot create it.

        Insert-only — an id held already (live or tombstoned, any space) is
        never touched; ``False`` says nothing was written. The caller must
        have proven the id is this space's (owner-bound to ``created_by`` in
        ``space_id``): event ids are global.
        """
        n = await self._db.enqueue_rowcount(
            "INSERT INTO space_calendar_events(id, space_id, summary, start_dt,"
            " end_dt, created_by, created_at, deleted_at, deleted_by)"
            " VALUES(?, ?, '', '', '', ?, COALESCE(NULLIF(?, ''), datetime('now')),"
            " datetime('now'), ?)"
            " ON CONFLICT(id) DO NOTHING",
            (event_id, space_id, created_by, created_at, deleted_by or None),
        )
        return n > 0

    async def list_event_tombstones_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[SpaceRowTombstone], int | None]:
        changed, changed_params = changed_since_sql("sync_seq", since)
        rows = rows_to_dicts(
            await self._db.fetchall(
                "SELECT rowid AS sync_rowid, id, created_by, created_at,"
                " deleted_at, deleted_by FROM space_calendar_events"
                " WHERE space_id=? AND deleted_at IS NOT NULL"
                + changed
                + " AND rowid > ? ORDER BY rowid LIMIT ?",
                (space_id, *changed_params, cursor or 0, int(limit)),
            )
        )
        return [
            SpaceRowTombstone(
                id=r["id"],
                owner=r["created_by"],
                created_at=r["created_at"] or "",
                deleted_at=r["deleted_at"],
                deleted_by=r["deleted_by"] or "",
            )
            for r in rows
        ], sync_page_cursor(rows, limit)

    # ── RSVPs ──────────────────────────────────────────────────────────

    async def upsert_rsvp(self, rsvp: CalendarRSVP, *, space_id: str) -> bool:
        """Upsert an RSVP, scoped through its parent event (§24.11).

        ``space_calendar_rsvps`` carries no ``space_id`` of its own, so
        the parent event is resolved *inside* the same statement: the
        row is written only if ``rsvp.event_id`` names an event in
        ``space_id``. ``False`` means it did not and nothing was
        written — never pre-read the parent in the caller.
        """
        if rsvp.status not in RSVPStatus.ALL:
            raise ValueError(f"invalid RSVP status {rsvp.status!r}")
        if not rsvp.occurrence_at:
            raise ValueError("CalendarRSVP.occurrence_at must be set")
        n = await self._db.enqueue_rowcount(
            """
            INSERT INTO space_calendar_rsvps(
                event_id, user_id, occurrence_at, status, updated_at
            )
            SELECT ?, ?, ?, ?, COALESCE(?, datetime('now'))
             WHERE EXISTS (
                 SELECT 1 FROM space_calendar_events
                  WHERE id=? AND space_id=? AND deleted_at IS NULL
             )
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
                rsvp.event_id,
                space_id,
            ),
        )
        return n > 0

    async def remove_rsvp(
        self,
        event_id: str,
        user_id: str,
        *,
        occurrence_at: str,
        space_id: str,
    ) -> bool:
        """Delete an RSVP, scoped through its parent event (§24.11)."""
        n = await self._db.enqueue_rowcount(
            """
            DELETE FROM space_calendar_rsvps
             WHERE event_id=? AND user_id=? AND occurrence_at=?
               AND EXISTS (
                   SELECT 1 FROM space_calendar_events WHERE id=? AND space_id=?
               )
            """,
            (event_id, user_id, occurrence_at, event_id, space_id),
        )
        return n > 0

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
        space_id: str,
    ) -> None:
        """Buffer an inbound RSVP whose event hasn't propagated yet.

        Idempotent: last-write-wins on (event_id, user_id, occurrence_at).
        Status ``"removed"`` represents a DELETE that arrived before its
        event — so when the event lands and we flush, the deletion is
        honoured (rather than the buffer resurrecting a stale RSVP).

        ``space_id`` is the space the §24.11 pipeline gated the sender
        on and is stored with the row, so the buffer cannot launder a
        cross-space write: :meth:`flush_pending_rsvps` only drains rows
        buffered under the same space as the event that landed.

        The key carries no space, so a row buffered for one space would
        block the same key for another. That is not a squat surface: the
        inbound handler accepts an RSVP only for a user seated on the
        sending household (§24.11 authorship), so the only household that
        can occupy ``(event, user, occurrence)`` is that user's own.
        """
        await self._db.enqueue(
            """
            INSERT INTO pending_federated_rsvps(
                event_id, user_id, occurrence_at, status, updated_at, space_id
            ) VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, user_id, occurrence_at) DO UPDATE SET
                status=excluded.status,
                updated_at=excluded.updated_at,
                space_id=excluded.space_id,
                received_at=datetime('now')
             -- A row buffered for another space is not ours to rewrite; a
             -- legacy NULL row is never flushed, so taking it over is safe.
             WHERE pending_federated_rsvps.space_id IS NULL
                OR pending_federated_rsvps.space_id = excluded.space_id
            """,
            (event_id, user_id, occurrence_at, status, updated_at, space_id),
        )

    async def flush_pending_rsvps(
        self,
        event_id: str,
        *,
        space_id: str,
    ) -> list[CalendarRSVP]:
        """Drain buffered RSVPs for ``event_id`` and apply them.

        Called when an event lands locally (either local create or
        inbound federation). Returns the list of applied RSVPs (excluding
        ``removed`` rows which result in a delete). The buffer rows are
        always cleared regardless of whether the apply succeeded —
        callers shouldn't see the same buffered RSVP twice.

        Only rows buffered under ``space_id`` are drained (§24.11) — a
        row buffered for another space stays put and ages out through
        :meth:`gc_pending_rsvps`, so the buffer can't be used to write
        into a space the sender was never gated on. Rows written before
        the ``space_id`` column existed carry NULL and likewise age out.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM pending_federated_rsvps WHERE event_id=? AND space_id=?",
            (event_id, space_id),
        )
        applied: list[CalendarRSVP] = []
        for r in rows:
            status = r["status"]
            occurrence_at = r["occurrence_at"]
            if status == "removed":
                await self.remove_rsvp(
                    event_id,
                    r["user_id"],
                    occurrence_at=occurrence_at,
                    space_id=space_id,
                )
            elif status in RSVPStatus.ALL:
                rsvp = CalendarRSVP(
                    event_id=event_id,
                    user_id=r["user_id"],
                    status=status,
                    updated_at=r["updated_at"],
                    occurrence_at=occurrence_at,
                )
                await self.upsert_rsvp(rsvp, space_id=space_id)
                applied.append(rsvp)
        if rows:
            await self._db.enqueue(
                "DELETE FROM pending_federated_rsvps WHERE event_id=? AND space_id=?",
                (event_id, space_id),
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

    # ── Phase D: reminders ─────────────────────────────────────────────

    async def upsert_reminder(self, reminder: EventReminder) -> None:
        if reminder.minutes_before < 0:
            raise ValueError("minutes_before must be >= 0")
        await self._db.enqueue(
            """
            INSERT INTO space_calendar_rsvp_reminders(
                event_id, user_id, occurrence_at, minutes_before, fire_at, sent_at
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(event_id, user_id, occurrence_at, minutes_before) DO UPDATE SET
                fire_at=excluded.fire_at,
                sent_at=excluded.sent_at
            """,
            (
                reminder.event_id,
                reminder.user_id,
                reminder.occurrence_at,
                int(reminder.minutes_before),
                reminder.fire_at,
                reminder.sent_at,
            ),
        )

    async def remove_reminder(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: str,
        minutes_before: int,
    ) -> None:
        await self._db.enqueue(
            """
            DELETE FROM space_calendar_rsvp_reminders
             WHERE event_id=? AND user_id=? AND occurrence_at=? AND minutes_before=?
            """,
            (event_id, user_id, occurrence_at, int(minutes_before)),
        )

    async def list_reminders(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: str | None = None,
    ) -> list[EventReminder]:
        if occurrence_at is None:
            rows = await self._db.fetchall(
                "SELECT * FROM space_calendar_rsvp_reminders "
                "WHERE event_id=? AND user_id=? "
                "ORDER BY occurrence_at, minutes_before",
                (event_id, user_id),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM space_calendar_rsvp_reminders "
                "WHERE event_id=? AND user_id=? AND occurrence_at=? "
                "ORDER BY minutes_before",
                (event_id, user_id, occurrence_at),
            )
        return [
            EventReminder(
                event_id=r["event_id"],
                user_id=r["user_id"],
                occurrence_at=r["occurrence_at"],
                minutes_before=int(r["minutes_before"]),
                fire_at=r["fire_at"],
                sent_at=r["sent_at"],
            )
            for r in rows
        ]

    async def list_due_reminders(
        self,
        *,
        now_iso: str,
        limit: int = 100,
    ) -> list[EventReminder]:
        """Reminders whose ``fire_at <= now`` and not yet sent.

        Used by :class:`CalendarReminderScheduler` to drain a small
        batch each tick (default 30 s).
        """
        rows = await self._db.fetchall(
            """
            SELECT * FROM space_calendar_rsvp_reminders
             WHERE sent_at IS NULL AND fire_at <= ?
             ORDER BY fire_at ASC
             LIMIT ?
            """,
            (now_iso, int(limit)),
        )
        return [
            EventReminder(
                event_id=r["event_id"],
                user_id=r["user_id"],
                occurrence_at=r["occurrence_at"],
                minutes_before=int(r["minutes_before"]),
                fire_at=r["fire_at"],
                sent_at=r["sent_at"],
            )
            for r in rows
        ]

    async def mark_reminder_sent(
        self,
        *,
        event_id: str,
        user_id: str,
        occurrence_at: str,
        minutes_before: int,
        sent_at: str,
    ) -> None:
        await self._db.enqueue(
            """
            UPDATE space_calendar_rsvp_reminders
               SET sent_at=?
             WHERE event_id=? AND user_id=? AND occurrence_at=? AND minutes_before=?
            """,
            (sent_at, event_id, user_id, occurrence_at, int(minutes_before)),
        )

    # ── Phase F: iCal feed tokens ─────────────────────────────────────

    async def upsert_feed_token(
        self,
        *,
        user_id: str,
        space_id: str,
        token: str,
    ) -> None:
        """Persist a feed token. Idempotent: a regenerate replaces the
        previous (user, space) row, also clearing any prior revoke.

        The raw token is SHA-256 hashed before storage — server-side dumps
        of ``space_calendar_feed_tokens`` no longer disclose live feed
        URLs. The raw token is still returned to the user at generation
        time (only the persisted form changes)."""
        await self._db.enqueue(
            """
            INSERT INTO space_calendar_feed_tokens(user_id, space_id, token_hash)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id, space_id) DO UPDATE SET
                token_hash=excluded.token_hash,
                created_at=datetime('now'),
                revoked_at=NULL
            """,
            (user_id, space_id, sha256_token_hash(token)),
        )

    async def get_feed_token(
        self,
        *,
        user_id: str,
        space_id: str,
    ) -> str | None:
        """Return the *hashed* feed token, or ``None`` if absent / revoked.

        Note: the raw token is no longer recoverable from storage. This
        accessor exists for tests and existence checks only — to mint a
        fresh raw token, call :meth:`upsert_feed_token` again."""
        row = await self._db.fetchone(
            "SELECT token_hash FROM space_calendar_feed_tokens "
            "WHERE user_id=? AND space_id=? AND revoked_at IS NULL",
            (user_id, space_id),
        )
        d = row_to_dict(row)
        return d["token_hash"] if d else None

    async def get_user_for_feed_token(
        self,
        token: str,
    ) -> tuple[str, str] | None:
        """Resolve a *raw* feed token to ``(user_id, space_id)``, or
        ``None`` if the token is unknown or revoked. The lookup hashes
        the token first because storage holds only the hash."""
        row = await self._db.fetchone(
            "SELECT user_id, space_id FROM space_calendar_feed_tokens "
            "WHERE token_hash=? AND revoked_at IS NULL",
            (sha256_token_hash(token),),
        )
        d = row_to_dict(row)
        if d is None:
            return None
        return d["user_id"], d["space_id"]

    async def revoke_feed_token(
        self,
        *,
        user_id: str,
        space_id: str,
    ) -> None:
        await self._db.enqueue(
            "UPDATE space_calendar_feed_tokens "
            "SET revoked_at=datetime('now') "
            "WHERE user_id=? AND space_id=? AND revoked_at IS NULL",
            (user_id, space_id),
        )


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
        rsvp_enabled=bool_col(row.get("rsvp_enabled", 0)),
        cover_url=row.get("cover_url"),
        location=row.get("location"),
        origin=row.get("origin") or "local",
        remote_event_id=row.get("remote_event_id"),
        remote_instance_id=row.get("remote_instance_id"),
        tz=row.get("tz") or "UTC",
        client_event_uuid=row.get("client_event_uuid"),
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
        cover_url=row.get("cover_url"),
        location=row.get("location"),
        tz=row.get("tz") or "UTC",
        announce_in_feed=bool_col(row.get("announce_in_feed", 0)),
    )
