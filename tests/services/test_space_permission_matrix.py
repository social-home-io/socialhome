"""Permission-matrix smoke test for SpaceService.

Asserts that every gated mutating service method refuses a non-member
actor (and the owner-only paths refuse non-owner members). Per-method
behavioural tests live in ``test_space_service.py``; this file only
checks "does *some* guard fire?", so a future contributor who ships a
new mutation without a guard fails loudly here.

Two checks run:

1. **Static enumeration.** :data:`GATED_METHODS` lists the public async
   methods that must reject non-members; :data:`UNGATED_METHODS` lists
   the ones that intentionally bypass the gate (reads, self-service,
   federation inbound). The two sets must together equal the public
   async surface of :class:`SpaceService` — if the surface drifts, the
   test fails and forces an explicit decision.

2. **Behavioural smoke.** A representative subset of the gated paths
   is invoked as a non-member actor; each call must raise either
   :class:`SpacePermissionError` or :class:`PermissionError`. We do not
   re-test every method here — the static check above is what guards
   against drift.
"""

from __future__ import annotations

import inspect

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.post import PostType
from socialhome.domain.space import (
    SpaceMember,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService


# ─── Method classification ────────────────────────────────────────────────

#: Public async methods that MUST refuse non-members (or non-owners).
#: Add new mutating methods here when they ship.
GATED_METHODS: frozenset[str] = frozenset({
    "set_cover",
    "clear_cover",
    "dissolve_space",
    "update_config",
    "add_member",
    "remove_member",
    "set_role",
    "update_member_profile",
    "set_member_picture",
    "clear_member_picture",
    "transfer_ownership",
    "ban",
    "unban",
    "create_invite_token",
    "invite_remote_user",
    "remove_remote_member",
    "approve_join_request",
    "deny_join_request",
    "create_post",
    "approve_moderation_item",
    "reject_moderation_item",
    "edit_post",
    "delete_post",
    "add_reaction",
    "remove_reaction",
    "add_comment",
    "edit_comment",
    "delete_comment",
    "upsert_link",
    "delete_link",
})

#: Public async methods that intentionally skip the member/admin gate.
#: Each entry needs a justification — listed inline.
UNGATED_METHODS: frozenset[str] = frozenset({
    # Pure reads.
    "list_feed",
    "list_links",
    "list_pending_moderation",
    "list_subscriptions",
    "is_subscribed",
    # Self-service joins / leaves — gate is on the *invite* / *request*,
    # not the join action itself.
    "create_space",                   # creator becomes owner
    "accept_invite_token",            # token validates the actor
    "accept_remote_invite",           # cross-instance — own user
    "decline_remote_invite",          # cross-instance — own user
    "request_join",                   # any user may request; gate is on approval
    "request_join_remote",            # cross-instance request
    "subscribe_to_space",             # public-space follow-only
    "unsubscribe_from_space",         # own subscription only
    # Federation inbound hooks — validated by the §24.11 inbound pipeline.
    "on_remote_join_request_approved",
    # Personal sidebar state — keyed on the calling user_id, no
    # space-permission shape.
    "pin",
    "unpin",
    "set_alias",
})


# ─── Drift guard ──────────────────────────────────────────────────────────


def _public_async_methods() -> set[str]:
    """Public async methods on :class:`SpaceService`."""
    return {
        name
        for name, member in inspect.getmembers(SpaceService)
        if inspect.iscoroutinefunction(member) and not name.startswith("_")
    }


def test_method_classification_covers_full_public_surface():
    """Every public async method on SpaceService must be classified.

    Catches the drift case where a contributor ships a new mutation
    without thinking about the permission gate. If this fails, add the
    new method to :data:`GATED_METHODS` (with a corresponding gate in
    the service) or to :data:`UNGATED_METHODS` (with an inline reason).
    """
    surface = _public_async_methods()
    classified = GATED_METHODS | UNGATED_METHODS
    missing = surface - classified
    overlap = GATED_METHODS & UNGATED_METHODS
    assert not missing, (
        "new SpaceService method(s) not classified in test_space_permission_matrix.py: "
        f"{sorted(missing)}"
    )
    assert not overlap, (
        f"method(s) listed as both gated and ungated: {sorted(overlap)}"
    )


# ─── Behavioural smoke ────────────────────────────────────────────────────


@pytest.fixture
async def stack(tmp_dir):
    """Minimal SpaceService stack with anna (owner) + bob (non-member)."""
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

    anna = await user_svc.provision(
        username="anna", display_name="Anna", is_admin=True
    )
    bob = await user_svc.provision(username="bob", display_name="Bob")
    space = await space_svc.create_space(owner_username="anna", name="S")
    public_space = await space_svc.create_space(
        owner_username="anna",
        name="P",
        space_type=SpaceType.PUBLIC,
        lat=47.5,
        lon=8.5,
        radius_km=5.0,
    )

    class Stack:
        pass

    s = Stack()
    s.db = db
    s.space_svc = space_svc
    s.space_repo = space_repo
    s.anna = anna
    s.bob = bob
    s.space = space
    s.public_space = public_space
    yield s
    await db.shutdown()


# Each row: a label and an async invoker that calls a mutating method
# as ``bob`` (a non-member). The smoke check asserts every call raises
# :class:`SpacePermissionError` or :class:`PermissionError`.
@pytest.mark.parametrize(
    "label,invoke",
    [
        (
            "update_config",
            lambda s: s.space_svc.update_config(
                s.space.id, actor_username="bob", name="hijacked"
            ),
        ),
        (
            "add_member",
            lambda s: s.space_svc.add_member(
                s.space.id, actor_username="bob", user_id="uid-x"
            ),
        ),
        (
            "set_role",
            lambda s: s.space_svc.set_role(
                s.space.id,
                actor_username="bob",
                user_id=s.anna.user_id,
                role=SpaceRole.ADMIN,
            ),
        ),
        (
            "transfer_ownership",
            lambda s: s.space_svc.transfer_ownership(
                s.space.id, actor_username="bob", to_user_id=s.bob.user_id
            ),
        ),
        (
            "ban",
            lambda s: s.space_svc.ban(
                s.space.id, actor_username="bob", user_id=s.anna.user_id
            ),
        ),
        (
            "create_post (non-member)",
            lambda s: s.space_svc.create_post(
                s.space.id,
                author_user_id=s.bob.user_id,
                type=PostType.TEXT,
                content="hi",
            ),
        ),
        (
            "upsert_link",
            lambda s: s.space_svc.upsert_link(
                space_id=s.space.id,
                actor_username="bob",
                link_id=None,
                label="x",
                url="https://example.com",
                position=0,
            ),
        ),
        (
            "create_invite_token",
            lambda s: s.space_svc.create_invite_token(
                s.space.id, actor_username="bob"
            ),
        ),
    ],
)
async def test_non_member_is_rejected(stack, label, invoke):
    """Each gated mutation refuses a non-member actor."""
    with pytest.raises((SpacePermissionError, PermissionError)):
        await invoke(stack)


async def test_assert_writable_member_blocks_subscriber():
    """Direct unit test for the new sync helper."""
    sub = SpaceMember(
        space_id="sp-x",
        user_id="uid-x",
        role=SpaceRole.SUBSCRIBER,
        joined_at="2026-04-28T00:00:00+00:00",
    )
    with pytest.raises(SpacePermissionError):
        SpaceService._assert_writable_member(sub, action="post")


@pytest.mark.parametrize("role", [SpaceRole.OWNER, SpaceRole.ADMIN, SpaceRole.MEMBER])
def test_assert_writable_member_allows_non_subscriber(role):
    """Owner / admin / member all pass the writable-member gate."""
    member = SpaceMember(
        space_id="sp-x",
        user_id="uid-x",
        role=role,
        joined_at="2026-04-28T00:00:00+00:00",
    )
    SpaceService._assert_writable_member(member, action="post")  # no raise


async def test_subscriber_cannot_post(stack):
    """Subscribing then attempting to post raises via _assert_writable_member."""
    await stack.space_svc.subscribe_to_space(stack.bob.user_id, stack.public_space.id)
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.create_post(
            stack.public_space.id,
            author_user_id=stack.bob.user_id,
            type=PostType.TEXT,
            content="should fail",
        )


async def test_subscriber_cannot_comment(stack):
    """Subscribing then attempting to comment raises via _assert_writable_member."""
    post = await stack.space_svc.create_post(
        stack.public_space.id,
        author_user_id=stack.anna.user_id,
        type=PostType.TEXT,
        content="anchor",
    )
    assert post is not None
    await stack.space_svc.subscribe_to_space(stack.bob.user_id, stack.public_space.id)
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.add_comment(
            post.id,
            author_user_id=stack.bob.user_id,
            content="should fail",
        )


async def test_subscriber_cannot_react(stack):
    """Subscribing then attempting to react raises via _reject_subscriber."""
    post = await stack.space_svc.create_post(
        stack.public_space.id,
        author_user_id=stack.anna.user_id,
        type=PostType.TEXT,
        content="anchor",
    )
    assert post is not None
    await stack.space_svc.subscribe_to_space(stack.bob.user_id, stack.public_space.id)
    with pytest.raises(SpacePermissionError):
        await stack.space_svc.add_reaction(
            post.id, user_id=stack.bob.user_id, emoji="👍"
        )
