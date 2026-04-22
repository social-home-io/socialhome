"""Shopping list routes — /api/shopping/* (§23.120)."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import shopping_service_key
from .base import BaseView


class ShoppingCollectionView(BaseView):
    """``GET /api/shopping`` + ``POST /api/shopping``."""

    async def get(self) -> web.Response:
        self.user
        include_completed = (
            self.request.query.get("include_completed", "false").lower() == "true"
        )
        items = await self.svc(shopping_service_key).list_items(
            include_completed=include_completed,
        )
        return self._json(
            [
                {
                    "id": i.id,
                    "text": i.text,
                    "completed": i.completed,
                    "created_by": i.created_by,
                    "created_at": i.created_at,
                    "completed_at": i.completed_at,
                }
                for i in items
            ]
        )

    async def post(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        item = await self.svc(shopping_service_key).add_item(
            body.get("text", ""),
            created_by=ctx.user_id,
        )
        return self._json(
            {
                "id": item.id,
                "text": item.text,
                "completed": item.completed,
                "created_by": item.created_by,
                "created_at": item.created_at,
            },
            status=201,
        )


class ShoppingItemDetailView(BaseView):
    """``DELETE /api/shopping/{id}``."""

    async def delete(self) -> web.Response:
        self.user
        item_id = self.match("id")
        await self.svc(shopping_service_key).delete_item(item_id)
        return self._json({"ok": True})


class ShoppingItemCompleteView(BaseView):
    """``PATCH /api/shopping/{id}/complete``."""

    async def patch(self) -> web.Response:
        self.user
        item_id = self.match("id")
        await self.svc(shopping_service_key).complete_item(item_id)
        return self._json({"ok": True})


class ShoppingItemUncompleteView(BaseView):
    """``PATCH /api/shopping/{id}/uncomplete``."""

    async def patch(self) -> web.Response:
        self.user
        item_id = self.match("id")
        await self.svc(shopping_service_key).uncomplete_item(item_id)
        return self._json({"ok": True})


class ShoppingClearCompletedView(BaseView):
    """``POST /api/shopping/clear-completed``."""

    async def post(self) -> web.Response:
        self.user
        count = await self.svc(shopping_service_key).clear_completed()
        return self._json({"cleared": count})
