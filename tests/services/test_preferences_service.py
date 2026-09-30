"""Tests for PreferencesService (§22 / §25)."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.preferences import (
    FeatureDisabledError,
    HouseholdPreferences,
    UserPreferences,
)
from socialhome.domain.space import SpacePermissionError
from socialhome.repositories.preferences_repo import SqlitePreferencesRepo
from socialhome.services.preferences_service import (
    PreferencesService,
    ScopeMismatchError,
)


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    yield PreferencesService(SqlitePreferencesRepo(db)), db
    await db.shutdown()


# ─── Reads ────────────────────────────────────────────────────────────────────


async def test_get_household_returns_defaults_when_no_row(env):
    svc, _ = env
    prefs = await svc.get_household()
    assert isinstance(prefs, HouseholdPreferences)
    assert prefs.household_name == "Home"
    assert prefs.tz == "UTC"
    assert prefs.feat_feed is True
    assert prefs.allow_text is True


async def test_get_user_returns_defaults_when_no_row(env):
    svc, _ = env
    prefs = await svc.get_user("u-1")
    assert isinstance(prefs, UserPreferences)
    assert prefs.user_id == "u-1"
    assert prefs.hide_highlights is False
    assert prefs.hide_momentum is False
    assert prefs.hide_bazaar is False


# ─── update_household ─────────────────────────────────────────────────────────


async def test_update_household_admin_changes_household_name(env):
    svc, _ = env
    after = await svc.update_household(
        actor_is_admin=True, household_name="The Rivendells"
    )
    assert after.household_name == "The Rivendells"


async def test_update_household_admin_changes_toggles(env):
    svc, _ = env
    after = await svc.update_household(
        actor_is_admin=True,
        toggles={"feat_pages": False, "allow_video": False},
    )
    assert after.feat_pages is False
    assert after.allow_video is False
    # Untouched defaults survive.
    assert after.feat_feed is True


async def test_update_household_non_admin_raises_permission_error(env):
    svc, _ = env
    with pytest.raises(SpacePermissionError):
        await svc.update_household(actor_is_admin=False, household_name="Hostile")


async def test_update_household_user_scope_key_raises_scope_mismatch(env):
    """hide_highlights is a user-scope preference — must be rejected."""
    svc, _ = env
    with pytest.raises(ScopeMismatchError):
        await svc.update_household(
            actor_is_admin=True,
            toggles={"hide_highlights": True},
        )


async def test_update_household_unknown_key_raises_scope_mismatch(env):
    """A key absent from PREFERENCE_SCOPE is treated as a scope mismatch."""
    svc, _ = env
    with pytest.raises(ScopeMismatchError):
        await svc.update_household(
            actor_is_admin=True,
            toggles={"feat_unknown": True},
        )


async def test_update_household_non_bool_toggle_raises_value_error(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.update_household(actor_is_admin=True, toggles={"feat_feed": "yes"})  # type: ignore[dict-item]


async def test_update_household_same_value_no_event(env, tmp_dir):
    """Idempotent — setting a key to its current value does not fire an event."""
    from socialhome.domain.events import HouseholdConfigChanged
    from socialhome.infrastructure.event_bus import EventBus

    _, db = env
    bus = EventBus()
    received: list[HouseholdConfigChanged] = []
    bus.subscribe(HouseholdConfigChanged, lambda e: received.append(e))  # type: ignore[arg-type]
    svc = PreferencesService(SqlitePreferencesRepo(db), bus=bus)

    # feat_feed defaults to True — setting it to True again is a no-op.
    await svc.update_household(actor_is_admin=True, toggles={"feat_feed": True})
    assert received == []


async def test_update_household_publishes_household_config_changed(env, tmp_dir):
    from socialhome.domain.events import HouseholdConfigChanged
    from socialhome.infrastructure.event_bus import EventBus

    _, db = env
    bus = EventBus()
    received: list[HouseholdConfigChanged] = []
    bus.subscribe(HouseholdConfigChanged, lambda e: received.append(e))  # type: ignore[arg-type]
    svc = PreferencesService(SqlitePreferencesRepo(db), bus=bus)

    await svc.update_household(actor_is_admin=True, toggles={"feat_pages": False})
    assert received
    # Only the changed key appears in the payload.
    assert received[0].changed == {"feat_pages": False}


# ─── require_enabled / require_post_type ─────────────────────────────────────


async def test_require_enabled_passes_when_section_on(env):
    svc, _ = env
    # Default: all sections on.
    await svc.require_enabled("feed")


async def test_require_enabled_raises_when_section_off(env):
    svc, _ = env
    await svc.update_household(actor_is_admin=True, toggles={"feat_tasks": False})
    with pytest.raises(FeatureDisabledError) as exc:
        await svc.require_enabled("tasks")
    assert exc.value.section == "tasks"


async def test_require_enabled_raises_on_unknown_section(env):
    """An unknown section name is rejected (highlights is absent from SECTIONS)."""
    svc, _ = env
    # "highlights" is not in SECTIONS — is_enabled returns False → FeatureDisabledError
    with pytest.raises(FeatureDisabledError):
        await svc.require_enabled("highlights")


async def test_require_post_type_allows_default(env):
    svc, _ = env
    # All allow_* default to True.
    await svc.require_post_type("image")


async def test_require_post_type_blocks_disallowed_type(env):
    svc, _ = env
    await svc.update_household(actor_is_admin=True, toggles={"allow_video": False})
    with pytest.raises(FeatureDisabledError) as exc:
        await svc.require_post_type("video")
    assert "post_type:video" in exc.value.section


# ─── set_tz_from_ha ───────────────────────────────────────────────────────────


async def test_set_tz_from_ha_writes_when_different(env, tmp_dir):
    from socialhome.domain.events import HouseholdConfigChanged
    from socialhome.infrastructure.event_bus import EventBus

    _, db = env
    bus = EventBus()
    received: list[HouseholdConfigChanged] = []
    bus.subscribe(HouseholdConfigChanged, lambda e: received.append(e))  # type: ignore[arg-type]
    svc = PreferencesService(SqlitePreferencesRepo(db), bus=bus)

    await svc.set_tz_from_ha("Europe/Berlin")

    prefs = await svc.get_household()
    assert prefs.tz == "Europe/Berlin"
    assert received and received[0].changed == {"tz": "Europe/Berlin"}


async def test_set_tz_from_ha_skips_when_unchanged(env, tmp_dir):
    """Idempotent — the HA adapter polls on every startup; no event fires
    when the stored value already matches."""
    from socialhome.domain.events import HouseholdConfigChanged
    from socialhome.infrastructure.event_bus import EventBus

    _, db = env
    bus = EventBus()
    received: list[HouseholdConfigChanged] = []
    bus.subscribe(HouseholdConfigChanged, lambda e: received.append(e))  # type: ignore[arg-type]
    svc = PreferencesService(SqlitePreferencesRepo(db), bus=bus)

    await svc.set_tz_from_ha("Europe/Berlin")
    received.clear()
    # Second call with the same value — no DB write, no event.
    await svc.set_tz_from_ha("Europe/Berlin")
    assert received == []


async def test_set_tz_from_ha_ignores_unknown_zone(env):
    """An invalid IANA name is silently dropped — no exception raised."""
    svc, _ = env
    await svc.set_tz_from_ha("Not/A/Tz")
    prefs = await svc.get_household()
    assert prefs.tz == "UTC"  # default unchanged


async def test_set_tz_from_ha_works_without_bus(env):
    """If no bus is wired (early bootstrap), the tz still persists."""
    _, db = env
    svc = PreferencesService(SqlitePreferencesRepo(db))
    await svc.set_tz_from_ha("Asia/Tokyo")
    prefs = await svc.get_household()
    assert prefs.tz == "Asia/Tokyo"


# ─── update_user ─────────────────────────────────────────────────────────────


async def test_update_user_persists_and_fires_event(env, tmp_dir):
    from socialhome.domain.events import UserPreferencesChanged
    from socialhome.infrastructure.event_bus import EventBus

    _, db = env
    bus = EventBus()
    received: list[UserPreferencesChanged] = []
    bus.subscribe(UserPreferencesChanged, lambda e: received.append(e))  # type: ignore[arg-type]
    svc = PreferencesService(SqlitePreferencesRepo(db), bus=bus)

    after = await svc.update_user("u-1", toggles={"hide_highlights": True})
    assert after.hide_highlights is True
    assert received
    assert received[0].user_id == "u-1"
    assert received[0].changed == {"hide_highlights": True}


async def test_update_user_household_scope_key_raises_scope_mismatch(env):
    """feat_feed is a household-scope preference — must be rejected for user update."""
    svc, _ = env
    with pytest.raises(ScopeMismatchError):
        await svc.update_user("u-1", toggles={"feat_feed": True})


async def test_update_user_unknown_key_raises_scope_mismatch(env):
    """A key absent from PREFERENCE_SCOPE is treated as a scope mismatch."""
    svc, _ = env
    with pytest.raises(ScopeMismatchError):
        await svc.update_user("u-1", toggles={"mystery_field": True})


async def test_update_user_same_value_no_event(env, tmp_dir):
    """Idempotent — setting a user toggle to its current value fires no event."""
    from socialhome.domain.events import UserPreferencesChanged
    from socialhome.infrastructure.event_bus import EventBus

    _, db = env
    bus = EventBus()
    received: list[UserPreferencesChanged] = []
    bus.subscribe(UserPreferencesChanged, lambda e: received.append(e))  # type: ignore[arg-type]
    svc = PreferencesService(SqlitePreferencesRepo(db), bus=bus)

    # hide_highlights defaults to False — setting it to False again is a no-op.
    await svc.update_user("u-1", toggles={"hide_highlights": False})
    assert received == []


async def test_update_user_two_users_are_independent(env):
    """Patching u-1's preferences does not affect u-2."""
    svc, _ = env
    await svc.update_user("u-1", toggles={"hide_highlights": True})
    await svc.update_user("u-2", toggles={"hide_momentum": True})

    prefs_u1 = await svc.get_user("u-1")
    prefs_u2 = await svc.get_user("u-2")

    assert prefs_u1.hide_highlights is True
    assert prefs_u1.hide_momentum is False  # u-1 didn't touch this

    assert prefs_u2.hide_highlights is False  # u-2 didn't touch this
    assert prefs_u2.hide_momentum is True


async def test_require_enabled_timetable_toggle(env):
    svc, _ = env
    await svc.require_enabled("timetable")  # on by default
    await svc.update_household(actor_is_admin=True, toggles={"feat_timetable": False})
    with pytest.raises(FeatureDisabledError) as exc:
        await svc.require_enabled("timetable")
    assert exc.value.section == "timetable"
