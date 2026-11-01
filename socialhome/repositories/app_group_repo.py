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
from ..domain.app_group import (
    GROUP_ENDED,
    GROUP_LOBBY,
    GroupHeld,
    GroupSeat,
    GroupSession,
    SeatKey,
    TableCard,
)
from ..domain.apps import SESSION_ACTIVE


@runtime_checkable
class AbstractAppGroupRepo(Protocol):
    async def get(self, session_id: str) -> GroupSession | None: ...
    async def save(self, table: GroupSession) -> None: ...
    async def save_newer(self, table: GroupSession) -> bool: ...
    async def delete(self, session_id: str) -> None: ...
    async def list_for_app(self, app_id: str) -> list[GroupSession]: ...
    async def list_for_space(self, space_id: str) -> list[GroupSession]: ...
    async def list_hosted(
        self, app_id: str, hub_instance: str, opener_user: str, *, since: str
    ) -> list[GroupSession]: ...
    async def count_open_from(self, hub_instance: str) -> int: ...
    async def count_seated(
        self, user_id: str, app_ids: list[str]
    ) -> dict[str, int]: ...
    async def list_lobby_expired(self, *, before: str) -> list[GroupSession]: ...
    async def prune(self, *, ended_before: str, quiet_before: str) -> int: ...
    async def get_seat_key(self, session_id: str) -> str | None: ...
    async def set_seat_key(self, session_id: str, value: str) -> bool: ...
    async def raise_seq(self, session_id: str, seq: int, *, now: str) -> None: ...
    async def hold_group_message(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        *,
        from_seat: int,
        seq: int,
        held: dict,
        now: str,
    ) -> bool: ...
    async def take_group_held(self, app_id: str, user_id: str) -> list[GroupHeld]: ...


def _seat_to_json(e: GroupSeat) -> dict:
    return {
        "seat": e.seat,
        "instance": e.instance,
        "user": e.user,
        "name": e.name,
        "household": e.household_name,
        "state": e.state,
        "joined_at": e.joined_at,
        "seat_key": e.seat_key.to_wire() if e.seat_key is not None else None,
        "user_id": e.user_id,
        "identity_pk": e.identity_pk,
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
        seat_key=SeatKey.from_wire(d["seat_key"]) if d.get("seat_key") else None,
        user_id=d.get("user_id"),
        identity_pk=d.get("identity_pk"),
    )


def _card_to_json(card: TableCard | None) -> str | None:
    if card is None:
        return None
    return orjson.dumps(
        {"seated": card.seated, "announce": card.announce, "seen_at": card.seen_at}
    ).decode()


def _card_from_json(raw: str | None) -> TableCard | None:
    if not raw:
        return None
    d = orjson.loads(raw)
    return TableCard(
        seated=int(d.get("seated") or 0),
        announce=bool(d.get("announce")),
        seen_at=str(d.get("seen_at") or ""),
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
        opener_user=row["opener_user"],
        card=_card_from_json(row["card_json"]),
    )


_COLUMNS = (
    "session_id, app_id, host_instance, host_user, hub_instance, space_id,"
    " state, end_reason, min_players, max_players, late_join, note,"
    " roster_json, roster_rev, created_at, started_at, expires_at, updated_at,"
    " opener_user, card_json"
)

_UPSERT = f"""INSERT INTO app_group_sessions ({_COLUMNS})
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
      updated_at = excluded.updated_at,
      card_json = excluded.card_json
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
        t.opener_user,
        _card_to_json(t.card),
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

    async def list_for_space(self, space_id: str) -> list[GroupSession]:
        """Every table of *space_id* here (v_77), newest first — bounded by
        the per-person caps and the prune."""
        rows = await self._db.fetchall(
            "SELECT * FROM app_group_sessions WHERE space_id = ?"
            " ORDER BY created_at DESC",
            (space_id,),
        )
        return [_row_to_table(r) for r in rows]

    async def list_hosted(
        self, app_id: str, hub_instance: str, opener_user: str, *, since: str
    ) -> list[GroupSession]:
        """The tables *opener_user* opened on *hub_instance* in *app_id* since
        *since* — the caps (bounded by them: ≤ 10 a day)."""
        rows = await self._db.fetchall(
            """SELECT * FROM app_group_sessions
               WHERE app_id = ? AND hub_instance = ? AND opener_user = ?
                 AND created_at >= ?
               ORDER BY created_at""",
            (app_id, hub_instance, opener_user, since),
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

    async def count_seated(self, user_id: str, app_ids: list[str]) -> dict[str, int]:
        """Per app of *app_ids*: the group tables *user_id* has a place at
        (their ``active`` seat row) that haven't ended — waiting for
        players or being played. One query for the Apps page's "2 games
        going"; only this person's seats, never anyone else's."""
        if not app_ids:
            return {}
        marks = ",".join("?" * len(app_ids))
        rows = await self._db.fetchall(
            f"""SELECT p.app_id AS app_id, COUNT(*) AS n
                  FROM app_pending_sessions p
                  JOIN app_group_sessions g
                    ON g.session_id = p.session_id AND g.app_id = p.app_id
                 WHERE p.app_id IN ({marks}) AND p.user_id = ?
                   AND p.state = ? AND json_type(p.payload_json, '$.group') = 'true'
                   AND g.state != ?
                 GROUP BY p.app_id""",
            (*app_ids, user_id, SESSION_ACTIVE, GROUP_ENDED),
        )
        return {str(r["app_id"]): int(r["n"]) for r in rows}

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

    # ─── This household's seat key and the table's numbering ──────────

    async def get_seat_key(self, session_id: str) -> str | None:
        """This household's own seat key at the table (KEK-wrapped JSON),
        or ``None``."""
        return await self._db.fetchval(
            "SELECT seat_key_json FROM app_group_sessions WHERE session_id = ?",
            (session_id,),
        )

    async def set_seat_key(self, session_id: str, value: str) -> bool:
        """Keep this household's seat key — only the first one: a key
        handed out stays. ``True`` when kept. No upsert touches it."""
        changed = await self._db.enqueue_rowcount(
            """UPDATE app_group_sessions SET seat_key_json = ?
               WHERE session_id = ? AND seat_key_json IS NULL""",
            (value, session_id),
        )
        return changed == 1

    async def raise_seq(self, session_id: str, seq: int, *, now: str) -> None:
        """The table's message number: the hub's last given, or the
        highest a copy has seen — it only goes up. A message counts as a
        touch (the 90-day quiet rule)."""
        await self._db.enqueue(
            """UPDATE app_group_sessions SET seq = MAX(seq, ?), updated_at = ?
               WHERE session_id = ?""",
            (seq, now, session_id),
        )

    # ─── The group mailbox (``app_group_held``) ───────────────────────

    async def hold_group_message(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        *,
        from_seat: int,
        seq: int,
        held: dict,
        now: str,
    ) -> bool:
        """Keep *held* as the latest message from *from_seat* for
        *user_id* — only while they are seated (an ``active`` row of
        theirs for the table) and only when newer than what is kept.
        ``True`` when kept. One atomic write."""
        changed = await self._db.enqueue_rowcount(
            """INSERT INTO app_group_held(app_id, user_id, session_id,
                   from_seat, seq, held_json, updated_at)
               SELECT ?, ?, ?, ?, ?, ?, ? FROM app_pending_sessions
                WHERE app_id = ? AND user_id = ? AND session_id = ?
                  AND state = 'active'
               ON CONFLICT(app_id, user_id, session_id, from_seat) DO UPDATE SET
                 seq = excluded.seq,
                 held_json = excluded.held_json,
                 updated_at = excluded.updated_at
               WHERE excluded.seq > app_group_held.seq""",
            (
                app_id,
                user_id,
                session_id,
                from_seat,
                seq,
                orjson.dumps(held).decode(),
                now,
                app_id,
                user_id,
                session_id,
            ),
        )
        return changed == 1

    async def take_group_held(self, app_id: str, user_id: str) -> list[GroupHeld]:
        """The person's app opened: every group message kept for them
        (tables they are seated at), oldest ``seq`` first — and cleared. A
        message kept again meanwhile (a newer ``seq``) stays for next time."""
        rows = await self._db.fetchall(
            """SELECT h.session_id, h.from_seat, h.seq, h.held_json, h.updated_at
                 FROM app_group_held h
                 JOIN app_pending_sessions p
                   ON p.app_id = h.app_id AND p.user_id = h.user_id
                  AND p.session_id = h.session_id
                WHERE h.app_id = ? AND h.user_id = ? AND p.state = 'active'
                ORDER BY h.session_id, h.seq, h.from_seat""",
            (app_id, user_id),
        )
        out: list[GroupHeld] = []
        for row in rows:
            taken = await self._db.enqueue_rowcount(
                """DELETE FROM app_group_held
                    WHERE app_id = ? AND user_id = ? AND session_id = ?
                      AND from_seat = ? AND seq = ?""",
                (app_id, user_id, row["session_id"], row["from_seat"], row["seq"]),
            )
            if taken:
                out.append(
                    GroupHeld(
                        session_id=str(row["session_id"]),
                        from_seat=int(row["from_seat"]),
                        seq=int(row["seq"]),
                        held=orjson.loads(row["held_json"]),
                        updated_at=str(row["updated_at"]),
                    )
                )
        return out
