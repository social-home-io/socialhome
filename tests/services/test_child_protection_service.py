"""Tests for ChildProtectionService (§CP)."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import SpacePermissionError
from socialhome.infrastructure.event_bus import EventBus
from datetime import datetime, timezone

from socialhome.domain.conversation import Conversation, ConversationType
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.cp_repo import SqliteCpRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.child_protection_service import (
    ChildProtectionService,
    GuardianRequiredError,
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
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('admin', 'admin-id', 'Admin', 1)",
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('lila', 'lila-id', 'Lila', 0)",
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('mom', 'mom-id', 'Mom', 0)",
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-adult', 'X', ?, 'admin', ?)",
        (iid, "ab" * 32),
    )
    svc = ChildProtectionService(SqliteCpRepo(db), SqliteUserRepo(db), EventBus())
    yield svc, db
    await db.shutdown()


# ─── Enable / disable ────────────────────────────────────────────────────


async def test_enable_protection_admin_succeeds(env):
    svc, db = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    row = await db.fetchone(
        "SELECT child_protection_enabled, declared_age FROM users WHERE username='lila'",
    )
    assert row["child_protection_enabled"] == 1
    assert row["declared_age"] == 12


async def test_enable_protection_non_admin_403(env):
    svc, _ = env
    with pytest.raises(SpacePermissionError):
        await svc.enable_protection(
            minor_username="lila",
            declared_age=12,
            actor_user_id="mom-id",
        )


async def test_enable_protection_invalid_age_422(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.enable_protection(
            minor_username="lila",
            declared_age=18,
            actor_user_id="admin-id",
        )
    with pytest.raises(ValueError):
        await svc.enable_protection(
            minor_username="lila",
            declared_age=-1,
            actor_user_id="admin-id",
        )


async def test_enable_protection_dob_consistency_check(env):
    svc, _ = env
    # 12-year-old DOB inconsistent with declared_age=8 → reject.
    with pytest.raises(ValueError):
        await svc.enable_protection(
            minor_username="lila",
            declared_age=8,
            actor_user_id="admin-id",
            date_of_birth="2014-01-01",  # ~12 years old
        )


async def test_enable_protection_invalid_dob_format(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.enable_protection(
            minor_username="lila",
            declared_age=12,
            actor_user_id="admin-id",
            date_of_birth="not-a-date",
        )


async def test_disable_protection(env):
    svc, db = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    await svc.disable_protection(
        minor_username="lila",
        actor_user_id="admin-id",
    )
    row = await db.fetchone(
        "SELECT child_protection_enabled, declared_age FROM users WHERE username='lila'",
    )
    assert row["child_protection_enabled"] == 0
    assert row["declared_age"] is None


# ─── Guardians ───────────────────────────────────────────────────────────


async def test_add_and_list_guardian(env):
    svc, _ = env
    await svc.add_guardian(
        minor_user_id="lila-id",
        guardian_user_id="mom-id",
        actor_user_id="admin-id",
    )
    assert await svc.list_guardians("lila-id") == ["mom-id"]
    assert await svc.list_minors_for_guardian("mom-id") == ["lila-id"]


async def test_add_guardian_self_rejected(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.add_guardian(
            minor_user_id="lila-id",
            guardian_user_id="lila-id",
            actor_user_id="admin-id",
        )


async def test_add_guardian_non_admin_403(env):
    svc, _ = env
    with pytest.raises(SpacePermissionError):
        await svc.add_guardian(
            minor_user_id="lila-id",
            guardian_user_id="mom-id",
            actor_user_id="mom-id",
        )


async def test_remove_guardian(env):
    svc, _ = env
    await svc.add_guardian(
        minor_user_id="lila-id",
        guardian_user_id="mom-id",
        actor_user_id="admin-id",
    )
    await svc.remove_guardian(
        minor_user_id="lila-id",
        guardian_user_id="mom-id",
        actor_user_id="admin-id",
    )
    assert await svc.list_guardians("lila-id") == []


async def test_is_guardian_of(env):
    svc, _ = env
    assert await svc.is_guardian_of("mom-id", "lila-id") is False
    await svc.add_guardian(
        minor_user_id="lila-id",
        guardian_user_id="mom-id",
        actor_user_id="admin-id",
    )
    assert await svc.is_guardian_of("mom-id", "lila-id") is True


# ─── Per-minor blocks ────────────────────────────────────────────────────


async def test_block_for_minor_requires_guardian(env):
    svc, _ = env
    with pytest.raises(GuardianRequiredError):
        await svc.block_user_for_minor(
            minor_user_id="lila-id",
            blocked_user_id="other-id",
            guardian_user_id="mom-id",
        )


async def test_block_then_unblock(env):
    svc, _ = env
    await svc.add_guardian(
        minor_user_id="lila-id",
        guardian_user_id="mom-id",
        actor_user_id="admin-id",
    )
    await svc.block_user_for_minor(
        minor_user_id="lila-id",
        blocked_user_id="bad-id",
        guardian_user_id="mom-id",
    )
    assert await svc.is_blocked_for_minor("lila-id", "bad-id") is True
    await svc.unblock_user_for_minor(
        minor_user_id="lila-id",
        blocked_user_id="bad-id",
        guardian_user_id="mom-id",
    )
    assert await svc.is_blocked_for_minor("lila-id", "bad-id") is False


# ─── Space age gate ──────────────────────────────────────────────────────


async def test_set_age_gate_admin_succeeds(env):
    svc, _ = env
    await svc.update_space_age_gate(
        "sp-adult",
        min_age=18,
        target_audience="adult",
        actor_user_id="admin-id",
    )
    gate = await svc.get_space_age_gate("sp-adult")
    assert gate["min_age"] == 18
    assert gate["target_audience"] == "adult"


async def test_set_age_gate_invalid_min_age(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.update_space_age_gate(
            "sp-adult",
            min_age=21,
            target_audience="adult",
            actor_user_id="admin-id",
        )


async def test_set_age_gate_invalid_audience(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.update_space_age_gate(
            "sp-adult",
            min_age=13,
            target_audience="cats",
            actor_user_id="admin-id",
        )


async def test_set_age_gate_unknown_space(env):
    svc, _ = env
    with pytest.raises(KeyError):
        await svc.update_space_age_gate(
            "sp-missing",
            min_age=13,
            target_audience="teen",
            actor_user_id="admin-id",
        )


async def test_set_age_gate_non_admin_403(env):
    svc, _ = env
    with pytest.raises(SpacePermissionError):
        await svc.update_space_age_gate(
            "sp-adult",
            min_age=13,
            target_audience="teen",
            actor_user_id="mom-id",
        )


async def test_get_age_gate_unknown_space_returns_defaults(env):
    svc, _ = env
    gate = await svc.get_space_age_gate("sp-missing")
    assert gate == {"min_age": 0, "target_audience": "all"}


# ─── §CP.F1 enforcement ─────────────────────────────────────────────────


async def test_check_age_gate_no_op_for_unprotected_user(env):
    svc, _ = env
    await svc.update_space_age_gate(
        "sp-adult",
        min_age=18,
        target_audience="adult",
        actor_user_id="admin-id",
    )
    # Should not raise — admin isn't a protected minor.
    await svc.check_space_age_gate("sp-adult", "admin-id")


async def test_check_age_gate_blocks_underage_minor(env):
    svc, _ = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    await svc.update_space_age_gate(
        "sp-adult",
        min_age=18,
        target_audience="adult",
        actor_user_id="admin-id",
    )
    with pytest.raises(SpacePermissionError, match="18"):
        await svc.check_space_age_gate("sp-adult", "lila-id")


async def test_check_age_gate_allows_minor_above_min_age(env):
    svc, _ = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=15,
        actor_user_id="admin-id",
    )
    await svc.update_space_age_gate(
        "sp-adult",
        min_age=13,
        target_audience="teen",
        actor_user_id="admin-id",
    )
    # 15 ≥ 13 → no raise.
    await svc.check_space_age_gate("sp-adult", "lila-id")


async def test_check_age_gate_no_op_when_min_age_zero(env):
    svc, _ = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=8,
        actor_user_id="admin-id",
    )
    # min_age default 0 → all ages allowed.
    await svc.check_space_age_gate("sp-adult", "lila-id")


# ─── §CP.F3 DM enforcement ──────────────────────────────────────────────


async def test_dm_allowed_unprotected_user_always(env):
    svc, _ = env
    assert (
        await svc.is_dm_allowed(
            sender_user_id="admin-id",
            target_instance_id="any-instance",
        )
        is True
    )


async def test_dm_allowed_local_dm_for_minor(env):
    svc, _ = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    assert (
        await svc.is_dm_allowed(
            sender_user_id="lila-id",
            target_instance_id=None,
        )
        is True
    )


async def test_dm_blocked_for_minor_to_unknown_remote(env):
    svc, _ = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    assert (
        await svc.is_dm_allowed(
            sender_user_id="lila-id",
            target_instance_id="never-paired-iid",
        )
        is False
    )


async def test_dm_allowed_for_minor_to_directly_paired_remote(env):
    svc, db = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    await db.enqueue(
        """
        INSERT INTO remote_instances(
            id, display_name, remote_identity_pk,
            key_self_to_remote, key_remote_to_self,
            remote_webhook_url, local_webhook_id, status, source
        ) VALUES('paired-iid', 'P', 'aa', 'k', 'k', 'https://x', 'wh',
                 'confirmed', 'manual')
        """,
    )
    assert (
        await svc.is_dm_allowed(
            sender_user_id="lila-id",
            target_instance_id="paired-iid",
        )
        is True
    )


# ─── Guardian audit log ────────────────────────────────────────────────────


async def test_record_action_persists_entry(env):
    svc, db = env
    await svc.record_action(
        minor_id="lila-id",
        guardian_id="mom-id",
        action="test",
        detail={"k": "v"},
    )
    entries = await svc.list_audit_log("lila-id")
    assert len(entries) == 1
    assert entries[0]["action"] == "test"


async def test_block_user_records_audit_entry(env):
    svc, db = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    await svc.add_guardian(
        minor_user_id="lila-id",
        guardian_user_id="mom-id",
        actor_user_id="admin-id",
    )
    await svc.block_user_for_minor(
        minor_user_id="lila-id",
        blocked_user_id="bad-id",
        guardian_user_id="mom-id",
    )
    entries = await svc.list_audit_log("lila-id")
    actions = [e["action"] for e in entries]
    assert "block_user" in actions


async def test_unblock_user_records_audit_entry(env):
    svc, db = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    await svc.add_guardian(
        minor_user_id="lila-id",
        guardian_user_id="mom-id",
        actor_user_id="admin-id",
    )
    await svc.block_user_for_minor(
        minor_user_id="lila-id",
        blocked_user_id="bad-id",
        guardian_user_id="mom-id",
    )
    await svc.unblock_user_for_minor(
        minor_user_id="lila-id",
        blocked_user_id="bad-id",
        guardian_user_id="mom-id",
    )
    actions = [e["action"] for e in await svc.list_audit_log("lila-id")]
    assert "unblock_user" in actions


async def test_get_audit_log_allows_guardian(env):
    svc, db = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    await svc.add_guardian(
        minor_user_id="lila-id",
        guardian_user_id="mom-id",
        actor_user_id="admin-id",
    )
    await svc.record_action(
        minor_id="lila-id",
        guardian_id="mom-id",
        action="t",
    )
    entries = await svc.get_audit_log(
        minor_user_id="lila-id",
        requester_user_id="mom-id",
    )
    assert len(entries) >= 1


async def test_get_audit_log_allows_admin(env):
    svc, db = env
    await svc.record_action(
        minor_id="lila-id",
        guardian_id="mom-id",
        action="t",
    )
    entries = await svc.get_audit_log(
        minor_user_id="lila-id",
        requester_user_id="admin-id",
    )
    assert len(entries) >= 1


async def test_get_audit_log_denies_stranger(env):
    svc, db = env
    await svc.record_action(
        minor_id="lila-id",
        guardian_id="mom-id",
        action="t",
    )
    with pytest.raises(GuardianRequiredError):
        await svc.get_audit_log(
            minor_user_id="lila-id",
            requester_user_id="mom-id",
        )


# ─── §CP.F2: block auto-removes from shared spaces ───────────────────────


async def test_block_user_removes_minor_from_shared_spaces(env):
    svc, db = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    await svc.add_guardian(
        minor_user_id="lila-id",
        guardian_user_id="mom-id",
        actor_user_id="admin-id",
    )
    # Put both lila and bad-id in the same space.
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
        ("sp-adult", "lila-id"),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
        ("sp-adult", "bad-id"),
    )
    await svc.block_user_for_minor(
        minor_user_id="lila-id",
        blocked_user_id="bad-id",
        guardian_user_id="mom-id",
    )
    row = await db.fetchone(
        "SELECT 1 FROM space_members WHERE space_id='sp-adult' AND user_id='lila-id'",
    )
    assert row is None


# ─── list_conversations_for_minor + list_dm_contacts_for_minor ──────────


async def _seed_conv(db, *, conv_id: str, members: list[str]) -> None:
    """Insert a DM conversation + its local members for a given set of
    usernames so ``list_for_user`` resolves each one."""
    c = Conversation(
        id=conv_id,
        type=ConversationType.DM,
        created_at=datetime.now(timezone.utc),
    )
    await db.enqueue(
        "INSERT INTO conversations(id, type, name, created_at,"
        " last_message_at, bot_enabled) VALUES(?, 'dm', NULL, ?, NULL, 0)",
        (c.id, c.created_at.isoformat()),
    )
    for u in members:
        await db.enqueue(
            "INSERT INTO conversation_members(conversation_id, username,"
            " joined_at) VALUES(?, ?, datetime('now'))",
            (conv_id, u),
        )


async def test_list_conversations_for_minor_returns_rows(env):
    svc, db = env
    svc.attach_conversation_repo(SqliteConversationRepo(db))
    await svc.add_guardian(
        minor_user_id="lila-id",
        guardian_user_id="mom-id",
        actor_user_id="admin-id",
    )
    await _seed_conv(db, conv_id="c1", members=["lila", "mom"])
    rows = await svc.list_conversations_for_minor(
        minor_user_id="lila-id",
        actor_user_id="mom-id",
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "c1"


async def test_list_conversations_for_minor_stranger_denied(env):
    svc, _ = env
    svc.attach_conversation_repo(SqliteConversationRepo(env[1]))
    with pytest.raises(GuardianRequiredError):
        await svc.list_conversations_for_minor(
            minor_user_id="lila-id",
            actor_user_id="mom-id",
        )


async def test_list_dm_contacts_dedups_and_excludes_minor(env):
    svc, db = env
    svc.attach_conversation_repo(SqliteConversationRepo(db))
    await svc.add_guardian(
        minor_user_id="lila-id",
        guardian_user_id="mom-id",
        actor_user_id="admin-id",
    )
    # lila is in two conversations that share a peer (mom).
    await _seed_conv(db, conv_id="c1", members=["lila", "mom"])
    await _seed_conv(db, conv_id="c2", members=["lila", "mom"])
    contacts = await svc.list_dm_contacts_for_minor(
        minor_user_id="lila-id",
        actor_user_id="mom-id",
    )
    assert [c["username"] for c in contacts] == ["mom"]


async def test_list_dm_contacts_admin_allowed(env):
    svc, db = env
    svc.attach_conversation_repo(SqliteConversationRepo(db))
    await _seed_conv(db, conv_id="c1", members=["lila", "mom"])
    contacts = await svc.list_dm_contacts_for_minor(
        minor_user_id="lila-id",
        actor_user_id="admin-id",
    )
    assert len(contacts) == 1
