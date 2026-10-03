"""Page domain types (§5.2).

Household and space-scoped Markdown pages. Pages use edit-locks and
versioned history; the service + repo layers coordinate those.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Longest page title (household and space pages) — the SPA's input cap.
MAX_PAGE_TITLE_LENGTH = 200


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
    #: Space pages (v_48): the host's sequence number of the version this
    #: row holds — 0 for a version the host has not sequenced.
    seq: int = 0
    #: Space pages (v_48): ``None`` unless this household holds an
    #: unacknowledged local draft; then the ``seq`` it was based on.
    pending_base_seq: int | None = None


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


@dataclass(slots=True, frozen=True)
class PageTombstone:
    """A deleted space page (migration 0073): its id, when it was deleted
    here (naive UTC, SQLite ``datetime('now')``), the page's creator (binds
    an owner-bound id to its space on the receiver) and the user who
    deleted it (empty when nobody can be named)."""

    id: str
    deleted_at: str
    created_by: str = ""
    deleted_by: str = ""


def page_tombstone_to_wire_dict(
    tombstone: PageTombstone, space_id: str
) -> dict[str, Any]:
    """The ``pages_deleted`` sync record of a deleted space page, and the
    replayed ``SPACE_PAGE_DELETED`` payload: ``{id, page_id, space_id,
    created_by, actor_user_id}`` (the deleter — the receiver judges the
    delete against the space's ``pages`` level for that user). The live
    ``SPACE_PAGE_DELETED`` shape plus ``created_by``."""
    return {
        "id": tombstone.id,
        "page_id": tombstone.id,
        "space_id": space_id,
        "created_by": tombstone.created_by,
        "actor_user_id": tombstone.deleted_by,
    }
