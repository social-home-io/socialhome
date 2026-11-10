"""Schedules exporter — slot defs + deadline for every schedule poll in a space.

The wrapper ``PostType.SCHEDULE`` post streams under the ``posts``
resource; this exporter ships the matching
``space_schedule_poll_meta`` + ``space_schedule_slots`` rows so a new
joiner sees the slot picker. Without it (F5 gap) the receiver landed
the wrapper post but the slot picker rendered empty.

v_92: the record also carries the poll's answers (``responses``:
``{slot_id, user_id, availability, responded_at}`` — every member household
gets each answer live, through ``SPACE_SCHEDULE_RESPONSE_UPDATED``), when
the host read them (``responses_at``) and its pick (``finalized_slot_id``);
a v_92 receiver takes them from the space's host (everybody's answers but
its own people's, last writer wins on ``responded_at``). A record never
splits across chunks, so the answers are bounded:
:data:`MAX_RESPONSES_PER_SCHEDULE`, the newest first; ``responses_partial``
marks a cut list, which the receiver never deletes from. A requester below
v_92 gets the slot definitions only (:meth:`SchedulesExporter.slots_only`).

An answer changes no row the change stamps cover (migration 0086), so a
missed answer heals on the host's daily full stream; a pick (it updates
the poll) heals on the next incremental one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..exporter import PagedExporterMixin
from ..window import SyncWindows
from .posts import iter_post_pages

if TYPE_CHECKING:
    from .....repositories.space_poll_repo import AbstractSpacePollRepo
    from .....repositories.space_post_repo import AbstractSpacePostRepo


#: The most answers one ``schedules`` record carries (~150 B each:
#: ≤ ~150 KB).
MAX_RESPONSES_PER_SCHEDULE: int = 1000


class SchedulesExporter(PagedExporterMixin):
    resource = "schedules"

    __slots__ = ("_answers", "_poll_repo", "_post_repo", "_windows")

    def __init__(
        self,
        poll_repo: AbstractSpacePollRepo,
        space_post_repo: AbstractSpacePostRepo,
        windows: SyncWindows,
        *,
        answers: bool = True,
    ) -> None:
        self._poll_repo = poll_repo
        self._post_repo = space_post_repo
        self._windows = windows
        self._answers = answers

    def slots_only(self) -> SchedulesExporter:
        """The record set of a requester below v_92: no answers, no pick."""
        return SchedulesExporter(
            self._poll_repo, self._post_repo, self._windows, answers=False
        )

    def iter_batches(self, space_id: str) -> AsyncIterator[list[dict[str, Any]]]:
        return self._pages(space_id, None)

    def iter_changed(
        self, space_id: str, since: int
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """The changed posts' records — a poll / schedule change (or a
        vote) touches its post (migration 0086)."""
        return self._pages(space_id, since)

    async def _pages(
        self, space_id: str, since: int | None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        window = await self._windows.for_space(space_id)
        async for posts in iter_post_pages(
            self._post_repo, space_id, window, since=since
        ):
            yield await self._records(posts)

    async def _records(self, posts) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in posts:
            meta = await self._poll_repo.get_schedule_meta(p.id)
            if meta is None:
                continue
            slots = await self._poll_repo.list_schedule_slots(p.id)
            record: dict[str, Any] = {
                "post_id": p.id,
                "title": meta["title"],
                "deadline": meta.get("deadline"),
                "slots": list(slots),
            }
            if self._answers:
                record["finalized_slot_id"] = meta.get("finalized_slot_id")
                record["responses_at"] = datetime.now(UTC).isoformat()
                answers = await self._poll_repo.list_schedule_answers(
                    p.id, limit=MAX_RESPONSES_PER_SCHEDULE + 1
                )
                if len(answers) > MAX_RESPONSES_PER_SCHEDULE:
                    answers = answers[:MAX_RESPONSES_PER_SCHEDULE]
                    record["responses_partial"] = True
                record["responses"] = answers
            out.append(record)
        return out
