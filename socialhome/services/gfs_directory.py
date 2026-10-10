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
import functools
import json
import logging
import time
from collections.abc import Callable

import aiohttp

from ..domain.federation import GfsConnection
from ..domain.gfs_member_publish import GFS_PUBLISH_MODE_STRICT
from .gfs_http import read_body_capped

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

#: Most space ids accepted from one directory, and the largest directory
#: body read. Over either the directory is refused (fail closed, logged) —
#: never truncated, which would make every space past the cut silently
#: unfollowable, and never answered by falling back to per-space probes,
#: which is the leak this cache exists to prevent. Sized with headroom far
#: past any realistic connection server (member publish read the directory
#: uncapped before). The body is parsed in a worker thread (CPU-bound), and
#: only the ids (and their publish mode) are kept after parsing.
MAX_DIRECTORY_IDS: int = 500_000
MAX_DIRECTORY_BODY_BYTES: int = 64 * 1024 * 1024

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

    __slots__ = ("_client", "_entries", "_owners", "_inflight", "_generation")

    def __init__(self, client: Callable[[], aiohttp.ClientSession | None]) -> None:
        # A getter, not a session: the cookie-less publish session is
        # attached after construction (``app._on_startup``).
        self._client = client
        self._entries: dict[str, tuple[dict[str, str] | None, float]] = {}
        # conn id → {space id: listed owning instance} of the same download.
        self._owners: dict[str, dict[str, str]] = {}
        self._inflight: dict[str, asyncio.Task[dict[str, str] | None]] = {}
        # Bumped by :meth:`forget`: a download started before it never
        # stores its (possibly stale) result.
        self._generation = 0

    def __len__(self) -> int:
        return len(self._entries)

    def forget(self, *_: object) -> None:
        """Drop every cached directory (key import, config change) and
        invalidate the downloads in flight: their results are not stored,
        and the next caller starts a fresh one."""
        self._generation += 1
        self._entries.clear()
        self._owners.clear()
        self._inflight.clear()

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

    def peek_owner(self, conn: GfsConnection, space_id: str) -> str | None:
        """Cache only, never a request: the owning instance *conn*'s fresh
        cached directory lists for *space_id* (``""`` when it names none),
        or ``None`` when no fresh copy lists it — for request paths that
        must not wait on a download."""
        cached = self._entries.get(conn.id)
        if cached is None or not self._fresh(cached) or cached[0] is None:
            return None
        if space_id not in cached[0]:
            return None
        return self._owners.get(conn.id, {}).get(space_id, "")

    async def owner(self, conn: GfsConnection, space_id: str) -> str | None:
        """:meth:`peek_owner`, downloading the directory when needed."""
        if not await self.lists(conn, space_id):
            return None
        return self._owners.get(conn.id, {}).get(space_id, "")

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
        """One download per connection at a time, run as its own task:
        every caller — the first one too — awaits it through
        :func:`asyncio.shield`, so a caller that is cancelled is the only
        one that sees ``CancelledError``; the others still get the result."""
        task = self._inflight.get(conn.id)
        if task is None:
            task = asyncio.create_task(
                self._read_and_store(conn, self._generation),
                name=f"gfs-directory-{conn.id}",
            )
            self._inflight[conn.id] = task
            task.add_done_callback(functools.partial(self._fetched, conn.id))
        return await asyncio.shield(task)

    def _fetched(self, conn_id: str, task: asyncio.Task[dict[str, str] | None]) -> None:
        if self._inflight.get(conn_id) is task:
            del self._inflight[conn_id]
        # Retrieved so a fetch every caller abandoned never logs as an
        # unretrieved task exception.
        if not task.cancelled():
            task.exception()

    async def _read_and_store(
        self, conn: GfsConnection, generation: int
    ) -> dict[str, str] | None:
        read = await self._read(conn)
        listed, owners = read if read is not None else (None, {})
        if generation == self._generation:
            self._store(conn.id, listed, owners)
        return listed

    def _store(
        self, conn_id: str, listed: dict[str, str] | None, owners: dict[str, str]
    ) -> None:
        now = _now()
        for cid, (_listed, at) in list(self._entries.items()):
            if now - at >= LISTING_TTL_S:
                del self._entries[cid]
                self._owners.pop(cid, None)
        self._entries[conn_id] = (listed, now)
        self._owners[conn_id] = owners

    async def _read(
        self, conn: GfsConnection
    ) -> tuple[dict[str, str], dict[str, str]] | None:
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
                raw = await read_body_capped(
                    resp, url=url, limit=MAX_DIRECTORY_BODY_BYTES
                )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("gfs_directory: fetch from %s failed: %s", url, exc)
            return None
        if raw is None:
            return None
        # Up to 64 MiB of JSON: parse in a worker thread. ``json.loads``
        # holds the GIL, so this slows the loop rather than freezing it —
        # the byte cap is what bounds the cost.
        return await asyncio.to_thread(_parse_directory, raw, url)


def _parse_directory(
    raw: bytes, url: str
) -> tuple[dict[str, str], dict[str, str]] | None:
    """``({space id: publish mode}, {space id: owning instance})`` of a
    directory body, or ``None`` when
    it is unparsable, has no ``spaces`` list, or lists more than
    :data:`MAX_DIRECTORY_IDS` (refused, never truncated)."""
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("gfs_directory: %s returned an unparsable body: %s", url, exc)
        return None
    spaces = body.get("spaces") if isinstance(body, dict) else None
    if not isinstance(spaces, list):
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
    entries = [
        sp
        for sp in spaces
        if isinstance(sp, dict) and isinstance(sp.get("space_id"), str)
    ]
    modes = {
        sp["space_id"]: (
            GFS_PUBLISH_MODE_STRICT
            if sp.get("member_publish_mode") == GFS_PUBLISH_MODE_STRICT
            else _TRUSTED
        )
        for sp in entries
    }
    owners = {
        sp["space_id"]: sp["owning_instance"]
        for sp in entries
        if isinstance(sp.get("owning_instance"), str)
    }
    return modes, owners
