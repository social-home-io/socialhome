"""Page domain types (§5.2).

Household and space-scoped Markdown pages. Pages use edit-locks and
versioned history; the service + repo layers coordinate those.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Page:
    """A Markdown page — household or space-scoped."""

    id: str
    title: str
    content: str
    created_by: str
    created_at: str
    updated_at: str

    space_id: str | None = None
    cover_image_url: str | None = None
    # Last author trail — populated from PATCH + revert so the UI can
    # show "Edited by Alice · 3 min ago" without pulling history.
    last_editor_user_id: str | None = None
    last_edited_at: str | None = None
    locked_by: str | None = None
    locked_at: str | None = None
    lock_expires_at: str | None = None
    delete_requested_by: str | None = None
    delete_requested_at: str | None = None
    delete_approved_by: str | None = None
    delete_approved_at: str | None = None


@dataclass(slots=True, frozen=True)
class PageVersion:
    """One row in ``page_edit_history`` — an older snapshot of a page."""

    id: str
    page_id: str
    version: int
    title: str
    content: str
    edited_by: str
    edited_at: str

    space_id: str | None = None
    cover_image_url: str | None = None
