"""Space-scoped poll + schedule-poll repository (§9 / §13).

Satisfies :class:`AbstractPollRepo` so a second :class:`PollService`
instance can operate against the space-scoped tables
(``space_polls`` / ``space_poll_options`` / ``space_poll_votes`` and
``space_schedule_*``) without any changes to the service layer.

The only behavioural difference from :class:`SqlitePollRepo` is that
:meth:`get_post_author` reads from ``space_posts`` — everything else
is a table-name swap.

The ``*_in_space`` methods are the federation-inbound surface
(:class:`AbstractSpacePollRepo`). The poll tables carry no ``space_id``
of their own — the scope lives on the wrapper ``space_posts`` row — so
each of them resolves the parent post *inside* the write and refuses
(returns ``False``) when it does not live in the space the §24.11
pipeline gated the sender on. The unscoped methods above stay for the
local :class:`PollService`, whose routes authorise the caller first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.space_item import stored_stamp
from .poll_repo import AbstractPollRepo

#: ``space_poll_options`` row ``?`` belongs to post ``?`` which lives in
#: space ``?``.
_OPTION_IN_SPACE = """
    SELECT 1 FROM space_poll_options o
      JOIN space_posts p ON p.id = o.post_id
     WHERE o.id=? AND o.post_id=? AND p.space_id=?
"""

#: The answers a schedule slot takes (the table's CHECK).
_AVAILABILITIES: frozenset[str] = frozenset({"yes", "no", "maybe"})

#: ``space_schedule_slots`` row ``?`` belongs to a post in space ``?``.
_SLOT_IN_SPACE = """
    SELECT 1 FROM space_schedule_slots s
      JOIN space_posts p ON p.id = s.post_id
     WHERE s.id=? AND p.space_id=?
"""


@runtime_checkable
class AbstractSpacePollRepo(AbstractPollRepo, Protocol):
    """Poll repo + the space-scoped writes federation inbound uses."""

    async def set_votes_in_space(
        self,
        *,
        space_id: str,
        post_id: str,
        option_ids: list[str],
        voter_user_id: str,
    ) -> bool: ...
    async def create_poll_in_space(
        self,
        *,
        space_id: str,
        post_id: str,
        question: str,
        closes_at: str | None,
        allow_multiple: bool,
        options: list[dict],
    ) -> bool | None: ...
    async def close_in_space(self, post_id: str, *, space_id: str) -> bool: ...
    async def create_schedule_poll_in_space(
        self,
        *,
        space_id: str,
        post_id: str,
        title: str,
        deadline: str | None,
        slots: list[dict],
    ) -> bool: ...
    async def upsert_schedule_response_in_space(
        self,
        *,
        space_id: str,
        slot_id: str,
        user_id: str,
        response: str,
    ) -> bool: ...
    async def delete_schedule_response_in_space(
        self,
        *,
        space_id: str,
        slot_id: str,
        user_id: str,
    ) -> bool: ...
    async def finalize_schedule_poll_in_space(
        self,
        *,
        space_id: str,
        post_id: str,
        slot_id: str,
    ) -> bool: ...
    async def list_votes(
        self, post_id: str, *, limit: int | None = None
    ) -> list[dict]: ...
    async def list_schedule_answers(
        self, post_id: str, *, limit: int
    ) -> list[dict]: ...
    async def apply_synced_poll(
        self,
        *,
        space_id: str,
        post_id: str,
        question: str,
        closes_at: str | None,
        allow_multiple: bool,
        closed: bool,
        options: list[dict],
        votes: list[tuple[str, str]] | None,
        keep_voters: frozenset[str],
        votes_partial: bool = False,
    ) -> bool: ...
    async def apply_synced_schedule_state(
        self,
        *,
        space_id: str,
        post_id: str,
        finalized_slot_id: str | None,
        responses: list[dict] | None,
        keep_users: frozenset[str],
        responses_at: str | None = None,
        responses_partial: bool = False,
    ) -> bool: ...


class SqliteSpacePollRepo:
    """SQLite-backed poll repo targeting the space-scoped tables."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── reply polls ────────────────────────────────────────────────────

    async def create_poll_if_absent(
        self,
        *,
        post_id: str,
        question: str,
        closes_at: str | None,
        allow_multiple: bool,
        options: list[dict],
    ) -> bool:
        """Create a reply poll for ``post_id`` unless one exists — the poll
        row and its options in ONE transaction, the options only when this
        call created the poll. Two racing calls can't duplicate options
        (a moderation resume). True when this call created it."""

        def _tx(conn) -> bool:
            cur = conn.execute(
                "INSERT INTO space_polls(post_id, question, closes_at, closed,"
                " allow_multiple) VALUES(?, ?, ?, 0, ?)"
                " ON CONFLICT(post_id) DO NOTHING",
                (post_id, question, closes_at, 1 if allow_multiple else 0),
            )
            if cur.rowcount == 0:
                return False
            conn.executemany(
                "INSERT INTO space_poll_options(id, post_id, text, position)"
                " VALUES(?, ?, ?, ?)",
                [
                    (o["id"], post_id, o["text"], int(o.get("position", 0)))
                    for o in options
                ],
            )
            return True

        created: bool = await self._db.transact(_tx)
        return created

    async def create_schedule_poll_if_absent(
        self,
        *,
        post_id: str,
        title: str,
        deadline: str | None,
        slots: list[dict],
    ) -> bool:
        """Create a schedule poll for ``post_id`` unless one exists — meta
        and slots in ONE transaction, the slots only when this call created
        the meta row. True when this call created it."""

        def _tx(conn) -> bool:
            cur = conn.execute(
                "INSERT INTO space_schedule_poll_meta(post_id, title, deadline,"
                " finalized_slot_id, closed) VALUES(?, ?, ?, NULL, 0)"
                " ON CONFLICT(post_id) DO NOTHING",
                (post_id, title, deadline),
            )
            if cur.rowcount == 0:
                return False
            conn.executemany(
                "INSERT INTO space_schedule_slots(id, post_id, slot_date, start_time,"
                " end_time, position) VALUES(?, ?, ?, ?, ?, ?)",
                [
                    (
                        s["id"],
                        post_id,
                        s["slot_date"],
                        s.get("start_time"),
                        s.get("end_time"),
                        int(s.get("position", 0)),
                    )
                    for s in slots
                ],
            )
            return True

        created: bool = await self._db.transact(_tx)
        return created

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
        # Only onto an option that still exists: a post delete drops its
        # poll (migration 0085 trigger), and a vote racing it must land
        # nowhere rather than fail the FK.
        await self._db.enqueue(
            "INSERT INTO space_poll_votes(option_id, voter_user_id)"
            " SELECT ?, ? WHERE EXISTS"
            " (SELECT 1 FROM space_poll_options WHERE id=?)",
            (option_id, voter_user_id, option_id),
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

    async def close_if_open(self, post_id: str) -> bool:
        n = await self._db.enqueue_rowcount(
            "UPDATE space_polls SET closed=1 WHERE post_id=? AND closed=0",
            (post_id,),
        )
        return n > 0

    async def list_due_to_close(self, now_iso: str) -> list[dict]:
        # Only the author's household closes a space poll: peers accept
        # SPACE_POLL_CLOSED only from it, and every member household holds
        # a copy of the poll — so a poll whose author isn't a local user
        # waits for its own household's close.
        rows = await self._db.fetchall(
            "SELECT sp.post_id, p.author, p.space_id FROM space_polls sp"
            " JOIN space_posts p ON p.id = sp.post_id"
            " JOIN users u ON u.user_id = p.author"
            " WHERE sp.closed=0 AND sp.closes_at IS NOT NULL"
            " AND p.deleted=0"
            " AND julianday(sp.closes_at) <= julianday(?)"
            " ORDER BY sp.closes_at",
            (now_iso,),
        )
        return [
            {
                "post_id": r["post_id"],
                "author": r["author"],
                "space_id": r["space_id"],
            }
            for r in rows
        ]

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

    async def list_voter_ids(self, post_id: str) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT DISTINCT pv.voter_user_id FROM space_poll_votes pv "
            "JOIN space_poll_options po ON po.id = pv.option_id "
            "WHERE po.post_id=? ORDER BY pv.voter_user_id",
            (post_id,),
        )
        return [r["voter_user_id"] for r in rows]

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
                datetime.now(UTC).isoformat(),
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

    # ── space-scoped writes (federation inbound, §24.11) ───────────────

    async def set_votes_in_space(
        self,
        *,
        space_id: str,
        post_id: str,
        option_ids: list[str],
        voter_user_id: str,
    ) -> bool:
        """Replace ``voter_user_id``'s votes on ``post_id`` with
        ``option_ids`` — the voter's whole selection, as
        ``SPACE_POLL_VOTE_CAST`` carries it (empty: the vote is retracted).

        One transaction that first proves the poll's post lives in
        ``space_id``, that the poll is still open (not closed, its
        ``closes_at`` not passed — the local ``PollService.cast_vote`` rule),
        that every option belongs to ``post_id``, and that a single-choice
        poll gets at most one. ``False`` = refused, nothing written (the
        voter's previous votes stay).
        """
        wanted = list(dict.fromkeys(option_ids))

        def _run(conn) -> bool:
            poll = conn.execute(
                "SELECT sp.allow_multiple FROM space_polls sp"
                " JOIN space_posts p ON p.id = sp.post_id"
                " WHERE sp.post_id=? AND p.space_id=? AND sp.closed=0"
                " AND (sp.closes_at IS NULL"
                "  OR julianday(sp.closes_at) > julianday('now'))",
                (post_id, space_id),
            ).fetchone()
            if poll is None or (len(wanted) > 1 and not poll["allow_multiple"]):
                return False
            for option_id in wanted:
                if (
                    conn.execute(
                        _OPTION_IN_SPACE, (option_id, post_id, space_id)
                    ).fetchone()
                    is None
                ):
                    return False
            conn.execute(
                "DELETE FROM space_poll_votes WHERE voter_user_id=? AND option_id IN "
                "(SELECT id FROM space_poll_options WHERE post_id=?)",
                (voter_user_id, post_id),
            )
            conn.executemany(
                "INSERT INTO space_poll_votes(option_id, voter_user_id) VALUES(?, ?)",
                [(option_id, voter_user_id) for option_id in wanted],
            )
            return True

        return bool(await self._db.transact(_run))

    async def create_poll_in_space(
        self,
        *,
        space_id: str,
        post_id: str,
        question: str,
        closes_at: str | None,
        allow_multiple: bool,
        options: list[dict],
    ) -> bool | None:
        """Create the poll a peer announced for ``post_id`` — the poll row
        and its options (``{id, text}`` in order: the list index is the
        position) in ONE transaction that first proves the post is a live
        post of ``space_id``.

        ``True``: this call created it. ``False``: the post already has a
        poll — kept as it is (a poll is never rewritten by a re-send).
        ``None``: refused, nothing written — the post is not a live post of
        this space, or an option id is already another poll's.
        """

        def _run(conn) -> bool | None:
            if (
                conn.execute(
                    "SELECT 1 FROM space_posts WHERE id=? AND space_id=? AND deleted=0",
                    (post_id, space_id),
                ).fetchone()
                is None
            ):
                return None
            if (
                conn.execute(
                    "SELECT 1 FROM space_polls WHERE post_id=?", (post_id,)
                ).fetchone()
                is not None
            ):
                return False
            for opt in options:
                if (
                    conn.execute(
                        "SELECT 1 FROM space_poll_options WHERE id=?",
                        (str(opt["id"]),),
                    ).fetchone()
                    is not None
                ):
                    return None
            conn.execute(
                "INSERT INTO space_polls(post_id, question, closes_at, closed,"
                " allow_multiple) VALUES(?, ?, ?, 0, ?)",
                (post_id, question, closes_at, 1 if allow_multiple else 0),
            )
            conn.executemany(
                "INSERT INTO space_poll_options(id, post_id, text, position)"
                " VALUES(?, ?, ?, ?)",
                [
                    (str(o["id"]), post_id, str(o["text"]), position)
                    for position, o in enumerate(options)
                ],
            )
            return True

        created: bool | None = await self._db.transact(_run)
        return created

    async def close_in_space(self, post_id: str, *, space_id: str) -> bool:
        """Close the poll on ``post_id`` if that post lives in ``space_id``."""
        n = await self._db.enqueue_rowcount(
            "UPDATE space_polls SET closed=1 WHERE post_id=? AND EXISTS ("
            " SELECT 1 FROM space_posts"
            "  WHERE id = space_polls.post_id AND space_id=?)",
            (post_id, space_id),
        )
        return n > 0

    async def create_schedule_poll_in_space(
        self,
        *,
        space_id: str,
        post_id: str,
        title: str,
        deadline: str | None,
        slots: list[dict],
    ) -> bool:
        """Upsert schedule-poll meta + slots onto a post of ``space_id``.

        Refused (``False``) when the wrapper post is not in that space, or
        is deleted (a deleted post holds no poll — migration 0085).
        A slot id already owned by a *different* post is left alone —
        the slot upsert only updates a row whose ``post_id`` matches — so
        naming another poll's slot ids cannot rewrite that poll.
        """

        def _run(conn) -> bool:
            if (
                conn.execute(
                    "SELECT 1 FROM space_posts WHERE id=? AND space_id=? AND deleted=0",
                    (post_id, space_id),
                ).fetchone()
                is None
            ):
                return False
            conn.execute(
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
                conn.execute(
                    """
                    INSERT INTO space_schedule_slots(
                        id, post_id, slot_date, start_time, end_time, position
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        slot_date=excluded.slot_date,
                        start_time=excluded.start_time,
                        end_time=excluded.end_time,
                        position=excluded.position
                     WHERE space_schedule_slots.post_id = excluded.post_id
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
            return True

        return bool(await self._db.transact(_run))

    async def upsert_schedule_response_in_space(
        self,
        *,
        space_id: str,
        slot_id: str,
        user_id: str,
        response: str,
    ) -> bool:
        """Record an availability answer on a slot of a ``space_id`` poll."""
        n = await self._db.enqueue_rowcount(
            f"""
            INSERT INTO space_schedule_responses(
                slot_id, user_id, availability, responded_at
            )
            SELECT ?, ?, ?, ?
             WHERE EXISTS ({_SLOT_IN_SPACE})
            ON CONFLICT(slot_id, user_id) DO UPDATE SET
                availability=excluded.availability,
                responded_at=excluded.responded_at
            """,
            (
                slot_id,
                user_id,
                response,
                datetime.now(UTC).isoformat(),
                slot_id,
                space_id,
            ),
        )
        return n > 0

    async def delete_schedule_response_in_space(
        self,
        *,
        space_id: str,
        slot_id: str,
        user_id: str,
    ) -> bool:
        """Retract an answer on a slot of a ``space_id`` poll."""
        n = await self._db.enqueue_rowcount(
            "DELETE FROM space_schedule_responses WHERE slot_id=? AND user_id=? "
            f"AND EXISTS ({_SLOT_IN_SPACE})",
            (slot_id, user_id, slot_id, space_id),
        )
        return n > 0

    async def finalize_schedule_poll_in_space(
        self,
        *,
        space_id: str,
        post_id: str,
        slot_id: str,
    ) -> bool:
        """Finalise ``post_id`` on ``slot_id`` — both must be in ``space_id``."""
        n = await self._db.enqueue_rowcount(
            """
            UPDATE space_schedule_poll_meta
               SET finalized_slot_id=?, closed=1
             WHERE post_id=?
               AND EXISTS (
                   SELECT 1 FROM space_schedule_slots s
                     JOIN space_posts p ON p.id = s.post_id
                    WHERE s.id=? AND s.post_id = space_schedule_poll_meta.post_id
                      AND p.space_id=?
               )
            """,
            (slot_id, post_id, slot_id, space_id),
        )
        return n > 0

    # ── §25.6 sync (v_92): the host's poll state onto a held post ──────

    async def list_votes(self, post_id: str, *, limit: int | None = None) -> list[dict]:
        """Every vote on ``post_id``'s poll, ``{option_id, voter_user_id}``,
        in a stable order (voter, option). ``limit`` (what the host's
        ``polls`` record carries): at most that many, the most recent
        voters' first, each voter's votes together."""
        if limit is None:
            rows = await self._db.fetchall(
                "SELECT pv.option_id, pv.voter_user_id FROM space_poll_votes pv "
                "JOIN space_poll_options po ON po.id = pv.option_id "
                "WHERE po.post_id=? ORDER BY pv.voter_user_id, po.position, po.id",
                (post_id,),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT pv.option_id, pv.voter_user_id FROM space_poll_votes pv "
                "JOIN space_poll_options po ON po.id = pv.option_id "
                "WHERE po.post_id=? ORDER BY MAX(pv.voted_at) OVER ("
                " PARTITION BY pv.voter_user_id) DESC,"
                " pv.voter_user_id, po.position, po.id LIMIT ?",
                (post_id, int(limit)),
            )
        return [
            {"option_id": r["option_id"], "voter_user_id": r["voter_user_id"]}
            for r in rows
        ]

    async def list_schedule_answers(self, post_id: str, *, limit: int) -> list[dict]:
        """At most ``limit`` answers on ``post_id``'s schedule poll, the
        newest first, with their ``responded_at`` — what the host's
        ``schedules`` record carries (last writer wins on the receiver)."""
        rows = await self._db.fetchall(
            "SELECT sr.slot_id, sr.user_id, sr.availability, sr.responded_at"
            " FROM space_schedule_responses sr"
            " JOIN space_schedule_slots s ON s.id = sr.slot_id"
            " WHERE s.post_id=?"
            " ORDER BY sr.responded_at DESC, sr.slot_id, sr.user_id LIMIT ?",
            (post_id, int(limit)),
        )
        return [
            {
                "slot_id": r["slot_id"],
                "user_id": r["user_id"],
                "availability": r["availability"],
                "responded_at": r["responded_at"],
            }
            for r in rows
        ]

    async def apply_synced_poll(
        self,
        *,
        space_id: str,
        post_id: str,
        question: str,
        closes_at: str | None,
        allow_multiple: bool,
        closed: bool,
        options: list[dict],
        votes: list[tuple[str, str]] | None,
        keep_voters: frozenset[str],
        votes_partial: bool = False,
    ) -> bool:
        """Converge the poll on ``post_id`` on the host's copy, in ONE
        transaction that first proves the post is a live post of
        ``space_id`` (``False``: refused, nothing written).

        The poll and its options (``{id, text}`` in order — the list index is
        the position) are created when missing and take the host's question
        and deadline; an option id another post owns is left alone. A poll
        never reopens: ``closed`` only closes. ``votes`` (``(option_id,
        voter)`` pairs; ``None`` — an older host that sends none — keeps the
        held votes) replaces the votes of everybody but ``keep_voters``, this
        household's own people, whose votes it authors itself;
        ``votes_partial`` (the host's list was cut) converges only the voters
        the list names — a voter it could not carry keeps the held votes.
        Only real differences are written, so re-applying the same snapshot
        stamps nothing (the migration 0086 touch triggers)."""

        def _run(conn) -> bool:
            if (
                conn.execute(
                    "SELECT 1 FROM space_posts WHERE id=? AND space_id=? AND deleted=0",
                    (post_id, space_id),
                ).fetchone()
                is None
            ):
                return False
            conn.execute(
                """
                INSERT INTO space_polls(
                    post_id, question, closes_at, closed, allow_multiple
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    question=excluded.question,
                    closes_at=excluded.closes_at,
                    allow_multiple=excluded.allow_multiple,
                    closed=MAX(space_polls.closed, excluded.closed)
                """,
                (
                    post_id,
                    question,
                    closes_at,
                    1 if closed else 0,
                    1 if allow_multiple else 0,
                ),
            )
            for position, opt in enumerate(options):
                conn.execute(
                    """
                    INSERT INTO space_poll_options(id, post_id, text, position)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        text=excluded.text,
                        position=excluded.position
                     WHERE space_poll_options.post_id = excluded.post_id
                    """,
                    (str(opt["id"]), post_id, str(opt["text"]), position),
                )
            if votes is None:
                return True
            mine = {
                str(r["id"])
                for r in conn.execute(
                    "SELECT id FROM space_poll_options WHERE post_id=?", (post_id,)
                ).fetchall()
            }
            held = {
                (r["option_id"], r["voter_user_id"])
                for r in conn.execute(
                    "SELECT pv.option_id, pv.voter_user_id FROM space_poll_votes pv"
                    " JOIN space_poll_options po ON po.id = pv.option_id"
                    " WHERE po.post_id=?",
                    (post_id,),
                ).fetchall()
                if r["voter_user_id"] not in keep_voters
            }
            wanted = {
                (option_id, voter)
                for option_id, voter in votes
                if option_id in mine and voter and voter not in keep_voters
            }
            if votes_partial:
                named = {voter for _o, voter in votes}
                held = {(o, v) for o, v in held if v in named}
            for option_id, voter in sorted(held - wanted):
                conn.execute(
                    "DELETE FROM space_poll_votes WHERE option_id=? AND voter_user_id=?",
                    (option_id, voter),
                )
            for option_id, voter in sorted(wanted - held):
                conn.execute(
                    "INSERT INTO space_poll_votes(option_id, voter_user_id)"
                    " VALUES(?, ?) ON CONFLICT DO NOTHING",
                    (option_id, voter),
                )
            return True

        return bool(await self._db.transact(_run))

    async def apply_synced_schedule_state(
        self,
        *,
        space_id: str,
        post_id: str,
        finalized_slot_id: str | None,
        responses: list[dict] | None,
        keep_users: frozenset[str],
        responses_at: str | None = None,
        responses_partial: bool = False,
    ) -> bool:
        """Converge a held schedule poll's answers and its pick on the
        host's copy, in ONE transaction that first proves the poll's post
        is a live post of ``space_id`` (``False``: refused, nothing written).

        ``finalized_slot_id`` (a slot of this poll) finalises it; a pick
        never reverts (``None`` leaves the held one). ``responses``
        (``{slot_id, user_id, availability, responded_at}``; ``None`` — an
        older host — keeps the held answers) converges the answers of
        everybody but ``keep_users``, this household's own people, last
        writer wins on ``responded_at``: an answer is taken only when newer
        than the held one, and a held answer the host lacks is dropped only
        when the host's list is whole (not ``responses_partial``) and the
        held one is not newer than the host's snapshot (``responses_at``,
        else the newest answer of the list) — one that reached this
        household live after the host read its list stays. Only real
        differences are written."""

        def _run(conn) -> bool:
            if (
                conn.execute(
                    "SELECT 1 FROM space_schedule_poll_meta m"
                    " JOIN space_posts p ON p.id = m.post_id"
                    " WHERE m.post_id=? AND p.space_id=? AND p.deleted=0",
                    (post_id, space_id),
                ).fetchone()
                is None
            ):
                return False
            slots = {
                str(r["id"])
                for r in conn.execute(
                    "SELECT id FROM space_schedule_slots WHERE post_id=?", (post_id,)
                ).fetchall()
            }
            if finalized_slot_id and finalized_slot_id in slots:
                conn.execute(
                    "UPDATE space_schedule_poll_meta"
                    " SET finalized_slot_id=?, closed=1"
                    " WHERE post_id=? AND finalized_slot_id IS NULL",
                    (finalized_slot_id, post_id),
                )
            if responses is None:
                return True
            held = {
                (r["slot_id"], r["user_id"]): (
                    r["availability"],
                    stored_stamp(r["responded_at"]),
                )
                for r in conn.execute(
                    "SELECT sr.slot_id, sr.user_id, sr.availability, sr.responded_at"
                    " FROM space_schedule_responses sr"
                    " JOIN space_schedule_slots s ON s.id = sr.slot_id"
                    " WHERE s.post_id=?",
                    (post_id,),
                ).fetchall()
                if r["user_id"] not in keep_users
            }
            wanted: dict[tuple[str, str], tuple[str, datetime | None]] = {}
            for resp in responses:
                slot_id = str(resp.get("slot_id") or "")
                user_id = str(resp.get("user_id") or "")
                availability = str(resp.get("availability") or "")
                if (
                    slot_id in slots
                    and user_id
                    and user_id not in keep_users
                    and availability in _AVAILABILITIES
                ):
                    wanted[(slot_id, user_id)] = (
                        availability,
                        stored_stamp(resp.get("responded_at")),
                    )
            newest = [at for _a, at in wanted.values() if at is not None]
            snapshot = stored_stamp(responses_at) or (max(newest) if newest else None)
            if not responses_partial and snapshot is not None:
                for key in sorted(held.keys() - wanted.keys()):
                    mine = held[key][1]
                    if mine is not None and mine > snapshot:
                        continue
                    conn.execute(
                        "DELETE FROM space_schedule_responses"
                        " WHERE slot_id=? AND user_id=?",
                        key,
                    )
            for (slot_id, user_id), (availability, theirs) in sorted(wanted.items()):
                have = held.get((slot_id, user_id))
                if have is not None and (
                    have[0] == availability
                    or theirs is None
                    or (have[1] is not None and have[1] >= theirs)
                ):
                    continue
                conn.execute(
                    "INSERT INTO space_schedule_responses("
                    "slot_id, user_id, availability, responded_at) VALUES(?, ?, ?, ?)"
                    " ON CONFLICT(slot_id, user_id) DO UPDATE SET"
                    " availability=excluded.availability,"
                    " responded_at=excluded.responded_at",
                    (
                        slot_id,
                        user_id,
                        availability,
                        (theirs or datetime.now(UTC)).isoformat(),
                    ),
                )
            return True

        return bool(await self._db.transact(_run))
