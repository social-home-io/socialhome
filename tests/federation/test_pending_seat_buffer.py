"""§24.11 — writes held until the seat they need reaches the roster mirror."""

from __future__ import annotations

from datetime import datetime, timezone

from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation.pending_seat_buffer import (
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_PER_SENDER,
    PendingSeatBuffer,
)


def _ev(
    n: int = 0,
    *,
    size: int = 10,
    et=FederationEventType.SPACE_POST_CREATED,
    sender: str = "inst-j",
):
    return FederationEvent(
        msg_id=f"m{n}",
        event_type=et,
        from_instance=sender,
        to_instance="us",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={"id": f"p{n}", "content": "x" * size},
        space_id="sp",
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_held_write_is_released_by_the_matching_user_seat() -> None:
    buf = PendingSeatBuffer()
    assert buf.hold(_ev(1), space_id="sp", user_id="u-j")
    assert buf.release(space_id="sp", instance_id="inst-x", user_id="u-other") == []
    released = buf.release(space_id="sp", instance_id="inst-x", user_id="u-j")
    assert [e.msg_id for e in released] == ["m1"]
    assert buf.release(space_id="sp", instance_id="inst-x", user_id="u-j") == []


def test_a_held_write_is_released_by_the_senders_first_seat() -> None:
    buf = PendingSeatBuffer()
    assert buf.hold(_ev(1), space_id="sp", instance_id="inst-j")
    assert buf.hold(_ev(2), space_id="sp", instance_id="inst-j")
    released = buf.release(space_id="sp", instance_id="inst-j", user_id="u-j")
    assert [e.msg_id for e in released] == ["m1", "m2"]


def test_another_space_does_not_release() -> None:
    buf = PendingSeatBuffer()
    buf.hold(_ev(1), space_id="sp", user_id="u-j")
    assert buf.release(space_id="sp-2", instance_id="i", user_id="u-j") == []
    assert len(buf) == 1


def test_entries_expire() -> None:
    clock = _Clock()
    buf = PendingSeatBuffer(ttl_seconds=60, clock=clock)
    buf.hold(_ev(1), space_id="sp", user_id="u-j")
    clock.now += 61
    assert buf.release(space_id="sp", instance_id="i", user_id="u-j") == []
    assert len(buf) == 0


def test_the_total_entry_cap_is_enforced() -> None:
    buf = PendingSeatBuffer(max_entries=3, max_per_key=10)
    assert all(buf.hold(_ev(n), space_id="sp", user_id=f"u{n}") for n in range(3))
    assert not buf.hold(_ev(9), space_id="sp", user_id="u9")
    assert len(buf) == 3


def test_the_per_key_cap_is_enforced() -> None:
    buf = PendingSeatBuffer(max_per_key=2)
    assert buf.hold(_ev(1), space_id="sp", user_id="u")
    assert buf.hold(_ev(2), space_id="sp", user_id="u")
    assert not buf.hold(_ev(3), space_id="sp", user_id="u")


def test_the_byte_cap_is_enforced() -> None:
    buf = PendingSeatBuffer(max_bytes=500)
    assert buf.hold(_ev(1, size=300), space_id="sp", user_id="a")
    assert not buf.hold(_ev(2, size=300), space_id="sp", user_id="b")
    buf.release(space_id="sp", instance_id="i", user_id="a")
    assert buf.hold(_ev(2, size=300), space_id="sp", user_id="b")


def test_media_bytes_are_never_held() -> None:
    buf = PendingSeatBuffer()
    assert not buf.hold(
        _ev(1, et=FederationEventType.SPACE_MEDIA_BLOB), space_id="sp", user_id="u"
    )


def test_a_hold_needs_a_key() -> None:
    buf = PendingSeatBuffer()
    assert not buf.hold(_ev(1), space_id="sp")
    assert not buf.hold(_ev(1), space_id="", user_id="u")


def test_the_same_event_is_held_once() -> None:
    buf = PendingSeatBuffer()
    ev = _ev(1)
    assert buf.hold(ev, space_id="sp", user_id="u")
    assert buf.hold(ev, space_id="sp", user_id="u")
    assert len(buf) == 1


def test_one_sending_household_cannot_take_every_slot() -> None:
    """A household's held writes are capped before the shared cap, so
    writes for many different user ids from one sender leave room for
    other households' held writes."""
    buf = PendingSeatBuffer(max_entries=10, max_per_key=10, max_per_sender=4)
    held = [
        buf.hold(_ev(n, sender="inst-noisy"), space_id="sp", user_id=f"made-up-{n}")
        for n in range(8)
    ]
    assert held == [True] * 4 + [False] * 4
    assert len(buf) == 4
    # Another household still gets its write held.
    assert buf.hold(_ev(100, sender="inst-other"), space_id="sp", user_id="u-real")
    # And the per-sender cap spans spaces and key kinds.
    assert not buf.hold(_ev(200, sender="inst-noisy"), space_id="sp-2", user_id="x")
    assert not buf.hold(
        _ev(201, sender="inst-noisy"), space_id="sp", instance_id="inst-noisy"
    )


def test_a_released_or_expired_entry_frees_the_senders_share() -> None:
    clock = _Clock()
    buf = PendingSeatBuffer(max_per_sender=1, ttl_seconds=60, clock=clock)
    assert buf.hold(_ev(1), space_id="sp", user_id="a")
    assert not buf.hold(_ev(2), space_id="sp", user_id="b")
    buf.release(space_id="sp", instance_id="i", user_id="a")
    assert buf.hold(_ev(2), space_id="sp", user_id="b")
    clock.now += 61
    assert buf.hold(_ev(3), space_id="sp", user_id="c")


def test_the_default_per_sender_cap_is_below_the_shared_cap() -> None:
    buf = PendingSeatBuffer()
    n = 0
    while buf.hold(_ev(n), space_id="sp", user_id=f"u{n}"):
        n += 1
    assert n == DEFAULT_MAX_PER_SENDER < DEFAULT_MAX_ENTRIES
