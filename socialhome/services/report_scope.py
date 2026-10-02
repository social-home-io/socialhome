"""Which space a reported item lives in.

A report about content inside a space is triaged by that space's content
authority, not by household admins (:mod:`.report_service`). The scope is
derived from the target itself — never trusted from the client or a peer —
by looking the item up in the repo that holds it:

* ``post`` / ``comment`` — a space post (or a comment on one); a household
  feed post / comment is household-level;
* ``page`` / ``sticky`` — a page / sticky whose ``space_id`` is set (a
  household page / sticky is household-level);
* ``task`` / ``calendar_event`` — a space task / space calendar event;
* ``gallery_item`` — an item whose album belongs to a space.

``user``, ``space``, ``highlight`` and ``moment`` are not content inside a
space; a ``user`` report is space-scoped only when the reporter names the
space (:meth:`ReportService.create_report`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.report import ReportTargetType

if TYPE_CHECKING:
    from ..repositories.post_repo import AbstractPostRepo
    from ..repositories.calendar_repo import AbstractSpaceCalendarRepo
    from ..repositories.gallery_repo import AbstractGalleryRepo
    from ..repositories.page_repo import AbstractPageRepo
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.sticky_repo import AbstractStickyRepo
    from ..repositories.task_repo import AbstractSpaceTaskRepo


#: Report targets that are content items (as opposed to a user / a space).
#: Every one is looked up; one that only exists inside spaces and is not
#: found is an unknown target (:data:`SPACE_ONLY_TARGETS`).
CONTENT_TARGETS: frozenset[ReportTargetType] = frozenset(
    {
        ReportTargetType.POST,
        ReportTargetType.COMMENT,
        ReportTargetType.PAGE,
        ReportTargetType.TASK,
        ReportTargetType.STICKY,
        ReportTargetType.CALENDAR_EVENT,
        ReportTargetType.GALLERY_ITEM,
    }
)

#: Targets with no household-level report path: they were added for space
#: moderation (0067), so one that resolves to no space is refused.
SPACE_ONLY_TARGETS: frozenset[ReportTargetType] = frozenset(
    {
        ReportTargetType.PAGE,
        ReportTargetType.TASK,
        ReportTargetType.STICKY,
        ReportTargetType.CALENDAR_EVENT,
        ReportTargetType.GALLERY_ITEM,
    }
)


#: The longest preview text handed to a moderator's report row.
PREVIEW_MAX = 160


@dataclass(slots=True, frozen=True)
class TargetScope:
    """Where a target was found. ``found=False`` → no repo knows it;
    ``space_id=None`` with ``found=True`` → household-level content.

    ``preview`` is a short plain-text glimpse of the item (its text /
    title / caption) for the moderator's report row, ``author`` the user
    who wrote / created / uploaded it (so they never triage a report on
    their own content) — both local display / policy only, never
    federated."""

    found: bool
    space_id: str | None = None
    preview: str | None = None
    #: Found, but soft-deleted (a post / comment removed in place).
    gone: bool = False
    author: str | None = None


_NOT_FOUND = TargetScope(found=False)


class ReportScope:
    """Looks a report target up and returns the space it lives in."""

    __slots__ = (
        "_posts",
        "_feed",
        "_pages",
        "_stickies",
        "_tasks",
        "_calendar",
        "_gallery",
    )

    def __init__(
        self,
        *,
        space_post_repo: "AbstractSpacePostRepo | None" = None,
        post_repo: "AbstractPostRepo | None" = None,
        page_repo: "AbstractPageRepo | None" = None,
        sticky_repo: "AbstractStickyRepo | None" = None,
        space_task_repo: "AbstractSpaceTaskRepo | None" = None,
        space_calendar_repo: "AbstractSpaceCalendarRepo | None" = None,
        gallery_repo: "AbstractGalleryRepo | None" = None,
    ) -> None:
        self._posts = space_post_repo
        self._feed = post_repo
        self._pages = page_repo
        self._stickies = sticky_repo
        self._tasks = space_task_repo
        self._calendar = space_calendar_repo
        self._gallery = gallery_repo

    async def of(self, tt: ReportTargetType, target_id: str) -> TargetScope:
        """The scope of ``(tt, target_id)``; ``found=False`` when unknown
        (or ``tt`` is not a content target)."""
        if not target_id:
            return _NOT_FOUND
        match tt:
            case ReportTargetType.POST:
                hit = await self._space_post(target_id)
                return hit if hit.found else await self._feed_post(target_id)
            case ReportTargetType.COMMENT:
                hit = await self._space_comment(target_id)
                return hit if hit.found else await self._feed_comment(target_id)
            case ReportTargetType.PAGE:
                if self._pages is None:
                    return _NOT_FOUND
                page = await self._pages.get(target_id)
                if page is None:
                    return _NOT_FOUND
                return _found(page.space_id, page, "title", "created_by")
            case ReportTargetType.STICKY:
                if self._stickies is None:
                    return _NOT_FOUND
                sticky = await self._stickies.get(target_id)
                if sticky is None:
                    return _NOT_FOUND
                return _found(sticky.space_id, sticky, "content", "author")
            case ReportTargetType.TASK:
                if self._tasks is None:
                    return _NOT_FOUND
                hit_t = await self._tasks.get(target_id)
                if hit_t is None:
                    return _NOT_FOUND
                return _found(hit_t[0], hit_t[1], "title", "created_by")
            case ReportTargetType.CALENDAR_EVENT:
                if self._calendar is None:
                    return _NOT_FOUND
                ev = await self._calendar.get_event(target_id)
                if ev is None:
                    return _NOT_FOUND
                return _found(ev[0], ev[1], "summary", "created_by")
            case ReportTargetType.GALLERY_ITEM:
                if self._gallery is None:
                    return _NOT_FOUND
                item = await self._gallery.get_item(target_id)
                if item is None:
                    return _NOT_FOUND
                album = await self._gallery.get_album(item.album_id)
                if album is None:
                    return _NOT_FOUND
                return _found(album.space_id, item, "caption", "uploaded_by")
            case _:
                return _NOT_FOUND

    async def _space_post(self, post_id: str) -> TargetScope:
        if self._posts is None or not post_id:
            return _NOT_FOUND
        hit = await self._posts.get(post_id)
        if hit is None:
            return _NOT_FOUND
        return _found(hit[0], hit[1], "content", "author")

    async def _space_comment(self, comment_id: str) -> TargetScope:
        if self._posts is None:
            return _NOT_FOUND
        comment = await self._posts.get_comment(comment_id)
        if comment is None:
            return _NOT_FOUND
        parent = await self._space_post(comment.post_id)
        if not parent.found:
            return _NOT_FOUND
        return _found(parent.space_id, comment, "content", "author")

    async def _feed_post(self, post_id: str) -> TargetScope:
        if self._feed is None:
            return _NOT_FOUND
        post = await self._feed.get(post_id)
        if post is None:
            return _NOT_FOUND
        return _found(None, post, "content", "author")

    async def _feed_comment(self, comment_id: str) -> TargetScope:
        if self._feed is None:
            return _NOT_FOUND
        comment = await self._feed.get_comment(comment_id)
        if comment is None:
            return _NOT_FOUND
        return _found(None, comment, "content", "author")


def _found(
    space_id: str | None,
    obj: object = None,
    text_attr: str = "",
    author_attr: str = "",
) -> TargetScope:
    author = getattr(obj, author_attr, None) if author_attr else None
    author_id = author if isinstance(author, str) and author else None
    if getattr(obj, "deleted", False) is True:
        return TargetScope(
            found=True, space_id=space_id or None, gone=True, author=author_id
        )
    text = getattr(obj, text_attr, None) if text_attr else None
    return TargetScope(
        found=True,
        space_id=space_id or None,
        preview=_clip(text),
        author=author_id,
    )


def _clip(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    flat = " ".join(text.split())
    if not flat:
        return None
    return flat if len(flat) <= PREVIEW_MAX else flat[: PREVIEW_MAX - 1] + "…"
