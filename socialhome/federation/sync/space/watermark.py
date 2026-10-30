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
  change, the household's protocol version (only the versions that change
  a stream, :func:`sync_version_bucket`), the space's retention, the set
  of resources this household gets;
* the last full stream is older than :data:`FULL_RESYNC_INTERVAL_S` — the
  daily anti-entropy pass that converges whatever a stamp cannot express (a
  chunk the receiver refused, a parent it lacked at the time). Only the
  space's host runs it (#2(f)): every receiver takes the roster, the bans
  and every other household's rows from the host alone, so the host's pass
  is the one that converges a space;
* the requester's BEGIN carries no valid ``have_seq``.

``have_seq`` (migration 0087) is the requester's echo: our snapshot of the
last stream it applied cleanly, stored in ITS database. The session streams
since ``min(watermark, have_seq)`` — so a requester restored from an older
file snapshot under the same identity (whose echo rolled back with its
rows) re-streams the gap on its next periodic session, while a ``have_seq``
above the watermark (forged, or ours rolled back) is clamped: it can never
make us skip a row our own watermark says it did not confirm.

Fail-safe toward more data, never less. See ``docs/protocol/sync.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ....domain.space import parse_have_seq

if TYPE_CHECKING:
    from ....repositories.space_sync_watermark_repo import (
        AbstractSpaceSyncWatermarkRepo,
    )

__all__ = [
    "FULL_RESYNC_INTERVAL_S",
    "SYNC_RECORD_LINES",
    "SYNC_SHAPE_VERSION",
    "SYNC_VERSION_HISTORY_THROUGH",
    "SyncWatermarks",
    "parse_have_seq",
    "session_shape",
    "sync_version_bucket",
]

log = logging.getLogger(__name__)

#: A household gets a full stream (rows, no media) at least this often,
#: whatever its watermark says.
FULL_RESYNC_INTERVAL_S: float = 24 * 3600.0

#: Bumped whenever an exporter's record gains or changes a field — or a
#: resource starts streaming incrementally: rows stamped (or changed
#: unstamped) before the change would otherwise never re-stream. Part of
#: every session shape, so a bump invalidates every watermark once.
#:
#: * 1 — migration 0086: posts, comments, chat, gallery, calendar and the
#:   0085 tombstones.
#: * 2 — migration 0088: task lists, tasks (active, archived), pages,
#:   timetables, their tombstones, and the live stickies / zones. A row of
#:   these changed between a v1 watermark and the upgrade carries no stamp.
#: * 3 — ``calendar`` streams every event as its stored row (a series once,
#:   with its rule — not one record per occurrence, in a ±10-year window
#:   around the provider's clock), and the receiver keeps the rule; a held
#:   gallery album takes the host's edits and recounts its items. Copies
#:   the old shape left wrong heal on one full stream, not on the next row
#:   change.
#: * 4 — migration 0096: a ``space_zones`` record carries ``icon``.
#: * 5 — migration 0122: ``member_pictures`` streams incrementally. A
#:   picture changed before the upgrade carries no stamp.
#: * 6 — migration 0135: a ``bazaar`` record carries the listing's
#:   questions and answers (``faq``, v_75).
SYNC_SHAPE_VERSION: int = 6


#: The requester's protocol version counts in the shape only where a
#: version changes what a sync stream carries or what its receiver takes
#: (#2(e)). Every version up to this one stays its own shape — that history
#: was never sorted into sync and non-sync bumps — so no watermark changes
#: when this rule ships.
SYNC_VERSION_HISTORY_THROUGH: int = 64

#: Later versions that change a sync stream: a new resource, a record field
#: a receiver now stores, a version gate in ``provider._exporter_for`` (a
#: tripwire test checks the gates). A bump not listed here keeps every
#: watermark, so it costs no household a full stream.
#:
#: * 75 — a ``bazaar`` record's ``faq`` (a listing's questions and
#:   answers), which a v_75 receiver stores: a household crossing it gets
#:   one full stream, so FAQs it was not sent while older reach it.
SYNC_RECORD_LINES: tuple[int, ...] = (75,)


def sync_version_bucket(peer_version: int) -> int:
    """The part of ``peer_version`` a session shape depends on."""
    if peer_version <= SYNC_VERSION_HISTORY_THROUGH:
        return peer_version
    return max(
        (line for line in SYNC_RECORD_LINES if line <= peer_version),
        default=SYNC_VERSION_HISTORY_THROUGH,
    )


def session_shape(
    *, peer_version: int, retention: str, resources: Iterable[str]
) -> str:
    """The shape a watermark is valid for: the provider's record shape, the
    requester's protocol version (as far as it changes a stream,
    :func:`sync_version_bucket`), the space's retention and the resources
    this requester is streamed."""
    version = sync_version_bucket(peer_version)
    return f"v{SYNC_SHAPE_VERSION}|p{version}|r{retention}|" + ",".join(resources)


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class SyncWatermarks:
    """Decide a session's ``since`` and record confirmed streams."""

    __slots__ = ("_clock", "_repo")

    def __init__(
        self,
        repo: AbstractSpaceSyncWatermarkRepo,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._clock = clock or (lambda: datetime.now(UTC))

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
        have_seq: int | None,
        anti_entropy: bool = True,
    ) -> int | None:
        """The stamp above which this session streams covered rows, or
        ``None`` for a full stream. ``have_seq``: the requester's echo
        (:func:`parse_have_seq`) — ``None`` streams in full, a lower one wins,
        a higher one is clamped to the watermark. ``anti_entropy`` (#2(f)):
        whether this provider runs the daily full pass — only the space's
        roster authority (the host) does; another household's provider
        keeps streaming incrementally once it streamed in full."""
        if sync_mode != "incremental" or have_seq is None:
            return None
        mark = await self._repo.get(space_id, instance_id)
        if mark is None or mark.shape != shape or not mark.full_at:
            return None
        full_at = _parse_utc(mark.full_at)
        if full_at is None:
            return None
        age = (self._clock() - full_at).total_seconds()
        if anti_entropy and age > FULL_RESYNC_INTERVAL_S:
            return None
        return min(mark.seq, have_seq)

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
