"""Inbound handler for peer-public-space directory sync (§D1a).

Receives :data:`FederationEventType.SPACE_DIRECTORY_SYNC` envelopes
carrying a full snapshot of the sender's ``type=public`` spaces.
Atomically replaces the cached rows for that peer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.federation import FederationEventType
from ..repositories.peer_space_directory_repo import (
    AbstractPeerSpaceDirectoryRepo,
    PeerSpaceDirectoryEntry,
)

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from .federation_service import FederationService

log = logging.getLogger(__name__)


class PeerDirectoryHandler:
    """Inbound dispatcher for :data:`SPACE_DIRECTORY_SYNC`."""

    __slots__ = ("_repo",)

    def __init__(self, repo: AbstractPeerSpaceDirectoryRepo) -> None:
        self._repo = repo

    def attach_to(self, federation_service: "FederationService") -> None:
        registry = federation_service._event_registry  # noqa: SLF001
        registry.register(
            FederationEventType.SPACE_DIRECTORY_SYNC,
            self._on_snapshot,
        )

    async def _on_snapshot(self, event: "FederationEvent") -> None:
        """Replace the cached directory for the sending instance."""
        items = event.payload.get("spaces")
        if not isinstance(items, list):
            log.debug(
                "SPACE_DIRECTORY_SYNC from %s: payload missing 'spaces'",
                event.from_instance,
            )
            return
        entries: list[PeerSpaceDirectoryEntry] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                entries.append(
                    PeerSpaceDirectoryEntry(
                        instance_id=event.from_instance,
                        space_id=str(item["space_id"]),
                        name=str(item.get("name") or ""),
                        description=item.get("description"),
                        emoji=item.get("emoji"),
                        member_count=int(item.get("member_count") or 0),
                        join_mode=str(item.get("join_mode") or "request"),
                        min_age=int(item.get("min_age") or 0),
                        target_audience=str(item.get("target_audience") or "all"),
                        updated_at=item.get("updated_at"),
                    )
                )
            except KeyError, TypeError, ValueError:
                continue
        await self._repo.replace_snapshot(event.from_instance, entries)
        log.debug(
            "SPACE_DIRECTORY_SYNC from %s: stored %d entries",
            event.from_instance,
            len(entries),
        )
