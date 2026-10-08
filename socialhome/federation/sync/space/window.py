"""What a §25.6 space sync streams: the space's retention window.

There is no fixed size limit on a sync. A space with ``retention_days``
set streams what that retention keeps — everything created at or after
``now - retention_days`` (plus the post types the space exempts from
retention); a space without it (keep forever) streams everything. The
retention sweep (:mod:`socialhome.infrastructure.space_retention_scheduler`)
expires older rows, so a household that streams them anyway would hand a
joiner what the space already let go.

Memory stays bounded however big the space is: exporters read their repo
in pages of :data:`SYNC_PAGE_SIZE` rows (keyset on the row id, so a page
never repeats or skips a row) and :class:`~.exporter.ChunkBuilder` turns
each page into ≤ 8 KB chunks before the next page is read. Nothing holds
a whole resource in memory.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from ....domain.space import Space
    from ....repositories.space_repo import AbstractSpaceRepo


T = TypeVar("T")

#: Rows per repository page while exporting. Each page becomes a handful
#: of chunks before the next page is read — the bound on what one export
#: holds in memory. Not a cap: an exporter pages until the repo runs dry.
SYNC_PAGE_SIZE: int = 200


@dataclass(slots=True, frozen=True)
class SyncWindow:
    """The rows of a space a sync streams.

    ``cutoff`` — naive UTC ``"YYYY-MM-DD HH:MM:SS"``: rows created before it
    are past the space's retention and stay out. ``None`` keeps forever:
    everything streams. Compared through SQLite ``datetime()`` on both
    sides, as the retention sweep does (stored ``created_at`` values mix
    the ISO ``T`` form and the naive form).

    ``exempt_types`` — post types the space exempts from retention
    (``spaces.retention_exempt_json``): their posts stream at any age, as
    the sweep keeps them at any age.
    """

    cutoff: str | None = None
    exempt_types: tuple[str, ...] = ()


#: Everything — a space with no retention set (or not held here).
KEEP_FOREVER = SyncWindow()


def window_for_space(
    space: "Space | None", *, now: datetime | None = None
) -> SyncWindow:
    """The :class:`SyncWindow` of ``space`` at ``now`` (default: now)."""
    if space is None or not space.retention_days or space.retention_days <= 0:
        return KEEP_FOREVER
    at = now if now is not None else datetime.now(timezone.utc)
    cutoff = (at - timedelta(days=int(space.retention_days))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return SyncWindow(
        cutoff=cutoff, exempt_types=tuple(sorted(space.retention_exempt_types))
    )


class SyncWindows:
    """Look up a space's :class:`SyncWindow` when an export starts."""

    __slots__ = ("_spaces",)

    def __init__(self, space_repo: "AbstractSpaceRepo") -> None:
        self._spaces = space_repo

    async def for_space(self, space_id: str) -> SyncWindow:
        return window_for_space(await self._spaces.get(space_id))


async def iter_pages(
    fetch: Callable[[int | None], Awaitable[tuple[list[T], int | None]]],
) -> AsyncIterator[list[T]]:
    """Drive a keyset-paged repo read to the end.

    ``fetch(cursor)`` returns ``(rows, next_cursor)`` — the shape of the
    repos' ``*_sync_page`` methods; ``None`` as the next cursor ends it.
    Yields each non-empty page as it is read.
    """
    cursor: int | None = None
    while True:
        rows, cursor = await fetch(cursor)
        if rows:
            yield rows
        if cursor is None:
            return


async def iter_tombstone_pages(
    fetch: Callable[[tuple[str, str] | None], Awaitable[list[T]]],
    key: Callable[[T], tuple[str, str]],
) -> AsyncIterator[list[T]]:
    """Drive a ``(deleted_at, id)`` keyset read of tombstones to the end.

    ``fetch(before)`` returns up to :data:`SYNC_PAGE_SIZE` tombstones,
    newest delete first, strictly after ``before`` (``None``: from the
    newest); ``key`` gives a tombstone's ``(deleted_at, id)``. A short
    page ends it.
    """
    before: tuple[str, str] | None = None
    while True:
        rows = await fetch(before)
        if rows:
            yield rows
        if len(rows) < SYNC_PAGE_SIZE:
            return
        before = key(rows[-1])
