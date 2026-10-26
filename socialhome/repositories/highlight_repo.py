"""Highlight repository (§Highlights).

Backs :class:`HighlightService` with SQLite I/O. Holds no business logic
beyond the queries — see ``services/highlight_service.py`` for the
"create-or-append-today", "fan out to peers", and "expire by retention"
flows.

Audience evaluation lives here only as a query filter: outbound
federation fan-out is done at the service layer (it needs to know about
:class:`RemoteInstance`s, which the repo does not). Receivers that
shouldn't see a highlight drop the inbound envelope before it reaches the
repo at all, so by the time a row exists locally we treat all
on-instance users as "in audience" — except for ``USERS``-kind highlights,
where the per-user allow-list is enforced on read.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.highlight import (
    Highlight,
    HighlightAudience,
    HighlightFrame,
    HighlightFrameReaction,
    HighlightFrameType,
    HighlightFrameView,
)
from .base import dump_json, load_json, row_to_dict, rows_to_dicts
from .cp_repo import GUARDIAN_BLOCK_COUNTERPARTS_SQL

#: Rows a viewer may read whoever the audience is: not expired, and the
#: author is on neither the viewer's personal block list (§Privacy) nor a
#: guardian block with them (§CP.F2, both directions). Binds the viewer
#: three times.
_READABLE_SQL = f"""
    datetime(expires_at) > datetime('now')
    AND author_user_id NOT IN (
        SELECT blocked_user_id FROM user_blocks WHERE blocker_user_id = ?
    )
    AND author_user_id NOT IN ({GUARDIAN_BLOCK_COUNTERPARTS_SQL})
"""

#: The viewer is in the highlight's audience: ``all_paired`` /
#: ``households`` admit every local user, ``users`` only the listed ones,
#: and the author always sees their own. Binds the viewer twice.
_IN_AUDIENCE_SQL = """
    (
        audience_kind IN ('all_paired','households')
        OR (audience_kind = 'users' AND EXISTS (
            SELECT 1 FROM json_each(highlights.audience_json) j
            WHERE j.value = ?
        ))
        OR author_user_id = ?
    )
"""


@runtime_checkable
class AbstractHighlightRepo(Protocol):
    async def find_or_create_today(
        self,
        *,
        author_user_id: str,
        audience_kind: HighlightAudience,
        audience: builtins.tuple[str, ...],
        highlight_date: str,
        expires_at: str,
    ) -> Highlight: ...
    async def append_frame(
        self,
        *,
        highlight_id: str,
        frame_type: HighlightFrameType,
        media_url: str,
        caption_text: str | None = None,
        caption_emoji: str | None = None,
        duration_ms: int | None = None,
    ) -> HighlightFrame: ...
    async def get_highlight(self, highlight_id: str) -> Highlight | None: ...
    async def get_frame(self, frame_id: str) -> HighlightFrame | None: ...
    async def list_frames(self, highlight_id: str) -> builtins.list[HighlightFrame]: ...
    async def list_visible_to(
        self,
        viewer_user_id: str,
    ) -> builtins.list[Highlight]: ...
    async def get_readable(
        self,
        highlight_id: str,
        viewer_user_id: str,
        *,
        require_audience: bool = True,
    ) -> Highlight | None: ...
    async def list_authored(
        self,
        author_user_id: str,
        *,
        limit: int = 50,
    ) -> builtins.list[Highlight]: ...
    async def mark_viewed(self, frame_id: str, viewer_user_id: str) -> bool: ...
    async def list_views_for_frame(
        self,
        frame_id: str,
    ) -> builtins.list[HighlightFrameView]: ...
    async def count_unseen_frames(
        self,
        highlight_id: str,
        viewer_user_id: str,
    ) -> int: ...
    async def count_viewers(self, highlight_id: str) -> int: ...
    async def set_reaction(
        self,
        frame_id: str,
        reactor_user_id: str,
        emoji: str,
    ) -> HighlightFrameReaction: ...
    async def clear_reaction(
        self,
        frame_id: str,
        reactor_user_id: str,
    ) -> None: ...
    async def list_reactions_for_frame(
        self,
        frame_id: str,
    ) -> builtins.list[HighlightFrameReaction]: ...
    async def reactions_by(
        self,
        reactor_user_id: str,
        frame_ids: Iterable[str],
    ) -> dict[str, str]: ...
    async def delete_frame(self, frame_id: str) -> None: ...
    async def delete_highlight(self, highlight_id: str) -> None: ...
    async def delete_by_author(self, author_user_id: str) -> int: ...
    async def list_expired_frame_media_urls(self) -> builtins.list[str]: ...
    async def list_over_max_frame_media_urls(
        self, author_user_id: str, max_count: int
    ) -> builtins.list[str]: ...
    async def prune_expired(self) -> int: ...
    async def prune_over_max(self, author_user_id: str, max_count: int) -> int: ...
    async def save_highlight(self, highlight: Highlight) -> Highlight: ...
    async def save_frame(self, frame: HighlightFrame) -> HighlightFrame: ...
    async def list_authors_with_highlights(self) -> builtins.list[str]: ...

    # Public publication state (§highlights_public). The GFS mints the share
    # tokens; this household keeps a SHA-256 of each one it minted (and has
    # not revoked) so it streams only for its own live links.
    async def mark_published(
        self,
        highlight_id: str,
        *,
        gfs_id: str,
        published_at: str,
    ) -> None: ...
    async def mark_unpublished(self, highlight_id: str) -> None: ...
    async def list_published_for(
        self,
        author_user_id: str,
    ) -> builtins.list[Highlight]: ...
    async def add_public_token(
        self, highlight_id: str, *, gfs_id: str, token_hash: str
    ) -> None: ...
    async def remove_public_token(self, highlight_id: str, token_hash: str) -> None: ...
    async def is_live_public_token(
        self, highlight_id: str, token_hash: str
    ) -> bool: ...


class SqliteHighlightRepo:
    """SQLite-backed :class:`AbstractHighlightRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Highlight rows ───────────────────────────────────────────────────────

    async def find_or_create_today(
        self,
        *,
        author_user_id: str,
        audience_kind: HighlightAudience,
        audience: tuple[str, ...],
        highlight_date: str,
        expires_at: str,
    ) -> Highlight:
        existing = await self._db.fetchone(
            "SELECT * FROM highlights WHERE author_user_id=? AND highlight_date=?",
            (author_user_id, highlight_date),
        )
        if existing is not None:
            return _row_to_highlight(row_to_dict(existing))  # type: ignore[return-value]
        highlight = Highlight(
            id=uuid.uuid4().hex,
            author_user_id=author_user_id,
            highlight_date=highlight_date,
            audience_kind=audience_kind,
            audience=tuple(audience),
            created_at=datetime.now(UTC).isoformat(),
            expires_at=expires_at,
        )
        # Two first frames of the day can race here: the loser's insert is
        # a no-op and it returns the winner's row, so the service compares
        # audiences (append or 409) instead of failing on UNIQUE.
        await self._db.enqueue(
            """
            INSERT INTO highlights(
                id, author_user_id, highlight_date, audience_kind, audience_json,
                created_at, expires_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(author_user_id, highlight_date) DO NOTHING
            """,
            (
                highlight.id,
                highlight.author_user_id,
                highlight.highlight_date,
                highlight.audience_kind.value,
                dump_json(list(highlight.audience)),
                highlight.created_at,
                highlight.expires_at,
            ),
        )
        stored = await self._db.fetchone(
            "SELECT * FROM highlights WHERE author_user_id=? AND highlight_date=?",
            (author_user_id, highlight_date),
        )
        return _row_to_highlight(row_to_dict(stored)) or highlight

    async def get_highlight(self, highlight_id: str) -> Highlight | None:
        row = await self._db.fetchone(
            "SELECT * FROM highlights WHERE id=?",
            (highlight_id,),
        )
        return _row_to_highlight(row_to_dict(row))

    async def list_visible_to(self, viewer_user_id: str) -> list[Highlight]:
        # The receiver dropped envelopes outside the audience already, so
        # by the time we read locally we trust ``highlights`` rows. The
        # ``USERS``-kind filter still applies because one row can be
        # surfaced to a strict subset of an instance's users. Personal
        # blocks (§Privacy) and guardian blocks (§CP.F2) hide authors in
        # both directions — see :data:`_READABLE_SQL` / :data:`_IN_AUDIENCE_SQL`.
        rows = await self._db.fetchall(
            f"""
            SELECT * FROM highlights
            WHERE {_READABLE_SQL} AND {_IN_AUDIENCE_SQL}
            ORDER BY highlight_date DESC, created_at DESC
            """,
            (viewer_user_id,) * 5,
        )
        return [s for s in (_row_to_highlight(d) for d in rows_to_dicts(rows)) if s]

    async def get_readable(
        self,
        highlight_id: str,
        viewer_user_id: str,
        *,
        require_audience: bool = True,
    ) -> Highlight | None:
        """``highlight_id`` iff *viewer_user_id* may read it — the same rule
        as :meth:`list_visible_to`, so the detail read can never show what
        the inbox hides. ``require_audience=False`` drops only the audience
        clause (the caller proved a share the viewer can read); expiry,
        personal blocks and guardian blocks always apply. ``None`` for an
        unknown id and a hidden one alike."""
        where = _READABLE_SQL
        params: tuple[str, ...] = (highlight_id,) + (viewer_user_id,) * 3
        if require_audience:
            where = f"{where} AND {_IN_AUDIENCE_SQL}"
            params += (viewer_user_id,) * 2
        row = await self._db.fetchone(
            f"SELECT * FROM highlights WHERE id = ? AND {where}",
            params,
        )
        return _row_to_highlight(row_to_dict(row))

    async def list_authored(
        self,
        author_user_id: str,
        *,
        limit: int = 50,
    ) -> list[Highlight]:
        rows = await self._db.fetchall(
            "SELECT * FROM highlights WHERE author_user_id=? "
            "ORDER BY highlight_date DESC, created_at DESC LIMIT ?",
            (author_user_id, int(limit)),
        )
        return [s for s in (_row_to_highlight(d) for d in rows_to_dicts(rows)) if s]

    async def list_authors_with_highlights(self) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT DISTINCT author_user_id FROM highlights",
        )
        return [r["author_user_id"] for r in rows_to_dicts(rows)]

    # ── Frames ──────────────────────────────────────────────────────────

    async def append_frame(
        self,
        *,
        highlight_id: str,
        frame_type: HighlightFrameType,
        media_url: str,
        caption_text: str | None = None,
        caption_emoji: str | None = None,
        duration_ms: int | None = None,
    ) -> HighlightFrame:
        next_seq_row = await self._db.fetchone(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_seq "
            "FROM highlight_frames WHERE highlight_id=?",
            (highlight_id,),
        )
        next_seq = int(next_seq_row["next_seq"]) if next_seq_row else 1
        frame = HighlightFrame(
            id=uuid.uuid4().hex,
            highlight_id=highlight_id,
            sequence=next_seq,
            frame_type=frame_type,
            media_url=media_url,
            caption_text=caption_text,
            caption_emoji=caption_emoji,
            duration_ms=duration_ms,
            created_at=datetime.now(UTC).isoformat(),
        )
        await self._db.enqueue(
            """
            INSERT INTO highlight_frames(
                id, highlight_id, sequence, frame_type, media_url,
                caption_text, caption_emoji, duration_ms, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                frame.id,
                frame.highlight_id,
                frame.sequence,
                frame.frame_type.value,
                frame.media_url,
                frame.caption_text,
                frame.caption_emoji,
                frame.duration_ms,
                frame.created_at,
            ),
        )
        return frame

    async def get_frame(self, frame_id: str) -> HighlightFrame | None:
        row = await self._db.fetchone(
            "SELECT * FROM highlight_frames WHERE id=?",
            (frame_id,),
        )
        return _row_to_frame(row_to_dict(row))

    async def list_frames(self, highlight_id: str) -> list[HighlightFrame]:
        rows = await self._db.fetchall(
            "SELECT * FROM highlight_frames WHERE highlight_id=? ORDER BY sequence ASC",
            (highlight_id,),
        )
        return [f for f in (_row_to_frame(d) for d in rows_to_dicts(rows)) if f]

    async def delete_frame(self, frame_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM highlight_frames WHERE id=?",
            (frame_id,),
        )

    async def delete_highlight(self, highlight_id: str) -> None:
        # Cascades to frames/views/reactions via FK.
        await self._db.enqueue(
            "DELETE FROM highlights WHERE id=?",
            (highlight_id,),
        )

    async def delete_by_author(self, author_user_id: str) -> int:
        """Hard-delete every highlight authored by ``author_user_id``.

        Drives the §Connection-Detail visibility cascade: when an
        inbound ``USER_REMOVED`` lands for this user, every highlight
        they ever published is removed from our local view. Frames /
        views / reactions cascade through the FK on ``highlights.id``.
        """
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM highlights WHERE author_user_id=?",
            (author_user_id,),
        )
        n = int(row["n"]) if row else 0
        if n:
            await self._db.enqueue(
                "DELETE FROM highlights WHERE author_user_id=?",
                (author_user_id,),
            )
        return n

    # ── Views ───────────────────────────────────────────────────────────

    async def mark_viewed(self, frame_id: str, viewer_user_id: str) -> bool:
        """Record that ``viewer_user_id`` saw ``frame_id``; ``True`` only the
        first time — a re-view is not news to anyone."""
        inserted = await self._db.enqueue_rowcount(
            """
            INSERT INTO highlight_frame_views(frame_id, viewer_user_id, viewed_at)
            VALUES(?, ?, datetime('now'))
            ON CONFLICT(frame_id, viewer_user_id) DO NOTHING
            """,
            (frame_id, viewer_user_id),
        )
        return bool(inserted)

    async def list_views_for_frame(
        self,
        frame_id: str,
    ) -> list[HighlightFrameView]:
        rows = await self._db.fetchall(
            "SELECT * FROM highlight_frame_views WHERE frame_id=? ORDER BY viewed_at ASC",
            (frame_id,),
        )
        return [
            HighlightFrameView(
                frame_id=r["frame_id"],
                viewer_user_id=r["viewer_user_id"],
                viewed_at=r["viewed_at"],
            )
            for r in rows_to_dicts(rows)
        ]

    async def count_unseen_frames(
        self,
        highlight_id: str,
        viewer_user_id: str,
    ) -> int:
        row = await self._db.fetchone(
            """
            SELECT COUNT(*) AS c
            FROM highlight_frames f
            LEFT JOIN highlight_frame_views v
              ON v.frame_id = f.id AND v.viewer_user_id = ?
            WHERE f.highlight_id = ? AND v.frame_id IS NULL
            """,
            (viewer_user_id, highlight_id),
        )
        return int(row["c"]) if row is not None else 0

    async def count_viewers(self, highlight_id: str) -> int:
        """How many people watched at least one frame — the author's
        "seen by N" (people, not frame views)."""
        row = await self._db.fetchone(
            """
            SELECT COUNT(DISTINCT v.viewer_user_id) AS c
            FROM highlight_frame_views v
            JOIN highlight_frames f ON f.id = v.frame_id
            WHERE f.highlight_id = ?
            """,
            (highlight_id,),
        )
        return int(row["c"]) if row is not None else 0

    # ── Reactions ───────────────────────────────────────────────────────

    async def set_reaction(
        self,
        frame_id: str,
        reactor_user_id: str,
        emoji: str,
    ) -> HighlightFrameReaction:
        now = datetime.now(UTC).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO highlight_frame_reactions(
                frame_id, reactor_user_id, emoji, reacted_at
            ) VALUES(?, ?, ?, ?)
            ON CONFLICT(frame_id, reactor_user_id) DO UPDATE SET
                emoji = excluded.emoji,
                reacted_at = excluded.reacted_at
            """,
            (frame_id, reactor_user_id, emoji, now),
        )
        return HighlightFrameReaction(
            frame_id=frame_id,
            reactor_user_id=reactor_user_id,
            emoji=emoji,
            reacted_at=now,
        )

    async def clear_reaction(
        self,
        frame_id: str,
        reactor_user_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM highlight_frame_reactions WHERE frame_id=? AND reactor_user_id=?",
            (frame_id, reactor_user_id),
        )

    async def reactions_by(
        self,
        reactor_user_id: str,
        frame_ids: Iterable[str],
    ) -> dict[str, str]:
        """``{frame_id: emoji}`` for the frames ``reactor_user_id`` reacted
        to — the viewer's own reaction per frame, in one read."""
        ids = list(frame_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = await self._db.fetchall(
            "SELECT frame_id, emoji FROM highlight_frame_reactions "
            f"WHERE reactor_user_id=? AND frame_id IN ({placeholders})",
            (reactor_user_id, *ids),
        )
        return {r["frame_id"]: r["emoji"] for r in rows_to_dicts(rows)}

    async def list_reactions_for_frame(
        self,
        frame_id: str,
    ) -> list[HighlightFrameReaction]:
        rows = await self._db.fetchall(
            "SELECT * FROM highlight_frame_reactions WHERE frame_id=? "
            "ORDER BY reacted_at ASC",
            (frame_id,),
        )
        return [
            HighlightFrameReaction(
                frame_id=r["frame_id"],
                reactor_user_id=r["reactor_user_id"],
                emoji=r["emoji"],
                reacted_at=r["reacted_at"],
            )
            for r in rows_to_dicts(rows)
        ]

    # ── Retention ───────────────────────────────────────────────────────

    async def _expired_ids(self) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT id FROM highlights WHERE datetime(expires_at) < datetime('now')",
        )
        return [r["id"] for r in rows_to_dicts(rows)]

    async def _over_max_ids(self, author_user_id: str, max_count: int) -> list[str]:
        if max_count <= 0:
            return []
        rows = await self._db.fetchall(
            "SELECT id FROM highlights WHERE author_user_id=? "
            "ORDER BY highlight_date DESC, created_at DESC",
            (author_user_id,),
        )
        return [r["id"] for r in rows_to_dicts(rows)][max_count:]

    async def _frame_media_urls(self, highlight_ids: list[str]) -> list[str]:
        """Frame ``media_url`` values for the given highlights (non-null)."""
        if not highlight_ids:
            return []
        placeholders = ",".join("?" * len(highlight_ids))
        rows = await self._db.fetchall(
            "SELECT media_url FROM highlight_frames "
            f"WHERE highlight_id IN ({placeholders}) AND media_url IS NOT NULL",
            tuple(highlight_ids),
        )
        return [r["media_url"] for r in rows_to_dicts(rows) if r.get("media_url")]

    async def list_expired_frame_media_urls(self) -> list[str]:
        """Frame media URLs for highlights :meth:`prune_expired` will drop.

        Collected before the prune (a superset-safe predicate match) so
        the caller can delete the backing files; the cascade ``DELETE``
        on ``highlights`` removes the frame rows.
        """
        return await self._frame_media_urls(await self._expired_ids())

    async def list_over_max_frame_media_urls(
        self,
        author_user_id: str,
        max_count: int,
    ) -> list[str]:
        """Frame media URLs for highlights :meth:`prune_over_max` will drop."""
        return await self._frame_media_urls(
            await self._over_max_ids(author_user_id, max_count)
        )

    async def prune_expired(self) -> int:
        ids = await self._expired_ids()
        for sid in ids:
            await self._db.enqueue("DELETE FROM highlights WHERE id=?", (sid,))
        return len(ids)

    async def prune_over_max(
        self,
        author_user_id: str,
        max_count: int,
    ) -> int:
        too_old = await self._over_max_ids(author_user_id, max_count)
        for sid in too_old:
            await self._db.enqueue("DELETE FROM highlights WHERE id=?", (sid,))
        return len(too_old)

    # ── Federation upserts (preserve remote ids) ────────────────────────

    async def save_highlight(self, highlight: Highlight) -> Highlight:
        await self._db.enqueue(
            """
            INSERT INTO highlights(
                id, author_user_id, highlight_date, audience_kind, audience_json,
                created_at, expires_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                audience_kind = excluded.audience_kind,
                audience_json = excluded.audience_json,
                expires_at    = excluded.expires_at
            """,
            (
                highlight.id,
                highlight.author_user_id,
                highlight.highlight_date,
                highlight.audience_kind.value,
                dump_json(list(highlight.audience)),
                highlight.created_at or datetime.now(UTC).isoformat(),
                highlight.expires_at,
            ),
        )
        return highlight

    async def save_frame(self, frame: HighlightFrame) -> HighlightFrame:
        await self._db.enqueue(
            """
            INSERT INTO highlight_frames(
                id, highlight_id, sequence, frame_type, media_url,
                caption_text, caption_emoji, duration_ms, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                sequence      = excluded.sequence,
                frame_type    = excluded.frame_type,
                media_url     = excluded.media_url,
                caption_text  = excluded.caption_text,
                caption_emoji = excluded.caption_emoji,
                duration_ms   = excluded.duration_ms
            """,
            (
                frame.id,
                frame.highlight_id,
                frame.sequence,
                frame.frame_type.value,
                frame.media_url,
                frame.caption_text,
                frame.caption_emoji,
                frame.duration_ms,
                frame.created_at or datetime.now(UTC).isoformat(),
            ),
        )
        return frame

    # ── Public publication (§highlights_public) ─────────────────────────────

    async def mark_published(
        self,
        highlight_id: str,
        *,
        gfs_id: str,
        published_at: str,
    ) -> None:
        """Record that ``highlight_id`` has been published via ``gfs_id``.

        Idempotent re-publish — overwrites the timestamp + gfs_id when
        the same highlight is published a second time (e.g. switched to a
        different GFS).
        """
        await self._db.enqueue(
            "UPDATE highlights SET public_gfs_id=?, public_published_at=? WHERE id=?",
            (gfs_id, published_at, highlight_id),
        )

    async def mark_unpublished(self, highlight_id: str) -> None:
        """Clear the publication state and forget every share token of it.
        Caller is responsible for the GFS-side DELETE — this method only
        flips the local state."""
        await self._db.enqueue(
            "DELETE FROM highlight_public_tokens WHERE highlight_id=?",
            (highlight_id,),
        )
        await self._db.enqueue(
            "UPDATE highlights SET public_gfs_id=NULL, public_published_at=NULL "
            "WHERE id=?",
            (highlight_id,),
        )

    async def add_public_token(
        self, highlight_id: str, *, gfs_id: str, token_hash: str
    ) -> None:
        """Remember a share token this household minted (its SHA-256)."""
        await self._db.enqueue(
            "INSERT OR IGNORE INTO highlight_public_tokens"
            "(token_hash, highlight_id, gfs_id) VALUES(?,?,?)",
            (token_hash, highlight_id, gfs_id),
        )

    async def remove_public_token(self, highlight_id: str, token_hash: str) -> None:
        await self._db.enqueue(
            "DELETE FROM highlight_public_tokens WHERE token_hash=? AND highlight_id=?",
            (token_hash, highlight_id),
        )

    async def is_live_public_token(self, highlight_id: str, token_hash: str) -> bool:
        """Whether *token_hash* is a live share token of *highlight_id*: one
        this household minted on the GFS the highlight is published on now,
        not revoked, and the highlight not past its expiry (the retention
        scheduler may not have pruned it yet)."""
        row = await self._db.fetchone(
            "SELECT 1 FROM highlight_public_tokens t "
            "JOIN highlights h ON h.id = t.highlight_id "
            "WHERE t.token_hash=? AND t.highlight_id=? "
            "AND h.public_gfs_id = t.gfs_id "
            "AND datetime(h.expires_at) > datetime('now')",
            (token_hash, highlight_id),
        )
        return row is not None

    async def list_published_for(self, author_user_id: str) -> list[Highlight]:
        rows = await self._db.fetchall(
            "SELECT * FROM highlights "
            "WHERE author_user_id=? AND public_gfs_id IS NOT NULL "
            "ORDER BY public_published_at DESC",
            (author_user_id,),
        )
        return [s for s in (_row_to_highlight(d) for d in rows_to_dicts(rows)) if s]


def _row_to_highlight(row: dict | None) -> Highlight | None:
    if row is None:
        return None
    audience_raw = load_json(row.get("audience_json"), [])
    if not isinstance(audience_raw, list):
        audience_raw = []
    audience: tuple[str, ...] = tuple(str(x) for x in audience_raw)
    try:
        kind = HighlightAudience(row.get("audience_kind") or "all_paired")
    except ValueError:
        kind = HighlightAudience.ALL_PAIRED
    return Highlight(
        id=row["id"],
        author_user_id=row["author_user_id"],
        highlight_date=row["highlight_date"],
        audience_kind=kind,
        audience=audience,
        created_at=row.get("created_at"),
        expires_at=row.get("expires_at"),
        public_gfs_id=row.get("public_gfs_id"),
        public_published_at=row.get("public_published_at"),
    )


def _row_to_frame(row: dict | None) -> HighlightFrame | None:
    if row is None:
        return None
    try:
        ftype = HighlightFrameType(row["frame_type"])
    except KeyError, ValueError:
        return None
    return HighlightFrame(
        id=row["id"],
        highlight_id=row["highlight_id"],
        sequence=int(row["sequence"]),
        frame_type=ftype,
        media_url=row["media_url"],
        caption_text=row.get("caption_text"),
        caption_emoji=row.get("caption_emoji"),
        duration_ms=(
            int(row["duration_ms"]) if row.get("duration_ms") is not None else None
        ),
        created_at=row.get("created_at"),
    )


__all__ = ["AbstractHighlightRepo", "SqliteHighlightRepo"]
