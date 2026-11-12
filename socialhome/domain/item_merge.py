"""The one merge rule for a change to an existing space item.

A space item (a post, a poll, a date poll, an event, a listing) is created
once; what people do to it afterwards — react, vote, answer, RSVP, edit —
reaches a household live, over several transports, from an outbox retry
and from the host's §25.6 stream, in any order and more than once. Every
such change is merged the same way:

* **A stamp per (item, author[, key]).** The author's household stamps the
  change when it is made (:func:`local_stamp`: its clock, never at or
  before the stamp it replaces) and sends the stamp with it. A change from
  a household that sends none (an older peer) is stamped with its
  envelope's timestamp (:func:`change_stamp`).
* **The newest stamp wins** (:func:`is_newer`; a tie changes nothing), so
  the result is the same in any arrival order, and a re-delivery is a
  no-op.
* **A removal is a stamp too** (a tombstone, kept
  :data:`TOMBSTONE_HORIZON_S`), so a late copy of what was removed never
  brings it back.
* **A stamp is bounded**: no more than the live skew ahead of the
  receiver's clock (:func:`~socialhome.domain.space_item.item_stamp`) — one
  far ahead would win every later change.

Where an item has a **cutoff** after which no change counts (a poll's
close), a change stamped after it counts nowhere and the newest one stamped
at or before it stands everywhere (:func:`standing`). A household may have
applied a change before it heard of the cutoff, so it keeps an author's
last few changes (:func:`record_change`) to fall back on.

One kind of change is not a last-writer-wins value and keeps its own rule:
a state that only ever **advances** (a bazaar bid: pending → declined /
withdrawn / accepted). Its merge is the highest rank (:func:`advance`) —
also the same in any order, and it needs no stamp.

Pure: no I/O. Stored stamps are naive UTC with microseconds
(:func:`~socialhome.domain.space_item.stamp_to_db`), so they compare as
strings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import orjson

from .federation_retention import RETENTION_DAYS
from .space_item import item_stamp, stamp_to_db

#: How long a removal's tombstone is kept: 30 days, the reach of a §25.6
#: resume and of the catch-up media window — longer than any duplicate can
#: stay in flight (the outbox's 7 days, a GFS queue's 24 h plus retries).
TOMBSTONE_HORIZON_S: int = 30 * 24 * 3600

#: How long after an item's cutoff a change stamped before it is still
#: taken: the outbox's retention — nothing honest stays queued longer. It
#: bounds how far back a household can date a change of its own people.
LATE_CHANGE_WINDOW_S: int = RETENTION_DAYS * 24 * 3600

#: How many of one author's changes to one item a household keeps while the
#: item has no cutoff yet (newest first). One is the value; the rest are
#: what :func:`standing` falls back on when a cutoff lands between them.
MAX_KEPT_CHANGES: int = 4


def change_stamp(
    raw: object, *, envelope_timestamp: str, now: datetime | None = None
) -> str | None:
    """The stored stamp of a received change.

    ``raw`` is the stamp the author's household sent (tz-aware ISO 8601);
    a household that sends none — an older peer — is stamped with its
    envelope's signed timestamp, so its change is merged like any other and
    never dropped. ``None``: ``raw`` is present but malformed or too far
    ahead of our clock (refused, like a reaction's ``at``)."""
    at = now or datetime.now(UTC)
    if raw is None:
        return stamp_to_db(item_stamp(envelope_timestamp, now=at) or at)
    stamp = item_stamp(raw, now=at)
    return stamp_to_db(stamp) if stamp is not None else None


def is_newer(at: str, held: str | None) -> bool:
    """Does a change stamped ``at`` replace the one stamped ``held``? Only
    a strictly newer one does: a re-delivery (the same stamp) is a no-op."""
    return held is None or at > held


def just_after(stamp: str) -> str:
    """The stored stamp one microsecond after ``stamp``."""
    try:
        held = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp_to_db(datetime.now(UTC))
    if held.tzinfo is None:
        held = held.replace(tzinfo=UTC)
    return stamp_to_db(held + timedelta(microseconds=1))


def local_stamp(held: str | None, *, now: datetime | None = None) -> str:
    """The stamp of a change made HERE: our clock — or, when the clock
    stepped back (or the held stamp came from one ahead), just after the
    stamp this change replaces, so a person's change is never older than
    the one before it."""
    at = stamp_to_db(now or datetime.now(UTC))
    return at if is_newer(at, held) else just_after(held or at)


def to_wire(stamp: str | None) -> str | None:
    """A stored stamp as it travels: tz-aware ISO 8601 (``None`` stays)."""
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def to_stored(raw: object) -> str | None:
    """Any ISO 8601 time (naive = UTC) in the stored stamp shape — for a
    time that is not a change stamp and may lie ahead (a poll's deadline).
    ``None`` for anything else."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return stamp_to_db(parsed)


def decode_stamp_map(raw: object) -> dict[str, list]:
    """A stored stamp map (a JSON object whose values are lists) — ``{}``
    for anything else. The caller shapes the entries."""
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = orjson.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, list)}


def tombstone_cutoff(now: datetime | None = None) -> str:
    """Tombstones stamped before this are dropped."""
    return stamp_to_db(
        (now or datetime.now(UTC)) - timedelta(seconds=TOMBSTONE_HORIZON_S)
    )


# ── A value with a cutoff: the author's last few changes ──────────────────


def record_change(
    entries: list[list], at: str, value: object, *, keep: int = MAX_KEPT_CHANGES
) -> bool:
    """Add the change ``[at, value]`` to one author's ``entries`` (newest
    first, at most ``keep``). ``False`` — nothing changed — for a stamp
    already held (a re-delivery) or one older than every kept entry of a
    full list. The same entries result in any arrival order."""
    if any(e[0] == at for e in entries):
        return False
    entries.append([at, value])
    entries.sort(key=lambda e: e[0], reverse=True)
    dropped = entries[keep:]
    del entries[keep:]
    return not any(e[0] == at for e in dropped)


def standing(entries: list[list], cutoff: str | None = None) -> list | None:
    """The change that counts: the newest of ``entries`` — stamped at or
    before ``cutoff`` when the item has one. ``None``: none counts."""
    for entry in entries:
        if cutoff is None or entry[0] <= cutoff:
            return entry
    return None


def settle(entries: list[list], cutoff: str) -> None:
    """The item's cutoff is known: keep only the change that stands."""
    entry = standing(entries, cutoff)
    entries[:] = [entry] if entry is not None else []


def too_late(cutoff: str, *, now: datetime | None = None) -> bool:
    """Is ``cutoff`` so long ago that no honest change stamped before it
    can still be in flight (:data:`LATE_CHANGE_WINDOW_S`)?"""
    limit = (now or datetime.now(UTC)) - timedelta(seconds=LATE_CHANGE_WINDOW_S)
    return cutoff < stamp_to_db(limit)


# ── A state that only advances ────────────────────────────────────────────


def advance(held: int, incoming: int) -> int:
    """Merge two ranks of a state that only ever advances: the higher one.
    Commutative, associative and idempotent — any arrival order, the same
    state — so it needs no stamp."""
    return max(held, incoming)
