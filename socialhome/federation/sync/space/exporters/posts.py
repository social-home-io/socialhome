"""Posts exporter for §25.6 space sync.

Every live post of the space inside its retention window (all of them
when the space keeps forever — :mod:`..window`), newest first, read page
by page so a big space streams in bounded memory.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any, TYPE_CHECKING

from ..exporter import PagedExporterMixin
from ..window import SYNC_PAGE_SIZE, SyncWindow, SyncWindows, iter_pages

if TYPE_CHECKING:
    from .....domain.post import Post
    from .....repositories.space_post_repo import AbstractSpacePostRepo


def iter_post_pages(
    repo: "AbstractSpacePostRepo",
    space_id: str,
    window: SyncWindow,
    *,
    since: int | None = None,
) -> AsyncIterator[list["Post"]]:
    """The space's live posts in ``window``, page by page. Shared by every
    exporter that walks posts (posts, polls, schedules) and the catch-up
    media. ``since``: only the posts changed after that stamp (an
    incremental session) — a reaction, a vote, a poll / schedule / listing
    change touches the post (migration 0086)."""

    async def fetch(cursor: int | None) -> tuple[list["Post"], int | None]:
        return await repo.list_sync_page(
            space_id,
            cutoff=window.cutoff,
            exempt_types=window.exempt_types,
            cursor=cursor,
            limit=SYNC_PAGE_SIZE,
            since=since,
        )

    return iter_pages(fetch)


class PostsExporter(PagedExporterMixin):
    """Exports the live ``space_posts`` rows inside the retention window."""

    resource = "posts"

    __slots__ = ("_repo", "_windows")

    def __init__(
        self, space_post_repo: "AbstractSpacePostRepo", windows: SyncWindows
    ) -> None:
        self._repo = space_post_repo
        self._windows = windows

    def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        return self._pages(space_id, None)

    def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        return self._pages(space_id, since)

    async def _pages(
        self, space_id: str, since: int | None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        window = await self._windows.for_space(space_id)
        async for posts in iter_post_pages(self._repo, space_id, window, since=since):
            yield [_post_to_dict(p) for p in posts]


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
