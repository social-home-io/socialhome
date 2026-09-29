"""§CP.F2: a guardian block holds on every path a message can take.

A guardian blocks someone for a protected account. From then on the two
can't reach each other — in either direction, local or cross-household,
live or by history sync, as text or media, in a 1:1 or a group, by call,
mention or notification. Against the real application (registry, SQLite,
routes):

* **Local** — neither opens or continues a 1:1, nor shares a group, nor
  calls; an existing 1:1 drops out of both lists.
* **Inbound** — a ``DM_MESSAGE`` / ``DM_HISTORY_CHUNK`` from a blocked
  remote sender to the protected account is not stored (fail closed), nor
  the ``DM_MEDIA_BLOB`` bytes that follow; in a group with others it is
  stored for them and hidden from the protected account.
* **Calls** — an inbound ``CALL_OFFER`` from a blocked caller never rings.
* **Notifications** — a blocked author's space post never notifies.
* The refusal wording is the same for both sides and never says why.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    child_protection_service_key,
    db_key,
    federation_inbound_service_key,
    federation_service_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.domain.federation import FederationEvent, FederationEventType

pytestmark = pytest.mark.security

FET = FederationEventType
PEER = "peer-rex"
REX = "u-rex"
LOCALS = ("admin", "kid", "bob", "carol")


def _h(name: str) -> dict:
    return {"Authorization": f"Bearer {name}-tok"}


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "gb.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seat(db, conv_id: str, *, locals_: tuple[str, ...], rex: bool) -> None:
    for name in locals_:
        await db.enqueue(
            "INSERT INTO conversation_members(conversation_id, username, joined_at)"
            " VALUES(?,?,datetime('now'))",
            (conv_id, name),
        )
    if rex:
        await db.enqueue(
            "INSERT INTO conversation_remote_members(conversation_id, instance_id,"
            " remote_username, joined_at, user_id) VALUES(?,?,?,datetime('now'),?)",
            (conv_id, PEER, "rex", REX),
        )


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    """admin (guardian) · kid (protected) · bob, carol (adults) · rex on a
    directly paired household. bob and rex are blocked for kid. Seeded
    before the block: a 1:1 kid↔bob, a 1:1 kid↔rex and a group
    kid+carol+rex hosted elsewhere."""
    app = create_app(_config(tmp_dir))
    tc = await aiohttp_client(app)
    db = app[db_key]
    ids = {name: f"u-{name}" for name in LOCALS}
    for name, uid in ids.items():
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin)"
            " VALUES(?,?,?,?)",
            (name, uid, name.title(), 1 if name == "admin" else 0),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
            " VALUES(?,?,?,?)",
            (f"t-{name}", uid, "web", sha256_token_hash(f"{name}-tok")),
        )
        # A password sign-in (survives protection, unlike a personal token).
        await db.enqueue(
            "INSERT INTO platform_users(username, display_name) VALUES(?, ?)",
            (name, name.title()),
        )
        await db.enqueue(
            "INSERT INTO platform_tokens(token_id, username, token_hash) VALUES(?,?,?)",
            (f"t-{name}", name, sha256_token_hash(f"{name}-tok")),
        )
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            PEER,
            PEER,
            "00" * 32,
            "k1",
            "k2",
            "https://rex/wh",
            "wh-rex",
            "confirmed",
            "manual",
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (REX, PEER, "rex", "Rex"),
    )
    for conv_id, ctype in (("c-kb", "dm"), ("c-kr", "dm"), ("g-krc", "group_dm")):
        await db.enqueue(
            "INSERT INTO conversations(id, type) VALUES(?, ?)", (conv_id, ctype)
        )
    await _seat(db, "c-kb", locals_=("kid", "bob"), rex=False)
    await _seat(db, "c-kr", locals_=("kid",), rex=True)
    await _seat(db, "g-krc", locals_=("kid", "carol"), rex=True)

    cp = app[child_protection_service_key]
    await cp.enable_protection(
        minor_username="kid", declared_age=12, actor_user_id=ids["admin"]
    )
    await cp.add_guardian(
        minor_user_id=ids["kid"],
        guardian_user_id=ids["admin"],
        actor_user_id=ids["admin"],
    )
    for blocked in (ids["bob"], REX):
        await cp.block_user_for_minor(
            minor_user_id=ids["kid"],
            blocked_user_id=blocked,
            guardian_user_id=ids["admin"],
        )
    tc._app = app
    tc._db = db
    tc._ids = ids
    return tc


async def _dispatch(app, event_type, payload, *, from_instance=PEER) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id=f"env-{payload.get('message_id') or payload.get('conversation_id')}",
                event_type=event_type,
                from_instance=from_instance,
                to_instance="self",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )


def _dm(conv_id: str, message_id: str, **extra) -> dict:
    return {
        "conversation_id": conv_id,
        "message_id": message_id,
        "sender_user_id": REX,
        "sender_display_name": "Rex",
        "type": "text",
        "content": "hi",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "recipient_user_ids": ["u-kid"],
        **extra,
    }


async def _messages(db, conv_id: str) -> list[str]:
    rows = await db.fetchall(
        "SELECT id FROM conversation_messages WHERE conversation_id=? ORDER BY id",
        (conv_id,),
    )
    return [r["id"] for r in rows]


async def _reseat(db, conv_id: str, username: str) -> None:
    """Seat *username* again — as a group's remote authority may, whatever
    this household did locally."""
    await db.enqueue(
        "UPDATE conversation_members SET deleted_at=NULL, left_version=NULL"
        " WHERE conversation_id=? AND username=?",
        (conv_id, username),
    )


async def _assert_blocked(resp) -> None:
    assert resp.status == 403, await resp.text()
    text = (await resp.text()).lower()
    # Same words for both sides; never why.
    assert "protect" not in text and "minor" not in text and "guardian" not in text


# ── Local DMs ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("actor", "body"),
    [
        ("bob", {"username": "kid"}),
        ("kid", {"username": "bob"}),
        ("kid", {"user_id": REX}),
    ],
)
async def test_nobody_opens_a_dm_across_a_guardian_block(env, actor, body):
    await _assert_blocked(
        await env.post("/api/conversations/dm", json=body, headers=_h(actor))
    )


async def test_an_earlier_dm_cannot_be_continued_from_either_side(env):
    for actor in ("kid", "bob"):
        await _assert_blocked(
            await env.post(
                "/api/conversations/c-kb/messages",
                json={"content": "hello"},
                headers=_h(actor),
            )
        )
    await _assert_blocked(
        await env.post(
            "/api/conversations/c-kr/messages",
            json={"content": "hello"},
            headers=_h("kid"),
        )
    )
    assert await _messages(env._db, "c-kb") == []
    assert await _messages(env._db, "c-kr") == []


async def test_an_earlier_dm_drops_out_of_both_lists(env):
    for actor, gone in (("kid", {"c-kb", "c-kr"}), ("bob", {"c-kb"})):
        r = await env.get("/api/conversations", headers=_h(actor))
        assert r.status == 200
        listed = {c["id"] for c in await r.json()}
        assert not listed & gone, (actor, listed)


async def test_other_people_are_unaffected(env):
    r = await env.post(
        "/api/conversations/dm", json={"username": "carol"}, headers=_h("bob")
    )
    assert r.status in (200, 201), await r.text()
    r = await env.post(
        "/api/conversations/dm", json={"username": "carol"}, headers=_h("kid")
    )
    assert r.status in (200, 201), await r.text()


# ── Groups ───────────────────────────────────────────────────────────────


async def test_no_group_holds_both(env):
    await _assert_blocked(
        await env.post(
            "/api/conversations/group",
            json={"members": ["kid", "carol"]},
            headers=_h("bob"),
        )
    )
    r = await env.post(
        "/api/conversations/group",
        json={"members": ["kid", "carol"]},
        headers=_h("admin"),
    )
    assert r.status == 201, await r.text()
    gid = (await r.json())["id"]
    await _assert_blocked(
        await env.post(
            f"/api/conversations/{gid}/members",
            json={"usernames": ["bob"]},
            headers=_h("admin"),
        )
    )


async def test_block_steps_the_account_out_of_a_shared_group(env):
    r = await env.post(
        "/api/conversations/group",
        json={"members": ["kid", "carol"]},
        headers=_h("admin"),
    )
    gid = (await r.json())["id"]
    await env._db.enqueue(
        "INSERT INTO conversation_members(conversation_id, username, joined_at)"
        " VALUES(?, 'bob', datetime('now'))",
        (gid,),
    )
    ids = env._ids
    await env._app[child_protection_service_key].block_user_for_minor(
        minor_user_id=ids["kid"],
        blocked_user_id=ids["carol"],
        guardian_user_id=ids["admin"],
    )
    row = await env._db.fetchone(
        "SELECT deleted_at FROM conversation_members"
        " WHERE conversation_id=? AND username='kid'",
        (gid,),
    )
    assert row["deleted_at"] is not None


async def test_the_block_stepped_the_account_out_of_the_remote_group(env):
    row = await env._db.fetchone(
        "SELECT deleted_at FROM conversation_members"
        " WHERE conversation_id='g-krc' AND username='kid'"
    )
    assert row["deleted_at"] is not None


async def test_account_cannot_post_into_a_group_seating_a_blocked_person(env):
    await _reseat(env._db, "g-krc", "kid")
    await _assert_blocked(
        await env.post(
            "/api/conversations/g-krc/messages",
            json={"content": "hi all"},
            headers=_h("kid"),
        )
    )
    r = await env.post(
        "/api/conversations/g-krc/messages",
        json={"content": "hi all"},
        headers=_h("carol"),
    )
    assert r.status == 201, await r.text()


# ── Inbound federation ───────────────────────────────────────────────────


async def test_inbound_dm_from_a_blocked_sender_is_not_stored(env):
    await _dispatch(env._app, FET.DM_MESSAGE, _dm("c-new", "m-new"))
    await _dispatch(env._app, FET.DM_MESSAGE, _dm("c-kr", "m-old"))
    assert (
        await env._db.fetchone("SELECT 1 FROM conversations WHERE id='c-new'") is None
    )
    assert await _messages(env._db, "c-kr") == []
    assert (
        await env._db.fetchone(
            "SELECT 1 FROM notifications WHERE user_id='u-kid' AND type='dm_message'"
        )
        is None
    )


async def test_media_bytes_after_a_refused_dm_are_not_stored(env, tmp_dir):
    await _dispatch(
        env._app,
        FET.DM_MESSAGE,
        _dm(
            "c-kr", "m-pic", type="image", media_blob_id="m-pic", mime_type="image/png"
        ),
    )
    await _dispatch(
        env._app,
        FET.DM_MEDIA_BLOB,
        {
            "media_blob_id": "m-pic",
            "message_id": "m-pic",
            "conversation_id": "c-kr",
            "mime_type": "image/png",
            "bytes_b64": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode(),
            "chunk_index": 0,
            "chunk_count": 1,
            "final": True,
        },
    )
    media = tmp_dir / "media"
    assert not media.exists() or not list(media.glob("m-pic*"))


async def test_group_message_reaches_the_others_but_not_the_account(env):
    await _reseat(env._db, "g-krc", "kid")
    await _dispatch(env._app, FET.DM_MESSAGE, _dm("g-krc", "m-g"))
    assert await _messages(env._db, "g-krc") == ["m-g"]
    r = await env.get("/api/conversations/g-krc/messages", headers=_h("carol"))
    assert [m["id"] for m in await r.json()] == ["m-g"]
    r = await env.get("/api/conversations/g-krc/messages", headers=_h("kid"))
    assert r.status == 200
    assert await r.json() == []
    notified = {
        r["user_id"]
        for r in await env._db.fetchall(
            "SELECT user_id FROM notifications WHERE type='dm_message'"
        )
    }
    assert "u-kid" not in notified


async def test_history_sync_from_a_blocked_sender_is_not_stored(env):
    await _dispatch(
        env._app,
        FET.DM_HISTORY_CHUNK,
        {
            "conversation_id": "c-kr",
            "chunk_index": 0,
            "messages": [
                {
                    "id": "m-hist",
                    "sender_user_id": REX,
                    "content": "earlier",
                    "type": "text",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        },
    )
    assert await _messages(env._db, "c-kr") == []


# ── Calls ────────────────────────────────────────────────────────────────


async def test_no_call_across_a_guardian_block(env):
    for actor in ("kid", "bob"):
        r = await env.post(
            "/api/calls",
            json={"conversation_id": "c-kb", "sdp_offer": "v=0", "call_type": "audio"},
            headers=_h(actor),
        )
        await _assert_blocked(r)


async def test_group_call_never_rings_across_a_guardian_block(env):
    db = env._db
    await db.enqueue("INSERT INTO conversations(id, type) VALUES('g-bkc', 'group_dm')")
    await _seat(db, "g-bkc", locals_=("bob", "kid", "carol"), rex=False)
    # bob calls the group: carol rings, kid doesn't.
    r = await env.post(
        "/api/calls",
        json={"conversation_id": "g-bkc", "sdp_offer": "v=0", "call_type": "audio"},
        headers=_h("bob"),
    )
    assert r.status in (200, 201), await r.text()
    call_id = (await r.json())["call_id"]
    row = await db.fetchone(
        "SELECT participant_user_ids FROM call_sessions WHERE id=?", (call_id,)
    )
    assert "u-carol" in row["participant_user_ids"]
    assert "u-kid" not in row["participant_user_ids"]
    # kid can neither join that call nor call the group.
    await _assert_blocked(
        await env.post(
            f"/api/calls/{call_id}/join",
            json={"sdp_offers": {"u-carol": "v=0"}},
            headers=_h("kid"),
        )
    )
    await _assert_blocked(
        await env.post(
            "/api/calls",
            json={"conversation_id": "g-bkc", "sdp_offer": "v=0", "call_type": "audio"},
            headers=_h("kid"),
        )
    )


async def test_inbound_call_offer_from_a_blocked_caller_never_rings(env):
    await _dispatch(
        env._app,
        FET.CALL_OFFER,
        {
            "call_id": "call-rex",
            "from_user": REX,
            "to_user": "u-kid",
            "conversation_id": "c-kr",
            "call_type": "audio",
            "sdp": "v=0",
        },
    )
    assert (
        await env._db.fetchone("SELECT 1 FROM call_sessions WHERE id='call-rex'")
        is None
    )


# ── Notifications ────────────────────────────────────────────────────────


async def test_space_post_from_a_blocked_author_never_notifies(env):
    r = await env.post("/api/spaces", json={"name": "Club"}, headers=_h("bob"))
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    # Joined after the block (the block itself drops shared spaces).
    for name in ("kid", "carol"):
        await env._db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
            (sid, env._ids[name]),
        )
    r = await env.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "hello @kid and @carol"},
        headers=_h("bob"),
    )
    assert r.status == 201, await r.text()
    notified = {
        row["user_id"]
        for row in await env._db.fetchall(
            "SELECT user_id FROM notifications WHERE link_url=?", (f"/spaces/{sid}",)
        )
    }
    assert "u-carol" in notified
    assert "u-kid" not in notified


# ── Wording + reactions ──────────────────────────────────────────────────


async def test_the_blocked_person_sees_a_personal_blocks_words(env):
    """Nothing tells the blocked person a guardian (or a protected account)
    is involved; the protected account learns it can't message them."""
    r = await env.post(
        "/api/conversations/dm", json={"username": "kid"}, headers=_h("bob")
    )
    assert r.status == 403
    assert (await r.json())["error"]["detail"] == "Recipient has you blocked."
    r = await env.post(
        "/api/conversations/dm", json={"username": "bob"}, headers=_h("kid")
    )
    assert r.status == 403
    assert (await r.json())["error"]["detail"] == "You can't message this person."


async def test_inbound_reaction_from_a_blocked_sender_is_not_stored(env):
    await env._db.enqueue(
        "INSERT INTO conversation_messages(id, conversation_id, sender_user_id,"
        " content) VALUES('m-kid', 'c-kr', 'u-kid', 'earlier')"
    )
    await _dispatch(
        env._app,
        FET.DM_MESSAGE_REACTION,
        {
            "conversation_id": "c-kr",
            "message_id": "m-kid",
            "user_id": REX,
            "emoji": "👍",
            "action": "add",
        },
    )
    assert (
        await env._db.fetchone(
            "SELECT 1 FROM message_reactions WHERE message_id='m-kid'"
        )
        is None
    )


# ── Follow-ups: unread badges, contact requests, blobs after a restart ──


async def test_withheld_group_message_is_not_counted_unread(env):
    await _reseat(env._db, "g-krc", "kid")
    await env._db.enqueue(
        "UPDATE conversation_members SET last_read_at='2000-01-01T00:00:00'"
        " WHERE conversation_id='g-krc'"
    )
    await _dispatch(env._app, FET.DM_MESSAGE, _dm("g-krc", "m-unread"))
    for actor, expected in (("kid", 0), ("carol", 1)):
        r = await env.get("/api/conversations/g-krc/unread", headers=_h(actor))
        assert r.status == 200, await r.text()
        assert (await r.json())["unread"] == expected, actor
    r = await env.get("/api/conversations", headers=_h("kid"))
    rows = {c["id"]: c for c in await r.json()}
    assert rows["g-krc"]["unread"] == 0


async def test_contact_request_from_a_blocked_user_is_not_stored(env):
    await _dispatch(
        env._app,
        FET.DM_CONTACT_REQUEST,
        {
            "requester_user_id": REX,
            "requester_display_name": "Rex",
            "recipient_user_id": "u-kid",
        },
    )
    assert await env._db.fetchone("SELECT 1 FROM dm_contact_requests") is None
    assert (
        await env._db.fetchone(
            "SELECT 1 FROM notifications WHERE user_id='u-kid'"
            " AND type LIKE 'dm_contact%'"
        )
        is None
    )


def _forget_in_memory_state(app) -> None:
    """What a restart loses: anything the inbound service kept in memory."""
    inbound = app[federation_inbound_service_key]
    for attr in ("_guardian_refused_dms",):
        state = getattr(inbound, attr, None)
        if state is not None:
            state.clear()


def _blob(conv_id: str, message_id: str) -> dict:
    return {
        "media_blob_id": message_id,
        "message_id": message_id,
        "conversation_id": conv_id,
        "mime_type": "image/png",
        "bytes_b64": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode(),
        "chunk_index": 0,
        "chunk_count": 1,
        "final": True,
    }


@pytest.mark.parametrize("conv_id", ["c-kr", "c-brand-new"])
async def test_media_after_a_refused_dm_is_refused_across_a_restart(
    env, tmp_dir, conv_id
):
    msg = _dm(
        conv_id,
        f"m-{conv_id}",
        type="image",
        media_blob_id=f"m-{conv_id}",
        mime_type="image/png",
    )
    await _dispatch(env._app, FET.DM_MESSAGE, msg)
    _forget_in_memory_state(env._app)
    await _dispatch(env._app, FET.DM_MEDIA_BLOB, _blob(conv_id, f"m-{conv_id}"))
    media = tmp_dir / "media"
    assert not media.exists() or not list(media.glob(f"m-{conv_id}*"))
