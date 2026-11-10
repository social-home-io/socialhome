"""Per-space epoch key domain type (§4.3 space content encryption)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class SpaceKey:
    """One row of ``space_keys``. ``content_key_hex`` is KEK-encrypted."""

    space_id: str
    epoch: int
    content_key_hex: str  # KEK-encrypted ciphertext (hex)
    created_at: str | None = None
    #: Instance id of the household that MINTED this epoch's key (Phase 4b).
    #: Used as a deterministic tiebreak when two delegated admins rotate to
    #: the same epoch concurrently — the smallest ``rotated_by`` wins so every
    #: receiver converges. ``None`` for legacy/owner-minted rows.
    rotated_by: str | None = None
