"""When a §25.6 periodic session may stream only what changed.

Every covered row carries a change stamp (``sync_seq``, migration 0086) set
by triggers from one per-household counter. The provider keeps, per
(space, member household), the counter snapshot of the last stream that
household confirmed with ``SPACE_SYNC_COMPLETE`` — its **watermark**. A
periodic (``"incremental"``) session then streams the covered rows stamped
above it. Everything else streams in full, and so does an incremental
session whenever the watermark cannot be trusted:

* none recorded (first sync, a dropped seat, an older requester that never
  confirms, a failed stream);
* recorded under another **shape** (:func:`session_shape`) — a record-shape
  change, the household's protocol version, the space's retention, the set
  of resources this household gets;
* the last full stream is older than :data:`FULL_RESYNC_INTERVAL_S` — the
  daily anti-entropy pass that converges whatever a stamp cannot express (a
  chunk the receiver refused, a requester restored from an older snapshot).

Fail-safe toward more data, never less. See ``docs/protocol/sync.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ....repositories.space_sync_watermark_repo import (
        AbstractSpaceSyncWatermarkRepo,
    )

log = logging.getLogger(__name__)

#: A household gets a full stream (rows, no media) at least this often,
#: whatever its watermark says.
FULL_RESYNC_INTERVAL_S: float = 24 * 3600.0

#: Bumped whenever an exporter's record gains or changes a field: rows
#: stamped before the change would otherwise never re-stream in the new
#: shape. Part of every session shape.
SYNC_SHAPE_VERSION: int = 1


def session_shape(
    *, peer_version: int, retention: str, resources: Iterable[str]
) -> str:
    """The shape a watermark is valid for: the provider's record shape, the
    requester's protocol version, the space's retention and the resources
    this requester is streamed."""
    return f"v{SYNC_SHAPE_VERSION}|p{peer_version}|r{retention}|" + ",".join(resources)


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class SyncWatermarks:
    """Decide a session's ``since`` and record confirmed streams."""

    __slots__ = ("_repo", "_clock")

    def __init__(
        self,
        repo: "AbstractSpaceSyncWatermarkRepo",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def snapshot(self) -> int:
        """The counter now — read before a stream reads any row, so a row
        changed while the stream runs is stamped above it."""
        return await self._repo.current_seq()

    async def since_for(
        self,
        *,
        space_id: str,
        instance_id: str,
        shape: str,
        sync_mode: str,
    ) -> int | None:
        """The stamp above which this session streams covered rows, or
        ``None`` for a full stream."""
        if sync_mode != "incremental":
            return None
        mark = await self._repo.get(space_id, instance_id)
        if mark is None or mark.shape != shape or not mark.full_at:
            return None
        full_at = _parse_utc(mark.full_at)
        if full_at is None:
            return None
        age = (self._clock() - full_at).total_seconds()
        if age > FULL_RESYNC_INTERVAL_S:
            return None
        return mark.seq

    async def confirm(
        self,
        *,
        space_id: str,
        instance_id: str,
        seq: int,
        shape: str,
        full: bool,
    ) -> None:
        """Record that ``instance_id`` holds everything up to ``seq``."""
        await self._repo.confirm(
            space_id,
            instance_id,
            seq=seq,
            shape=shape,
            full_at=self._clock().isoformat() if full else None,
        )
