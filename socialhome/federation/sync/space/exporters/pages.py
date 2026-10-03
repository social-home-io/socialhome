"""Pages exporter — ``space_pages`` rows (v_48: with the host's ``seq``,
the version hash and the open conflict list, so a member household mirrors
the host's version by ``seq``; a household's own draft bookkeeping
``pending_base_seq`` never leaves it)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TYPE_CHECKING

from .....domain.page_version import version_hash
from .....services.page_conflict_service import side_to_wire

if TYPE_CHECKING:
    from .....repositories.page_repo import AbstractPageRepo


class PagesExporter:
    resource = "pages"

    __slots__ = ("_repo",)

    def __init__(self, page_repo: "AbstractPageRepo") -> None:
        self._repo = page_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        pages = await self._repo.list(space_id=space_id)
        out: list[dict[str, Any]] = []
        for p in pages:
            record = asdict(p)
            record.pop("pending_base_seq", None)
            record["version_hash"] = version_hash(p.title, p.content, p.cover_image_url)
            sides = await self._repo.list_conflict_sides(p.id, space_id=space_id)
            record["conflict"] = [side_to_wire(s) for s in sides]
            out.append(record)
        return out
