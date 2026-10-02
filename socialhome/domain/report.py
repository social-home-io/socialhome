"""User-filed content reports (spam, harassment, …).

Any member can flag content. Who triages a report depends on its scope:

* **space-scoped** (``space_id`` set) — content inside a space (a post,
  comment, page, task, sticky, calendar event, gallery item) or a member's
  conduct in it. The space's content authority (owner / admin /
  moderator, :data:`~socialhome.domain.space.CONTENT_AUTHORITY_ROLES`)
  triages it; household admins do not see it.
* **household-level** (``space_id`` is ``None``) — feed content, a user
  outside any space, a space itself, highlights, moments. Household admins
  triage it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ReportCategory(StrEnum):
    SPAM = "spam"
    HARASSMENT = "harassment"
    INAPPROPRIATE = "inappropriate"
    MISINFORMATION = "misinformation"
    OTHER = "other"


class ReportStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ReportTargetType(StrEnum):
    POST = "post"
    COMMENT = "comment"
    USER = "user"
    SPACE = "space"
    HIGHLIGHT = "highlight"
    MOMENT = "moment"
    PAGE = "page"
    TASK = "task"
    STICKY = "sticky"
    CALENDAR_EVENT = "calendar_event"
    GALLERY_ITEM = "gallery_item"


@dataclass(slots=True, frozen=True)
class ContentReport:
    id: str
    target_type: ReportTargetType
    target_id: str
    reporter_user_id: str
    category: ReportCategory
    notes: str | None
    status: ReportStatus
    created_at: datetime
    reporter_instance_id: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    #: The space the report is about; ``None`` = household-level.
    space_id: str | None = None
    #: The subject, when at filing they were the space's owner and its
    #: only content authority — the one person who may dismiss it
    #: anonymously (``ReportService.review_space``).
    sole_reviewer_user_id: str | None = None


@dataclass(slots=True, frozen=True)
class SpaceReportView:
    """One row of a space's report queue, as one reviewer sees it.

    ``anonymous``: the reviewer is the report's subject (the reported
    member, or the item's author) and the space has no other content
    authority to hand it to — they see it without the reporter, and
    ``dismiss_only`` (they may not "resolve" a report about themself)."""

    report: ContentReport
    preview: str | None = None
    gone: bool = False
    anonymous: bool = False
    dismiss_only: bool = False


class DuplicateReportError(Exception):
    """Reporter has already filed a report on this target."""


class ReportRateLimitedError(Exception):
    """Reporter has hit the per-day cap."""
