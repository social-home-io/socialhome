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
  owner / admins create, edit, resolve conflicts on or delete pages; under
  ``MODERATED`` a member's new page, and their edit / delete / conflict
  resolution of somebody else's, waits in the space moderation queue
  (:class:`PageModerationHandler` replays it on approval);
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
from typing import TYPE_CHECKING, Any

from ..domain.events import PageCreated, PageDeleted, PageUpdated
from ..domain.page import Page, PageVersion
from ..domain.space import (
    AccessDecision,
    ContentAction,
    ModerationStaleError,
    ModerationTargetGoneError,
    Space,
    SpaceModerationItem,
    SpacePermissionError,
)
from ..media_signer import strip_signature_query, strip_signed_media_in_markdown
from ..repositories.page_repo import mint_page_id, new_page
from .bus_publisher import BusPublisherMixin
from .content_access import ContentAccessMixin
from .page_conflict_service import NoActiveConflictError
from .space_moderation_service import ApplyResult, item_payload_snapshot
from .space_service import SpaceService

if TYPE_CHECKING:
    from .space_moderation_service import ModerationSubmitter
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

    __slots__ = ("_pages", "_spaces", "_bus", "_conflicts", "_moderation")

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
        self._moderation: "ModerationSubmitter | None" = None

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
        cover_image_url: str | None = None,
        page_id: str | None = None,
        approved_by: str | None = None,
    ) -> Page:
        """Create a page. ``approved_by`` (the moderation queue's replay)
        gates the write as that approver; ``page_id`` is the id the queued
        item minted. The page stays the actor's."""
        clean_title = _title(title)
        body = strip_signed_media_in_markdown(content or "") or ""
        cover = strip_signature_query(cover_image_url) if cover_image_url else None
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "pages",
            ContentAction.CREATE,
            approved_by is None,
        )
        if decision is AccessDecision.QUEUE:
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "pages",
                ContentAction.CREATE,
                payload={
                    "entity": "page",
                    "target_id": mint_page_id(
                        space_id=space_id, created_by=actor_user_id
                    ),
                    "title": clean_title,
                    "content": body,
                    "cover_image_url": cover,
                },
            )
        page = new_page(
            title=clean_title,
            content=body,
            created_by=actor_user_id,
            space_id=space_id,
            cover_image_url=cover,
            page_id=page_id,
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
        approved_by: str | None = None,
    ) -> Page:
        """Apply the sent fields. ``base_updated_at`` — the ``updated_at``
        the editor loaded — makes a concurrent save a
        :class:`PageStaleError` instead of a silent overwrite."""
        page = await self.get(space_id, page_id)
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "pages",
            ContentAction.EDIT,
            approved_by is None and page.created_by == actor_user_id,
        )
        if base_updated_at and base_updated_at != page.updated_at:
            raise PageStaleError(page)
        patch = _page_patch(
            title=title, content=content, cover_image_url=cover_image_url
        )
        if decision is AccessDecision.QUEUE:
            before = _page_dict(page)
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "pages",
                ContentAction.EDIT,
                payload={
                    "entity": "page",
                    "target_id": page.id,
                    "patch": patch,
                    "base_updated_at": page.updated_at,
                },
                snapshot={k: before[k] for k in patch},
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        fields: dict = {
            "updated_at": now_iso,
            "last_editor_user_id": actor_user_id,
            "last_edited_at": now_iso,
            **patch,
        }
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

    async def delete(
        self,
        space_id: str,
        page_id: str,
        *,
        actor_user_id: str,
        approved_by: str | None = None,
    ) -> None:
        page = await self.get(space_id, page_id)
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "pages",
            ContentAction.DELETE,
            approved_by is None and page.created_by == actor_user_id,
        )
        if decision is AccessDecision.QUEUE:
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "pages",
                ContentAction.DELETE,
                payload={"entity": "page", "target_id": page.id},
                snapshot=_page_dict(page),
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
        approved_by: str | None = None,
    ) -> str:
        """Pick a side of an open edit conflict (§4.4.4.1) — an EDIT of the
        page. Returns the body that is now current."""
        if self._conflicts is None:
            raise RuntimeError("SpacePageService: conflict service not attached")
        page = await self.get(space_id, page_id)
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "pages",
            ContentAction.EDIT,
            approved_by is None and page.created_by == actor_user_id,
        )
        if decision is AccessDecision.QUEUE:
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "pages",
                ContentAction.EDIT,
                payload={
                    "entity": "page",
                    "target_id": page.id,
                    "op": "resolve_conflict",
                    "resolution": resolution,
                    "merged_content": merged_content,
                    "base_updated_at": page.updated_at,
                },
                snapshot={"content": page.content},
            )
        return await self._conflicts.resolve_conflict(
            space_id=space_id,
            page_id=page_id,
            user_id=actor_user_id,
            resolution=resolution,
            merged_content=merged_content,
        )


def _page_patch(*, title: object, content: object, cover_image_url: object) -> dict:
    """The sent fields, normalised the way a direct save stores them."""
    patch: dict[str, Any] = {}
    if not isinstance(title, _Unset):
        patch["title"] = _title(title)
    if not isinstance(content, _Unset):
        patch["content"] = strip_signed_media_in_markdown(
            content if isinstance(content, str) else None
        )
    if not isinstance(cover_image_url, _Unset):
        patch["cover_image_url"] = strip_signature_query(
            cover_image_url if isinstance(cover_image_url, str) else None
        )
    return patch


def _page_dict(page: Page) -> dict[str, Any]:
    return {
        "title": page.title,
        "content": page.content,
        "cover_image_url": page.cover_image_url,
        "created_by": page.created_by,
        "updated_at": page.updated_at,
    }


_RESOLUTIONS = ("mine", "theirs", "merged_content")


class PageModerationHandler:
    """Queue items of a space's wiki (``pages`` create / edit / delete, a
    conflict resolution riding ``edit``). An edit whose page changed since
    it was submitted is :class:`ModerationStaleError` (409 ``STALE``) until
    the approver forces it — then latest wins."""

    __slots__ = ("_svc",)

    def __init__(self, service: SpacePageService) -> None:
        self._svc = service

    def validate(self, space: Space, payload: dict) -> dict:
        if payload.get("entity") != "page" or not isinstance(
            payload.get("target_id"), str
        ):
            raise ValueError("not a page submission")
        out: dict[str, Any] = {"entity": "page", "target_id": payload["target_id"]}
        if payload.get("op") == "resolve_conflict":
            if payload.get("resolution") not in _RESOLUTIONS:
                raise ValueError("unknown conflict resolution")
            out.update(
                op="resolve_conflict",
                resolution=payload["resolution"],
                merged_content=payload.get("merged_content"),
                base_updated_at=payload.get("base_updated_at"),
            )
        elif "patch" in payload:
            raw = payload["patch"]
            if not isinstance(raw, dict) or not raw:
                raise ValueError("an edit needs at least one field")
            out["patch"] = _page_patch(
                title=raw["title"] if "title" in raw else UNSET,
                content=raw["content"] if "content" in raw else UNSET,
                cover_image_url=(
                    raw["cover_image_url"] if "cover_image_url" in raw else UNSET
                ),
            )
            out["base_updated_at"] = payload.get("base_updated_at")
        elif "title" in payload:
            out["title"] = _title(payload["title"])
            out["content"] = strip_signed_media_in_markdown(
                payload.get("content") or ""
            )
            cover = payload.get("cover_image_url")
            out["cover_image_url"] = (
                strip_signature_query(cover) if isinstance(cover, str) else None
            )
        return out

    async def snapshot(self, space_id: str, target_id: str) -> dict | None:
        try:
            page = await self._svc.get(space_id, target_id)
        except KeyError:
            return None
        return _page_dict(page)

    async def apply(
        self, item: SpaceModerationItem, *, approved_by: str, force: bool
    ) -> ApplyResult:
        p = item.payload
        target = str(p["target_id"])
        live = await self.snapshot(item.space_id, target)
        match item.action:
            case ContentAction.CREATE.value:
                if live is None:
                    await self._svc.create(
                        item.space_id,
                        actor_user_id=item.submitted_by,
                        title=p["title"],
                        content=p.get("content", ""),
                        cover_image_url=p.get("cover_image_url"),
                        page_id=target,
                        approved_by=approved_by,
                    )
            case ContentAction.EDIT.value:
                if live is None:
                    raise ModerationTargetGoneError(target)
                base = p.get("base_updated_at")
                if base and live["updated_at"] != base and not force:
                    proposed = (
                        dict(p.get("patch") or {})
                        if p.get("op") != "resolve_conflict"
                        else {"content": p.get("merged_content")}
                    )
                    snapshot = item_payload_snapshot(item) or {}
                    raise ModerationStaleError(
                        current={k: live.get(k) for k in (proposed or live)},
                        proposed=proposed,
                        base=snapshot,
                    )
                if p.get("op") == "resolve_conflict":
                    try:
                        await self._svc.resolve_conflict(
                            item.space_id,
                            target,
                            actor_user_id=item.submitted_by,
                            resolution=p["resolution"],
                            merged_content=p.get("merged_content"),
                            approved_by=approved_by,
                        )
                    except NoActiveConflictError as exc:
                        raise ModerationTargetGoneError(target) from exc
                else:
                    await self._svc.update(
                        item.space_id,
                        target,
                        actor_user_id=item.submitted_by,
                        approved_by=approved_by,
                        **p["patch"],
                    )
            case ContentAction.DELETE.value:
                if live is not None:
                    await self._svc.delete(
                        item.space_id,
                        target,
                        actor_user_id=item.submitted_by,
                        approved_by=approved_by,
                    )
        return ApplyResult(target_id=target)

    def preview(self, item: SpaceModerationItem) -> dict:
        p = item.payload
        if item.action == ContentAction.DELETE.value:
            return item_payload_snapshot(item) or {}
        if item.action == ContentAction.EDIT.value:
            if p.get("op") == "resolve_conflict":
                return {
                    "resolution": p.get("resolution"),
                    "content": p.get("merged_content"),
                }
            return dict(p.get("patch") or {})
        return {
            "title": p.get("title"),
            "content": p.get("content"),
            "cover_image_url": p.get("cover_image_url"),
        }
