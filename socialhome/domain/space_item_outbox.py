"""The durable retry queue of a GFS space's content items (v_97, migration
0158) — the row shape and its rules.

Owner 2026-11-27: content in a GFS space waits in a persisted queue until
every planned GFS accepted it — never a fallback to the legacy per-household
fan-out. A permanent refusal, an item too large for any GFS, a full queue
or a sealing failure is shown to the author ("Couldn't send to {space}")
with Retry / Discard.
"""

from __future__ import annotations

from dataclasses import dataclass

#: An item the GFS refused for good (a 4xx other than 408 / 429).
UNSENT_REFUSED: str = "refused"
#: The sealed item is larger than any GFS takes.
UNSENT_TOO_LARGE: str = "too_large"
#: The space's queue was full: the item was never stored.
UNSENT_QUEUE_FULL: str = "queue_full"
#: Sealing failed (a bug, logged at WARNING): nothing was stored.
UNSENT_SEAL_FAILED: str = "seal_failed"

#: The item waited so long that receivers would drop it (its signed
#: stamp is too old): never sent, Discard only.
UNSENT_TOO_OLD: str = "too_old"

UNSENT_ERRORS: frozenset[str] = frozenset(
    {
        UNSENT_REFUSED,
        UNSENT_TOO_LARGE,
        UNSENT_QUEUE_FULL,
        UNSENT_SEAL_FAILED,
        UNSENT_TOO_OLD,
    }
)

#: v_97 — the kind (``event_type``) of a row that uploads one space file
#: blob (or one piece of a larger file) to one GFS's media cache: the same
#: durable queue as the items, so a file waits, retries and is shown to its
#: author exactly like content. Its payload is a locator, never the bytes.
MEDIA_UPLOAD_ROW: str = "space_media_upload"

#: ``gfs_id`` of an item whose GFS list was empty when it was sealed: it is
#: planned when it is due.
UNPLANNED_GFS: str = ""

#: Items (rows) one space may hold queued or failed. An active person's
#: chat at 1/s for half an hour of GFS outage is 1 800 rows; a household
#: rarely has more than a few people writing at once.
SPACE_QUEUE_MAX_ITEMS: int = 2_000
#: Bytes of ciphertext one space may hold (2 000 chat items ≈ 5.6 MB; the
#: rest is room for a few large posts).
SPACE_QUEUE_MAX_BYTES: int = 32 * 1024 * 1024
#: Failed items one space keeps WITH their bytes (for Retry); past either
#: bound a newly failed item keeps no bytes and only counts on its author's
#: one counter row — a GFS refusing everything can't fill the disk.
SPACE_FAILED_MAX_ITEMS: int = 200
SPACE_FAILED_MAX_BYTES: int = 8 * 1024 * 1024

#: Wait before retry *n + 1* of a GFS's head item; the last step repeats
#: until the GFS accepts — a GFS that is down means waiting (a reconnect
#: makes everything due at once).
RETRY_BACKOFF_S: tuple[float, ...] = (5.0, 15.0, 60.0, 300.0, 600.0)
#: Ceiling on a ``Retry-After`` we honour (a hostile server must not park
#: the queue for a day).
MAX_RETRY_AFTER_S: float = 900.0


def backoff_s(attempts: int, retry_after_s: float | None = None) -> float:
    """Seconds before the next try after ``attempts`` failed tries; a
    longer ``Retry-After`` (capped) wins."""
    step = RETRY_BACKOFF_S[min(max(attempts, 1), len(RETRY_BACKOFF_S)) - 1]
    if retry_after_s is not None:
        step = max(step, min(retry_after_s, MAX_RETRY_AFTER_S))
    return step


@dataclass(slots=True, frozen=True)
class QueuedSpaceItem:
    """One ``space_item_outbox`` row: an item for one planned GFS."""

    item_id: str
    gfs_id: str
    space_id: str
    epoch: int
    ring_class: str
    event_type: str
    author_user_id: str
    payload: str | None
    next_attempt_at: str
    created_at: str
    supersede_key: str | None = None
    attempts: int = 0
    status: str = "pending"
    error: str | None = None
    #: Times planned afresh after its GFS stopped serving the space.
    replans: int = 0
    #: Another row of the item was accepted (this one only drops).
    delivered: bool = False

    @property
    def bytes(self) -> int:
        return len(self.payload) if self.payload is not None else 0

    @property
    def retryable(self) -> bool:
        """Whether Retry can send it (it kept its bytes)."""
        return self.payload is not None


@dataclass(slots=True, frozen=True)
class UnsentItem:
    """What the author sees of one item that could not be sent."""

    item_id: str
    event_type: str
    error: str
    created_at: str
    retryable: bool
    #: How many items the row stands for (a full queue counts its refusals
    #: on one row per author).
    count: int = 1

    def to_wire(self) -> dict:
        return {
            "item_id": self.item_id,
            "event_type": self.event_type,
            "error": self.error,
            "created_at": self.created_at,
            "retryable": self.retryable,
            "count": self.count,
        }
