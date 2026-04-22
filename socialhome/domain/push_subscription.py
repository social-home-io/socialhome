"""Push subscription domain type (§21 / §25.3)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class PushSubscription:
    """One row of ``push_subscriptions``."""

    id: str
    user_id: str
    endpoint: str
    p256dh: str
    auth_secret: str
    device_label: str | None = None
    created_at: str | None = None
