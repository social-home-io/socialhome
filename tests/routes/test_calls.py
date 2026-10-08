"""HTTP tests for /api/calls + /api/conversations/{id}/calls (spec §26).

Covers the happy path (initiate → answer → ICE → hangup), conversation
membership guards (IDOR defence), call-history listing, join + quality
endpoints, and the ICE-server config route.
"""

from __future__ import annotations

from socialhome.app_keys import call_signaling_service_key
from socialhome.auth import sha256_token_hash
from socialhome.domain.federation import FederationEventType

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


# ─── Callee side of a cross-household call ───────────────────────────────


async def _seed_remote_carol(client, conv_id: str) -> None:
    """Carol lives on the paired ``carol-house`` and is in *conv_id*."""
    db = client._db
    await db.enqueue(
        "INSERT INTO remote_instances"
        "(id, display_name, remote_identity_pk, key_self_to_remote,"
        " key_remote_to_self, remote_inbox_url, local_inbox_id, status,"
        " source) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "carol-house",
            "Carol's",
            "00" * 32,
            "k1",
            "k2",
            "https://carol/wh",
            "wh-carol",
            "confirmed",
            "manual",
        ),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES(?,?,?,?)",
        ("carol-uid", "carol-house", "carol", "Carol"),
    )
    await db.enqueue(
        "INSERT INTO conversation_remote_members"
        "(conversation_id, instance_id, remote_username) VALUES(?,?,?)",
        (conv_id, "carol-house", "carol"),
    )


class _InboundEvent:
    def __init__(self, event_type, from_instance, payload):
        self.event_type = event_type
        self.from_instance = from_instance
        self.payload = payload


async def test_callee_can_answer_ice_and_hangup_a_federated_call(client, monkeypatch):
    """Bob (local) is rung by a ``CALL_OFFER`` from carol's household. Every
    callee-side route authorises against the persisted ``call_sessions``
    row — before it was written on the inbound offer, all of them 404'd
    and a cross-household call could ring but never be answered."""
    bob_tok = await _seed_bob(client, conv_id="conv-x")
    await _seed_remote_carol(client, "conv-x")
    svc = client.app[call_signaling_service_key]
    sent = []

    async def _send_event(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type, payload))

    monkeypatch.setattr(type(svc._federation), "send_event", _send_event)
    await svc.handle_federated_signal(
        _InboundEvent(
            FederationEventType.CALL_OFFER,
            "carol-house",
            {
                "call_id": "call-fed-1",
                "conversation_id": "conv-x",
                "from_user": "carol-uid",
                "to_user": "bob-uid",
                "call_type": "video",
            },
        )
    )

    r = await client.post(
        "/api/calls/call-fed-1/answer",
        json={"sdp_answer": "v=0\r\nans\r\n"},
        headers=_auth(bob_tok),
    )
    assert r.status == 200
    assert [(t, e) for t, e, _p in sent] == [
        ("carol-house", FederationEventType.CALL_ANSWER),
    ]
    r = await client.post(
        "/api/calls/call-fed-1/answer",
        json={"sdp_answer": "v=0\r\nagain\r\n"},
        headers=_auth(bob_tok),
    )
    assert r.status == 409  # a second device can't re-answer
    r = await client.post(
        "/api/calls/call-fed-1/ice",
        json={"candidate": {"candidate": "x", "sdpMid": "0"}},
        headers=_auth(bob_tok),
    )
    assert r.status == 204
    r = await client.post("/api/calls/call-fed-1/hangup", headers=_auth(bob_tok))
    assert r.status == 204
    assert sent[-1][:2] == ("carol-house", FederationEventType.CALL_HANGUP)
    r = await client.get("/api/conversations/conv-x/calls", headers=_auth(bob_tok))
    [row] = (await r.json())["calls"]
    assert row["status"] == "ended"
    assert row["call_type"] == "video"


async def test_federated_offer_for_foreign_conversation_is_not_stored(client):
    """An offer naming a conversation the sending household isn't in
    neither rings nor leaves a row behind (the callee's routes 404)."""
    bob_tok = await _seed_bob(client, conv_id="conv-y")
    svc = client.app[call_signaling_service_key]
    await svc.handle_federated_signal(
        _InboundEvent(
            FederationEventType.CALL_OFFER,
            "mallory-house",
            {
                "call_id": "call-fed-2",
                "conversation_id": "conv-y",
                "from_user": "mallory-uid",
                "to_user": "bob-uid",
                "call_type": "audio",
            },
        )
    )
    r = await client.post(
        "/api/calls/call-fed-2/answer",
        json={"sdp_answer": "v=0\r\n"},
        headers=_auth(bob_tok),
    )
    assert r.status == 404


# ─── group-call mesh (§26.4) ──────────────────────────────────────────


async def _group_conv(client) -> tuple[str, str]:
    bob_tok = await _seed_bob(client, conv_id="conv-g")
    await client._db.enqueue(
        "UPDATE conversations SET type='group_dm' WHERE id=?", ("conv-g",)
    )
    carol_tok = await _seed_carol(client, "conv-g")
    return bob_tok, carol_tok


async def _group_call(client) -> tuple[str, str, str]:
    """admin calls bob + carol in a group DM with one offer each."""
    bob_tok, carol_tok = await _group_conv(client)
    r = await client.post(
        "/api/calls",
        json={
            "conversation_id": "conv-g",
            "call_type": "video",
            "sdp_offers": {"bob-uid": "offer-bob", "carol-uid": "offer-carol"},
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["participants"] == sorted([client._uid, "bob-uid", "carol-uid"])
    return body["call_id"], bob_tok, carol_tok


async def test_group_call_with_per_callee_offers_and_mesh_signalling(client):
    cid, bob_tok, carol_tok = await _group_call(client)
    # Both callees answer the caller — the second is not a 409.
    for tok in (bob_tok, carol_tok):
        r = await client.post(
            f"/api/calls/{cid}/answer", json={"sdp_answer": "ans"}, headers=_auth(tok)
        )
        assert r.status == 200
    # bob opens his leg to carol; carol answers it, addressed to bob.
    r = await client.post(
        f"/api/calls/{cid}/join",
        json={"sdp_offers": {"carol-uid": "leg-offer"}},
        headers=_auth(bob_tok),
    )
    assert (await r.json())["joined"] == ["carol-uid"]
    r = await client.post(
        f"/api/calls/{cid}/answer",
        json={"sdp_answer": "leg-ans", "to_user": "bob-uid"},
        headers=_auth(carol_tok),
    )
    assert r.status == 200
    r = await client.post(
        f"/api/calls/{cid}/ice",
        json={"candidate": {"candidate": "c"}, "to_user": "bob-uid"},
        headers=_auth(carol_tok),
    )
    assert r.status == 204
    # bob leaving doesn't end the call for the other two.
    r = await client.post(f"/api/calls/{cid}/hangup", json={}, headers=_auth(bob_tok))
    assert r.status == 204
    svc = client.server.app[call_signaling_service_key]
    assert svc.get_call(cid).present == {client._uid, "carol-uid"}


async def test_group_call_ice_and_answer_targets_must_be_participants(client):
    cid, bob_tok, _carol_tok = await _group_call(client)
    r = await client.post(
        f"/api/calls/{cid}/ice",
        json={"candidate": {}, "to_user": "stranger-uid"},
        headers=_auth(bob_tok),
    )
    assert r.status == 403
    r = await client.post(
        f"/api/calls/{cid}/answer",
        json={"sdp_answer": "x", "to_user": "stranger-uid"},
        headers=_auth(bob_tok),
    )
    assert r.status == 403


async def test_initiate_rejects_a_non_object_sdp_offers(client):
    await _seed_bob(client)
    r = await client.post(
        "/api/calls",
        json={"conversation_id": "conv-ab", "sdp_offers": ["x"]},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_initiate_refuses_a_call_over_the_mesh_cap(client, monkeypatch):
    monkeypatch.setattr("socialhome.services.call_service.MAX_CALL_PARTICIPANTS", 2)
    await _group_conv(client)
    r = await client.post(
        "/api/calls",
        json={"conversation_id": "conv-g", "sdp_offer": "v=0\r\n"},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    assert (await r.json())["error"] == "too_many_participants"


async def test_a_mesh_legs_worth_of_ice_is_not_rate_limited(client):
    """Regression: ICE fell under the broad ``/api/calls`` 10/min limit, so
    a group call's legs (several candidates each) got 429s within seconds
    and never connected."""
    cid, bob_tok, _carol_tok = await _group_call(client)
    for i in range(40):
        r = await client.post(
            f"/api/calls/{cid}/ice",
            json={"candidate": {"candidate": f"c{i}"}, "to_user": "carol-uid"},
            headers=_auth(bob_tok),
        )
        assert r.status == 204, (i, r.status)


async def test_a_callees_mesh_signalling_is_not_starved_by_quality_samples(client):
    """Regression (live three-household group call): ``answer``, ``join``,
    ``quality`` and ``ice-servers`` all shared the ``/api/calls`` 10/min
    bucket. A browser samples call quality every 10 s, so a callee who had
    been in a call within the last minute got 429 on ``POST /join`` — the
    callee-to-callee leg was silently never offered."""
    cid, bob_tok, _carol_tok = await _group_call(client)
    bob = _auth(bob_tok)
    # A minute of quality samples from the call Bob was just in (this one
    # stands in for it) plus two page loads' ICE-server fetches.
    for _ in range(6):
        r = await client.post(
            f"/api/calls/{cid}/quality", json={"rtt_ms": 1}, headers=bob
        )
        assert r.status == 204
    for _ in range(2):
        r = await client.get("/api/calls/ice-servers", headers=bob)
        assert r.status == 200
    # Then the whole callee side of a group call: answer the ring, offer
    # the leg to Carol, answer a peer's leg.
    r = await client.post(
        f"/api/calls/{cid}/answer", json={"sdp_answer": "ans"}, headers=bob
    )
    assert r.status == 200
    r = await client.post(
        f"/api/calls/{cid}/join",
        json={"sdp_offers": {"carol-uid": "leg-offer"}},
        headers=bob,
    )
    assert r.status == 200, r.status
    r = await client.post(
        f"/api/calls/{cid}/answer",
        json={"sdp_answer": "leg-ans", "to_user": "carol-uid"},
        headers=bob,
    )
    assert r.status == 200, r.status


# ─── System chats: no calls ───────────────────────────────────────────────


async def test_no_call_starts_in_the_household_chat(client):
    await _seed_bob(client)
    r = await client.get("/api/household/chat", headers=_auth(client._tok))
    cid = (await r.json())["conversation_id"]
    r = await client.post(
        "/api/calls",
        json={"conversation_id": cid, "sdp_offer": "v=0\r\n", "call_type": "audio"},
        headers=_auth(client._tok),
    )
    assert r.status == 409
    assert (await r.json())["error"] == "calls_unavailable"
