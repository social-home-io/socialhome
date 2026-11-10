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


def test_a_fresh_household_has_heard_nobody_yet() -> None:
    contact = HostContact(clock=_Clock(), wall=_wall)
    assert contact.last_heard_iso("sp") is None
    assert contact.heard_since("sp", 0.0) is False


def test_heard_marks_the_space_with_both_clocks() -> None:
    clock = _Clock()
    contact = HostContact(clock=clock, wall=_wall)
    clock.at = 1500.0
    t0 = contact.now()
    contact.heard("sp")
    assert contact.heard_since("sp", t0) is True
    assert contact.heard_since("sp", t0 + 1) is False
    assert contact.last_heard_iso("sp") == "2026-11-10T12:00:00+00:00"
    # Another space is still unheard.
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


def test_a_host_is_silent_from_an_unanswered_round_until_heard() -> None:
    """The scheduler marks a space silent when the host left a round's BEGIN
    unanswered (``went_silent``); the host's next answer clears it. Before
    any unanswered round the host is not silent (``SPACE_MEDIA_WANTED``
    asks a helper only while it is)."""
    contact = HostContact(clock=_Clock(), wall=_wall)
    assert contact.is_silent("sp") is False
    contact.went_silent("sp")
    assert contact.is_silent("sp") is True
    assert contact.is_silent("other") is False
    contact.heard("sp")
    assert contact.is_silent("sp") is False
    contact.went_silent("")
    assert contact.is_silent("") is False


def test_silent_spaces_stay_bounded(monkeypatch) -> None:
    monkeypatch.setattr(host_contact_mod, "MAX_SPACES_TRACKED", 2)
    contact = HostContact(clock=_Clock(), wall=_wall)
    for space in ("a", "b", "c"):
        contact.went_silent(space)
    assert contact.is_silent("a") is False
    assert contact.is_silent("c") is True
