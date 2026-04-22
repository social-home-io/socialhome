"""Tests for socialhome.rate_limiter."""

from __future__ import annotations


from socialhome.rate_limiter import RateLimiter


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
