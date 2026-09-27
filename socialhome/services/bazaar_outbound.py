"""Outbound federation bridge for bazaar listings.

Subscribes to :class:`BazaarListingCreated` and broadcasts the matching
:data:`FederationEventType.BAZAAR_LISTING_CREATED` to every household
that has a member in the listing's space.

The wrapper ``PostType.BAZAAR`` post federates via :class:`SpacePostOutbound`
already, carrying only the caption text. Without this service the remote
member sees ``🛍 Title`` and nothing else — no mode, no price, no photos,
no status. This module ships the actual :class:`BazaarListing` payload so
the remote SPA can render the listing card the same way the originating
household does.

Image bytes hand off to :class:`SpaceMediaSyncService` after the metadata
broadcast — the same outbox the post + gallery paths use. The
``correlation_id`` for a bazaar blob is ``listing.post_id`` so a future
re-enqueue (catch-up, retry) hits the same primary key.

Sender-side gating: the broadcast respects
:data:`FederationCapability.MIN_FOR_BAZAAR_LISTING`. Sub-v_10 peers
silently see only the wrapper post (today's behaviour) instead of partial
data with missing fields.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    BazaarBidPlaced,
    BazaarListingCancelled,
    BazaarListingCreated,
    BazaarListingExpired,
    BazaarOfferAccepted,
)
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.bazaar_repo import AbstractBazaarRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .space_media_sync_service import SpaceMediaSyncService

log = logging.getLogger(__name__)


class BazaarOutbound:
    """Bus-event → federation broadcaster for bazaar listings."""

    __slots__ = (
        "_bus",
        "_federation",
        "_bazaar_repo",
        "_media_sync",
        "_federation_repo",
        "_users",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: "FederationService",
        bazaar_repo: "AbstractBazaarRepo",
        media_sync: "SpaceMediaSyncService | None" = None,
        federation_repo=None,
        user_repo: "AbstractUserRepo | None" = None,
    ) -> None:
        self._bus = bus
        #: Settlement is the seller's household's to announce: every
        #: household runs the expiry sweep over every listing it mirrors, and
        #: receivers accept a listing's status change only from the seller's
        #: household. ``None`` keeps the historic always-announce behaviour.
        self._users = user_repo
        self._federation = federation_service
        self._bazaar_repo = bazaar_repo
        self._media_sync = media_sync
        self._federation_repo = federation_repo
        self._bus.subscribe(BazaarListingCreated, self._on_listing_created)
        self._bus.subscribe(BazaarListingExpired, self._on_listing_expired)
        self._bus.subscribe(BazaarListingCancelled, self._on_listing_cancelled)
        # F7 — cross-household bid + offer flow.
        self._bus.subscribe(BazaarBidPlaced, self._on_bid_placed)
        self._bus.subscribe(BazaarOfferAccepted, self._on_offer_accepted)

    async def _on_listing_created(self, event: BazaarListingCreated) -> None:
        """Fan ``BAZAAR_LISTING_CREATED`` to every member household.

        The bus event only carries IDs — fetch the full row from the
        repo so the payload mirrors the receiver-side expectations
        for a complete listing reconstruction.
        """
        listing = await self._bazaar_repo.get_listing(event.listing_post_id)
        if listing is None:
            log.warning(
                "BAZAAR_LISTING_CREATED: listing %s vanished before "
                "broadcast — skipping",
                event.listing_post_id,
            )
            return
        payload: dict = {
            "post_id": listing.post_id,
            "space_id": listing.space_id,
            "seller_user_id": listing.seller_user_id,
            "mode": listing.mode.value,
            "title": listing.title,
            "description": listing.description,
            "image_urls": list(listing.image_urls),
            "end_time": listing.end_time,
            "currency": listing.currency,
            "status": listing.status.value,
            "price": listing.price,
            "start_price": listing.start_price,
            "step_price": listing.step_price,
            "created_at": listing.created_at,
        }
        try:
            await self._federation.broadcast_to_space_members(
                listing.space_id,
                FederationEventType.BAZAAR_LISTING_CREATED,
                payload,
                min_proto_version=FederationCapability.MIN_FOR_BAZAAR_LISTING,
            )
        except Exception:
            log.exception(
                "BAZAAR_LISTING_CREATED broadcast failed for space=%s listing=%s",
                listing.space_id,
                listing.post_id,
            )
        # Hand off image bytes to the shared media outbox. Same pattern
        # as SpacePostOutbound: enqueue per-peer rows so the scheduler
        # can chunk + retry independently of the metadata broadcast.
        if (
            self._media_sync is not None
            and self._federation_repo is not None
            and listing.image_urls
        ):
            try:
                targets = await self._federation_repo.list_member_instance_ids(
                    listing.space_id,
                )
            except Exception:
                log.exception(
                    "BAZAAR media enqueue: list peers failed for space=%s listing=%s",
                    listing.space_id,
                    listing.post_id,
                )
                return
            own = getattr(self._federation, "_own_instance_id", "") or ""
            targets = [t for t in targets if t and t != own]
            if targets:
                try:
                    await self._media_sync.enqueue_for_blob(
                        space_id=listing.space_id,
                        correlation_id=listing.post_id,
                        target_instance_ids=targets,
                        media_urls=list(listing.image_urls),
                    )
                except Exception:
                    log.exception(
                        "BAZAAR media enqueue failed for space=%s listing=%s",
                        listing.space_id,
                        listing.post_id,
                    )

    async def _on_listing_expired(self, event: BazaarListingExpired) -> None:
        """Fan ``BAZAAR_LISTING_UPDATED`` for sold + expired terminal
        states (F8).

        ``BazaarListingExpired`` covers BOTH the auction-ended-with-winner
        case (``final_status='sold'``) and the auction-ended-no-winner
        case (``final_status='expired'``). Receivers update their local
        ``bazaar_listings`` row's ``status`` (+ ``winner_user_id`` /
        ``winning_price`` / ``sold_at`` if applicable) by post_id.
        """
        if not await self._seller_is_ours(event.seller_user_id):
            return
        await self._fan_status_update(event.listing_post_id)

    async def _seller_is_ours(self, seller_user_id: str) -> bool:
        """Only the seller's own household announces a settlement."""
        if self._users is None:
            return True
        return await self._users.get_by_user_id(seller_user_id) is not None

    async def _on_listing_cancelled(
        self,
        event: BazaarListingCancelled,
    ) -> None:
        """Fan ``BAZAAR_LISTING_UPDATED`` for ``status='cancelled'``
        — seller pulled the listing before any terminal resolution."""
        await self._fan_status_update(event.listing_post_id)

    async def _fan_status_update(self, post_id: str) -> None:
        """Lookup the post-mutation row and broadcast the new status.

        The row state is the source of truth — by the time the bus
        event arrives the seller's instance has already updated the
        ``status`` column (and ``winner_user_id`` etc. for sold).
        We re-read the row and broadcast just the status-bearing
        fields, not the full listing payload, so receivers' UPDATE
        WHERE post_id=? is a small write.
        """
        listing = await self._bazaar_repo.get_listing(post_id)
        if listing is None:
            log.warning(
                "BAZAAR_LISTING_UPDATED: listing %s vanished before "
                "broadcast — skipping",
                post_id,
            )
            return
        payload: dict = {
            "post_id": listing.post_id,
            "space_id": listing.space_id,
            "status": listing.status.value,
            "winner_user_id": listing.winner_user_id,
            "winning_price": listing.winning_price,
            "sold_at": listing.sold_at,
        }
        try:
            await self._federation.broadcast_to_space_members(
                listing.space_id,
                FederationEventType.BAZAAR_LISTING_UPDATED,
                payload,
                min_proto_version=FederationCapability.MIN_FOR_BAZAAR_STATUS,
            )
        except Exception:
            log.exception(
                "BAZAAR_LISTING_UPDATED broadcast failed for space=%s listing=%s",
                listing.space_id,
                listing.post_id,
            )

    async def _on_bid_placed(self, event: BazaarBidPlaced) -> None:
        """F7: ship a bid to the seller's host + every other space
        member so they all see the same canonical bid_id + amount.

        The bidder's local instance has ALREADY written to its own
        ``bazaar_bids`` via the regular ``place_bid`` path; this
        broadcast lets every other instance mirror that row.

        ``space_id`` is required for membership-gated broadcast — it
        ships on the bus event since v_12.
        """
        if not event.space_id:
            return  # household-only listing or pre-v_12 bus payload
        payload: dict = {
            "bid_id": event.bid_id,
            "listing_post_id": event.listing_post_id,
            "space_id": event.space_id,
            "seller_user_id": event.seller_user_id,
            "bidder_user_id": event.bidder_user_id,
            "amount": event.amount,
            "new_end_time": event.new_end_time,
            "message": event.message,
        }
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.BAZAAR_BID_PLACED,
                payload,
                min_proto_version=FederationCapability.MIN_FOR_BAZAAR_BIDS,
            )
        except Exception:
            log.exception(
                "BAZAAR_BID_PLACED broadcast failed for space=%s listing=%s bid=%s",
                event.space_id,
                event.listing_post_id,
                event.bid_id,
            )

    async def _on_offer_accepted(self, event: BazaarOfferAccepted) -> None:
        """F7: notify the bidder + every other member that the seller
        accepted this offer (or that the auction closed with a winner).

        Receivers flip the bid row's ``accepted`` flag and update the
        listing's status via ``mark_sold`` so winner_user_id /
        winning_price stay consistent. The matching F8
        ``BAZAAR_LISTING_UPDATED`` will also fire and route through
        ``mark_sold`` — duplicate writes are idempotent (gated on
        ``status='active'``).
        """
        if not event.space_id:
            return
        if not await self._seller_is_ours(event.seller_user_id):
            return
        payload: dict = {
            "bid_id": event.bid_id,
            "listing_post_id": event.listing_post_id,
            "space_id": event.space_id,
            "seller_user_id": event.seller_user_id,
            "buyer_user_id": event.buyer_user_id,
            "price": event.price,
        }
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.BAZAAR_OFFER_ACCEPTED,
                payload,
                min_proto_version=FederationCapability.MIN_FOR_BAZAAR_BIDS,
            )
        except Exception:
            log.exception(
                "BAZAAR_OFFER_ACCEPTED broadcast failed for space=%s listing=%s bid=%s",
                event.space_id,
                event.listing_post_id,
                event.bid_id,
            )
