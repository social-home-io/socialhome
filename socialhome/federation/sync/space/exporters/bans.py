"""Bans exporter — the space's person bans (``space_bans`` rows via
:class:`AbstractSpaceRepo.list_bans`) and, since P7b, its household bans
(``space_instance_bans``: ``{space_id, instance_id, banned_at, reason}``, no
``user_id`` — a receiver before it skips them)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .....repositories.space_repo import AbstractSpaceRepo


class BansExporter:
    resource = "bans"

    __slots__ = ("_repo",)

    def __init__(self, space_repo: AbstractSpaceRepo) -> None:
        self._repo = space_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        # Both already return plain dicts.
        return list(await self._repo.list_bans(space_id)) + list(
            await self._repo.list_household_bans(space_id)
        )
