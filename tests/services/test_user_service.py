"""Tests for socialhome.services.user_service."""

from __future__ import annotations

import json

import pytest

from socialhome.crypto import generate_identity_keypair, derive_instance_id
from socialhome.db.database import AsyncDatabase
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    """Full service stack for user service tests."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)

    class Stack:
        pass

    s = Stack()
    s.db = db
    s.user_svc = user_svc

    async def provision_user(username, **kw):
        return await user_svc.provision(username=username, display_name=username, **kw)

    s.provision_user = provision_user
    yield s
    await db.shutdown()


async def test_provision_and_query(stack):
    """Provisioned user is retrievable with the correct fields."""
    u = await stack.provision_user("pascal", is_admin=True, email="p@x.com")
    assert u.is_admin and u.user_id
    got = await stack.user_svc.get("pascal")
    assert got.email == "p@x.com"


async def test_idempotent_provision(stack):
    """Provisioning the same user twice returns the same user_id."""
    u1 = await stack.provision_user("anna")
    u2 = await stack.provision_user("anna")
    assert u1.user_id == u2.user_id


async def test_deprovision_and_reactivate(stack):
    """Deprovisioned user is inactive; re-provisioning reactivates them."""
    await stack.provision_user("pascal")
    await stack.user_svc.deprovision("pascal")
    got = await stack.user_svc.get("pascal")
    assert got.state == "inactive"
    u2 = await stack.provision_user("pascal")
    assert u2.state == "active"


async def test_reserved_username_rejected(stack):
    """Provisioning a reserved username raises ValueError."""
    with pytest.raises(ValueError):
        await stack.provision_user("admin")


async def test_set_admin(stack):
    """set_admin grants admin privilege."""
    await stack.provision_user("pascal")
    await stack.user_svc.set_admin("pascal", True)
    assert (await stack.user_svc.get("pascal")).is_admin


async def test_patch_preferences(stack):
    """patch_preferences merges and removes keys correctly."""
    await stack.provision_user("pascal")
    u = await stack.user_svc.patch_preferences("pascal", {"theme": "dark"})
    prefs = json.loads(u.preferences_json)
    assert prefs["theme"] == "dark"
    u2 = await stack.user_svc.patch_preferences("pascal", {"theme": None, "tz": "UTC"})
    prefs2 = json.loads(u2.preferences_json)
    assert "theme" not in prefs2 and prefs2["tz"] == "UTC"


async def test_set_status(stack):
    """set_status updates the user's emoji and text fields."""
    await stack.provision_user("pascal")
    u = await stack.user_svc.set_status("pascal", emoji="🎉", text="party")
    assert u.status.emoji == "🎉"
    u2 = await stack.user_svc.set_status("pascal")
    assert u2.status.emoji is None


async def test_api_token_lifecycle(stack):
    """create, list, and revoke API tokens for a user."""
    await stack.provision_user("pascal")
    tid, raw = await stack.user_svc.create_api_token("pascal", label="laptop")
    assert len(raw) > 40
    tokens = await stack.user_svc.list_api_tokens("pascal")
    assert len(tokens) == 1
    await stack.user_svc.revoke_api_token(tid)


async def test_blocks(stack):
    """block / unblock toggles the block relationship between two users."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    await stack.user_svc.block("anna", b.user_id)
    assert await stack.user_svc.is_blocked(a.user_id, b.user_id)
    await stack.user_svc.unblock("anna", b.user_id)
    assert not await stack.user_svc.is_blocked(a.user_id, b.user_id)


async def test_self_block_rejected(stack):
    """A user cannot block themselves."""
    a = await stack.provision_user("anna")
    with pytest.raises(ValueError):
        await stack.user_svc.block("anna", a.user_id)


async def test_list_active_filters_deleted(stack):
    """list_active excludes deprovisioned users."""
    await stack.provision_user("anna")
    await stack.provision_user("bob")
    assert len(await stack.user_svc.list_active()) == 2
    await stack.user_svc.deprovision("bob")
    assert len(await stack.user_svc.list_active()) == 1


async def test_deprovision_unknown_user(stack):
    """Deprovisioning an unknown user raises KeyError."""
    with pytest.raises(KeyError):
        await stack.user_svc.deprovision("ghost")


async def test_provision_records_source_ha(stack):
    """``source='ha'`` persists so the HA admin panel can distinguish."""
    user = await stack.user_svc.provision(
        username="alice",
        display_name="Alice",
        source="ha",
    )
    assert user.source == "ha"
    # Re-read from the repo to confirm persistence.
    fresh = await stack.user_svc.get("alice")
    assert fresh is not None and fresh.source == "ha"


async def test_provision_defaults_to_manual(stack):
    """Legacy call sites without ``source`` get 'manual'."""
    user = await stack.user_svc.provision(
        username="manual",
        display_name="Manual",
    )
    assert user.source == "manual"


async def test_provision_rejects_invalid_source(stack):
    with pytest.raises(ValueError, match="invalid source"):
        await stack.user_svc.provision(
            username="u",
            display_name="U",
            source="bogus",
        )


async def test_deprovision_ha_user_removes_row(stack):
    from socialhome.domain.events import UserDeprovisioned

    await stack.user_svc.provision(
        username="alice",
        display_name="Alice",
        source="ha",
    )
    fired: list[UserDeprovisioned] = []

    async def _on(event: UserDeprovisioned) -> None:
        fired.append(event)

    stack.user_svc._bus.subscribe(UserDeprovisioned, _on)
    await stack.user_svc.deprovision_ha_user("alice")
    assert len(fired) == 1
    # The row should be soft-deleted (state inactive).
    user = await stack.user_svc.get("alice")
    assert user is None or not user.is_active()


async def test_deprovision_ha_user_rejects_manual_rows(stack):
    await stack.user_svc.provision(
        username="manual",
        display_name="Manual",  # source='manual'
    )
    with pytest.raises(PermissionError, match="not HA-synced"):
        await stack.user_svc.deprovision_ha_user("manual")


async def test_deprovision_ha_user_unknown_user(stack):
    with pytest.raises(KeyError):
        await stack.user_svc.deprovision_ha_user("ghost")


async def test_set_status_unknown_user(stack):
    """Setting status for an unknown user raises KeyError."""
    with pytest.raises(KeyError):
        await stack.user_svc.set_status("ghost", emoji="🎉")


async def test_clear_onboarding(stack):
    """clear_onboarding sets is_new_member to False."""
    u = await stack.provision_user("new")
    assert u.is_new_member
    await stack.user_svc.clear_onboarding("new")
    got = await stack.user_svc.get("new")
    assert not got.is_new_member


async def test_create_token_empty_label(stack):
    """Empty token label raises ValueError."""
    await stack.provision_user("a")
    with pytest.raises(ValueError):
        await stack.user_svc.create_api_token("a", label="  ")


async def test_user_provision_long_username(stack):
    """Username exceeding 32 chars raises ValueError."""
    with pytest.raises(ValueError, match="32 characters"):
        await stack.user_svc.provision(username="x" * 33, display_name="X")


async def test_user_deprovision_unknown(stack):
    """Deprovisioning unknown user raises KeyError."""
    with pytest.raises(KeyError):
        await stack.user_svc.deprovision("ghost")


async def test_user_set_status_unknown(stack):
    """Setting status for unknown user raises KeyError."""
    with pytest.raises(KeyError):
        await stack.user_svc.set_status("ghost", emoji="😊")


async def test_user_create_token_unknown(stack):
    """Creating token for unknown user raises KeyError."""
    with pytest.raises(KeyError):
        await stack.user_svc.create_api_token("ghost", label="x")


async def test_user_block_unknown_blocker(stack):
    """Blocking with unknown blocker raises KeyError."""
    with pytest.raises(KeyError):
        await stack.user_svc.block("ghost", "uid")


async def test_user_unblock(stack):
    """Unblock a user."""
    a = await stack.provision_user("ublk_a")
    b = await stack.provision_user("ublk_b")
    await stack.user_svc.block("ublk_a", b.user_id)
    await stack.user_svc.unblock("ublk_a", b.user_id)
    assert not await stack.user_svc.is_blocked(a.user_id, b.user_id)


async def test_user_get_by_user_id(stack):
    """get_by_user_id returns the user."""
    u = await stack.provision_user("byid")
    got = await stack.user_svc.get_by_user_id(u.user_id)
    assert got.username == "byid"


async def test_user_list_active(stack):
    """list_active returns only active users."""
    await stack.provision_user("act1")
    await stack.provision_user("act2")
    active = await stack.user_svc.list_active()
    assert len(active) >= 2
