"""What's in each section of a space — the counts behind the meta line of
More's section rows ("5 open · 2 yours", "2 albums · 5 photos").

Read-time only: every number comes from the rows the sections themselves
list (no counter columns, no migration). A section the space has turned
off is left out (``None``), so the SPA never shows a count for a section
it doesn't offer. Every seated reader — a follower included — may ask:
these are the sections a follower reads too (the map and the chat, which
a follower never sees, have no count here).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ..domain.task import TaskStatus
from ..repositories.bazaar_repo import AbstractBazaarRepo
from ..repositories.calendar_repo import AbstractSpaceCalendarRepo
from ..repositories.gallery_repo import AbstractGalleryRepo
from ..repositories.page_repo import AbstractPageRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.sticky_repo import AbstractStickyRepo
from ..repositories.task_repo import AbstractSpaceTaskRepo
from .space_member_guard import require_seat

log = logging.getLogger(__name__)

#: How far ahead "upcoming" events reach.
UPCOMING_DAYS = 30

#: The most albums one summary reads (the gallery repo's page cap).
_ALBUM_PAGE = 200


class SpaceSummaryService:
    """``GET /api/spaces/{id}/summary`` — per-section counts for a seated
    reader (see the module docstring)."""

    __slots__ = (
        "_bazaar",
        "_calendar",
        "_gallery",
        "_pages",
        "_spaces",
        "_stickies",
        "_tasks",
    )

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        task_repo: AbstractSpaceTaskRepo,
        page_repo: AbstractPageRepo,
        gallery_repo: AbstractGalleryRepo,
        sticky_repo: AbstractStickyRepo,
        bazaar_repo: AbstractBazaarRepo,
        calendar_repo: AbstractSpaceCalendarRepo,
    ) -> None:
        self._spaces = space_repo
        self._tasks = task_repo
        self._pages = page_repo
        self._gallery = gallery_repo
        self._stickies = sticky_repo
        self._bazaar = bazaar_repo
        self._calendar = calendar_repo

    async def summary(
        self, space_id: str, *, user_id: str, now: datetime | None = None
    ) -> dict:
        """The counts per section. The seat is checked first, so an unknown
        id and a space the caller isn't in answer the same 403; a dissolved
        space with a seat left is a ``KeyError`` (404)."""
        await require_seat(self._spaces, space_id, user_id)
        space = await self._spaces.get(space_id)
        if space is None:
            raise KeyError(f"Space {space_id!r} not found")
        f = space.features
        return {
            "tasks": await self._task_counts(space_id, user_id) if f.todo else None,
            "pages": (
                {"count": len(await self._pages.list(space_id=space_id))}
                if f.pages
                else None
            ),
            "gallery": await self._gallery_counts(space_id) if f.gallery else None,
            "stickies": (
                {"count": len(await self._stickies.list(space_id=space_id))}
                if f.stickies
                else None
            ),
            "bazaar": (
                {"active": len(await self._bazaar.list_active_in_spaces((space_id,)))}
                if f.bazaar
                else None
            ),
            "calendar": (
                await self._upcoming(space_id, now or datetime.now(UTC))
                if f.calendar
                else None
            ),
        }

    async def _task_counts(self, space_id: str, user_id: str) -> dict:
        open_tasks = [
            t
            for t in await self._tasks.list_by_space(space_id)
            if t.status != TaskStatus.DONE and t.archived_at is None
        ]
        return {
            "open": len(open_tasks),
            "mine": sum(1 for t in open_tasks if user_id in t.assignees),
        }

    async def _gallery_counts(self, space_id: str) -> dict:
        albums = await self._gallery.list_albums(space_id, limit=_ALBUM_PAGE)
        return {
            "albums": len(albums),
            "items": sum(a.item_count for a in albums),
        }

    async def _upcoming(self, space_id: str, now: datetime) -> dict:
        """Occurrences (a recurring event once per occurrence) still to
        come or under way in the next :data:`UPCOMING_DAYS` days."""
        events = await self._calendar.list_events_in_range(
            space_id, start=now, end=now + timedelta(days=UPCOMING_DAYS)
        )
        return {"upcoming": len(events), "days": UPCOMING_DAYS}
