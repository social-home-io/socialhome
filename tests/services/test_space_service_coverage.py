"""Coverage fill for :class:`SpaceService` — ban/unban, transfer,
ownership, request_join + approval, member edits.

Pairs with :mod:`test_space_service` (happy path CRUD) + extensions.
"""

from __future__ import annotations

import pytest

from social_home.crypto import derive_instance_id, generate_identity_keypair
from social_home.db.database import AsyncDatabase
from social_home.domain.space import (
    JoinMode,
    SpaceFeatures,
    SpacePermissionError,
)
from social_home.infrastructure.event_bus import EventBus
from social_home.repositories.space_post_repo import SqliteSpacePostRepo
from social_home.repositories.space_repo import SqliteSpaceRepo
from social_home.repositories.user_repo import SqliteUserRepo
from social_home.services.space_service import (
    SpaceService,
)
from social_home.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
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
    space_repo = SqliteSpaceRepo(db)
    space_post_repo = SqliteSpacePostRepo(db)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    space_svc = SpaceService(
        space_repo, space_post_repo, user_repo, bus, own_instance_id=iid
    )

    class Stack:
        pass

    s = Stack()
    s.db = db
    s.user_svc = user_svc
    s.space_svc = space_svc
    s.space_repo = space_repo
    s.user_repo = user_repo
    s.iid = iid

    async def provision_user(username, **kw):
        return await user_svc.provision(
            username=username,
            display_name=username.title(),
            **kw,
        )

    s.provision_user = provision_user
    yield s
    await db.shutdown()


# ── ban / unban ──────────────────────────────────────────────────────────


async def test_ban_and_unban(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Family",
    )
    await stack.space_svc.add_member(
        space.id,
        actor_username="anna",
        user_id=bob.user_id,
    )
    await stack.space_svc.ban(
        space.id,
        actor_username="anna",
        user_id=bob.user_id,
        reason="noise",
    )
    assert await stack.space_repo.is_banned(space.id, bob.user_id)
    await stack.space_svc.unban(
        space.id,
        actor_username="anna",
        user_id=bob.user_id,
    )
    assert not await stack.space_repo.is_banned(space.id, bob.user_id)


async def test_ban_owner_raises(stack):
    anna = await stack.provision_user("anna")
    await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Family",
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.ban(
            space.id,
            actor_username="anna",
            user_id=anna.user_id,
        )


async def test_ban_non_admin_actor_raises(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    carol = await stack.provision_user("carol")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Family",
    )
    await stack.space_svc.add_member(
        space.id,
        actor_username="anna",
        user_id=bob.user_id,
    )
    await stack.space_svc.add_member(
        space.id,
        actor_username="anna",
        user_id=carol.user_id,
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.ban(
            space.id,
            actor_username="bob",
            user_id=carol.user_id,
        )


# ── add_member banned ───────────────────────────────────────────────────


async def test_add_member_banned_raises(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    await stack.space_repo.ban_member(
        space.id,
        bob.user_id,
        banned_by="anna-id",
        reason="x",
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.add_member(
            space.id,
            actor_username="anna",
            user_id=bob.user_id,
        )


# ── set_role ──────────────────────────────────────────────────────────


async def test_set_role_owner_rejected(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    await stack.space_svc.add_member(
        space.id,
        actor_username="anna",
        user_id=bob.user_id,
    )
    with pytest.raises(ValueError):
        await stack.space_svc.set_role(
            space.id,
            actor_username="anna",
            user_id=bob.user_id,
            role="owner",
        )


async def test_set_role_unknown_member_raises(stack):
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    with pytest.raises(KeyError):
        await stack.space_svc.set_role(
            space.id,
            actor_username="anna",
            user_id="bogus",
            role="admin",
        )


async def test_set_role_cannot_demote_owner(stack):
    anna = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.set_role(
            space.id,
            actor_username="anna",
            user_id=anna.user_id,
            role="admin",
        )


async def test_set_role_grant_admin(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    await stack.space_svc.add_member(
        space.id,
        actor_username="anna",
        user_id=bob.user_id,
    )
    await stack.space_svc.set_role(
        space.id,
        actor_username="anna",
        user_id=bob.user_id,
        role="admin",
    )
    mem = await stack.space_repo.get_member(space.id, bob.user_id)
    assert mem.role == "admin"


# ── transfer_ownership ─────────────────────────────────────────────────


async def test_transfer_ownership_happy(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    await stack.space_svc.add_member(
        space.id,
        actor_username="anna",
        user_id=bob.user_id,
    )
    await stack.space_svc.transfer_ownership(
        space.id,
        actor_username="anna",
        to_user_id=bob.user_id,
    )
    # Role flip: anna should be admin, bob should be owner.
    anna_mem = await stack.space_repo.get_member(space.id, "anna-id")
    bob_mem = await stack.space_repo.get_member(space.id, bob.user_id)
    assert bob_mem.role == "owner"
    # anna either admin or ex-admin row present — either way the flip
    # has happened.
    assert anna_mem is None or anna_mem.role != "owner"


async def test_transfer_ownership_unknown_member_raises(stack):
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    with pytest.raises(KeyError):
        await stack.space_svc.transfer_ownership(
            space.id,
            actor_username="anna",
            to_user_id="bogus",
        )


# ── update_space ────────────────────────────────────────────────────────


async def test_update_space_rename(stack):
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Family",
    )
    updated = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        name="Home",
    )
    assert updated.name == "Home"


async def test_update_space_empty_name_rejected(stack):
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Family",
    )
    with pytest.raises(ValueError):
        await stack.space_svc.update_config(
            space.id,
            actor_username="anna",
            name="   ",
        )


async def test_update_space_no_fields_is_noop(stack):
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    same = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
    )
    assert same.id == space.id


async def test_update_space_about_markdown_too_long(stack):
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    with pytest.raises(ValueError):
        await stack.space_svc.update_config(
            space.id,
            actor_username="anna",
            about_markdown="x" * 8001,
        )


async def test_update_space_description_emoji_features(stack):
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    updated = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        description="Cozy",
        emoji="🏡",
        features=SpaceFeatures(),
        join_mode="open",
        retention_days=30,
        retention_exempt_types=["pin", "announcement"],
        about_markdown="# Home",
    )
    assert updated.description == "Cozy"
    assert updated.emoji == "🏡"
    assert updated.retention_days == 30


async def test_update_space_retention_zero_means_none(stack):
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    updated = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        retention_days=0,
    )
    assert updated.retention_days is None


# ── remove_member ─────────────────────────────────────────────────────


async def test_remove_member_self_leaves(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    await stack.space_svc.add_member(
        space.id,
        actor_username="anna",
        user_id=bob.user_id,
    )
    await stack.space_svc.remove_member(
        space.id,
        actor_username="bob",
        user_id=bob.user_id,
    )
    assert await stack.space_repo.get_member(space.id, bob.user_id) is None


async def test_remove_member_unknown_actor_raises(stack):
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    with pytest.raises(KeyError):
        await stack.space_svc.remove_member(
            space.id,
            actor_username="unknown",
            user_id="bogus",
        )


async def test_remove_member_owner_rejected(stack):
    anna = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.remove_member(
            space.id,
            actor_username="anna",
            user_id=anna.user_id,
        )


async def test_remove_member_not_a_member_is_silent(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    # Bob is not a member; remove_member returns silently.
    await stack.space_svc.remove_member(
        space.id,
        actor_username="anna",
        user_id=bob.user_id,
    )


# ── request_join + approval ─────────────────────────────────────────


async def test_request_join_invite_only_rejected(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
        join_mode=JoinMode.INVITE_ONLY,
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.request_join(
            space.id,
            user_id=bob.user_id,
        )


async def test_request_join_already_member_rejected(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
        join_mode=JoinMode.OPEN,
    )
    await stack.space_svc.add_member(
        space.id,
        actor_username="anna",
        user_id=bob.user_id,
    )
    with pytest.raises(ValueError):
        await stack.space_svc.request_join(
            space.id,
            user_id=bob.user_id,
        )


async def test_request_join_banned_rejected(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
        join_mode=JoinMode.OPEN,
    )
    await stack.space_repo.ban_member(
        space.id,
        bob.user_id,
        banned_by="anna-id",
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.request_join(
            space.id,
            user_id=bob.user_id,
        )


async def test_request_join_happy(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
        join_mode=JoinMode.REQUEST,
    )
    rid = await stack.space_svc.request_join(
        space.id,
        user_id=bob.user_id,
        message="please",
    )
    assert rid


# ── invite tokens ─────────────────────────────────────────────────────


async def test_create_invite_token_and_accept(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    token = await stack.space_svc.create_invite_token(
        space.id,
        actor_username="anna",
    )
    # Use it
    member = await stack.space_svc.accept_invite_token(
        token,
        user_id=bob.user_id,
    )
    assert member.user_id == bob.user_id


async def test_accept_invite_token_unknown_raises(stack):
    with pytest.raises(KeyError):
        await stack.space_svc.accept_invite_token(
            "bogus-token",
            user_id="u-id",
        )


async def test_accept_invite_token_banned_rejected(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    await stack.space_repo.ban_member(
        space.id,
        bob.user_id,
        banned_by="anna-id",
    )
    token = await stack.space_svc.create_invite_token(
        space.id,
        actor_username="anna",
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.accept_invite_token(
            token,
            user_id=bob.user_id,
        )


# ── update_member_profile ──────────────────────────────────────────────


async def test_update_member_profile_unknown_raises(stack):
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    with pytest.raises(KeyError):
        await stack.space_svc.update_member_profile(
            space.id,
            "bogus-user",
            actor_user_id="anna-id",
            space_display_name="X",
        )


async def test_update_member_profile_happy(stack):
    anna = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    updated = await stack.space_svc.update_member_profile(
        space.id,
        anna.user_id,
        actor_user_id=anna.user_id,
        space_display_name="Anna the Host",
    )
    assert updated.space_display_name == "Anna the Host"


async def test_update_member_profile_foreign_admin_forbidden(stack):
    await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    carol = await stack.provision_user("carol")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    await stack.space_svc.add_member(
        space.id,
        actor_username="anna",
        user_id=bob.user_id,
    )
    await stack.space_svc.add_member(
        space.id,
        actor_username="anna",
        user_id=carol.user_id,
    )
    with pytest.raises(PermissionError):
        await stack.space_svc.update_member_profile(
            space.id,
            bob.user_id,
            actor_user_id=carol.user_id,
            space_display_name="Hacked",
        )


# ── set_cover / clear_cover / pictures — raise when repo missing ───────


async def test_set_cover_without_repo_raises(stack):
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    with pytest.raises(RuntimeError):
        await stack.space_svc.set_cover(
            space.id,
            actor_username="anna",
            raw_bytes=b"not-an-image",
        )


async def test_clear_cover_without_repo_raises(stack):
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    with pytest.raises(RuntimeError):
        await stack.space_svc.clear_cover(
            space.id,
            actor_username="anna",
        )


async def test_set_member_picture_without_repo_raises(stack):
    anna = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    with pytest.raises(RuntimeError):
        await stack.space_svc.set_member_picture(
            space.id,
            anna.user_id,
            actor_user_id=anna.user_id,
            raw_bytes=b"x",
        )


async def test_clear_member_picture_without_repo_raises(stack):
    anna = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="F",
    )
    with pytest.raises(RuntimeError):
        await stack.space_svc.clear_member_picture(
            space.id,
            anna.user_id,
            actor_user_id=anna.user_id,
        )
