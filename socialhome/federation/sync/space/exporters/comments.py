"""Comments exporter — walks posts, lists comments per post."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.space_post_repo import AbstractSpacePostRepo


class CommentsExporter:
    resource = "comments"

    __slots__ = ("_repo",)

    def __init__(self, space_post_repo: "AbstractSpacePostRepo") -> None:
        self._repo = space_post_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        posts = await self._repo.list_feed(space_id, limit=1000)
        out: list[dict[str, Any]] = []
        for p in posts:
            comments = await self._repo.list_comments(p.id)
            for c in comments:
                out.append(_comment_to_dict(c))
        return out


def _comment_to_dict(comment) -> dict[str, Any]:
    d = asdict(comment)
    if d.get("created_at") is not None and not isinstance(d["created_at"], str):
        d["created_at"] = d["created_at"].isoformat()
    if d.get("type") and not isinstance(d["type"], str):
        d["type"] = d["type"].value
    return d
