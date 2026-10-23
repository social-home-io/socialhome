"""Tests for AppCatalogService."""

import json
from typing import Any, Self

import aiohttp
import pytest
from aiohttp import web

from socialhome.services.app_catalog_service import (
    CATALOG_TTL_S,
    RELEASE_MAX_FIELD_SIZE,
    AppCatalogService,
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
    with pytest.raises(aiohttp.ClientResponseError):
        await svc.fetch_catalog()


async def test_release_session_bounds_the_header_size():
    assert RELEASE_MAX_FIELD_SIZE >= 32 * 1024
    async with release_session() as session:
        assert session._max_field_size == RELEASE_MAX_FIELD_SIZE
