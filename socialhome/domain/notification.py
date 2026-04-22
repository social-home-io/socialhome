"""Notification domain type (§17.2).

One notification-centre entry per user. Body is redacted for
privacy-sensitive content (DMs, location, UGC) per §25.3.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Notification:
    """One notification centre entry."""

    id: str
    user_id: str
    type: str  # free-form category tag, e.g. "mention"
    title: str
    created_at: str

    body: str | None = None  # redacted for DM / location / UGC
    link_url: str | None = None
    read_at: str | None = None

    @property
    def is_read(self) -> bool:
        return self.read_at is not None
