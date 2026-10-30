"""Unit tests for :class:`BazaarOutbound`.

The wrapper ``PostType.BAZAAR`` post federates via :class:`SpacePostOutbound`
with just the caption; this service ships the full listing payload + image
bytes so remote members see what's actually for sale. These tests cover the
bus subscription, payload shape, and media outbox enqueue.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.domain.events import (
    BazaarBidPlaced,
    BazaarBidWithdrawn,
    BazaarListingCreated,
    BazaarOfferAccepted,
    BazaarOfferRejected,
)
from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.post import BazaarListing, BazaarMode, BazaarStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_remote_member_repo import SpaceRemoteMember
from socialhome.services.bazaar_outbound import BazaarOutbound
from socialhome.services.moderation_release import release_scope


def _make_listing(**overrides) -> BazaarListing:
    defaults = {
        "post_id": "bzr-1",
        "space_id": "sp-1",
        "seller_user_id": "u-seller",
        "mode": BazaarMode.FIXED,
        "title": "Vintage chair",
        "end_time": "2026-06-01T00:00:00+00:00",
        "currency": "USD",
        "status": BazaarStatus.ACTIVE,
        "created_at": "2026-05-23T10:00:00+00:00",
        "description": "A nice chair",
        "image_urls": ("api/media/chair-1.webp", "api/media/chair-2.webp"),
        "price": 4500,
    }
    defaults.update(overrides)
    return BazaarListing(**defaults)


@pytest.fixture
def federation_service():
    svc = MagicMock()
    svc.broadcast_to_space_members = AsyncMock()
    svc.peer_supports = AsyncMock(return_value=True)
    svc._own_instance_id = "inst-self"
    return svc


@pytest.fixture
def media_sync():
    m = MagicMock()
    m.enqueue_for_blob = AsyncMock()
    return m


@pytest.fixture
def federation_repo():
    r = MagicMock()
    r.list_member_instance_ids = AsyncMock(
        return_value=["inst-self", "inst-peer-a", "inst-peer-b"],
    )
    return r


async def test_listing_created_broadcasts_full_payload(
    federation_service,
    media_sync,
    federation_repo,
):
    """The bus event only has IDs — the outbound fetches the row and
    ships the COMPLETE :class:`BazaarListing` payload. Sub-v_10 peers
    are skipped via the ``min_proto_version`` gate."""
    bus = EventBus()
    bazaar_repo = MagicMock()
    listing = _make_listing()
    bazaar_repo.get_listing = AsyncMock(return_value=listing)
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    await bus.publish(
        BazaarListingCreated(
            listing_post_id="bzr-1",
            space_id="sp-1",
            seller_user_id="u-seller",
            mode="fixed",
            title="Vintage chair",
            occurred_at=datetime.fromisoformat(
                "2026-05-23T10:00:00+00:00",
            ),
        ),
    )

    # Broadcast called with the right event type + payload + version gate.
    federation_service.broadcast_to_space_members.assert_awaited_once()
    call = federation_service.broadcast_to_space_members.await_args
    assert call.args[0] == "sp-1"
    assert call.args[1] == FederationEventType.BAZAAR_LISTING_CREATED
    payload = call.args[2]
    assert payload["post_id"] == "bzr-1"
    assert payload["title"] == "Vintage chair"
    assert payload["mode"] == "fixed"
    assert payload["status"] == "active"
    assert payload["price"] == 4500
    assert payload["image_urls"] == [
        "api/media/chair-1.webp",
        "api/media/chair-2.webp",
    ]
    # Sender gates on v_10 — sub-v_10 peers don't see partial data.
    assert (
        call.kwargs["min_proto_version"] == FederationCapability.MIN_FOR_BAZAAR_LISTING
    )


async def test_listing_created_enqueues_image_bytes(
    federation_service,
    media_sync,
    federation_repo,
):
    """The metadata broadcast is followed by per-peer outbox enqueues so
    the receiver actually has the photo bytes — not just URLs that
    resolve to nothing on their host."""
    bus = EventBus()
    bazaar_repo = MagicMock()
    listing = _make_listing()
    bazaar_repo.get_listing = AsyncMock(return_value=listing)
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    await bus.publish(
        BazaarListingCreated(
            listing_post_id="bzr-1",
            space_id="sp-1",
            seller_user_id="u-seller",
            mode="fixed",
            title="Vintage chair",
            occurred_at=datetime.fromisoformat(
                "2026-05-23T10:00:00+00:00",
            ),
        ),
    )

    media_sync.enqueue_for_blob.assert_awaited_once()
    kwargs = media_sync.enqueue_for_blob.await_args.kwargs
    assert kwargs["space_id"] == "sp-1"
    # correlation_id is listing.post_id — same as the wrapper post's id,
    # so the outbox PK dedups correctly against any later catch-up.
    assert kwargs["correlation_id"] == "bzr-1"
    # Own instance filtered out, remote peers targeted.
    assert set(kwargs["target_instance_ids"]) == {"inst-peer-a", "inst-peer-b"}
    assert kwargs["media_urls"] == [
        "api/media/chair-1.webp",
        "api/media/chair-2.webp",
    ]


async def test_listing_created_no_images_skips_media_enqueue(
    federation_service,
    media_sync,
    federation_repo,
):
    """A listing without photos still broadcasts the metadata but
    skips the media outbox — no point enqueueing an empty URL list."""
    bus = EventBus()
    bazaar_repo = MagicMock()
    bazaar_repo.get_listing = AsyncMock(return_value=_make_listing(image_urls=()))
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    await bus.publish(
        BazaarListingCreated(
            listing_post_id="bzr-1",
            space_id="sp-1",
            seller_user_id="u-seller",
            mode="fixed",
            title="No-photo listing",
            occurred_at=datetime.fromisoformat(
                "2026-05-23T10:00:00+00:00",
            ),
        ),
    )
    federation_service.broadcast_to_space_members.assert_awaited_once()
    media_sync.enqueue_for_blob.assert_not_awaited()


async def test_listing_vanished_before_broadcast_is_logged(
    federation_service,
    media_sync,
    federation_repo,
    caplog,
):
    """If the listing got deleted between the bus event firing and our
    repo lookup, log + skip — don't broadcast a half-built row."""
    import logging

    bus = EventBus()
    bazaar_repo = MagicMock()
    bazaar_repo.get_listing = AsyncMock(return_value=None)
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    with caplog.at_level(logging.WARNING):
        await bus.publish(
            BazaarListingCreated(
                listing_post_id="bzr-gone",
                space_id="sp-1",
                seller_user_id="u-seller",
                mode="fixed",
                title="Gone",
                occurred_at=datetime.fromisoformat(
                    "2026-05-23T10:00:00+00:00",
                ),
            ),
        )
    assert any("vanished before broadcast" in r.getMessage() for r in caplog.records)
    federation_service.broadcast_to_space_members.assert_not_awaited()
    media_sync.enqueue_for_blob.assert_not_awaited()


# ─── F8: Status updates (SOLD / EXPIRED / CANCELLED) ─────────────────


async def test_listing_expired_broadcasts_status_update(
    federation_service,
    media_sync,
    federation_repo,
):
    """``BazaarListingExpired`` fires on auction-end (sold OR expired);
    the row's ``status`` column is the source of truth so the outbound
    re-reads it and broadcasts the actual terminal status."""
    from socialhome.domain.events import BazaarListingExpired

    bus = EventBus()
    bazaar_repo = MagicMock()
    sold_listing = _make_listing(
        status=BazaarStatus.SOLD,
        winner_user_id="u-bidder",
        winning_price=4200,
        sold_at="2026-05-23T18:00:00+00:00",
    )
    bazaar_repo.get_listing = AsyncMock(return_value=sold_listing)
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    await bus.publish(
        BazaarListingExpired(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            final_status="sold",
        ),
    )
    federation_service.broadcast_to_space_members.assert_awaited_once()
    call = federation_service.broadcast_to_space_members.await_args
    assert call.args[1] == FederationEventType.BAZAAR_LISTING_UPDATED
    payload = call.args[2]
    assert payload["post_id"] == "bzr-1"
    assert payload["status"] == "sold"
    assert payload["winner_user_id"] == "u-bidder"
    assert payload["winning_price"] == 4200
    # Gates on v_11 so sub-v_11 peers don't get the partial update.
    assert (
        call.kwargs["min_proto_version"] == FederationCapability.MIN_FOR_BAZAAR_STATUS
    )


async def test_listing_cancelled_broadcasts_status_update(
    federation_service,
    media_sync,
    federation_repo,
):
    """``BazaarListingCancelled`` fires when the seller pulls the listing
    pre-resolution. The outbound re-reads the row and broadcasts
    ``status='cancelled'``."""
    from socialhome.domain.events import BazaarListingCancelled

    bus = EventBus()
    bazaar_repo = MagicMock()
    cancelled = _make_listing(status=BazaarStatus.CANCELLED)
    bazaar_repo.get_listing = AsyncMock(return_value=cancelled)
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    await bus.publish(
        BazaarListingCancelled(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
        ),
    )
    federation_service.broadcast_to_space_members.assert_awaited_once()
    payload = federation_service.broadcast_to_space_members.await_args.args[2]
    assert payload["status"] == "cancelled"
    # No winner/price for a cancelled listing.
    assert payload["winner_user_id"] is None
    assert payload["winning_price"] is None


async def test_status_update_skips_when_listing_gone(
    federation_service,
    media_sync,
    federation_repo,
):
    """If the row vanished before the bus event arrived, log + skip."""
    from socialhome.domain.events import BazaarListingCancelled

    bus = EventBus()
    bazaar_repo = MagicMock()
    bazaar_repo.get_listing = AsyncMock(return_value=None)
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    await bus.publish(
        BazaarListingCancelled(listing_post_id="bzr-gone", seller_user_id="u"),
    )
    federation_service.broadcast_to_space_members.assert_not_awaited()


# ─── F7: Cross-household bids + offer acceptance ─────────────────────


async def test_bid_placed_broadcasts(
    federation_service,
    media_sync,
    federation_repo,
):
    from socialhome.domain.events import BazaarBidPlaced

    bus = EventBus()
    bazaar_repo = MagicMock()  # outbound doesn't read for bid events
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    await bus.publish(
        BazaarBidPlaced(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            bidder_user_id="u-bidder",
            amount=4200,
            new_end_time="2026-06-01T00:00:00+00:00",
            bid_id="bid-1",
            space_id="sp-1",
            message="my best offer",
        ),
    )
    federation_service.broadcast_to_space_members.assert_awaited_once()
    call = federation_service.broadcast_to_space_members.await_args
    assert call.args[0] == "sp-1"
    assert call.args[1] == FederationEventType.BAZAAR_BID_PLACED
    payload = call.args[2]
    assert payload["bid_id"] == "bid-1"
    assert payload["listing_post_id"] == "bzr-1"
    assert payload["bidder_user_id"] == "u-bidder"
    assert payload["amount"] == 4200
    assert payload["message"] == "my best offer"
    assert call.kwargs["min_proto_version"] == FederationCapability.MIN_FOR_BAZAAR_BIDS


async def test_offer_accepted_broadcasts(
    federation_service,
    media_sync,
    federation_repo,
):
    from socialhome.domain.events import BazaarOfferAccepted

    bus = EventBus()
    bazaar_repo = MagicMock()
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    bazaar_repo.get_listing = AsyncMock(
        return_value=_make_listing(
            status=BazaarStatus.SOLD, winner_user_id="u-buyer", winning_price=4500
        )
    )
    await bus.publish(
        BazaarOfferAccepted(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            buyer_user_id="u-buyer",
            price=4500,
            bid_id="bid-1",
            space_id="sp-1",
        ),
    )
    first, second = federation_service.broadcast_to_space_members.await_args_list
    assert first.args[1] == FederationEventType.BAZAAR_OFFER_ACCEPTED
    payload = first.args[2]
    assert payload["bid_id"] == "bid-1"
    assert payload["price"] == 4500
    # The sale reaches every member household too: the bid row's flag alone
    # left the listing "active" everywhere but on the seller's household.
    assert second.args[1] == FederationEventType.BAZAAR_LISTING_UPDATED
    assert second.args[2]["status"] == "sold"
    assert second.args[2]["winner_user_id"] == "u-buyer"


async def test_bid_placed_without_space_id_skips_broadcast(
    federation_service,
    media_sync,
    federation_repo,
):
    """A bid event without space_id (pre-v_12 publisher or household
    listing) → silent skip; nothing to broadcast to."""
    from socialhome.domain.events import BazaarBidPlaced

    bus = EventBus()
    bazaar_repo = MagicMock()
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    await bus.publish(
        BazaarBidPlaced(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            bidder_user_id="u-bidder",
            amount=100,
            new_end_time="2026-06-01T00:00:00+00:00",
            bid_id="bid-1",
            space_id=None,
        ),
    )
    federation_service.broadcast_to_space_members.assert_not_awaited()


async def test_a_settlement_is_announced_only_by_the_sellers_household(
    federation_service,
    media_sync,
    federation_repo,
):
    """Every household runs the expiry sweep over the listings it mirrors;
    only the seller's household broadcasts the result."""
    from socialhome.domain.events import BazaarListingExpired, BazaarOfferAccepted

    bus = EventBus()
    bazaar_repo = MagicMock()
    bazaar_repo.get_listing = AsyncMock(return_value=_make_listing())
    users = MagicMock()
    users.get_by_user_id = AsyncMock(return_value=None)  # seller is not ours
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=media_sync,
        federation_repo=federation_repo,
        user_repo=users,
    )
    await bus.publish(
        BazaarListingExpired(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            final_status="expired",
        ),
    )
    await bus.publish(
        BazaarOfferAccepted(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            buyer_user_id="u-b",
            price=5,
            space_id="sp-1",
            bid_id="b-1",
        ),
    )
    federation_service.broadcast_to_space_members.assert_not_awaited()
    users.get_by_user_id = AsyncMock(return_value=object())  # now it is
    await bus.publish(
        BazaarListingExpired(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            final_status="expired",
        ),
    )
    federation_service.broadcast_to_space_members.assert_awaited_once()


async def test_a_listing_released_with_its_post_carries_the_approval_block(
    federation_service,
    media_sync,
    federation_repo,
):
    """v_43: a listing that rode a reviewed post is published by the
    approval — it names the release so receivers accept it for the seller."""
    bus = EventBus()
    bazaar_repo = MagicMock()
    bazaar_repo.get_listing = AsyncMock(return_value=_make_listing())
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        media_sync=media_sync,
        federation_repo=federation_repo,
    )
    with release_scope("item-1", "u-mod"):
        await bus.publish(
            BazaarListingCreated(
                listing_post_id="bzr-1",
                space_id="sp-1",
                seller_user_id="u-seller",
                mode="fixed",
                title="Vintage chair",
                occurred_at=datetime.fromisoformat("2026-05-23T10:00:00+00:00"),
            ),
        )
    payload = federation_service.broadcast_to_space_members.await_args.args[2]
    assert payload["moderation"] == {"item_id": "item-1", "approved_by": "u-mod"}


# ─── Offers (v_73) — the seller's and the offerer's household only ──────


def _seats(instance_id: str | None = "inst-seller", *, tombstoned: bool = False):
    seats = MagicMock()
    row = (
        None
        if instance_id is None
        else SpaceRemoteMember(
            space_id="sp-1",
            instance_id=instance_id,
            user_id="u-x",
            tombstoned=tombstoned,
        )
    )
    seats.get_including_tombstones = AsyncMock(return_value=row)
    return seats


def _users(*ours: str):
    users = MagicMock()

    async def _get(user_id):
        return object() if user_id in ours else None

    users.get_by_user_id = AsyncMock(side_effect=_get)
    return users


def _outbound(federation_service, *, seats=None, users=None, listing=None):
    bus = EventBus()
    bazaar_repo = MagicMock()
    bazaar_repo.get_listing = AsyncMock(return_value=listing or _make_listing())
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=bazaar_repo,
        user_repo=users if users is not None else _users(),
        remote_member_repo=seats if seats is not None else _seats(),
    )
    return bus


def _offer_placed(**overrides) -> BazaarBidPlaced:
    return BazaarBidPlaced(
        **{
            "listing_post_id": "bzr-1",
            "seller_user_id": "u-seller",
            "bidder_user_id": "u-buyer",
            "amount": 9000,
            "new_end_time": "2026-06-01T00:00:00+00:00",
            "space_id": "sp-1",
            "message": "cash on Saturday?",
            "offer_id": "o-1",
            **overrides,
        }
    )


async def test_an_offer_goes_to_the_sellers_household_only(federation_service):
    seats = _seats("inst-seller")
    bus = _outbound(federation_service, seats=seats)
    await bus.publish(_offer_placed())
    federation_service.broadcast_to_space_members.assert_awaited_once()
    call = federation_service.broadcast_to_space_members.await_args
    assert call.args[0] == "sp-1"
    assert call.args[1] == FederationEventType.BAZAAR_OFFER_MADE
    assert call.args[2] == {
        "offer_id": "o-1",
        "listing_post_id": "bzr-1",
        "space_id": "sp-1",
        "offerer_user_id": "u-buyer",
        "amount": 9000,
        "message": "cash on Saturday?",
    }
    assert call.kwargs["only_instances"] == frozenset({"inst-seller"})
    assert (
        call.kwargs["min_proto_version"] == FederationCapability.MIN_FOR_BAZAAR_OFFERS
    )
    seats.get_including_tombstones.assert_awaited_once_with("sp-1", "", "u-seller")


@pytest.mark.parametrize(
    ("label", "seats", "users", "event"),
    [
        ("the seller is ours", "live", ("u-seller",), {}),
        ("the seller holds no seat", "none", (), {}),
        ("the seller's seat is gone", "tombstoned", (), {}),
        ("applied from a peer", "live", (), {"origin_instance_id": "inst-peer"}),
        ("no space", "live", (), {"space_id": None}),
    ],
)
async def test_an_offer_that_stays_home(federation_service, label, seats, users, event):
    seat_repo = {
        "live": _seats(),
        "none": _seats(None),
        "tombstoned": _seats(tombstoned=True),
    }[seats]
    bus = _outbound(federation_service, seats=seat_repo, users=_users(*users))
    await bus.publish(_offer_placed(**event))
    assert federation_service.broadcast_to_space_members.await_count == 0, label


async def test_without_a_roster_offers_stay_home(federation_service):
    bus = EventBus()
    BazaarOutbound(
        bus=bus,
        federation_service=federation_service,
        bazaar_repo=MagicMock(),
        user_repo=_users(),
    )
    await bus.publish(_offer_placed())
    federation_service.broadcast_to_space_members.assert_not_awaited()


async def test_a_failed_offer_send_is_logged_not_raised(federation_service, caplog):
    federation_service.broadcast_to_space_members = AsyncMock(
        side_effect=RuntimeError("down")
    )
    bus = _outbound(federation_service)
    with caplog.at_level("ERROR"):
        await bus.publish(_offer_placed())
    assert "bazaar_offer_made send failed" in caplog.text


async def test_an_accepted_offer_answers_the_buyer_and_announces_the_sale(
    federation_service,
):
    seats = _seats("inst-buyer")
    bus = _outbound(
        federation_service,
        seats=seats,
        users=_users("u-seller"),
        listing=_make_listing(
            status=BazaarStatus.SOLD, winner_user_id="u-buyer", winning_price=9000
        ),
    )
    await bus.publish(
        BazaarOfferAccepted(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            buyer_user_id="u-buyer",
            price=9000,
            space_id="sp-1",
            offer_id="o-1",
        )
    )
    answer, sale = federation_service.broadcast_to_space_members.await_args_list
    assert answer.args[1] == FederationEventType.BAZAAR_OFFER_UPDATED
    assert answer.args[2] == {
        "offer_id": "o-1",
        "listing_post_id": "bzr-1",
        "space_id": "sp-1",
        "status": "accepted",
    }
    assert answer.kwargs["only_instances"] == frozenset({"inst-buyer"})
    seats.get_including_tombstones.assert_awaited_once_with("sp-1", "", "u-buyer")
    assert sale.args[1] == FederationEventType.BAZAAR_LISTING_UPDATED
    assert "only_instances" not in sale.kwargs


async def test_a_mirrored_acceptance_is_never_echoed(federation_service):
    bus = _outbound(federation_service, users=_users("u-seller"))
    await bus.publish(
        BazaarOfferAccepted(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            buyer_user_id="u-buyer",
            price=9000,
            space_id="sp-1",
            offer_id="o-1",
            origin_instance_id="inst-peer",
        )
    )
    federation_service.broadcast_to_space_members.assert_not_awaited()


async def test_a_rejected_offer_answers_the_buyer_only(federation_service):
    seats = _seats("inst-buyer")
    bus = _outbound(federation_service, seats=seats, users=_users("u-seller"))
    await bus.publish(
        BazaarOfferRejected(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            bidder_user_id="u-buyer",
            bid_id="o-1",
            reason="too low, sorry",
            space_id="sp-1",
            offer_id="o-1",
        )
    )
    call = federation_service.broadcast_to_space_members.await_args
    assert call.args[1] == FederationEventType.BAZAAR_OFFER_UPDATED
    # The seller's reason rides the sealed answer to the offerer's
    # household — the only one it goes to.
    assert call.args[2] == {
        "offer_id": "o-1",
        "listing_post_id": "bzr-1",
        "space_id": "sp-1",
        "status": "rejected",
        "reason": "too low, sorry",
    }
    assert call.kwargs["only_instances"] == frozenset({"inst-buyer"})


async def test_a_rejected_offer_without_a_reason_sends_none(federation_service):
    bus = _outbound(
        federation_service, seats=_seats("inst-buyer"), users=_users("u-seller")
    )
    await bus.publish(
        BazaarOfferRejected(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            bidder_user_id="u-buyer",
            bid_id="o-1",
            space_id="sp-1",
            offer_id="o-1",
        )
    )
    assert (
        "reason" not in federation_service.broadcast_to_space_members.await_args.args[2]
    )


def _bid_rejected(**overrides) -> BazaarOfferRejected:
    return BazaarOfferRejected(
        **{
            "listing_post_id": "bzr-1",
            "seller_user_id": "u-seller",
            "bidder_user_id": "u-buyer",
            "bid_id": "b-1",
            "reason": "not to you",
            "space_id": "sp-1",
            **overrides,
        }
    )


async def test_a_declined_bid_reaches_every_member_the_reason_only_the_bidder(
    federation_service,
):
    bus = _outbound(
        federation_service, seats=_seats("inst-buyer"), users=_users("u-seller")
    )
    await bus.publish(_bid_rejected())
    call = federation_service.broadcast_to_space_members.await_args
    assert call.args[1] == FederationEventType.BAZAAR_OFFER_UPDATED
    assert call.args[2] == {
        "bid_id": "b-1",
        "listing_post_id": "bzr-1",
        "space_id": "sp-1",
        "status": "rejected",
    }
    assert "only_instances" not in call.kwargs
    assert (
        call.kwargs["min_proto_version"] == FederationCapability.MIN_FOR_BAZAAR_OFFERS
    )
    per_peer = call.kwargs["per_peer"]
    base = dict(call.args[2])
    assert await per_peer("inst-buyer", base) == {**base, "reason": "not to you"}
    other = await per_peer("inst-other", base)
    assert other == base and other is not base
    assert base == call.args[2]  # never mutated


@pytest.mark.parametrize(
    ("label", "overrides", "seats", "users"),
    [
        ("no reason", {"reason": None}, "inst-buyer", ("u-seller",)),
        ("bidder is ours", {}, "inst-buyer", ("u-seller", "u-buyer")),
        ("bidder holds no seat", {}, None, ("u-seller",)),
    ],
)
async def test_a_declined_bid_without_a_remote_bidder_carries_no_reason(
    federation_service, label, overrides, seats, users
):
    bus = _outbound(federation_service, seats=_seats(seats), users=_users(*users))
    await bus.publish(_bid_rejected(**overrides))
    call = federation_service.broadcast_to_space_members.await_args
    assert call is not None, label
    assert call.kwargs["per_peer"] is None, label
    assert "reason" not in call.args[2], label


async def test_only_the_sellers_household_declines_a_bid(federation_service):
    bus = _outbound(federation_service, seats=_seats("inst-buyer"), users=_users())
    await bus.publish(_bid_rejected())
    federation_service.broadcast_to_space_members.assert_not_awaited()


async def test_a_failed_bid_decline_send_is_logged_not_raised(federation_service):
    federation_service.broadcast_to_space_members.side_effect = RuntimeError("down")
    bus = _outbound(
        federation_service, seats=_seats("inst-buyer"), users=_users("u-seller")
    )
    await bus.publish(_bid_rejected())
    federation_service.broadcast_to_space_members.assert_awaited_once()


@pytest.mark.parametrize(
    "overrides",
    [
        {"offer_id": "", "bid_id": ""},  # neither an offer nor a bid
        {"space_id": None},
        {"origin_instance_id": "inst-peer"},
    ],
)
async def test_a_rejection_that_is_not_ours_to_send(federation_service, overrides):
    bus = _outbound(federation_service, users=_users("u-seller"))
    await bus.publish(
        BazaarOfferRejected(
            **{
                "listing_post_id": "bzr-1",
                "seller_user_id": "u-seller",
                "bidder_user_id": "u-buyer",
                "bid_id": "o-1",
                "space_id": "sp-1",
                "offer_id": "o-1",
                **overrides,
            }
        )
    )
    federation_service.broadcast_to_space_members.assert_not_awaited()


async def test_a_withdrawn_offer_tells_the_sellers_household(federation_service):
    seats = _seats("inst-seller")
    bus = _outbound(federation_service, seats=seats, users=_users("u-buyer"))
    await bus.publish(
        BazaarBidWithdrawn(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            bidder_user_id="u-buyer",
            bid_id="o-1",
            space_id="sp-1",
            offer_id="o-1",
        )
    )
    call = federation_service.broadcast_to_space_members.await_args
    assert call.args[1] == FederationEventType.BAZAAR_OFFER_UPDATED
    assert call.args[2]["status"] == "withdrawn"
    assert call.kwargs["only_instances"] == frozenset({"inst-seller"})
    seats.get_including_tombstones.assert_awaited_once_with("sp-1", "", "u-seller")


@pytest.mark.parametrize(
    "overrides",
    [
        {"offer_id": "", "bid_id": ""},  # neither an offer nor a bid
        {"space_id": None},
        {"origin_instance_id": "inst-peer"},
    ],
)
async def test_a_withdrawal_that_is_not_ours_to_send(federation_service, overrides):
    bus = _outbound(federation_service)
    await bus.publish(
        BazaarBidWithdrawn(
            **{
                "listing_post_id": "bzr-1",
                "seller_user_id": "u-seller",
                "bidder_user_id": "u-buyer",
                "bid_id": "o-1",
                "space_id": "sp-1",
                "offer_id": "o-1",
                **overrides,
            }
        )
    )
    federation_service.broadcast_to_space_members.assert_not_awaited()


async def test_a_mirrored_bid_is_never_echoed(federation_service):
    bus = _outbound(federation_service)
    await bus.publish(
        BazaarBidPlaced(
            listing_post_id="bzr-1",
            seller_user_id="u-seller",
            bidder_user_id="u-bidder",
            amount=4200,
            new_end_time="2026-06-01T00:00:00+00:00",
            bid_id="bid-1",
            space_id="sp-1",
            origin_instance_id="inst-peer",
        )
    )
    federation_service.broadcast_to_space_members.assert_not_awaited()


# ── A withdrawn auction bid ──────────────────────────────────────────────


def _bid_withdrawn(**overrides) -> BazaarBidWithdrawn:
    return BazaarBidWithdrawn(
        **{
            "listing_post_id": "bzr-1",
            "seller_user_id": "u-seller",
            "bidder_user_id": "u-buyer",
            "bid_id": "b-1",
            "space_id": "sp-1",
            **overrides,
        }
    )


async def test_a_withdrawn_bid_reaches_every_member_household(federation_service):
    seats = _seats("inst-seller")
    bus = _outbound(federation_service, seats=seats, users=_users("u-buyer"))
    await bus.publish(_bid_withdrawn())
    call = federation_service.broadcast_to_space_members.await_args
    assert call.args[0] == "sp-1"
    assert call.args[1] == FederationEventType.BAZAAR_OFFER_UPDATED
    assert call.args[2] == {
        "bid_id": "b-1",
        "listing_post_id": "bzr-1",
        "space_id": "sp-1",
        "status": "withdrawn",
    }
    # Every member household mirrors the bid: no narrowing to one.
    assert "only_instances" not in call.kwargs
    assert (
        call.kwargs["min_proto_version"] == FederationCapability.MIN_FOR_BAZAAR_OFFERS
    )
    seats.get_including_tombstones.assert_not_awaited()


async def test_only_the_bidders_household_sends_a_bid_withdrawal(federation_service):
    bus = _outbound(federation_service, users=_users("u-seller"))
    await bus.publish(_bid_withdrawn())
    federation_service.broadcast_to_space_members.assert_not_awaited()


async def test_a_failed_bid_withdrawal_send_is_logged_not_raised(federation_service):
    federation_service.broadcast_to_space_members.side_effect = RuntimeError("down")
    bus = _outbound(federation_service, users=_users("u-buyer"))
    await bus.publish(_bid_withdrawn())
    federation_service.broadcast_to_space_members.assert_awaited_once()
