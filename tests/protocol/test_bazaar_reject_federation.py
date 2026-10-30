"""§27.9 release blocker: a declined bid and the seller's reason cross
households.

Marked ``@pytest.mark.security``. The same five households as
:mod:`tests.protocol.test_bazaar_offer_federation` (space ``SP`` hosted on
**a**, anna sells; bob on **b** and cara on **c** are members, dora on
**d** follows, eve on **e** is no member at all), real crypto, real SQLite,
the real §24.11 pipeline and the real bazaar / notification services.

Proves: a declined auction bid reaches every member household's copy of
the bid (the high bid stays the same everywhere) but the seller's reason
only bob's household; a declined offer's reason reaches bob's household
only; neither reason rides outside the ciphertext; a decline from anyone
but the seller's household, a replay, a second decline and an over-long
reason change nothing; an older household is sent nothing.
"""

from __future__ import annotations

import json

import pytest

from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.post import BAZAAR_REJECT_REASON_MAX_CHARS
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

#: Long and unique, so finding it in a wire capture can only mean a leak.
REASON = "my sister wants the blue bike after all, sorry Bob, maybe next time"


async def _auction(world) -> None:
    for h in (world.a, world.b, world.c, world.d):
        await h.db.enqueue(
            "UPDATE bazaar_listings SET mode='auction', start_price=100, step_price=10"
        )


async def _bid(h, bid_id: str) -> tuple | None:
    row = await h.db.fetchone(
        "SELECT rejected, rejection_reason FROM bazaar_bids WHERE id=?", (bid_id,)
    )
    return None if row is None else (bool(row["rejected"]), row["rejection_reason"])


async def _offer(h, offer_id: str) -> tuple | None:
    row = await h.db.fetchone(
        "SELECT status, rejection_reason FROM bazaar_offers WHERE id=?", (offer_id,)
    )
    return None if row is None else (row["status"], row["rejection_reason"])


def _answers(world) -> list[tuple[str, str]]:
    return [(f, t) for f, t, _ in _traffic(world, "bazaar_offer_updated")]


# ── A declined auction bid ────────────────────────────────────────────────


async def test_a_declined_bid_reaches_the_bidder_with_the_reason(world):
    a, b, c, d, e = world.a, world.b, world.c, world.d, world.e
    await _auction(world)
    bid = await b.bazaar.place_bid(
        listing_post_id=LISTING, bidder_user_id="u-bob", amount=500
    )
    await a.bazaar.reject_offer(bid_id=bid.id, actor_user_id="u-anna", reason=REASON)
    # bob's household: declined, with anna's words; bob is told.
    assert await _bid(b, bid.id) == (True, REASON)
    assert await _notif_types(b, "u-bob") == ["bazaar_offer_rejected"]
    # Every other member household's copy of the bid is declined too, so
    # the high bid reads the same everywhere — but without the reason.
    assert await _bid(c, bid.id) == (True, None)
    assert await _bid(d, bid.id) == (True, None)
    assert await c.bazaar_repo.highest_bid(LISTING) is None
    # Nobody else is told; the non-member household holds nothing.
    assert await _notif_types(c, "u-cara") == []
    assert await _bid(e, bid.id) is None
    assert sorted(_answers(world)) == [("a", "b"), ("a", "c"), ("a", "d")]


async def test_a_bid_declined_without_a_reason_still_reaches_the_bidder(world):
    a, b = world.a, world.b
    await _auction(world)
    bid = await b.bazaar.place_bid(
        listing_post_id=LISTING, bidder_user_id="u-bob", amount=500
    )
    await a.bazaar.reject_offer(bid_id=bid.id, actor_user_id="u-anna")
    assert await _bid(b, bid.id) == (True, None)
    assert await _notif_types(b, "u-bob") == ["bazaar_offer_rejected"]


async def test_the_bid_list_shows_the_reason_to_seller_and_bidder_only(world):
    """Each side reads the words in its own bid list; cara sees the bid
    declined on her household, never why."""
    a, b, c = world.a, world.b, world.c
    await _auction(world)
    bid = await b.bazaar.place_bid(
        listing_post_id=LISTING, bidder_user_id="u-bob", amount=500
    )
    await a.bazaar.reject_offer(bid_id=bid.id, actor_user_id="u-anna", reason=REASON)
    [seen_by_anna] = await a.bazaar.list_bids(LISTING, viewer_user_id="u-anna")
    [seen_by_bob] = await b.bazaar.list_bids(LISTING, viewer_user_id="u-bob")
    [seen_by_cara] = await c.bazaar.list_bids(LISTING, viewer_user_id="u-cara")
    assert seen_by_anna.rejection_reason == seen_by_bob.rejection_reason == REASON
    assert (seen_by_cara.rejected, seen_by_cara.rejection_reason) == (True, None)


# ── A declined offer's reason ─────────────────────────────────────────────


async def test_a_declined_offers_reason_reaches_the_buyer_only(world):
    a, b, c, d, e = world.a, world.b, world.c, world.d, world.e
    offer = await b.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-bob", amount=5000
    )
    await a.bazaar.reject_fixed_offer(
        offer_id=offer.id, actor_user_id="u-anna", reason=REASON
    )
    assert await _offer(a, offer.id) == ("rejected", REASON)
    assert await _offer(b, offer.id) == ("rejected", REASON)
    assert await _notif_types(b, "u-bob") == ["bazaar_offer_rejected"]
    for h in (c, d, e):
        assert await _offer(h, offer.id) is None
    assert _answers(world) == [("a", "b")]


async def test_no_reason_rides_outside_the_ciphertext(world):
    a, b = world.a, world.b
    offer = await b.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-bob", amount=5000
    )
    await a.bazaar.reject_fixed_offer(
        offer_id=offer.id, actor_user_id="u-anna", reason=REASON
    )
    await _auction(world)
    bid = await b.bazaar.place_bid(
        listing_post_id=LISTING, bidder_user_id="u-bob", amount=500
    )
    await a.bazaar.reject_offer(bid_id=bid.id, actor_user_id="u-anna", reason=REASON)
    traffic = _traffic(world, "bazaar_offer_updated")
    assert len(traffic) == 4  # the offer's answer + the bid's to b, c, d
    for _frm, _to, env in traffic:
        raw = json.dumps(env)
        for leak in (REASON, offer.id, bid.id, LISTING, "u-bob", "u-anna"):
            assert leak not in raw, leak
        assert "encrypted_payload" in env
        for field in ("reason", "status", "bid_id", "offer_id"):
            assert field not in env


# ── Refusals over the real pipeline ───────────────────────────────────────


def _decline(bid_id: str, reason: str | None = "x") -> dict:
    return {
        "bid_id": bid_id,
        "listing_post_id": LISTING,
        "status": "rejected",
        **({"reason": reason} if reason is not None else {}),
    }


@pytest.mark.parametrize(
    ("label", "sender", "target"),
    [
        ("another member household", "c", "b"),
        ("the bidder's own household", "b", "c"),
        ("a follower-only household", "d", "b"),
        ("a household that is no member", "e", "b"),
    ],
)
async def test_only_the_sellers_household_declines_a_bid(world, label, sender, target):
    await _auction(world)
    bid = await world.b.bazaar.place_bid(
        listing_post_id=LISTING, bidder_user_id="u-bob", amount=500
    )
    victim = getattr(world, target)
    await _forge(
        getattr(world, sender), victim, FET.BAZAAR_OFFER_UPDATED, _decline(bid.id)
    )
    assert await _bid(victim, bid.id) == (False, None), label
    assert await _notif_types(world.b, "u-bob") == [], label


async def test_a_replayed_decline_changes_nothing(world):
    a, b = world.a, world.b
    await _auction(world)
    bid = await b.bazaar.place_bid(
        listing_post_id=LISTING, bidder_user_id="u-bob", amount=500
    )
    await a.bazaar.reject_offer(bid_id=bid.id, actor_user_id="u-anna", reason=REASON)
    [env] = [e for _f, t, e in _traffic(world, "bazaar_offer_updated") if t == "b"]
    with pytest.raises(ValueError):
        await b.federation.handle_inbound_envelope(
            "b-inbox-for-a", json.dumps(env).encode()
        )
    # Nor does a fresh, second decline from the seller's household swap
    # the words bob already has, or tell him twice.
    await _forge(a, b, FET.BAZAAR_OFFER_UPDATED, _decline(bid.id, "changed my mind"))
    assert await _bid(b, bid.id) == (True, REASON)
    assert await _notif_types(b, "u-bob") == ["bazaar_offer_rejected"]


async def test_an_over_long_reason_is_refused_by_the_buyers_household(world):
    a, b = world.a, world.b
    offer = await b.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-bob", amount=5000
    )
    await _auction(world)
    bid = await b.bazaar.place_bid(
        listing_post_id=LISTING, bidder_user_id="u-bob", amount=500
    )
    long = "x" * (BAZAAR_REJECT_REASON_MAX_CHARS + 1)
    await _forge(
        a,
        b,
        FET.BAZAAR_OFFER_UPDATED,
        {
            "offer_id": offer.id,
            "listing_post_id": LISTING,
            "status": "rejected",
            "reason": long,
        },
    )
    await _forge(a, b, FET.BAZAAR_OFFER_UPDATED, _decline(bid.id, long))
    assert await _offer(b, offer.id) == ("pending", None)
    assert await _bid(b, bid.id) == (False, None)
    assert await _notif_types(b, "u-bob") == []


async def test_an_older_household_is_sent_no_decline(world):
    a, b = world.a, world.b
    await _auction(world)
    bid = await b.bazaar.place_bid(
        listing_post_id=LISTING, bidder_user_id="u-bob", amount=500
    )
    # (``set_proto_version`` is a high-water mark: lower it by hand.)
    await a.db.enqueue(
        "UPDATE remote_instances SET proto_version=? WHERE id=?",
        (FederationCapability.MIN_FOR_BAZAAR_OFFERS - 1, b.iid),
    )
    await a.bazaar.reject_offer(bid_id=bid.id, actor_user_id="u-anna", reason=REASON)
    # As before: bob's household keeps an open bid; the others still hear.
    assert await _bid(b, bid.id) == (False, None)
    assert sorted(_answers(world)) == [("a", "c"), ("a", "d")]
