"""Bazaar exporter — every listing for a space.

The wrapper ``PostType.BAZAAR`` post already streams under the
``posts`` resource; this exporter ships the matching
``BazaarListing`` rows so the receiver can render the full listing
card (mode / price / photos / status). Image bytes ride the
existing :class:`SpaceMediaSyncService` outbox — see
:meth:`SpaceSyncService._enqueue_catchup_media` for the enqueue.

Each record carries the listing's questions and answers (``faq``, v_75):
its live entries and its removal markers (a marker keeps a late edit of a
removed entry from bringing it back on the receiver). At most 10 live
entries and 20 markers per listing, so a record grows by ≤ ~8 KB.

v_92: each record also carries the listing's bids (``bids``: the newest
:data:`MAX_BIDS_PER_LISTING`, as every member household sees them live —
never the seller's decline reason, which is the bidder's alone). A v_92
receiver takes them from the space's host: a bid it lacks is added, a held
one only ever turns accepted / declined / withdrawn on. A bid changes no
stamped row (migration 0086), so a missed one heals on the host's daily
full stream. Offers and Ask-the-seller threads are between the seller's
and the buyer's households only and never stream. A requester below v_92
gets the record without ``bids`` (:meth:`BazaarExporter.without_bids`).

Without this exporter (#445 shipped realtime + image bytes only),
a new joiner saw the wrapper post but the ``bazaar_listings`` row
stayed empty until the seller fired a new ``BAZAAR_LISTING_CREATED``
event — broken cards for every pre-existing listing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from .....domain.bazaar_faq import entry_to_wire
from ..exporter import PagedExporterMixin
from ..window import SYNC_PAGE_SIZE, SyncWindows, iter_pages

if TYPE_CHECKING:
    from .....domain.bazaar_faq import BazaarFaqEntry
    from .....domain.post import BazaarBid, BazaarListing
    from .....repositories.bazaar_repo import AbstractBazaarRepo


class BazaarExporter(PagedExporterMixin):
    """Every listing whose wrapper post streams (live, inside the space's
    retention window), page by page."""

    resource = "bazaar"

    __slots__ = ("_bids", "_repo", "_windows")

    def __init__(
        self,
        bazaar_repo: AbstractBazaarRepo,
        windows: SyncWindows,
        *,
        bids: bool = True,
    ) -> None:
        self._repo = bazaar_repo
        self._windows = windows
        self._bids = bids

    def without_bids(self) -> BazaarExporter:
        """The record set of a requester below v_92: no ``bids``."""
        return BazaarExporter(self._repo, self._windows, bids=False)

    def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        return self._pages(space_id, None)

    def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """The listings whose wrapper post changed — a listing change
        touches the post (migration 0086)."""
        return self._pages(space_id, since)

    async def _pages(
        self, space_id: str, since: int | None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        window = await self._windows.for_space(space_id)

        async def fetch(cursor: int | None) -> tuple[list[BazaarListing], int | None]:
            return await self._repo.list_sync_page(
                space_id,
                cutoff=window.cutoff,
                exempt_types=window.exempt_types,
                cursor=cursor,
                limit=SYNC_PAGE_SIZE,
                since=since,
            )

        async for listings in iter_pages(fetch):
            ids = [lst.post_id for lst in listings]
            faq = await self._repo.list_faq_for_listings(ids)
            records = _records(listings, faq)
            if self._bids:
                bids = await self._repo.list_bids_for_listings(
                    ids, per_listing=MAX_BIDS_PER_LISTING
                )
                for record in records:
                    record["bids"] = [
                        _bid_to_wire(b) for b in bids.get(record["post_id"], ())
                    ]
            yield records


#: The most bids one ``bazaar`` record carries — the newest (a receiver
#: never reads a missing bid as a removal, so a cut list is safe).
MAX_BIDS_PER_LISTING: int = 200


def _bid_to_wire(bid: BazaarBid) -> dict[str, Any]:
    """A bid as every member household sees it live — never the seller's
    decline reason, which only the bidder's household gets."""
    return {
        "id": bid.id,
        "bidder_user_id": bid.bidder_user_id,
        "amount": bid.amount,
        "message": bid.message,
        "accepted": bid.accepted,
        "rejected": bid.rejected,
        "withdrawn": bid.withdrawn,
        "created_at": bid.created_at,
    }


def _records(
    listings: list[BazaarListing], faq: dict[str, list[BazaarFaqEntry]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for lst in listings:
        out.append(
            {
                "post_id": lst.post_id,
                "space_id": lst.space_id,
                "seller_user_id": lst.seller_user_id,
                "mode": lst.mode.value,
                "title": lst.title,
                "description": lst.description,
                "image_urls": list(lst.image_urls),
                "end_time": lst.end_time,
                "currency": lst.currency,
                "status": lst.status.value,
                "price": lst.price,
                "start_price": lst.start_price,
                "step_price": lst.step_price,
                "winner_user_id": lst.winner_user_id,
                "winning_price": lst.winning_price,
                "sold_at": lst.sold_at,
                "created_at": lst.created_at,
                "faq": [entry_to_wire(e) for e in faq.get(lst.post_id, ())],
            },
        )
    return out
