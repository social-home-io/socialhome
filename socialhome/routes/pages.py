"""Page routes — /api/pages/* (section 5.2) and space page conflict resolution (section 4.4.4.1).

Scope (§24.11): the household ``/api/pages[/{id}…]`` routes only ever
act on household pages (the ``pages`` table) — a space page id there is
404, member or not. The ``/api/spaces/{id}/pages[/{pid}…]`` routes check
membership of the PATH space (403 before any feature or id check), honour
the per-space ``pages`` feature, and only reach that space's pages (an id
from another space or the household is 404). Space writes additionally
need a writable seat (subscribers → 403) in a non-archived space (→ 403).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from aiohttp import web

from ..app_keys import (
    event_bus_key,
    media_signer_key,
    page_repo_key,
    space_page_service_key,
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
from ..media_signer import (
    MediaUrlSigner,
    sign_media_urls_in_markdown,
    strip_signature_query,
    strip_signed_media_in_markdown,
)
from ..repositories.page_repo import (
    PageLockError,
    PageNotFoundError,
    PageVersion,
    new_page,
)
from ..security import error_response
from ..services.space_page_service import PageStaleError, snapshot_page_version
from .base import BaseView


def _signed_page_dict(request: web.Request, page) -> dict:
    """:func:`_page_dict` with the request's media-URL signer applied.

    Convenience wrapper for the route handlers — keeps the signer
    plumbing in one place.
    """
    signer = request.app.get(media_signer_key)
    return _page_dict(page, signer=signer)


def _page_dict(page, *, signer: MediaUrlSigner | None = None) -> dict:
    """Serialise a page row.

    When ``signer`` is provided every ``/api/media/{filename}`` URL in
    the markdown ``content`` (and the scalar ``cover_image_url``) is
    re-signed with a fresh 1h-TTL signature so the SPA can render
    images without an ``Authorization`` header. Storage stays
    canonical — the PATCH/POST handlers strip the signature on save.
    """
    content = page.content
    cover = page.cover_image_url
    if signer is not None:
        if content:
            content = sign_media_urls_in_markdown(content, signer)
        if cover and (cover.startswith("/api/") or cover.startswith("api/")):
            cover = signer.sign(cover)
    return {
        "id": page.id,
        "title": page.title,
        "content": content,
        "created_by": page.created_by,
        "created_at": page.created_at,
        "updated_at": page.updated_at,
        "last_editor_user_id": page.last_editor_user_id,
        "last_edited_at": page.last_edited_at,
        "space_id": page.space_id,
        "cover_image_url": cover,
        "locked_by": page.locked_by,
        "locked_at": page.locked_at,
        "lock_expires_at": page.lock_expires_at,
    }


def _version_dict(v: PageVersion) -> dict:
    return {
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


class PageCollectionView(BaseView):
    """GET/POST /api/pages — list or create pages."""

    async def get(self) -> web.Response:
        self.user  # auth gate
        repo = self.svc(page_repo_key)
        pages = await repo.list(space_id=None)
        return web.json_response([_signed_page_dict(self.request, p) for p in pages])

    async def post(self) -> web.Response:
        ctx = self.user
        await self.require_household_feature("pages")
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        body = await self.body()
        title = body.get("title", "").strip()
        # Strip ``?exp=&sig=…`` from any /api/media/ URL the editor might
        # have echoed back — bodies are persisted canonical so the server
        # can re-sign them on each read with a fresh signature.
        content = strip_signed_media_in_markdown(body.get("content", "")) or ""
        if not title:
            return error_response(422, "UNPROCESSABLE", "title is required.")
        p = new_page(
            title=title,
            content=content,
            created_by=ctx.user_id,
        )
        await repo.save(p, space_id=p.space_id)
        await bus.publish(
            PageCreated(
                page_id=p.id,
                space_id=p.space_id,
                title=p.title,
                content=p.content,
            )
        )
        return web.json_response(_signed_page_dict(self.request, p), status=201)


class PageDetailView(BaseView):
    """GET/PATCH/DELETE /api/pages/{id} — get, update, delete a page."""

    async def get(self) -> web.Response:
        self.user  # auth gate
        repo = self.svc(page_repo_key)
        page_id = self.match("id")
        p = await repo.get_household_page(page_id)
        if p is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        return web.json_response(_signed_page_dict(self.request, p))

    async def patch(self) -> web.Response:
        ctx = self.user
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        page_id = self.match("id")
        body = await self.body()
        p = await repo.get_household_page(page_id)
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
                {
                    "error": "stale_update",
                    "current": _signed_page_dict(self.request, p),
                },
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
            kwargs["content"] = strip_signed_media_in_markdown(body["content"])
        if "cover_image_url" in body:
            kwargs["cover_image_url"] = strip_signature_query(body["cover_image_url"])
        updated = replace(p, **kwargs)
        await repo.save(updated, space_id=updated.space_id)
        await snapshot_page_version(repo, previous=p, editor_user_id=ctx.user_id)
        await bus.publish(
            PageUpdated(
                page_id=updated.id,
                space_id=updated.space_id,
                title=updated.title,
                content=updated.content,
            )
        )
        return web.json_response(_signed_page_dict(self.request, updated))

    async def delete(self) -> web.Response:
        self.user  # auth gate
        repo = self.svc(page_repo_key)
        bus = self.svc(event_bus_key)
        page_id = self.match("id")
        if await repo.get_household_page(page_id) is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        await repo.delete(page_id, space_id=None)
        await bus.publish(PageDeleted(page_id=page_id, space_id=None))
        return web.json_response({"ok": True})


class PageLockView(BaseView):
    """GET/POST/DELETE /api/pages/{id}/lock — inspect, acquire, release."""

    async def get(self) -> web.Response:
        self.user  # auth gate
        repo = self.svc(page_repo_key)
        page_id = self.match("id")
        if await repo.get_household_page(page_id) is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        lock = await repo.get_lock(page_id)
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
        await bus.publish(
            PageEditLockAcquired(
                page_id=page_id,
                space_id=None,
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
        if await repo.get_household_page(page_id) is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        await repo.release_lock(page_id, ctx.user_id)
        await bus.publish(PageEditLockReleased(page_id=page_id, space_id=None))
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
        if await repo.get_household_page(page_id) is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        versions = await repo.list_versions(page_id, space_id=None)
        return web.json_response([_version_dict(v) for v in versions])


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
        current = await repo.get_household_page(page_id)
        if current is None:
            return error_response(404, "NOT_FOUND", "Page not found.")
        target = next(
            (
                v
                for v in await repo.list_versions(page_id, space_id=None)
                if v.version == version_num
            ),
            None,
        )
        if target is None:
            return error_response(404, "NOT_FOUND", f"Version {version_num} not found.")
        await snapshot_page_version(repo, previous=current, editor_user_id=ctx.user_id)
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
        await repo.save(reverted, space_id=None)
        await bus.publish(
            PageUpdated(
                page_id=reverted.id,
                space_id=reverted.space_id,
                title=reverted.title,
                content=reverted.content,
            )
        )
        return web.json_response(_signed_page_dict(self.request, reverted))


class PageDeleteRequestView(BaseView):
    """POST /api/pages/{id}/delete-request — member asks for deletion."""

    async def post(self) -> web.Response:
        ctx = self.user
        repo = self.svc(page_repo_key)
        page_id = self.match("id")
        p = await repo.get_household_page(page_id)
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
        p = await repo.get_household_page(page_id)
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
        await repo.delete(page_id, space_id=None)
        await bus.publish(PageDeleted(page_id=page_id, space_id=None))
        return web.json_response({"ok": True, "deleted": True})


class PageDeleteCancelView(BaseView):
    """POST /api/pages/{id}/delete-cancel — drop a pending delete request."""

    async def post(self) -> web.Response:
        ctx = self.user
        repo = self.svc(page_repo_key)
        page_id = self.match("id")
        p = await repo.get_household_page(page_id)
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


class _SpacePagesBase(BaseView):
    """Shared membership / feature / writer gate for space pages. The page
    rules themselves live in :class:`SpacePageService`."""

    async def _require_space_member(
        self, space_id: str, user_id: str, *, write: bool = False
    ) -> bool:
        """``False`` (→ 403) unless ``user_id`` is a member of the PATH
        space; then the space's ``pages`` feature (403
        ``FEATURE_DISABLED``). ``write`` additionally refuses an
        archived space (read-only) and a read-only subscriber (403)."""
        space_repo = self.svc(space_repo_key)
        member = await space_repo.get_member(space_id, user_id)
        if member is None:
            return False
        await self.require_space_feature(space_id, "pages")
        if write:
            await self.svc(space_page_service_key).require_writer(space_id, user_id)
        return True


class SpacePageCollectionView(_SpacePagesBase):
    """GET/POST /api/spaces/{id}/pages — list or create space pages."""

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_space_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        pages = await self.svc(space_page_service_key).list(space_id)
        return web.json_response([_signed_page_dict(self.request, p) for p in pages])

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_space_member(space_id, ctx.user_id, write=True):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        await self.require_household_feature("pages")
        body = await self.body()
        if not str(body.get("title") or "").strip():
            return error_response(422, "UNPROCESSABLE", "title is required.")
        p = await self.svc(space_page_service_key).create(
            space_id,
            actor_user_id=ctx.user_id,
            title=body.get("title"),
            content=body.get("content", ""),
        )
        return web.json_response(_signed_page_dict(self.request, p), status=201)


class SpacePageDetailView(_SpacePagesBase):
    """GET/PATCH/DELETE /api/spaces/{id}/pages/{pid}."""

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_space_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        p = await self.svc(space_page_service_key).get(space_id, self.match("pid"))
        return web.json_response(_signed_page_dict(self.request, p))

    async def patch(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_space_member(space_id, ctx.user_id, write=True):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        body = await self.body()
        if "title" in body and not str(body["title"] or "").strip():
            return error_response(422, "UNPROCESSABLE", "title must not be empty.")
        fields = {
            k: body[k] for k in ("title", "content", "cover_image_url") if k in body
        }
        try:
            updated = await self.svc(space_page_service_key).update(
                space_id,
                self.match("pid"),
                actor_user_id=ctx.user_id,
                base_updated_at=body.get("base_updated_at"),
                **fields,
            )
        except PageStaleError as exc:
            # Someone else saved since this editor loaded the page: the
            # client turns the 409 into the side-by-side conflict UI
            # (§23.72), and a WS ``page.conflict`` reaches any other tab.
            p = exc.current
            await self.svc(event_bus_key).publish(
                PageConflictEmitted(
                    page_id=p.id,
                    space_id=p.space_id,
                    theirs=p.content,
                    theirs_by=p.last_editor_user_id or p.created_by,
                )
            )
            return web.json_response(
                {
                    "error": "stale_update",
                    "current": _signed_page_dict(self.request, p),
                },
                status=409,
            )
        return web.json_response(_signed_page_dict(self.request, updated))

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_space_member(space_id, ctx.user_id, write=True):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        await self.svc(space_page_service_key).delete(
            space_id, self.match("pid"), actor_user_id=ctx.user_id
        )
        return web.json_response({"ok": True})


class SpacePageVersionView(_SpacePagesBase):
    """GET /api/spaces/{id}/pages/{pid}/versions — a space page's edit history.

    Read-only (no space lock / revert surface). Member-only: a
    non-member is 403 before the feature (403 ``FEATURE_DISABLED``) and
    id (404) checks; a ``pid`` that isn't a page of space ``id`` is 404.
    Only the history recorded under this space is returned.
    """

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_space_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        versions = await self.svc(space_page_service_key).versions(
            space_id, self.match("pid")
        )
        return web.json_response([_version_dict(v) for v in versions])


class PageConflictView(_SpacePagesBase):
    """POST /api/spaces/{id}/pages/{pid}/resolve-conflict (section 4.4.4.1)."""

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        page_id = self.match("pid")
        if not await self._require_space_member(space_id, ctx.user_id, write=True):
            return error_response(403, "FORBIDDEN", "Not a space member.")
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
        new_body = await self.svc(space_page_service_key).resolve_conflict(
            space_id,
            page_id,
            actor_user_id=ctx.user_id,
            resolution=resolution,
            merged_content=merged,
        )
        return web.json_response({"ok": True, "content": new_body})
