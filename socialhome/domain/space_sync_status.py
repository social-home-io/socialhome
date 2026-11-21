"""Whether a space needs sync, from the outcomes its acting hosts gave us.

Each sync of a space from a household acting as its host here (the owner, or
the trusted inviting admin — ``federation/host_equivalence.acting_hosts``)
ends in one :class:`SpaceSyncResult`, kept on that household's
``space_instances`` row (migration 0150) with the time of its last ``ok``.
:func:`sync_state` turns those rows into one :class:`SpaceSyncState` at read
time — nothing about the state itself is stored.

Pure: no I/O. The service that reads the rows and knows whether a stream is
running is ``services/space_sync_status.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class SpaceSyncResult(StrEnum):
    """How a host's last answer to our ``SPACE_SYNC_BEGIN`` ended."""

    #: A clean stream, or the quiet sentinel of an up-to-date space.
    OK = "ok"
    #: Records held back or a chunk lost: the next round streams again.
    UNCLEAN = "unclean"
    #: Refused for now (busy, its hourly budget spent) or cut mid-stream.
    DEFERRED = "deferred"
    #: A round's BEGIN went unanswered for the scheduler's full wait.
    UNREACHABLE = "unreachable"

    @classmethod
    def parse(cls, raw: object) -> SpaceSyncResult | None:
        try:
            return cls(str(raw)) if raw is not None else None
        except ValueError:
            return None


class SpaceSyncState(StrEnum):
    """What a member sees about a space it does not host."""

    UP_TO_DATE = "up_to_date"
    CATCHING_UP = "catching_up"
    BEHIND = "behind"
    HOST_OFFLINE = "host_offline"
    NEVER = "never"


#: An ``ok`` this recent is up to date whatever came after it but an unclean
#: stream (a host busy for a moment does not make a space behind). Hosts are
#: asked every 30 minutes, so a healthy space refreshes it twice an hour.
FRESH_OK_S: float = 60 * 60
#: With ``ok`` as the last result, up to date until no ``ok`` came for this
#: long (two missed rounds and slack); past it the space is behind.
STALE_OK_S: float = 2 * 60 * 60


@dataclass(slots=True, frozen=True)
class SpaceSyncOutcome:
    """One acting host's row: its last result and its last ``ok`` (UTC ISO
    8601, ``None`` if never)."""

    instance_id: str
    result: SpaceSyncResult | None
    ok_at: str | None


@dataclass(slots=True, frozen=True)
class SpaceSyncStatus:
    """``GET /api/spaces/{id}`` ``sync`` and the ``space.sync_state`` frame.
    ``waiting_to_send`` — our own items of the space still in the outbox."""

    state: SpaceSyncState
    last_ok_at: str | None
    waiting_to_send: int
    #: v_95 — what the newest signed sync page says (``SyncPagePosition``
    #: wire form; ``None``: no page held).
    page: dict | None = None

    def to_wire(self) -> dict:
        wire: dict = {
            "state": self.state.value,
            "last_ok_at": self.last_ok_at,
            "waiting_to_send": self.waiting_to_send,
        }
        if self.page is not None:
            wire["page"] = self.page
        return wire


def parse_utc(raw: str | None) -> datetime | None:
    """A stored UTC time — ISO 8601 or SQLite's naive ``YYYY-MM-DD HH:MM:SS``
    — as an aware datetime; ``None`` if absent or unreadable."""
    if not raw:
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def sync_state(
    outcomes: Iterable[SpaceSyncOutcome],
    *,
    receiving: bool = False,
    now: datetime,
) -> tuple[SpaceSyncState, str | None]:
    """The state and the newest ``ok`` over the acting hosts' *outcomes*.

    In order: **up to date** when a host answered ``ok`` within
    :data:`STALE_OK_S`, or its ``ok`` is within :data:`FRESH_OK_S` and only a
    refusal followed; **catching up** while a stream from one is running
    (*receiving*); **host offline** when a host was found unreachable and
    none answered since; **never** while no stream arrived (no ``ok``, no
    unclean one); else **behind**."""
    rows = list(outcomes)
    oks = [(parse_utc(o.ok_at), o) for o in rows]
    newest = max(
        ((at, o) for at, o in oks if at is not None),
        key=lambda pair: pair[0],
        default=None,
    )
    last_ok = newest[1].ok_at if newest is not None else None
    for at, o in oks:
        if at is None:
            continue
        age = now - at
        if o.result is SpaceSyncResult.OK and age <= timedelta(seconds=STALE_OK_S):
            return SpaceSyncState.UP_TO_DATE, last_ok
        if o.result is SpaceSyncResult.DEFERRED and age <= timedelta(
            seconds=FRESH_OK_S
        ):
            return SpaceSyncState.UP_TO_DATE, last_ok
    if receiving:
        return SpaceSyncState.CATCHING_UP, last_ok
    unreachable = any(o.result is SpaceSyncResult.UNREACHABLE for o in rows)
    answering = any(
        o.result is not None and o.result is not SpaceSyncResult.UNREACHABLE
        for o in rows
    )
    if unreachable and not answering:
        return SpaceSyncState.HOST_OFFLINE, last_ok
    if last_ok is None and not any(o.result is SpaceSyncResult.UNCLEAN for o in rows):
        # Nothing arrived yet (a refusal is no stream).
        return SpaceSyncState.NEVER, None
    return SpaceSyncState.BEHIND, last_ok
