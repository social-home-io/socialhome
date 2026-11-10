"""Tests for the out-of-order key arrival cache (#122).

Background — when a federation payload that requires a space content
key lands before the key has been imported, it can't decrypt. The
cache stashes the redeliver closure and drains it on the matching
:class:`SpaceContentKeyImported` bus event. See
``socialhome/services/pending_decrypts_cache.py`` for the design.
"""

from __future__ import annotations

import asyncio

from socialhome.domain.events import SpaceContentKeyImported
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.pending_decrypts_cache import (
    DEFAULT_MAX_ENTRIES,
    PendingDecryptsCache,
)


async def test_stash_and_drain_on_matching_event():
    """Happy path — chunk stashes, key arrives, redeliver fires once."""
    bus = EventBus()
    cache = PendingDecryptsCache(bus=bus)

    calls: list[str] = []

    async def _redeliver() -> None:
        calls.append("replayed")

    cache.stash("sp-1", 7, _redeliver)
    assert len(cache) == 1
    assert calls == []

    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=7))
    assert calls == ["replayed"]
    assert len(cache) == 0


async def test_non_matching_event_does_not_drain():
    """Mismatched ``(space_id, epoch)`` leaves the entry parked."""
    bus = EventBus()
    cache = PendingDecryptsCache(bus=bus)

    calls: list[str] = []

    async def _redeliver() -> None:
        calls.append("never")

    cache.stash("sp-1", 7, _redeliver)
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=8))
    await bus.publish(SpaceContentKeyImported(space_id="sp-OTHER", epoch=7))
    assert calls == []
    assert len(cache) == 1


async def test_multiple_stashes_for_same_key_all_replay():
    """Two chunks for the same epoch — both replay on the single
    ``SpaceContentKeyImported`` event. Producers ship sync chunks in
    parallel; the cache must not coalesce them."""
    bus = EventBus()
    cache = PendingDecryptsCache(bus=bus)
    calls: list[int] = []

    async def _make(idx: int):
        async def _r() -> None:
            calls.append(idx)

        return _r

    cache.stash("sp-1", 3, await _make(1))
    cache.stash("sp-1", 3, await _make(2))
    cache.stash("sp-1", 3, await _make(3))
    assert len(cache) == 3

    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=3))
    # FIFO order
    assert calls == [1, 2, 3]
    assert len(cache) == 0


async def test_partial_drain_leaves_other_entries():
    """Entries for an unrelated epoch survive while a matching epoch
    drains. The cache is a flat queue — non-matching entries stay."""
    bus = EventBus()
    cache = PendingDecryptsCache(bus=bus)

    fired: list[str] = []

    async def _on_a():
        fired.append("a")

    async def _on_b():
        fired.append("b")

    cache.stash("sp-1", 1, _on_a)
    cache.stash("sp-2", 1, _on_b)
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=1))
    assert fired == ["a"]
    assert len(cache) == 1  # sp-2/1 still parked


async def test_redeliver_exception_does_not_block_others():
    """If one redeliver raises, the others still execute and the
    queue still drains."""
    bus = EventBus()
    cache = PendingDecryptsCache(bus=bus)
    fired: list[str] = []

    async def _bad():
        raise RuntimeError("simulated decrypt failure")

    async def _good():
        fired.append("good")

    cache.stash("sp-1", 0, _bad)
    cache.stash("sp-1", 0, _good)
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=0))
    assert fired == ["good"]
    assert len(cache) == 0


async def test_cache_caps_at_max_entries():
    """Over-cap stashes evict the oldest entries (FIFO). Operators see
    the eviction in logs; the producer's most recent send wins."""
    bus = EventBus()
    cache = PendingDecryptsCache(bus=bus, max_entries=3)

    fired: list[int] = []

    async def _mk(idx: int):
        async def _r() -> None:
            fired.append(idx)

        return _r

    for i in range(5):
        cache.stash("sp", 0, await _mk(i))
    # 5 stashes with cap=3 — entries 0 and 1 dropped, 2/3/4 retained.
    assert len(cache) == 3
    await bus.publish(SpaceContentKeyImported(space_id="sp", epoch=0))
    assert fired == [2, 3, 4]


async def test_publish_with_no_entries_is_noop():
    """Bus event arrives while cache is empty — no error, no replay."""
    bus = EventBus()
    cache = PendingDecryptsCache(bus=bus)
    # Just shouldn't raise.
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=0))
    assert len(cache) == 0


async def test_default_max_entries_is_published_constant():
    """The default cap is exported so operators / tests can reference
    it without copy-paste."""
    assert DEFAULT_MAX_ENTRIES == 256


async def test_sync_redeliver_is_awaited_in_order():
    """The redeliver callable must be ``async``; ``stash`` records it
    as-is and the bus event awaits each one sequentially. Two
    concurrent ``publish`` calls don't interleave redelivers (the
    callbacks run on the same asyncio task)."""
    bus = EventBus()
    cache = PendingDecryptsCache(bus=bus)
    order: list[str] = []

    async def _slow():
        order.append("slow-start")
        await asyncio.sleep(0.01)
        order.append("slow-end")

    async def _fast():
        order.append("fast")

    cache.stash("sp", 0, _slow)
    cache.stash("sp", 0, _fast)
    await bus.publish(SpaceContentKeyImported(space_id="sp", epoch=0))
    # slow-start, slow-end, fast — strict sequential drain.
    assert order == ["slow-start", "slow-end", "fast"]
