"""Bazaar (marketplace) routes — /api/bazaar/* (§5.2 / §9 / §23.15)."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    bazaar_repo_key,
    bazaar_service_key,
    media_signer_key,
    space_repo_key,
)
from ..media_signer import sign_media_urls_in, strip_signature_query
from ..repositories.bazaar_repo import BidStateError, OfferStateError
from ..security import error_response
from ..services.bazaar_service import (
    _UNSET,
    BazaarServiceError,
    BidNotFoundError,
    ListingNotFoundError,
    OfferNotFoundError,
)
from .base import BaseView


def _listing_dict(listing) -> dict:
    return {
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
        "winner_user_id": listing.winner_user_id,
        "winning_price": listing.winning_price,
        "sold_at": listing.sold_at,
        "created_at": listing.created_at,
    }


def _listing_dict_signed(request: web.Request, listing) -> dict:
    """:func:`_listing_dict` + sign ``image_urls`` for the SPA."""
    payload = _listing_dict(listing)
    signer = request.app.get(media_signer_key)
    if signer is not None:
        sign_media_urls_in(payload, signer)
    return payload


def _bid_dict(bid) -> dict:
    return {
        "id": bid.id,
        "listing_post_id": bid.listing_post_id,
        "bidder_user_id": bid.bidder_user_id,
        "amount": bid.amount,
        "message": bid.message,
        "accepted": bid.accepted,
        "rejected": bid.rejected,
        "rejection_reason": bid.rejection_reason,
        "withdrawn": bid.withdrawn,
        "created_at": bid.created_at,
    }


def _offer_dict(offer) -> dict:
    return {
        "id": offer.id,
        "listing_post_id": offer.listing_post_id,
        "offerer_user_id": offer.offerer_user_id,
        "amount": offer.amount,
        "message": offer.message,
        "status": offer.status,
        "created_at": offer.created_at,
        "responded_at": offer.responded_at,
    }


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid integer: {value!r}") from exc


class BazaarCollectionView(BaseView):
    """``GET /api/bazaar`` — list listings, optionally filtered.

    Query params:
      * ``seller=me`` — listings owned by the caller (any status)
      * ``status=active|sold|expired|cancelled`` — filter (default: active)

    ``POST /api/bazaar`` creates a new listing.
    """

    async def get(self) -> web.Response:
        """List active listings the caller can see.

        Listings live inside spaces, so visibility follows space
        membership: the caller sees their own listings (any status,
        across all of their spaces) when ``seller=me``, otherwise the
        active listings in spaces they belong to.
        """
        ctx = self.user
        repo = self.svc(bazaar_repo_key)
        q = self.request.query
        if q.get("seller") == "me":
            listings = await repo.list_by_seller(ctx.user_id)
        else:
            spaces = await self.svc(space_repo_key).list_for_user(ctx.user_id)
            space_ids = tuple(s.id for s in spaces)
            listings = await repo.list_active_in_spaces(space_ids)
        return web.json_response(
            [_listing_dict_signed(self.request, lst) for lst in listings],
        )

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        body = await self.body()

        space_id = str(body.get("space_id") or "").strip()
        if not space_id:
            return error_response(422, "UNPROCESSABLE", "space_id is required.")
        title = str(body.get("title") or "").strip()
        if not title:
            return error_response(422, "UNPROCESSABLE", "title is required.")
        mode = body.get("mode")
        if not mode:
            return error_response(422, "UNPROCESSABLE", "mode is required.")
        currency = str(body.get("currency") or "").upper().strip()
        if not currency:
            return error_response(
                422,
                "UNPROCESSABLE",
                "currency is required.",
            )

        image_urls = body.get("image_urls") or []
        if not isinstance(image_urls, list):
            return error_response(
                422,
                "UNPROCESSABLE",
                "image_urls must be a list.",
            )

        try:
            listing = await svc.create_listing(
                space_id=space_id,
                seller_user_id=ctx.user_id,
                mode=mode,
                title=title,
                currency=currency,
                description=body.get("description"),
                duration_days=int(body.get("duration_days") or 7),
                image_urls=tuple(strip_signature_query(str(u)) for u in image_urls),
                price=_int_or_none(body.get("price")),
                start_price=_int_or_none(body.get("start_price")),
                step_price=_int_or_none(body.get("step_price")),
            )
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(
            _listing_dict_signed(self.request, listing),
            status=201,
        )


class BazaarDetailView(BaseView):
    """``GET/PATCH/DELETE /api/bazaar/{id}``."""

    async def get(self) -> web.Response:
        self.user  # auth check
        listing_id = self.match("id")
        repo = self.svc(bazaar_repo_key)
        listing = await repo.get_listing(listing_id)
        if listing is None:
            return error_response(404, "NOT_FOUND", "Listing not found.")
        return web.json_response(_listing_dict_signed(self.request, listing))

    async def patch(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        body = await self.body()
        title = body.get("title", _UNSET)
        description = body.get("description", _UNSET)
        try:
            listing = await svc.update_listing(
                post_id=self.match("id"),
                actor_user_id=ctx.user_id,
                title=title,
                description=description,
            )
        except ListingNotFoundError:
            return error_response(404, "NOT_FOUND", "Listing not found.")
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        except BazaarServiceError as exc:
            return error_response(409, "CONFLICT", str(exc))
        return web.json_response(_listing_dict_signed(self.request, listing))

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        try:
            await svc.cancel_listing(
                post_id=self.match("id"),
                actor_user_id=ctx.user_id,
            )
        except ListingNotFoundError:
            return error_response(404, "NOT_FOUND", "Listing not found.")
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except BazaarServiceError as exc:
            return error_response(409, "CONFLICT", str(exc))
        return web.Response(status=204)


class BazaarBidCollectionView(BaseView):
    """``GET/POST /api/bazaar/{id}/bids``."""

    async def get(self) -> web.Response:
        self.user  # auth check
        svc = self.svc(bazaar_service_key)
        bids = await svc.list_bids(self.match("id"))
        return web.json_response([_bid_dict(b) for b in bids])

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        body = await self.body()

        amount = body.get("amount")
        if amount is None:
            return error_response(422, "UNPROCESSABLE", "amount is required.")
        try:
            amount_i = int(amount)
        except TypeError, ValueError:
            return error_response(
                422,
                "UNPROCESSABLE",
                "amount must be an integer.",
            )

        try:
            bid = await svc.place_bid(
                listing_post_id=self.match("id"),
                bidder_user_id=ctx.user_id,
                amount=amount_i,
                message=body.get("message"),
            )
        except ListingNotFoundError:
            return error_response(404, "NOT_FOUND", "Listing not found.")
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        return web.json_response(_bid_dict(bid), status=201)


class BazaarBidDetailView(BaseView):
    """``DELETE /api/bazaar/{id}/bids/{bid_id}`` — bidder withdraws."""

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        try:
            await svc.withdraw_bid(
                bid_id=self.match("bid_id"),
                actor_user_id=ctx.user_id,
            )
        except BidNotFoundError:
            return error_response(404, "NOT_FOUND", "Bid not found.")
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except BazaarServiceError as exc:
            return error_response(409, "BID_STATE_ERROR", str(exc))
        return web.Response(status=204)


class BazaarBidAcceptView(BaseView):
    """``POST /api/bazaar/{id}/bids/{bid_id}/accept``."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        try:
            await svc.accept_offer(
                bid_id=self.match("bid_id"),
                actor_user_id=ctx.user_id,
            )
        except BidNotFoundError:
            return error_response(404, "NOT_FOUND", "Bid not found.")
        except ListingNotFoundError:
            return error_response(404, "NOT_FOUND", "Listing not found.")
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except BazaarServiceError as exc:
            return error_response(409, "BID_STATE_ERROR", str(exc))
        except BidStateError as exc:
            return error_response(409, "BID_STATE_ERROR", str(exc))
        return web.json_response({"ok": True})


class BazaarBidRejectView(BaseView):
    """``POST /api/bazaar/{id}/bids/{bid_id}/reject``."""

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        body = await self.body()
        try:
            await svc.reject_offer(
                bid_id=self.match("bid_id"),
                actor_user_id=ctx.user_id,
                reason=(body.get("reason") or None),
            )
        except BidNotFoundError:
            return error_response(404, "NOT_FOUND", "Bid not found.")
        except ListingNotFoundError:
            return error_response(404, "NOT_FOUND", "Listing not found.")
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except BazaarServiceError as exc:
            return error_response(409, "BID_STATE_ERROR", str(exc))
        return web.json_response({"ok": True})


# ─── Fixed-price offers (§23.23) ────────────────────────────────────────


class BazaarOfferCollectionView(BaseView):
    """``GET/POST /api/bazaar/{id}/offers``.

    ``GET`` — seller sees every offer; other members see only their own.
    ``POST`` — create a new offer on a fixed/negotiable listing.
    Body: ``{amount, message?}``.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        try:
            offers = await svc.list_offers(
                self.match("id"),
                actor_user_id=ctx.user_id,
            )
        except ListingNotFoundError:
            return error_response(404, "NOT_FOUND", "Listing not found.")
        return web.json_response([_offer_dict(o) for o in offers])

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        body = await self.body()
        amount = body.get("amount")
        if amount is None:
            return error_response(422, "UNPROCESSABLE", "amount is required.")
        try:
            amount_i = int(amount)
        except TypeError, ValueError:
            return error_response(
                422,
                "UNPROCESSABLE",
                "amount must be an integer.",
            )
        try:
            offer = await svc.make_offer(
                listing_post_id=self.match("id"),
                offerer_user_id=ctx.user_id,
                amount=amount_i,
                message=body.get("message"),
            )
        except ListingNotFoundError:
            return error_response(404, "NOT_FOUND", "Listing not found.")
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except ValueError as exc:
            return error_response(422, "UNPROCESSABLE", str(exc))
        except BazaarServiceError as exc:
            return error_response(409, "CONFLICT", str(exc))
        return web.json_response(_offer_dict(offer), status=201)


class BazaarOfferDetailView(BaseView):
    """``DELETE /api/bazaar/{id}/offers/{offer_id}`` — offerer withdraws."""

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        try:
            updated = await svc.withdraw_fixed_offer(
                offer_id=self.match("offer_id"),
                actor_user_id=ctx.user_id,
            )
        except OfferNotFoundError:
            return error_response(404, "NOT_FOUND", "Offer not found.")
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except OfferStateError as exc:
            return error_response(409, "OFFER_STATE_ERROR", str(exc))
        return web.json_response(_offer_dict(updated))


class BazaarOfferAcceptView(BaseView):
    """``POST /api/bazaar/{id}/offers/{offer_id}/accept`` — seller accepts.

    Flips the listing to ``sold`` and auto-rejects every other pending
    offer on the same listing in one atomic flow.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        try:
            updated = await svc.accept_fixed_offer(
                offer_id=self.match("offer_id"),
                actor_user_id=ctx.user_id,
            )
        except OfferNotFoundError:
            return error_response(404, "NOT_FOUND", "Offer not found.")
        except ListingNotFoundError:
            return error_response(404, "NOT_FOUND", "Listing not found.")
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except OfferStateError as exc:
            return error_response(409, "OFFER_STATE_ERROR", str(exc))
        except BazaarServiceError as exc:
            return error_response(409, "CONFLICT", str(exc))
        return web.json_response(_offer_dict(updated))


class BazaarOfferRejectView(BaseView):
    """``POST /api/bazaar/{id}/offers/{offer_id}/reject`` — seller rejects.

    Body: ``{reason?}``. Listing stays active.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        body = await self.body()
        try:
            updated = await svc.reject_fixed_offer(
                offer_id=self.match("offer_id"),
                actor_user_id=ctx.user_id,
                reason=(body.get("reason") or None),
            )
        except OfferNotFoundError:
            return error_response(404, "NOT_FOUND", "Offer not found.")
        except ListingNotFoundError:
            return error_response(404, "NOT_FOUND", "Listing not found.")
        except PermissionError as exc:
            return error_response(403, "FORBIDDEN", str(exc))
        except OfferStateError as exc:
            return error_response(409, "OFFER_STATE_ERROR", str(exc))
        return web.json_response(_offer_dict(updated))


# ─── Saved listings (§23.23 — buyer bookmarks) ─────────────────────────


class BazaarSaveView(BaseView):
    """``POST /api/bazaar/{id}/save`` — bookmark.

    ``DELETE /api/bazaar/{id}/save`` — unbookmark.
    ``GET /api/bazaar/{id}/save`` — is-saved probe (returns ``{saved}``).
    """

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        saved = await svc.is_listing_saved(
            user_id=ctx.user_id,
            post_id=self.match("id"),
        )
        return web.json_response({"saved": bool(saved)})

    async def post(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        try:
            await svc.save_listing(
                user_id=ctx.user_id,
                post_id=self.match("id"),
            )
        except ListingNotFoundError:
            return error_response(404, "NOT_FOUND", "Listing not found.")
        return web.json_response({"saved": True}, status=201)

    async def delete(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        await svc.unsave_listing(
            user_id=ctx.user_id,
            post_id=self.match("id"),
        )
        return web.json_response({"saved": False})


class MySavedBazaarView(BaseView):
    """``GET /api/me/bazaar/saved`` — the caller's bookmarked listings.

    Returns ``{saved: [{post_id, saved_at}]}`` newest first. The client
    hydrates each listing via ``GET /api/bazaar/{post_id}``.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        svc = self.svc(bazaar_service_key)
        saved = await svc.list_saved_listings(ctx.user_id)
        return web.json_response({"saved": saved})
