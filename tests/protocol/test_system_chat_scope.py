"""Release-blocker protocol tests: no DM event ever reaches a system chat.

Marked ``@pytest.mark.security``.

The household chat (``conversations.system_scope = 'household'``) is
group-DM storage that never leaves this household; a space's chat
(``'space'``) travels only as the v_55 ``SPACE_CHAT_*`` events, never as a
DM. A paired household
must not write into it, edit, delete or react to its messages, pull its
history, push history into it, or re-roster it through any ``DM_*`` event
— not even when it (somehow) holds a seat row there. The control case
proves the forged seat alone would have been enough for a plain
conversation, so every refusal below is the system-chat rule at work.

Runs against the real application registry and SQLite.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.config import Config
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.federation_service import FederationService

pytestmark = pytest.mark.security

FET = FederationEventType

PEER = "peer-bob"
CHAT = "c-household"
SPACE_CHAT = "c-space"
PLAIN = "c-plain"


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "sys.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


async def _seed_conversation(
    db, conv_id: str, system_scope: str | None, space_id: str | None = None
) -> None:
    await db.enqueue(
        "INSERT INTO conversations(id, type, system_scope, space_id)"
        " VALUES(?, ?, ?, ?)",
        (
            conv_id,
            "dm" if system_scope is None else "group_dm",
            system_scope,
            space_id,
        ),
    )
    await db.enqueue(
        "INSERT INTO conversation_members(conversation_id, username) VALUES(?, 'anna')",
        (conv_id,),
    )
    # The forged part: a remote seat for Bob's household.
    await db.enqueue(
        "INSERT INTO conversation_remote_members(conversation_id, instance_id,"
        " remote_username) VALUES(?, ?, 'bob')",
        (conv_id, PEER),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('anna','u-anna','Anna')"
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
            f"https://{PEER}/wh",
            f"wh-{PEER}",
            "confirmed",
            "manual",
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES('u-bob', ?, 'bob', 'Bob')",
        (PEER,),
    )
    await _seed_conversation(db, CHAT, "household")
    await _seed_conversation(db, PLAIN, None)
    # A space's chat, Bob's household a writer member of the space.
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'S', 'us', 'anna', ?)",
        ("ab" * 32,),
    )
    await db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES('sp-1', ?, 'u-bob', 'member')",
        (PEER,),
    )
    await _seed_conversation(db, SPACE_CHAT, "space", "sp-1")
    for msg_id, sender, conv in (
        ("m-anna", "u-anna", CHAT),
        ("m-bob", "u-bob", CHAT),
        ("s-anna", "u-anna", SPACE_CHAT),
        ("s-bob", "u-bob", SPACE_CHAT),
    ):
        await db.enqueue(
            "INSERT INTO conversation_messages(id, conversation_id, sender_user_id,"
            " content, created_at) VALUES(?,?,?,?,?)",
            (msg_id, conv, sender, "chat", "2026-05-01T10:00:00+00:00"),
        )
    sent: list[tuple[str, FederationEventType, dict]] = []

    async def _record_send(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type, payload))

    monkeypatch.setattr(FederationService, "send_event", _record_send)
    return app, db, sent


async def _state(db) -> dict[str, list[tuple]]:
    queries = {
        "conversations": "SELECT id, type, name, system_scope, membership_version"
        " FROM conversations ORDER BY id",
        "members": "SELECT conversation_id, username, deleted_at"
        " FROM conversation_members ORDER BY 1, 2",
        "messages": "SELECT id, conversation_id, sender_user_id, content, deleted"
        " FROM conversation_messages ORDER BY id",
        "reactions": "SELECT message_id, user_id, emoji FROM message_reactions"
        " ORDER BY 1, 2, 3",
    }
    return {
        name: [tuple(r) for r in await db.fetchall(sql, ())]
        for name, sql in queries.items()
    }


async def _send(app, event_type, payload) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id="m",
                event_type=event_type,
                from_instance=PEER,
                to_instance="us",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )


def _dm(conv: str, msg_id: str, content: str = "forged", **extra) -> dict:
    return {
        "conversation_id": conv,
        "message_id": msg_id,
        "sender_user_id": "u-bob",
        "content": content,
        "occurred_at": "2026-05-02T10:00:00+00:00",
        "recipient_user_ids": ["u-anna"],
        **extra,
    }


_ATTACKS = [
    pytest.param(FET.DM_MESSAGE, _dm(CHAT, "m-new"), id="posts into the chat"),
    pytest.param(
        FET.DM_MESSAGE,
        _dm(CHAT, "m-bob", "rewritten", edited_at="2026-05-02T11:00:00+00:00"),
        id="edits a chat message",
    ),
    pytest.param(
        FET.DM_MESSAGE,
        _dm("c-elsewhere", "m-bob", "moved"),
        id="re-homes a chat message id",
    ),
    pytest.param(
        FET.DM_MESSAGE_DELETED,
        {"conversation_id": CHAT, "message_id": "m-bob"},
        id="deletes a chat message",
    ),
    pytest.param(
        FET.DM_MESSAGE_REACTION,
        {
            "conversation_id": CHAT,
            "message_id": "m-anna",
            "user_id": "u-bob",
            "emoji": "👍",
            "action": "add",
        },
        id="reacts in the chat",
    ),
    pytest.param(
        FET.DM_GROUP_ROSTER,
        {
            "conversation_id": CHAT,
            "version": 9,
            "name": "taken",
            "members": [],
        },
        id="re-rosters the chat",
    ),
    pytest.param(
        FET.DM_HISTORY_CHUNK,
        {
            "conversation_id": CHAT,
            "chunk_index": 0,
            "is_last": True,
            "messages": [
                {
                    "id": "m-hist",
                    "sender_user_id": "u-bob",
                    "content": "backfilled",
                    "created_at": "2026-05-01T09:00:00+00:00",
                }
            ],
        },
        id="pushes history into the chat",
    ),
]


#: The same attacks aimed at a space's chat: it federates only as the v_55
#: ``SPACE_CHAT_*`` events (authorship-bound to the space roster), so a DM
#: event naming its conversation id is refused outright.
_SPACE_CHAT_ATTACKS = [
    pytest.param(
        FET.DM_MESSAGE, _dm(SPACE_CHAT, "s-new"), id="posts into a space chat"
    ),
    pytest.param(
        FET.DM_MESSAGE,
        _dm(SPACE_CHAT, "s-bob", "rewritten", edited_at="2026-05-02T11:00:00+00:00"),
        id="edits a space chat message",
    ),
    pytest.param(
        FET.DM_MESSAGE,
        _dm("c-elsewhere", "s-bob", "moved"),
        id="re-homes a space chat message id",
    ),
    pytest.param(
        FET.DM_MESSAGE_DELETED,
        {"conversation_id": SPACE_CHAT, "message_id": "s-anna"},
        id="deletes a space chat message",
    ),
    pytest.param(
        FET.DM_MESSAGE_REACTION,
        {
            "conversation_id": SPACE_CHAT,
            "message_id": "s-anna",
            "user_id": "u-bob",
            "emoji": "👍",
            "action": "add",
        },
        id="reacts in a space chat",
    ),
    pytest.param(
        FET.DM_GROUP_ROSTER,
        {"conversation_id": SPACE_CHAT, "version": 9, "name": "x", "members": []},
        id="re-rosters a space chat",
    ),
]


@pytest.mark.parametrize(("event_type", "payload"), _ATTACKS + _SPACE_CHAT_ATTACKS)
async def test_dm_events_never_touch_a_system_chat(env, event_type, payload):
    app, db, _ = env
    before = await _state(db)
    await _send(app, event_type, payload)
    assert await _state(db) == before


async def test_history_of_a_system_chat_is_never_handed_out(env):
    app, _, sent = env
    await _send(app, FET.DM_HISTORY_REQUEST, {"conversation_id": CHAT})
    await _send(app, FET.DM_HISTORY_REQUEST, {"conversation_id": SPACE_CHAT})
    assert sent == []


async def test_media_bytes_for_a_system_chat_message_are_refused(env, tmp_dir):
    app, db, _ = env
    await db.enqueue(
        "UPDATE conversation_messages SET type='image', media_blob_id='m-bob'"
        " WHERE id='m-bob'"
    )
    before = await _state(db)
    await _send(
        app,
        FET.DM_MEDIA_BLOB,
        {
            "conversation_id": CHAT,
            "message_id": "m-bob",
            "media_blob_id": "m-bob",
            "bytes_b64": "aGVsbG8=",
            "mime_type": "image/webp",
        },
    )
    assert await _state(db) == before
    media = tmp_dir / "media"
    assert not media.exists() or not any(
        p.name.startswith("m-bob") for p in media.iterdir()
    )


async def test_control_the_same_seat_reaches_a_plain_conversation(env):
    """Without ``system_scope`` the (forged) seat would have been enough."""
    app, db, _ = env
    await _send(app, FET.DM_MESSAGE, _dm(PLAIN, "m-plain", "hello"))
    messages = (await _state(db))["messages"]
    assert ("m-plain", PLAIN, "u-bob", "hello", 0) in messages
