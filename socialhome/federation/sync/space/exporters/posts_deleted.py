"""Deleted posts exporter — a space's post tombstones (§25.6).

A post delete keeps its row (``space_posts.deleted = 1``, content
cleared): that soft-deleted row is the post's tombstone — the same one a
delete that overtook its create leaves (v_49). Streaming the tombstones is
how a household that missed ``SPACE_POST_DELETED`` (offline past the
outbox, a mesh drop) learns of the delete: the receiver soft-deletes its
copy, and — from the host — records the tombstone for an id it never
held, so no stale copy streamed later can create it. Without it, a
household that missed a delete kept the post forever and, as a catch-up
provider, re-spread it to every joiner.

Each record names the post and its author, never content: ``{id,
post_id, author, created_at}`` — the author is what the receiver's
authority check (the author's household, or content authority) and the
owner-bound id check need. A separate resource rather than a flag on
``posts`` records, so an older receiver drops it as unknown instead of
reading a tombstone as a live post. Ships before ``posts`` in
:data:`~..exporter.RESOURCE_ORDER`.

Window: the tombstones of posts inside the space's retention window —
the same window as the live posts (a post past retention is gone for
every household anyway); every tombstone when the space keeps forever.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ..exporter import PagedExporterMixin
from ..window import SyncWindows
from .posts import iter_post_pages

if TYPE_CHECKING:
    from .....domain.post import Post
    from .....repositories.space_post_repo import AbstractSpacePostRepo


class PostsDeletedExporter(PagedExporterMixin):
    resource = "posts_deleted"

    __slots__ = ("_repo", "_windows")

    def __init__(
        self, space_post_repo: "AbstractSpacePostRepo", windows: SyncWindows
    ) -> None:
        self._repo = space_post_repo
        self._windows = windows

    async def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        window = await self._windows.for_space(space_id)
        async for posts in iter_post_pages(self._repo, space_id, window, deleted=True):
            yield [post_tombstone_record(p) for p in posts]


def post_tombstone_record(post: "Post") -> dict[str, Any]:
    """The wire record of a deleted post — ids and author only."""
    return {
        "id": post.id,
        "post_id": post.id,
        "author": post.author,
        "created_at": post.created_at.isoformat(),
    }
