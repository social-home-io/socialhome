"""The bounded, expiring hold under both inbound wait buffers."""

from __future__ import annotations

from socialhome.federation.bounded_hold import BoundedHold


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _hold(**kw) -> BoundedHold[str]:
    args = {
        "ttl_seconds": 60.0,
        "max_entries": 10,
        "max_per_key": 5,
        "max_per_sender": 4,
        "max_bytes": 1000,
    }
    args.update(kw)
    return BoundedHold(**args)


def test_release_hands_back_oldest_first_and_forgets() -> None:
    clock = _Clock()
    h = _hold(clock=clock)
    assert h.hold(("sp", "post", "p"), "a", token=1, sender="s", size=10)
    clock.now += 1
    assert h.hold(("sp", "post", "p"), "b", token=2, sender="s", size=10)
    clock.now += 1
    assert h.hold(("sp", "post", "q"), "c", token=3, sender="s", size=10)
    assert h.release(("sp", "post", "q"), ("sp", "post", "p")) == ["a", "b", "c"]
    assert len(h) == 0
    assert h.held_bytes == 0
    assert h.release(("sp", "post", "p")) == []


def test_a_duplicate_token_takes_no_second_slot() -> None:
    h = _hold()
    assert h.hold(("sp", "post", "p"), "a", token="t", sender="s", size=10)
    assert h.hold(("sp", "post", "p"), "a2", token="t", sender="s", size=10)
    assert len(h) == 1
    assert h.release(("sp", "post", "p")) == ["a"]


def test_every_bound_is_enforced() -> None:
    per_sender = _hold()
    for n in range(4):
        assert per_sender.hold(("sp", "k", str(n)), n, token=n, sender="s", size=1)
    assert not per_sender.hold(("sp", "k", "x"), 9, token=9, sender="s", size=1)
    assert per_sender.hold(("sp", "k", "x"), 9, token=9, sender="other", size=1)

    per_key = _hold(max_per_sender=100)
    for n in range(5):
        assert per_key.hold(("sp", "k", "p"), n, token=n, sender=f"s{n}", size=1)
    assert not per_key.hold(("sp", "k", "p"), 9, token=9, sender="s9", size=1)

    entries = _hold(max_per_sender=100, max_per_key=100, max_entries=3)
    for n in range(3):
        assert entries.hold(("sp", "k", str(n)), n, token=n, sender=f"s{n}", size=1)
    assert not entries.hold(("sp", "k", "x"), 9, token=9, sender="s9", size=1)

    byte_cap = _hold(max_bytes=100)
    assert byte_cap.hold(("sp", "k", "p"), 1, token=1, sender="s", size=60)
    assert not byte_cap.hold(("sp", "k", "p"), 2, token=2, sender="s", size=60)
    assert byte_cap.held_bytes == 60


def test_expired_entries_are_dropped_and_reported() -> None:
    clock = _Clock()
    expired: list = []
    h = _hold(clock=clock, on_expire=lambda key, entry: expired.append((key, entry)))
    h.hold(("sp", "post", "p"), "old", token=1, sender="s", size=10)
    clock.now += 30
    h.hold(("sp", "post", "p"), "new", token=2, sender="s", size=10)
    clock.now += 31
    assert h.release(("sp", "post", "p")) == ["new"]
    assert expired == [(("sp", "post", "p"), "old")]
    assert h.held_bytes == 0


def test_expiry_frees_the_slots() -> None:
    clock = _Clock()
    h = _hold(clock=clock, max_entries=1, max_per_sender=1)
    assert h.hold(("sp", "k", "p"), 1, token=1, sender="s", size=1)
    assert not h.hold(("sp", "k", "q"), 2, token=2, sender="s", size=1)
    clock.now += 61
    h.purge()
    assert len(h) == 0
    assert h.hold(("sp", "k", "q"), 2, token=2, sender="s", size=1)


def test_the_per_sender_byte_budget_and_entry_cap() -> None:
    h = _hold(max_per_sender=100, max_bytes=1000, max_bytes_per_sender=100)
    assert h.hold(("sp", "k", "p"), 1, token=1, sender="s", size=60)
    assert not h.hold(("sp", "k", "p"), 2, token=2, sender="s", size=60)
    assert h.hold(("sp", "k", "p"), 3, token=3, sender="t", size=60)
    capped = _hold(max_entry_bytes=50)
    assert not capped.hold(("sp", "k", "p"), 1, token=1, sender="s", size=51)
    assert capped.hold(("sp", "k", "p"), 2, token=2, sender="s", size=50)
