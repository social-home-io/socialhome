"""Release-blocker protocol tests: a DM write binds to the household that signed it.

Marked ``@pytest.mark.security``.

Direct-message events carry a ``conversation_id``, a ``message_id`` and
the person they speak for. The rule these tests encode, against the real
application registry and SQLite:

    A household writes only into a conversation it holds a seat in, only
    for its own seated user, and only to messages its own user sent. Never
    as a local member, never as another household's user, never into a
    conversation it is not part of, never over another person's message.
    A first message may open a new conversation only for a local
    recipient, and never by reusing an existing message id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.federation_service import FederationService

pytestmark = pytest.mark.security

FET = FederationEventType

PEER = "peer-bob"  # household in conversation c-bob
OTHER = "peer-dora"  # household in conversation c-dora


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "dm.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_peer(db, instance_id: str, user_id: str, username: str) -> None:
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            instance_id,
            "00" * 32,
            "k1",
            "k2",
            f"https://{instance_id}/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (user_id, instance_id, username, username),
    )


async def _seed_conversation(db, conv_id, *, local, remote=()) -> None:
    await db.enqueue("INSERT INTO conversations(id, type) VALUES(?, 'dm')", (conv_id,))
    for username in local:
        await db.enqueue(
            "INSERT INTO conversation_members(conversation_id, username) VALUES(?,?)",
            (conv_id, username),
        )
    for instance_id, username in remote:
        await db.enqueue(
            "INSERT INTO conversation_remote_members(conversation_id, instance_id,"
            " remote_username) VALUES(?,?,?)",
            (conv_id, instance_id, username),
        )


async def _seed_message(db, msg_id, conv_id, sender, content) -> None:
    await db.enqueue(
        "INSERT INTO conversation_messages(id, conversation_id, sender_user_id,"
        " content, created_at) VALUES(?,?,?,?,?)",
        (msg_id, conv_id, sender, content, "2026-05-01T10:00:00+00:00"),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    for username in ("anna", "carl"):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, f"u-{username}", username.title()),
        )
    await _seed_peer(db, PEER, "u-bob", "bob")
    await _seed_peer(db, OTHER, "u-dora", "dora")
    await _seed_conversation(db, "c-bob", local=("anna",), remote=((PEER, "bob"),))
    await _seed_conversation(db, "c-dora", local=("anna",), remote=((OTHER, "dora"),))
    await _seed_conversation(db, "c-local", local=("anna", "carl"))
    # Older rows: a 1:1 with Bob whose remote seat was never written (Bob
    # already wrote in it), and a 1:1 nobody remote ever wrote in.
    await _seed_conversation(db, "c-legacy", local=("anna",))
    await _seed_conversation(db, "c-orphan", local=("anna",))
    await _seed_message(db, "m-anna", "c-bob", "u-anna", "anna to bob")
    await _seed_message(db, "m-bob", "c-bob", "u-bob", "bob to anna")
    await _seed_message(db, "m-dora", "c-dora", "u-dora", "dora to anna")
    await _seed_message(db, "m-local", "c-local", "u-anna", "anna to carl")
    await _seed_message(db, "m-legacy", "c-legacy", "u-bob", "old bob to anna")
    await _seed_message(db, "m-orphan", "c-orphan", "u-anna", "note to self")
    await db.enqueue(
        "INSERT INTO message_reactions(message_id, user_id, emoji) VALUES(?,?,?)",
        ("m-dora", "u-anna", "👍"),
    )
    sent: list[tuple[str, FederationEventType, dict]] = []

    async def _record_send(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type, payload))

    monkeypatch.setattr(FederationService, "send_event", _record_send)
    return app, db, sent


async def _state(db) -> dict[str, list[tuple]]:
    queries = {
        "conversations": "SELECT id, type FROM conversations ORDER BY id",
        "members": "SELECT conversation_id, username FROM conversation_members"
        " ORDER BY 1, 2",
        "remote_members": "SELECT conversation_id, instance_id, remote_username"
        " FROM conversation_remote_members ORDER BY 1, 2, 3",
        "messages": "SELECT id, conversation_id, sender_user_id, content, deleted"
        " FROM conversation_messages ORDER BY id",
        "reactions": "SELECT message_id, user_id, emoji FROM message_reactions"
        " ORDER BY 1, 2, 3",
    }
    return {
        name: [tuple(r) for r in await db.fetchall(sql, ())]
        for name, sql in queries.items()
    }


async def _send(app, event_type, payload, *, from_instance=PEER) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m",
                event_type=event_type,
                from_instance=from_instance,
                to_instance="us",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )


def _dm(conv, msg_id, sender, content="forged", **extra) -> dict:
    return {
        "conversation_id": conv,
        "message_id": msg_id,
        "sender_user_id": sender,
        "content": content,
        "occurred_at": "2026-05-02T10:00:00+00:00",
        "recipient_user_ids": ["u-anna"],
        **extra,
    }


# ─── DM_MESSAGE ──────────────────────────────────────────────────────────

_MESSAGE_ATTACKS = [
    pytest.param(_dm("c-bob", "m-new", "u-anna"), PEER, id="speaks as a local member"),
    pytest.param(_dm("c-bob", "m-new", "u-dora"), PEER, id="speaks as a third user"),
    pytest.param(_dm("c-local", "m-new", "u-bob"), PEER, id="posts into a local DM"),
    pytest.param(_dm("c-dora", "m-new", "u-bob"), PEER, id="posts into another DM"),
    pytest.param(_dm("c-bob", "m-bob", "u-dora"), OTHER, id="unseated household"),
    pytest.param(_dm("c-orphan", "m-new", "u-bob"), PEER, id="claims an unseated 1:1"),
    pytest.param(
        _dm("c-legacy", "m-new", "u-dora"), OTHER, id="claims another's old 1:1"
    ),
    pytest.param(
        _dm("c-bob", "m-anna", "u-bob", edited_at="2026-05-02T11:00:00+00:00"),
        PEER,
        id="edits a local member's message",
    ),
    pytest.param(_dm("c-bob", "m-dora", "u-bob"), PEER, id="reuses a foreign id"),
    pytest.param(
        _dm("c-new", "m-local", "u-bob"), PEER, id="new conversation reusing an id"
    ),
    pytest.param(
        _dm("c-new", "m-new", "u-bob", recipient_user_ids=["u-dora"]),
        PEER,
        id="new conversation without a local recipient",
    ),
    pytest.param(
        _dm("c-new", "m-new", "u-dora", recipient_user_ids=["u-anna"]),
        PEER,
        id="new conversation as a third user",
    ),
]


@pytest.mark.parametrize(("payload", "sender"), _MESSAGE_ATTACKS)
async def test_dm_message_outside_its_scope_changes_nothing(env, payload, sender):
    app, db, _ = env
    before = await _state(db)
    await _send(app, FET.DM_MESSAGE, payload, from_instance=sender)
    assert await _state(db) == before


async def test_the_seated_household_writes_and_edits_its_own_messages(env):
    app, db, _ = env
    await _send(app, FET.DM_MESSAGE, _dm("c-bob", "m-new", "u-bob", "hello"))
    await _send(
        app,
        FET.DM_MESSAGE,
        _dm("c-bob", "m-bob", "u-bob", "edited", edited_at="2026-05-02T11:00:00Z"),
    )
    messages = (await _state(db))["messages"]
    assert ("m-new", "c-bob", "u-bob", "hello", 0) in messages
    assert ("m-bob", "c-bob", "u-bob", "edited", 0) in messages


async def test_a_first_message_opens_a_conversation_for_the_local_recipient(env):
    app, db, _ = env
    await _send(app, FET.DM_MESSAGE, _dm("c-new", "m-first", "u-bob", "hi anna"))
    state = await _state(db)
    assert ("c-new", "anna") in state["members"]
    assert ("c-new", PEER, "bob") in state["remote_members"]
    assert ("m-first", "c-new", "u-bob", "hi anna", 0) in state["messages"]


async def test_a_message_never_re_seats_anyone_in_an_existing_conversation(env):
    """A seated sender naming more recipients adds no member to the thread."""
    app, db, _ = env
    await _send(
        app,
        FET.DM_MESSAGE,
        _dm("c-bob", "m-new", "u-bob", recipient_user_ids=["u-anna", "u-carl"]),
    )
    state = await _state(db)
    assert ("c-bob", "carl") not in state["members"]


# ─── DM_MESSAGE_DELETED ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message_id", "sender"),
    [
        pytest.param("m-anna", PEER, id="a local member's message"),
        pytest.param("m-local", PEER, id="a message in a local DM"),
        pytest.param("m-dora", PEER, id="another household's message"),
        pytest.param("m-bob", OTHER, id="spoofed by another household"),
    ],
)
async def test_dm_delete_outside_its_scope_changes_nothing(env, message_id, sender):
    app, db, _ = env
    before = await _state(db)
    await _send(
        app,
        FET.DM_MESSAGE_DELETED,
        {"conversation_id": "c-bob", "message_id": message_id},
        from_instance=sender,
    )
    assert await _state(db) == before


async def test_dm_delete_needs_the_messages_own_conversation(env):
    app, db, _ = env
    before = await _state(db)
    await _send(
        app,
        FET.DM_MESSAGE_DELETED,
        {"conversation_id": "c-dora", "message_id": "m-bob"},
    )
    assert await _state(db) == before


async def test_the_sender_household_deletes_its_own_message(env):
    app, db, _ = env
    await _send(
        app,
        FET.DM_MESSAGE_DELETED,
        {"conversation_id": "c-bob", "message_id": "m-bob"},
    )
    rows = {r[0]: r for r in (await _state(db))["messages"]}
    assert rows["m-bob"][4] == 1


# ─── DM_MESSAGE_REACTION ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message_id", "user_id", "action", "sender"),
    [
        pytest.param("m-bob", "u-anna", "add", PEER, id="reacts as a local member"),
        pytest.param("m-bob", "u-dora", "add", PEER, id="reacts as a third user"),
        pytest.param("m-local", "u-bob", "add", PEER, id="reacts in a local DM"),
        pytest.param("m-dora", "u-bob", "add", PEER, id="reacts in another DM"),
        pytest.param(
            "m-dora", "u-anna", "remove", PEER, id="clears a local member's reaction"
        ),
        pytest.param("m-bob", "u-bob", "add", OTHER, id="spoofed by another household"),
    ],
)
async def test_dm_reaction_outside_its_scope_changes_nothing(
    env, message_id, user_id, action, sender
):
    app, db, _ = env
    before = await _state(db)
    await _send(
        app,
        FET.DM_MESSAGE_REACTION,
        {
            "conversation_id": "c-bob",
            "message_id": message_id,
            "user_id": user_id,
            "emoji": "👍",
            "action": action,
        },
        from_instance=sender,
    )
    assert await _state(db) == before


async def test_the_seated_user_reacts_and_unreacts(env):
    app, db, _ = env
    payload = {
        "conversation_id": "c-bob",
        "message_id": "m-anna",
        "user_id": "u-bob",
        "emoji": "🎉",
    }
    await _send(app, FET.DM_MESSAGE_REACTION, {**payload, "action": "add"})
    assert ("m-anna", "u-bob", "🎉") in (await _state(db))["reactions"]
    await _send(app, FET.DM_MESSAGE_REACTION, {**payload, "action": "remove"})
    assert ("m-anna", "u-bob", "🎉") not in (await _state(db))["reactions"]


# ─── DM_USER_TYPING ──────────────────────────────────────────────────────


@pytest.fixture
def typing_frames(env, monkeypatch):
    app, _, _ = env
    typing = app[federation_service_key]._typing_service
    frames: list[tuple[list[str], dict]] = []

    async def _record(_self, user_ids, frame):
        frames.append((list(user_ids), frame))
        return len(user_ids)

    monkeypatch.setattr(type(typing._ws), "broadcast_to_users", _record)
    return frames


@pytest.mark.parametrize(
    ("conv", "user_id", "sender"),
    [
        pytest.param("c-bob", "u-anna", PEER, id="types as a local member"),
        pytest.param("c-bob", "u-dora", PEER, id="types as a third user"),
        pytest.param("c-local", "u-bob", PEER, id="types into a local DM"),
        pytest.param("c-dora", "u-bob", PEER, id="types into another DM"),
    ],
)
async def test_typing_outside_its_scope_reaches_nobody(
    env, typing_frames, conv, user_id, sender
):
    app, _, _ = env
    await _send(
        app,
        FET.DM_USER_TYPING,
        {"conversation_id": conv, "sender_user_id": user_id, "sender_username": "x"},
        from_instance=sender,
    )
    assert typing_frames == []


async def test_the_seated_user_typing_reaches_the_local_member(env, typing_frames):
    app, _, _ = env
    await _send(
        app,
        FET.DM_USER_TYPING,
        {"conversation_id": "c-bob", "sender_user_id": "u-bob", "sender_username": "x"},
    )
    assert [targets for targets, _ in typing_frames] == [["u-anna"]]
    assert typing_frames[0][1]["sender_username"] == "bob"


# ─── DM_HISTORY_REQUEST / DM_HISTORY_CHUNK ───────────────────────────────


def _history_chunks(sent) -> list[dict]:
    return [p for _, t, p in sent if t == FET.DM_HISTORY_CHUNK]


@pytest.mark.parametrize(
    "conv", [pytest.param("c-local", id="local DM"), pytest.param("c-dora", id="other")]
)
async def test_history_of_a_conversation_the_peer_is_not_in_is_not_sent(env, conv):
    app, _, sent = env
    await _send(app, FET.DM_HISTORY_REQUEST, {"conversation_id": conv})
    assert _history_chunks(sent) == []


async def test_history_of_the_peers_own_conversation_is_sent(env):
    app, _, sent = env
    await _send(app, FET.DM_HISTORY_REQUEST, {"conversation_id": "c-bob"})
    ids = {m["id"] for chunk in _history_chunks(sent) for m in chunk["messages"]}
    assert ids == {"m-anna", "m-bob"}


def _chunk(conv, *messages) -> dict:
    return {"conversation_id": conv, "chunk_index": 0, "messages": list(messages)}


def _row(msg_id, sender, content="forged") -> dict:
    return {
        "id": msg_id,
        "sender_user_id": sender,
        "content": content,
        "created_at": "2026-05-02T10:00:00+00:00",
    }


@pytest.mark.parametrize(
    ("payload", "sender"),
    [
        pytest.param(_chunk("c-local", _row("m-x", "u-bob")), PEER, id="local DM"),
        pytest.param(_chunk("c-dora", _row("m-x", "u-bob")), PEER, id="other DM"),
        pytest.param(_chunk("c-bob", _row("m-anna", "u-anna")), PEER, id="overwrite"),
        pytest.param(
            _chunk("c-dora", _row("m-dora", "u-dora")), PEER, id="rewrite foreign"
        ),
        pytest.param(_chunk("c-bob", _row("m-x", "u-anna")), PEER, id="as local"),
        pytest.param(_chunk("c-bob", _row("m-x", "u-dora")), PEER, id="as third"),
        pytest.param(_chunk("c-bob", _row("m-dora", "u-bob")), PEER, id="foreign id"),
        pytest.param(_chunk("c-bob", _row("m-x", "u-bob")), OTHER, id="unseated"),
    ],
)
async def test_history_chunk_outside_its_scope_changes_nothing(env, payload, sender):
    app, db, _ = env
    before = await _state(db)
    await _send(app, FET.DM_HISTORY_CHUNK, payload, from_instance=sender)
    assert await _state(db) == before


async def test_history_chunk_fills_in_the_seated_users_missing_message(env):
    app, db, _ = env
    await _send(
        app, FET.DM_HISTORY_CHUNK, _chunk("c-bob", _row("m-missed", "u-bob", "late"))
    )
    assert ("m-missed", "c-bob", "u-bob", "late", 0) in (await _state(db))["messages"]


# ─── A DM that beat its sender's profile sync ────────────────────────────


async def test_a_dm_from_a_not_yet_synced_sender_lands_once_they_sync(env):
    """The sender's user row can trail their first DM. The message is held,
    not lost, and lands once the sender's own household syncs them."""
    app, db, _ = env
    new_id = derive_user_id(bytes(32), "erin")  # the peers' pinned key is all-zero
    await _send(app, FET.DM_MESSAGE, _dm("c-new", "m-erin", new_id, "hi anna"))
    assert "m-erin" not in {r[0] for r in (await _state(db))["messages"]}
    await _send(
        app, FET.USERS_SYNC, {"users": [{"user_id": new_id, "username": "erin"}]}
    )
    messages = (await _state(db))["messages"]
    assert ("m-erin", "c-new", new_id, "hi anna", 0) in messages


async def test_a_held_dm_is_dropped_when_another_household_syncs_the_id(env):
    app, db, _ = env
    new_id = derive_user_id(bytes(32), "erin")
    await _send(
        app, FET.DM_MESSAGE, _dm("c-new", "m-erin", new_id), from_instance=OTHER
    )
    await _send(
        app, FET.USERS_SYNC, {"users": [{"user_id": new_id, "username": "erin"}]}
    )
    assert "m-erin" not in {r[0] for r in (await _state(db))["messages"]}


async def test_an_old_1_1_missing_its_remote_seat_heals_for_its_writer(env):
    """A 1:1 written before seats were recorded heals for the remote user who
    already wrote in it — and only for them."""
    app, db, _ = env
    await _send(app, FET.DM_MESSAGE, _dm("c-legacy", "m-new", "u-bob", "again"))
    state = await _state(db)
    assert ("c-legacy", PEER, "bob") in state["remote_members"]
    assert ("m-new", "c-legacy", "u-bob", "again", 0) in state["messages"]


async def test_history_chunk_carries_the_senders_own_edits_and_deletes(env):
    """A catch-up copy of the sender's own message may carry its later edit
    or delete — the stored row's sender and conversation match the chunk."""
    app, db, _ = env
    edited = {
        **_row("m-bob", "u-bob", "bob, edited"),
        "edited_at": "2026-05-03T09:00:00+00:00",
    }
    await _send(app, FET.DM_HISTORY_CHUNK, _chunk("c-bob", edited))
    rows = {r[0]: r for r in (await _state(db))["messages"]}
    assert rows["m-bob"][3] == "bob, edited"
    await _send(
        app,
        FET.DM_HISTORY_CHUNK,
        _chunk("c-bob", {**_row("m-bob", "u-bob", ""), "deleted": True}),
    )
    rows = {r[0]: r for r in (await _state(db))["messages"]}
    assert rows["m-bob"][4] == 1
