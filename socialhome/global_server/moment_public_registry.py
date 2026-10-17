"""GFS moment-public registry (§Momentum-public).

Three responsibilities:

1. **User registration** — keep the public directory of opted-in users
   and the home-instance public key the GFS denormalises so any
   follower can verify the author's per-moment signature without
   round-tripping back to this registry.
2. **Follow graph** — track which instances/users follow each
   registered author. The fan-out path reads the followers-of(author)
   set on every incoming ``moment_public`` frame.
3. **Fan-out** — given a signed envelope from the author's instance,
   look up the followers and push an ``incoming_public_moment`` WS
   frame to each unique follower instance. The registry holds zero
   moment bytes — the fan-out is in-memory only, then forgotten.

This module *only* wires the data model + push primitives. The HTTP
routes live in ``routes/moments_public.py`` and the WS frame handler
hooks live in the GFS WS endpoint.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .domain import GfsMomentFollow, GfsUserRegistration
from .repositories import (
    AbstractGfsMomentFollowRepo,
    AbstractGfsUserRegistrationRepo,
)
from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)


class MomentPublicRegistry:
    """Per-GFS facade over registrations + follows + fan-out."""

    __slots__ = ("_follows", "_users", "_ws_registry")

    def __init__(
        self,
        users: AbstractGfsUserRegistrationRepo,
        follows: AbstractGfsMomentFollowRepo,
        ws_registry: GfsWebSocketRegistry,
    ) -> None:
        self._users = users
        self._follows = follows
        self._ws_registry = ws_registry

    # ── Registration ────────────────────────────────────────────────────

    async def register_user(
        self,
        *,
        user_id: str,
        instance_id: str,
        username: str,
        display_name: str,
        home_instance_pk: str,
        picture_url: str | None = None,
        bio: str | None = None,
        picture_digest: str | None = None,
    ) -> GfsUserRegistration:
        reg = GfsUserRegistration(
            user_id=user_id,
            instance_id=instance_id,
            username=username,
            display_name=display_name,
            picture_url=picture_url,
            home_instance_pk=home_instance_pk,
            registered_at=int(time.time()),
            status="active",
            bio=bio,
            picture_digest=picture_digest,
        )
        await self._users.upsert(reg)
        return reg

    async def deregister_user(self, user_id: str) -> bool:
        deleted = await self._users.delete(user_id)
        return deleted > 0

    async def get_registration(self, user_id: str) -> GfsUserRegistration | None:
        return await self._users.get(user_id)

    async def list_directory(
        self, *, q: str | None = None, limit: int = 200
    ) -> list[GfsUserRegistration]:
        return await self._users.list_active(q=q, limit=limit)

    async def set_picture_digest(
        self, *, user_id: str, picture_digest: str | None
    ) -> None:
        await self._users.set_picture_digest(
            user_id=user_id, picture_digest=picture_digest
        )

    # ── Follow graph ────────────────────────────────────────────────────

    async def add_follow(
        self,
        *,
        follower_user_id: str,
        follower_instance_id: str,
        followed_user_id: str,
    ) -> GfsMomentFollow:
        # Refuse to record a follow against an unknown / suspended
        # author so the directory stays the source of truth.
        target = await self._users.get(followed_user_id)
        if target is None or target.status != "active":
            raise LookupError(
                f"author {followed_user_id!r} not registered or suspended"
            )
        follow = GfsMomentFollow(
            follower_user_id=follower_user_id,
            follower_instance_id=follower_instance_id,
            followed_user_id=followed_user_id,
            created_at=int(time.time()),
        )
        await self._follows.upsert(follow)
        # Notify the author's instance so the UI follower count ticks.
        await self._notify_author(target.instance_id, follow, action="add")
        return follow

    async def remove_follow(
        self,
        *,
        follower_user_id: str,
        followed_user_id: str,
    ) -> bool:
        deleted = await self._follows.delete(
            follower_user_id=follower_user_id,
            followed_user_id=followed_user_id,
        )
        if deleted == 0:
            return False
        target = await self._users.get(followed_user_id)
        if target is not None:
            await self._notify_author(
                target.instance_id,
                GfsMomentFollow(
                    follower_user_id=follower_user_id,
                    # We've already deleted the row; reuse a synthetic
                    # follow record to carry the notify payload.
                    follower_instance_id="",
                    followed_user_id=followed_user_id,
                    created_at=int(time.time()),
                ),
                action="remove",
            )
        return True

    async def follower_count(self, followed_user_id: str) -> int:
        return await self._follows.follower_count(followed_user_id)

    # ── Fan-out ─────────────────────────────────────────────────────────

    async def fan_out_moment(
        self,
        *,
        envelope: dict[str, Any],
    ) -> int:
        """Push a signed ``moment_public`` envelope to every follower
        instance and return the number of unique instances reached.

        ``envelope`` must already be a dict ready to ship over the WS;
        the GFS forwards it verbatim as the ``payload`` of an
        ``incoming_public_moment`` frame so the recipient verifies the
        author's signature directly.
        """
        author = envelope.get("author_user_id")
        if not author:
            log.warning("moment_public: envelope missing author_user_id")
            return 0
        followers = await self._follows.followers_of(author)
        seen: set[str] = set()
        delivered = 0
        for f in followers:
            if f.follower_instance_id in seen:
                continue
            seen.add(f.follower_instance_id)
            ok = await self._ws_registry.send(
                f.follower_instance_id,
                {"type": "incoming_public_moment", "payload": envelope},
            )
            if ok:
                delivered += 1
        log.info(
            "moment_public.fanout: author=%s followers=%d delivered=%d",
            author,
            len(followers),
            delivered,
        )
        return delivered

    async def fan_out_delete(
        self,
        *,
        envelope: dict[str, Any],
    ) -> int:
        author = envelope.get("author_user_id")
        if not author:
            return 0
        followers = await self._follows.followers_of(author)
        seen: set[str] = set()
        delivered = 0
        for f in followers:
            if f.follower_instance_id in seen:
                continue
            seen.add(f.follower_instance_id)
            ok = await self._ws_registry.send(
                f.follower_instance_id,
                {"type": "incoming_public_moment_delete", "payload": envelope},
            )
            if ok:
                delivered += 1
        return delivered

    # ── Internal ────────────────────────────────────────────────────────

    async def _notify_author(
        self,
        author_instance_id: str,
        follow: GfsMomentFollow,
        *,
        action: str,
    ) -> None:
        await self._ws_registry.send(
            author_instance_id,
            {
                "type": "follow_changed",
                "action": action,
                "follower_user_id": follow.follower_user_id,
                "follower_instance_id": follow.follower_instance_id,
                "followed_user_id": follow.followed_user_id,
            },
        )
