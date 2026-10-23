"""Fetch + parse the remote app catalog published by ``socialhome-apps``.

The catalog is a JSON document ``{"apps": [<AppCatalogEntry>, ...]}``
served from a GitHub release asset. This service only *reads* it; install
(download + verify + unpack) lives in :class:`AppService`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import aiohttp
import orjson

from ..domain.apps import AppCatalogEntry

log = logging.getLogger(__name__)

#: How long (seconds) a cached catalog result is considered fresh.
CATALOG_TTL_S: float = 24 * 60 * 60

#: Longest single response header the catalog and bundle downloads read.
#: GitHub's release-download responses carry a Content-Security-Policy
#: header longer than aiohttp's default 8 190 bytes (since 2026-10-23:
#: ``400 Got more than 8190 bytes when reading``), which failed every
#: catalog fetch and bundle download. Still bounded: a header past this is
#: refused, as before.
RELEASE_MAX_FIELD_SIZE: int = 64 * 1024


def release_session() -> aiohttp.ClientSession:
    """A short-lived session for the app catalog and bundle downloads, with
    room for the release host's long response headers
    (:data:`RELEASE_MAX_FIELD_SIZE`)."""
    return aiohttp.ClientSession(max_field_size=RELEASE_MAX_FIELD_SIZE)


class AppCatalogService:
    """Fetch and parse the remote app catalog.

    Results are cached in memory for :data:`CATALOG_TTL_S` seconds.
    Pass ``force=True`` to :meth:`fetch_catalog` to bypass the cache.
    """

    __slots__ = ("_cache", "_cache_ts", "_catalog_url", "_session_factory")

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        catalog_url: str,
    ) -> None:
        """Initialize the service.

        Args:
            session_factory: A callable that returns an aiohttp.ClientSession.
            catalog_url: The URL of the catalog.json to fetch.
        """
        self._session_factory = session_factory
        self._catalog_url = catalog_url
        self._cache: list[AppCatalogEntry] | None = None
        self._cache_ts: float | None = None

    @property
    def last_fetched_monotonic(self) -> float | None:
        """Return the ``time.monotonic()`` timestamp of the last fetch, or ``None``."""
        return self._cache_ts

    async def fetch_catalog(self, *, force: bool = False) -> list[AppCatalogEntry]:
        """GET the catalog and parse it.

        Malformed entries are skipped (logged at WARNING) so one bad entry
        can't break Browse.

        The result is cached for :data:`CATALOG_TTL_S` seconds.  Pass
        ``force=True`` to bypass the cache and re-fetch unconditionally.

        Args:
            force: If ``True``, ignore the in-memory cache and re-fetch.

        Returns:
            List of valid AppCatalogEntry objects.
        """
        now = time.monotonic()
        if (
            not force
            and self._cache is not None
            and self._cache_ts is not None
            and (now - self._cache_ts) < CATALOG_TTL_S
        ):
            return self._cache

        async with (
            self._session_factory() as session,
            session.get(self._catalog_url) as resp,
        ):
            resp.raise_for_status()
            raw = await resp.text()
        doc = orjson.loads(raw)
        out: list[AppCatalogEntry] = []
        for item in doc.get("apps", []):
            try:
                out.append(AppCatalogEntry.from_dict(item))
            except (ValueError, TypeError) as exc:
                log.warning("skipping malformed catalog entry %r: %s", item, exc)

        self._cache = out
        self._cache_ts = time.monotonic()
        return out
