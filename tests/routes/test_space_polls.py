"""Tests for space-scoped poll + schedule-poll routes (§9 / §13)."""

from __future__ import annotations

from socialhome.auth import sha256_token_hash

from .conftest import _auth


async def _seed_space(client, space_id: str = "sp-polls"):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key, space_type) "
        "VALUES(?, 'Polls', 'iid', 'admin', ?, 'household')",
        (space_id, "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'admin')",
        (space_id, client._uid),
    )


async def _seed_post(
    client,
    *,
    space_id: str = "sp-polls",
    post_id: str = "sp-post-1",
    post_type: str = "poll",
    author: str | None = None,
):
    db = client._db
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content, created_at) "
        "VALUES(?, ?, ?, ?, 'Pizza?', datetime('now'))",
        (post_id, space_id, author or client._uid, post_type),
    )


async def _seed_poll(
    client,
    *,
    space_id: str = "sp-polls",
    post_id: str = "sp-post-1",
    closed: bool = False,
):
    await _seed_post(client, space_id=space_id, post_id=post_id)
    db = client._db
    await db.enqueue(
        "INSERT INTO space_polls(post_id, question, closed) VALUES(?, ?, ?)",
        (post_id, "Pizza?", 1 if closed else 0),
    )
    await db.enqueue(
        "INSERT INTO space_poll_options(id, post_id, text, position) VALUES(?,?,?,?)",
        ("opt-y", post_id, "Yes", 0),
    )
    await db.enqueue(
        "INSERT INTO space_poll_options(id, post_id, text, position) VALUES(?,?,?,?)",
        ("opt-n", post_id, "No", 1),
    )


# ─── Reply polls ────────────────────────────────────────────────────────


async def test_space_poll_vote_updates_summary(client):
    await _seed_space(client)
    await _seed_poll(client)
    r = await client.post(
        "/api/spaces/sp-polls/posts/sp-post-1/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    assert data["space_id"] == "sp-polls"
    counts = {o["id"]: o["vote_count"] for o in data["options"]}
    assert counts == {"opt-y": 1, "opt-n": 0}


async def test_space_poll_vote_replaces_previous_choice(client):
    await _seed_space(client)
    await _seed_poll(client)
    await client.post(
        "/api/spaces/sp-polls/posts/sp-post-1/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    r = await client.post(
        "/api/spaces/sp-polls/posts/sp-post-1/poll/vote",
        json={"option_id": "opt-n"},
        headers=_auth(client._tok),
    )
    data = await r.json()
    counts = {o["id"]: o["vote_count"] for o in data["options"]}
    assert counts == {"opt-y": 0, "opt-n": 1}


async def test_space_poll_retract_vote(client):
    await _seed_space(client)
    await _seed_poll(client)
    await client.post(
        "/api/spaces/sp-polls/posts/sp-post-1/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    r = await client.delete(
        "/api/spaces/sp-polls/posts/sp-post-1/poll/vote",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    counts = {o["id"]: o["vote_count"] for o in data["options"]}
    assert counts == {"opt-y": 0, "opt-n": 0}


async def test_space_poll_vote_closed_returns_409(client):
    await _seed_space(client)
    await _seed_poll(client, closed=True)
    r = await client.post(
        "/api/spaces/sp-polls/posts/sp-post-1/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    assert r.status == 409


async def test_space_poll_vote_unknown_poll_404(client):
    await _seed_space(client)
    r = await client.post(
        "/api/spaces/sp-polls/posts/missing/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_space_poll_vote_unknown_option_422(client):
    await _seed_space(client)
    await _seed_poll(client)
    r = await client.post(
        "/api/spaces/sp-polls/posts/sp-post-1/poll/vote",
        json={"option_id": "ghost"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_space_poll_non_member_forbidden(client):
    await _seed_space(client)
    await _seed_poll(client)
    # Second user *not* in the space.
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('outsider','out-id','Out',0)",
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES('t2','out-id','t',?)",
        (sha256_token_hash("out-tok"),),
    )
    r = await client.post(
        "/api/spaces/sp-polls/posts/sp-post-1/poll/vote",
        json={"option_id": "opt-y"},
        headers={"Authorization": "Bearer out-tok"},
    )
    assert r.status == 403


async def test_space_poll_close_by_author(client):
    await _seed_space(client)
    await _seed_poll(client)
    r = await client.post(
        "/api/spaces/sp-polls/posts/sp-post-1/poll/close",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["closed"] is True


async def test_space_poll_close_by_non_author_forbidden(client):
    await _seed_space(client)
    # Post authored by a different user_id.
    await _seed_poll(
        client,
        post_id="sp-post-other",
    )
    # Overwrite the author column so the seeded admin isn't the author.
    await client._db.enqueue(
        "UPDATE space_posts SET author='other-uid' WHERE id='sp-post-other'",
    )
    r = await client.post(
        "/api/spaces/sp-polls/posts/sp-post-other/poll/close",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_space_poll_summary_returns_space_id(client):
    await _seed_space(client)
    await _seed_poll(client)
    r = await client.get(
        "/api/spaces/sp-polls/posts/sp-post-1/poll",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    assert data["space_id"] == "sp-polls"
    assert data["question"] == "Pizza?"
    assert data["user_vote"] == []


async def test_space_poll_create_via_post(client):
    await _seed_space(client)
    await _seed_post(client, post_id="new-poll", post_type="poll")
    r = await client.post(
        "/api/spaces/sp-polls/posts/new-poll/poll",
        json={
            "question": "What's for dinner?",
            "options": ["Pizza", "Tacos"],
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["question"] == "What's for dinner?"
    assert [o["text"] for o in body["options"]] == ["Pizza", "Tacos"]
    assert body["space_id"] == "sp-polls"


async def test_space_poll_create_too_few_options_422(client):
    await _seed_space(client)
    await _seed_post(client, post_id="bad-poll", post_type="poll")
    r = await client.post(
        "/api/spaces/sp-polls/posts/bad-poll/poll",
        json={"question": "Q", "options": ["Only one"]},
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── Schedule polls ─────────────────────────────────────────────────────


async def _seed_schedule_post(
    client,
    *,
    space_id: str = "sp-polls",
    post_id: str = "sp-sched-1",
):
    await _seed_post(
        client,
        space_id=space_id,
        post_id=post_id,
        post_type="schedule",
    )


async def _create_schedule_poll(
    client,
    *,
    space_id: str = "sp-polls",
    post_id: str = "sp-sched-1",
    slots: list | None = None,
):
    slots = slots or [
        {"id": "slot-A", "slot_date": "2026-05-01"},
        {"id": "slot-B", "slot_date": "2026-05-02", "start_time": "18:00"},
    ]
    return await client.post(
        f"/api/spaces/{space_id}/posts/{post_id}/schedule-poll",
        json={"title": "Pizza night", "slots": slots},
        headers=_auth(client._tok),
    )


async def test_space_schedule_create_returns_shape(client):
    await _seed_space(client)
    await _seed_schedule_post(client)
    r = await _create_schedule_poll(client)
    assert r.status == 201
    data = await r.json()
    assert data["title"] == "Pizza night"
    assert data["space_id"] == "sp-polls"
    assert [s["id"] for s in data["slots"]] == ["slot-A", "slot-B"]


async def test_space_schedule_respond_and_summary(client):
    await _seed_space(client)
    await _seed_schedule_post(client)
    await _create_schedule_poll(client)
    r = await client.post(
        "/api/spaces/sp-polls/schedule-polls/sp-sched-1/respond",
        json={"slot_id": "slot-A", "response": "yes"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    yes_rows = [
        x
        for x in data["responses"]
        if x["slot_id"] == "slot-A" and x["availability"] == "yes"
    ]
    assert len(yes_rows) == 1
    assert yes_rows[0]["user_id"] == client._uid


async def test_space_schedule_finalize_sets_winner_and_closes(client):
    await _seed_space(client)
    await _seed_schedule_post(client, post_id="sp-sched-3")
    await _create_schedule_poll(client, post_id="sp-sched-3")
    r = await client.post(
        "/api/spaces/sp-polls/schedule-polls/sp-sched-3/finalize",
        json={"slot_id": "slot-B"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    assert data["finalized_slot_id"] == "slot-B"
    assert data["closed"] is True


async def test_space_schedule_finalize_non_author_403(client):
    await _seed_space(client)
    await _seed_schedule_post(client, post_id="sp-sched-4")
    await _create_schedule_poll(client, post_id="sp-sched-4")
    # Second user joins the space but isn't the post author.
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('outsider','out-id','Out',0)",
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) "
        "VALUES('sp-polls', 'out-id', 'member')",
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('t3','out-id','t',?)",
        (sha256_token_hash("out-tok"),),
    )
    r = await client.post(
        "/api/spaces/sp-polls/schedule-polls/sp-sched-4/finalize",
        json={"slot_id": "slot-A"},
        headers={"Authorization": "Bearer out-tok"},
    )
    assert r.status == 403


async def test_space_schedule_retract_deletes_response(client):
    await _seed_space(client)
    await _seed_schedule_post(client, post_id="sp-sched-7")
    await _create_schedule_poll(client, post_id="sp-sched-7")
    await client.post(
        "/api/spaces/sp-polls/schedule-polls/sp-sched-7/respond",
        json={"slot_id": "slot-A", "response": "yes"},
        headers=_auth(client._tok),
    )
    r = await client.delete(
        "/api/spaces/sp-polls/schedule-polls/sp-sched-7/slots/slot-A/response",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    data = await r.json()
    assert not [x for x in data["responses"] if x["slot_id"] == "slot-A"]


async def test_space_schedule_summary_non_member_forbidden(client):
    await _seed_space(client)
    await _seed_schedule_post(client)
    await _create_schedule_poll(client)
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('outsider','out-id','Out',0)",
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES('t4','out-id','t',?)",
        (sha256_token_hash("out-tok"),),
    )
    r = await client.get(
        "/api/spaces/sp-polls/schedule-polls/sp-sched-1/summary",
        headers={"Authorization": "Bearer out-tok"},
    )
    assert r.status == 403


# ─── §4.3: attaching a poll is part of making a poll post ───────────────


async def _member_in_admin_only_posts(client) -> dict:
    token = "mem-tok"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES('mem', 'uid-mem', 'Mem', 0)"
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES('t-mem', 'uid-mem', 't', ?)",
        (sha256_token_hash(token),),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) "
        "VALUES('sp-polls', 'uid-mem', 'member')"
    )
    await client._db.enqueue(
        "UPDATE spaces SET posts_access='admin_only' WHERE id='sp-polls'"
    )
    return _auth(token)


async def test_admin_only_posts_refuse_a_members_poll_and_schedule(client):
    await _seed_space(client)
    await _seed_post(client, post_id="admins-post", post_type="poll")
    mem = await _member_in_admin_only_posts(client)
    for path, body in (
        (
            "/api/spaces/sp-polls/posts/admins-post/poll",
            {"question": "Q?", "options": ["a", "b"]},
        ),
        (
            "/api/spaces/sp-polls/posts/admins-post/schedule-poll",
            {"title": "T", "slots": [{"slot_date": "2026-05-01"}]},
        ),
    ):
        r = await client.post(path, json=body, headers=mem)
        assert r.status == 403, await r.text()
        assert (await r.json())["error"]["code"] == "ACCESS_ADMIN_ONLY"
    rows = await client._db.fetchall("SELECT * FROM space_polls")
    assert rows == []
    # The admin still attaches one.
    r = await client.post(
        "/api/spaces/sp-polls/posts/admins-post/poll",
        json={"question": "Q?", "options": ["a", "b"]},
        headers=_auth(client._tok),
    )
    assert r.status == 201


async def test_admin_only_posts_still_take_a_members_vote(client):
    await _seed_space(client)
    await _seed_post(client, post_id="p-vote", post_type="poll")
    r = await client.post(
        "/api/spaces/sp-polls/posts/p-vote/poll",
        json={"question": "Q?", "options": ["a", "b"]},
        headers=_auth(client._tok),
    )
    option_id = (await r.json())["options"][0]["id"]
    mem = await _member_in_admin_only_posts(client)
    r = await client.post(
        "/api/spaces/sp-polls/posts/p-vote/poll/vote",
        json={"option_id": option_id},
        headers=mem,
    )
    assert r.status in (200, 201), await r.text()


async def test_a_vote_on_a_deleted_posts_poll_is_a_clean_404(client):
    """Migration 0085: deleting a post drops its poll; a vote that arrives
    afterwards (a stale tab) is a 404, never an integrity error / 500."""
    await _seed_space(client)
    await _seed_poll(client)
    await client._db.enqueue(
        "UPDATE space_posts SET deleted=1 WHERE id='sp-post-1'", ()
    )
    r = await client.post(
        "/api/spaces/sp-polls/posts/sp-post-1/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    assert r.status == 404
    assert await client._db.fetchall("SELECT * FROM space_poll_votes", ()) == []


async def test_a_vote_racing_the_post_delete_is_a_clean_404(client, monkeypatch):
    """The post is deleted between the poll read and the vote write: the
    vote lands nowhere (its option is gone) and the answer is a 404."""
    from socialhome.app_keys import space_poll_service_key

    await _seed_space(client)
    await _seed_poll(client)
    svc = client.server.app[space_poll_service_key]
    cls = type(svc._repo)
    real = cls.list_user_votes

    async def _delete_then_list(self, *args, **kwargs):
        await client._db.enqueue(
            "UPDATE space_posts SET deleted=1 WHERE id='sp-post-1'", ()
        )
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(cls, "list_user_votes", _delete_then_list)
    r = await client.post(
        "/api/spaces/sp-polls/posts/sp-post-1/poll/vote",
        json={"option_id": "opt-y"},
        headers=_auth(client._tok),
    )
    assert r.status == 404
    assert await client._db.fetchall("SELECT * FROM space_poll_votes", ()) == []
