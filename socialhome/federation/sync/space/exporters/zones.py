"""Per-space zones exporter — ``space_zones`` rows scoped to a space.

Streams the zone catalogue (§23.8.7) over the chunked sync channel so
a remote member instance joining mid-life picks up every zone, not
just the ones added after their join. Live CRUD afterwards continues
to ride the per-event ``SPACE_ZONE_UPSERTED`` / ``SPACE_ZONE_DELETED``
federation events handled by :mod:`federation_inbound.space_content`.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.space_zone_repo import AbstractSpaceZoneRepo


class ZonesExporter:
    resource = "space_zones"

    __slots__ = ("_repo",)

    def __init__(self, zone_repo: "AbstractSpaceZoneRepo") -> None:
        self._repo = zone_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        zones = await self._repo.list_for_space(space_id)
        return [asdict(z) for z in zones]
