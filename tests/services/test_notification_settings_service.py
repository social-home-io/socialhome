"""Tests for socialhome.services.notification_settings_service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.conversation import (
    MUTED_FOREVER,
    Conversation,
    ConversationMember,
    ConversationType,
    RemoteConversationMember,
    SystemChatScope,
)
from socialhome.domain.notification_kinds import (
    NotifDelivery,
    NotifKind,
    SpaceNotifPref,
)
from socialhome.domain.post import Post, PostType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.post_repo import SqlitePostRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.notification_settings_service import (
    NotificationSettingsService,
    space_choice_json,
)
from socialhome.services.user_service import UserService

_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
async def s(tmp_dir):
    kp = generate_identity_keypair()
    db = AsyncDatabase(tmp_dir / "settings.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (
            derive_instance_id(kp.public_key),
            kp.private_key.hex(),
            kp.public_key.hex(),
            "aa" * 32,
        ),
    )
    users = SqliteUserRepo(db)
    user_svc = UserService(users, EventBus(), own_instance_public_key=kp.public_key)
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))
    notifs = SqliteNotificationRepo(db)
    convos = SqliteConversationRepo(db)
    posts, space_posts = SqlitePostRepo(db), SqliteSpacePostRepo(db)
    svc = NotificationSettingsService(
        users=users,
        user_service=user_svc,
        notifs=notifs,
        spaces=spaces,
        convos=convos,
        posts=posts,
        space_posts=space_posts,
    )

    class S:
        pass

    st = S()
    st.db, st.svc, st.users, st.user_svc = db, svc, users, user_svc
    st.notifs, st.convos, st.posts, st.space_posts = notifs, convos, posts, space_posts
    st.anna = await user_svc.provision(username="anna", display_name="Anna")
    st.bob = await user_svc.provision(username="bob", display_name="Bob")
    yield st
    await db.shutdown()


async def _space(s, sid: str, space_type: str = "household", role: str = "member"):
    await s.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type) VALUES(?, ?, 'i', 'anna', ?, ?)",
        (sid, sid.title(), "ab" * 32, space_type),
    )
    await s.db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        (sid, s.anna.user_id, role),
    )


async def _seat(s, conv: Conversation, *usernames: str) -> None:
    await s.convos.create(conv)
    for name in usernames:
        await s.convos.add_member(
            ConversationMember(
                conversation_id=conv.id, username=name, joined_at=_NOW.isoformat()
            )
        )


def _conv(cid, ctype=ConversationType.DM, **kw) -> Conversation:
    return Conversation(id=cid, type=ctype, created_at=_NOW, **kw)


async def _me(s):
    return await s.svc.get_user("anna")


# ── Kinds ─────────────────────────────────────────────────────────────


async def test_set_kind_keeps_the_other_choices(s):
    await s.svc.set_kind("anna", "reactions", "push")
    got = await s.svc.set_kind("anna", "plans", "in_app")
    assert got.delivery(NotifKind.REACTIONS) is NotifDelivery.PUSH
    assert got.delivery(NotifKind.PLANS) is NotifDelivery.IN_APP


@pytest.mark.parametrize(
    ("kind", "choice"),
    [("needs_you", "off"), ("messages", "in_app"), ("x", "push"), ("plans", "x")],
)
async def test_set_kind_refuses_what_does_not_exist(s, kind, choice):
    with pytest.raises(ValueError):
        await s.svc.set_kind("anna", kind, choice)


async def test_get_user_unknown_is_a_key_error(s):
    with pytest.raises(KeyError):
        await s.svc.get_user("nobody")


# ── Spaces (D9) ───────────────────────────────────────────────────────


async def test_space_level_needs_a_seat(s):
    await _space(s, "fam")
    with pytest.raises(PermissionError):
        await s.svc.space_choice(s.bob.user_id, "fam")


async def test_muting_twice_still_comes_back_to_the_default(s):
    """A space that was muted forever (an old ``muted`` row) muted again for
    an hour comes back to the default, never to "muted"."""
    await _space(s, "fam")
    await s.notifs.set_space_notif_level(
        user_id=s.anna.user_id, space_id="fam", level="muted"
    )
    got = await s.svc.set_space_level(s.anna.user_id, "fam", "muted", "1h")
    assert (got["level"], got["then_level"]) == ("muted", "all")


def test_space_choice_json_shapes():
    assert space_choice_json(None, "all", _NOW) == {
        "level": "all",
        "is_default": True,
        "default_level": "all",
        "muted_until": None,
        "then_level": None,
    }
    old = SpaceNotifPref("muted")
    assert space_choice_json(old, "mentions", _NOW)["level"] == "muted"
    assert space_choice_json(old, "mentions", _NOW)["muted_until"] is None


# ── Muted and changed ─────────────────────────────────────────────────


async def test_changes_list_spaces_chats_and_posts(s):
    await _space(s, "fam")  # household: Everything by default
    await _space(s, "town", "public")  # public: Replies and mentions
    await _space(s, "same", "public")
    await s.notifs.set_space_notif_level(
        user_id=s.anna.user_id, space_id="fam", level="mentions"
    )
    await s.notifs.set_space_notif_level(
        user_id=s.anna.user_id, space_id="same", level="mentions"
    )  # chosen, but the default: not listed
    soon = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await s.notifs.set_space_notif_level(
        user_id=s.anna.user_id, space_id="town", level="mentions", muted_until=soon
    )
    # A muted 1:1 with Bob, a group at mentions, a quiet household chat,
    # a space chat at "all" (its default is mentions), a plain 1:1.
    await _seat(s, _conv("dm1"), "anna", "bob")
    await s.convos.set_muted_until("dm1", "anna", MUTED_FOREVER)
    await _seat(s, _conv("g1", ConversationType.GROUP_DM, name="Weekend"), "anna")
    await s.convos.set_notif_level("g1", "anna", "mentions")
    await _seat(
        s,
        _conv(
            "hh",
            ConversationType.GROUP_DM,
            system_scope=SystemChatScope.HOUSEHOLD,
        ),
        "anna",
    )
    await _seat(
        s,
        _conv(
            "sc",
            ConversationType.GROUP_DM,
            system_scope=SystemChatScope.SPACE,
            space_id="town",
        ),
        "anna",
    )
    await _seat(s, _conv("plain"), "anna", "bob")
    await s.posts.save(
        Post(
            id="p1",
            author=s.bob.user_id,
            type=PostType.TEXT,
            content="Flohmarkt   am\nSamstag",
            created_at=_NOW,
        )
    )
    await s.svc.mute_post(await _me(s), "post:p1")

    got = await s.svc.changes(await _me(s))
    lines = [(c.target, c.id, c.level, c.place, c.name) for c in got]
    assert lines == [
        ("space", "fam", "mentions", "household", "Fam"),
        ("space", "town", "muted", "public", "Town"),
        ("chat", "dm1", "muted", "dm", "Bob"),
        ("chat", "g1", "mentions", "group", "Weekend"),
        ("chat", "sc", "all", "space", "Town"),
        ("post", "post:p1", "muted", "post", None),
    ]
    assert got[1].then_level == "mentions"
    assert got[2].muted_until == MUTED_FOREVER
    assert got[2].then_level == "all"
    assert got[4].space_name == "Town"
    assert got[5].snippet == "Flohmarkt am Samstag"


async def test_a_1to1_with_a_remote_person_is_named(s):
    await s.convos.create(_conv("dm-r"))
    await s.convos.add_member(
        ConversationMember(
            conversation_id="dm-r", username="anna", joined_at=_NOW.isoformat()
        )
    )
    await s.convos.add_remote_member(
        RemoteConversationMember(
            conversation_id="dm-r",
            instance_id="inst-b",
            remote_username="zoe",
            joined_at=_NOW.isoformat(),
            display_name="Zoë",
        )
    )
    await s.convos.set_muted_until("dm-r", "anna", MUTED_FOREVER)
    got = await s.svc.changes(await _me(s))
    assert [(c.id, c.name) for c in got] == [("dm-r", "Zoë")]


async def test_a_post_that_is_gone_is_not_listed(s):
    await s.user_svc.patch_preferences(
        "anna", {"notif_muted_posts": {"post:gone": datetime.now(UTC).isoformat()}}
    )
    assert await s.svc.changes(await _me(s)) == []


async def test_a_space_post_shows_its_space(s):
    await _space(s, "fam")
    await s.db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES('sp1', 'fam', ?, 'text', 'Grill on Sunday')",
        (s.bob.user_id,),
    )
    await s.svc.mute_post(await _me(s), "space_post:sp1")
    got = await s.svc.changes(await _me(s))
    assert [(c.id, c.space_id, c.space_name, c.snippet) for c in got] == [
        ("space_post:sp1", "fam", "Fam", "Grill on Sunday")
    ]
    # Left the space: the line (and the post's words) are gone.
    await s.db.enqueue("DELETE FROM space_members WHERE space_id='fam'")
    assert await s.svc.changes(await _me(s)) == []


async def test_unmute_post(s):
    await s.posts.save(
        Post(
            id="p1",
            author=s.bob.user_id,
            type=PostType.TEXT,
            content="x",
            created_at=_NOW,
        )
    )
    await s.posts.save(
        Post(
            id="p2",
            author=s.bob.user_id,
            type=PostType.TEXT,
            content="y",
            created_at=_NOW,
        )
    )
    me = await _me(s)
    await s.svc.mute_post(me, "post:p1")
    await s.svc.mute_post(await _me(s), "post:p2")
    await s.svc.unmute_post("anna", "post:p1")
    assert list((await s.svc.settings("anna")).muted_posts) == ["post:p2"]
    await s.svc.unmute_post("anna", "post:p2")
    await s.svc.unmute_post("anna", "post:never")
    assert (await s.svc.settings("anna")).muted_posts == {}
    assert "notif_muted_posts" not in (await _me(s)).preferences_json
