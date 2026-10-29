"""Tests for AppCatalogService."""

import asyncio
import json
from typing import Any, Self

import aiohttp
import pytest
from aiohttp import web

from socialhome.domain.apps import (
    MAX_CATALOG_BYTES,
    AppBundleTooLargeError,
    AppCatalogUnreachableError,
    AppDownloadError,
)
from socialhome.services import app_catalog_service as catalog_mod
from socialhome.services.app_catalog_service import (
    CATALOG_RETRY_S,
    CATALOG_TTL_S,
    RELEASE_MAX_FIELD_SIZE,
    RELEASE_TIMEOUT_S,
    AppCatalogService,
    catalog_source_host,
    download_bundle,
    release_session,
)


class _FakeResp:
    """Fake aiohttp response for testing."""

    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *a: object) -> bool:
        return False

    async def text(self) -> str:
        return json.dumps(self._payload)

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _CountingSession:
    """Fake aiohttp ClientSession that counts how many times .get() is called."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.get_call_count: int = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *a: object) -> bool:
        return False

    def get(self, url: str, **kw: Any) -> _FakeResp:
        self.get_call_count += 1
        return _FakeResp(self._payload)


class _FakeSession:
    """Fake aiohttp ClientSession for testing."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *a: object) -> bool:
        return False

    def get(self, url: str, **kw: Any) -> _FakeResp:
        return _FakeResp(self._payload)


@pytest.mark.asyncio
async def test_fetch_catalog_parses_entries():
    """fetch_catalog should parse valid entries."""
    payload = {
        "apps": [
            {
                "app_id": "chess",
                "name": "Chess",
                "latest_version": "1.0.0",
                "description": "Play chess",
                "icon_url": None,
                "capabilities": ["storage", "federation"],
                "bundle_url": "https://example/chess-1.0.0.tgz",
                "bundle_sha256": "cd" * 32,
            }
        ]
    }
    svc = AppCatalogService(
        session_factory=lambda: _FakeSession(payload),
        catalog_url="https://example/catalog.json",
    )
    entries = await svc.fetch_catalog()
    assert len(entries) == 1
    assert entries[0].app_id == "chess"


@pytest.mark.asyncio
async def test_fetch_catalog_skips_malformed_entries():
    """fetch_catalog should skip entries that fail from_dict."""
    payload = {"apps": [{"app_id": "broken"}]}  # missing required fields
    svc = AppCatalogService(
        session_factory=lambda: _FakeSession(payload),
        catalog_url="https://example/catalog.json",
    )
    assert await svc.fetch_catalog() == []


# ─── TTL cache tests ──────────────────────────────────────────────────────────


def _chess_payload() -> dict[str, Any]:
    return {
        "apps": [
            {
                "app_id": "chess",
                "name": "Chess",
                "latest_version": "1.0.0",
                "description": "Play chess",
                "icon_url": None,
                "capabilities": [],
                "bundle_url": "https://example/chess-1.0.0.tgz",
                "bundle_sha256": "cd" * 32,
            }
        ]
    }


@pytest.mark.asyncio
async def test_second_fetch_within_ttl_uses_cache(monkeypatch: pytest.MonkeyPatch):
    """A 2nd fetch_catalog() within TTL must NOT call the session again."""
    session = _CountingSession(_chess_payload())
    svc = AppCatalogService(
        session_factory=lambda: session,
        catalog_url="https://example/catalog.json",
    )

    first = await svc.fetch_catalog()
    assert len(first) == 1

    second = await svc.fetch_catalog()
    assert second == first

    # The HTTP layer must have been called exactly once
    assert session.get_call_count == 1


@pytest.mark.asyncio
async def test_force_refresh_ignores_cache(monkeypatch: pytest.MonkeyPatch):
    """force=True bypasses the TTL cache and re-fetches."""
    session = _CountingSession(_chess_payload())
    svc = AppCatalogService(
        session_factory=lambda: session,
        catalog_url="https://example/catalog.json",
    )

    await svc.fetch_catalog()
    assert session.get_call_count == 1

    await svc.fetch_catalog(force=True)
    assert session.get_call_count == 2


@pytest.mark.asyncio
async def test_expired_ttl_refetches(monkeypatch: pytest.MonkeyPatch):
    """A cached result older than CATALOG_TTL_S must trigger a re-fetch."""

    session = _CountingSession(_chess_payload())
    svc = AppCatalogService(
        session_factory=lambda: session,
        catalog_url="https://example/catalog.json",
    )

    # Fetch once — cache is populated
    await svc.fetch_catalog()
    assert session.get_call_count == 1

    # Wind the cache timestamp back past the TTL threshold
    assert svc.last_fetched_monotonic is not None
    monkeypatch.setattr(
        svc,
        "_cache_ts",
        svc.last_fetched_monotonic - CATALOG_TTL_S - 1.0,
    )

    # A non-force fetch should see the stale cache and re-fetch
    await svc.fetch_catalog()
    assert session.get_call_count == 2


@pytest.mark.asyncio
async def test_last_fetched_monotonic_is_none_before_first_fetch():
    """last_fetched_monotonic is None before any fetch."""
    svc = AppCatalogService(
        session_factory=lambda: _FakeSession(_chess_payload()),
        catalog_url="https://example/catalog.json",
    )
    assert svc.last_fetched_monotonic is None


@pytest.mark.asyncio
async def test_last_fetched_monotonic_set_after_fetch():
    """last_fetched_monotonic is set to a positive value after a fetch."""
    svc = AppCatalogService(
        session_factory=lambda: _FakeSession(_chess_payload()),
        catalog_url="https://example/catalog.json",
    )
    await svc.fetch_catalog()
    assert svc.last_fetched_monotonic is not None
    assert svc.last_fetched_monotonic > 0


# ── GitHub's release download headers (2026-10-23) ─────────────────────
#
# ``github.com/…/releases/latest/download/catalog.json`` answers with a
# Content-Security-Policy header longer than aiohttp's default 8 190-byte
# field limit, so every catalog fetch (and every bundle download) failed
# with ``400 Got more than 8190 bytes when reading`` — Browse answered 500.

_LONG_CSP = "default-src 'none'; " + "; ".join(
    f"img-src https://cdn{i}.githubassets.example" for i in range(400)
)


@pytest.fixture
async def long_header_server(aiohttp_server):
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {"apps": []}, headers={"Content-Security-Policy": _LONG_CSP}
        )

    app = web.Application()
    app.router.add_get("/catalog.json", handler)
    return await aiohttp_server(app)


async def test_a_release_host_header_over_8190_bytes_is_read(long_header_server):
    assert len(_LONG_CSP) > 8190
    url = str(long_header_server.make_url("/catalog.json"))
    svc = AppCatalogService(session_factory=release_session, catalog_url=url)
    assert await svc.fetch_catalog() == []


async def test_the_default_session_limit_is_what_broke(long_header_server):
    """The regression this guards: aiohttp's default refuses the header."""
    url = str(long_header_server.make_url("/catalog.json"))
    svc = AppCatalogService(session_factory=aiohttp.ClientSession, catalog_url=url)
    with pytest.raises(AppCatalogUnreachableError) as info:
        await svc.fetch_catalog()
    assert isinstance(info.value.__cause__, aiohttp.ClientResponseError)


async def test_release_session_bounds_the_header_size():
    assert RELEASE_MAX_FIELD_SIZE >= 32 * 1024
    async with release_session() as session:
        assert session._max_field_size == RELEASE_MAX_FIELD_SIZE


# ─── Bundle download: size cap + explicit timeout ────────────────────────────


async def test_release_session_has_an_explicit_timeout():
    """The Install button never spins for aiohttp's 5-minute default."""
    async with release_session() as session:
        assert session.timeout.total == RELEASE_TIMEOUT_S
    assert RELEASE_TIMEOUT_S <= 120


@pytest.fixture
async def bundle_server(aiohttp_server):
    async def fixed(request: web.Request) -> web.Response:
        return web.Response(body=b"x" * int(request.query.get("n", "10")))

    async def streamed(request: web.Request) -> web.StreamResponse:
        # Chunked: no Content-Length to refuse up front.
        resp = web.StreamResponse()
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for _ in range(int(request.query.get("chunks", "4"))):
            await resp.write(b"y" * 1024)
        await resp.write_eof()
        return resp

    async def slow(request: web.Request) -> web.Response:
        await asyncio.sleep(5)
        return web.Response(body=b"late")

    async def missing(request: web.Request) -> web.Response:
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/fixed.tgz", fixed)
    app.router.add_get("/streamed.tgz", streamed)
    app.router.add_get("/slow.tgz", slow)
    app.router.add_get("/missing.tgz", missing)
    return await aiohttp_server(app)


async def test_download_bundle_returns_the_body(bundle_server):
    url = str(bundle_server.make_url("/fixed.tgz?n=10"))
    assert await download_bundle(url) == b"x" * 10


async def test_download_bundle_refuses_a_declared_size_over_the_cap(
    bundle_server, monkeypatch
):
    monkeypatch.setattr(catalog_mod, "MAX_BUNDLE_DOWNLOAD_BYTES", 100)
    url = str(bundle_server.make_url("/fixed.tgz?n=101"))
    with pytest.raises(AppBundleTooLargeError):
        await download_bundle(url)


async def test_download_bundle_stops_reading_a_stream_past_the_cap(
    bundle_server, monkeypatch
):
    monkeypatch.setattr(catalog_mod, "MAX_BUNDLE_DOWNLOAD_BYTES", 2048)
    with pytest.raises(AppBundleTooLargeError):
        await download_bundle(str(bundle_server.make_url("/streamed.tgz?chunks=4")))
    # At the cap exactly is fine.
    body = await download_bundle(str(bundle_server.make_url("/streamed.tgz?chunks=2")))
    assert len(body) == 2048


async def test_download_bundle_times_out_with_a_clear_error(bundle_server, monkeypatch):
    monkeypatch.setattr(catalog_mod, "RELEASE_TIMEOUT_S", 0.2)
    with pytest.raises(AppDownloadError) as info:
        await download_bundle(str(bundle_server.make_url("/slow.tgz")))
    assert info.value.status == 502
    assert info.value.code == "APP_DOWNLOAD_FAILED"


async def test_download_bundle_http_error_is_a_download_failure(bundle_server):
    with pytest.raises(AppDownloadError):
        await download_bundle(str(bundle_server.make_url("/missing.tgz")))


# ─── Unreachable catalog: serve the last good answer, say it's stale ─────────


class _FlakySession:
    """A session whose .get() answers with *payload* or fails with *error*,
    switchable between calls; counts the calls."""

    def __init__(self, payload: dict[str, Any] | str) -> None:
        self.payload: dict[str, Any] | str = payload
        self.error: BaseException | None = None
        self.calls = 0

    def __call__(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *a: object) -> bool:
        return False

    def get(self, url: str, **kw: Any) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        payload = self.payload
        if isinstance(payload, str):
            return _RawResp(payload)
        return _FakeResp(payload)


class _RawResp(_FakeResp):
    def __init__(self, body: str) -> None:
        super().__init__({})
        self._body = body

    async def text(self) -> str:
        return self._body


def _flaky_service() -> tuple[AppCatalogService, _FlakySession]:
    session = _FlakySession(_chess_payload())
    svc = AppCatalogService(
        session_factory=session, catalog_url="https://example/catalog.json"
    )
    return svc, session


async def test_status_before_any_fetch_is_never_checked():
    svc, _ = _flaky_service()
    assert svc.status.checked_at is None
    assert svc.status.stale is False


async def test_a_good_fetch_records_when_the_catalog_answered():
    svc, _ = _flaky_service()
    await svc.fetch_catalog()
    status = svc.status
    assert status.stale is False
    assert status.checked_at is not None
    assert status.checked_at.endswith("+00:00")  # UTC


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientConnectionError("down"),
        TimeoutError(),
    ],
)
async def test_a_failed_refresh_serves_the_last_good_catalog_as_stale(error):
    svc, session = _flaky_service()
    first = await svc.fetch_catalog()
    answered_at = svc.status.checked_at
    session.error = error
    again = await svc.fetch_catalog(force=True)
    assert [e.app_id for e in again] == [e.app_id for e in first]
    assert svc.status.stale is True
    # Still the time the catalog last answered, not the failed try.
    assert svc.status.checked_at == answered_at


async def test_a_body_that_is_not_a_catalog_is_unreachable_not_a_crash():
    svc, session = _flaky_service()
    await svc.fetch_catalog()
    session.payload = "<html>proxy error</html>"
    assert len(await svc.fetch_catalog(force=True)) == 1
    assert svc.status.stale is True
    session.payload = "[1, 2]"  # JSON, but not a catalog object
    assert len(await svc.fetch_catalog(force=True)) == 1
    assert svc.status.stale is True


async def test_unreachable_with_nothing_held_raises_a_coded_error():
    svc, session = _flaky_service()
    session.error = aiohttp.ClientConnectionError("down")
    with pytest.raises(AppCatalogUnreachableError) as info:
        await svc.fetch_catalog()
    assert info.value.status == 503
    assert info.value.code == "APP_CATALOG_UNREACHABLE"
    assert svc.status.stale is True
    assert svc.status.checked_at is None


async def test_after_a_failure_reads_answer_at_once_until_the_retry_window():
    """A page load while the host is down doesn't wait on the network
    again for CATALOG_RETRY_S; an explicit check (force) always retries."""
    svc, session = _flaky_service()
    await svc.fetch_catalog()
    # Expire the TTL so a plain read would go to the network.
    svc._cache_ts = svc._cache_ts - CATALOG_TTL_S - 1  # type: ignore[operator]
    session.error = aiohttp.ClientConnectionError("down")
    await svc.fetch_catalog()
    assert session.calls == 2
    await svc.fetch_catalog()  # inside the retry window: no network
    assert session.calls == 2
    await svc.fetch_catalog(force=True)
    assert session.calls == 3
    # Past the window a plain read tries again.
    svc._failed_ts = svc._failed_ts - CATALOG_RETRY_S - 1  # type: ignore[operator]
    await svc.fetch_catalog()
    assert session.calls == 4


async def test_inside_the_retry_window_with_nothing_held_raises_without_network():
    svc, session = _flaky_service()
    session.error = aiohttp.ClientConnectionError("down")
    with pytest.raises(AppCatalogUnreachableError):
        await svc.fetch_catalog()
    with pytest.raises(AppCatalogUnreachableError):
        await svc.fetch_catalog()
    assert session.calls == 1


async def test_the_catalog_answering_again_clears_stale():
    svc, session = _flaky_service()
    await svc.fetch_catalog()
    session.error = aiohttp.ClientConnectionError("down")
    await svc.fetch_catalog(force=True)
    assert svc.status.stale is True
    session.error = None
    await svc.fetch_catalog(force=True)
    assert svc.status.stale is False


# ─── Catalog v2: bounded body, honest source label (D6) ──────────────────────


async def test_a_catalog_over_the_size_cap_is_refused():
    """r2-proposal §7: the whole catalog is at most MAX_CATALOG_BYTES —
    a bigger body is treated as unreachable (the held copy is served)."""
    session = _FlakySession(_chess_payload())
    svc = AppCatalogService(
        session_factory=session, catalog_url="https://example/catalog.json"
    )
    assert [e.app_id for e in await svc.fetch_catalog()] == ["chess"]
    session.payload = '{"apps": [], "pad": "' + "x" * MAX_CATALOG_BYTES + '"}'
    assert [e.app_id for e in await svc.fetch_catalog(force=True)] == ["chess"]
    assert svc.status.stale is True


async def test_a_declared_length_over_the_cap_is_refused_before_reading():
    class _Big(_FakeResp):
        content_length = MAX_CATALOG_BYTES + 1

        async def text(self) -> str:  # pragma: no cover - must not be read
            raise AssertionError("read a body declared too large")

    class _Session(_FlakySession):
        def get(self, url: str, **kw: Any) -> Any:
            return _Big({})

    svc = AppCatalogService(
        session_factory=_Session({}), catalog_url="https://example/catalog.json"
    )
    with pytest.raises(AppCatalogUnreachableError):
        await svc.fetch_catalog()


@pytest.mark.parametrize(
    ("url", "host"),
    [
        (
            "https://github.com/social-home-io/socialhome-apps/releases/latest/download/catalog.json",
            None,
        ),
        (
            "https://github.com/social-home-io/socialhome-apps/releases/download/2026.10.29.1/catalog.json",
            None,
        ),
        ("https://apps.example.org/catalog.json", "apps.example.org"),
        ("https://github.com/someone-else/apps/releases/catalog.json", "github.com"),
    ],
)
def test_catalog_source_host(url, host):
    assert catalog_source_host(url) == host
    svc = AppCatalogService(session_factory=lambda: None, catalog_url=url)
    assert svc.source_host == host
