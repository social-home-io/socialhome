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

from ..domain.apps import (
    MAX_BUNDLE_DOWNLOAD_BYTES,
    AppBundleTooLargeError,
    AppCatalogEntry,
    AppDownloadError,
)

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


#: Whole-request timeout (seconds) for a catalog fetch or bundle download —
#: explicit, so Install / Update fail with a clear error instead of spinning
#: for aiohttp's 5-minute default.
RELEASE_TIMEOUT_S: float = 60.0

#: Read size while streaming a bundle download.
_DOWNLOAD_CHUNK_BYTES = 64 * 1024


def release_session() -> aiohttp.ClientSession:
    """A short-lived session for the app catalog and bundle downloads, with
    room for the release host's long response headers
    (:data:`RELEASE_MAX_FIELD_SIZE`) and a :data:`RELEASE_TIMEOUT_S` cap."""
    return aiohttp.ClientSession(
        max_field_size=RELEASE_MAX_FIELD_SIZE,
        timeout=aiohttp.ClientTimeout(total=RELEASE_TIMEOUT_S),
    )


async def download_bundle(url: str) -> bytes:
    """GET an app bundle from the operator-configured catalog's *url*.

    Bounded: a declared ``Content-Length`` past
    :data:`~socialhome.domain.apps.MAX_BUNDLE_DOWNLOAD_BYTES` is refused
    before reading, and the body is streamed and abandoned the moment it
    passes the cap (a chunked answer declares nothing), so a hostile or
    broken host can't exhaust memory. The session's
    :data:`RELEASE_TIMEOUT_S` bounds the time.

    Redirects are followed: release assets redirect to the CDN.

    Raises :class:`AppBundleTooLargeError` past the cap and
    :class:`AppDownloadError` on a timeout, network or HTTP error.
    """
    cap = MAX_BUNDLE_DOWNLOAD_BYTES
    try:
        async with release_session() as session, session.get(url) as resp:
            resp.raise_for_status()
            if resp.content_length is not None and resp.content_length > cap:
                raise AppBundleTooLargeError(
                    f"The app bundle is larger than {cap // (1024 * 1024)} MiB."
                )
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK_BYTES):
                buf += chunk
                if len(buf) > cap:
                    raise AppBundleTooLargeError(
                        f"The app bundle is larger than {cap // (1024 * 1024)} MiB."
                    )
            return bytes(buf)
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.warning("app bundle download from %s failed: %r", url, exc)
        raise AppDownloadError() from exc


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
