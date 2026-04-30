"""Post repository — persistence for the household feed (``feed_posts``)
plus its comment tree and personal "saved" (bookmark) list.

Scope for v1:

* :class:`SqlitePostRepo` covers ``feed_posts``, ``post_comments`` and
  ``saved_posts``.
* Reactions live inline in ``feed_posts.reactions`` as JSON
  (``{emoji: [user_id, …]}``) — the add/remove helpers read-modify-write
  inside :meth:`AsyncDatabase.transact` so two concurrent reactors never
  overwrite each other.
* Comment trees are fetched via a single ``WHERE post_id=?`` query; nesting
  is rebuilt client-side by the service layer for display.
* Comment counts on the parent post are maintained by explicit
  ``increment_comment_count`` / ``decrement_comment_count`` calls from
  :class:`FeedService` — the repo does NOT do implicit bookkeeping.

Space-feed (``space_posts`` / ``space_post_comments``) will live in a
sibling module once the space services come online; the SQL shape is a
near-copy of the household tables, so the row-mapping helpers here can be
reused by that module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..utils.datetime import parse_iso8601_optional
from ..domain.post import (
    Comment,
    CommentType,
    FileMeta,
    LocationData,
    MAX_DISTINCT_REACTIONS_PER_POST,
    Post,
    PostType,
)
from ._spec import Spec, spec_to_sql
from .base import bool_col, dump_json, load_json, row_to_dict, rows_to_dicts


#: Columns the Specification pattern is allowed to filter / order on
#: when ``find()`` is called against ``feed_posts``.
_FEED_POST_COLS: frozenset[str] = frozenset(
    {
        "id",
        "author",
        "type",
        "content",
        "media_url",
        "comment_count",
        "pinned",
        "deleted",
        "edited_at",
        "no_link_preview",
        "moderated",
        "created_at",
    }
)


@runtime_checkable
class AbstractPostRepo(Protocol):
    # Posts ---------------------------------------------------------------
    async def save(self, post: Post) -> Post: ...
    async def get(self, post_id: str) -> Post | None: ...
    async def list_feed(
        self,
        *,
        before: str | None = None,
        limit: int = 20,
    ) -> list[Post]: ...
    async def find(self, spec: Spec) -> list[Post]: ...
    async def soft_delete(self, post_id: str) -> None: ...
    async def edit(self, post_id: str, new_content: str) -> None: ...

    # Reactions -----------------------------------------------------------
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

    # Comment counters (maintained by FeedService on comment add/remove) --
    async def increment_comment_count(self, post_id: str) -> None: ...
    async def decrement_comment_count(self, post_id: str) -> None: ...

    # Comments ------------------------------------------------------------
    async def add_comment(self, comment: Comment) -> Comment: ...
    async def get_comment(self, comment_id: str) -> Comment | None: ...
    async def list_comments(self, post_id: str) -> list[Comment]: ...
    async def soft_delete_comment(self, comment_id: str) -> None: ...
    async def edit_comment(
        self,
        comment_id: str,
        new_content: str,
    ) -> None: ...

    # Saved / bookmarks ---------------------------------------------------
    async def save_bookmark(self, user_id: str, post_id: str) -> None: ...
    async def unsave_bookmark(self, user_id: str, post_id: str) -> None: ...
    async def list_bookmarks(self, user_id: str) -> list[Post]: ...

    # Feed read watermark -------------------------------------------------
    async def set_read_watermark(
        self, user_id: str, last_read_post_id: str | None
    ) -> None: ...
    async def get_read_watermark(self, user_id: str) -> dict | None: ...


# ─── Concrete SQLite implementation ───────────────────────────────────────


class SqlitePostRepo:
    """SQLite-backed :class:`AbstractPostRepo` for the household feed."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Posts ──────────────────────────────────────────────────────────

    async def save(self, post: Post) -> Post:
        await self._db.enqueue(
            """
            INSERT INTO feed_posts(
                id, author, type, content, media_url, reactions,
                comment_count, pinned, deleted, edited_at, no_link_preview,
                moderated, file_meta_json, location_json, image_urls_json,
                created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')))
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
                file_meta_json=excluded.file_meta_json,
                location_json=excluded.location_json,
                image_urls_json=excluded.image_urls_json
            """,
            (
                post.id,
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
                _encode_location(post.location),
                _encode_image_urls(post.image_urls),
                _iso_or_none(post.created_at),
            ),
        )
        return post

    async def get(self, post_id: str) -> Post | None:
        row = await self._db.fetchone(
            "SELECT * FROM feed_posts WHERE id=?",
            (post_id,),
        )
        return _row_to_post(row_to_dict(row))

    async def list_feed(
        self,
        *,
        before: str | None = None,
        limit: int = 20,
    ) -> list[Post]:
        """Return posts ordered by ``created_at DESC``.

        ``before`` is an ISO-8601 timestamp used for keyset pagination; if
        provided, only posts strictly older are returned. ``limit`` is
        passed through as-is; callers are expected to clamp it upstream
        (route handlers use ``min(max(int(…), 1), 50)`` per §5.2 spec).

        The bespoke shape stays for the common case; arbitrary filter
        combinations should use :meth:`find` with a :class:`Spec`.
        """
        where: list[tuple[str, str, object]] = [("deleted", "=", 0)]
        if before is not None:
            where.append(("created_at", "<", before))
        spec = Spec(
            where=where,
            order_by=[("created_at", "DESC")],
            limit=int(limit),
        )
        return await self.find(spec)

    async def find(self, spec: Spec) -> list[Post]:
        """Specification-pattern entry point for ``feed_posts`` reads.

        See :mod:`socialhome.repositories._spec`. Every column named
        in ``spec.where`` / ``spec.order_by`` is gated against the
        :data:`_FEED_POST_COLS` allow-list before being interpolated
        into the SQL.
        """
        clause, params = spec_to_sql(
            spec,
            table="feed_posts",
            allowed_cols=_FEED_POST_COLS,
        )
        sql = "SELECT * FROM feed_posts " + clause
        rows = await self._db.fetchall(sql, params)
        return [p for p in (_row_to_post(d) for d in rows_to_dicts(rows)) if p]

    async def soft_delete(self, post_id: str) -> None:
        await self._db.enqueue(
            """
            UPDATE feed_posts
               SET deleted=1, content=NULL, media_url=NULL
             WHERE id=?
            """,
            (post_id,),
        )

    async def edit(self, post_id: str, new_content: str) -> None:
        await self._db.enqueue(
            "UPDATE feed_posts SET content=?, edited_at=datetime('now') WHERE id=?",
            (new_content, post_id),
        )

    # ── Reactions (inline JSON, atomic) ────────────────────────────────

    async def add_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
    ) -> Post:
        """Atomically add ``user_id`` to ``reactions[emoji]`` on the post.

        Raises :class:`KeyError` if the post does not exist or is deleted.
        Raises :class:`ValueError` if adding this emoji would exceed
        :data:`MAX_DISTINCT_REACTIONS_PER_POST`.
        """

        def _run(conn):
            row = conn.execute(
                "SELECT * FROM feed_posts WHERE id=? AND deleted=0",
                (post_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"post {post_id!r} not found or deleted")
            row_dict = {k: row[k] for k in row.keys()}
            reactions = _decode_reactions(row_dict["reactions"])
            if (
                emoji not in reactions
                and len(reactions) >= MAX_DISTINCT_REACTIONS_PER_POST
            ):
                raise ValueError("too many distinct reactions on this post")
            reactions.setdefault(emoji, set()).add(user_id)
            conn.execute(
                "UPDATE feed_posts SET reactions=? WHERE id=?",
                (_encode_reactions(_to_frozenset(reactions)), post_id),
            )
            row = conn.execute(
                "SELECT * FROM feed_posts WHERE id=?",
                (post_id,),
            ).fetchone()
            return {k: row[k] for k in row.keys()}

        row = await self._db.transact(_run)
        return _row_to_post(row)  # type: ignore[return-value]

    async def remove_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
    ) -> Post:
        """Atomically remove ``user_id`` from ``reactions[emoji]``.

        A no-op (returns the current post) when ``user_id`` was not in the
        bucket, or the emoji key was missing. If the bucket becomes empty
        the emoji key is dropped so UI consumers can use the presence of a
        key as a "anyone reacted?" signal.
        """

        def _run(conn):
            row = conn.execute(
                "SELECT * FROM feed_posts WHERE id=?",
                (post_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"post {post_id!r} not found")
            row_dict = {k: row[k] for k in row.keys()}
            reactions = _decode_reactions(row_dict["reactions"])
            bucket = reactions.get(emoji)
            if bucket and user_id in bucket:
                bucket.discard(user_id)
                if not bucket:
                    reactions.pop(emoji, None)
                conn.execute(
                    "UPDATE feed_posts SET reactions=? WHERE id=?",
                    (_encode_reactions(_to_frozenset(reactions)), post_id),
                )
                row = conn.execute(
                    "SELECT * FROM feed_posts WHERE id=?",
                    (post_id,),
                ).fetchone()
            return {k: row[k] for k in row.keys()}

        row = await self._db.transact(_run)
        return _row_to_post(row)  # type: ignore[return-value]

    # ── Comment counters ───────────────────────────────────────────────

    async def increment_comment_count(self, post_id: str) -> None:
        await self._db.enqueue(
            "UPDATE feed_posts SET comment_count = comment_count + 1 WHERE id=?",
            (post_id,),
        )

    async def decrement_comment_count(self, post_id: str) -> None:
        await self._db.enqueue(
            "UPDATE feed_posts "
            "SET comment_count = MAX(0, comment_count - 1) WHERE id=?",
            (post_id,),
        )

    # ── Comments ───────────────────────────────────────────────────────

    async def add_comment(self, comment: Comment) -> Comment:
        await self._db.enqueue(
            """
            INSERT INTO post_comments(
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
            "SELECT * FROM post_comments WHERE id=?",
            (comment_id,),
        )
        return _row_to_comment(row_to_dict(row))

    async def list_comments(self, post_id: str) -> list[Comment]:
        """Flat list of comments for ``post_id``, oldest first.

        The service layer re-assembles the tree by ``parent_id`` so this
        method stays cheap and side-effect-free.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM post_comments WHERE post_id=? ORDER BY created_at",
            (post_id,),
        )
        return [c for c in (_row_to_comment(d) for d in rows_to_dicts(rows)) if c]

    async def soft_delete_comment(self, comment_id: str) -> None:
        await self._db.enqueue(
            """
            UPDATE post_comments
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
            UPDATE post_comments
               SET content=?, edited_at=datetime('now')
             WHERE id=? AND deleted=0
            """,
            (new_content, comment_id),
        )

    # ── Saved / bookmarks ──────────────────────────────────────────────

    async def save_bookmark(self, user_id: str, post_id: str) -> None:
        await self._db.enqueue(
            "INSERT OR IGNORE INTO saved_posts(user_id, post_id) VALUES(?, ?)",
            (user_id, post_id),
        )

    async def unsave_bookmark(self, user_id: str, post_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM saved_posts WHERE user_id=? AND post_id=?",
            (user_id, post_id),
        )

    async def list_bookmarks(self, user_id: str) -> list[Post]:
        rows = await self._db.fetchall(
            """
            SELECT p.* FROM feed_posts p
              JOIN saved_posts s ON s.post_id = p.id
             WHERE s.user_id = ? AND p.deleted = 0
             ORDER BY s.saved_at DESC
            """,
            (user_id,),
        )
        return [p for p in (_row_to_post(d) for d in rows_to_dicts(rows)) if p]

    # ── Feed read watermark ────────────────────────────────────────────
    #
    # Per-user scroll-restoration pointer (§23.17.1). A single row per
    # user; upsert every time the client reports a new top-most read
    # post id. ``last_read_post_id`` may be ``NULL`` to record "user
    # has scrolled but not yet read any specific post" — rare in
    # practice but permitted by the schema.

    async def set_read_watermark(
        self, user_id: str, last_read_post_id: str | None
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO feed_read_positions(user_id, last_read_post_id, last_read_at)
            VALUES(?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                last_read_post_id=excluded.last_read_post_id,
                last_read_at=datetime('now')
            """,
            (user_id, last_read_post_id),
        )

    async def get_read_watermark(self, user_id: str) -> dict | None:
        row = await self._db.fetchone(
            "SELECT last_read_post_id, last_read_at FROM feed_read_positions"
            " WHERE user_id=?",
            (user_id,),
        )
        if row is None:
            return None
        return {
            "last_read_post_id": row["last_read_post_id"],
            "last_read_at": row["last_read_at"],
        }


# ─── Helpers ──────────────────────────────────────────────────────────────


def _iso_or_none(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _encode_reactions(reactions) -> str:
    """Serialise ``{emoji: frozenset | set | list}`` → JSON ``{emoji: [users…]}``."""
    plain: dict[str, list[str]] = {}
    for emoji, users in (reactions or {}).items():
        plain[emoji] = sorted(users)
    return dump_json(plain)


def _decode_reactions(raw: str | None) -> dict[str, set[str]]:
    obj = load_json(raw, {}) or {}
    return {k: set(v or []) for k, v in obj.items()}


def _to_frozenset(d: dict[str, set[str]]) -> dict[str, frozenset[str]]:
    return {k: frozenset(v) for k, v in d.items()}


def _encode_file_meta(meta: FileMeta | None) -> str | None:
    if meta is None:
        return None
    return dump_json(
        {
            "url": meta.url,
            "mime_type": meta.mime_type,
            "original_name": meta.original_name,
            "size_bytes": meta.size_bytes,
        }
    )


def _decode_file_meta(raw: str | None) -> FileMeta | None:
    obj = load_json(raw, None)
    if not obj:
        return None
    return FileMeta(
        url=obj["url"],
        mime_type=obj["mime_type"],
        original_name=obj["original_name"],
        size_bytes=int(obj["size_bytes"]),
    )


def _encode_location(loc: LocationData | None) -> str | None:
    if loc is None:
        return None
    payload: dict[str, Any] = {"lat": float(loc.lat), "lon": float(loc.lon)}
    if loc.label is not None:
        payload["label"] = loc.label
    return dump_json(payload)


def _decode_location(raw: str | None) -> LocationData | None:
    obj = load_json(raw, None)
    if not obj:
        return None
    return LocationData(
        lat=float(obj["lat"]),
        lon=float(obj["lon"]),
        label=obj.get("label"),
    )


def _encode_image_urls(urls: tuple[str, ...]) -> str | None:
    """Serialise a multi-image post's URL list. ``None`` for an empty
    list so the column reads as NULL on non-image posts (slightly
    cheaper than ``'[]'`` and matches existing convention)."""
    if not urls:
        return None
    return dump_json(list(urls))


def _decode_image_urls(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(load_json(raw, []))


def _row_to_post(row: dict | None) -> Post | None:
    if row is None:
        return None
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
        image_urls=_decode_image_urls(row.get("image_urls_json")),
        reactions=reactions,
        comment_count=int(row.get("comment_count") or 0),
        pinned=bool_col(row.get("pinned", 0)),
        deleted=bool_col(row.get("deleted", 0)),
        edited_at=parse_iso8601_optional(row.get("edited_at")),
        no_link_preview=bool_col(row.get("no_link_preview", 0)),
        moderated=bool_col(row.get("moderated", 0)),
        file_meta=_decode_file_meta(row.get("file_meta_json")),
        location=_decode_location(row.get("location_json")),
    )


def _row_to_comment(row: dict | None) -> Comment | None:
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
