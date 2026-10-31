"""Group sessions (tables) — the pure domain: a table's reads, changes and
the receiver's bound check on a hub's snapshot (v_76)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.app_group import (
    END_CANCELLED,
    GROUP_BATCH_MAX_MESSAGES,
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
    AppGroupBusyError,
    AppGroupLimitError,
    AppGroupSeatError,
    AppGroupSizeError,
    AppGroupTooFewError,
    GroupSeat,
    GroupSession,
    GroupWireError,
    SeatKey,
    note_ok,
    parse_batch,
    parse_message,
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
    assert AppGroupBusyError(12).params == {"retry_after": 12}
    assert AppGroupBusyError(1).status == 429
    assert AppGroupSeatError().code == "APP_GROUP_SEAT"


# ─── Seat keys on the roster ──────────────────────────────────────────────

KEY_B = SeatKey(kem_suite="x25519", pub="pubB", sig_suite="ed25519", sig="sigB")
KEY_HUB = SeatKey(kem_suite="x25519", pub="pubH", sig_suite="ed25519", sig="sigH")


def test_seat_keys_travel_for_seated_people_and_come_back():
    anna = dataclasses.replace(ANNA, seat_key=KEY_HUB)
    ben = dataclasses.replace(BEN, seat_key=KEY_B)
    gone = _seat("cleo", SEAT_LEFT, seat=3, instance="c", seat_key=KEY_B)
    t = _table(anna, ben, gone)
    wire = t.to_wire()
    assert wire["roster"][0]["seat_key"] == KEY_HUB.to_wire()
    assert "seat_key" not in wire["roster"][2]  # someone who left: no key
    got = _parse(wire)
    assert got.seat_key_of("b") == KEY_B
    assert got.seat_key_of("hub") == KEY_HUB
    assert got.seat_key_of("c") is None


def test_two_keys_for_one_household_are_refused():
    ben = dataclasses.replace(BEN, seat_key=KEY_B)
    bob = _seat("bob", seat=3, instance="b", seat_key=KEY_HUB)
    with pytest.raises(GroupWireError):
        _parse(_table(ANNA, ben, bob).to_wire())


@pytest.mark.parametrize(
    "key",
    [
        "x",
        {"seat_kem_suite": "x25519", "pub": "p", "sig_suite": "ed25519"},
        {**KEY_B.to_wire(), "pub": ""},
        {**KEY_B.to_wire(), "sig": "a" * 300},
        {**KEY_B.to_wire(), "pub": 5},
    ],
)
def test_a_seat_key_out_of_shape_drops_the_snapshot(key):
    wire = _table(ANNA, BEN).to_wire()
    wire["roster"][1]["seat_key"] = key
    with pytest.raises(GroupWireError):
        _parse(wire)


# ─── Messages ─────────────────────────────────────────────────────────────

SEALED = {"to_seat": 3, "seat_kem_suite": "x25519", "nonce": "n", "ct": "c"}


def test_a_message_to_everyone_round_trips_through_the_hub():
    m = parse_message(
        {"to": "all", "msg_id": "abc", "turn": 2},
        data={"x": 1},
        has_data=True,
        relayed=False,
    )
    assert m.to is None and not m.direct and m.data == {"x": 1} and m.turn == 2
    assert m.to_hub() == {"to": "all", "msg_id": "abc", "turn": 2}
    numbered = dataclasses.replace(m, seq=7, from_seat=1)
    out = numbered.relayed()
    assert out == {
        "seq": 7,
        "from_seat": 1,
        "to": "all",
        "msg_id": "abc",
        "turn": 2,
        "data": {"x": 1},
    }
    again = parse_message(out, has_data=False, relayed=True)
    assert again == numbered


def test_a_directed_message_passes_on_only_each_households_entries():
    other = {**SEALED, "to_seat": 4}
    m = parse_message(
        {"to": [3, 4], "msg_id": "m1", "sealed": [SEALED, other]},
        has_data=False,
        relayed=False,
    )
    assert m.direct and m.to == (3, 4)
    assert m.to_hub()["sealed"] == [SEALED, other]
    numbered = dataclasses.replace(m, seq=8, from_seat=2)
    out = numbered.relayed(frozenset({4}))
    assert out["sealed"] == [other] and "data" not in out
    got = parse_message(out, has_data=False, relayed=True)
    assert got.sealed == (other,) and got.to == (3, 4)


def test_the_last_message_says_so_on_both_legs():
    m = parse_message(
        {"to": "all", "msg_id": "end", "final": True},
        data={"over": True},
        has_data=True,
        relayed=False,
    )
    assert m.final and m.to_hub()["final"] is True
    out = dataclasses.replace(m, seq=3, from_seat=2).relayed()
    assert out["final"] is True
    assert parse_message(out, has_data=False, relayed=True).final
    sealed = parse_message(
        {"to": [3], "msg_id": "e2", "final": True, "sealed": [SEALED]},
        has_data=False,
        relayed=False,
    )
    assert sealed.final and dataclasses.replace(sealed, seq=1).relayed()["final"]
    # Not final: the key stays off the wire.
    plain = parse_message(
        {"to": "all", "msg_id": "a", "final": False},
        data=1,
        has_data=True,
        relayed=False,
    )
    assert not plain.final and "final" not in plain.to_hub()


@pytest.mark.parametrize(
    ("group", "has_data"),
    [
        ("x", True),
        ({"to": "all", "msg_id": "a", "final": 1}, True),
        ({"to": "all", "msg_id": "a", "final": "true"}, True),
        ({"to": "all", "msg_id": "a"}, False),  # everyone needs data
        ({"to": "all", "msg_id": "a", "sealed": [SEALED]}, True),
        ({"to": [3], "msg_id": "a", "sealed": [SEALED]}, True),  # data beside a seal
        ({"to": [3], "msg_id": "a"}, False),
        ({"to": [3], "msg_id": "a", "sealed": []}, False),
        ({"to": [3, 4], "msg_id": "a", "sealed": [SEALED]}, False),  # one missing
        ({"to": [3], "msg_id": "a", "sealed": [{**SEALED, "to_seat": 5}]}, False),
        ({"to": [3], "msg_id": "a", "sealed": [SEALED, SEALED]}, False),
        ({"to": [3], "msg_id": "a", "sealed": ["x"]}, False),
        ({"to": [3], "msg_id": "a", "sealed": [{**SEALED, "to_seat": True}]}, False),
        ({"to": [], "msg_id": "a"}, True),
        ({"to": [3, 3], "msg_id": "a", "sealed": [SEALED]}, False),
        ({"to": list(range(1, 31)), "msg_id": "a"}, True),
        ({"to": ["3"], "msg_id": "a"}, True),
        ({"to": "some", "msg_id": "a"}, True),
        ({"to": "all", "msg_id": ""}, True),
        ({"to": "all", "msg_id": "a" * 33}, True),
        ({"to": "all", "msg_id": "a|b"}, True),
        ({"to": "all", "msg_id": "a", "turn": 0}, True),
        ({"to": "all", "msg_id": "a", "turn": True}, True),
    ],
)
def test_a_message_out_of_shape_is_refused(group, has_data):
    with pytest.raises(GroupWireError):
        parse_message(group, data={}, has_data=has_data, relayed=False)


@pytest.mark.parametrize(
    "msg",
    [
        {"to": "all", "msg_id": "a", "from_seat": 1, "data": 1},  # no seq
        {"to": "all", "msg_id": "a", "seq": 0, "from_seat": 1, "data": 1},
        {"to": "all", "msg_id": "a", "seq": 1, "data": 1},  # no from_seat
        {"to": "all", "msg_id": "a", "seq": 1, "from_seat": 1},  # no data
        {"to": [3], "msg_id": "a", "seq": 1, "from_seat": 1, "sealed": []},
    ],
)
def test_a_relayed_message_out_of_shape_is_refused(msg):
    with pytest.raises(GroupWireError):
        parse_message(msg, has_data=False, relayed=True)


def test_a_relayed_directed_message_may_carry_only_some_entries():
    msg = {"to": [3, 4], "msg_id": "a", "seq": 2, "from_seat": 1, "sealed": [SEALED]}
    assert parse_message(msg, has_data=False, relayed=True).sealed == (SEALED,)


def test_parse_batch_bounds():
    one = {"to": "all", "msg_id": "a", "seq": 1, "from_seat": 1, "data": None}
    assert len(parse_batch({"msgs": [one, {**one, "seq": 2}]})) == 2
    for bad in (
        None,
        {},
        {"msgs": []},
        {"msgs": [one] * (GROUP_BATCH_MAX_MESSAGES + 1)},
        {"msgs": [{**one, "seq": "1"}]},
    ):
        with pytest.raises(GroupWireError):
            parse_batch(bad)


def test_a_space_tables_roster_carries_user_ids_and_identity_keys():
    """v_77: at a space table each entry names the person's space user id
    and their household's identity key (64 hex) — both optional, both
    round-trip; a malformed identity key drops the snapshot."""
    pk = "ab" * 32
    ben = dataclasses.replace(BEN, user_id="uid-ben", identity_pk=pk)
    got = _parse(_table(ANNA, ben).to_wire())
    entry = got.roster[1]
    assert (entry.user_id, entry.identity_pk) == ("uid-ben", pk)
    assert got.roster[0].user_id is None and got.roster[0].identity_pk is None
    for bad in ("AB" * 32, "ab" * 31, 7):
        wire = _table(ANNA, ben).to_wire()
        wire["roster"][1]["identity_pk"] = bad
        with pytest.raises(GroupWireError):
            _parse(wire)
