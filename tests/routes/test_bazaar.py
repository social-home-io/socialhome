"""Tests for socialhome.routes.bazaar."""

from socialhome.auth import sha256_token_hash

from .conftest import _auth


async def test_list_active_listings(client):
    """GET /api/bazaar returns active listings (empty at start)."""
    r = await client.get("/api/bazaar", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert isinstance(body, list)


async def _make_space(client, **extra) -> str:
    h = _auth(client._tok)
    r = await client.post(
        "/api/spaces", json={"name": "Market", "emoji": "🛍", **extra}, headers=h
    )
    assert r.status == 201
    sid = (await r.json())["id"]
    # A new space starts without the Bazaar (owner decision 3): turn it on.
    r = await client.patch(
        f"/api/spaces/{sid}", json={"features": {"bazaar": True}}, headers=h
    )
    assert r.status == 200, await r.text()
    return sid


async def _make_listing(client, sid: str, **extra) -> dict:
    h = _auth(client._tok)
    r = await client.post(
        "/api/bazaar",
        json={
            "space_id": sid,
            "title": "Bike",
            "mode": "fixed",
            "currency": "EUR",
            "price": 5000,
            **extra,
        },
        headers=h,
    )
    assert r.status == 201, await r.text()
    return await r.json()


async def test_space_bazaar_tab_lists_listings(client):
    """GET /api/spaces/{id}/bazaar returns that space's listings."""
    h = _auth(client._tok)
    sid = await _make_space(client)
    await _make_listing(client, sid)
    r = await client.get(f"/api/spaces/{sid}/bazaar", headers=h)
    assert r.status == 200
    body = await r.json()
    assert len(body) == 1
    assert body[0]["title"] == "Bike"
    assert body[0]["space_id"] == sid


async def test_listing_hidden_from_feed_by_default(client):
    """A new listing lives in the Bazaar tab, not the feed (announce off)."""
    h = _auth(client._tok)
    sid = await _make_space(client)
    await _make_listing(client, sid)
    feed = await (await client.get(f"/api/spaces/{sid}/feed", headers=h)).json()
    assert all(p["type"] != "bazaar" for p in feed), feed
    # ...but the listing is in the Bazaar tab.
    tab = await (await client.get(f"/api/spaces/{sid}/bazaar", headers=h)).json()
    assert len(tab) == 1


async def test_listing_appears_in_feed_when_announced(client):
    """``announce_in_feed=True`` surfaces the wrapper post in the feed."""
    h = _auth(client._tok)
    sid = await _make_space(client)
    await _make_listing(client, sid, announce_in_feed=True)
    feed = await (await client.get(f"/api/spaces/{sid}/feed", headers=h)).json()
    assert any(p["type"] == "bazaar" for p in feed), feed


async def test_bazaar_feature_off_blocks_create_and_browse(client):
    """With the space's bazaar feature disabled, creating a listing is
    rejected and the space bazaar endpoint 403s."""
    h = _auth(client._tok)
    sid = await _make_space(client)
    patch = await client.patch(
        f"/api/spaces/{sid}",
        json={"features": {"bazaar": False, "pages": True, "gallery": True}},
        headers=h,
    )
    assert patch.status == 200
    create = await client.post(
        "/api/bazaar",
        json={
            "space_id": sid,
            "title": "X",
            "mode": "fixed",
            "currency": "EUR",
            "price": 100,
        },
        headers=h,
    )
    assert create.status == 403
    browse = await client.get(f"/api/spaces/{sid}/bazaar", headers=h)
    assert browse.status == 403


# ── "Ask the seller" (v_74) — POST /api/bazaar/{id}/ask, GET …/threads ──


async def _seat_user(client, sid: str, username: str, role: str) -> dict:
    """A second local user with a seat in ``sid``; returns their headers."""
    uid = f"uid-{username}"
    token = f"{username}-tok"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES(?, ?, ?, 0)",
        (username, uid, username.title()),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
        " VALUES(?, ?, 't', ?)",
        (f"t-{username}", uid, sha256_token_hash(token)),
    )
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        (sid, uid, role),
    )
    return _auth(token)


async def test_ask_a_seller_in_the_household_opens_the_normal_dm(client):
    sid = await _make_space(client)
    listing = await _make_listing(client, sid)
    mia = await _seat_user(client, sid, "mia", "member")
    r = await client.post(f"/api/bazaar/{listing['post_id']}/ask", headers=mia)
    assert r.status == 201, await r.text()
    body = await r.json()
    assert body["kind"] == "dm"
    # The same button again lands in the same DM.
    r = await client.post(f"/api/bazaar/{listing['post_id']}/ask", headers=mia)
    assert (await r.json())["conversation_id"] == body["conversation_id"]
    # A DM is no listing thread: the seller's thread list stays empty.
    r = await client.get(
        f"/api/bazaar/{listing['post_id']}/threads", headers=_auth(client._tok)
    )
    assert r.status == 200 and await r.json() == []


async def test_ask_refuses_followers_the_seller_and_strangers(client):
    sid = await _make_space(client)
    listing = await _make_listing(client, sid)
    fern = await _seat_user(client, sid, "fern", "subscriber")
    r = await client.post(f"/api/bazaar/{listing['post_id']}/ask", headers=fern)
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "SUBSCRIBER_READ_ONLY"
    r = await client.post(
        f"/api/bazaar/{listing['post_id']}/ask", headers=_auth(client._tok)
    )
    assert (await r.json())["error"]["code"] == "OWN_LISTING"
    r = await client.post("/api/bazaar/no-such-listing/ask", headers=fern)
    assert r.status == 404
    r = await client.get("/api/bazaar/no-such-listing/threads", headers=fern)
    assert r.status == 404


# ── Questions and answers (v_75) — /api/bazaar/{id}/faq ─────────────────


async def test_the_seller_adds_edits_and_removes_questions_and_answers(client):
    seller = _auth(client._tok)
    sid = await _make_space(client)
    listing = await _make_listing(client, sid)
    pid = listing["post_id"]
    r = await client.post(
        f"/api/bazaar/{pid}/faq",
        json={"question": "  Does it\n fold? ", "answer": "Yes,\r\nflat."},
        headers=seller,
    )
    assert r.status == 201, await r.text()
    entry = await r.json()
    assert entry["question"] == "Does it fold?"
    assert entry["answer"] == "Yes,\nflat."
    assert set(entry) == {"entry_id", "question", "answer", "position"}
    # The seller rewords the question.
    r = await client.patch(
        f"/api/bazaar/{pid}/faq/{entry['entry_id']}",
        json={"question": "Can it fold flat?", "answer": "Yes."},
        headers=seller,
    )
    assert r.status == 200, await r.text()
    # Everyone who sees the listing reads it — a follower included.
    fern = await _seat_user(client, sid, "fern", "subscriber")
    r = await client.get(f"/api/bazaar/{pid}/faq", headers=fern)
    assert r.status == 200
    assert [(e["question"], e["answer"]) for e in await r.json()] == [
        ("Can it fold flat?", "Yes.")
    ]
    detail = await (await client.get(f"/api/bazaar/{pid}", headers=fern)).json()
    assert [e["entry_id"] for e in detail["faq"]] == [entry["entry_id"]]
    r = await client.delete(
        f"/api/bazaar/{pid}/faq/{entry['entry_id']}", headers=seller
    )
    assert r.status == 204
    assert await (await client.get(f"/api/bazaar/{pid}/faq", headers=fern)).json() == []
    r = await client.delete(
        f"/api/bazaar/{pid}/faq/{entry['entry_id']}", headers=seller
    )
    assert r.status == 404


async def test_only_the_seller_writes_the_faq(client):
    sid = await _make_space(client)
    pid = (await _make_listing(client, sid))["post_id"]
    mia = await _seat_user(client, sid, "mia", "member")
    r = await client.post(
        f"/api/bazaar/{pid}/faq", json={"question": "Q?", "answer": "A."}, headers=mia
    )
    assert r.status == 403
    # A stranger to the space can neither read nor write: the unknown id's 404.
    other = await _make_space(client)
    stranger = await _seat_user(client, other, "sam", "member")
    for method, path in (
        ("get", f"/api/bazaar/{pid}/faq"),
        ("post", f"/api/bazaar/{pid}/faq"),
        ("patch", f"/api/bazaar/{pid}/faq/{'a' * 32}"),
        ("delete", f"/api/bazaar/{pid}/faq/{'a' * 32}"),
    ):
        r = await getattr(client, method)(
            path, json={"question": "Q?", "answer": "A."}, headers=stranger
        )
        assert r.status == 404, (method, path)


async def test_faq_bounds_are_enforced(client):
    seller = _auth(client._tok)
    sid = await _make_space(client)
    pid = (await _make_listing(client, sid))["post_id"]
    for body in (
        {"question": "", "answer": "A"},
        {"question": "x" * 201, "answer": "A"},
        {"question": "Q", "answer": "y" * 501},
        {"question": "Q", "answer": None},
        {"question": "bell\x07", "answer": "A"},
    ):
        r = await client.post(f"/api/bazaar/{pid}/faq", json=body, headers=seller)
        assert r.status == 422, body
        assert (await r.json())["error"]["code"] == "BAZAAR_FAQ_TEXT"
    for n in range(10):
        r = await client.post(
            f"/api/bazaar/{pid}/faq",
            json={"question": f"Q{n}", "answer": "A"},
            headers=seller,
        )
        assert r.status == 201
    r = await client.post(
        f"/api/bazaar/{pid}/faq",
        json={"question": "Q11", "answer": "A"},
        headers=seller,
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "BAZAAR_FAQ_FULL"


async def test_no_faq_changes_once_the_listing_ended(client):
    seller = _auth(client._tok)
    sid = await _make_space(client)
    pid = (await _make_listing(client, sid))["post_id"]
    r = await client.post(
        f"/api/bazaar/{pid}/faq", json={"question": "Q", "answer": "A"}, headers=seller
    )
    entry = await r.json()
    assert (await client.delete(f"/api/bazaar/{pid}", headers=seller)).status == 204
    r = await client.post(
        f"/api/bazaar/{pid}/faq", json={"question": "Q2", "answer": "A"}, headers=seller
    )
    assert r.status == 422
    # What was published stays readable with the ended listing.
    faq = await (await client.get(f"/api/bazaar/{pid}/faq", headers=seller)).json()
    assert [e["entry_id"] for e in faq] == [entry["entry_id"]]


async def test_a_protected_seller_cannot_write_the_faq(client):
    seller = _auth(client._tok)
    sid = await _make_space(client)
    pid = (await _make_listing(client, sid))["post_id"]
    await client._db.enqueue(
        "UPDATE users SET child_protection_enabled=1 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.post(
        f"/api/bazaar/{pid}/faq", json={"question": "Q", "answer": "A"}, headers=seller
    )
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "ACCOUNT_PROTECTED"


async def test_faq_add_is_refused_once_the_listing_had_thirty_entries(client):
    seller = _auth(client._tok)
    sid = await _make_space(client)
    pid = (await _make_listing(client, sid))["post_id"]
    # 29 removed entries (their markers stay for the life of the listing)
    # and one live one: room on the listing, but no new entry left.
    for n in range(29):
        await client._db.enqueue(
            "INSERT INTO bazaar_listing_faq(listing_post_id, entry_id, rev,"
            " removed) VALUES(?,?,?,1)",
            (pid, f"{n:032x}", n + 1),
        )
    r = await client.post(
        f"/api/bazaar/{pid}/faq", json={"question": "Q", "answer": "A"}, headers=seller
    )
    assert r.status == 201
    eid = (await r.json())["entry_id"]
    r = await client.post(
        f"/api/bazaar/{pid}/faq", json={"question": "Q2", "answer": "A"}, headers=seller
    )
    assert r.status == 409
    body = await r.json()
    assert body["error"]["code"] == "BAZAAR_FAQ_USED_UP"
    assert body["error"]["params"] == {"max": 30}
    # The live entry can still be reworded.
    r = await client.patch(
        f"/api/bazaar/{pid}/faq/{eid}",
        json={"question": "Q?", "answer": "A!"},
        headers=seller,
    )
    assert r.status == 200


async def test_faq_changes_are_rate_capped(client):
    seller = _auth(client._tok)
    sid = await _make_space(client)
    pid = (await _make_listing(client, sid))["post_id"]
    r = await client.post(
        f"/api/bazaar/{pid}/faq", json={"question": "Q", "answer": "A"}, headers=seller
    )
    eid = (await r.json())["entry_id"]
    for n in range(29):
        r = await client.patch(
            f"/api/bazaar/{pid}/faq/{eid}",
            json={"question": f"Q{n}", "answer": "A"},
            headers=seller,
        )
        assert r.status == 200, n
    r = await client.patch(
        f"/api/bazaar/{pid}/faq/{eid}",
        json={"question": "Q", "answer": "A"},
        headers=seller,
    )
    assert r.status == 429
    assert (await r.json())["error"]["code"] == "BAZAAR_FAQ_RATE_LIMITED"
