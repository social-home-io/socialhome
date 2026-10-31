"""Release-blocker protocol tests: a group table speaks only through its hub (v_76).

Marked ``@pytest.mark.security``.

The rules these tests encode, over three households with real SQLite:

* A copy of a table takes a snapshot only from the household that hosts it
  (the hub), and only a newer one; nobody else can speak for a table.
* The hub seats only people it invited, each only from their own household;
  it acts on start / remove / close only from the host person's household.
* Who else was invited never travels; a ``group_*`` verb reaches only a
  household at v_76 and is taken only from one; an invite past the sheet's
  bound, for a space, or naming another host is dropped.
* A group invite can't re-point a session id a person already has.
* Every ``group_*`` message rides ``APP_SESSION`` — the sealed event path —
  and carries only the fields the table needs.
"""

from __future__ import annotations

import pytest

from socialhome.domain.app_group import SEAT_INVITED, SEAT_REMOVED, SEAT_SEATED
from socialhome.domain.apps import AppPendingSession
from tests.app_group_network import APP, Network, quiz_app

pytestmark = pytest.mark.security


@pytest.fixture
async def net(tmp_path):
    n = Network(tmp_path)
    hub = await n.household("hub")
    b = await n.household("b")
    c = await n.household("c")
    old = await n.household("old", version=75)
    await hub.add_user("anna")
    await b.add_user("ben")
    await c.add_user("cleo")
    await old.add_user("olga")
    for home in (hub, b, c, old):
        await home.app_repo.install(quiz_app())
    await n.pair(hub, b)
    await n.pair(hub, c)
    await n.pair(b, c)  # b and c also know each other: a forger in reach
    await n.pair(hub, old)
    yield n
    await n.close()


async def _open(net) -> str:
    hub, b, c = net.homes["hub"], net.homes["b"], net.homes["c"]
    view = await hub.groups.open_table(
        app_id=APP,
        actor_user_id=hub.uid("anna"),
        targets=[hub.target(b, "ben"), hub.target(c, "cleo")],
    )
    await net.flush()
    return view["session_id"]


def _forge(net, sender: str, to: str, payload: dict) -> None:
    net.queue.append((sender, to, payload))


async def test_only_the_hub_speaks_for_a_table(net):
    b = net.homes["b"]
    sid = await _open(net)
    wire = (await net.homes["hub"].group_repo.get(sid)).to_wire()
    forged = {
        **wire,
        "rev": 99,
        "state": "ended",
        "end_reason": "cancelled",
    }
    for verb in ("group_roster", "group_start", "group_close", "group_seat"):
        _forge(
            net,
            "c",
            "b",
            {"app_id": APP, "session_id": sid, "verb": verb, "table": forged},
        )
    _forge(
        net,
        "c",
        "b",
        {
            "app_id": APP,
            "session_id": sid,
            "verb": "group_close",
            "to_user": "ben",
            "reason": "removed",
            "table": forged,
        },
    )
    await net.flush()
    table = await b.group_repo.get(sid)
    assert (table.state, table.roster_rev, table.hub_instance) == ("lobby", 1, "hub")
    assert await b.app_repo.get_pending_session(APP, b.uid("ben"), sid) is not None


async def test_an_older_snapshot_never_wins(net):
    hub, b = net.homes["hub"], net.homes["b"]
    sid = await _open(net)
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    newest = (await b.group_repo.get(sid)).roster_rev
    stale = (await hub.group_repo.get(sid)).to_wire()
    stale["rev"] = 1
    stale["roster"] = stale["roster"][:1]
    _forge(
        net,
        "hub",
        "b",
        {"app_id": APP, "session_id": sid, "verb": "group_roster", "table": stale},
    )
    await net.flush()
    assert (await b.group_repo.get(sid)).roster_rev == newest
    assert (await b.group_repo.get(sid)).entry("b", "ben").state == SEAT_SEATED


async def test_the_hub_seats_only_who_it_invited_from_their_own_household(net):
    hub = net.homes["hub"]
    sid = await _open(net)
    # c joins in Ben's name; b joins someone never invited.
    _forge(
        net,
        "c",
        "hub",
        {"app_id": APP, "session_id": sid, "verb": "group_join", "from_user": "ben"},
    )
    _forge(
        net,
        "b",
        "hub",
        {
            "app_id": APP,
            "session_id": sid,
            "verb": "group_join",
            "from_user": "mallory",
        },
    )
    await net.flush()
    table = await hub.group_repo.get(sid)
    assert [e.user for e in table.seated] == ["anna"]
    assert table.entry("b", "ben").state == SEAT_INVITED
    assert table.entry("b", "mallory") is None
    assert "group_seat" not in net.verbs()


async def test_only_the_host_persons_household_commands(net):
    hub, b, c = net.homes["hub"], net.homes["b"], net.homes["c"]
    sid = await _open(net)
    for home, user in ((b, "ben"), (c, "cleo")):
        await home.groups.join(app_id=APP, session_id=sid, actor_user_id=home.uid(user))
        await net.flush()
    seat_of_cleo = (await hub.group_repo.get(sid)).entry("c", "cleo").seat
    # b claims to be Anna; c (not the host) asks directly.
    for sender, who in (("b", "anna"), ("c", "cleo")):
        for verb, extra in (
            ("group_start", {}),
            ("group_remove", {"seat": seat_of_cleo}),
            ("group_close", {}),
        ):
            _forge(
                net,
                sender,
                "hub",
                {
                    "app_id": APP,
                    "session_id": sid,
                    "verb": verb,
                    "from_user": who,
                    **extra,
                },
            )
    await net.flush()
    table = await hub.group_repo.get(sid)
    assert table.state == "lobby"
    assert table.entry("c", "cleo").state == SEAT_SEATED
    assert table.entry("c", "cleo").state != SEAT_REMOVED


async def test_who_else_was_invited_never_travels(net):
    await _open(net)
    for _s, _t, payload in net.sent:
        table = payload.get("table")
        if table is not None:
            assert {e["state"] for e in table["roster"]} <= {
                "seated",
                "left",
                "removed",
            }
        assert set(payload) <= {
            "app_id",
            "session_id",
            "verb",
            "to_user",
            "from_user",
            "others",
            "reason",
            "seat",
            "table",
        }


async def test_a_household_below_v76_is_never_sent_and_never_heard(net):
    hub = net.homes["hub"]
    sid = await _open(net)
    assert "old" not in {t for _s, t, _p in net.sent}
    wire = (await hub.group_repo.get(sid)).to_wire()
    # An older household (or one claiming to be) can't invite here.
    _forge(
        net,
        "old",
        "hub",
        {
            "app_id": APP,
            "session_id": "x" * 32,
            "verb": "group_invite",
            "to_user": "anna",
            "from_user": "olga",
            "table": {**wire, "roster": [{**wire["roster"][0], "instance": "old"}]},
        },
    )
    await net.flush()
    assert await hub.group_repo.get("x" * 32) is None


def _invite(table: dict, *, to_user="ben", from_user="anna", sid="t" * 32, **extra):
    return {
        "app_id": APP,
        "session_id": sid,
        "verb": "group_invite",
        "to_user": to_user,
        "from_user": from_user,
        "table": {**table, **extra},
    }


async def test_an_invite_out_of_bounds_is_dropped(net):
    hub, b = net.homes["hub"], net.homes["b"]
    sid = await _open(net)
    wire = (await hub.group_repo.get(sid)).to_wire()
    cases = [
        _invite(wire, sid="a" * 32, max=11),  # past the sheet's 10
        _invite(wire, sid="b" * 32, space_id="sp1"),  # space tables: v_77
        _invite(wire, sid="c" * 32, from_user="cleo"),  # not the host
        _invite(wire, sid="d" * 32, state="ended", end_reason="cancelled"),
    ]
    for payload in cases:
        _forge(net, "hub", "b", payload)
    await net.flush()
    for payload in cases:
        sid_ = payload["session_id"]
        assert await b.group_repo.get(sid_) is None
        assert await b.app_repo.get_pending_session(APP, b.uid("ben"), sid_) is None


async def test_a_group_invite_cant_repoint_a_session_one_has(net):
    hub, b = net.homes["hub"], net.homes["b"]
    sid = await _open(net)
    wire = (await hub.group_repo.get(sid)).to_wire()
    mine = "m" * 32
    await b.app_repo.add_pending_session(
        AppPendingSession(
            app_id=APP,
            user_id=b.uid("ben"),
            session_id=mine,
            from_instance="c",
            from_user="cleo",
            payload={"app_id": APP, "session_id": mine, "verb": "open"},
            created_at="2026-10-31T10:00:00+00:00",
        )
    )
    _forge(net, "hub", "b", _invite(wire, sid=mine))
    await net.flush()
    row = await b.app_repo.get_pending_session(APP, b.uid("ben"), mine)
    assert not row.is_group and row.from_instance == "c"
    assert await b.group_repo.get(mine) is None


async def test_another_hub_cant_take_over_a_table_id(net):
    b = net.homes["b"]
    sid = await _open(net)
    wire = (await b.group_repo.get(sid)).to_wire()
    # c re-invites Ben to the same table id as if it hosted it.
    _forge(
        net,
        "c",
        "b",
        _invite(
            {
                **wire,
                "roster": [{**wire["roster"][0], "instance": "c", "user": "cleo"}],
            },
            sid=sid,
            from_user="cleo",
        ),
    )
    await net.flush()
    assert (await b.group_repo.get(sid)).hub_instance == "hub"


async def test_mirrors_from_one_hub_are_bounded(net, monkeypatch):
    hub, b = net.homes["hub"], net.homes["b"]
    sid = await _open(net)
    wire = (await hub.group_repo.get(sid)).to_wire()
    # The host's seat key is signed for the real table id: forged ids carry
    # none (a key moved to another table is dropped — tested apart).
    for entry in wire["roster"]:
        entry.pop("seat_key", None)
    monkeypatch.setattr(
        "socialhome.services.app_group_service.GROUP_MIRRORS_PER_HUB_MAX", 2
    )
    for n in range(3):
        _forge(net, "hub", "b", _invite(wire, sid=f"{n}" * 32))
    await net.flush()
    held = [await b.group_repo.get(f"{n}" * 32) for n in range(3)]
    # One table from before plus one more: the third is dropped.
    assert [h is not None for h in held] == [True, False, False]
