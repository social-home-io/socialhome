"""Comments exporter — the live comments of a space's live posts (§25.6).

Read straight from the space's comments page by page (oldest stored
first, so a reply never streams before its parent), inside the space's
retention window — the parent post's: a comment streams while its post
does. A deleted comment is never streamed here; it travels as a
``comments_deleted`` tombstone (:mod:`.comments_deleted`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from ..exporter import PagedExporterMixin
from ..window import SYNC_PAGE_SIZE, SyncWindow, SyncWindows, iter_pages

if TYPE_CHECKING:
    from .....domain.post import Comment
    from .....repositories.space_post_repo import AbstractSpacePostRepo


def iter_comment_pages(
    repo: "AbstractSpacePostRepo",
    space_id: str,
    window: SyncWindow,
    *,
    deleted: bool = False,
) -> AsyncIterator[list["Comment"]]:
    """The space's comments in ``window`` — live ones on live posts, or
    (``deleted``) the tombstones on any post — page by page."""

    async def fetch(cursor: int | None) -> tuple[list["Comment"], int | None]:
        return await repo.list_comments_sync_page(
            space_id,
            deleted=deleted,
            cutoff=window.cutoff,
            exempt_types=window.exempt_types,
            cursor=cursor,
            limit=SYNC_PAGE_SIZE,
        )

    return iter_pages(fetch)


class CommentsExporter(PagedExporterMixin):
    resource = "comments"

    __slots__ = ("_repo", "_windows")

    def __init__(
        self, space_post_repo: "AbstractSpacePostRepo", windows: SyncWindows
    ) -> None:
        self._repo = space_post_repo
        self._windows = windows

    async def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        window = await self._windows.for_space(space_id)
        async for comments in iter_comment_pages(self._repo, space_id, window):
            yield [_comment_to_dict(c) for c in comments]


def _comment_to_dict(comment) -> dict[str, Any]:
    d = asdict(comment)
    if d.get("created_at") is not None and not isinstance(d["created_at"], str):
        d["created_at"] = d["created_at"].isoformat()
    if d.get("edited_at") is not None and not isinstance(d["edited_at"], str):
        d["edited_at"] = d["edited_at"].isoformat()
    if d.get("type") and not isinstance(d["type"], str):
        d["type"] = d["type"].value
    return d
