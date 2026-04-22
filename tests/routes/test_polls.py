"""Tests for poll + schedule-poll routes (§9)."""

from __future__ import annotations


from .conftest import _auth


async def _seed_poll(client, *, post_id: str = "p-1", closed: bool = False):
    db = client._db
    # Feed post authored by the test admin user.
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content, created_at) "
        "VALUES(?, ?, 'poll', 'Pizza?', datetime('now'))",
        (post_id, client._uid),
    )
    await db.enqueue(
        "INSERT INTO polls(post_id, question, closed) VALUES(?, ?, ?)",
        (post_id, "Pizza?", 1 if closed else 0),
    )
    await db.enqueue(
        "INSERT INTO poll_options(id, post_id, text, position) VALUES(?,?,?,?)",
        ("opt-y", post_id, "Yes", 0),
    )
    await db.enqueue(
        "INSERT INTO poll_options(id, post_id, text, position) VALUES(?,?,?,?)",
        ("opt-n", post_id, "No", 1),
    )


async def test_poll_vote_updates_summary(client):
    await _seed_poll(client)
    r = await client.post(
        "/api/posts/p-1/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    counts = {o["id"]: o["vote_count"] for o in data["options"]}
    assert counts == {"opt-y": 1, "opt-n": 0}


async def test_poll_vote_replaces_previous_choice(client):
    await _seed_poll(client)
    await client.post(
        "/api/posts/p-1/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    r = await client.post(
        "/api/posts/p-1/poll/vote",
        json={"option_id": "opt-n"},
        headers=_auth(client._tok),
    )
    data = await r.json()
    counts = {o["id"]: o["vote_count"] for o in data["options"]}
    assert counts == {"opt-y": 0, "opt-n": 1}


async def test_poll_vote_unknown_option_422(client):
    await _seed_poll(client)
    r = await client.post(
        "/api/posts/p-1/poll/vote",
        json={"option_id": "ghost"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_poll_vote_unknown_poll_404(client):
    r = await client.post(
        "/api/posts/p-missing/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_poll_vote_closed_returns_409(client):
    await _seed_poll(client, closed=True)
    r = await client.post(
        "/api/posts/p-1/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    assert r.status == 409


async def test_poll_retract_vote(client):
    await _seed_poll(client)
    await client.post(
        "/api/posts/p-1/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    r = await client.delete(
        "/api/posts/p-1/poll/vote",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    counts = {o["id"]: o["vote_count"] for o in data["options"]}
    assert counts == {"opt-y": 0, "opt-n": 0}


async def test_poll_close_by_author(client):
    await _seed_poll(client)
    r = await client.post(
        "/api/posts/p-1/poll/close",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["closed"] is True


async def test_poll_close_by_non_author_forbidden(client):
    # Different author on the post.
    db = client._db
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content, created_at) "
        "VALUES('p-2', 'other-uid', 'poll', 'Q?', datetime('now'))",
    )
    await db.enqueue(
        "INSERT INTO polls(post_id, question, closed) VALUES('p-2', 'Q?', 0)",
    )
    r = await client.post(
        "/api/posts/p-2/poll/close",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_poll_summary_after_votes(client):
    await _seed_poll(client)
    r = await client.get(
        "/api/posts/p-1/poll",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    assert data["question"] == "Pizza?"
    assert [o["id"] for o in data["options"]] == ["opt-y", "opt-n"]
    # New shape fields — surface allow_multiple + user_vote + total.
    assert data["allow_multiple"] is False
    assert data["user_vote"] == []
    assert data["total_votes"] == 0


async def test_poll_create_via_post(client):
    # Seed only the parent feed post; POST /poll creates + options.
    await client._db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content, created_at) "
        "VALUES('p-new', ?, 'poll', 'Pick one', datetime('now'))",
        (client._uid,),
    )
    r = await client.post(
        "/api/posts/p-new/poll",
        json={
            "question": "What's for dinner?",
            "options": ["Pizza", "Tacos", "Sushi"],
            "allow_multiple": False,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["question"] == "What's for dinner?"
    assert [o["text"] for o in body["options"]] == ["Pizza", "Tacos", "Sushi"]
    assert body["allow_multiple"] is False
    assert body["closed"] is False


async def test_poll_create_too_few_options_422(client):
    await client._db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content, created_at) "
        "VALUES('p-bad', ?, 'poll', 'Q', datetime('now'))",
        (client._uid,),
    )
    r = await client.post(
        "/api/posts/p-bad/poll",
        json={"question": "Q", "options": ["Only one"]},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_poll_allow_multiple_toggle(client):
    # Seed manually with allow_multiple=1.
    await client._db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content, created_at) "
        "VALUES('p-multi', ?, 'poll', 'Pick multi', datetime('now'))",
        (client._uid,),
    )
    await client._db.enqueue(
        "INSERT INTO polls(post_id, question, closed, allow_multiple)"
        " VALUES('p-multi', 'Pick', 0, 1)",
    )
    await client._db.enqueue(
        "INSERT INTO poll_options(id, post_id, text, position) "
        "VALUES('mo-a', 'p-multi', 'A', 0)",
    )
    await client._db.enqueue(
        "INSERT INTO poll_options(id, post_id, text, position) "
        "VALUES('mo-b', 'p-multi', 'B', 1)",
    )
    # Select A, then B — both should stick since multi is allowed.
    await client.post(
        "/api/posts/p-multi/poll/vote",
        json={"option_id": "mo-a"},
        headers=_auth(client._tok),
    )
    r = await client.post(
        "/api/posts/p-multi/poll/vote",
        json={"option_id": "mo-b"},
        headers=_auth(client._tok),
    )
    data = await r.json()
    counts = {o["id"]: o["vote_count"] for o in data["options"]}
    assert counts == {"mo-a": 1, "mo-b": 1}
    assert set(data["user_vote"]) == {"mo-a", "mo-b"}
    # Re-click A — toggles it off, B still set.
    r2 = await client.post(
        "/api/posts/p-multi/poll/vote",
        json={"option_id": "mo-a"},
        headers=_auth(client._tok),
    )
    data2 = await r2.json()
    counts2 = {o["id"]: o["vote_count"] for o in data2["options"]}
    assert counts2 == {"mo-a": 0, "mo-b": 1}
    assert data2["user_vote"] == ["mo-b"]


async def test_poll_bad_json_400(client):
    await _seed_poll(client)
    r = await client.post(
        "/api/posts/p-1/poll/vote",
        data="oops",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_poll_missing_option_id_422(client):
    await _seed_poll(client)
    r = await client.post(
        "/api/posts/p-1/poll/vote",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── Schedule polls ────────────────────────────────────────────────────────


async def _seed_schedule_post(client, post_id: str = "sp-1"):
    """Create the parent feed post that a schedule poll attaches to."""
    await client._db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content, created_at) "
        "VALUES(?, ?, 'schedule', ?, datetime('now'))",
        (post_id, client._uid, "When?"),
    )


async def _create_poll(client, post_id: str = "sp-1", slots: list | None = None):
    slots = slots or [
        {"id": "slot-A", "slot_date": "2026-05-01"},
        {"id": "slot-B", "slot_date": "2026-05-02", "start_time": "18:00"},
    ]
    return await client.post(
        f"/api/posts/{post_id}/schedule-poll",
        json={"title": "Pizza night", "slots": slots},
        headers=_auth(client._tok),
    )


async def test_schedule_create_returns_shape(client):
    await _seed_schedule_post(client)
    r = await _create_poll(client)
    assert r.status == 201
    data = await r.json()
    assert data["title"] == "Pizza night"
    assert [s["id"] for s in data["slots"]] == ["slot-A", "slot-B"]
    assert data["responses"] == []
    assert data["finalized_slot_id"] is None
    assert data["closed"] is False


async def test_schedule_respond_and_summary(client):
    await _seed_schedule_post(client)
    await _create_poll(client)
    r = await client.post(
        "/api/schedule-polls/sp-1/respond",
        json={"slot_id": "slot-A", "response": "yes"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    # Per-user response rows, not aggregate counts.
    yes_rows = [
        x
        for x in data["responses"]
        if x["slot_id"] == "slot-A" and x["availability"] == "yes"
    ]
    assert len(yes_rows) == 1
    assert yes_rows[0]["user_id"] == client._uid


async def test_schedule_respond_updates_existing(client):
    await _seed_schedule_post(client, "sp-2")
    await _create_poll(
        client,
        "sp-2",
        [
            {"id": "slot-A", "slot_date": "2026-05-01"},
        ],
    )
    await client.post(
        "/api/schedule-polls/sp-2/respond",
        json={"slot_id": "slot-A", "response": "yes"},
        headers=_auth(client._tok),
    )
    r = await client.post(
        "/api/schedule-polls/sp-2/respond",
        json={"slot_id": "slot-A", "response": "no"},
        headers=_auth(client._tok),
    )
    data = await r.json()
    responses = [x for x in data["responses"] if x["slot_id"] == "slot-A"]
    assert len(responses) == 1
    assert responses[0]["availability"] == "no"


async def test_schedule_finalize_sets_winner_and_closes(client):
    await _seed_schedule_post(client, "sp-3")
    await _create_poll(client, "sp-3")
    r = await client.post(
        "/api/schedule-polls/sp-3/finalize",
        json={"slot_id": "slot-B"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    assert data["finalized_slot_id"] == "slot-B"
    assert data["closed"] is True


async def test_schedule_finalize_non_author_403(client):
    from socialhome.auth import sha256_token_hash

    await _seed_schedule_post(client, "sp-4")
    await _create_poll(client, "sp-4")
    # Seed a second user who isn't the author.
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('outsider','out-id','Out',0)",
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('to1','out-id','t',?)",
        (sha256_token_hash("out-tok"),),
    )
    r = await client.post(
        "/api/schedule-polls/sp-4/finalize",
        json={"slot_id": "slot-A"},
        headers={"Authorization": "Bearer out-tok"},
    )
    assert r.status == 403


async def test_schedule_respond_invalid_response_422(client):
    await _seed_schedule_post(client, "sp-5")
    await _create_poll(client, "sp-5")
    r = await client.post(
        "/api/schedule-polls/sp-5/respond",
        json={"slot_id": "slot-A", "response": "unsure"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_schedule_respond_missing_fields_422(client):
    await _seed_schedule_post(client, "sp-6")
    await _create_poll(client, "sp-6")
    r = await client.post(
        "/api/schedule-polls/sp-6/respond",
        json={"slot_id": "slot-A"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_schedule_retract_deletes_response(client):
    await _seed_schedule_post(client, "sp-7")
    await _create_poll(client, "sp-7")
    await client.post(
        "/api/schedule-polls/sp-7/respond",
        json={"slot_id": "slot-A", "response": "yes"},
        headers=_auth(client._tok),
    )
    r = await client.delete(
        "/api/schedule-polls/sp-7/slots/slot-A/response",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    yes_for_slot = [
        x
        for x in data["responses"]
        if x["slot_id"] == "slot-A" and x["availability"] == "yes"
    ]
    assert yes_for_slot == []
