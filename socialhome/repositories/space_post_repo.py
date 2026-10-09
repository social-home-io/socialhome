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

import json

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.post import (
    Comment,
    CommentType,
    MAX_DISTINCT_REACTIONS_PER_POST,
    Post,
    PostTombstone,
    PostType,
)
from ..domain.space_item import StaleItemStamp, stamp_to_db
from ..utils.datetime import parse_iso8601_optional
from .base import (
    bool_col,
    changed_since_sql,
    retention_window_sql,
    row_to_dict,
    rows_to_dicts,
    sync_page_cursor,
)
from .post_repo import (  # reuse the household post helpers verbatim
    _decode_reactions,
    _encode_reactions,
    _encode_file_meta,
    _decode_file_meta,
    _decode_link_preview,
    _encode_link_preview,
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
    async def list_sync_page(
        self,
        space_id: str,
        *,
        cutoff: str | None = None,
        exempt_types: tuple[str, ...] = (),
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[Post], int | None]:
        """One page of the space's live posts for a §25.6 sync, newest
        first. Anchors included.

        The catch-up exporters enumerate posts through this, NOT
        :meth:`list_feed`: the feed query drops ``hidden_from_feed`` rows
        (bazaar listing / calendar event anchors the author chose not to
        announce), and a joiner that never receives the anchor cannot
        store the listing that hangs on it — ``bazaar_listings.post_id``
        references ``space_posts(id)``.

        ``cutoff`` / ``exempt_types`` are the space's retention window
        (:func:`~socialhome.repositories.base.retention_window_sql`).
        Returns ``(posts, next_cursor)``; ``next_cursor`` is ``None`` once
        the last page is read, else what to pass as ``cursor`` for the
        next one (keyset on the row id: no page repeats or skips a row).
        """
        ...

    async def list_post_tombstones_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[PostTombstone], int | None]:
        """One page of the space's deleted posts (``posts_deleted``),
        newest first — every one, whatever its age: a delete, or a retention
        expiry on the host, must reach every household that holds the row.
        Identity only (no content). Same ``(rows, next_cursor)`` paging as
        :meth:`list_sync_page`."""
        ...

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
        clear_link_preview: bool = False,
        edited_at: str | None = None,
    ) -> bool: ...

    async def add_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
        *,
        space_id: str,
        stamp: str | None = None,
    ) -> Post: ...
    async def remove_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
        *,
        space_id: str,
        stamp: str | None = None,
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
    async def list_comments_sync_page(
        self,
        space_id: str,
        *,
        deleted: bool = False,
        cutoff: str | None = None,
        exempt_types: tuple[str, ...] = (),
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[Comment], int | None]:
        """One page of the space's comments for a §25.6 sync, oldest
        stored first (a reply never before its parent): the live ones on
        live posts, or (``deleted=True``) the deleted ones on any post —
        the ``comments_deleted`` tombstones. The retention window is the
        parent post's. Same ``(rows, next_cursor)`` paging as
        :meth:`list_sync_page`."""
        ...

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
        edited_at: str | None = None,
    ) -> bool: ...


class SqliteSpacePostRepo:
    """SQLite-backed :class:`AbstractSpacePostRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Posts ──────────────────────────────────────────────────────────

    async def save(self, space_id: str, post: Post) -> Post | None:
        """Insert or update a space post. ``None`` = refused.

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
                image_urls_json, linked_highlight_id, hidden_from_feed,
                link_preview_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')))
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
                hidden_from_feed=excluded.hidden_from_feed,
                link_preview_json=excluded.link_preview_json
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
                _encode_link_preview(post.link_preview),
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

    async def list_sync_page(
        self,
        space_id: str,
        *,
        cutoff: str | None = None,
        exempt_types: tuple[str, ...] = (),
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[Post], int | None]:
        # No ``hidden_from_feed`` filter — see the protocol docstring. The
        # receiver stores the flag with the row, so the joiner's feed stays
        # exactly as clean as the provider's.
        window, window_params = retention_window_sql(
            "created_at", cutoff, type_col="type", exempt_types=exempt_types
        )
        changed, changed_params = changed_since_sql("sync_seq", since)
        rows = rows_to_dicts(
            await self._db.fetchall(
                "SELECT rowid AS sync_rowid, * FROM space_posts"
                " WHERE space_id=? AND deleted=0"
                + window
                + changed
                + " AND (? IS NULL OR rowid < ?)"
                " ORDER BY rowid DESC LIMIT ?",
                (
                    space_id,
                    *window_params,
                    *changed_params,
                    cursor,
                    cursor,
                    int(limit),
                ),
            )
        )
        return [_row_to_space_post(d) for d in rows], sync_page_cursor(rows, limit)

    async def list_post_tombstones_page(
        self,
        space_id: str,
        *,
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[PostTombstone], int | None]:
        changed, changed_params = changed_since_sql("sync_seq", since)
        rows = rows_to_dicts(
            await self._db.fetchall(
                "SELECT rowid AS sync_rowid, id, author, type, created_at,"
                " moderated, moderated_by FROM space_posts"
                " WHERE space_id=? AND deleted=1"
                + changed
                + " AND (? IS NULL OR rowid < ?) ORDER BY rowid DESC LIMIT ?",
                (space_id, *changed_params, cursor, cursor, int(limit)),
            )
        )
        tombstones = [
            PostTombstone(
                id=d["id"],
                author=d["author"],
                type=d["type"],
                created_at=d["created_at"],
                moderated=bool_col(d["moderated"]),
                moderated_by=d["moderated_by"],
            )
            for d in rows
        ]
        return tombstones, sync_page_cursor(rows, limit)

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

        Scoped to ``space_id`` — returns ``False`` when the post
        does not live in that space (or does not exist).
        """
        return (
            await self._db.enqueue_rowcount(
                """
                UPDATE space_posts
                   SET deleted=1, content=NULL, media_url=NULL,
                       link_preview_json=NULL,
                       moderated=CASE WHEN ? IS NOT NULL THEN 1 ELSE moderated END,
                       moderated_by=COALESCE(?, moderated_by)
                 WHERE id=? AND space_id=?
                """,
                (moderated_by, moderated_by, post_id, space_id),
            )
            > 0
        )

    async def edit(
        self,
        post_id: str,
        new_content: str,
        *,
        space_id: str,
        clear_link_preview: bool = False,
        edited_at: str | None = None,
    ) -> bool:
        """Replace a post's body. ``False`` = not in ``space_id``.
        ``clear_link_preview`` drops the link card (the edit changed or
        removed the link it was built for).

        ``edited_at`` (v_49, a member-published edit) is the author's signed
        edit time in the column's naive-UTC shape: the edit lands only on a
        live post whose stored ``edited_at`` is older (last writer wins), so
        ``False`` also means "a newer edit — or a delete — is already held".
        Without it the edit is stamped ``datetime('now')`` as before."""
        if edited_at is None:
            return (
                await self._db.enqueue_rowcount(
                    "UPDATE space_posts SET content=?, edited_at=datetime('now'), "
                    "link_preview_json=CASE WHEN ? THEN NULL "
                    "ELSE link_preview_json END "
                    "WHERE id=? AND space_id=?",
                    (new_content, int(clear_link_preview), post_id, space_id),
                )
                > 0
            )
        return (
            await self._db.enqueue_rowcount(
                "UPDATE space_posts SET content=?, edited_at=?, "
                "link_preview_json=CASE WHEN ? THEN NULL ELSE link_preview_json END "
                "WHERE id=? AND space_id=? AND deleted=0 "
                "AND (edited_at IS NULL OR edited_at < ?)",
                (
                    new_content,
                    edited_at,
                    int(clear_link_preview),
                    post_id,
                    space_id,
                    edited_at,
                ),
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
    #
    # Every add / remove also records, in the same transaction, WHEN it was
    # made for its (user, emoji) in ``reaction_stamps_json`` (migration
    # 0075): ``stamp`` is a member-relayed reaction's signed time (naive
    # UTC), a local or unstamped write is stamped now. A stamped write that
    # is not newer than the recorded one raises :class:`StaleItemStamp` and
    # changes nothing — a late duplicate add can never bring back a removed
    # reaction, across restarts too. A removal keeps its entry (added =
    # false) as the tombstone.

    async def add_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
        *,
        space_id: str,
        stamp: str | None = None,
    ) -> Post:
        """Add a reaction inside ``space_id``.

        Both the SELECT and the UPDATE carry ``AND space_id=?`` so a post
        belonging to another space raises ``KeyError`` instead of being
        mutated.
        """
        return await self._react(
            post_id, emoji, user_id, space_id=space_id, stamp=stamp, added=True
        )

    async def remove_reaction(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
        *,
        space_id: str,
        stamp: str | None = None,
    ) -> Post:
        """Remove a reaction inside ``space_id`` — see
        :meth:`add_reaction` for the scoping rule."""
        return await self._react(
            post_id, emoji, user_id, space_id=space_id, stamp=stamp, added=False
        )

    async def _react(
        self,
        post_id: str,
        emoji: str,
        user_id: str,
        *,
        space_id: str,
        stamp: str | None,
        added: bool,
    ) -> Post:
        at = stamp or stamp_to_db(datetime.now(timezone.utc))

        def _run(conn):
            where = "id=? AND space_id=?" + (" AND deleted=0" if added else "")
            row = conn.execute(
                f"SELECT * FROM space_posts WHERE {where}",
                (post_id, space_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"space post {post_id!r} not found")
            row_dict = {k: row[k] for k in row.keys()}
            stamps = _decode_stamps(row_dict.get("reaction_stamps_json"))
            key = f"{user_id}\x00{emoji}"
            held = stamps.get(key)
            if stamp is not None and held is not None and held[0] >= stamp:
                raise StaleItemStamp(f"reaction on {post_id!r} is not newer")
            reactions = _decode_reactions(row_dict["reactions"])
            if added:
                if (
                    emoji not in reactions
                    and len(reactions) >= MAX_DISTINCT_REACTIONS_PER_POST
                ):
                    raise ValueError("too many distinct reactions on this post")
                reactions.setdefault(emoji, set()).add(user_id)
            else:
                bucket = reactions.get(emoji)
                if bucket and user_id in bucket:
                    bucket.discard(user_id)
                    if not bucket:
                        reactions.pop(emoji, None)
            stamps[key] = [at, added]
            conn.execute(
                "UPDATE space_posts SET reactions=?, reaction_stamps_json=? "
                "WHERE id=? AND space_id=?",
                (
                    _encode_reactions(_to_frozenset(reactions)),
                    _encode_stamps(stamps),
                    post_id,
                    space_id,
                ),
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
        """Bump the counter for a post in ``space_id``."""
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
        """Lower the counter for a post in ``space_id``."""
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
        """Insert a comment, but only onto a post in ``space_id``. ``False``
        also for an id already held (a member-relayed copy, a tombstone):
        the held row is kept, never overwritten, and never an error — so a
        batch (a sync chunk) carries on past it.

        A reply's ``parent_id`` must name a comment on the *same* post —
        a thread cannot hang under a comment of another post (or space).

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
                    deleted, edited_at, created_at
                )
                SELECT ?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now'))
                 WHERE EXISTS (
                     SELECT 1 FROM space_posts WHERE id=? AND space_id=?
                 )
                   AND (
                     ? IS NULL OR EXISTS (
                         SELECT 1 FROM space_post_comments
                          WHERE id=? AND post_id=?
                     )
                   )
                ON CONFLICT(id) DO NOTHING
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
                    (
                        stamp_to_db(comment.edited_at)
                        if comment.edited_at is not None
                        else None
                    ),
                    _iso_or_none(comment.created_at),
                    comment.post_id,
                    space_id,
                    comment.parent_id,
                    comment.parent_id,
                    comment.post_id,
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

    async def list_comments_sync_page(
        self,
        space_id: str,
        *,
        deleted: bool = False,
        cutoff: str | None = None,
        exempt_types: tuple[str, ...] = (),
        cursor: int | None = None,
        limit: int = 200,
        since: int | None = None,
    ) -> tuple[list[Comment], int | None]:
        window, window_params = retention_window_sql(
            "p.created_at", cutoff, type_col="p.type", exempt_types=exempt_types
        )
        changed, changed_params = changed_since_sql("c.sync_seq", since)
        rows = rows_to_dicts(
            await self._db.fetchall(
                "SELECT c.rowid AS sync_rowid, c.* FROM space_post_comments c"
                " JOIN space_posts p ON p.id = c.post_id"
                " WHERE p.space_id=? AND c.deleted=?"
                + ("" if deleted else " AND p.deleted=0")
                + window
                + changed
                + " AND c.rowid > ? ORDER BY c.rowid LIMIT ?",
                (
                    space_id,
                    int(deleted),
                    *window_params,
                    *changed_params,
                    cursor or 0,
                    int(limit),
                ),
            )
        )
        comments = [c for c in (_row_to_space_comment(d) for d in rows) if c]
        return comments, sync_page_cursor(rows, limit)

    async def soft_delete_comment(self, comment_id: str, *, space_id: str) -> bool:
        """Soft-delete a comment whose parent post is in ``space_id``.

        ``False`` also for a comment that is already deleted, so a replayed
        delete cannot lower the parent's counter twice.
        """
        return (
            await self._db.enqueue_rowcount(
                """
                UPDATE space_post_comments
                   SET deleted=1, content=NULL, media_url=NULL
                 WHERE id=? AND deleted=0 AND EXISTS (
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
        edited_at: str | None = None,
    ) -> bool:
        """Edit a comment whose parent post is in ``space_id``.

        ``edited_at`` — see :meth:`edit`: with it, the edit lands only over
        an older stored stamp (last writer wins)."""
        return (
            await self._db.enqueue_rowcount(
                """
                UPDATE space_post_comments
                   SET content=?, edited_at=COALESCE(?, datetime('now'))
                 WHERE id=? AND deleted=0 AND EXISTS (
                     SELECT 1 FROM space_posts
                      WHERE id = space_post_comments.post_id AND space_id=?
                 )
                   AND (? IS NULL OR edited_at IS NULL OR edited_at < ?)
                """,
                (new_content, edited_at, comment_id, space_id, edited_at, edited_at),
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
        link_preview=_decode_link_preview(row.get("link_preview_json")),
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


#: How long a reaction removal's tombstone is kept: longer than any
#: duplicate of the add can still arrive — the GFS holds queued items for
#: 24 h, the publisher's retry queue adds its backoff on top. Past it the
#: tombstone is dropped.
REACTION_TOMBSTONE_HORIZON_S: int = 48 * 3600

#: Entries one user may hold per post. Over it, that user's OWN oldest
#: tombstones go first, so nobody can push out someone else's.
MAX_REACTION_STAMPS_PER_USER: int = 64

#: Last-resort bound per post (oldest tombstones first, then oldest).
MAX_REACTION_STAMPS_PER_POST: int = 4096


def _decode_stamps(raw: object) -> dict[str, list]:
    """``reaction_stamps_json`` → ``{"user\\0emoji": [stamp, added]}``."""
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k): [str(v[0]), bool(v[1])]
        for k, v in data.items()
        if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)
    }


def _encode_stamps(stamps: dict[str, list]) -> str:
    """Bound the map by AGE first — a tombstone lives
    :data:`REACTION_TOMBSTONE_HORIZON_S` — then per user, then per post;
    each cap evicts tombstones before live entries, oldest first."""
    cutoff = stamp_to_db(
        datetime.now(timezone.utc) - timedelta(seconds=REACTION_TOMBSTONE_HORIZON_S)
    )
    for k in [k for k, (at, added) in stamps.items() if not added and at < cutoff]:
        del stamps[k]

    def _evict(keys: list[str], cap: int) -> None:
        order = sorted(keys, key=lambda k: (bool(stamps[k][1]), stamps[k][0]))
        for k in order[: max(0, len(keys) - cap)]:
            del stamps[k]

    by_user: dict[str, list[str]] = {}
    for k in stamps:
        by_user.setdefault(k.split("\x00", 1)[0], []).append(k)
    for keys in by_user.values():
        _evict(keys, MAX_REACTION_STAMPS_PER_USER)
    _evict(list(stamps), MAX_REACTION_STAMPS_PER_POST)
    return json.dumps(stamps, ensure_ascii=False, sort_keys=True)
