"""HTTP tests for /api/bazaar/* — improves coverage of routes/bazaar.py."""

from __future__ import annotations


from .conftest import _auth


_TEST_SPACE_ID = "space-bazaar-route"


async def _seed_space(client, space_id: str = _TEST_SPACE_ID) -> str:
    """Insert a space + add the test user as a member (idempotent)."""
    db = client._db
    await db.enqueue(
        """
        INSERT OR IGNORE INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key
        ) VALUES(?, ?, ?, ?, ?)
        """,
        (space_id, "Bazaar Test Space", "iid-test", client._uid, "00" * 32),
    )
    await db.enqueue(
        "INSERT OR IGNORE INTO space_members(space_id, user_id, role) "
        "VALUES(?, ?, 'owner')",
        (space_id, client._uid),
    )
    return space_id


async def _seed_listing(
    client,
    *,
    listing_id: str = "lst-1",
    seller: str | None = None,
    space_id: str = _TEST_SPACE_ID,
) -> None:
    seller = seller or client._uid
    db = client._db
    await _seed_space(client, space_id)
    # bazaar_listings.post_id now FKs space_posts(id).
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content) "
        "VALUES(?, ?, ?, 'bazaar', '')",
        (listing_id, space_id, seller),
    )
    await db.enqueue(
        "INSERT INTO bazaar_listings("
        "  post_id, space_id, seller_user_id, title, mode, end_time, currency"
        ") VALUES(?, ?, ?, ?, 'fixed', '2099-01-01T00:00:00+00:00', 'USD')",
        (listing_id, space_id, seller, "Test listing"),
    )


# ─── List ─────────────────────────────────────────────────────────────────


async def test_list_listings_returns_active(client):
    await _seed_listing(client, listing_id="lst-A")
    r = await client.get("/api/bazaar", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert any(lst["post_id"] == "lst-A" for lst in body)


# ─── Get ──────────────────────────────────────────────────────────────────


async def test_get_listing_returns_404_when_missing(client):
    r = await client.get("/api/bazaar/missing", headers=_auth(client._tok))
    assert r.status == 404


async def test_get_listing_returns_existing(client):
    await _seed_listing(client, listing_id="lst-B")
    r = await client.get("/api/bazaar/lst-B", headers=_auth(client._tok))
    assert r.status == 200
    assert (await r.json())["post_id"] == "lst-B"


# ─── Place bid ────────────────────────────────────────────────────────────


async def test_place_bid_requires_amount(client):
    await _seed_listing(client, listing_id="lst-C")
    r = await client.post(
        "/api/bazaar/lst-C/bids",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_place_bid_rejects_non_int_amount(client):
    await _seed_listing(client, listing_id="lst-D")
    r = await client.post(
        "/api/bazaar/lst-D/bids",
        json={"amount": "lots"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_place_bid_returns_bid(client):
    # Seed listing owned by someone else so the test user is a valid bidder
    # (§23.15: seller cannot bid on own listing).
    await _seed_listing(client, listing_id="lst-E", seller="other-user")
    r = await client.post(
        "/api/bazaar/lst-E/bids",
        json={"amount": 100, "message": "I'll take it"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["amount"] == 100


async def test_place_bid_on_own_listing_rejected(client):
    """Seller cannot bid on their own listing (§23.15)."""
    await _seed_listing(client, listing_id="lst-self")
    r = await client.post(
        "/api/bazaar/lst-self/bids",
        json={"amount": 100},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    body = await r.json()
    assert "own listing" in body["error"]["detail"]


async def test_place_bid_rejects_zero_amount(client):
    await _seed_listing(client, listing_id="lst-zero", seller="other")
    r = await client.post(
        "/api/bazaar/lst-zero/bids",
        json={"amount": 0},
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── Accept offer ─────────────────────────────────────────────────────────


async def test_accept_offer_requires_seller(client):
    # Listing owned by 'other-user'; our test user places a bid, then a
    # *third* user tries to accept. Since the acceptor isn't the seller we
    # expect 403. We need a real bid-id here so the service gets past the
    # 404 lookup and reaches the seller check.
    await _seed_listing(client, listing_id="lst-F", seller="other-user")
    bid_r = await client.post(
        "/api/bazaar/lst-F/bids",
        json={"amount": 100},
        headers=_auth(client._tok),
    )
    bid_id = (await bid_r.json())["id"]
    # client._uid is the bidder, not the seller — accept must 403.
    r = await client.post(
        f"/api/bazaar/lst-F/bids/{bid_id}/accept",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_accept_offer_unknown_listing_404(client):
    r = await client.post(
        "/api/bazaar/missing/bids/x/accept",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_accept_offer_unknown_bid_404(client):
    await _seed_listing(client, listing_id="lst-G")
    r = await client.post(
        "/api/bazaar/lst-G/bids/missing/accept",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_place_bid_invalid_json_400(client):
    await _seed_listing(client, listing_id="lst-H")
    r = await client.post(
        "/api/bazaar/lst-H/bids",
        data="not-json",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


# ─── Create listing ───────────────────────────────────────────────────────


async def test_create_listing_succeeds(client):
    space_id = await _seed_space(client)
    r = await client.post(
        "/api/bazaar",
        json={
            "space_id": space_id,
            "title": "Classic bike",
            "description": "good condition",
            "mode": "fixed",
            "price": 5000,
            "currency": "USD",
            "duration_days": 3,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    assert body["title"] == "Classic bike"
    assert body["mode"] == "fixed"
    assert body["status"] == "active"
    assert body["space_id"] == space_id


async def test_create_listing_missing_space_id_422(client):
    r = await client.post(
        "/api/bazaar",
        json={"title": "x", "mode": "fixed", "price": 100, "currency": "USD"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_create_listing_non_member_403(client):
    # Seed a space the test user isn't a member of.
    db = client._db
    await db.enqueue(
        """
        INSERT INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key
        ) VALUES('space-foreign', 'Foreign', 'iid-test', 'someone-else', ?)
        """,
        ("00" * 32,),
    )
    r = await client.post(
        "/api/bazaar",
        json={
            "space_id": "space-foreign",
            "title": "Sneaky listing",
            "mode": "fixed",
            "price": 100,
            "currency": "USD",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_create_listing_missing_title_422(client):
    space_id = await _seed_space(client)
    r = await client.post(
        "/api/bazaar",
        json={
            "space_id": space_id,
            "mode": "fixed",
            "price": 100,
            "currency": "USD",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_create_listing_auction_requires_start_price(client):
    r = await client.post(
        "/api/bazaar",
        json={
            "title": "Rare comic",
            "mode": "auction",
            "currency": "USD",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ─── Reject / withdraw / cancel ──────────────────────────────────────────


async def test_reject_offer_seller_only(client):
    # Seller is someone else, so we (the test user) can bid but can't reject.
    await _seed_listing(client, listing_id="lst-rej", seller="other-user")
    bid_r = await client.post(
        "/api/bazaar/lst-rej/bids",
        json={"amount": 100},
        headers=_auth(client._tok),
    )
    bid_id = (await bid_r.json())["id"]
    # Now our test user tries to reject their own bid — they aren't the seller.
    r = await client.post(
        f"/api/bazaar/lst-rej/bids/{bid_id}/reject",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_withdraw_bid_by_bidder_succeeds(client):
    await _seed_listing(client, listing_id="lst-wd", seller="other-user")
    bid_r = await client.post(
        "/api/bazaar/lst-wd/bids",
        json={"amount": 100},
        headers=_auth(client._tok),
    )
    bid_id = (await bid_r.json())["id"]
    r = await client.delete(
        f"/api/bazaar/lst-wd/bids/{bid_id}",
        headers=_auth(client._tok),
    )
    assert r.status == 204


async def test_cancel_listing_seller_only(client):
    await _seed_listing(client, listing_id="lst-cxl", seller="other-user")
    r = await client.delete(
        "/api/bazaar/lst-cxl",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_cancel_own_listing_succeeds(client):
    await _seed_listing(client, listing_id="lst-cxl-ok")
    r = await client.delete(
        "/api/bazaar/lst-cxl-ok",
        headers=_auth(client._tok),
    )
    assert r.status == 204
    # Listing status is now cancelled.
    got = await client.get(
        "/api/bazaar/lst-cxl-ok",
        headers=_auth(client._tok),
    )
    assert (await got.json())["status"] == "cancelled"


# ─── Fixed-price offers (§23.23) ──────────────────────────────────────────


async def test_make_offer_returns_pending(client):
    await _seed_listing(client, listing_id="lst-off-1", seller="other-user")
    r = await client.post(
        "/api/bazaar/lst-off-1/offers",
        json={"amount": 4500, "message": "Trade for my bike?"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["status"] == "pending"
    assert body["amount"] == 4500


async def test_make_offer_on_own_listing_rejected(client):
    await _seed_listing(client, listing_id="lst-off-self")
    r = await client.post(
        "/api/bazaar/lst-off-self/offers",
        json={"amount": 4500},
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_make_offer_requires_positive_amount(client):
    await _seed_listing(client, listing_id="lst-off-neg", seller="other-user")
    r = await client.post(
        "/api/bazaar/lst-off-neg/offers",
        json={"amount": 0},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_make_offer_amount_required(client):
    await _seed_listing(client, listing_id="lst-off-noamt", seller="other-user")
    r = await client.post(
        "/api/bazaar/lst-off-noamt/offers",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_offerer_sees_only_own_offer(client):
    await _seed_listing(client, listing_id="lst-off-2", seller="other-user")
    # Seed a foreign offer directly at the DB level.
    db = client._db
    await db.enqueue(
        "INSERT INTO bazaar_offers(id, listing_post_id, offerer_user_id,"
        " amount, status) VALUES(?,?,?,?,?)",
        ("foreign-offer", "lst-off-2", "someone-else", 100, "pending"),
    )
    # Now the test user offers.
    await client.post(
        "/api/bazaar/lst-off-2/offers",
        json={"amount": 200},
        headers=_auth(client._tok),
    )
    r = await client.get(
        "/api/bazaar/lst-off-2/offers",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    offers = await r.json()
    assert all(o["offerer_user_id"] == client._uid for o in offers)
    assert len(offers) == 1


async def test_seller_sees_every_offer(client):
    await _seed_listing(client, listing_id="lst-off-3")  # self is seller
    db = client._db
    for i, uid in enumerate(("u-a", "u-b", "u-c")):
        await db.enqueue(
            "INSERT INTO bazaar_offers(id, listing_post_id, offerer_user_id,"
            " amount, status) VALUES(?,?,?,?,?)",
            (f"o-{i}", "lst-off-3", uid, 100 * (i + 1), "pending"),
        )
    r = await client.get(
        "/api/bazaar/lst-off-3/offers",
        headers=_auth(client._tok),
    )
    offers = await r.json()
    assert len(offers) == 3


async def test_accept_offer_sells_listing_and_rejects_siblings(client):
    await _seed_listing(client, listing_id="lst-acc-1")  # self is seller
    db = client._db
    for i, (uid, amt) in enumerate((("u-a", 100), ("u-b", 200), ("u-c", 150))):
        await db.enqueue(
            "INSERT INTO bazaar_offers(id, listing_post_id, offerer_user_id,"
            " amount, status) VALUES(?,?,?,?,?)",
            (f"acc-{i}", "lst-acc-1", uid, amt, "pending"),
        )
    r = await client.post(
        "/api/bazaar/lst-acc-1/offers/acc-1/accept",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["status"] == "accepted"

    offers = await (
        await client.get(
            "/api/bazaar/lst-acc-1/offers",
            headers=_auth(client._tok),
        )
    ).json()
    statuses = {o["id"]: o["status"] for o in offers}
    assert statuses == {
        "acc-0": "rejected",
        "acc-1": "accepted",
        "acc-2": "rejected",
    }
    # Listing flips to sold with the accepted offer's amount.
    r2 = await client.get(
        "/api/bazaar/lst-acc-1",
        headers=_auth(client._tok),
    )
    listing = await r2.json()
    assert listing["status"] == "sold"
    assert listing["winning_price"] == 200


async def test_accept_offer_non_seller_forbidden(client):
    await _seed_listing(client, listing_id="lst-acc-2", seller="other-user")
    await client._db.enqueue(
        "INSERT INTO bazaar_offers(id, listing_post_id, offerer_user_id,"
        " amount, status) VALUES('acc-x','lst-acc-2','u-z',100,'pending')",
    )
    r = await client.post(
        "/api/bazaar/lst-acc-2/offers/acc-x/accept",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_reject_offer_leaves_listing_active(client):
    await _seed_listing(client, listing_id="lst-rej-1")  # self is seller
    await client._db.enqueue(
        "INSERT INTO bazaar_offers(id, listing_post_id, offerer_user_id,"
        " amount, status) VALUES('rej-1','lst-rej-1','u-z',100,'pending')",
    )
    r = await client.post(
        "/api/bazaar/lst-rej-1/offers/rej-1/reject",
        json={"reason": "too low"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["status"] == "rejected"
    # Listing still active.
    r2 = await client.get(
        "/api/bazaar/lst-rej-1",
        headers=_auth(client._tok),
    )
    assert (await r2.json())["status"] == "active"


async def test_withdraw_own_offer(client):
    await _seed_listing(client, listing_id="lst-wd-1", seller="other-user")
    r1 = await client.post(
        "/api/bazaar/lst-wd-1/offers",
        json={"amount": 100},
        headers=_auth(client._tok),
    )
    offer_id = (await r1.json())["id"]
    r = await client.delete(
        f"/api/bazaar/lst-wd-1/offers/{offer_id}",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["status"] == "withdrawn"


async def test_withdraw_by_non_offerer_forbidden(client):
    await _seed_listing(client, listing_id="lst-wd-2", seller="other-user")
    await client._db.enqueue(
        "INSERT INTO bazaar_offers(id, listing_post_id, offerer_user_id,"
        " amount, status) VALUES('wd-foreign','lst-wd-2','u-other',100,'pending')",
    )
    r = await client.delete(
        "/api/bazaar/lst-wd-2/offers/wd-foreign",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_double_accept_fails_with_state_error(client):
    await _seed_listing(client, listing_id="lst-dbl")  # self is seller
    await client._db.enqueue(
        "INSERT INTO bazaar_offers(id, listing_post_id, offerer_user_id,"
        " amount, status) VALUES('dbl-1','lst-dbl','u-z',100,'pending')",
    )
    await client.post(
        "/api/bazaar/lst-dbl/offers/dbl-1/accept",
        headers=_auth(client._tok),
    )
    r = await client.post(
        "/api/bazaar/lst-dbl/offers/dbl-1/accept",
        headers=_auth(client._tok),
    )
    assert r.status == 409


# ─── Saved listings ───────────────────────────────────────────────────────


async def test_save_unsave_listing(client):
    await _seed_listing(client, listing_id="lst-save-1", seller="other-user")
    r = await client.post(
        "/api/bazaar/lst-save-1/save",
        headers=_auth(client._tok),
    )
    assert r.status == 201
    assert (await r.json())["saved"] is True
    # Probe.
    r2 = await client.get(
        "/api/bazaar/lst-save-1/save",
        headers=_auth(client._tok),
    )
    assert (await r2.json())["saved"] is True
    # Unsave.
    r3 = await client.delete(
        "/api/bazaar/lst-save-1/save",
        headers=_auth(client._tok),
    )
    assert r3.status == 200
    assert (await r3.json())["saved"] is False


async def test_save_listing_is_idempotent(client):
    await _seed_listing(client, listing_id="lst-save-id", seller="other-user")
    for _ in range(3):
        await client.post(
            "/api/bazaar/lst-save-id/save",
            headers=_auth(client._tok),
        )
    r = await client.get(
        "/api/me/bazaar/saved",
        headers=_auth(client._tok),
    )
    body = await r.json()
    assert [s["post_id"] for s in body["saved"]] == ["lst-save-id"]


async def test_save_listing_404_when_missing(client):
    r = await client.post(
        "/api/bazaar/no-such-listing/save",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_list_my_saved_returns_both(client):
    await _seed_listing(client, listing_id="lst-s1", seller="u2")
    await _seed_listing(client, listing_id="lst-s2", seller="u2")
    await client.post("/api/bazaar/lst-s1/save", headers=_auth(client._tok))
    await client.post("/api/bazaar/lst-s2/save", headers=_auth(client._tok))
    r = await client.get(
        "/api/me/bazaar/saved",
        headers=_auth(client._tok),
    )
    body = await r.json()
    # Per-row saved_at has 1-second SQLite resolution — don't assert
    # relative ordering within a single test second.
    assert {s["post_id"] for s in body["saved"]} == {"lst-s1", "lst-s2"}
