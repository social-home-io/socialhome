"""Shopping service — thin orchestration wrapper around :class:`AbstractShoppingRepo`.

Provides service-layer entry points for the household shopping list.
Route handlers call these methods and never touch the repo directly.

**Scope**: local household only. The shopping list is intentionally
not federated to paired households — short-lived items that don't
benefit from cross-household sync. See the :mod:`domain.events`
``Shopping*`` dataclasses for the corresponding (WS-only) domain
events that RealtimeService fans out over the household WebSocket.

Raises the usual domain exceptions:

* ``KeyError``   → 404 (item not found)
* ``ValueError`` → 422 (validation failure)
"""

from __future__ import annotations

from ..domain.events import (
    ShoppingItemAdded,
    ShoppingItemRemoved,
    ShoppingItemsCleared,
    ShoppingItemToggled,
    ShoppingItemUpdated,
    ShoppingStoreDeleted,
    ShoppingStoreRenamed,
    ShoppingStoresReordered,
)
from ..domain.shopping import ShoppingStore, StoreRenameResult
from ..infrastructure.event_bus import EventBus
from ..repositories.shopping_repo import (
    AbstractShoppingRepo,
    ShoppingItem,
    UNSET_FIELD,
)
from .bus_publisher import BusPublisherMixin


#: Upper bound on the free-form store name. Keeps the chip / pill
#: real estate sensible across the UI; the autosuggest dropdown also
#: paginates poorly past this length.
_STORE_MAX = 80


def _clean_store(value: str | None) -> str | None:
    """Normalise a store name from the wire.

    Trims surrounding whitespace, collapses an empty / whitespace-only
    string to ``None`` (so blank input clears the field), and clamps
    to :data:`_STORE_MAX` so a hostile or sloppy caller can't push a
    paragraph into the column.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    return trimmed[:_STORE_MAX]


class ShoppingService(BusPublisherMixin):
    """Household shopping list operations."""

    __slots__ = ("_repo", "_bus")

    def __init__(
        self,
        shopping_repo: AbstractShoppingRepo,
        bus: EventBus | None = None,
    ) -> None:
        self._repo = shopping_repo
        # Bus is optional so legacy tests that pre-date the WS fan-out
        # can instantiate a bare service. Production wiring in ``app.py``
        # always injects a live bus so RealtimeService broadcasts land.
        self._bus = bus

    async def add_item(
        self,
        text: str,
        *,
        created_by: str,
        store: str | None = None,
    ) -> ShoppingItem:
        text = text.strip()
        if not text:
            raise ValueError("shopping item text must not be empty")
        clean_store = _clean_store(store)
        item = await self._repo.add(text, created_by=created_by, store=clean_store)
        await self._emit(
            ShoppingItemAdded(
                item_id=item.id,
                text=item.text,
                created_by=item.created_by,
                created_at=item.created_at,
                store=item.store,
            )
        )
        return item

    async def get_item(self, item_id: str) -> ShoppingItem:
        item = await self._repo.get(item_id)
        if item is None:
            raise KeyError(f"shopping item {item_id!r} not found")
        return item

    async def list_items(
        self,
        *,
        include_completed: bool = False,
    ) -> list[ShoppingItem]:
        return await self._repo.list(include_completed=include_completed)

    async def update_item(
        self,
        item_id: str,
        *,
        text: str | None = None,
        store: object = UNSET_FIELD,
    ) -> ShoppingItem:
        """Patch ``text`` / ``store`` on an item. Sentinel pattern
        again — :data:`UNSET_FIELD` (the default) for ``store`` means
        "leave alone"; ``None`` clears it; a string sets it.
        """
        new_text: str | None = None
        if text is not None:
            new_text = text.strip()
            if not new_text:
                raise ValueError("shopping item text must not be empty")
        if store is UNSET_FIELD:
            cleaned_store: object = UNSET_FIELD
        else:
            assert store is None or isinstance(store, str)
            cleaned_store = _clean_store(store)
        updated = await self._repo.update_item(
            item_id,
            text=new_text,
            store=cleaned_store,
        )
        if updated is None:
            raise KeyError(f"shopping item {item_id!r} not found")
        await self._emit(
            ShoppingItemUpdated(
                item_id=updated.id,
                text=updated.text,
                store=updated.store,
            )
        )
        return updated

    async def complete_item(self, item_id: str) -> None:
        item = await self._repo.get(item_id)
        if item is None:
            raise KeyError(f"shopping item {item_id!r} not found")
        await self._repo.complete(item_id)
        await self._emit(
            ShoppingItemToggled(
                item_id=item_id,
                completed=True,
            )
        )

    async def uncomplete_item(self, item_id: str) -> None:
        item = await self._repo.get(item_id)
        if item is None:
            raise KeyError(f"shopping item {item_id!r} not found")
        await self._repo.uncomplete(item_id)
        await self._emit(
            ShoppingItemToggled(
                item_id=item_id,
                completed=False,
            )
        )

    async def delete_item(self, item_id: str) -> None:
        item = await self._repo.get(item_id)
        if item is None:
            raise KeyError(f"shopping item {item_id!r} not found")
        await self._repo.delete(item_id)
        await self._emit(ShoppingItemRemoved(item_id=item_id))

    async def clear_completed(self) -> int:
        """Remove all completed items. Returns the count removed."""
        count = await self._repo.clear_completed()
        if self._bus is not None and count > 0:
            await self._bus.publish(ShoppingItemsCleared(count=count))
        return count

    # ─── Stores ──────────────────────────────────────────────────────────

    async def list_stores(self) -> list[ShoppingStore]:
        return await self._repo.list_stores()

    async def reorder_stores(self, ordered_names: list[str]) -> list[ShoppingStore]:
        """Apply the new store order, return the canonical post-reorder
        list, and broadcast :class:`ShoppingStoresReordered` so paired
        tabs / members can patch their local cache.
        """
        await self._repo.reorder_stores(ordered_names)
        result = await self._repo.list_stores()
        await self._emit(
            ShoppingStoresReordered(
                order=tuple(s.name for s in result),
            )
        )
        return result

    async def create_store(self, name: str) -> ShoppingStore:
        """Add a store to the catalogue without attaching an item to it.

        Cleans the name the same way every other store input is cleaned
        (trim + clamp to :data:`_STORE_MAX`) and rejects a blank one so
        the route can answer 422. Idempotent case-insensitively — a
        store that already exists comes back unchanged, casing and
        ``sort_order`` intact, so re-tapping "+ Add store" is harmless.
        """
        clean = _clean_store(name)
        if clean is None:
            raise ValueError("store name must not be empty")
        return await self._repo.create_store(clean)

    async def rename_store(
        self,
        old_name: str,
        new_name: str,
    ) -> StoreRenameResult | None:
        """Rename a catalogue row + cascade to items. Returns ``None``
        when no store matches ``old_name`` (route layer maps to a 404);
        raises ``ValueError`` when ``new_name`` is blank (→ 422).

        A ``new_name`` another store already holds is a MERGE, not a
        conflict — see :meth:`AbstractShoppingRepo.rename_store`. Either
        way :class:`ShoppingStoreRenamed` is broadcast (carrying the
        surviving spelling) so paired tabs patch their local catalogue
        and items' ``store`` field; a merge simply collapses two entries
        into one on receipt. No event fires when the name is unchanged.
        """
        clean_new = _clean_store(new_name)
        if clean_new is None:
            raise ValueError("store name must not be empty")
        result = await self._repo.rename_store(old_name, clean_new)
        if result is not None and result.old_name != result.new_name:
            await self._emit(
                ShoppingStoreRenamed(
                    old_name=result.old_name,
                    new_name=result.new_name,
                )
            )
        return result

    async def delete_store(self, name: str) -> int:
        """Remove a catalogue row + clear it from every item that
        referenced it. Returns the count of items whose ``store`` was
        set to NULL — useful as a "moved X items to No store" toast
        hint. Broadcasts :class:`ShoppingStoreDeleted`.
        """
        affected = await self._repo.delete_store(name)
        await self._emit(ShoppingStoreDeleted(name=name.strip()))
        return affected
