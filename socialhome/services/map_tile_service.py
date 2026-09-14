"""Map tile proxy — fetches raster tiles upstream on the SPA's behalf.

OpenStreetMap's tile policy requires every request to identify itself via
``User-Agent`` or ``Referer``, and returns HTTP 403 otherwise. A browser
can supply neither: both are forbidden header names, so ``fetch()`` from
the SPA cannot set them and referer-less requests are blocked. The result
is a grey map.

Home Assistant Core's own ``map_tiles`` integration reaches the same
conclusion: the accepted alternative is an identifying application
``User-Agent``, which a backend proxy can supply and a browser cannot.

So the backend fetches tiles instead, with an identifying ``User-Agent``,
and keeps a small in-memory LRU cache in front of the upstream to stay a
polite client. The cache is deliberately memory-only — hosts commonly run
from SD cards and a tile cache would be a write-amplification machine.

Operator templates for non-OSM providers (MapTiler, Thunderforest, Stadia,
Mapbox) carry an API key in the query string, so the full upstream URL is
a secret: nothing here ever logs it or puts it in an exception message —
only ``scheme://host/path`` is ever emitted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)

#: Highest zoom level the proxy will request. OSM raster tiles stop here.
MAX_ZOOM: int = 19

#: Media types a tile response may carry. Anything else is a failure —
#: an HTML error page must never be handed back as a tile.
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp"}
)

#: Chunk size for the bounded body read.
_READ_CHUNK: int = 64 * 1024


class TileCoordinateError(ValueError):
    """Requested tile coordinates are outside the valid slippy-map grid."""


class TileUnavailableError(RuntimeError):
    """Upstream failed and no cached tile (fresh or stale) can stand in."""


@dataclass(slots=True, frozen=True)
class Tile:
    """A fetched raster tile.

    Service-local DTO: this is not a row shape (nothing persists tiles),
    so it lives here rather than in ``domain/``.
    """

    body: bytes
    content_type: str


@dataclass(slots=True)
class _CacheEntry:
    """Cached tile plus the clock reading at which it was fetched."""

    tile: Tile
    fetched_at: float


class MapTileService:
    """Proxy + in-memory LRU cache for upstream raster map tiles."""

    __slots__ = (
        "_cache",
        "_cache_bytes",
        "_clock",
        "_failure_backoff",
        "_fetch_semaphore",
        "_max_cache_bytes",
        "_max_fetch_bytes",
        "_refresh_after",
        "_session",
        "_template",
        "_upstream_failed_at",
        "_user_agent",
    )

    def __init__(
        self,
        upstream_template: str,
        *,
        user_agent: str,
        max_cache_bytes: int = 32 * 1024 * 1024,
        refresh_after_seconds: int = 7 * 24 * 3600,
        max_fetch_bytes: int = 8 * 1024 * 1024,
        max_concurrent_fetches: int = 8,
        failure_backoff_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._template = upstream_template
        self._user_agent = user_agent
        self._max_cache_bytes = max_cache_bytes
        self._refresh_after = refresh_after_seconds
        self._max_fetch_bytes = max_fetch_bytes
        self._failure_backoff = failure_backoff_seconds
        self._clock = clock
        self._session: aiohttp.ClientSession | None = None
        self._cache: OrderedDict[tuple[int, int, int], _CacheEntry] = OrderedDict()
        self._cache_bytes = 0
        # Each in-flight fetch may buffer up to ``max_fetch_bytes``, so the
        # only bound on peak memory is how many run at once. The route layer
        # cannot see these buffers — the bound has to live here.
        self._fetch_semaphore = asyncio.Semaphore(max_concurrent_fetches)
        self._upstream_failed_at: float | None = None

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        """Provide the shared aiohttp session after construction."""
        if self._session is None:
            self._session = session

    # ─── Public API ──────────────────────────────────────────────────────

    async def fetch(self, z: int, x: int, y: int) -> Tile:
        """Return the tile at ``z/x/y``, from cache or upstream.

        Raises
        ------
        TileCoordinateError
            Coordinates are outside the slippy-map grid.
        TileUnavailableError
            Upstream failed and nothing cached can stand in.
        """
        self._validate(z, x, y)
        key = (z, x, y)
        now = self._clock()

        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)  # a read counts as a use
            if now - cached.fetched_at < self._refresh_after:
                return cached.tile
            if self._in_backoff(now):
                # Upstream just failed and we hold a servable tile: don't
                # hammer the very server whose policy we are respecting.
                return cached.tile

        try:
            tile = await self._fetch_upstream(z, x, y)
        except TileUnavailableError:
            self._upstream_failed_at = now
            if cached is not None:
                # A stale entry is NEVER evicted for being expired: while
                # upstream is unreachable an old tile is what keeps the map
                # readable. It is only ever dropped by LRU pressure.
                return cached.tile
            raise

        self._upstream_failed_at = None
        self._store(key, tile, now)
        return tile

    # ─── Internals ───────────────────────────────────────────────────────

    @staticmethod
    def _validate(z: int, x: int, y: int) -> None:
        """Fail closed on anything outside the grid.

        The route layer MUST hand over already-parsed ``int`` coordinates:
        anything that is not a true ``int`` (``bool`` included — ``True``
        would render as ``1`` in the URL, and a ``float`` as ``0.5``) is
        rejected here rather than reaching ``str.format``. The URL is built
        from the operator's template plus these validated ints and nothing
        else, so the host and path shape stay operator-controlled.
        """
        for name, value in (("zoom", z), ("x", x), ("y", y)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TileCoordinateError(f"{name} must be an int: {value!r}")
        if not 0 <= z <= MAX_ZOOM:
            raise TileCoordinateError(f"zoom out of range: {z}")
        limit = 1 << z
        if not 0 <= x < limit:
            raise TileCoordinateError(f"x out of range for z={z}: {x}")
        if not 0 <= y < limit:
            raise TileCoordinateError(f"y out of range for z={z}: {y}")

    def _in_backoff(self, now: float) -> bool:
        """True while the post-failure quiet window is still open."""
        failed_at = self._upstream_failed_at
        return failed_at is not None and now - failed_at < self._failure_backoff

    def _build_url(self, z: int, x: int, y: int) -> str:
        """Substitute the tile coordinates into the operator's template.

        Raises ``TileUnavailableError`` (never a bare ``KeyError`` /
        ``IndexError``) so a mistyped template surfaces as a diagnosable
        tile failure instead of a 500 per tile. The template itself is
        never logged — it can carry an API key.
        """
        try:
            return self._template.format(z=z, x=x, y=y)
        except KeyError as exc:
            placeholder = exc.args[0] if exc.args else "?"
            log.warning(
                "map_tile: upstream template uses unsupported placeholder {%s} — "
                "only {z}, {x} and {y} are substituted",
                placeholder,
            )
            raise TileUnavailableError(
                f"upstream template uses unsupported placeholder {{{placeholder}}}"
            ) from exc
        except IndexError as exc:
            log.warning(
                "map_tile: upstream template uses a positional placeholder "
                "({} or {0}) — only the named {z}, {x} and {y} are substituted"
            )
            raise TileUnavailableError(
                "upstream template uses a positional placeholder"
            ) from exc

    @staticmethod
    def _safe_url(url: str) -> str:
        """``scheme://host/path`` — the query (API keys!) is dropped.

        The path IS logged. Every provider we know of carries its key in
        a query parameter, so this is the right trade for a diagnosable
        log line; an operator whose template puts a secret in the path
        should expect it in WARNING output.
        """
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"

    @staticmethod
    def _scrub(text: str, url: str) -> str:
        """Redact the upstream query string wherever it appears in ``text``."""
        query = urlsplit(url).query
        if query:
            text = text.replace(query, "<redacted>")
        return text

    async def _fetch_upstream(self, z: int, x: int, y: int) -> Tile:
        session = self._session
        url = self._build_url(z, x, y)
        safe_url = self._safe_url(url)
        if session is None:
            log.warning(
                "map_tile: no shared HTTP session wired — cannot fetch %s", safe_url
            )
            raise TileUnavailableError("no HTTP session attached")

        headers = {"User-Agent": self._user_agent, "Accept-Encoding": "gzip"}
        # Cap the chunk size so the bounded read overshoots the byte cap by
        # at most one byte rather than by a whole 64 KiB chunk.
        chunk_size = min(_READ_CHUNK, self._max_fetch_bytes + 1)
        try:
            async with (
                self._fetch_semaphore,
                session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp,
            ):
                if resp.status != 200:
                    log.warning(
                        "map_tile: upstream %s returned HTTP %d",
                        safe_url,
                        resp.status,
                    )
                    raise TileUnavailableError(f"upstream HTTP {resp.status}")
                media_type = (
                    resp.headers.get("Content-Type", "")
                    .split(";")[0]
                    .strip()
                    .lower()  # HTTP media types are case-insensitive
                )
                if media_type not in ALLOWED_CONTENT_TYPES:
                    log.warning(
                        "map_tile: upstream %s returned content type %r",
                        safe_url,
                        media_type,
                    )
                    raise TileUnavailableError(
                        f"unexpected content type {media_type!r}"
                    )
                # Bounded read — Content-Length is upstream-controlled, so
                # we count what actually arrives and never buffer past the
                # cap.
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.content.iter_chunked(chunk_size):
                    size += len(chunk)
                    if size > self._max_fetch_bytes:
                        log.warning(
                            "map_tile: upstream %s body exceeded %d bytes",
                            safe_url,
                            self._max_fetch_bytes,
                        )
                        raise TileUnavailableError("upstream body too large")
                    chunks.append(chunk)
        except TileUnavailableError:
            raise
        except Exception as exc:
            # The transport error's own text can echo the full URL — scrub
            # the query before it reaches a log or an exception message.
            detail = self._scrub(f"{type(exc).__name__}: {exc}", url)
            log.warning("map_tile: upstream %s fetch failed: %s", safe_url, detail)
            raise TileUnavailableError(f"upstream fetch failed: {detail}") from exc

        return Tile(body=b"".join(chunks), content_type=media_type)

    def _store(self, key: tuple[int, int, int], tile: Tile, now: float) -> None:
        """Insert ``tile`` and evict least-recently-used entries to fit."""
        existing = self._cache.pop(key, None)
        if existing is not None:
            self._cache_bytes -= len(existing.tile.body)

        size = len(tile.body)
        if size > self._max_cache_bytes:
            # Never worth evicting the whole cache for one oversized tile.
            return

        self._cache[key] = _CacheEntry(tile=tile, fetched_at=now)
        self._cache_bytes += size

        while self._cache_bytes > self._max_cache_bytes:
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= len(evicted.tile.body)
