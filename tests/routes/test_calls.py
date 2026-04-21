"""HTTP tests for /api/calls + /api/conversations/{id}/calls (spec §26).

Covers the happy path (initiate → answer → ICE → hangup), conversation
membership guards (IDOR defence), call-history listing, join + quality
endpoints, and the ICE-server config route.
"""

from __future__ import annotations

from social_home.auth import sha256_token_hash

from .conftest import _auth


# ── Helpers ──────────────────────────────────────────────────────────


async def _seed_bob(client, *, conv_id: str = "conv-ab") -> str:
    """Seed a second local user + a 1:1 DM between admin and Bob.

    Returns the bob API token string.
    """
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("bob", "bob-uid", "Bob"),
    )
    raw = "bob-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-bob", "bob-uid", "t", sha256_token_hash(raw)),
    )
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) "
        "VALUES(?, 'dm', datetime('now'))",
        (conv_id,),
    )
    await db.enqueue(
        "INSERT INTO conversation_members(conversation_id, username, joined_at) "
        "VALUES(?, ?, datetime('now'))",
        (conv_id, "admin"),
    )
    await db.enqueue(
        "INSERT INTO conversation_members(conversation_id, username, joined_at) "
        "VALUES(?, ?, datetime('now'))",
        (conv_id, "bob"),
    )
    return raw


async def _seed_carol(client, conv_id: str) -> str:
    """Add Carol + include her in an existing group DM."""
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("carol", "carol-uid", "Carol"),
    )
    raw = "carol-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-carol", "carol-uid", "t", sha256_token_hash(raw)),
    )
    await db.enqueue(
        "INSERT INTO conversation_members(conversation_id, username, joined_at) "
        "VALUES(?, ?, datetime('now'))",
        (conv_id, "carol"),
    )
    return raw


# ─── /api/webrtc/ice_servers ──────────────────────────────────────────────


async def test_ice_servers_requires_auth(client):
    r = await client.get("/api/webrtc/ice_servers")
    assert r.status == 401


async def test_ice_servers_returns_stun(client):
    r = await client.get("/api/webrtc/ice_servers", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert isinstance(body["ice_servers"], list)
    urls = [s.get("urls") for s in body["ice_servers"]]
    assert any("stun" in (u[0] if isinstance(u, list) else u) for u in urls)


# ─── POST /api/calls (validation + membership) ────────────────────────────


async def test_initiate_call_requires_conversation_id(client):
    r = await client.post(
        "/api/calls",
        json={"sdp_offer": "v=0\r\n"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_initiate_call_requires_offer(client):
    await _seed_bob(client)
    r = await client.post(
        "/api/calls",
        json={"conversation_id": "conv-ab"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_initiate_call_non_member_is_403(client):
    # Bob exists but admin is NOT added to this conversation.
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("bob", "bob-uid", "Bob"),
    )
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) "
        "VALUES(?, 'dm', datetime('now'))",
        ("conv-bob-only",),
    )
    await db.enqueue(
        "INSERT INTO conversation_members(conversation_id, username, joined_at) "
        "VALUES(?, 'bob', datetime('now'))",
        ("conv-bob-only",),
    )
    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-bob-only",
            "sdp_offer": "v=0\r\n",
            "call_type": "audio",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_initiate_call_happy_path_returns_call_id(client):
    await _seed_bob(client)
    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-ab",
            "sdp_offer": "v=0\r\nofr\r\n",
            "call_type": "video",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["call_id"].startswith("call-")
    assert body["status"] == "ringing"
    assert body["conversation_id"] == "conv-ab"


# ─── Full lifecycle: initiate → answer → ICE → hangup ────────────────────


async def test_call_lifecycle_initiate_answer_ice_hangup(client):
    bob_tok = await _seed_bob(client)
    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-ab",
            "sdp_offer": "v=0\r\n",
            "call_type": "audio",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    cid = (await r.json())["call_id"]

    r = await client.post(
        f"/api/calls/{cid}/answer",
        json={"sdp_answer": "v=0\r\nans\r\n"},
        headers=_auth(bob_tok),
    )
    assert r.status == 200
    assert (await r.json())["status"] == "in_progress"

    r = await client.post(
        f"/api/calls/{cid}/ice",
        json={"candidate": {"candidate": "x", "sdpMid": "0"}},
        headers=_auth(client._tok),
    )
    assert r.status == 204

    r = await client.post(
        f"/api/calls/{cid}/hangup",
        headers=_auth(client._tok),
    )
    assert r.status == 204


# ─── Per-route membership guards ─────────────────────────────────────────


async def test_answer_from_non_member_is_403(client):
    await _seed_bob(client)
    db = client._db
    # Carol exists + has a token but is NOT in conv-ab.
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("carol", "carol-uid", "Carol"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-c", "carol-uid", "t", sha256_token_hash("carol-tok")),
    )
    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-ab",
            "sdp_offer": "v=0\r\n",
            "call_type": "audio",
        },
        headers=_auth(client._tok),
    )
    cid = (await r.json())["call_id"]
    r = await client.post(
        f"/api/calls/{cid}/answer",
        json={"sdp_answer": "v=0\r\n"},
        headers=_auth("carol-tok"),
    )
    assert r.status == 403


async def test_answer_unknown_call_returns_404(client):
    r = await client.post(
        "/api/calls/missing/answer",
        json={"sdp_answer": "v=0\r\n"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_ice_unknown_call_returns_404(client):
    r = await client.post(
        "/api/calls/missing/ice",
        json={"candidate": {}},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_decline_by_caller_is_forbidden(client):
    await _seed_bob(client)
    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-ab",
            "sdp_offer": "v=0\r\n",
            "call_type": "audio",
        },
        headers=_auth(client._tok),
    )
    cid = (await r.json())["call_id"]
    r = await client.post(
        f"/api/calls/{cid}/decline",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_decline_by_callee_is_ok(client):
    bob_tok = await _seed_bob(client)
    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-ab",
            "sdp_offer": "v=0\r\n",
            "call_type": "audio",
        },
        headers=_auth(client._tok),
    )
    cid = (await r.json())["call_id"]
    r = await client.post(
        f"/api/calls/{cid}/decline",
        headers=_auth(bob_tok),
    )
    assert r.status == 204


# ─── /api/calls/active ────────────────────────────────────────────────────


async def test_active_calls_lists_participant(client):
    await _seed_bob(client)
    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-ab",
            "sdp_offer": "v=0\r\n",
            "call_type": "audio",
        },
        headers=_auth(client._tok),
    )
    cid = (await r.json())["call_id"]
    r = await client.get("/api/calls/active", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert any(c["call_id"] == cid for c in body)


# ─── /api/conversations/{id}/calls (history) ─────────────────────────────


async def test_conversation_history_requires_member(client):
    await _seed_bob(client)
    # Carol isn't in conv-ab → 403.
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("carol", "carol-uid", "Carol"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-c", "carol-uid", "t", sha256_token_hash("carol-tok")),
    )
    r = await client.get(
        "/api/conversations/conv-ab/calls",
        headers=_auth("carol-tok"),
    )
    assert r.status == 403


async def test_conversation_history_lists_past_calls(client):
    await _seed_bob(client)
    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-ab",
            "sdp_offer": "v=0\r\n",
            "call_type": "audio",
        },
        headers=_auth(client._tok),
    )
    cid = (await r.json())["call_id"]
    await client.post(
        f"/api/calls/{cid}/hangup",
        headers=_auth(client._tok),
    )
    r = await client.get(
        "/api/conversations/conv-ab/calls",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert any(c["call_id"] == cid for c in body["calls"])


# ─── /api/calls/{id}/join (group late-join) ──────────────────────────────


async def test_join_call_by_conversation_member(client):
    await _seed_bob(client, conv_id="conv-abc")
    # Mutate to a group DM + add carol.
    db = client._db
    await db.enqueue(
        "UPDATE conversations SET type='group_dm' WHERE id=?",
        ("conv-abc",),
    )
    carol_tok = await _seed_carol(client, "conv-abc")

    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-abc",
            "sdp_offer": "v=0\r\n",
            "call_type": "audio",
        },
        headers=_auth(client._tok),
    )
    cid = (await r.json())["call_id"]
    r = await client.post(
        f"/api/calls/{cid}/join",
        json={"sdp_offers": {client._uid: "offer-to-admin", "bob-uid": "offer-to-bob"}},
        headers=_auth(carol_tok),
    )
    assert r.status == 200
    body = await r.json()
    assert set(body["joined"]) == {client._uid, "bob-uid"}


async def test_join_call_non_member_forbidden(client):
    await _seed_bob(client)
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("eve", "eve-uid", "Eve"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-e", "eve-uid", "t", sha256_token_hash("eve-tok")),
    )
    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-ab",
            "sdp_offer": "v=0\r\n",
            "call_type": "audio",
        },
        headers=_auth(client._tok),
    )
    cid = (await r.json())["call_id"]
    r = await client.post(
        f"/api/calls/{cid}/join",
        json={"sdp_offers": {client._uid: "x"}},
        headers=_auth("eve-tok"),
    )
    assert r.status == 403


async def test_join_call_unknown_call_404(client):
    r = await client.post(
        "/api/calls/no-such/join",
        json={"sdp_offers": {}},
        headers=_auth(client._tok),
    )
    assert r.status == 404


# ─── /api/calls/{id}/quality ─────────────────────────────────────────────


async def test_quality_post_and_list(client):
    await _seed_bob(client)
    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-ab",
            "sdp_offer": "v=0\r\n",
            "call_type": "audio",
        },
        headers=_auth(client._tok),
    )
    cid = (await r.json())["call_id"]
    r = await client.post(
        f"/api/calls/{cid}/quality",
        json={"rtt_ms": 42, "jitter_ms": 3, "loss_pct": 0.5, "audio_bitrate": 32000},
        headers=_auth(client._tok),
    )
    assert r.status == 204
    r = await client.get(
        f"/api/calls/{cid}/quality",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert len(body["samples"]) == 1
    assert body["samples"][0]["rtt_ms"] == 42


async def test_quality_guard_non_member_is_403(client):
    await _seed_bob(client)
    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-ab",
            "sdp_offer": "v=0\r\n",
            "call_type": "audio",
        },
        headers=_auth(client._tok),
    )
    cid = (await r.json())["call_id"]
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("eve", "eve-uid", "Eve"),
    )
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-e", "eve-uid", "t", sha256_token_hash("eve-tok")),
    )
    r = await client.post(
        f"/api/calls/{cid}/quality",
        json={"rtt_ms": 42},
        headers=_auth("eve-tok"),
    )
    assert r.status == 403
