"""Tests for socialhome.domain.space_app_table — the card of a space table
(v_77): what the hub sends, the receiver's bound check, and how a household
keeps a card it has nobody at."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from socialhome.domain.app_group import (
    END_CANCELLED,
    GROUP_ENDED,
    GROUP_LOBBY,
    GROUP_PLAYING,
    SEAT_SEATED,
    GroupSeat,
    GroupSession,
    GroupWireError,
    TableCard,
)
from socialhome.domain.space_app_table import (
    CARD_CLOSED,
    CARD_FULL,
    CARD_OPEN,
    CARD_PLAYING,
    SpaceTableLimitError,
    card_state,
    card_table,
    card_wire,
    parse_card,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
T0 = NOW.isoformat()


def _seat(n: int, user: str = "anna", instance: str = "hub") -> GroupSeat:
    return GroupSeat(
        instance=instance,
        user=user,
        name=user.title(),
        state=SEAT_SEATED,
        seat=n,
        joined_at=T0,
        user_id=f"uid-{user}",
    )


def _table(**kw) -> GroupSession:
    base = {
        "session_id": "t1",
        "app_id": "quiz",
        "host_instance": "hub",
        "host_user": "anna",
        "hub_instance": "hub",
        "state": GROUP_LOBBY,
        "min_players": 2,
        "max_players": 3,
        "roster": (_seat(1),),
        "roster_rev": 4,
        "created_at": T0,
        "expires_at": "2026-11-03T12:00:00+00:00",
        "updated_at": T0,
        "space_id": "sp1",
        "note": "Bring snacks",
    }
    base.update(kw)
    return GroupSession(**base)


def test_card_state_follows_the_table():
    assert card_state(_table()) == CARD_OPEN
    full = _table(roster=(_seat(1), _seat(2, "ben", "b"), _seat(3, "cleo", "c")))
    assert card_state(full) == CARD_FULL
    assert card_state(_table(state=GROUP_PLAYING, started_at=T0)) == CARD_PLAYING
    late = _table(state=GROUP_PLAYING, started_at=T0, late_join=True)
    assert card_state(late) == CARD_OPEN
    ended = _table(state=GROUP_ENDED, end_reason=END_CANCELLED)
    assert card_state(ended) == CARD_CLOSED


def test_card_wire_carries_no_roster():
    wire = card_wire(_table(), "sp1", announce=True)
    assert wire["space_id"] == "sp1"
    assert wire["host_user_id"] == "uid-anna"
    assert wire["host_name"] == "Anna"
    assert wire["seated"] == 1
    assert wire["state"] == CARD_OPEN
    assert wire["announce"] is True
    assert "roster" not in wire


def test_parse_card_round_trips_into_a_kept_table():
    card = parse_card(card_wire(_table(), "sp1"), now=NOW)
    kept = card_table(card, space_id="sp1", hub_instance="hub", seen_at=T0)
    assert kept.state == GROUP_LOBBY
    assert kept.card == TableCard(seated=1, announce=False, seen_at=T0)
    assert kept.roster[0].user_id == "uid-anna"
    assert (kept.min_players, kept.max_players, kept.roster_rev) == (2, 3, 4)
    assert kept.space_id == "sp1"
    playing = parse_card(
        card_wire(_table(state=GROUP_PLAYING, started_at=T0), "sp1"), now=NOW
    )
    kept_playing = card_table(playing, space_id="sp1", hub_instance="hub", seen_at=T0)
    assert kept_playing.state == GROUP_PLAYING
    closed = parse_card(
        card_wire(_table(state=GROUP_ENDED, end_reason=END_CANCELLED), "sp1"),
        now=NOW,
    )
    kept_closed = card_table(closed, space_id="sp1", hub_instance="hub", seen_at=T0)
    assert (kept_closed.state, kept_closed.end_reason) == (GROUP_ENDED, END_CANCELLED)


@pytest.mark.parametrize(
    "change",
    [
        {"rev": 0},
        {"seated": 4},
        {"state": "lobby"},
        {"players": {"min": 1, "max": 3}},
        {"players": {"min": 2, "max": 31}},
        {"players": None},
        {"note": "two\nlines"},
        {"host_name": "x" * 81},
        {"host_user_id": None},
        {"expires_at": "2026-12-31T12:00:00+00:00"},
        {"expires_at": "soon"},
        {"end_reason": "cancelled"},
        {"announce": "yes"},
    ],
)
def test_parse_card_refuses_anything_out_of_shape(change):
    wire = {**card_wire(_table(), "sp1"), **change}
    with pytest.raises(GroupWireError):
        parse_card(wire, now=NOW)


def test_parse_card_refuses_a_non_dict_and_unknown_end_reason():
    with pytest.raises(GroupWireError):
        parse_card(["nope"], now=NOW)
    wire = card_wire(_table(state=GROUP_ENDED, end_reason=END_CANCELLED), "sp1")
    wire["end_reason"] = "bored"
    with pytest.raises(GroupWireError):
        parse_card(wire, now=NOW)


def test_a_closed_card_without_a_reason_reads_finished():
    wire = card_wire(_table(state=GROUP_ENDED, end_reason=END_CANCELLED), "sp1")
    wire["end_reason"] = None
    assert parse_card(wire, now=NOW).end_reason == "finished"


def test_limit_error_codes():
    assert SpaceTableLimitError().code == "SPACE_TABLE_DAILY_LIMIT"
    assert SpaceTableLimitError(announce=True).code == "SPACE_TABLE_ANNOUNCE_LIMIT"
