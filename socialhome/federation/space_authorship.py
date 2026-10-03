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

Rules, picked per event family by the handlers:

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
  writer household (``member`` / ``moderator`` / ``admin``), the row's
  attribution untouched (the repo upserts never rewrite ``created_by`` /
  ``author``).
* Two authority tiers, mirroring ``SETTINGS_AUTHORITY_ROLES`` and
  ``CONTENT_AUTHORITY_ROLES`` (v_41):

  * **settings** — :meth:`is_admin_household` (the host, or a live
    ``admin`` seat on the sender) and its per-user form :meth:`admin_as`.
    Zones and timetables. A ``moderator`` seat never passes.
  * **content** — :meth:`has_content_authority` (the host, or a live
    ``admin`` or ``moderator`` seat) and its per-user form
    :meth:`moderates_as`. Moderation edits / deletes of others' content,
    a moderated post's re-edit, gallery tombstones, RSVP overrides.

  Both per-user forms share :meth:`_seated_as`: the named user holds the
  seat on the sender, or — from the host, the roster authority — a live
  writer seat on the host (the owner is mirrored as a member) or a
  relayed remote user's live seat of the tier.
* :meth:`may_mutate` — edits / deletes of an owned row: :meth:`acts_for`
  for the row's owner, or a household with **content authority**. That is
  the federated form of the local "author or space moderator" rule
  (``SpaceService.delete_post``, ``edit_comment``,
  ``GalleryService.delete_item`` …); moderation deletes ride the same
  ``*_DELETED`` events, so without it a moderated post would disappear
  everywhere except on the households that did not moderate it.

* :meth:`access_admits` — the space's per-feature access level (§4.3,
  v_42), checked AFTER the rules above by every write to posts, pages,
  tasks / task lists, stickies and calendar events, against the receiver's
  OWN copy of the features: ``OPEN`` admits; any other level first binds a
  named ``actor_user_id`` to the sender (:meth:`acts_for`); ``ADMIN_ONLY``
  then needs that actor to be an admin as the sender records it
  (:meth:`admin_as`) — or, from an older sender that names no actor, the
  sending household to hold settings authority (:meth:`is_admin_household`).
  ``MODERATED`` admits content authority, own rows and layout moves — and a
  plain member's write only as a moderation release.
* :meth:`may_author_approved` — a moderation release (v_43): content an
  approver household applied from the queue, attributed to its submitter,
  carrying the approval block ``moderation: {item_id, approved_by}``. It
  stands in for :meth:`may_author` on a create and for the ``MODERATED``
  rule of :meth:`access_admits`.

The bot bridge posts under the shared :data:`SYSTEM_AUTHOR` identity,
which is no member at all: any writer household may create such a row
(nobody is impersonated), and only content authority may change one.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..domain.space import (
    CONTENT_AUTHORITY_ROLES,
    MODERATION_BLOCK_KEY,
    SETTINGS_AUTHORITY_ROLES,
    WRITER_ROLES,
    ContentAction,
    ModerationApproval,
    ModerationStatus,
    SpaceFeatureAccess,
    SpaceRole,
)
from ..domain.federation_capabilities import FederationCapability
from ..domain.user import SYSTEM_AUTHOR
from .moderation_approval import (
    FEATURE_OF_EVENT,
    item_matches_event,
    needs_held_row,
)

from ..domain.federation import FederationEvent, FederationEventType

if TYPE_CHECKING:
    from ..domain.space import SpaceModerationItem
    from .pending_seat_buffer import PendingSeatBuffer
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

#: ``(space_id, feature, target_id) -> our copy of the row`` (the moderation
#: handler's snapshot), ``None`` when not held.
HeldRows = Callable[[str, str, str], Awaitable["dict | None"]]
#: ``(item, approved_by)`` — a release of a held item was accepted.
ReleaseSeen = Callable[["SpaceModerationItem", str], Awaitable[None]]

#: Seats that write — the same set as the §24.11 follower gate. A remote
#: seat is never ``owner``; the set is :data:`WRITER_ROLES` as strings.
_WRITER_ROLES: frozenset[str] = frozenset(r.value for r in WRITER_ROLES)

#: Settings authority on a remote seat: ``admin`` only (never ``moderator``).
_ADMIN_SEATS: frozenset[str] = frozenset(
    r.value for r in SETTINGS_AUTHORITY_ROLES if r is not SpaceRole.OWNER
)

#: Content authority on a remote seat: ``admin`` or ``moderator``.
_CONTENT_SEATS: frozenset[str] = frozenset(
    r.value for r in CONTENT_AUTHORITY_ROLES if r is not SpaceRole.OWNER
)

#: The statuses of a queue row a release may still land on (v_43). Approve
#: beats reject — a row this household rejected while another approved
#: takes the published content, so every copy converges; an expired or
#: purged row takes nothing.
_RELEASABLE: frozenset[ModerationStatus] = frozenset(
    {ModerationStatus.PENDING, ModerationStatus.APPROVED, ModerationStatus.REJECTED}
)


def payload_actor(event: "FederationEvent") -> str | None:
    """The payload's ``actor_user_id`` (v_42) — the user who made the write
    — or ``None`` from an older sender. For :meth:`SpaceAuthorship.access_admits`."""
    actor = event.payload.get("actor_user_id")
    return actor if isinstance(actor, str) and actor else None


def _quiet_refusal(*_args: object) -> None:
    """The refusal log of a ``quiet`` :meth:`SpaceAuthorship.access_admits`."""


class SpaceAuthorship:
    """Bind the users a space-content payload names to the sending household."""

    __slots__ = (
        "_spaces",
        "_seats",
        "_users",
        "_pending",
        "_instances",
        "_held_rows",
        "_on_release",
    )

    def __init__(
        self,
        *,
        space_repo: "AbstractSpaceRepo",
        remote_member_repo: "AbstractSpaceRemoteMemberRepo",
        user_repo: "AbstractUserRepo",
        pending: "PendingSeatBuffer | None" = None,
        federation_repo: "AbstractFederationRepo | None" = None,
    ) -> None:
        self._spaces = space_repo
        #: The senders' advertised ``proto_version`` — whether a missing
        #: ``actor_user_id`` means an older household (see
        #: :meth:`access_admits`).
        self._instances = federation_repo
        self._seats = remote_member_repo
        self._users = user_repo
        #: Where a write naming a user we hold no row for at all waits for
        #: that user's seat (see :meth:`hold_or_refuse`). ``None`` refuses.
        self._pending = pending
        #: Our own copy of a release's target (the moderation handlers'
        #: snapshot) — an edit's release is checked against it. ``None``
        #: refuses every release of an edit we hold the item of.
        self._held_rows: HeldRows | None = None
        #: Told when a release of an item we hold is accepted (the row moves
        #: to approved before its decision arrives).
        self._on_release: ReleaseSeen | None = None

    def attach_moderation(
        self, *, held_rows: "HeldRows", on_release: "ReleaseSeen"
    ) -> None:
        """Wire the moderation queue's read of our rows and its release
        hook (``app._build_space_moderation``)."""
        self._held_rows = held_rows
        self._on_release = on_release

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

    async def item_access_admits(
        self,
        *,
        origin_instance_id: str,
        space_id: str,
        feature: str,
        author_user_id: str,
    ) -> bool:
        """Does ``feature``'s access level, as THIS household holds it, admit
        a NEW item by ``author_user_id`` published by household
        ``origin_instance_id`` over the GFS (v_49 ``space_item``)?

        The member-publish counterpart of :meth:`access_admits` for a write
        that arrives with no federation envelope: the author must hold a
        live writer seat on ``origin_instance_id``; ``ADMIN_ONLY`` needs an
        admin seat, ``MODERATED`` content authority — a plain member's post
        under review is never member-published (it waits in the host's
        queue). Unlike :meth:`access_admits` it never holds the item for a
        trailing seat: the same post also arrives over federation, where the
        full rule (and the hold) applies. Defence in depth behind the writer
        cert, whose scope and user binding already encode this."""
        event = FederationEvent(
            msg_id=f"space-item:{space_id}",
            event_type=FederationEventType.SPACE_POST_CREATED,
            from_instance=origin_instance_id,
            to_instance="",
            timestamp="",
            payload={},
            space_id=space_id,
        )
        space = await self._spaces.get(space_id)
        if space is None or not await self.acts_for(event, space_id, author_user_id):
            return False
        level = space.features.access_level(feature)
        if level is SpaceFeatureAccess.OPEN:
            return True
        if level is SpaceFeatureAccess.ADMIN_ONLY:
            return await self.admin_as(event, space_id, author_user_id)
        return await self.moderates_as(event, space_id, author_user_id)

    async def is_host(self, event: "FederationEvent", space_id: str) -> bool:
        """The sender is the household hosting ``space_id``."""
        sender = str(event.from_instance or "")
        if not sender or not space_id:
            return False
        space = await self._spaces.get(space_id)
        return space is not None and space.owner_instance_id == sender

    async def is_admin_household(self, event: "FederationEvent", space_id: str) -> bool:
        """The host, or a household holding a live ``admin`` seat here.

        Settings authority (zones, timetables): a ``moderator`` seat does
        NOT count — see :meth:`has_content_authority`.
        """
        return await self._sender_holds(event, space_id, _ADMIN_SEATS)

    async def has_content_authority(
        self, event: "FederationEvent", space_id: str
    ) -> bool:
        """The host, or a household holding a live ``admin`` or
        ``moderator`` seat here — may act on other people's content."""
        return await self._sender_holds(event, space_id, _CONTENT_SEATS)

    async def _sender_holds(
        self,
        event: "FederationEvent",
        space_id: str,
        roles: frozenset[str],
    ) -> bool:
        if await self.is_host(event, space_id):
            return True
        sender = str(event.from_instance or "")
        if not sender:
            return False
        seats = await self._seats.list_for_instance(
            space_id, sender, include_tombstoned=False
        )
        return any(s.role in roles for s in seats)

    async def admin_as(
        self,
        event: "FederationEvent",
        space_id: str,
        user_id: str,
    ) -> bool:
        """May the sender record an admin-only write as ``user_id``?

        The per-user form of :meth:`is_admin_household`, for content only a
        space owner / admin may change (timetables). A ``moderator`` seat
        never passes. See :meth:`_seated_as` for the rules.
        """
        return await self._seated_as(event, space_id, user_id, _ADMIN_SEATS)

    async def moderates_as(
        self,
        event: "FederationEvent",
        space_id: str,
        user_id: str,
    ) -> bool:
        """May the sender record a content-authority write as ``user_id``?

        The per-user form of :meth:`has_content_authority`: like
        :meth:`admin_as`, but a live ``moderator`` seat passes too.
        """
        return await self._seated_as(event, space_id, user_id, _CONTENT_SEATS)

    async def _seated_as(
        self,
        event: "FederationEvent",
        space_id: str,
        user_id: str,
        roles: frozenset[str],
    ) -> bool:
        """``user_id`` holds one of ``roles`` as the sender records it.

        * sent by a **non-host** household: ``user_id`` holds a live seat
          in ``roles`` on that household — an admin household cannot pass
          off its plain member's edit, and nobody names another household's
          admin / moderator;
        * sent by the **host**: ``user_id`` holds a live writer seat on the
          host, or the host relays a remote user whose own live seat is in
          ``roles``.

        Why a host-seated *member* passes: the roster wire mirrors the
        space's owner as a plain ``member`` seat (a remote seat has no owner
        role), and member households — mesh-only and invite-link ones above
        all — hold no other record of who the owner is, so demanding an
        admin seat would refuse the owner's (the teacher's) every live edit
        there. It costs nothing: the host is the roster authority and could
        authority-sign any of its users into an ``admin`` seat anyway, and an
        honest host never emits a plain member's edit — its local guards
        refuse one.

        Followers, removed (tombstoned) seats, banned, blank and local
        users, and the shared bot identity, hold nothing.
        """
        if not user_id or user_id == SYSTEM_AUTHOR or not space_id:
            return False
        if await self._spaces.is_banned(space_id, user_id):
            return False
        if await self._is_local_user(user_id):
            return False
        sender = str(event.from_instance or "")
        if not sender:
            return False
        seat = await self._seats.get(space_id, sender, user_id)
        is_host = await self.is_host(event, space_id)
        if seat is not None:
            if seat.role in roles:
                return True
            return is_host and seat.role in _WRITER_ROLES
        if not is_host:
            return False
        # The host relaying a remote user: a live seat in ``roles`` on the
        # user's own household (keyed on (space, user) — a user has one
        # household).
        row = await self._seats.get_including_tombstones(space_id, "", user_id)
        return (
            row is not None
            and not row.tombstoned
            and row.instance_id != sender
            and row.role in roles
        )

    async def approver_holds(
        self,
        event: "FederationEvent",
        space_id: str,
        user_id: str,
        *,
        admin: bool = False,
    ) -> bool:
        """Is ``user_id`` an approver (content authority; ``admin`` →
        settings authority) the sender may name on a moderation decision or
        release? :meth:`moderates_as` / :meth:`admin_as` — and, from the
        HOST only, one of OUR users, by the role our own roster gives them
        (the host publishes what our moderator approved, and tells us)."""
        if await self._is_local_user(user_id):
            if not await self.is_host(event, space_id):
                return False
            if await self._spaces.is_banned(space_id, user_id):
                return False
            member = await self._spaces.get_member(space_id, user_id)
            wanted = SETTINGS_AUTHORITY_ROLES if admin else CONTENT_AUTHORITY_ROLES
            return member is not None and str(member.role) in {r.value for r in wanted}
        if admin:
            return await self.admin_as(event, space_id, user_id)
        return await self.moderates_as(event, space_id, user_id)

    async def writes_here(self, event: "FederationEvent", space_id: str) -> bool:
        """The host, or a household holding a live writer seat (``member`` /
        ``moderator`` / ``admin``).

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
        if MODERATION_BLOCK_KEY in event.payload:
            # Content released from the moderation queue (v_43): judged by
            # the release alone — the only way any household authors a row
            # for one of OUR users, and then only for an item we hold.
            return await self.may_author_approved(event, space_id, user_id)
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

    async def may_author_approved(
        self,
        event: "FederationEvent",
        space_id: str,
        author: str,
    ) -> bool:
        """Is ``event`` a valid moderation release of ``author``'s item (v_43)?

        The approval block ``moderation: {item_id, approved_by}`` is a claim
        by the sender; every part of it is re-derived from what this
        household already holds:

        * the event is a reviewable content write (:data:`FEATURE_OF_EVENT`);
        * the sender has **content authority** (the host, or a live
          ``admin`` / ``moderator`` seat) and records the release as
          ``approved_by``, a content-authority user seated on it
          (:meth:`moderates_as` — live seats, so a demoted moderator fails);
          under ``ADMIN_ONLY`` the approver must be an admin (:meth:`admin_as`);
        * ``author`` still holds a live writer seat in the space and is not
          banned — their local seat when they are one of ours, else their
          mirrored seat on their own household;
        * when ``author`` is one of OUR users, we hold the very item: same
          id, space and submitter, the same feature / kind of write /
          target, the same content (:func:`item_matches_event`), still
          releasable — no household can author as our people otherwise. Any
          household holding the item checks it the same way.

        Refusals log at WARNING.
        """
        reason, item = await self._release_refusal(event, space_id, author)
        if reason is None:
            if item is not None and self._on_release is not None:
                approval = ModerationApproval.from_wire(
                    event.payload.get(MODERATION_BLOCK_KEY)
                )
                if approval is not None:
                    await self._on_release(item, approval.approved_by)
            return True
        log.warning(
            "%s from %s: release of %r's content in space %s refused — %s",
            getattr(event, "event_type", "?"),
            getattr(event, "from_instance", "?"),
            author,
            space_id,
            reason,
        )
        return False

    async def _release_refusal(
        self, event: "FederationEvent", space_id: str, author: str
    ) -> tuple[str | None, "SpaceModerationItem | None"]:
        """``(reason, item)``: why the release is refused (``None``: it is
        not), and the held queue item it releases, if any."""
        approval = ModerationApproval.from_wire(event.payload.get(MODERATION_BLOCK_KEY))
        if approval is None:
            return "no well-formed approval block", None
        feature = FEATURE_OF_EVENT.get(event.event_type)
        if feature is None:
            return "not a reviewable content write", None
        if not author or author == SYSTEM_AUTHOR:
            return "no author to release for", None
        space = await self._spaces.get(space_id)
        if space is None:
            return "unknown space", None
        # Only the host applies a queue item — from its own stored copy —
        # so only the host can send its release (I2: a moderator household
        # cannot make one up). The host is the roster authority already.
        if not await self.is_host(event, space_id):
            return "only the space's host releases a queue item", None
        if not await self.approver_holds(event, space_id, approval.approved_by):
            return f"{approval.approved_by!r} holds no content-authority seat", None
        if space.features.access_level(
            feature
        ) is SpaceFeatureAccess.ADMIN_ONLY and not await self.approver_holds(
            event, space_id, approval.approved_by, admin=True
        ):
            return f"{feature} is admin-only and the approver is no admin", None
        if await self._spaces.is_banned(space_id, author):
            return "the author is banned", None
        local = await self._is_local_user(author)
        if local:
            member = await self._spaces.get_member(space_id, author)
            if member is None or str(member.role) not in _WRITER_ROLES:
                return "the author holds no writer seat here", None
        else:
            seat = await self._seats.get_including_tombstones(space_id, "", author)
            if seat is None or seat.tombstoned or seat.role not in _WRITER_ROLES:
                return "the author holds no live writer seat", None
        item = await self._spaces.get_moderation_item(approval.item_id)
        if item is not None and not item.feature:
            item = None  # a decision tombstone, not a copy of the item
        if item is None:
            if local:
                return "one of our users, and we hold no such item", None
            return None, None
        held: dict | None = None
        if needs_held_row(item):
            target = str((item.payload or {}).get("target_id") or "")
            held = (
                await self._held_rows(space_id, item.feature, target)
                if self._held_rows is not None and target
                else None
            )
        if (
            item.space_id != space_id
            or item.submitted_by != author
            or item.status not in _RELEASABLE
            or not item_matches_event(item, event.event_type, event.payload, held=held)
        ):
            return "it does not match the item held here", None
        return None, item

    async def may_mutate(
        self,
        event: "FederationEvent",
        space_id: str,
        owner_user_id: str,
        *,
        settings: bool = False,
    ) -> bool:
        """May the sender edit / delete a row owned by ``owner_user_id``?

        The row's owner, or content authority (host / admin / moderator).
        ``settings=True`` raises the bar for a non-owner to settings
        authority (host / admin) — a whole gallery album, which the local
        service also keeps from a moderator.
        """
        if owner_user_id != SYSTEM_AUTHOR and await self.acts_for(
            event, space_id, owner_user_id, any_role=True
        ):
            return True
        if settings:
            return await self.is_admin_household(event, space_id)
        return await self.has_content_authority(event, space_id)

    async def access_admits(
        self,
        event: "FederationEvent",
        space_id: str,
        feature: str,
        action: ContentAction,
        *,
        actor: str | None,
        row_owner: str = "",
        release_ok: bool = False,
        quiet: bool = False,
    ) -> bool:
        """Does ``space_id``'s ``feature`` access level, as THIS household
        holds it, admit the write?

        Called after the event family's authorship rule. ``actor`` is the
        payload's ``actor_user_id`` (v_42) — the user who made the write.
        ``row_owner`` is the row's creator: for a ``CREATE`` the claimed
        author / ``created_by`` (already bound to the sender by
        :meth:`may_author`), for an edit / delete the held row's.

        * ``OPEN`` → admitted.
        * A ``CREATE``'s actor **is** its author: a named actor that differs
          is refused — an admin household cannot pass its plain member's row
          off as its admin's. The one exception (``release_ok``) is a v_42
          host's moderation release, which named the approver; a v_43
          release names the author and carries the approval block. The host relaying a remote member's row (resume replay)
          is admitted, as :meth:`may_author` admits it.
        * A named actor must hold a live writer seat on the sender
          (:meth:`acts_for`; any live seat for an edit / delete of their own
          row); one this space has no record of yet is held for its seat.
          The shared bot identity stands for its household instead.
        * ``ADMIN_ONLY`` → the actor is an admin as the sender records it
          (:meth:`admin_as`; the host's own people via their mirrored seat).
          An edit / delete naming no actor is refused from a v_42 sender —
          every v_42 producer names one; from an older sender (or one that
          never advertised) the sending household must hold settings
          authority (:meth:`is_admin_household`), as it must for the shared
          bot identity — and that only on a bot's own row. A moderator never
          passes.
        * ``MODERATED`` → content authority (:meth:`moderates_as`; an
          actor-less older sender: :meth:`has_content_authority`), an edit /
          delete of the actor's own row, and a layout move. A plain member's
          create or edit of someone else's row waits for review, so it is
          admitted only as a release (below) — posts included, whatever
          version the sender advertises.
        * A payload carrying an approval block (v_43) is judged by the
          release alone (:meth:`may_author_approved`), at every level but
          ``OPEN``.

        A refusal logs at WARNING — unless ``quiet``, for a caller that
        reports its refusals itself (once per §25.6 sync chunk, rather than
        once per record on every scheduler tick). An unknown space admits
        nothing beyond ``OPEN``: there is no level to check against.
        """
        refuse = _quiet_refusal if quiet else self._log_access_refusal
        space = await self._spaces.get(space_id)
        if space is None:
            refuse(event, space_id, feature, action, actor, "unknown")
            return False
        level = space.features.access_level(feature)
        if level is SpaceFeatureAccess.OPEN:
            return True
        if MODERATION_BLOCK_KEY in event.payload:
            return await self._release_admitted(
                event, space_id, feature, action, actor=actor, row_owner=row_owner
            )
        if action is ContentAction.CREATE and row_owner:
            if actor and actor != row_owner:
                if not (release_ok and await self.is_host(event, space_id)):
                    refuse(
                        event,
                        space_id,
                        feature,
                        action,
                        actor,
                        "actor is not the author",
                    )
                    return False
            else:
                actor = row_owner
                if (
                    actor != SYSTEM_AUTHOR
                    and await self.is_host(event, space_id)
                    and not await self.acts_for(event, space_id, actor)
                    and await self.may_author(event, space_id, actor)
                ):
                    # The host relaying a remote member's row.
                    return True
        named = bool(actor) and actor != SYSTEM_AUTHOR
        # An edit / delete of one's OWN row may come from a read-only seat
        # (a demoted author), as ``may_mutate`` allows; anything else needs
        # a writer seat on the sender.
        own_row = (
            named
            and actor == row_owner
            and action in (ContentAction.EDIT, ContentAction.DELETE)
        )
        if named and not await self.acts_for(
            event, space_id, str(actor), any_role=own_row
        ):
            # Unknown here at all → the gossip seating them may trail the
            # write: hold it until the seat lands (else refuse, logged).
            await self.hold_or_refuse(
                event,
                space_id=space_id,
                what=f"{feature} {action.value}",
                row_id="",
                user_id=str(actor),
            )
            return False
        if level is SpaceFeatureAccess.ADMIN_ONLY:
            if named:
                admitted = await self.admin_as(event, space_id, str(actor))
            elif actor == SYSTEM_AUTHOR:
                # The bot identity stands for its household on a BOT's own
                # row only (an admin-configured bot's post); as the actor of
                # an edit / delete of anybody else's row it is nobody.
                admitted = row_owner == SYSTEM_AUTHOR and await self.is_admin_household(
                    event, space_id
                )
            elif not await self._sender_names_actors(event):
                admitted = await self.is_admin_household(event, space_id)
            else:
                admitted = False  # a v_42 sender that named nobody
            if not admitted:
                refuse(event, space_id, feature, action, actor, level)
            return admitted
        # MODERATED. A plain member's create, or edit / delete of someone
        # else's row, is exactly a write that waits for review — every
        # household submits it to the reviewers (v_43) and only its release
        # (the approval block, above) publishes it. Sent straight on, it is
        # refused, fail closed, on every receiver alike: a modified stub
        # cannot publish past review. Own edits / deletes and layout moves
        # proceed, as locally. Posts follow the same rule, whatever version
        # the sender advertises (``PEERS_TOO_OLD`` names an older household
        # when the level is set).
        if action is ContentAction.LAYOUT or own_row:
            return True
        if named:
            admitted = await self.moderates_as(event, space_id, str(actor))
        elif actor == SYSTEM_AUTHOR:
            # A bot posts (never writes the other features): its household
            # must hold content authority — a member's personal bot is
            # refused under review at its source (``BOT_POSTS_REVIEWED``).
            admitted = (
                feature == "posts"
                and row_owner == SYSTEM_AUTHOR
                and await self.has_content_authority(event, space_id)
            )
        elif not await self._sender_names_actors(event):
            admitted = await self.has_content_authority(event, space_id)
        else:
            admitted = False  # a v_42 sender that named nobody
        if not admitted:
            refuse(event, space_id, feature, action, actor, level)
        return admitted

    async def _release_admitted(
        self,
        event: "FederationEvent",
        space_id: str,
        feature: str,
        action: ContentAction,
        *,
        actor: str | None,
        row_owner: str,
    ) -> bool:
        """A write carrying an approval block (v_43) is judged by the release
        alone: the author it releases for is the create's author (the
        payload may name them, or — from a v_42-style producer — the
        approver, as the actor), or the actor of an edit / delete.
        :meth:`may_author_approved` binds everything else."""
        approval = ModerationApproval.from_wire(event.payload.get(MODERATION_BLOCK_KEY))
        if action is ContentAction.CREATE:
            author = row_owner
            allowed_actors = {None, "", row_owner}
            if approval is not None:
                allowed_actors.add(approval.approved_by)
            if actor not in allowed_actors:
                self._log_access_refusal(
                    event, space_id, feature, action, actor, "actor is not the author"
                )
                return False
        else:
            author = str(actor or "")
        if not await self.may_author_approved(event, space_id, author):
            self._log_access_refusal(
                event,
                space_id,
                feature,
                action,
                actor,
                "invalid release",
            )
            return False
        return True

    async def _sender_names_actors(self, event: "FederationEvent") -> bool:
        """The sender advertised v_42+ — it names ``actor_user_id`` on every
        collaborative write. Without a federation repo, assume it does
        (strict). A peer that never advertised reads as old."""
        return await self._sender_supports(
            event, FederationCapability.MIN_FOR_CONTENT_ACCESS_ENFORCEMENT
        )

    async def _sender_supports(self, event: "FederationEvent", version: int) -> bool:
        """The sender advertised at least ``version``. Without a federation
        repo, assume it did (strict). A peer that never advertised reads as
        old."""
        if self._instances is None:
            return True
        try:
            peer = await self._instances.get_instance(str(event.from_instance or ""))
        except Exception:  # pragma: no cover — defensive
            return True
        return peer is not None and peer.proto_version >= version

    @staticmethod
    def _log_access_refusal(
        event: "FederationEvent",
        space_id: str,
        feature: str,
        action: ContentAction,
        actor: str | None,
        level: object,
    ) -> None:
        log.warning(
            "%s from %s: %s %s in space %s by %r — refused by the space's "
            "%s access level (%s)",
            getattr(event, "event_type", "?"),
            getattr(event, "from_instance", "?"),
            feature,
            action.value,
            space_id,
            actor or None,
            feature,
            getattr(level, "value", level),
        )

    async def hold_or_refuse(
        self,
        event: "FederationEvent",
        *,
        space_id: str,
        what: str,
        row_id: str,
        user_id: str,
    ) -> bool:
        """Refuse a write naming ``user_id`` — or, when this space has no
        record of that user at all (the roster gossip seating them has not
        reached us yet), hold it until their seat lands. ``True`` when it
        was held (it is replayed later — not a refusal to answer).

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
            return True
        self.log_refusal(
            event, space_id=space_id, what=what, row_id=row_id, user_id=user_id
        )
        return False

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
