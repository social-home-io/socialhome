"""A bounded, expiring, keyed hold — the core of every inbound wait buffer.

Two inbound buffers park a write that arrived before the state it needs:

* :class:`~socialhome.federation.pending_seat_buffer.PendingSeatBuffer` —
  a space write (or DM) waiting for the author's roster seat (or user row);
* :class:`~socialhome.federation.pending_parent_buffer.PendingParentBuffer`
  — a comment, reaction, edit or delete waiting for the post (or the parent
  comment) it references.

Both are fed by remote households, so both must be cheap to flood: capped in
entries, entries per sending household, entries per key and bytes (optionally
also bytes per sending household and per entry), and every entry expires. The
per-sender caps are checked before the shared ones, so no single household
can take every slot or every byte. In memory on purpose — a restart loses
what was held, the same outcome as before the buffers existed.

:class:`BoundedHold` owns exactly that bookkeeping; what a held entry *is*
(a :class:`~socialhome.domain.federation.FederationEvent`, a replay
callable) and what releases it is the wrapping buffer's business.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

#: ``(space_id, kind, id)`` — the state a held entry waits for.
HoldKey = tuple[str, str, str]


@dataclass(slots=True, frozen=True)
class _Held[T]:
    """Buffer-local record (never leaves this module)."""

    entry: T
    token: object
    sender: str
    size: int
    expires_at: float


class BoundedHold[T]:
    """Keyed, expiring, capped storage for held inbound entries."""

    __slots__ = (
        "_by_key",
        "_ttl",
        "_max_entries",
        "_max_per_key",
        "_max_per_sender",
        "_max_bytes",
        "_max_bytes_per_sender",
        "_max_entry_bytes",
        "_bytes",
        "_clock",
        "_on_expire",
    )

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        max_per_key: int,
        max_per_sender: int,
        max_bytes: int,
        max_bytes_per_sender: int | None = None,
        max_entry_bytes: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_expire: Callable[[HoldKey, T], None] | None = None,
    ) -> None:
        self._by_key: dict[HoldKey, list[_Held[T]]] = {}
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._max_per_key = max_per_key
        self._max_per_sender = max_per_sender
        self._max_bytes = max_bytes
        #: ``None`` = only the shared ``max_bytes`` budget applies.
        self._max_bytes_per_sender = max_bytes_per_sender
        self._max_entry_bytes = max_entry_bytes
        self._bytes = 0
        self._clock = clock
        self._on_expire = on_expire

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_key.values())

    @property
    def held_bytes(self) -> int:
        """Bytes currently held (the ``max_bytes`` budget's fill)."""
        return self._bytes

    def _held_from(self, sender: str) -> tuple[int, int]:
        """``(entries, bytes)`` currently held from ``sender``."""
        entries = size = 0
        for bucket in self._by_key.values():
            for h in bucket:
                if h.sender == sender:
                    entries += 1
                    size += h.size
        return entries, size

    def purge(self) -> None:
        """Drop every expired entry (``on_expire`` is told about each)."""
        now = self._clock()
        for key in list(self._by_key):
            bucket = self._by_key[key]
            live = [h for h in bucket if h.expires_at > now]
            for h in bucket:
                if h.expires_at <= now:
                    self._bytes -= h.size
                    if self._on_expire is not None:
                        self._on_expire(key, h.entry)
            if live:
                self._by_key[key] = live
            else:
                del self._by_key[key]

    def hold(
        self, key: HoldKey, entry: T, *, token: object, sender: str, size: int
    ) -> bool:
        """Hold ``entry`` under ``key``. ``True`` when it is held — also when
        an entry with the same ``token`` already waits under ``key`` (a
        duplicate delivery takes no second slot); ``False`` when a bound is
        hit."""
        self.purge()
        bucket = self._by_key.get(key, [])
        if any(h.token == token for h in bucket):
            return True
        if self._max_entry_bytes is not None and size > self._max_entry_bytes:
            return False
        sender_entries, sender_bytes = self._held_from(sender)
        if (
            sender_entries >= self._max_per_sender
            or (
                self._max_bytes_per_sender is not None
                and sender_bytes + size > self._max_bytes_per_sender
            )
            or len(self) >= self._max_entries
            or len(bucket) >= self._max_per_key
            or self._bytes + size > self._max_bytes
        ):
            return False
        bucket.append(_Held(entry, token, sender, size, self._clock() + self._ttl))
        self._by_key[key] = bucket
        self._bytes += size
        return True

    def release(self, *keys: HoldKey) -> list[T]:
        """Hand back (and forget) every live entry held under ``keys``,
        oldest first."""
        self.purge()
        out: list[_Held[T]] = []
        for key in keys:
            out.extend(self._by_key.pop(key, []))
        self._bytes -= sum(h.size for h in out)
        out.sort(key=lambda h: h.expires_at)
        return [h.entry for h in out]
