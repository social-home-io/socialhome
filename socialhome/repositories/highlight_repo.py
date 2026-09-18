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
from datetime import datetime, timezone
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
    async def list_authored(
        self,
        author_user_id: str,
        *,
        limit: int = 50,
    ) -> builtins.list[Highlight]: ...
    async def mark_viewed(self, frame_id: str, viewer_user_id: str) -> None: ...
    async def list_views_for_frame(
        self,
        frame_id: str,
    ) -> builtins.list[HighlightFrameView]: ...
    async def count_unseen_frames(
        self,
        highlight_id: str,
        viewer_user_id: str,
    ) -> int: ...
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

    # Public publication state (§highlights_public). The token registry
    # lives on GFS — SH only knows whether *some* publication exists.
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
            created_at=datetime.now(timezone.utc).isoformat(),
            expires_at=expires_at,
        )
        await self._db.enqueue(
            """
            INSERT INTO highlights(
                id, author_user_id, highlight_date, audience_kind, audience_json,
                created_at, expires_at
            ) VALUES(?,?,?,?,?,?,?)
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
        return highlight

    async def get_highlight(self, highlight_id: str) -> Highlight | None:
        row = await self._db.fetchone(
            "SELECT * FROM highlights WHERE id=?",
            (highlight_id,),
        )
        return _row_to_highlight(row_to_dict(row))

    async def list_visible_to(self, viewer_user_id: str) -> list[Highlight]:
        # Visible if:
        #   - author is local OR audience_kind in ('all_paired','households'),
        #   - or audience_kind='users' AND viewer in the user allow-list.
        # The receiver dropped envelopes outside the audience already, so
        # by the time we read locally we trust ``highlights`` rows. The
        # ``USERS``-kind filter still applies because one row can be
        # surfaced to a strict subset of an instance's users.
        # Personal blocks are applied here (§Privacy): authors the viewer
        # has blocked never surface in their inbox, even if the audience
        # would otherwise admit them. Block list stays local — see
        # ``user_blocks`` table.
        rows = await self._db.fetchall(
            """
            SELECT * FROM highlights
            WHERE datetime(expires_at) > datetime('now')
              AND author_user_id NOT IN (
                  SELECT blocked_user_id FROM user_blocks
                  WHERE blocker_user_id = ?
              )
              AND (
                  audience_kind IN ('all_paired','households')
                  OR (audience_kind = 'users' AND EXISTS (
                      SELECT 1 FROM json_each(highlights.audience_json) j
                      WHERE j.value = ?
                  ))
                  OR author_user_id = ?
              )
            ORDER BY highlight_date DESC, created_at DESC
            """,
            (viewer_user_id, viewer_user_id, viewer_user_id),
        )
        return [s for s in (_row_to_highlight(d) for d in rows_to_dicts(rows)) if s]

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
            created_at=datetime.now(timezone.utc).isoformat(),
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

    async def mark_viewed(self, frame_id: str, viewer_user_id: str) -> None:
        await self._db.enqueue(
            """
            INSERT INTO highlight_frame_views(frame_id, viewer_user_id, viewed_at)
            VALUES(?, ?, datetime('now'))
            ON CONFLICT(frame_id, viewer_user_id) DO NOTHING
            """,
            (frame_id, viewer_user_id),
        )

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

    # ── Reactions ───────────────────────────────────────────────────────

    async def set_reaction(
        self,
        frame_id: str,
        reactor_user_id: str,
        emoji: str,
    ) -> HighlightFrameReaction:
        now = datetime.now(timezone.utc).isoformat()
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
                highlight.created_at or datetime.now(timezone.utc).isoformat(),
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
                frame.created_at or datetime.now(timezone.utc).isoformat(),
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
        """Clear the publication state. Caller is responsible for the
        GFS-side DELETE — this method only flips the local flag."""
        await self._db.enqueue(
            "UPDATE highlights SET public_gfs_id=NULL, public_published_at=NULL "
            "WHERE id=?",
            (highlight_id,),
        )

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
