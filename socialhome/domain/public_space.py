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
    #: ``request``. A pure MEMBERSHIP gate: it says nothing about whether the
    #: content is readable (see ``allow_subscribers``). Fail-closed default so
    #: a directory that reports none never advertises a wider way in than the
    #: host offers.
    join_mode: str = "invite_only"
    #: Whether the host opted this space into read-only followers. False ⇒ the
    #: listing is discoverable but the content is NOT publicly readable (no
    #: subscription, no content relay, no content key) and the SPA must not
    #: offer Subscribe. Fail-closed default so a directory that reports none
    #: never widens access.
    allow_subscribers: bool = False
    #: The host's join contact as this household verified and pinned it
    #: (JSON, migration 0143, v_84): ``{"contact": {...} | null,
    #: "authority_pk": hex, "seq": int, "gfs": url, "conflict": bool}`` — the
    #: keys an "Ask to join" through the GFS is sealed to, the space
    #: authority key that signed them (pinned on first sight), and the GFS it
    #: was pinned from. ``None`` when no listing carried an authority key.
    join_contact: str | None = None
