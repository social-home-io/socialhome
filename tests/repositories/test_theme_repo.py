"""Tests for SqliteThemeRepo (household + per-space themes)."""

from __future__ import annotations

import pytest

from social_home.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from social_home.db.database import AsyncDatabase
from social_home.repositories.theme_repo import (
    HouseholdTheme,
    SqliteThemeRepo,
    validate_color,
)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'X', ?, 'pascal', ?)",
        (iid, "ab" * 32),
    )
    yield SqliteThemeRepo(db)
    await db.shutdown()


# ─── validate_color ──────────────────────────────────────────────────────


def test_validate_color_accepts_six_digit_hex():
    assert validate_color("#abcdef") == "#abcdef"
    assert validate_color("#ABCDEF") == "#abcdef"


def test_validate_color_rejects_three_digit_form():
    with pytest.raises(ValueError):
        validate_color("#abc")


def test_validate_color_rejects_missing_hash():
    with pytest.raises(ValueError):
        validate_color("ff0000")


def test_validate_color_rejects_non_hex():
    with pytest.raises(ValueError):
        validate_color("#ggggzz")


def test_validate_color_rejects_non_string():
    with pytest.raises(ValueError):
        validate_color(0xABCDEF)  # type: ignore[arg-type]


# ─── household ───────────────────────────────────────────────────────────


async def test_get_household_returns_defaults_when_unset(env):
    theme = await env.get_household()
    assert isinstance(theme, HouseholdTheme)
    assert theme.primary_color == "#4A90E2"
    assert theme.accent_color == "#F5A623"


async def test_update_household_persists(env):
    theme = await env.update_household(
        primary_color="#112233",
        accent_color="#445566",
    )
    assert theme.primary_color == "#112233"
    assert theme.accent_color == "#445566"
    # Re-read to confirm.
    again = await env.get_household()
    assert again.primary_color == "#112233"


async def test_update_household_is_upsert(env):
    await env.update_household(primary_color="#000000", accent_color="#111111")
    await env.update_household(primary_color="#aaaaaa", accent_color="#bbbbbb")
    cur = await env.get_household()
    assert cur.primary_color == "#aaaaaa"


# ─── space ───────────────────────────────────────────────────────────────


async def test_get_space_returns_none_when_unset(env):
    assert await env.get_space("sp-1") is None


async def test_upsert_space_persists(env):
    theme = await env.upsert_space(
        space_id="sp-1",
        primary_color="#abcdef",
        accent_color="#fedcba",
    )
    assert theme.primary_color == "#abcdef"
    again = await env.get_space("sp-1")
    assert again is not None
    assert again.accent_color == "#fedcba"
