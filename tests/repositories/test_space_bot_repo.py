"""Tests for SqliteSpaceBotRepo — CRUD + token hash lookups."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.domain.space_bot import BOT_TOKEN_PREFIX, BotScope, SpaceBotSlugTakenError
from socialhome.repositories.space_bot_repo import (
    SqliteSpaceBotRepo,
    _hash_token,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo


@pytest.fixture
async def env(tmp_dir):
    """Env with a seeded space and a bot repo."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    space_repo = SqliteSpaceRepo(db)
    bot_repo = SqliteSpaceBotRepo(db)
    await space_repo.save(
        Space(
            id="sp-1",
            name="Home",
            owner_instance_id="inst-x",
            owner_username="alice",
            identity_public_key="aabb" * 16,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
            bot_enabled=True,
        )
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.space_repo = space_repo
    e.bot_repo = bot_repo
    yield e
    await db.shutdown()


async def test_create_returns_plaintext_token(env):
    """create() returns the raw token once; the row stores only its hash."""
    bot, raw = await env.bot_repo.create(
        bot_id="b1",
        space_id="sp-1",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
        created_by="uid-alice",
    )
    assert raw.startswith(BOT_TOKEN_PREFIX)
    assert bot.token_hash == _hash_token(raw)
    # The plaintext is never re-derivable from the stored row.
    refreshed = await env.bot_repo.get("b1")
    assert refreshed.token_hash == bot.token_hash
    assert refreshed.name == "Doorbell"


async def test_duplicate_slug_raises(env):
    """(space_id, scope, slug) is UNIQUE — second create with same triple fails."""
    await env.bot_repo.create(
        bot_id="b1",
        space_id="sp-1",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
        created_by="uid-alice",
    )
    with pytest.raises(SpaceBotSlugTakenError):
        await env.bot_repo.create(
            bot_id="b2",
            space_id="sp-1",
            scope=BotScope.SPACE,
            slug="doorbell",
            name="Other",
            icon="🔔",
            created_by="uid-alice",
        )


async def test_same_slug_different_scope_ok(env):
    """Space-scope and member-scope "gym-timer" slugs can coexist."""
    await env.bot_repo.create(
        bot_id="b1",
        space_id="sp-1",
        scope=BotScope.SPACE,
        slug="gym-timer",
        name="Household gym",
        icon="⏱️",
        created_by="uid-alice",
    )
    bot2, _ = await env.bot_repo.create(
        bot_id="b2",
        space_id="sp-1",
        scope=BotScope.MEMBER,
        slug="gym-timer",
        name="Pascal's gym",
        icon="⏱️",
        created_by="uid-pascal",
    )
    assert bot2.scope is BotScope.MEMBER


async def test_get_by_token_hash(env):
    """get_by_token_hash is the bot-bridge hot path — must match sha256(raw)."""
    _, raw = await env.bot_repo.create(
        bot_id="b1",
        space_id="sp-1",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
        created_by="uid-alice",
    )
    found = await env.bot_repo.get_by_token_hash(_hash_token(raw))
    assert found is not None and found.bot_id == "b1"
    # Wrong hash → None (not a 500).
    assert await env.bot_repo.get_by_token_hash("0" * 64) is None


async def test_list_for_space_and_member(env):
    """list_for_space returns all bots; list_for_member filters scope=member+owner."""
    await env.bot_repo.create(
        bot_id="b-space",
        space_id="sp-1",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
        created_by="uid-admin",
    )
    await env.bot_repo.create(
        bot_id="b-mine",
        space_id="sp-1",
        scope=BotScope.MEMBER,
        slug="gym-timer",
        name="My gym",
        icon="⏱️",
        created_by="uid-pascal",
    )
    await env.bot_repo.create(
        bot_id="b-theirs",
        space_id="sp-1",
        scope=BotScope.MEMBER,
        slug="laundry",
        name="Their laundry",
        icon="🧺",
        created_by="uid-paulus",
    )
    assert len(await env.bot_repo.list_for_space("sp-1")) == 3
    mine = await env.bot_repo.list_for_member("sp-1", "uid-pascal")
    assert [b.bot_id for b in mine] == ["b-mine"]


async def test_rotate_token_invalidates_old_hash(env):
    """rotate_token issues a new raw token; old hash no longer resolves."""
    _, raw1 = await env.bot_repo.create(
        bot_id="b1",
        space_id="sp-1",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
        created_by="uid-alice",
    )
    result = await env.bot_repo.rotate_token("b1")
    assert result is not None
    bot, raw2 = result
    assert raw1 != raw2
    assert await env.bot_repo.get_by_token_hash(_hash_token(raw1)) is None
    found = await env.bot_repo.get_by_token_hash(_hash_token(raw2))
    assert found.bot_id == "b1"


async def test_update_only_changes_provided_fields(env):
    """update() leaves unspecified fields untouched."""
    bot, _ = await env.bot_repo.create(
        bot_id="b1",
        space_id="sp-1",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
        created_by="uid-alice",
    )
    updated = await env.bot_repo.update("b1", name="Front Door")
    assert updated.name == "Front Door"
    assert updated.icon == "🔔"  # unchanged
    assert updated.slug == "doorbell"  # slugs are immutable anyway


async def test_delete_removes_row(env):
    """delete() removes the row; subsequent get returns None."""
    await env.bot_repo.create(
        bot_id="b1",
        space_id="sp-1",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
        created_by="uid-alice",
    )
    await env.bot_repo.delete("b1")
    assert await env.bot_repo.get("b1") is None
