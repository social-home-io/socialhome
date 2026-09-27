"""Highlight service (§Highlights).

Orchestrates the create/append/delete/share/expire flows on top of
:class:`AbstractHighlightRepo`. Holds no SQL, no HTTP, no media validation —
those live in repos, routes, and the existing ``MediaValidator``
respectively.

The encryption-first rule (§25.8.21) shapes the federation outbound:
this service collects the *plaintext* domain event (id, sequence,
audience, expires_at, captions, media url) and emits a
:class:`HighlightFrameAdded` (or sibling) on the bus. The
``HighlightFederationOutbound`` subscriber receives it, fans out to peer
instances based on the audience kind, and each per-peer envelope sends
the body inside the encrypted payload. See
``highlight_federation_outbound.py``.

Authorization: callers are responsible for verifying the
``actor_user_id`` matches the resource owner (route layer does this via
:class:`BaseView`). The service still raises :class:`PermissionError`
when an actor tries to mutate someone else's frame / highlight so any
mis-wired caller can't do silent damage.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
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
from ..media.cleanup import unlink_unreferenced
from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
from .user_preferences import parse_highlights_preferences

if TYPE_CHECKING:
    import pathlib

    from ..infrastructure.event_bus import EventBus
    from ..repositories.highlight_repo import AbstractHighlightRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

#: Hard ceiling on frames per highlight per day. Prevents storage abuse and
#: keeps the viewer responsive on slow networks.
MAX_FRAMES_PER_HIGHLIGHT: int = 30


class HighlightNotFoundError(LookupError):
    """The targeted highlight or frame does not exist."""


class HighlightForbiddenError(PermissionError):
    """The actor is not allowed to mutate this highlight / frame."""


class HighlightFrameLimitError(ValueError):
    """The author's per-day frame cap has been reached."""


class HighlightService:
    """Personal highlights: create / view / react / share / expire."""

    __slots__ = ("_highlights", "_users", "_bus", "_media_dir", "_media_refs")

    def __init__(
        self,
        repo: "AbstractHighlightRepo",
        user_repo: "AbstractUserRepo",
        bus: "EventBus",
        *,
        media_dir: "pathlib.Path | None" = None,
        media_refs: AbstractMediaReferenceRepo | None = None,
    ) -> None:
        self._highlights = repo
        self._users = user_repo
        self._bus = bus
        # When both are set, frame media files are removed on delete /
        # expiry once no other row references them.
        self._media_dir = media_dir
        self._media_refs = media_refs

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

        now = datetime.now(timezone.utc)
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
    ) -> None:
        frame = await self._highlights.get_frame(frame_id)
        if frame is None:
            raise HighlightNotFoundError(frame_id)
        highlight = await self._highlights.get_highlight(frame.highlight_id)
        if highlight is None:
            raise HighlightNotFoundError(frame.highlight_id)
        if highlight.author_user_id == viewer_user_id:
            # Authors don't accumulate views on their own frames.
            return
        await self._highlights.mark_viewed(frame_id, viewer_user_id)
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
    ) -> None:
        frame = await self._highlights.get_frame(frame_id)
        if frame is None:
            raise HighlightNotFoundError(frame_id)
        highlight = await self._highlights.get_highlight(frame.highlight_id)
        if highlight is None:
            raise HighlightNotFoundError(frame.highlight_id)
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
    ) -> None:
        frame = await self._highlights.get_frame(frame_id)
        if frame is None:
            raise HighlightNotFoundError(frame_id)
        highlight = await self._highlights.get_highlight(frame.highlight_id)
        if highlight is None:
            raise HighlightNotFoundError(frame.highlight_id)
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
        frame = await self._highlights.get_frame(frame_id)
        if frame is None:
            raise HighlightNotFoundError(frame_id)
        highlight = await self._highlights.get_highlight(frame.highlight_id)
        if highlight is None:
            raise HighlightNotFoundError(frame.highlight_id)
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
        highlight = await self._highlights.get_highlight(highlight_id)
        if highlight is None:
            raise HighlightNotFoundError(highlight_id)
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
            unseen = await self._highlights.count_unseen_frames(
                highlight.id, viewer_user_id
            )
            out.append(
                {
                    "highlight": highlight,
                    "frames": frames,
                    "unseen_count": unseen,
                }
            )
        return out

    async def get_with_frames(
        self,
        highlight_id: str,
    ) -> tuple[Highlight, list[HighlightFrame]] | None:
        highlight = await self._highlights.get_highlight(highlight_id)
        if highlight is None:
            return None
        frames = await self._highlights.list_frames(highlight_id)
        return highlight, frames

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
        highlight = await self._highlights.get_highlight(highlight_id)
        if highlight is None:
            raise HighlightNotFoundError(highlight_id)
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
    ) -> Any:
        """Send a DM that quotes a highlight frame.

        Reuses the existing DM send pipeline; the snapshot is frozen on
        the message (as a JSON blob) so the reply stays meaningful
        after retention purges the underlying frame.
        """
        frame = await self._highlights.get_frame(frame_id)
        if frame is None:
            raise HighlightNotFoundError(frame_id)
        highlight = await self._highlights.get_highlight(frame.highlight_id)
        if highlight is None:
            raise HighlightNotFoundError(frame.highlight_id)
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
    "HighlightForbiddenError",
    "HighlightFrameLimitError",
    "HighlightNotFoundError",
    "HighlightService",
]


# Used by tests / CLI to mint deterministic ids when needed; main code
# uses uuid4 inside the repos.
def _new_id() -> str:
    return uuid.uuid4().hex
