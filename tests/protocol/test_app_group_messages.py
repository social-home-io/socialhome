"""Release-blocker protocol tests: messages at a group table (v_76).

Marked ``@pytest.mark.security``.

* A directed message is sealed to the target household's seat key: the hub
  that passes it on, and every other household, sees only ciphertext; a
  hub that moves it to another seat, relabels its sender or swaps a seat
  key gets nothing opened.
* A seat key is signed by its household's identity key and checked against
  the key a receiver already stores for that household; an unknown suite is
  refused, never defaulted.
* The hub takes a message only from a person seated now, from their own
  household; a receiver takes a batch only from the table's hub, once per
  ``seq``.
* Sizes are bounded on the way in (64 KiB live, 16 KiB held).
"""

from __future__ import annotations

import copy

import orjson
import pytest

from socialhome.crypto import b64url_encode, generate_identity_keypair
from socialhome.domain.app_group import GROUP_MESSAGE_MAX_BYTES, GROUP_SEQ_WINDOW
from socialhome.domain.federation import FederationEventType
from socialhome.federation.seat_crypto import new_seat_keypair, sign_seat_key
from tests.app_group_network import APP, Network, quiz_app

pytestmark = pytest.mark.security

MSG = FederationEventType.APP_MESSAGE


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
    for home in (hub, b, c):
        await home.app_repo.install(quiz_app())
    await n.pair(hub, b)
    await n.pair(hub, c)
    await n.pair(b, c)  # b and c know each other: they check each other's keys
    await n.pair(hub, old)
    yield n
    await n.close()


async def _table(net) -> tuple[str, dict[str, int]]:
    hub, b, c = net.homes["hub"], net.homes["b"], net.homes["c"]
    view = await hub.groups.open_table(
        app_id=APP,
        actor_user_id=hub.uid("anna"),
        targets=[hub.target(b, "ben"), hub.target(c, "cleo")],
    )
    sid = view["session_id"]
    await net.flush()
    for home, name in ((b, "ben"), (c, "cleo")):
        await home.groups.join(app_id=APP, session_id=sid, actor_user_id=home.uid(name))
        await net.flush()
    await hub.groups.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await net.flush()
    table = await hub.group_repo.get(sid)
    return sid, {e.user: e.seat for e in table.seated}


def _frames(home, name):
    return [
        f
        for uid, f in home.ws.frames
        if uid == home.uid(name) and f["type"] == "app.group.message"
    ]


def _last_batch(net, to: str) -> dict:
    return [p for p in net.messages(to=to) if "msgs" in p.get("group", {})][-1]


async def test_the_hub_never_holds_a_directed_messages_plaintext(net):
    hub, c = net.homes["hub"], net.homes["c"]
    sid, seats = await _table(net)
    await net.homes["b"].groups.send_message(
        app_id=APP,
        session_id=sid,
        actor_user_id=net.homes["b"].uid("ben"),
        data={"whisper": "MARKER-91827"},
        to=[seats["cleo"]],
    )
    await net.flush()
    assert [f["data"] for f in _frames(c, "cleo")] == [{"whisper": "MARKER-91827"}]
    # Not in any envelope (to or from the hub), its frames, or its database.
    assert b"MARKER-91827" not in orjson.dumps([p for _s, _t, p in net.sent])
    assert b"MARKER-91827" not in orjson.dumps([f for _u, f in hub.ws.frames])
    rows = await hub.db.fetchall("SELECT held_json FROM app_group_held")
    assert all(b"MARKER" not in r["held_json"].encode() for r in rows)


async def _whisper_batch(net, seats, sid):
    b = net.homes["b"]
    await b.groups.send_message(
        app_id=APP,
        session_id=sid,
        actor_user_id=b.uid("ben"),
        data="psst",
        to=[seats["cleo"]],
    )
    await net.flush()
    return _last_batch(net, "c")


async def test_a_hub_that_relabels_or_moves_a_sealed_message_gets_nothing_through(net):
    c = net.homes["c"]
    sid, seats = await _table(net)
    batch = await _whisper_batch(net, seats, sid)
    assert len(_frames(c, "cleo")) == 1
    msg = batch["group"]["msgs"][0]
    # Claimed from Anna's seat (the hub's) instead of Ben's.
    relabel = copy.deepcopy(batch)
    relabel["group"]["msgs"][0].update(seq=50, from_seat=seats["anna"])
    # A fresh msg_id (the key and AAD bind it).
    remint = copy.deepcopy(batch)
    remint["group"]["msgs"][0].update(seq=51, msg_id=b64url_encode(b"x" * 16))
    # Changed ciphertext.
    flipped = copy.deepcopy(batch)
    ct = flipped["group"]["msgs"][0]["sealed"][0]["ct"]
    flipped["group"]["msgs"][0]["sealed"][0]["ct"] = (
        "B" if ct[0] != "B" else "C"
    ) + ct[1:]
    flipped["group"]["msgs"][0]["seq"] = 52
    for forged in (relabel, remint, flipped):
        net.forge("hub", "c", forged, MSG)
    await net.flush()
    assert len(_frames(c, "cleo")) == 1
    assert msg["sealed"][0]["to_seat"] == seats["cleo"]


async def test_an_unknown_seal_suite_is_refused(net):
    c = net.homes["c"]
    sid, seats = await _table(net)
    batch = copy.deepcopy(await _whisper_batch(net, seats, sid))
    entry = batch["group"]["msgs"][0]
    entry["seq"] = 60
    entry["sealed"][0]["seat_kem_suite"] = "x25519+mlkem768"
    net.forge("hub", "c", batch, MSG)
    await net.flush()
    assert len(_frames(c, "cleo")) == 1


async def test_a_batch_is_taken_only_from_the_tables_hub(net):
    c = net.homes["c"]
    sid, seats = await _table(net)
    net.forge(
        "b",
        "c",
        {
            "app_id": APP,
            "session_id": sid,
            "group": {
                "msgs": [
                    {
                        "seq": 9,
                        "from_seat": seats["anna"],
                        "to": "all",
                        "msg_id": "m",
                        "data": "fake",
                    }
                ]
            },
        },
        MSG,
    )
    await net.flush()
    assert _frames(c, "cleo") == []


async def test_a_repeated_or_ancient_seq_is_dropped(net):
    hub, c = net.homes["hub"], net.homes["c"]
    sid, _seats = await _table(net)
    await hub.groups.send_message(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), data="one"
    )
    await net.flush()
    batch = _last_batch(net, "c")
    net.forge("hub", "c", copy.deepcopy(batch), MSG)  # the same again
    await net.flush()
    assert [f["data"] for f in _frames(c, "cleo")] == ["one"]
    # Far ahead, then one far behind it.
    ahead = copy.deepcopy(batch)
    ahead["group"]["msgs"][0].update(seq=GROUP_SEQ_WINDOW + 10, msg_id="a2")
    behind = copy.deepcopy(batch)
    behind["group"]["msgs"][0].update(seq=5, msg_id="a3")
    net.forge("hub", "c", ahead, MSG)
    net.forge("hub", "c", behind, MSG)
    await net.flush()
    assert [f["seq"] for f in _frames(c, "cleo")] == [1, GROUP_SEQ_WINDOW + 10]


def _to_hub(sid, from_user="ben", data="x", **group):
    return {
        "app_id": APP,
        "session_id": sid,
        "from_user": from_user,
        "group": {"to": "all", "msg_id": "m1", **group},
        "data": data,
    }


async def test_the_hub_takes_messages_only_from_seated_people_of_their_household(net):
    hub, c = net.homes["hub"], net.homes["c"]
    sid, _seats = await _table(net)
    net.forge("c", "hub", _to_hub(sid, from_user="ben"), MSG)  # c posing as Ben
    net.forge("b", "hub", _to_hub(sid, from_user="bob"), MSG)  # nobody seated
    await net.flush()
    assert _frames(hub, "anna") == [] and _frames(c, "cleo") == []
    assert (await hub.group_repo.get(sid)).seq == 0
    b = net.homes["b"]
    await b.groups.leave(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    net.forge("b", "hub", _to_hub(sid), MSG)  # Ben left
    await net.flush()
    assert _frames(hub, "anna") == []


async def test_the_hub_refuses_oversize_and_malformed_messages(net):
    hub = net.homes["hub"]
    sid, seats = await _table(net)
    big = "x" * (GROUP_MESSAGE_MAX_BYTES + 1)
    net.forge("b", "hub", _to_hub(sid, data=big), MSG)
    huge_ct = {
        "to_seat": seats["cleo"],
        "seat_kem_suite": "x25519",
        "nonce": "A" * 16,
        "ct": "A" * 200_000,
    }
    p = _to_hub(sid, to=[seats["cleo"]], sealed=[huge_ct])
    del p["data"]
    net.forge("b", "hub", p, MSG)
    bad_suite = {**huge_ct, "ct": "A" * 40, "seat_kem_suite": "rsa"}
    p2 = _to_hub(sid, to=[seats["cleo"]], sealed=[bad_suite])
    del p2["data"]
    net.forge("b", "hub", p2, MSG)
    # To someone not seated, and a turn for nobody.
    net.forge("b", "hub", _to_hub(sid, turn=42), MSG)
    await net.flush()
    assert (await hub.group_repo.get(sid)).seq == 0
    assert _frames(hub, "anna") == []


async def test_a_household_below_v76_is_not_heard(net):
    hub = net.homes["hub"]
    sid, _seats = await _table(net)
    net.forge("old", "hub", _to_hub(sid), MSG)
    await net.flush()
    assert (await hub.group_repo.get(sid)).seq == 0


async def test_a_join_with_a_bad_seat_key_is_not_seated(net):
    hub, b, c = net.homes["hub"], net.homes["b"], net.homes["c"]
    view = await hub.groups.open_table(
        app_id=APP,
        actor_user_id=hub.uid("anna"),
        targets=[hub.target(b, "ben"), hub.target(c, "cleo")],
    )
    sid = view["session_id"]
    await net.flush()
    _priv, pub = new_seat_keypair()
    stranger = generate_identity_keypair()
    good = sign_seat_key(
        seed=b.fed.own_identity_seed, session_id=sid, instance="b", pub=pub
    )
    for key in (
        None,  # none at all
        {**good, "seat_kem_suite": "x448"},  # unknown suite
        sign_seat_key(seed=stranger.private_key, session_id=sid, instance="b", pub=pub),
        sign_seat_key(
            seed=b.fed.own_identity_seed, session_id="other", instance="b", pub=pub
        ),
    ):
        payload = {
            "app_id": APP,
            "session_id": sid,
            "verb": "group_join",
            "from_user": "ben",
        }
        if key is not None:
            payload["seat_key"] = key
        net.forge("b", "hub", payload)
        await net.flush()
        assert [e.user for e in (await hub.group_repo.get(sid)).seated] == ["anna"]
    net.forge(
        "b",
        "hub",
        {
            "app_id": APP,
            "session_id": sid,
            "verb": "group_join",
            "from_user": "ben",
            "seat_key": good,
        },
    )
    await net.flush()
    assert [e.user for e in (await hub.group_repo.get(sid)).seated] == ["anna", "ben"]


async def test_a_snapshot_with_a_swapped_seat_key_is_dropped(net):
    hub, b, c = net.homes["hub"], net.homes["b"], net.homes["c"]
    sid, _seats = await _table(net)
    wire = (await hub.group_repo.get(sid)).to_wire()
    _priv, pub = new_seat_keypair()
    # The hub hands c a key of its own for Ben's household (signed by the
    # hub): c is paired with b and knows b's identity key — dropped.
    swapped = copy.deepcopy(wire)
    swapped["rev"] += 5
    for e in swapped["roster"]:
        if e["instance"] == "b":
            e["seat_key"] = sign_seat_key(
                seed=hub.fed.own_identity_seed, session_id=sid, instance="b", pub=pub
            )
    net.forge(
        "hub",
        "c",
        {"app_id": APP, "session_id": sid, "verb": "group_roster", "table": swapped},
    )
    # And c's own key replaced.
    own = copy.deepcopy(wire)
    own["rev"] += 6
    for e in own["roster"]:
        if e["instance"] == "c":
            e["seat_key"] = sign_seat_key(
                seed=c.fed.own_identity_seed, session_id=sid, instance="c", pub=pub
            )
    net.forge(
        "hub",
        "c",
        {"app_id": APP, "session_id": sid, "verb": "group_roster", "table": own},
    )
    # An unknown suite on any key.
    odd = copy.deepcopy(wire)
    odd["rev"] += 7
    odd["roster"][0]["seat_key"]["sig_suite"] = "ed448"
    net.forge(
        "hub",
        "b",
        {"app_id": APP, "session_id": sid, "verb": "group_roster", "table": odd},
    )
    await net.flush()
    assert (await c.group_repo.get(sid)).roster_rev == wire["rev"]
    assert (await b.group_repo.get(sid)).roster_rev == wire["rev"]


async def test_apps_see_seats_never_usernames(net):
    c = net.homes["c"]
    sid, _seats = await _table(net)
    b = net.homes["b"]
    await b.groups.send_message(
        app_id=APP, session_id=sid, actor_user_id=b.uid("ben"), data=1
    )
    await net.flush()
    (frame,) = _frames(c, "cleo")
    assert set(frame) == {
        "type",
        "app_id",
        "session_id",
        "seq",
        "from_seat",
        "direct",
        "data",
    }
    held = await c.groups.take_held(app_id=APP, actor_user_id=c.uid("cleo"))
    assert set(held[0]) == {"session_id", "seq", "from_seat", "direct", "data"}


async def test_hub_batches_carry_no_usernames(net):
    sid, _seats = await _table(net)
    hub = net.homes["hub"]
    await hub.groups.send_message(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), data=1
    )
    await net.flush()
    batch = _last_batch(net, "c")
    assert set(batch) == {"app_id", "session_id", "group"}
    assert set(batch["group"]["msgs"][0]) == {
        "seq",
        "from_seat",
        "to",
        "msg_id",
        "data",
    }


async def test_a_hub_cant_replay_a_sealed_message_under_a_new_seq(net):
    c = net.homes["c"]
    sid, seats = await _table(net)
    batch = await _whisper_batch(net, seats, sid)
    assert len(_frames(c, "cleo")) == 1
    replay = copy.deepcopy(batch)
    replay["group"]["msgs"][0]["seq"] = 77  # same msg_id, sender, seal
    net.forge("hub", "c", replay, MSG)
    await net.flush()
    assert len(_frames(c, "cleo")) == 1
    held = await c.groups.take_held(app_id=APP, actor_user_id=c.uid("cleo"))
    assert [h["seq"] for h in held] == [1]
