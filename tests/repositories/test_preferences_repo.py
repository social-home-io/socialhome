"""Tests for SqlitePreferencesRepo (household + per-user rows)."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.preferences import HouseholdPreferences, UserPreferences
from socialhome.repositories.preferences_repo import (
    HOUSEHOLD_ROW_ID,
    SqlitePreferencesRepo,
)


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "prefs.db", batch_timeout_ms=10)
    await db.startup()
    yield SqlitePreferencesRepo(db)
    await db.shutdown()


# ─── get_household ────────────────────────────────────────────────────────────


async def test_get_household_returns_defaults_when_no_row(repo):
    prefs = await repo.get_household()
    assert isinstance(prefs, HouseholdPreferences)
    # Spot-check a sample of the dataclass defaults.
    assert prefs.household_name == "Home"
    assert prefs.tz == "UTC"
    assert prefs.feat_feed is True
    assert prefs.feat_presence is True
    assert prefs.feat_gallery is True
    assert prefs.feat_timetable is True
    assert prefs.allow_text is True


async def test_feat_timetable_round_trips(repo):
    await repo.ensure_row(HOUSEHOLD_ROW_ID)
    assert (await repo.get_household()).feat_timetable is True
    await repo.set_household_value("feat_timetable", 0)
    assert (await repo.get_household()).feat_timetable is False


async def test_get_household_returns_row_values_after_ensure_and_set(repo):
    await repo.ensure_row(HOUSEHOLD_ROW_ID)
    await repo.set_household_value("feat_feed", 0)
    await repo.set_household_value("household_name", "My Home")

    prefs = await repo.get_household()
    assert prefs.feat_feed is False
    assert prefs.household_name == "My Home"
    # Untouched columns keep their defaults.
    assert prefs.feat_pages is True


# ─── get_user ─────────────────────────────────────────────────────────────────


async def test_get_user_returns_defaults_when_no_row(repo):
    prefs = await repo.get_user("alice")
    assert isinstance(prefs, UserPreferences)
    assert prefs.user_id == "alice"
    assert prefs.hide_highlights is False
    assert prefs.hide_momentum is False
    assert prefs.hide_bazaar is False


async def test_get_user_returns_row_values_after_ensure_and_set(repo):
    await repo.ensure_row("alice")
    await repo.set_user_value("alice", "hide_highlights", 1)
    await repo.set_user_value("alice", "hide_bazaar", 1)

    prefs = await repo.get_user("alice")
    assert prefs.user_id == "alice"
    assert prefs.hide_highlights is True
    assert prefs.hide_bazaar is True
    assert prefs.hide_momentum is False


# ─── ensure_row ───────────────────────────────────────────────────────────────


async def test_ensure_row_household_is_idempotent(repo):
    await repo.ensure_row(HOUSEHOLD_ROW_ID)
    await repo.ensure_row(HOUSEHOLD_ROW_ID)  # second call must not raise or duplicate
    prefs = await repo.get_household()
    assert prefs.household_name == "Home"


async def test_ensure_row_user_does_not_affect_household(repo):
    await repo.ensure_row(HOUSEHOLD_ROW_ID)
    await repo.set_household_value("feat_tasks", 0)

    await repo.ensure_row("bob")
    # Household row must be unchanged.
    h = await repo.get_household()
    assert h.feat_tasks is False

    # User row has fresh defaults.
    u = await repo.get_user("bob")
    assert u.user_id == "bob"
    assert u.hide_highlights is False


# ─── set_household_value ──────────────────────────────────────────────────────


async def test_set_household_value_updates_only_household_row(repo):
    await repo.ensure_row(HOUSEHOLD_ROW_ID)
    await repo.ensure_row("carol")

    await repo.set_household_value("feat_calendar", 0)

    h = await repo.get_household()
    assert h.feat_calendar is False

    # Per-user row must be unaffected (hide_highlights is the user-scope col).
    u = await repo.get_user("carol")
    assert u.hide_highlights is False


async def test_set_household_value_rejects_unknown_key(repo):
    await repo.ensure_row(HOUSEHOLD_ROW_ID)
    with pytest.raises(KeyError, match="unknown household preference key"):
        await repo.set_household_value("hide_highlights", 1)  # user-scope key


async def test_set_household_value_rejects_garbage_key(repo):
    await repo.ensure_row(HOUSEHOLD_ROW_ID)
    with pytest.raises(KeyError, match="unknown household preference key"):
        await repo.set_household_value("DROP TABLE preferences; --", 1)


# ─── set_user_value ───────────────────────────────────────────────────────────


async def test_set_user_value_updates_only_user_row(repo):
    await repo.ensure_row(HOUSEHOLD_ROW_ID)
    await repo.ensure_row("dave")

    await repo.set_user_value("dave", "hide_momentum", 1)

    u = await repo.get_user("dave")
    assert u.hide_momentum is True

    # Household row must be completely unaffected.
    h = await repo.get_household()
    assert h.feat_feed is True


async def test_set_user_value_no_cross_talk_between_users(repo):
    await repo.ensure_row("eve")
    await repo.ensure_row("frank")

    await repo.set_user_value("eve", "hide_bazaar", 1)

    e = await repo.get_user("eve")
    f = await repo.get_user("frank")
    assert e.hide_bazaar is True
    assert f.hide_bazaar is False  # frank untouched


async def test_set_user_value_rejects_household_scope_key(repo):
    await repo.ensure_row("grace")
    with pytest.raises(KeyError, match="unknown user preference key"):
        await repo.set_user_value("grace", "feat_feed", 0)  # household-scope key


async def test_set_user_value_rejects_garbage_key(repo):
    await repo.ensure_row("henry")
    with pytest.raises(KeyError, match="unknown user preference key"):
        await repo.set_user_value("henry", "DROP TABLE preferences; --", 1)
