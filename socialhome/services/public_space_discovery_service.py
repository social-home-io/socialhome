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

from ..domain.space import normalize_category, normalize_min_age
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.public_space_repo import (
    AbstractPublicSpaceRepo,
    PublicSpaceListing,
)
from .gfs_http import (
    MAX_GFS_DIRECTORY_BODY_BYTES,
    MAX_GFS_DIRECTORY_ITEMS,
    read_json_capped,
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
        "_stop",
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
        self._stop = asyncio.Event()
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
        if self._gfs_connection_repo is None:
            return
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._refresh_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(
            self._poll_loop(),
            name="PublicSpaceDiscoveryPoller",
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._refresh_event is not None:
            # Unblock any pending wait inside the poll loop so it can exit.
            self._refresh_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
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
            listings = await self._fetch_directory(conn.inbox_url)
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
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception:
                log.exception("public_space_discovery: poll tick failed")
            if self._stop.is_set():
                return
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
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self._poll_interval,
                    )
            except asyncio.TimeoutError:
                continue
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
        # The GFS serves its public directory at ``/gfs/spaces``
        # (``global_server/routes/__init__.py``). ``/api/public_spaces`` is
        # *this household's own* API route — polling it 404'd forever.
        url = f"{gfs_url.rstrip('/')}/gfs/spaces"
        try:
            async with client.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    # INFO, not DEBUG: a directory that never answers means
                    # the Global tab stays permanently empty — that must be
                    # diagnosable from default logs.
                    log.info(
                        "public_space_discovery: GFS directory %s returned HTTP %d",
                        url,
                        resp.status,
                    )
                    return []
                # Bounded read: a paired GFS is still remote input, and
                # aiohttp caps nothing by default — an unbounded body would
                # let a hostile directory OOM the household. Over the cap
                # reads as a failed poll (fail-soft: next tick retries).
                body = await read_json_capped(
                    resp,
                    url=url,
                    limit=MAX_GFS_DIRECTORY_BODY_BYTES,
                )
                if body is None:
                    return []
        except Exception as exc:
            # Fail-soft: a down GFS must not break the poll loop — but stay
            # visible so a persistent outage isn't silent.
            log.info("public_space_discovery: fetch failed for %s: %s", url, exc)
            return []

        items = body.get("spaces") if isinstance(body, dict) else body
        if not isinstance(items, list):
            return []
        if len(items) > MAX_GFS_DIRECTORY_ITEMS:
            # Independent of the byte cap: a body well under the size limit
            # can still carry an enormous number of minimal rows, each of
            # which would become a ``public_space_cache`` write. Import the
            # first N and say so — truncating beats both OOM and a poll tick
            # that never finishes.
            log.warning(
                "public_space_discovery: %s returned %d listings — importing"
                " the first %d",
                url,
                len(items),
                MAX_GFS_DIRECTORY_ITEMS,
            )
            items = items[:MAX_GFS_DIRECTORY_ITEMS]
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
                        # The GFS directory (``GlobalSpace``) is geo-less and
                        # carries no emoji — geo filtering applies only to
                        # peer-directory listings, so leave these unset
                        # rather than invent values.
                        emoji=None,
                        lat=None,
                        lon=None,
                        radius_km=None,
                        member_count=int(
                            item.get("subscriber_count")
                            or item.get("member_count", 0)
                            or 0,
                        ),
                        # Clamped: the GFS's ``min_age`` is an unconstrained
                        # int, and the ``public_space_cache`` CHECK would
                        # raise on e.g. 15 — aborting the whole poll tick.
                        min_age=normalize_min_age(item.get("min_age")),
                        category=normalize_category(item.get("category")),
                    )
                )
            except KeyError, TypeError, ValueError:
                continue
        return out
