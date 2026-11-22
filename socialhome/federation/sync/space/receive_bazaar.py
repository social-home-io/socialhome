"""§25.6 sync of a space's bazaar listings (their FAQ and bids)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ....domain.bazaar_faq import (
    BAZAAR_FAQ_MAX_ENTRIES_EVER,
    BazaarFaqEntry,
    entry_from_wire,
)
from ....domain.federation import FederationEvent
from ....domain.file_hashes import parse_file_hashes
from ....domain.post import (
    BAZAAR_MAX_IMAGES,
    BazaarBid,
    BazaarListing,
    BazaarMode,
    BazaarStatus,
)
from ....domain.space_media_fields import item_media_urls
from ....services.inbound_media_store import space_media_refs
from .receiver_base import VERIFIED_ROW, ReceiverBase
from .stream_health import HeldBack

if TYPE_CHECKING:
    from ....federation.space_authorship import SpaceAuthorship
    from ....repositories.bazaar_repo import AbstractBazaarRepo

log = logging.getLogger(__name__)


class BazaarMixin(ReceiverBase):
    """The bazaar resource of :class:`~.receiver.SpaceSyncReceiver`."""

    __slots__ = ()

    async def _persist_bazaar(
        self, space_id: str, records: list[dict[str, Any]], *, provider: str
    ) -> None:
        # F4: catch-up bazaar listings so a new joiner sees the
        # full listing card (mode / price / photos / status) — not
        # just the wrapper post's caption. The wrapper post was
        # already persisted by the ``posts`` resource above, so
        # the BazaarListing FK to space_posts(id) is satisfied.
        if self._bazaar_repo is None:
            log.debug(
                "received %d bazaar records — no bazaar_repo wired, skipping",
                len(records),
            )
            return
        bazaar_from_host = await self._is_host(space_id, provider)
        for r in records:
            listing = _bazaar_listing_from_record(r, space_id)
            if listing is not None:
                # The provider ships the photos (catch-up): its names.
                held_listing = await self._bazaar_repo.get_listing(listing.post_id)
                names = self._media_names.localize(
                    space_id,
                    provider,
                    item_media_urls("bazaar", listing),
                    held=(
                        item_media_urls("bazaar", held_listing)
                        if held_listing is not None
                        else ()
                    ),
                    # v_85 — the host's hashes only.
                    hashes=parse_file_hashes(r) if bazaar_from_host else None,
                    verified=VERIFIED_ROW.get() == (space_id, provider),
                )
                listing = names.item("bazaar", listing)
                try:
                    if not await self._bazaar_repo.save_listing(
                        listing, space_id=space_id
                    ):
                        log.warning(
                            "space sync: bazaar listing %s is not a post of "
                            "space %s — refusing the write",
                            listing.post_id,
                            space_id,
                        )
                    else:
                        await _apply_bazaar_faq(
                            self._bazaar_repo, listing.post_id, r.get("faq")
                        )
                        raw_bids = r.get("bids")
                        if bazaar_from_host and isinstance(raw_bids, list):
                            # v_92 — the host's bids: a missed one is
                            # added, a held one only ever turns
                            # accepted / declined / withdrawn on; our
                            # own people's are never added or
                            # withdrawn from here.
                            await self._bazaar_repo.apply_synced_bids(
                                listing.post_id,
                                _bids_from_record(raw_bids, listing.post_id),
                                space_id=space_id,
                                keep_bidders=await self._local_people(space_id),
                            )
                except Exception as exc:
                    # FK / CHECK violation (anchor post missing,
                    # unknown mode). A listing the joiner cannot store
                    # is a listing the joiner never sees, so this is a
                    # WARNING, not DEBUG — until the posts exporter
                    # shipped anchor posts it fired for every
                    # unannounced listing, on every joiner, unseen.
                    log.warning(
                        "bazaar catch-up save_listing failed for post_id=%s: %s",
                        listing.post_id,
                        exc,
                        exc_info=True,
                    )

    async def _authored_bazaar(
        self,
        auth: SpaceAuthorship,
        space_id: str,
        rid: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: HeldBack,
    ) -> bool:
        post_id = str(r.get("post_id") or "")
        if self._bazaar_repo is None or not post_id:
            return False
        if await self._bazaar_repo.get_listing(post_id) is not None:
            return False
        got = await self._space_post_repo.get(post_id)
        seller = str(r.get("seller_user_id") or "")
        if got is None or got[1].author != seller:
            return False
        return await self._may_author(held_back, event, space_id, seller)


async def _apply_bazaar_faq(
    repo: AbstractBazaarRepo, listing_post_id: str, items: object
) -> None:
    """A ``bazaar`` record's questions and answers (``faq``, v_75), taken
    entry by entry under the live rule (newer ``rev`` wins, a removal is
    final, ≤ 10 live): removals first, so a full listing makes room before
    the entries that replaced them. A malformed entry is skipped; past
    :data:`_FAQ_RECORD_MAX_ITEMS` the record is refused (a listing never
    holds more entries than that, live and removed together)."""
    if items is None:
        return  # an older provider
    if not isinstance(items, list) or len(items) > _FAQ_RECORD_MAX_ITEMS:
        log.warning(
            "space sync: bazaar listing %s carries a malformed faq — skipped",
            listing_post_id,
        )
        return
    entries: list[BazaarFaqEntry] = []
    for item in items:
        try:
            entries.append(entry_from_wire(listing_post_id, item))
        except ValueError:
            log.warning(
                "space sync: bazaar listing %s: malformed faq entry skipped",
                listing_post_id,
            )
    entries.sort(key=lambda e: (not e.removed, e.rev))
    for entry in entries:
        await repo.apply_faq_entry(entry)


#: The most ``faq`` items one §25.6 ``bazaar`` record may carry: every
#: entry a listing ever holds, all its removal markers included (so a late
#: joiner never takes a stale edit of a removed entry).
_FAQ_RECORD_MAX_ITEMS = BAZAAR_FAQ_MAX_ENTRIES_EVER


#: The longest bid message a record may carry (the live bid's cap).
_MAX_BID_MESSAGE = 280


def _bids_from_record(raw: list[Any], post_id: str) -> list[BazaarBid]:
    """The host's ``bids`` of a listing — a malformed entry is skipped; a
    decline reason is never taken (it is the bidder's alone)."""
    out: list[BazaarBid] = []
    for b in raw:
        if not isinstance(b, dict):
            continue
        bid_id = str(b.get("id") or "")
        bidder = str(b.get("bidder_user_id") or "")
        amount = b.get("amount")
        if not bid_id or not bidder or type(amount) is not int or amount < 0:
            continue
        message = b.get("message")
        out.append(
            BazaarBid(
                id=bid_id,
                listing_post_id=post_id,
                bidder_user_id=bidder,
                amount=amount,
                # Never the host's clock: a missed bid is stamped with
                # its arrival here, as the live one (bazaar_repo).
                created_at="",
                message=(
                    message[:_MAX_BID_MESSAGE] if isinstance(message, str) else None
                ),
                accepted=b.get("accepted") is True,
                rejected=b.get("rejected") is True,
                withdrawn=b.get("withdrawn") is True,
            )
        )
    return out


def _bazaar_listing_from_record(
    r: dict[str, Any],
    space_id: str,
) -> BazaarListing | None:
    """Reconstruct a :class:`BazaarListing` from an exporter chunk record.

    Lenient: missing required fields or unknown mode/status → log + skip
    the row. The wrapper post must already be persisted (FK to
    space_posts.id); if it isn't, ``save_listing`` will raise IntegrityError
    and the caller catches it.
    """
    post_id = r.get("post_id")
    seller = r.get("seller_user_id")
    mode_raw = r.get("mode")
    title = r.get("title")
    if not post_id or not seller or not mode_raw or title is None:
        log.debug("bazaar record missing required field: %r", r)
        return None
    try:
        mode = BazaarMode(str(mode_raw))
        status = BazaarStatus(str(r.get("status") or "active"))
    except ValueError:
        log.debug(
            "bazaar record unknown mode/status: mode=%r status=%r",
            mode_raw,
            r.get("status"),
        )
        return None
    return BazaarListing(
        post_id=str(post_id),
        space_id=str(r.get("space_id") or space_id),
        seller_user_id=str(seller),
        mode=mode,
        title=str(title),
        end_time=str(r.get("end_time") or ""),
        currency=str(r.get("currency") or "USD"),
        status=status,
        created_at=str(r.get("created_at") or ""),
        description=r.get("description"),
        image_urls=space_media_refs(r.get("image_urls"), limit=BAZAAR_MAX_IMAGES),
        price=r.get("price"),
        start_price=r.get("start_price"),
        step_price=r.get("step_price"),
        winner_user_id=r.get("winner_user_id"),
        winning_price=r.get("winning_price"),
        sold_at=r.get("sold_at"),
    )
