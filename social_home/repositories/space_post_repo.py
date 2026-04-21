"""Space post repository — the space-feed analogue of :mod:`post_repo`.

Operates on ``space_posts`` and ``space_post_comments``. The schema of
those tables is near-identical to the household ones, so the row-mapping
and JSON helpers are imported from :mod:`post_repo` rather than
duplicated.

Scope for v1:

* :class:`SqliteSpacePostRepo` covers save / get / list_feed (scoped by
  ``space_id``) / soft_delete / edit / reactions (atomic, with cap) /
  comment counters / comment CRUD.
* Does NOT cover space polls, schedule polls, moderation queue, or
  pinning — those ride on separate tables and will land in follow-up
  repos as the space services come online.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.post import (
    Comment,
    CommentType,
    MAX_DISTINCT_REACTIONS_PER_POST,
    Post,
    PostType,
)
from ..utils.datetime import parse_iso8601_optional
from .base import bool_col, row_to_dict, rows_to_dicts
from .post_repo import (  # reuse the household post helpers verbatim
    _decode_reactions,
    _encode_reactions,
    _encode_file_meta,
    _decode_file_meta,
    _iso_or_none,
    _to_frozenset,
)


@runtime_checkable
class AbstractSpacePostRepo(Protocol):
    async def save(self, space_id: str, post: Post) -> Post: ...
    async def get(self, post_id: str) -> tuple[str, Post] | None: ...
    async def list_feed(
        self,
        space_id: str,
        *,
        before: str | None = None,
        limit: int = 20,
    ) -> list[Post]: ...
    async def soft_delete(
        self, post_id: str, *, moderated_by: str | None = None
    ) -> None: ...
    async def edit(self, post_id: str, new_content: str) -> None: ...

    async def add_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
    ) -> Post: ...
    async def remove_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
    ) -> Post: ...

    async def increment_comment_count(self, post_id: str) -> None: ...
    async def decrement_comment_count(self, post_id: str) -> None: ...

    async def add_comment(self, comment: Comment) -> Comment: ...
    async def get_comment(self, comment_id: str) -> Comment | None: ...
    async def list_comments(self, post_id: str) -> list[Comment]: ...
    async def soft_delete_comment(self, comment_id: str) -> None: ...
    async def edit_comment(
        self,
        comment_id: str,
        new_content: str,
    ) -> None: ...


class SqliteSpacePostRepo:
    """SQLite-backed :class:`AbstractSpacePostRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Posts ──────────────────────────────────────────────────────────

    async def save(self, space_id: str, post: Post) -> Post:
        await self._db.enqueue(
            """
            INSERT INTO space_posts(
                id, space_id, author, type, content, media_url, reactions,
                comment_count, pinned, deleted, edited_at, no_link_preview,
                moderated, file_meta_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')))
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                media_url=excluded.media_url,
                reactions=excluded.reactions,
                comment_count=excluded.comment_count,
                pinned=excluded.pinned,
                deleted=excluded.deleted,
                edited_at=excluded.edited_at,
                no_link_preview=excluded.no_link_preview,
                moderated=excluded.moderated,
                file_meta_json=excluded.file_meta_json
            """,
            (
                post.id,
                space_id,
                post.author,
                post.type.value,
                post.content,
                post.media_url,
                _encode_reactions(post.reactions),
                int(post.comment_count),
                int(post.pinned),
                int(post.deleted),
                _iso_or_none(post.edited_at),
                int(post.no_link_preview),
                int(post.moderated),
                _encode_file_meta(post.file_meta),
                _iso_or_none(post.created_at),
            ),
        )
        return post

    async def get(self, post_id: str) -> tuple[str, Post] | None:
        """Return ``(space_id, post)`` — space id lives only on the row."""
        row = await self._db.fetchone(
            "SELECT * FROM space_posts WHERE id=?",
            (post_id,),
        )
        d = row_to_dict(row)
        if d is None:
            return None
        return d["space_id"], _row_to_space_post(d)

    async def list_feed(
        self,
        space_id: str,
        *,
        before: str | None = None,
        limit: int = 20,
    ) -> list[Post]:
        if before is None:
            rows = await self._db.fetchall(
                "SELECT * FROM space_posts "
                "WHERE space_id=? AND deleted=0 "
                "ORDER BY created_at DESC LIMIT ?",
                (space_id, int(limit)),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM space_posts "
                "WHERE space_id=? AND deleted=0 AND created_at < ? "
                "ORDER BY created_at DESC LIMIT ?",
                (space_id, before, int(limit)),
            )
        return [_row_to_space_post(d) for d in rows_to_dicts(rows)]

    async def soft_delete(
        self,
        post_id: str,
        *,
        moderated_by: str | None = None,
    ) -> None:
        """Soft-delete a space post. ``moderated_by`` sets the
        ``moderated=1`` flag so the service layer can distinguish a
        self-delete from an admin removal (§5.2 moderation).
        """
        await self._db.enqueue(
            """
            UPDATE space_posts
               SET deleted=1, content=NULL, media_url=NULL,
                   moderated=CASE WHEN ? IS NOT NULL THEN 1 ELSE moderated END
             WHERE id=?
            """,
            (moderated_by, post_id),
        )

    async def edit(self, post_id: str, new_content: str) -> None:
        await self._db.enqueue(
            "UPDATE space_posts SET content=?, edited_at=datetime('now') WHERE id=?",
            (new_content, post_id),
        )

    # ── Reactions (atomic) ─────────────────────────────────────────────

    async def add_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
    ) -> Post:
        def _run(conn):
            row = conn.execute(
                "SELECT * FROM space_posts WHERE id=? AND deleted=0",
                (post_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"space post {post_id!r} not found or deleted")
            row_dict = {k: row[k] for k in row.keys()}
            reactions = _decode_reactions(row_dict["reactions"])
            if (
                emoji not in reactions
                and len(reactions) >= MAX_DISTINCT_REACTIONS_PER_POST
            ):
                raise ValueError("too many distinct reactions on this post")
            reactions.setdefault(emoji, set()).add(user_id)
            conn.execute(
                "UPDATE space_posts SET reactions=? WHERE id=?",
                (_encode_reactions(_to_frozenset(reactions)), post_id),
            )
            row = conn.execute(
                "SELECT * FROM space_posts WHERE id=?",
                (post_id,),
            ).fetchone()
            return {k: row[k] for k in row.keys()}

        row = await self._db.transact(_run)
        return _row_to_space_post(row)

    async def remove_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
    ) -> Post:
        def _run(conn):
            row = conn.execute(
                "SELECT * FROM space_posts WHERE id=?",
                (post_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"space post {post_id!r} not found")
            row_dict = {k: row[k] for k in row.keys()}
            reactions = _decode_reactions(row_dict["reactions"])
            bucket = reactions.get(emoji)
            if bucket and user_id in bucket:
                bucket.discard(user_id)
                if not bucket:
                    reactions.pop(emoji, None)
                conn.execute(
                    "UPDATE space_posts SET reactions=? WHERE id=?",
                    (_encode_reactions(_to_frozenset(reactions)), post_id),
                )
                row = conn.execute(
                    "SELECT * FROM space_posts WHERE id=?",
                    (post_id,),
                ).fetchone()
            return {k: row[k] for k in row.keys()}

        row = await self._db.transact(_run)
        return _row_to_space_post(row)

    # ── Comment counters ───────────────────────────────────────────────

    async def increment_comment_count(self, post_id: str) -> None:
        await self._db.enqueue(
            "UPDATE space_posts SET comment_count = comment_count + 1 WHERE id=?",
            (post_id,),
        )

    async def decrement_comment_count(self, post_id: str) -> None:
        await self._db.enqueue(
            "UPDATE space_posts "
            "SET comment_count = MAX(0, comment_count - 1) WHERE id=?",
            (post_id,),
        )

    # ── Comments ───────────────────────────────────────────────────────

    async def add_comment(self, comment: Comment) -> Comment:
        await self._db.enqueue(
            """
            INSERT INTO space_post_comments(
                id, post_id, parent_id, author, type, content, media_url,
                deleted, created_at
            ) VALUES(?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')))
            """,
            (
                comment.id,
                comment.post_id,
                comment.parent_id,
                comment.author,
                comment.type.value,
                comment.content,
                comment.media_url,
                int(comment.deleted),
                _iso_or_none(comment.created_at),
            ),
        )
        return comment

    async def get_comment(self, comment_id: str) -> Comment | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_post_comments WHERE id=?",
            (comment_id,),
        )
        return _row_to_space_comment(row_to_dict(row))

    async def list_comments(self, post_id: str) -> list[Comment]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_post_comments WHERE post_id=? ORDER BY created_at",
            (post_id,),
        )
        return [c for c in (_row_to_space_comment(d) for d in rows_to_dicts(rows)) if c]

    async def soft_delete_comment(self, comment_id: str) -> None:
        await self._db.enqueue(
            """
            UPDATE space_post_comments
               SET deleted=1, content=NULL, media_url=NULL
             WHERE id=?
            """,
            (comment_id,),
        )

    async def edit_comment(
        self,
        comment_id: str,
        new_content: str,
    ) -> None:
        await self._db.enqueue(
            """
            UPDATE space_post_comments
               SET content=?, edited_at=datetime('now')
             WHERE id=? AND deleted=0
            """,
            (new_content, comment_id),
        )


# ─── Row → domain ─────────────────────────────────────────────────────────


def _row_to_space_post(row: dict) -> Post:
    reactions = {
        k: frozenset(v) for k, v in _decode_reactions(row.get("reactions")).items()
    }
    return Post(
        id=row["id"],
        author=row["author"],
        type=PostType(row["type"]),
        created_at=parse_iso8601_optional(row.get("created_at"))
        or datetime.now(timezone.utc),
        content=row.get("content"),
        media_url=row.get("media_url"),
        reactions=reactions,
        comment_count=int(row.get("comment_count") or 0),
        pinned=bool_col(row.get("pinned", 0)),
        deleted=bool_col(row.get("deleted", 0)),
        edited_at=parse_iso8601_optional(row.get("edited_at")),
        no_link_preview=bool_col(row.get("no_link_preview", 0)),
        moderated=bool_col(row.get("moderated", 0)),
        file_meta=_decode_file_meta(row.get("file_meta_json")),
    )


def _row_to_space_comment(row: dict | None) -> Comment | None:
    if row is None:
        return None
    return Comment(
        id=row["id"],
        post_id=row["post_id"],
        author=row["author"],
        type=CommentType(row.get("type", "text")),
        created_at=parse_iso8601_optional(row.get("created_at"))
        or datetime.now(timezone.utc),
        parent_id=row.get("parent_id"),
        content=row.get("content"),
        media_url=row.get("media_url"),
        deleted=bool_col(row.get("deleted", 0)),
        edited_at=parse_iso8601_optional(row.get("edited_at")),
    )
