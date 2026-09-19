"""Space post repository — the space-feed analogue of :mod:`post_repo`.

Operates on ``space_posts`` and ``space_post_comments``. The schema of
those tables is near-identical to the household ones, so the row-mapping
and JSON helpers are imported from :mod:`post_repo` rather than
duplicated.

Scope:

* :class:`SqliteSpacePostRepo` covers save / get / list_feed (scoped by
  ``space_id``) / soft_delete / edit / reactions (atomic, with cap) /
  comment counters / comment CRUD / pinning.
* Polls live in :mod:`space_poll_repo` and :mod:`poll_repo`; moderation
  reports live in :mod:`report_repo`.
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
    _encode_location,
    _decode_location,
    _encode_image_urls,
    _decode_image_urls,
    _iso_or_none,
    _to_frozenset,
)


@runtime_checkable
class AbstractSpacePostRepo(Protocol):
    async def save(self, space_id: str, post: Post) -> Post | None: ...
    async def get(self, post_id: str) -> tuple[str, Post] | None: ...
    async def get_by_linked_event_id(
        self,
        event_id: str,
    ) -> tuple[str, Post] | None: ...
    async def list_feed(
        self,
        space_id: str,
        *,
        before: str | None = None,
        limit: int = 20,
    ) -> list[Post]: ...
    async def list_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[Post]: ...
    async def soft_delete(
        self,
        post_id: str,
        *,
        space_id: str,
        moderated_by: str | None = None,
    ) -> bool: ...
    async def edit(
        self,
        post_id: str,
        new_content: str,
        *,
        space_id: str,
    ) -> bool: ...

    async def add_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
        *,
        space_id: str,
    ) -> Post: ...
    async def remove_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
        *,
        space_id: str,
    ) -> Post: ...

    async def increment_comment_count(
        self,
        post_id: str,
        *,
        space_id: str,
    ) -> bool: ...
    async def decrement_comment_count(
        self,
        post_id: str,
        *,
        space_id: str,
    ) -> bool: ...

    async def list_space_media_urls(self, space_id: str) -> list[str]: ...
    async def add_comment(self, comment: Comment, *, space_id: str) -> bool: ...
    async def get_comment(self, comment_id: str) -> Comment | None: ...
    async def list_comments(self, post_id: str) -> list[Comment]: ...
    async def list_comments_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[tuple[str, Comment]]: ...
    async def soft_delete_comment(
        self,
        comment_id: str,
        *,
        space_id: str,
    ) -> bool: ...
    async def edit_comment(
        self,
        comment_id: str,
        new_content: str,
        *,
        space_id: str,
    ) -> bool: ...


class SqliteSpacePostRepo:
    """SQLite-backed :class:`AbstractSpacePostRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Posts ──────────────────────────────────────────────────────────

    async def save(self, space_id: str, post: Post) -> Post | None:
        """Insert or update a space post. ``None`` = refused (#693).

        The ``DO UPDATE`` never touches ``space_id`` and is gated on
        ``space_posts.space_id = excluded.space_id``, so an id already
        owned by another space cannot be hijacked by a sender gated for
        this one: SQLite reports ``rowcount == 0`` for the skipped
        conflict resolution and the caller sees ``None``.
        """
        changed = await self._db.enqueue_rowcount(
            """
            INSERT INTO space_posts(
                id, space_id, author, bot_id, linked_event_id, type, content,
                media_url, reactions, comment_count, pinned, deleted, edited_at,
                no_link_preview, moderated, file_meta_json, location_json,
                image_urls_json, linked_highlight_id, hidden_from_feed, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')))
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
                image_urls_json=excluded.image_urls_json,
                linked_event_id=excluded.linked_event_id,
                linked_highlight_id=excluded.linked_highlight_id,
                hidden_from_feed=excluded.hidden_from_feed
             WHERE space_posts.space_id = excluded.space_id
            """,
            (
                post.id,
                space_id,
                post.author,
                post.bot_id,
                post.linked_event_id,
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
                post.linked_highlight_id,
                int(post.hidden_from_feed),
                _iso_or_none(post.created_at),
            ),
        )
        return post if changed else None

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

    async def get_by_linked_event_id(
        self,
        event_id: str,
    ) -> tuple[str, Post] | None:
        """Return the auto-created event post for ``event_id``, if any.

        Used by :class:`CalendarFeedBridge` to find the post produced for
        a given calendar event so subsequent updates / deletes can edit
        the same row rather than creating a duplicate.
        """
        row = await self._db.fetchone(
            "SELECT * FROM space_posts WHERE linked_event_id=? LIMIT 1",
            (event_id,),
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
        # ``hidden_from_feed=0`` drops listing/event anchor posts whose
        # author chose not to announce them in the feed — they live in
        # their own tab (Bazaar, Calendar) instead.
        if before is None:
            rows = await self._db.fetchall(
                "SELECT * FROM space_posts "
                "WHERE space_id=? AND deleted=0 AND hidden_from_feed=0 "
                "ORDER BY created_at DESC LIMIT ?",
                (space_id, int(limit)),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM space_posts "
                "WHERE space_id=? AND deleted=0 AND hidden_from_feed=0 "
                "AND created_at < ? "
                "ORDER BY created_at DESC LIMIT ?",
                (space_id, before, int(limit)),
            )
        return [_row_to_space_post(d) for d in rows_to_dicts(rows)]

    async def list_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[Post]:
        """Posts created after ``since`` (ISO-8601), oldest-first.

        Used by ``SpaceSyncResumeProvider`` (spec §11452) to replay
        catch-up events to a peer that just reconnected. Soft-deleted
        rows are skipped — the provider will not back-emit deletes.
        Capped at ``limit`` (default 500) to bound a single resume
        burst; callers paginate by re-issuing with the latest
        ``created_at`` as the new ``since``.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM space_posts "
            "WHERE space_id=? AND deleted=0 AND created_at > ? "
            "ORDER BY created_at ASC LIMIT ?",
            (space_id, since, int(limit)),
        )
        return [_row_to_space_post(d) for d in rows_to_dicts(rows)]

    async def soft_delete(
        self,
        post_id: str,
        *,
        space_id: str,
        moderated_by: str | None = None,
    ) -> bool:
        """Soft-delete a space post. ``moderated_by`` sets the
        ``moderated=1`` flag so the service layer can distinguish a
        self-delete from an admin removal (§5.2 moderation).

        Scoped to ``space_id`` (#693) — returns ``False`` when the post
        does not live in that space (or does not exist).
        """
        return (
            await self._db.enqueue_rowcount(
                """
                UPDATE space_posts
                   SET deleted=1, content=NULL, media_url=NULL,
                       moderated=CASE WHEN ? IS NOT NULL THEN 1 ELSE moderated END
                 WHERE id=? AND space_id=?
                """,
                (moderated_by, post_id, space_id),
            )
            > 0
        )

    async def edit(
        self,
        post_id: str,
        new_content: str,
        *,
        space_id: str,
    ) -> bool:
        """Replace a post's body. ``False`` = not in ``space_id`` (#693)."""
        return (
            await self._db.enqueue_rowcount(
                "UPDATE space_posts SET content=?, edited_at=datetime('now') "
                "WHERE id=? AND space_id=?",
                (new_content, post_id, space_id),
            )
            > 0
        )

    async def list_space_media_urls(self, space_id: str) -> list[str]:
        """Every ``api/media/<file>`` URL a space's posts + image comments
        reference, for on-disk cleanup when the space is hard-deleted.

        Covers post ``media_url`` + the ``image_urls`` gallery list +
        image-comment ``media_url``. Returned values are the raw stored
        strings (``unlink_media`` resolves the basename). Must be called
        *before* the space rows are dropped.
        """
        urls: list[str] = []
        post_rows = await self._db.fetchall(
            "SELECT media_url, image_urls_json FROM space_posts WHERE space_id=?",
            (space_id,),
        )
        for r in post_rows:
            if r["media_url"]:
                urls.append(r["media_url"])
            urls.extend(_decode_image_urls(r["image_urls_json"]))
        comment_rows = await self._db.fetchall(
            """
            SELECT c.media_url
              FROM space_post_comments c
              JOIN space_posts p ON c.post_id = p.id
             WHERE p.space_id=? AND c.media_url IS NOT NULL
            """,
            (space_id,),
        )
        urls.extend(r["media_url"] for r in comment_rows if r["media_url"])
        return urls

    # ── Reactions (atomic) ─────────────────────────────────────────────

    async def add_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
        *,
        space_id: str,
    ) -> Post:
        """Add a reaction inside ``space_id`` (#693).

        Both the SELECT and the UPDATE carry ``AND space_id=?`` so a post
        belonging to another space raises ``KeyError`` instead of being
        mutated.
        """

        def _run(conn):
            row = conn.execute(
                "SELECT * FROM space_posts WHERE id=? AND space_id=? AND deleted=0",
                (post_id, space_id),
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
                "UPDATE space_posts SET reactions=? WHERE id=? AND space_id=?",
                (_encode_reactions(_to_frozenset(reactions)), post_id, space_id),
            )
            row = conn.execute(
                "SELECT * FROM space_posts WHERE id=? AND space_id=?",
                (post_id, space_id),
            ).fetchone()
            return {k: row[k] for k in row.keys()}

        row = await self._db.transact(_run)
        return _row_to_space_post(row)

    async def remove_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
        *,
        space_id: str,
    ) -> Post:
        """Remove a reaction inside ``space_id`` (#693) — see
        :meth:`add_reaction` for the scoping rule."""

        def _run(conn):
            row = conn.execute(
                "SELECT * FROM space_posts WHERE id=? AND space_id=?",
                (post_id, space_id),
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
                    "UPDATE space_posts SET reactions=? WHERE id=? AND space_id=?",
                    (_encode_reactions(_to_frozenset(reactions)), post_id, space_id),
                )
                row = conn.execute(
                    "SELECT * FROM space_posts WHERE id=? AND space_id=?",
                    (post_id, space_id),
                ).fetchone()
            return {k: row[k] for k in row.keys()}

        row = await self._db.transact(_run)
        return _row_to_space_post(row)

    # ── Comment counters ───────────────────────────────────────────────

    async def increment_comment_count(
        self,
        post_id: str,
        *,
        space_id: str,
    ) -> bool:
        """Bump the counter for a post in ``space_id`` (#693)."""
        return (
            await self._db.enqueue_rowcount(
                "UPDATE space_posts SET comment_count = comment_count + 1 "
                "WHERE id=? AND space_id=?",
                (post_id, space_id),
            )
            > 0
        )

    async def decrement_comment_count(
        self,
        post_id: str,
        *,
        space_id: str,
    ) -> bool:
        """Lower the counter for a post in ``space_id`` (#693)."""
        return (
            await self._db.enqueue_rowcount(
                "UPDATE space_posts "
                "SET comment_count = MAX(0, comment_count - 1) "
                "WHERE id=? AND space_id=?",
                (post_id, space_id),
            )
            > 0
        )

    # ── Comments ───────────────────────────────────────────────────────

    async def add_comment(self, comment: Comment, *, space_id: str) -> bool:
        """Insert a comment, but only onto a post in ``space_id`` (#693).

        ``space_post_comments`` has no ``space_id`` of its own — the scope
        lives on the parent post — so the guard is an ``EXISTS`` sub-select
        inside the same statement rather than a handler-side pre-read
        (which would race and could be skipped by a new caller).
        """
        return (
            await self._db.enqueue_rowcount(
                """
                INSERT INTO space_post_comments(
                    id, post_id, parent_id, author, type, content, media_url,
                    deleted, created_at
                )
                SELECT ?,?,?,?,?,?,?,?, COALESCE(?, datetime('now'))
                 WHERE EXISTS (
                     SELECT 1 FROM space_posts WHERE id=? AND space_id=?
                 )
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
                    comment.post_id,
                    space_id,
                ),
            )
            > 0
        )

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

    async def list_comments_since(
        self,
        space_id: str,
        since: str,
        *,
        limit: int = 500,
    ) -> list[tuple[str, Comment]]:
        """Comments on posts in *space_id* with ``created_at > since``.

        Returns ``(post_id, Comment)`` pairs so the resume replay can
        emit the ``post_id`` field every ``SPACE_COMMENT_*`` event
        carries. Joins ``space_post_comments`` with ``space_posts`` to
        scope by space (the comment row itself only knows its parent
        post). Soft-deleted comments are omitted from the burst.
        """
        rows = await self._db.fetchall(
            "SELECT c.* FROM space_post_comments c "
            "JOIN space_posts p ON p.id = c.post_id "
            "WHERE p.space_id=? AND c.deleted=0 AND c.created_at > ? "
            "ORDER BY c.created_at ASC LIMIT ?",
            (space_id, since, int(limit)),
        )
        out: list[tuple[str, Comment]] = []
        for d in rows_to_dicts(rows):
            comment = _row_to_space_comment(d)
            if comment is None:
                continue
            out.append((d["post_id"], comment))
        return out

    async def soft_delete_comment(self, comment_id: str, *, space_id: str) -> bool:
        """Soft-delete a comment whose parent post is in ``space_id`` (#693)."""
        return (
            await self._db.enqueue_rowcount(
                """
                UPDATE space_post_comments
                   SET deleted=1, content=NULL, media_url=NULL
                 WHERE id=? AND EXISTS (
                     SELECT 1 FROM space_posts
                      WHERE id = space_post_comments.post_id AND space_id=?
                 )
                """,
                (comment_id, space_id),
            )
            > 0
        )

    async def edit_comment(
        self,
        comment_id: str,
        new_content: str,
        *,
        space_id: str,
    ) -> bool:
        """Edit a comment whose parent post is in ``space_id`` (#693)."""
        return (
            await self._db.enqueue_rowcount(
                """
                UPDATE space_post_comments
                   SET content=?, edited_at=datetime('now')
                 WHERE id=? AND deleted=0 AND EXISTS (
                     SELECT 1 FROM space_posts
                      WHERE id = space_post_comments.post_id AND space_id=?
                 )
                """,
                (new_content, comment_id, space_id),
            )
            > 0
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
        bot_id=row.get("bot_id"),
        linked_event_id=row.get("linked_event_id"),
        linked_highlight_id=row.get("linked_highlight_id"),
        hidden_from_feed=bool_col(row.get("hidden_from_feed", 0)),
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
