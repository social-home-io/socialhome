"""Outbound federation for "Ask the seller" threads (v_74).

Subscribes to :class:`DmMessageCreated` and, for a message a local person
wrote in a listing thread (``Conversation.bazaar_listing_id`` set), sends one
``BAZAAR_THREAD_MESSAGE`` to the OTHER person's household — and to nobody
else:

* **One recipient.** :meth:`FederationService.broadcast_to_space_members`
  narrowed with ``only_instances`` to that single household. It never adds
  a target: the household must be a member household of the listing's
  space (``space_instances``) and not banned, so a message can only ever
  reach a household the space already delivers to.
* **The space's own transport.** Each per-peer send rides
  ``send_with_mesh_fallback``: a directly paired member gets it directly, a
  non-paired one under ``SPACE_ROUTED`` (sealed end to end to the target's
  ephemeral key — a relay household sees ciphertext only), a link-joined one
  through its ``space_session`` relay seat (sealed under that pair's session
  key — the GFS forwards ciphertext only). No direct channel is opened that
  the space did not already have.
* **Encryption-first.** ``space_id`` is the only routing field; everything
  else (listing, buyer, author, text) is inside the encrypted payload. No
  conversation id travels: both households derive it
  (:func:`~socialhome.domain.bazaar_thread.listing_thread_id`).
* **Version gate.** The target must be at ``MIN_FOR_BAZAAR_THREAD`` by
  :meth:`FederationService.space_member_supports`; below it nothing is sent
  (the buyer's open already refused with ``SELLER_HOUSEHOLD_TOO_OLD``).
* **No echo.** A message that arrived from another household carries
  ``origin_instance_id`` and is never sent on.

Cost: 1 recipient × 1 envelope (≈ 0.5–1.5 KB: a ≤ 1000-character text plus
ids, sealed) per message — no fan-out.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import DmMessageCreated
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..infrastructure.event_bus import EventBus
from ..repositories.bazaar_repo import AbstractBazaarRepo
from ..repositories.conversation_repo import AbstractConversationRepo

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)


class BazaarThreadOutbound:
    """A local message in a listing thread → ``BAZAAR_THREAD_MESSAGE`` to the
    other person's household."""

    __slots__ = ("_bus", "_convos", "_federation", "_listings")

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
        conversation_repo: AbstractConversationRepo,
        bazaar_repo: AbstractBazaarRepo,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._convos = conversation_repo
        self._listings = bazaar_repo

    def wire(self) -> None:
        self._bus.subscribe(DmMessageCreated, self._on_created)

    async def _on_created(self, event: DmMessageCreated) -> None:
        if event.origin_instance_id is not None or event.system_scope is not None:
            return
        conv = await self._convos.get(event.conversation_id)
        if conv is None or conv.bazaar_listing_id is None:
            return
        listing = await self._listings.get_listing(conv.bazaar_listing_id)
        if listing is None:
            return
        seats = [
            s
            for s in await self._convos.list_remote_members(conv.id)
            if s.user_id is not None
        ]
        if len(seats) != 1:
            log.warning(
                "listing thread %s has %d remote seats — not sent", conv.id, len(seats)
            )
            return
        other = seats[0]
        buyer = (
            other.user_id
            if event.sender_user_id == listing.seller_user_id
            else event.sender_user_id
        )
        target = other.instance_id
        if not await self._federation.space_member_supports(
            target, min_version=FederationCapability.MIN_FOR_BAZAAR_THREAD
        ):
            log.info(
                "listing thread %s: household %s is below v_%d — not sent",
                conv.id,
                target,
                FederationCapability.MIN_FOR_BAZAAR_THREAD,
            )
            return
        payload = {
            "space_id": listing.space_id,
            "listing_post_id": listing.post_id,
            "buyer_user_id": buyer,
            "author_user_id": event.sender_user_id,
            "message_id": event.message_id,
            "content": event.content,
            "reply_to_id": event.reply_to_id,
            "created_at": event.occurred_at.isoformat(),
        }
        try:
            result = await self._federation.broadcast_to_space_members(
                listing.space_id,
                FederationEventType.BAZAAR_THREAD_MESSAGE,
                payload,
                only_instances={target},
            )
        except Exception:
            log.exception(
                "BAZAAR_THREAD_MESSAGE to %s failed (thread %s, message %s)",
                target,
                conv.id,
                event.message_id,
            )
            return
        if result.attempted == 0:
            # Not a member household of the space (any more), or banned:
            # the space delivers nothing to it, and neither does a thread.
            log.warning(
                "listing thread %s: %s is no member household of space %s — "
                "message %s not sent",
                conv.id,
                target,
                listing.space_id,
                event.message_id,
            )
            return
        for failure in result.terminal_failures:
            log.warning(
                "listing thread %s: message %s to %s not delivered (%s)",
                conv.id,
                event.message_id,
                failure.instance_id,
                failure.error,
            )
