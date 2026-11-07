"""The on-demand service's policy and pipe (v_83) — unit level; the HTTP
flows are in ``tests/global_server/routes/test_space_media_on_demand.py``."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from socialhome.domain.gfs_space_media_on_demand import MODE_CACHE, MODE_STREAM
from socialhome.global_server import space_media_on_demand as mod
from socialhome.global_server.config import SpaceMediaOnDemandConfig
from socialhome.global_server.public import ClientIpResolver
from socialhome.global_server.space_media import SpaceMediaLimits
from socialhome.global_server.space_media_on_demand import (
    GfsSpaceMediaOnDemand,
    MediaSession,
    StreamFailed,
    build_space_media_wanted_rate_limit,
)


class _Store:
    def __init__(self, *, total: int = 0, max_bytes: int = 1000) -> None:
        self.total = total
        self.limits = SpaceMediaLimits(
            max_bytes=max_bytes,
            ttl_s=3600,
            space_max_bytes=1000,
            space_uploads_per_hour=1,
        )

    async def totals(self) -> tuple[int, int]:
        return 1, self.total


def _svc(store: _Store, **cfg) -> GfsSpaceMediaOnDemand:
    return GfsSpaceMediaOnDemand(
        store=store,  # type: ignore[arg-type]
        demand=None,  # type: ignore[arg-type]
        federation=None,  # type: ignore[arg-type]
        channels=None,  # type: ignore[arg-type]
        config=SpaceMediaOnDemandConfig(**cfg),
    )


NOW = 10_000_000


@pytest.mark.parametrize(
    "requests,stored_ago,total,expected",
    [
        # Old or never-seen, one-off: stream.
        (1, None, 0, MODE_STREAM),
        (1, 8 * 86_400, 0, MODE_STREAM),
        # Fresh while the cache has room: cache.
        (1, 3600, 0, MODE_CACHE),
        # Fresh, but the cache is full (≥ 95 %): stream.
        (1, 3600, 950, MODE_STREAM),
        (2, 3600, 949, MODE_CACHE),
        # Asked N times in the window: cache, even full and old.
        (3, None, 1000, MODE_CACHE),
    ],
)
async def test_cache_or_stream(requests, stored_ago, total, expected):
    svc = _svc(_Store(total=total))
    stored_at = None if stored_ago is None else NOW - stored_ago
    assert await svc.decide(requests, stored_at, now=NOW) == expected


def test_capability_follows_the_switch():
    assert _svc(_Store()).capability() == {"wait_s": 25}
    assert _svc(_Store(), enabled=False).capability() is None
    assert _svc(_Store(max_bytes=0)).capability() is None


def _session(size: int) -> MediaSession:
    return MediaSession(
        ticket="t",
        blob_id="b",
        scope_kind="space",
        scope_id="s",
        mode="stream",
        deadline=0,
    )


async def _drain(session: MediaSession) -> bytes:
    out = b""
    async for chunk in session.chunks():
        out += chunk
    return out


async def test_the_last_byte_waits_for_the_address_check():
    s = _session(4)
    s.size = 4
    reader = asyncio.create_task(_drain(s))
    s.feed(b"ab")
    s.feed(b"cd")
    await asyncio.sleep(0.01)
    assert s.visible() == 3 and not reader.done()
    s.finish(ok=True)
    assert await reader == b"abcd"


async def test_a_failed_pipe_never_completes_a_reader():
    s = _session(4)
    s.size = 4
    reader = asyncio.create_task(_drain(s))
    s.feed(b"abcd")
    await asyncio.sleep(0.01)
    s.finish(ok=False)
    with pytest.raises(StreamFailed):
        await reader


async def test_a_late_joiner_gets_the_whole_blob():
    s = _session(3)
    s.size = 3
    s.feed(b"xyz")
    s.finish(ok=True)
    assert await _drain(s) == b"xyz"


async def test_the_wanted_limiter_sheds_per_client():
    resolver = ClientIpResolver(trusted_proxies=())
    app = web.Application(
        middlewares=[build_space_media_wanted_rate_limit(resolver, nodes=lambda: 1)]
    )

    async def ok(_request):
        return web.Response(text="ok")

    app.router.add_post("/gfs/media/{blob_id:[0-9a-f]{64}}/wanted", ok)
    app.router.add_post("/other", ok)
    async with TestClient(TestServer(app)) as tc:
        url = f"/gfs/media/{'a' * 64}/wanted"
        codes = [
            (await tc.post(url)).status
            for _ in range(mod.WANTED_MAX_PER_MINUTE_PER_IP + 1)
        ]
        assert codes[:-1] == [200] * mod.WANTED_MAX_PER_MINUTE_PER_IP
        assert codes[-1] == 429
        assert (await tc.post("/other")).status == 200


def test_waiting_readers_are_bounded_per_client_and_prefix():
    gate = _svc(_Store()).wait_gate
    assert all(gate.enter("c1", "p1", 0) for _ in range(mod.WAIT_PER_CLIENT))
    assert not gate.enter("c1", "p1", 0)
    assert all(
        gate.enter("c2", "p1", 0)
        for _ in range(mod.WAIT_PER_PREFIX - mod.WAIT_PER_CLIENT)
    )
    assert not gate.enter("c3", "p1", 0)
    assert gate.enter("c3", "p2", 0)


class _BudgetStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self.used: dict[str, int] = {}

    async def reserve_hourly(self, label: str, size: int, cap: int) -> bool:
        if self.used.get(label, 0) + size > cap:
            return False
        self.used[label] = self.used.get(label, 0) + size
        return True


async def test_one_reader_prefix_gets_only_its_share_of_a_scopes_stream_bytes():
    """Review M3: one network prefix can't use up a scope's stream budget."""
    store = _BudgetStore()
    svc = _svc(store, scope_stream_bytes_per_hour=800)  # type: ignore[arg-type]
    mine = _session(0)
    mine.opener_prefix = "p1"
    other = _session(0)
    other.opener_prefix = "p2"
    assert await svc.charge_stream(mine, 100)
    assert not await svc.charge_stream(mine, 1)  # its 1/8 share is used up
    assert await svc.charge_stream(other, 100)
