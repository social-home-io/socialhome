"""Which households of a space are *member* households.

:class:`SpaceWriterHouseholds` narrows a space fan-out (``only_instances``
of :meth:`FederationService.broadcast_to_space_members`) and a §25.6
catch-up resource to the households holding a writer seat, so member-only
data never reaches a household that only follows the space.
"""

from __future__ import annotations

from ..domain.space import WRITER_ROLES
from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo

_WRITER_ROLE_VALUES: frozenset[str] = frozenset(r.value for r in WRITER_ROLES)


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
    """

    __slots__ = ("_own_instance_id", "_seats")

    def __init__(
        self,
        remote_member_repo: AbstractSpaceRemoteMemberRepo,
        *,
        own_instance_id: str,
    ) -> None:
        self._seats = remote_member_repo
        self._own_instance_id = own_instance_id

    async def writer_households(self, space_id: str) -> frozenset[str]:
        """Every other household holding a live writer seat in ``space_id``."""
        ids = await self._seats.list_instances_with_roles(space_id, _WRITER_ROLE_VALUES)
        return frozenset(i for i in ids if i and i != self._own_instance_id)

    async def may_receive(self, space_id: str, instance_id: str) -> bool:
        """Whether household ``instance_id`` holds a member's seat in
        ``space_id`` (may receive its member-only data)."""
        return instance_id in await self.writer_households(space_id)
