"""Sticky-note routes — /api/stickies/* (§19).

Two surfaces:

* ``/api/stickies`` — household sticky board. Any household member with
  the ``stickies`` feature toggle enabled can add/edit/delete.
* ``/api/spaces/{id}/stickies`` — per-space sticky board, scoped to
  members. Federates via ``SPACE_STICKY_*`` events.

Scope (§24.11): the household routes only ever touch household stickies
(``space_id IS NULL``) and the space routes only that space's stickies —
an id from any other scope is 404. Space writes additionally require a
writable seat (subscribers → 403) in a non-archived space (→ 403), and
every space handler honours the per-space ``stickies`` feature toggle
(only that one — the household toggle gates the household board).
The rules live in :class:`StickyService`; handlers stay thin.

Both surfaces publish :class:`StickyCreated` / :class:`StickyUpdated` /
:class:`StickyDeleted` so :class:`RealtimeService` can fan out WS frames
(``sticky.created`` / ``sticky.updated`` / ``sticky.deleted``) to other
open tabs + co-members.
"""

from __future__ import annotations

from aiohttp import web

from ..app_keys import space_repo_key, sticky_service_key
from ..domain.sticky import DEFAULT_STICKY_COLOR, Sticky
from ..security import error_response
from .base import BaseView


def _sticky_dict(s: Sticky) -> dict:
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


def _create_kwargs(body: object) -> dict:
    if not isinstance(body, dict):
        raise ValueError("sticky body must be a JSON object")
    return {
        "content": body.get("content", ""),
        "color": body.get("color", DEFAULT_STICKY_COLOR),
        "position_x": body.get("position_x", 0.0),
        "position_y": body.get("position_y", 0.0),
    }


def _update_kwargs(body: object) -> dict:
    if not isinstance(body, dict):
        raise ValueError("sticky body must be a JSON object")
    return {
        "content": body.get("content"),
        "color": body.get("color"),
        "position_x": body.get("position_x"),
        "position_y": body.get("position_y"),
    }


class StickyCollectionView(BaseView):
    """``GET /api/stickies`` + ``POST /api/stickies`` — household scope."""

    async def get(self) -> web.Response:
        self.user
        await self.require_household_feature("stickies")
        stickies = await self.svc(sticky_service_key).list(space_id=None)
        return self._json([_sticky_dict(s) for s in stickies])

    async def post(self) -> web.Response:
        ctx = self.user
        await self.require_household_feature("stickies")
        body = await self.body()
        sticky = await self.svc(sticky_service_key).create(
            author=ctx.user_id, space_id=None, **_create_kwargs(body)
        )
        return self._json(_sticky_dict(sticky), status=201)


class StickyDetailView(BaseView):
    """``PATCH /api/stickies/{id}`` + ``DELETE /api/stickies/{id}``.

    Household board only — a space sticky id here is 404 (the service
    looks the id up with ``space_id IS NULL``).
    """

    async def patch(self) -> web.Response:
        ctx = self.user
        await self.require_household_feature("stickies")
        body = await self.body()
        sticky = await self.svc(sticky_service_key).update(
            self.match("id"),
            space_id=None,
            actor_user_id=ctx.user_id,
            **_update_kwargs(body),
        )
        return self._json(_sticky_dict(sticky))

    async def delete(self) -> web.Response:
        ctx = self.user
        await self.require_household_feature("stickies")
        await self.svc(sticky_service_key).delete(
            self.match("id"), space_id=None, actor_user_id=ctx.user_id
        )
        return self._json({"ok": True})


# ─── Space-scoped board ─────────────────────────────────────────────────
#
# Every handler passes the PATH ``space_id`` into the service, which
# answers 404 (KeyError) for a sticky id living in another space or on
# the household board. Write handlers additionally reject read-only
# subscribers and archived spaces (403) via ``require_writer``.


class _SpaceStickiesBase(BaseView):
    async def _require_member(
        self, space_id: str, user_id: str, *, write: bool = False
    ) -> bool:
        space_repo = self.svc(space_repo_key)
        if await space_repo.get_member(space_id, user_id) is None:
            return False
        await self.require_space_feature(space_id, "stickies")
        if write:
            await self.svc(sticky_service_key).require_writer(space_id, user_id)
        return True


class SpaceStickyCollectionView(_SpaceStickiesBase):
    """``GET /api/spaces/{id}/stickies`` + ``POST``."""

    async def get(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        stickies = await self.svc(sticky_service_key).list(space_id=space_id)
        return self._json([_sticky_dict(s) for s in stickies])

    async def post(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id, write=True):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        body = await self.body()
        sticky = await self.svc(sticky_service_key).create(
            author=ctx.user_id, space_id=space_id, **_create_kwargs(body)
        )
        return self._json(_sticky_dict(sticky), status=201)


class SpaceStickyDetailView(_SpaceStickiesBase):
    """``PATCH/DELETE /api/spaces/{id}/stickies/{sid}``."""

    async def patch(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id, write=True):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        body = await self.body()
        sticky = await self.svc(sticky_service_key).update(
            self.match("sid"),
            space_id=space_id,
            actor_user_id=ctx.user_id,
            **_update_kwargs(body),
        )
        return self._json(_sticky_dict(sticky))

    async def delete(self) -> web.Response:
        ctx = self.user
        space_id = self.match("id")
        if not await self._require_member(space_id, ctx.user_id, write=True):
            return error_response(403, "FORBIDDEN", "Not a space member.")
        await self.svc(sticky_service_key).delete(
            self.match("sid"), space_id=space_id, actor_user_id=ctx.user_id
        )
        return self._json({"ok": True})
