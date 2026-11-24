"""Shared space membership / role-guard primitives.

Space-scoped services (spaces, zones, bots, …) all gate writes on the
same two reads:

1. **Is the caller a member of this space?** — ``space_repo.get_member``,
   raise :class:`SpacePermissionError` if not.
2. **Does the caller hold a required role?** — same lookup, then compare
   ``member.role`` against an allowed set (``OWNER``/``ADMIN``/…).

Many call sites address the actor by *username* and must first resolve
it to a user id via the user repo (raising ``KeyError`` for an unknown
user). These three primitives — ``_member_or_raise``, ``_role_or_raise``,
``_actor_or_raise`` — are the recurring kernels; this mixin owns them so
each service's public guard methods (``_require_member`` /
``_require_admin_or_owner`` / …) become thin one-liners that keep their
own names, signatures, and error messages.

Behaviour-only mixin (``__slots__ = ()``): it reads the ``_spaces`` and
``_users`` slots the consuming service already declares, so it composes
with other mixins (e.g. ``BusPublisherMixin`` on ``SpaceZoneService``).
See :mod:`socialhome.services.bus_publisher` for why composable mixins
carry no slots.

:func:`require_seat` is the read gate (any seat, followers included,
403 :data:`NOT_A_SPACE_MEMBER` otherwise) as a free function, for services
that hold the space repo under another slot name (themes, approvals).
:func:`require_member_seat` is its narrower sibling for the reads that are
about the space's *people* (proposals, map pins, zones): a follower is an
outsider there and gets the same 403.

:func:`user_id_proven` is the person-ID anchor proof every path that seats
a person another household names checks first (P5);
:func:`user_id_of_another_household` is the receivers' rule for trusting a
seat the host seated.

Compare roles against the :class:`SpaceRole` enum (never bare strings) —
the caller passes the allowed set; this mixin only does the membership
read and the ``in`` check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..crypto import derive_user_id
from ..domain.errors import NotFoundError
from ..domain.space import WRITER_ROLES, SpacePermissionError

if TYPE_CHECKING:
    from collections.abc import Collection

    from ..domain.space import SpaceMember, SpaceRole
    from ..domain.user import User
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo


class SpaceMemberGuardMixin:
    """Mixin: membership/role guards over the consuming service's repos.

    The consuming service declares ``_spaces`` and ``_users`` in its own
    ``__slots__``; this mixin contributes only the shared guard
    primitives, so it owns no slots.
    """

    __slots__ = ()

    _spaces: AbstractSpaceRepo
    _users: AbstractUserRepo

    async def _member_or_raise(self, space_id: str, user_id: str) -> SpaceMember:
        """Return the caller's membership row, or raise if they aren't a member."""
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        return member

    async def _role_or_raise(
        self,
        space_id: str,
        user_id: str,
        roles: Collection[SpaceRole],
        *,
        message: str,
    ) -> SpaceMember:
        """Return the membership row iff its role is in ``roles``; else raise.

        Raises :class:`SpacePermissionError` with ``message`` when the
        caller isn't a member or holds a role outside ``roles``.
        """
        member = await self._spaces.get_member(space_id, user_id)
        if member is None or member.role not in roles:
            raise SpacePermissionError(message)
        return member

    async def _actor_or_raise(self, username: str, *, label: str = "actor") -> User:
        """Resolve ``username`` to a :class:`User`, or raise ``KeyError``.

        ``label`` tunes the error noun so call sites keep their existing
        message wording (``"actor"`` vs ``"user"``).
        """
        actor = await self._users.get(username)
        if actor is None:
            raise NotFoundError(f"{label} {username!r} not found")
        return actor


#: The 403 detail every space read gate answers — the same text the
#: route-level gates (calendar, tasks, pages…) return.
NOT_A_SPACE_MEMBER = "Not a space member."


async def seat_of(
    space_repo: AbstractSpaceRepo, space_id: str, user_id: str
) -> SpaceMember | None:
    """The caller's seat in ``space_id`` — any role — or ``None``: no
    seat, or a seat row of a **banned** user (banning drops the seat in
    the same transaction, but a seat that survived a ban — an older
    half-applied ban, a federation race — never counts)."""
    member = await space_repo.get_member(space_id, user_id)
    if member is None or await space_repo.is_banned(space_id, user_id):
        return None
    return member


async def require_seat(
    space_repo: AbstractSpaceRepo, space_id: str, user_id: str
) -> SpaceMember:
    """The read gate for a space's content: the caller's seat — any role,
    a read-only follower (``subscriber``) included — or
    :class:`SpacePermissionError` (403 ``Not a space member.``).

    A free function, not a mixin method, so services that hold the space
    repo under another slot name (themes, approvals) share it too. It
    never looks at the ``spaces`` row, so an unknown id answers the same
    403 as a space the caller isn't in — no existence oracle.

    A **banned** user is refused too, even with a seat row
    (:func:`seat_of`) — the same 403, so it reveals nothing more."""
    member = await seat_of(space_repo, space_id, user_id)
    if member is None:
        raise SpacePermissionError(NOT_A_SPACE_MEMBER)
    return member


async def require_member_seat(
    space_repo: AbstractSpaceRepo, space_id: str, user_id: str
) -> SpaceMember:
    """:func:`require_seat` minus followers: the caller's seat when it is
    a member's (owner / admin / moderator / member), else the same 403.

    For the reads about the space's people rather than its posts —
    governance (proposals) and whereabouts (map pins, zones). A follower
    reads the posts but is an outsider to the people (owner decision 13:
    the chat refuses them, and a follower-only household receives neither
    the chat nor the members' whereabouts nor the proposals), so they get
    the outsider's answer, not a distinct one."""
    member = await require_seat(space_repo, space_id, user_id)
    if member.role not in WRITER_ROLES:
        raise SpacePermissionError(NOT_A_SPACE_MEMBER)
    return member


def user_id_proven(identity_pk: bytes | None, anchor: object, user_id: str) -> bool:
    """Whether the household holding ``identity_pk`` proves ``user_id`` is
    one of its people: ``derive_user_id(identity_pk, anchor) == user_id`` —
    the construction every household mints its users with (``anchor`` is
    the person's ``identity_anchor or username``).

    The one gate on every path that SEATS a person another household names
    (an invite-link redeem — paired, mesh or §D2b bootstrap —, a private
    invitation's accept, a follow approval). ``identity_pk`` is the key the
    receiver already pins for the sending household (its
    ``remote_instances`` row, a mesh claim deriving to the sender, or a
    §D2b body's key bound to its instance id), so a household can only ever
    seat ids derived from its own key: never somebody else's, never one of
    OUR people (theirs derive from our key). Fail closed: a missing key or
    anchor, a non-string anchor, or any derivation error is ``False``.

    Callers check it before any ban / seat / roster / token read and answer
    a failure exactly like their other refusals, so the answer depends only
    on the requester's own anchor (no probing). Never log the anchor."""
    if not identity_pk or not isinstance(anchor, str) or not anchor or not user_id:
        return False
    try:
        return derive_user_id(identity_pk, anchor) == user_id
    except ValueError:
        return False


async def user_id_of_another_household(
    *,
    seats: AbstractSpaceRemoteMemberRepo,
    users: AbstractUserRepo,
    space_id: str,
    instance_id: str,
    user_id: str,
) -> bool:
    """Whether a seat household ``instance_id`` holds under ``user_id`` in
    ``space_id`` is NOT proof of acting for that person: ``user_id`` is one
    of OUR people, or holds a LIVE seat there under another household.

    The receivers' rule (``SpaceAuthorship.acts_for`` and the member-publish
    seat checks). Receivers see no anchors: they trust the seats the host
    seated (authority-signed roster gossip), and since P5 a host seats only
    ids the seated household proves (:func:`user_id_proven`). So only a
    legacy seat a pre-proof host gave can put one id under two households;
    a tombstone does not count, so an admin removing such a seat clears it
    and nobody is locked out for good. The local lookup is a primary-key
    read; the roster read filters ``(space_id, user_id)`` on the primary
    key's ``space_id`` prefix (≈ 10 000 index rows for a 10 000-follower
    space), run only after the sender's seat was found."""
    if await users.get_by_user_id(user_id) is not None:
        return True
    return bool(
        await seats.list_live_instances_of_user(space_id, user_id) - {instance_id}
    )
