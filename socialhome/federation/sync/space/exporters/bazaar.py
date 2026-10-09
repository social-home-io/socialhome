"""Bazaar exporter — every listing for a space.

The wrapper ``PostType.BAZAAR`` post already streams under the
``posts`` resource; this exporter ships the matching
``BazaarListing`` rows so the receiver can render the full listing
card (mode / price / photos / status). Image bytes ride the
existing :class:`SpaceMediaSyncService` outbox — see
:meth:`SpaceSyncService._enqueue_catchup_media` for the enqueue.

Without this exporter (#445 shipped realtime + image bytes only),
a new joiner saw the wrapper post but the ``bazaar_listings`` row
stayed empty until the seller fired a new ``BAZAAR_LISTING_CREATED``
event — broken cards for every pre-existing listing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, TYPE_CHECKING

from ..exporter import PagedExporterMixin
from ..window import SYNC_PAGE_SIZE, SyncWindows, iter_pages

if TYPE_CHECKING:
    from .....domain.post import BazaarListing
    from .....repositories.bazaar_repo import AbstractBazaarRepo


class BazaarExporter(PagedExporterMixin):
    """Every listing whose wrapper post streams (live, inside the space's
    retention window), page by page."""

    resource = "bazaar"

    __slots__ = ("_repo", "_windows")

    def __init__(self, bazaar_repo: "AbstractBazaarRepo", windows: SyncWindows) -> None:
        self._repo = bazaar_repo
        self._windows = windows

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

        async def fetch(cursor: int | None) -> tuple[list["BazaarListing"], int | None]:
            return await self._repo.list_sync_page(
                space_id,
                cutoff=window.cutoff,
                exempt_types=window.exempt_types,
                cursor=cursor,
                limit=SYNC_PAGE_SIZE,
                since=since,
            )

        async for listings in iter_pages(fetch):
            yield _records(listings)


def _records(listings: list["BazaarListing"]) -> list[dict[str, Any]]:
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
            },
        )
    return out
