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
#: A host's last answer (any result) older than this — two missed rounds —
#: no longer counts as the host answering: with another host found
#: unreachable, the space reads "host offline".
ANSWER_STALE_S: float = 60 * 60
#: "Some posts may be missing" waits for the SECOND failed round after the
#: newest ``ok``: rounds come every 30 minutes, so that round's answer is at
#: least this long after the ``ok`` (one failed round is a hiccup the next
#: round usually fixes — nobody is warned about it).
MISSING_AFTER_S: float = 45 * 60


@dataclass(slots=True, frozen=True)
class SpaceSyncOutcome:
    """One acting host's row: its last result, its last ``ok`` and when
    that last result came (UTC ISO 8601, ``None`` if never / not known)."""

    instance_id: str
    result: SpaceSyncResult | None
    ok_at: str | None
    result_at: str | None


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
    #: "Some posts may be missing" — :func:`may_be_missing`; the one
    #: warning the SPA shows (header chip, Spaces list dot).
    may_be_missing: bool = False
    #: Posts written here that no other household has yet ("Not sent
    #: yet" on the post): every member household still waits for them in
    #: our outbox and no GFS took them. Newest first, bounded.
    unsent_posts: tuple[str, ...] = ()

    def to_wire(self) -> dict:
        wire: dict = {
            "state": self.state.value,
            "last_ok_at": self.last_ok_at,
            "waiting_to_send": self.waiting_to_send,
            "may_be_missing": self.may_be_missing,
            "unsent_posts": list(self.unsent_posts),
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


def answered_at(outcome: SpaceSyncOutcome) -> datetime | None:
    """When *outcome*'s host last answered (any result but ``unreachable``):
    the time of its last result, or for an ``ok`` without one its ``ok``
    time; ``None`` when it never answered or the time is not known."""
    if outcome.result is None or outcome.result is SpaceSyncResult.UNREACHABLE:
        return None
    at = parse_utc(outcome.result_at)
    if at is None and outcome.result is SpaceSyncResult.OK:
        at = parse_utc(outcome.ok_at)
    return at


def sync_state(
    outcomes: Iterable[SpaceSyncOutcome],
    *,
    receiving: bool = False,
    now: datetime,
) -> tuple[SpaceSyncState, str | None]:
    """The state and the newest ``ok`` over the acting hosts' *outcomes*.

    A host is **offline** for the space when one was found unreachable and
    none answered (any result) within :data:`ANSWER_STALE_S` — a last
    answer two rounds old is no answer, whatever it said. Then, in order:
    **up to date** (not offline) when a host answered ``ok`` within
    :data:`STALE_OK_S`, or its ``ok`` is within :data:`FRESH_OK_S` and only
    a refusal followed; **catching up** while a stream from one is running
    (*receiving*); **host offline**; **never** while no stream arrived (no
    ``ok``, no unclean one); else **behind**."""
    rows = list(outcomes)
    oks = [(parse_utc(o.ok_at), o) for o in rows]
    newest = max(
        ((at, o) for at, o in oks if at is not None),
        key=lambda pair: pair[0],
        default=None,
    )
    last_ok = newest[1].ok_at if newest is not None else None
    unreachable = any(o.result is SpaceSyncResult.UNREACHABLE for o in rows)
    answering = any(
        at is not None and now - at <= timedelta(seconds=ANSWER_STALE_S)
        for at in map(answered_at, rows)
    )
    offline = unreachable and not answering
    for at, o in [] if offline else oks:
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
    if offline:
        return SpaceSyncState.HOST_OFFLINE, last_ok
    if last_ok is None and not any(o.result is SpaceSyncResult.UNCLEAN for o in rows):
        # Nothing arrived yet (a refusal is no stream).
        return SpaceSyncState.NEVER, None
    return SpaceSyncState.BEHIND, last_ok


def may_be_missing(
    state: SpaceSyncState,
    outcomes: Iterable[SpaceSyncOutcome],
    *,
    proven_missing: bool = False,
) -> bool:
    """Whether to tell the member "Some posts may be missing".

    Never while a stream is arriving (**catching up**). Yes when the newest
    signed sync page PROVED a row missing (*proven_missing*: ``match`` false
    with ``missing`` > 0 — known only after the round that carried the
    listing, so it is still true after that automatic sync). Otherwise only
    **behind** counts, and only from the second failed round after the
    newest ``ok`` (:data:`MISSING_AFTER_S`) — or with no ``ok`` ever while a
    stream arrived unclean. An away host (**host offline**) and a space not
    synced yet (**never**) are no warning."""
    if state is SpaceSyncState.CATCHING_UP:
        return False
    if proven_missing:
        return True
    if state is not SpaceSyncState.BEHIND:
        return False
    rows = list(outcomes)
    oks = [at for at in (parse_utc(o.ok_at) for o in rows) if at is not None]
    if not oks:
        return True
    answers = [at for at in map(answered_at, rows) if at is not None]
    if not answers:
        return False
    return max(answers) - max(oks) >= timedelta(seconds=MISSING_AFTER_S)
