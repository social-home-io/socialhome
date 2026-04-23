"""Space-scoped poll + schedule-poll repository (§9 / §13).

Satisfies :class:`AbstractPollRepo` so a second :class:`PollService`
instance can operate against the space-scoped tables
(``space_polls`` / ``space_poll_options`` / ``space_poll_votes`` and
``space_schedule_*``) without any changes to the service layer.

The only behavioural difference from :class:`SqlitePollRepo` is that
:meth:`get_post_author` reads from ``space_posts`` — everything else
is a table-name swap.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..db import AsyncDatabase


class SqliteSpacePollRepo:
    """SQLite-backed poll repo targeting the space-scoped tables."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── reply polls ────────────────────────────────────────────────────

    async def create_poll(
        self,
        *,
        post_id: str,
        question: str,
        closes_at: str | None,
        allow_multiple: bool,
        options: list[dict],
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_polls(
                post_id, question, closes_at, closed, allow_multiple
            ) VALUES(?, ?, ?, 0, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                question=excluded.question,
                closes_at=excluded.closes_at,
                allow_multiple=excluded.allow_multiple
            """,
            (post_id, question, closes_at, 1 if allow_multiple else 0),
        )
        for opt in options:
            await self._db.enqueue(
                """
                INSERT INTO space_poll_options(id, post_id, text, position)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    text=excluded.text,
                    position=excluded.position
                """,
                (
                    opt["id"],
                    post_id,
                    opt["text"],
                    int(opt.get("position", 0)),
                ),
            )

    async def get_meta(self, post_id: str) -> dict | None:
        row = await self._db.fetchone(
            "SELECT post_id, question, closes_at, closed, allow_multiple "
            "FROM space_polls WHERE post_id=?",
            (post_id,),
        )
        if row is None:
            return None
        d = dict(row)
        d["closed"] = bool(d.get("closed"))
        d["allow_multiple"] = bool(d.get("allow_multiple"))
        return d

    async def option_belongs_to_post(
        self,
        *,
        option_id: str,
        post_id: str,
    ) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM space_poll_options WHERE id=? AND post_id=?",
            (option_id, post_id),
        )
        return row is not None

    async def clear_user_votes(
        self,
        *,
        post_id: str,
        voter_user_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM space_poll_votes WHERE voter_user_id=? AND option_id IN "
            "(SELECT id FROM space_poll_options WHERE post_id=?)",
            (voter_user_id, post_id),
        )

    async def insert_vote(
        self,
        *,
        option_id: str,
        voter_user_id: str,
    ) -> None:
        await self._db.enqueue(
            "INSERT INTO space_poll_votes(option_id, voter_user_id) VALUES(?, ?)",
            (option_id, voter_user_id),
        )

    async def get_post_author(self, post_id: str) -> str | None:
        row = await self._db.fetchone(
            "SELECT author FROM space_posts WHERE id=?",
            (post_id,),
        )
        return row["author"] if row else None

    async def close(self, post_id: str) -> None:
        await self._db.enqueue(
            "UPDATE space_polls SET closed=1 WHERE post_id=?",
            (post_id,),
        )

    async def list_user_votes(
        self,
        post_id: str,
        voter_user_id: str,
    ) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT pv.option_id FROM space_poll_votes pv "
            "JOIN space_poll_options po ON po.id = pv.option_id "
            "WHERE pv.voter_user_id=? AND po.post_id=?",
            (voter_user_id, post_id),
        )
        return [r["option_id"] for r in rows]

    async def list_options_with_counts(
        self,
        post_id: str,
    ) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT id, text, (SELECT COUNT(*) FROM space_poll_votes "
            " WHERE option_id = space_poll_options.id) AS count "
            "FROM space_poll_options WHERE post_id=? ORDER BY position",
            (post_id,),
        )
        return [
            {"id": r["id"], "text": r["text"], "count": int(r["count"])} for r in rows
        ]

    # ── schedule polls ─────────────────────────────────────────────────

    async def create_schedule_poll(
        self,
        *,
        post_id: str,
        title: str,
        deadline: str | None,
        slots: list[dict],
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_schedule_poll_meta(
                post_id, title, deadline, finalized_slot_id, closed
            ) VALUES(?, ?, ?, NULL, 0)
            ON CONFLICT(post_id) DO UPDATE SET
                title=excluded.title,
                deadline=excluded.deadline
            """,
            (post_id, title, deadline),
        )
        for s in slots:
            await self._db.enqueue(
                """
                INSERT INTO space_schedule_slots(
                    id, post_id, slot_date, start_time, end_time, position
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    slot_date=excluded.slot_date,
                    start_time=excluded.start_time,
                    end_time=excluded.end_time,
                    position=excluded.position
                """,
                (
                    s["id"],
                    post_id,
                    s["slot_date"],
                    s.get("start_time"),
                    s.get("end_time"),
                    int(s.get("position", 0)),
                ),
            )

    async def get_schedule_meta(self, post_id: str) -> dict | None:
        row = await self._db.fetchone(
            "SELECT post_id, title, deadline, finalized_slot_id, closed "
            "FROM space_schedule_poll_meta WHERE post_id=?",
            (post_id,),
        )
        if row is None:
            return None
        return {
            "post_id": row["post_id"],
            "title": row["title"],
            "deadline": row["deadline"],
            "finalized_slot_id": row["finalized_slot_id"],
            "closed": bool(row["closed"]),
        }

    async def list_schedule_slots(self, post_id: str) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT id, slot_date, start_time, end_time, position "
            "FROM space_schedule_slots WHERE post_id=? "
            "ORDER BY position, slot_date",
            (post_id,),
        )
        return [
            {
                "id": r["id"],
                "slot_date": r["slot_date"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "position": int(r["position"] or 0),
            }
            for r in rows
        ]

    async def list_schedule_responses(
        self,
        post_id: str,
    ) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT sr.slot_id, sr.user_id, sr.availability
              FROM space_schedule_responses sr
              JOIN space_schedule_slots s ON s.id = sr.slot_id
             WHERE s.post_id = ?
            """,
            (post_id,),
        )
        return [
            {
                "slot_id": r["slot_id"],
                "user_id": r["user_id"],
                "availability": r["availability"],
            }
            for r in rows
        ]

    async def finalize_schedule_poll(
        self,
        *,
        post_id: str,
        slot_id: str,
    ) -> dict | None:
        row = await self._db.fetchone(
            "SELECT id, slot_date, start_time, end_time, position "
            "FROM space_schedule_slots WHERE id=? AND post_id=?",
            (slot_id, post_id),
        )
        if row is None:
            return None
        await self._db.enqueue(
            "UPDATE space_schedule_poll_meta "
            "SET finalized_slot_id=?, closed=1 WHERE post_id=?",
            (slot_id, post_id),
        )
        return {
            "id": row["id"],
            "slot_date": row["slot_date"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "position": int(row["position"] or 0),
        }

    async def upsert_schedule_response(
        self,
        *,
        slot_id: str,
        user_id: str,
        response: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_schedule_responses(
                slot_id, user_id, availability, responded_at
            ) VALUES(?, ?, ?, ?)
            ON CONFLICT(slot_id, user_id) DO UPDATE SET
                availability=excluded.availability,
                responded_at=excluded.responded_at
            """,
            (
                slot_id,
                user_id,
                response,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    async def delete_schedule_response(
        self,
        *,
        slot_id: str,
        user_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM space_schedule_responses WHERE slot_id=? AND user_id=?",
            (slot_id, user_id),
        )
