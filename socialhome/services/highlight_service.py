"""Highlight service (§Highlights).

Orchestrates the create/append/delete/share/expire flows on top of
:class:`AbstractHighlightRepo`. Holds no SQL, no HTTP, no media byte
validation — those live in repos, routes, and the existing
``MediaValidator`` respectively. It does refuse a frame whose
``media_url`` is not a local upload (``require_local_media_ref``).

The encryption-first rule (§25.8.21) shapes the federation outbound:
this service collects the *plaintext* domain event (id, sequence,
audience, expires_at, captions, media url) and emits a
:class:`HighlightFrameAdded` (or sibling) on the bus. The
``HighlightFederationOutbound`` subscriber receives it, fans out to peer
instances based on the audience kind, and each per-peer envelope sends
the body inside the encrypted payload. See
``highlight_federation_outbound.py``.

Authorization: every read and every frame action first proves the
caller may read the highlight (:meth:`HighlightService._require_readable`)
— the inbox rule (audience, expiry, personal and guardian blocks), or a
share post of it the caller can read. A caller who may not read it gets
:class:`HighlightNotFoundError`, word for word what an unknown id gets,
so an id is never an existence oracle. A reader who is not the author
and tries to mutate it gets :class:`HighlightForbiddenError`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import orjson

from ..domain.events import (
    HighlightFrameAdded,
    HighlightFrameReactionChanged,
    HighlightFrameRemoved,
    HighlightFrameViewed,
    HighlightRemoved,
)
from ..domain.highlight import (
    Highlight,
    HighlightAudience,
    HighlightFrame,
    HighlightFrameReplySnapshot,
    HighlightFrameType,
)
from ..domain.post import Post, PostType
from ..media.cleanup import unlink_unreferenced
from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
from .inbound_media_store import require_local_media_ref
from .space_member_guard import seat_of
from .user_preferences import parse_highlights_preferences

if TYPE_CHECKING:
    import pathlib

    from ..infrastructure.event_bus import EventBus
    from ..repositories.highlight_repo import AbstractHighlightRepo
    from ..repositories.post_repo import AbstractPostRepo
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

#: Hard ceiling on frames per highlight per day. Prevents storage abuse and
#: keeps the viewer responsive on slow networks.
MAX_FRAMES_PER_HIGHLIGHT: int = 30

#: The one message an unknown id and a highlight the caller may not read
#: both answer with — the 404 body must not tell them apart.
NOT_FOUND_MESSAGE = "highlight not found"


class HighlightNotFoundError(LookupError):
    """The targeted highlight or frame does not exist."""


class HighlightForbiddenError(PermissionError):
    """The actor is not allowed to mutate this highlight / frame."""


class HighlightFrameLimitError(ValueError):
    """The author's per-day frame cap has been reached."""


class HighlightService:
    """Personal highlights: create / view / react / share / expire."""

    __slots__ = (
        "_bus",
        "_highlights",
        "_media_dir",
        "_media_refs",
        "_posts",
        "_space_posts",
        "_spaces",
        "_users",
    )

    def __init__(
        self,
        repo: AbstractHighlightRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
        *,
        media_dir: pathlib.Path | None = None,
        media_refs: AbstractMediaReferenceRepo | None = None,
        post_repo: AbstractPostRepo | None = None,
        space_post_repo: AbstractSpacePostRepo | None = None,
        space_repo: AbstractSpaceRepo | None = None,
    ) -> None:
        self._highlights = repo
        self._users = user_repo
        self._bus = bus
        # When both are set, frame media files are removed on delete /
        # expiry once no other row references them.
        self._media_dir = media_dir
        self._media_refs = media_refs
        # The share path: a ``highlight_share`` post opens the highlight
        # to whoever reads that post. Unwired, no share opens anything.
        self._posts = post_repo
        self._space_posts = space_post_repo
        self._spaces = space_repo

    # ── Read gate ──────────────────────────────────────────────────────

    async def _require_readable(
        self,
        highlight_id: str,
        viewer_user_id: str,
        via_post_id: str | None = None,
    ) -> Highlight:
        """The highlight, iff *viewer_user_id* may read it.

        Two ways in, and nothing else — an unguessable id is not one:

        * the inbox rule (:meth:`AbstractHighlightRepo.get_readable`): the
          author, or someone in the audience, while it is live and no
          personal or guardian block stands between them;
        * a share: *via_post_id* is a live ``highlight_share`` post of
          this highlight, written by its author, that the viewer can read
          (the household feed, or a space they hold a seat in — a
          follower included). The share waives only the audience; expiry
          and both block kinds still apply.

        Anything else raises :class:`HighlightNotFoundError` with
        :data:`NOT_FOUND_MESSAGE`, exactly like an unknown id.
        """
        highlight = await self._highlights.get_readable(highlight_id, viewer_user_id)
        if highlight is None and via_post_id:
            stored = await self._highlights.get_highlight(highlight_id)
            if stored is not None and await self._shared_via(
                via_post_id, stored, viewer_user_id
            ):
                highlight = await self._highlights.get_readable(
                    highlight_id, viewer_user_id, require_audience=False
                )
        if highlight is None:
            raise HighlightNotFoundError(NOT_FOUND_MESSAGE)
        return highlight

    async def _require_readable_frame(
        self,
        frame_id: str,
        viewer_user_id: str,
        via_post_id: str | None = None,
    ) -> tuple[HighlightFrame, Highlight]:
        """``(frame, highlight)`` iff the viewer may read the frame's
        highlight (:meth:`_require_readable`); the same 404 otherwise."""
        frame = await self._highlights.get_frame(frame_id)
        if frame is None:
            raise HighlightNotFoundError(NOT_FOUND_MESSAGE)
        highlight = await self._require_readable(
            frame.highlight_id, viewer_user_id, via_post_id
        )
        return frame, highlight

    async def _shared_via(
        self, post_id: str, highlight: Highlight, viewer_user_id: str
    ) -> bool:
        """Is *post_id* a live share of *highlight* by its own author that
        *viewer_user_id* can read? Only the author shares a highlight
        (:meth:`share_to_feed`), so a post by anyone else linking it —
        a forged federated one included — opens nothing."""
        post: Post | None = None
        if self._posts is not None:
            # The household feed: every local member reads it.
            post = await self._posts.get(post_id)
        if post is None and self._space_posts is not None and self._spaces:
            found = await self._space_posts.get(post_id)
            if found is not None:
                space_id, post = found
                space = await self._spaces.get(space_id)
                if (
                    space is None
                    or space.dissolved
                    or await seat_of(self._spaces, space_id, viewer_user_id) is None
                ):
                    return False
        return (
            post is not None
            and not post.deleted
            and post.type == PostType.HIGHLIGHT_SHARE
            and post.linked_highlight_id == highlight.id
            and post.author == highlight.author_user_id
        )

    # ── Create / append ─────────────────────────────────────────────────

    async def create_or_append_frame(
        self,
        *,
        author_user_id: str,
        frame_type: HighlightFrameType | str,
        media_url: str,
        caption_text: str | None = None,
        caption_emoji: str | None = None,
        duration_ms: int | None = None,
        audience_kind: HighlightAudience | str | None = None,
        audience: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[Highlight, HighlightFrame]:
        """Find or create today's highlight for *author_user_id* and append a frame.

        Returns ``(highlight, frame)``. Publishes :class:`HighlightFrameAdded`
        with ``is_first_frame=True`` for the very first frame of the day
        (so federation outbound can pick the right
        :class:`FederationEventType`). Subsequent frames the same day
        re-use the existing :class:`Highlight` row.
        """
        # Local uploads only — before anything is written, so a refused
        # frame leaves no empty highlight behind. A remote URL would leak
        # every viewer's IP, and receivers drop it (``_frame_from_payload``).
        media_url = require_local_media_ref(media_url)
        author = await self._users.get_by_user_id(author_user_id)
        if author is None:
            raise LookupError(f"author {author_user_id!r} not found")
        prefs = parse_highlights_preferences(author.preferences_json)

        # Default audience flows from the author's preferences when the
        # caller did not pin one explicitly.
        if audience_kind is None:
            kind = prefs.default_audience_kind
            ids: tuple[str, ...] = prefs.default_audience
        else:
            kind = (
                audience_kind
                if isinstance(audience_kind, HighlightAudience)
                else HighlightAudience(audience_kind)
            )
            ids = tuple(audience or ())
            if kind is HighlightAudience.ALL_PAIRED:
                ids = ()

        ftype = (
            frame_type
            if isinstance(frame_type, HighlightFrameType)
            else HighlightFrameType(frame_type)
        )

        now = datetime.now(UTC)
        highlight_date = now.strftime("%Y-%m-%d")
        expires_at = (now + timedelta(days=prefs.retention_days)).isoformat()

        highlight = await self._highlights.find_or_create_today(
            author_user_id=author_user_id,
            audience_kind=kind,
            audience=ids,
            highlight_date=highlight_date,
            expires_at=expires_at,
        )

        # Cap frames per highlight per day.
        existing = await self._highlights.list_frames(highlight.id)
        is_first = len(existing) == 0
        if len(existing) >= MAX_FRAMES_PER_HIGHLIGHT:
            raise HighlightFrameLimitError(
                f"highlight already has {MAX_FRAMES_PER_HIGHLIGHT} frames today"
            )

        frame = await self._highlights.append_frame(
            highlight_id=highlight.id,
            frame_type=ftype,
            media_url=media_url,
            caption_text=caption_text,
            caption_emoji=caption_emoji,
            duration_ms=duration_ms,
        )

        await self._bus.publish(
            HighlightFrameAdded(
                highlight_id=highlight.id,
                frame_id=frame.id,
                author_user_id=author_user_id,
                highlight_date=highlight.highlight_date,
                sequence=frame.sequence,
                is_first_frame=is_first,
                audience_kind=highlight.audience_kind.value,
                audience=highlight.audience,
                frame_type=ftype.value,
                media_url=media_url,
                caption_text=caption_text,
                caption_emoji=caption_emoji,
                duration_ms=duration_ms,
                expires_at=highlight.expires_at or expires_at,
            )
        )
        return highlight, frame

    # ── View / react ───────────────────────────────────────────────────

    async def mark_frame_viewed(
        self,
        *,
        frame_id: str,
        viewer_user_id: str,
        via_post_id: str | None = None,
    ) -> None:
        frame, highlight = await self._require_readable_frame(
            frame_id, viewer_user_id, via_post_id
        )
        if highlight.author_user_id == viewer_user_id:
            # Authors don't accumulate views on their own frames.
            return
        if not await self._highlights.mark_viewed(frame_id, viewer_user_id):
            # Seen before: the author already has this receipt (and a remote
            # author's household its HIGHLIGHT_FRAME_VIEWED) — re-viewing a
            # highlight sends nothing.
            return
        await self._bus.publish(
            HighlightFrameViewed(
                highlight_id=highlight.id,
                frame_id=frame.id,
                viewer_user_id=viewer_user_id,
                author_user_id=highlight.author_user_id,
            )
        )

    async def react_to_frame(
        self,
        *,
        frame_id: str,
        reactor_user_id: str,
        emoji: str,
        via_post_id: str | None = None,
    ) -> None:
        frame, highlight = await self._require_readable_frame(
            frame_id, reactor_user_id, via_post_id
        )
        await self._highlights.set_reaction(frame_id, reactor_user_id, emoji)
        await self._bus.publish(
            HighlightFrameReactionChanged(
                highlight_id=highlight.id,
                frame_id=frame.id,
                reactor_user_id=reactor_user_id,
                author_user_id=highlight.author_user_id,
                emoji=emoji,
            )
        )

    async def clear_reaction(
        self,
        *,
        frame_id: str,
        reactor_user_id: str,
        via_post_id: str | None = None,
    ) -> None:
        frame, highlight = await self._require_readable_frame(
            frame_id, reactor_user_id, via_post_id
        )
        await self._highlights.clear_reaction(frame_id, reactor_user_id)
        await self._bus.publish(
            HighlightFrameReactionChanged(
                highlight_id=highlight.id,
                frame_id=frame.id,
                reactor_user_id=reactor_user_id,
                author_user_id=highlight.author_user_id,
                emoji=None,
            )
        )

    # ── Delete ─────────────────────────────────────────────────────────

    async def delete_frame(
        self,
        *,
        frame_id: str,
        actor_user_id: str,
    ) -> None:
        frame, highlight = await self._require_readable_frame(frame_id, actor_user_id)
        if highlight.author_user_id != actor_user_id:
            raise HighlightForbiddenError("only the author can delete their frame")
        await self._highlights.delete_frame(frame_id)
        await unlink_unreferenced(self._media_dir, self._media_refs, [frame.media_url])
        await self._bus.publish(
            HighlightFrameRemoved(
                highlight_id=highlight.id,
                frame_id=frame_id,
                author_user_id=highlight.author_user_id,
                audience_kind=highlight.audience_kind.value,
                audience=highlight.audience,
            )
        )

    async def delete_highlight(
        self,
        *,
        highlight_id: str,
        actor_user_id: str,
    ) -> None:
        highlight = await self._require_readable(highlight_id, actor_user_id)
        if highlight.author_user_id != actor_user_id:
            raise HighlightForbiddenError("only the author can delete their highlight")
        # Collect frame media before the cascade delete so we can drop the
        # files too (deleting the highlight row cascades the frame rows).
        frame_urls = (
            [f.media_url for f in await self._highlights.list_frames(highlight_id)]
            if self._media_dir is not None
            else []
        )
        await self._highlights.delete_highlight(highlight_id)
        await unlink_unreferenced(self._media_dir, self._media_refs, frame_urls)
        await self._bus.publish(
            HighlightRemoved(
                highlight_id=highlight.id,
                author_user_id=highlight.author_user_id,
                audience_kind=highlight.audience_kind.value,
                audience=highlight.audience,
            )
        )

    # ── Read ───────────────────────────────────────────────────────────

    async def list_visible(self, viewer_user_id: str) -> list[dict[str, Any]]:
        """Return the inbox view: highlights visible to *viewer_user_id*.

        Each entry is a dict ready for the route layer to serialise:
        ``{highlight, frames, unseen_count}`` — frames are
        :class:`HighlightFrame` instances, ``unseen_count`` is an ``int``.
        Sorted newest-first by ``highlight_date``.
        """
        highlights = await self._highlights.list_visible_to(viewer_user_id)
        out: list[dict[str, Any]] = []
        for highlight in highlights:
            frames = await self._highlights.list_frames(highlight.id)
            # Your own highlight is never unseen: authors' views are not
            # recorded (``mark_frame_viewed``), so counting them would
            # keep the "new" ring and badge on your own highlight forever.
            unseen = (
                0
                if highlight.author_user_id == viewer_user_id
                else await self._highlights.count_unseen_frames(
                    highlight.id, viewer_user_id
                )
            )
            out.append(
                {
                    "highlight": highlight,
                    "frames": frames,
                    "unseen_count": unseen,
                }
            )
        return out

    async def get_for_viewer(
        self,
        highlight_id: str,
        viewer_user_id: str,
        *,
        via_post_id: str | None = None,
    ) -> tuple[Highlight, list[HighlightFrame]]:
        """The detail read: ``(highlight, frames)`` iff the viewer may read
        it (:meth:`_require_readable`), else the unknown-id 404."""
        highlight = await self._require_readable(
            highlight_id, viewer_user_id, via_post_id
        )
        return highlight, await self._highlights.list_frames(highlight.id)

    async def get_frame(self, frame_id: str) -> HighlightFrame | None:
        return await self._highlights.get_frame(frame_id)

    # ── Share into a feed ──────────────────────────────────────────────

    async def share_to_feed(
        self,
        *,
        highlight_id: str,
        actor_user_id: str,
        scope: str,  # 'household' | 'space'
        space_id: str | None,
        note: str | None,
        feed_service: Any,
        space_service: Any,
    ) -> Any:
        """Create a ``highlight_share`` post in the household or space feed.

        The route layer threads the existing feed/space services in via
        the keyword arguments — keeps :class:`HighlightService` from holding
        a hard dependency on either service module while still avoiding
        SQL in the service.
        """
        highlight = await self._require_readable(highlight_id, actor_user_id)
        # Only the author can share their own highlight (avoids re-sharing
        # someone else's content outside of the audience the author
        # picked).
        if highlight.author_user_id != actor_user_id:
            raise HighlightForbiddenError(
                "only the author of a highlight can share it into a feed"
            )
        body = (note or "").strip() or None
        if scope == "household":
            return await feed_service.create_post(
                author_user_id=actor_user_id,
                type="highlight_share",
                content=body,
                linked_highlight_id=highlight.id,
            )
        if scope == "space":
            if not space_id:
                raise ValueError("space_id is required when scope='space'")
            return await space_service.create_post(
                space_id=space_id,
                author_user_id=actor_user_id,
                type="highlight_share",
                content=body,
                linked_highlight_id=highlight.id,
            )
        raise ValueError(f"unknown share scope: {scope!r}")

    # ── DM reply to a frame ────────────────────────────────────────────

    async def dm_reply_to_frame(
        self,
        *,
        frame_id: str,
        sender_user_id: str,
        conversation_id: str,
        content: str,
        dm_service: Any,
        via_post_id: str | None = None,
    ) -> Any:
        """Send a DM that quotes a highlight frame.

        Reuses the existing DM send pipeline; the snapshot is frozen on
        the message (as a JSON blob) so the reply stays meaningful
        after retention purges the underlying frame.
        """
        frame, highlight = await self._require_readable_frame(
            frame_id, sender_user_id, via_post_id
        )
        sender = await self._users.get_by_user_id(sender_user_id)
        if sender is None:
            raise LookupError(f"sender {sender_user_id!r} not found")
        snapshot = HighlightFrameReplySnapshot(
            thumb_url=frame.media_url,
            author_user_id=highlight.author_user_id,
            highlight_date=highlight.highlight_date,
            caption_text=frame.caption_text,
            caption_emoji=frame.caption_emoji,
        )
        snapshot_json = orjson.dumps(
            {
                "thumb_url": snapshot.thumb_url,
                "author_user_id": snapshot.author_user_id,
                "highlight_date": snapshot.highlight_date,
                "caption_text": snapshot.caption_text,
                "caption_emoji": snapshot.caption_emoji,
            },
            option=orjson.OPT_SORT_KEYS,
        ).decode()
        return await dm_service.send_message(
            conversation_id,
            sender_username=sender.username,
            content=content,
            reply_to_highlight_frame_id=frame.id,
            reply_to_highlight_frame_snapshot=snapshot_json,
        )

    # ── Retention ──────────────────────────────────────────────────────

    async def expire_due(self) -> tuple[int, int]:
        """Run one retention pass.

        Two phases:
          1. ``prune_expired`` — drop highlights past their author-set
             retention cutoff.
          2. ``prune_over_max`` per author — once the author has more
             than their ``max_count`` highlights the oldest are dropped.

        Returns ``(expired_count, over_max_count)`` for telemetry. Safe
        to call as often as the scheduler ticks; the underlying queries
        are idempotent.
        """
        media_urls: list[str] = []
        if self._media_dir is not None:
            media_urls.extend(await self._highlights.list_expired_frame_media_urls())
        expired = await self._highlights.prune_expired()
        over_max = 0
        authors = await self._highlights.list_authors_with_highlights()
        for author_user_id in authors:
            user = await self._users.get_by_user_id(author_user_id)
            prefs = parse_highlights_preferences(
                user.preferences_json if user is not None else None
            )
            if self._media_dir is not None:
                media_urls.extend(
                    await self._highlights.list_over_max_frame_media_urls(
                        author_user_id, prefs.max_count
                    )
                )
            over_max += await self._highlights.prune_over_max(
                author_user_id, prefs.max_count
            )
        await unlink_unreferenced(self._media_dir, self._media_refs, media_urls)
        return expired, over_max


__all__ = [
    "MAX_FRAMES_PER_HIGHLIGHT",
    "NOT_FOUND_MESSAGE",
    "HighlightForbiddenError",
    "HighlightFrameLimitError",
    "HighlightNotFoundError",
    "HighlightService",
]


# Used by tests / CLI to mint deterministic ids when needed; main code
# uses uuid4 inside the repos.
def _new_id() -> str:
    return uuid.uuid4().hex
