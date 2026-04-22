"""Bans exporter — ``space_bans`` rows via :class:`AbstractSpaceRepo.list_bans`."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.space_repo import AbstractSpaceRepo


class BansExporter:
    resource = "bans"

    __slots__ = ("_repo",)

    def __init__(self, space_repo: "AbstractSpaceRepo") -> None:
        self._repo = space_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        # ``list_bans`` already returns plain dicts.
        return list(await self._repo.list_bans(space_id))
