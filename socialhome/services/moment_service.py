"""Momentum service — household-broadcast posts (§Momentum).

Orchestrates :class:`AbstractMomentRepo`, :class:`AbstractUserRepo`,
and :class:`PreferencesService` to enforce the spec rules:

* ≤ 1 000 chars text, optional image or ≤ 15-second video.
* 1 top-level moment per author per 15 minutes (replies + reactions
  exempt — otherwise back-and-forth threads would die on the timer).
* Replies are themselves moments; ``parent_moment_id`` links them.
* Absolute 7-day retention on disk; the visibility query collapses
  this to 24 h for non-followers.

The service publishes :class:`MomentCreated` / :class:`MomentDeleted`
/ :class:`MomentReactionChanged` on the bus so federation outbound +
realtime push pick them up.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..federation.owner_bound_id import MOMENT_KIND, mint_owner_bound_id
from ..domain.events import (
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
)
from ..domain.moment import (
    MOMENT_MAX_CONTENT_LEN,
    MOMENT_MAX_VIDEO_MS,
    MOMENT_RETENTION_DAYS,
    Moment,
)
from ..media.cleanup import unlink_unreferenced
from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
from .user_preferences import parse_moment_preferences

if TYPE_CHECKING:
    import pathlib

    from ..infrastructure.event_bus import EventBus
    from ..repositories.moment_repo import AbstractMomentRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


#: Minimum interval between two top-level moments by the same author.
#: Replies and reactions are exempt — see ``create_moment``.
MOMENT_RATE_WINDOW = timedelta(minutes=15)


class MomentNotFoundError(KeyError):
    """Raised when a moment id can't be found locally."""


class MomentRateLimitError(Exception):
    """Raised when an author tries to post a top-level moment within
    :data:`MOMENT_RATE_WINDOW` of their last one. Mapped to HTTP 429
    in :class:`BaseView._iter`.
    """


class MomentService:
    """Create + react + delete moments and drive their lifecycle."""

    __slots__ = (
        "_moments",
        "_users",
        "_bus",
        "_own_instance_id",
        "_media_dir",
        "_media_refs",
    )

    def __init__(
        self,
        moment_repo: "AbstractMomentRepo",
        user_repo: "AbstractUserRepo",
        bus: "EventBus",
        *,
        own_instance_id: str = "",
        media_dir: "pathlib.Path | None" = None,
        media_refs: AbstractMediaReferenceRepo | None = None,
    ) -> None:
        self._moments = moment_repo
        self._users = user_repo
        self._bus = bus
        self._own_instance_id = own_instance_id
        # When both are set, the backing media file is removed on delete /
        # expiry once no other row references it.
        self._media_dir = media_dir
        self._media_refs = media_refs

    def attach_instance_id(self, own_instance_id: str) -> None:
        """Late binding for the instance id (set after federation
        identity comes online during app startup)."""
        self._own_instance_id = own_instance_id

    # ── Create ─────────────────────────────────────────────────────────

    async def create_moment(
        self,
        *,
        author_user_id: str,
        content: str,
        media_url: str | None = None,
        media_type: str | None = None,
        duration_ms: int | None = None,
        parent_moment_id: str | None = None,
        is_public: bool = False,
    ) -> Moment:
        content = (content or "").strip()
        if len(content) > MOMENT_MAX_CONTENT_LEN:
            raise ValueError(
                f"Moment text exceeds {MOMENT_MAX_CONTENT_LEN} characters."
            )
        if not content and media_url is None:
            raise ValueError("A moment needs text or media — both can't be empty.")
        if media_type is not None and media_type not in ("image", "video"):
            raise ValueError(f"Unsupported media_type: {media_type!r}")
        if media_type == "video":
            if duration_ms is None or duration_ms <= 0:
                raise ValueError("Video moments must include duration_ms.")
            if duration_ms > MOMENT_MAX_VIDEO_MS:
                raise ValueError(
                    f"Video moments cap at {MOMENT_MAX_VIDEO_MS // 1000} seconds."
                )
        if media_type != "video":
            duration_ms = None

        parent_author_user_id: str | None = None
        if parent_moment_id is not None:
            parent = await self._moments.get(parent_moment_id)
            if parent is None:
                raise MomentNotFoundError(parent_moment_id)
            # Threads stay flat: a reply to a reply attaches to the
            # original root so list_replies(root) returns the whole
            # conversation in a single query.
            if parent.parent_moment_id is not None:
                root = await self._moments.get(parent.parent_moment_id)
                parent_moment_id = parent.parent_moment_id
                parent_author_user_id = (
                    root.author_user_id if root is not None else parent.author_user_id
                )
            else:
                parent_author_user_id = parent.author_user_id

        # Rate-limit only top-level moments (replies are exempt).
        if parent_moment_id is None:
            since = datetime.now(timezone.utc) - MOMENT_RATE_WINDOW
            recent = await self._moments.count_recent_for_author(
                author_user_id,
                since_iso=since.isoformat(),
            )
            if recent >= 1:
                raise MomentRateLimitError(
                    "You can post one moment every 15 minutes. "
                    "Replies and reactions don't count."
                )

        now = datetime.now(timezone.utc)
        moment = Moment(
            # Owner-bound (v_36): no other household can announce this id
            # first — as a moment or a delete — for its own user.
            id=mint_owner_bound_id(
                MOMENT_KIND, space_id="", owner_user_id=author_user_id
            ),
            author_user_id=author_user_id,
            content=content,
            media_url=media_url,
            media_type=media_type,
            duration_ms=duration_ms,
            parent_moment_id=parent_moment_id,
            origin_instance_id=self._own_instance_id,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=MOMENT_RETENTION_DAYS)).isoformat(),
            is_public=is_public,
            received_via="self",
        )
        await self._moments.save(moment)
        await self._bus.publish(
            MomentCreated(
                moment_id=moment.id,
                author_user_id=moment.author_user_id,
                content=moment.content,
                media_url=moment.media_url,
                media_type=moment.media_type,
                duration_ms=moment.duration_ms,
                parent_moment_id=moment.parent_moment_id,
                parent_author_user_id=parent_author_user_id,
                origin_instance_id=moment.origin_instance_id,
                expires_at=moment.expires_at,
            )
        )
        return moment

    # ── Delete ─────────────────────────────────────────────────────────

    async def delete_moment(
        self,
        moment_id: str,
        *,
        actor_user_id: str,
        actor_is_admin: bool = False,
    ) -> None:
        moment = await self._require(moment_id)
        if moment.author_user_id != actor_user_id and not actor_is_admin:
            raise PermissionError("Only the author or an admin can delete this moment.")
        await self._moments.delete(moment_id)
        await unlink_unreferenced(self._media_dir, self._media_refs, [moment.media_url])
        await self._bus.publish(
            MomentDeleted(
                moment_id=moment_id,
                author_user_id=moment.author_user_id,
                origin_instance_id=moment.origin_instance_id,
            )
        )

    # ── Reactions ──────────────────────────────────────────────────────

    async def react(
        self,
        moment_id: str,
        *,
        reactor_user_id: str,
        emoji: str,
    ) -> None:
        if not emoji:
            raise ValueError("Reaction emoji is required.")
        moment = await self._require(moment_id)
        await self._moments.set_reaction(moment_id, reactor_user_id, emoji)
        await self._bus.publish(
            MomentReactionChanged(
                moment_id=moment_id,
                reactor_user_id=reactor_user_id,
                author_user_id=moment.author_user_id,
                emoji=emoji,
            )
        )

    async def clear_reaction(
        self,
        moment_id: str,
        *,
        reactor_user_id: str,
    ) -> None:
        moment = await self._require(moment_id)
        await self._moments.clear_reaction(moment_id, reactor_user_id)
        await self._bus.publish(
            MomentReactionChanged(
                moment_id=moment_id,
                reactor_user_id=reactor_user_id,
                author_user_id=moment.author_user_id,
                emoji=None,
            )
        )

    # ── Reads ──────────────────────────────────────────────────────────

    async def get_moment(self, moment_id: str) -> Moment:
        return await self._require(moment_id)

    async def list_inbox(
        self,
        *,
        viewer_user_id: str,
        before: str | None = None,
        limit: int = 50,
        tag: str | None = None,
    ) -> list[Moment]:
        # Per-user max-hops visibility (§Momentum-relay-policy):
        # parse the viewer's preferences once and forward the cap to
        # the repo so the inbox query hides moments that arrived at
        # a higher hop than the user wants to see.
        viewer = await self._users.get_by_user_id(viewer_user_id)
        prefs = parse_moment_preferences(
            viewer.preferences_json if viewer is not None else None
        )
        return await self._moments.list_visible_to(
            viewer_user_id,
            before=before,
            limit=limit,
            tag=tag,
            max_hops=prefs.max_hops,
        )

    async def list_replies(self, moment_id: str) -> list[Moment]:
        return await self._moments.list_replies(moment_id)

    async def list_reactions(self, moment_id: str):
        return await self._moments.list_reactions(moment_id)

    async def list_top_hashtags(
        self,
        *,
        viewer_user_id: str,
        limit: int = 20,
    ) -> list[dict]:
        """Trending tags inside the viewer's visibility window.

        The repo aggregates with the same block + retention filter as
        :meth:`list_inbox`, so a blocked author or an out-of-window
        moment doesn't pollute the chip row.
        """
        rows = await self._moments.list_top_hashtags(
            viewer_user_id,
            limit=limit,
        )
        return [{"tag": tag, "count": n} for tag, n in rows]

    # ── Retention scheduler hook ───────────────────────────────────────

    async def expire_due(self) -> int:
        """Drop moments past the absolute 7-day cap. Reactions cascade."""
        # Collect the backing media URLs before the prune so we can drop
        # the files too (the row delete alone would orphan them on disk).
        urls = (
            await self._moments.list_expired_media_urls()
            if self._media_dir is not None
            else []
        )
        n = await self._moments.prune_expired()
        if n:
            log.info("momentum: pruned %d expired moments", n)
        await unlink_unreferenced(self._media_dir, self._media_refs, urls)
        return n

    # ── Internal helpers ───────────────────────────────────────────────

    async def _require(self, moment_id: str) -> Moment:
        m = await self._moments.get(moment_id)
        if m is None:
            raise MomentNotFoundError(moment_id)
        return m
