"""Tests for socialhome.repositories.bazaar_repo."""

from __future__ import annotations

import uuid

import pytest

from socialhome.domain.post import BazaarListing, BazaarMode, BazaarStatus
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


async def _seed_post(db, post_id: str):
    """Insert a minimal feed_posts row to satisfy the FK constraint."""
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content) VALUES(?,?,?,?)",
        (post_id, "u1", "bazaar", "listing"),
    )


async def test_bazaar_listing_lifecycle(env):
    """Fixed-price listing: create, mark sold, attempt double-sold is rejected."""
    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)

    listing = BazaarListing(
        post_id=pid,
        seller_user_id="u1",
        mode=BazaarMode.FIXED,
        title="Old bike",
        end_time="2099-01-01T00:00:00",
        currency="EUR",
        status=BazaarStatus.ACTIVE,
        created_at=None,
        price=5000,
    )
    saved = await env.bazaar_repo.save_listing(listing)
    assert saved.title == "Old bike"

    active = await env.bazaar_repo.list_active()
    assert any(lst.post_id == pid for lst in active)

    await env.bazaar_repo.mark_sold(pid, winner_user_id="u2", winning_price=4500)
    got = await env.bazaar_repo.get_listing(pid)
    assert got.status == BazaarStatus.SOLD
    assert got.winner_user_id == "u2"

    with pytest.raises(ValueError):
        await env.bazaar_repo.mark_sold(pid, winner_user_id="u3", winning_price=4000)


async def test_bazaar_bid_state_machine(env):
    """Offer mode: place bids, withdraw one, accept another (sibling auto-rejected)."""
    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)
    listing = BazaarListing(
        post_id=pid,
        seller_user_id="u1",
        mode=BazaarMode.OFFER,
        title="Guitar",
        end_time="2099-01-01T00:00:00",
        currency="USD",
        status=BazaarStatus.ACTIVE,
        created_at=None,
        price=20000,
    )
    await env.bazaar_repo.save_listing(listing)

    bid_a = new_bid(listing_post_id=pid, bidder_user_id="buyer_a", amount=18000)
    bid_b = new_bid(listing_post_id=pid, bidder_user_id="buyer_b", amount=19000)
    await env.bazaar_repo.place_bid(bid_a)
    await env.bazaar_repo.place_bid(bid_b)

    await env.bazaar_repo.withdraw_bid(bid_a.id)
    got_a = await env.bazaar_repo.get_bid(bid_a.id)
    assert got_a.withdrawn

    with pytest.raises(BidStateError):
        await env.bazaar_repo.withdraw_bid(bid_a.id)

    await env.bazaar_repo.accept_offer(bid_b.id)
    got_b = await env.bazaar_repo.get_bid(bid_b.id)
    assert got_b.accepted

    with pytest.raises(BidStateError):
        await env.bazaar_repo.accept_offer(bid_b.id)


async def test_bazaar_reject_offer(env):
    """Seller rejects an offer; BidStateError on reject-after-accept."""
    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)
    listing = BazaarListing(
        post_id=pid,
        seller_user_id="u1",
        mode=BazaarMode.OFFER,
        title="Camera",
        end_time="2099-01-01T00:00:00",
        currency="GBP",
        status=BazaarStatus.ACTIVE,
        created_at=None,
        price=30000,
    )
    await env.bazaar_repo.save_listing(listing)

    bid = new_bid(listing_post_id=pid, bidder_user_id="buyer_x", amount=28000)
    await env.bazaar_repo.place_bid(bid)

    await env.bazaar_repo.accept_offer(bid.id)
    with pytest.raises(BidStateError):
        await env.bazaar_repo.reject_offer(bid.id, reason="changed mind")

    bid2 = new_bid(listing_post_id=pid, bidder_user_id="buyer_y", amount=27000)
    await env.bazaar_repo.place_bid(bid2)
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
                seller_user_id="u1",
                mode=BazaarMode.FIXED,
                title="Item",
                end_time=end,
                currency="EUR",
                status=BazaarStatus.ACTIVE,
                created_at=None,
                price=100,
            )
        )

    expired = await env.bazaar_repo.list_expired()
    expired_ids = {lst.post_id for lst in expired}
    assert pid1 in expired_ids
    assert pid2 not in expired_ids

    await env.bazaar_repo.mark_expired(pid1)
    assert (await env.bazaar_repo.get_listing(pid1)).status == BazaarStatus.EXPIRED

    await env.bazaar_repo.mark_cancelled(pid2)
    assert (await env.bazaar_repo.get_listing(pid2)).status == BazaarStatus.CANCELLED


async def test_bazaar_currency_validation(env):
    """Invalid currency raises ValueError on save_listing."""
    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)
    listing = BazaarListing(
        post_id=pid,
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
        await env.bazaar_repo.save_listing(listing)


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
            seller_user_id="u1",
            mode=BazaarMode.AUCTION,
            title="Painting",
            end_time=close_soon,
            currency="EUR",
            status=BazaarStatus.ACTIVE,
            created_at=None,
            start_price=100,
            step_price=10,
        )
    )
    await env.bazaar_repo.place_bid(
        new_bid(
            listing_post_id=pid,
            bidder_user_id="u2",
            amount=110,
        )
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
            seller_user_id="u1",
            mode=BazaarMode.AUCTION,
            title="Vase",
            end_time=original_end,
            currency="EUR",
            status=BazaarStatus.ACTIVE,
            created_at=None,
            start_price=100,
            step_price=10,
        )
    )
    await env.bazaar_repo.place_bid(
        new_bid(
            listing_post_id=pid,
            bidder_user_id="u2",
            amount=110,
        )
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
            seller_user_id="u1",
            mode=BazaarMode.OFFER,
            title="Clock",
            end_time=close_soon,
            currency="EUR",
            status=BazaarStatus.ACTIVE,
            created_at=None,
            price=100,
        )
    )
    await env.bazaar_repo.place_bid(
        new_bid(
            listing_post_id=pid,
            bidder_user_id="u2",
            amount=100,
        )
    )
    listing = await env.bazaar_repo.get_listing(pid)
    assert listing.end_time == close_soon
