"""Public-space listing domain type (§8 / §23.117)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class PublicSpaceListing:
    """A public-space entry surfaced via discovery."""

    space_id: str
    instance_id: str
    name: str
    description: str | None = None
    emoji: str | None = None
    lat: float | None = None
    lon: float | None = None
    radius_km: float | None = None
    member_count: int = 0
    cached_at: str | None = None
    min_age: int = 0
    target_audience: str = "all"
