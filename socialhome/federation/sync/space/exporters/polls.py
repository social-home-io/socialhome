"""Polls exporter — polls attached to space posts.

Walks posts for the space, calls :class:`AbstractPollRepo.get_meta` /
``list_options_with_counts`` for each post that has a poll, emits
one record per poll.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.poll_repo import AbstractPollRepo
    from .....repositories.space_post_repo import AbstractSpacePostRepo


class PollsExporter:
    resource = "polls"

    __slots__ = ("_poll_repo", "_post_repo")

    def __init__(
        self,
        poll_repo: "AbstractPollRepo",
        space_post_repo: "AbstractSpacePostRepo",
    ) -> None:
        self._poll_repo = poll_repo
        self._post_repo = space_post_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        posts = await self._post_repo.list_feed(space_id, limit=1000)
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
