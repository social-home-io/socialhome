"""Sticky-note routes — /api/stickies/* (§19).

Two surfaces:

* ``/api/stickies`` — household sticky board. Any household member with
  the ``stickies`` feature toggle enabled can add/edit/delete.
* ``/api/spaces/{id}/stickies`` — per-space sticky board, scoped to
  members. Federates via ``SPACE_STICKY_*`` events.

Both surfaces publish :class:`StickyCreated` / :class:`StickyUpdated` /
:class:`StickyDeleted` so :class:`RealtimeService` can fan out WS frames
(``sticky.created`` / ``sticky.updated`` / ``sticky.deleted``) to other
open tabs + co-members.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import (
    event_bus_key,
    space_repo_key,
    sticky_repo_key,
)
from ..domain.events import StickyCreated, StickyDeleted, StickyUpdated
from ..security import error_response
from .base import BaseView


def _sticky_dict(s) -> dict:
    return {
        "id": s.id,
        "author": s.author,
        "content": s.content,
        "color": s.color,
        "position_x": s.position_x,
        "position_y": s.position_y,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "space_id": s.space_id,
    }


class StickyCollectionView(BaseView):
    """``GET /api/stickies`` + ``POST /api/stickies`` — household scope."""

    async def get(self) -> web.Response:
        self.user
        repo = self.svc(sticky_repo_key)
        stickies = await repo.list(space_id=None)
        return self._json([_sticky_dict(s) for s in stickies])

    async def post(self) -> web.Response:
        ctx = self.user
        await self.require_household_feature("stickies")
        body = await self.body()
        bus = self.svc(event_bus_key)
        sticky = await self.svc(sticky_repo_key).add(
            author=ctx.user_id,
            content=body.get("content", ""),
            color=body.get("color", "#FFF9B1"),
            position_x=float(body.get("position_x", 0.0)),
            position_y=float(body.get("position_y", 0.0)),
            space_id=None,
        )
        await bus.publish(
            StickyCreated(
                sticky_id=sticky.id,
                space_id=None,
                author=sticky.author,
                content=sticky.content,
                color=sticky.color,
                position_x=sticky.position_x,
                position_y=sticky.position_y,
            )
        )
        return self._json(_sticky_dict(sticky), status=201)


class StickyDetailView(BaseView):
    """``PATCH /api/stickies/{id}`` + ``DELETE /api/stickies/{id}``."""

    async def patch(self) -> web.Response:
        self.user
        sticky_id = self.match("id")
        body = await self.body()
        repo = self.svc(sticky_repo_key)
        bus = self.svc(event_bus_key)

        sticky = await repo.get(sticky_id)
        if sticky is None:
            return error_response(404, "NOT_FOUND", "Sticky not found.")

        if "content" in body:
            await repo.update_content(sticky_id, body["content"])
        if "position_x" in body or "position_y" in body:
            x = float(body.get("position_x", sticky.position_x))
            y = float(body.get("position_y", sticky.position_y))
            await repo.update_position(sticky_id, x, y)
        if "color" in body:
            await repo.update_color(sticky_id, body["color"])

        updated = await repo.get(sticky_id)
        if updated is None:
            return error_response(404, "NOT_FOUND", "Sticky not found.")
        await bus.publish(
            StickyUpdated(
                sticky_id=updated.id,
                space_id=updated.space_id,
                content=updated.content,
                color=updated.color,
                position_x=updated.position_x,
                position_y=updated.position_y,
            )
        )
        return self._json(_sticky_dict(updated))

    async def delete(self) -> web.Response:
        self.user
        sticky_id = self.match("id")
        repo = self.svc(sticky_repo_key)
        bus = self.svc(event_bus_key)
        sticky = await repo.get(sticky_id)
        if sticky is None:
            return error_response(404, "NOT_FOUND", "Sticky not found.")
        await repo.delete(sticky_id)
        await bus.publish(
            StickyDeleted(
                sticky_id=sticky_id,
                space_id=sticky.space_id,
            )
        )
        return self._json({"ok": True})


# ─── Space-scoped board ─────────────────────────────────────────────────


class SpaceStickyCollectionView(BaseView):
    """``GET /api/spaces/{id}/stickies`` + ``POST``."""

    async def _require_member(self, space_id: str, user_id: str) -> bool:
        space_repo = self.svc(space_repo_key)
        return await space_repo.get_member(space_id, user_id) is not None

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        repo = self.svc(sticky_repo_key)
        stickies = await repo.list(space_id=space_id)
        return self._json([_sticky_dict(s) for s in stickies])

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        await self.require_household_feature("stickies")
        body = await self.body()
        bus = self.svc(event_bus_key)
        sticky = await self.svc(sticky_repo_key).add(
            author=ctx.user_id,
            content=body.get("content", ""),
            color=body.get("color", "#FFF9B1"),
            position_x=float(body.get("position_x", 0.0)),
            position_y=float(body.get("position_y", 0.0)),
            space_id=space_id,
        )
        await bus.publish(
            StickyCreated(
                sticky_id=sticky.id,
                space_id=space_id,
                author=sticky.author,
                content=sticky.content,
                color=sticky.color,
                position_x=sticky.position_x,
                position_y=sticky.position_y,
            )
        )
        return self._json(_sticky_dict(sticky), status=201)


class SpaceStickyDetailView(BaseView):
    """``PATCH/DELETE /api/spaces/{id}/stickies/{sid}``."""

    async def _require_member(self, space_id: str, user_id: str) -> bool:
        space_repo = self.svc(space_repo_key)
        return await space_repo.get_member(space_id, user_id) is not None

    async def _load(self, space_id: str, sticky_id: str):
        repo = self.svc(sticky_repo_key)
        sticky = await repo.get(sticky_id)
        if sticky is None or sticky.space_id != space_id:
            return None
        return sticky

    async def patch(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        repo = self.svc(sticky_repo_key)
        bus = self.svc(event_bus_key)
        sticky = await self._load(space_id, self.match("sid"))
        if sticky is None:
            return error_response(404, "NOT_FOUND", "Sticky not found.")

        body = await self.body()
        if "content" in body:
            await repo.update_content(sticky.id, body["content"])
        if "position_x" in body or "position_y" in body:
            x = float(body.get("position_x", sticky.position_x))
            y = float(body.get("position_y", sticky.position_y))
            await repo.update_position(sticky.id, x, y)
        if "color" in body:
            await repo.update_color(sticky.id, body["color"])

        updated = await repo.get(sticky.id)
        if updated is None:
            return error_response(404, "NOT_FOUND", "Sticky not found.")
        await bus.publish(
            StickyUpdated(
                sticky_id=updated.id,
                space_id=updated.space_id,
                content=updated.content,
                color=updated.color,
                position_x=updated.position_x,
                position_y=updated.position_y,
            )
        )
        return self._json(_sticky_dict(updated))

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        repo = self.svc(sticky_repo_key)
        bus = self.svc(event_bus_key)
        sticky = await self._load(space_id, self.match("sid"))
        if sticky is None:
            return error_response(404, "NOT_FOUND", "Sticky not found.")
        await repo.delete(sticky.id)
        await bus.publish(
            StickyDeleted(
                sticky_id=sticky.id,
                space_id=space_id,
            )
        )
        return self._json({"ok": True})
