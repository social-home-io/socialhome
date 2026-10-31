"""Group-session repo — the ``app_group_sessions`` table (0136, v_76).

One row per table this household knows: the hub's own (authoritative) or
a copy a player's household keeps as the hub last sent it. Every value is
a bound ``?`` parameter. The roster rides as JSON; nothing here reads
inside it — who is at which table is found through the people's own
``app_pending_sessions`` rows.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import orjson

from ..db import AsyncDatabase
from ..domain.app_group import GROUP_ENDED, GROUP_LOBBY, GroupSeat, GroupSession


@runtime_checkable
class AbstractAppGroupRepo(Protocol):
    async def get(self, session_id: str) -> GroupSession | None: ...
    async def save(self, table: GroupSession) -> None: ...
    async def save_newer(self, table: GroupSession) -> bool: ...
    async def delete(self, session_id: str) -> None: ...
    async def list_for_app(self, app_id: str) -> list[GroupSession]: ...
    async def list_hosted(
        self, app_id: str, hub_instance: str, host_user: str, *, since: str
    ) -> list[GroupSession]: ...
    async def count_open_from(self, hub_instance: str) -> int: ...
    async def list_lobby_expired(self, *, before: str) -> list[GroupSession]: ...
    async def prune(self, *, ended_before: str, quiet_before: str) -> int: ...


def _seat_to_json(e: GroupSeat) -> dict:
    return {
        "seat": e.seat,
        "instance": e.instance,
        "user": e.user,
        "name": e.name,
        "household": e.household_name,
        "state": e.state,
        "joined_at": e.joined_at,
    }


def _seat_from_json(d: dict) -> GroupSeat:
    return GroupSeat(
        instance=str(d["instance"]),
        user=str(d["user"]),
        name=str(d["name"]),
        state=str(d["state"]),
        household_name=d.get("household"),
        seat=int(d.get("seat") or 0),
        joined_at=d.get("joined_at"),
    )


def _row_to_table(row) -> GroupSession:
    return GroupSession(
        session_id=str(row["session_id"]),
        app_id=str(row["app_id"]),
        host_instance=str(row["host_instance"]),
        host_user=str(row["host_user"]),
        hub_instance=str(row["hub_instance"]),
        space_id=row["space_id"],
        state=str(row["state"]),
        end_reason=row["end_reason"],
        min_players=int(row["min_players"]),
        max_players=int(row["max_players"]),
        late_join=bool(row["late_join"]),
        note=row["note"],
        roster=tuple(_seat_from_json(d) for d in orjson.loads(row["roster_json"])),
        roster_rev=int(row["roster_rev"]),
        seq=int(row["seq"]),
        created_at=str(row["created_at"]),
        started_at=row["started_at"],
        expires_at=str(row["expires_at"]),
        updated_at=str(row["updated_at"]),
    )


_COLUMNS = (
    "session_id, app_id, host_instance, host_user, hub_instance, space_id,"
    " state, end_reason, min_players, max_players, late_join, note,"
    " roster_json, roster_rev, created_at, started_at, expires_at, updated_at"
)

_UPSERT = f"""INSERT INTO app_group_sessions ({_COLUMNS})
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(session_id) DO UPDATE SET
      host_instance = excluded.host_instance,
      host_user = excluded.host_user,
      state = excluded.state,
      end_reason = excluded.end_reason,
      min_players = excluded.min_players,
      max_players = excluded.max_players,
      late_join = excluded.late_join,
      note = excluded.note,
      roster_json = excluded.roster_json,
      roster_rev = excluded.roster_rev,
      started_at = excluded.started_at,
      expires_at = excluded.expires_at,
      updated_at = excluded.updated_at
    -- A table's app, hub and space never change: a row for the same id
    -- from anyone else changes nothing.
    WHERE app_group_sessions.hub_instance = excluded.hub_instance
      AND app_group_sessions.app_id = excluded.app_id"""


def _args(t: GroupSession) -> tuple:
    return (
        t.session_id,
        t.app_id,
        t.host_instance,
        t.host_user,
        t.hub_instance,
        t.space_id,
        t.state,
        t.end_reason,
        t.min_players,
        t.max_players,
        1 if t.late_join else 0,
        t.note,
        orjson.dumps([_seat_to_json(e) for e in t.roster]).decode(),
        t.roster_rev,
        t.created_at,
        t.started_at,
        t.expires_at,
        t.updated_at,
    )


class SqliteAppGroupRepo:
    """SQLite-backed :class:`AbstractAppGroupRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get(self, session_id: str) -> GroupSession | None:
        row = await self._db.fetchone(
            "SELECT * FROM app_group_sessions WHERE session_id = ?", (session_id,)
        )
        return _row_to_table(row) if row is not None else None

    async def save(self, table: GroupSession) -> None:
        """Keep *table* as it is now — the hub's own row after a change."""
        await self._db.enqueue(_UPSERT, _args(table))

    async def save_newer(self, table: GroupSession) -> bool:
        """Keep a hub's snapshot only when it is newer (``roster_rev``)
        than the copy held, and from the same hub for the same app. ``True``
        when kept — one atomic write, so two snapshots racing keep the
        newest."""
        changed = await self._db.enqueue_rowcount(
            _UPSERT + " AND excluded.roster_rev > app_group_sessions.roster_rev",
            _args(table),
        )
        return changed == 1

    async def delete(self, session_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM app_group_sessions WHERE session_id = ?", (session_id,)
        )

    async def list_for_app(self, app_id: str) -> list[GroupSession]:
        """Every table of *app_id* here — what an uninstall ends."""
        rows = await self._db.fetchall(
            "SELECT * FROM app_group_sessions WHERE app_id = ? ORDER BY created_at",
            (app_id,),
        )
        return [_row_to_table(r) for r in rows]

    async def list_hosted(
        self, app_id: str, hub_instance: str, host_user: str, *, since: str
    ) -> list[GroupSession]:
        """The tables *host_user* opened on *hub_instance* in *app_id* since
        *since* — the caps (bounded by them: ≤ 10 a day)."""
        rows = await self._db.fetchall(
            """SELECT * FROM app_group_sessions
               WHERE app_id = ? AND hub_instance = ? AND host_user = ?
                 AND created_at >= ?
               ORDER BY created_at""",
            (app_id, hub_instance, host_user, since),
        )
        return [_row_to_table(r) for r in rows]

    async def count_open_from(self, hub_instance: str) -> int:
        """Tables from *hub_instance* that haven't ended — a receiver's
        bound on one hub."""
        return int(
            await self._db.fetchval(
                """SELECT COUNT(*) FROM app_group_sessions
                   WHERE hub_instance = ? AND state != ?""",
                (hub_instance, GROUP_ENDED),
                default=0,
            )
        )

    async def list_lobby_expired(self, *, before: str) -> list[GroupSession]:
        """Tables still waiting for players whose time ran out before
        *before*."""
        rows = await self._db.fetchall(
            """SELECT * FROM app_group_sessions
               WHERE state = ? AND expires_at < ?
               ORDER BY expires_at""",
            (GROUP_LOBBY, before),
        )
        return [_row_to_table(r) for r in rows]

    async def prune(self, *, ended_before: str, quiet_before: str) -> int:
        """Forget tables that ended before *ended_before* and any other
        nobody touched since *quiet_before*. Returns how many."""
        where = (
            " WHERE (state = ? AND updated_at < ?) OR (state != ? AND updated_at < ?)"
        )
        args = (GROUP_ENDED, ended_before, GROUP_ENDED, quiet_before)
        before = await self._db.fetchval(
            "SELECT COUNT(*) FROM app_group_sessions" + where, args, default=0
        )
        await self._db.enqueue("DELETE FROM app_group_sessions" + where, args)
        return int(before)
