"""One shared, cached read of each paired GFS's WHOLE public directory.

``GET {gfs}/gfs/spaces`` is the only question a household may ask a
connection server about which spaces it lists: it is the same request for
every household and names no space. A space-specific probe
(``GET /gfs/spaces/{id}``) or an identity-bound (un)subscribe tells the
server which space this household cares about, so every such request is
gated on this directory first — by the mirror (follower subscribe and the
legacy (un)subscribe fallback) and by member publish (which servers to
publish / auto-subscribe on, and whether a space went strict).

One instance is shared by both services (``app._build_*``), so a server's
directory is downloaded once per TTL rather than once per caller.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import aiohttp

from ..domain.federation import GfsConnection
from ..domain.gfs_member_publish import GFS_PUBLISH_MODE_STRICT
from .gfs_http import MAX_GFS_DIRECTORY_BODY_BYTES, read_json_capped

log = logging.getLogger(__name__)

#: A directory that listed something is reused this long.
LISTING_TTL_S: float = 600.0

#: An EMPTY directory is reused this long — a fresh server fills up.
LISTING_NEGATIVE_TTL_S: float = 60.0

#: An unreadable directory (down, non-200, malformed, over a cap) is
#: remembered this long, so a failing server is not re-hit on every call.
FAILURE_TTL_S: float = 5.0

#: A caller that asks with ``refresh_on_miss`` re-reads a cached directory
#: that lacks the space once it is this old — a space published a moment
#: ago must not stay unfollowable for a whole TTL.
MISS_REFRESH_S: float = 5.0

#: Most space ids accepted from one directory. Over it the directory is
#: refused (fail closed, logged) rather than truncated: a silently cut list
#: would make every space past the cut unfollowable with no trace.
MAX_DIRECTORY_IDS: int = 50_000

#: The directory is a bigger body than a single listing; still bounded.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

_TRUSTED = "trusted"


def _now() -> float:
    """The cache clock (a seam: tests move it without freezing the loop's)."""
    return time.monotonic()


class GfsDirectoryCache:
    """Per-connection cache of ``{space id: member publish mode}``.

    Concurrent misses for one connection share ONE fetch. Entries past the
    longest TTL are pruned on every store, so a removed connection's entry
    does not linger.
    """

    __slots__ = ("_client", "_entries", "_inflight")

    def __init__(self, client: Callable[[], aiohttp.ClientSession | None]) -> None:
        # A getter, not a session: the cookie-less publish session is
        # attached after construction (``app._on_startup``).
        self._client = client
        self._entries: dict[str, tuple[dict[str, str] | None, float]] = {}
        self._inflight: dict[str, asyncio.Future[dict[str, str] | None]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def forget(self, *_: object) -> None:
        """Drop every cached directory (key import, config change)."""
        self._entries.clear()

    async def directory(self, conn: GfsConnection) -> dict[str, str] | None:
        """*conn*'s directory, or ``None`` when it can't be read."""
        cached = self._entries.get(conn.id)
        if cached is not None and self._fresh(cached):
            return cached[0]
        return await self._fetch(conn)

    async def lists(
        self, conn: GfsConnection, space_id: str, *, refresh_on_miss: bool = False
    ) -> bool:
        """Whether *conn*'s directory lists *space_id*. With
        ``refresh_on_miss`` a cached directory lacking the id is re-read
        once it is :data:`MISS_REFRESH_S` old."""
        listed = await self.directory(conn)
        if listed is not None and space_id in listed:
            return True
        if not refresh_on_miss:
            return False
        cached = self._entries.get(conn.id)
        if cached is not None and _now() - cached[1] < MISS_REFRESH_S:
            return False
        listed = await self._fetch(conn)
        return listed is not None and space_id in listed

    async def mode(self, conn: GfsConnection, space_id: str) -> str | None:
        """The listed member publish mode, or ``None`` when not listed."""
        listed = await self.directory(conn)
        return None if listed is None else listed.get(space_id)

    @staticmethod
    def _fresh(entry: tuple[dict[str, str] | None, float]) -> bool:
        listed, at = entry
        if listed is None:
            ttl = FAILURE_TTL_S
        elif listed:
            ttl = LISTING_TTL_S
        else:
            ttl = LISTING_NEGATIVE_TTL_S
        return _now() - at < ttl

    async def _fetch(self, conn: GfsConnection) -> dict[str, str] | None:
        pending = self._inflight.get(conn.id)
        if pending is not None:
            return await asyncio.shield(pending)
        fut: asyncio.Future[dict[str, str] | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._inflight[conn.id] = fut
        try:
            listed = await self._read(conn)
            self._store(conn.id, listed)
            fut.set_result(listed)
            return listed
        except BaseException as exc:
            fut.set_exception(exc)
            # Retrieved here so an unawaited future never logs the error.
            fut.exception()
            raise
        finally:
            del self._inflight[conn.id]

    def _store(self, conn_id: str, listed: dict[str, str] | None) -> None:
        now = _now()
        for cid, (_listed, at) in list(self._entries.items()):
            if now - at >= LISTING_TTL_S:
                del self._entries[cid]
        self._entries[conn_id] = (listed, now)

    async def _read(self, conn: GfsConnection) -> dict[str, str] | None:
        client = self._client()
        if client is None:
            return None
        url = f"{conn.inbox_url.rstrip('/')}/gfs/spaces"
        try:
            async with client.get(
                url, allow_redirects=False, timeout=_HTTP_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    log.warning("gfs_directory: %s returned HTTP %d", url, resp.status)
                    return None
                body = await read_json_capped(
                    resp, url=url, limit=MAX_GFS_DIRECTORY_BODY_BYTES
                )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("gfs_directory: fetch from %s failed: %s", url, exc)
            return None
        spaces = body.get("spaces") if isinstance(body, dict) else None
        if not isinstance(spaces, list):
            if body is not None:
                log.warning("gfs_directory: %s returned no spaces list", url)
            return None
        if len(spaces) > MAX_DIRECTORY_IDS:
            log.warning(
                "gfs_directory: %s lists %d spaces (cap %d) — refusing the "
                "directory rather than truncating it",
                url,
                len(spaces),
                MAX_DIRECTORY_IDS,
            )
            return None
        # ``member_publish_mode`` (v_50) is absent on an older server, which
        # reads as ``trusted``.
        return {
            sp["space_id"]: (
                GFS_PUBLISH_MODE_STRICT
                if sp.get("member_publish_mode") == GFS_PUBLISH_MODE_STRICT
                else _TRUSTED
            )
            for sp in spaces
            if isinstance(sp, dict) and isinstance(sp.get("space_id"), str)
        }
