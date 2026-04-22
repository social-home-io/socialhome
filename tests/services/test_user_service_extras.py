"""Coverage extras for UserService — KeyError + json decode branches."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.user_service import UserService, _validate_username


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
    repo = SqliteUserRepo(db)
    yield UserService(repo, EventBus(), own_instance_public_key=kp.public_key), db
    await db.shutdown()


# ─── KeyError paths ──────────────────────────────────────────────────────


async def test_set_admin_unknown_user_raises(env):
    svc, _ = env
    with pytest.raises(KeyError):
        await svc.set_admin("nobody", True)


async def test_patch_preferences_unknown_user_raises(env):
    svc, _ = env
    with pytest.raises(KeyError):
        await svc.patch_preferences("nobody", {"x": 1})


async def test_clear_onboarding_unknown_user_raises(env):
    svc, _ = env
    with pytest.raises(KeyError):
        await svc.clear_onboarding("nobody")


async def test_create_api_token_unknown_user_raises(env):
    svc, _ = env
    with pytest.raises(KeyError):
        await svc.create_api_token("nobody", label="x")


async def test_create_api_token_empty_label_raises(env):
    svc, _ = env
    await svc.provision(username="alice", display_name="A")
    with pytest.raises(ValueError):
        await svc.create_api_token("alice", label="   ")


async def test_list_api_tokens_unknown_user_raises(env):
    svc, _ = env
    with pytest.raises(KeyError):
        await svc.list_api_tokens("nobody")


async def test_block_unknown_user_raises(env):
    svc, _ = env
    with pytest.raises(KeyError):
        await svc.block("nobody", "target-id")


async def test_unblock_unknown_user_raises(env):
    svc, _ = env
    with pytest.raises(KeyError):
        await svc.unblock("nobody", "target-id")


# ─── patch_preferences corrupted JSON path ──────────────────────────────


async def test_patch_preferences_handles_corrupted_json(env):
    svc, db = env
    await svc.provision(username="alice", display_name="A")
    # Corrupt the preferences_json column.
    await db.enqueue(
        "UPDATE users SET preferences_json='not-json' WHERE username='alice'",
    )
    user = await svc.patch_preferences("alice", {"theme": "dark"})
    # Should recover by starting fresh.
    import json as _json

    assert _json.loads(user.preferences_json) == {"theme": "dark"}


# ─── _validate_username branches ────────────────────────────────────────


def test_validate_username_empty_raises():
    with pytest.raises(ValueError):
        _validate_username("")


def test_validate_username_too_long_raises():
    with pytest.raises(ValueError):
        _validate_username("x" * 100)


def test_validate_username_reserved_raises():
    from socialhome.domain.user import RESERVED_USERNAMES

    sample = next(iter(RESERVED_USERNAMES))
    with pytest.raises(ValueError):
        _validate_username(sample)
