"""Bazaar marketplace service (§9, §23.15).

Routes call the service; the service publishes domain events that
:class:`NotificationService` and :class:`RealtimeService` translate
into in-app notifications + WS broadcasts.

Lives next to :class:`SqliteBazaarRepo` rather than inside it because
the repo enforces row-level invariants (currency validation, anti-snipe
end-time bump) while the service enforces business rules (caller
permissions, event publishing, auction-close fan-out).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..domain.events import (
    BazaarBidPlaced,
    BazaarBidWithdrawn,
    BazaarListingCancelled,
    BazaarListingCreated,
    BazaarListingExpired,
    BazaarListingUpdated,
    BazaarOfferAccepted,
    BazaarOfferRejected,
)
from ..domain.post import (
    BAZAAR_CURRENCIES,
    BAZAAR_MAX_DURATION_DAYS,
    BAZAAR_MAX_IMAGES,
    BazaarBid,
    BazaarListing,
    BazaarMode,
    BazaarStatus,
)
from ..infrastructure.event_bus import EventBus
from ..domain.post import BazaarOffer
from ..repositories.bazaar_repo import (
    AbstractBazaarRepo,
    BidStateError,
    new_bid,
    new_offer,
)

if TYPE_CHECKING:
    from .feed_service import FeedService

log = logging.getLogger(__name__)


class BazaarServiceError(Exception):
    """Base class for bazaar-service errors."""


class ListingNotFoundError(BazaarServiceError):
    """Raised when a listing reference is unknown."""


class BidNotFoundError(BazaarServiceError):
    """Raised when a bid reference is unknown."""


class OfferNotFoundError(BazaarServiceError):
    """Raised when a fixed-price offer reference is unknown."""


class _Unset:
    """Sentinel for update-listing partial patches."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "_UNSET"


_UNSET = _Unset()


class BazaarService:
    """Service-level wrapper around :class:`AbstractBazaarRepo`."""

    __slots__ = ("_repo", "_bus", "_feed")

    def __init__(self, repo: AbstractBazaarRepo, bus: EventBus) -> None:
        self._repo = repo
        self._bus = bus
        self._feed: FeedService | None = None

    def attach_feed(self, feed_service: "FeedService") -> None:
        """Wire :class:`FeedService` so ``create_listing`` can mint the
        matching ``PostType.BAZAAR`` feed-post row in the same call."""
        self._feed = feed_service

    # ─── Listings ────────────────────────────────────────────────────────

    async def get_listing(self, post_id: str) -> BazaarListing:
        listing = await self._repo.get_listing(post_id)
        if listing is None:
            raise ListingNotFoundError(post_id)
        return listing

    async def list_active(self) -> list[BazaarListing]:
        return await self._repo.list_active()

    async def list_by_seller(
        self,
        seller_user_id: str,
    ) -> list[BazaarListing]:
        return await self._repo.list_by_seller(seller_user_id)

    async def create_listing(
        self,
        *,
        seller_user_id: str,
        mode: str,
        title: str,
        currency: str,
        duration_days: int = BAZAAR_MAX_DURATION_DAYS,
        description: str | None = None,
        image_urls: tuple[str, ...] = (),
        price: int | None = None,
        start_price: int | None = None,
        step_price: int | None = None,
    ) -> BazaarListing:
        """Mint a listing + its parent feed post atomically.

        Raises :class:`ValueError` on any validation failure, which the
        route layer maps to HTTP 422.
        """
        if self._feed is None:
            raise RuntimeError("feed service not attached")
        mode_val = _coerce_mode(mode)
        title_clean = title.strip()
        if not title_clean:
            raise ValueError("title is required")
        if len(title_clean) > 200:
            raise ValueError("title too long")
        if currency not in BAZAAR_CURRENCIES:
            raise ValueError(f"unsupported currency {currency!r}")
        duration_days = max(1, min(int(duration_days), BAZAAR_MAX_DURATION_DAYS))
        if len(image_urls) > BAZAAR_MAX_IMAGES:
            raise ValueError(
                f"too many images (max {BAZAAR_MAX_IMAGES})",
            )
        _validate_price_fields(mode_val, price, start_price, step_price)

        now = datetime.now(timezone.utc)
        end_time = (now + timedelta(days=duration_days)).isoformat()

        # Mint the parent feed post first so ``post_id`` is stable.
        caption = f"🛍 {title_clean}" + (
            f" — {description.strip()}" if description else ""
        )
        post = await self._feed.create_post(
            author_user_id=seller_user_id,
            type="bazaar",
            content=caption,
        )

        listing = BazaarListing(
            post_id=post.id,
            seller_user_id=seller_user_id,
            mode=mode_val,
            title=title_clean,
            end_time=end_time,
            currency=currency,
            status=BazaarStatus.ACTIVE,
            created_at=now.isoformat(),
            description=description.strip() if description else None,
            image_urls=tuple(image_urls),
            price=price,
            start_price=start_price,
            step_price=step_price,
        )
        await self._repo.save_listing(listing)
        await self._bus.publish(
            BazaarListingCreated(
                listing_post_id=listing.post_id,
                seller_user_id=seller_user_id,
                mode=mode_val.value,
                title=listing.title,
            )
        )
        return listing

    async def update_listing(
        self,
        *,
        post_id: str,
        actor_user_id: str,
        description: str | None | _Unset = _UNSET,
        title: str | None | _Unset = _UNSET,
    ) -> BazaarListing:
        """Patch mutable fields on an active listing (seller only)."""
        listing = await self.get_listing(post_id)
        if listing.seller_user_id != actor_user_id:
            raise PermissionError("only the seller may edit this listing")
        if listing.status is not BazaarStatus.ACTIVE:
            raise BazaarServiceError("listing is not active")

        next_title = listing.title
        next_description = listing.description
        if not isinstance(title, _Unset):
            cleaned = (title or "").strip()
            if not cleaned:
                raise ValueError("title cannot be empty")
            if len(cleaned) > 200:
                raise ValueError("title too long")
            next_title = cleaned
        if not isinstance(description, _Unset):
            next_description = description.strip() if description else None

        updated = BazaarListing(
            post_id=listing.post_id,
            seller_user_id=listing.seller_user_id,
            mode=listing.mode,
            title=next_title,
            end_time=listing.end_time,
            currency=listing.currency,
            status=listing.status,
            created_at=listing.created_at,
            description=next_description,
            image_urls=listing.image_urls,
            price=listing.price,
            start_price=listing.start_price,
            step_price=listing.step_price,
            winner_user_id=listing.winner_user_id,
            winning_price=listing.winning_price,
            sold_at=listing.sold_at,
        )
        await self._repo.save_listing(updated)
        await self._bus.publish(
            BazaarListingUpdated(
                listing_post_id=updated.post_id,
                seller_user_id=updated.seller_user_id,
            )
        )
        return updated

    async def cancel_listing(
        self,
        *,
        post_id: str,
        actor_user_id: str,
    ) -> None:
        """Seller pulls a listing. Only allowed while ACTIVE."""
        listing = await self.get_listing(post_id)
        if listing.seller_user_id != actor_user_id:
            raise PermissionError(
                "only the seller may cancel this listing",
            )
        if listing.status is not BazaarStatus.ACTIVE:
            raise BazaarServiceError("listing is not active")
        await self._repo.mark_cancelled(post_id)
        await self._bus.publish(
            BazaarListingCancelled(
                listing_post_id=post_id,
                seller_user_id=listing.seller_user_id,
            )
        )

    # ─── Bids ────────────────────────────────────────────────────────────

    async def place_bid(
        self,
        *,
        listing_post_id: str,
        bidder_user_id: str,
        amount: int,
        message: str | None = None,
    ) -> BazaarBid:
        """Place a bid on the listing.

        Business rules enforced here:
          * seller cannot bid on their own listing
          * amount must be positive
          * AUCTION / BID_FROM must beat the current high bid + step
          * listing must be ACTIVE
        """
        listing = await self.get_listing(listing_post_id)
        if listing.status is not BazaarStatus.ACTIVE:
            raise ValueError("listing is not active")
        if listing.seller_user_id == bidder_user_id:
            raise ValueError("seller cannot bid on own listing")
        if int(amount) <= 0:
            raise ValueError("amount must be positive")
        if listing.mode in (BazaarMode.AUCTION, BazaarMode.BID_FROM):
            floor = listing.start_price or 0
            step = listing.step_price or 0
            highest = await self._repo.highest_bid(listing_post_id)
            if highest is not None:
                floor = max(floor, int(highest.amount) + step)
            if int(amount) < floor:
                raise ValueError(
                    f"amount must be at least {floor}",
                )

        bid = new_bid(
            listing_post_id=listing_post_id,
            bidder_user_id=bidder_user_id,
            amount=int(amount),
            message=message,
        )
        bid = await self._repo.place_bid(bid)
        # Reload to surface any anti-snipe extension to subscribers.
        refreshed = await self._repo.get_listing(listing_post_id)
        await self._bus.publish(
            BazaarBidPlaced(
                listing_post_id=listing_post_id,
                seller_user_id=listing.seller_user_id,
                bidder_user_id=bidder_user_id,
                amount=int(amount),
                new_end_time=refreshed.end_time if refreshed else listing.end_time,
            )
        )
        return bid

    async def accept_offer(self, *, bid_id: str, actor_user_id: str) -> None:
        """Seller accepts an OFFER-mode bid → marks listing sold."""
        bid = await self._repo.get_bid(bid_id)
        if bid is None:
            raise BidNotFoundError(bid_id)
        listing = await self.get_listing(bid.listing_post_id)
        if listing.seller_user_id != actor_user_id:
            raise PermissionError("Only the seller may accept this offer")
        try:
            await self._repo.accept_offer(bid_id)
        except BidStateError as exc:
            raise BazaarServiceError(str(exc)) from exc
        await self._repo.mark_sold(
            listing.post_id,
            winner_user_id=bid.bidder_user_id,
            winning_price=bid.amount,
        )
        await self._bus.publish(
            BazaarOfferAccepted(
                listing_post_id=listing.post_id,
                seller_user_id=listing.seller_user_id,
                buyer_user_id=bid.bidder_user_id,
                price=bid.amount,
            )
        )

    async def reject_offer(
        self,
        *,
        bid_id: str,
        actor_user_id: str,
        reason: str | None = None,
    ) -> None:
        bid = await self._repo.get_bid(bid_id)
        if bid is None:
            raise BidNotFoundError(bid_id)
        listing = await self.get_listing(bid.listing_post_id)
        if listing.seller_user_id != actor_user_id:
            raise PermissionError("Only the seller may reject this offer")
        try:
            await self._repo.reject_offer(bid_id, reason=reason)
        except BidStateError as exc:
            raise BazaarServiceError(str(exc)) from exc
        await self._bus.publish(
            BazaarOfferRejected(
                listing_post_id=listing.post_id,
                seller_user_id=listing.seller_user_id,
                bidder_user_id=bid.bidder_user_id,
                bid_id=bid_id,
                reason=reason,
            )
        )

    async def withdraw_bid(
        self,
        *,
        bid_id: str,
        actor_user_id: str,
    ) -> None:
        """Bidder withdraws their own bid while the listing is still open."""
        bid = await self._repo.get_bid(bid_id)
        if bid is None:
            raise BidNotFoundError(bid_id)
        if bid.bidder_user_id != actor_user_id:
            raise PermissionError("only the bidder may withdraw this bid")
        listing = await self.get_listing(bid.listing_post_id)
        try:
            await self._repo.withdraw_bid(bid_id)
        except BidStateError as exc:
            raise BazaarServiceError(str(exc)) from exc
        await self._bus.publish(
            BazaarBidWithdrawn(
                listing_post_id=listing.post_id,
                seller_user_id=listing.seller_user_id,
                bidder_user_id=bid.bidder_user_id,
                bid_id=bid_id,
            )
        )

    async def list_bids(self, post_id: str) -> list[BazaarBid]:
        return await self._repo.list_bids(post_id)

    # ─── Fixed-price offers (§23.23) ────────────────────────────────────

    async def make_offer(
        self,
        *,
        listing_post_id: str,
        offerer_user_id: str,
        amount: int,
        message: str | None = None,
    ) -> BazaarOffer:
        """Place a new offer on a fixed-price / negotiable listing.

        Auction listings reject offer creation — auction uses
        :meth:`place_bid` instead. A seller can't offer on their own
        listing; already-sold / expired / cancelled listings reject.
        """
        listing = await self.get_listing(listing_post_id)
        if listing.status != BazaarStatus.ACTIVE:
            raise BazaarServiceError(
                f"listing {listing_post_id!r} is {listing.status.value}, "
                "no new offers accepted",
            )
        if listing.mode == BazaarMode.AUCTION:
            raise BazaarServiceError(
                "auction listings accept bids, not offers — use POST /bids",
            )
        if offerer_user_id == listing.seller_user_id:
            raise PermissionError("cannot offer on your own listing")
        if int(amount) <= 0:
            raise ValueError("amount must be positive")

        offer = new_offer(
            listing_post_id=listing_post_id,
            offerer_user_id=offerer_user_id,
            amount=int(amount),
            message=message,
        )
        await self._repo.create_offer(offer)
        # Reuse BazaarBidPlaced so the seller notification path handles
        # both bid + offer uniformly. ``new_end_time`` is the listing's
        # own end_time — fixed-price offers don't anti-snipe.
        await self._bus.publish(
            BazaarBidPlaced(
                listing_post_id=listing_post_id,
                seller_user_id=listing.seller_user_id,
                bidder_user_id=offerer_user_id,
                amount=int(amount),
                new_end_time=listing.end_time,
            )
        )
        return offer

    async def accept_fixed_offer(
        self,
        *,
        offer_id: str,
        actor_user_id: str,
    ) -> BazaarOffer:
        """Seller accepts one offer → listing flips to sold and every
        other pending offer is auto-rejected.

        Raises :class:`PermissionError` if the actor isn't the seller,
        :class:`OfferStateError` if the offer isn't pending.
        """
        offer = await self._repo.get_offer(offer_id)
        if offer is None:
            raise OfferNotFoundError(offer_id)
        listing = await self.get_listing(offer.listing_post_id)
        if actor_user_id != listing.seller_user_id:
            raise PermissionError("only the seller may accept offers")
        if listing.status != BazaarStatus.ACTIVE:
            raise BazaarServiceError(
                "listing is no longer active — cannot accept offers",
            )

        updated = await self._repo.update_offer_status(offer_id, "accepted")
        await self._repo.reject_other_pending_offers(
            offer.listing_post_id,
            except_offer_id=offer_id,
        )
        await self._repo.mark_sold(
            offer.listing_post_id,
            winner_user_id=offer.offerer_user_id,
            winning_price=offer.amount,
        )
        await self._bus.publish(
            BazaarOfferAccepted(
                listing_post_id=offer.listing_post_id,
                seller_user_id=listing.seller_user_id,
                buyer_user_id=offer.offerer_user_id,
                price=offer.amount,
            )
        )
        return updated

    async def reject_fixed_offer(
        self,
        *,
        offer_id: str,
        actor_user_id: str,
        reason: str | None = None,
    ) -> BazaarOffer:
        """Seller rejects a single pending offer. Listing stays active
        so other buyers can still make offers."""
        offer = await self._repo.get_offer(offer_id)
        if offer is None:
            raise OfferNotFoundError(offer_id)
        listing = await self.get_listing(offer.listing_post_id)
        if actor_user_id != listing.seller_user_id:
            raise PermissionError("only the seller may reject offers")
        updated = await self._repo.update_offer_status(offer_id, "rejected")
        await self._bus.publish(
            BazaarOfferRejected(
                listing_post_id=offer.listing_post_id,
                seller_user_id=listing.seller_user_id,
                bidder_user_id=offer.offerer_user_id,
                bid_id=offer_id,
                reason=reason,
            )
        )
        return updated

    async def withdraw_fixed_offer(
        self,
        *,
        offer_id: str,
        actor_user_id: str,
    ) -> BazaarOffer:
        """Offerer withdraws a pending offer (before the seller acts).

        Raises :class:`PermissionError` if the actor isn't the offerer,
        :class:`OfferStateError` if the offer isn't pending.
        """
        offer = await self._repo.get_offer(offer_id)
        if offer is None:
            raise OfferNotFoundError(offer_id)
        if actor_user_id != offer.offerer_user_id:
            raise PermissionError("only the offerer may withdraw")
        return await self._repo.update_offer_status(offer_id, "withdrawn")

    async def list_offers(
        self,
        listing_post_id: str,
        *,
        actor_user_id: str,
    ) -> list[BazaarOffer]:
        """Return offers on a listing.

        The seller sees every offer; other callers see only their own.
        Sellers routing-need the full list to pick which to accept;
        other participants get their row back so they can poll the
        status of their own offer.
        """
        listing = await self.get_listing(listing_post_id)
        if actor_user_id == listing.seller_user_id:
            return await self._repo.list_offers_for_listing(listing_post_id)
        # Filter by offerer — the caller's own offer (or none).
        all_offers = await self._repo.list_offers_for_offerer(actor_user_id)
        return [o for o in all_offers if o.listing_post_id == listing_post_id]

    # ─── Saved listings (§23.23 — buyer bookmarks) ──────────────────────

    async def save_listing(
        self,
        *,
        user_id: str,
        post_id: str,
    ) -> None:
        """Bookmark a listing for the caller. Idempotent."""
        await self.get_listing(post_id)  # existence check
        await self._repo.save_listing_bookmark(user_id=user_id, post_id=post_id)

    async def unsave_listing(
        self,
        *,
        user_id: str,
        post_id: str,
    ) -> None:
        await self._repo.unsave_listing_bookmark(
            user_id=user_id,
            post_id=post_id,
        )

    async def is_listing_saved(
        self,
        *,
        user_id: str,
        post_id: str,
    ) -> bool:
        return await self._repo.is_listing_saved(
            user_id=user_id,
            post_id=post_id,
        )

    async def list_saved_listings(
        self,
        user_id: str,
    ) -> list[dict]:
        return await self._repo.list_saved_listings(user_id)

    # ─── Expiry ──────────────────────────────────────────────────────────

    async def expire_due(self, *, now_iso: str | None = None) -> int:
        """Close every active auction whose ``end_time`` has passed.

        Sets status=expired (or sold, when there's a winning bid).
        Publishes :class:`BazaarListingExpired` per listing closed.
        Returns the count of listings transitioned.
        """
        listings = await self._repo.list_expired(now_iso=now_iso)
        n = 0
        for listing in listings:
            highest = await self._repo.highest_bid(listing.post_id)
            if listing.mode.value == "auction" and highest is not None:
                await self._repo.mark_sold(
                    listing.post_id,
                    winner_user_id=highest.bidder_user_id,
                    winning_price=highest.amount,
                )
                await self._bus.publish(
                    BazaarOfferAccepted(
                        listing_post_id=listing.post_id,
                        seller_user_id=listing.seller_user_id,
                        buyer_user_id=highest.bidder_user_id,
                        price=highest.amount,
                    )
                )
            else:
                await self._repo.mark_expired(listing.post_id)
            await self._bus.publish(
                BazaarListingExpired(
                    listing_post_id=listing.post_id,
                    seller_user_id=listing.seller_user_id,
                    final_status=(
                        BazaarStatus.SOLD.value
                        if (listing.mode.value == "auction" and highest is not None)
                        else BazaarStatus.EXPIRED.value
                    ),
                )
            )
            n += 1
        return n


def _coerce_mode(value: str | BazaarMode) -> BazaarMode:
    if isinstance(value, BazaarMode):
        return value
    try:
        return BazaarMode(value)
    except ValueError as exc:
        raise ValueError(f"invalid bazaar mode {value!r}") from exc


def _validate_price_fields(
    mode: BazaarMode,
    price: int | None,
    start_price: int | None,
    step_price: int | None,
) -> None:
    if mode in (BazaarMode.FIXED, BazaarMode.NEGOTIABLE):
        if price is None or price <= 0:
            raise ValueError(f"{mode.value} listing requires a positive price")
    if mode in (BazaarMode.AUCTION, BazaarMode.BID_FROM):
        if start_price is None or start_price <= 0:
            raise ValueError(
                f"{mode.value} listing requires start_price > 0",
            )
        if step_price is not None and step_price < 0:
            raise ValueError("step_price cannot be negative")


# ─── Scheduler ───────────────────────────────────────────────────────────


class BazaarExpiryScheduler:
    """Background loop that closes due auctions on a fixed cadence."""

    __slots__ = ("_svc", "_interval", "_task", "_stop")

    def __init__(
        self,
        service: BazaarService,
        *,
        interval_seconds: float = 60.0,
    ) -> None:
        self._svc = service
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                n = await self._svc.expire_due()
                if n:
                    log.info("bazaar: closed %d expired listings", n)
            except Exception as exc:  # pragma: no cover
                log.warning("bazaar expiry loop failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue


# uuid kept in the import list for future expansion (§23.15 sub-bids).
_ = uuid
