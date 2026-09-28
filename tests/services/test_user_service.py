"""Tests for socialhome.services.user_service."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import (
    generate_identity_keypair,
    derive_instance_id,
    derive_user_id,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import UserStatusChanged
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
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
    key_manager = KeyManager.from_data_dir(tmp_dir)
    user_svc = UserService(
        user_repo,
        bus,
        own_instance_public_key=kp.public_key,
        key_manager=key_manager,
    )

    class Stack:
        pass

    s = Stack()
    s.db = db
    s.bus = bus
    s.user_repo = user_repo
    s.user_svc = user_svc
    s.key_manager = key_manager
    s.own_instance_pk = kp.public_key

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


async def test_provision_mints_user_identity_key(stack):
    """Provisioning a user mints a KEK-wrapped Ed25519 identity key immediately."""
    await stack.provision_user("pascal")
    row = await stack.db.fetchone(
        "SELECT user_identity_public_key, user_identity_private_key "
        "FROM users WHERE username=?",
        ("pascal",),
    )
    assert row["user_identity_public_key"] is not None
    assert row["user_identity_private_key"] is not None
    # Public half is a 32-byte Ed25519 key, hex-encoded.
    assert len(bytes.fromhex(row["user_identity_public_key"])) == 32
    # Private half is KEK-wrapped and decrypts to the 32-byte seed.
    seed = stack.key_manager.decrypt(row["user_identity_private_key"])
    assert len(seed) == 32


async def test_provision_new_user_uses_uuid_anchor(stack):
    """A newly provisioned user gets a uuid4 identity_anchor and derives
    user_id from the anchor (not the username)."""
    await stack.provision_user("pascal")
    row = await stack.db.fetchone(
        "SELECT identity_anchor, user_id FROM users WHERE username=?",
        ("pascal",),
    )
    anchor = row["identity_anchor"]
    assert anchor is not None
    assert anchor != "pascal"
    # uuid4().hex is 32 lowercase hex chars.
    assert len(bytes.fromhex(anchor)) == 16
    assert row["user_id"] == derive_user_id(stack.own_instance_pk, anchor)
    assert row["user_id"] != derive_user_id(stack.own_instance_pk, "pascal")


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


async def test_revoke_own_api_token_is_scoped_to_owner(stack):
    """``revoke_own_api_token`` only revokes the caller's own token."""
    await stack.provision_user("pascal")
    await stack.provision_user("bob")
    tid, _ = await stack.user_svc.create_api_token("pascal", label="laptop")
    await stack.user_svc.revoke_own_api_token("bob", tid)
    rows = await stack.user_svc.list_api_tokens("pascal")
    assert rows[0]["revoked_at"] is None
    await stack.user_svc.revoke_own_api_token("pascal", tid)
    rows = await stack.user_svc.list_api_tokens("pascal")
    assert rows[0]["revoked_at"] is not None


async def test_revoke_own_api_token_unknown_user_raises(stack):
    with pytest.raises(KeyError):
        await stack.user_svc.revoke_own_api_token("nobody", "tid")


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


async def test_block_publishes_user_blocked_event(stack):
    """``block()`` emits :class:`UserBlocked` so realtime listeners can react."""
    from socialhome.domain.events import UserBlocked, UserUnblocked

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    seen: list = []
    stack.bus.subscribe(UserBlocked, lambda ev: seen.append(("blocked", ev)))
    stack.bus.subscribe(UserUnblocked, lambda ev: seen.append(("unblocked", ev)))

    await stack.user_svc.block("anna", b.user_id)
    await stack.user_svc.unblock("anna", b.user_id)

    kinds = [k for k, _ in seen]
    assert kinds == ["blocked", "unblocked"]
    assert seen[0][1].blocker_user_id == a.user_id
    assert seen[0][1].blocked_user_id == b.user_id
    assert seen[1][1].blocker_user_id == a.user_id
    assert seen[1][1].blocked_user_id == b.user_id


async def test_list_blocked_returns_dicts_newest_first(stack):
    """``list_blocked`` exposes blocked entries with timestamps."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    c = await stack.provision_user("carol")

    # Forced timestamps so ORDER BY is testable without sleeping.
    await stack.db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id, blocked_at) "
        "VALUES(?, ?, ?)",
        (a.user_id, b.user_id, "2026-01-01T00:00:00Z"),
    )
    await stack.db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id, blocked_at) "
        "VALUES(?, ?, ?)",
        (a.user_id, c.user_id, "2026-02-01T00:00:00Z"),
    )

    rows = await stack.user_svc.list_blocked("anna")
    assert [r["user_id"] for r in rows] == [c.user_id, b.user_id]
    assert rows[0]["blocked_at"] == "2026-02-01T00:00:00Z"


async def test_list_blocked_unknown_user_raises(stack):
    """``list_blocked`` for an unknown blocker raises KeyError → maps to 404."""
    with pytest.raises(KeyError):
        await stack.user_svc.list_blocked("ghost")


# ── Follows (§Momentum) ──────────────────────────────────────────────


async def test_follow_unfollow_publishes_events(stack):
    """``follow / unfollow`` emit :class:`UserFollowed` / :class:`UserUnfollowed`."""
    from socialhome.domain.events import UserFollowed, UserUnfollowed

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    seen: list = []
    stack.bus.subscribe(UserFollowed, lambda ev: seen.append(("followed", ev)))
    stack.bus.subscribe(UserUnfollowed, lambda ev: seen.append(("unfollowed", ev)))

    await stack.user_svc.follow("anna", b.user_id)
    await stack.user_svc.unfollow("anna", b.user_id)

    kinds = [k for k, _ in seen]
    assert kinds == ["followed", "unfollowed"]
    assert seen[0][1].follower_user_id == a.user_id
    assert seen[0][1].followed_user_id == b.user_id


async def test_follow_self_rejected_at_service(stack):
    a = await stack.provision_user("anna")
    with pytest.raises(ValueError):
        await stack.user_svc.follow("anna", a.user_id)


async def test_follow_unknown_follower_raises(stack):
    with pytest.raises(KeyError):
        await stack.user_svc.follow("ghost", "uid-bob")


async def test_unfollow_unknown_follower_raises(stack):
    with pytest.raises(KeyError):
        await stack.user_svc.unfollow("ghost", "uid-bob")


async def test_list_following_returns_dicts_newest_first(stack):
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    c = await stack.provision_user("carol")
    await stack.db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id, created_at) "
        "VALUES(?, ?, ?)",
        (a.user_id, b.user_id, "2026-01-01T00:00:00Z"),
    )
    await stack.db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id, created_at) "
        "VALUES(?, ?, ?)",
        (a.user_id, c.user_id, "2026-02-01T00:00:00Z"),
    )
    rows = await stack.user_svc.list_following("anna")
    assert [r["user_id"] for r in rows] == [c.user_id, b.user_id]
    assert rows[0]["created_at"] == "2026-02-01T00:00:00Z"


async def test_list_following_unknown_user_raises(stack):
    with pytest.raises(KeyError):
        await stack.user_svc.list_following("ghost")


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


# ─── rename_username (mutable username) ─────────────────────────────────────


async def test_rename_username_renames_row_and_cascades(stack):
    """A manual user renames: users row + cascaded child + post_comments
    author all move, and a UserProfileUpdated event fires."""
    from socialhome.domain.events import UserProfileUpdated

    u = await stack.provision_user("bob", source="manual")
    await stack.db.enqueue(
        "INSERT INTO presence(username, entity_id, state) VALUES(?,?,?)",
        ("bob", "person.bob", "home"),
    )
    await stack.db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content) VALUES(?,?,?,?)",
        ("p-1", u.user_id, "text", "hi"),
    )
    await stack.db.enqueue(
        "INSERT INTO post_comments(id, post_id, author, type, content) "
        "VALUES(?,?,?,?,?)",
        ("c-1", "p-1", "bob", "text", "nice"),
    )
    seen: list = []
    stack.bus.subscribe(UserProfileUpdated, lambda ev: seen.append(ev))

    await stack.user_svc.rename_username("bob", "bobby")

    assert await stack.user_svc.get("bob") is None
    renamed = await stack.user_svc.get("bobby")
    assert renamed is not None
    assert renamed.user_id == u.user_id  # user_id immutable across rename

    pres = await stack.db.fetchone(
        "SELECT username FROM presence WHERE entity_id=?", ("person.bob",)
    )
    assert pres["username"] == "bobby"
    com = await stack.db.fetchone(
        "SELECT author FROM post_comments WHERE id=?", ("c-1",)
    )
    assert com["author"] == "bobby"

    assert len(seen) == 1
    assert seen[0].username == "bobby"
    assert seen[0].user_id == u.user_id


async def test_rename_username_to_taken_name_raises(stack):
    """Renaming onto an existing username raises and changes nothing."""
    await stack.provision_user("bob", source="manual")
    await stack.provision_user("carol", source="manual")

    with pytest.raises(ValueError):
        await stack.user_svc.rename_username("bob", "carol")

    assert await stack.user_svc.get("bob") is not None
    assert await stack.user_svc.get("carol") is not None


async def test_rename_username_ha_user_forbidden(stack):
    """An HA-synced user's username is controlled by HA — rename is forbidden."""
    await stack.provision_user("ha-bob", source="ha")

    with pytest.raises(PermissionError):
        await stack.user_svc.rename_username("ha-bob", "ha-bobby")

    assert await stack.user_svc.get("ha-bob") is not None
    assert await stack.user_svc.get("ha-bobby") is None


async def test_rename_username_unknown_user_raises(stack):
    """Renaming a non-existent user raises KeyError."""
    with pytest.raises(KeyError):
        await stack.user_svc.rename_username("ghost", "phantom")


async def test_rename_username_same_name_is_noop(stack):
    """Renaming to the same name is a no-op: no error, no event."""
    from socialhome.domain.events import UserProfileUpdated

    await stack.provision_user("bob", source="manual")
    seen: list = []
    stack.bus.subscribe(UserProfileUpdated, lambda ev: seen.append(ev))

    await stack.user_svc.rename_username("bob", "bob")

    assert await stack.user_svc.get("bob") is not None
    assert seen == []


async def test_rename_username_reserved_name_raises(stack):
    """A reserved / invalid new name is rejected by validation."""
    await stack.provision_user("bob", source="manual")

    with pytest.raises(ValueError):
        await stack.user_svc.rename_username("bob", "admin")

    assert await stack.user_svc.get("bob") is not None


# ─── set_handle (public, per-household-unique handle) ───────────────────────


async def test_provision_sets_handle_to_username(stack):
    """A newly provisioned user gets handle == username."""
    u = await stack.provision_user("bob")
    assert u.handle == "bob"
    fresh = await stack.user_svc.get("bob")
    assert fresh is not None and fresh.handle == "bob"


async def test_set_handle_updates_row_and_publishes_event(stack):
    """set_handle changes the row and publishes UserProfileUpdated."""
    from socialhome.domain.events import UserProfileUpdated

    u = await stack.provision_user("bob")
    seen: list = []
    stack.bus.subscribe(UserProfileUpdated, lambda ev: seen.append(ev))

    await stack.user_svc.set_handle("bob", "bobby")

    fresh = await stack.user_svc.get("bob")
    assert fresh is not None and fresh.handle == "bobby"
    assert len(seen) == 1
    assert seen[0].user_id == u.user_id
    assert seen[0].handle == "bobby"


async def test_set_handle_taken_case_insensitive_raises(stack):
    """A handle already taken (any case) by another user raises ValueError."""
    await stack.provision_user("bob")
    await stack.provision_user("carol")

    with pytest.raises(ValueError, match="already taken"):
        await stack.user_svc.set_handle("bob", "CAROL")

    fresh = await stack.user_svc.get("bob")
    assert fresh is not None and fresh.handle == "bob"


async def test_set_handle_editable_for_ha_user(stack):
    """An ha-source user CAN change their handle (no source guard)."""
    await stack.provision_user("ha-bob", source="ha")

    await stack.user_svc.set_handle("ha-bob", "publicbob")

    fresh = await stack.user_svc.get("ha-bob")
    assert fresh is not None and fresh.handle == "publicbob"


async def test_set_handle_same_value_is_noop(stack):
    """Setting the handle to its current value publishes nothing."""
    from socialhome.domain.events import UserProfileUpdated

    await stack.provision_user("bob")
    seen: list = []
    stack.bus.subscribe(UserProfileUpdated, lambda ev: seen.append(ev))

    await stack.user_svc.set_handle("bob", "bob")

    assert seen == []


async def test_set_handle_unknown_user_raises(stack):
    with pytest.raises(KeyError):
        await stack.user_svc.set_handle("ghost", "phantom")


async def test_set_handle_reserved_name_raises(stack):
    """A reserved / invalid handle is rejected by validation."""
    await stack.provision_user("bob")

    with pytest.raises(ValueError):
        await stack.user_svc.set_handle("bob", "admin")

    fresh = await stack.user_svc.get("bob")
    assert fresh is not None and fresh.handle == "bob"


# ─── apply_ha_username (HA-authoritative rename-follow) ─────────────────────


async def _seed_ha_user(stack, *, username, external_id):
    """Insert an HA-source user with a stable external_id."""
    uid = derive_user_id(stack.own_instance_pk, username)
    await stack.db.enqueue(
        "INSERT INTO users(user_id, username, display_name, is_admin,"
        " created_at, source, external_id, identity_anchor)"
        " VALUES(?,?,?,1,?,'ha',?,?)",
        (uid, username, username, "2026-01-01T00:00:00+00:00", external_id, username),
    )
    return uid


async def test_apply_ha_username_follows_rename(stack):
    """HA person renamed: matched by external_id, the local row is renamed,
    a child row cascades, and UserProfileUpdated fires."""
    from socialhome.domain.events import UserProfileUpdated

    uid = await _seed_ha_user(stack, username="oldname", external_id="ha-1")
    await stack.db.enqueue(
        "INSERT INTO presence(username, entity_id, state) VALUES(?,?,?)",
        ("oldname", "person.oldname", "home"),
    )
    seen: list = []
    stack.bus.subscribe(UserProfileUpdated, lambda ev: seen.append(ev))

    await stack.user_svc.apply_ha_username("ha-1", "newname")

    assert await stack.user_svc.get("oldname") is None
    renamed = await stack.user_svc.get("newname")
    assert renamed is not None and renamed.user_id == uid
    assert renamed.source == "ha"
    assert renamed.external_id == "ha-1"
    # Only one row for this external_id.
    count = await stack.db.fetchval(
        "SELECT COUNT(*) FROM users WHERE external_id=?", ("ha-1",), default=0
    )
    assert count == 1
    pres = await stack.db.fetchone(
        "SELECT username FROM presence WHERE entity_id=?", ("person.oldname",)
    )
    assert pres["username"] == "newname"
    assert len(seen) == 1
    assert seen[0].username == "newname" and seen[0].user_id == uid


async def test_apply_ha_username_unchanged_is_noop(stack):
    """Same HA name → no rename, no event (idempotent across boots)."""
    from socialhome.domain.events import UserProfileUpdated

    await _seed_ha_user(stack, username="samename", external_id="ha-2")
    seen: list = []
    stack.bus.subscribe(UserProfileUpdated, lambda ev: seen.append(ev))

    await stack.user_svc.apply_ha_username("ha-2", "samename")

    assert await stack.user_svc.get("samename") is not None
    assert seen == []


async def test_apply_ha_username_unknown_external_id_is_noop(stack):
    """Unknown external_id → nothing happens (no crash, no event)."""
    from socialhome.domain.events import UserProfileUpdated

    seen: list = []
    stack.bus.subscribe(UserProfileUpdated, lambda ev: seen.append(ev))

    await stack.user_svc.apply_ha_username("ha-ghost", "whoever")

    assert seen == []


async def test_apply_ha_username_invalid_name_kept(stack):
    """An invalid HA name keeps the old username (logs WARNING, no crash)."""
    from socialhome.domain.events import UserProfileUpdated

    await _seed_ha_user(stack, username="keepme", external_id="ha-3")
    seen: list = []
    stack.bus.subscribe(UserProfileUpdated, lambda ev: seen.append(ev))

    # "admin" is reserved → invalid.
    await stack.user_svc.apply_ha_username("ha-3", "admin")

    assert await stack.user_svc.get("keepme") is not None
    assert await stack.user_svc.get("admin") is None
    assert seen == []


async def test_apply_ha_username_only_matches_ha_source(stack):
    """A manual user with the same external_id value is never matched."""
    # Manual user happens to carry external_id via direct insert.
    uid = derive_user_id(stack.own_instance_pk, "manualguy")
    await stack.db.enqueue(
        "INSERT INTO users(user_id, username, display_name, is_admin,"
        " created_at, source, external_id, identity_anchor)"
        " VALUES(?,?,?,0,?,'manual',?,?)",
        (uid, "manualguy", "manualguy", "2026-01-01T00:00:00+00:00", "ha-4", "anchor"),
    )

    await stack.user_svc.apply_ha_username("ha-4", "renamed")

    # Untouched — source!='ha'.
    assert await stack.user_svc.get("manualguy") is not None
    assert await stack.user_svc.get("renamed") is None


# ─── Status: validation, clear-after, expiry ─────────────────────────────

_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


async def test_set_status_publishes_status_changed(stack):
    await stack.provision_user("pascal")
    seen: list[UserStatusChanged] = []

    async def _on(e):
        seen.append(e)

    stack.bus.subscribe(UserStatusChanged, _on)
    u = await stack.user_svc.set_status("pascal", emoji=" 🎉 ", text=" party ")
    assert (u.status.emoji, u.status.text) == ("🎉", "party")
    assert seen[-1].status == u.status
    await stack.user_svc.set_status("pascal", emoji=None, text="")
    assert seen[-1].status is None
    # Clearing an already-clear status is a no-op — no duplicate frame.
    await stack.user_svc.set_status("pascal")
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("30m", "2026-09-28T12:30:00+00:00"),
        ("1h", "2026-09-28T13:00:00+00:00"),
        ("4h", "2026-09-28T16:00:00+00:00"),
        (None, None),
    ],
)
async def test_set_status_clear_after_choices(stack, choice, expected):
    await stack.provision_user("pascal")
    u = await stack.user_svc.set_status(
        "pascal", emoji="🎉", clear_after=choice, now=_NOW
    )
    assert u.status.expires_at == expected
    stored = await stack.user_repo.get("pascal")
    assert stored.status.expires_at == expected


async def test_set_status_today_ends_at_local_midnight(stack):
    await stack.provision_user("pascal")
    await stack.user_svc.set_tz("pascal", "Europe/Zurich")
    u = await stack.user_svc.set_status(
        "pascal", text="out", clear_after="today", now=_NOW
    )
    # 12:00Z is 14:00 in Zurich (CEST, +02:00); midnight there is 22:00Z.
    assert u.status.expires_at == "2026-09-28T22:00:00+00:00"


async def test_set_status_explicit_iso_clear_after(stack):
    await stack.provision_user("pascal")
    u = await stack.user_svc.set_status(
        "pascal", text="out", clear_after="2026-09-29T08:00:00+02:00", now=_NOW
    )
    assert u.status.expires_at == "2026-09-29T06:00:00+00:00"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "x" * 81},
        {"text": "two\nlines"},
        {"text": 42},
        {"emoji": "busy"},
        {"emoji": "🎉 🎉"},
        {"emoji": "🎉" * 17},
        {"emoji": "🎉", "clear_after": "2h"},
        {"emoji": "🎉", "clear_after": 30},
        {"emoji": "🎉", "clear_after": "2026-09-28T11:00:00+00:00"},  # past
        {"emoji": "🎉", "clear_after": "2026-10-06T12:00:00+00:00"},  # > 7 d
        {"emoji": "🎉", "clear_after": "2026-09-28T13:00:00"},  # naive
    ],
)
async def test_set_status_rejects_invalid_input(stack, kwargs):
    await stack.provision_user("pascal")
    with pytest.raises(ValueError):
        await stack.user_svc.set_status("pascal", now=_NOW, **kwargs)
    assert (await stack.user_repo.get("pascal")).status.is_set is False


async def test_set_status_accepts_multi_codepoint_emoji(stack):
    await stack.provision_user("pascal")
    for emoji in ("👍🏽", "👨‍👩‍👧‍👦", "1️⃣", "🏳️‍🌈"):
        u = await stack.user_svc.set_status("pascal", emoji=emoji)
        assert u.status.emoji == emoji


async def test_clear_expired_statuses_clears_and_publishes(stack):
    await stack.provision_user("pascal")
    await stack.provision_user("anna")
    await stack.user_svc.set_status("pascal", emoji="🎉", clear_after="30m", now=_NOW)
    await stack.user_svc.set_status("anna", emoji="🌴", clear_after="4h", now=_NOW)
    seen: list[UserStatusChanged] = []

    async def _on(e):
        seen.append(e)

    stack.bus.subscribe(UserStatusChanged, _on)
    later = _NOW + timedelta(hours=1)
    assert await stack.user_svc.clear_expired_statuses(now=later) == 1
    pascal = await stack.user_repo.get("pascal")
    assert pascal.status.is_set is False
    assert (await stack.user_repo.get("anna")).status.emoji == "🌴"
    assert [(e.user_id, e.status) for e in seen] == [(pascal.user_id, None)]
    # A second sweep finds nothing left to clear.
    assert await stack.user_svc.clear_expired_statuses(now=later) == 0
