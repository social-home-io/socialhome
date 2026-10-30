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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..domain.events import (
    BazaarBidPlaced,
    BazaarBidWithdrawn,
    BazaarListingCancelled,
    BazaarListingCreated,
    BazaarListingExpired,
    BazaarOfferAccepted,
    BazaarOfferRejected,
)
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..infrastructure.event_bus import EventBus
from .moderation_release import with_release

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.bazaar_repo import AbstractBazaarRepo
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.user_repo import AbstractUserRepo
    from .space_media_sync_service import SpaceMediaSyncService

log = logging.getLogger(__name__)


class BazaarOutbound:
    """Bus-event → federation broadcaster for bazaar listings."""

    __slots__ = (
        "_bazaar_repo",
        "_bus",
        "_federation",
        "_federation_repo",
        "_media_sync",
        "_seats",
        "_users",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
        bazaar_repo: AbstractBazaarRepo,
        media_sync: SpaceMediaSyncService | None = None,
        federation_repo=None,
        user_repo: AbstractUserRepo | None = None,
        remote_member_repo: AbstractSpaceRemoteMemberRepo | None = None,
    ) -> None:
        self._bus = bus
        #: The space's roster mirror: which household seats a remote buyer
        #: or seller, so an offer (v_73) goes to that one household only.
        #: ``None``: offers stay on the household they were made on.
        self._seats = remote_member_repo
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
        # v_73 — an offer's answer and its withdrawal.
        self._bus.subscribe(BazaarOfferRejected, self._on_offer_rejected)
        self._bus.subscribe(BazaarBidWithdrawn, self._on_offer_withdrawn)

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
        # A listing riding a reviewed post is published by its approval,
        # which it names (v_43).
        with_release(payload)
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
        if not await self._is_ours(event.seller_user_id):
            return
        await self._fan_status_update(event.listing_post_id)

    async def _is_ours(self, user_id: str) -> bool:
        """``user_id`` is one of this household's people — only the
        seller's own household announces a settlement or an answer, only
        the bidder's a bid's withdrawal."""
        if self._users is None:
            return True
        return await self._users.get_by_user_id(user_id) is not None

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
            # When it ended (a withdrawal pulls end_time in): receivers
            # close the listing's Ask-the-seller threads from the same
            # moment. Additive — an older receiver ignores it.
            "end_time": listing.end_time,
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

        An OFFER (``offer_id`` set, v_73) is not a bid: it goes to the
        seller's household only (:meth:`_send_offer_made`).
        """
        if event.origin_instance_id or not event.space_id:
            return  # applied from a peer, or a pre-v_12 bus payload
        if event.offer_id:
            await self._send_offer_made(event)
            return
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
        if event.origin_instance_id or not event.space_id:
            return
        if not await self._is_ours(event.seller_user_id):
            return
        if event.offer_id:
            await self._send_offer_update(
                space_id=event.space_id,
                to_user_id=event.buyer_user_id,
                offer_id=event.offer_id,
                listing_post_id=event.listing_post_id,
                status="accepted",
            )
            # The sale itself is everybody's: every member household marks
            # the listing sold (and drops its own pending offers on it).
            await self._fan_status_update(event.listing_post_id)
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
        # The listing is sold: every member household marks it so (the
        # bid row's flag alone left it "active" everywhere but here).
        await self._fan_status_update(event.listing_post_id)

    # ─── Offers (v_73) — between the seller's and the offerer's household ──

    async def _send_offer_made(self, event: BazaarBidPlaced) -> None:
        """``BAZAAR_OFFER_MADE`` to the seller's household — one envelope,
        never the rest of the space: an offer is the seller's and the
        offerer's business only (``list_offers`` shows it to nobody else)."""
        assert event.space_id is not None
        await self._send_to_household_of(
            event.space_id,
            event.seller_user_id,
            FederationEventType.BAZAAR_OFFER_MADE,
            {
                "offer_id": event.offer_id,
                "listing_post_id": event.listing_post_id,
                "space_id": event.space_id,
                "offerer_user_id": event.bidder_user_id,
                "amount": event.amount,
                "message": event.message,
            },
        )

    async def _on_offer_rejected(self, event: BazaarOfferRejected) -> None:
        """The seller declined an offer — tell the offerer's household,
        the seller's reason (if any) with it. A declined auction BID
        (``bid_id``, no ``offer_id``) goes to every member household
        instead (:meth:`_send_bid_rejected`)."""
        if event.origin_instance_id or not event.space_id:
            return
        if not event.offer_id:
            if event.bid_id:
                await self._send_bid_rejected(event)
            return
        await self._send_offer_update(
            space_id=event.space_id,
            to_user_id=event.bidder_user_id,
            offer_id=event.offer_id,
            listing_post_id=event.listing_post_id,
            status="rejected",
            reason=event.reason,
        )

    async def _send_bid_rejected(self, event: BazaarOfferRejected) -> None:
        """A declined bid: ``BAZAAR_OFFER_UPDATED`` keyed on ``bid_id``.

        Every member household mirrors every bid (the high bid reads the
        same everywhere), so every one of them learns the bid is out —
        the same envelope count as the bid itself. The seller's REASON is
        for the bidder only: it rides the bidder's household's sealed copy
        alone (``per_peer``), never another member's. Sent from the
        seller's household only; a household below v_73 is sent nothing
        and one at v_73 that only knows ``offer_id`` drops it as before —
        no version bump needed.
        """
        assert event.space_id is not None
        if not await self._is_ours(event.seller_user_id):
            return
        payload: dict = {
            "bid_id": event.bid_id,
            "listing_post_id": event.listing_post_id,
            "space_id": event.space_id,
            "status": "rejected",
        }
        bidder_household = (
            await self._household_of(event.space_id, event.bidder_user_id)
            if event.reason
            else None
        )
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.BAZAAR_OFFER_UPDATED,
                payload,
                min_proto_version=FederationCapability.MIN_FOR_BAZAAR_OFFERS,
                per_peer=(
                    _reason_only_for(bidder_household, event.reason)
                    if bidder_household is not None and event.reason
                    else None
                ),
            )
        except Exception:
            log.exception(
                "BAZAAR_OFFER_UPDATED (bid) send failed for space=%s bid=%s",
                event.space_id,
                event.bid_id,
            )

    async def _on_offer_withdrawn(self, event: BazaarBidWithdrawn) -> None:
        """The offerer took an offer back — tell the seller's household,
        so it can no longer be accepted there. A withdrawn auction BID
        (``bid_id``, no ``offer_id``) goes to every member household
        instead (:meth:`_send_bid_withdrawn`)."""
        if event.origin_instance_id or not event.space_id:
            return
        if not event.offer_id:
            if event.bid_id:
                await self._send_bid_withdrawn(event)
            return
        await self._send_offer_update(
            space_id=event.space_id,
            to_user_id=event.seller_user_id,
            offer_id=event.offer_id,
            listing_post_id=event.listing_post_id,
            status="withdrawn",
        )

    async def _send_bid_withdrawn(self, event: BazaarBidWithdrawn) -> None:
        """A withdrawn bid: ``BAZAAR_OFFER_UPDATED`` keyed on ``bid_id``,
        ``status: withdrawn``.

        Every member household mirrors every bid, so every one of them —
        the seller's included — must drop it from the high bid (it would
        still set the floor there, and could still win): the same envelope
        count as the bid itself. Sent from the bidder's household only; a
        household below v_73 is sent nothing and one at v_73 that does not
        know the ``bid_id`` form drops it — the bid stays open there, as
        before. No version bump needed.
        """
        assert event.space_id is not None
        if not await self._is_ours(event.bidder_user_id):
            return
        try:
            await self._federation.broadcast_to_space_members(
                event.space_id,
                FederationEventType.BAZAAR_OFFER_UPDATED,
                {
                    "bid_id": event.bid_id,
                    "listing_post_id": event.listing_post_id,
                    "space_id": event.space_id,
                    "status": "withdrawn",
                },
                min_proto_version=FederationCapability.MIN_FOR_BAZAAR_OFFERS,
            )
        except Exception:
            log.exception(
                "BAZAAR_OFFER_UPDATED (bid withdrawn) send failed for space=%s bid=%s",
                event.space_id,
                event.bid_id,
            )

    async def _send_offer_update(
        self,
        *,
        space_id: str,
        to_user_id: str,
        offer_id: str,
        listing_post_id: str,
        status: str,
        reason: str | None = None,
    ) -> None:
        """An offer's new status, and for a rejection the seller's reason
        (sealed; an older receiver ignores the extra field)."""
        payload: dict = {
            "offer_id": offer_id,
            "listing_post_id": listing_post_id,
            "space_id": space_id,
            "status": status,
        }
        if reason:
            payload["reason"] = reason
        await self._send_to_household_of(
            space_id, to_user_id, FederationEventType.BAZAAR_OFFER_UPDATED, payload
        )

    async def _send_to_household_of(
        self,
        space_id: str,
        user_id: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        """Send to the ONE member household that seats ``user_id`` in the
        space — direct, or sealed end to end through the mesh / GFS relay
        like any space event (``broadcast_to_space_members`` narrowed with
        ``only_instances``, so a household that is no member is never a
        target). Nothing when ``user_id`` is ours (same household), has no
        live seat, or its household is below v_73: the offer then stays
        where it is, as it always did."""
        household = await self._household_of(space_id, user_id)
        if household is None:
            log.debug(
                "%s: %s is ours or holds no live seat in space %s — not sent",
                event_type.value,
                user_id,
                space_id,
            )
            return
        try:
            await self._federation.broadcast_to_space_members(
                space_id,
                event_type,
                payload,
                min_proto_version=FederationCapability.MIN_FOR_BAZAAR_OFFERS,
                only_instances=frozenset({household}),
            )
        except Exception:
            log.exception(
                "%s send failed for space=%s offer=%s",
                event_type.value,
                space_id,
                payload.get("offer_id"),
            )

    async def _household_of(self, space_id: str, user_id: str) -> str | None:
        """The other member household seating ``user_id`` in the space —
        ``None`` when the person is ours, holds no live seat, or there is
        no roster mirror to ask."""
        if self._seats is None:
            return None
        if self._users is not None and (
            await self._users.get_by_user_id(user_id) is not None
        ):
            return None
        seat = await self._seats.get_including_tombstones(space_id, "", user_id)
        if seat is None or seat.tombstoned:
            return None
        return seat.instance_id


def _reason_only_for(
    household: str, reason: str
) -> Callable[[str, dict], Awaitable[dict]]:
    """``per_peer`` for a declined bid: the seller's reason rides the
    bidder's household's sealed copy only, never another member's."""

    async def _copy(instance_id: str, base: dict) -> dict:
        if instance_id == household:
            return {**base, "reason": reason}
        return dict(base)

    return _copy
