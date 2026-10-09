"""Deleted zones exporter — a space's zone tombstones (§25.6, migration
0085). Ships before ``space_zones`` in :data:`RESOURCE_ORDER`; see
:mod:`.row_tombstones` for the shared shape. A tombstone carries no name
and no circle — never a coordinate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .row_tombstones import RowTombstonesExporter

if TYPE_CHECKING:
    from .....repositories.space_zone_repo import AbstractSpaceZoneRepo


class ZonesDeletedExporter(RowTombstonesExporter):
    resource = "space_zones_deleted"
    owner_key = "created_by"

    __slots__ = ()

    def __init__(self, zone_repo: "AbstractSpaceZoneRepo") -> None:
        super().__init__(zone_repo.list_tombstones_page)
