"""AppGroupService — messages at a table (v_76): to everyone, to chosen
seats (sealed to their household's seat key), numbering, the per-sender
mailbox, "Your turn" and the send budget.

Households (``tests/app_group_network.py``): **hub** (Anna, the host, and
Ada), **b** (Ben), **c** (Cleo); the hub is paired with b and c, b and c
are not paired with each other.
"""

from __future__ import annotations

import orjson
import pytest

from socialhome.domain.app_group import (
    GROUP_HELD_MAX_BYTES,
    AppGroupBusyError,
    AppGroupClosedError,
    AppGroupNotFoundError,
    AppGroupSeatError,
)
from socialhome.domain.events import AppTurn, AppTurnsSeen
from socialhome.services.app_group_budget import TableSendBudget
from tests.app_group_network import APP, Network, quiz_app


@pytest.fixture
async def net(tmp_path):
    n = Network(tmp_path)
    hub = await n.household("hub")
    b = await n.household("b")
    c = await n.household("c")
    await hub.add_user("anna")
    await hub.add_user("ada")
    await b.add_user("ben")
    await c.add_user("cleo")
    for home in (hub, b, c):
        await home.app_repo.install(quiz_app())
    await n.pair(hub, b)
    await n.pair(hub, c)
    yield n
    await n.close()


def _who(net):
    hub, b, c = net.homes["hub"], net.homes["b"], net.homes["c"]
    return {"anna": hub, "ada": hub, "ben": b, "cleo": c}


async def _table(net, *, start: bool = True) -> tuple[str, dict[str, int]]:
    """A table with everyone seated (and started): its id and the seats."""
    hub, b, c = net.homes["hub"], net.homes["b"], net.homes["c"]
    view = await hub.groups.open_table(
        app_id=APP,
        actor_user_id=hub.uid("anna"),
        targets=[hub.target(hub, "ada"), hub.target(b, "ben"), hub.target(c, "cleo")],
    )
    sid = view["session_id"]
    await net.flush()
    for name in ("ada", "ben", "cleo"):
        home = _who(net)[name]
        await home.groups.join(app_id=APP, session_id=sid, actor_user_id=home.uid(name))
        await net.flush()
    if start:
        await hub.groups.start(
            app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
        )
        await net.flush()
    seats = {}
    for name, home in _who(net).items():
        got = await home.groups.get_table(
            app_id=APP, session_id=sid, actor_user_id=home.uid(name)
        )
        seats[name] = got["me"]
    return sid, seats


async def _send(net, name, sid, data, **kw):
    home = _who(net)[name]
    got = await home.groups.send_message(
        app_id=APP, session_id=sid, actor_user_id=home.uid(name), data=data, **kw
    )
    await net.flush()
    return got


def _frames(home, user_id):
    return [
        f
        for uid, f in home.ws.frames
        if uid == user_id and f["type"] == "app.group.message"
    ]


async def test_everyone_gets_a_message_to_everyone_once_numbered(net):
    hub, b, c = net.homes["hub"], net.homes["b"], net.homes["c"]
    sid, seats = await _table(net)
    assert all(seats.values())
    assert await _send(net, "ben", sid, {"round": 1}) == {"seq": None}
    for home, name in ((hub, "anna"), (hub, "ada"), (c, "cleo")):
        (frame,) = _frames(home, home.uid(name))
        assert frame["seq"] == 1 and frame["from_seat"] == seats["ben"]
        assert frame["data"] == {"round": 1} and frame["direct"] is False
    assert _frames(b, b.uid("ben")) == []  # no echo to the sender
    # One envelope per household: hub → c only (b sent it; hub is local).
    out = [p for p in net.messages() if "msgs" in p["group"]]
    assert [m["group"]["msgs"][0]["seq"] for m in out] == [1]
    assert (await hub.group_repo.get(sid)).seq == 1
    assert (await c.group_repo.get(sid)).seq == 1


async def test_the_hub_numbers_its_own_peoples_messages(net):
    hub, c = net.homes["hub"], net.homes["c"]
    sid, _seats = await _table(net)
    assert await _send(net, "anna", sid, "hi") == {"seq": 1}
    assert await _send(net, "ada", sid, "yo") == {"seq": 2}
    assert [f["seq"] for f in _frames(c, c.uid("cleo"))] == [1, 2]
    # Ada hears Anna (same household) and not herself.
    assert [f["data"] for f in _frames(hub, hub.uid("ada"))] == ["hi"]


async def test_a_directed_message_reaches_only_its_seat_and_the_hub_sees_ciphertext(
    net,
):
    hub, _b, c = net.homes["hub"], net.homes["b"], net.homes["c"]
    sid, seats = await _table(net)
    secret = {"hand": "QUEEN-OF-HEARTS-7731"}
    await _send(net, "ben", sid, secret, to=[seats["cleo"]])
    (frame,) = _frames(c, c.uid("cleo"))
    assert frame["data"] == secret and frame["direct"] is True and frame["seq"] == 1
    assert _frames(hub, hub.uid("anna")) == [] and _frames(hub, hub.uid("ada")) == []
    # Nothing the hub received or sent carries the plaintext.
    for _s, _t, payload in net.sent:
        assert b"QUEEN-OF-HEARTS" not in orjson.dumps(payload)
    assert b"QUEEN-OF-HEARTS" not in orjson.dumps([f for _u, f in hub.ws.frames])
    # Ben's household sealed it; the hub kept nothing readable.
    assert (await hub.group_repo.take_group_held(APP, hub.uid("anna"))) == []


async def test_directed_from_the_hub_and_to_the_hub(net):
    hub, b, c = net.homes["hub"], net.homes["b"], net.homes["c"]
    sid, seats = await _table(net)
    await _send(net, "anna", sid, "to cleo", to=[seats["cleo"]])
    await _send(net, "ben", sid, "to ada", to=[seats["ada"]])
    assert [f["data"] for f in _frames(c, c.uid("cleo"))] == ["to cleo"]
    assert [f["data"] for f in _frames(hub, hub.uid("ada"))] == ["to ada"]
    assert _frames(hub, hub.uid("anna")) == []
    assert _frames(b, b.uid("ben")) == []


async def test_seat_errors(net):
    sid, seats = await _table(net)
    with pytest.raises(AppGroupSeatError):
        await _send(net, "ben", sid, 1, to=[seats["ben"]])
    with pytest.raises(AppGroupSeatError):
        await _send(net, "ben", sid, 1, to=[99])
    with pytest.raises(AppGroupSeatError):
        await _send(net, "ben", sid, 1, turn=99)
    with pytest.raises(AppGroupSeatError):
        await _send(net, "anna", sid, 1, to=[seats["anna"]])


async def test_only_seated_people_send_and_not_after_the_end(net):
    hub, b = net.homes["hub"], net.homes["b"]
    sid, _seats = await _table(net)
    await b.groups.leave(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    with pytest.raises(AppGroupNotFoundError):  # no seat row any more
        await _send(net, "ben", sid, 1)
    await hub.groups.close(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await net.flush()
    with pytest.raises(AppGroupClosedError):
        await _send(net, "ada", sid, 1)


async def test_messages_in_the_lobby_are_fine(net):
    c = net.homes["c"]
    sid, _seats = await _table(net, start=False)
    await _send(net, "ben", sid, "ready?")
    assert [f["data"] for f in _frames(c, c.uid("cleo"))] == ["ready?"]


async def test_the_mailbox_keeps_each_players_latest(net):
    c = net.homes["c"]
    sid, seats = await _table(net)
    await _send(net, "ben", sid, {"v": 1})
    await _send(net, "anna", sid, {"v": 2})
    await _send(net, "ben", sid, {"v": 3})
    await _send(net, "ada", sid, {"v": "x"}, to=[seats["cleo"]])
    held = await c.groups.take_held(app_id=APP, actor_user_id=c.uid("cleo"))
    assert [(h["seq"], h["from_seat"], h["data"], h["direct"]) for h in held] == [
        (2, seats["anna"], {"v": 2}, False),
        (3, seats["ben"], {"v": 3}, False),
        (4, seats["ada"], {"v": "x"}, True),
    ]
    assert await c.groups.take_held(app_id=APP, actor_user_id=c.uid("cleo")) == []
    seen = c.bus.of(AppTurnsSeen)
    assert seen and seen[-1].session_ids == (sid,)


async def test_a_big_message_goes_out_live_only(net):
    c = net.homes["c"]
    sid, _seats = await _table(net)
    big = "x" * (GROUP_HELD_MAX_BYTES + 10)
    await _send(net, "ben", sid, big)
    assert [f["data"] for f in _frames(c, c.uid("cleo"))] == [big]
    assert await c.groups.take_held(app_id=APP, actor_user_id=c.uid("cleo")) == []


async def test_your_turn_only_on_the_named_seat(net):
    hub, b, c = net.homes["hub"], net.homes["b"], net.homes["c"]
    sid, seats = await _table(net)
    await _send(net, "ben", sid, {"board": 1}, turn=seats["cleo"])
    (turn,) = c.bus.of(AppTurn)
    assert turn.to_user_id == c.uid("cleo") and turn.session_id == sid
    assert turn.from_display.startswith("Ben")
    assert turn.from_user_id is None  # c isn't paired with b: a name only
    assert hub.bus.of(AppTurn) == [] and b.bus.of(AppTurn) == []
    await _send(net, "cleo", sid, {"board": 2}, turn=seats["anna"])
    (anna_turn,) = hub.bus.of(AppTurn)
    assert anna_turn.to_user_id == hub.uid("anna")
    assert anna_turn.from_user_id == "r-c-cleo"  # the hub knows Cleo


async def test_a_blocked_sender_is_not_heard(net):
    hub, c = net.homes["hub"], net.homes["c"]
    sid, _seats = await _table(net)
    # Cleo blocks Anna (straight in the repo: no leave follows here).
    await c.user_repo.block(c.uid("cleo"), "r-hub-anna")
    await _send(net, "anna", sid, "hello")
    assert _frames(c, c.uid("cleo")) == []
    assert [f["data"] for f in _frames(hub, hub.uid("ada"))] == ["hello"]


async def test_the_hub_send_budget(net, monkeypatch):
    sid, seats = await _table(net)
    hub = net.homes["hub"]
    monkeypatch.setattr(
        "socialhome.services.app_group_service.TableSendBudget",
        lambda: TableSendBudget(limit=2),
    )
    hub.groups._budgets.clear()
    # Anna to everyone: b and c — 2 envelopes, the whole budget.
    await _send(net, "anna", sid, 1)
    with pytest.raises(AppGroupBusyError) as err:
        await _send(net, "ada", sid, 2)
    assert err.value.params["retry_after"] >= 1
    # Only the hub's own people: free.
    await _send(net, "anna", sid, 3, to=[seats["ada"]])


async def test_a_senders_household_applies_the_budget_too(net, monkeypatch):
    sid, _seats = await _table(net)
    b = net.homes["b"]
    monkeypatch.setattr(
        "socialhome.services.app_group_service.TableSendBudget",
        lambda: TableSendBudget(limit=1),
    )
    await _send(net, "ben", sid, 1)  # 1 envelope: hub → c
    with pytest.raises(AppGroupBusyError):
        await _send(net, "ben", sid, 2)
    assert len([p for p in net.messages() if p.get("from_user") == "ben"]) == 1
    assert b.groups._budgets


async def test_coalescing_puts_a_burst_in_one_envelope(net, monkeypatch):
    hub, c = net.homes["hub"], net.homes["c"]
    sid, _seats = await _table(net)
    hub.groups._coalesce_every = 0.05
    for n in range(3):
        await hub.groups.send_message(
            app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), data=n
        )
    await net.flush()
    to_c = [p for p in net.messages(to="c") if "msgs" in p["group"]]
    assert [len(p["group"]["msgs"]) for p in to_c] == [3]
    assert [f["seq"] for f in _frames(c, c.uid("cleo"))] == [1, 2, 3]


async def test_view_carries_seq(net):
    c = net.homes["c"]
    sid, _seats = await _table(net)
    await _send(net, "ben", sid, 1)
    got = await c.groups.get_table(
        app_id=APP, session_id=sid, actor_user_id=c.uid("cleo")
    )
    assert got["seq"] == 1


async def test_stop_sends_what_waits(net):
    hub = net.homes["hub"]
    sid, _seats = await _table(net)
    hub.groups._coalesce_every = 30.0
    await hub.groups.send_message(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), data="bye"
    )
    before = len(net.messages(to="c"))
    await hub.groups.stop()
    assert len(net.messages(to="c")) == before + 1


async def test_one_seat_key_per_household(net):
    hub, b = net.homes["hub"], net.homes["b"]
    sid, seats = await _table(net)
    table = await hub.group_repo.get(sid)
    anna, ada = table.at_seat(seats["anna"]), table.at_seat(seats["ada"])
    assert anna.seat_key is not None and anna.seat_key == ada.seat_key
    ben = table.at_seat(seats["ben"])
    assert ben.seat_key is not None and ben.seat_key != anna.seat_key
    # Ben's household keeps its key KEK-wrapped, never in the clear.
    raw = await b.group_repo.get_seat_key(sid)
    record = orjson.loads(raw)
    assert record["pub"] == ben.seat_key.pub and ":" in record["priv"]


async def test_installed_app_turned_off_holds_without_live_frames(net):
    c = net.homes["c"]
    sid, seats = await _table(net)
    await c.app_repo.set_enabled(APP, enabled=False)
    await _send(net, "ben", sid, {"v": 1}, turn=seats["cleo"])
    assert _frames(c, c.uid("cleo")) == []
    assert c.bus.of(AppTurn) == []
    await c.app_repo.set_enabled(APP, enabled=True)
    held = await c.groups.take_held(app_id=APP, actor_user_id=c.uid("cleo"))
    assert [h["data"] for h in held] == [{"v": 1}]
