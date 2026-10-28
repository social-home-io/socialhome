"""§Privacy: a personal block holds where the app already promised it.

A personal block (``POST /api/blocks``) is the blocker's own: it never
leaves their household, so every rule is enforced here. These close the
gaps between what the block promises (``docs/api.md`` → *Personal user
blocks*, the block dialog's copy) and what each path did. Against the real
application (registry, SQLite, routes):

* **Cross-household 1:1 DMs** — a ``DM_MESSAGE`` from a remote person the
  local party blocked is not stored and opens no conversation; the blocker
  can't send into an earlier 1:1 with them, and that 1:1 drops out of the
  blocker's list — as it already did for a local peer.
* **1:1 calls** follow the 1:1 DM rule, both ways, local and inbound.
* **Notifications** — household posts, comments and follows by someone the
  recipient blocked never notify them.

Everyone else is unaffected.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    notification_delivery_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.domain.federation import FederationEvent, FederationEventType

pytestmark = pytest.mark.security

FET = FederationEventType
PEER = "peer-rex"
REX = "u-rex"
LOCALS = ("ann", "bob", "carol")


def _h(name: str) -> dict:
    return {"Authorization": f"Bearer {name}-tok"}


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "pb.db"),
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
            " remote_username, joined_at) VALUES(?,?,?,datetime('now'))",
            (conv_id, PEER, "rex"),
        )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    """ann · bob · carol here, rex on a paired household. Seeded before ann
    blocks bob and rex: 1:1s ann↔bob, ann↔rex and carol↔rex."""
    app = create_app(_config(tmp_dir))
    tc = await aiohttp_client(app)
    db = app[db_key]

    async def _no_wire(self, **_kw):
        return None

    monkeypatch.setattr(type(app[federation_service_key]), "send_event", _no_wire)
    for name in LOCALS:
        uid = f"u-{name}"
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (name, uid, name.title()),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
            " VALUES(?,?,?,?)",
            (f"t-{name}", uid, "web", sha256_token_hash(f"{name}-tok")),
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
    for conv_id in ("c-ab", "c-ar", "c-cr"):
        await db.enqueue(
            "INSERT INTO conversations(id, type) VALUES(?, 'dm')", (conv_id,)
        )
    await _seat(db, "c-ab", locals_=("ann", "bob"), rex=False)
    await _seat(db, "c-ar", locals_=("ann",), rex=True)
    await _seat(db, "c-cr", locals_=("carol",), rex=True)
    for blocked in ("u-bob", REX):
        r = await tc.post("/api/blocks", json={"user_id": blocked}, headers=_h("ann"))
        assert r.status in (200, 201, 204), await r.text()
    tc._app = app
    tc._db = db
    return tc


async def _dispatch(app, event_type, payload) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id=f"env-{payload.get('message_id') or payload.get('call_id')}",
                event_type=event_type,
                from_instance=PEER,
                to_instance="self",
                timestamp=datetime.now(UTC).isoformat(),
                payload=payload,
            )
        )


def _dm(conv_id: str, message_id: str, to: str) -> dict:
    return {
        "conversation_id": conv_id,
        "message_id": message_id,
        "sender_user_id": REX,
        "sender_display_name": "Rex",
        "type": "text",
        "content": "hi",
        "occurred_at": datetime.now(UTC).isoformat(),
        "recipient_user_ids": [to],
    }


async def _messages(db, conv_id: str) -> list[str]:
    rows = await db.fetchall(
        "SELECT id FROM conversation_messages WHERE conversation_id=?", (conv_id,)
    )
    return [r["id"] for r in rows]


# ── Cross-household 1:1 DMs ──────────────────────────────────────────────


async def test_inbound_dm_from_a_blocked_remote_sender_is_not_stored(env):
    await _dispatch(env._app, FET.DM_MESSAGE, _dm("c-ar", "m-old", "u-ann"))
    await _dispatch(env._app, FET.DM_MESSAGE, _dm("c-new", "m-new", "u-ann"))
    assert await _messages(env._db, "c-ar") == []
    assert (
        await env._db.fetchone("SELECT 1 FROM conversations WHERE id='c-new'") is None
    )
    assert (
        await env._db.fetchone("SELECT 1 FROM notifications WHERE user_id='u-ann'")
        is None
    )


async def test_media_bytes_for_a_refused_dm_are_not_stored(env, tmp_dir):
    """The bytes after a refused message — or racing ahead of it — have no
    home: the 1:1 is here, and its local party blocked the sender."""
    await _dispatch(
        env._app,
        FET.DM_MEDIA_BLOB,
        {
            "media_blob_id": "m-pic",
            "message_id": "m-pic",
            "conversation_id": "c-ar",
            "mime_type": "image/png",
            "bytes_b64": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode(),
            "chunk_index": 0,
            "chunk_count": 1,
            "final": True,
        },
    )
    media = tmp_dir / "media"
    assert not media.exists() or not list(media.rglob("m-pic*"))


async def test_inbound_dm_to_someone_who_did_not_block_is_stored(env):
    await _dispatch(env._app, FET.DM_MESSAGE, _dm("c-cr", "m-carol", "u-carol"))
    assert await _messages(env._db, "c-cr") == ["m-carol"]


async def test_blocker_cannot_send_into_an_earlier_remote_dm(env):
    r = await env.post(
        "/api/conversations/c-ar/messages",
        json={"content": "hello"},
        headers=_h("ann"),
    )
    assert r.status == 403, await r.text()
    assert await _messages(env._db, "c-ar") == []


async def test_earlier_remote_dm_drops_out_of_the_blockers_list(env):
    r = await env.get("/api/conversations", headers=_h("ann"))
    assert r.status == 200
    assert {c["id"] for c in await r.json()}.isdisjoint({"c-ar", "c-ab"})
    r = await env.get("/api/conversations", headers=_h("carol"))
    assert "c-cr" in {c["id"] for c in await r.json()}


def _history(conv_id: str, message_id: str) -> dict:
    return {
        "conversation_id": conv_id,
        "chunk_index": 0,
        "messages": [
            {
                "id": message_id,
                "sender_user_id": REX,
                "content": "earlier",
                "type": "text",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    }


async def test_history_sync_never_puts_back_a_refused_dm(env):
    await _dispatch(env._app, FET.DM_HISTORY_CHUNK, _history("c-ar", "m-hist"))
    assert await _messages(env._db, "c-ar") == []
    await _dispatch(env._app, FET.DM_HISTORY_CHUNK, _history("c-cr", "m-ok"))
    assert await _messages(env._db, "c-cr") == ["m-ok"]


async def test_inbound_reaction_in_a_blocked_one_to_one_is_not_stored(env):
    await env._db.enqueue(
        "INSERT INTO conversation_messages(id, conversation_id, sender_user_id,"
        " content) VALUES('m-ann', 'c-ar', 'u-ann', 'earlier')"
    )
    await _dispatch(
        env._app,
        FET.DM_MESSAGE_REACTION,
        {
            "conversation_id": "c-ar",
            "message_id": "m-ann",
            "user_id": REX,
            "emoji": "👍",
            "action": "add",
        },
    )
    assert (
        await env._db.fetchone(
            "SELECT 1 FROM message_reactions WHERE message_id='m-ann'"
        )
        is None
    )


@pytest.mark.parametrize("hider", ["ann", "bob"])
async def test_a_hidden_one_to_one_still_refuses_both_sides(env, hider):
    """Hiding a 1:1 (``deleted_at`` on a 1:1 seat) doesn't stop delivery to
    that member, so it can't switch the block gate off either."""
    await env._db.enqueue(
        "UPDATE conversation_members SET deleted_at=datetime('now')"
        " WHERE conversation_id='c-ab' AND username=?",
        (hider,),
    )
    for actor in ("ann", "bob"):
        r = await env.post(
            "/api/conversations/c-ab/messages",
            json={"content": "hello"},
            headers=_h(actor),
        )
        assert r.status in (403, 404), (actor, r.status, await r.text())
    assert await _messages(env._db, "c-ab") == []


async def test_groups_ignore_personal_blocks(env):
    """A group with the blocked person still works — for messages from a
    remote member and for calls."""
    db = env._db
    await db.enqueue("INSERT INTO conversations(id, type) VALUES('g-abr', 'group_dm')")
    await _seat(db, "g-abr", locals_=("ann", "bob"), rex=True)
    msg = _dm("g-abr", "m-grp", "u-ann")
    await _dispatch(env._app, FET.DM_MESSAGE, msg)
    assert await _messages(db, "g-abr") == ["m-grp"]
    # bob calls a two-seat group with ann (who blocked him): one callee,
    # but a group — personal blocks don't apply.
    await db.enqueue("INSERT INTO conversations(id, type) VALUES('g-ab', 'group_dm')")
    await _seat(db, "g-ab", locals_=("ann", "bob"), rex=False)
    r = await env.post(
        "/api/calls",
        json={"conversation_id": "g-ab", "sdp_offer": "v=0", "call_type": "audio"},
        headers=_h("bob"),
    )
    assert r.status in (200, 201), await r.text()


# ── 1:1 calls ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("actor", ["ann", "bob"])
async def test_no_one_to_one_call_across_a_personal_block(env, actor):
    r = await env.post(
        "/api/calls",
        json={"conversation_id": "c-ab", "sdp_offer": "v=0", "call_type": "audio"},
        headers=_h(actor),
    )
    assert r.status == 403, await r.text()


async def test_inbound_call_from_a_blocked_remote_caller_never_rings(env):
    await _dispatch(
        env._app,
        FET.CALL_OFFER,
        {
            "call_id": "call-rex",
            "from_user": REX,
            "to_user": "u-ann",
            "conversation_id": "c-ar",
            "call_type": "audio",
            "sdp": "v=0",
        },
    )
    assert (
        await env._db.fetchone("SELECT 1 FROM call_sessions WHERE id='call-rex'")
        is None
    )


# ── Notifications ────────────────────────────────────────────────────────


async def _notified(env, **where) -> set[str]:
    # The rows are written by the delivery worker, after the request returns:
    # wait for it, or a "never notified" check would pass on an empty table.
    await env.app[notification_delivery_key].drain()
    col, val = next(iter(where.items()))
    rows = await env._db.fetchall(
        f"SELECT user_id FROM notifications WHERE {col}=?", (val,)
    )
    return {r["user_id"] for r in rows}


async def test_household_post_and_comment_by_a_blocked_author_never_notify(env):
    r = await env.post(
        "/api/feed/posts",
        json={"type": "text", "content": "hello all"},
        headers=_h("bob"),
    )
    assert r.status == 201, await r.text()
    post_id = (await r.json())["id"]
    notified = await _notified(env, type="post_created")
    assert "u-carol" in notified and "u-ann" not in notified
    r = await env.post(
        f"/api/feed/posts/{post_id}/comments",
        json={"content": "me too"},
        headers=_h("bob"),
    )
    assert r.status == 201, await r.text()
    notified = await _notified(env, type="comment_added")
    assert "u-carol" in notified and "u-ann" not in notified


async def test_a_blocked_persons_follow_never_notifies(env):
    r = await env.post(
        "/api/moments/follows", json={"user_id": "u-ann"}, headers=_h("bob")
    )
    assert r.status == 201, await r.text()
    assert "u-ann" not in await _notified(env, type="user_followed")
    r = await env.post(
        "/api/moments/follows", json={"user_id": "u-ann"}, headers=_h("carol")
    )
    assert r.status == 201, await r.text()
    assert "u-ann" in await _notified(env, type="user_followed")
