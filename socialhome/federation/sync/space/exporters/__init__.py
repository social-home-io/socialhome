"""Per-resource exporters used by :class:`SpaceSyncService` (§25.6).

Each module in this package wraps one repo and exposes a
:class:`ResourceExporter`-compatible object: a single ``list_records``
method returning the resource's rows for a space, as plain dicts
suitable for JSON + encryption.

Adding a twelfth resource is a mechanical addition: drop a new module
here, register it in :data:`ALL_EXPORTERS` at the bottom, and add the
resource id to :data:`RESOURCE_ORDER` in the exporter framework.
"""

from .bans import BansExporter
from .calendar import CalendarExporter
from .comments import CommentsExporter
from .gallery import GalleryExporter
from .members import MembersExporter
from .pages import PagesExporter
from .polls import PollsExporter
from .posts import PostsExporter
from .stickies import StickiesExporter
from .tasks import TasksExporter
from .tasks_archived import TasksArchivedExporter

__all__ = [
    "BansExporter",
    "CalendarExporter",
    "CommentsExporter",
    "GalleryExporter",
    "MembersExporter",
    "PagesExporter",
    "PollsExporter",
    "PostsExporter",
    "StickiesExporter",
    "TasksExporter",
    "TasksArchivedExporter",
]
