"""Notification domain type (§17.2).

One notification-centre entry per user. Body is redacted for
privacy-sensitive content (DMs, location, UGC) per §25.3.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Rows that ask someone to act (a "Needs you" item): a join request, a
#: post waiting for review, a report. Unread, they survive both the
#: per-user cap and the age purge — dropping one would lose the only
#: pointer to work still waiting. Once handled (by anyone) they read
#: as read for every recipient and age out like any other row.
ACTION_ITEM_TYPES: frozenset[str] = frozenset(
    {"space_join_requested", "moderation_pending", "space_report"}
)


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
