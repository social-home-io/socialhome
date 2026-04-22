"""Posts exporter for §25.6 space sync."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.space_post_repo import AbstractSpacePostRepo


class PostsExporter:
    """Exports non-deleted ``space_posts`` rows."""

    resource = "posts"

    __slots__ = ("_repo",)

    def __init__(self, space_post_repo: "AbstractSpacePostRepo") -> None:
        self._repo = space_post_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        # Upper-bound limit; household-scale spaces have well under 1000 posts.
        posts = await self._repo.list_feed(space_id, limit=1000)
        return [_post_to_dict(p) for p in posts]


def _post_to_dict(post) -> dict[str, Any]:
    d = asdict(post)
    # Serialise non-JSON-native types.
    if d.get("created_at") is not None and not isinstance(d["created_at"], str):
        d["created_at"] = d["created_at"].isoformat()
    if d.get("edited_at") is not None and not isinstance(d["edited_at"], str):
        d["edited_at"] = d["edited_at"].isoformat()
    if d.get("type") and not isinstance(d["type"], str):
        d["type"] = d["type"].value
    # Reactions are ``dict[str, frozenset[str]]`` — coerce to lists for JSON.
    reactions = d.get("reactions")
    if reactions:
        d["reactions"] = {k: sorted(v) for k, v in reactions.items()}
    else:
        d["reactions"] = {}
    # file_meta is a nested dataclass.
    if d.get("file_meta") is not None:
        fm = d["file_meta"]
        if not isinstance(fm, dict):
            d["file_meta"] = asdict(fm)
    # Drop polls/schedules from the sync stream — they carry state that
    # the poll_repo / schedule_repo owns separately. The ``polls``
    # resource exporter handles polls specifically.
    d.pop("poll", None)
    d.pop("schedule", None)
    return d
