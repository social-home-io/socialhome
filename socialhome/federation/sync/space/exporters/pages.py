"""Pages exporter — ``space_pages`` rows."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.page_repo import AbstractPageRepo


class PagesExporter:
    resource = "pages"

    __slots__ = ("_repo",)

    def __init__(self, page_repo: "AbstractPageRepo") -> None:
        self._repo = page_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        pages = await self._repo.list(space_id=space_id)
        return [asdict(p) for p in pages]
