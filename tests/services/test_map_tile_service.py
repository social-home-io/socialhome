"""Tests for :mod:`socialhome.services.map_tile_service`.

Unit tests only — no real network. The upstream is a stub session in the
same style as ``tests/services/test_public_space_discovery_service.py``
(an object with a ``get()`` returning an async context manager).
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from socialhome.services.map_tile_service import (
    MapTileService,
    Tile,
    TileCoordinateError,
    TileUnavailableError,
)

TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
UA = "SocialHome/1.0 (+https://social-home.io)"


# ─── Stub upstream ───────────────────────────────────────────────────────


class _StubContent:
    """Minimal stand-in for ``aiohttp`` ``resp.content``.

    Counts how many chunks the consumer actually pulled, so a test can
    tell a streaming cap from a read-it-all-then-check implementation.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.chunks_consumed = 0

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._body), n):
            self.chunks_consumed += 1
            yield self._body[i : i + n]


class _StubResp:
    def __init__(
        self,
        *,
        status: int = 200,
        content_type: str = "image/png",
        body: bytes = b"PNGDATA",
        content_length: int | None = None,
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            # Upstream-controlled and therefore untrustworthy: it may lie
            # in either direction.
            self.headers["Content-Length"] = str(content_length)
        self.content = _StubContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _StubSession:
    """Records every ``get()`` call; replays queued responses in order."""

    def __init__(self, *responses) -> None:
        # ``responses`` entries are either _StubResp factories (callables),
        # _StubResp instances, or exceptions to raise.
        self._responses = list(responses) or [_StubResp()]
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        item = (
            self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        )
        if isinstance(item, BaseException):
            raise item
        return item() if callable(item) else item


class _RaisingSession:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        raise self._exc


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _svc(session=None, **kw) -> MapTileService:
    svc = MapTileService(TEMPLATE, user_agent=UA, **kw)
    if session is not None:
        svc.attach_session(session)
    return svc


# ─── Happy path ──────────────────────────────────────────────────────────


async def test_fetch_returns_body_and_content_type():
    session = _StubSession(lambda: _StubResp(body=b"PNGDATA"))
    svc = _svc(session)
    tile = await svc.fetch(3, 2, 1)
    assert isinstance(tile, Tile)
    assert tile.body == b"PNGDATA"
    assert tile.content_type == "image/png"


async def test_fetch_sends_identifying_user_agent_and_gzip():
    """The identifying ``User-Agent`` IS the fix — pin it."""
    session = _StubSession()
    svc = _svc(session)
    await svc.fetch(3, 2, 1)
    _url, kwargs = session.calls[0]
    assert kwargs["headers"] == {
        "User-Agent": UA,
        "Accept-Encoding": "gzip",
    }


async def test_fetch_builds_url_from_template():
    session = _StubSession()
    svc = _svc(session)
    await svc.fetch(12, 2145, 1436)
    assert session.calls[0][0] == "https://tile.openstreetmap.org/12/2145/1436.png"


async def test_content_type_with_charset_suffix_is_accepted():
    session = _StubSession(lambda: _StubResp(content_type="image/png; charset=binary"))
    svc = _svc(session)
    tile = await svc.fetch(1, 0, 0)
    assert tile.content_type == "image/png"


@pytest.mark.parametrize(
    "ctype,expected",
    [
        ("image/png", "image/png"),
        ("image/jpeg", "image/jpeg"),
        ("image/webp", "image/webp"),
        # HTTP media types are case-insensitive; rejecting these would look
        # exactly like the grey-map bug we are fixing.
        ("Image/PNG", "image/png"),
        ("IMAGE/JPEG; charset=binary", "image/jpeg"),
    ],
)
async def test_all_allowed_image_types(ctype, expected):
    session = _StubSession(lambda: _StubResp(content_type=ctype))
    svc = _svc(session)
    assert (await svc.fetch(1, 0, 0)).content_type == expected


# ─── Coordinate validation (fail closed) ─────────────────────────────────


@pytest.mark.parametrize(
    "z,x,y",
    [
        (20, 0, 0),  # z too big
        (-1, 0, 0),  # negative z
        (1, -1, 0),  # negative x
        (1, 0, -1),  # negative y
        (1, 2, 0),  # x >= 2**z
        (1, 0, 2),  # y >= 2**z
        (0, 1, 0),  # x >= 2**0
        # Not a true int: ``True`` would render as ``1`` and a float as
        # ``0.5`` in the upstream path, and ``1 << 1.5`` is a bare TypeError.
        (True, 0, 0),
        (1, True, 0),
        (1, 0, True),
        (1.0, 0, 0),
        (1, 0.5, 0),
        (1, 0, 0.5),
    ],
)
async def test_invalid_coordinates_raise_and_make_no_http_call(z, x, y):
    session = _StubSession()
    svc = _svc(session)
    with pytest.raises(TileCoordinateError):
        await svc.fetch(z, x, y)
    assert session.calls == []


async def test_max_valid_coordinates_are_accepted():
    session = _StubSession()
    svc = _svc(session)
    await svc.fetch(19, 2**19 - 1, 2**19 - 1)
    assert len(session.calls) == 1


# ─── Caching ─────────────────────────────────────────────────────────────


async def test_fresh_cache_hit_makes_no_second_http_call():
    session = _StubSession()
    clock = _Clock()
    svc = _svc(session, clock=clock, refresh_after_seconds=100)
    first = await svc.fetch(1, 0, 0)
    clock.t += 50  # still fresh
    second = await svc.fetch(1, 0, 0)
    assert len(session.calls) == 1
    assert second == first


async def test_stale_entry_refetches_upstream():
    session = _StubSession(
        lambda: _StubResp(body=b"OLD"),
        lambda: _StubResp(body=b"NEW"),
    )
    clock = _Clock()
    svc = _svc(session, clock=clock, refresh_after_seconds=100)
    assert (await svc.fetch(1, 0, 0)).body == b"OLD"
    clock.t += 500
    assert (await svc.fetch(1, 0, 0)).body == b"NEW"
    assert len(session.calls) == 2


async def test_stale_entry_survives_upstream_failure():
    """A stale tile keeps the map alive; it is never evicted for age."""
    ok = _StubResp(body=b"OLD")
    session = _StubSession(lambda: ok, lambda: _StubResp(status=500))
    clock = _Clock()
    svc = _svc(session, clock=clock, refresh_after_seconds=100)
    await svc.fetch(1, 0, 0)
    clock.t += 500
    tile = await svc.fetch(1, 0, 0)
    assert tile.body == b"OLD"
    # Still retained for the next failure, too.
    clock.t += 500
    assert (await svc.fetch(1, 0, 0)).body == b"OLD"


# ─── Upstream failures ───────────────────────────────────────────────────


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_bad_status_with_empty_cache_raises(status):
    session = _StubSession(lambda: _StubResp(status=status))
    svc = _svc(session)
    with pytest.raises(TileUnavailableError):
        await svc.fetch(1, 0, 0)


async def test_non_image_content_type_raises():
    session = _StubSession(lambda: _StubResp(content_type="text/html"))
    svc = _svc(session)
    with pytest.raises(TileUnavailableError):
        await svc.fetch(1, 0, 0)


async def test_oversize_body_raises_and_is_not_cached():
    session = _StubSession(lambda: _StubResp(body=b"x" * 5000))
    svc = _svc(session, max_fetch_bytes=1000)
    with pytest.raises(TileUnavailableError):
        await svc.fetch(1, 0, 0)
    # Nothing partial was cached.
    with pytest.raises(TileUnavailableError):
        await svc.fetch(1, 0, 0)


async def test_transport_exception_raises():
    svc = _svc(_RaisingSession(OSError("connection reset")))
    with pytest.raises(TileUnavailableError):
        await svc.fetch(1, 0, 0)


async def test_upstream_failure_logs_warning(caplog):
    session = _StubSession(lambda: _StubResp(status=403))
    svc = _svc(session)
    with caplog.at_level("WARNING"), pytest.raises(TileUnavailableError):
        await svc.fetch(1, 0, 0)
    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert "403" in caplog.text


async def test_no_session_attached_raises_tile_unavailable():
    svc = MapTileService(TEMPLATE, user_agent=UA)
    with pytest.raises(TileUnavailableError):
        await svc.fetch(1, 0, 0)


async def test_attach_session_is_idempotent():
    first = _StubSession()
    second = _StubSession()
    svc = _svc(first)
    svc.attach_session(second)
    await svc.fetch(1, 0, 0)
    assert len(first.calls) == 1
    assert second.calls == []


# ─── LRU eviction ────────────────────────────────────────────────────────


async def test_lru_evicts_least_recently_used_and_keeps_recently_read():
    # Each tile is 10 bytes; cap 25 bytes → at most 2 tiles.
    session = _StubSession(
        lambda: _StubResp(body=b"A" * 10),
        lambda: _StubResp(body=b"B" * 10),
        lambda: _StubResp(body=b"C" * 10),
        lambda: _StubResp(body=b"A" * 10),
    )
    svc = _svc(session, max_cache_bytes=25, refresh_after_seconds=10_000)
    await svc.fetch(1, 0, 0)  # A
    await svc.fetch(1, 1, 0)  # B
    await svc.fetch(1, 0, 0)  # read A again → B is now the LRU
    assert len(session.calls) == 2
    await svc.fetch(1, 1, 1)  # C → evicts B
    # A survived: served from cache, no new request.
    assert (await svc.fetch(1, 0, 0)).body == b"A" * 10
    assert len(session.calls) == 3
    # B was evicted: refetched.
    await svc.fetch(1, 1, 0)
    assert len(session.calls) == 4


async def test_tile_larger_than_cache_cap_is_returned_but_not_cached():
    session = _StubSession(lambda: _StubResp(body=b"Z" * 100))
    svc = _svc(session, max_cache_bytes=10, refresh_after_seconds=10_000)
    tile = await svc.fetch(1, 0, 0)
    assert tile.body == b"Z" * 100
    await svc.fetch(1, 0, 0)
    assert len(session.calls) == 2


# ─── F1: a bad operator template is a tile failure, not a 500 ────────────


@pytest.mark.parametrize(
    "template,needle",
    [
        # The single most common mirror shape: a subdomain placeholder.
        ("https://{s}.tile.example/{z}/{x}/{y}.png", "{s}"),
        # A `{}` / `{0}` typo raises IndexError rather than KeyError.
        ("https://tile.example/{0}/{x}/{y}.png", "positional"),
        ("https://tile.example/{}/{x}/{y}.png", "positional"),
    ],
)
async def test_unsupported_template_placeholder_raises_tile_unavailable(
    template, needle, caplog
):
    session = _StubSession()
    svc = MapTileService(template, user_agent=UA)
    svc.attach_session(session)
    with caplog.at_level("WARNING"), pytest.raises(TileUnavailableError):
        await svc.fetch(1, 0, 0)
    assert session.calls == []
    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert needle in caplog.text


async def test_bad_template_never_logs_the_template_itself(caplog):
    """The template can carry an API key — only the placeholder is named."""
    svc = MapTileService(
        "https://{s}.tile.example/{z}/{x}/{y}.png?apikey=SUPERSECRET123",
        user_agent=UA,
    )
    svc.attach_session(_StubSession())
    with caplog.at_level("WARNING"), pytest.raises(TileUnavailableError) as err:
        await svc.fetch(1, 0, 0)
    assert "SUPERSECRET123" not in caplog.text
    assert "SUPERSECRET123" not in str(err.value)


# ─── F2: the upstream query string (API keys) never reaches a log ────────

SECRET = "SUPERSECRET123"
KEYED_TEMPLATE = f"https://tiles.example.com/{{z}}/{{x}}/{{y}}.png?apikey={SECRET}"


def _keyed_svc(session, **kw) -> MapTileService:
    svc = MapTileService(KEYED_TEMPLATE, user_agent=UA, **kw)
    svc.attach_session(session)
    return svc


def _assert_no_secret(caplog, err) -> None:
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert SECRET not in str(record.msg)
    assert SECRET not in caplog.text
    assert SECRET not in str(err.value)


@pytest.mark.parametrize(
    "session_factory",
    [
        # bad status
        lambda: _StubSession(lambda: _StubResp(status=403)),
        # bad content type
        lambda: _StubSession(lambda: _StubResp(content_type="text/html")),
        # oversize body
        lambda: _StubSession(lambda: _StubResp(body=b"x" * 5000)),
        # transport error whose own text echoes the full URL
        lambda: _RaisingSession(
            OSError(
                f"cannot connect to https://tiles.example.com/1/0/0.png?apikey={SECRET}"
            )
        ),
    ],
)
async def test_api_key_never_appears_in_logs_or_exceptions(session_factory, caplog):
    # max_fetch_bytes=1000 forces the oversize path for the 5000-byte body.
    svc = _keyed_svc(session_factory(), max_fetch_bytes=1000)
    with caplog.at_level("DEBUG"), pytest.raises(TileUnavailableError) as err:
        await svc.fetch(1, 0, 0)
    _assert_no_secret(caplog, err)
    # The host + path are still logged, so a failure stays diagnosable.
    assert "tiles.example.com" in caplog.text


async def test_missing_session_log_omits_the_query(caplog):
    svc = MapTileService(KEYED_TEMPLATE, user_agent=UA)
    with caplog.at_level("WARNING"), pytest.raises(TileUnavailableError) as err:
        await svc.fetch(1, 0, 0)
    _assert_no_secret(caplog, err)


# ─── F3: the byte cap is a streaming cap, and Content-Length is a lie ────


async def test_oversize_body_stops_reading_instead_of_draining_the_stream():
    """A read-it-all-then-check implementation would fail this test."""
    body = b"x" * 1_000_000
    resp = _StubResp(body=body)
    session = _StubSession(resp)
    svc = _svc(session, max_fetch_bytes=1000)
    with pytest.raises(TileUnavailableError):
        await svc.fetch(1, 0, 0)
    # Many chunks were available; the service stopped as soon as the cap
    # was crossed rather than buffering the whole million bytes.
    assert resp.content.chunks_consumed <= 2
    assert len(body) // 1001 > 100  # the stream really did have many chunks


async def test_huge_content_length_header_is_not_trusted():
    """A lying (huge) Content-Length must not reject a small real body."""
    session = _StubSession(lambda: _StubResp(body=b"PNGDATA", content_length=10**9))
    svc = _svc(session, max_fetch_bytes=1000)
    assert (await svc.fetch(1, 0, 0)).body == b"PNGDATA"


async def test_small_content_length_header_does_not_excuse_a_huge_body():
    """The reverse lie: a tiny header with an oversize body still fails."""
    session = _StubSession(lambda: _StubResp(body=b"x" * 50_000, content_length=7))
    svc = _svc(session, max_fetch_bytes=1000)
    with pytest.raises(TileUnavailableError):
        await svc.fetch(1, 0, 0)


# ─── F4a: in-flight fetches are bounded ──────────────────────────────────


class _ConcurrencyProbeSession:
    """Tracks how many upstream fetches are open at the same moment."""

    def __init__(self) -> None:
        self.live = 0
        self.peak = 0
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self._Resp(self)

    class _Resp:
        def __init__(self, owner) -> None:
            self._owner = owner
            self.status = 200
            self.headers = {"Content-Type": "image/png"}
            self.content = _StubContent(b"PNGDATA")

        async def __aenter__(self):
            self._owner.live += 1
            self._owner.peak = max(self._owner.peak, self._owner.live)
            await asyncio.sleep(0.01)
            return self

        async def __aexit__(self, *a):
            self._owner.live -= 1
            return False


async def test_concurrent_fetches_are_bounded_by_the_semaphore():
    session = _ConcurrencyProbeSession()
    svc = _svc(session, max_concurrent_fetches=2)
    await asyncio.gather(*(svc.fetch(4, i, 0) for i in range(8)))
    assert len(session.calls) == 8
    assert session.peak <= 2


# ─── F4b: an upstream outage is not hammered while a stale tile exists ───


async def test_stale_serve_backs_off_instead_of_hammering_upstream():
    session = _StubSession(
        lambda: _StubResp(body=b"OLD"),
        lambda: _StubResp(status=500),
    )
    clock = _Clock()
    svc = _svc(
        session,
        clock=clock,
        refresh_after_seconds=100,
        failure_backoff_seconds=60,
    )
    await svc.fetch(1, 0, 0)  # populate
    clock.t += 500  # now stale
    assert len(session.calls) == 1

    # First stale request tries upstream once and fails over to the stale tile.
    assert (await svc.fetch(1, 0, 0)).body == b"OLD"
    assert len(session.calls) == 2

    # Every request inside the backoff window is served from the stale entry
    # without touching the dead upstream.
    for _ in range(10):
        clock.t += 1
        assert (await svc.fetch(1, 0, 0)).body == b"OLD"
    assert len(session.calls) == 2

    # Once the window has elapsed, upstream is tried again.
    clock.t += 60
    assert (await svc.fetch(1, 0, 0)).body == b"OLD"
    assert len(session.calls) == 3


async def test_backoff_clears_after_a_successful_fetch():
    session = _StubSession(
        lambda: _StubResp(body=b"OLD"),
        lambda: _StubResp(status=500),
        lambda: _StubResp(body=b"NEW"),
        lambda: _StubResp(body=b"NEWER"),
    )
    clock = _Clock()
    svc = _svc(
        session,
        clock=clock,
        refresh_after_seconds=100,
        failure_backoff_seconds=60,
    )
    await svc.fetch(1, 0, 0)
    clock.t += 500
    assert (await svc.fetch(1, 0, 0)).body == b"OLD"  # failure recorded
    clock.t += 100  # window elapsed
    assert (await svc.fetch(1, 0, 0)).body == b"NEW"  # success clears backoff
    clock.t += 500
    assert (await svc.fetch(1, 0, 0)).body == b"NEWER"  # not suppressed
    assert len(session.calls) == 4


async def test_failure_with_no_cached_tile_still_reaches_upstream_every_time():
    """Backoff only applies when there is something stale to serve.

    Deliberate, not an oversight: a cold-cache circuit breaker would let
    ONE legitimate 404 — an ocean tile at high zoom is a normal miss —
    blackhole every map for the whole backoff window. With nothing
    cached there is also nothing to protect the user from, so the
    request goes upstream and the bounded semaphore + rate limit are
    what cap the traffic. Do not "fix" this into a breaker.
    """
    session = _StubSession(lambda: _StubResp(status=500))
    clock = _Clock()
    svc = _svc(session, clock=clock, failure_backoff_seconds=60)
    for _ in range(3):
        with pytest.raises(TileUnavailableError):
            await svc.fetch(1, 0, 0)
    assert len(session.calls) == 3


# ─── F7: the request timeout is bounded ──────────────────────────────────


async def test_fetch_sends_a_bounded_timeout():
    session = _StubSession()
    svc = _svc(session)
    await svc.fetch(3, 2, 1)
    _url, kwargs = session.calls[0]
    timeout = kwargs["timeout"]
    assert isinstance(timeout, aiohttp.ClientTimeout)
    assert timeout.total == 15
