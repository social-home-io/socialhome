"""“Ask the seller” — open, list and guard the threads about a listing (v_74).

A buyer with a writer seat in a listing's space asks its seller about it.
:meth:`BazaarThreadService.ask` picks the one right path behind the one
button:

* the seller is in the buyer's own household, or in a household it paired
  with directly → the normal 1:1 DM (:meth:`DmService.create_dm`) — no new
  wire;
* anyone else in the space (a mesh-only member, a link-joined one) → the
  private listing thread: a ``dm`` conversation with
  ``bazaar_listing_id`` set, seating the buyer and the seller's remote
  seat. Its messages travel as ``BAZAAR_THREAD_MESSAGE`` to the seller's
  household only (:class:`~socialhome.services.bazaar_thread_outbound
  .BazaarThreadOutbound`). Nothing here creates a contact, a pairing or a
  ``remote_users`` row.

:meth:`require_write` is the live gate :class:`DmService` asks on every
message a local person writes in a listing thread: their trading seat
(a writer for the buyer, any seat for the seller), not a protected account
(``ProtectedCapability.BAZAAR``), the other person still seated in the space
(not banned, not removed), and the thread still open
(:func:`~socialhome.domain.bazaar_thread.listing_thread_writable`). Blocks
and guardian blocks are the DM rules :class:`DmService` already applies to
the two seats.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..domain.bazaar_thread import (
    ListingThreadClosedError,
    ListingThreadUnsupportedError,
    listing_thread_closes_at,
    listing_thread_id,
    listing_thread_writable,
)
from ..domain.child_protection import ProtectedCapability
from ..domain.conversation import Conversation
from ..domain.federation import InstanceSource, PairingStatus
from ..domain.federation_capabilities import FederationCapability
from ..domain.post import BazaarListing
from ..domain.space import WRITER_ROLES
from ..repositories.bazaar_repo import AbstractBazaarRepo
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from .bazaar_service import BazaarService, ListingNotFoundError, OwnListingError
from .dm_service import (
    GUARDIAN_BLOCK_DETAIL,
    RECIPIENT_BLOCKED_DETAIL,
    YOU_BLOCKED_DETAIL,
    DmService,
    RecipientBlockedError,
)
from .protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)

_WRITER_ROLE_VALUES: frozenset[str] = frozenset(r.value for r in WRITER_ROLES)

#: How the seller's side names a buyer it holds no roster name for.
_SOMEONE = "Someone"


@dataclass(slots=True, frozen=True)
class AskResult:
    """Where "Ask the seller" leads: ``"dm"`` (the normal 1:1) or
    ``"thread"`` (the private listing thread), and the conversation."""

    kind: str
    conversation_id: str


@dataclass(slots=True, frozen=True)
class ListingThreadInfo:
    """What a thread view shows about its listing, for one viewer."""

    listing: BazaarListing
    #: ``"seller"`` or ``"buyer"`` — the viewer's side.
    role: str
    #: The other person in the thread.
    other_user_id: str
    #: Either side may still write.
    writable: bool
    #: When the thread turns (turned) read-only; ``None`` while the listing
    #: is active.
    closes_at: datetime | None


class BazaarThreadService(ProtectionGateMixin):
    """Open, list and guard "Ask the seller" threads."""

    __slots__ = (
        "_bazaar",
        "_child_protection",
        "_convos",
        "_dms",
        "_federation",
        "_federation_repo",
        "_listings",
        "_seats",
        "_spaces",
        "_users",
    )

    def __init__(
        self,
        *,
        bazaar_service: BazaarService,
        bazaar_repo: AbstractBazaarRepo,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        space_repo: AbstractSpaceRepo,
        remote_member_repo: AbstractSpaceRemoteMemberRepo,
        dm_service: DmService,
    ) -> None:
        self._bazaar = bazaar_service
        self._listings = bazaar_repo
        self._convos = conversation_repo
        self._users = user_repo
        self._spaces = space_repo
        self._seats = remote_member_repo
        self._dms = dm_service
        self._federation: FederationService | None = None
        self._federation_repo: AbstractFederationRepo | None = None
        self._child_protection = None

    def attach_federation(
        self,
        federation_service: FederationService,
        federation_repo: AbstractFederationRepo,
    ) -> None:
        """Wire federation (built after this service in ``app.py``). Without
        it no thread to another household can be opened (fail closed)."""
        self._federation = federation_service
        self._federation_repo = federation_repo

    # ── Opening ────────────────────────────────────────────────────────

    async def ask(self, post_id: str, *, username: str) -> AskResult:
        """ "Ask the seller" on ``post_id`` for the local ``username``.

        Raises ``ListingNotFoundError`` (404) when the listing isn't
        visible to them, ``OwnListingError`` for the seller,
        ``SpacePermissionError`` ``SUBSCRIBER_READ_ONLY`` (403) for a
        follower, ``AccountProtectedError`` (403) for a protected account,
        :class:`RecipientBlockedError` (403) across a block,
        :class:`ListingThreadClosedError` (409) once the thread could no
        longer be written, :class:`ListingThreadUnsupportedError` (409) for
        a seller's household below v_74.
        """
        user = await self._users.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        listing = await self._bazaar.tradeable_listing(post_id, user.user_id)
        if listing.seller_user_id == user.user_id:
            raise OwnListingError()
        await self._require_unrestricted(user.user_id, ProtectedCapability.BAZAAR)
        if not listing_thread_writable(listing, now=datetime.now(UTC)):
            raise ListingThreadClosedError()
        await self._require_open_space(listing.space_id)
        await self._guard_blocks(user.user_id, listing.seller_user_id)
        thread_id = listing_thread_id(listing.post_id, user.user_id)
        existing = await self._convos.get(thread_id)
        if existing is not None and existing.bazaar_listing_id == listing.post_id:
            # A thread already asked in stays the place to talk, whatever
            # the two households did since (paired, or not).
            await self._reopen(existing, listing, user.user_id, username)
            return AskResult(kind="thread", conversation_id=existing.id)
        seller_local = await self._users.get_by_user_id(listing.seller_user_id)
        if seller_local is not None:
            conv = await self._dms.create_dm(
                creator_username=username, other_username=seller_local.username
            )
            return AskResult(kind="dm", conversation_id=conv.id)
        if await self._paired_with_seller(listing.seller_user_id):
            conv = await self._dms.create_dm(
                creator_username=username, other_user_id=listing.seller_user_id
            )
            return AskResult(kind="dm", conversation_id=conv.id)
        seat = await self._live_seat(listing.space_id, listing.seller_user_id)
        if seat is None:
            # The seller no longer sits in the space: nobody to ask.
            raise ListingNotFoundError(post_id)
        if self._federation is None or not await self._federation.space_member_supports(
            seat.instance_id, min_version=FederationCapability.MIN_FOR_BAZAAR_THREAD
        ):
            raise ListingThreadUnsupportedError()
        conv = await self._convos.open_listing_thread(
            conversation_id=thread_id,
            listing_post_id=listing.post_id,
            username=username,
            remote_instance_id=seat.instance_id,
            remote_user_id=listing.seller_user_id,
            remote_display_name=seat.display_name or _SOMEONE,
        )
        return AskResult(kind="thread", conversation_id=conv.id)

    async def _reopen(
        self, conv: Conversation, listing: BazaarListing, user_id: str, username: str
    ) -> None:
        """Show a thread the buyer hid again (re-seat their own row)."""
        for m in await self._convos.list_members(conv.id):
            if m.username == username and m.deleted_at is None:
                return
        seat = await self._live_seat(listing.space_id, listing.seller_user_id)
        remote = await self._convos.list_remote_members(conv.id)
        if seat is None or not remote:
            return
        await self._convos.open_listing_thread(
            conversation_id=conv.id,
            listing_post_id=listing.post_id,
            username=username,
            remote_instance_id=remote[0].instance_id,
            remote_user_id=listing.seller_user_id,
            remote_display_name=seat.display_name or remote[0].display_name or _SOMEONE,
        )

    async def _paired_with_seller(self, seller_user_id: str) -> bool:
        """The seller's household is one this household paired with
        directly — a ``remote_users`` row on a CONFIRMED peer that is not a
        space-only (``space_session``) relationship. Then the normal DM
        is the way to talk."""
        if self._federation_repo is None:
            return False
        remote = await self._users.get_remote(seller_user_id)
        if remote is None:
            return False
        peer = await self._federation_repo.get_instance(remote.instance_id)
        return (
            peer is not None
            and peer.status is PairingStatus.CONFIRMED
            and peer.source is not InstanceSource.SPACE_SESSION
        )

    async def _live_seat(self, space_id: str, user_id: str):
        """``user_id``'s live roster seat in ``space_id`` (any household),
        or ``None`` — removed, banned or never seated."""
        row = await self._seats.get_including_tombstones(space_id, "", user_id)
        if row is None or row.tombstoned:
            return None
        if await self._spaces.is_banned(space_id, user_id):
            return None
        return row

    async def _guard_blocks(self, user_id: str, other_user_id: str) -> None:
        """The DM block rules, both ways, before a thread is opened."""
        if await self._guardian_blocked(user_id, other_user_id):
            raise RecipientBlockedError(
                GUARDIAN_BLOCK_DETAIL
                if await self._is_protected(user_id)
                else RECIPIENT_BLOCKED_DETAIL
            )
        if await self._users.is_blocked(other_user_id, user_id):
            raise RecipientBlockedError(RECIPIENT_BLOCKED_DETAIL)
        if await self._users.is_blocked(user_id, other_user_id):
            raise RecipientBlockedError(YOU_BLOCKED_DETAIL)

    # ── Reading ────────────────────────────────────────────────────────

    async def list_threads(
        self, post_id: str, *, username: str
    ) -> list[tuple[Conversation, ListingThreadInfo]]:
        """The threads about ``post_id`` the viewer sits in: every buyer's
        for the seller, their own for a buyer. 404 for a listing they
        can't see."""
        user = await self._users.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        listing = await self._bazaar.get_visible_listing(post_id, user.user_id)
        out: list[tuple[Conversation, ListingThreadInfo]] = []
        for conv in await self._convos.list_listing_threads(listing.post_id, username):
            info = await self._info_for(conv, listing, user.user_id)
            if info is not None:
                out.append((conv, info))
        return out

    async def info(
        self, conv: Conversation, viewer_user_id: str
    ) -> ListingThreadInfo | None:
        """The listing card + state for one listing thread, or ``None`` when
        it is no listing thread or its listing is gone."""
        if conv.bazaar_listing_id is None:
            return None
        listing = await self._listings.get_listing(conv.bazaar_listing_id)
        if listing is None:
            return None
        return await self._info_for(conv, listing, viewer_user_id)

    async def _info_for(
        self, conv: Conversation, listing: BazaarListing, viewer_user_id: str
    ) -> ListingThreadInfo | None:
        other = await self._other_user_id(conv)
        if other is None:
            return None
        now = datetime.now(UTC)
        return ListingThreadInfo(
            listing=listing,
            role="seller" if listing.seller_user_id == viewer_user_id else "buyer",
            other_user_id=other,
            writable=listing_thread_writable(listing, now=now),
            closes_at=listing_thread_closes_at(listing),
        )

    async def _other_user_id(self, conv: Conversation) -> str | None:
        for seat in await self._convos.list_remote_members(conv.id):
            if seat.user_id:
                return seat.user_id
        return None

    # ── The live write gate (DmService asks it) ────────────────────────

    async def require_write(self, conv: Conversation, user_id: str) -> None:
        """May the local ``user_id`` write in the listing thread ``conv``
        right now? Raises otherwise (see the module docstring)."""
        listing = (
            await self._listings.get_listing(conv.bazaar_listing_id)
            if conv.bazaar_listing_id
            else None
        )
        if listing is None:
            raise ListingThreadClosedError()
        await self._require_unrestricted(user_id, ProtectedCapability.BAZAAR)
        own = await self._spaces.get_member(listing.space_id, user_id)
        if own is None:
            raise PermissionError("You are no longer in this listing's space.")
        is_seller = listing.seller_user_id == user_id
        if not is_seller and str(own.role) not in _WRITER_ROLE_VALUES:
            raise PermissionError("Only members who can trade may ask the seller.")
        other = await self._other_user_id(conv)
        seat = (
            await self._live_seat(listing.space_id, other)
            if other is not None
            else None
        )
        if seat is None or (is_seller and seat.role not in _WRITER_ROLE_VALUES):
            raise PermissionError("The other person is no longer in this space.")
        if not listing_thread_writable(listing, now=datetime.now(UTC)):
            raise ListingThreadClosedError()
        await self._require_open_space(listing.space_id)

    async def _require_open_space(self, space_id: str) -> None:
        """The listing's space still trades here: held, not dissolved or
        archived, its Bazaar on — what every receiver checks too, so a
        message is never written here that the other side drops."""
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.dissolved
            or space.archived
            or not space.features.bazaar
        ):
            raise ListingThreadClosedError()
