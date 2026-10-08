"""Tests for socialhome.repositories.bazaar_repo."""

from __future__ import annotations

import uuid

import pytest

from socialhome.domain.post import BazaarBid, BazaarListing, BazaarMode, BazaarStatus
from socialhome.repositories.bazaar_repo import (
    BidStateError,
    SqliteBazaarRepo,
    new_bid,
)


@pytest.fixture
async def env(tmp_dir):
    """Full repo stack wired to a single in-process SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase
    from socialhome.infrastructure.event_bus import EventBus

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.kp = kp
    e.iid = iid
    e.bus = EventBus()
    e.bazaar_repo = SqliteBazaarRepo(db)
    yield e
    await db.shutdown()


_DEFAULT_SPACE_ID = "space-bazaar-test"


async def _seed_space(db, space_id: str = _DEFAULT_SPACE_ID) -> str:
    """Insert a space row (idempotent) so listings can FK to it."""
    await db.enqueue(
        """
        INSERT OR IGNORE INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key
        ) VALUES(?, ?, ?, ?, ?)
        """,
        (space_id, "Test Space", "iid-test", "u1", "00" * 32),
    )
    return space_id


async def _seed_post(db, post_id: str, space_id: str = _DEFAULT_SPACE_ID):
    """Insert a space + space_posts row to satisfy the bazaar listing FK."""
    await _seed_space(db, space_id)
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content) "
        "VALUES(?,?,?,?,?)",
        (post_id, space_id, "u1", "bazaar", "listing"),
    )
    return space_id


async def test_bazaar_listing_lifecycle(env):
    """Fixed-price listing: create, mark sold, attempt double-sold is rejected."""
    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)

    listing = BazaarListing(
        post_id=pid,
        space_id=_DEFAULT_SPACE_ID,
        seller_user_id="u1",
        mode=BazaarMode.FIXED,
        title="Old bike",
        end_time="2099-01-01T00:00:00",
        currency="EUR",
        status=BazaarStatus.ACTIVE,
        created_at=None,
        price=5000,
    )
    assert await env.bazaar_repo.save_listing(listing, space_id=_DEFAULT_SPACE_ID)
    saved = await env.bazaar_repo.get_listing(pid)
    assert saved.title == "Old bike"

    active = await env.bazaar_repo.list_active()
    assert any(lst.post_id == pid for lst in active)

    await env.bazaar_repo.mark_sold(
        pid, winner_user_id="u2", winning_price=4500, space_id=_DEFAULT_SPACE_ID
    )
    got = await env.bazaar_repo.get_listing(pid)
    assert got.status == BazaarStatus.SOLD
    assert got.winner_user_id == "u2"

    with pytest.raises(ValueError):
        await env.bazaar_repo.mark_sold(
            pid, winner_user_id="u3", winning_price=4000, space_id=_DEFAULT_SPACE_ID
        )


async def test_bazaar_bid_state_machine(env):
    """Offer mode: place bids, withdraw one, accept another (sibling auto-rejected)."""
    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)
    listing = BazaarListing(
        post_id=pid,
        space_id=_DEFAULT_SPACE_ID,
        seller_user_id="u1",
        mode=BazaarMode.OFFER,
        title="Guitar",
        end_time="2099-01-01T00:00:00",
        currency="USD",
        status=BazaarStatus.ACTIVE,
        created_at=None,
        price=20000,
    )
    await env.bazaar_repo.save_listing(listing, space_id=_DEFAULT_SPACE_ID)

    bid_a = new_bid(listing_post_id=pid, bidder_user_id="buyer_a", amount=18000)
    bid_b = new_bid(listing_post_id=pid, bidder_user_id="buyer_b", amount=19000)
    await env.bazaar_repo.place_bid(bid_a, space_id=_DEFAULT_SPACE_ID)
    await env.bazaar_repo.place_bid(bid_b, space_id=_DEFAULT_SPACE_ID)

    await env.bazaar_repo.withdraw_bid(bid_a.id)
    got_a = await env.bazaar_repo.get_bid(bid_a.id)
    assert got_a.withdrawn

    with pytest.raises(BidStateError):
        await env.bazaar_repo.withdraw_bid(bid_a.id)

    await env.bazaar_repo.accept_offer(bid_b.id, space_id=_DEFAULT_SPACE_ID)
    got_b = await env.bazaar_repo.get_bid(bid_b.id)
    assert got_b.accepted

    with pytest.raises(BidStateError):
        await env.bazaar_repo.accept_offer(bid_b.id, space_id=_DEFAULT_SPACE_ID)


async def test_bazaar_relayed_bid_empty_created_at_does_not_win_tie(env):
    """Regression: a federation-relayed bid whose payload carries no
    ``created_at`` must not jump ``highest_bid``'s
    ``ORDER BY amount DESC, created_at ASC`` tie-break ahead of an earlier
    LOCAL bid at the same amount.

    ``place_bid``'s INSERT relies on ``COALESCE(?, datetime('now'))`` to
    default a missing timestamp — but SQLite's ``COALESCE`` only falls
    through on a real ``NULL``, and an empty string ``''`` is not NULL. A
    bid built with ``created_at=""`` therefore stores the literal empty
    string, which sorts *before* any real timestamp on the ``ASC``
    tie-break, so a same-amount relayed bid always "wins" regardless of
    arrival order.
    """
    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)
    listing = BazaarListing(
        post_id=pid,
        space_id=_DEFAULT_SPACE_ID,
        seller_user_id="u1",
        mode=BazaarMode.AUCTION,
        title="Vase",
        end_time="2099-01-01T00:00:00",
        currency="USD",
        status=BazaarStatus.ACTIVE,
        created_at=None,
        price=None,
    )
    await env.bazaar_repo.save_listing(listing, space_id=_DEFAULT_SPACE_ID)

    # Earlier LOCAL bid — gets a real, populated created_at.
    local_bid = new_bid(listing_post_id=pid, bidder_user_id="buyer_local", amount=5000)
    await env.bazaar_repo.place_bid(local_bid, space_id=_DEFAULT_SPACE_ID)

    # Federation-relayed bid, same amount (tie), payload carried no
    # created_at — mirrors what the inbound handler builds when a relayed
    # BAZAAR_BID_PLACED payload has neither `created_at` nor `occurred_at`.
    relayed_bid = BazaarBid(
        id=uuid.uuid4().hex,
        listing_post_id=pid,
        bidder_user_id="buyer_remote",
        amount=5000,
        created_at="",
    )
    await env.bazaar_repo.place_bid(relayed_bid, space_id=_DEFAULT_SPACE_ID)

    stored = await env.bazaar_repo.get_bid(relayed_bid.id)
    assert stored is not None
    assert stored.created_at != "", (
        "empty created_at defeated COALESCE's datetime('now') default"
    )

    winner = await env.bazaar_repo.highest_bid(pid)
    assert winner is not None
    assert winner.id == local_bid.id, (
        "earlier local bid must win a tied amount, not the relayed bid"
    )


async def test_bazaar_reject_offer(env):
    """Seller rejects an offer; BidStateError on reject-after-accept."""
    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)
    listing = BazaarListing(
        post_id=pid,
        space_id=_DEFAULT_SPACE_ID,
        seller_user_id="u1",
        mode=BazaarMode.OFFER,
        title="Camera",
        end_time="2099-01-01T00:00:00",
        currency="GBP",
        status=BazaarStatus.ACTIVE,
        created_at=None,
        price=30000,
    )
    await env.bazaar_repo.save_listing(listing, space_id=_DEFAULT_SPACE_ID)

    bid = new_bid(listing_post_id=pid, bidder_user_id="buyer_x", amount=28000)
    await env.bazaar_repo.place_bid(bid, space_id=_DEFAULT_SPACE_ID)

    await env.bazaar_repo.accept_offer(bid.id, space_id=_DEFAULT_SPACE_ID)
    with pytest.raises(BidStateError):
        await env.bazaar_repo.reject_offer(bid.id, reason="changed mind")

    bid2 = new_bid(listing_post_id=pid, bidder_user_id="buyer_y", amount=27000)
    await env.bazaar_repo.place_bid(bid2, space_id=_DEFAULT_SPACE_ID)
    await env.bazaar_repo.reject_offer(bid2.id, reason="price too low")
    got = await env.bazaar_repo.get_bid(bid2.id)
    assert got.rejected
    assert got.rejection_reason == "price too low"


async def test_bazaar_expired_and_cancelled(env):
    """Listing expiry and cancellation transitions work correctly."""
    pid1 = uuid.uuid4().hex
    pid2 = uuid.uuid4().hex
    for pid in (pid1, pid2):
        await _seed_post(env.db, pid)

    past_end = "2000-01-01T00:00:00"
    for pid, end in ((pid1, past_end), (pid2, "2099-01-01T00:00:00")):
        await env.bazaar_repo.save_listing(
            BazaarListing(
                post_id=pid,
                space_id=_DEFAULT_SPACE_ID,
                seller_user_id="u1",
                mode=BazaarMode.FIXED,
                title="Item",
                end_time=end,
                currency="EUR",
                status=BazaarStatus.ACTIVE,
                created_at=None,
                price=100,
            ),
            space_id=_DEFAULT_SPACE_ID,
        )

    expired = await env.bazaar_repo.list_expired()
    expired_ids = {lst.post_id for lst in expired}
    assert pid1 in expired_ids
    assert pid2 not in expired_ids

    await env.bazaar_repo.mark_expired(pid1, space_id=_DEFAULT_SPACE_ID)
    assert (await env.bazaar_repo.get_listing(pid1)).status == BazaarStatus.EXPIRED

    await env.bazaar_repo.mark_cancelled(pid2, space_id=_DEFAULT_SPACE_ID)
    assert (await env.bazaar_repo.get_listing(pid2)).status == BazaarStatus.CANCELLED


async def test_bazaar_currency_validation(env):
    """Invalid currency raises ValueError on save_listing."""
    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)
    listing = BazaarListing(
        post_id=pid,
        space_id=_DEFAULT_SPACE_ID,
        seller_user_id="u1",
        mode=BazaarMode.FIXED,
        title="Thing",
        end_time="2099-01-01T00:00:00",
        currency="FAKE",
        status=BazaarStatus.ACTIVE,
        created_at=None,
        price=100,
    )
    with pytest.raises(ValueError):
        await env.bazaar_repo.save_listing(listing, space_id=_DEFAULT_SPACE_ID)


# ─── §23.15 auction anti-snipe ─────────────────────────────────────────────


async def test_auction_antisnipe_extends_end_time(env):
    """Bid within 5 min of close pushes the close-time +5 min."""
    from datetime import datetime, timedelta, timezone

    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)
    # Auction that ends in 60 seconds — squarely inside the snipe window.
    close_soon = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    await env.bazaar_repo.save_listing(
        BazaarListing(
            post_id=pid,
            space_id=_DEFAULT_SPACE_ID,
            seller_user_id="u1",
            mode=BazaarMode.AUCTION,
            title="Painting",
            end_time=close_soon,
            currency="EUR",
            status=BazaarStatus.ACTIVE,
            created_at=None,
            start_price=100,
            step_price=10,
        ),
        space_id=_DEFAULT_SPACE_ID,
    )
    await env.bazaar_repo.place_bid(
        new_bid(
            listing_post_id=pid,
            bidder_user_id="u2",
            amount=110,
        ),
        space_id=_DEFAULT_SPACE_ID,
    )
    listing = await env.bazaar_repo.get_listing(pid)
    new_end = datetime.fromisoformat(
        listing.end_time.replace("Z", "+00:00"),
    )
    if new_end.tzinfo is None:
        new_end = new_end.replace(tzinfo=timezone.utc)
    # New end should be ~5 min in the future, well past the original 60 s.
    assert (new_end - datetime.now(timezone.utc)).total_seconds() > 60


async def test_auction_no_extend_outside_snipe_window(env):
    """Bid when >5 min remain leaves ``end_time`` untouched."""
    from datetime import datetime, timedelta, timezone

    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)
    original_end = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    await env.bazaar_repo.save_listing(
        BazaarListing(
            post_id=pid,
            space_id=_DEFAULT_SPACE_ID,
            seller_user_id="u1",
            mode=BazaarMode.AUCTION,
            title="Vase",
            end_time=original_end,
            currency="EUR",
            status=BazaarStatus.ACTIVE,
            created_at=None,
            start_price=100,
            step_price=10,
        ),
        space_id=_DEFAULT_SPACE_ID,
    )
    await env.bazaar_repo.place_bid(
        new_bid(
            listing_post_id=pid,
            bidder_user_id="u2",
            amount=110,
        ),
        space_id=_DEFAULT_SPACE_ID,
    )
    listing = await env.bazaar_repo.get_listing(pid)
    assert listing.end_time == original_end


async def test_non_auction_modes_do_not_extend(env):
    """Only AUCTION listings snipe-extend. FIXED / OFFER stay put."""
    from datetime import datetime, timedelta, timezone

    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)
    close_soon = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    await env.bazaar_repo.save_listing(
        BazaarListing(
            post_id=pid,
            space_id=_DEFAULT_SPACE_ID,
            seller_user_id="u1",
            mode=BazaarMode.OFFER,
            title="Clock",
            end_time=close_soon,
            currency="EUR",
            status=BazaarStatus.ACTIVE,
            created_at=None,
            price=100,
        ),
        space_id=_DEFAULT_SPACE_ID,
    )
    await env.bazaar_repo.place_bid(
        new_bid(
            listing_post_id=pid,
            bidder_user_id="u2",
            amount=100,
        ),
        space_id=_DEFAULT_SPACE_ID,
    )
    listing = await env.bazaar_repo.get_listing(pid)
    assert listing.end_time == close_soon


# ── Fixed-price offers (§23.23) ────────────────────────────────────────────


async def _seed_listing(env, pid: str, *, seller: str = "u1"):
    await _seed_post(env.db, pid)
    await env.bazaar_repo.save_listing(
        BazaarListing(
            post_id=pid,
            space_id=_DEFAULT_SPACE_ID,
            seller_user_id=seller,
            mode=BazaarMode.FIXED,
            title="Thing",
            end_time="2099-01-01T00:00:00",
            currency="EUR",
            status=BazaarStatus.ACTIVE,
            created_at=None,
            price=5000,
        ),
        space_id=_DEFAULT_SPACE_ID,
    )


async def test_create_and_get_offer(env):
    from socialhome.repositories.bazaar_repo import new_offer

    pid = uuid.uuid4().hex
    await _seed_listing(env, pid)
    offer = new_offer(
        listing_post_id=pid,
        offerer_user_id="u2",
        amount=4500,
        message="Trade?",
    )
    await env.bazaar_repo.create_offer(offer)
    fetched = await env.bazaar_repo.get_offer(offer.id)
    assert fetched is not None
    assert fetched.amount == 4500
    assert fetched.status == "pending"
    assert fetched.message == "Trade?"


async def test_list_offers_for_listing_and_offerer(env):
    from socialhome.repositories.bazaar_repo import new_offer

    pid = uuid.uuid4().hex
    await _seed_listing(env, pid)
    for uid, amt in (("u2", 4500), ("u3", 4800), ("u2", 4600)):
        await env.bazaar_repo.create_offer(
            new_offer(
                listing_post_id=pid,
                offerer_user_id=uid,
                amount=amt,
            )
        )
    all_offers = await env.bazaar_repo.list_offers_for_listing(pid)
    assert len(all_offers) == 3
    u2_offers = await env.bazaar_repo.list_offers_for_offerer("u2")
    assert len(u2_offers) == 2
    assert all(o.offerer_user_id == "u2" for o in u2_offers)


async def test_update_offer_status_accepts(env):
    from socialhome.repositories.bazaar_repo import new_offer

    pid = uuid.uuid4().hex
    await _seed_listing(env, pid)
    offer = new_offer(listing_post_id=pid, offerer_user_id="u2", amount=100)
    await env.bazaar_repo.create_offer(offer)
    updated = await env.bazaar_repo.update_offer_status(offer.id, "accepted")
    assert updated.status == "accepted"
    assert updated.responded_at is not None


async def test_update_offer_status_rejects_illegal_transition(env):
    from socialhome.repositories.bazaar_repo import OfferStateError, new_offer

    pid = uuid.uuid4().hex
    await _seed_listing(env, pid)
    offer = new_offer(listing_post_id=pid, offerer_user_id="u2", amount=100)
    await env.bazaar_repo.create_offer(offer)
    await env.bazaar_repo.update_offer_status(offer.id, "rejected")
    # rejected → accepted is illegal
    with pytest.raises(OfferStateError):
        await env.bazaar_repo.update_offer_status(offer.id, "accepted")


async def test_reject_other_pending_offers(env):
    from socialhome.repositories.bazaar_repo import new_offer

    pid = uuid.uuid4().hex
    await _seed_listing(env, pid)
    winner = new_offer(listing_post_id=pid, offerer_user_id="u2", amount=100)
    other1 = new_offer(listing_post_id=pid, offerer_user_id="u3", amount=80)
    other2 = new_offer(listing_post_id=pid, offerer_user_id="u4", amount=90)
    for o in (winner, other1, other2):
        await env.bazaar_repo.create_offer(o)
    n = await env.bazaar_repo.reject_other_pending_offers(
        pid, except_offer_id=winner.id
    )
    assert n == 2
    assert (await env.bazaar_repo.get_offer(winner.id)).status == "pending"
    assert (await env.bazaar_repo.get_offer(other1.id)).status == "rejected"
    assert (await env.bazaar_repo.get_offer(other2.id)).status == "rejected"


async def test_update_offer_status_unknown_raises(env):
    with pytest.raises(KeyError):
        await env.bazaar_repo.update_offer_status("no-such", "accepted")


# ── Saved-listing bookmarks ─────────────────────────────────────────────────


async def _seed_user(env, *, username: str, user_id: str):
    await env.db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, user_id, username),
    )


async def test_save_and_list_bookmarks(env):
    pid = uuid.uuid4().hex
    await _seed_listing(env, pid)
    await _seed_user(env, username="alice", user_id="u-alice")
    await env.bazaar_repo.save_listing_bookmark(user_id="u-alice", post_id=pid)
    assert await env.bazaar_repo.is_listing_saved(user_id="u-alice", post_id=pid)
    saved = await env.bazaar_repo.list_saved_listings("u-alice")
    assert [s["post_id"] for s in saved] == [pid]


async def test_save_is_idempotent(env):
    pid = uuid.uuid4().hex
    await _seed_listing(env, pid)
    await _seed_user(env, username="alice", user_id="u-alice")
    await env.bazaar_repo.save_listing_bookmark(user_id="u-alice", post_id=pid)
    await env.bazaar_repo.save_listing_bookmark(user_id="u-alice", post_id=pid)
    saved = await env.bazaar_repo.list_saved_listings("u-alice")
    assert len(saved) == 1


async def test_unsave_removes_bookmark(env):
    pid = uuid.uuid4().hex
    await _seed_listing(env, pid)
    await _seed_user(env, username="alice", user_id="u-alice")
    await env.bazaar_repo.save_listing_bookmark(user_id="u-alice", post_id=pid)
    await env.bazaar_repo.unsave_listing_bookmark(user_id="u-alice", post_id=pid)
    assert not await env.bazaar_repo.is_listing_saved(user_id="u-alice", post_id=pid)
    assert await env.bazaar_repo.list_saved_listings("u-alice") == []


# ─── §24.11 space scoping ──────────────────────────────────────────────────

_OTHER_SPACE_ID = "space-bazaar-other"


def _listing(pid: str, *, space_id: str = _DEFAULT_SPACE_ID, **kw) -> BazaarListing:
    fields = dict(
        post_id=pid,
        space_id=space_id,
        seller_user_id="u1",
        mode=BazaarMode.OFFER,
        title="Item",
        end_time="2099-01-01T00:00:00",
        currency="EUR",
        status=BazaarStatus.ACTIVE,
        created_at=None,
        price=100,
    )
    fields.update(kw)
    return BazaarListing(**fields)


@pytest.fixture
async def other_listing(env):
    """A listing (with one pending bid) of ``_OTHER_SPACE_ID``, plus a
    bare post of the default space the tests can point at."""
    pid = "pid-other"
    await _seed_post(env.db, pid, _OTHER_SPACE_ID)
    await _seed_post(env.db, "pid-mine")
    assert await env.bazaar_repo.save_listing(
        _listing(pid, space_id=_OTHER_SPACE_ID), space_id=_OTHER_SPACE_ID
    )
    bid = new_bid(listing_post_id=pid, bidder_user_id="buyer", amount=90)
    await env.bazaar_repo.place_bid(bid, space_id=_OTHER_SPACE_ID)
    return pid, bid.id


async def test_save_listing_refuses_a_listing_of_another_space(env, other_listing):
    pid, _ = other_listing
    assert not await env.bazaar_repo.save_listing(
        _listing(pid, title="pwned"), space_id=_DEFAULT_SPACE_ID
    )
    got = await env.bazaar_repo.get_listing(pid)
    assert got.title == "Item"
    assert got.space_id == _OTHER_SPACE_ID


async def test_save_listing_refuses_a_wrapper_post_of_another_space(env):
    """A new listing must sit on a post of the scoped space."""
    await _seed_post(env.db, "pid-x", _OTHER_SPACE_ID)
    await _seed_space(env.db)
    assert not await env.bazaar_repo.save_listing(
        _listing("pid-x"), space_id=_DEFAULT_SPACE_ID
    )
    assert await env.bazaar_repo.get_listing("pid-x") is None


async def test_save_listing_uses_the_scope_not_the_dataclass(env, other_listing):
    assert await env.bazaar_repo.save_listing(
        _listing("pid-mine", space_id=_OTHER_SPACE_ID), space_id=_DEFAULT_SPACE_ID
    )
    assert (await env.bazaar_repo.get_listing("pid-mine")).space_id == (
        _DEFAULT_SPACE_ID
    )


async def test_status_mutators_are_scoped(env, other_listing):
    pid, _ = other_listing
    assert not await env.bazaar_repo.mark_expired(pid, space_id=_DEFAULT_SPACE_ID)
    assert not await env.bazaar_repo.mark_cancelled(pid, space_id=_DEFAULT_SPACE_ID)
    with pytest.raises(ValueError):
        await env.bazaar_repo.mark_sold(
            pid, space_id=_DEFAULT_SPACE_ID, winner_user_id="evil", winning_price=1
        )
    got = await env.bazaar_repo.get_listing(pid)
    assert got.status == BazaarStatus.ACTIVE
    assert got.winner_user_id is None
    assert await env.bazaar_repo.mark_cancelled(pid, space_id=_OTHER_SPACE_ID)


async def test_place_bid_is_scoped(env, other_listing):
    pid, _ = other_listing
    evil = new_bid(listing_post_id=pid, bidder_user_id="evil", amount=500)
    with pytest.raises(ValueError):
        await env.bazaar_repo.place_bid(evil, space_id=_DEFAULT_SPACE_ID)
    assert await env.bazaar_repo.get_bid(evil.id) is None


async def test_accept_offer_is_scoped(env, other_listing):
    _pid, bid_id = other_listing
    with pytest.raises(ValueError):
        await env.bazaar_repo.accept_offer(bid_id, space_id=_DEFAULT_SPACE_ID)
    assert not (await env.bazaar_repo.get_bid(bid_id)).accepted
    await env.bazaar_repo.accept_offer(bid_id, space_id=_OTHER_SPACE_ID)
    assert (await env.bazaar_repo.get_bid(bid_id)).accepted


async def test_list_sync_page_follows_the_wrapper_posts_window(env):
    """§25.6: every listing whose wrapper post streams — live, inside the
    retention window — page by page, never a fixed count."""
    for pid in ("bz-new-1", "bz-new-2", "bz-old", "bz-gone"):
        await _seed_listing(env, pid)
    await env.db.enqueue(
        "UPDATE space_posts SET created_at='2020-01-01 00:00:00' WHERE id='bz-old'"
    )
    await env.db.enqueue("UPDATE space_posts SET deleted=1 WHERE id='bz-gone'")
    seen: list[str] = []
    cursor = None
    while True:
        page, cursor = await env.bazaar_repo.list_sync_page(
            _DEFAULT_SPACE_ID, cursor=cursor, limit=1
        )
        seen.extend(lst.post_id for lst in page)
        if cursor is None:
            break
    # Newest stored first; a deleted post's listing never streams.
    assert seen == ["bz-old", "bz-new-2", "bz-new-1"]
    windowed, _ = await env.bazaar_repo.list_sync_page(
        _DEFAULT_SPACE_ID, cutoff="2025-01-01 00:00:00"
    )
    assert {lst.post_id for lst in windowed} == {"bz-new-1", "bz-new-2"}
    exempt, _ = await env.bazaar_repo.list_sync_page(
        _DEFAULT_SPACE_ID, cutoff="2025-01-01 00:00:00", exempt_types=("bazaar",)
    )
    assert {lst.post_id for lst in exempt} == {"bz-new-1", "bz-new-2", "bz-old"}
    other, _ = await env.bazaar_repo.list_sync_page("space-elsewhere")
    assert other == []
