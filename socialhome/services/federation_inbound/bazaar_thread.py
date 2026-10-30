"""Inbound ``BAZAAR_THREAD_MESSAGE`` — "Ask the seller" (v_74).

By the time the handler runs, the §24.11 pipeline has verified the envelope
(signature, timestamp, replay, ban, peer class — a link-joined household may
send it —, the writer gate — a follower-only household's message dies there
— and the archive gate). The handler then re-decides everything about the
PEOPLE and ROWS the payload names, on this household's own facts, and
drops (WARNING) anything else:

* **The space and the listing.** The payload's space must be the
  envelope's (:func:`resolve_space_id`), held here, not dissolved, with its
  Bazaar on; the listing must be held here and belong to that space.
* **Who writes to whom.** Exactly two directions exist:

  - *buyer → seller*: the seller is a local user holding a seat here; the
    author is the payload's ``buyer_user_id`` and holds a live **writer**
    seat in the space on the sending household
    (:meth:`SpaceAuthorship.acts_for` — strict: no host relay, nobody
    speaks for another household's people), is not banned and is not the
    seller. This opens the thread here on its first message.
  - *seller → buyer*: the buyer is a local user holding a writer seat here,
    the author is the listing's seller seated on the sending household, and
    the thread already exists here — a seller never cold-opens a thread.

* **Child protection and blocks.** The local person must not be a protected
  account (``ProtectedCapability.BAZAAR``); a personal block either way or
  a guardian block between the two drops the message. (The other side's
  people are judged by their own household before it sends.)
* **Still open.** The listing is active, or ended less than
  :data:`~socialhome.domain.bazaar_thread.LISTING_THREAD_GRACE` ago.
* **The message.** Text only, at most ``MAX_DM_LENGTH`` characters, a fresh
  32-hex id (an id held anywhere already is dropped — no overwrite, no
  replay into another conversation), a reply only to a message of the same
  thread. A future ``created_at`` is clamped to now.

Accepted messages land in the thread like local ones and publish
:class:`DmMessageCreated` (WS frame, bell / push) stamped with
``origin_instance_id`` so the outbound never echoes them.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ...domain.bazaar_thread import listing_thread_id, listing_thread_writable
from ...domain.child_protection import ProtectedCapability
from ...domain.conversation import ConversationMessage
from ...domain.events import DmMessageCreated
from ...domain.federation import FederationEvent, FederationEventType
from ...domain.space import WRITER_ROLES
from ...federation.space_scope import resolve_space_id
from ...infrastructure.event_bus import EventBus
from ...repositories.bazaar_repo import AbstractBazaarRepo
from ...repositories.conversation_repo import AbstractConversationRepo
from ...repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
from ...repositories.space_repo import AbstractSpaceRepo
from ...repositories.user_repo import AbstractUserRepo
from ...utils.datetime import as_utc, parse_iso8601_lenient
from ..dm_service import MAX_DM_LENGTH
from ..protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from ...federation.federation_service import FederationService

log = logging.getLogger(__name__)

_WRITER_ROLE_VALUES: frozenset[str] = frozenset(r.value for r in WRITER_ROLES)

#: A message id this household mints for a thread: ``uuid4().hex``.
_MESSAGE_ID = re.compile(r"^[0-9a-f]{32}$")

#: How a person reads when no roster name is held for them.
_SOMEONE = "Someone"


def _str(payload: dict, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _refuse(event: FederationEvent, message_id: str, reason: str) -> None:
    """WARNING for a refused thread message — a peer bug or a misbehaving
    peer, never benign delivery noise."""
    log.warning(
        "%s from %s: thread message %s refused — %s",
        event.event_type,
        event.from_instance,
        message_id or "?",
        reason,
    )


class BazaarThreadInboundHandlers(ProtectionGateMixin):
    """Register and run the ``BAZAAR_THREAD_MESSAGE`` handler."""

    __slots__ = (
        "_bus",
        "_child_protection",
        "_convos",
        "_listings",
        "_seats",
        "_spaces",
        "_users",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        bazaar_repo: AbstractBazaarRepo,
        space_repo: AbstractSpaceRepo,
        remote_member_repo: AbstractSpaceRemoteMemberRepo,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
    ) -> None:
        self._bus = bus
        self._listings = bazaar_repo
        self._spaces = space_repo
        self._seats = remote_member_repo
        self._convos = conversation_repo
        self._users = user_repo
        self._child_protection = None

    def attach_to(self, federation_service: FederationService) -> None:
        """Register the handler on the event registry."""
        federation_service._event_registry.register(
            FederationEventType.BAZAAR_THREAD_MESSAGE, self._on_message
        )

    async def _on_message(self, event: FederationEvent) -> None:
        await self.apply(event)

    async def apply(self, event: FederationEvent) -> bool:
        """Apply one thread message; ``True`` when it was stored."""
        p = event.payload if isinstance(event.payload, dict) else {}
        message_id = _str(p, "message_id")
        listing_id = _str(p, "listing_post_id")
        buyer = _str(p, "buyer_user_id")
        author = _str(p, "author_user_id")
        content = _str(p, "content")
        sender = str(event.from_instance or "")
        if not (message_id and listing_id and buyer and author and content and sender):
            _refuse(event, message_id, "incomplete")
            return False
        if not _MESSAGE_ID.match(message_id):
            _refuse(event, message_id, "malformed message id")
            return False
        if len(content) > MAX_DM_LENGTH:
            _refuse(event, message_id, "content too long")
            return False
        space_id = resolve_space_id(event)
        if not space_id:
            _refuse(event, message_id, "no space")
            return False
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved or not space.features.bazaar:
            _refuse(event, message_id, "space not held, dissolved or Bazaar off")
            return False
        listing = await self._listings.get_listing(listing_id)
        if listing is None or listing.space_id != space_id:
            _refuse(event, message_id, "listing not held in this space")
            return False
        seller = listing.seller_user_id
        if buyer == seller:
            _refuse(event, message_id, "a seller can't ask themselves")
            return False
        if await self._spaces.is_banned(space_id, author):
            _refuse(event, message_id, "author is banned from the space")
            return False
        author_seat = await self._seats.get(space_id, sender, author)
        if author == buyer:
            # buyer → seller: our seller, their writer.
            local_id, remote_id = seller, buyer
            if author_seat is None or author_seat.role not in _WRITER_ROLE_VALUES:
                _refuse(event, message_id, "buyer holds no writer seat on the sender")
                return False
            if await self._spaces.get_member(space_id, seller) is None:
                _refuse(event, message_id, "the seller holds no seat here")
                return False
        elif author == seller:
            # seller → buyer: our writer, their seller — in a thread we opened.
            local_id, remote_id = buyer, seller
            if author_seat is None:
                _refuse(event, message_id, "seller holds no seat on the sender")
                return False
            member = await self._spaces.get_member(space_id, buyer)
            if member is None or str(member.role) not in _WRITER_ROLE_VALUES:
                _refuse(event, message_id, "the buyer holds no writer seat here")
                return False
        else:
            _refuse(event, message_id, "author is neither the buyer nor the seller")
            return False
        local = await self._users.get_by_user_id(local_id)
        if local is None:
            _refuse(event, message_id, "the addressed person does not live here")
            return False
        thread_id = listing_thread_id(listing.post_id, buyer)
        existing = await self._convos.get(thread_id)
        if author == seller and (
            existing is None
            or existing.bazaar_listing_id != listing.post_id
            or not any(
                m.username == local.username
                for m in await self._convos.list_members(thread_id)
            )
        ):
            _refuse(event, message_id, "no thread here — a seller never opens one")
            return False
        if not listing_thread_writable(listing, now=datetime.now(UTC)):
            _refuse(event, message_id, "the thread is closed")
            return False
        if await self._is_restricted(local_id, ProtectedCapability.BAZAAR):
            _refuse(event, message_id, "the addressed person can't trade")
            return False
        if (
            await self._users.is_blocked(local_id, remote_id)
            or await self._users.is_blocked(remote_id, local_id)
            or await self._guardian_blocked(local_id, remote_id)
        ):
            _refuse(event, message_id, "a block separates the two")
            return False
        if await self._convos.get_message(message_id) is not None:
            log.debug(
                "%s from %s: thread message %s already held",
                event.event_type,
                sender,
                message_id,
            )
            return False
        created_at = as_utc(parse_iso8601_lenient(p.get("created_at")))
        # A message from the future would sit atop the thread forever.
        created_at = min(created_at, datetime.now(UTC))
        name = (
            author_seat.display_name if author_seat is not None else None
        ) or _SOMEONE
        conv = await self._convos.open_listing_thread(
            conversation_id=thread_id,
            listing_post_id=listing.post_id,
            username=local.username,
            remote_instance_id=sender,
            remote_user_id=remote_id,
            remote_display_name=name,
            seat_at=created_at.isoformat(),
        )
        reply_to_id: str | None = _str(p, "reply_to_id") or None
        if reply_to_id is not None:
            target = await self._convos.get_message(reply_to_id)
            if target is None or target.conversation_id != conv.id:
                reply_to_id = None
        msg = ConversationMessage(
            id=message_id,
            conversation_id=conv.id,
            sender_user_id=author,
            content=content,
            created_at=created_at,
            reply_to_id=reply_to_id,
        )
        if not await self._convos.insert_message_if_absent(msg):
            return False
        await self._bus.publish(
            DmMessageCreated(
                conversation_id=conv.id,
                message_id=msg.id,
                sender_user_id=author,
                sender_display_name=name,
                recipient_user_ids=(local_id,),
                content=content,
                reply_to_id=reply_to_id,
                occurred_at=msg.created_at,
                origin_instance_id=sender,
            )
        )
        return True
