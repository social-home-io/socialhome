"""A listing's public questions and answers — the seller's FAQ (v_75).

The seller adds, edits and removes short question / answer pairs on their
own active listing — often from a buyer's question in an "Ask the seller"
thread, reworded (the SPA pre-fills the editor; nothing here links an
entry to a thread or a person: an entry never names who asked). Everyone
who can see the listing reads them.

Rules on the seller's household (the receivers re-check them,
``federation_inbound/bazaar_faq.py``):

* only the seller, only while the listing is active, only as an edit of
  their own post under the space's ``posts`` access level
  (:meth:`BazaarService.editable_listing`), never a protected account
  (§CP.R, ``ProtectedCapability.BAZAAR``);
* plain text, a question ≤ 200 characters on one line, an answer ≤ 500
  (:func:`~socialhome.domain.bazaar_faq.clean_question` /
  :func:`~socialhome.domain.bazaar_faq.clean_answer`), at most 10 entries;
* at most :data:`~socialhome.domain.bazaar_faq.BAZAAR_FAQ_RATE_CAP` changes
  per listing per hour — each one goes to every member household.

Every change publishes :class:`BazaarListingFaqChanged`; the outbound sends
that one entry to every member household (``BazaarOutbound``) and the
realtime service refreshes open listings. Nobody is notified: it is public
information on the listing, read where the listing is read.
"""

from __future__ import annotations

import logging

from ..domain.bazaar_faq import (
    BAZAAR_FAQ_RATE_CAP,
    BazaarFaqEntry,
    BazaarFaqRateLimitedError,
    clean_answer,
    clean_question,
)
from ..domain.child_protection import ProtectedCapability
from ..domain.events import BazaarListingFaqChanged
from ..domain.post import BazaarListing
from ..infrastructure.event_bus import EventBus
from ..rate_limiter import RateLimiter
from ..repositories.bazaar_repo import AbstractBazaarRepo
from .bazaar_service import BazaarService
from .protection_gate import ProtectionGateMixin

log = logging.getLogger(__name__)

_RATE_WINDOW_S = int(BAZAAR_FAQ_RATE_CAP[0].total_seconds())
_RATE_LIMIT = BAZAAR_FAQ_RATE_CAP[1]


class BazaarFaqService(ProtectionGateMixin):
    """Read and write a listing's questions and answers."""

    __slots__ = ("_bazaar", "_bus", "_changes", "_child_protection", "_repo")

    def __init__(
        self,
        *,
        bazaar_service: BazaarService,
        bazaar_repo: AbstractBazaarRepo,
        bus: EventBus,
    ) -> None:
        self._bazaar = bazaar_service
        self._repo = bazaar_repo
        self._bus = bus
        self._child_protection = None
        #: FAQ changes per listing in the last hour (the send backstop).
        self._changes = RateLimiter()

    async def list_entries(
        self, post_id: str, viewer_user_id: str
    ) -> list[BazaarFaqEntry]:
        """The listing's live entries, for anyone who may see the listing
        (the unknown id's 404 for anyone else) — a protected account and a
        follower included: reading a listing is not trading."""
        await self._bazaar.get_visible_listing(post_id, viewer_user_id)
        return await self._repo.list_faq(post_id)

    async def add(
        self, post_id: str, *, actor_user_id: str, question: object, answer: object
    ) -> BazaarFaqEntry:
        """A new entry at the end of the listing's list."""
        listing = await self._writable(post_id, actor_user_id)
        q, a = clean_question(question), clean_answer(answer)
        self._charge(post_id)
        entry = await self._repo.add_faq_entry(post_id, question=q, answer=a)
        await self._announce(listing, entry)
        return entry

    async def edit(
        self,
        post_id: str,
        entry_id: str,
        *,
        actor_user_id: str,
        question: object,
        answer: object,
    ) -> BazaarFaqEntry:
        """New words for a live entry (the seller may reword a question)."""
        listing = await self._writable(post_id, actor_user_id)
        q, a = clean_question(question), clean_answer(answer)
        self._charge(post_id)
        entry = await self._repo.edit_faq_entry(post_id, entry_id, question=q, answer=a)
        await self._announce(listing, entry)
        return entry

    async def remove(self, post_id: str, entry_id: str, *, actor_user_id: str) -> None:
        """Take an entry off the listing, everywhere."""
        listing = await self._writable(post_id, actor_user_id)
        self._charge(post_id)
        entry = await self._repo.remove_faq_entry(post_id, entry_id)
        await self._announce(listing, entry)

    async def _writable(self, post_id: str, actor_user_id: str) -> BazaarListing:
        listing = await self._bazaar.editable_listing(post_id, actor_user_id)
        # §CP.R: a protected account doesn't trade (its listings were closed
        # when protection began; this holds the line for a race).
        await self._require_unrestricted(actor_user_id, ProtectedCapability.BAZAAR)
        if not self._changes.has_room(
            post_id, limit=_RATE_LIMIT, window_s=_RATE_WINDOW_S
        ):
            raise BazaarFaqRateLimitedError
        return listing

    def _charge(self, post_id: str) -> None:
        self._changes.is_allowed(post_id, limit=_RATE_LIMIT, window_s=_RATE_WINDOW_S)
        self._changes.prune(window_s=_RATE_WINDOW_S)

    async def _announce(self, listing: BazaarListing, entry: BazaarFaqEntry) -> None:
        await self._bus.publish(
            BazaarListingFaqChanged(
                space_id=listing.space_id,
                seller_user_id=listing.seller_user_id,
                entry=entry,
            )
        )
