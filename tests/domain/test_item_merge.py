"""The one merge rule for a change to an existing space item."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.item_merge import (
    TOMBSTONE_HORIZON_S,
    advance,
    change_stamp,
    decode_stamp_map,
    is_newer,
    just_after,
    local_stamp,
    to_stored,
    to_wire,
    tombstone_cutoff,
)
from socialhome.domain.space_item import MAX_ITEM_CLOCK_SKEW_S

NOW = datetime(2026, 11, 12, 10, 0, 0, tzinfo=UTC)
T1 = "2026-11-12 09:00:00.000000"
T2 = "2026-11-12 09:00:05.000000"
T3 = "2026-11-12 09:00:09.000000"


# ── stamps ────────────────────────────────────────────────────────────────


def test_a_sent_stamp_is_kept_in_the_stored_shape():
    got = change_stamp(
        "2026-11-12T10:30:00.250000+01:00",
        envelope_timestamp=NOW.isoformat(),
        now=NOW,
    )
    assert got == "2026-11-12 09:30:00.250000"


def test_an_older_peers_change_is_stamped_with_its_envelope():
    """No ``at`` on the wire (a household below v_94): the envelope's
    signed timestamp orders the change — it is never dropped."""
    sent = (NOW - timedelta(seconds=2)).isoformat()
    assert change_stamp(None, envelope_timestamp=sent, now=NOW) == (
        "2026-11-12 09:59:58.000000"
    )


def test_an_unusable_envelope_timestamp_falls_back_to_now():
    assert change_stamp(None, envelope_timestamp="", now=NOW) == (
        "2026-11-12 10:00:00.000000"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "yesterday",
        42,
        "2026-11-12T09:00:00",  # naive: not a wire stamp
        (NOW + timedelta(seconds=MAX_ITEM_CLOCK_SKEW_S + 1)).isoformat(),
    ],
    ids=["empty", "text", "number", "naive", "too far ahead"],
)
def test_a_sent_stamp_that_is_malformed_or_ahead_is_refused(raw):
    """A stamp far ahead would win every later change, so it is refused —
    and a present but broken one is never replaced by the envelope's."""
    assert change_stamp(raw, envelope_timestamp=NOW.isoformat(), now=NOW) is None


def test_a_stamp_within_the_skew_is_taken():
    ahead = (NOW + timedelta(seconds=MAX_ITEM_CLOCK_SKEW_S - 1)).isoformat()
    assert change_stamp(ahead, envelope_timestamp=NOW.isoformat(), now=NOW)


def test_only_a_strictly_newer_stamp_replaces():
    assert is_newer(T1, None)
    assert is_newer(T2, T1)
    assert not is_newer(T1, T1)  # a re-delivery
    assert not is_newer(T1, T2)


def test_a_local_change_is_never_older_than_the_one_it_replaces():
    assert local_stamp(None, now=NOW) == "2026-11-12 10:00:00.000000"
    assert local_stamp(T1, now=NOW) == "2026-11-12 10:00:00.000000"
    # The clock stepped back (or the held stamp came from one ahead).
    held = "2026-11-12 10:00:03.000000"
    assert local_stamp(held, now=NOW) == "2026-11-12 10:00:03.000001"
    assert just_after(T1) == "2026-11-12 09:00:00.000001"
    assert len(just_after("not a stamp")) == len(T1)  # our clock, stored shape


def test_stamps_round_trip_between_the_stored_and_the_wire_shape():
    wire = to_wire(T1)
    assert wire == "2026-11-12T09:00:00+00:00"
    assert change_stamp(wire, envelope_timestamp="", now=NOW) == T1
    assert to_wire(None) is None and to_wire("") is None and to_wire("x") is None


def test_any_time_can_be_put_in_the_stored_shape():
    """A deadline is no change stamp: it may lie ahead, and may be naive."""
    assert to_stored("2030-01-01T00:00:00Z") == "2030-01-01 00:00:00.000000"
    assert to_stored("2026-11-12 09:00:00") == T1
    assert to_stored("2026-11-12T10:00:00+01:00") == T1
    assert to_stored(None) is None and to_stored("soon") is None


def test_a_stamp_map_decodes_to_lists_or_nothing():
    assert decode_stamp_map('{"a": ["t", true], "b": 3, "c": []}') == {
        "a": ["t", True],
        "c": [],
    }
    for raw in (None, "", "{", "[]", 7):
        assert decode_stamp_map(raw) == {}


def test_a_tombstone_lives_thirty_days():
    assert TOMBSTONE_HORIZON_S == 30 * 24 * 3600
    assert tombstone_cutoff(NOW) == "2026-10-13 10:00:00.000000"


# ── a state that only advances ────────────────────────────────────────────


def test_a_state_that_only_advances_ends_the_same_in_any_order():
    for order in itertools.permutations((0, 1, 2, 3)):
        state = 0
        for rank in order:
            state = advance(state, rank)
        assert state == 3
    assert advance(2, 1) == 2 and advance(1, 2) == 2 and advance(2, 2) == 2
