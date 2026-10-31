"""Release-blocker protocol tests: game tables in a space (v_77).

Marked ``@pytest.mark.security``.

* **Members only.** A table's card and every one of its verbs and messages
  go to member households holding a writer seat — never a follower-only
  household, never a household outside the space — and only over the
  space's own transport; the same verbs as a bare ``APP_SESSION`` /
  ``APP_MESSAGE`` are dropped.
* **No global spaces.** Neither opened nor taken there.
* **The hub decides who sits** by its own copy of the space roster: a live
  writer seat on the sending household, not banned, the identity key the
  household id is the fingerprint of.
* **No hub's word for a seat key.** At a space table every seated
  household's identity key rides the roster and must derive to its id; a
  hub that swaps another household's seat key (or its identity key) gets
  its snapshot dropped — paired with that household or not.
"""

from __future__ import annotations

import copy
import dataclasses

import orjson
import pytest

from socialhome.domain.federation import FederationEventType
from socialhome.domain.space import SpaceFeatures, SpaceType
from socialhome.domain.space_app_table import SpaceAppNotAllowedError, card_wire
from socialhome.federation.seat_crypto import new_seat_keypair, sign_seat_key
from tests.space_table_network import APP, SPACE, SpaceNetwork

pytestmark = pytest.mark.security

TABLE = FederationEventType.SPACE_APP_TABLE
SESSION = FederationEventType.SPACE_APP_SESSION


@pytest.fixture
async def net(tmp_path):
    n = SpaceNetwork(tmp_path)
    host = await n.space_home("host")
    b = await n.space_home("b")
    m = await n.space_home("m")
    f = await n.space_home("f")
    x = await n.space_home("x")
    for sh, user in ((host, "anna"), (b, "ben"), (m, "mia"), (f, "fred"), (x, "xena")):
        await sh.home.add_user(user)
    await n.install(host, b, m, f, x)
    await n.pair(host.home, b.home)
    await n.pair(host.home, f.home)
    await n.pair(host.home, x.home)
    n.knows(host, m)
    n.knows(m, host)
    await n.make_space(
        host,
        members={
            host: {"anna": "owner"},
            b: {"ben": "member"},
            m: {"mia": "member"},
            f: {"fred": "subscriber"},
        },
    )
    yield n
    await n.close()


def _h(net):
    return {sh.name: sh for sh in net.space_homes.values()}


async def _table(net) -> str:
    h = _h(net)
    view = await h["host"].tables.open_table(
        space_id=SPACE, actor_user_id=h["host"].uid("anna"), app_id=APP
    )
    sid = view["session_id"]
    await net.flush()
    for name, user in (("b", "ben"), ("m", "mia")):
        await h[name].tables.join(
            space_id=SPACE, session_id=sid, actor_user_id=h[name].uid(user)
        )
        await net.flush()
    return sid


def _queue(net, sender, to, event_type, payload):
    net.queue.append((sender.instance, to.instance, payload))
    net.sent.append((sender.instance, to.instance, payload))
    net.types[id(payload)] = event_type
    net.space_of[id(payload)] = SPACE


async def test_nothing_of_a_table_reaches_followers_or_outsiders(net):
    h = _h(net)
    sid = await _table(net)
    await h["host"].home.groups.start(
        app_id=APP, session_id=sid, actor_user_id=h["host"].uid("anna")
    )
    await net.flush()
    await h["b"].home.groups.send_message(
        app_id=APP, session_id=sid, actor_user_id=h["b"].uid("ben"), data="SECRET-77"
    )
    await net.flush()
    for name in ("f", "x"):
        to = h[name].instance
        assert [p for _s, t, p in net.sent if t == to] == []
        assert await h[name].home.group_repo.get(sid) is None
    # Every envelope of the table rode the space (never a bare app event).
    assert {net.types[id(p)] for _s, _t, p in net.sent} <= {TABLE, SESSION}
    assert b"SECRET-77" in orjson.dumps([p for _s, _t, p in net.sent])


async def test_a_global_space_has_no_tables(net):
    h = _h(net)
    sid = await _table(net)
    hub = await h["host"].home.group_repo.get(sid)
    for sh in net.space_homes.values():
        space = await sh.spaces.get(SPACE)
        if space is not None:
            await sh.spaces.save(
                dataclasses.replace(space, space_type=SpaceType.GLOBAL)
            )
    with pytest.raises(SpaceAppNotAllowedError):
        await h["host"].tables.open_table(
            space_id=SPACE, actor_user_id=h["host"].uid("anna"), app_id=APP
        )
    wire = card_wire(dataclasses.replace(hub, session_id="9" * 32), SPACE)
    _queue(net, h["host"], h["b"], TABLE, wire)
    await net.flush()
    assert await h["b"].home.group_repo.get("9" * 32) is None


async def test_a_space_tables_verbs_as_bare_app_events_are_dropped(net):
    h = _h(net)
    sid = await _table(net)
    before = await h["host"].home.group_repo.get(sid)
    net.forge(
        h["b"].instance,
        h["host"].instance,
        {
            "app_id": APP,
            "session_id": sid,
            "verb": "group_leave",
            "from_user": "ben",
            "reason": "left",
        },
    )
    net.forge(
        h["b"].instance,
        h["host"].instance,
        {"app_id": APP, "session_id": sid, "from_user": "ben", "group": {}},
        FederationEventType.APP_MESSAGE,
    )
    await net.flush()
    after = await h["host"].home.group_repo.get(sid)
    assert (after.roster_rev, after.seq) == (before.roster_rev, before.seq)


async def test_a_follower_or_banned_member_is_never_seated(net):
    h = _h(net)
    sid = await _table(net)
    await h["host"].home.db.enqueue(
        "INSERT INTO space_bans(space_id, user_id, banned_by) VALUES(?, ?, 'anna')",
        (SPACE, h["m"].uid("mia")),
    )
    for sh, user in ((h["f"], "fred"), (h["m"], "mia")):
        _queue(
            net,
            sh,
            h["host"],
            SESSION,
            {
                "space_id": SPACE,
                "kind": "session",
                "body": {
                    "app_id": APP,
                    "session_id": sid,
                    "verb": "group_join",
                    "from_user": user + "2",
                    "user_id": sh.uid(user),
                    "identity_pk": sh.home.fed.own_identity_pk.hex(),
                },
            },
        )
    await net.flush()
    hub = await h["host"].home.group_repo.get(sid)
    assert sorted(e.user for e in hub.seated) == ["anna", "ben", "mia"]


async def test_a_hub_cant_swap_a_seat_key_even_between_unpaired_players(net):
    """b and m aren't paired: b knows m's identity key only as the roster's
    ``identity_pk``, checked against m's household id — the hub can't hand
    b a key of its own for m."""
    h = _h(net)
    sid = await _table(net)
    wire = (await h["host"].home.group_repo.get(sid)).to_wire()
    _priv, pub = new_seat_keypair()
    m_id = h["m"].instance
    swapped = copy.deepcopy(wire)
    swapped["rev"] += 5
    for e in swapped["roster"]:
        if e["instance"] == m_id:
            e["seat_key"] = sign_seat_key(
                seed=h["host"].home.fed.own_identity_seed,
                session_id=sid,
                instance=m_id,
                pub=pub,
            )
    # …nor pass off its own identity key as m's along with it.
    relabelled = copy.deepcopy(swapped)
    relabelled["rev"] += 1
    for e in relabelled["roster"]:
        if e["instance"] == m_id:
            e["identity_pk"] = h["host"].home.fed.own_identity_pk.hex()
    # …nor drop the identity key, so the seat key rests on its word.
    bare = copy.deepcopy(swapped)
    bare["rev"] += 2
    for e in bare["roster"]:
        e.pop("identity_pk", None)
    for table in (swapped, relabelled, bare):
        _queue(
            net,
            h["host"],
            h["b"],
            SESSION,
            {
                "space_id": SPACE,
                "kind": "session",
                "body": {
                    "app_id": APP,
                    "session_id": sid,
                    "verb": "group_roster",
                    "table": table,
                },
            },
        )
    await net.flush()
    assert (await h["b"].home.group_repo.get(sid)).roster_rev == wire["rev"]


async def test_a_card_for_a_space_without_games_is_dropped(net):
    h = _h(net)
    sid = await _table(net)
    hub = await h["host"].home.group_repo.get(sid)
    space = await h["m"].spaces.get(SPACE)
    await h["m"].spaces.save(
        dataclasses.replace(space, features=SpaceFeatures(allowed_apps=()))
    )
    wire = card_wire(dataclasses.replace(hub, session_id="8" * 32), SPACE)
    _queue(net, h["host"], h["m"], TABLE, wire)
    await net.flush()
    assert await h["m"].home.group_repo.get("8" * 32) is None
