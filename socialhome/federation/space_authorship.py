"""Who may write space content on behalf of whom (§24.11).

The §24.11 pipeline authenticates the *household* that signed an
envelope (``from_instance``) and the follower gate decides whether that
household may write to the gated space at all. Neither says anything
about the *person* a payload names: ``author``, ``created_by``,
``voter_user_id``, ``seller_user_id`` … are fields the sender writes. A
member household that could name somebody else's member there could post
as them, vote for them, or edit and delete their rows.

:class:`SpaceAuthorship` answers that question from the one fact the
receiver already holds and the sender cannot forge: the roster mirror in
``space_remote_members``, keyed on ``(space_id, instance_id, user_id)``.
A remote ``user_id`` is derived from its home instance's key, so a live
seat for ``(space, from_instance, user)`` exists only when ``user`` really
is a member of this space *on the household that signed the envelope*.
No new key, table or field — the same lookup the
``SPACE_MEMBER_PROFILE_UPDATED`` handler uses.

Four rules, picked per event family by the handlers:

* :meth:`acts_for` — strict: the named user is seated on the sender.
  Personal actions (a vote, an RSVP, a schedule answer, a bid) and the
  owner-only state changes (closing one's poll, finalising one's
  schedule, a seller settling a listing) — nobody else does these, the
  local services refuse them too.
* :meth:`may_author` — creates: :meth:`acts_for`, or the space **host**
  relaying a row of a *remote* member. The host re-emits every member's
  rows on the §25.6 resume / §319.6 resync replay (with
  ``from_instance = host``), and it is already the authority whose roster
  every other household trusts. It may never author as one of *our* local
  users — the receiver knows its own people, and their rows only ever
  originate here.
* :meth:`writes_here` — the collaborative families (pages, stickies,
  calendar events), which any member may edit or delete locally: any
  writer household, the row's attribution untouched (the repo upserts
  never rewrite ``created_by`` / ``author``).
* :meth:`may_mutate` — edits / deletes of an owned row: :meth:`acts_for`
  for the row's owner, or a **moderator** household — the host, or a
  household holding a live ``admin`` seat. That is the federated form of
  the local "author or space admin" rule (``SpaceService.delete_post``,
  ``edit_comment``, ``GalleryService.delete_item`` …); moderation deletes
  ride the same ``*_DELETED`` events, so without it a moderated post would
  disappear everywhere except on the households that did not moderate it.

The bot bridge posts under the shared :data:`SYSTEM_AUTHOR` identity,
which is no member at all: any writer household may create such a row
(nobody is impersonated), and only a moderator may change one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.space import SpaceRole
from ..domain.user import SYSTEM_AUTHOR

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from .pending_seat_buffer import PendingSeatBuffer
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

#: Seats that write — the same set as the §24.11 follower gate.
_WRITER_ROLES: frozenset[str] = frozenset(
    {SpaceRole.MEMBER.value, SpaceRole.ADMIN.value}
)


class SpaceAuthorship:
    """Bind the users a space-content payload names to the sending household."""

    __slots__ = ("_spaces", "_seats", "_users", "_pending")

    def __init__(
        self,
        *,
        space_repo: "AbstractSpaceRepo",
        remote_member_repo: "AbstractSpaceRemoteMemberRepo",
        user_repo: "AbstractUserRepo",
        pending: "PendingSeatBuffer | None" = None,
    ) -> None:
        self._spaces = space_repo
        self._seats = remote_member_repo
        self._users = user_repo
        #: Where a write naming a user we hold no row for at all waits for
        #: that user's seat (see :meth:`hold_or_refuse`). ``None`` refuses.
        self._pending = pending

    async def acts_for(
        self,
        event: "FederationEvent",
        space_id: str,
        user_id: str,
        *,
        any_role: bool = False,
    ) -> bool:
        """``user_id`` holds a live WRITER seat in ``space_id`` on the sender.

        ``any_role`` also accepts a read-only ``subscriber`` seat — for a
        change to a row that user already owns, never for a new write.
        """
        sender = str(event.from_instance or "")
        if not user_id or not sender or not space_id:
            return False
        seat = await self._seats.get(space_id, sender, user_id)
        if seat is None:
            return False
        return any_role or seat.role in _WRITER_ROLES

    async def is_host(self, event: "FederationEvent", space_id: str) -> bool:
        """The sender is the household hosting ``space_id``."""
        sender = str(event.from_instance or "")
        if not sender or not space_id:
            return False
        space = await self._spaces.get(space_id)
        return space is not None and space.owner_instance_id == sender

    async def is_moderator(self, event: "FederationEvent", space_id: str) -> bool:
        """The host, or a household holding a live ``admin`` seat here."""
        if await self.is_host(event, space_id):
            return True
        sender = str(event.from_instance or "")
        if not sender:
            return False
        seats = await self._seats.list_for_instance(
            space_id, sender, include_tombstoned=False
        )
        return any(s.role == SpaceRole.ADMIN.value for s in seats)

    async def writes_here(self, event: "FederationEvent", space_id: str) -> bool:
        """The host, or a household holding a live ``member`` / ``admin`` seat.

        The rule for the collaborative families (pages, stickies, calendar
        events — any member may edit or delete them locally). It repeats the
        §24.11 follower gate on purpose, at the handler, so a code path that
        reaches the handler without passing the gate still gets the answer.
        """
        if await self.is_host(event, space_id):
            return True
        sender = str(event.from_instance or "")
        if not sender:
            return False
        seats = await self._seats.list_for_instance(
            space_id, sender, include_tombstoned=False
        )
        return any(s.role in _WRITER_ROLES for s in seats)

    async def _is_local_user(self, user_id: str) -> bool:
        return await self._users.get_by_user_id(user_id) is not None

    async def _known_in_space(self, space_id: str, user_id: str) -> bool:
        """Any row at all — live or removed, on any household."""
        return (
            await self._seats.get_including_tombstones(space_id, "", user_id)
            is not None
        )

    async def may_author(
        self,
        event: "FederationEvent",
        space_id: str,
        user_id: str,
        *,
        subscriber_comment: bool = False,
    ) -> bool:
        """May the sender create a row attributed to ``user_id``?

        ``subscriber_comment`` is the one write a read-only seat may make:
        a comment, when the space turned ``allow_subscriber_comment`` on.
        """
        if user_id == SYSTEM_AUTHOR:
            return True
        if await self.acts_for(event, space_id, user_id):
            return True
        if subscriber_comment and await self._subscriber_may_comment(
            event, space_id, user_id
        ):
            return True
        if not user_id or await self._is_local_user(user_id):
            return False
        # The host relays rows of people it seated — never of a user this
        # space has no record of anywhere.
        return await self.is_host(event, space_id) and await self._known_in_space(
            space_id, user_id
        )

    async def _subscriber_may_comment(
        self, event: "FederationEvent", space_id: str, user_id: str
    ) -> bool:
        seat = await self._seats.get(space_id, str(event.from_instance or ""), user_id)
        if seat is None or seat.role != SpaceRole.SUBSCRIBER.value:
            return False
        space = await self._spaces.get(space_id)
        return bool(space is not None and space.features.allow_subscriber_comment)

    async def may_mutate(
        self,
        event: "FederationEvent",
        space_id: str,
        owner_user_id: str,
    ) -> bool:
        """May the sender edit / delete a row owned by ``owner_user_id``?"""
        if owner_user_id != SYSTEM_AUTHOR and await self.acts_for(
            event, space_id, owner_user_id, any_role=True
        ):
            return True
        return await self.is_moderator(event, space_id)

    async def hold_or_refuse(
        self,
        event: "FederationEvent",
        *,
        space_id: str,
        what: str,
        row_id: str,
        user_id: str,
    ) -> None:
        """Refuse a write naming ``user_id`` — or, when this space has no
        record of that user at all (the roster gossip seating them has not
        reached us yet), hold it until their seat lands.

        A user seated on ANOTHER household, or one who was removed, is
        known, so that is always a refusal: those are not a race.
        """
        if (
            self._pending is not None
            and user_id
            and user_id != SYSTEM_AUTHOR
            and not await self._known_in_space(space_id, user_id)
            and not await self._is_local_user(user_id)
            and self._pending.hold(event, space_id=space_id, user_id=user_id)
        ):
            log.info(
                "%s from %s: %s %s in space %s names %r, whose seat has not "
                "reached us yet — holding the write until it does",
                getattr(event, "event_type", "?"),
                getattr(event, "from_instance", "?"),
                what,
                row_id,
                space_id,
                user_id,
            )
            return
        self.log_refusal(
            event, space_id=space_id, what=what, row_id=row_id, user_id=user_id
        )

    @staticmethod
    def log_refusal(
        event: "FederationEvent",
        *,
        space_id: str,
        what: str,
        row_id: str,
        user_id: str,
    ) -> None:
        """WARNING for a write that names a user the sender does not speak for.

        Unlike a missing row, this is never benign delivery noise: the
        sender signed an envelope acting for somebody who is not its member.
        """
        log.warning(
            "%s from %s: %s %s in space %s names %r, who is not a member of "
            "the sending household there — refusing the write",
            getattr(event, "event_type", "?"),
            getattr(event, "from_instance", "?"),
            what,
            row_id,
            space_id,
            user_id,
        )
