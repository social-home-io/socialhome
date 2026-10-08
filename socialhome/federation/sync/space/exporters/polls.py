"""Polls exporter — polls attached to space posts.

Walks posts for the space, calls :class:`AbstractPollRepo.get_meta` /
``list_options_with_counts`` for each post that has a poll, emits
one record per poll.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, TYPE_CHECKING

from ..exporter import PagedExporterMixin
from ..window import SyncWindows
from .posts import iter_post_pages

if TYPE_CHECKING:
    from .....repositories.poll_repo import AbstractPollRepo
    from .....repositories.space_post_repo import AbstractSpacePostRepo


class PollsExporter(PagedExporterMixin):
    resource = "polls"

    __slots__ = ("_poll_repo", "_post_repo", "_windows")

    def __init__(
        self,
        poll_repo: "AbstractPollRepo",
        space_post_repo: "AbstractSpacePostRepo",
        windows: SyncWindows,
    ) -> None:
        self._poll_repo = poll_repo
        self._post_repo = space_post_repo
        self._windows = windows

    async def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        window = await self._windows.for_space(space_id)
        async for posts in iter_post_pages(self._post_repo, space_id, window):
            yield await self._records(posts)

    async def _records(self, posts) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in posts:
            meta = await self._poll_repo.get_meta(p.id)
            if meta is None:
                continue
            options = await self._poll_repo.list_options_with_counts(p.id)
            out.append(
                {
                    "post_id": p.id,
                    "meta": meta,
                    "options": options,
                }
            )
        return out
