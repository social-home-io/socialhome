"""Tests for socialhome.services.dm_mentions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationType,
    RemoteConversationMember,
    SystemChatScope,
)
from socialhome.domain.mention import MentionType
from socialhome.domain.user import RemoteUser
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.dm_mentions import DmMentionResolver
from socialhome.services.user_service import UserService


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES('peer-b', 'peer-b', ?, 'k1',"
        " 'k2', 'https://peer-b/wh', 'wh-peer-b', 'confirmed', 'manual')",
        ("00" * 32,),
    )
    users = SqliteUserRepo(db)
    convos = SqliteConversationRepo(db)
    user_svc = UserService(users, EventBus(), own_instance_public_key=kp.public_key)
    assert derive_instance_id(kp.public_key)

    class Env:
        pass

    e = Env()
    e.db, e.users, e.convos, e.user_svc = db, users, convos, user_svc
    e.resolver = DmMentionResolver(convos, users)
    yield e
    await db.shutdown()


async def _group(env, conv_id, *usernames, type=ConversationType.GROUP_DM):
    await env.convos.create(
        Conversation(id=conv_id, type=type, created_at=datetime.now(timezone.utc))
    )
    for u in usernames:
        await env.convos.add_member(
            ConversationMember(
                conversation_id=conv_id,
                username=u,
                joined_at=datetime.now(timezone.utc).isoformat(),
            )
        )


async def test_resolves_local_and_remote_seats(env):
    """Local members, a paired remote member (``remote_users``) and a group
    seat on a household never paired with (seat ``user_id``) all resolve."""
    anna = await env.user_svc.provision(username="anna", display_name="Anna")
    bob = await env.user_svc.provision(username="bob", display_name="Bob")
    await env.user_svc.provision(username="dave", display_name="Dave")
    await _group(env, "g1", "anna", "bob")
    await env.users.upsert_remote(
        RemoteUser(
            user_id="remote-carol-1",
            instance_id="peer-b",
            remote_username="carol",
            display_name="Carol",
            handle="caz",
        )
    )
    await env.convos.add_remote_member(
        RemoteConversationMember(
            conversation_id="g1",
            instance_id="peer-b",
            remote_username="carol",
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    await env.convos.add_remote_member(
        RemoteConversationMember(
            conversation_id="g1",
            instance_id="peer-z",
            remote_username="erin",
            joined_at=datetime.now(timezone.utc).isoformat(),
            user_id="seat-erin-1",
            display_name="Erin",
        )
    )
    out = await env.resolver.resolve("g1", "@bob @caz @erin @dave @anna")
    assert [m.user_id for m in out] == [
        bob.user_id,
        "remote-carol-1",
        "seat-erin-1",
        anna.user_id,
    ]
    # dave is a household user but not seated → never resolves.
    tokens = await env.resolver.tokens("g1")
    assert tokens[bob.user_id] == "bob"
    assert tokens["remote-carol-1"] == "caz"
    assert tokens["seat-erin-1"] == "erin"


async def test_here_is_never_a_dm_mention(env):
    await env.user_svc.provision(username="anna", display_name="Anna")
    bob = await env.user_svc.provision(username="bob", display_name="Bob")
    await _group(env, "g2", "anna", "bob")
    out = await env.resolver.resolve("g2", "@here @bob")
    assert [(m.type, m.user_id) for m in out] == [(MentionType.USER, bob.user_id)]


async def test_member_who_left_the_group_does_not_resolve(env):
    await env.user_svc.provision(username="anna", display_name="Anna")
    await env.user_svc.provision(username="bob", display_name="Bob")
    await _group(env, "g3", "anna", "bob")
    await env.convos.soft_leave("g3", "bob")
    assert await env.resolver.resolve("g3", "@bob") == ()


async def test_added_returns_only_newly_mentioned(env):
    await env.user_svc.provision(username="anna", display_name="Anna")
    bob = await env.user_svc.provision(username="bob", display_name="Bob")
    carl = await env.user_svc.provision(username="carl", display_name="Carl")
    await _group(env, "g4", "anna", "bob", "carl")
    out = await env.resolver.added("g4", "hi @bob", "hi @bob and @carl")
    assert [m.user_id for m in out] == [carl.user_id]
    assert await env.resolver.added("g4", "hi @bob", "hi @Bob!") == ()
    assert bob.user_id


async def test_empty_or_at_free_content_short_circuits(env):
    assert await env.resolver.resolve("nope", "") == ()
    assert await env.resolver.resolve("nope", "no mentions here") == ()
    assert await env.resolver.added("nope", "@x", "plain") == ()


async def test_remote_seat_without_any_name_is_skipped(env):
    """A 1:1 seat with no ``remote_users`` row and no seat user_id carries no
    identity — it can't be mentioned."""
    await env.user_svc.provision(username="anna", display_name="Anna")
    await _group(env, "d1", "anna", type=ConversationType.DM)
    await env.convos.add_remote_member(
        RemoteConversationMember(
            conversation_id="d1",
            instance_id="peer-b",
            remote_username="ghost",
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    assert await env.resolver.resolve("d1", "@ghost") == ()


class _Policy:
    """A system-chat policy stub: a computed roster on other households."""

    def __init__(self, *seats: RemoteConversationMember) -> None:
        self.seats = list(seats)

    async def remote_seats(self, conv) -> list[RemoteConversationMember]:
        return self.seats


async def _system_chat(env, conv_id, *usernames):
    await env.convos.create(
        Conversation(
            id=conv_id,
            type=ConversationType.GROUP_DM,
            created_at=datetime.now(timezone.utc),
            system_scope=SystemChatScope.HOUSEHOLD,
        )
    )
    for u in usernames:
        await env.convos.add_member(
            ConversationMember(
                conversation_id=conv_id,
                username=u,
                joined_at=datetime.now(timezone.utc).isoformat(),
            )
        )


def _computed(user_id: str, remote_username: str = "") -> RemoteConversationMember:
    return RemoteConversationMember(
        conversation_id="sys",
        instance_id="peer-b",
        remote_username=remote_username,
        joined_at="",
        user_id=user_id,
        display_name=user_id,
    )


async def test_system_chat_resolves_the_policys_remote_roster(env):
    """A space chat keeps no remote seat rows: the policy's computed roster
    (looked up by ``user_id`` when it names no login) makes members on
    other households mentionable."""
    anna = await env.user_svc.provision(username="anna", display_name="Anna")
    await _system_chat(env, "sys", "anna")
    await env.users.upsert_remote(
        RemoteUser(
            user_id="remote-carol-1",
            instance_id="peer-b",
            remote_username="carol",
            display_name="Carol",
            handle="caz",
        )
    )
    # A stored remote seat is ignored for a system chat.
    await env.convos.add_remote_member(
        RemoteConversationMember(
            conversation_id="sys",
            instance_id="peer-b",
            remote_username="erin",
            joined_at=datetime.now(timezone.utc).isoformat(),
            user_id="seat-erin-1",
        )
    )
    policy = _Policy(
        _computed("remote-carol-1"),
        # Unknown here and no login on the roster: not mentionable.
        _computed("remote-nameless"),
        # Unknown here, but the roster named a login.
        _computed("remote-frank", remote_username="frank"),
    )
    resolver = DmMentionResolver(env.convos, env.users, policy)  # type: ignore[arg-type]
    out = await resolver.resolve("sys", "@anna @caz @frank @erin")
    assert [m.user_id for m in out] == [anna.user_id, "remote-carol-1", "remote-frank"]
    tokens = await resolver.tokens("sys")
    assert tokens["remote-carol-1"] == "caz"
    assert "remote-nameless" not in tokens


async def test_system_chat_without_policy_has_no_remote_seats(env):
    await env.user_svc.provision(username="anna", display_name="Anna")
    await _system_chat(env, "sys", "anna")
    conv = await env.convos.get("sys")
    assert conv is not None
    assert await env.resolver.remote_seats(conv) == []


async def test_a_seat_without_a_login_is_looked_up_by_user_id(env):
    """A stored (person-made group) seat that names no ``remote_username``
    falls back to ``remote_users`` by ``user_id`` — the person's handle
    resolves and becomes their composer token."""
    await env.user_svc.provision(username="anna", display_name="Anna")
    await _group(env, "g9", "anna")
    await env.users.upsert_remote(
        RemoteUser(
            user_id="remote-gus-1",
            instance_id="peer-b",
            remote_username="gus",
            display_name="Gus",
            handle="gussy",
        )
    )
    await env.convos.add_remote_member(
        RemoteConversationMember(
            conversation_id="g9",
            instance_id="peer-b",
            remote_username="",
            joined_at=datetime.now(timezone.utc).isoformat(),
            user_id="remote-gus-1",
        )
    )
    out = await env.resolver.resolve("g9", "hey @gussy")
    assert [m.user_id for m in out] == ["remote-gus-1"]
    assert (await env.resolver.tokens("g9"))["remote-gus-1"] == "gussy"
