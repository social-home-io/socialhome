"""Moment repository — persistence for the Momentum pillar (§Momentum).

Manages two tables:

* ``moments`` — per-author broadcast posts (text + optional image /
  short video). Holds local AND federated remote-author rows; the
  ``author_user_id`` column has no FK so peers' content can land
  alongside local content (same convention as
  ``conversation_messages.sender_user_id``).
* ``moment_reactions`` — one row per (moment_id, reactor_user_id),
  holding the current emoji.
* ``moment_saves`` — one row per (user_id, moment_id): a person keeps
  someone's moment past its expiry (decision 6b). The database drops a
  save when the moment is deleted or the person unfollows its author
  (migration 0124's triggers);
  :meth:`SqliteMomentRepo.drop_saves_from_origin` drops them on unpair.

A delete never drops the ``moments`` row: it becomes a **tombstone**
(content, media, tags and reactions wiped, ``deleted_at`` set) that lives
until the row's ``expires_at``, when :meth:`SqliteMomentRepo.prune_expired`
removes it like any other expired moment. Every read below skips
tombstones, and :meth:`SqliteMomentRepo.save` never writes over one — so a
replayed or late-arriving create for a deleted id cannot bring it back.

The visibility query in :meth:`list_visible_to` does *all* the
filtering in one statement: drop blocked authors, collapse the
author-chosen retention (``expires_at``, 1–30 days) to 24 h for
non-followers, and apply the
``before`` cursor for pagination.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import orjson

from ..db import AsyncDatabase
from ..domain.moment import (
    Moment,
    MomentReaction,
    MomentReactionRoute,
    MomentTombstone,
    SavedMoment,
    extract_hashtags,
    normalize_hashtag,
)
from .base import row_to_dict, rows_to_dicts
from .cp_repo import GUARDIAN_BLOCK_COUNTERPARTS_SQL

#: A protected account (§CP.R) never sees a moment that arrived through a GFS
#: (``received_via = 'gfs'`` — a stranger's public moment another member of
#: the household follows). Binds the viewer once.
_NOT_GFS_FOR_PROTECTED_SQL = """
    NOT (
        m.received_via = 'gfs'
        AND EXISTS (
            SELECT 1 FROM users
             WHERE user_id = ? AND child_protection_enabled = 1
        )
    )
"""

#: A moment written on this household (``received_via = 'self'``) and not
#: deleted: kept for its author past ``expires_at`` (decision 6a — "your
#: moments stay in your archive"). Local only: other households prune their
#: copy at the signed ``expires_at`` as before; nothing on the wire changes.
_OWN_ARCHIVED_SQL = "(m.received_via = 'self' AND m.deleted_at IS NULL)"

#: Somebody here saved the moment (decision 6b). A tombstone never has a
#: save (migration 0124 drops them on delete).
_SAVED_BY_ANYONE_SQL = (
    "EXISTS (SELECT 1 FROM moment_saves AS s WHERE s.moment_id = m.id)"
)

#: What :meth:`SqliteMomentRepo.prune_expired` removes: every row past its
#: ``expires_at`` except a live moment kept in its author's archive or
#: saved by someone here (kept, with its file, while any save of it
#: exists). A tombstone (own or not) still goes at expiry.
_PRUNABLE_SQL = (
    "datetime(m.expires_at) < datetime('now') AND NOT "
    + _OWN_ARCHIVED_SQL
    + " AND NOT "
    + _SAVED_BY_ANYONE_SQL
)

#: The viewer follows the moment's author — on a paired household
#: (``user_follows``) or through a GFS (``moment_public_follows``): both are
#: a follow, so both keep the author's moments until their ``expires_at``
#: (decision 6d). Binds the viewer twice.
_FOLLOWS_AUTHOR_SQL = """
    (
        EXISTS (
            SELECT 1 FROM user_follows
             WHERE follower_user_id = ? AND followed_user_id = m.author_user_id
        )
        OR EXISTS (
            SELECT 1 FROM moment_public_follows
             WHERE follower_user_id = ? AND followed_user_id = m.author_user_id
        )
    )
"""

#: What the inbox shows a viewer — the one rule :meth:`list_visible_to` and
#: :meth:`get_readable` share, so a detail read can never show what the
#: inbox hides: no personal block (§Privacy) or guardian block (§CP.F2,
#: either way) between viewer and author; younger than 24 h, or the viewer
#: follows the author (then until its ``expires_at``); not expired or deleted;
#: within the viewer's ``max_hops``; and never a moment that arrived through a
#: GFS (a stranger's public moment) for a protected account (§CP.R) — the
#: row is household-wide, so the filter is per viewer. Binds the viewer
#: :data:`_VISIBLE_VIEWER_BINDS` times, then the hop cap.
_VISIBLE_SQL = f"""
    {_NOT_GFS_FOR_PROTECTED_SQL}
    AND m.author_user_id NOT IN (
        SELECT blocked_user_id FROM user_blocks WHERE blocker_user_id = ?
    )
    AND m.author_user_id NOT IN ({GUARDIAN_BLOCK_COUNTERPARTS_SQL})
    AND (
        (julianday('now') - julianday(m.created_at)) * 24 < 24
        OR {_FOLLOWS_AUTHOR_SQL}
    )
    AND datetime(m.expires_at) > datetime('now')
    AND m.deleted_at IS NULL
    AND m.hop_count <= ?
"""

#: How many times :data:`_VISIBLE_SQL` binds the viewer (every placeholder
#: but the trailing hop cap).
_VISIBLE_VIEWER_BINDS = _VISIBLE_SQL.count("?") - 1

#: A moment the viewer saved (decision 6b): readable past the 24 h window
#: and past its expiry, for them only — still never across a personal or
#: guardian block (§CP.F2), never a GFS-delivered one for a protected
#: account (§CP.R), within the viewer's ``max_hops``. Binds the viewer
#: :data:`_SAVED_VIEWER_BINDS` times, then the hop cap.
_SAVED_READABLE_SQL = f"""
    EXISTS (
        SELECT 1 FROM moment_saves AS s
         WHERE s.moment_id = m.id AND s.user_id = ?
    )
    AND {_NOT_GFS_FOR_PROTECTED_SQL}
    AND m.author_user_id NOT IN (
        SELECT blocked_user_id FROM user_blocks WHERE blocker_user_id = ?
    )
    AND m.author_user_id NOT IN ({GUARDIAN_BLOCK_COUNTERPARTS_SQL})
    AND m.deleted_at IS NULL
    AND m.hop_count <= ?
"""

#: How many times :data:`_SAVED_READABLE_SQL` binds the viewer.
_SAVED_VIEWER_BINDS = _SAVED_READABLE_SQL.count("?") - 1

#: What one viewer may read: their own live moment (one written here also
#: past its expiry — :data:`_OWN_ARCHIVED_SQL`), anything the inbox shows
#: them (:data:`_VISIBLE_SQL`), or a moment they saved
#: (:data:`_SAVED_READABLE_SQL`). The detail read uses it, so it never
#: shows what the inbox and the Saved list hide. Bind with
#: :func:`_readable_params`.
_READABLE_SQL = f"""
    (
      (m.author_user_id = ? AND m.deleted_at IS NULL
       AND ({_OWN_ARCHIVED_SQL} OR datetime(m.expires_at) > datetime('now')))
      OR ({_VISIBLE_SQL})
      OR ({_SAVED_READABLE_SQL})
    )
"""


def _capped_hops(max_hops: int) -> int:
    """``max_hops`` clamped to the wire cap [1, 3] (below 1 would hide even
    the viewer's own)."""
    return max(1, min(int(max_hops), 3))


def _saved_params(viewer_user_id: str, max_hops: int) -> tuple:
    """Bindings for :data:`_SAVED_READABLE_SQL`."""
    return (*(viewer_user_id,) * _SAVED_VIEWER_BINDS, _capped_hops(max_hops))


def _readable_params(viewer_user_id: str, max_hops: int) -> tuple:
    """Bindings for :data:`_READABLE_SQL`."""
    return (
        viewer_user_id,
        *(viewer_user_id,) * _VISIBLE_VIEWER_BINDS,
        _capped_hops(max_hops),
        *_saved_params(viewer_user_id, max_hops),
    )


#: What one viewer may read of a *thread*: the inbox rule without its
#: 24-hour window. A reply drops out for a personal or guardian block, child
#: protection (§CP.R), the viewer's ``max_hops`` or expiry — never because
#: it is a day old and the viewer doesn't follow whoever wrote it, which
#: would empty a thread whose root (a followed author's) is still readable
#: and shrink its count. Binds the viewer, then the hop cap; bind with
#: :func:`_thread_params`.
_THREAD_READABLE_SQL = f"""
    (
      (m.author_user_id = ? AND m.deleted_at IS NULL
       AND ({_OWN_ARCHIVED_SQL} OR datetime(m.expires_at) > datetime('now')))
      OR (
        {_NOT_GFS_FOR_PROTECTED_SQL}
        AND m.author_user_id NOT IN (
            SELECT blocked_user_id FROM user_blocks WHERE blocker_user_id = ?
        )
        AND m.author_user_id NOT IN ({GUARDIAN_BLOCK_COUNTERPARTS_SQL})
        AND datetime(m.expires_at) > datetime('now')
        AND m.deleted_at IS NULL
        AND m.hop_count <= ?
      )
    )
"""


def _thread_params(viewer_user_id: str, max_hops: int) -> tuple:
    """Bindings for :data:`_THREAD_READABLE_SQL`: the viewer for every
    placeholder but the last, then ``max_hops`` clamped to [1, 3]."""
    capped_hops = max(1, min(int(max_hops), 3))
    return (*(viewer_user_id,) * (_THREAD_READABLE_SQL.count("?") - 1), capped_hops)


@runtime_checkable
class AbstractMomentRepo(Protocol):
    async def save(self, moment: Moment) -> Moment: ...
    async def get(
        self,
        moment_id: str,
        *,
        include_deleted: bool = False,
    ) -> Moment | None: ...
    async def delete(self, moment_id: str) -> None: ...
    async def tombstone(
        self,
        moment_id: str,
        *,
        author_user_id: str,
        origin_instance_id: str,
        expires_at: str,
    ) -> MomentTombstone: ...
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
    async def list_replies(
        self,
        parent_moment_id: str,
        *,
        viewer_user_id: str | None = None,
        max_hops: int = 3,
    ) -> list[Moment]: ...
    async def get_readable(
        self,
        moment_id: str,
        viewer_user_id: str,
        *,
        max_hops: int = 3,
    ) -> Moment | None: ...
    async def mark_public(self, moment_id: str) -> None: ...
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
    async def apply_reaction_at(
        self,
        moment_id: str,
        reactor_user_id: str,
        emoji: str | None,
        *,
        reacted_at: str,
    ) -> bool: ...

    # Reaction routes (v_67) -----------------------------------------------
    async def set_reaction_route(
        self, moment_id: str, route: MomentReactionRoute
    ) -> None: ...
    async def get_reaction_routes(
        self, moment_ids: list[str]
    ) -> dict[str, MomentReactionRoute]: ...

    # Engagement counters --------------------------------------------------
    async def count_engagement_for(
        self,
        moment_ids: list[str],
        *,
        viewer_user_id: str | None = None,
        max_hops: int = 3,
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
    async def list_own_archive(
        self,
        author_user_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> list[Moment]: ...
    async def list_own_media(
        self, author_user_id: str | None = None
    ) -> list[Moment]: ...
    async def clear_media(self, moment_id: str) -> bool: ...

    # Saves (decision 6b) -------------------------------------------------
    async def add_save(self, user_id: str, moment_id: str) -> bool: ...
    async def remove_save(self, user_id: str, moment_id: str) -> None: ...
    async def saved_ids(self, user_id: str, moment_ids: list[str]) -> set[str]: ...
    async def list_saved(
        self,
        user_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
        max_hops: int = 3,
    ) -> list[SavedMoment]: ...
    async def list_saved_media(
        self, user_id: str | None = None
    ) -> list[SavedMoment]: ...
    async def clear_saved_media(self, moment_id: str, user_id: str) -> bool: ...
    async def drop_saves_from_origin(self, instance_id: str) -> int: ...


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
            WHERE moments.deleted_at IS NULL
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
                "INSERT OR IGNORE INTO moment_hashtags(moment_id, tag)"
                " SELECT ?, ? WHERE EXISTS("
                "SELECT 1 FROM moments WHERE id=? AND deleted_at IS NULL)",
                (moment.id, tag, moment.id),
            )
        return moment

    async def get(
        self,
        moment_id: str,
        *,
        include_deleted: bool = False,
    ) -> Moment | None:
        """The live moment, or ``None``. ``include_deleted`` also returns a
        tombstone (``deleted_at`` set) — for the id→author binding check."""
        row = await self._db.fetchone(
            "SELECT * FROM moments WHERE id=?"
            + ("" if include_deleted else " AND deleted_at IS NULL"),
            (moment_id,),
        )
        return _row_to_moment(row_to_dict(row))

    async def delete(self, moment_id: str) -> None:
        """Turn a stored moment into a tombstone; no-op for an unknown id."""
        await self._db.transact(
            lambda conn: _tombstone_rows(conn, "id=?", (moment_id,)),
        )

    async def tombstone(
        self,
        moment_id: str,
        *,
        author_user_id: str,
        origin_instance_id: str,
        expires_at: str,
    ) -> MomentTombstone:
        """Record that ``moment_id`` was deleted, stored or not.

        A stored row is wiped in place and keeps its own ``expires_at`` (a
        replay of its create carries the same expiry, so it is refused for
        as long as it could be shown). An unknown id gets a content-free
        row expiring at ``expires_at`` — the caller passes the maximum
        moment lifetime, so the delete is held until any create for it
        could arrive. Says what changed, in one transaction:
        :attr:`MomentTombstone.REMOVED` (a live moment wiped),
        :attr:`~MomentTombstone.RECORDED` (a new tombstone for an id never
        held) or :attr:`~MomentTombstone.ALREADY` (nothing — a replay).
        """

        def _run(conn) -> MomentTombstone:
            if _tombstone_rows(conn, "id=?", (moment_id,)) > 0:
                return MomentTombstone.REMOVED
            cur = conn.execute(
                "INSERT OR IGNORE INTO moments(id, author_user_id, content,"
                " origin_instance_id, expires_at, received_via, deleted_at)"
                " VALUES(?, ?, '', ?, ?, 'household', datetime('now'))",
                (moment_id, author_user_id, origin_instance_id, expires_at),
            )
            if cur.rowcount > 0:
                return MomentTombstone.RECORDED
            return MomentTombstone.ALREADY

        return await self._db.transact(_run)

    async def delete_by_author(self, author_user_id: str) -> int:
        """Tombstone every moment authored by ``author_user_id``.

        Drives the §Connection-Detail visibility cascade: when an inbound
        ``USER_REMOVED`` lands for this user, every moment they ever
        posted is removed from our local view. Forward-only would leave
        orphan content from a user whose row was just deprovisioned —
        the receiver-side rule treats hide as full removal, matching
        the "hide = remove" UX the SPA copy promises.
        """
        return await self._db.transact(
            lambda conn: _tombstone_rows(conn, "author_user_id=?", (author_user_id,)),
        )

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
        * Author is not on the viewer's :table:`user_blocks`, and no
          guardian block (§CP.F2) separates the two.
        * EITHER the moment is < 24 h old, OR the viewer follows the
          author — directly or through a GFS — (then until the moment's
          ``expires_at``, the author's choice of 1–30 days).
        * The row's ``hop_count`` is ≤ the viewer's ``max_hops``
          preference (default 3 = the wire cap, so all rows pass).
        * ``before`` is an ISO-8601 ``created_at`` cursor; ``NULL``
          fetches from newest.
        * ``tag`` (lowercase) restricts the result to moments tagged
          with that hashtag — used by the Browse / archive page.
        """
        limit = max(1, min(int(limit), 100))
        normalised_tag = normalize_hashtag(tag) if tag else None
        # Clamp ``max_hops`` to [1, 3]: the wire cap is 3 and a
        # value below 1 would hide every row including the user's
        # own moments.
        capped_hops = max(1, min(int(max_hops), 3))
        rows = await self._db.fetchall(
            f"""
            SELECT m.* FROM moments AS m
             WHERE {_VISIBLE_SQL}
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
                *(viewer_user_id,) * _VISIBLE_VIEWER_BINDS,
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
               AND deleted_at IS NULL
               AND datetime(expires_at) > datetime('now')
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (author_user_id, limit),
        )
        return [m for m in (_row_to_moment(d) for d in rows_to_dicts(rows)) if m]

    async def mark_public(self, moment_id: str) -> None:
        """Mark a moment received from another household as public: the
        author also published it through a GFS, and that verified copy
        arrived after the paired one. Only that flag changes — content,
        hops and where the row came from stay. Never a moment authored
        here (``received_via='self'`` decides its own) or a tombstone."""
        await self._db.enqueue(
            "UPDATE moments SET is_public = 1"
            " WHERE id = ? AND received_via != 'self' AND deleted_at IS NULL",
            (moment_id,),
        )

    async def get_readable(
        self,
        moment_id: str,
        viewer_user_id: str,
        *,
        max_hops: int = 3,
    ) -> Moment | None:
        """``moment_id`` iff *viewer_user_id* may read it: its author (while
        it is live, or from their archive once expired), or anyone the inbox
        would show it to
        (:data:`_VISIBLE_SQL`). ``None`` for an unknown id and a hidden one
        alike."""
        row = await self._db.fetchone(
            f"SELECT m.* FROM moments AS m WHERE m.id = ? AND {_READABLE_SQL}",
            (moment_id, *_readable_params(viewer_user_id, max_hops)),
        )
        return _row_to_moment(row_to_dict(row)) if row is not None else None

    async def list_replies(
        self,
        parent_moment_id: str,
        *,
        viewer_user_id: str | None = None,
        max_hops: int = 3,
    ) -> list[Moment]:
        """Replies in chronological order so the thread reads top-down.

        With ``viewer_user_id``, only the replies that viewer may read —
        :data:`_THREAD_READABLE_SQL`, the inbox rule without its 24-hour
        window: no personal or guardian block, within ``max_hops``, never a
        GFS-delivered reply for a protected viewer (§CP.R)."""
        if viewer_user_id is None:
            rows = await self._db.fetchall(
                "SELECT * FROM moments WHERE parent_moment_id=?"
                " AND deleted_at IS NULL ORDER BY created_at ASC",
                (parent_moment_id,),
            )
        else:
            rows = await self._db.fetchall(
                f"SELECT m.* FROM moments AS m WHERE m.parent_moment_id=?"
                f" AND {_THREAD_READABLE_SQL} ORDER BY m.created_at ASC",
                (parent_moment_id, *_thread_params(viewer_user_id, max_hops)),
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
            "AND parent_moment_id IS NULL AND deleted_at IS NULL",
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

    async def apply_reaction_at(
        self,
        moment_id: str,
        reactor_user_id: str,
        emoji: str | None,
        *,
        reacted_at: str,
    ) -> bool:
        """Apply a reaction made at *reacted_at* (``YYYY-MM-DD
        HH:MM:SS.mmm`` UTC, the column's shape with milliseconds) only when
        it is newer than both the stored reaction and the stored removal —
        reactions routed from far away (v_67) can arrive out of order or be
        replayed. ``emoji is None`` removes it and keeps a removal watermark
        (``moment_reaction_removals``, migration 0125) so an older add can
        never bring it back, even after a restart. Returns whether a
        reaction row changed; one transaction, live moments only."""

        def _run(conn) -> bool:
            if (
                conn.execute(
                    "SELECT 1 FROM moments WHERE id=? AND deleted_at IS NULL",
                    (moment_id,),
                ).fetchone()
                is None
            ):
                return False
            key = (moment_id, reactor_user_id)
            stored = conn.execute(
                "SELECT emoji, reacted_at FROM moment_reactions"
                " WHERE moment_id=? AND reactor_user_id=?",
                key,
            ).fetchone()
            removed = conn.execute(
                "SELECT removed_at FROM moment_reaction_removals"
                " WHERE moment_id=? AND reactor_user_id=?",
                key,
            ).fetchone()
            marks = [str(r[-1]) for r in (stored, removed) if r is not None]
            if marks and reacted_at <= max(marks):
                return False  # not newer than what we hold: older, or a replay
            if emoji is None:
                # The trigger drops the reaction row along with the insert.
                conn.execute(
                    "INSERT INTO moment_reaction_removals(moment_id,"
                    " reactor_user_id, removed_at) VALUES(?, ?, ?)"
                    " ON CONFLICT(moment_id, reactor_user_id) DO UPDATE SET"
                    " removed_at=excluded.removed_at",
                    (*key, reacted_at),
                )
                return stored is not None
            # The triggers drop the removal watermark along with the upsert.
            conn.execute(
                "INSERT INTO moment_reactions(moment_id, reactor_user_id, emoji,"
                " reacted_at) VALUES(?, ?, ?, ?)"
                " ON CONFLICT(moment_id, reactor_user_id) DO UPDATE SET"
                " emoji=excluded.emoji, reacted_at=excluded.reacted_at",
                (*key, emoji, reacted_at),
            )
            return stored is None or stored[0] != emoji

        return await self._db.transact(_run)

    # ── Reaction routes (v_67) ─────────────────────────────────────────

    async def set_reaction_route(
        self, moment_id: str, route: MomentReactionRoute
    ) -> None:
        """Keep how a reaction to *moment_id* gets back to its origin. Only
        on a live moment that arrived from another household (the column's
        CHECK); a tombstone never keeps one (migration 0125's trigger)."""
        await self._db.enqueue(
            "UPDATE moments SET reaction_route=? WHERE id=?"
            " AND received_via='household' AND deleted_at IS NULL",
            (
                orjson.dumps(
                    {
                        "kw": route.keywrap_pk,
                        "via": route.via,
                        "rt": list(route.tokens),
                        "rt_suite": route.token_suite,
                    }
                ).decode(),
                moment_id,
            ),
        )

    async def get_reaction_routes(
        self, moment_ids: list[str]
    ) -> dict[str, MomentReactionRoute]:
        """The stored route of each live moment in *moment_ids* that has
        one (an unreadable row is skipped)."""
        if not moment_ids:
            return {}
        out: dict[str, MomentReactionRoute] = {}
        for start in range(0, len(moment_ids), 500):
            chunk = moment_ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = await self._db.fetchall(
                f"SELECT id, reaction_route FROM moments WHERE id IN ({marks})"
                " AND reaction_route IS NOT NULL AND deleted_at IS NULL",
                tuple(chunk),
            )
            for row in rows:
                route = _route_from_json(row["reaction_route"])
                if route is not None:
                    out[row["id"]] = route
        return out

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
        *,
        viewer_user_id: str | None = None,
        max_hops: int = 3,
    ) -> dict[str, dict[str, int]]:
        """Aggregate reactions + replies for the given moment ids.

        Returns ``{moment_id: {"reaction_count": N, "reply_count": M}}``
        with zeros for ids that have no rows. One trip per table —
        cheap enough for the typical inbox page (≤ 50 ids).

        With ``viewer_user_id``, ``reply_count`` counts only the replies
        :meth:`list_replies` shows that viewer, so the count never
        hints at a reply a block hides.

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
        if viewer_user_id is None:
            readable, params = "m.deleted_at IS NULL", ()
        else:
            readable = _THREAD_READABLE_SQL
            params = _thread_params(viewer_user_id, max_hops)
        rep_rows = await self._db.fetchall(
            f"""
            SELECT m.parent_moment_id AS moment_id, COUNT(*) AS n
              FROM moments AS m
             WHERE m.parent_moment_id IN ({placeholders})
               AND {readable}
             GROUP BY m.parent_moment_id
            """,
            (*moment_ids, *params),
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
            f"""
            SELECT mh.tag AS tag, COUNT(*) AS n
              FROM moment_hashtags AS mh
              JOIN moments AS m ON m.id = mh.moment_id
             WHERE {_NOT_GFS_FOR_PROTECTED_SQL}
               AND m.author_user_id NOT IN (
                 SELECT blocked_user_id FROM user_blocks
                  WHERE blocker_user_id = ?
             )
               AND m.author_user_id NOT IN ({GUARDIAN_BLOCK_COUNTERPARTS_SQL})
               AND (
                 (julianday('now') - julianday(m.created_at)) * 24 < 24
                 OR {_FOLLOWS_AUTHOR_SQL}
               )
               AND datetime(m.expires_at) > datetime('now')
               AND m.deleted_at IS NULL
             GROUP BY mh.tag
             ORDER BY n DESC, mh.tag ASC
             LIMIT ?
            """,
            (viewer_user_id,) * _VISIBLE_VIEWER_BINDS + (limit,),
        )
        return [(r["tag"], int(r["n"])) for r in rows]

    # ── Retention ──────────────────────────────────────────────────────

    async def list_expired_media_urls(self) -> list[str]:
        """``media_url`` of every row :meth:`prune_expired` would drop.

        Collected before the prune so the caller can delete the backing
        files. The predicate matches ``prune_expired`` exactly, and the
        prune's ``DELETE`` runs a hair later (a superset), so every URL
        returned here is guaranteed to be pruned — we never report a URL
        whose row survives. A moment kept in its author's archive keeps
        its file.
        """
        rows = await self._db.fetchall(
            f"SELECT m.media_url FROM moments AS m WHERE {_PRUNABLE_SQL}"
            " AND m.media_url IS NOT NULL",
        )
        return [r["media_url"] for r in rows_to_dicts(rows) if r.get("media_url")]

    async def prune_expired(self) -> int:
        """Drop rows past their ``expires_at`` — others' moments and
        every delete tombstone. A live moment written here stays in its
        author's archive (decision 6a). Reactions cascade.

        A public moment's share-link item key (``moment_public_shares``)
        goes at expiry too, archived or not: a share copy never outlives
        the moment, so the key has nothing left to open."""
        row = await self._db.fetchone(
            f"SELECT COUNT(*) AS n FROM moments AS m WHERE {_PRUNABLE_SQL}",
        )
        n = int(row["n"]) if row else 0

        def _run(conn) -> None:
            conn.execute(
                "DELETE FROM moment_public_shares WHERE moment_id IN ("
                " SELECT id FROM moments"
                " WHERE datetime(expires_at) < datetime('now'))"
            )
            if n:
                conn.execute(
                    "DELETE FROM moments WHERE id IN ("
                    f" SELECT m.id FROM moments AS m WHERE {_PRUNABLE_SQL})"
                )

        await self._db.transact(_run)
        return n

    # ── Your own archive (decision 6a) ─────────────────────────────────

    async def list_own_archive(
        self,
        author_user_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> list[Moment]:
        """Every moment *author_user_id* wrote here and hasn't deleted —
        top-level moments and replies, live and past expiry — newest
        first. ``before`` is the ``created_at`` cursor of the last row of
        the previous page."""
        limit = max(1, min(int(limit), 100))
        rows = await self._db.fetchall(
            f"""
            SELECT m.* FROM moments AS m
             WHERE m.author_user_id = ? AND {_OWN_ARCHIVED_SQL}
               AND (? IS NULL OR m.created_at < ?)
             ORDER BY m.created_at DESC
             LIMIT ?
            """,
            (author_user_id, before, before, limit),
        )
        return [m for m in (_row_to_moment(d) for d in rows_to_dicts(rows)) if m]

    async def list_own_media(self, author_user_id: str | None = None) -> list[Moment]:
        """Archive moments that hold a file — one author's, or (``None``)
        everyone's — grouped by author, oldest first: the order the
        per-person archive cap drops pictures in."""
        rows = await self._db.fetchall(
            f"""
            SELECT m.* FROM moments AS m
             WHERE {_OWN_ARCHIVED_SQL} AND m.media_url IS NOT NULL
               AND (? IS NULL OR m.author_user_id = ?)
             ORDER BY m.author_user_id ASC, m.created_at ASC
            """,
            (author_user_id, author_user_id),
        )
        return [m for m in (_row_to_moment(d) for d in rows_to_dicts(rows)) if m]

    async def clear_media(self, moment_id: str) -> bool:
        """Drop the file reference of an archived moment (its text stays).

        Only a moment past its expiry that is kept in its author's archive:
        one other people can still see keeps its picture. ``True`` iff a
        row changed; the caller removes the file once nothing references
        it."""
        changed = await self._db.enqueue_rowcount(
            "UPDATE moments AS m SET media_url = NULL, media_type = NULL,"
            " duration_ms = NULL"
            f" WHERE m.id = ? AND {_OWN_ARCHIVED_SQL}"
            " AND m.media_url IS NOT NULL"
            " AND datetime(m.expires_at) < datetime('now')",
            (moment_id,),
        )
        return changed > 0

    # ── Saves (decision 6b) ────────────────────────────────────────────

    async def add_save(self, user_id: str, moment_id: str) -> bool:
        """*user_id* keeps *moment_id*. ``True`` iff the save exists
        afterwards — a repeat keeps the first ``saved_at``; an unknown id or
        a tombstone is never saved (``False``). The caller checks that the
        person may read the moment and that a delete can reach it."""
        await self._db.enqueue(
            "INSERT OR IGNORE INTO moment_saves(user_id, moment_id, saved_at)"
            " SELECT ?, id, ? FROM moments WHERE id = ? AND deleted_at IS NULL",
            (user_id, datetime.now(UTC).isoformat(), moment_id),
        )
        return bool(await self.saved_ids(user_id, [moment_id]))

    async def remove_save(self, user_id: str, moment_id: str) -> None:
        """Forget *user_id*'s save of *moment_id* (no-op when absent). The
        row then goes at the next prune if it is past expiry and nobody
        else keeps it."""
        await self._db.enqueue(
            "DELETE FROM moment_saves WHERE user_id = ? AND moment_id = ?",
            (user_id, moment_id),
        )

    async def saved_ids(self, user_id: str, moment_ids: list[str]) -> set[str]:
        """Which of *moment_ids* *user_id* saved — one query per page."""
        if not moment_ids:
            return set()
        placeholders = ",".join("?" for _ in moment_ids)
        rows = await self._db.fetchall(
            "SELECT moment_id FROM moment_saves"
            f" WHERE user_id = ? AND moment_id IN ({placeholders})",
            (user_id, *moment_ids),
        )
        return {r["moment_id"] for r in rows}

    async def list_saved(
        self,
        user_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
        max_hops: int = 3,
    ) -> list[SavedMoment]:
        """*user_id*'s saved moments they may still read
        (:data:`_SAVED_READABLE_SQL`), newest save first. ``before`` is the
        ``saved_at`` of the last row of the previous page."""
        limit = max(1, min(int(limit), 100))
        rows = await self._db.fetchall(
            f"""
            SELECT m.*, sv.saved_at AS sv_saved_at
              FROM moment_saves AS sv
              JOIN moments AS m ON m.id = sv.moment_id
             WHERE sv.user_id = ?
               AND {_SAVED_READABLE_SQL}
               AND (? IS NULL OR sv.saved_at < ?)
             ORDER BY sv.saved_at DESC
             LIMIT ?
            """,
            (user_id, *_saved_params(user_id, max_hops), before, before, limit),
        )
        return _saved_rows(rows_to_dicts(rows), user_id)

    async def list_saved_media(self, user_id: str | None = None) -> list[SavedMoment]:
        """Saves of moments that hold a file and weren't written here — one
        saver's, or (``None``) everyone's — grouped by saver, oldest save
        first: the order the per-person archive cap drops saved pictures
        in. A housemate's moment counts toward its author's archive, not the
        saver's, so it is left out."""
        rows = await self._db.fetchall(
            """
            SELECT m.*, sv.user_id AS sv_user_id, sv.saved_at AS sv_saved_at
              FROM moment_saves AS sv
              JOIN moments AS m ON m.id = sv.moment_id
             WHERE m.received_via != 'self' AND m.media_url IS NOT NULL
               AND m.deleted_at IS NULL
               AND (? IS NULL OR sv.user_id = ?)
             ORDER BY sv.user_id ASC, sv.saved_at ASC
            """,
            (user_id, user_id),
        )
        return _saved_rows(rows_to_dicts(rows))

    async def clear_saved_media(self, moment_id: str, user_id: str) -> bool:
        """Drop the file reference of a moment *user_id* saved (its text
        stays) — only past its expiry (others who still see it keep the
        picture) and only when no one else here saved it (one person's cap
        never takes another's picture). ``True`` iff a row changed; the
        caller removes the file once nothing references it."""
        changed = await self._db.enqueue_rowcount(
            "UPDATE moments AS m SET media_url = NULL, media_type = NULL,"
            " duration_ms = NULL"
            " WHERE m.id = ? AND m.received_via != 'self'"
            " AND m.media_url IS NOT NULL AND m.deleted_at IS NULL"
            " AND datetime(m.expires_at) < datetime('now')"
            " AND EXISTS (SELECT 1 FROM moment_saves"
            "  WHERE moment_id = m.id AND user_id = ?)"
            " AND NOT EXISTS (SELECT 1 FROM moment_saves"
            "  WHERE moment_id = m.id AND user_id != ?)",
            (moment_id, user_id, user_id),
        )
        return changed > 0

    async def drop_saves_from_origin(self, instance_id: str) -> int:
        """Drop every save of a moment that came from the household
        *instance_id* over the mesh (``received_via = 'household'``, by its
        ``origin_instance_id``) — on unpair (sign-off 3): that household's
        later deletes can no longer reach the copy. A moment stores only
        its origin, not the peer that relayed it; saving is offered only
        when the origin itself is a paired household, so the origin is the
        household it came through. Returns how many saves went."""
        return await self._db.enqueue_rowcount(
            "DELETE FROM moment_saves WHERE moment_id IN ("
            " SELECT id FROM moments"
            "  WHERE origin_instance_id = ? AND received_via = 'household')",
            (instance_id,),
        )


def _saved_rows(rows: list[dict], user_id: str | None = None) -> list[SavedMoment]:
    """``SELECT m.*, sv_saved_at[, sv_user_id]`` rows → :class:`SavedMoment`."""
    out: list[SavedMoment] = []
    for row in rows:
        saved_at = str(row.pop("sv_saved_at"))
        saver = str(row.pop("sv_user_id", None) or user_id or "")
        moment = _row_to_moment(row)
        if moment is not None:
            out.append(SavedMoment(moment=moment, user_id=saver, saved_at=saved_at))
    return out


def _tombstone_rows(conn, where: str, params: tuple) -> int:
    """Wipe the live moments matching ``where`` down to tombstones.

    Runs inside :meth:`AsyncDatabase.transact`. Keeps the id, author, origin
    and ``expires_at`` (what a tombstone must remember); drops content,
    media, tags, reactions, every save (migration 0124's trigger) and the
    reaction route (0125's trigger), in this same transaction; detaches
    replies (``parent_moment_id`` →
    NULL, as the old hard delete's ``ON DELETE SET NULL`` did). Returns the
    number of live moments tombstoned. ``where`` is a fixed column
    predicate from this module, never caller input.
    """
    ids = [
        r[0]
        for r in conn.execute(
            f"SELECT id FROM moments WHERE {where} AND deleted_at IS NULL",
            params,
        ).fetchall()
    ]
    for mid in ids:
        conn.execute("DELETE FROM moment_hashtags WHERE moment_id=?", (mid,))
        conn.execute("DELETE FROM moment_reactions WHERE moment_id=?", (mid,))
        conn.execute(
            "UPDATE moments SET parent_moment_id=NULL WHERE parent_moment_id=?",
            (mid,),
        )
        conn.execute(
            "UPDATE moments SET content='', media_url=NULL, media_type=NULL,"
            " duration_ms=NULL, parent_moment_id=NULL, is_public=0,"
            " deleted_at=datetime('now') WHERE id=?",
            (mid,),
        )
    return len(ids)


def _route_from_json(text: object) -> MomentReactionRoute | None:
    """A stored ``reaction_route`` as :class:`MomentReactionRoute`, or
    ``None`` when it doesn't parse into one."""
    try:
        data = orjson.loads(str(text))
        tokens = data["rt"]
        if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
            return None
        return MomentReactionRoute(
            keywrap_pk=str(data["kw"]),
            via=str(data["via"]),
            tokens=tuple(tokens),
            token_suite=str(data["rt_suite"]),
        )
    except orjson.JSONDecodeError, KeyError, TypeError:
        return None


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
        deleted_at=row.get("deleted_at"),
    )
