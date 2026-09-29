"""Tests for socialhome.services.space_mentions."""

from __future__ import annotations

from dataclasses import replace

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.mention import MentionType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.domain.user import RemoteUser
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_mentions import SpaceMentionResolver
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES('peer-b', 'peer-b', ?, 'k1',"
        " 'k2', 'https://peer-b/wh', 'wh-peer-b', 'confirmed', 'manual')",
        ("00" * 32,),
    )
    bus = EventBus()
    users = SqliteUserRepo(db)
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x09" * 32))
    remote = SqliteSpaceRemoteMemberRepo(db)
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)
    space_svc = SpaceService(
        spaces, SqliteSpacePostRepo(db), users, bus, own_instance_id=iid
    )

    class Env:
        pass

    e = Env()
    e.db, e.users, e.spaces, e.remote = db, users, spaces, remote
    e.user_svc, e.space_svc = user_svc, space_svc
    e.resolver = SpaceMentionResolver(spaces, users, remote)
    yield e
    await db.shutdown()


async def _seat_remote(env, space_id, user_id, *, username, handle=None):
    await env.users.upsert_remote(
        RemoteUser(
            user_id=user_id,
            instance_id="peer-b",
            remote_username=username,
            display_name=username.title(),
            handle=handle,
        )
    )
    await env.remote.add(
        space_id=space_id,
        instance_id="peer-b",
        user_id=user_id,
        user_pk=None,
        display_name=username.title(),
    )


async def test_resolves_local_and_remote_members(env):
    anna = await env.user_svc.provision(username="anna", display_name="Anna")
    bob = await env.user_svc.provision(username="bob", display_name="Bob")
    space = await env.space_svc.create_space(owner_username="anna", name="S")
    await env.space_svc.add_member(space.id, actor_username="anna", user_id=bob.user_id)
    await _seat_remote(env, space.id, "remote-carol-1", username="carol")

    # No author → @here is dropped (fail closed); user mentions stay.
    out = await env.resolver.resolve(
        space.id, "hi @bob and @Carol, @here", author_id=None
    )
    assert [m.type for m in out] == [MentionType.USER, MentionType.USER]
    assert [m.user_id for m in out] == [bob.user_id, "remote-carol-1"]
    assert anna.user_id not in {m.user_id for m in out}


async def test_non_member_never_resolves(env):
    """A household user who isn't in the space can't be mentioned into it."""
    await env.user_svc.provision(username="anna", display_name="Anna")
    await env.user_svc.provision(username="dave", display_name="Dave")
    space = await env.space_svc.create_space(owner_username="anna", name="S")
    assert await env.resolver.resolve(space.id, "@dave look", author_id=None) == ()


async def test_handle_resolves_and_empty_content_short_circuits(env):
    await env.user_svc.provision(username="anna", display_name="Anna")
    bob = await env.user_svc.provision(username="bob", display_name="Bob")
    await env.user_svc.set_handle("bob", "bobby")
    space = await env.space_svc.create_space(owner_username="anna", name="S")
    await env.space_svc.add_member(space.id, actor_username="anna", user_id=bob.user_id)
    out = await env.resolver.resolve(space.id, "@bobby and @bob", author_id=None)
    assert [m.user_id for m in out] == [bob.user_id]
    assert await env.resolver.resolve(space.id, None, author_id=None) == ()
    assert await env.resolver.resolve(space.id, "no mentions", author_id=None) == ()


async def test_colliding_handles_get_qualified_tokens(env):
    """A local and a remote ``anna``: the bare token resolves to nobody,
    the tokens the members API hands the composer resolve uniquely."""
    anna = await env.user_svc.provision(username="anna", display_name="Anna")
    space = await env.space_svc.create_space(owner_username="anna", name="S")
    await _seat_remote(env, space.id, "ranna0123456", username="anna")

    tokens = await env.resolver.tokens(space.id)
    assert set(tokens) == {anna.user_id, "ranna0123456"}
    assert await env.resolver.resolve(space.id, "@anna", author_id=None) == ()
    for uid, tok in tokens.items():
        assert tok is not None
        out = await env.resolver.resolve(space.id, f"ping @{tok}", author_id=None)
        assert [m.user_id for m in out] == [uid]


async def test_deprovisioned_remote_and_missing_rows_are_skipped(env):
    await env.user_svc.provision(username="anna", display_name="Anna")
    space = await env.space_svc.create_space(owner_username="anna", name="S")
    # Remote seat without a remote_users row → no handle to match.
    await env.remote.add(
        space_id=space.id,
        instance_id="peer-b",
        user_id="ghost-1",
        user_pk=None,
        display_name="Ghost",
    )
    await env.users.upsert_remote(
        RemoteUser(
            user_id="gone-1",
            instance_id="peer-b",
            remote_username="gone",
            display_name="Gone",
        )
    )
    await env.users.mark_remote_deprovisioned("gone-1")
    await env.remote.add(
        space_id=space.id,
        instance_id="peer-b",
        user_id="gone-1",
        user_pk=None,
        display_name="Gone",
    )
    tokens = await env.resolver.tokens(space.id)
    assert "ghost-1" not in tokens and "gone-1" not in tokens
    assert await env.resolver.resolve(space.id, "@gone @ghost", author_id=None) == ()


async def test_without_remote_repo_only_local_members(env):
    bob = await env.user_svc.provision(username="bob", display_name="Bob")
    await env.user_svc.provision(username="anna", display_name="Anna")
    space = await env.space_svc.create_space(owner_username="anna", name="S")
    await env.space_svc.add_member(space.id, actor_username="anna", user_id=bob.user_id)
    await _seat_remote(env, space.id, "remote-carol-1", username="carol")
    local_only = SpaceMentionResolver(env.spaces, env.users, None)
    out = await local_only.resolve(space.id, "@bob @carol", author_id=None)
    assert [m.user_id for m in out] == [bob.user_id]


async def test_remote_user_on_a_local_seat_and_inactive_local_user(env):
    """A remote user seated in ``space_members`` resolves via
    ``remote_users``; a soft-deleted local member is not mentionable."""
    await env.user_svc.provision(username="anna", display_name="Anna")
    eve = await env.user_svc.provision(username="eve", display_name="Eve")
    space = await env.space_svc.create_space(owner_username="anna", name="S")
    await env.space_svc.add_member(space.id, actor_username="anna", user_id=eve.user_id)
    await env.users.soft_delete("eve")
    await env.users.upsert_remote(
        RemoteUser(
            user_id="r-zed",
            instance_id="peer-b",
            remote_username="zed",
            display_name="Zed",
            handle="zeddy",
        )
    )
    await env.spaces.save_member(
        SpaceMember(space_id=space.id, user_id="r-zed", role="member", joined_at="")
    )
    out = await env.resolver.resolve(space.id, "@zeddy @eve", author_id=None)
    assert [m.user_id for m in out] == ["r-zed"]
    assert (await env.resolver.tokens(space.id))["r-zed"] == "zeddy"


# ─── @here: toggle + role, read from THIS household's roster ───────────────


async def _here_space(env, *, allow=True):
    """Space owned by local ``anna`` with local admin ``ada``, local member
    ``bob``, a remote admin seat, a remote member seat and a remote
    subscriber seat."""
    await env.user_svc.provision(username="anna", display_name="Anna")
    ada = await env.user_svc.provision(username="ada", display_name="Ada")
    bob = await env.user_svc.provision(username="bob", display_name="Bob")
    space = await env.space_svc.create_space(owner_username="anna", name="S")
    for u in (ada, bob):
        await env.space_svc.add_member(
            space.id, actor_username="anna", user_id=u.user_id
        )
    await env.spaces.set_role(space.id, ada.user_id, SpaceRole.ADMIN.value)
    for uid, uname, role in (
        ("r-admin", "radmin", SpaceRole.ADMIN.value),
        ("r-member", "rmember", SpaceRole.MEMBER.value),
        ("r-sub", "rsub", SpaceRole.SUBSCRIBER.value),
    ):
        await _seat_remote(env, space.id, uid, username=uname)
        await env.remote.set_role(space.id, "peer-b", uid, role)
    fresh = await env.spaces.get(space.id)
    await env.spaces.save(replace(fresh, allow_here_mention=allow))
    return space, ada, bob


async def test_here_kept_only_for_owner_and_admins(env):
    space, ada, bob = await _here_space(env)
    anna = await env.users.get("anna")

    async def here(author_id):
        out = await env.resolver.resolve(space.id, "@here standup", author_id=author_id)
        return [m.type for m in out] == [MentionType.HERE]

    assert await here(anna.user_id)  # local owner
    assert await here(ada.user_id)  # local admin
    assert await here("r-admin")  # remote admin seat (roster mirror)
    assert not await here(bob.user_id)  # local member
    assert not await here("r-member")  # remote member seat
    assert not await here("r-sub")  # remote subscriber seat
    assert not await here("stranger")  # no seat at all
    assert not await here(None)  # unknown author → fail closed


async def test_here_dropped_when_space_toggle_off(env):
    space, ada, _ = await _here_space(env, allow=False)
    anna = await env.users.get("anna")
    for author in (anna.user_id, ada.user_id, "r-admin"):
        assert await env.resolver.resolve(space.id, "@here hi", author_id=author) == ()


async def test_here_disallowed_keeps_user_mentions(env):
    space, _, bob = await _here_space(env)
    out = await env.resolver.resolve(space.id, "@here @ada", author_id=bob.user_id)
    assert [m.type for m in out] == [MentionType.USER]


async def test_remote_host_owner_may_use_here(env):
    """On a member household the host's owner is mirrored as a plain seat
    (a remote seat can't hold ``owner``); it is recognised as the owner by
    being seated on the host instance under the space's owner username."""
    space = Space(
        id="sp-stub",
        name="Stub",
        owner_instance_id="peer-b",
        owner_username="hostowner",
        identity_public_key="00" * 32,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        allow_here_mention=True,
    )
    await env.spaces.save(space)
    await _seat_remote(env, space.id, "r-owner", username="hostowner")
    await _seat_remote(env, space.id, "r-imposter", username="hostowner2")
    out = await env.resolver.resolve(space.id, "@here", author_id="r-owner")
    assert [m.type for m in out] == [MentionType.HERE]
    assert await env.resolver.resolve(space.id, "@here", author_id="r-imposter") == ()
    # The owner username seated on a DIFFERENT household is not the owner.
    await env.db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES('peer-c', 'peer-c', ?, 'k1',"
        " 'k2', 'https://peer-c/wh', 'wh-peer-c', 'confirmed', 'manual')",
        ("00" * 32,),
    )
    await env.users.upsert_remote(
        RemoteUser(
            user_id="r-fake-owner",
            instance_id="peer-c",
            remote_username="hostowner",
            display_name="Fake",
        )
    )
    await env.remote.add(
        space_id=space.id,
        instance_id="peer-c",
        user_id="r-fake-owner",
        user_pk=None,
        display_name="Fake",
    )
    assert await env.resolver.resolve(space.id, "@here", author_id="r-fake-owner") == ()


async def test_here_unknown_space_is_dropped(env):
    assert await env.resolver.resolve("nope", "@here", author_id="x") == ()
