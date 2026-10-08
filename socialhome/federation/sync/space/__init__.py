"""Direct-peer space content sync (§25.6).

Signalling lives in :mod:`~socialhome.federation.sync_manager` and
:mod:`~socialhome.federation.sync_rtc`. This package implements the
content-transfer layer: once a DataChannel is open the provider
streams encrypted, signed chunks of space content; the requester
persists them. Every resource type (:data:`~.exporter.RESOURCE_ORDER`)
passes through a common :class:`ResourceExporter` Protocol so adding one
is a small addition rather than a rewrite. What streams is the space's
retention window, page by page (:mod:`window`).

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
from .window import SyncWindows

__all__ = [
    "ChunkBuilder",
    "RESOURCE_ORDER",
    "ResourceExporter",
    "SpaceSyncService",
    "SpaceSyncReceiver",
    "SpaceSyncScheduler",
    "SyncWindows",
]
