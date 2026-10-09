"""A deleted space row as a §25.6 ``*_deleted`` sync resource streams it.

Stickies, space calendar events, gallery albums / items and space zones
keep their row when deleted (migration 0085): content blanked, ``deleted_at``
/ ``deleted_by`` set. :class:`SpaceRowTombstone` is that row's identity —
what a household that missed the delete needs to apply it with the live
delete's authority rule — and never its content.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class SpaceRowTombstone:
    """A tombstoned space row.

    ``owner`` is the row's creator — the sticky's ``author``, the event's
    ``created_by``, the album's ``owner_user_id``, the item's
    ``uploaded_by``, the zone's ``created_by``: the receiver binds an
    owner-bound id to it and judges the delete against it. ``deleted_at``
    is naive UTC (SQLite ``datetime('now')``); ``deleted_by`` names the user
    who made the delete (empty when nobody can be named). ``parent_id`` is
    the album of a gallery item (empty otherwise).
    """

    id: str
    owner: str
    created_at: str
    deleted_at: str
    deleted_by: str = ""
    parent_id: str = ""
