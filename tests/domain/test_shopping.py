"""Tests for the shopping-list domain dataclasses."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.shopping import (
    ShoppingItem,
    ShoppingStore,
    StoreRenameResult,
)


def test_shopping_item_is_frozen():
    item = ShoppingItem(
        id="i1",
        text="Milk",
        completed=False,
        created_by="u1",
        created_at="2026-09-15T10:00:00+00:00",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.text = "Bread"  # type: ignore[misc]


def test_shopping_item_store_defaults_to_unassigned():
    """``None`` is the "No store" bucket — distinct from any name."""
    item = ShoppingItem(
        id="i1",
        text="Milk",
        completed=False,
        created_by="u1",
        created_at="2026-09-15T10:00:00+00:00",
    )
    assert item.store is None


def test_shopping_store_is_frozen():
    store = ShoppingStore(name="Migros", sort_order=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        store.sort_order = 4  # type: ignore[misc]


def test_store_rename_result_is_frozen():
    result = StoreRenameResult(
        old_name="Aldi",
        new_name="Migros",
        merged=True,
        moved_items=2,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.merged = False  # type: ignore[misc]


def test_store_rename_result_new_name_is_the_survivor():
    """On a merge ``new_name`` is the spelling that SURVIVED.

    That is the target's existing casing, not whatever the caller
    typed — the SPA renders this value back to the user, so the
    distinction is the whole point of carrying it.
    """
    result = StoreRenameResult(
        old_name="Aldi",
        new_name="Migros",
        merged=True,
        moved_items=2,
    )
    assert result.new_name == "Migros"
    assert result.moved_items == 2
