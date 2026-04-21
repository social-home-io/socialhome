"""GFS domain types — row-shaped dataclasses for the Global Federation Server.

Aligned with spec §24.6. Adds fraud-report, appeal, admin-session, and
cluster-node dataclasses for the full admin portal.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ClientInstance:
    """A registered household instance."""

    instance_id: str
    display_name: str
    public_key: str  # Ed25519 verify key (hex)
    endpoint_url: str
    status: str = "pending"  # 'pending' | 'active' | 'banned'
    auto_accept: bool = False
    connected_at: str = ""  # ISO 8601


@dataclass(slots=True, frozen=True)
class GlobalSpace:
    """A global (discoverable) space published to this GFS."""

    space_id: str
    owning_instance: str
    name: str = ""
    description: str | None = None
    about_markdown: str | None = None
    cover_url: str | None = None
    min_age: int = 0
    target_audience: str = "all"
    accent_color: str = "#6366f1"
    status: str = "pending"  # 'pending' | 'active' | 'banned'
    subscriber_count: int = 0
    posts_per_week: float = 0.0
    published_at: str = ""  # ISO 8601


@dataclass(slots=True, frozen=True)
class GfsSubscriber:
    """A subscriber row: instance + webhook for fan-out delivery."""

    instance_id: str
    endpoint_url: str


@dataclass(slots=True, frozen=True)
class GfsFraudReport:
    """A household-admin fraud report against a space or instance."""

    id: str
    target_type: str  # 'space' | 'instance'
    target_id: str
    category: str
    notes: str | None
    reporter_instance_id: str
    reporter_user_id: str | None
    status: str  # 'pending' | 'dismissed' | 'acted'
    created_at: int  # unix epoch
    reviewed_by: str | None = None
    reviewed_at: int | None = None


@dataclass(slots=True, frozen=True)
class GfsAppeal:
    """A banned household's one-shot appeal message."""

    id: str
    target_type: str  # 'space' | 'instance'
    target_id: str
    message: str
    status: str  # 'pending' | 'lifted' | 'dismissed'
    created_at: int
    decided_at: int | None = None
    decided_by: str | None = None


@dataclass(slots=True, frozen=True)
class AdminSession:
    token: str
    expires_at: int
    created_at: int


@dataclass(slots=True, frozen=True)
class ClusterNode:
    """A GFS cluster node (spec §24.10.3)."""

    node_id: str
    url: str
    public_key: str = ""
    status: str = "unknown"  # 'online' | 'offline' | 'syncing' | 'unknown'
    last_seen: str | None = None
    added_at: str = ""
    active_sync_sessions: int = 0

    @property
    def address(self) -> str:
        """Back-compat alias — older callers use ``address`` for ``url``."""
        return self.url


@dataclass(slots=True, frozen=True)
class RtcConnection:
    """Transport mode per client instance (spec §24.12).

    ``transport`` is ``'webrtc'`` when the household's DataChannel is up,
    ``'webhook'`` when falling back to HTTPS push. ``last_ping_at`` is
    bumped by each RTC-ping or webhook fallback write so the admin UI
    can show online/offline per peer.
    """

    instance_id: str
    transport: str = "webhook"
    connected_at: str = ""
    last_ping_at: str = ""


# Backwards-compatible aliases for the pre-spec stub names so existing
# tests / imports keep working through the transition.
GfsInstance = ClientInstance
GfsSpace = GlobalSpace
