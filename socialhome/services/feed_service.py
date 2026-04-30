"""Feed service — household-feed business logic.

Orchestrates :class:`AbstractPostRepo` and :class:`AbstractUserRepo`,
publishing domain events on the injected :class:`EventBus` so that the
notification service, WebSocket manager and any federation subscribers
can react.

Permissions enforced here:

* Only the author or an admin may edit / delete a post or comment.
* Only actions on non-deleted posts are allowed.
* Reaction emoji are stripped down to a single grapheme cluster to avoid
  invisible-character abuse (§5.2).

The route layer is expected to catch ``ValueError`` → 422,
``PermissionError`` → 403, ``KeyError`` → 404 as usual.
"""

from __future__ import annotations

import unicodedata
import uuid
from datetime import datetime, timezone

from ..domain.events import (
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    PostCreated,
    PostDeleted,
    PostEdited,
    PostReactionChanged,
)
from ..domain.post import (
    FEED_POST_MAX_IMAGES,
    Comment,
    CommentType,
    FileMeta,
    LocationData,
    Post,
    PostType,
)
from ..domain.presence import truncate_coord
from ..infrastructure.event_bus import EventBus
from ..repositories.post_repo import AbstractPostRepo
from ..repositories.user_repo import AbstractUserRepo


#: Max content length for a text / transcript post. Longer content is
#: rejected with ValueError; image posts can still carry a caption up to
#: this length.
MAX_POST_LENGTH = 10_000

#: Max comment length.
MAX_COMMENT_LENGTH = 2_000

#: Cap for the optional location-post label (composer hint, not a hard
#: spec). Long enough for "Pascal's Cottage near Sintra" without
#: collapsing the feed card; short enough to keep the federated payload
#: lean.
LOCATION_LABEL_MAX = 80


class FeedService:
    """Household-feed CRUD + reactions + comments."""

    __slots__ = ("_posts", "_users", "_bus", "_household", "_quota")

    def __init__(
        self,
        post_repo: AbstractPostRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
    ) -> None:
        self._posts = post_repo
        self._users = user_repo
        self._bus = bus
        self._household = None  # set via attach_household_features
        self._quota = None  # set via attach_storage_quota

    def attach_household_features(self, svc) -> None:
        """Wire :class:`HouseholdFeaturesService` for toggle enforcement
        (§18). When attached, ``create_post`` gates on both the feed
        section toggle and the per-post-type allowlist.
        """
        self._household = svc

    def attach_storage_quota(self, svc) -> None:
        """Wire :class:`StorageQuotaService` so ``create_post`` with
        ``file_meta`` pre-checks the household's remaining budget."""
        self._quota = svc

    # ── Posts ──────────────────────────────────────────────────────────

    async def create_post(
        self,
        *,
        author_user_id: str,
        type: PostType | str,
        content: str | None = None,
        media_url: str | None = None,
        image_urls: tuple[str, ...] | list[str] = (),
        file_meta: FileMeta | None = None,
        location: LocationData | None = None,
        pinned: bool = False,
        no_link_preview: bool = False,
    ) -> Post:
        """Persist a new post in the household feed.

        Raises :class:`ValueError` on validation failures (missing author,
        bad type, over-length content, etc.) and :class:`KeyError` if the
        author is unknown.

        Image posts use ``image_urls`` exclusively (1 to
        :data:`FEED_POST_MAX_IMAGES` URLs); ``media_url`` stays ``None``.
        Other types may set ``media_url`` (video / file) but must leave
        ``image_urls`` empty.
        """
        author = await self._require_author(author_user_id)
        post_type = _coerce_post_type(type)
        image_urls_tuple = tuple(image_urls)
        _validate_content(
            post_type,
            content,
            file_meta,
            location,
            image_urls_tuple,
        )
        # Truncate to 4dp at the service boundary regardless of what the
        # client sent — the column never holds higher precision than the
        # federated form (§GPS truncation).
        if location is not None:
            location = LocationData(
                lat=truncate_coord(location.lat) or 0.0,
                lon=truncate_coord(location.lon) or 0.0,
                label=location.label,
            )

        # Household feature toggles (§18). Feed can be disabled entirely
        # or only certain post types (e.g. allow_video=False) may be on.
        if self._household is not None:
            await self._household.require_enabled("feed")
            await self._household.require_post_type(
                post_type.value if hasattr(post_type, "value") else str(post_type),
            )
        # Storage quota pre-check for posts with attached media.
        if self._quota is not None and file_meta is not None:
            size = int(getattr(file_meta, "size_bytes", 0) or 0)
            if size > 0:
                await self._quota.check_can_store(size)

        post = Post(
            id=uuid.uuid4().hex,
            author=author.user_id,
            type=post_type,
            created_at=datetime.now(timezone.utc),
            content=content,
            # Image posts route their URLs through ``image_urls``;
            # ``media_url`` stays for video / file scalars only.
            media_url=None if post_type is PostType.IMAGE else media_url,
            image_urls=image_urls_tuple,
            file_meta=file_meta,
            location=location,
            pinned=bool(pinned),
            no_link_preview=bool(no_link_preview),
        )
        await self._posts.save(post)
        await self._bus.publish(PostCreated(post=post))
        return post

    async def edit_post(
        self,
        post_id: str,
        *,
        editor_user_id: str,
        new_content: str,
    ) -> Post:
        """Replace a post's content. Only the author or an admin may edit."""
        post = await self._require_post(post_id)
        await self._require_author_or_admin(post.author, editor_user_id)
        _validate_text_length(new_content, limit=MAX_POST_LENGTH)

        await self._posts.edit(post_id, new_content)
        updated = await self._posts.get(post_id)
        assert updated is not None  # we just edited it
        await self._bus.publish(PostEdited(post=updated))
        return updated

    async def delete_post(
        self,
        post_id: str,
        *,
        actor_user_id: str,
    ) -> None:
        """Soft-delete a post. Only the author or an admin may delete."""
        post = await self._require_post(post_id)
        await self._require_author_or_admin(post.author, actor_user_id)
        await self._posts.soft_delete(post_id)
        await self._bus.publish(PostDeleted(post_id=post_id))

    async def get_post(self, post_id: str) -> Post:
        return await self._require_post(post_id)

    async def list_feed(
        self,
        *,
        before: str | None = None,
        limit: int = 20,
    ) -> list[Post]:
        """Return a page of posts ordered newest-first.

        Caller is responsible for clamping ``limit`` at the route layer
        (§5.2 pagination rule — 1 ≤ limit ≤ 50).
        """
        limit = max(1, min(int(limit), 50))
        return await self._posts.list_feed(before=before, limit=limit)

    # ── Reactions ──────────────────────────────────────────────────────

    async def add_reaction(
        self,
        post_id: str,
        *,
        user_id: str,
        emoji: str,
    ) -> Post:
        emoji = _normalise_emoji(emoji)
        post = await self._posts.add_reaction(post_id, emoji, user_id)
        await self._bus.publish(PostReactionChanged(post=post))
        return post

    async def remove_reaction(
        self,
        post_id: str,
        *,
        user_id: str,
        emoji: str,
    ) -> Post:
        emoji = _normalise_emoji(emoji)
        post = await self._posts.remove_reaction(post_id, emoji, user_id)
        await self._bus.publish(PostReactionChanged(post=post))
        return post

    # ── Comments ───────────────────────────────────────────────────────

    async def add_comment(
        self,
        post_id: str,
        *,
        author_user_id: str,
        content: str | None = None,
        media_url: str | None = None,
        comment_type: CommentType | str = CommentType.TEXT,
        parent_id: str | None = None,
    ) -> Comment:
        post = await self._require_post(post_id)
        author = await self._require_author(author_user_id)
        ctype = _coerce_comment_type(comment_type)
        if ctype is CommentType.TEXT:
            _validate_text_length(content, limit=MAX_COMMENT_LENGTH)
        elif ctype is CommentType.IMAGE and not media_url:
            raise ValueError("image comment requires media_url")

        if parent_id is not None:
            parent = await self._posts.get_comment(parent_id)
            if parent is None or parent.post_id != post_id:
                raise KeyError(f"parent comment {parent_id!r} not found in this post")

        comment = Comment(
            id=uuid.uuid4().hex,
            post_id=post.id,
            author=author.user_id,
            type=ctype,
            created_at=datetime.now(timezone.utc),
            parent_id=parent_id,
            content=content,
            media_url=media_url,
        )
        await self._posts.add_comment(comment)
        await self._posts.increment_comment_count(post_id)
        await self._bus.publish(CommentAdded(post_id=post_id, comment=comment))
        return comment

    async def edit_comment(
        self,
        comment_id: str,
        *,
        editor_user_id: str,
        new_content: str,
    ) -> Comment:
        """Edit a comment's body. Author-or-admin only. Text only — image
        comments cannot be edited in v1."""
        comment = await self._posts.get_comment(comment_id)
        if comment is None or comment.deleted:
            raise KeyError(f"comment {comment_id!r} not found")
        if comment.type is not CommentType.TEXT:
            raise ValueError("only text comments can be edited")
        await self._require_author_or_admin(comment.author, editor_user_id)
        _validate_text_length(new_content, limit=MAX_COMMENT_LENGTH)
        if not new_content.strip():
            raise ValueError("comment body cannot be empty")
        await self._posts.edit_comment(comment_id, new_content)
        updated = await self._posts.get_comment(comment_id)
        assert updated is not None
        await self._bus.publish(
            CommentUpdated(post_id=updated.post_id, comment=updated),
        )
        return updated

    async def delete_comment(
        self,
        comment_id: str,
        *,
        actor_user_id: str,
    ) -> None:
        comment = await self._posts.get_comment(comment_id)
        if comment is None:
            raise KeyError(f"comment {comment_id!r} not found")
        if comment.deleted:
            return
        await self._require_author_or_admin(comment.author, actor_user_id)
        await self._posts.soft_delete_comment(comment_id)
        await self._posts.decrement_comment_count(comment.post_id)
        await self._bus.publish(
            CommentDeleted(post_id=comment.post_id, comment_id=comment_id),
        )

    async def list_comments(self, post_id: str) -> list[Comment]:
        return await self._posts.list_comments(post_id)

    # ── Bookmarks ──────────────────────────────────────────────────────

    async def bookmark(self, user_id: str, post_id: str) -> None:
        await self._require_post(post_id)
        await self._posts.save_bookmark(user_id, post_id)

    async def unbookmark(self, user_id: str, post_id: str) -> None:
        await self._posts.unsave_bookmark(user_id, post_id)

    async def list_bookmarks(self, user_id: str) -> list[Post]:
        return await self._posts.list_bookmarks(user_id)

    # ── Read watermark (§23.17.1) ──────────────────────────────────────

    async def mark_read(self, user_id: str, *, post_id: str | None) -> None:
        """Record the user's most-recent-read post id for scroll
        restoration. ``post_id=None`` is accepted — a client that wants
        to clear the marker can send it explicitly. When a non-null
        ``post_id`` is given it must refer to an existing, non-deleted
        post; otherwise :class:`KeyError` is raised.
        """
        if post_id is not None:
            await self._require_post(post_id)
        await self._posts.set_read_watermark(user_id, post_id)

    async def get_read_watermark(self, user_id: str) -> dict | None:
        """Return ``{last_read_post_id, last_read_at}`` for the user or
        ``None`` if they've never marked anything read.
        """
        return await self._posts.get_read_watermark(user_id)

    # ── Internal helpers ───────────────────────────────────────────────

    async def _require_post(self, post_id: str) -> Post:
        post = await self._posts.get(post_id)
        if post is None or post.deleted:
            raise KeyError(f"post {post_id!r} not found")
        return post

    async def _require_author(self, user_id: str):
        author = await self._users.get_by_user_id(user_id)
        if author is None:
            raise KeyError(f"user {user_id!r} not found")
        if not author.is_active():
            raise PermissionError(f"user {user_id!r} is not active")
        return author

    async def _require_author_or_admin(
        self,
        post_author_id: str,
        actor_user_id: str,
    ) -> None:
        if post_author_id == actor_user_id:
            return
        actor = await self._users.get_by_user_id(actor_user_id)
        if actor is None:
            raise PermissionError("not authorised")
        if not actor.is_admin:
            raise PermissionError("only the author or an admin can do this")


# ─── Validation helpers ───────────────────────────────────────────────────

_EMOJI_MAX_CODEPOINTS = 20  # guardrail for abusive ZWJ sequences


def _normalise_emoji(emoji: str) -> str:
    """Strip whitespace, NFC-normalise, and cap the codepoint length.

    The spec (§5.2) normalises emoji to NFC on write and trims ZWJ chains
    that exceed a reasonable length to prevent invisible-character abuse.
    """
    cleaned = emoji.strip()
    if not cleaned:
        raise ValueError("emoji must not be empty")
    normalised = unicodedata.normalize("NFC", cleaned)
    if len(normalised) > _EMOJI_MAX_CODEPOINTS:
        raise ValueError("emoji too long")
    return normalised


def _coerce_post_type(value: PostType | str) -> PostType:
    if isinstance(value, PostType):
        return value
    try:
        return PostType(value)
    except ValueError as exc:
        raise ValueError(f"invalid post type {value!r}") from exc


def _coerce_comment_type(value: CommentType | str) -> CommentType:
    if isinstance(value, CommentType):
        return value
    try:
        return CommentType(value)
    except ValueError as exc:
        raise ValueError(f"invalid comment type {value!r}") from exc


def _validate_content(
    post_type: PostType,
    content: str | None,
    file_meta: FileMeta | None,
    location: LocationData | None = None,
    image_urls: tuple[str, ...] = (),
) -> None:
    if post_type is PostType.FILE:
        if file_meta is None:
            raise ValueError("file post requires file_meta")
    elif post_type is PostType.IMAGE:
        # Image posts must carry 1..FEED_POST_MAX_IMAGES URLs in
        # ``image_urls`` — ``media_url`` is unused for this type.
        if not image_urls:
            raise ValueError("image post requires at least one image_url")
        if len(image_urls) > FEED_POST_MAX_IMAGES:
            raise ValueError(
                f"image post may carry at most {FEED_POST_MAX_IMAGES} images",
            )
    elif post_type is PostType.VIDEO:
        # media_url check is implicit — the route uploads media first and
        # passes the URL through to create_post. Content (caption) is
        # optional but bounded.
        pass
    elif post_type is PostType.LOCATION:
        if location is None:
            raise ValueError("location post requires lat/lon")
        if location.label is not None and len(location.label) > LOCATION_LABEL_MAX:
            raise ValueError(
                f"location label exceeds {LOCATION_LABEL_MAX} characters",
            )
    elif post_type in (PostType.TEXT, PostType.TRANSCRIPT):
        if not content or not content.strip():
            raise ValueError(f"{post_type.value} post requires content")
    # Non-image post types must not carry ``image_urls`` — keeps the
    # column meaning unambiguous on the read side.
    if post_type is not PostType.IMAGE and image_urls:
        raise ValueError(
            f"{post_type.value} post must not carry image_urls",
        )
    _validate_text_length(content, limit=MAX_POST_LENGTH)


def _validate_text_length(
    content: str | None,
    *,
    limit: int,
) -> None:
    if content is None:
        return
    if len(content) > limit:
        raise ValueError(f"content exceeds maximum length of {limit} characters")
