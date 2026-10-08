"""Tests for socialhome.rate_limiter."""

from __future__ import annotations

from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from socialhome.rate_limiter import (
    RateLimiter,
    build_rate_limit_middleware,
    client_bucket,
)


async def test_allows_within_limit():
    """Requests within the rate limit are all permitted."""
    times = iter([0.0, 0.1, 0.2])
    rl = RateLimiter(monotonic=lambda: next(times))
    assert await rl.check("u1", "/api/x", limit=3, window_s=1) is True
    assert await rl.check("u1", "/api/x", limit=3, window_s=1) is True
    assert await rl.check("u1", "/api/x", limit=3, window_s=1) is True


async def test_blocks_over_limit():
    """The request exceeding the limit is denied."""
    times = iter([0.0, 0.1, 0.2, 0.3])
    rl = RateLimiter(monotonic=lambda: next(times))
    for _ in range(3):
        await rl.check("u1", "/api/x", limit=3, window_s=1)
    assert await rl.check("u1", "/api/x", limit=3, window_s=1) is False


async def test_different_users_independent():
    """Rate limits are per-user; u2 is not affected by u1 hitting the limit."""
    times = iter([0.0, 0.1, 0.2, 0.3, 0.4])
    rl = RateLimiter(monotonic=lambda: next(times))
    for _ in range(3):
        await rl.check("u1", "/api/x", limit=3, window_s=1)
    assert await rl.check("u1", "/api/x", limit=3, window_s=1) is False
    assert await rl.check("u2", "/api/x", limit=3, window_s=1) is True


def test_sync_is_allowed():
    """Synchronous is_allowed helper works without awaiting."""
    t = [0.0]
    rl = RateLimiter(monotonic=lambda: t[0])
    assert rl.is_allowed("key", limit=2, window_s=1) is True
    assert rl.is_allowed("key", limit=2, window_s=1) is True
    assert rl.is_allowed("key", limit=2, window_s=1) is False


def test_picker_glob_pattern_matches_action_endpoints():
    """Glob patterns with `*` match the {id} segment via fnmatch."""
    import fnmatch

    pattern = "/api/spaces/*/ban"
    assert fnmatch.fnmatchcase("/api/spaces/sp-1/ban", pattern)
    assert fnmatch.fnmatchcase("/api/spaces/abc-xyz/ban", pattern)
    assert not fnmatch.fnmatchcase("/api/spaces/sp-1", pattern)
    assert not fnmatch.fnmatchcase("/api/spaces/sp-1/members", pattern)


def test_picker_prefix_match_still_works():
    """Plain-prefix patterns continue to match via startswith."""
    assert "/api/pairing/initiate".startswith("/api/pairing")
    assert "/api/pairing/connections".startswith("/api/pairing")
    assert not "/api/pages".startswith("/api/pairing")


def test_reset_clears_all_buckets():
    """reset() with no key empties every bucket."""
    rl = RateLimiter()
    rl.is_allowed("k", limit=1, window_s=60)
    rl.reset()
    assert rl.is_allowed("k", limit=1, window_s=60) is True


def test_reset_clears_single_bucket():
    """reset(key) clears only that one bucket, leaving others intact."""
    t = [0.0]
    rl = RateLimiter(monotonic=lambda: t[0])
    rl.is_allowed("a", limit=1, window_s=60)
    rl.is_allowed("b", limit=1, window_s=60)
    rl.reset("a")
    assert rl.is_allowed("a", limit=1, window_s=60) is True
    assert rl.is_allowed("b", limit=1, window_s=60) is False


async def test_check_uses_first_two_path_segments_as_bucket():
    """check() buckets by /segment-1/segment-2 regardless of trailing path."""
    rl = RateLimiter()
    assert (
        await rl.check(
            "u1",
            "/api/feed/posts/123/comments",
            limit=100,
            window_s=60,
        )
        is True
    )


async def _hit(mw, path: str, user: str = "u1") -> int:
    request = make_mocked_request("POST", path)
    request["user"] = SimpleNamespace(user_id=user)

    async def handler(_req):
        return web.Response(status=204)

    return (await mw(request, handler)).status


async def test_each_limit_rule_counts_only_its_own_requests():
    """Regression (live group call): every ``/api/calls/*`` request landed in
    one ``api/calls`` bucket whatever rule it matched, so 300/min of trickle
    ICE (or the quality sampler) used up the 10/min an initiate or a
    ``join`` is checked against — and the next call 429'd."""
    mw = build_rate_limit_middleware(
        RateLimiter(),
        limits={"/api/calls/*/ice": (300, 60), "/api/calls": (3, 60)},
    )
    for _ in range(20):
        assert await _hit(mw, "/api/calls/c1/ice") == 204
    for _ in range(3):
        assert await _hit(mw, "/api/calls") == 204
    assert await _hit(mw, "/api/calls") == 429
    # …and the broad rule's spend doesn't eat the specific one either.
    assert await _hit(mw, "/api/calls/c1/ice") == 204
    # Unmatched paths keep their per-first-two-segments default bucket.
    assert await _hit(mw, "/api/feed/posts") == 204


def test_retry_after_s_counts_down_to_the_oldest_stamp_leaving_the_window():
    """``Retry-After`` for a full sliding-window bucket is the time until
    its oldest request ages out — the first moment a new one is allowed."""
    now = [100.0]
    rl = RateLimiter(monotonic=lambda: now[0])
    assert rl.is_allowed("k", limit=2, window_s=60)
    now[0] = 110.0
    assert rl.is_allowed("k", limit=2, window_s=60)
    assert not rl.is_allowed("k", limit=2, window_s=60)
    now[0] = 120.0
    # Oldest stamp (100) leaves the 60 s window at 160 → 40 s from now.
    assert rl.retry_after_s("k", window_s=60) == 40
    now[0] = 159.5
    assert rl.retry_after_s("k", window_s=60) == 1  # rounded up, never 0


def test_retry_after_s_is_zero_for_an_empty_bucket():
    rl = RateLimiter()
    assert rl.retry_after_s("never-seen", window_s=60) == 0


async def test_the_most_specific_prefix_wins_whatever_the_order():
    """``/api/pairing/connections`` (per-peer reads, admin toggles) must not
    fall into the 5/min handshake bucket of ``/api/pairing`` — opening a
    connection's Manage panel twice and flipping a switch 429'd."""
    mw = build_rate_limit_middleware(
        RateLimiter(),
        limits={"/api/pairing": (1, 60), "/api/pairing/connections": (3, 60)},
    )
    for _ in range(3):
        assert await _hit(mw, "/api/pairing/connections/peer-1/visible-users") == 204
    assert await _hit(mw, "/api/pairing/connections/peer-1") == 429
    # The handshake endpoints keep the tight bucket of their own.
    assert await _hit(mw, "/api/pairing/initiate") == 204
    assert await _hit(mw, "/api/pairing/initiate") == 429


async def test_a_glob_rule_still_beats_a_literal_prefix():
    mw = build_rate_limit_middleware(
        RateLimiter(),
        limits={"/api/calls": (1, 60), "/api/calls/*/ice": (3, 60)},
    )
    for _ in range(3):
        assert await _hit(mw, "/api/calls/c1/ice") == 204
    assert await _hit(mw, "/api/calls/c1/ice") == 429
    assert await _hit(mw, "/api/calls") == 204


# ─── client_bucket — the per-address rate-limit key ──────────────────────


def test_client_bucket_keeps_ipv4_as_is():
    assert client_bucket("203.0.113.9") == "203.0.113.9"


def test_client_bucket_collapses_ipv6_to_its_64():
    """One IPv6 subscriber owns a whole /64 and can rotate through 2^64
    addresses — keying on the full address hands it a fresh bucket each
    time. Every address in the /64 shares one bucket."""
    a = client_bucket("2001:db8:1:2::1")
    b = client_bucket("2001:db8:1:2:ffff:ffff:ffff:fffe")
    assert a == b == "2001:db8:1:2::/64"


def test_client_bucket_separates_different_64s():
    assert client_bucket("2001:db8:1:2::1") != client_bucket("2001:db8:1:3::1")


def test_client_bucket_unmaps_ipv4_mapped_ipv6():
    assert client_bucket("::ffff:198.51.100.7") == "198.51.100.7"


def test_client_bucket_strips_zone_id():
    assert client_bucket("fe80::1%eth0") == client_bucket("fe80::2%eth1")


def test_client_bucket_missing_or_unparseable():
    assert client_bucket(None) == "unknown"
    assert client_bucket("") == "unknown"
    # Not an address (e.g. a unix-socket peer) — passed through, still a key.
    assert client_bucket("not-an-ip") == "not-an-ip"
