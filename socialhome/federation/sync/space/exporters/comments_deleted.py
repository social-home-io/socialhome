"""Deleted comments exporter — a space's comment tombstones (§25.6).

A comment delete keeps its row (``space_post_comments.deleted = 1``,
content cleared) — the comment's tombstone, as for posts
(:mod:`.posts_deleted`). Streaming them is how a household that missed
``SPACE_COMMENT_DELETED`` learns of the delete: the receiver soft-deletes
its copy (and lowers the post's comment count), and — from the host —
records the tombstone for an id it never held on a post it holds, so a
stale copy streamed later cannot create it.

Each record names the comment, its post and its author, never content:
``{id, comment_id, post_id, author, created_at}``. A separate resource,
so an older receiver drops it as unknown. Ships after ``posts`` (a stub
needs its post held) and before ``comments`` in
:data:`~..exporter.RESOURCE_ORDER`. No retention window (unlike the live
comments): a delete must reach every household that holds the comment,
whatever its age. Oldest stored first (row id order), page by page.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ..exporter import PagedExporterMixin
from ..window import KEEP_FOREVER
from .comments import iter_comment_pages

if TYPE_CHECKING:
    from .....domain.post import Comment
    from .....repositories.space_post_repo import AbstractSpacePostRepo


class CommentsDeletedExporter(PagedExporterMixin):
    resource = "comments_deleted"

    __slots__ = ("_repo",)

    def __init__(self, space_post_repo: "AbstractSpacePostRepo") -> None:
        self._repo = space_post_repo

    def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        return self._pages(space_id, None)

    def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        return self._pages(space_id, since)

    async def _pages(
        self, space_id: str, since: int | None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        async for comments in iter_comment_pages(
            self._repo, space_id, KEEP_FOREVER, deleted=True, since=since
        ):
            yield [comment_tombstone_record(c) for c in comments]


def comment_tombstone_record(comment: "Comment") -> dict[str, Any]:
    """The wire record of a deleted comment — ids and author only."""
    return {
        "id": comment.id,
        "comment_id": comment.id,
        "post_id": comment.post_id,
        "author": comment.author,
        "created_at": comment.created_at.isoformat(),
    }
