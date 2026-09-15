"""Shopping list routes — /api/shopping/* (§23.120)."""

from __future__ import annotations

from aiohttp import web

from ..app_keys import shopping_service_key
from ..repositories.shopping_repo import UNSET_FIELD
from ..security import error_response
from .base import BaseView


def _item_dict(item) -> dict:
    """Wire-shape for a :class:`ShoppingItem`. ``store`` is always
    present (``None`` when unassigned) so the SPA can patch a stale
    cache entry without a re-fetch."""
    return {
        "id": item.id,
        "text": item.text,
        "completed": item.completed,
        "created_by": item.created_by,
        "created_at": item.created_at,
        "completed_at": item.completed_at,
        "store": item.store,
    }


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
        return self._json([_item_dict(i) for i in items])

    async def post(self) -> web.Response:
        ctx = self.user
        body = await self.body()
        item = await self.svc(shopping_service_key).add_item(
            body.get("text", ""),
            created_by=ctx.user_id,
            store=body.get("store"),
        )
        return self._json(_item_dict(item), status=201)


class ShoppingItemDetailView(BaseView):
    """``PATCH`` / ``DELETE /api/shopping/{id}``."""

    async def patch(self) -> web.Response:
        """Edit ``text`` and / or ``store`` on an existing item.

        Tri-state on ``store``:

        * key omitted in body → keep the existing value;
        * ``store: null`` → clear the field;
        * ``store: "Aldi"`` → set the field.
        """
        self.user
        item_id = self.match("id")
        body = await self.body()
        store = body["store"] if "store" in body else UNSET_FIELD
        item = await self.svc(shopping_service_key).update_item(
            item_id,
            text=body.get("text"),
            store=store,
        )
        return self._json(_item_dict(item))

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


class ShoppingStoresView(BaseView):
    """``GET /api/shopping/stores`` — return the household catalogue
    in canonical ``sort_order``.

    ``POST /api/shopping/stores`` — add a store without having to
    assign it to an item first. Body ``{"name": "Bakery"}``. Idempotent
    case-insensitively: a store that already exists comes back as-is
    (same casing, same ``sort_order``) with a 201, so the SPA's
    "+ Add store" action never has to reconcile a conflict.
    """

    async def get(self) -> web.Response:
        self.user
        stores = await self.svc(shopping_service_key).list_stores()
        return self._json(
            [{"name": s.name, "sort_order": s.sort_order} for s in stores]
        )

    async def post(self) -> web.Response:
        self.user
        body = await self.body()
        name = body.get("name")
        store = await self.svc(shopping_service_key).create_store(
            name if isinstance(name, str) else "",
        )
        return self._json(
            {"name": store.name, "sort_order": store.sort_order},
            status=201,
        )


class ShoppingStoresOrderView(BaseView):
    """``PUT /api/shopping/stores/order`` — replace the household's
    drag-defined trip order.

    Body: ``{"order": ["Bakery", "Aldi", "Whole Foods"]}``. Unknown
    names are ignored (the SPA might be reordering on stale data);
    catalogue names that are missing from ``order`` retain their
    relative order but shift past the explicitly-ordered tail.
    """

    async def put(self) -> web.Response:
        self.user
        body = await self.body()
        order = body.get("order") or []
        if not isinstance(order, list):
            return error_response(
                422,
                "UNPROCESSABLE",
                "`order` must be an array of store names.",
            )
        stores = await self.svc(shopping_service_key).reorder_stores(
            [str(n) for n in order],
        )
        return self._json(
            [{"name": s.name, "sort_order": s.sort_order} for s in stores]
        )


class ShoppingStoreDetailView(BaseView):
    """``PATCH /api/shopping/stores/{name}`` — rename a catalogue row
    and cascade to every item that referenced it.

    ``DELETE /api/shopping/stores/{name}`` — remove the catalogue row
    + clear ``store`` on every item that referenced it (items drop
    into the "No store" bucket).

    Both are idempotent on a missing row — PATCH returns 404, DELETE
    returns 200 with ``{cleared: 0}`` so an operator double-clicking
    the trash icon doesn't see an alarming error.

    A PATCH onto a name another store already holds is a MERGE, not a
    409: the old store's items fold onto the survivor. The response
    says so via ``merged`` + ``moved_items`` so the SPA can toast
    "merged into Migros — 3 items moved" rather than a bare rename.
    ``old_name`` / ``new_name`` carry the catalogue's own spellings
    (the lookup is case-insensitive), not the casing the caller typed.
    """

    async def patch(self) -> web.Response:
        self.user
        old_name = self.match("name")
        body = await self.body()
        new_name = body.get("name")
        if not isinstance(new_name, str) or not new_name.strip():
            return error_response(
                422,
                "UNPROCESSABLE",
                "`name` (new store name) is required.",
            )
        result = await self.svc(shopping_service_key).rename_store(
            old_name,
            new_name,
        )
        if result is None:
            return error_response(404, "NOT_FOUND", f"No store named {old_name!r}.")
        return self._json(
            {
                "old_name": result.old_name,
                "new_name": result.new_name,
                "merged": result.merged,
                "moved_items": result.moved_items,
            }
        )

    async def delete(self) -> web.Response:
        self.user
        name = self.match("name")
        cleared = await self.svc(shopping_service_key).delete_store(name)
        return self._json({"name": name, "cleared": cleared})
