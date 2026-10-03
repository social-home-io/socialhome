"""In-memory sliding-window rate limiter (§5.2).

Per-process, household scale. Buckets are keyed on ``(user_id, path_prefix)``
so that two endpoints under the same top-level path share a quota.

The data structure is a ``dict[bucket_key, list[float]]`` of monotonic
timestamps. Old entries are garbage-collected lazily on every ``check()`` —
no background cleanup task is required.
"""

from __future__ import annotations

import fnmatch
import math
import time
from collections.abc import Callable

from aiohttp import web

from .security import error_response


class RateLimiter:
    """A trivially simple sliding-window rate limiter.

    Usage::

        limiter = RateLimiter()
        allowed = await limiter.check(user_id, path, limit=60, window_s=60)
        if not allowed:
            return error_response(429, "RATE_LIMITED")

    The first two path segments define the bucket, so e.g.
    ``/api/posts/123/comments`` and ``/api/posts/456/comments`` share a
    bucket — a user hammering either still hits the same quota.
    """

    __slots__ = ("_windows", "_monotonic")

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._windows: dict[str, list[float]] = {}
        self._monotonic = monotonic

    async def check(self, user_id: str, path: str, limit: int, window_s: int) -> bool:
        """Return ``True`` if the request is allowed, ``False`` if not."""
        bucket = self._bucket(user_id, path)
        now = self._monotonic()
        cutoff = now - window_s
        times = [t for t in self._windows.get(bucket, ()) if t > cutoff]
        if len(times) >= limit:
            self._windows[bucket] = times
            return False
        times.append(now)
        self._windows[bucket] = times
        return True

    def is_allowed(self, key: str, *, limit: int, window_s: int) -> bool:
        """Synchronous variant keyed on an arbitrary string.

        Useful in places where ``check()`` would require contriving a
        ``path`` — e.g. background workers. Returns the same semantics.
        """
        now = self._monotonic()
        cutoff = now - window_s
        times = [t for t in self._windows.get(key, ()) if t > cutoff]
        if len(times) >= limit:
            self._windows[key] = times
            return False
        times.append(now)
        self._windows[key] = times
        return True

    def retry_after_s(self, key: str, *, window_s: int) -> int:
        """Whole seconds until ``key``'s oldest request leaves the window.

        That is the first moment a refused caller can be allowed again —
        what an HTTP ``Retry-After`` should say. Rounded up and never below
        ``1`` for a bucket that holds anything (``0`` for an empty one), so
        a caller that honours it does not come back a fraction early. Pure
        read: records nothing.
        """
        times = self._windows.get(key)
        if not times:
            return 0
        remaining = times[0] + window_s - self._monotonic()
        return max(1, math.ceil(remaining))

    def reset(self, bucket_or_key: str | None = None) -> None:
        """Reset a single bucket (or the whole limiter when ``None``)."""
        if bucket_or_key is None:
            self._windows.clear()
        else:
            self._windows.pop(bucket_or_key, None)

    @staticmethod
    def _bucket(user_id: str, path: str) -> str:
        parts = path.strip("/").split("/")[:2]
        return f"{user_id}:{'/'.join(parts)}"


# Convenience middleware factory — used by `app.py` to build a global
# aiohttp middleware from a shared RateLimiter instance. The middleware
# signature matches aiohttp's `@web.middleware` decorator expectations.
def build_rate_limit_middleware(
    limiter: RateLimiter,
    *,
    default_limit: int = 60,
    default_window_s: int = 60,
    limits: dict[str, tuple[int, int]] | None = None,
):
    """Return an aiohttp middleware that enforces per-endpoint limits.

    ``limits`` maps a path prefix (``"/api/media"``) to a
    ``(limit, window_s)`` tuple that overrides the defaults. Handlers that
    want tighter limits add themselves here; anything not present falls
    back to ``default_limit`` / ``default_window_s``.
    """
    limits = limits or {}

    def _pick(path: str) -> tuple[str | None, int, int]:
        # Two key flavours:
        #   * literal prefix — most specific wins via insertion order
        #     (callers list narrower prefixes first).
        #   * fnmatch-style with ``*`` — matches the whole path; great
        #     for ``/api/spaces/*/ban`` style action endpoints.
        for pattern, (limit, window_s) in limits.items():
            if "*" in pattern:
                if fnmatch.fnmatchcase(path, pattern):
                    return pattern, limit, window_s
            elif path.startswith(pattern):
                return pattern, limit, window_s
        return None, default_limit, default_window_s

    @web.middleware
    async def middleware(request: "web.Request", handler) -> "web.StreamResponse":
        user = request.get("user")
        # Unauthenticated requests bypass per-user limits; the
        # authentication middleware will reject them before they reach a
        # protected handler anyway.
        if not user:
            return await handler(request)
        rule, limit, window_s = _pick(request.path)
        user_id = getattr(user, "user_id", None) or str(user)
        # A request counts only against the rule it matched: a looser rule
        # (``/api/calls/*/ice``, 300/min) must not use up a tighter one
        # sharing its first path segments (``/api/calls``, 10/min) — they
        # used to share one ``api/calls`` bucket. Unmatched paths keep the
        # per-first-two-segments default bucket.
        if rule is not None:
            allowed = limiter.is_allowed(
                f"{user_id}:rule:{rule}", limit=limit, window_s=window_s
            )
        else:
            allowed = await limiter.check(user_id, request.path, limit, window_s)
        if not allowed:
            return error_response(
                429,
                "RATE_LIMITED",
                "Too many requests — wait a moment.",
            )
        return await handler(request)

    return middleware
