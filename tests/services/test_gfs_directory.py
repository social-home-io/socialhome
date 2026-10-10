"""Tests for socialhome.services.gfs_directory."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

import socialhome.services.gfs_directory as mod
from socialhome.domain.federation import GfsConnection
from socialhome.services.gfs_directory import GfsDirectoryCache


class _Content:
    def __init__(self, raw: bytes):
        self._raw = raw

    async def read(self, n: int = -1) -> bytes:
        size = len(self._raw) if n < 0 else n
        out, self._raw = self._raw[:size], self._raw[size:]
        return out


class _Resp:
    def __init__(self, status: int, raw: bytes, gate: asyncio.Event | None):
        self.status = status
        self.content = _Content(raw)
        self.content_length = len(raw)
        self._gate = gate

    async def __aenter__(self):
        if self._gate is not None:
            await self._gate.wait()
        return self

    async def __aexit__(self, *_):
        return False


class _Session:
    def __init__(self, status: int = 200, body=None, *, raw: bytes | None = None):
        self.status = status
        self.raw = raw if raw is not None else json.dumps(body or {}).encode()
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.fail: Exception | None = None

    def get(self, url, **_kw):
        self.calls.append(url)
        if self.fail is not None:
            raise self.fail
        return _Resp(self.status, self.raw, self.gate)


def _conn(cid: str = "c1", url: str = "https://g.test") -> GfsConnection:
    return GfsConnection(
        id=cid,
        gfs_instance_id=f"inst-{cid}",
        display_name=cid,
        public_key="pk",
        inbox_url=url,
        status="active",
        paired_at="2025-01-01T00:00:00+00:00",
    )


def _dir(*ids: str, strict: tuple[str, ...] = ()) -> dict:
    return {
        "spaces": [
            {
                "space_id": i,
                **({"member_publish_mode": "strict"} if i in strict else {}),
            }
            for i in ids
        ]
    }


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(mod, "_now", lambda: now[0])
    return now


async def test_reads_the_whole_directory_with_modes(clock):
    s = _Session(body=_dir("a", "b", strict=("b",)))
    cache = GfsDirectoryCache(lambda: s)
    assert await cache.directory(_conn()) == {"a": "trusted", "b": "strict"}
    assert await cache.lists(_conn(), "a")
    assert await cache.mode(_conn(), "b") == "strict"
    assert await cache.mode(_conn(), "zz") is None
    assert s.calls == ["https://g.test/gfs/spaces"]


async def test_ttls_positive_negative_and_failure(clock):
    s = _Session(body=_dir("a"))
    cache = GfsDirectoryCache(lambda: s)
    await cache.directory(_conn())
    clock[0] += mod.LISTING_TTL_S - 1
    await cache.directory(_conn())
    assert len(s.calls) == 1
    clock[0] += 1
    await cache.directory(_conn())
    assert len(s.calls) == 2

    empty = _Session(body=_dir())
    cache = GfsDirectoryCache(lambda: empty)
    await cache.directory(_conn())
    clock[0] += mod.LISTING_NEGATIVE_TTL_S
    await cache.directory(_conn())
    assert len(empty.calls) == 2

    down = _Session(status=503)
    cache = GfsDirectoryCache(lambda: down)
    assert await cache.directory(_conn()) is None
    assert await cache.directory(_conn()) is None
    assert len(down.calls) == 1
    clock[0] += mod.FAILURE_TTL_S
    assert await cache.directory(_conn()) is None
    assert len(down.calls) == 2


async def test_refresh_on_miss_rereads_a_stale_enough_copy(clock):
    s = _Session(body=_dir("a"))
    cache = GfsDirectoryCache(lambda: s)
    assert not await cache.lists(_conn(), "new", refresh_on_miss=True)
    assert len(s.calls) == 1  # just fetched — not re-read
    s.raw = json.dumps(_dir("a", "new")).encode()
    assert not await cache.lists(_conn(), "new")  # plain miss: cached copy
    clock[0] += mod.MISS_REFRESH_S
    assert await cache.lists(_conn(), "new", refresh_on_miss=True)
    assert len(s.calls) == 2


@pytest.mark.parametrize(
    "session",
    [
        _Session(body={"spaces": "nope"}),
        _Session(raw=b"not json"),
        _Session(body={"other": 1}),
    ],
)
async def test_unreadable_bodies_list_nothing(clock, session):
    cache = GfsDirectoryCache(lambda: session)
    assert await cache.directory(_conn()) is None
    assert not await cache.lists(_conn(), "a")


async def test_non_string_ids_are_skipped(clock):
    s = _Session(body={"spaces": [1, {"space_id": ["a"]}, {}, {"space_id": "ok"}]})
    assert await GfsDirectoryCache(lambda: s).directory(_conn()) == {"ok": "trusted"}


async def test_transport_error_and_no_session(clock):
    s = _Session()
    s.fail = OSError("boom")
    assert await GfsDirectoryCache(lambda: s).directory(_conn()) is None
    assert await GfsDirectoryCache(lambda: None).directory(_conn()) is None


async def test_over_the_item_cap_fails_closed_and_logs(clock, monkeypatch, caplog):
    monkeypatch.setattr(mod, "MAX_DIRECTORY_IDS", 2)
    s = _Session(body=_dir("a", "b", "c"))
    with caplog.at_level(logging.WARNING):
        assert await GfsDirectoryCache(lambda: s).directory(_conn()) is None
    assert "refusing the directory" in caplog.text


async def test_concurrent_misses_share_one_fetch(clock):
    s = _Session(body=_dir("a"))
    s.gate = asyncio.Event()
    cache = GfsDirectoryCache(lambda: s)
    tasks = [asyncio.create_task(cache.lists(_conn(), "a")) for _ in range(5)]
    await asyncio.sleep(0)
    s.gate.set()
    assert await asyncio.gather(*tasks) == [True] * 5
    assert len(s.calls) == 1


async def test_a_failing_shared_fetch_raises_to_every_waiter(clock, monkeypatch):
    s = _Session(body=_dir("a"))
    s.gate = asyncio.Event()
    cache = GfsDirectoryCache(lambda: s)

    async def _boom(self, conn):
        await s.gate.wait()
        raise RuntimeError("bug")

    monkeypatch.setattr(GfsDirectoryCache, "_read", _boom)
    tasks = [asyncio.create_task(cache.directory(_conn())) for _ in range(2)]
    await asyncio.sleep(0)
    s.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)
    await asyncio.sleep(0)
    assert cache._inflight == {}


async def test_cancelling_the_first_caller_never_cancels_the_others(clock):
    """L7: the download is its own task; a cancelled caller is the only one
    that sees ``CancelledError``."""
    s = _Session(body=_dir("a"))
    s.gate = asyncio.Event()
    cache = GfsDirectoryCache(lambda: s)
    first = asyncio.create_task(cache.lists(_conn(), "a"))
    await asyncio.sleep(0)
    second = asyncio.create_task(cache.lists(_conn(), "a"))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    s.gate.set()
    assert await second is True
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(s.calls) == 1
    # The result was stored: no second download.
    assert await cache.lists(_conn(), "a")
    assert len(s.calls) == 1


async def test_a_fetch_every_caller_abandoned_still_completes_quietly(clock):
    s = _Session(body=_dir("a"))
    s.gate = asyncio.Event()
    cache = GfsDirectoryCache(lambda: s)
    only = asyncio.create_task(cache.directory(_conn()))
    await asyncio.sleep(0)
    only.cancel()
    s.gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert cache._inflight == {}
    assert await cache.directory(_conn()) == {"a": "trusted"}
    assert len(s.calls) == 1


async def test_stale_entries_of_other_connections_are_pruned(clock):
    s = _Session(body=_dir("a"))
    cache = GfsDirectoryCache(lambda: s)
    await cache.directory(_conn("gone"))
    clock[0] += mod.LISTING_TTL_S
    await cache.directory(_conn("live"))
    assert len(cache) == 1


async def test_forget_drops_everything(clock):
    s = _Session(body=_dir("a"))
    cache = GfsDirectoryCache(lambda: s)
    await cache.directory(_conn())
    cache.forget("ignored", "args")
    assert len(cache) == 0
