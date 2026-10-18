"""The Bazaar membership gate — a listing, its bids and its offers are only
visible to people with a seat in the listing's space.

Regression for the bug where ``GET /api/bazaar/{id}`` and
``GET /api/bazaar/{id}/bids`` had no membership check: any authenticated
user of the household who knew (or guessed) a listing id read the listing
and every bid on it, and could bid, make an offer or bookmark it from
outside the space.

The rule (:meth:`BazaarService.get_visible_listing`): a listing is visible
to anyone with a seat in its space — owner, admin, moderator, member and a
read-only follower (``subscriber``) — and to its seller. Anyone else gets
the same ``404 NOT_FOUND "Listing not found."`` an unknown id gets, so a
listing id is no oracle. Bidding and making offers need a *writer* seat:
a follower may look, not trade (``403 SUBSCRIBER_READ_ONLY``).
"""

import pytest

from socialhome.auth import sha256_token_hash

from .conftest import _auth

_SELLER = ("sally", "uid-sally")
_MEMBER = ("mia", "uid-mia")
_FOLLOWER = ("fern", "uid-fern")
#: Has a seat — but in another space.
_OTHER = ("otto", "uid-otto")
#: In no space at all.
_GUEST = ("guest", "uid-guest")

_SPACE = "sp-market"
_OTHER_SPACE = "sp-elsewhere"
_AUCTION = "lst-auction"
_FIXED = "lst-fixed"
_BID = "bid-mia"
_OFFER = "offer-mia"


async def _add_user(client, username: str, user_id: str) -> dict:
    token = f"{username}-tok"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) "
        "VALUES(?, ?, ?, 0)",
        (username, user_id, username.title()),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES(?, ?, 't', ?)",
        (f"t-{username}", user_id, sha256_token_hash(token)),
    )
    return _auth(token)


async def _seed_space(client, sid: str) -> None:
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, join_mode, allow_subscribers)"
        " VALUES(?, ?, 'inst', 'sally', ?, 'private', 'invite_only', 1)",
        (sid, f"Space {sid}", "ab" * 32),
    )


async def _seat(client, sid: str, user_id: str, role: str) -> None:
    await client._db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
        (sid, user_id, role),
    )


async def _listing(client, post_id: str, mode: str) -> None:
    await client._db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES(?, ?, ?, 'bazaar', 'secret bike')",
        (post_id, _SPACE, _SELLER[1]),
    )
    await client._db.enqueue(
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode,"
        " title, end_time, currency, status, price, start_price, step_price)"
        " VALUES(?, ?, ?, ?, 'Secret bike', '2999-01-01T00:00:00+00:00',"
        " 'EUR', 'active', 5000, 100, 10)",
        (post_id, _SPACE, _SELLER[1], mode),
    )


@pytest.fixture
async def seeded(client):
    """Space ``sp-market`` (sally owner, mia member, fern follower) with an
    auction listing carrying mia's bid and a fixed listing carrying mia's
    offer; otto sits in another space only; guest is in no space."""
    hdr = {
        "seller": await _add_user(client, *_SELLER),
        "member": await _add_user(client, *_MEMBER),
        "follower": await _add_user(client, *_FOLLOWER),
        "other": await _add_user(client, *_OTHER),
        "guest": await _add_user(client, *_GUEST),
    }
    await _seed_space(client, _SPACE)
    await _seed_space(client, _OTHER_SPACE)
    await _seat(client, _SPACE, _SELLER[1], "owner")
    await _seat(client, _SPACE, _MEMBER[1], "member")
    await _seat(client, _SPACE, _FOLLOWER[1], "subscriber")
    await _seat(client, _OTHER_SPACE, _OTHER[1], "owner")
    await _listing(client, _AUCTION, "auction")
    await _listing(client, _FIXED, "fixed")
    await client._db.enqueue(
        "INSERT INTO bazaar_bids(id, listing_post_id, bidder_user_id, amount)"
        " VALUES(?, ?, ?, 200)",
        (_BID, _AUCTION, _MEMBER[1]),
    )
    await client._db.enqueue(
        "INSERT INTO bazaar_offers(id, listing_post_id, offerer_user_id,"
        " amount, status, created_at) VALUES(?, ?, ?, 4000, 'pending',"
        " '2026-01-01T00:00:00+00:00')",
        (_OFFER, _FIXED, _MEMBER[1]),
    )
    return client, hdr


async def _not_found(resp, detail: str = "Listing not found.") -> dict:
    assert resp.status == 404, await resp.text()
    body = await resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["detail"] == detail
    return body


_OUTSIDERS = ("guest", "other")


# ── The reported bug: reads ─────────────────────────────────────────────


@pytest.mark.parametrize("who", _OUTSIDERS)
async def test_outsider_cannot_read_a_listing(seeded, who):
    """The reported repro: a household user with no seat in the space read
    the listing by id."""
    client, hdr = seeded
    await _not_found(await client.get(f"/api/bazaar/{_AUCTION}", headers=hdr[who]))


@pytest.mark.parametrize("who", _OUTSIDERS)
async def test_outsider_cannot_read_bids(seeded, who):
    client, hdr = seeded
    await _not_found(await client.get(f"/api/bazaar/{_AUCTION}/bids", headers=hdr[who]))


@pytest.mark.parametrize("who", _OUTSIDERS)
async def test_outsider_cannot_read_offers(seeded, who):
    client, hdr = seeded
    await _not_found(await client.get(f"/api/bazaar/{_FIXED}/offers", headers=hdr[who]))


async def test_not_visible_answers_exactly_like_an_unknown_id(seeded):
    """No existence oracle: a listing the caller can't see and an id that
    doesn't exist get byte-identical answers on every by-id route."""
    client, hdr = seeded
    h = hdr["guest"]
    for method, suffix, body in (
        ("GET", "", None),
        ("PATCH", "", {"title": "x"}),
        ("DELETE", "", None),
        ("GET", "/bids", None),
        ("POST", "/bids", {"amount": 500}),
        ("GET", "/offers", None),
        ("POST", "/offers", {"amount": 500}),
        ("POST", "/save", None),
    ):
        real = await client.request(
            method, f"/api/bazaar/{_AUCTION}{suffix}", json=body, headers=h
        )
        ghost = await client.request(
            method, f"/api/bazaar/no-such-listing{suffix}", json=body, headers=h
        )
        assert (real.status, await real.text()) == (
            ghost.status,
            await ghost.text(),
        ), (method, suffix)
        assert real.status == 404, (method, suffix)


@pytest.mark.parametrize("who", ("seller", "member", "follower"))
async def test_seat_holders_read_listing_bids_and_offers(seeded, who):
    """Any seat reads — the follower included."""
    client, hdr = seeded
    r = await client.get(f"/api/bazaar/{_AUCTION}", headers=hdr[who])
    assert r.status == 200, await r.text()
    assert (await r.json())["title"] == "Secret bike"
    r = await client.get(f"/api/bazaar/{_AUCTION}/bids", headers=hdr[who])
    assert r.status == 200, await r.text()
    assert [b["id"] for b in await r.json()] == [_BID]
    r = await client.get(f"/api/bazaar/{_FIXED}/offers", headers=hdr[who])
    assert r.status == 200, await r.text()


async def test_seller_who_left_still_sees_and_cancels_own_listing(seeded):
    """The seller's own listing stays theirs (``GET /api/bazaar?seller=me``
    lists it anyway) — leaving the space must not strand it."""
    client, hdr = seeded
    await client._db.enqueue(
        "DELETE FROM space_members WHERE space_id=? AND user_id=?",
        (_SPACE, _SELLER[1]),
    )
    r = await client.get(f"/api/bazaar/{_AUCTION}", headers=hdr["seller"])
    assert r.status == 200, await r.text()
    r = await client.delete(f"/api/bazaar/{_FIXED}", headers=hdr["seller"])
    assert r.status == 204, await r.text()


# ── Writes ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", _OUTSIDERS)
async def test_outsider_cannot_bid_offer_or_save(seeded, who):
    client, hdr = seeded
    h = hdr[who]
    await _not_found(
        await client.post(
            f"/api/bazaar/{_AUCTION}/bids", json={"amount": 500}, headers=h
        )
    )
    await _not_found(
        await client.post(
            f"/api/bazaar/{_FIXED}/offers", json={"amount": 500}, headers=h
        )
    )
    await _not_found(await client.post(f"/api/bazaar/{_FIXED}/save", headers=h))
    bids = await client._db.fetchall(
        "SELECT id FROM bazaar_bids WHERE bidder_user_id=?", (_GUEST[1],)
    )
    assert bids == []


async def test_follower_may_save_but_not_bid_or_offer(seeded):
    """A follower reads and bookmarks; trading needs a writer seat."""
    client, hdr = seeded
    h = hdr["follower"]
    for path, amount in ((f"{_AUCTION}/bids", 500), (f"{_FIXED}/offers", 500)):
        r = await client.post(f"/api/bazaar/{path}", json={"amount": amount}, headers=h)
        assert r.status == 403, await r.text()
        assert (await r.json())["error"]["code"] == "SUBSCRIBER_READ_ONLY"
    r = await client.post(f"/api/bazaar/{_FIXED}/save", headers=h)
    assert r.status == 201, await r.text()


async def test_member_may_bid_and_offer(seeded):
    client, hdr = seeded
    h = hdr["member"]
    r = await client.post(
        f"/api/bazaar/{_AUCTION}/bids", json={"amount": 500}, headers=h
    )
    assert r.status == 201, await r.text()
    r = await client.post(
        f"/api/bazaar/{_FIXED}/offers", json={"amount": 4500}, headers=h
    )
    assert r.status == 201, await r.text()


@pytest.mark.parametrize("who", _OUTSIDERS)
async def test_outsider_patch_and_delete_answer_not_found(seeded, who):
    """Before the gate a non-seller got 403 "only the seller…" for a real
    listing and 404 for an unknown id — an existence oracle."""
    client, hdr = seeded
    h = hdr[who]
    await _not_found(
        await client.patch(f"/api/bazaar/{_FIXED}", json={"title": "x"}, headers=h)
    )
    await _not_found(await client.delete(f"/api/bazaar/{_FIXED}", headers=h))


async def test_member_non_seller_patch_is_forbidden(seeded):
    """A member sees the listing, so the seller-only refusal stays 403."""
    client, hdr = seeded
    r = await client.patch(
        f"/api/bazaar/{_FIXED}", json={"title": "x"}, headers=hdr["member"]
    )
    assert r.status == 403, await r.text()


@pytest.mark.parametrize("who", _OUTSIDERS)
async def test_outsider_bid_and_offer_actions_answer_not_found(seeded, who):
    """accept / reject / withdraw by bid or offer id: an outsider gets the
    unknown-id 404, never the seller-only / bidder-only 403."""
    client, hdr = seeded
    h = hdr[who]
    base = f"/api/bazaar/{_AUCTION}/bids/{_BID}"
    await _not_found(await client.post(f"{base}/accept", headers=h), "Bid not found.")
    await _not_found(
        await client.post(f"{base}/reject", json={}, headers=h), "Bid not found."
    )
    await _not_found(await client.delete(base, headers=h), "Bid not found.")
    base = f"/api/bazaar/{_FIXED}/offers/{_OFFER}"
    await _not_found(await client.post(f"{base}/accept", headers=h), "Offer not found.")
    await _not_found(
        await client.post(f"{base}/reject", json={}, headers=h), "Offer not found."
    )
    await _not_found(await client.delete(base, headers=h), "Offer not found.")


async def test_bidder_who_left_can_still_withdraw_own_bid(seeded):
    client, hdr = seeded
    await client._db.enqueue(
        "DELETE FROM space_members WHERE space_id=? AND user_id=?",
        (_SPACE, _MEMBER[1]),
    )
    r = await client.delete(
        f"/api/bazaar/{_AUCTION}/bids/{_BID}", headers=hdr["member"]
    )
    assert r.status == 204, await r.text()
    r = await client.delete(
        f"/api/bazaar/{_FIXED}/offers/{_OFFER}", headers=hdr["member"]
    )
    assert r.status == 200, await r.text()


# ── Saved listings ──────────────────────────────────────────────────────


async def test_my_saved_drops_listings_of_a_space_the_user_left(seeded):
    """A bookmark of a listing whose space the user has left no longer
    lists — the client would only hydrate it into a 404 anyway."""
    client, hdr = seeded
    h = hdr["follower"]
    assert (await client.post(f"/api/bazaar/{_FIXED}/save", headers=h)).status == 201
    r = await client.get("/api/me/bazaar/saved", headers=h)
    assert [s["post_id"] for s in (await r.json())["saved"]] == [_FIXED]
    await client._db.enqueue(
        "DELETE FROM space_members WHERE space_id=? AND user_id=?",
        (_SPACE, _FOLLOWER[1]),
    )
    r = await client.get("/api/me/bazaar/saved", headers=h)
    assert r.status == 200
    assert (await r.json())["saved"] == []
    # Un-bookmarking still works after leaving.
    r = await client.delete(f"/api/bazaar/{_FIXED}/save", headers=h)
    assert r.status == 200


@pytest.mark.parametrize(
    ("who", "can_trade"), [("member", True), ("follower", False), ("seller", False)]
)
async def test_detail_says_whether_the_viewer_may_trade(seeded, who, can_trade):
    """The SPA hides Buy / bid / offer for a follower (and the seller):
    ``viewer_can_trade`` on ``GET /api/bazaar/{id}``."""
    client, hdr = seeded
    r = await client.get(f"/api/bazaar/{_FIXED}", headers=hdr[who])
    assert r.status == 200, await r.text()
    assert (await r.json())["viewer_can_trade"] is can_trade
