"""When a seed holder's relay copy of a member's public post is redundant.

A member household publishes its own post over the GFS (``space_item``,
v_49; to every GFS the owner listed since v_57). Every seed holder of the
space also relays the same post as ``space_post_public``
(:meth:`SpacePublicOutbound._relay_remote_authored`), because a follower on
an older build reads only that copy. That is one extra fan-out of every
post per seed holder.

On a given GFS the copy can be skipped only when all of these hold
(owner decision 2026-10-23, option A of finding #3):

* the author's household is at v_57 or later (it publishes to every
  listed server, anonymously under the writer group key);
* the server is on the owner's signed list for the space (where the
  author publishes);
* the server proved ``member_publish_trusted`` and ``member_publish_strict``
  (it carries that publish) and ``space_item_followers`` (it answers the
  question below) under its signed capability block;
* the server says no follower there lacks ``space_item``: the authority-
  signed ``GET /gfs/spaces/{id}/subscribers?summary=1`` reports
  ``without_space_item == 0`` (GFS migration 0023, from each follower's own
  ``accepts``).

Anything unknown — no list, an unreachable server, a refused query, an
author we can't judge — keeps the relay (fail-safe toward the copy). The
summary is cached briefly per (space, server), so a burst of posts asks once.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING

import aiohttp

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
    sign_authority_event,
)
from ..domain.federation_capabilities import FederationCapability

if TYPE_CHECKING:
    from ..domain.federation import GfsConnection
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_connection_service import GfsConnectionService

log = logging.getLogger(__name__)

#: How long a server's "no follower lacks space_item" answer is trusted.
SUMMARY_TTL_S: float = 60.0

#: Most (space, server) answers kept; past it the cache is cleared.
_MAX_SUMMARIES: int = 4096

#: ``(instance_id, min_version) -> bool`` — ``FederationService.peer_supports``.
PeerSupports = Callable[..., Awaitable[bool]]

#: Per target connection: ``True`` when the relay copy may be skipped there.
SkipTarget = Callable[["GfsConnection"], Awaitable[bool]]


class SpaceRelayCoverage:
    """Decides, per GFS, whether a remote member's post needs our copy."""

    __slots__ = ("_gfs", "_peer_supports", "_spaces", "_summaries")

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        gfs_service: GfsConnectionService,
        peer_supports: PeerSupports | None = None,
    ) -> None:
        self._spaces = space_repo
        self._gfs = gfs_service
        #: ``FederationService.peer_supports`` — wired once the federation
        #: stack exists. Until then nothing is skipped.
        self._peer_supports = peer_supports
        #: ``(space_id, conn id) -> (monotonic time, covered)``.
        self._summaries: dict[tuple[str, str], tuple[float, bool]] = {}

    def attach_peer_supports(self, peer_supports: PeerSupports) -> None:
        """Wire ``FederationService.peer_supports`` (the author's version)."""
        self._peer_supports = peer_supports

    async def redundant_on(
        self, space_id: str, author_instance_id: str | None
    ) -> SkipTarget | None:
        """The per-server test for this post, or ``None`` when the copy
        must go everywhere (an author below v_57 or unknown, no owner list,
        no seed). Never raises."""
        if not author_instance_id or self._peer_supports is None:
            return None
        try:
            if not await self._peer_supports(
                author_instance_id,
                min_version=FederationCapability.MIN_FOR_MULTI_GFS_PUBLISH,
            ):
                log.debug(
                    "relay coverage: %s is below v_57 or unknown — copy kept",
                    author_instance_id,
                )
                return None
            space = await self._spaces.get(space_id)
            if space is None or space.gfs_servers is None:
                log.debug(
                    "relay coverage: no owner list for space %s — copy kept",
                    space_id,
                )
                return None
            listed = {k.lower() for k in space.gfs_servers.public_keys()}
            seed = await self._spaces.get_space_seed(space_id)
        except Exception:
            log.exception("relay coverage: reading space %s failed", space_id)
            return None
        if not listed or seed is None:
            return None

        async def _skip(conn: GfsConnection) -> bool:
            try:
                return await self._covered(space_id, seed, listed, conn)
            except Exception:
                log.exception(
                    "relay coverage: checking %s for space %s failed", conn.id, space_id
                )
                return False

        return _skip

    async def _covered(
        self, space_id: str, seed: bytes, listed: set[str], conn: GfsConnection
    ) -> bool:
        if (conn.public_key or "").lower() not in listed:
            log.debug("relay coverage: %s is not on the owner's list", conn.id)
            return False
        if not (
            await self._gfs.member_publish_trusted_supported(conn)
            and await self._gfs.member_publish_strict_supported(conn)
            and await self._gfs.space_item_followers_supported(conn)
        ):
            log.debug(
                "relay coverage: %s did not prove member publish and "
                "space_item_followers — copy kept",
                conn.id,
            )
            return False
        key = (space_id, conn.id)
        now = monotonic()
        held = self._summaries.get(key)
        if held is not None and now - held[0] < SUMMARY_TTL_S:
            return held[1]
        covered = await self._ask(space_id, seed, conn)
        log.debug(
            "relay coverage: %s for space %s — every follower reads space_item: %s",
            conn.id,
            space_id,
            covered,
        )
        if covered is None:
            return False  # unknown: keep the copy, ask again next time
        if len(self._summaries) >= _MAX_SUMMARIES:
            self._summaries.clear()
        self._summaries[key] = (now, covered)
        return covered

    async def _ask(
        self, space_id: str, seed: bytes, conn: GfsConnection
    ) -> bool | None:
        """``True`` when the server says no follower lacks ``space_item``;
        ``None`` when it can't tell us. Over the cookie-less publish session:
        the query is authority-signed and names no household."""
        client = self._gfs.publish_client()
        if client is None or not conn.inbox_url:
            return None
        ts = datetime.now(UTC).isoformat()
        sig = sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
            space_id=space_id,
            payload={"space_id": space_id, "ts": ts},
            space_seed=seed,
        )
        params = {
            "ts": ts,
            "authority_sig": sig["authority_sig"],
            "authority_sig_suite": sig["authority_sig_suite"],
            "summary": "1",
        }
        try:
            async with client.get(
                f"{conn.inbox_url}/gfs/spaces/{space_id}/subscribers",
                params=params,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.warning(
                        "relay coverage: %s answered HTTP %d for space %s",
                        conn.id,
                        resp.status,
                        space_id,
                    )
                    return None
                data = await resp.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            log.warning("relay coverage: asking %s failed: %s", conn.id, exc)
            return None
        without = data.get("without_space_item") if isinstance(data, dict) else None
        if not isinstance(without, int) or isinstance(without, bool) or without < 0:
            return None
        return without == 0
