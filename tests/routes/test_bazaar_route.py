"""HTTP tests for /api/bazaar/* — improves coverage of routes/bazaar.py."""

from __future__ import annotations


from .conftest import _auth


async def _seed_listing(
    client, *, listing_id: str = "lst-1", seller: str | None = None
) -> None:
    seller = seller or client._uid
    db = client._db
    # bazaar_listings.post_id FKs feed_posts(id), so we need a parent feed post.
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content) VALUES(?, ?, 'text', '')",
        (listing_id, seller),
    )
    await db.enqueue(
        "INSERT INTO bazaar_listings(post_id, seller_user_id, title, mode, end_time, currency)"
        " VALUES(?, ?, ?, 'fixed', '2099-01-01T00:00:00+00:00', 'USD')",
        (listing_id, seller, "Test listing"),
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
    r = await client.post(
        "/api/bazaar",
        json={
            "title": "Classic bike",
            "description": "good condition",
            "mode": "fixed",
            "price": 5000,
            "currency": "USD",
            "duration_days": 3,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["title"] == "Classic bike"
    assert body["mode"] == "fixed"
    assert body["status"] == "active"


async def test_create_listing_missing_title_422(client):
    r = await client.post(
        "/api/bazaar",
        json={"mode": "fixed", "price": 100, "currency": "USD"},
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
