"""Inbound ``BAZAAR_LISTING_FAQ_UPDATED`` — a listing's questions and
answers (v_75).

By the time the handler runs, the §24.11 pipeline has verified the envelope
(signature, timestamp, replay, ban, peer class — a link-joined household may
send it —, the writer gate — a follower-only household's change dies there —
and the archive gate). The handler then re-decides everything on this
household's own facts and drops (WARNING) anything else:

* **The space and the listing.** The payload's space is the envelope's
  (:func:`resolve_space_id`), held here and not dissolved; the listing is
  held here, belongs to that space and is still active (an FAQ change to a
  listing that ended is noise from a race, dropped at DEBUG).
* **Only the seller.** The listing's seller holds a live **writer** seat in
  the space on the SENDING household (:meth:`SpaceAuthorship.acts_for` —
  strict: no host relay, nobody speaks for another household's people), is
  not banned, and is not one of this household's own people (only our own
  household writes our sellers' FAQs).
* **The entry.** :func:`~socialhome.domain.bazaar_faq.entry_from_wire`: a
  32-hex id, a positive ``rev``, and text the sender's own cleaning leaves
  unchanged — plain text, a question ≤ 200 characters on one line, an
  answer ≤ 500. Stored newer-``rev``-wins, a removal is final, at most 10
  live entries (:meth:`AbstractBazaarRepo.apply_faq_entry`).
* **Not a flood.** At most twice the sender's cap of changes per listing
  per hour, counted in memory (a backstop against a hostile household).

An accepted change publishes :class:`BazaarListingFaqChanged` stamped with
``origin_instance_id`` (open listings refresh; the outbound never sends it
on). Nobody is notified.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...domain.bazaar_faq import BAZAAR_FAQ_RATE_CAP, entry_from_wire
from ...domain.events import BazaarListingFaqChanged
from ...domain.federation import FederationEvent, FederationEventType
from ...domain.post import BazaarStatus
from ...federation.space_authorship import SpaceAuthorship
from ...federation.space_scope import resolve_space_id
from ...infrastructure.event_bus import EventBus
from ...rate_limiter import RateLimiter
from ...repositories.bazaar_repo import AbstractBazaarRepo
from ...repositories.space_repo import AbstractSpaceRepo
from ...repositories.user_repo import AbstractUserRepo

if TYPE_CHECKING:
    from ...federation.federation_service import FederationService

log = logging.getLogger(__name__)

_WINDOW_S = int(BAZAAR_FAQ_RATE_CAP[0].total_seconds())
#: The receiver's backstop: twice what an honest seller's household sends.
_ARRIVAL_CAP = 2 * BAZAAR_FAQ_RATE_CAP[1]


def _refuse(event: FederationEvent, listing_id: str, reason: str) -> None:
    """WARNING for a refused FAQ change — a peer bug or a misbehaving
    peer, never benign delivery noise."""
    log.warning(
        "%s from %s: FAQ change on listing %s refused — %s",
        event.event_type,
        event.from_instance,
        listing_id or "?",
        reason,
    )


class BazaarFaqInboundHandlers:
    """Register and run the ``BAZAAR_LISTING_FAQ_UPDATED`` handler."""

    __slots__ = ("_arrivals", "_authorship", "_bus", "_listings", "_spaces", "_users")

    def __init__(
        self,
        *,
        bus: EventBus,
        bazaar_repo: AbstractBazaarRepo,
        space_repo: AbstractSpaceRepo,
        user_repo: AbstractUserRepo,
        authorship: SpaceAuthorship,
    ) -> None:
        self._bus = bus
        self._listings = bazaar_repo
        self._spaces = space_repo
        self._users = user_repo
        self._authorship = authorship
        #: FAQ changes taken per listing in the last hour.
        self._arrivals = RateLimiter()

    def attach_to(self, federation_service: FederationService) -> None:
        """Register the handler on the event registry."""
        federation_service._event_registry.register(
            FederationEventType.BAZAAR_LISTING_FAQ_UPDATED, self._on_faq_updated
        )

    async def _on_faq_updated(self, event: FederationEvent) -> None:
        await self.apply(event)

    async def apply(self, event: FederationEvent) -> bool:
        """Apply one FAQ change; ``True`` when it changed what we hold."""
        p = event.payload if isinstance(event.payload, dict) else {}
        listing_id = p.get("listing_post_id")
        if not isinstance(listing_id, str) or not listing_id:
            _refuse(event, "", "no listing")
            return False
        try:
            entry = entry_from_wire(listing_id, p)
        except ValueError as exc:
            _refuse(event, listing_id, f"malformed entry: {exc}")
            return False
        space_id = resolve_space_id(event)
        if not space_id:
            _refuse(event, listing_id, "no space")
            return False
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            _refuse(event, listing_id, "space not held or dissolved")
            return False
        listing = await self._listings.get_listing(listing_id)
        if listing is None or listing.space_id != space_id:
            _refuse(event, listing_id, "listing not held in this space")
            return False
        seller = listing.seller_user_id
        if await self._users.get_by_user_id(seller) is not None:
            _refuse(event, listing_id, "the seller is ours — only we write it")
            return False
        if not await self._authorship.acts_for(event, space_id, seller):
            _refuse(event, listing_id, "the seller holds no writer seat on the sender")
            return False
        if await self._spaces.is_banned(space_id, seller):
            _refuse(event, listing_id, "the seller is banned from the space")
            return False
        if listing.status is not BazaarStatus.ACTIVE:
            log.debug(
                "%s from %s: listing %s is %s — FAQ change not applied",
                event.event_type,
                event.from_instance,
                listing_id,
                listing.status.value,
            )
            return False
        if not self._arrivals.is_allowed(
            listing_id, limit=_ARRIVAL_CAP, window_s=_WINDOW_S
        ):
            _refuse(event, listing_id, "over the listing's FAQ change cap")
            return False
        self._arrivals.prune(window_s=_WINDOW_S)
        if not await self._listings.apply_faq_entry(entry):
            log.debug(
                "%s from %s: FAQ entry %s on %s not newer, removed or past "
                "the maximum — not applied",
                event.event_type,
                event.from_instance,
                entry.entry_id,
                listing_id,
            )
            return False
        await self._bus.publish(
            BazaarListingFaqChanged(
                space_id=space_id,
                seller_user_id=seller,
                entry=entry,
                origin_instance_id=str(event.from_instance or "") or None,
            )
        )
        return True
