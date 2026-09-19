"""Schedules exporter — slot defs + deadline for every schedule poll in a space.

The wrapper ``PostType.SCHEDULE`` post streams under the ``posts``
resource; this exporter ships the matching
``space_schedule_poll_meta`` + ``space_schedule_slots`` rows so a new
joiner sees the slot picker. Without it (F5 gap) the receiver landed
the wrapper post but the slot picker rendered empty.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .....repositories.poll_repo import AbstractPollRepo
    from .....repositories.space_post_repo import AbstractSpacePostRepo


class SchedulesExporter:
    resource = "schedules"

    __slots__ = ("_poll_repo", "_post_repo")

    def __init__(
        self,
        poll_repo: "AbstractPollRepo",
        space_post_repo: "AbstractSpacePostRepo",
    ) -> None:
        self._poll_repo = poll_repo
        self._post_repo = space_post_repo

    async def list_records(self, space_id: str) -> list[dict[str, Any]]:
        posts = await self._post_repo.list_for_sync(space_id, limit=1000)
        out: list[dict[str, Any]] = []
        for p in posts:
            meta = await self._poll_repo.get_schedule_meta(p.id)
            if meta is None:
                continue
            slots = await self._poll_repo.list_schedule_slots(p.id)
            out.append(
                {
                    "post_id": p.id,
                    "title": meta["title"],
                    "deadline": meta.get("deadline"),
                    "slots": list(slots),
                },
            )
        return out
