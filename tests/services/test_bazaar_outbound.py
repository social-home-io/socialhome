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

from socialhome.domain.events import BazaarListingCreated
from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.post import BazaarListing, BazaarMode, BazaarStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.bazaar_outbound import BazaarOutbound
from socialhome.services.moderation_release import release_scope


def _make_listing(**overrides) -> BazaarListing:
    defaults = dict(
        post_id="bzr-1",
        space_id="sp-1",
        seller_user_id="u-seller",
        mode=BazaarMode.FIXED,
        title="Vintage chair",
        end_time="2026-06-01T00:00:00+00:00",
        currency="USD",
        status=BazaarStatus.ACTIVE,
        created_at="2026-05-23T10:00:00+00:00",
        description="A nice chair",
        image_urls=("api/media/chair-1.webp", "api/media/chair-2.webp"),
        price=4500,
    )
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
    federation_service.broadcast_to_space_members.assert_awaited_once()
    call = federation_service.broadcast_to_space_members.await_args
    assert call.args[1] == FederationEventType.BAZAAR_OFFER_ACCEPTED
    payload = call.args[2]
    assert payload["bid_id"] == "bid-1"
    assert payload["price"] == 4500


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
