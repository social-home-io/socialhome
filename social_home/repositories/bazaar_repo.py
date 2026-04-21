"""Bazaar repository — marketplace listings + bids/offers.

Tables:

* ``bazaar_listings`` — one row per listing. Mode + status drive the
  lifecycle (``active`` → ``sold`` / ``expired`` / ``cancelled``).
* ``bazaar_bids`` — bids for AUCTION / offers for OFFER mode. OFFER rows
  also carry accepted/rejected/withdrawn flags (state machine). AUCTION
  rows ignore those flags; the highest ``amount`` at ``end_time`` wins.

All prices are integer currency smallest-units (cents for EUR/USD, yen for
JPY) — never floats. The service layer computes the user-visible formatted
value from :data:`~social_home.domain.post.BAZAAR_CURRENCIES`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.post import (
    BAZAAR_CURRENCIES,
    BazaarBid,
    BazaarListing,
    BazaarMode,
    BazaarStatus,
)
from .base import bool_col, dump_json, load_json, row_to_dict, rows_to_dicts


#: §23.15 auction anti-snipe: bids arriving within this window of the
#: close-time push the close-time back by :data:`SNIPE_EXTEND_SECONDS`.
SNIPE_WINDOW_SECONDS: int = 5 * 60
SNIPE_EXTEND_SECONDS: int = 5 * 60


# ─── Bid state-machine errors ─────────────────────────────────────────────


class BidStateError(Exception):
    """Raised when a bid transition would violate the OFFER state machine."""


@runtime_checkable
class AbstractBazaarRepo(Protocol):
    # Listings ------------------------------------------------------------
    async def save_listing(self, listing: BazaarListing) -> BazaarListing: ...
    async def get_listing(self, post_id: str) -> BazaarListing | None: ...
    async def list_active(self) -> list[BazaarListing]: ...
    async def list_by_seller(self, seller_user_id: str) -> list[BazaarListing]: ...
    async def list_expired(
        self, *, now_iso: str | None = None
    ) -> list[BazaarListing]: ...
    async def mark_sold(
        self,
        post_id: str,
        *,
        winner_user_id: str,
        winning_price: int,
    ) -> None: ...
    async def mark_expired(self, post_id: str) -> None: ...
    async def mark_cancelled(self, post_id: str) -> None: ...

    # Bids / offers -------------------------------------------------------
    async def place_bid(self, bid: BazaarBid) -> BazaarBid: ...
    async def get_bid(self, bid_id: str) -> BazaarBid | None: ...
    async def list_bids(self, post_id: str) -> list[BazaarBid]: ...
    async def highest_bid(self, post_id: str) -> BazaarBid | None: ...
    async def accept_offer(self, bid_id: str) -> None: ...
    async def reject_offer(
        self,
        bid_id: str,
        *,
        reason: str | None = None,
    ) -> None: ...
    async def withdraw_bid(self, bid_id: str) -> None: ...


class SqliteBazaarRepo:
    """SQLite-backed :class:`AbstractBazaarRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Listings ───────────────────────────────────────────────────────

    async def save_listing(self, listing: BazaarListing) -> BazaarListing:
        if listing.currency not in BAZAAR_CURRENCIES:
            raise ValueError(f"unsupported currency {listing.currency!r}")
        await self._db.enqueue(
            """
            INSERT INTO bazaar_listings(
                post_id, seller_user_id, mode, title, description,
                image_urls_json, end_time, currency, status,
                price, start_price, step_price,
                winner_user_id, winning_price, sold_at, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')))
            ON CONFLICT(post_id) DO UPDATE SET
                seller_user_id=excluded.seller_user_id,
                mode=excluded.mode,
                title=excluded.title,
                description=excluded.description,
                image_urls_json=excluded.image_urls_json,
                end_time=excluded.end_time,
                currency=excluded.currency,
                status=excluded.status,
                price=excluded.price,
                start_price=excluded.start_price,
                step_price=excluded.step_price,
                winner_user_id=excluded.winner_user_id,
                winning_price=excluded.winning_price,
                sold_at=excluded.sold_at
            """,
            (
                listing.post_id,
                listing.seller_user_id,
                listing.mode.value,
                listing.title,
                listing.description,
                dump_json(list(listing.image_urls)),
                listing.end_time,
                listing.currency,
                listing.status.value,
                listing.price,
                listing.start_price,
                listing.step_price,
                listing.winner_user_id,
                listing.winning_price,
                listing.sold_at,
                listing.created_at,
            ),
        )
        return listing

    async def get_listing(self, post_id: str) -> BazaarListing | None:
        row = await self._db.fetchone(
            "SELECT * FROM bazaar_listings WHERE post_id=?",
            (post_id,),
        )
        return _row_to_listing(row_to_dict(row))

    async def list_active(self) -> list[BazaarListing]:
        rows = await self._db.fetchall(
            "SELECT * FROM bazaar_listings WHERE status='active' "
            "ORDER BY created_at DESC",
        )
        return [lst for lst in (_row_to_listing(d) for d in rows_to_dicts(rows)) if lst]

    async def list_by_seller(
        self,
        seller_user_id: str,
    ) -> list[BazaarListing]:
        rows = await self._db.fetchall(
            "SELECT * FROM bazaar_listings WHERE seller_user_id=? "
            "ORDER BY created_at DESC",
            (seller_user_id,),
        )
        return [lst for lst in (_row_to_listing(d) for d in rows_to_dicts(rows)) if lst]

    async def list_expired(
        self,
        *,
        now_iso: str | None = None,
    ) -> list[BazaarListing]:
        cutoff = now_iso or datetime.now(timezone.utc).isoformat()
        rows = await self._db.fetchall(
            """
            SELECT * FROM bazaar_listings
             WHERE status='active' AND end_time < ?
             ORDER BY end_time
            """,
            (cutoff,),
        )
        return [lst for lst in (_row_to_listing(d) for d in rows_to_dicts(rows)) if lst]

    async def mark_sold(
        self,
        post_id: str,
        *,
        winner_user_id: str,
        winning_price: int,
    ) -> None:
        """Atomic status transition ``active`` → ``sold`` with winner stamp."""

        def _run(conn):
            cur = conn.execute(
                """
                UPDATE bazaar_listings
                   SET status='sold',
                       winner_user_id=?, winning_price=?,
                       sold_at=datetime('now')
                 WHERE post_id=? AND status='active'
                """,
                (winner_user_id, int(winning_price), post_id),
            )
            if cur.rowcount == 0:
                raise ValueError(
                    f"listing {post_id!r} is not active (cannot mark sold)"
                )

        await self._db.transact(_run)

    async def mark_expired(self, post_id: str) -> None:
        await self._db.enqueue(
            "UPDATE bazaar_listings SET status='expired' "
            "WHERE post_id=? AND status='active'",
            (post_id,),
        )

    async def mark_cancelled(self, post_id: str) -> None:
        await self._db.enqueue(
            "UPDATE bazaar_listings SET status='cancelled' "
            "WHERE post_id=? AND status='active'",
            (post_id,),
        )

    # ── Bids / offers ──────────────────────────────────────────────────

    async def place_bid(self, bid: BazaarBid) -> BazaarBid:
        """Insert a new bid. The listing must be ``active``.

        For AUCTION / BID_FROM listings, callers are expected to check the
        minimum-increment rule at the service layer before calling this.
        This repo enforces that the target listing exists and is active.

        §23.15 anti-snipe: if the listing is an auction and the bid lands
        within :data:`SNIPE_WINDOW_SECONDS` of ``end_time``, extend
        ``end_time`` by :data:`SNIPE_EXTEND_SECONDS`. The service
        returns the updated deadline so the WS broadcast can carry it.
        """

        def _run(conn):
            row = conn.execute(
                "SELECT status, mode, end_time FROM bazaar_listings WHERE post_id=?",
                (bid.listing_post_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"listing {bid.listing_post_id!r} not found")
            if row[0] != BazaarStatus.ACTIVE.value:
                raise ValueError(f"listing {bid.listing_post_id!r} is not active")
            conn.execute(
                """
                INSERT INTO bazaar_bids(
                    id, listing_post_id, bidder_user_id, amount, message,
                    accepted, rejected, rejection_reason, withdrawn,
                    created_at
                ) VALUES(?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')))
                """,
                (
                    bid.id,
                    bid.listing_post_id,
                    bid.bidder_user_id,
                    int(bid.amount),
                    bid.message,
                    int(bid.accepted),
                    int(bid.rejected),
                    bid.rejection_reason,
                    int(bid.withdrawn),
                    bid.created_at,
                ),
            )
            # Anti-snipe for auction mode (§23.15).
            if row[1] == "auction":
                end_time = row[2]
                try:
                    end_dt = datetime.fromisoformat(
                        end_time.replace("Z", "+00:00"),
                    )
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                except ValueError, AttributeError:
                    return
                now = datetime.now(timezone.utc)
                if end_dt > now and (end_dt - now) <= timedelta(
                    seconds=SNIPE_WINDOW_SECONDS
                ):
                    new_end = (
                        now + timedelta(seconds=SNIPE_EXTEND_SECONDS)
                    ).isoformat()
                    conn.execute(
                        "UPDATE bazaar_listings SET end_time=? WHERE post_id=?",
                        (new_end, bid.listing_post_id),
                    )

        await self._db.transact(_run)
        return bid

    async def get_bid(self, bid_id: str) -> BazaarBid | None:
        row = await self._db.fetchone(
            "SELECT * FROM bazaar_bids WHERE id=?",
            (bid_id,),
        )
        return _row_to_bid(row_to_dict(row))

    async def list_bids(self, post_id: str) -> list[BazaarBid]:
        rows = await self._db.fetchall(
            "SELECT * FROM bazaar_bids WHERE listing_post_id=? ORDER BY created_at",
            (post_id,),
        )
        return [b for b in (_row_to_bid(d) for d in rows_to_dicts(rows)) if b]

    async def highest_bid(self, post_id: str) -> BazaarBid | None:
        """Return the single highest *non-withdrawn* bid, or ``None``.

        Useful for determining an auction winner at end-time.
        """
        row = await self._db.fetchone(
            """
            SELECT * FROM bazaar_bids
             WHERE listing_post_id=? AND withdrawn=0
             ORDER BY amount DESC, created_at ASC
             LIMIT 1
            """,
            (post_id,),
        )
        return _row_to_bid(row_to_dict(row))

    async def accept_offer(self, bid_id: str) -> None:
        """OFFER: accept this bid and reject every other pending offer.

        Enforces the OFFER state machine: only a pending offer (not yet
        accepted / rejected / withdrawn) can be accepted. Sibling pending
        offers transition to ``rejected`` with the standard "another offer
        was accepted" reason.
        """

        def _run(conn):
            # Check target is pending
            row = conn.execute(
                "SELECT listing_post_id, accepted, rejected, withdrawn "
                "FROM bazaar_bids WHERE id=?",
                (bid_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"bid {bid_id!r} not found")
            listing_post_id = row[0]
            if row[1] or row[2] or row[3]:
                raise BidStateError(f"bid {bid_id!r} is not pending — cannot accept")
            conn.execute(
                "UPDATE bazaar_bids SET accepted=1 WHERE id=?",
                (bid_id,),
            )
            conn.execute(
                """
                UPDATE bazaar_bids
                   SET rejected=1,
                       rejection_reason='another offer was accepted'
                 WHERE listing_post_id=?
                   AND id != ?
                   AND accepted=0 AND rejected=0 AND withdrawn=0
                """,
                (listing_post_id, bid_id),
            )

        await self._db.transact(_run)

    async def reject_offer(
        self,
        bid_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        def _run(conn):
            row = conn.execute(
                "SELECT accepted, rejected, withdrawn FROM bazaar_bids WHERE id=?",
                (bid_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"bid {bid_id!r} not found")
            if row[0] or row[2]:
                raise BidStateError(
                    f"bid {bid_id!r} already accepted or withdrawn — cannot reject"
                )
            conn.execute(
                "UPDATE bazaar_bids SET rejected=1, rejection_reason=? WHERE id=?",
                (reason, bid_id),
            )

        await self._db.transact(_run)

    async def withdraw_bid(self, bid_id: str) -> None:
        def _run(conn):
            row = conn.execute(
                "SELECT accepted, rejected, withdrawn FROM bazaar_bids WHERE id=?",
                (bid_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"bid {bid_id!r} not found")
            if row[0] or row[1] or row[2]:
                raise BidStateError(f"bid {bid_id!r} is not pending — cannot withdraw")
            conn.execute(
                "UPDATE bazaar_bids SET withdrawn=1 WHERE id=?",
                (bid_id,),
            )

        await self._db.transact(_run)


# ─── Row → domain ─────────────────────────────────────────────────────────


def _row_to_listing(row: dict | None) -> BazaarListing | None:
    if row is None:
        return None
    image_urls = tuple(load_json(row.get("image_urls_json"), []))
    return BazaarListing(
        post_id=row["post_id"],
        seller_user_id=row["seller_user_id"],
        mode=BazaarMode(row["mode"]),
        title=row["title"],
        end_time=row["end_time"],
        currency=row["currency"],
        status=BazaarStatus(row["status"]),
        created_at=row["created_at"],
        description=row.get("description"),
        image_urls=image_urls,
        price=row.get("price"),
        start_price=row.get("start_price"),
        step_price=row.get("step_price"),
        winner_user_id=row.get("winner_user_id"),
        winning_price=row.get("winning_price"),
        sold_at=row.get("sold_at"),
    )


def _row_to_bid(row: dict | None) -> BazaarBid | None:
    if row is None:
        return None
    return BazaarBid(
        id=row["id"],
        listing_post_id=row["listing_post_id"],
        bidder_user_id=row["bidder_user_id"],
        amount=int(row["amount"]),
        created_at=row["created_at"],
        message=row.get("message"),
        accepted=bool_col(row.get("accepted", 0)),
        rejected=bool_col(row.get("rejected", 0)),
        rejection_reason=row.get("rejection_reason"),
        withdrawn=bool_col(row.get("withdrawn", 0)),
    )


def new_bid(
    *,
    listing_post_id: str,
    bidder_user_id: str,
    amount: int,
    message: str | None = None,
) -> BazaarBid:
    return BazaarBid(
        id=uuid.uuid4().hex,
        listing_post_id=listing_post_id,
        bidder_user_id=bidder_user_id,
        amount=int(amount),
        created_at=datetime.now(timezone.utc).isoformat(),
        message=message,
    )
