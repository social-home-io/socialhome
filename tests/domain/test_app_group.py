"""Group sessions (tables) — the pure domain: a table's reads, changes and
the receiver's bound check on a hub's snapshot (v_76)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.app_group import (
    END_CANCELLED,
    GROUP_ENDED,
    GROUP_LOBBY,
    GROUP_MAX_PLAYERS,
    GROUP_NOTE_MAX,
    GROUP_PLAYING,
    GROUP_ROSTER_MAX,
    SEAT_DECLINED,
    SEAT_INVITED,
    SEAT_LEFT,
    SEAT_REMOVED,
    SEAT_SEATED,
    AppGroupLimitError,
    AppGroupSizeError,
    AppGroupTooFewError,
    GroupSeat,
    GroupSession,
    GroupWireError,
    note_ok,
    parse_table,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def _seat(user: str, state: str = SEAT_SEATED, seat: int = 0, **kw) -> GroupSeat:
    return GroupSeat(
        instance=kw.pop("instance", "hub"),
        user=user,
        name=user.title(),
        state=state,
        seat=seat,
        **kw,
    )


def _table(*roster: GroupSeat, **kw) -> GroupSession:
    base = {
        "session_id": "t1",
        "app_id": "quiz",
        "host_instance": "hub",
        "host_user": "anna",
        "hub_instance": "hub",
        "state": GROUP_LOBBY,
        "min_players": 3,
        "max_players": 4,
        "roster": roster,
        "roster_rev": 1,
        "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=3)).isoformat(),
        "updated_at": NOW.isoformat(),
    }
    base.update(kw)
    return GroupSession(**base)


ANNA = _seat("anna", seat=1, joined_at="2026-10-31T10:00:00+00:00")
BEN = _seat("ben", seat=2, instance="b", joined_at="2026-10-31T10:05:00+00:00")
CLEO = _seat("cleo", seat=3, instance="c", joined_at="2026-10-31T10:01:00+00:00")
DEV = _seat("dev", SEAT_INVITED, instance="d")


def test_reads_of_a_table():
    t = _table(ANNA, BEN, DEV, _seat("eve", SEAT_DECLINED, instance="e"))
    assert [e.user for e in t.seated] == ["anna", "ben"]
    assert t.pending_invites == 1
    assert t.invited_total == 3
    assert t.host_seat == 1
    assert t.next_seat == 3
    assert not t.is_full
    assert not t.can_start  # 2 seated, min 3
    assert t.is_open
    assert t.households() == {"hub", "b"}
    assert t.entry("b", "ben") == BEN
    assert t.at_seat(2) == BEN
    assert t.at_seat(0) is None
    assert t.is_host("hub", "anna") and not t.is_host("b", "ben")


def test_a_full_table_and_one_being_played():
    t = _table(ANNA, BEN, CLEO, _seat("dev", seat=4, instance="d"))
    assert t.is_full and t.can_start
    playing = dataclasses.replace(t, state=GROUP_PLAYING)
    assert not playing.is_open
    assert dataclasses.replace(playing, late_join=True).is_open
    assert not dataclasses.replace(t, state=GROUP_ENDED).is_open


def test_the_player_seated_longest_takes_over():
    t = _table(ANNA, BEN, CLEO)
    assert t.successor() == CLEO
    assert _table(ANNA).successor() is None


def test_with_entry_keeps_seat_order_and_bumped_moves_rev():
    t = _table(ANNA, DEV)
    seated = t.with_entry(dataclasses.replace(DEV, state=SEAT_SEATED, seat=2))
    assert [e.user for e in seated.roster] == ["anna", "dev"]
    later = seated.bumped("2026-10-31T13:00:00+00:00", state=GROUP_PLAYING)
    assert later.roster_rev == 2 and later.state == GROUP_PLAYING
    assert later.updated_at == "2026-10-31T13:00:00+00:00"


def test_the_snapshot_never_names_who_else_was_invited():
    wire = _table(ANNA, BEN, DEV, _seat("eve", SEAT_DECLINED, instance="e")).to_wire()
    assert [e["user"] for e in wire["roster"]] == ["anna", "ben"]
    assert wire["host_seat"] == 1
    assert set(wire["roster"][0]) == {
        "seat",
        "instance",
        "user",
        "name",
        "household",
        "state",
    }


def _parse(raw, **kw):
    return parse_table(
        raw, session_id="t1", app_id="quiz", hub_instance=kw.get("hub", "hub"), now=NOW
    )


def test_a_snapshot_round_trips():
    t = _table(ANNA, BEN, _seat("cleo", SEAT_LEFT, seat=3, instance="c"), note="Hi")
    got = _parse(t.to_wire())
    assert got.roster_rev == 1
    assert [(e.user, e.state, e.seat) for e in got.roster] == [
        ("anna", SEAT_SEATED, 1),
        ("ben", SEAT_SEATED, 2),
        ("cleo", SEAT_LEFT, 3),
    ]
    assert (got.host_instance, got.host_user) == ("hub", "anna")
    assert got.note == "Hi"
    assert got.hub_instance == "hub"


def test_the_host_is_who_the_snapshot_says_and_the_hub_who_sent_it():
    t = _table(ANNA, BEN)
    wire = t.to_wire()
    wire["host_seat"] = 2
    got = _parse(wire, hub="sender")
    assert (got.host_instance, got.host_user) == ("b", "ben")
    assert got.hub_instance == "sender"


def _wire(**changes):
    wire = _table(ANNA, BEN).to_wire()
    wire.update(changes)
    return wire


@pytest.mark.parametrize(
    "raw",
    [
        None,
        _wire(rev=0),
        _wire(state="paused"),
        _wire(state=GROUP_ENDED),  # an end needs a reason
        _wire(end_reason=END_CANCELLED),  # …and only an end has one
        _wire(min=1),
        _wire(max=GROUP_MAX_PLAYERS + 1),
        _wire(min=4, max=3),
        _wire(late_join="yes"),
        _wire(note="x" * (GROUP_NOTE_MAX + 1)),
        _wire(note="two\nlines"),
        _wire(expires_at=(NOW + timedelta(days=20)).isoformat()),
        _wire(expires_at="soon"),
        _wire(roster=[]),
        _wire(roster="nope"),
        _wire(host_seat=9),
        _wire(space_id="s1"),
        _wire(max=2, min=2, roster=[ANNA.to_wire(), BEN.to_wire(), CLEO.to_wire()]),
        _wire(roster=[ANNA.to_wire(), ANNA.to_wire()]),
        _wire(roster=[ANNA.to_wire(), {**BEN.to_wire(), "seat": 1}]),
        _wire(roster=[ANNA.to_wire(), {**BEN.to_wire(), "state": SEAT_INVITED}]),
        _wire(roster=[ANNA.to_wire(), {**BEN.to_wire(), "name": ""}]),
        _wire(roster=[ANNA.to_wire(), {**BEN.to_wire(), "name": "x" * 81}]),
        _wire(roster=[ANNA.to_wire(), {**BEN.to_wire(), "user": 7}]),
        _wire(roster=[ANNA.to_wire(), {**BEN.to_wire(), "seat": True}]),
        _wire(roster=[ANNA.to_wire(), "ben"]),
        _wire(
            roster=[
                _seat(f"p{i}", SEAT_LEFT, seat=i + 1).to_wire()
                for i in range(GROUP_ROSTER_MAX + 1)
            ]
        ),
    ],
)
def test_a_snapshot_out_of_shape_is_dropped(raw):
    with pytest.raises(GroupWireError):
        _parse(raw)


def test_an_ended_snapshot_keeps_its_reason():
    got = _parse(_wire(state=GROUP_ENDED, end_reason=END_CANCELLED))
    assert got.end_reason == END_CANCELLED


def test_removed_players_travel():
    wire = _table(ANNA, _seat("ben", SEAT_REMOVED, seat=2, instance="b")).to_wire()
    assert _parse(wire).roster[1].state == SEAT_REMOVED


def test_note_ok():
    assert note_ok(None) is None
    assert note_ok("   ") is None
    assert note_ok(" Bring snacks ") == "Bring snacks"
    with pytest.raises(ValueError):
        note_ok("x" * (GROUP_NOTE_MAX + 1))
    with pytest.raises(ValueError):
        note_ok("a\nb")


def test_coded_errors_carry_their_numbers():
    assert AppGroupTooFewError(3).params == {"min": 3}
    assert AppGroupSizeError(2, 10).params == {"min": 2, "max": 10}
    assert AppGroupLimitError(daily=True).code == "APP_GROUP_DAILY_LIMIT"
    assert AppGroupLimitError(daily=False).params == {"limit": 5}
