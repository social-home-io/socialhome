"""In-memory cache for federation payloads that arrived before their
space content key did (#122 out-of-order key arrival).

Background — when a household receives a §25.6 sync chunk or a sealed-
sender GFS envelope, the decrypt path needs the per-epoch
``space_keys`` row to exist. Under normal flow the §D1b accept handshake
ships the key well before any content reaches the receiver, so the
cache stays empty. Three races can land a content payload ahead of the
key:

* §D1b invite handshake completes on the host, the host starts shipping
  content immediately, and the network delivers ``SPACE_POST_CREATED``
  before ``SPACE_PRIVATE_INVITE`` on a parallel path.
* A §25.6 catch-up sync hands the receiver a chunk encrypted under an
  epoch the receiver doesn't have a key for yet (post-rotation churn).
* A peer rejoins after a prolonged absence, picks up the latest epoch's
  posts before its key import finishes.

Without this cache the receiver logs ``sync chunk decrypt failed`` and
drops the payload — at best the user sees a content gap; at worst a
post never materialises locally. With the cache, payloads stash on
decrypt failure and the matching :class:`SpaceContentKeyImported`
event triggers a replay.

Design:

* **In-memory, bounded.** The cache is process-local and capped by
  ``max_entries`` (default 256). Over the cap, oldest entries are
  evicted (FIFO). Restart wipes everything — the next sync handshake
  re-pulls anything that hadn't drained.
* **Generic callable shape.** ``stash(space_id, epoch, redeliver)``
  closes over whatever context the caller needs (a sync chunk's raw
  bytes, a sealed envelope's encrypted_payload). On
  :class:`SpaceContentKeyImported`, the cache invokes every matching
  ``redeliver`` callable and drops them from the cache. Failures in
  the callable are logged + swallowed so one bad entry doesn't poison
  the queue.
* **Same-event matching only.** The cache matches strictly on
  ``(space_id, epoch)`` — no cross-epoch fallbacks. If a chunk was
  stashed for epoch 5 and the receiver later imports epoch 7, the
  stashed entry stays parked until epoch 5's key actually arrives
  (which it will, because the host ships every epoch's key on §D1b
  member acceptance / re-acceptance).

Forward-compat: once a future PR moves additional content paths under
``SpaceContentEncryption`` (currently §25.6 sync + the Phase-5a
public-relay path are the callers), the cache accepts them without
modification —
they just become more producers of ``stash`` calls.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Awaitable, Callable

from ..domain.events import SpaceContentKeyImported
from ..infrastructure.event_bus import EventBus


log = logging.getLogger(__name__)


#: Default cap on the number of stashed entries. 256 covers a household
#: that's caught up to thousands of posts across dozens of spaces, with
#: the practical churn limited to whatever's in-flight during a single
#: handshake window (seconds to minutes). Operators can raise this on
#: the rare deployment that sees larger pending sets.
DEFAULT_MAX_ENTRIES: int = 256


class PendingDecryptsCache:
    """FIFO cache of redeliver callables, drained on key arrival."""

    __slots__ = ("_entries", "_max")

    def __init__(
        self,
        *,
        bus: EventBus,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._entries: deque[tuple[str, int, Callable[[], Awaitable[None]]]] = deque()
        self._max = max_entries
        bus.subscribe(SpaceContentKeyImported, self._on_key_imported)

    def stash(
        self,
        space_id: str,
        epoch: int,
        redeliver: Callable[[], Awaitable[None]],
    ) -> None:
        """Park ``redeliver`` until the matching key lands.

        ``redeliver`` is an async no-arg callable — the caller closes
        over whatever context it needs (envelope bytes, parsed fields,
        repo handles). Same callable can be stashed multiple times by
        a busy producer; each entry replays independently.
        """
        self._entries.append((space_id, epoch, redeliver))
        # Cap from the front: oldest entries get dropped first. We log
        # the eviction so a flooded cache is visible in the operator's
        # journal — under normal flow this branch never runs.
        while len(self._entries) > self._max:
            dropped = self._entries.popleft()
            log.warning(
                "PendingDecryptsCache: evicting oldest entry "
                "(space=%s epoch=%d) — cache full at %d entries",
                dropped[0],
                dropped[1],
                self._max,
            )

    async def _on_key_imported(
        self,
        event: SpaceContentKeyImported,
    ) -> None:
        """Replay every entry that matches the imported (space_id, epoch)."""
        survivors: deque[tuple[str, int, Callable[[], Awaitable[None]]]] = deque()
        to_replay: list[Callable[[], Awaitable[None]]] = []
        for entry in self._entries:
            if entry[0] == event.space_id and entry[1] == event.epoch:
                to_replay.append(entry[2])
            else:
                survivors.append(entry)
        self._entries = survivors
        for redeliver in to_replay:
            try:
                await redeliver()
            except Exception:
                # One bad redeliver MUST NOT block the others. The
                # producer is expected to log internally too; we
                # cover it here for safety.
                log.exception(
                    "PendingDecryptsCache: redeliver raised for space=%s epoch=%d",
                    event.space_id,
                    event.epoch,
                )

    # ── Testing / introspection ────────────────────────────────────────

    def __len__(self) -> int:
        """Current cache depth — handy for tests and the ``/api/admin``
        operator dashboard once we add a "pending decrypts" gauge."""
        return len(self._entries)
