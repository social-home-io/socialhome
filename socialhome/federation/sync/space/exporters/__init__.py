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
from .bazaar import BazaarExporter
from .calendar import CalendarExporter
from .calendar_deleted import CalendarDeletedExporter
from .chat_messages import ChatMessagesDeletedExporter, ChatMessagesExporter
from .comments import CommentsExporter
from .comments_deleted import CommentsDeletedExporter
from .gallery import GalleryExporter
from .gallery_deleted import (
    GalleryAlbumsDeletedExporter,
    GalleryItemsDeletedExporter,
)
from .member_pictures import MemberPicturesExporter
from .members import MembersExporter
from .pages import PagesExporter
from .pages_deleted import PagesDeletedExporter
from .polls import PollsExporter
from .posts import PostsExporter
from .posts_deleted import PostsDeletedExporter
from .schedules import SchedulesExporter
from .stickies import StickiesExporter
from .stickies_deleted import StickiesDeletedExporter
from .task_lists import TaskListsExporter
from .task_lists_deleted import TaskListsDeletedExporter
from .tasks import TasksExporter
from .tasks_deleted import TasksDeletedExporter
from .tasks_archived import TasksArchivedExporter
from .timetables import TimetablesExporter
from .zones import ZonesExporter
from .zones_deleted import ZonesDeletedExporter

__all__ = [
    "BansExporter",
    "BazaarExporter",
    "CalendarDeletedExporter",
    "CalendarExporter",
    "ChatMessagesDeletedExporter",
    "ChatMessagesExporter",
    "CommentsDeletedExporter",
    "CommentsExporter",
    "GalleryAlbumsDeletedExporter",
    "GalleryExporter",
    "GalleryItemsDeletedExporter",
    "MemberPicturesExporter",
    "MembersExporter",
    "PagesDeletedExporter",
    "PagesExporter",
    "PollsExporter",
    "PostsDeletedExporter",
    "PostsExporter",
    "SchedulesExporter",
    "StickiesDeletedExporter",
    "StickiesExporter",
    "TaskListsDeletedExporter",
    "TaskListsExporter",
    "TasksExporter",
    "TasksArchivedExporter",
    "TasksDeletedExporter",
    "TimetablesExporter",
    "ZonesDeletedExporter",
    "ZonesExporter",
]
