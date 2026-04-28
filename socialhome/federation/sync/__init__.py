"""Federation sync primitives.

All "catch-up" streaming protocols live under this package so the
DataChannel + chunk + signature scaffolding stays in one place:

* :mod:`.space` — full-resource space content sync (§25.6).
* :mod:`.dm_history` — direct-message history gap-fill for newly
  paired peers or peers returning from unreachability.

Each sub-package exposes a provider/receiver/scheduler triple. The
umbrella re-exports the names :mod:`socialhome.app` needs so the
wiring remains a single import line per sync kind.
"""

from .space import (
    RESOURCE_ORDER,
    ChunkBuilder,
    ResourceExporter,
    SpaceSyncReceiver,
    SpaceSyncScheduler,
    SpaceSyncService,
)
from .space.exporters import (
    BansExporter,
    CalendarExporter,
    CommentsExporter,
    GalleryExporter,
    MembersExporter,
    PagesExporter,
    PollsExporter,
    PostsExporter,
    StickiesExporter,
    TasksArchivedExporter,
    TasksExporter,
    ZonesExporter,
)

__all__ = [
    "BansExporter",
    "CalendarExporter",
    "ChunkBuilder",
    "CommentsExporter",
    "GalleryExporter",
    "MembersExporter",
    "PagesExporter",
    "PollsExporter",
    "PostsExporter",
    "RESOURCE_ORDER",
    "ResourceExporter",
    "SpaceSyncReceiver",
    "SpaceSyncScheduler",
    "SpaceSyncService",
    "StickiesExporter",
    "TasksArchivedExporter",
    "TasksExporter",
    "ZonesExporter",
]
