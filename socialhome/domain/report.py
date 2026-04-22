"""User-filed content reports (spam, harassment, …).

Reports are a household-level admin concern — any member can flag a
post / comment / user / space, and the household admins triage through
:class:`ReportService`. Federation is scoped out in v1; reports stay
local-to-household.
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


class DuplicateReportError(Exception):
    """Reporter has already filed a report on this target."""


class ReportRateLimitedError(Exception):
    """Reporter has hit the per-day cap."""
