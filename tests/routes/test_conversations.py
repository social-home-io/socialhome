"""Tests for conversation routes — /api/conversations/* endpoints."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import conversation_repo_key
from socialhome.app_keys import db_key as _db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id, generate_identity_keypair
from socialhome.domain.conversation import ConversationMessage
from socialhome.services.dm_service import DmService


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(tmp_dir):
    """App client with admin (pascal) and regular user (bob)."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        db = app[_db_key]
        _row = await db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'"
        )
        _pk = bytes.fromhex(_row["identity_public_key"])

        class _KP:
            public_key = _pk

        kp = _KP()
        uid = derive_user_id(kp.public_key, "pascal")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
            ("pascal", uid, "Pascal"),
        )
        raw_token = "test-token-raw"
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-1", uid, "test", sha256_token_hash(raw_token)),
        )
        uid2 = derive_user_id(kp.public_key, "bob")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
            ("bob", uid2, "Bob"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-2", uid2, "test", sha256_token_hash("bob-token-raw")),
        )
        tc._admin_token = raw_token
        tc._admin_uid = uid
        tc._bob_token = "bob-token-raw"
        tc._bob_uid = uid2
        yield tc


async def test_list_conversations_includes_member_preview(client):
    """``GET /api/conversations`` ships a per-row members preview +
    member_count so the inbox can render avatar stacks + a peer-name
    fallback without N+1 follow-up fetches."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    resp = await client.get(
        "/api/conversations",
        headers=_auth(client._admin_token),
    )
    rows = await resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert "members" in row
    assert "member_count" in row
    assert row["member_count"] == 2
    # Per-row unread count powers the sidebar Chats badge + per-row
    # chips. Empty conversation: starts at 0.
    assert row["unread"] == 0
    # The preview filters out *me* (the caller); only the peer should
    # appear so the inbox can render "Bob" without manual filtering.
    assert {m["username"] for m in row["members"]} == {"bob"}
    assert row["members"][0]["display_name"] == "Bob"
    # Brand-new conversation: caller's read watermark is None.
    assert "last_read_at" in row
    assert row["last_read_at"] is None


async def test_list_conversations_surfaces_caller_last_read_at(client):
    """``last_read_at`` on each row reflects the caller's own watermark
    — the SPA uses it to find the first-unread message in the loaded
    window and anchor the entry scroll to a "New messages" divider."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    # Post a message + mark-as-read to advance the watermark.
    await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hello"},
        headers=_auth(client._admin_token),
    )
    await client.post(
        f"/api/conversations/{conv_id}/read",
        json={},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        "/api/conversations",
        headers=_auth(client._admin_token),
    )
    rows = await resp.json()
    row = next(r for r in rows if r["id"] == conv_id)
    assert row["last_read_at"] is not None
    # ISO 8601 shape — the SPA does Date.parse on this.
    assert "T" in row["last_read_at"]


async def test_list_conversations_group_dm_carries_all_peers(client):
    """Group DMs surface every other member in the preview so the
    inbox can render an avatar stack and a peer-name fallback like
    'Bob · Carol'."""
    # Need a third user; seed one directly via the app's DB handle.
    db = client.app[_db_key]
    await db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name)"
        " VALUES('carol', 'c-id', 'Carol')",
    )
    r = await client.post(
        "/api/conversations/group",
        json={"members": ["bob", "carol"], "name": "Lunch crew"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    resp = await client.get(
        "/api/conversations",
        headers=_auth(client._admin_token),
    )
    rows = await resp.json()
    row = next(r for r in rows if r["type"] == "group_dm")
    assert row["member_count"] == 3
    assert {m["username"] for m in row["members"]} == {"bob", "carol"}


async def test_list_dm_members_carries_online_status(client):
    """GET /api/conversations/{id}/members returns rows with the
    session-presence triple — needed for the WhatsApp-style status line
    in the thread header."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    resp = await client.get(
        f"/api/conversations/{conv_id}/members",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    rows = await resp.json()
    assert len(rows) == 2
    for m in rows:
        assert "user_id" in m
        assert "username" in m
        assert "display_name" in m
        # ``picture_url`` is part of the contract the DM thread relies
        # on to show the peer avatar next to the TopBar title without
        # a follow-up fetch. ``None`` is fine — the SPA's ``Avatar``
        # component falls back to initials.
        assert "picture_url" in m
        assert m["picture_url"] is None or isinstance(m["picture_url"], str)
        assert "is_self" in m
        assert "is_online" in m
        assert "is_idle" in m
        assert "last_seen_at" in m
    # Exactly one row should be is_self=True (the caller).
    assert sum(1 for m in rows if m["is_self"]) == 1


async def _seed_remote_brother(client) -> str:
    """Seat a federated peer ('brother@peer-b') in both ``remote_instances``
    and ``remote_users`` the way the peer-directory snapshot would after
    a successful pairing. Returns the remote ``user_id``."""
    db = client.app[_db_key]
    remote_uid = "uid-brother-remote"
    await db.enqueue(
        """INSERT OR IGNORE INTO remote_instances(
               id, display_name, remote_identity_pk, key_self_to_remote,
               key_remote_to_self, remote_inbox_url, local_inbox_id,
               status, source
           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "peer-b",
            "Peer B",
            "00" * 32,
            "k1",
            "k2",
            "https://peer-b.example/federation/inbox/x",
            "local-inbox",
            "confirmed",
            "manual",
        ),
    )
    await db.enqueue(
        """INSERT OR IGNORE INTO remote_users(
               user_id, instance_id, remote_username, display_name, alias,
               visible_to, picture_hash, bio, status_json,
               public_key, public_key_version, synced_at
           ) VALUES(?, ?, ?, ?, NULL, '\"all\"', ?, NULL, NULL,
                    NULL, 0, datetime('now'))""",
        (
            remote_uid,
            "peer-b",
            "brother",
            "Brother",
            "pic-hash-abc",
        ),
    )
    return remote_uid


async def test_list_conversations_includes_remote_peer_preview(client):
    """Regression: a cross-household DM (creator + ``RemoteConversationMember``
    for a federated peer) used to render as "Direct message" with no avatar
    in the inbox because the endpoint only joined the local member table.
    The remote peer must appear in ``members`` with display_name + picture
    URL so ``DmInboxPage`` can build the row title and avatar stack."""
    remote_uid = await _seed_remote_brother(client)

    r = await client.post(
        "/api/conversations/dm",
        json={"user_id": remote_uid},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201

    resp = await client.get(
        "/api/conversations",
        headers=_auth(client._admin_token),
    )
    rows = await resp.json()
    assert len(rows) == 1
    row = rows[0]
    # The caller is local + the brother is remote → member_count covers
    # both rosters even though only the brother survives the self-filter.
    assert row["member_count"] == 2
    assert {m["username"] for m in row["members"]} == {"brother"}
    peer = row["members"][0]
    assert peer["user_id"] == remote_uid
    assert peer["display_name"] == "Brother"
    # ``picture_url`` is the cache-busting relative path the SPA already
    # resolves against ``document.baseURI`` — same shape as for local
    # users, just keyed on the federated peer's globally-unique user_id.
    assert peer["picture_url"] == f"api/users/{remote_uid}/picture?v=pic-hash-abc"


async def test_list_dm_members_includes_remote_peer(client):
    """Regression mirror for the thread-header path: ``GET
    /api/conversations/{id}/members`` has to surface the federated peer
    so the DM thread header can render the brother's name + avatar
    without a follow-up fetch."""
    remote_uid = await _seed_remote_brother(client)

    r = await client.post(
        "/api/conversations/dm",
        json={"user_id": remote_uid},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]

    resp = await client.get(
        f"/api/conversations/{conv_id}/members",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    rows = await resp.json()
    assert len(rows) == 2
    by_user_id = {m["user_id"]: m for m in rows}
    assert remote_uid in by_user_id
    brother = by_user_id[remote_uid]
    assert brother["display_name"] == "Brother"
    assert brother["username"] == "brother"
    assert brother["picture_url"] == (f"api/users/{remote_uid}/picture?v=pic-hash-abc")
    # A remote peer is never the caller.
    assert brother["is_self"] is False
    # Presence fields are part of the contract — the SPA renders the
    # status line uniformly for local and remote rows. Offline by
    # default in a fresh test (no USER_ONLINE envelope landed).
    assert brother["is_online"] is False
    assert brother["is_idle"] is False
    # Exactly one ``is_self`` row, and it's the local caller (Pascal).
    assert sum(1 for m in rows if m["is_self"]) == 1
    self_row = next(m for m in rows if m["is_self"])
    assert self_row["username"] == "pascal"


async def test_create_dm(client):
    """POST /api/conversations/dm creates a DM and returns 201."""
    resp = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert "id" in body
    assert body["type"] == "dm"


async def test_send_message(client):
    """POST /api/conversations/{id}/messages sends a message and returns 201."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    resp = await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hello bob"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201


async def test_list_messages(client):
    """GET /api/conversations/{id}/messages returns messages in the conversation."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hello bob"},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        f"/api/conversations/{conv_id}/messages",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    msgs = await resp.json()
    assert len(msgs) == 1


async def test_mark_read(client):
    """POST /api/conversations/{id}/read marks the conversation as read."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hi"},
        headers=_auth(client._admin_token),
    )
    resp = await client.post(
        f"/api/conversations/{conv_id}/read",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200


async def test_mark_read_clears_dm_notifications(client):
    """Opening a thread (POST /read) clears the bell badge for that
    conversation — the recipient's ``dm_message`` notification rows
    flip to read so the unread count drops in lockstep."""
    # Anna sends a DM to Bob.
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hi bob"},
        headers=_auth(client._admin_token),
    )
    # Bob has an unread `dm_message` notification.
    bell = await client.get(
        "/api/notifications/unread-count",
        headers=_auth(client._bob_token),
    )
    assert (await bell.json())["unread"] >= 1
    # Bob opens the thread.
    await client.post(
        f"/api/conversations/{conv_id}/read",
        headers=_auth(client._bob_token),
    )
    # Bell drops to 0 — the route auto-cleared the dm_message row.
    bell2 = await client.get(
        "/api/notifications/unread-count",
        headers=_auth(client._bob_token),
    )
    assert (await bell2.json())["unread"] == 0


async def test_unread_count(client):
    """GET /api/conversations/{id}/unread returns the unread message count."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hi"},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        f"/api/conversations/{conv_id}/unread",
        headers=_auth(client._bob_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert "unread" in body
    assert body["unread"] >= 1


async def test_create_group_dm(client):
    """POST /api/conversations/group creates a group DM and returns 201."""
    # Create a third user first (group DM requires at least 3 participants)
    from socialhome.app_keys import db_key as _db_key
    from socialhome.crypto import derive_user_id

    db = client.app[_db_key]
    kp = generate_identity_keypair()
    uid3 = derive_user_id(kp.public_key, "carol")
    await db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("carol", uid3, "Carol"),
    )
    resp = await client.post(
        "/api/conversations/group",
        json={"members": ["bob", "carol"], "name": "Team"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["type"] == "group_dm"


async def test_list_conversations(client):
    """GET /api/conversations lists the user's active conversations."""
    await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        "/api/conversations",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert len(body) >= 1


async def test_list_conversations_survives_a_removal_landing_mid_listing(client):
    """A group seat removed while the list is being built drops that one
    row — it never 403s the caller's whole inbox.

    Regression for the federation-demo ``group-dm`` flake: the authority's
    roster update (removing carol) landed on c between ``list_for_user``
    and the per-row reads, and the row's membership re-check raised
    ``PermissionError`` → 403 for ``GET /api/conversations``.
    """
    db = client.app[_db_key]
    await db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name)"
        " VALUES('carol', 'c-id', 'Carol')",
    )
    r = await client.post(
        "/api/conversations/group",
        json={"members": ["bob", "carol"], "name": "Lunch crew"},
        headers=_auth(client._admin_token),
    )
    gid = (await r.json())["id"]
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "pascal"},
        headers=_auth(client._bob_token),
    )
    dm_id = (await r.json())["id"]

    original = DmService.list_conversations

    async def _list_then_remove(self, username):
        convs = await original(self, username)
        # The removal lands after the snapshot, before the per-row reads.
        await db.enqueue(
            "UPDATE conversation_members SET deleted_at=datetime('now')"
            " WHERE conversation_id=? AND username='bob'",
            (gid,),
        )
        return convs

    with patch.object(DmService, "list_conversations", _list_then_remove):
        resp = await client.get(
            "/api/conversations",
            headers=_auth(client._bob_token),
        )
    assert resp.status == 200
    ids = {row["id"] for row in await resp.json()}
    assert dm_id in ids
    assert gid not in ids


async def test_create_dm_with_self_is_error(client):
    """POST /api/conversations/dm with own username returns an error (422 or 404)."""
    resp = await client.post(
        "/api/conversations/dm",
        json={"username": "pascal"},
        headers=_auth(client._admin_token),
    )
    assert resp.status in (422, 404)


async def test_create_dm_nonexistent_user_404(client):
    """POST /api/conversations/dm with unknown username returns 404."""
    resp = await client.post(
        "/api/conversations/dm",
        json={"username": "nobody"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 404


async def test_send_empty_message_422(client):
    """POST messages with empty content returns 422."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    resp = await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": ""},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_send_message_non_local_media_url_422(client):
    """F5: a ``javascript:`` / remote ``media_url`` is refused with the
    coded 422 ``INVALID_MEDIA_URL``; nothing is stored."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    for bad in ("javascript:alert(document.domain)", "https://tracker.example/p.png"):
        resp = await client.post(
            f"/api/conversations/{conv_id}/messages",
            json={"type": "file", "media_url": bad, "file_name": "x.pdf"},
            headers=_auth(client._admin_token),
        )
        assert resp.status == 422
        body = await resp.json()
        assert body["error"]["code"] == "INVALID_MEDIA_URL"
        assert bad not in json.dumps(body)
    resp = await client.get(
        f"/api/conversations/{conv_id}/messages",
        headers=_auth(client._admin_token),
    )
    assert await resp.json() == []


async def test_list_messages_drops_stored_non_local_media_url(client):
    """F5 existing rows: a ``media_url`` stored before the send / inbound
    gates (``javascript:``, a remote tracker) is never served — the list
    answers ``media_url: null`` for it, while a local ref still comes back."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    repo = client.app[conversation_repo_key]
    for mid, url in (
        ("m-js", "javascript:alert(document.domain)"),
        ("m-remote", "https://tracker.example/p.png"),
        ("m-ok", "api/media/ok.pdf"),
    ):
        await repo.save_message(
            ConversationMessage(
                id=mid,
                conversation_id=conv_id,
                sender_user_id=client._admin_uid,
                content="",
                type="file",
                media_url=url,
                file_name=f"{mid}.pdf",
                created_at=datetime.now(timezone.utc),
            )
        )
    resp = await client.get(
        f"/api/conversations/{conv_id}/messages",
        headers=_auth(client._admin_token),
    )
    by_id = {m["id"]: m for m in await resp.json()}
    assert by_id["m-js"]["media_url"] is None
    assert by_id["m-remote"]["media_url"] is None
    assert by_id["m-ok"]["media_url"].split("?", 1)[0] == "api/media/ok.pdf"


async def test_send_location_message_is_rounded_and_malformed_422(client):
    """A location DM is stored + listed with 4-dp coords; junk is a 422."""
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    resp = await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={
            "type": "location",
            "content": json.dumps(
                {"lat": 52.370216789, "lon": 4.895167912, "accuracy_m": 8}
            ),
        },
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    bad = await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"type": "location", "content": "somewhere nice"},
        headers=_auth(client._admin_token),
    )
    assert bad.status == 422
    listed = await client.get(
        f"/api/conversations/{conv_id}/messages",
        headers=_auth(client._admin_token),
    )
    rows = await listed.json()
    assert len(rows) == 1
    assert rows[0]["type"] == "location"
    assert json.loads(rows[0]["content"]) == {
        "lat": 52.3702,
        "lon": 4.8952,
        "label": None,
        "accuracy_m": 25,
    }


# ── DM reliability (§12.5) ─────────────────────────────────────────────────


async def test_mark_read_returns_marked_count(client):
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    # Bob sends two messages so admin can mark them read.
    for body in ("hello", "hi again"):
        await client.post(
            f"/api/conversations/{conv_id}/messages",
            json={"content": body},
            headers=_auth(client._bob_token),
        )
    resp = await client.post(
        f"/api/conversations/{conv_id}/read",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["marked"] == 2


async def test_mark_delivered_upserts_state(client):
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    r2 = await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hello"},
        headers=_auth(client._bob_token),
    )
    msg_id = (await r2.json())["id"]
    resp = await client.post(
        f"/api/conversations/{conv_id}/messages/{msg_id}/delivered",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    # Read-back shows the row with state='delivered'.
    r3 = await client.get(
        f"/api/conversations/{conv_id}/delivery-states",
        headers=_auth(client._admin_token),
    )
    states = (await r3.json())["states"]
    assert len(states) == 1
    assert states[0]["state"] == "delivered"
    assert states[0]["message_id"] == msg_id


async def test_delivery_states_respects_message_ids_filter(client):
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    msg_ids = []
    for body in ("one", "two", "three"):
        rx = await client.post(
            f"/api/conversations/{conv_id}/messages",
            json={"content": body},
            headers=_auth(client._bob_token),
        )
        msg_ids.append((await rx.json())["id"])
    # Mark the whole conversation read so each message has a row.
    await client.post(
        f"/api/conversations/{conv_id}/read",
        headers=_auth(client._admin_token),
    )
    keep = msg_ids[0]
    r2 = await client.get(
        f"/api/conversations/{conv_id}/delivery-states?message_ids={keep}",
        headers=_auth(client._admin_token),
    )
    body = await r2.json()
    assert len(body["states"]) == 1
    assert body["states"][0]["message_id"] == keep


async def test_gaps_endpoint_starts_empty(client):
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    resp = await client.get(
        f"/api/conversations/{conv_id}/gaps",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    assert (await resp.json())["gaps"] == []


async def test_gaps_endpoint_non_member_forbidden(client):
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    # Seed a third user who isn't a member.
    from socialhome.crypto import derive_user_id
    from socialhome.auth import sha256_token_hash

    db = client.server.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    pk = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk, "carl")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("carl", uid, "Carl"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("tid-3", uid, "carl", sha256_token_hash("carl-tok")),
    )
    resp = await client.get(
        f"/api/conversations/{conv_id}/gaps",
        headers={"Authorization": "Bearer carl-tok"},
    )
    # PermissionError → base _iter maps to 403.
    assert resp.status == 403


# ── Mute ───────────────────────────────────────────────────────────────────


async def _dm_with_bob(client) -> str:
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    return (await r.json())["id"]


async def _row(client, token: str, conv_id: str) -> dict:
    r = await client.get("/api/conversations", headers=_auth(token))
    return next(c for c in await r.json() if c["id"] == conv_id)


async def test_mute_put_and_delete_round_trip(client):
    """PUT mutes the caller only; the list shows it; DELETE unmutes."""
    conv_id = await _dm_with_bob(client)
    assert (await _row(client, client._bob_token, conv_id))["muted_until"] is None

    r = await client.put(
        f"/api/conversations/{conv_id}/mute",
        json={"duration": "1h"},
        headers=_auth(client._bob_token),
    )
    assert r.status == 200
    until = (await r.json())["muted_until"]
    assert until.endswith("+00:00")
    assert (await _row(client, client._bob_token, conv_id))["muted_until"] == until
    # The other member's own view is untouched — a mute is personal.
    assert (await _row(client, client._admin_token, conv_id))["muted_until"] is None

    r = await client.delete(
        f"/api/conversations/{conv_id}/mute",
        headers=_auth(client._bob_token),
    )
    assert r.status == 200
    assert (await r.json()) == {"muted_until": None}
    assert (await _row(client, client._bob_token, conv_id))["muted_until"] is None


async def test_mute_forever(client):
    conv_id = await _dm_with_bob(client)
    r = await client.put(
        f"/api/conversations/{conv_id}/mute",
        json={"duration": "forever"},
        headers=_auth(client._bob_token),
    )
    assert (await r.json())["muted_until"].startswith("9999-")


async def test_mute_rejects_a_bad_duration(client):
    conv_id = await _dm_with_bob(client)
    for body in ({"duration": "2d"}, {}, {"duration": 3600}):
        r = await client.put(
            f"/api/conversations/{conv_id}/mute",
            json=body,
            headers=_auth(client._bob_token),
        )
        assert r.status == 422, body


async def test_mute_is_members_only(client):
    conv_id = await _dm_with_bob(client)
    db = client.server.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    uid = derive_user_id(bytes.fromhex(row["identity_public_key"]), "carl")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("carl", uid, "Carl"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("tid-3", uid, "carl", sha256_token_hash("carl-tok")),
    )
    carl = {"Authorization": "Bearer carl-tok"}
    r = await client.put(
        f"/api/conversations/{conv_id}/mute", json={"duration": "1h"}, headers=carl
    )
    assert r.status == 403
    r = await client.delete(f"/api/conversations/{conv_id}/mute", headers=carl)
    assert r.status == 403
    r = await client.put(
        "/api/conversations/no-such-conv/mute",
        json={"duration": "1h"},
        headers=carl,
    )
    assert r.status in (403, 404)


async def test_muted_conversation_counts_unread_but_rings_no_bell(client):
    """A message into a muted conversation still counts unread for the
    muted member, but adds no bell notification."""
    conv_id = await _dm_with_bob(client)
    await client.put(
        f"/api/conversations/{conv_id}/mute",
        json={"duration": "8h"},
        headers=_auth(client._bob_token),
    )
    await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "hi bob"},
        headers=_auth(client._admin_token),
    )
    assert (await _row(client, client._bob_token, conv_id))["unread"] == 1
    bell = await client.get(
        "/api/notifications/unread-count", headers=_auth(client._bob_token)
    )
    assert (await bell.json())["unread"] == 0

    # Unmuted again: the next message rings.
    await client.delete(
        f"/api/conversations/{conv_id}/mute", headers=_auth(client._bob_token)
    )
    await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "still there?"},
        headers=_auth(client._admin_token),
    )
    bell = await client.get(
        "/api/notifications/unread-count", headers=_auth(client._bob_token)
    )
    assert (await bell.json())["unread"] == 1


# ── Video message media_status ─────────────────────────────────────────────


async def _dm_video_message(client) -> tuple[str, str, str]:
    """Create a 1:1 DM + a video message + a matching transcode row.

    Returns ``(conv_id, message_id, output_fn)``. Stops the scheduler so
    the row stays put while the test inspects readiness.
    """
    from socialhome.app_keys import (
        media_transcode_repo_key,
        media_transcode_service_key,
    )

    await client.app[media_transcode_service_key].stop()
    r = await client.post(
        "/api/conversations/dm",
        json={"username": "bob"},
        headers=_auth(client._admin_token),
    )
    conv_id = (await r.json())["id"]
    fn = "dmvid000000000000000000000000.webm"
    r = await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"type": "video", "media_url": f"api/media/{fn}"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    msg_id = (await r.json())["id"]
    await client.app[media_transcode_repo_key].enqueue(
        output_filename=fn,
        source_path="/tmp/src.bin",
        thumbnail_filename="thumb.webp",
        owner_user_id=client._admin_uid,
    )
    return conv_id, msg_id, fn


async def _dm_messages(client, conv_id: str) -> list[dict]:
    r = await client.get(
        f"/api/conversations/{conv_id}/messages",
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    return await r.json()


async def test_dm_video_message_media_status_processing(client):
    from socialhome.app_keys import media_transcode_repo_key

    conv_id, msg_id, fn = await _dm_video_message(client)
    await client.app[media_transcode_repo_key].mark_processing(fn)
    m = next(x for x in await _dm_messages(client, conv_id) if x["id"] == msg_id)
    assert m["type"] == "video"
    assert m["media_status"] == "processing"


async def test_dm_video_message_media_status_ready_after_complete(client):
    from socialhome.app_keys import media_transcode_repo_key

    conv_id, msg_id, fn = await _dm_video_message(client)
    await client.app[media_transcode_repo_key].complete(fn)
    m = next(x for x in await _dm_messages(client, conv_id) if x["id"] == msg_id)
    assert m["media_status"] == "ready"


async def test_dm_video_message_media_status_failed(client):
    from socialhome.app_keys import media_transcode_repo_key

    conv_id, msg_id, fn = await _dm_video_message(client)
    await client.app[media_transcode_repo_key].mark_failed(fn, "boom")
    m = next(x for x in await _dm_messages(client, conv_id) if x["id"] == msg_id)
    assert m["media_status"] == "failed"


async def test_dm_text_message_has_no_processing_status(client):
    conv_id, _msg_id, _fn = await _dm_video_message(client)
    r = await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "just words"},
        headers=_auth(client._admin_token),
    )
    text_id = (await r.json())["id"]
    m = next(x for x in await _dm_messages(client, conv_id) if x["id"] == text_id)
    assert m.get("media_status") != "processing"


async def test_dm_video_message_has_signed_poster(client):
    conv_id, msg_id, _fn = await _dm_video_message(client)
    m = next(x for x in await _dm_messages(client, conv_id) if x["id"] == msg_id)
    poster = m["media_thumbnail_url"]
    base = poster.split("?", 1)[0]
    assert base == "api/media/dmvid000000000000000000000000.webp"
    media_base = m["media_url"].split("?", 1)[0]
    assert base[: -len(".webp")] == media_base[: -len(".webm")]
    assert "exp=" in poster and "sig=" in poster


async def test_dm_text_message_has_no_poster(client):
    conv_id, _msg_id, _fn = await _dm_video_message(client)
    r = await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": "just words"},
        headers=_auth(client._admin_token),
    )
    text_id = (await r.json())["id"]
    m = next(x for x in await _dm_messages(client, conv_id) if x["id"] == text_id)
    assert "media_thumbnail_url" not in m


# ── Cross-household groups (v_37) ─────────────────────────────────────────


async def _seed_household(db, instance_id: str, user_id: str, username: str, *, v=37):
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            instance_id,
            f"{username.title()}'s house",
            "00" * 32,
            "k1",
            "k2",
            f"https://{instance_id}/wh",
            f"wh-{instance_id}",
            "confirmed",
            "manual",
            v,
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        (user_id, instance_id, username, username.title()),
    )


@pytest.fixture
async def households(client, monkeypatch):
    from socialhome.domain.federation import DeliveryResult
    from socialhome.federation.federation_service import FederationService

    sent: list[tuple[str, str, dict]] = []

    async def _send(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type.value, payload))
        return DeliveryResult(instance_id=to_instance_id, ok=True)

    monkeypatch.setattr(FederationService, "send_event", _send)
    db = client.app[_db_key]
    await _seed_household(db, "inst-rita", "u-rita", "rita")
    await _seed_household(db, "inst-olaf", "u-olaf", "olaf", v=36)
    return sent


async def test_a_group_can_include_people_from_other_households(client, households):
    h = _auth(client._admin_token)
    r = await client.post(
        "/api/conversations/group",
        json={"members": ["bob"], "member_user_ids": ["u-rita"], "name": "Mix"},
        headers=h,
    )
    assert r.status == 201
    conv_id = (await r.json())["id"]
    assert [(i, e) for i, e, _ in households] == [("inst-rita", "dm_group_roster")]
    resp = await client.get(f"/api/conversations/{conv_id}/members", headers=h)
    rita = next(m for m in await resp.json() if m["user_id"] == "u-rita")
    assert rita["household_name"] == "Rita's house"
    assert rita["instance_id"] == "inst-rita"
    listing = await (await client.get("/api/conversations", headers=h)).json()
    row = next(c for c in listing if c["id"] == conv_id)
    assert row["managed_here"] is True and row["member_count"] == 3


async def test_a_person_on_an_older_household_is_refused_with_a_reason(
    client, households
):
    r = await client.post(
        "/api/conversations/group",
        json={"members": ["bob"], "member_user_ids": ["u-olaf"]},
        headers=_auth(client._admin_token),
    )
    assert r.status == 422
    assert "GROUP_MEMBER_UNSUPPORTED" in await r.text()
    assert "needs a Social Home update" in await r.text()
    err = (await r.json())["error"]
    # The SPA words the reason itself, in the user's language.
    assert err["params"] == {"reason": "too_old", "name": "Olaf"}
    assert households == []


async def _mixed_group(client) -> str:
    r = await client.post(
        "/api/conversations/group",
        json={"members": ["bob"], "member_user_ids": ["u-rita"]},
        headers=_auth(client._admin_token),
    )
    return (await r.json())["id"]


async def test_group_add_rename_remove_and_leave(client, households):
    h = _auth(client._admin_token)
    db = client.app[_db_key]
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("carol", "u-carol", "Carol"),
    )
    conv_id = await _mixed_group(client)
    r = await client.post(
        f"/api/conversations/{conv_id}/members",
        json={"usernames": ["carol"]},
        headers=h,
    )
    assert r.status == 201
    r = await client.patch(
        f"/api/conversations/{conv_id}", json={"name": "Crew"}, headers=h
    )
    assert r.status == 200
    r = await client.delete(f"/api/conversations/{conv_id}/members/u-rita", headers=h)
    assert r.status == 200
    versions = [p["version"] for _i, e, p in households if e == "dm_group_roster"]
    assert versions == [1, 2, 3, 4]
    r = await client.post(
        f"/api/conversations/{conv_id}/leave", headers=_auth(client._bob_token)
    )
    assert r.status == 200
    r = await client.get(
        f"/api/conversations/{conv_id}/members", headers=_auth(client._bob_token)
    )
    assert r.status == 403
    resp = await client.get(f"/api/conversations/{conv_id}/members", headers=h)
    assert {m["username"] for m in await resp.json()} == {"pascal", "carol"}
    listing = await (await client.get("/api/conversations", headers=h)).json()
    assert next(c for c in listing if c["id"] == conv_id)["name"] == "Crew"


async def test_members_of_a_conversation_are_for_its_members_only(client, households):
    conv_id = await _mixed_group(client)
    db = client.app[_db_key]
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("eve", "u-eve", "Eve"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("tid-eve", "u-eve", "t", sha256_token_hash("eve-token")),
    )
    eve = _auth("eve-token")
    r = await client.get(f"/api/conversations/{conv_id}/members", headers=eve)
    assert r.status == 403
    r = await client.post(
        f"/api/conversations/{conv_id}/members",
        json={"usernames": ["eve"]},
        headers=eve,
    )
    assert r.status == 403


async def test_a_group_kept_elsewhere_is_read_only_here(client, households):
    from socialhome.app_keys import federation_service_key
    from socialhome.domain.federation import FederationEvent, FederationEventType
    from socialhome.federation.owner_bound_id import (
        GROUP_CONVERSATION_KIND,
        mint_owner_bound_id,
    )

    fed = client.app[federation_service_key]
    own = fed.own_instance_id
    conv_id = mint_owner_bound_id(
        GROUP_CONVERSATION_KIND, space_id="", owner_user_id="inst-rita"
    )
    (handler,) = fed._event_registry.handlers_for(FederationEventType.DM_GROUP_ROSTER)
    await handler(
        FederationEvent(
            msg_id="m",
            event_type=FederationEventType.DM_GROUP_ROSTER,
            from_instance="inst-rita",
            to_instance=own,
            timestamp="2026-09-28T00:00:00+00:00",
            payload={
                "conversation_id": conv_id,
                "version": 1,
                "name": "Rita's",
                "members": [
                    {
                        "user_id": "u-rita",
                        "instance_id": "inst-rita",
                        "username": "rita",
                    },
                    {"user_id": client._admin_uid, "instance_id": own, "username": "p"},
                    {"user_id": client._bob_uid, "instance_id": own, "username": "b"},
                ],
            },
        )
    )
    h = _auth(client._admin_token)
    listing = await (await client.get("/api/conversations", headers=h)).json()
    row = next(c for c in listing if c["id"] == conv_id)
    assert row["managed_here"] is False and row["type"] == "group_dm"
    r = await client.patch(
        f"/api/conversations/{conv_id}", json={"name": "x"}, headers=h
    )
    assert r.status == 403
    households.clear()
    r = await client.post(f"/api/conversations/{conv_id}/leave", headers=h)
    assert r.status == 200
    assert [(i, e) for i, e, _ in households] == [("inst-rita", "dm_group_leave")]


# ── Group notification level + mentions ────────────────────────────────────


async def _team_with_carol(client) -> str:
    """Group "Team": pascal (admin), bob, carol."""
    db = client.server.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    uid = derive_user_id(bytes.fromhex(row["identity_public_key"]), "carol")
    await db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("carol", uid, "Carol"),
    )
    await db.enqueue(
        "INSERT OR IGNORE INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES(?,?,?,?)",
        ("tid-carol", uid, "carol", sha256_token_hash("carol-tok")),
    )
    r = await client.post(
        "/api/conversations/group",
        json={"members": ["bob", "carol"], "name": "Team"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    return (await r.json())["id"]


async def _bell(client, token: str) -> list[dict]:
    r = await client.get("/api/notifications", headers=_auth(token))
    body = await r.json()
    return body["notifications"] if isinstance(body, dict) else body


async def test_notif_prefs_round_trip_and_list_field(client):
    conv_id = await _team_with_carol(client)
    url = f"/api/conversations/{conv_id}/notif-prefs"
    r = await client.get(url, headers=_auth(client._bob_token))
    assert (await r.json()) == {"level": "all"}
    r = await client.put(
        url, json={"level": "mentions"}, headers=_auth(client._bob_token)
    )
    assert r.status == 200 and (await r.json()) == {"level": "mentions"}
    assert (await _row(client, client._bob_token, conv_id))["notif_level"] == (
        "mentions"
    )
    # Personal: the others still ring for everything.
    assert (await _row(client, client._admin_token, conv_id))["notif_level"] == "all"


async def test_notif_prefs_rejects_bad_levels_one_to_one_and_outsiders(client):
    conv_id = await _team_with_carol(client)
    url = f"/api/conversations/{conv_id}/notif-prefs"
    for body in ({"level": "muted"}, {}, {"level": 1}):
        r = await client.put(url, json=body, headers=_auth(client._bob_token))
        assert r.status == 422, body
    dm_id = await _dm_with_bob(client)
    r = await client.put(
        f"/api/conversations/{dm_id}/notif-prefs",
        json={"level": "mentions"},
        headers=_auth(client._bob_token),
    )
    assert r.status == 422
    db = client.server.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    uid = derive_user_id(bytes.fromhex(row["identity_public_key"]), "dora")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("dora", uid, "Dora"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("tid-dora", uid, "dora", sha256_token_hash("dora-tok")),
    )
    dora = {"Authorization": "Bearer dora-tok"}
    assert (await client.get(url, headers=dora)).status == 403
    r = await client.put(url, json={"level": "mentions"}, headers=dora)
    assert r.status == 403


async def test_members_carry_the_mention_token(client):
    conv_id = await _team_with_carol(client)
    r = await client.get(
        f"/api/conversations/{conv_id}/members", headers=_auth(client._bob_token)
    )
    tokens = {m["username"]: m["mention"] for m in await r.json()}
    assert tokens == {"pascal": "pascal", "bob": "bob", "carol": "carol"}


async def test_group_mention_rings_a_mentions_level_member_end_to_end(client):
    """Bob at level 'mentions': a plain message rings nothing; a message
    that @-mentions him gives one 'dm_mention' bell; Carol ('all') gets the
    ordinary bells and no mention bell."""
    conv_id = await _team_with_carol(client)
    await client.put(
        f"/api/conversations/{conv_id}/notif-prefs",
        json={"level": "mentions"},
        headers=_auth(client._bob_token),
    )
    send = f"/api/conversations/{conv_id}/messages"
    await client.post(
        send, json={"content": "lunch?"}, headers=_auth(client._admin_token)
    )
    assert await _bell(client, client._bob_token) == []
    await client.post(
        send, json={"content": "@bob lunch?"}, headers=_auth(client._admin_token)
    )
    notes = await _bell(client, client._bob_token)
    assert [(n["type"], n["title"]) for n in notes] == [
        ("dm_mention", "Pascal mentioned you in Team")
    ]
    carol = [n["type"] for n in await _bell(client, "carol-tok")]
    assert "dm_mention" not in carol and "dm_message" in carol


# ── Edit / delete a message ────────────────────────────────────────────────


async def _send(client, token: str, conv_id: str, content: str) -> str:
    r = await client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": content},
        headers=_auth(token),
    )
    assert r.status == 201
    return (await r.json())["id"]


async def _message(client, token: str, conv_id: str, mid: str) -> dict:
    r = await client.get(f"/api/conversations/{conv_id}/messages", headers=_auth(token))
    return next(m for m in await r.json() if m["id"] == mid)


async def test_sender_edits_own_message(client):
    conv_id = await _dm_with_bob(client)
    mid = await _send(client, client._admin_token, conv_id, "helo")
    r = await client.patch(
        f"/api/conversations/{conv_id}/messages/{mid}",
        json={"content": "hello"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    body = await r.json()
    assert body["id"] == mid and body["content"] == "hello"
    assert body["edited_at"]
    row = await _message(client, client._bob_token, conv_id, mid)
    assert row["content"] == "hello" and row["edited_at"]


async def test_only_the_sender_may_edit_or_delete(client):
    conv_id = await _dm_with_bob(client)
    mid = await _send(client, client._admin_token, conv_id, "mine")
    url = f"/api/conversations/{conv_id}/messages/{mid}"
    r = await client.patch(
        url, json={"content": "bob was here"}, headers=_auth(client._bob_token)
    )
    assert r.status == 403
    r = await client.delete(url, headers=_auth(client._bob_token))
    assert r.status == 403
    assert (await _message(client, client._admin_token, conv_id, mid))[
        "content"
    ] == "mine"


async def test_edit_rejects_empty_missing_and_wrong_conversation(client):
    conv_id = await _dm_with_bob(client)
    mid = await _send(client, client._admin_token, conv_id, "x")
    url = f"/api/conversations/{conv_id}/messages/{mid}"
    for body in ({"content": ""}, {}, {"content": 5}):
        r = await client.patch(url, json=body, headers=_auth(client._admin_token))
        assert r.status == 422, body
    r = await client.patch(
        f"/api/conversations/{conv_id}/messages/nope",
        json={"content": "y"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 404
    # A message id addressed under another conversation is not found there.
    other = await _team_with_carol(client)
    r = await client.patch(
        f"/api/conversations/{other}/messages/{mid}",
        json={"content": "y"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 404


async def test_sender_deletes_own_message_and_it_cannot_be_edited_after(client):
    conv_id = await _dm_with_bob(client)
    mid = await _send(client, client._admin_token, conv_id, "oops")
    url = f"/api/conversations/{conv_id}/messages/{mid}"
    r = await client.delete(url, headers=_auth(client._admin_token))
    assert r.status == 200
    assert (await _message(client, client._bob_token, conv_id, mid))["deleted"]
    r = await client.patch(
        url, json={"content": "back"}, headers=_auth(client._admin_token)
    )
    assert r.status == 422


async def test_edit_that_adds_a_mention_rings_only_that_member(client):
    """End to end over HTTP: bob (level 'mentions') gets a dm_mention bell
    when pascal edits a message to add @bob; editing again doesn't re-ring."""
    conv_id = await _team_with_carol(client)
    await client.put(
        f"/api/conversations/{conv_id}/notif-prefs",
        json={"level": "mentions"},
        headers=_auth(client._bob_token),
    )
    mid = await _send(client, client._admin_token, conv_id, "who's in?")
    assert await _bell(client, client._bob_token) == []
    url = f"/api/conversations/{conv_id}/messages/{mid}"
    await client.patch(
        url, json={"content": "who's in? @bob"}, headers=_auth(client._admin_token)
    )
    assert [n["type"] for n in await _bell(client, client._bob_token)] == ["dm_mention"]
    await client.post(
        f"/api/conversations/{conv_id}/read", headers=_auth(client._bob_token)
    )
    await client.patch(
        url, json={"content": "who's in, @bob?"}, headers=_auth(client._admin_token)
    )
    unread = await client.get(
        "/api/notifications/unread-count", headers=_auth(client._bob_token)
    )
    assert (await unread.json())["unread"] == 0


# ── GET /api/conversations/{id} ─────────────────────────────────────────


async def test_get_one_conversation_matches_its_list_row(client):
    """The single read ships exactly the caller's list row — group and 1:1,
    including the caller's own mute / level."""
    group_id = await _team_with_carol(client)
    dm_id = await _dm_with_bob(client)
    await client.put(
        f"/api/conversations/{dm_id}/mute",
        json={"duration": "1h"},
        headers=_auth(client._bob_token),
    )
    await client.put(
        f"/api/conversations/{group_id}/notif-prefs",
        json={"level": "mentions"},
        headers=_auth(client._bob_token),
    )
    for conv_id in (group_id, dm_id):
        for token in (client._bob_token, client._admin_token):
            r = await client.get(f"/api/conversations/{conv_id}", headers=_auth(token))
            assert r.status == 200
            assert await r.json() == await _row(client, token, conv_id)
    r = await client.get(
        f"/api/conversations/{group_id}", headers=_auth(client._bob_token)
    )
    body = await r.json()
    assert body["type"] == "group_dm"
    assert body["name"] == "Team"
    assert body["managed_here"] is True
    assert body["notif_level"] == "mentions"
    r = await client.get(
        f"/api/conversations/{dm_id}", headers=_auth(client._bob_token)
    )
    assert (await r.json())["muted_until"] is not None


async def test_get_one_conversation_unknown_is_404(client):
    r = await client.get(
        "/api/conversations/no-such-conversation",
        headers=_auth(client._admin_token),
    )
    assert r.status == 404


async def test_get_one_conversation_is_members_only(client):
    """An outsider, and a member who left the group, get 403."""
    group_id = await _team_with_carol(client)
    dm_id = await _dm_with_bob(client)
    r = await client.get(f"/api/conversations/{dm_id}", headers=_auth("carol-tok"))
    assert r.status == 403

    r = await client.post(
        f"/api/conversations/{group_id}/leave", headers=_auth("carol-tok")
    )
    assert r.status == 200
    r = await client.get(f"/api/conversations/{group_id}", headers=_auth("carol-tok"))
    assert r.status == 403


async def test_get_one_conversation_requires_auth(client):
    dm_id = await _dm_with_bob(client)
    r = await client.get(f"/api/conversations/{dm_id}")
    assert r.status == 401


async def test_get_one_conversation_hidden_by_a_personal_block_is_403(client):
    """A 1:1 the caller's personal block hides from their list is 403 here
    too; the blocked side (whose list still shows it) keeps reading it."""
    dm_id = await _dm_with_bob(client)
    r = await client.post(
        "/api/blocks",
        json={"user_id": client._bob_uid},
        headers=_auth(client._admin_token),
    )
    assert r.status in (200, 201, 204)
    r = await client.get(
        f"/api/conversations/{dm_id}", headers=_auth(client._admin_token)
    )
    assert r.status == 403
    r = await client.get(
        f"/api/conversations/{dm_id}", headers=_auth(client._bob_token)
    )
    assert r.status == 200


async def test_get_one_conversation_hidden_by_a_guardian_block_is_403(client):
    """§CP.F2: a guardian block separating bob (protected) from pascal hides
    their 1:1 from both lists — and from the single read, either side."""
    dm_id = await _dm_with_bob(client)
    db = client.app[_db_key]
    await db.enqueue(
        "UPDATE users SET child_protection_enabled=1 WHERE user_id=?",
        (client._bob_uid,),
    )
    await db.enqueue(
        "INSERT INTO cp_minor_blocks(minor_user_id, blocked_user_id, blocked_by)"
        " VALUES(?,?,?)",
        (client._bob_uid, client._admin_uid, client._admin_uid),
    )
    for token in (client._bob_token, client._admin_token):
        r = await client.get("/api/conversations", headers=_auth(token))
        assert dm_id not in {row["id"] for row in await r.json()}
        r = await client.get(f"/api/conversations/{dm_id}", headers=_auth(token))
        assert r.status == 403


async def test_get_one_conversation_is_not_found_for_the_household_chat(client):
    """The household chat is a system chat, not a DM: its metadata comes
    from ``GET /api/household/chat``; the DM item route never serves it."""
    r = await client.get("/api/household/chat", headers=_auth(client._bob_token))
    assert r.status == 200
    cid = (await r.json())["conversation_id"]
    r = await client.get(f"/api/conversations/{cid}", headers=_auth(client._bob_token))
    assert r.status == 404
