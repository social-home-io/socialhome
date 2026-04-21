"""Tests for social_home.platform.adapter — dataclasses, helpers, and factory."""

from __future__ import annotations

import pytest

from social_home.platform import build_platform_adapter
from social_home.platform.adapter import (
    ExternalUser,
    InstanceConfig,
    _extract_bearer,
)


# ── InstanceConfig ────────────────────────────────────────────────────────────


def test_instance_config_fields():
    """InstanceConfig stores all five fields and is frozen."""
    cfg = InstanceConfig(
        location_name="Home Sweet Home",
        latitude=51.5074,
        longitude=-0.1278,
        time_zone="Europe/London",
        currency="GBP",
    )
    assert cfg.location_name == "Home Sweet Home"
    assert cfg.latitude == 51.5074
    assert cfg.longitude == -0.1278
    assert cfg.time_zone == "Europe/London"
    assert cfg.currency == "GBP"


def test_instance_config_frozen():
    """InstanceConfig raises FrozenInstanceError on mutation attempt."""
    cfg = InstanceConfig(
        location_name="X",
        latitude=0.0,
        longitude=0.0,
        time_zone="UTC",
        currency="USD",
    )
    with pytest.raises((AttributeError, TypeError)):
        cfg.location_name = "Y"  # type: ignore[misc]


# ── ExternalUser ──────────────────────────────────────────────────────────────


def test_external_user_required_fields():
    """ExternalUser stores all required fields correctly."""
    user = ExternalUser(
        username="alice",
        display_name="Alice",
        picture_url="https://example.com/alice.jpg",
        is_admin=True,
    )
    assert user.username == "alice"
    assert user.display_name == "Alice"
    assert user.picture_url == "https://example.com/alice.jpg"
    assert user.is_admin is True
    assert user.email is None


def test_external_user_with_email():
    """ExternalUser accepts an optional email field."""
    user = ExternalUser(
        username="bob",
        display_name="Bob",
        picture_url=None,
        is_admin=False,
        email="bob@example.com",
    )
    assert user.email == "bob@example.com"


def test_external_user_frozen():
    """ExternalUser raises on mutation attempt."""
    user = ExternalUser(
        username="carol",
        display_name="Carol",
        picture_url=None,
        is_admin=False,
    )
    with pytest.raises((AttributeError, TypeError)):
        user.username = "mallory"  # type: ignore[misc]


# ── _extract_bearer ───────────────────────────────────────────────────────────


class _FakeRequest:
    """Minimal stand-in for aiohttp.web.Request."""

    def __init__(
        self,
        headers: dict | None = None,
        query: dict | None = None,
    ) -> None:
        self.headers = headers or {}
        self.query = query or {}


def test_extract_bearer_from_header():
    """Authorization: Bearer <token> header is extracted correctly."""
    req = _FakeRequest(headers={"Authorization": "Bearer my-secret-token"})
    assert _extract_bearer(req) == "my-secret-token"


def test_extract_bearer_from_query():
    """?token= query parameter is extracted when no Authorization header is present."""
    req = _FakeRequest(query={"token": "query-token"})
    assert _extract_bearer(req) == "query-token"


def test_extract_bearer_header_takes_precedence():
    """Authorization header token wins over ?token= query parameter."""
    req = _FakeRequest(
        headers={"Authorization": "Bearer header-token"},
        query={"token": "query-token"},
    )
    assert _extract_bearer(req) == "header-token"


def test_extract_bearer_missing():
    """Returns None when neither Authorization header nor ?token= is present."""
    req = _FakeRequest()
    assert _extract_bearer(req) is None


def test_extract_bearer_non_bearer_scheme():
    """Authorization header with a non-Bearer scheme returns None (falls to query)."""
    req = _FakeRequest(headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert _extract_bearer(req) is None


def test_extract_bearer_empty_token():
    """Authorization: Bearer with no token (empty string) returns None."""
    req = _FakeRequest(headers={"Authorization": "Bearer "})
    assert _extract_bearer(req) is None


# ── build_platform_adapter ────────────────────────────────────────────────────


def test_build_platform_adapter_ha(tmp_path):
    """build_platform_adapter('ha', ...) returns a HomeAssistantAdapter."""
    from social_home.config import Config
    from social_home.platform.ha.adapter import HomeAssistantAdapter

    cfg = Config(mode="ha")
    adapter = build_platform_adapter("ha", db=None, config=cfg)
    assert isinstance(adapter, HomeAssistantAdapter)


async def test_build_platform_adapter_standalone(tmp_path):
    """build_platform_adapter('standalone', ...) returns a StandaloneAdapter."""
    from social_home.config import Config
    from social_home.db.database import AsyncDatabase
    from social_home.platform.standalone.adapter import StandaloneAdapter

    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()
    try:
        cfg = Config(mode="standalone")
        adapter = build_platform_adapter("standalone", db=db, config=cfg)
        assert isinstance(adapter, StandaloneAdapter)
    finally:
        await db.shutdown()


def test_build_platform_adapter_unknown_mode():
    """build_platform_adapter raises ValueError for unrecognised mode strings."""
    from social_home.config import Config

    cfg = Config(mode="unknown")
    with pytest.raises(ValueError, match="Unknown platform mode"):
        build_platform_adapter("unknown", db=None, config=cfg)
