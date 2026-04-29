"""Tests for socialhome.services.space_service."""

from __future__ import annotations

import pytest

from socialhome.crypto import generate_identity_keypair, derive_instance_id
from socialhome.db.database import AsyncDatabase
from socialhome.domain.post import PostType
from socialhome.domain.space import (
    JoinMode,
    SpaceFeatureAccess,
    SpaceFeatures,
    SpacePermissionError,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    """Full service stack for space service tests."""
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
    s.space_post_repo = space_post_repo
    s.iid = iid

    async def provision_user(username, **kw):
        return await user_svc.provision(username=username, display_name=username, **kw)

    s.provision_user = provision_user
    yield s
    await db.shutdown()


async def test_create_and_dissolve(stack):
    """Creating a space adds the owner as a member; dissolving removes the space."""
    _a = await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(owner_username="anna", name="Family")
    assert space.name == "Family"
    members = await stack.space_repo.list_members(space.id)
    assert any(m.role == "owner" for m in members)
    await stack.space_svc.dissolve_space(space.id, actor_username="anna")
    with pytest.raises(KeyError):
        await stack.space_svc.list_feed(space.id)


async def test_member_management(stack):
    """add_member and remove_member adjust the member count correctly."""
    await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    members = await stack.space_repo.list_members(space.id)
    assert len(members) == 2
    await stack.space_svc.remove_member(
        space.id, actor_username="anna", user_id=b.user_id
    )
    members = await stack.space_repo.list_members(space.id)
    assert len(members) == 1


async def test_ban_and_unban(stack):
    """ban removes the member; unban clears the ban record."""
    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.ban(space.id, actor_username="anna", user_id=b.user_id)
    assert await stack.space_repo.is_banned(space.id, b.user_id)
    assert await stack.space_repo.get_member(space.id, b.user_id) is None
    await stack.space_svc.unban(space.id, actor_username="anna", user_id=b.user_id)
    assert not await stack.space_repo.is_banned(space.id, b.user_id)


async def test_invite_flow(stack):
    """Invite token can be created and accepted; expired token is rejected."""
    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    tok = await stack.space_svc.create_invite_token(
        space.id, actor_username="anna", uses=1
    )
    m = await stack.space_svc.accept_invite_token(tok, user_id=b.user_id)
    assert m.role == "member"
    with pytest.raises(KeyError):
        await stack.space_svc.accept_invite_token(tok, user_id="uid-x")


async def test_set_role(stack):
    """set_role updates a member's role in the space."""
    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.set_role(
        space.id, actor_username="anna", user_id=b.user_id, role="admin"
    )
    m = await stack.space_repo.get_member(space.id, b.user_id)
    assert m.role == "admin"


async def test_non_owner_cannot_dissolve(stack):
    """Non-owner dissolving a space raises SpacePermissionError."""
    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.dissolve_space(space.id, actor_username="bob")


async def test_space_location_post_round_trip(stack):
    """Space-scoped location post: lat/lon truncated to 4dp at the
    service boundary, label preserved, post persisted."""
    from socialhome.domain.post import LocationData

    a = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        space.id,
        author_user_id=a.user_id,
        type=PostType.LOCATION,
        location=LocationData(lat=52.5200123456, lon=4.0600987, label="Marina"),
    )
    assert p is not None
    assert p.location is not None
    assert p.location.lat == 52.5200
    assert p.location.lon == 4.0601
    assert p.location.label == "Marina"


async def test_space_location_post_requires_coords(stack):
    """LOCATION without a LocationData payload is a 422 / ValueError."""
    a = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    with pytest.raises(ValueError, match="lat/lon"):
        await stack.space_svc.create_post(
            space.id,
            author_user_id=a.user_id,
            type=PostType.LOCATION,
        )


async def test_space_location_post_label_capped(stack):
    """Label longer than LOCATION_LABEL_MAX (80) raises ValueError."""
    from socialhome.domain.post import LocationData

    a = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    with pytest.raises(ValueError, match="label exceeds"):
        await stack.space_svc.create_post(
            space.id,
            author_user_id=a.user_id,
            type=PostType.LOCATION,
            location=LocationData(lat=10.0, lon=20.0, label="x" * 81),
        )


async def test_space_post_with_moderation(stack):
    """Moderated space queues regular member posts; admin posts go through directly."""
    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    result = await stack.space_svc.create_post(
        space.id,
        author_user_id=b.user_id,
        type=PostType.TEXT,
        content="pending",
    )
    assert result is None
    direct = await stack.space_svc.create_post(
        space.id,
        author_user_id=a.user_id,
        type=PostType.TEXT,
        content="admin ok",
    )
    assert direct is not None


async def test_approve_moderation_item_persists_post(stack):
    """Approving a queued post persists it and marks the queue item APPROVED."""
    from socialhome.domain.space import ModerationStatus

    a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    # Bob's post goes to the queue.
    assert (
        await stack.space_svc.create_post(
            space.id,
            author_user_id=b.user_id,
            type=PostType.TEXT,
            content="hello",
        )
        is None
    )
    pending = await stack.space_svc.list_pending_moderation(
        space.id,
        actor_username="anna",
    )
    assert len(pending) == 1
    approved_post = await stack.space_svc.approve_moderation_item(
        space.id,
        pending[0].id,
        actor_username="anna",
    )
    assert approved_post.content == "hello"
    assert approved_post.author == b.user_id
    # Item is now APPROVED; no longer listed as pending.
    assert (
        await stack.space_svc.list_pending_moderation(
            space.id,
            actor_username="anna",
        )
        == []
    )
    # The queued row should be loadable with its new status.
    item = await stack.space_svc._spaces.get_moderation_item(pending[0].id)
    assert item is not None and item.status is ModerationStatus.APPROVED
    assert item.reviewed_by == a.user_id


async def test_reject_moderation_item_records_reason(stack):
    from socialhome.domain.space import ModerationStatus

    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    await stack.space_svc.create_post(
        space.id,
        author_user_id=b.user_id,
        type=PostType.TEXT,
        content="spam",
    )
    pending = await stack.space_svc.list_pending_moderation(
        space.id,
        actor_username="anna",
    )
    await stack.space_svc.reject_moderation_item(
        space.id,
        pending[0].id,
        actor_username="anna",
        reason="off-topic",
    )
    item = await stack.space_svc._spaces.get_moderation_item(pending[0].id)
    assert item is not None
    assert item.status is ModerationStatus.REJECTED
    assert item.rejection_reason == "off-topic"


async def test_moderation_requires_admin(stack):
    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    await stack.space_svc.create_post(
        space.id,
        author_user_id=b.user_id,
        type=PostType.TEXT,
        content="x",
    )
    pending = await stack.space_svc.list_pending_moderation(
        space.id,
        actor_username="anna",
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.approve_moderation_item(
            space.id,
            pending[0].id,
            actor_username="bob",
        )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.list_pending_moderation(
            space.id,
            actor_username="bob",
        )


async def test_double_decide_raises_already_decided(stack):
    from socialhome.domain.space import ModerationAlreadyDecidedError

    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED),
    )
    await stack.space_svc.create_post(
        space.id,
        author_user_id=b.user_id,
        type=PostType.TEXT,
        content="x",
    )
    pending = await stack.space_svc.list_pending_moderation(
        space.id,
        actor_username="anna",
    )
    await stack.space_svc.approve_moderation_item(
        space.id,
        pending[0].id,
        actor_username="anna",
    )
    with pytest.raises(ModerationAlreadyDecidedError):
        await stack.space_svc.approve_moderation_item(
            space.id,
            pending[0].id,
            actor_username="anna",
        )
    with pytest.raises(ModerationAlreadyDecidedError):
        await stack.space_svc.reject_moderation_item(
            space.id,
            pending[0].id,
            actor_username="anna",
        )


async def test_space_post_admin_only(stack):
    """ADMIN_ONLY space rejects regular member posts with SpacePermissionError."""
    _a = await stack.provision_user("anna")
    b = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(space.id, actor_username="anna", user_id=b.user_id)
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(posts_access=SpaceFeatureAccess.ADMIN_ONLY),
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.create_post(
            space.id,
            author_user_id=b.user_id,
            type=PostType.TEXT,
            content="denied",
        )


async def test_transfer_ownership(stack):
    """Transferring ownership makes the new owner's role 'owner' and demotes the old one."""
    anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="Family")
    await stack.space_svc.add_member(
        space.id, actor_username="anna", user_id=bob.user_id
    )
    await stack.space_svc.transfer_ownership(
        space.id,
        actor_username="anna",
        to_user_id=bob.user_id,
    )
    anna_member = await stack.space_repo.get_member(space.id, anna.user_id)
    bob_member = await stack.space_repo.get_member(space.id, bob.user_id)
    assert bob_member.role == "owner"
    assert anna_member.role == "admin"


async def test_join_request_approve(stack):
    """Open space: request to join, then admin approves, user becomes a member."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Open",
        join_mode=JoinMode.OPEN,
    )
    req_id = await stack.space_svc.request_join(space.id, user_id=bob.user_id)
    member = await stack.space_svc.approve_join_request(req_id, actor_username="anna")
    assert member.user_id == bob.user_id
    assert member.role == "member"


async def test_join_request_deny(stack):
    """Denied join request does not add the user to the space."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Open",
        join_mode=JoinMode.OPEN,
    )
    req_id = await stack.space_svc.request_join(space.id, user_id=bob.user_id)
    await stack.space_svc.deny_join_request(req_id, actor_username="anna")
    members = await stack.space_repo.list_members(space.id)
    assert bob.user_id not in {m.user_id for m in members}


async def test_invite_only_rejects_join_request(stack):
    """Invite-only space rejects join requests with SpacePermissionError."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Private",
        join_mode=JoinMode.INVITE_ONLY,
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.request_join(space.id, user_id=bob.user_id)


async def test_update_config_branches(stack):
    """update_config handles name, description+emoji, features, join_mode, retention."""
    _anna = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="Original")

    updated = await stack.space_svc.update_config(
        space.id, actor_username="anna", name="Renamed"
    )
    assert updated.name == "Renamed"

    updated2 = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        description="A great space",
        emoji="🏠",
    )
    assert updated2.description == "A great space"

    new_features = SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED)
    updated3 = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=new_features,
    )
    assert updated3.features.posts_access == SpaceFeatureAccess.MODERATED

    updated4 = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        join_mode=JoinMode.OPEN,
    )
    assert updated4.join_mode == JoinMode.OPEN

    updated5 = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        retention_days=30,
    )
    assert updated5.retention_days == 30

    updated6 = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        retention_days=0,
    )
    assert updated6.retention_days is None


async def test_update_config_accepts_retention_exempt_types(stack):
    """retention_exempt_types round-trips through the repo."""
    await stack.provision_user("anna")
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Exempt",
    )
    updated = await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        retention_exempt_types=["list", "poll", "", "  ", "schedule"],
    )
    # Empty / whitespace entries stripped; rest preserved as a tuple.
    assert updated.retention_exempt_types == ("list", "poll", "schedule")


async def test_public_space_requires_coordinates(stack):
    """Creating a public space without lat/lon raises ValueError."""
    await stack.provision_user("a")
    with pytest.raises(ValueError, match="lat"):
        await stack.space_svc.create_space(
            owner_username="a",
            name="Pub",
            space_type=SpaceType.PUBLIC,
            join_mode=JoinMode.OPEN,
        )


async def test_public_space_with_coordinates(stack):
    """Public space stores 4dp-truncated coordinates."""
    await stack.provision_user("a")
    s = await stack.space_svc.create_space(
        owner_username="a",
        name="Pub",
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.OPEN,
        lat=52.376543,
        lon=4.895678,
        radius_km=5.0,
    )
    assert s.lat == 52.3765 and s.lon == 4.8957


async def test_non_member_cannot_post(stack):
    """Non-member posting raises SpacePermissionError."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.create_post(
            space.id,
            author_user_id=bob.user_id,
            type=PostType.TEXT,
            content="Unauthorised post",
        )


async def test_pin_unpin_alias(stack):
    """Sidebar pin, unpin, and space alias operations complete without error."""
    anna = await stack.provision_user("anna")
    space = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.pin(anna.user_id, space.id, position=1)
    await stack.space_svc.unpin(anna.user_id, space.id)
    await stack.space_svc.set_alias(space.id, username="anna", alias="home")
    assert True


# ─── Space post CRUD edge paths ──────────────────────────────────────────


async def test_space_edit_post_nonexistent(stack):
    """Editing a nonexistent space post raises KeyError."""
    with pytest.raises(KeyError):
        await stack.space_svc.edit_post("nope", editor_user_id="u", new_content="x")


async def test_space_edit_post_author_allowed(stack):
    """Author can edit their own space post."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=bob.user_id)
    p = await stack.space_svc.create_post(
        s.id, author_user_id=bob.user_id, type=PostType.TEXT, content="v1"
    )
    updated = await stack.space_svc.edit_post(
        p.id, editor_user_id=bob.user_id, new_content="v2"
    )
    assert updated.content == "v2"


async def test_space_edit_post_non_admin_rejected(stack):
    """Non-author non-admin editing raises PermissionError."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    carl = await stack.provision_user("carl")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=bob.user_id)
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=carl.user_id)
    p = await stack.space_svc.create_post(
        s.id, author_user_id=bob.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(PermissionError):
        await stack.space_svc.edit_post(
            p.id, editor_user_id=carl.user_id, new_content="y"
        )


async def test_space_delete_post_self_no_moderated_flag(stack):
    """Self-deleting a space post does not set moderated flag."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    await stack.space_svc.delete_post(p.id, actor_user_id=anna.user_id)
    got = (await stack.space_post_repo.get(p.id))[1]
    assert got.deleted and not got.moderated


async def test_space_delete_post_admin_sets_moderated(stack):
    """Admin deleting another's post sets moderated flag."""
    anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=bob.user_id)
    p = await stack.space_svc.create_post(
        s.id, author_user_id=bob.user_id, type=PostType.TEXT, content="x"
    )
    await stack.space_svc.delete_post(p.id, actor_user_id=anna.user_id)
    got = (await stack.space_post_repo.get(p.id))[1]
    assert got.deleted and got.moderated


async def test_space_delete_post_nonexistent(stack):
    """Deleting a nonexistent post raises KeyError."""
    with pytest.raises(KeyError):
        await stack.space_svc.delete_post("nope", actor_user_id="u")


async def test_space_delete_post_non_admin_rejected(stack):
    """Non-author non-admin cannot delete another's post."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    carl = await stack.provision_user("carl")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=bob.user_id)
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=carl.user_id)
    p = await stack.space_svc.create_post(
        s.id, author_user_id=bob.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(PermissionError):
        await stack.space_svc.delete_post(p.id, actor_user_id=carl.user_id)


async def test_space_reactions(stack):
    """Add and remove reaction on a space post."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    r = await stack.space_svc.add_reaction(p.id, user_id=anna.user_id, emoji=" 👍 ")
    assert "👍" in r.reactions
    r2 = await stack.space_svc.remove_reaction(p.id, user_id=anna.user_id, emoji="👍")
    assert "👍" not in r2.reactions


async def test_space_reaction_empty_rejected(stack):
    """Empty emoji raises ValueError."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(ValueError, match="empty"):
        await stack.space_svc.add_reaction(p.id, user_id=anna.user_id, emoji="")


async def test_space_comment_and_delete(stack):
    """Add comment, then admin deletes it."""
    anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=bob.user_id)
    p = await stack.space_svc.create_post(
        s.id, author_user_id=bob.user_id, type=PostType.TEXT, content="x"
    )
    c = await stack.space_svc.add_comment(
        p.id, author_user_id=bob.user_id, content="nice"
    )
    await stack.space_svc.delete_comment(c.id, actor_user_id=anna.user_id)
    got = await stack.space_post_repo.get_comment(c.id)
    assert got.deleted


async def test_space_comment_non_member_rejected(stack):
    """Non-member cannot comment on a space post."""
    anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.add_comment(
            p.id, author_user_id=bob.user_id, content="nope"
        )


async def test_space_comment_on_deleted_post(stack):
    """Commenting on a deleted post raises KeyError."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    await stack.space_svc.delete_post(p.id, actor_user_id=anna.user_id)
    with pytest.raises(KeyError, match="deleted"):
        await stack.space_svc.add_comment(
            p.id, author_user_id=anna.user_id, content="late"
        )


async def test_space_comment_empty_content_rejected(stack):
    """Empty comment content raises ValueError."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(ValueError, match="content"):
        await stack.space_svc.add_comment(
            p.id, author_user_id=anna.user_id, content="  "
        )


async def test_space_delete_comment_nonexistent(stack):
    """Deleting a nonexistent comment raises KeyError."""
    with pytest.raises(KeyError):
        await stack.space_svc.delete_comment("nope", actor_user_id="u")


async def test_space_delete_comment_non_admin_rejected(stack):
    """Non-author non-admin cannot delete someone else's comment."""
    _anna = await stack.provision_user("anna")
    bob = await stack.provision_user("bob")
    carl = await stack.provision_user("carl")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=bob.user_id)
    await stack.space_svc.add_member(s.id, actor_username="anna", user_id=carl.user_id)
    p = await stack.space_svc.create_post(
        s.id, author_user_id=bob.user_id, type=PostType.TEXT, content="x"
    )
    c = await stack.space_svc.add_comment(
        p.id, author_user_id=bob.user_id, content="hi"
    )
    with pytest.raises(PermissionError):
        await stack.space_svc.delete_comment(c.id, actor_user_id=carl.user_id)


async def test_space_list_feed(stack):
    """list_feed returns posts scoped to the space."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="a"
    )
    await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="b"
    )
    feed = await stack.space_svc.list_feed(s.id, limit=10)
    assert len(feed) == 2


async def test_space_create_post_type_not_allowed(stack):
    """Posting a disallowed type raises SpacePermissionError."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(
        owner_username="anna",
        name="S",
        features=SpaceFeatures(allowed_post_types=("text",)),
    )
    with pytest.raises(SpacePermissionError, match="does not allow"):
        await stack.space_svc.create_post(
            s.id,
            author_user_id=anna.user_id,
            type="image",
            media_url="/img.webp",
        )


async def test_space_create_post_text_empty_rejected(stack):
    """Text post with empty content raises ValueError."""
    anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(owner_username="anna", name="S")
    with pytest.raises(ValueError, match="content"):
        await stack.space_svc.create_post(
            s.id,
            author_user_id=anna.user_id,
            type=PostType.TEXT,
            content="  ",
        )


async def test_public_space_coordinate_truncation(stack):
    """Public space coordinates are truncated to 4dp."""
    _anna = await stack.provision_user("anna")
    s = await stack.space_svc.create_space(
        owner_username="anna",
        name="Pub",
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.OPEN,
        lat=52.376543,
        lon=4.895678,
        radius_km=5.0,
    )
    assert s.lat == 52.3765
    assert s.lon == 4.8957


# ── Helper function coverage ──────────────────────────────────────────────


def test_coerce_space_type_string():
    """String space type is coerced to enum."""
    from socialhome.services.space_service import _coerce_space_type
    from socialhome.domain.space import SpaceType

    assert _coerce_space_type("private") is SpaceType.PRIVATE
    assert _coerce_space_type(SpaceType.PUBLIC) is SpaceType.PUBLIC


def test_coerce_space_type_invalid():
    """Invalid space type string raises ValueError."""
    from socialhome.services.space_service import _coerce_space_type

    with pytest.raises(ValueError, match="invalid space type"):
        _coerce_space_type("bogus")


def test_coerce_join_mode_string():
    """String join mode is coerced to enum."""
    from socialhome.services.space_service import _coerce_join_mode
    from socialhome.domain.space import JoinMode

    assert _coerce_join_mode("open") is JoinMode.OPEN
    assert _coerce_join_mode(JoinMode.INVITE_ONLY) is JoinMode.INVITE_ONLY


def test_coerce_join_mode_invalid():
    """Invalid join mode raises ValueError."""
    from socialhome.services.space_service import _coerce_join_mode

    with pytest.raises(ValueError, match="invalid join mode"):
        _coerce_join_mode("bogus")


def test_coerce_post_type():
    """Post type coercion works for strings and enums."""
    from socialhome.services.space_service import _coerce_post_type

    assert _coerce_post_type("text") is PostType.TEXT
    assert _coerce_post_type(PostType.IMAGE) is PostType.IMAGE
    with pytest.raises(ValueError):
        _coerce_post_type("bogus")


def test_coerce_comment_type():
    """Comment type coercion works."""
    from socialhome.services.space_service import _coerce_comment_type
    from socialhome.domain.post import CommentType

    assert _coerce_comment_type("text") is CommentType.TEXT
    assert _coerce_comment_type(CommentType.IMAGE) is CommentType.IMAGE
    with pytest.raises(ValueError):
        _coerce_comment_type("bogus")


def test_validate_space_content_file():
    """File post without file_meta raises ValueError."""
    from socialhome.services.space_service import _validate_space_content

    with pytest.raises(ValueError, match="file_meta"):
        _validate_space_content(PostType.FILE, None, None)


def test_validate_space_content_text_empty():
    """Text post with empty content raises ValueError."""
    from socialhome.services.space_service import _validate_space_content

    with pytest.raises(ValueError, match="content"):
        _validate_space_content(PostType.TEXT, "   ", None)


def test_validate_text_length():
    """Over-length content raises ValueError."""
    from socialhome.services.space_service import _validate_text_length

    with pytest.raises(ValueError, match="maximum length"):
        _validate_text_length("x" * 10001, limit=10000)
    _validate_text_length(None, limit=100)  # None is OK


# ── More service edge paths ───────────────────────────────────────────────


async def test_space_create_unknown_owner(stack):
    """Creating space with unknown owner raises KeyError."""
    with pytest.raises(KeyError, match="owner"):
        await stack.space_svc.create_space(owner_username="ghost", name="X")


async def test_space_create_empty_name(stack):
    """Creating space with empty name raises ValueError."""
    await stack.provision_user("emp")
    with pytest.raises(ValueError, match="empty"):
        await stack.space_svc.create_space(owner_username="emp", name="  ")


async def test_space_update_unknown_actor(stack):
    """update_config with unknown actor raises KeyError."""
    _anna = await stack.provision_user("upd_anna")
    s = await stack.space_svc.create_space(owner_username="upd_anna", name="S")
    with pytest.raises(KeyError):
        await stack.space_svc.update_config(s.id, actor_username="ghost", name="X")


async def test_space_remove_member_unknown_actor(stack):
    """remove_member with unknown actor raises KeyError."""
    _anna = await stack.provision_user("rm_anna")
    s = await stack.space_svc.create_space(owner_username="rm_anna", name="S")
    with pytest.raises(KeyError):
        await stack.space_svc.remove_member(s.id, actor_username="ghost", user_id="x")


async def test_space_edit_post_deleted_rejected(stack):
    """Editing a deleted post raises KeyError."""
    anna = await stack.provision_user("edel_anna")
    s = await stack.space_svc.create_space(owner_username="edel_anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    await stack.space_svc.delete_post(p.id, actor_user_id=anna.user_id)
    with pytest.raises(KeyError, match="deleted"):
        await stack.space_svc.edit_post(
            p.id, editor_user_id=anna.user_id, new_content="y"
        )


async def test_space_comment_image_no_media(stack):
    """Image comment without media_url raises ValueError."""
    anna = await stack.provision_user("img_anna")
    s = await stack.space_svc.create_space(owner_username="img_anna", name="S")
    p = await stack.space_svc.create_post(
        s.id, author_user_id=anna.user_id, type=PostType.TEXT, content="x"
    )
    with pytest.raises(ValueError, match="media_url"):
        await stack.space_svc.add_comment(
            p.id, author_user_id=anna.user_id, comment_type="image"
        )


# ── Subscriptions (read-only membership) ──────────────────────────────────


async def test_subscribe_public_space_adds_subscriber_member(stack):
    """Subscribing to a public space inserts a ``role='subscriber'`` row in
    ``space_members`` — subscribers are read-only members under the hood."""
    owner = await stack.provision_user("owner1")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner1", name="P", space_type=SpaceType.GLOBAL
    )
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)

    assert await stack.space_svc.is_subscribed(fan.user_id, space.id) is True
    member = await stack.space_repo.get_member(space.id, fan.user_id)
    assert member is not None
    assert member.role == "subscriber"
    # The space owner is still an owner, not demoted.
    owner_mem = await stack.space_repo.get_member(space.id, owner.user_id)
    assert owner_mem.role == "owner"


async def test_subscribe_private_space_rejected(stack):
    """Private / household spaces cannot be followed — joining requires
    an invite."""
    await stack.provision_user("owner2")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner2", name="Priv", space_type=SpaceType.PRIVATE
    )
    with pytest.raises(SpacePermissionError, match="public / global"):
        await stack.space_svc.subscribe_to_space(fan.user_id, space.id)


async def test_subscribe_is_idempotent(stack):
    """Double-subscribe does not error and does not create duplicate rows."""
    await stack.provision_user("owner3")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner3", name="P", space_type=SpaceType.GLOBAL
    )
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)
    follows = await stack.space_svc.list_subscriptions(fan.user_id)
    assert len(follows) == 1


async def test_subscribe_does_not_demote_existing_member(stack):
    """An existing real member who calls follow stays at their current
    role — never gets demoted to subscriber."""
    await stack.provision_user("owner4")
    real = await stack.provision_user("real")
    space = await stack.space_svc.create_space(
        owner_username="owner4", name="P", space_type=SpaceType.GLOBAL
    )
    await stack.space_svc.add_member(
        space.id, actor_username="owner4", user_id=real.user_id
    )
    await stack.space_svc.subscribe_to_space(real.user_id, space.id)
    member = await stack.space_repo.get_member(space.id, real.user_id)
    assert member.role == "member"
    # Not listed as a subscriber.
    assert await stack.space_svc.list_subscriptions(real.user_id) == []


async def test_unsubscribe_removes_subscriber_only(stack):
    """Unsubscribe removes a ``role='subscriber'`` row; a real member is
    untouched (so unsubscribe can't be used to silently leave a space)."""
    await stack.provision_user("owner5")
    fan = await stack.provision_user("fan")
    real = await stack.provision_user("real")
    space = await stack.space_svc.create_space(
        owner_username="owner5", name="P", space_type=SpaceType.GLOBAL
    )
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)
    await stack.space_svc.add_member(
        space.id, actor_username="owner5", user_id=real.user_id
    )

    await stack.space_svc.unsubscribe_from_space(fan.user_id, space.id)
    assert await stack.space_repo.get_member(space.id, fan.user_id) is None

    await stack.space_svc.unsubscribe_from_space(real.user_id, space.id)
    still = await stack.space_repo.get_member(space.id, real.user_id)
    assert still is not None
    assert still.role == "member"


async def test_list_subscriptions_only_returns_subscribers(stack):
    """``list_subscriptions`` filters out spaces where the user is a real
    member — only ``role='subscriber'`` rows are listed."""
    await stack.provision_user("owner6")
    u = await stack.provision_user("multi")
    pub = await stack.space_svc.create_space(
        owner_username="owner6", name="Pub", space_type=SpaceType.GLOBAL
    )
    mem_space = await stack.space_svc.create_space(
        owner_username="owner6", name="Mem", space_type=SpaceType.GLOBAL
    )
    await stack.space_svc.subscribe_to_space(u.user_id, pub.id)
    await stack.space_svc.add_member(
        mem_space.id, actor_username="owner6", user_id=u.user_id
    )
    follows = await stack.space_svc.list_subscriptions(u.user_id)
    assert [r["space_id"] for r in follows] == [pub.id]


async def test_subscriber_cannot_create_post(stack):
    """§ read-only membership: subscribers are rejected on post create."""
    await stack.provision_user("owner7")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner7", name="P", space_type=SpaceType.GLOBAL
    )
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)
    with pytest.raises(SpacePermissionError, match="subscribers can only read"):
        await stack.space_svc.create_post(
            space.id,
            author_user_id=fan.user_id,
            type=PostType.TEXT,
            content="should be blocked",
        )


async def test_subscriber_cannot_comment(stack):
    await stack.provision_user("owner8")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner8", name="P", space_type=SpaceType.GLOBAL
    )
    post = await stack.space_svc.create_post(
        space.id,
        author_user_id=(await stack.user_svc.get("owner8")).user_id,
        type=PostType.TEXT,
        content="hi",
    )
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)
    with pytest.raises(SpacePermissionError, match="subscribers can only read"):
        await stack.space_svc.add_comment(
            post.id, author_user_id=fan.user_id, content="reply"
        )


async def test_subscriber_cannot_react(stack):
    await stack.provision_user("owner9")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner9", name="P", space_type=SpaceType.GLOBAL
    )
    post = await stack.space_svc.create_post(
        space.id,
        author_user_id=(await stack.user_svc.get("owner9")).user_id,
        type=PostType.TEXT,
        content="hi",
    )
    await stack.space_svc.subscribe_to_space(fan.user_id, space.id)
    with pytest.raises(SpacePermissionError, match="subscribers can only read"):
        await stack.space_svc.add_reaction(post.id, user_id=fan.user_id, emoji="👍")


async def test_subscribe_banned_user_rejected(stack):
    await stack.provision_user("owner10")
    fan = await stack.provision_user("fan")
    space = await stack.space_svc.create_space(
        owner_username="owner10", name="P", space_type=SpaceType.GLOBAL
    )
    # Seed a ban row directly.
    await stack.space_repo.ban_member(
        space.id, fan.user_id, banned_by="owner10-uid", reason="test"
    )
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.subscribe_to_space(fan.user_id, space.id)


async def test_update_config_publishes_location_mode_changed(stack):
    """Flipping ``features.location_mode`` publishes
    :class:`SpaceLocationModeChanged` so SpaceLocationOutbound can
    refire the latest presence under the new tier (§23.8.6)."""
    from socialhome.domain.events import SpaceLocationModeChanged

    captured: list[SpaceLocationModeChanged] = []

    async def _capture(ev: SpaceLocationModeChanged) -> None:
        captured.append(ev)

    stack.space_svc._bus.subscribe(SpaceLocationModeChanged, _capture)

    _a = await stack.provision_user("anna", is_admin=True)
    space = await stack.space_svc.create_space(
        owner_username="anna",
        name="Loc",
    )
    # Default mode is gps; flipping to zone_only must publish.
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(location=True, location_mode="zone_only"),
    )
    assert len(captured) == 1
    assert captured[0].space_id == space.id
    assert captured[0].new_mode == "zone_only"

    # Same mode again — no extra publish.
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(location=True, location_mode="zone_only"),
    )
    assert len(captured) == 1

    # Back to gps — publishes again.
    await stack.space_svc.update_config(
        space.id,
        actor_username="anna",
        features=SpaceFeatures(location=True, location_mode="gps"),
    )
    assert len(captured) == 2
    assert captured[1].new_mode == "gps"
