"""Members exporter — ``space_members`` rows."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TYPE_CHECKING

from .....domain.space import SpaceRole

if TYPE_CHECKING:
    from .....repositories.space_repo import AbstractSpaceRepo
    from ..exporter import ResourceExporter


class MembersExporter:
    resource = "members"

    __slots__ = ("_repo",)

    def __init__(self, space_repo: "AbstractSpaceRepo") -> None:
        self._repo = space_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        members = await self._repo.list_members(space_id)
        return [asdict(m) for m in members]


class PreModeratorMembersExporter:
    """The members stream for a requester below v_41.

    Such a receiver writes each record into its own ``space_members``,
    whose role CHECK predates ``moderator`` — one moderator row would raise
    and lose the rest of the chunk. So a moderator ships as ``member``: the
    role it knows that grants no more.
    """

    __slots__ = ("_inner",)

    resource = "members"

    def __init__(self, inner: "ResourceExporter") -> None:
        self._inner = inner

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        return [
            {**r, "role": SpaceRole.MEMBER.value}
            if r.get("role") == SpaceRole.MODERATOR.value
            else r
            for r in await self._inner.list_records(space_id)
        ]
