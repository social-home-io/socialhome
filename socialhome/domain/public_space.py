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
    #: Discovery category (§23.50) — normalizes to ``"general"`` if unknown.
    category: str = "general"
    #: How the host household lets people in — ``invite_only`` / ``open`` /
    #: ``request``. ``invite_only`` means the listing is discoverable but the
    #: content is NOT publicly readable (no subscription, no content key).
    #: Fail-closed default so a directory that reports none never widens
    #: access.
    join_mode: str = "invite_only"
