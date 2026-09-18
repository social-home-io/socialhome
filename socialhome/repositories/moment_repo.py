"""Moment repository — persistence for the Momentum pillar (§Momentum).

Manages two tables:

* ``moments`` — per-author broadcast posts (text + optional image /
  short video). Holds local AND federated remote-author rows; the
  ``author_user_id`` column has no FK so peers' content can land
  alongside local content (same convention as
  ``conversation_messages.sender_user_id``).
* ``moment_reactions`` — one row per (moment_id, reactor_user_id),
  holding the current emoji.

The visibility query in :meth:`list_visible_to` does *all* the
filtering in one statement: drop blocked authors, collapse the
absolute 7-day retention to 24 h for non-followers, and apply the
``before`` cursor for pagination.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.moment import Moment, MomentReaction, extract_hashtags
from .base import row_to_dict, rows_to_dicts


@runtime_checkable
class AbstractMomentRepo(Protocol):
    async def save(self, moment: Moment) -> Moment: ...
    async def get(self, moment_id: str) -> Moment | None: ...
    async def delete(self, moment_id: str) -> None: ...
    async def delete_by_author(self, author_user_id: str) -> int: ...
    async def list_visible_to(
        self,
        viewer_user_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
        tag: str | None = None,
        max_hops: int = 3,
    ) -> list[Moment]: ...
    async def list_public_for(
        self,
        author_user_id: str,
        *,
        limit: int = 50,
    ) -> list[Moment]: ...
    async def list_replies(self, parent_moment_id: str) -> list[Moment]: ...
    async def count_recent_for_author(
        self,
        author_user_id: str,
        *,
        since_iso: str,
    ) -> int: ...
    async def has_visible_recipient(
        self,
        *,
        author_user_id: str,
        hop_count: int,
    ) -> bool: ...

    # Reactions -----------------------------------------------------------
    async def set_reaction(
        self,
        moment_id: str,
        reactor_user_id: str,
        emoji: str,
    ) -> None: ...
    async def clear_reaction(
        self,
        moment_id: str,
        reactor_user_id: str,
    ) -> None: ...
    async def list_reactions(self, moment_id: str) -> list[MomentReaction]: ...

    # Engagement counters --------------------------------------------------
    async def count_engagement_for(
        self,
        moment_ids: list[str],
    ) -> dict[str, dict[str, int]]: ...

    # Hashtags ------------------------------------------------------------
    async def list_hashtags_for(self, moment_id: str) -> list[str]: ...
    async def list_top_hashtags(
        self,
        viewer_user_id: str,
        *,
        limit: int = 20,
    ) -> list[tuple[str, int]]: ...

    # Retention -----------------------------------------------------------
    async def list_expired_media_urls(self) -> list[str]: ...
    async def prune_expired(self) -> int: ...


class SqliteMomentRepo:
    """SQLite-backed :class:`AbstractMomentRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save(self, moment: Moment) -> Moment:
        """Upsert by id. Inbound federation handlers call this with
        the dedup-by-id semantics the relay needs.

        Re-extracts hashtags from the (possibly edited) content and
        rewrites the ``moment_hashtags`` rows so the trending list
        and the tag-filter query stay correct after a relayed update.
        """
        await self._db.enqueue(
            """
            INSERT INTO moments(
                id, author_user_id, content, media_url, media_type,
                duration_ms, parent_moment_id, origin_instance_id,
                created_at, expires_at, hop_count, is_public,
                received_via, received_via_gfs_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                media_url=excluded.media_url,
                media_type=excluded.media_type,
                duration_ms=excluded.duration_ms,
                expires_at=excluded.expires_at,
                hop_count=excluded.hop_count,
                is_public=excluded.is_public,
                received_via=excluded.received_via,
                received_via_gfs_id=excluded.received_via_gfs_id
            """,
            (
                moment.id,
                moment.author_user_id,
                moment.content,
                moment.media_url,
                moment.media_type,
                moment.duration_ms,
                moment.parent_moment_id,
                moment.origin_instance_id,
                moment.created_at,
                moment.expires_at,
                int(moment.hop_count),
                int(moment.is_public),
                moment.received_via,
                moment.received_via_gfs_id,
            ),
        )
        # DELETE-INSERT keeps the tag set in sync with the latest
        # content. Cheap because moment_hashtags is keyed on
        # (moment_id, tag) and a moment has at most
        # ``MOMENT_MAX_HASHTAGS_PER_POST`` rows.
        await self._db.enqueue(
            "DELETE FROM moment_hashtags WHERE moment_id=?",
            (moment.id,),
        )
        for tag in extract_hashtags(moment.content):
            await self._db.enqueue(
                "INSERT OR IGNORE INTO moment_hashtags(moment_id, tag) VALUES(?, ?)",
                (moment.id, tag),
            )
        return moment

    async def get(self, moment_id: str) -> Moment | None:
        row = await self._db.fetchone(
            "SELECT * FROM moments WHERE id=?",
            (moment_id,),
        )
        return _row_to_moment(row_to_dict(row))

    async def delete(self, moment_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM moments WHERE id=?",
            (moment_id,),
        )

    async def delete_by_author(self, author_user_id: str) -> int:
        """Hard-delete every moment authored by ``author_user_id``.

        Drives the §Connection-Detail visibility cascade: when an inbound
        ``USER_REMOVED`` lands for this user, every moment they ever
        posted is removed from our local view. Forward-only would leave
        orphan content from a user whose row was just deprovisioned —
        the receiver-side rule treats hide as full removal, matching
        the "hide = remove" UX the SPA copy promises.
        """
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM moments WHERE author_user_id=?",
            (author_user_id,),
        )
        n = int(row["n"]) if row else 0
        if n:
            await self._db.enqueue(
                "DELETE FROM moments WHERE author_user_id=?",
                (author_user_id,),
            )
        return n

    async def list_visible_to(
        self,
        viewer_user_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
        tag: str | None = None,
        max_hops: int = 3,
    ) -> list[Moment]:
        """One query covers blocks, retention, hop visibility, and
        the cursor.

        Visibility:
        * Author is not on the viewer's :table:`user_blocks`.
        * EITHER the moment is < 24 h old, OR the viewer follows the
          author (then up to 7 d, the absolute ``expires_at``).
        * The row's ``hop_count`` is ≤ the viewer's ``max_hops``
          preference (default 3 = the wire cap, so all rows pass).
        * ``before`` is an ISO-8601 ``created_at`` cursor; ``NULL``
          fetches from newest.
        * ``tag`` (lowercase) restricts the result to moments tagged
          with that hashtag — used by the Browse / archive page.
        """
        limit = max(1, min(int(limit), 100))
        normalised_tag = tag.strip().lower().lstrip("#") if tag else None
        # Clamp ``max_hops`` to [1, 3]: the wire cap is 3 and a
        # value below 1 would hide every row including the user's
        # own moments.
        capped_hops = max(1, min(int(max_hops), 3))
        rows = await self._db.fetchall(
            """
            SELECT m.* FROM moments AS m
             WHERE m.author_user_id NOT IN (
                 SELECT blocked_user_id FROM user_blocks
                  WHERE blocker_user_id = ?
             )
               AND (
                 (julianday('now') - julianday(m.created_at)) * 24 < 24
                 OR EXISTS (
                     SELECT 1 FROM user_follows
                      WHERE follower_user_id = ?
                        AND followed_user_id = m.author_user_id
                 )
               )
               AND datetime(m.expires_at) > datetime('now')
               AND m.hop_count <= ?
               AND (? IS NULL OR m.created_at < ?)
               AND (
                 ? IS NULL OR EXISTS (
                     SELECT 1 FROM moment_hashtags
                      WHERE moment_id = m.id AND tag = ?
                 )
               )
             ORDER BY m.created_at DESC
             LIMIT ?
            """,
            (
                viewer_user_id,
                viewer_user_id,
                capped_hops,
                before,
                before,
                normalised_tag,
                normalised_tag,
                limit,
            ),
        )
        return [m for m in (_row_to_moment(d) for d in rows_to_dicts(rows)) if m]

    async def has_visible_recipient(
        self,
        *,
        author_user_id: str,
        hop_count: int,
    ) -> bool:
        """True iff at least one local user can see this moment under
        their preferences and block list. Used by the inbound
        federation handler to skip the local persist when no one
        wants the row (pure pass-through — relay continues).

        A user "can see" iff:
        * The author is NOT on their :table:`user_blocks` row.
        * Their ``moments.max_hops`` preference (read from
          ``users.preferences_json``) is ≥ ``hop_count``.

        The preference is parsed inline via SQLite's ``json_extract``
        so we don't have to load every user just to compute the
        gate. Rows without a ``moments.max_hops`` key default to the
        wire cap of 3 (most-permissive — matches new accounts).
        """
        row = await self._db.fetchone(
            """
            SELECT 1 FROM users AS u
             WHERE NOT EXISTS (
                 SELECT 1 FROM user_blocks
                  WHERE blocker_user_id = u.user_id
                    AND blocked_user_id = ?
             )
               AND COALESCE(
                   json_extract(u.preferences_json, '$.moments.max_hops'),
                   3
               ) >= ?
             LIMIT 1
            """,
            (author_user_id, int(hop_count)),
        )
        return row is not None

    async def list_public_for(
        self,
        author_user_id: str,
        *,
        limit: int = 50,
    ) -> list[Moment]:
        """The author's current PUBLIC, non-expired moments, newest-first.

        Drives the §Momentum-public live index a guest reads through the
        GFS over WebRTC (or the GFS-relay fallback). The predicate is the
        privacy invariant: ONLY ``is_public=1`` rows whose ``expires_at``
        is still in the future are ever returned — private / household
        moments and expired rows are excluded at the query layer so no
        non-public byte can leak onto the public stream.
        """
        limit = max(1, min(int(limit), 100))
        rows = await self._db.fetchall(
            """
            SELECT * FROM moments
             WHERE author_user_id = ? AND is_public = 1
               AND datetime(expires_at) > datetime('now')
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (author_user_id, limit),
        )
        return [m for m in (_row_to_moment(d) for d in rows_to_dicts(rows)) if m]

    async def list_replies(self, parent_moment_id: str) -> list[Moment]:
        """Replies in chronological order so the thread reads top-down."""
        rows = await self._db.fetchall(
            "SELECT * FROM moments WHERE parent_moment_id=? ORDER BY created_at ASC",
            (parent_moment_id,),
        )
        return [m for m in (_row_to_moment(d) for d in rows_to_dicts(rows)) if m]

    async def count_recent_for_author(
        self,
        author_user_id: str,
        *,
        since_iso: str,
    ) -> int:
        """Top-level moments only — replies are exempt from rate-limit."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM moments "
            "WHERE author_user_id=? AND created_at>=? "
            "AND parent_moment_id IS NULL",
            (author_user_id, since_iso),
        )
        return int(row["n"]) if row else 0

    # ── Reactions ──────────────────────────────────────────────────────

    async def set_reaction(
        self,
        moment_id: str,
        reactor_user_id: str,
        emoji: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO moment_reactions(moment_id, reactor_user_id, emoji)
            VALUES(?, ?, ?)
            ON CONFLICT(moment_id, reactor_user_id) DO UPDATE SET
                emoji=excluded.emoji,
                reacted_at=datetime('now')
            """,
            (moment_id, reactor_user_id, emoji),
        )

    async def clear_reaction(
        self,
        moment_id: str,
        reactor_user_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM moment_reactions WHERE moment_id=? AND reactor_user_id=?",
            (moment_id, reactor_user_id),
        )

    async def list_reactions(self, moment_id: str) -> list[MomentReaction]:
        rows = await self._db.fetchall(
            "SELECT moment_id, reactor_user_id, emoji, reacted_at "
            "FROM moment_reactions WHERE moment_id=? "
            "ORDER BY reacted_at ASC",
            (moment_id,),
        )
        return [
            MomentReaction(
                moment_id=r["moment_id"],
                reactor_user_id=r["reactor_user_id"],
                emoji=r["emoji"],
                reacted_at=r["reacted_at"],
            )
            for r in rows
        ]

    # ── Engagement counters ────────────────────────────────────────────

    async def count_engagement_for(
        self,
        moment_ids: list[str],
    ) -> dict[str, dict[str, int]]:
        """Aggregate reactions + replies for the given moment ids.

        Returns ``{moment_id: {"reaction_count": N, "reply_count": M}}``
        with zeros for ids that have no rows. One trip per table —
        cheap enough for the typical inbox page (≤ 50 ids).

        The Twitter-style row layout shows these counts inline, so the
        SPA can render the chip row without a per-row follow-up fetch.
        """
        out: dict[str, dict[str, int]] = {
            mid: {"reaction_count": 0, "reply_count": 0} for mid in moment_ids
        }
        if not moment_ids:
            return out
        placeholders = ",".join("?" for _ in moment_ids)
        rxn_rows = await self._db.fetchall(
            f"""
            SELECT moment_id, COUNT(*) AS n
              FROM moment_reactions
             WHERE moment_id IN ({placeholders})
             GROUP BY moment_id
            """,
            tuple(moment_ids),
        )
        for r in rxn_rows:
            out[r["moment_id"]]["reaction_count"] = int(r["n"])
        rep_rows = await self._db.fetchall(
            f"""
            SELECT parent_moment_id AS moment_id, COUNT(*) AS n
              FROM moments
             WHERE parent_moment_id IN ({placeholders})
             GROUP BY parent_moment_id
            """,
            tuple(moment_ids),
        )
        for r in rep_rows:
            out[r["moment_id"]]["reply_count"] = int(r["n"])
        return out

    # ── Hashtags ───────────────────────────────────────────────────────

    async def list_hashtags_for(self, moment_id: str) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT tag FROM moment_hashtags WHERE moment_id=? ORDER BY tag",
            (moment_id,),
        )
        return [r["tag"] for r in rows]

    async def list_top_hashtags(
        self,
        viewer_user_id: str,
        *,
        limit: int = 20,
    ) -> list[tuple[str, int]]:
        """Return ``[(tag, n), …]`` of the most-used tags inside the
        viewer's visible window.

        The aggregation re-applies the same visibility filter as
        :meth:`list_visible_to` so a tag from a blocked author or a
        moment past the viewer's retention window doesn't pollute the
        trending list. Sorted by count desc, then alphabetically so
        the order is stable for equal counts.
        """
        limit = max(1, min(int(limit), 50))
        rows = await self._db.fetchall(
            """
            SELECT mh.tag AS tag, COUNT(*) AS n
              FROM moment_hashtags AS mh
              JOIN moments AS m ON m.id = mh.moment_id
             WHERE m.author_user_id NOT IN (
                 SELECT blocked_user_id FROM user_blocks
                  WHERE blocker_user_id = ?
             )
               AND (
                 (julianday('now') - julianday(m.created_at)) * 24 < 24
                 OR EXISTS (
                     SELECT 1 FROM user_follows
                      WHERE follower_user_id = ?
                        AND followed_user_id = m.author_user_id
                 )
               )
               AND datetime(m.expires_at) > datetime('now')
             GROUP BY mh.tag
             ORDER BY n DESC, mh.tag ASC
             LIMIT ?
            """,
            (viewer_user_id, viewer_user_id, limit),
        )
        return [(r["tag"], int(r["n"])) for r in rows]

    # ── Retention ──────────────────────────────────────────────────────

    async def list_expired_media_urls(self) -> list[str]:
        """``media_url`` of every row :meth:`prune_expired` would drop.

        Collected before the prune so the caller can delete the backing
        files. The predicate matches ``prune_expired`` exactly, and the
        prune's ``DELETE`` runs a hair later (a superset), so every URL
        returned here is guaranteed to be pruned — we never report a URL
        whose row survives.
        """
        rows = await self._db.fetchall(
            "SELECT media_url FROM moments "
            "WHERE datetime(expires_at) < datetime('now') "
            "AND media_url IS NOT NULL",
        )
        return [r["media_url"] for r in rows_to_dicts(rows) if r.get("media_url")]

    async def prune_expired(self) -> int:
        """Drop rows past their absolute 7-day cap. Reactions cascade."""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM moments "
            "WHERE datetime(expires_at) < datetime('now')",
        )
        n = int(row["n"]) if row else 0
        if n == 0:
            return 0
        await self._db.enqueue(
            "DELETE FROM moments WHERE datetime(expires_at) < datetime('now')",
        )
        return n


def _row_to_moment(row: dict | None) -> Moment | None:
    if row is None:
        return None
    return Moment(
        id=row["id"],
        author_user_id=row["author_user_id"],
        content=row.get("content") or "",
        media_url=row.get("media_url"),
        media_type=row.get("media_type"),
        duration_ms=row.get("duration_ms"),
        parent_moment_id=row.get("parent_moment_id"),
        origin_instance_id=row["origin_instance_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        hop_count=int(row.get("hop_count") or 1),
        is_public=bool(row.get("is_public") or 0),
        received_via=row.get("received_via") or "self",
        received_via_gfs_id=row.get("received_via_gfs_id"),
    )
