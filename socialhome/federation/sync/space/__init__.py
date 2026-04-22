"""Direct-peer space content sync (§25.6).

Signalling lives in :mod:`~socialhome.federation.sync_manager` and
:mod:`~socialhome.federation.sync_rtc`. This package implements the
content-transfer layer: once a DataChannel is open the provider
streams encrypted, signed chunks of space content; the requester
persists them. The 11 resource types (bans, members, posts, comments,
tasks, tasks_archived, pages, stickies, calendar, gallery, polls)
pass through a common :class:`ResourceExporter` Protocol so adding a
twelfth is a small addition rather than a rewrite.

Modules:

* :mod:`exporter` — :class:`ResourceExporter` protocol +
  :class:`ChunkBuilder` helper (encrypt + sign + size-budget).
* :mod:`exporters` — one module per resource type.
* :mod:`provider` — :class:`SpaceSyncService` orchestrates outbound
  chunk streaming.
* :mod:`receiver` — :class:`SpaceSyncReceiver` verifies + decrypts +
  persists inbound chunks.
* :mod:`scheduler` — :class:`SpaceSyncScheduler` drives initiation
  (event-driven on pair-confirm + periodic every 30 min).
"""

from .exporter import ChunkBuilder, ResourceExporter, RESOURCE_ORDER
from .provider import SpaceSyncService
from .receiver import SpaceSyncReceiver
from .scheduler import SpaceSyncScheduler

__all__ = [
    "ChunkBuilder",
    "RESOURCE_ORDER",
    "ResourceExporter",
    "SpaceSyncService",
    "SpaceSyncReceiver",
    "SpaceSyncScheduler",
]
