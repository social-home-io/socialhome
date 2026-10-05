"""Coverage fill for :class:`BazaarService` — update/cancel/reject/withdraw."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.post import BazaarListing, BazaarMode, BazaarStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.bazaar_repo import SqliteBazaarRepo
from socialhome.services.bazaar_service import (
    BazaarService,
    BazaarServiceError,
    BidNotFoundError,
    BidTooLowError,
    ListingNotActiveError,
    ListingNotFoundError,
    OwnListingError,
    _coerce_mode,
    _validate_price_fields,
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
    yield e
    await db.shutdown()


_DEFAULT_SPACE_ID = "space-bazaar-cov"


async def _seed_listing(env, *, mode: BazaarMode, seller="u-seller") -> str:
    pid = uuid.uuid4().hex
    await env.db.enqueue(
        """
        INSERT OR IGNORE INTO spaces(
            id, name, owner_instance_id, owner_username, identity_public_key
        ) VALUES(?, ?, ?, ?, ?)
        """,
        (_DEFAULT_SPACE_ID, "Test Space", "iid-test", seller, "00" * 32),
    )
    await env.db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content) "
        "VALUES(?,?,?,?,?)",
        (pid, _DEFAULT_SPACE_ID, seller, "bazaar", ""),
    )
    end_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    await env.repo.save_listing(
        BazaarListing(
            post_id=pid,
            space_id=_DEFAULT_SPACE_ID,
            seller_user_id=seller,
            mode=mode,
            title="Item",
            end_time=end_iso,
            currency="EUR",
            status=BazaarStatus.ACTIVE,
            created_at=None,
            start_price=100,
            step_price=10,
            price=100,
        ),
        space_id=_DEFAULT_SPACE_ID,
    )
    return pid


# ── create_listing validation helpers (pure) ─────────────────────────────


def test_coerce_mode_from_string():
    assert _coerce_mode("auction") == BazaarMode.AUCTION


def test_coerce_mode_passthrough():
    assert _coerce_mode(BazaarMode.FIXED) == BazaarMode.FIXED


def test_coerce_mode_bad_value_raises():
    with pytest.raises(ValueError):
        _coerce_mode("bogus")


def test_validate_price_fixed_requires_price():
    with pytest.raises(ValueError):
        _validate_price_fields(BazaarMode.FIXED, None, None, None)


def test_validate_price_auction_requires_start_price():
    with pytest.raises(ValueError):
        _validate_price_fields(BazaarMode.AUCTION, None, None, None)


def test_validate_price_negative_step_rejected():
    with pytest.raises(ValueError):
        _validate_price_fields(BazaarMode.AUCTION, None, 100, -1)


def test_validate_price_auction_ok():
    _validate_price_fields(BazaarMode.AUCTION, None, 100, 10)


def test_validate_price_fixed_ok():
    _validate_price_fields(BazaarMode.FIXED, 100, None, None)


# ── update_listing ───────────────────────────────────────────────────────


class _OpenSpaces:
    """Stands in for SpaceService: the posts level lets every edit through."""

    async def require_content_access(self, *_a, **_kw):
        return None


async def test_update_listing_non_seller_raises(env):
    pid = await _seed_listing(env, mode=BazaarMode.FIXED)
    with pytest.raises(PermissionError):
        await env.svc.update_listing(
            post_id=pid,
            actor_user_id="u-stranger",
            title="X",
        )


async def test_update_listing_inactive_raises(env):
    pid = await _seed_listing(env, mode=BazaarMode.FIXED)
    await env.repo.mark_cancelled(pid, space_id=_DEFAULT_SPACE_ID)
    with pytest.raises(BazaarServiceError):
        await env.svc.update_listing(
            post_id=pid,
            actor_user_id="u-seller",
            title="X",
        )


async def test_update_listing_empty_title_raises(env):
    env.svc.attach_spaces(_OpenSpaces())
    pid = await _seed_listing(env, mode=BazaarMode.FIXED)
    with pytest.raises(ValueError):
        await env.svc.update_listing(
            post_id=pid,
            actor_user_id="u-seller",
            title="   ",
        )


async def test_update_listing_too_long_title_raises(env):
    env.svc.attach_spaces(_OpenSpaces())
    pid = await _seed_listing(env, mode=BazaarMode.FIXED)
    with pytest.raises(ValueError):
        await env.svc.update_listing(
            post_id=pid,
            actor_user_id="u-seller",
            title="x" * 201,
        )


async def test_update_listing_happy_path(env):
    env.svc.attach_spaces(_OpenSpaces())
    pid = await _seed_listing(env, mode=BazaarMode.FIXED)
    updated = await env.svc.update_listing(
        post_id=pid,
        actor_user_id="u-seller",
        title="New Title",
        description="Updated desc",
    )
    assert updated.title == "New Title"
    assert updated.description == "Updated desc"


# ── cancel_listing ───────────────────────────────────────────────────────


async def test_cancel_listing_non_seller_raises(env):
    pid = await _seed_listing(env, mode=BazaarMode.FIXED)
    with pytest.raises(PermissionError):
        await env.svc.cancel_listing(
            post_id=pid,
            actor_user_id="u-stranger",
        )


async def test_cancel_listing_inactive_raises(env):
    pid = await _seed_listing(env, mode=BazaarMode.FIXED)
    await env.repo.mark_cancelled(pid, space_id=_DEFAULT_SPACE_ID)
    with pytest.raises(BazaarServiceError):
        await env.svc.cancel_listing(
            post_id=pid,
            actor_user_id="u-seller",
        )


async def test_cancel_listing_happy(env):
    pid = await _seed_listing(env, mode=BazaarMode.FIXED)
    await env.svc.cancel_listing(
        post_id=pid,
        actor_user_id="u-seller",
    )
    listing = await env.repo.get_listing(pid)
    assert listing.status == BazaarStatus.CANCELLED


# ── place_bid validation ─────────────────────────────────────────────────


async def test_place_bid_self_raises(env):
    pid = await _seed_listing(env, mode=BazaarMode.AUCTION)
    with pytest.raises(OwnListingError):
        await env.svc.place_bid(
            listing_post_id=pid,
            bidder_user_id="u-seller",
            amount=200,
        )


async def test_place_bid_non_positive_raises(env):
    pid = await _seed_listing(env, mode=BazaarMode.AUCTION)
    with pytest.raises(ValueError):
        await env.svc.place_bid(
            listing_post_id=pid,
            bidder_user_id="u-b",
            amount=0,
        )


async def test_place_bid_below_floor_raises(env):
    pid = await _seed_listing(env, mode=BazaarMode.AUCTION)
    await env.svc.place_bid(
        listing_post_id=pid,
        bidder_user_id="u-b",
        amount=110,
    )
    # Next bid must clear 110 + step=10 → 120.
    with pytest.raises(BidTooLowError) as exc_info:
        await env.svc.place_bid(
            listing_post_id=pid,
            bidder_user_id="u-c",
            amount=115,
        )
    assert exc_info.value.params == {"floor_amount": 120, "currency": "EUR"}


async def test_place_bid_on_inactive_raises(env):
    pid = await _seed_listing(env, mode=BazaarMode.AUCTION)
    await env.repo.mark_cancelled(pid, space_id=_DEFAULT_SPACE_ID)
    with pytest.raises(ListingNotActiveError):
        await env.svc.place_bid(
            listing_post_id=pid,
            bidder_user_id="u-b",
            amount=500,
        )


# ── reject_offer / withdraw_bid ──────────────────────────────────────────


async def test_reject_offer_unknown_bid_raises(env):
    with pytest.raises(BidNotFoundError):
        await env.svc.reject_offer(bid_id="ghost", actor_user_id="u")


async def test_reject_offer_non_seller_raises(env):
    pid = await _seed_listing(env, mode=BazaarMode.OFFER)
    bid = await env.svc.place_bid(
        listing_post_id=pid,
        bidder_user_id="u-b",
        amount=50,
    )
    with pytest.raises(PermissionError):
        await env.svc.reject_offer(
            bid_id=bid.id,
            actor_user_id="u-stranger",
        )


async def test_reject_offer_happy(env):
    pid = await _seed_listing(env, mode=BazaarMode.OFFER)
    bid = await env.svc.place_bid(
        listing_post_id=pid,
        bidder_user_id="u-b",
        amount=50,
    )
    await env.svc.reject_offer(
        bid_id=bid.id,
        actor_user_id="u-seller",
        reason="too low",
    )


async def test_withdraw_bid_unknown_raises(env):
    with pytest.raises(BidNotFoundError):
        await env.svc.withdraw_bid(bid_id="ghost", actor_user_id="u")


async def test_withdraw_bid_non_bidder_raises(env):
    pid = await _seed_listing(env, mode=BazaarMode.AUCTION)
    bid = await env.svc.place_bid(
        listing_post_id=pid,
        bidder_user_id="u-b",
        amount=110,
    )
    with pytest.raises(PermissionError):
        await env.svc.withdraw_bid(
            bid_id=bid.id,
            actor_user_id="u-other",
        )


async def test_withdraw_bid_happy(env):
    pid = await _seed_listing(env, mode=BazaarMode.AUCTION)
    bid = await env.svc.place_bid(
        listing_post_id=pid,
        bidder_user_id="u-b",
        amount=110,
    )
    await env.svc.withdraw_bid(
        bid_id=bid.id,
        actor_user_id="u-b",
    )


async def test_list_bids_empty(env):
    pid = await _seed_listing(env, mode=BazaarMode.AUCTION)
    rows = await env.svc.list_bids(pid)
    assert rows == []


async def test_get_listing_unknown_raises(env):
    with pytest.raises(ListingNotFoundError):
        await env.svc.get_listing("ghost")


class _AdminOnlySpaces:
    """Stands in for SpaceService: every posts write is ADMIN_ONLY-refused."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def require_content_access(
        self, space_id, actor_user_id, feature, action, *, owns_target
    ):
        from socialhome.domain.space import AccessAdminOnlyError

        self.calls.append((space_id, actor_user_id, feature, action, owns_target))
        raise AccessAdminOnlyError(feature)


async def test_update_listing_is_a_posts_edit_under_admin_only(env):
    """A listing's title / description is its wrapper post's content — an
    ADMIN_ONLY posts level refuses the seller's edit, row unchanged."""
    from socialhome.domain.space import AccessAdminOnlyError, ContentAction

    pid = await _seed_listing(env, mode=BazaarMode.FIXED)
    spaces = _AdminOnlySpaces()
    env.svc.attach_spaces(spaces)
    with pytest.raises(AccessAdminOnlyError):
        await env.svc.update_listing(
            post_id=pid, actor_user_id="u-seller", title="Changed"
        )
    assert spaces.calls == [
        (_DEFAULT_SPACE_ID, "u-seller", "posts", ContentAction.EDIT, True)
    ]
    assert (await env.repo.get_listing(pid)).title == "Item"


async def test_update_listing_fails_closed_without_the_space_service(env):
    """No space service → no access gate → no edit (never fail open)."""
    pid = await _seed_listing(env, mode=BazaarMode.FIXED)
    with pytest.raises(RuntimeError):
        await env.svc.update_listing(post_id=pid, actor_user_id="u-seller", title="X")
    assert (await env.repo.get_listing(pid)).title == "Item"
