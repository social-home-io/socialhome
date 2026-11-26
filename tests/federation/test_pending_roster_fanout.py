"""Fan-outs a just-joined household made before the host's roster came."""

from __future__ import annotations

from socialhome.domain.federation import FederationEventType
from socialhome.federation.pending_roster_fanout import (
    PendingFanout,
    PendingRosterFanout,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _fanout(n: int) -> PendingFanout:
    return PendingFanout(
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload={"id": f"p{n}"},
        reached=frozenset({"host"}),
    )


def _ids(fanouts: list[PendingFanout]) -> list[str]:
    return [f.payload["id"] for f in fanouts]


def test_only_a_waiting_space_keeps_its_fanouts_until_its_wait_ends():
    pending = PendingRosterFanout()
    assert pending.record("sp", _fanout(1), size=10) is False
    pending.expect("sp")
    assert pending.waiting("sp")
    assert pending.record("sp", _fanout(1), size=10)
    assert pending.record("sp", _fanout(2), size=10)
    assert _ids(pending.take("sp")) == ["p1", "p2"]
    # Taking does not end the wait: what comes meanwhile is kept too.
    assert pending.record("sp", _fanout(3), size=10)
    assert _ids(pending.take("sp")) == ["p3"]
    pending.end("sp")
    assert not pending.waiting("sp")
    assert pending.record("sp", _fanout(4), size=10) is False
    assert pending.take("sp") == []


def test_the_wait_and_what_it_kept_end_after_the_ttl():
    clock = _Clock()
    pending = PendingRosterFanout(ttl_seconds=300.0, clock=clock)
    pending.expect("sp")
    pending.record("sp", _fanout(1), size=10)
    clock.now += 301.0
    assert not pending.waiting("sp")
    assert pending.take("sp") == []
    # A late re-expect starts empty: nothing from the old wait comes back.
    pending.expect("sp")
    assert pending.take("sp") == []


def test_a_refused_fanout_ends_the_whole_wait():
    """All or nothing: a create kept without its later delete would bring
    deleted content back."""
    pending = PendingRosterFanout(max_per_space=2, max_bytes=25)
    pending.expect("sp")
    assert pending.record("sp", _fanout(1), size=10)
    assert pending.record("sp", _fanout(2), size=10)
    assert pending.record("sp", _fanout(3), size=1) is False
    assert not pending.waiting("sp")
    assert pending.take("sp") == []
    pending.expect("sp2")
    assert pending.record("sp2", _fanout(4), size=26) is False  # bytes
    assert not pending.waiting("sp2")


def test_the_space_whose_wait_ends_first_gives_way_when_every_slot_is_taken():
    clock = _Clock()
    pending = PendingRosterFanout(max_spaces=2, clock=clock)
    pending.expect("old")
    pending.record("old", _fanout(1), size=10)
    clock.now += 1.0
    pending.expect("mid")
    clock.now += 1.0
    pending.expect("new")
    assert not pending.waiting("old")
    assert pending.take("old") == []
    assert pending.waiting("mid") and pending.waiting("new")
    # Expecting a space already waiting takes no second slot.
    pending.expect("mid")
    assert pending.waiting("new")
