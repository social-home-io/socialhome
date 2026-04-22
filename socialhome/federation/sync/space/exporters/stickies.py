"""Stickies exporter — ``stickies`` rows scoped to a space."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.sticky_repo import AbstractStickyRepo


class StickiesExporter:
    resource = "stickies"

    __slots__ = ("_repo",)

    def __init__(self, sticky_repo: "AbstractStickyRepo") -> None:
        self._repo = sticky_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        stickies = await self._repo.list(space_id=space_id)
        return [asdict(s) for s in stickies]
