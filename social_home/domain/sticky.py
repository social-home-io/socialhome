"""Sticky-note domain type (§19)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Sticky:
    """One sticky note. ``space_id=None`` means household-scope."""

    id: str
    author: str  # user_id
    content: str
    color: str
    position_x: float
    position_y: float
    created_at: str
    updated_at: str
    space_id: str | None = None  # None = household board
