"""Deleted stickies exporter — a space's sticky tombstones (§25.6,
migration 0085). Ships before ``stickies`` in :data:`RESOURCE_ORDER`; see
:mod:`.row_tombstones` for the shared shape."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .row_tombstones import RowTombstonesExporter

if TYPE_CHECKING:
    from .....repositories.sticky_repo import AbstractStickyRepo


class StickiesDeletedExporter(RowTombstonesExporter):
    resource = "stickies_deleted"
    owner_key = "author"

    __slots__ = ()

    def __init__(self, sticky_repo: "AbstractStickyRepo") -> None:
        super().__init__(sticky_repo.list_tombstones_page)
