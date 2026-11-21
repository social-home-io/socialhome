"""A WARNING when one client bucket takes most of a GFS's traffic.

Every per-client limit on the GFS keys on
:meth:`~socialhome.global_server.public.ClientIpResolver.bucket`. Behind a
CDN or proxy that the resolver can't see through — Cloudflare in front of a
proxy that *appends* to ``X-Forwarded-For`` instead of setting it, or a
proxy missing from ``[server] trusted_proxies`` — every visitor resolves to
the same few addresses, and the per-client limits become one shared limit
that the first busy minute uses up for everyone. Nothing fails loudly; the
GFS just starts answering 429.

:class:`ClientSpreadWatch` counts requests per bucket over
:data:`SPREAD_WINDOW_S`. When a window with at least
:data:`SPREAD_MIN_REQUESTS` requests closes with one bucket above
:data:`SPREAD_SHARE` of them, it logs a WARNING naming that bucket — at
most once per :data:`SPREAD_WARN_EVERY_S`. In memory only, at most
``max_buckets`` distinct buckets a window (past that a window is spread by
definition). ``/healthz`` (the balancer's own probe) and ``/cluster/*``
(sibling nodes) are not counted. The recipe that keeps the real client
address: ``docs/operations/gfs-cluster.md`` § Client addresses behind a CDN.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

from aiohttp import web

from .public import ClientIpResolver

log = logging.getLogger(__name__)

__all__ = [
    "SPREAD_MAX_BUCKETS",
    "SPREAD_MIN_REQUESTS",
    "SPREAD_SHARE",
    "SPREAD_WARN_EVERY_S",
    "SPREAD_WINDOW_S",
    "ClientSpreadWatch",
    "build_client_spread_watch",
]

#: Length of one counting window.
SPREAD_WINDOW_S = 300
#: Requests a window needs before its spread means anything (2 / s): a
#: quiet GFS where one household is the busiest client is not collapsed.
SPREAD_MIN_REQUESTS = 600
#: Share of a window's requests one bucket must exceed to warn.
SPREAD_SHARE = 0.5
#: At most one WARNING this often.
SPREAD_WARN_EVERY_S = 3600
#: Distinct buckets counted per window.
SPREAD_MAX_BUCKETS = 10_000


class _Observer(Protocol):
    def observe(self, bucket: str) -> None: ...


class ClientSpreadWatch:
    """Per-bucket request counts over a rolling window; see the module."""

    __slots__ = (
        "_clock",
        "_counts",
        "_last_warned",
        "_max_buckets",
        "_total",
        "_window_start",
    )

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_buckets: int = SPREAD_MAX_BUCKETS,
    ) -> None:
        self._clock = clock
        self._max_buckets = max_buckets
        self._counts: dict[str, int] = {}
        self._total = 0
        self._window_start = clock()
        self._last_warned: float | None = None

    @property
    def tracked_buckets(self) -> int:
        return len(self._counts)

    def observe(self, bucket: str) -> None:
        """Count one request from ``bucket``."""
        now = self._clock()
        if now - self._window_start >= SPREAD_WINDOW_S:
            self._close(now)
        self._total += 1
        if bucket in self._counts:
            self._counts[bucket] += 1
        elif len(self._counts) < self._max_buckets:
            self._counts[bucket] = 1

    def _close(self, now: float) -> None:
        counts, total = self._counts, self._total
        self._counts, self._total, self._window_start = {}, 0, now
        if total < SPREAD_MIN_REQUESTS or not counts:
            return
        if (
            self._last_warned is not None
            and now - self._last_warned < SPREAD_WARN_EVERY_S
        ):
            return
        top, hits = max(counts.items(), key=lambda kv: kv[1])
        if hits <= total * SPREAD_SHARE:
            return
        self._last_warned = now
        log.warning(
            "GFS: one client address (%s) made %d of the last %d requests "
            "(%d%%) in %d min — if that is your proxy or CDN, every visitor "
            "shares one rate-limit budget. Make the proxy SET "
            "X-Forwarded-For to the real client (behind Cloudflare: from "
            "CF-Connecting-IP, only for Cloudflare's addresses) and list it "
            "in [server] trusted_proxies; see docs/operations/gfs-cluster.md. "
            "Logged at most once an hour.",
            top,
            hits,
            total,
            round(100 * hits / total),
            SPREAD_WINDOW_S // 60,
        )


def _counted(path: str) -> bool:
    return path != "/healthz" and not path.startswith("/cluster/")


def build_client_spread_watch(
    resolver: ClientIpResolver, watch: _Observer | None = None
):
    """Middleware feeding every request's client bucket to ``watch``
    (a fresh :class:`ClientSpreadWatch` by default). Never refuses."""
    observer: _Observer = watch if watch is not None else ClientSpreadWatch()

    @web.middleware
    async def _spread_watch(request: web.Request, handler):
        if _counted(request.rel_url.path):
            observer.observe(resolver.bucket(request))
        return await handler(request)

    return _spread_watch
