"""Space pages — the wiki tab of a space (§4.4.4, §24.11).

Every read and write takes the caller's (path) ``space_id`` and only ever
reaches that space's pages: a page id of another space or of the household
is :class:`KeyError` (→ 404, never 403, so the answer doesn't confirm the id
exists elsewhere). Membership of the path space and the per-space ``pages``
feature toggle are checked by the route first; this service owns the rest:

* :meth:`SpacePageService.require_writer` — a write needs a non-archived
  space and a writable seat (subscribers read, never write);
* every write passes the space's ``pages`` access level for the acting
  user (§4.3, :class:`ContentAccessMixin`) — under ``ADMIN_ONLY`` only the
  owner / admins create, edit, resolve conflicts on or delete pages;
* each write publishes :class:`PageCreated` / :class:`PageUpdated` /
  :class:`PageDeleted` with the actor, which ``PageFederationOutbound``
  federates to the space's member households.

Bodies are stored canonical: a signed ``/api/media/…?exp=&sig=`` URL the
editor echoed back is stripped on save, and re-signed on each read by the
route.
"""

from __future__ import annotations

import builtins
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.events import PageCreated, PageDeleted, PageUpdated
from ..domain.page import Page, PageVersion
from ..domain.space import ContentAction, SpacePermissionError
from ..media_signer import strip_signature_query, strip_signed_media_in_markdown
from ..repositories.page_repo import new_page
from .bus_publisher import BusPublisherMixin
from .content_access import ContentAccessMixin
from .space_service import SpaceService

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus
    from ..repositories.page_repo import AbstractPageRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .page_conflict_service import PageConflictService


class _Unset:
    """Sentinel: a field the caller did not send (distinct from ``None``)."""


UNSET = _Unset()


class PageStaleError(Exception):
    """An update based on an ``updated_at`` that is no longer current —
    somebody else saved first. Carries the page as it now is, for the
    side-by-side conflict view (§23.72)."""

    def __init__(self, current: Page) -> None:
        super().__init__("page changed since it was loaded")
        self.current = current


async def snapshot_page_version(
    pages: "AbstractPageRepo", *, previous: Page, editor_user_id: str
) -> None:
    """Persist ``previous`` as a row in ``page_edit_history``.

    Called right before an edit or revert so the history always carries a
    copy of the pre-change state — the versions list then shows "what the
    page looked like before the current live body".
    """
    await pages.save_version(
        PageVersion(
            id=uuid.uuid4().hex,
            page_id=previous.id,
            version=await pages.next_version_number(previous.id),
            title=previous.title,
            content=previous.content,
            edited_by=editor_user_id,
            edited_at=datetime.now(timezone.utc).isoformat(),
            space_id=previous.space_id,
            cover_image_url=previous.cover_image_url,
        )
    )


def _title(value: object) -> str:
    title = str(value or "").strip()
    if not title:
        raise ValueError("page title must not be empty")
    return title


class SpacePageService(BusPublisherMixin, ContentAccessMixin):
    """Scoped page operations for one space's wiki."""

    __slots__ = ("_pages", "_spaces", "_bus", "_conflicts")

    def __init__(
        self,
        page_repo: "AbstractPageRepo",
        *,
        space_repo: "AbstractSpaceRepo",
        bus: "EventBus | None" = None,
        conflict_service: "PageConflictService | None" = None,
    ) -> None:
        self._pages = page_repo
        self._spaces = space_repo
        self._bus = bus
        self._conflicts = conflict_service

    # ── Writer gate ──────────────────────────────────────────────────────

    async def require_writer(self, space_id: str, user_id: str) -> None:
        """Raise unless ``user_id`` may write this space's pages.

        * unknown / dissolved space → :class:`KeyError` (404);
        * archived space (read-only) → :class:`SpacePermissionError`;
        * not a member, or a read-only subscriber →
          :class:`SpacePermissionError` (403).
        """
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        if space.archived:
            raise SpacePermissionError(
                "space is archived (read-only) — unarchive it to make changes",
            )
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        SpaceService.assert_writable_member(member, action="edit pages", space=space)

    # ── Reads ────────────────────────────────────────────────────────────

    async def list(self, space_id: str) -> builtins.list[Page]:
        return await self._pages.list(space_id=space_id)

    async def get(self, space_id: str, page_id: str) -> Page:
        page = await self._pages.get_space_page(page_id, space_id=space_id)
        if page is None:
            raise KeyError(f"page {page_id!r} not found in this space")
        return page

    async def versions(self, space_id: str, page_id: str) -> builtins.list[PageVersion]:
        """The edit history recorded under this space (read-only)."""
        await self.get(space_id, page_id)
        return await self._pages.list_versions(page_id, space_id=space_id)

    # ── Writes ───────────────────────────────────────────────────────────

    async def create(
        self,
        space_id: str,
        *,
        actor_user_id: str,
        title: object,
        content: str | None = "",
    ) -> Page:
        clean_title = _title(title)
        await self._gate(space_id, actor_user_id, "pages", ContentAction.CREATE, True)
        page = new_page(
            title=clean_title,
            content=strip_signed_media_in_markdown(content or "") or "",
            created_by=actor_user_id,
            space_id=space_id,
        )
        await self._pages.save(page, space_id=space_id)
        await self._emit(
            PageCreated(
                page_id=page.id,
                space_id=space_id,
                title=page.title,
                content=page.content,
                actor_user_id=actor_user_id,
            )
        )
        return page

    async def update(
        self,
        space_id: str,
        page_id: str,
        *,
        actor_user_id: str,
        title: object = UNSET,
        content: object = UNSET,
        cover_image_url: object = UNSET,
        base_updated_at: str | None = None,
    ) -> Page:
        """Apply the sent fields. ``base_updated_at`` — the ``updated_at``
        the editor loaded — makes a concurrent save a
        :class:`PageStaleError` instead of a silent overwrite."""
        page = await self.get(space_id, page_id)
        await self._gate(
            space_id,
            actor_user_id,
            "pages",
            ContentAction.EDIT,
            page.created_by == actor_user_id,
        )
        if base_updated_at and base_updated_at != page.updated_at:
            raise PageStaleError(page)
        now_iso = datetime.now(timezone.utc).isoformat()
        fields: dict = {
            "updated_at": now_iso,
            "last_editor_user_id": actor_user_id,
            "last_edited_at": now_iso,
        }
        if not isinstance(title, _Unset):
            fields["title"] = _title(title)
        if not isinstance(content, _Unset):
            fields["content"] = strip_signed_media_in_markdown(
                content if isinstance(content, str) else None
            )
        if not isinstance(cover_image_url, _Unset):
            fields["cover_image_url"] = strip_signature_query(
                cover_image_url if isinstance(cover_image_url, str) else None
            )
        updated = replace(page, **fields)
        await self._pages.save(updated, space_id=space_id)
        await snapshot_page_version(
            self._pages, previous=page, editor_user_id=actor_user_id
        )
        await self._emit(
            PageUpdated(
                page_id=updated.id,
                space_id=space_id,
                title=updated.title,
                content=updated.content,
                actor_user_id=actor_user_id,
            )
        )
        return updated

    async def delete(self, space_id: str, page_id: str, *, actor_user_id: str) -> None:
        page = await self.get(space_id, page_id)
        await self._gate(
            space_id,
            actor_user_id,
            "pages",
            ContentAction.DELETE,
            page.created_by == actor_user_id,
        )
        await self._pages.delete(page.id, space_id=space_id)
        # ``space_id`` is what routes the delete to the space's member
        # households (``PageFederationOutbound`` drops a scope-less one).
        await self._emit(
            PageDeleted(page_id=page.id, space_id=space_id, actor_user_id=actor_user_id)
        )

    async def resolve_conflict(
        self,
        space_id: str,
        page_id: str,
        *,
        actor_user_id: str,
        resolution: str,
        merged_content: str | None = None,
    ) -> str:
        """Pick a side of an open edit conflict (§4.4.4.1) — an EDIT of the
        page. Returns the body that is now current."""
        if self._conflicts is None:
            raise RuntimeError("SpacePageService: conflict service not attached")
        page = await self.get(space_id, page_id)
        await self._gate(
            space_id,
            actor_user_id,
            "pages",
            ContentAction.EDIT,
            page.created_by == actor_user_id,
        )
        return await self._conflicts.resolve_conflict(
            space_id=space_id,
            page_id=page_id,
            user_id=actor_user_id,
            resolution=resolution,
            merged_content=merged_content,
        )
