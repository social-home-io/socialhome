"""§27.9 release blocker: a withdrawn auction bid crosses households.

Marked ``@pytest.mark.security``. The same five households as
:mod:`tests.protocol.test_bazaar_offer_federation` (space ``SP`` hosted on
**a**, anna sells; bob on **b** and cara on **c** are members, dora on
**d** follows, eve on **e** is no member at all), real crypto, real SQLite,
the real §24.11 pipeline and the real bazaar / notification services.

Proves: bob's withdrawn bid is withdrawn on every member household's copy
(the high bid falls back to the next one everywhere, so it can no longer
set the floor or win) and the non-member household holds nothing; nothing
rides outside the ciphertext; a withdrawal from anyone but the household
seating the bidder, a replay, a withdrawal of a bid already declined and
one after the auction ended change nothing; an older household is sent
nothing; the seller is not notified.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.services.bazaar_service import BazaarServiceError
from tests.protocol.test_bazaar_offer_federation import (
    LISTING,
    _forge,
    _notif_types,
    _traffic,
    world,
)

pytestmark = pytest.mark.security

FET = FederationEventType
__all__ = ["world"]


async def _auction(world) -> None:
    for h in (world.a, world.b, world.c, world.d):
        await h.db.enqueue(
            "UPDATE bazaar_listings SET mode='auction', start_price=100, step_price=10"
        )


async def _withdrawn(h, bid_id: str) -> bool | None:
    row = await h.db.fetchone("SELECT withdrawn FROM bazaar_bids WHERE id=?", (bid_id,))
    return None if row is None else bool(row["withdrawn"])


async def _high(h) -> int | None:
    bid = await h.bazaar_repo.highest_bid(LISTING)
    return None if bid is None else bid.amount


def _withdrawals(world) -> list[tuple[str, str]]:
    return sorted((f, t) for f, t, _ in _traffic(world, "bazaar_offer_updated"))


def _withdraw(bid_id: str) -> dict:
    return {"bid_id": bid_id, "listing_post_id": LISTING, "status": "withdrawn"}


async def _two_bids(world):
    """cara bids 500, bob outbids her with 600 — every member mirrors both."""
    await _auction(world)
    cara = await world.c.bazaar.place_bid(
        listing_post_id=LISTING, bidder_user_id="u-cara", amount=500
    )
    bob = await world.b.bazaar.place_bid(
        listing_post_id=LISTING, bidder_user_id="u-bob", amount=600
    )
    for h in (world.a, world.b, world.c, world.d):
        assert await _high(h) == 600, h.name
    return cara, bob


# ── The round trip ────────────────────────────────────────────────────────


async def test_a_withdrawn_bid_is_withdrawn_on_every_member_household(world):
    a, b, c, d, e = world.a, world.b, world.c, world.d, world.e
    _cara, bob = await _two_bids(world)
    told_anna = await _notif_types(a, "u-anna")
    await b.bazaar.withdraw_bid(bid_id=bob.id, actor_user_id="u-bob")
    for h in (a, b, c, d):
        assert await _withdrawn(h, bob.id) is True, h.name
        # cara's bid is the high bid again everywhere: bob's no longer
        # sets the floor, nor can it win at the end.
        assert await _high(h) == 500, h.name
    assert await _withdrawn(e, bob.id) is None
    assert _withdrawals(world) == [("b", "a"), ("b", "c"), ("b", "d")]
    # Nobody is told of a withdrawal — the seller neither.
    assert await _notif_types(a, "u-anna") == told_anna
    assert await _notif_types(c, "u-cara") == []


async def test_no_withdrawal_field_rides_outside_the_ciphertext(world):
    _cara, bob = await _two_bids(world)
    await world.b.bazaar.withdraw_bid(bid_id=bob.id, actor_user_id="u-bob")
    traffic = _traffic(world, "bazaar_offer_updated")
    assert len(traffic) == 3
    for _frm, _to, env in traffic:
        raw = json.dumps(env)
        for leak in (bob.id, LISTING, "u-bob", "withdrawn"):
            assert leak not in raw, leak
        assert "encrypted_payload" in env
        for field in ("status", "bid_id"):
            assert field not in env


# ── Refusals over the real pipeline ───────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "sender", "target"),
    [
        ("the seller's household", "a", "c"),
        ("another member household", "c", "a"),
        ("a follower-only household", "d", "a"),
        ("a household that is no member", "e", "a"),
    ],
)
async def test_only_the_bidders_household_withdraws_a_bid(world, label, sender, target):
    _cara, bob = await _two_bids(world)
    victim = getattr(world, target)
    await _forge(
        getattr(world, sender), victim, FET.BAZAAR_OFFER_UPDATED, _withdraw(bob.id)
    )
    assert await _withdrawn(victim, bob.id) is False, label
    assert await _high(victim) == 600, label


async def test_a_replayed_withdrawal_changes_nothing(world):
    a, b = world.a, world.b
    _cara, bob = await _two_bids(world)
    await b.bazaar.withdraw_bid(bid_id=bob.id, actor_user_id="u-bob")
    [env] = [e for _f, t, e in _traffic(world, "bazaar_offer_updated") if t == "a"]
    with pytest.raises(ValueError):
        await a.federation.handle_inbound_envelope(
            "a-inbox-for-b", json.dumps(env).encode()
        )
    # A fresh second withdrawal is a no-op too.
    await _forge(b, a, FET.BAZAAR_OFFER_UPDATED, _withdraw(bob.id))
    assert await _withdrawn(a, bob.id) is True
    assert await _high(a) == 500


async def test_a_declined_bid_can_not_be_withdrawn(world):
    a, b, c = world.a, world.b, world.c
    _cara, bob = await _two_bids(world)
    await a.bazaar.reject_offer(bid_id=bob.id, actor_user_id="u-anna")
    with pytest.raises(BazaarServiceError):
        await b.bazaar.withdraw_bid(bid_id=bob.id, actor_user_id="u-bob")
    # Nor by sending it anyway: the decline stands everywhere.
    await _forge(b, c, FET.BAZAAR_OFFER_UPDATED, _withdraw(bob.id))
    row = await c.db.fetchone(
        "SELECT rejected, withdrawn FROM bazaar_bids WHERE id=?", (bob.id,)
    )
    assert (bool(row["rejected"]), bool(row["withdrawn"])) == (True, False)


async def test_a_bid_can_not_be_withdrawn_once_the_auction_ended(world):
    """Past ``end_time`` the high bid is the winner, even before the
    seller's household's expiry sweep closed the listing — the bidder
    can't back out of it there, nor here."""
    a, b = world.a, world.b
    _cara, bob = await _two_bids(world)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    for h in (a, b):
        await h.db.enqueue("UPDATE bazaar_listings SET end_time=?", (past,))
    with pytest.raises(BazaarServiceError):
        await b.bazaar.withdraw_bid(bid_id=bob.id, actor_user_id="u-bob")
    await _forge(b, a, FET.BAZAAR_OFFER_UPDATED, _withdraw(bob.id))
    assert await _withdrawn(a, bob.id) is False
    assert await _high(a) == 600
    await a.bazaar.expire_due()
    listing = await a.bazaar_repo.get_listing(LISTING)
    assert (listing.status.value, listing.winner_user_id) == ("sold", "u-bob")


async def test_a_bid_on_a_closed_listing_can_not_be_withdrawn(world):
    a, b = world.a, world.b
    _cara, bob = await _two_bids(world)
    for h in (a, b):
        await h.db.enqueue("UPDATE bazaar_listings SET status='cancelled'")
    with pytest.raises(BazaarServiceError):
        await b.bazaar.withdraw_bid(bid_id=bob.id, actor_user_id="u-bob")
    await _forge(b, a, FET.BAZAAR_OFFER_UPDATED, _withdraw(bob.id))
    assert await _withdrawn(a, bob.id) is False


async def test_an_older_household_is_sent_no_withdrawal(world):
    a, b = world.a, world.b
    _cara, bob = await _two_bids(world)
    # (``set_proto_version`` is a high-water mark: lower it by hand.)
    await b.db.enqueue(
        "UPDATE remote_instances SET proto_version=? WHERE id=?",
        (FederationCapability.MIN_FOR_BAZAAR_OFFERS - 1, a.iid),
    )
    await b.bazaar.withdraw_bid(bid_id=bob.id, actor_user_id="u-bob")
    # As before: the older household keeps the bid; the others hear.
    assert await _withdrawn(a, bob.id) is False
    assert _withdrawals(world) == [("b", "c"), ("b", "d")]
