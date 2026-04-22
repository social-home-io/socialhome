"""Public-space discovery — periodic poll of paired GFS instances.

Each active GFS connection maintains a directory of public spaces that
have opted-in to discovery. This service polls all paired GFS instances
on an interval and mirrors the results into ``public_space_cache`` so
the client can browse without touching any GFS on every page load.

If no GFS connections are paired the service is a no-op.

The poll is best-effort: errors are logged and the next tick retries.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.public_space_repo import (
    AbstractPublicSpaceRepo,
    PublicSpaceListing,
)

log = logging.getLogger(__name__)


#: Default poll interval — once an hour is plenty for discovery; we
#: don't need real-time accuracy for browse listings.
DEFAULT_POLL_INTERVAL_SECONDS: float = 3600

#: Cache TTL for stale public_space_cache rows. Anything older is
#: purged on the next poll so a removed public space disappears.
DEFAULT_CACHE_TTL_HOURS: int = 24


class PublicSpaceDiscoveryService:
    """Background poller for public-space discovery.

    Parameters
    ----------
    repo:
        Persistence target for cached listings.
    gfs_connection_repo:
        Repository for active GFS connections. The service polls each
        active connection on every tick.
    poll_interval_seconds:
        How often to poll. Default 1 hour.
    cache_ttl_hours:
        Discard cache entries older than this on each poll.
    http_client:
        Optional aiohttp.ClientSession-like for tests. In production the
        shared app session is provided after construction via
        :meth:`attach_session`.
    """

    __slots__ = (
        "_repo",
        "_gfs_connection_repo",
        "_poll_interval",
        "_cache_ttl",
        "_http_client",
        "_task",
        "_running",
        "_refresh_event",
    )

    def __init__(
        self,
        repo: AbstractPublicSpaceRepo,
        *,
        gfs_connection_repo: AbstractGfsConnectionRepo | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        cache_ttl_hours: int = DEFAULT_CACHE_TTL_HOURS,
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._repo = repo
        self._gfs_connection_repo = gfs_connection_repo
        self._poll_interval = poll_interval_seconds
        self._cache_ttl = cache_ttl_hours
        self._http_client: aiohttp.ClientSession | None = http_client
        self._task: asyncio.Task | None = None
        self._running = False
        self._refresh_event: asyncio.Event | None = None

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        """Provide the shared aiohttp session after construction."""
        if self._http_client is None:
            self._http_client = session

    @property
    def is_active(self) -> bool:
        """Whether the service has a GFS connection repo to poll against."""
        return self._gfs_connection_repo is not None

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running or self._gfs_connection_repo is None:
            return
        self._running = True
        self._refresh_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(
            self._poll_loop(),
            name="PublicSpaceDiscoveryPoller",
        )

    async def stop(self) -> None:
        self._running = False
        if self._refresh_event is not None:
            # Unblock any pending wait inside the poll loop so it can exit.
            self._refresh_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError, Exception:
                pass
            self._task = None

    async def refresh_now(self) -> None:
        """Trigger an out-of-cycle refresh. The next tick of the poll
        loop runs immediately instead of waiting for the scheduled
        interval. If the service isn't running (no GFS paired) this
        is a no-op.
        """
        if self._refresh_event is not None:
            self._refresh_event.set()

    # ─── Public single-tick API (also drivable from tests) ───────────────

    async def poll_once(self) -> int:
        """Run one poll cycle. Returns total count of cached listings."""
        if self._gfs_connection_repo is None:
            return 0

        active_connections = await self._gfs_connection_repo.list_active()
        if not active_connections:
            return 0

        total = 0
        for conn in active_connections:
            listings = await self._fetch_directory(conn.endpoint_url)
            for listing in listings:
                if await self._repo.is_instance_blocked(listing.instance_id):
                    continue
                await self._repo.upsert(listing)
            total += len(listings)

        # Purge stale cache rows.
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=self._cache_ttl)
        ).isoformat()
        purged = await self._repo.purge_older_than(cutoff)
        if purged:
            log.debug("public_space_discovery: purged %d stale rows", purged)

        return total

    # ─── Internals ────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.poll_once()
            except Exception:
                log.exception("public_space_discovery: poll tick failed")
            try:
                if self._refresh_event is not None:
                    try:
                        await asyncio.wait_for(
                            self._refresh_event.wait(),
                            timeout=self._poll_interval,
                        )
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        self._refresh_event.clear()
                else:
                    await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                return

    async def _fetch_directory(self, gfs_url: str) -> list[PublicSpaceListing]:
        client = self._http_client
        if client is None:
            log.debug(
                "public_space_discovery: no shared HTTP session wired — skipping %s",
                gfs_url,
            )
            return []
        url = f"{gfs_url.rstrip('/')}/api/public_spaces"
        try:
            async with client.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.debug(
                        "public_space_discovery: GFS %s returned HTTP %d",
                        gfs_url,
                        resp.status,
                    )
                    return []
                body = await resp.json()
        except Exception as exc:
            log.debug("public_space_discovery: fetch failed for %s: %s", gfs_url, exc)
            return []

        items = body.get("spaces") if isinstance(body, dict) else body
        if not isinstance(items, list):
            return []
        out: list[PublicSpaceListing] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                out.append(
                    PublicSpaceListing(
                        space_id=str(item["space_id"]),
                        instance_id=str(
                            item.get("instance_id") or item.get("owning_instance", ""),
                        ),
                        name=str(item.get("name", "")),
                        description=item.get("description"),
                        emoji=item.get("emoji"),
                        lat=item.get("lat"),
                        lon=item.get("lon"),
                        radius_km=item.get("radius_km"),
                        member_count=int(item.get("member_count", 0) or 0),
                        min_age=int(item.get("min_age", 0) or 0),
                        target_audience=str(item.get("target_audience", "all")),
                    )
                )
            except KeyError, TypeError, ValueError:
                continue
        return out
