"""Which files of a GFS space a household keeps without anyone looking.

Owner 2026-11-28: a GFS space's files reach a household only through the
GFS space media cache (v_97), and that cache keeps a blob for days. So the
households themselves keep the files people are likely to look at — and
can re-share them (v_83 on demand asks any holder) — while old catch-up
content is fetched only when someone opens it. One rule for members and
followers alike (no role-based delivery); the household acting as the
space's host keeps every file regardless (``SpaceMediaGfsFetcher``).

==================  ======================================================
Source              Synced on arrival
==================  ======================================================
LIVE                always — an item that arrives live (it is new)
GALLERY             always — albums stay complete, catch-up included
PAGE                always — the current version (and its open sides)
POST, COMMENT       while new: within :data:`LIVE_WINDOW` of creation
CALENDAR            while the event is upcoming (or a series goes on)
BAZAAR              while the listing is active
MODERATION          pictures only (a reviewer's held submission)
==================  ======================================================

Every synced file is at most :data:`SYNC_MAX_FILE_BYTES`; a larger one (a
long video in pieces) is fetched on view, and kept by the host. A
household's synced files of one space are bounded by its storage setting
(:data:`DEFAULT_SPACE_SYNC_BYTES`, 0 = only what it opens), all spaces'
together by :data:`DEFAULT_SPACE_SYNC_TOTAL_BYTES`, and all of them by the
household quota's free space; past a cap the least recently viewed synced
file goes first, never one viewed within :data:`VIEWED_KEEP` — except for
the household's own upload, which always wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

#: The largest file a household syncs without a view (owner 2026-11-28,
#: every space size). ~90 GB of CDN-cacheable GFS egress for a 30 MB video
#: in a 10 000-seat space with ~3 000 households opening it.
SYNC_MAX_FILE_BYTES: int = 50 * 1024 * 1024

#: How long a post's or comment's files stay "new" (synced on catch-up).
LIVE_WINDOW: timedelta = timedelta(days=7)

#: How far ahead of this household's clock an item's stamp may be.
MAX_CLOCK_SKEW: timedelta = timedelta(minutes=5)

#: The default storage for one space's synced files on a household.
DEFAULT_SPACE_SYNC_BYTES: int = 2 * 1024 * 1024 * 1024

#: The default storage for the synced files of every space together (owner
#: 2026-11-28). Synced files also only ever use the household quota's FREE
#: space: the household's own uploads evict them.
DEFAULT_SPACE_SYNC_TOTAL_BYTES: int = 8 * 1024 * 1024 * 1024

#: A file viewed this recently is never evicted for the budget.
VIEWED_KEEP: timedelta = timedelta(days=30)

#: File extensions that are pictures (the moderation rule).
PICTURE_EXTENSIONS: frozenset[str] = frozenset({"webp", "jpg", "jpeg", "png", "gif"})


class SyncSource(StrEnum):
    """Where a described file comes from (the policy's row)."""

    LIVE = "live"
    GALLERY = "gallery"
    PAGE = "page"
    POST = "post"
    COMMENT = "comment"
    CALENDAR = "calendar"
    BAZAAR = "bazaar"
    MODERATION = "moderation"


@dataclass(slots=True, frozen=True)
class SyncHint:
    """What the policy needs to know about an item: its source, when it
    was created (posts, comments) and until when it is current (an event's
    end, a listing's end; ``None``: it goes on)."""

    source: SyncSource
    created_at: datetime | None = None
    until: datetime | None = None


#: An item that arrived live.
LIVE_HINT: SyncHint = SyncHint(SyncSource.LIVE)


def is_picture(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in PICTURE_EXTENSIONS


def syncs(hint: SyncHint, *, size: int, name: str, now: datetime) -> bool:
    """Whether a household keeps this file without a view."""
    if size > SYNC_MAX_FILE_BYTES:
        return False
    match hint.source:
        case SyncSource.LIVE | SyncSource.GALLERY | SyncSource.PAGE:
            return True
        case SyncSource.POST | SyncSource.COMMENT:
            # A stamp ahead of the clock (past the allowed skew) is not "new":
            # an author's clock can't make old content sync (Fable M11).
            return (
                hint.created_at is not None
                and hint.created_at <= now + MAX_CLOCK_SKEW
                and now - hint.created_at <= LIVE_WINDOW
            )
        case SyncSource.CALENDAR | SyncSource.BAZAAR:
            return hint.until is None or hint.until >= now
        case SyncSource.MODERATION:
            return is_picture(name)
    return False  # pragma: no cover - exhaustive


def parse_moment(value: object) -> datetime | None:
    """A stored timestamp (SQLite's naive UTC ``YYYY-MM-DD HH:MM:SS`` or a
    tz-aware ISO string, or a ``datetime``) as an aware UTC datetime;
    ``None`` when it is not one."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
