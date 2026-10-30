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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..domain.child_protection import ProtectedCapability
from ..domain.errors import CodedError
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
    BAZAAR_OFFER_MESSAGE_MAX_CHARS,
    BAZAAR_REJECT_REASON_MAX_CHARS,
    BazaarBid,
    BazaarListing,
    BazaarMode,
    BazaarOffer,
    BazaarStatus,
    Post,
)
from ..domain.space import (
    WRITER_ROLES,
    ContentAction,
    SpaceMember,
    SpacePermissionError,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.bazaar_repo import (
    AbstractBazaarRepo,
    BidStateError,
    new_bid,
    new_offer,
)
from .inbound_media_store import require_local_media_refs
from .protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from .space_service import SpaceService

log = logging.getLogger(__name__)


class BazaarServiceError(Exception):
    """Base class for bazaar-service errors."""


class ListingNotFoundError(BazaarServiceError):
    """Raised when a listing reference is unknown."""


class BidNotFoundError(BazaarServiceError):
    """Raised when a bid reference is unknown."""


class OfferNotFoundError(BazaarServiceError):
    """Raised when a fixed-price offer reference is unknown."""


class BidTooLowError(CodedError):
    """A bid below the listing's floor (start price, or the high bid plus
    the step). ``params`` carry the floor in the listing's stored units
    (minor units — cents — except zero-decimal currencies) and the
    currency, so the SPA can format it as money."""

    status = 422
    code = "BID_TOO_LOW"

    def __init__(self, floor: int, currency: str) -> None:
        super().__init__(
            f"amount must be at least {floor}",
            params={"floor_amount": floor, "currency": currency},
        )


class OwnListingError(CodedError):
    """A seller bidding on, or making an offer for, their own listing."""

    code = "OWN_LISTING"
    detail = "seller cannot bid on or offer for own listing"


class ListingNotActiveError(CodedError):
    """A bid or offer on a listing that is sold, expired or cancelled."""

    code = "LISTING_NOT_ACTIVE"
    detail = "listing is not active"


class _Unset:
    """Sentinel for update-listing partial patches."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "_UNSET"


_UNSET = _Unset()


class BazaarService(ProtectionGateMixin):
    """Service-level wrapper around :class:`AbstractBazaarRepo`.

    Listings are scoped to a single :class:`Space` — :meth:`create_listing`
    mints the wrapper post via :class:`SpaceService.create_post` so the
    post lives in ``space_posts`` (not the household feed) and inherits
    the space's membership / federation rules.
    """

    __slots__ = ("_bus", "_child_protection", "_repo", "_spaces")

    def __init__(self, repo: AbstractBazaarRepo, bus: EventBus) -> None:
        self._repo = repo
        self._bus = bus
        self._spaces: SpaceService | None = None
        self._child_protection = None

    def attach_spaces(self, space_service: SpaceService) -> None:
        """Wire :class:`SpaceService` so ``create_listing`` can mint the
        matching ``PostType.BAZAAR`` post inside the listing's space."""
        self._spaces = space_service

    # ─── Listings ────────────────────────────────────────────────────────

    async def has_listing(self, post_id: str) -> bool:
        """Does a listing ride on the wrapper post ``post_id``?"""
        return await self._repo.get_listing(post_id) is not None

    async def get_listing(self, post_id: str) -> BazaarListing:
        listing = await self._repo.get_listing(post_id)
        if listing is None:
            raise ListingNotFoundError(post_id)
        return listing

    # ─── Visibility (who may see / trade on a listing) ──────────────────

    async def _seat(self, listing: BazaarListing, user_id: str) -> SpaceMember | None:
        """``user_id``'s seat in the listing's space (any role, a follower
        included), or ``None`` — no seat, a dissolved space. Fails closed
        without the space service."""
        if self._spaces is None:
            raise RuntimeError("space service not attached")
        try:
            return await self._spaces.require_reader(listing.space_id, user_id)
        except SpacePermissionError, KeyError:
            return None

    async def get_visible_listing(
        self, post_id: str, viewer_user_id: str
    ) -> BazaarListing:
        """The listing, for someone who may see it: a seat in its space
        (any role — a follower reads the Bazaar tab too) or its seller
        (``GET /api/bazaar?seller=me`` lists it wherever they are now).
        Anyone else gets :class:`ListingNotFoundError` — the same 404 an
        unknown id gets, so a listing id is no oracle."""
        listing = await self._repo.get_listing(post_id)
        if listing is None:
            raise ListingNotFoundError(post_id)
        if listing.seller_user_id == viewer_user_id:
            return listing
        if await self._seat(listing, viewer_user_id) is None:
            raise ListingNotFoundError(post_id)
        return listing

    async def viewer_can_trade(self, listing: BazaarListing, user_id: str) -> bool:
        """May ``user_id`` bid / make an offer here — a writer seat in the
        listing's space (a follower looks, saves, but doesn't trade). The
        SPA hides the buy controls on ``False``; the write paths enforce
        it regardless (:meth:`_tradeable_listing`). Never the seller, who
        can't buy their own listing."""
        if listing.seller_user_id == user_id:
            return False
        seat = await self._seat(listing, user_id)
        return seat is not None and seat.role in WRITER_ROLES

    async def tradeable_listing(
        self, post_id: str, buyer_user_id: str
    ) -> BazaarListing:
        """Public :meth:`_tradeable_listing` — for "Ask the seller", which
        is trading too (a writer seat; a follower gets 403)."""
        return await self._tradeable_listing(post_id, buyer_user_id)

    async def _tradeable_listing(
        self, post_id: str, buyer_user_id: str
    ) -> BazaarListing:
        """The listing a buyer bids / makes an offer on: visible to them
        (404 otherwise) and — trading being a write — not from a
        read-only follower seat (403 ``SUBSCRIBER_READ_ONLY``). The
        seller falls through so the own-listing rule answers them."""
        listing = await self._repo.get_listing(post_id)
        if listing is None:
            raise ListingNotFoundError(post_id)
        if listing.seller_user_id == buyer_user_id:
            return listing
        seat = await self._seat(listing, buyer_user_id)
        if seat is None:
            raise ListingNotFoundError(post_id)
        assert self._spaces is not None  # _seat fails closed without it
        self._spaces.assert_writable_member(seat, action="trade")
        return listing

    async def _listing_of_bid_or_offer(
        self,
        listing_post_id: str,
        actor_user_id: str,
        *,
        owner_user_id: str,
        not_found: Exception,
    ) -> BazaarListing:
        """The listing behind a bid / offer the actor acts on. Its owner
        (the bidder / offerer) always reaches it — withdrawing after
        leaving the space must still work; anyone else needs to see the
        listing, or gets ``not_found`` — never the seller-only 403 that
        would confirm the bid exists."""
        if actor_user_id == owner_user_id:
            return await self.get_listing(listing_post_id)
        try:
            return await self.get_visible_listing(listing_post_id, actor_user_id)
        except ListingNotFoundError:
            raise not_found from None

    async def list_active(self) -> list[BazaarListing]:
        return await self._repo.list_active()

    async def list_by_seller(
        self,
        seller_user_id: str,
    ) -> list[BazaarListing]:
        return await self._repo.list_by_seller(seller_user_id)

    async def _refuse_protected_buyer(self, buyer_user_id: str) -> None:
        """§CP.R: nothing is sold to a protected account (an offer or bid it
        made before protection). Worded for the seller — says nothing of why."""
        if await self._is_restricted(buyer_user_id, ProtectedCapability.BAZAAR):
            raise BazaarServiceError("This offer can no longer be accepted.")

    async def on_account_protected(self, user_id: str) -> None:
        """§CP.R retroactive: close the account's active listings.

        They end as ``cancelled`` — an existing, federated state every
        viewer already sees as closed — so nothing is bought from or sold
        to the account. Its pending offers and bids go with the listing.
        Not reopened when protection lifts: relisting is the seller's call.
        """
        for listing in await self._repo.list_by_seller(user_id):
            if listing.status is BazaarStatus.ACTIVE:
                await self.cancel_listing(
                    post_id=listing.post_id, actor_user_id=user_id
                )

    async def create_listing(
        self,
        *,
        space_id: str,
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
        announce_in_feed: bool = False,
    ) -> BazaarListing:
        """Mint a listing + its parent space post atomically.

        ``space_id`` is required — bazaar listings live inside spaces.
        :class:`SpaceService.create_post` enforces membership + the
        space's per-feature access decisions, and a :class:`SpacePermissionError`
        propagates out as HTTP 403 via the route layer's central mapper.

        The listing always lives in the space's Bazaar tab. ``announce_in_feed``
        controls only whether its wrapper post also surfaces in the feed
        stream — defaults False so the feed stays opt-in (§23.15). The
        wrapper post is always created (it anchors the listing id, comment
        thread, and media) but carries ``hidden_from_feed`` unless announced.

        Raises :class:`ValueError` on shape-of-input validation failure
        (mapped to HTTP 422).
        """
        # §CP.R: a protected account doesn't trade with other households.
        await self._require_unrestricted(seller_user_id, ProtectedCapability.BAZAAR)
        if self._spaces is None:
            raise RuntimeError("space service not attached")
        space = await self._spaces.get_space(space_id)
        if space is None:
            raise BazaarServiceError("space not found")
        if not space.features.bazaar:
            raise SpacePermissionError("the Bazaar is disabled in this space")
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
        image_urls = require_local_media_refs(image_urls)
        _validate_price_fields(mode_val, price, start_price, step_price)

        caption = f"🛍 {title_clean}" + (
            f" — {description.strip()}" if description else ""
        )
        fields = {
            "mode": mode_val.value,
            "title": title_clean,
            "currency": currency,
            "duration_days": duration_days,
            "description": description.strip() if description else None,
            "image_urls": list(image_urls),
            "price": price,
            "start_price": start_price,
            "step_price": step_price,
        }
        # Mint the wrapper post inside the listing's space, carrying the
        # listing as its attachment. SpaceService validates membership +
        # writability + per-feature access; we don't duplicate those
        # checks here. Under a MODERATED posts level the post — listing
        # and all — goes to the review queue (ContentQueuedForReview → 202)
        # and the listing is created only when the post is approved, so a
        # pending listing never exists without its post.
        post = await self._spaces.create_post(
            space_id,
            author_user_id=seller_user_id,
            type="bazaar",
            content=caption,
            hidden_from_feed=not announce_in_feed,
            attachments={"bazaar": fields},
        )
        return await self.get_listing(post.id)

    async def create_listing_for_post(
        self, *, space_id: str, post: Post, fields: dict
    ) -> BazaarListing:
        """The listing riding with ``post`` — created now unless it exists
        (see :meth:`create_listing_once`)."""
        await self.create_listing_once(space_id=space_id, post=post, fields=fields)
        held = await self._repo.get_listing(post.id)
        if held is None:
            raise BazaarServiceError("the listing's post is not in this space")
        return held

    async def create_listing_once(
        self, *, space_id: str, post: Post, fields: dict
    ) -> bool:
        """Persist the listing that rides with a just-persisted wrapper
        ``post`` (a direct create, or its approval from the moderation
        queue). The listing runs ``duration_days`` from now. Create-once:
        a repeat returns the listing already held, announcing nothing."""
        mode_val = _coerce_mode(str(fields.get("mode") or ""))
        currency = str(fields.get("currency") or "")
        if currency not in BAZAAR_CURRENCIES:
            raise ValueError(f"unsupported currency {currency!r}")
        price = fields.get("price")
        start_price = fields.get("start_price")
        step_price = fields.get("step_price")
        _validate_price_fields(mode_val, price, start_price, step_price)
        duration_days = max(
            1, min(int(fields.get("duration_days") or 7), BAZAAR_MAX_DURATION_DAYS)
        )
        now = datetime.now(UTC)
        listing = BazaarListing(
            post_id=post.id,
            space_id=space_id,
            seller_user_id=post.author,
            mode=mode_val,
            title=str(fields.get("title") or ""),
            end_time=(now + timedelta(days=duration_days)).isoformat(),
            currency=currency,
            status=BazaarStatus.ACTIVE,
            created_at=now.isoformat(),
            description=fields.get("description"),
            image_urls=tuple(str(u) for u in (fields.get("image_urls") or ())),
            price=price,
            start_price=start_price,
            step_price=step_price,
        )
        if not await self._repo.insert_listing_once(listing, space_id=space_id):
            # Already there (a racing / repeated approve) — never twice.
            return False
        await self._bus.publish(
            BazaarListingCreated(
                listing_post_id=listing.post_id,
                space_id=space_id,
                seller_user_id=post.author,
                mode=mode_val.value,
                title=listing.title,
            )
        )
        return True

    async def list_space_listings(self, space_id: str) -> list[BazaarListing]:
        """Every listing in a space (any status), newest-first — backs the
        space Bazaar tab. Membership + the ``bazaar`` feature gate are
        enforced by the route before this is called."""
        return await self._repo.list_in_space(space_id)

    async def editable_listing(self, post_id: str, actor_user_id: str) -> BazaarListing:
        """The listing its seller may change now — its title, description
        or questions and answers: visible to them (404 otherwise), theirs
        (403), active (409) and, being the wrapper post's content, an edit
        of their own post under the space's ``posts`` access level."""
        listing = await self.get_visible_listing(post_id, actor_user_id)
        if listing.seller_user_id != actor_user_id:
            raise PermissionError("only the seller may edit this listing")
        if listing.status is not BazaarStatus.ACTIVE:
            raise BazaarServiceError("listing is not active")
        if self._spaces is None:
            # Fail closed: without the space service there is no access gate.
            raise RuntimeError("space service not attached")
        await self._spaces.require_content_access(
            listing.space_id,
            actor_user_id,
            "posts",
            ContentAction.EDIT,
            owns_target=True,
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
        """Patch mutable fields on an active listing (seller only). Someone
        who can't see the listing gets the unknown id's 404."""
        listing = await self.editable_listing(post_id, actor_user_id)

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
            space_id=listing.space_id,
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
        await self._repo.save_listing(updated, space_id=listing.space_id)
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
        """Seller pulls a listing. Only allowed while ACTIVE. Someone who
        can't see the listing gets the unknown id's 404."""
        listing = await self.get_visible_listing(post_id, actor_user_id)
        if listing.seller_user_id != actor_user_id:
            raise PermissionError(
                "only the seller may cancel this listing",
            )
        if listing.status is not BazaarStatus.ACTIVE:
            raise BazaarServiceError("listing is not active")
        # The listing ends now, not at its scheduled end: its threads
        # close 14 days from here (``listing_ended_at``).
        await self._repo.mark_cancelled(
            post_id,
            space_id=listing.space_id,
            ended_at=datetime.now(UTC).isoformat(),
        )
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
          * the bidder isn't a protected account (§CP.R)
          * the bidder holds a writer seat in the listing's space (404
            without a seat, 403 for a read-only follower)
        """
        await self._require_unrestricted(bidder_user_id, ProtectedCapability.BAZAAR)
        listing = await self._tradeable_listing(listing_post_id, bidder_user_id)
        if listing.status is not BazaarStatus.ACTIVE:
            raise ListingNotActiveError(status=422)
        if listing.seller_user_id == bidder_user_id:
            raise OwnListingError(status=422)
        if int(amount) <= 0:
            raise ValueError("amount must be positive")
        if listing.mode in (BazaarMode.AUCTION, BazaarMode.BID_FROM):
            floor = listing.start_price or 0
            step = listing.step_price or 0
            highest = await self._repo.highest_bid(listing_post_id)
            if highest is not None:
                floor = max(floor, int(highest.amount) + step)
            if int(amount) < floor:
                raise BidTooLowError(floor, listing.currency)

        bid = new_bid(
            listing_post_id=listing_post_id,
            bidder_user_id=bidder_user_id,
            amount=int(amount),
            message=message,
        )
        bid = await self._repo.place_bid(bid, space_id=listing.space_id)
        # Reload to surface any anti-snipe extension to subscribers.
        refreshed = await self._repo.get_listing(listing_post_id)
        await self._bus.publish(
            BazaarBidPlaced(
                listing_post_id=listing_post_id,
                seller_user_id=listing.seller_user_id,
                bidder_user_id=bidder_user_id,
                amount=int(amount),
                new_end_time=refreshed.end_time if refreshed else listing.end_time,
                bid_id=bid.id,
                space_id=listing.space_id,
                message=message,
            )
        )
        return bid

    async def accept_offer(self, *, bid_id: str, actor_user_id: str) -> None:
        """Seller accepts an OFFER-mode bid → marks listing sold."""
        bid = await self._repo.get_bid(bid_id)
        if bid is None:
            raise BidNotFoundError(bid_id)
        listing = await self._listing_of_bid_or_offer(
            bid.listing_post_id,
            actor_user_id,
            owner_user_id=bid.bidder_user_id,
            not_found=BidNotFoundError(bid_id),
        )
        if listing.seller_user_id != actor_user_id:
            raise PermissionError("Only the seller may accept this offer")
        # §CP.R: nor sell — even from a listing made before protection —
        # nor is anything sold to a protected account.
        await self._require_unrestricted(actor_user_id, ProtectedCapability.BAZAAR)
        await self._refuse_protected_buyer(bid.bidder_user_id)
        try:
            await self._repo.accept_offer(bid_id, space_id=listing.space_id)
        except BidStateError as exc:
            raise BazaarServiceError(str(exc)) from exc
        await self._repo.mark_sold(
            listing.post_id,
            space_id=listing.space_id,
            winner_user_id=bid.bidder_user_id,
            winning_price=bid.amount,
        )
        await self._bus.publish(
            BazaarOfferAccepted(
                listing_post_id=listing.post_id,
                seller_user_id=listing.seller_user_id,
                buyer_user_id=bid.bidder_user_id,
                price=bid.amount,
                bid_id=bid_id,
                space_id=listing.space_id,
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
        listing = await self._listing_of_bid_or_offer(
            bid.listing_post_id,
            actor_user_id,
            owner_user_id=bid.bidder_user_id,
            not_found=BidNotFoundError(bid_id),
        )
        if listing.seller_user_id != actor_user_id:
            raise PermissionError("Only the seller may reject this offer")
        reason = _clean_reason(reason)
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
                # The bidder's household — and every member household's
                # copy of the bid — hears of it (``BAZAAR_OFFER_UPDATED``).
                space_id=listing.space_id,
            )
        )

    async def withdraw_bid(
        self,
        *,
        bid_id: str,
        actor_user_id: str,
    ) -> None:
        """Bidder withdraws their own bid while the listing is still open:
        ``active`` and before ``end_time``, and the bid neither accepted nor
        declined (409 ``BID_STATE_ERROR`` otherwise)."""
        bid = await self._repo.get_bid(bid_id)
        if bid is None:
            raise BidNotFoundError(bid_id)
        listing = await self._listing_of_bid_or_offer(
            bid.listing_post_id,
            actor_user_id,
            owner_user_id=bid.bidder_user_id,
            not_found=BidNotFoundError(bid_id),
        )
        if bid.bidder_user_id != actor_user_id:
            raise PermissionError("only the bidder may withdraw this bid")
        # Past ``end_time`` the high bid is the winner, even before the
        # expiry sweep closes the listing: no backing out of it then.
        if not listing.is_open(datetime.now(UTC)):
            raise BazaarServiceError(
                "the listing is closed — the bid can no longer be withdrawn"
            )
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
                # Every member household's copy of the bid hears of it
                # (``BAZAAR_OFFER_UPDATED`` keyed on ``bid_id``).
                space_id=listing.space_id,
            )
        )

    async def list_bids(self, post_id: str, *, viewer_user_id: str) -> list[BazaarBid]:
        """The bids on a listing the viewer may see (404 otherwise).

        Every member sees every bid and whether it was declined; the
        seller's reason only the seller and that bidder.
        """
        listing = await self.get_visible_listing(post_id, viewer_user_id)
        bids = await self._repo.list_bids(post_id)
        if viewer_user_id == listing.seller_user_id:
            return bids
        return [
            b
            if b.rejection_reason is None or b.bidder_user_id == viewer_user_id
            else replace(b, rejection_reason=None)
            for b in bids
        ]

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
        listing; already-sold / expired / cancelled listings reject. A
        protected account can't make offers (§CP.R).
        """
        await self._require_unrestricted(offerer_user_id, ProtectedCapability.BAZAAR)
        listing = await self._tradeable_listing(listing_post_id, offerer_user_id)
        if listing.status != BazaarStatus.ACTIVE:
            raise ListingNotActiveError(status=409)
        if listing.mode == BazaarMode.AUCTION:
            raise BazaarServiceError(
                "auction listings accept bids, not offers — use POST /bids",
            )
        if offerer_user_id == listing.seller_user_id:
            raise OwnListingError(status=403)
        if int(amount) <= 0:
            raise ValueError("amount must be positive")
        message = message or None
        if message is not None and not isinstance(message, str):
            raise ValueError("message must be text")
        if message is not None and len(message) > BAZAAR_OFFER_MESSAGE_MAX_CHARS:
            raise ValueError(
                f"message must be at most {BAZAAR_OFFER_MESSAGE_MAX_CHARS} characters"
            )

        offer = new_offer(
            listing_post_id=listing_post_id,
            offerer_user_id=offerer_user_id,
            amount=int(amount),
            message=message,
        )
        await self._repo.create_offer(offer)
        # Reuse BazaarBidPlaced so the seller notification path handles
        # both bid + offer uniformly. ``new_end_time`` is the listing's
        # own end_time — fixed-price offers don't anti-snipe. ``offer_id``
        # (not ``bid_id``) sends it to the seller's household only (v_73).
        await self._bus.publish(
            BazaarBidPlaced(
                listing_post_id=listing_post_id,
                seller_user_id=listing.seller_user_id,
                bidder_user_id=offerer_user_id,
                amount=int(amount),
                new_end_time=listing.end_time,
                space_id=listing.space_id,
                message=message,
                offer_id=offer.id,
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
        listing = await self._listing_of_bid_or_offer(
            offer.listing_post_id,
            actor_user_id,
            owner_user_id=offer.offerer_user_id,
            not_found=OfferNotFoundError(offer_id),
        )
        if actor_user_id != listing.seller_user_id:
            raise PermissionError("only the seller may accept offers")
        await self._require_unrestricted(actor_user_id, ProtectedCapability.BAZAAR)
        await self._refuse_protected_buyer(offer.offerer_user_id)
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
            space_id=listing.space_id,
            winner_user_id=offer.offerer_user_id,
            winning_price=offer.amount,
        )
        await self._bus.publish(
            BazaarOfferAccepted(
                listing_post_id=offer.listing_post_id,
                seller_user_id=listing.seller_user_id,
                buyer_user_id=offer.offerer_user_id,
                price=offer.amount,
                space_id=listing.space_id,
                offer_id=offer_id,
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
        listing = await self._listing_of_bid_or_offer(
            offer.listing_post_id,
            actor_user_id,
            owner_user_id=offer.offerer_user_id,
            not_found=OfferNotFoundError(offer_id),
        )
        if actor_user_id != listing.seller_user_id:
            raise PermissionError("only the seller may reject offers")
        reason = _clean_reason(reason)
        updated = await self._repo.update_offer_status(
            offer_id, "rejected", reason=reason
        )
        await self._bus.publish(
            BazaarOfferRejected(
                listing_post_id=offer.listing_post_id,
                seller_user_id=listing.seller_user_id,
                bidder_user_id=offer.offerer_user_id,
                bid_id=offer_id,
                reason=reason,
                space_id=listing.space_id,
                offer_id=offer_id,
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
        listing = await self._listing_of_bid_or_offer(
            offer.listing_post_id,
            actor_user_id,
            owner_user_id=offer.offerer_user_id,
            not_found=OfferNotFoundError(offer_id),
        )
        if actor_user_id != offer.offerer_user_id:
            raise PermissionError("only the offerer may withdraw")
        updated = await self._repo.update_offer_status(offer_id, "withdrawn")
        # The seller's household (another one, v_73) must not accept it.
        await self._bus.publish(
            BazaarBidWithdrawn(
                listing_post_id=offer.listing_post_id,
                seller_user_id=listing.seller_user_id,
                bidder_user_id=offer.offerer_user_id,
                bid_id=offer_id,
                space_id=listing.space_id,
                offer_id=offer_id,
            )
        )
        return updated

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
        status of their own offer. Someone who can't see the listing gets
        the unknown id's 404.
        """
        listing = await self.get_visible_listing(listing_post_id, actor_user_id)
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
        """Bookmark a listing the caller can see. Idempotent."""
        await self.get_visible_listing(post_id, user_id)
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
        """The caller's bookmarks of listings they can still see — one of
        a space they left is dropped (it would only hydrate into a 404)."""
        out: list[dict] = []
        for row in await self._repo.list_saved_listings(user_id):
            try:
                await self.get_visible_listing(str(row["post_id"]), user_id)
            except ListingNotFoundError:
                continue
            out.append(row)
        return out

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
            if highest is not None and await self._is_restricted(
                highest.bidder_user_id, ProtectedCapability.BAZAAR
            ):
                # §CP.R: a bid placed before protection never wins.
                highest = None
            if listing.mode.value == "auction" and highest is not None:
                await self._repo.mark_sold(
                    listing.post_id,
                    space_id=listing.space_id,
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
                await self._repo.mark_expired(
                    listing.post_id, space_id=listing.space_id
                )
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


def _clean_reason(reason: object) -> str | None:
    """The seller's reason for declining: trimmed text of at most
    :data:`BAZAAR_REJECT_REASON_MAX_CHARS`, or ``None`` for none.
    ``ValueError`` (422) for anything else."""
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be text")  # a 422, like every bad field
    if reason is None:
        return None
    reason = reason.strip()
    if len(reason) > BAZAAR_REJECT_REASON_MAX_CHARS:
        raise ValueError(
            f"reason must be at most {BAZAAR_REJECT_REASON_MAX_CHARS} characters"
        )
    return reason or None


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
    if mode in (BazaarMode.FIXED, BazaarMode.NEGOTIABLE) and (
        price is None or price <= 0
    ):
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

    __slots__ = ("_interval", "_stop", "_svc", "_task")

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
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                n = await self._svc.expire_due()
                if n:
                    log.info("bazaar: closed %d expired listings", n)
            except Exception as exc:  # pragma: no cover
                log.warning("bazaar expiry loop failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                continue


# uuid kept in the import list for future expansion (§23.15 sub-bids).
_ = uuid
