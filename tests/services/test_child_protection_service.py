"""Tests for ChildProtectionService (§CP)."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.child_protection import (
    PROTECTED_ACCOUNT_RESTRICTIONS,
    AccountProtectedError,
    ProtectedCapability,
)
from socialhome.domain.space import SpacePermissionError
from socialhome.infrastructure.event_bus import EventBus
from socialhome.security import SENSITIVE_FIELDS
from datetime import datetime, timezone

from socialhome.domain.conversation import Conversation, ConversationType
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.cp_repo import SqliteCpRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.child_protection_service import (
    ChildProtectionService,
    GuardianRequiredError,
    UserNotFoundError,
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


async def test_list_protection_status_admin_reflects_state(env):
    svc, _ = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=8,
        actor_user_id="admin-id",
    )
    rows = await svc.list_protection_status(actor_user_id="admin-id")
    by_user = {r["username"]: r for r in rows}
    assert by_user["lila"]["is_minor"] is True
    assert by_user["lila"]["declared_age"] == 8
    assert by_user["mom"]["is_minor"] is False
    assert by_user["admin"]["is_minor"] is False


async def test_list_protection_status_non_admin_403(env):
    svc, _ = env
    with pytest.raises(SpacePermissionError):
        await svc.list_protection_status(actor_user_id="mom-id")


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
        actor_user_id="admin-id",
    )
    gate = await svc.get_space_age_gate("sp-adult")
    assert gate["min_age"] == 18


async def test_update_age_gate_min_age_only(env):
    svc, _ = env
    await svc.update_space_age_gate(
        "sp-adult",
        min_age=16,
        actor_user_id="admin-id",
    )
    gate = await svc.get_space_age_gate("sp-adult")
    assert gate["min_age"] == 16


async def test_set_age_gate_invalid_min_age(env):
    svc, _ = env
    with pytest.raises(ValueError):
        await svc.update_space_age_gate(
            "sp-adult",
            min_age=21,
            actor_user_id="admin-id",
        )


async def test_set_age_gate_unknown_space(env):
    svc, _ = env
    with pytest.raises(KeyError):
        await svc.update_space_age_gate(
            "sp-missing",
            min_age=13,
            actor_user_id="admin-id",
        )


async def test_set_age_gate_non_admin_403(env):
    svc, _ = env
    with pytest.raises(SpacePermissionError):
        await svc.update_space_age_gate(
            "sp-adult",
            min_age=13,
            actor_user_id="mom-id",
        )


async def test_get_age_gate_unknown_space_returns_defaults(env):
    svc, _ = env
    gate = await svc.get_space_age_gate("sp-missing")
    assert gate == {"min_age": 0}


# ─── §CP.F1 enforcement ─────────────────────────────────────────────────


async def test_check_age_gate_no_op_for_unprotected_user(env):
    svc, _ = env
    await svc.update_space_age_gate(
        "sp-adult",
        min_age=18,
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


async def test_is_age_allowed_direct_check(env):
    """is_age_allowed gates against an explicit min_age (used by the
    federated invite-link redeem path before the local stub exists)."""
    svc, _ = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=8,
        actor_user_id="admin-id",
    )
    # min_age 0 → everyone allowed (even the protected minor).
    assert await svc.is_age_allowed("lila-id", 0) is True
    # protected minor below the bar → blocked.
    assert await svc.is_age_allowed("lila-id", 18) is False
    # protected minor at/above the bar → allowed.
    assert await svc.is_age_allowed("lila-id", 8) is True
    # unprotected user → always allowed, even for an 18+ bar.
    assert await svc.is_age_allowed("mom-id", 18) is True


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
            remote_inbox_url, local_inbox_id, status, source
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


# ─── Membership audit ────────────────────────────────────────────────────


async def test_record_membership_change_noop_for_non_minor(env):
    """A user without child-protection enabled triggers no audit rows."""
    svc, db = env
    # 'mom' is not CP-covered.
    await svc.record_membership_change(
        user_id="mom-id",
        space_id="sp-adult",
        action="joined",
        actor_id="admin-id",
    )
    row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM minor_space_memberships_audit",
    )
    assert row["n"] == 0


async def test_record_membership_change_writes_row_for_minor(env):
    svc, db = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    await svc.record_membership_change(
        user_id="lila-id",
        space_id="sp-adult",
        action="joined",
        actor_id="admin-id",
    )
    rows = await db.fetchall(
        "SELECT * FROM minor_space_memberships_audit WHERE minor_user_id=?",
        ("lila-id",),
    )
    assert len(rows) == 1
    assert rows[0]["action"] == "joined"
    assert rows[0]["space_id"] == "sp-adult"
    assert rows[0]["actor_id"] == "admin-id"


async def test_record_membership_change_rejects_bad_action(env):
    svc, _ = env
    with pytest.raises(ValueError, match="invalid membership action"):
        await svc.record_membership_change(
            user_id="lila-id",
            space_id="sp",
            action="exiled",  # not in the table's CHECK set
            actor_id="admin-id",
        )


async def test_get_membership_audit_guardian_access(env):
    svc, _ = env
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
    await svc.record_membership_change(
        user_id="lila-id",
        space_id="sp-adult",
        action="removed",
        actor_id="admin-id",
    )
    entries = await svc.get_membership_audit(
        minor_user_id="lila-id",
        requester_user_id="mom-id",
    )
    assert len(entries) == 1
    assert entries[0]["action"] == "removed"


async def test_get_membership_audit_admin_access(env):
    svc, _ = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    await svc.record_membership_change(
        user_id="lila-id",
        space_id="sp-adult",
        action="joined",
        actor_id="admin-id",
    )
    # 'admin' is a household admin — must be allowed even though not a
    # guardian.
    entries = await svc.get_membership_audit(
        minor_user_id="lila-id",
        requester_user_id="admin-id",
    )
    assert len(entries) == 1


async def test_get_membership_audit_blocks_strangers(env):
    svc, _ = env
    await svc.enable_protection(
        minor_username="lila",
        declared_age=12,
        actor_user_id="admin-id",
    )
    # 'mom' is neither guardian nor admin yet.
    with pytest.raises(GuardianRequiredError):
        await svc.get_membership_audit(
            minor_user_id="lila-id",
            requester_user_id="mom-id",
        )


# ─── User-existence prechecks (audit follow-up to #280) ──────────────────


async def test_enable_protection_unknown_username_raises(env):
    """A typo'd ``minor_username`` would otherwise UPDATE 0 rows silently
    and still emit ``CpProtectionEnabled`` — surface 404 instead."""
    svc, _ = env
    with pytest.raises(UserNotFoundError) as excinfo:
        await svc.enable_protection(
            minor_username="ghost",
            declared_age=12,
            actor_user_id="admin-id",
        )
    assert "ghost" in str(excinfo.value)


async def test_disable_protection_unknown_username_raises(env):
    svc, _ = env
    with pytest.raises(UserNotFoundError):
        await svc.disable_protection(
            minor_username="ghost",
            actor_user_id="admin-id",
        )


async def test_add_guardian_unknown_minor_raises(env):
    """``cp_guardians.minor_user_id`` is FK to ``users(user_id)``; without
    the precheck a bad id surfaces as ``sqlite3.IntegrityError`` 500."""
    svc, _ = env
    with pytest.raises(UserNotFoundError) as excinfo:
        await svc.add_guardian(
            minor_user_id="nonexistent",
            guardian_user_id="mom-id",
            actor_user_id="admin-id",
        )
    assert "nonexistent" in str(excinfo.value)


async def test_add_guardian_unknown_guardian_raises(env):
    """Same precheck for ``guardian_user_id`` — symmetric FK."""
    svc, _ = env
    with pytest.raises(UserNotFoundError) as excinfo:
        await svc.add_guardian(
            minor_user_id="lila-id",
            guardian_user_id="nonexistent",
            actor_user_id="admin-id",
        )
    assert "nonexistent" in str(excinfo.value)


async def test_add_guardian_both_present_still_works(env):
    """Sanity check: the precheck doesn't break the happy path."""
    svc, db = env
    await svc.add_guardian(
        minor_user_id="lila-id",
        guardian_user_id="mom-id",
        actor_user_id="admin-id",
    )
    row = await db.fetchone(
        "SELECT 1 FROM cp_guardians WHERE minor_user_id='lila-id' "
        "AND guardian_user_id='mom-id'",
    )
    assert row is not None


# ─── Protected-account restrictions (§CP.R) ──────────────────────────────


async def test_unprotected_user_has_no_restrictions(env):
    svc, _ = env
    assert await svc.is_protected("lila-id") is False
    assert await svc.restrictions_for("lila-id") == ()
    # No-op for an unprotected user.
    await svc.require_unrestricted("lila-id", ProtectedCapability.BAZAAR)


async def test_protected_user_is_restricted(env):
    svc, _ = env
    await svc.enable_protection(
        minor_username="lila", declared_age=12, actor_user_id="admin-id"
    )
    assert await svc.is_protected("lila-id") is True
    assert await svc.restrictions_for("lila-id") == PROTECTED_ACCOUNT_RESTRICTIONS
    with pytest.raises(AccountProtectedError) as exc_info:
        await svc.require_unrestricted("lila-id", ProtectedCapability.API_TOKENS)
    assert exc_info.value.capability is ProtectedCapability.API_TOKENS


async def test_disabling_protection_lifts_restrictions(env):
    svc, _ = env
    await svc.enable_protection(
        minor_username="lila", declared_age=12, actor_user_id="admin-id"
    )
    await svc.disable_protection(minor_username="lila", actor_user_id="admin-id")
    assert await svc.is_protected("lila-id") is False
    await svc.require_unrestricted("lila-id", ProtectedCapability.BAZAAR)


async def test_unknown_user_is_not_protected(env):
    svc, _ = env
    assert await svc.is_protected("nobody-id") is False


async def test_protection_summary_for_self_lists_guardians(env):
    svc, _ = env
    await svc.enable_protection(
        minor_username="lila", declared_age=12, actor_user_id="admin-id"
    )
    await svc.add_guardian(
        minor_user_id="lila-id", guardian_user_id="mom-id", actor_user_id="admin-id"
    )
    summary = await svc.protection_summary_for_self("lila-id")
    assert summary == {
        "protected": True,
        "restrictions": [c.value for c in PROTECTED_ACCOUNT_RESTRICTIONS],
        "guardians": [{"user_id": "mom-id", "username": "mom", "display_name": "Mom"}],
    }
    # Never leaks the sensitive CP fields.
    assert not (set(summary) & SENSITIVE_FIELDS)


async def test_protection_summary_for_unprotected_user(env):
    svc, _ = env
    assert await svc.protection_summary_for_self("mom-id") == {
        "protected": False,
        "restrictions": [],
        "guardians": [],
    }
