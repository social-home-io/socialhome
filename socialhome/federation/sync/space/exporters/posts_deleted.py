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

Each record names the post, never its content: ``{id, post_id, author,
type, created_at, moderated}`` plus ``actor_user_id`` — the moderator —
for a moderator removal (migration 0084). The author and the moderator
are what the receiver's authority check needs (the live rule: the
author's household, or content authority with ``moderates_as`` under a
restricted ``posts`` level); ``type`` keeps a host stub's post type, so a
retention-exempt type stays exempt. A separate resource rather than a flag
on ``posts`` records, so an older receiver drops it as unknown instead of
reading a tombstone as a live post. Ships before ``posts`` in
:data:`~..exporter.RESOURCE_ORDER`.

No retention window: every tombstone streams, page by page. Only the host
runs the retention sweep, which soft-deletes expired posts; their
tombstones are how that expiry reaches the member households, which never
sweep on their own. A tombstone is a few dozen bytes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ..exporter import PagedExporterMixin
from ..window import SYNC_PAGE_SIZE, iter_pages

if TYPE_CHECKING:
    from .....domain.post import PostTombstone
    from .....repositories.space_post_repo import AbstractSpacePostRepo


class PostsDeletedExporter(PagedExporterMixin):
    resource = "posts_deleted"

    __slots__ = ("_repo",)

    def __init__(self, space_post_repo: "AbstractSpacePostRepo") -> None:
        self._repo = space_post_repo

    async def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        async def fetch(
            cursor: int | None,
        ) -> tuple[list["PostTombstone"], int | None]:
            return await self._repo.list_post_tombstones_page(
                space_id, cursor=cursor, limit=SYNC_PAGE_SIZE
            )

        async for page in iter_pages(fetch):
            yield [post_tombstone_record(t) for t in page]


def post_tombstone_record(tombstone: "PostTombstone") -> dict[str, Any]:
    """The wire record of a deleted post — identity only, never content."""
    record: dict[str, Any] = {
        "id": tombstone.id,
        "post_id": tombstone.id,
        "author": tombstone.author,
        "type": tombstone.type,
        "created_at": tombstone.created_at,
        "moderated": tombstone.moderated,
    }
    if tombstone.moderated and tombstone.moderated_by:
        record["actor_user_id"] = tombstone.moderated_by
    return record
