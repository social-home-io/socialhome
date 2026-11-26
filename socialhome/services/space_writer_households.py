"""Which households of a space hold a seat — a *member* (writer) seat, or
any seat.

:class:`SpaceWriterHouseholds` narrows a space fan-out (``only_instances``
of :meth:`FederationService.broadcast_to_space_members`) and a §25.6
catch-up resource to the households holding a writer seat, so member-only
data never reaches a household that only follows the space. Built with
``roles=SEAT_ROLES`` it answers for EVERY seat, a follower's included —
what the transport needs (the member directory's keys and mailbox
addresses): a follower is a member with the read-only role on every path
(owner 2026-11-23), so how a household is reached never depends on its
role; only what it may read or write does.
"""

from __future__ import annotations

from ..domain.space import WRITER_ROLES, SpaceRole
from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
from ..repositories.space_repo import AbstractSpaceRepo

#: Every seat of a space, a follower's (``subscriber``) included.
SEAT_ROLES: frozenset[SpaceRole] = frozenset(SpaceRole)


class SpaceWriterHouseholds:
    """The member households of a space: those holding at least one live
    WRITER seat (owner / admin / moderator / member), by this household's
    roster mirror — never a household whose people only follow it.

    One rule for everything only the space's *members* may receive, live
    and by §25.6 catch-up: the chat (v_55), the members' whereabouts
    (``SPACE_LOCATION_UPDATED``, zones) and governance (proposal mirrors).
    A follower-only household is a reader of the posts, not of the people
    (owner decision 13). A household with no live roster row yet is left
    out — fail closed; catch-up fills it in later.

    *roles* widens the set: ``SEAT_ROLES`` counts every live seat (the
    member directory, which carries keys and addresses, not member-only
    data).
    """

    __slots__ = ("_own_instance_id", "_roles", "_seats")

    def __init__(
        self,
        remote_member_repo: AbstractSpaceRemoteMemberRepo,
        *,
        own_instance_id: str,
        roles: frozenset[SpaceRole] = WRITER_ROLES,
    ) -> None:
        self._seats = remote_member_repo
        self._own_instance_id = own_instance_id
        self._roles = frozenset(r.value for r in roles)

    async def households(self, space_id: str) -> frozenset[str]:
        """Every other household holding a live seat of our roles (writer
        seats by default) in ``space_id``."""
        ids = await self._seats.list_instances_with_roles(space_id, self._roles)
        return frozenset(i for i in ids if i and i != self._own_instance_id)

    async def may_receive(self, space_id: str, instance_id: str) -> bool:
        """Whether household ``instance_id`` holds a seat of our roles (a
        member's seat by default) in ``space_id``."""
        return instance_id in await self.households(space_id)


async def holds_member_seat(
    space_repo: AbstractSpaceRepo,
    space_id: str,
    *,
    roles: frozenset[SpaceRole] = WRITER_ROLES,
) -> bool:
    """Does THIS household hold a member's seat in ``space_id`` — a local
    user with a writer role (any seat with ``roles=SEAT_ROLES``)? The
    receiving half of the rule above: a follower-only household drops
    member-only data an older sender still pushes it (pins, zones, proposal
    mirrors) instead of storing it."""
    local = set(await space_repo.list_local_member_user_ids(space_id))
    return any(
        m.user_id in local and m.role in roles
        for m in await space_repo.list_members(space_id)
    )
