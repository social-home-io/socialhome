"""IdempotencyCache — TTL-bounded set of seen keys (§4.4).

The federation pipeline already has :class:`~socialhome.crypto.ReplayCache`
which deduplicates ``msg_id`` over a 1-hour window.  This is a more
general primitive: any service needing "have we seen this key recently?"
can use it.

Use cases:

* Inbound federation events with an ``idempotency_key`` payload field
  (e.g. ``CALL_OFFER`` retransmits while the channel is renegotiating).
* HTTP POST handlers that accept an ``Idempotency-Key`` header per
  RFC-9457 — repeated submissions return the cached result.
* Outbound retry orchestration that should not re-enqueue a key already
  in flight.

The cache is in-memory and per-process. For multi-node deployments use
the federation_replay_cache table directly.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Hashable


@dataclass(slots=True)
class _Entry:
    key: Hashable
    expires_at: float


class IdempotencyCache:
    """In-memory cache of recently-seen keys.

    Parameters
    ----------
    ttl_seconds:
        How long a key counts as 'seen' before it can be re-accepted.
    max_entries:
        Hard cap on the number of cached keys; oldest entries are
        evicted when the cap is hit. Defaults to 100 000 — enough for
        ~28 hours at 1 event/second.
    """

    __slots__ = ("_ttl", "_max", "_entries", "_index")

    def __init__(
        self, *, ttl_seconds: float = 3600, max_entries: int = 100_000
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: deque[_Entry] = deque()
        self._index: dict[Hashable, float] = {}

    # ─── Core API ─────────────────────────────────────────────────────────

    def seen(self, key: Hashable, *, now: float | None = None) -> bool:
        """Has this key been observed within the TTL?  Pure read.

        Does not insert. Use :meth:`mark_seen` (or :meth:`check_and_mark`)
        when you want admission control.
        """
        now = now if now is not None else time.monotonic()
        self._evict(now)
        expiry = self._index.get(key)
        return expiry is not None and expiry > now

    def mark_seen(self, key: Hashable, *, now: float | None = None) -> None:
        """Record a key as seen, regardless of prior state."""
        now = now if now is not None else time.monotonic()
        self._evict(now)
        expires = now + self._ttl
        if key in self._index:
            # Refresh expiry; old entry will be skipped during eviction
            # (we keep the index value as the canonical expiry).
            self._index[key] = expires
            self._entries.append(_Entry(key=key, expires_at=expires))
        else:
            self._entries.append(_Entry(key=key, expires_at=expires))
            self._index[key] = expires
        self._enforce_cap()

    def check_and_mark(self, key: Hashable, *, now: float | None = None) -> bool:
        """Atomic 'is this key new? if so, mark it seen.'

        Returns ``True`` when *key* was unseen (and is now recorded),
        ``False`` when it was already in the cache.
        """
        now = now if now is not None else time.monotonic()
        if self.seen(key, now=now):
            return False
        self.mark_seen(key, now=now)
        return True

    def size(self, *, now: float | None = None) -> int:
        """Approximate live entry count (cleans on demand)."""
        self._evict(now if now is not None else time.monotonic())
        return len(self._index)

    def clear(self) -> None:
        self._entries.clear()
        self._index.clear()

    # ─── Internals ────────────────────────────────────────────────────────

    def _evict(self, now: float) -> None:
        while self._entries and self._entries[0].expires_at <= now:
            stale = self._entries.popleft()
            current = self._index.get(stale.key)
            # Only drop the index entry if our entry is the current one.
            if current is not None and current <= now:
                self._index.pop(stale.key, None)

    def _enforce_cap(self) -> None:
        while len(self._index) > self._max and self._entries:
            stale = self._entries.popleft()
            self._index.pop(stale.key, None)
