"""Final coverage push for routes/calls.py + routes/users.py error branches."""

from __future__ import annotations

from social_home.auth import sha256_token_hash

from .conftest import _auth


async def _seed_dm(
    client, conv_id: str, *, peer: str = "bob", peer_uid: str = "bob-uid"
) -> str:
    """Seed a second local user and a 1:1 DM. Returns bob's token."""
    db = client._db
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (peer, peer_uid, peer.title()),
    )
    raw = f"{peer}-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"t-{peer}", peer_uid, "t", sha256_token_hash(raw)),
    )
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) "
        "VALUES(?, 'dm', datetime('now'))",
        (conv_id,),
    )
    for u in ("admin", peer):
        await db.enqueue(
            "INSERT INTO conversation_members(conversation_id, username, joined_at) "
            "VALUES(?, ?, datetime('now'))",
            (conv_id, u),
        )
    return raw


# ─── /api/calls/{id}/answer error paths ──────────────────────────────────


async def test_calls_answer_unknown_call_404(client):
    r = await client.post(
        "/api/calls/missing/answer",
        json={"sdp_answer": "v=0\r\n"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_calls_answer_wrong_callee_403(client):
    await _seed_dm(client, "c-1")
    r = await client.post(
        "/api/calls",
        json={"conversation_id": "c-1", "sdp_offer": "v=0\r\n", "call_type": "audio"},
        headers=_auth(client._tok),
    )
    cid = (await r.json())["call_id"]
    # Caller (admin) tries to answer their own call → 403.
    r = await client.post(
        f"/api/calls/{cid}/answer",
        json={"sdp_answer": "v=0\r\n"},
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ─── /api/calls/{id}/ice success path ────────────────────────────────────


async def test_calls_ice_success_204(client):
    await _seed_dm(client, "c-ice")
    r = await client.post(
        "/api/calls",
        json={"conversation_id": "c-ice", "sdp_offer": "v=0\r\n", "call_type": "audio"},
        headers=_auth(client._tok),
    )
    cid = (await r.json())["call_id"]
    r = await client.post(
        f"/api/calls/{cid}/ice",
        json={"candidate": {"candidate": "candidate:1 1 UDP 1 1.2.3.4 1 typ host"}},
        headers=_auth(client._tok),
    )
    assert r.status == 204


# ─── /api/calls/{id}/hangup forbidden ────────────────────────────────────


async def test_calls_hangup_non_participant_403(client):
    await _seed_dm(client, "c-ho")
    db = client._db
    # Intruder user + token — not in the DM.
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("intruder", "intruder-id", "X"),
    )
    raw = "intruder-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-int", "intruder-id", "t", sha256_token_hash(raw)),
    )
    r = await client.post(
        "/api/calls",
        json={"conversation_id": "c-ho", "sdp_offer": "v=0\r\n", "call_type": "audio"},
        headers=_auth(client._tok),
    )
    cid = (await r.json())["call_id"]
    r = await client.post(f"/api/calls/{cid}/hangup", headers=_auth(raw))
    assert r.status == 403


# ─── ice_servers with TURN configured ────────────────────────────────────


async def test_ice_servers_includes_turn_when_configured(client):
    from social_home.app_keys import config_key
    import dataclasses as _dc

    cfg = client.server.app[config_key]
    new_cfg = _dc.replace(
        cfg,
        webrtc_turn_url="turn:turn.example.com:3478",
        webrtc_turn_user="alice",
        webrtc_turn_cred="s3cret",
    )
    client.server.app[config_key] = new_cfg
    r = await client.get("/api/webrtc/ice_servers", headers=_auth(client._tok))
    body = await r.json()
    has_turn = any(
        any("turn" in u for u in (s.get("urls") or [])) for s in body["ice_servers"]
    )
    assert has_turn
