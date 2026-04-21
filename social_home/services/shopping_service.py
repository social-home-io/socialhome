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
)
from ..infrastructure.event_bus import EventBus
from ..repositories.shopping_repo import AbstractShoppingRepo, ShoppingItem


class ShoppingService:
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

    async def add_item(self, text: str, *, created_by: str) -> ShoppingItem:
        text = text.strip()
        if not text:
            raise ValueError("shopping item text must not be empty")
        item = await self._repo.add(text, created_by=created_by)
        if self._bus is not None:
            await self._bus.publish(
                ShoppingItemAdded(
                    item_id=item.id,
                    text=item.text,
                    created_by=item.created_by,
                    created_at=item.created_at,
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

    async def complete_item(self, item_id: str) -> None:
        item = await self._repo.get(item_id)
        if item is None:
            raise KeyError(f"shopping item {item_id!r} not found")
        await self._repo.complete(item_id)
        if self._bus is not None:
            await self._bus.publish(
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
        if self._bus is not None:
            await self._bus.publish(
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
        if self._bus is not None:
            await self._bus.publish(ShoppingItemRemoved(item_id=item_id))

    async def clear_completed(self) -> int:
        """Remove all completed items. Returns the count removed."""
        count = await self._repo.clear_completed()
        if self._bus is not None and count > 0:
            await self._bus.publish(ShoppingItemsCleared(count=count))
        return count
