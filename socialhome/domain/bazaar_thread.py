"""“Ask the seller” — a private thread about one bazaar listing (v_74).

A buyer with a writer seat in a listing's space asks the seller about it
in a 1:1 text thread tied to that one listing. It is a ``dm`` conversation
(``conversations.bazaar_listing_id`` set, migration 0133) seating the local
person and one remote seat for the other person; every message travels to
the other person's household only, sealed, over the space's own transport
(``BAZAAR_THREAD_MESSAGE``).

Pure helpers — no I/O:

* :func:`listing_thread_id` — both households derive the same conversation
  id from the listing and the buyer, so no id rides the wire and one buyer
  has exactly one thread per listing (the primary key enforces it).
* :func:`listing_thread_writable` — open while the listing is active and
  for :data:`LISTING_THREAD_GRACE` after it ended (sold, expired or
  withdrawn): the pickup chat after a sale. Read-only afterwards.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from ..utils.datetime import as_utc, parse_iso8601_optional
from .errors import CodedError
from .post import BazaarListing, BazaarStatus

#: How long a thread stays writable after its listing ended (owner
#: decision 2026-10-30: ~14 days — the handover window after a sale).
LISTING_THREAD_GRACE = timedelta(days=14)

#: Domain separator of :func:`listing_thread_id` (versioned, so a later
#: derivation never collides with this one).
_THREAD_ID_TAG = b"socialhome:bazaar-thread:v1"

#: Every listing-thread id starts with this — never a uuid4 hex DM id — so
#: the DM paths can refuse to open a conversation under such an id (a
#: paired household squatting on a thread's derived id).
LISTING_THREAD_ID_PREFIX = "bzt-"


def listing_thread_id(listing_post_id: str, buyer_user_id: str) -> str:
    """The conversation id of ``buyer_user_id``'s thread about a listing.

    Deterministic, so the buyer's and the seller's households name the same
    thread without shipping an id, and a second "Ask the seller" from the
    same buyer reopens the thread instead of starting another. 32
    characters like every other conversation id:
    :data:`LISTING_THREAD_ID_PREFIX` and 28 hex characters (112 bits).
    """
    digest = hashlib.sha256(
        b"\0".join(
            (
                _THREAD_ID_TAG,
                listing_post_id.encode(),
                buyer_user_id.encode(),
            )
        )
    )
    return LISTING_THREAD_ID_PREFIX + digest.hexdigest()[:28]


def is_listing_thread_id(conversation_id: str) -> bool:
    """Is ``conversation_id`` shaped like a :func:`listing_thread_id`?"""
    return conversation_id.startswith(LISTING_THREAD_ID_PREFIX)


#: What an unreadable end stamp reads as: long ago (fail closed — the
#: thread is read-only rather than open forever).
_LONG_AGO = datetime.min.replace(tzinfo=UTC)


def listing_ended_at(listing: BazaarListing) -> datetime | None:
    """When the listing ended (UTC), or ``None`` while it is active.

    A sale ends it at ``sold_at``; an expiry at ``end_time``. A withdrawn
    listing keeps no timestamp of its own, so its scheduled ``end_time``
    stands in (at most the listing's duration after the withdrawal). An
    unreadable stamp reads as long ago — fail closed.
    """
    if listing.status is BazaarStatus.ACTIVE:
        return None
    stamp = listing.end_time
    if listing.status is BazaarStatus.SOLD and listing.sold_at:
        stamp = listing.sold_at
    parsed = parse_iso8601_optional(stamp)
    return as_utc(parsed) if parsed is not None else _LONG_AGO


def listing_thread_closes_at(listing: BazaarListing) -> datetime | None:
    """When a thread about ``listing`` turns read-only; ``None`` while the
    listing is active (no end yet)."""
    ended = listing_ended_at(listing)
    if ended is None:
        return None
    if ended == _LONG_AGO:
        return ended
    return ended + LISTING_THREAD_GRACE


def listing_thread_writable(listing: BazaarListing, *, now: datetime) -> bool:
    """May either side still write in a thread about ``listing`` at ``now``?"""
    closes = listing_thread_closes_at(listing)
    return closes is None or now < closes


class ListingThreadClosedError(CodedError, PermissionError):
    """A message in a thread whose listing ended more than the grace ago."""

    status = 409
    code = "LISTING_THREAD_CLOSED"
    detail = "This conversation is closed: the listing ended over 14 days ago."


class ListingThreadUnsupportedError(CodedError):
    """The seller's household runs a version without "Ask the seller"."""

    status = 409
    code = "SELLER_HOUSEHOLD_TOO_OLD"
    detail = "The seller's household needs an update before you can ask here."


class ListingThreadTextOnlyError(CodedError, ValueError):
    """Anything but a text message (or an edit / reaction) in a thread."""

    status = 422
    code = "LISTING_THREAD_TEXT_ONLY"
    detail = "Only text messages can be sent about a listing."


class ListingThreadNoChangesError(CodedError, ValueError):
    """An edit, delete or reaction in a thread (v1 sends text only; a
    change would stay on one side)."""

    status = 422
    code = "LISTING_THREAD_NO_CHANGES"
    detail = "Messages about a listing can't be edited, deleted or reacted to."
