"""How one §25.6 sync stream went (:class:`StreamHealth`) and what a
persist held back (:class:`HeldBack`) — the bookkeeping
:class:`~.receiver.SpaceSyncReceiver` keeps per stream.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from enum import StrEnum

log = logging.getLogger(__name__)


#: Streams whose health is tracked at once (bounded: a stream whose
#: sentinel never comes is evicted oldest-first).
MAX_TRACKED_STREAMS: int = 256


class HeldBack:
    """Records of one sync chunk refused only **for now** — they would land
    on a later stream, so the chunk does not count as applied and the
    stream is reported unclean (:class:`StreamHealth`); the provider keeps
    its watermark and the next periodic session streams them again,
    instead of the daily full pass.

    Only a refusal that a later stream can turn into a write counts: a
    record naming a user this space has no record of at all yet (the
    roster gossip seating them trails the stream — the same race the live
    path holds a write for), a tombstone / item whose parent is not held
    here at all yet. (A persist that failed where the caller logs rather
    than raises — the gallery album / item writes — is :meth:`fail`: an
    unclean chunk, never a hold.) A refusal by rule (held already, deleted here, a user
    known but not seated on the provider, another space's id) would be
    refused again and is not counted — it must not keep every stream
    unclean."""

    __slots__ = ("count", "failed")

    def __init__(self) -> None:
        self.count = 0
        #: A persist failed (logged by its caller rather than raised): the
        #: chunk is unclean outright, never a forgivable hold.
        self.failed = False

    def add(self, n: int = 1) -> None:
        self.count += n

    def fail(self) -> None:
        self.failed = True


#: Consecutive streams from one provider for one space that may be unclean
#: ONLY because records were held back (:class:`HeldBack`). The next one is
#: reported clean: a record that never lands (a parent this household will
#: never hold, an author it will never learn) must not keep every periodic
#: session unclean — the provider would never confirm, and past the daily
#: full-pass interval every 30-minute session would be a full stream.
HELD_BACK_LIMIT: int = 3


#: The same bound for a row that fails to persist on every stream (a gallery
#: row the database refuses for good): higher, since a failure may be a
#: passing lock — but a permanently broken row must not force a full
#: re-stream on every session forever either. Logged at WARNING.
PERSIST_FAILURE_LIMIT: int = 10


class StreamVerdict(StrEnum):
    """How a §25.6 stream ended here."""

    CLEAN = "clean"
    #: Every chunk arrived and was stored, but records were held back for
    #: a retry (:class:`HeldBack`) — unclean, bounded by
    #: :data:`HELD_BACK_LIMIT`.
    HELD_BACK = "held_back"
    #: Every chunk arrived, but a row failed to persist where its caller
    #: logs rather than raises (a gallery album / item write) — unclean,
    #: bounded by :data:`PERSIST_FAILURE_LIMIT`.
    PERSIST_FAILED = "persist_failed"
    UNCLEAN = "unclean"


class _Stream:
    """One stream's tally (module-local)."""

    __slots__ = (
        "archived",
        "failed",
        "held",
        "indices",
        "opaque",
        "pending",
        "persist_failed",
        "plain",
    )

    def __init__(self) -> None:
        #: Chunks stored that carry no index (an older provider).
        self.plain = 0
        #: Chunks of a resource we do not know and could not even open (a
        #: newer provider's): dropped by rule, counted with no index.
        self.opaque = 0
        #: Distinct ``chunk_index`` values of the chunks stored (v_56): a
        #: chunk delivered twice counts once, so a duplicate cannot stand
        #: in for a chunk that was lost.
        self.indices: set[int] = set()
        self.failed = False
        self.held = False
        self.persist_failed = False
        self.pending = 0
        #: The space's archive flag when the stream's first chunk landed —
        #: ``None`` until then (see :meth:`StreamHealth.archive_changed`).
        self.archived: bool | None = None


class StreamHealth:
    """Whether every chunk of a §25.6 stream was applied here.

    The requester tells the provider with ``SPACE_SYNC_COMPLETE {clean}``;
    only a clean stream advances the provider's incremental-sync watermark.
    A stream is clean when its sentinel names how many chunks the provider
    sent (``chunk_count``), exactly that many distinct chunks were
    **stored** here — counted once a chunk's records are persisted, never on
    arrival, and by their signed ``chunk_index`` (v_56) so a duplicate
    cannot mask a lost chunk —, none failed to decrypt / parse / persist or
    held a record back for a retry, and none is still waiting for its epoch
    key. Anything else — a chunk lost on the way, a persist that raised or
    is still running, a tampered chunk, a record refused only for now, an
    older provider that sends no count — is unclean, so the next periodic
    session re-streams it rather than the provider skipping rows this
    household never stored.

    Counting on arrival made the verdict depend on chunks being persisted
    one after another: relayed chunks are separate inbound events, so the
    sentinel can be handled while an earlier chunk still persists — and
    that persist may yet fail.
    """

    __slots__ = ("_finished", "_streams")

    def __init__(self) -> None:
        self._streams: OrderedDict[str, _Stream] = OrderedDict()
        #: Streams whose verdict was given: a chunk finishing after its
        #: sentinel changes nothing, and must not open a new entry.
        self._finished: OrderedDict[str, None] = OrderedDict()

    def _entry(self, sync_id: str) -> _Stream | None:
        if sync_id in self._finished:
            return None
        entry = self._streams.get(sync_id)
        if entry is None:
            entry = self._streams[sync_id] = _Stream()
            while len(self._streams) > MAX_TRACKED_STREAMS:
                self._streams.popitem(last=False)
        return entry

    def begun(self, sync_id: str) -> bool:
        """Has the stream been seen (or finished) already?"""
        return sync_id in self._streams or sync_id in self._finished

    def begin(self, sync_id: str, *, archived: bool) -> None:
        """Note the space's archive flag as the stream's first chunk lands."""
        entry = self._entry(sync_id)
        if entry is not None and entry.archived is None:
            entry.archived = archived

    def archive_changed(self, sync_id: str, *, archived: bool) -> bool:
        """Did the space's archive flag change while the stream ran? Read
        at the sentinel, before :meth:`finish`. A stream that saw no chunk
        recorded nothing it could have refused."""
        entry = self._streams.get(sync_id)
        return (
            entry is not None
            and entry.archived is not None
            and entry.archived != archived
        )

    def applied(self, sync_id: str, index: object = None) -> None:
        """A chunk's records are stored (or refused for good)."""
        entry = self._entry(sync_id)
        if entry is None:
            return
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            entry.indices.add(index)
        else:
            entry.plain += 1

    def opaque(self, sync_id: str) -> None:
        """A chunk of an unknown resource that could not be opened: dropped
        by rule (forward compatibility), its index unknowable — counted
        apart, so it fills no slot but its own."""
        entry = self._entry(sync_id)
        if entry is not None:
            entry.opaque += 1

    def persist_failed(self, sync_id: str, index: object = None) -> None:
        """A chunk arrived but a row of it failed to persist (logged by its
        caller rather than raised)."""
        self.applied(sync_id, index)
        entry = self._entry(sync_id)
        if entry is not None:
            entry.persist_failed = True

    def held(self, sync_id: str, index: object = None) -> None:
        """A chunk stored all but records held back for a retry."""
        self.applied(sync_id, index)
        entry = self._entry(sync_id)
        if entry is not None:
            entry.held = True

    def failed(self, sync_id: str) -> None:
        entry = self._entry(sync_id)
        if entry is not None:
            entry.failed = True

    def stashed(self, sync_id: str) -> None:
        entry = self._entry(sync_id)
        if entry is not None:
            entry.pending += 1

    def resolved(self, sync_id: str) -> None:
        entry = self._streams.get(sync_id)
        if entry is not None and entry.pending > 0:
            entry.pending -= 1

    def finish(self, sync_id: str, chunk_count: object) -> StreamVerdict:
        """End the stream and give its verdict."""
        entry = self._streams.pop(sync_id, None) or _Stream()
        self._finished[sync_id] = None
        while len(self._finished) > MAX_TRACKED_STREAMS:
            self._finished.popitem(last=False)
        if not isinstance(chunk_count, int) or isinstance(chunk_count, bool):
            return StreamVerdict.UNCLEAN
        # A numbered stream (v_56) counts its distinct indices only: an
        # unnumbered chunk in it (none should be) cannot stand in for one.
        stored = (len(entry.indices) if entry.indices else entry.plain) + entry.opaque
        if entry.failed or entry.pending or stored != chunk_count:
            return StreamVerdict.UNCLEAN
        if entry.persist_failed:
            return StreamVerdict.PERSIST_FAILED
        return StreamVerdict.HELD_BACK if entry.held else StreamVerdict.CLEAN


class HeldBackStreaks:
    """Consecutive held-back-only verdicts per (space, provider), bounded by
    :data:`HELD_BACK_LIMIT`, and consecutive persist-failure verdicts,
    bounded by :data:`PERSIST_FAILURE_LIMIT` (see there). Any other verdict
    breaks a run. In memory: a restart starts the count over — at worst
    that many more unclean sessions."""

    __slots__ = ("_streaks", "_warned")

    def __init__(self) -> None:
        self._streaks: OrderedDict[tuple[str, str, str], int] = OrderedDict()
        self._warned: OrderedDict[tuple[str, str, str], None] = OrderedDict()

    def clean(self, verdict: StreamVerdict, space_id: str, provider: str) -> bool:
        """Whether to report the stream clean."""
        kinds = {
            StreamVerdict.HELD_BACK: HELD_BACK_LIMIT,
            StreamVerdict.PERSIST_FAILED: PERSIST_FAILURE_LIMIT,
        }
        # "In a row": any verdict breaks the other kinds' runs.
        for kind in kinds:
            if kind is not verdict:
                self._streaks.pop((space_id, provider, kind.value), None)
        if verdict is StreamVerdict.CLEAN:
            return True
        limit = kinds.get(verdict)
        if limit is None:
            return False
        key = (space_id, provider, verdict.value)
        streak = self._streaks.pop(key, 0) + 1
        if streak < limit:
            self._streaks[key] = streak
            while len(self._streaks) > MAX_TRACKED_STREAMS:
                self._streaks.popitem(last=False)
            return False
        if key not in self._warned:
            self._warned[key] = None
            while len(self._warned) > MAX_TRACKED_STREAMS:
                self._warned.popitem(last=False)
            log.warning(
                "sync: %d streams in a row from %s for space %s %s — taking "
                "them as refused and reporting this stream clean",
                streak,
                provider,
                space_id,
                "held records back that never landed"
                if verdict is StreamVerdict.HELD_BACK
                else "failed to store the same rows",
            )
        return True
