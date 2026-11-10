"""Tests for :mod:`socialhome.federation.sync.space.host_contact`."""

from __future__ import annotations

from datetime import UTC, datetime

from socialhome.federation.sync.space import host_contact as host_contact_mod
from socialhome.federation.sync.space.host_contact import HostContact


class _Clock:
    def __init__(self, at: float = 1000.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


def _wall() -> datetime:
    return datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def test_a_fresh_household_counts_silence_from_its_start() -> None:
    """Baseline = construction time: a household that just booted has
    heard nobody yet, and waits a full window before it calls it silence."""
    clock = _Clock()
    contact = HostContact(clock=clock, wall=_wall)
    assert contact.silent_for("sp") == 0.0
    clock.at += 90.0
    assert contact.silent_for("sp") == 90.0
    assert contact.last_heard_iso("sp") is None
    assert contact.heard_since("sp", 1000.0) is False


def test_heard_marks_the_space_with_both_clocks() -> None:
    clock = _Clock()
    contact = HostContact(clock=clock, wall=_wall)
    clock.at = 1500.0
    t0 = contact.now()
    contact.heard("sp")
    assert contact.heard_since("sp", t0) is True
    assert contact.heard_since("sp", t0 + 1) is False
    assert contact.last_heard_iso("sp") == "2026-11-10T12:00:00+00:00"
    clock.at = 1530.0
    assert contact.silent_for("sp") == 30.0
    # Another space is still silent since the start.
    assert contact.silent_for("other") == 530.0
    assert contact.heard_since("other", 0.0) is False


def test_an_empty_space_id_is_ignored() -> None:
    contact = HostContact(clock=_Clock(), wall=_wall)
    contact.heard("")
    assert contact.last_heard_iso("") is None


def test_memory_stays_bounded(monkeypatch) -> None:
    monkeypatch.setattr(host_contact_mod, "MAX_SPACES_TRACKED", 2)
    contact = HostContact(clock=_Clock(), wall=_wall)
    for space in ("a", "b", "c"):
        contact.heard(space)
    assert contact.last_heard_iso("a") is None
    assert contact.last_heard_iso("b") is not None
    # Hearing one again keeps it the newest.
    contact.heard("b")
    contact.heard("d")
    assert contact.last_heard_iso("b") is not None
    assert contact.last_heard_iso("c") is None


def test_a_helper_ask_is_remembered_per_space() -> None:
    """The scheduler's next helper resume starts from the last one it sent
    (minus an overlap), not from the host's last answer again."""
    contact = HostContact(clock=_Clock(), wall=_wall)
    assert contact.last_helper_ask_iso("sp") is None
    contact.helper_asked("sp")
    assert contact.last_helper_ask_iso("sp") == "2026-11-10T12:00:00+00:00"
    assert contact.last_helper_ask_iso("other") is None
    contact.helper_asked("")
    assert contact.last_helper_ask_iso("") is None


def test_helper_asks_stay_bounded(monkeypatch) -> None:
    monkeypatch.setattr(host_contact_mod, "MAX_SPACES_TRACKED", 2)
    contact = HostContact(clock=_Clock(), wall=_wall)
    for space in ("a", "b", "c"):
        contact.helper_asked(space)
    assert contact.last_helper_ask_iso("a") is None
    assert contact.last_helper_ask_iso("c") is not None
