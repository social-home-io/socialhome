"""Tests for HouseholdFeaturesService (§22)."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import SpacePermissionError
from socialhome.repositories.household_features_repo import (
    SqliteHouseholdFeaturesRepo,
)
from socialhome.services.household_features_service import (
    HouseholdFeatures,
    HouseholdFeaturesService,
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
    yield HouseholdFeaturesService(SqliteHouseholdFeaturesRepo(db)), db
    await db.shutdown()


# ─── Reads ───────────────────────────────────────────────────────────────


async def test_get_returns_defaults_when_unset(env):
    svc, _ = env
    feats = await svc.get()
    assert isinstance(feats, HouseholdFeatures)
    assert feats.household_name == "Home"
    assert feats.feat_feed is True
    assert feats.allow_text is True


# ─── Writes ──────────────────────────────────────────────────────────────


async def test_update_admin_changes_household_name(env):
    svc, _ = env
    await svc.update(actor_is_admin=True, household_name="The Rivendells")
    feats = await svc.get()
    assert feats.household_name == "The Rivendells"


async def test_update_admin_changes_toggles(env):
    svc, _ = env
    await svc.update(
        actor_is_admin=True,
        toggles={"feat_bazaar": False, "allow_video": False},
    )
    feats = await svc.get()
    assert feats.feat_bazaar is False
    assert feats.allow_video is False
    # Untouched defaults survive.
    assert feats.feat_feed is True


async def test_update_non_admin_403(env):
    svc, _ = env
    with pytest.raises(SpacePermissionError):
        await svc.update(actor_is_admin=False, household_name="Hostile")


async def test_update_empty_name_422(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.update(actor_is_admin=True, household_name="")


async def test_update_too_long_name_422(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.update(actor_is_admin=True, household_name="x" * 200)


async def test_update_non_bool_toggle_422(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.update(actor_is_admin=True, toggles={"feat_feed": "yes"})


async def test_update_unknown_toggle_silently_ignored(env):
    svc, _ = env
    await svc.update(
        actor_is_admin=True,
        toggles={"unknown_key": True, "feat_pages": False},
    )
    feats = await svc.get()
    assert feats.feat_pages is False


async def test_update_no_args_returns_unchanged(env):
    svc, _ = env
    feats = await svc.update(actor_is_admin=True)
    assert feats.household_name == "Home"


# ─── Enforcement (§18) ───────────────────────────────────────────────────


async def test_require_enabled_passes_when_section_on(env):
    svc, _ = env
    # Default: all sections on.
    await svc.require_enabled("tasks")


async def test_require_enabled_raises_when_section_off(env):
    from socialhome.domain.household_features import FeatureDisabledError

    svc, _ = env
    await svc.update(actor_is_admin=True, toggles={"feat_tasks": False})
    with pytest.raises(FeatureDisabledError) as exc:
        await svc.require_enabled("tasks")
    assert exc.value.section == "tasks"


async def test_require_enabled_raises_on_unknown_section(env):
    from socialhome.domain.household_features import FeatureDisabledError

    svc, _ = env
    # Unknown section name → refuse. New features must flip the toggle
    # in the schema before being visible server-side.
    with pytest.raises(FeatureDisabledError):
        await svc.require_enabled("teleport")


async def test_require_post_type_blocks_disallowed_type(env):
    from socialhome.domain.household_features import FeatureDisabledError

    svc, _ = env
    await svc.update(actor_is_admin=True, toggles={"allow_video": False})
    with pytest.raises(FeatureDisabledError) as exc:
        await svc.require_post_type("video")
    assert "post_type:video" in exc.value.section


async def test_require_post_type_allows_default(env):
    svc, _ = env
    # All allow_* default to True.
    await svc.require_post_type("image")


# ─── HouseholdConfigChanged event (§23.13) ───────────────────────────────


async def test_update_publishes_household_config_changed(env, tmp_dir):
    from socialhome.domain.events import HouseholdConfigChanged
    from socialhome.infrastructure.event_bus import EventBus
    from socialhome.repositories.household_features_repo import (
        SqliteHouseholdFeaturesRepo,
    )

    _, db = env
    bus = EventBus()
    received = []

    async def _capture(evt):
        received.append(evt)

    bus.subscribe(HouseholdConfigChanged, _capture)
    svc_bus = HouseholdFeaturesService(
        SqliteHouseholdFeaturesRepo(db),
        bus=bus,
    )
    await svc_bus.update(actor_is_admin=True, toggles={"feat_bazaar": False})
    assert received
    assert received[0].changed == {"feat_bazaar": False}


async def test_update_no_change_no_event(env, tmp_dir):
    from socialhome.domain.events import HouseholdConfigChanged
    from socialhome.infrastructure.event_bus import EventBus
    from socialhome.repositories.household_features_repo import (
        SqliteHouseholdFeaturesRepo,
    )

    _, db = env
    bus = EventBus()
    received = []
    bus.subscribe(
        HouseholdConfigChanged,
        lambda e: received.append(e),  # type: ignore[arg-type]
    )
    svc_bus = HouseholdFeaturesService(
        SqliteHouseholdFeaturesRepo(db),
        bus=bus,
    )
    # Setting the same value as the current default → no change → no event.
    await svc_bus.update(actor_is_admin=True, toggles={"feat_feed": True})
    assert received == []
