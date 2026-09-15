"""Shopping-list domain types (§17)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ShoppingItem:
    """One entry on the household shopping list.

    ``store`` is the free-form name of the store / shop where the
    item should be bought (e.g. ``"Aldi"``, ``"Bakery"``). It mirrors
    a row in :class:`ShoppingStore` — the catalogue is what carries
    the household-defined trip order. ``None`` means "unassigned";
    the SPA renders those under a trailing "No store" group.
    """

    id: str
    text: str
    completed: bool
    created_by: str  # user_id
    created_at: str
    completed_at: str | None = None
    store: str | None = None


@dataclass(slots=True, frozen=True)
class ShoppingStore:
    """One entry in the household's shopping-store catalogue.

    Rows are auto-upserted by the repo on first reference from
    :class:`ShoppingItem.store` so the catalogue grows organically.
    The durable state is ``sort_order`` — the index the household
    has dragged this store to in their "trip order". The catalogue
    persists even when every item that referenced the store is
    gone, so a family doesn't have to re-sort after each shop.
    """

    name: str
    sort_order: int


@dataclass(slots=True, frozen=True)
class StoreRenameResult:
    """Outcome of renaming a shopping-store catalogue row.

    A rename that lands on a name another store already holds
    (case-insensitively — the ``ux_shopping_stores_name_nocase`` guard
    from migration 0048) is a **merge**, not an error: the old store's
    items fold onto the survivor's exact spelling and the old catalogue
    row goes away. That is a household's only way to collapse a
    duplicate, so the route reports it rather than 409-ing.

    ``new_name`` is the spelling that SURVIVED — on a merge that is the
    target's existing casing, not the casing the caller typed.
    ``moved_items`` counts the items whose ``store`` column was
    rewritten, so the SPA can toast "moved 3 items".
    """

    old_name: str
    new_name: str
    merged: bool
    moved_items: int
