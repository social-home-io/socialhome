"""Tests for BazaarService + BazaarExpiryScheduler (§9, §23.15)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    BazaarBidPlaced,
    BazaarListingExpired,
    BazaarOfferAccepted,
)
from socialhome.domain.post import BazaarListing, BazaarMode, BazaarStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.bazaar_repo import SqliteBazaarRepo
from socialhome.services.bazaar_service import (
    BazaarExpiryScheduler,
    BazaarService,
    BidNotFoundError,
    ListingNotFoundError,
)


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class E:
        pass

    e = E()
    e.db = db
    e.repo = SqliteBazaarRepo(db)
    e.bus = EventBus()
    e.svc = BazaarService(e.repo, e.bus)
    e.events: list = []

    async def _capture(evt):
        e.events.append(evt)

    e.bus.subscribe(BazaarBidPlaced, _capture)
    e.bus.subscribe(BazaarOfferAccepted, _capture)
    e.bus.subscribe(BazaarListingExpired, _capture)
    yield e
    await db.shutdown()


_DEFAULT_SPACE_ID = "space-bazaar-svc"


async def _seed_post(db, post_id: str, space_id: str = _DEFAULT_SPACE_ID):
    await db.enqueue(
        """
        INSERT OR IGNORE INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key
        ) VALUES(?, ?, ?, ?, ?)
        """,
        (space_id, "Test Space", "iid-test", "u-seller", "00" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content) "
        "VALUES(?,?,?,?,?)",
        (post_id, space_id, "u-seller", "bazaar", ""),
    )


async def _seed_listing(env, *, mode: BazaarMode, end_in: timedelta) -> str:
    pid = uuid.uuid4().hex
    await _seed_post(env.db, pid)
    end_iso = (datetime.now(timezone.utc) + end_in).isoformat()
    await env.repo.save_listing(
        BazaarListing(
            post_id=pid,
            space_id=_DEFAULT_SPACE_ID,
            seller_user_id="u-seller",
            mode=mode,
            title="Item",
            end_time=end_iso,
            currency="EUR",
            status=BazaarStatus.ACTIVE,
            created_at=None,
            start_price=100,
            step_price=10,
            price=100,
        )
    )
    return pid


# ─── place_bid ────────────────────────────────────────────────────────────


async def test_place_bid_publishes_bid_placed_event(env):
    pid = await _seed_listing(env, mode=BazaarMode.AUCTION, end_in=timedelta(hours=1))
    await env.svc.place_bid(
        listing_post_id=pid,
        bidder_user_id="u-bidder",
        amount=110,
    )
    placed = [e for e in env.events if isinstance(e, BazaarBidPlaced)]
    assert len(placed) == 1
    assert placed[0].amount == 110
    assert placed[0].seller_user_id == "u-seller"
    assert placed[0].bidder_user_id == "u-bidder"


async def test_place_bid_carries_extended_end_time(env):
    """Anti-snipe: bid in last 5 min pushes end_time and the event reflects it."""
    pid = await _seed_listing(
        env, mode=BazaarMode.AUCTION, end_in=timedelta(seconds=60)
    )
    await env.svc.place_bid(
        listing_post_id=pid,
        bidder_user_id="u-bidder",
        amount=110,
    )
    placed = [e for e in env.events if isinstance(e, BazaarBidPlaced)]
    new_end = datetime.fromisoformat(
        placed[0].new_end_time.replace("Z", "+00:00"),
    )
    if new_end.tzinfo is None:
        new_end = new_end.replace(tzinfo=timezone.utc)
    # New end should be ~5 min in the future.
    assert (new_end - datetime.now(timezone.utc)).total_seconds() > 60


async def test_place_bid_unknown_listing_raises(env):
    with pytest.raises(ListingNotFoundError):
        await env.svc.place_bid(
            listing_post_id="ghost",
            bidder_user_id="u",
            amount=100,
        )


# ─── accept_offer ─────────────────────────────────────────────────────────


async def test_accept_offer_marks_sold_and_publishes(env):
    pid = await _seed_listing(env, mode=BazaarMode.OFFER, end_in=timedelta(days=1))
    bid = await env.svc.place_bid(
        listing_post_id=pid,
        bidder_user_id="u-bidder",
        amount=120,
    )
    await env.svc.accept_offer(bid_id=bid.id, actor_user_id="u-seller")
    listing = await env.repo.get_listing(pid)
    assert listing.status == BazaarStatus.SOLD
    accepted = [e for e in env.events if isinstance(e, BazaarOfferAccepted)]
    assert accepted[0].buyer_user_id == "u-bidder"
    assert accepted[0].price == 120


async def test_accept_offer_by_non_seller_forbidden(env):
    pid = await _seed_listing(env, mode=BazaarMode.OFFER, end_in=timedelta(days=1))
    bid = await env.svc.place_bid(
        listing_post_id=pid,
        bidder_user_id="u-bidder",
        amount=120,
    )
    with pytest.raises(PermissionError):
        await env.svc.accept_offer(bid_id=bid.id, actor_user_id="u-stranger")


async def test_accept_unknown_bid_raises(env):
    with pytest.raises(BidNotFoundError):
        await env.svc.accept_offer(bid_id="ghost", actor_user_id="u")


# ─── expire_due ───────────────────────────────────────────────────────────


async def test_expire_due_closes_past_auctions_with_winner(env):
    pid = await _seed_listing(env, mode=BazaarMode.AUCTION, end_in=timedelta(hours=1))
    await env.svc.place_bid(
        listing_post_id=pid,
        bidder_user_id="u-bidder",
        amount=200,
    )
    # Force the listing into the past.
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    await env.db.enqueue(
        "UPDATE bazaar_listings SET end_time=? WHERE post_id=?",
        (past, pid),
    )
    closed = await env.svc.expire_due()
    assert closed == 1
    listing = await env.repo.get_listing(pid)
    assert listing.status == BazaarStatus.SOLD
    assert any(isinstance(e, BazaarListingExpired) for e in env.events)


async def test_expire_due_closes_no_bid_listing_as_expired(env):
    pid = await _seed_listing(env, mode=BazaarMode.AUCTION, end_in=timedelta(hours=1))
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    await env.db.enqueue(
        "UPDATE bazaar_listings SET end_time=? WHERE post_id=?",
        (past, pid),
    )
    closed = await env.svc.expire_due()
    assert closed == 1
    listing = await env.repo.get_listing(pid)
    assert listing.status == BazaarStatus.EXPIRED


# ─── Scheduler ────────────────────────────────────────────────────────────


async def test_scheduler_double_start_idempotent(env):
    s = BazaarExpiryScheduler(env.svc, interval_seconds=10.0)
    await s.start()
    await s.start()  # no-op
    await s.stop()


async def test_scheduler_stop_without_start_safe(env):
    s = BazaarExpiryScheduler(env.svc)
    await s.stop()


async def test_scheduler_loop_calls_expire(env):
    s = BazaarExpiryScheduler(env.svc, interval_seconds=0.05)
    await s.start()
    await asyncio.sleep(0.12)
    await s.stop()
