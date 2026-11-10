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
    DEFAULT_MAX_KID_ENTRIES,
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


# ─── v_90: multi-recipient frames parked by kid ──────────────────────────


_KID_A = "A" * 22
_KID_B = "B" * 22


def _kid_cache(bus: EventBus, kids: dict[tuple[str, int], str], **kw):
    cache = PendingDecryptsCache(bus=bus, **kw)
    asks: list[tuple[str, int]] = []

    async def _resolve(space_id: str, epoch: int) -> str | None:
        asks.append((space_id, epoch))
        return kids.get((space_id, epoch))

    cache.attach_kid_resolver(_resolve)
    return cache, asks


async def test_a_kid_entry_replays_when_its_epoch_key_is_imported():
    bus = EventBus()
    cache, asks = _kid_cache(bus, {("sp-1", 3): _KID_A, ("sp-1", 4): _KID_B})
    calls: list[str] = []

    async def _a() -> None:
        calls.append("a")

    async def _b() -> None:
        calls.append("b")

    cache.stash_kid(_KID_A, _a)
    cache.stash_kid(_KID_B, _b)
    assert cache.kid_depth() == 2 and len(cache) == 0
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=3))
    assert calls == ["a"]
    assert asks == [("sp-1", 3)]  # resolved once per import
    assert cache.kid_depth() == 1
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=4))
    assert calls == ["a", "b"] and cache.kid_depth() == 0


async def test_no_kid_entries_cost_no_resolve():
    bus = EventBus()
    _cache, asks = _kid_cache(bus, {("sp-1", 3): _KID_A})
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=3))
    assert asks == []


async def test_an_unresolved_import_leaves_kid_entries_parked():
    bus = EventBus()
    cache, _ = _kid_cache(bus, {})
    calls: list[str] = []

    async def _a() -> None:
        calls.append("a")

    cache.stash_kid(_KID_A, _a)
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=3))
    assert calls == [] and cache.kid_depth() == 1


async def test_kid_entries_have_their_own_bound_and_never_evict_sync_entries():
    """A junk-kid flood on a mailbox fills only the kid deque (FIFO,
    DEFAULT_MAX_KID_ENTRIES); the §25.6 sync entries stay parked."""
    assert DEFAULT_MAX_KID_ENTRIES == 64
    bus = EventBus()
    cache, _ = _kid_cache(bus, {("sp-1", 3): _KID_A})
    calls: list[str] = []

    async def _sync() -> None:
        calls.append("sync")

    async def _real() -> None:
        calls.append("real")

    async def _junk() -> None:
        calls.append("junk")

    cache.stash("sp-1", 3, _sync)
    cache.stash_kid(_KID_A, _real)
    for _ in range(DEFAULT_MAX_KID_ENTRIES):
        cache.stash_kid(_KID_B, _junk)
    assert cache.kid_depth() == DEFAULT_MAX_KID_ENTRIES
    assert len(cache) == 1  # the sync entry survived the flood
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=3))
    assert calls == ["sync"]  # the oldest kid entry was the one evicted


async def test_a_raising_kid_replay_does_not_block_the_others():
    bus = EventBus()
    cache, _ = _kid_cache(bus, {("sp-1", 3): _KID_A})
    calls: list[str] = []

    async def _boom() -> None:
        raise RuntimeError("bad frame")

    async def _ok() -> None:
        calls.append("ok")

    cache.stash_kid(_KID_A, _boom)
    cache.stash_kid(_KID_A, _ok)
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=3))
    assert calls == ["ok"] and cache.kid_depth() == 0


async def test_a_failing_resolver_keeps_kid_entries_and_still_replays_sync():
    bus = EventBus()
    cache = PendingDecryptsCache(bus=bus)
    calls: list[str] = []

    async def _resolve(space_id: str, epoch: int) -> str | None:
        raise RuntimeError("store down")

    async def _kid() -> None:
        calls.append("kid")

    async def _sync() -> None:
        calls.append("sync")

    cache.attach_kid_resolver(_resolve)
    cache.stash_kid(_KID_A, _kid)
    cache.stash("sp-1", 3, _sync)
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=3))
    assert calls == ["sync"] and cache.kid_depth() == 1


async def test_without_a_resolver_kid_entries_stay_parked():
    bus = EventBus()
    cache = PendingDecryptsCache(bus=bus)
    calls: list[str] = []

    async def _kid() -> None:
        calls.append("kid")

    cache.stash_kid(_KID_A, _kid)
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=3))
    assert calls == [] and cache.kid_depth() == 1


async def test_a_replay_that_parks_again_is_kept_for_the_next_import():
    """A replay that still finds no key re-stashes; it is not lost and does
    not loop within the same import."""
    bus = EventBus()
    cache, _ = _kid_cache(bus, {("sp-1", 3): _KID_A})
    runs: list[int] = []

    async def _again() -> None:
        runs.append(1)
        cache.stash_kid(_KID_A, _again)

    cache.stash_kid(_KID_A, _again)
    await bus.publish(SpaceContentKeyImported(space_id="sp-1", epoch=3))
    assert runs == [1] and cache.kid_depth() == 1
