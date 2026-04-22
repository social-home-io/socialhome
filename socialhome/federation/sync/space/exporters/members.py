"""Members exporter — ``space_members`` rows."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.space_repo import AbstractSpaceRepo


class MembersExporter:
    resource = "members"

    __slots__ = ("_repo",)

    def __init__(self, space_repo: "AbstractSpaceRepo") -> None:
        self._repo = space_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        members = await self._repo.list_members(space_id)
        return [asdict(m) for m in members]
