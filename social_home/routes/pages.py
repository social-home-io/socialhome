"""Page routes — /api/pages/* (section 5.2) and space page conflict resolution (section 4.4.4.1)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from aiohttp import web

from ..app_keys import (
    event_bus_key,
    page_conflict_service_key,
    page_repo_key,
    space_repo_key,
)
from ..domain.events import (
    PageConflictEmitted,
    PageCreated,
    PageDeleted,
    PageEditLockAcquired,
    PageEditLockReleased,
    PageUpdated,
)
from ..repositories.page_repo import (
    PageLockError,
    PageNotFoundError,
    PageVersion,
    new_page,
)
from ..security import error_response
from .base import BaseView


def _page_dict(page) -> dict:
    return {
        "id": page.id,
        "title": page.title,
        "content": page.content,
        "created_by": page.created_by,
        "created_at": page.created_at,
        "updated_at": page.updated_at,
        "last_editor_user_id": page.last_editor_user_id,
        "last_edited_at": page.last_edited_at,
        "space_id": page.space_id,
        "cover_image_url": page.cover_image_url,
        "locked_by": page.locked_by,
        "locked_at": page.locked_at,
        "lock_expires_at": page.lock_expires_at,
    }


async def _snapshot_version(repo, *, previous, editor_user_id: str) -> None:
    """Persist ``previous`` as a row in ``page_edit_history``.

    Called right before an edit or revert so the history always carries
    a copy of the pre-change state — the ``versions`` list then shows
    "what the page looked like before the current live body".
    """
    next_no = await repo.next_version_number(previous.id)
    await repo.save_version(
        PageVersion(
            id=uuid.uuid4().hex,
            page_id=previous.id,
            version=next_no,
            title=previous.title,
            content=previous.content,
            edited_by=editor_user_id,
            edited_at=datetime.now(timezone.utc).isoformat(),
            space_id=previous.space_id,
            cover_image_url=previous.cover_image_url,
        )
    )


class PageCollectionView(BaseView):
    """GET/POST /api/pages — list or create pages."""

    async def get(self) -> web.Response:
        self.user  # auth gate
        repo = self.svc(page_repo_key)
        pages = await repo.list(space_id=None)
        return web.json_response([_page_dict(p) for p in pages])

    async def post(self) -> web.Response:
        ctx = self.user
        await self.require_household_feature("pages")
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        body = await self.body()
        title = body.get("title", "").strip()
        content = body.get("content", "")
        if not title:
            return error_response(422, "UNPROCESSABLE", "title is required.")
        p = new_page(
            title=title,
            content=content,
            created_by=ctx.user_id,
        )
        p = await repo.save(p)
        await bus.publish(
            PageCreated(
                page_id=p.id,
                space_id=p.space_id,
                title=p.title,
                content=p.content,
            )
        )
        return web.json_response(_page_dict(p), status=201)


class PageDetailView(BaseView):
    """GET/PATCH/DELETE /api/pages/{id} — get, update, delete a page."""

    async def get(self) -> web.Response:
        self.user  # auth gate
        repo = self.svc(page_repo_key)
        page_id = self.match("id")
        p = await repo.get(page_id)
        if p is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        return web.json_response(_page_dict(p))

    async def patch(self) -> web.Response:
        ctx = self.user
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        page_id = self.match("id")
        body = await self.body()
        p = await repo.get(page_id)
        if p is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        # Optimistic-concurrency check — if the caller sends the
        # ``updated_at`` they last saw and it no longer matches the
        # DB row, someone else has edited since. The client turns this
        # 409 into the side-by-side conflict UI (§23.72). We also
        # emit a WS ``page.conflict`` so any still-open editor tab
        # surfaces the conflict without needing to retry the PATCH.
        base = body.get("base_updated_at")
        if base and base != p.updated_at:
            theirs_by = p.last_editor_user_id or p.created_by
            await bus.publish(
                PageConflictEmitted(
                    page_id=p.id,
                    space_id=p.space_id,
                    theirs=p.content,
                    theirs_by=theirs_by,
                )
            )
            return web.json_response(
                {"error": "stale_update", "current": _page_dict(p)},
                status=409,
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        kwargs: dict = {
            "updated_at": now_iso,
            "last_editor_user_id": ctx.user_id,
            "last_edited_at": now_iso,
        }
        if "title" in body:
            title = body["title"].strip()
            if not title:
                return error_response(422, "UNPROCESSABLE", "title must not be empty.")
            kwargs["title"] = title
        if "content" in body:
            kwargs["content"] = body["content"]
        if "cover_image_url" in body:
            kwargs["cover_image_url"] = body["cover_image_url"]
        updated = replace(p, **kwargs)
        updated = await repo.save(updated)
        await _snapshot_version(repo, previous=p, editor_user_id=ctx.user_id)
        await bus.publish(
            PageUpdated(
                page_id=updated.id,
                space_id=updated.space_id,
                title=updated.title,
                content=updated.content,
            )
        )
        return web.json_response(_page_dict(updated))

    async def delete(self) -> web.Response:
        self.user  # auth gate
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        page_id = self.match("id")
        p = await repo.get(page_id)
        if p is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        await repo.delete(page_id)
        await bus.publish(PageDeleted(page_id=page_id))
        return web.json_response({"ok": True})


class PageLockView(BaseView):
    """GET/POST/DELETE /api/pages/{id}/lock — inspect, acquire, release."""

    async def get(self) -> web.Response:
        self.user  # auth gate
        repo = self.svc(page_repo_key)
        lock = await repo.get_lock(self.match("id"))
        return web.json_response(lock)

    async def post(self) -> web.Response:
        ctx = self.user
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        page_id = self.match("id")
        try:
            await repo.acquire_lock(page_id, ctx.user_id)
        except PageLockError as exc:
            current = await repo.get_lock(page_id)
            return web.json_response(
                {"error": "lock_held", "detail": str(exc), "current": current},
                status=409,
            )
        except PageNotFoundError:
            return error_response(404, "NOT_FOUND", "Page not found.")
        lock = await repo.get_lock(page_id)
        p = await repo.get(page_id)
        await bus.publish(
            PageEditLockAcquired(
                page_id=page_id,
                space_id=p.space_id if p else None,
                locked_by=ctx.user_id,
                lock_expires_at=(lock or {}).get("lock_expires_at") or "",
            )
        )
        return web.json_response({"ok": True, "locked_by": ctx.user_id})

    async def delete(self) -> web.Response:
        ctx = self.user
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        page_id = self.match("id")
        p = await repo.get(page_id)
        await repo.release_lock(page_id, ctx.user_id)
        await bus.publish(
            PageEditLockReleased(
                page_id=page_id,
                space_id=p.space_id if p else None,
            )
        )
        return web.json_response({"ok": True})


class PageLockRefreshView(BaseView):
    """POST /api/pages/{id}/lock/refresh — extend the caller's lock.

    Returns 204 on success, 409 if another editor now holds the lock,
    404 if the page is missing. Clients heartbeat every 30 s.
    """

    async def post(self) -> web.Response:
        ctx = self.user
        repo = self.svc(page_repo_key)
        page_id = self.match("id")
        try:
            await repo.refresh_lock(page_id, ctx.user_id)
        except PageLockError as exc:
            current = await repo.get_lock(page_id)
            return web.json_response(
                {"error": "lock_held", "detail": str(exc), "current": current},
                status=409,
            )
        except PageNotFoundError:
            return error_response(404, "NOT_FOUND", "Page not found.")
        return web.Response(status=204)


class PageVersionView(BaseView):
    """GET /api/pages/{id}/versions — list edit history."""

    async def get(self) -> web.Response:
        self.user  # auth gate
        repo = self.svc(page_repo_key)
        page_id = self.match("id")
        versions = await repo.list_versions(page_id)
        return web.json_response(
            [
                {
                    "id": v.id,
                    "page_id": v.page_id,
                    "version": v.version,
                    "title": v.title,
                    "content": v.content,
                    "edited_by": v.edited_by,
                    "edited_at": v.edited_at,
                    "space_id": v.space_id,
                    "cover_image_url": v.cover_image_url,
                }
                for v in versions
            ]
        )


class PageRevertView(BaseView):
    """POST /api/pages/{id}/revert — revert to a previous version (admin)."""

    async def post(self) -> web.Response:
        ctx = self.user
        if not ctx.is_admin:
            return error_response(
                403,
                "FORBIDDEN",
                "Only household admins can revert a page.",
            )
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        page_id = self.match("id")
        body = await self.body()
        try:
            version_num = int(body["version"])
        except TypeError, ValueError:
            return error_response(
                422,
                "UNPROCESSABLE",
                "version must be an integer.",
            )
        current = await repo.get(page_id)
        if current is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        target = next(
            (v for v in await repo.list_versions(page_id) if v.version == version_num),
            None,
        )
        if target is None:
            return error_response(404, "NOT_FOUND", f"Version {version_num} not found.")
        await _snapshot_version(repo, previous=current, editor_user_id=ctx.user_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        reverted = replace(
            current,
            title=target.title,
            content=target.content,
            cover_image_url=target.cover_image_url,
            updated_at=now_iso,
            last_editor_user_id=ctx.user_id,
            last_edited_at=now_iso,
        )
        await repo.save(reverted)
        await bus.publish(
            PageUpdated(
                page_id=reverted.id,
                space_id=reverted.space_id,
                title=reverted.title,
                content=reverted.content,
            )
        )
        return web.json_response(_page_dict(reverted))


class PageDeleteRequestView(BaseView):
    """POST /api/pages/{id}/delete-request — member asks for deletion."""

    async def post(self) -> web.Response:
        ctx = self.user
        repo = self.svc(page_repo_key)
        page_id = self.match("id")
        p = await repo.get(page_id)
        if p is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        await repo.request_delete(page_id, ctx.user_id)
        return web.json_response(
            {
                "ok": True,
                "requested_by": ctx.user_id,
                "status": "awaiting_approval",
            }
        )


class PageDeleteApproveView(BaseView):
    """POST /api/pages/{id}/delete-approve — admin confirms the delete."""

    async def post(self) -> web.Response:
        ctx = self.user
        if not ctx.is_admin:
            return error_response(
                403,
                "FORBIDDEN",
                "Only household admins can approve a delete.",
            )
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        page_id = self.match("id")
        p = await repo.get(page_id)
        if p is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        if p.delete_requested_by is None:
            return error_response(
                409,
                "NOT_REQUESTED",
                "No pending delete request for this page.",
            )
        if p.delete_requested_by == ctx.user_id:
            return error_response(
                409,
                "SELF_APPROVE",
                "The user who requested deletion cannot approve it.",
            )
        await repo.approve_delete(page_id, ctx.user_id)
        await repo.delete(page_id)
        await bus.publish(PageDeleted(page_id=page_id))
        return web.json_response({"ok": True, "deleted": True})


class PageDeleteCancelView(BaseView):
    """POST /api/pages/{id}/delete-cancel — drop a pending delete request."""

    async def post(self) -> web.Response:
        ctx = self.user
        repo = self.svc(page_repo_key)
        page_id = self.match("id")
        p = await repo.get(page_id)
        if p is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        # Only the requester or an admin can cancel.
        if p.delete_requested_by not in (None, ctx.user_id) and not ctx.is_admin:
            return error_response(
                403,
                "FORBIDDEN",
                "Only the requester or an admin can cancel this delete.",
            )
        await repo.clear_delete_request(page_id)
        return web.json_response({"ok": True, "status": "cancelled"})


class SpacePageCollectionView(BaseView):
    """GET/POST /api/spaces/{id}/pages — list or create space pages."""

    async def _require_space_member(self, space_id: str, user_id: str) -> bool:
        space_repo = self.svc(space_repo_key)
        member = await space_repo.get_member(space_id, user_id)
        return member is not None

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_space_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        repo = self.svc(page_repo_key)
        pages = await repo.list(space_id=space_id)
        return web.json_response([_page_dict(p) for p in pages])

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_space_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        await self.require_household_feature("pages")
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        body = await self.body()
        title = body.get("title", "").strip()
        content = body.get("content", "")
        if not title:
            return error_response(422, "UNPROCESSABLE", "title is required.")
        p = new_page(
            title=title,
            content=content,
            created_by=ctx.user_id,
            space_id=space_id,
        )
        p = await repo.save(p)
        await bus.publish(
            PageCreated(
                page_id=p.id,
                space_id=p.space_id,
                title=p.title,
                content=p.content,
            )
        )
        return web.json_response(_page_dict(p), status=201)


class SpacePageDetailView(BaseView):
    """GET/PATCH/DELETE /api/spaces/{id}/pages/{pid}."""

    async def _require_space_member(self, space_id: str, user_id: str) -> bool:
        space_repo = self.svc(space_repo_key)
        member = await space_repo.get_member(space_id, user_id)
        return member is not None

    async def _load(self, space_id: str, page_id: str):
        repo = self.svc(page_repo_key)
        p = await repo.get(page_id)
        if p is None or p.space_id != space_id:
            return None
        return p

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_space_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        p = await self._load(space_id, self.match("pid"))
        if p is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        return web.json_response(_page_dict(p))

    async def patch(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_space_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        p = await self._load(space_id, self.match("pid"))
        if p is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        body = await self.body()
        base = body.get("base_updated_at")
        if base and base != p.updated_at:
            theirs_by = p.last_editor_user_id or p.created_by
            await bus.publish(
                PageConflictEmitted(
                    page_id=p.id,
                    space_id=p.space_id,
                    theirs=p.content,
                    theirs_by=theirs_by,
                )
            )
            return web.json_response(
                {"error": "stale_update", "current": _page_dict(p)},
                status=409,
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        kwargs: dict = {
            "updated_at": now_iso,
            "last_editor_user_id": ctx.user_id,
            "last_edited_at": now_iso,
        }
        if "title" in body:
            title = body["title"].strip()
            if not title:
                return error_response(422, "UNPROCESSABLE", "title must not be empty.")
            kwargs["title"] = title
        if "content" in body:
            kwargs["content"] = body["content"]
        if "cover_image_url" in body:
            kwargs["cover_image_url"] = body["cover_image_url"]
        updated = replace(p, **kwargs)
        updated = await repo.save(updated)
        await _snapshot_version(repo, previous=p, editor_user_id=ctx.user_id)
        await bus.publish(
            PageUpdated(
                page_id=updated.id,
                space_id=updated.space_id,
                title=updated.title,
                content=updated.content,
            )
        )
        return web.json_response(_page_dict(updated))

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_space_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        p = await self._load(space_id, self.match("pid"))
        if p is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        await repo.delete(p.id)
        await bus.publish(PageDeleted(page_id=p.id))
        return web.json_response({"ok": True})


class PageConflictView(BaseView):
    """POST /api/spaces/{id}/pages/{pid}/resolve-conflict (section 4.4.4.1)."""

    async def post(self) -> web.Response:
        ctx = self.user
        conflict_svc = self.svc(page_conflict_service_key)
        space_id = self.match("id")
        page_id = self.match("pid")
        body = await self.body()
        resolution = str(body.get("resolution") or "")
        merged = body.get("content")
        if resolution not in ("mine", "theirs", "merged_content"):
            return error_response(
                422,
                "UNPROCESSABLE",
                "resolution must be 'mine', 'theirs', or 'merged_content'.",
            )
        if resolution == "merged_content" and not merged:
            return error_response(
                422,
                "UNPROCESSABLE",
                "content is required when resolution is 'merged_content'.",
            )
        new_body = await conflict_svc.resolve_conflict(
            space_id=space_id,
            page_id=page_id,
            user_id=ctx.user_id,
            resolution=resolution,
            merged_content=merged,
        )
        return web.json_response({"ok": True, "content": new_body})
