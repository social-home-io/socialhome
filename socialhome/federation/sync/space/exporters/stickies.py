"""Stickies exporter — ``stickies`` rows scoped to a space."""

from __future__ import annotations

from collections.abc import AsyncIterator
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
        return await self._records(space_id, None)

    async def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """The live stickies changed after ``since`` (§25.6 incremental,
        stamped since migration 0086); deletes ride ``stickies_deleted``."""
        yield await self._records(space_id, since)

    async def _records(self, space_id: str, since: int | None) -> list[dict[str, Any]]:
        stickies = await self._repo.list(space_id=space_id, since_seq=since)
        return [asdict(s) for s in stickies]
