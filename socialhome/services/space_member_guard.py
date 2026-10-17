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

Compare roles against the :class:`SpaceRole` enum (never bare strings) —
the caller passes the allowed set; this mixin only does the membership
read and the ``in`` check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.space import SpacePermissionError

if TYPE_CHECKING:
    from collections.abc import Collection

    from ..domain.space import SpaceMember, SpaceRole
    from ..domain.user import User
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
            raise KeyError(f"{label} {username!r} not found")
        return actor
