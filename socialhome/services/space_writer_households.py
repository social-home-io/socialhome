"""Which households of a space hold a seat — any seat, or a writer seat.

:class:`SpaceWriterHouseholds` narrows a space fan-out (``only_instances``
of :meth:`FederationService.broadcast_to_space_members`) and a §25.6
catch-up resource to the households holding a seat of the given roles.
Since P10 (owner 2026-11-26: "a follower is a limited member but still a
member") every read audience is ``roles=SEAT_ROLES`` — chat, the members'
whereabouts, zones, proposals, app-table cards, sync pages: a follower
reads everything a member does; only its writes are gated (the reader
cert, the switches, the writer seat checks). The writer default remains
for the callers that pick writers on purpose.
"""

from __future__ import annotations

from ..domain.space import WRITER_ROLES, SpaceRole
from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
from ..repositories.space_repo import AbstractSpaceRepo

#: Every seat of a space, a follower's (``subscriber``) included.
SEAT_ROLES: frozenset[SpaceRole] = frozenset(SpaceRole)


class SpaceWriterHouseholds:
    """The households of a space holding at least one live seat of
    *roles* (writer seats by default; ``SEAT_ROLES`` — every seat, a
    follower's included — for every read audience since P10), by this
    household's roster mirror. A household with no live roster row yet is
    left out — fail closed; catch-up fills it in later.
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
    roles: frozenset[SpaceRole] = SEAT_ROLES,
) -> bool:
    """Does THIS household hold a seat in ``space_id`` — a local user of
    the space, a follower included (owner 2026-11-26, P10: a follower is a
    limited member but still a member, and reads everything a member
    does)? The receiving half of every space fan-out: a household with
    nobody of the space here keeps none of its data. ``roles`` narrows it
    (``WRITER_ROLES``: a local writer)."""
    local = set(await space_repo.list_local_member_user_ids(space_id))
    return any(
        m.user_id in local and m.role in roles
        for m in await space_repo.list_members(space_id)
    )
