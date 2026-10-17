"""Tests for AppCatalogService."""

import json
from typing import Any, Self

import pytest

from socialhome.services.app_catalog_service import CATALOG_TTL_S, AppCatalogService


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
