"""Shopping-list domain type (§17)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ShoppingItem:
    """One entry on the household shopping list."""

    id: str
    text: str
    completed: bool
    created_by: str  # user_id
    created_at: str
    completed_at: str | None = None
