"""SpaceTableService — game tables in a space (v_77), across households.

Households (``tests/space_table_network.py``), all members of the private
space "Lakeside" hosted by **host**:

* **host** — Anna (owner), Mo (moderator);
* **b** — Ben (member), paired with host;
* **m** — Mia (member), paired with nobody: reached over the mesh, known
  to the host by its mesh claim only (b doesn't know it at all);
* **f** — Fred, a follower (subscriber) only;
* **x** — Xena, no member of the space at all.

Real SQLite repos, the real group and space-table services, a fake
federation that delivers space events only to the space's member
households and records what went where.
"""

from __future__ import annotations

import dataclasses

import orjson
import pytest

from socialhome.domain.app_group import (
    END_APP_REMOVED,
    END_CANCELLED,
    END_CLOSED_BY_MODERATOR,
    GROUP_ENDED,
    GROUP_LOBBY,
    GROUP_PLAYING,
    SEAT_SEATED,
    AppGroupCantJoinError,
    AppGroupFullError,
    AppGroupNotFoundError,
    AppGroupNotHostError,
    AppGroupSizeError,
)
from socialhome.domain.apps import AppNotEnabledError, AppNotFoundError
from socialhome.domain.events import (
    AppTableAnnounced,
    AppTableClosed,
    SpaceFeaturesApplied,
    SpaceMemberLeft,
)
from socialhome.domain.federation import FederationEventType
from socialhome.domain.space import SpaceFeatures, SpaceType, SubscriberReadOnlyError
from socialhome.domain.space_app_table import (
    CARD_FULL,
    CARD_OPEN,
    CARD_PLAYING,
    SpaceAppNotAllowedError,
    SpaceTableLimitError,
    card_wire,
)
from tests.space_table_network import APP, SPACE, SpaceNetwork

SPACE_TABLE = FederationEventType.SPACE_APP_TABLE.value
SPACE_SESSION = FederationEventType.SPACE_APP_SESSION.value


@pytest.fixture
async def net(tmp_path):
    n = SpaceNetwork(tmp_path)
    host = await n.space_home("host")
    b = await n.space_home("b")
    m = await n.space_home("m")
    f = await n.space_home("f")
    x = await n.space_home("x")
    await host.home.add_user("anna")
    await host.home.add_user("mo")
    await b.home.add_user("ben")
    await m.home.add_user("mia")
    await f.home.add_user("fred")
    await x.home.add_user("xena")
    await n.install(host, b, m, f, x)
    await n.pair(host.home, b.home)
    await n.pair(host.home, f.home)
    # m is reached over the mesh: the host knows its version from its claim.
    n.knows(host, m)
    n.knows(m, host)
    await n.make_space(
        host,
        members={
            host: {"anna": "owner", "mo": "moderator"},
            b: {"ben": "member"},
            m: {"mia": "member"},
            f: {"fred": "subscriber"},
        },
    )
    yield n
    await n.close()


def _h(net):
    homes = list(net.space_homes.values())
    return {sh.name: sh for sh in homes}


async def _open(net, who="host", user="anna", **kw) -> str:
    sh = _h(net)[who]
    view = await sh.tables.open_table(
        space_id=SPACE, actor_user_id=sh.uid(user), app_id=APP, **kw
    )
    await net.flush()
    return view["session_id"]


async def _join(net, who, user, sid):
    sh = _h(net)[who]
    got = await sh.tables.join(
        space_id=SPACE, session_id=sid, actor_user_id=sh.uid(user)
    )
    await net.flush()
    return got


# ─── Opening and the card ──────────────────────────────────────────────────


async def test_a_card_reaches_member_households_and_nobody_else(net):
    h = _h(net)
    sid = await _open(net, note="Bring snacks")
    cards = [(s, t) for s, t, _p in net.space_events() if t == SPACE_TABLE]
    receivers = {
        to
        for s, to, p in net.sent
        if net.types.get(id(p)) is FederationEventType.SPACE_APP_TABLE
    }
    # The follower-only household and the household outside the space hear
    # nothing; the mesh-only member does (the host knows its version).
    assert receivers == {h["b"].instance, h["m"].instance}
    assert all(s == h["host"].instance for s, _t in cards)
    for name in ("b", "m"):
        kept = await h[name].home.group_repo.get(sid)
        assert kept is not None and kept.space_id == SPACE
        assert kept.card is not None and kept.card.seated == 1
        assert kept.note == "Bring snacks"
    for name in ("f", "x"):
        assert await h[name].home.group_repo.get(sid) is None
    # The card names no player: no roster on the wire.
    for _s, t, p in net.space_events():
        if t == SPACE_TABLE:
            assert "roster" not in p and p["seated"] == 1


async def test_members_see_the_open_table_and_followers_see_nothing(net):
    h = _h(net)
    sid = await _open(net)
    listing = await h["b"].tables.list_tables(
        space_id=SPACE, actor_user_id=h["b"].uid("ben")
    )
    (table,) = listing["tables"]
    assert table["session_id"] == sid and table["state"] == CARD_OPEN
    assert table["host_name"] == "Anna" and table["app"]["installed"] is True
    assert listing["allowed_apps"] == [APP] and listing["can_edit_games"] is False
    with pytest.raises(SubscriberReadOnlyError):
        await h["f"].tables.list_tables(
            space_id=SPACE, actor_user_id=h["f"].uid("fred")
        )
    with pytest.raises(KeyError):
        await h["x"].tables.list_tables(
            space_id=SPACE, actor_user_id=h["x"].uid("xena")
        )


async def test_only_listed_apps_in_a_space_that_may_have_games(net):
    h = _h(net)
    space = await h["host"].spaces.get(SPACE)
    await h["host"].spaces.save(
        dataclasses.replace(space, features=SpaceFeatures(allowed_apps=()))
    )
    with pytest.raises(SpaceAppNotAllowedError):
        await _open(net)
    await h["host"].spaces.save(dataclasses.replace(space, space_type=SpaceType.GLOBAL))
    with pytest.raises(SpaceAppNotAllowedError):
        await _open(net)
    await h["host"].spaces.save(dataclasses.replace(space, archived=True))
    with pytest.raises(SpaceAppNotAllowedError):
        await _open(net)


async def test_followers_cant_open_and_sizes_hold(net):
    with pytest.raises(SubscriberReadOnlyError):
        await _open(net, "f", "fred")
    with pytest.raises(AppGroupSizeError):
        await _open(net, max_players=9)
    # A moderator opens one like any member.
    assert await _open(net, user="mo")


async def test_three_a_day_per_person_and_one_let_everyone_know(net):
    h = _h(net)
    await _open(net, announce=True)
    with pytest.raises(SpaceTableLimitError) as exc:
        await _open(net, announce=True)
    assert exc.value.code == "SPACE_TABLE_ANNOUNCE_LIMIT"
    await _open(net)
    await _open(net)
    with pytest.raises(SpaceTableLimitError) as exc:
        await _open(net)
    assert exc.value.code == "SPACE_TABLE_DAILY_LIMIT"
    # "Let everyone know": one row for each member elsewhere, once.
    for name, user in (("b", "ben"), ("m", "mia")):
        rows = h[name].home.bus.of(AppTableAnnounced)
        assert [r.to_user_id for r in rows] == [h[name].uid(user)]
        assert rows[0].space_name == "Lakeside" and rows[0].host_display == "Anna"
    # …and Mo at the host's own household, never Anna herself.
    rows = h["host"].home.bus.of(AppTableAnnounced)
    assert [r.to_user_id for r in rows] == [h["host"].uid("mo")]
    assert h["f"].home.bus.of(AppTableAnnounced) == []


# ─── Joining, capacity, Start ─────────────────────────────────────────────


async def test_members_of_unpaired_households_join_and_start(net):
    h = _h(net)
    sid = await _open(net)
    await _join(net, "b", "ben", sid)
    got = await _join(net, "m", "mia", sid)
    assert got["my_state"] in ("joining", SEAT_SEATED)
    hub = await h["host"].home.group_repo.get(sid)
    assert [e.user for e in hub.seated] == ["anna", "ben", "mia"]
    # Every space message went over the space, never as a bare APP_SESSION.
    assert all(
        net.types[id(p)] is not FederationEventType.APP_SESSION
        for _s, _t, p in net.sent
    )
    # b holds m's seat key — checked against m's identity key, which b knows
    # only from the roster (its household id is that key's fingerprint).
    mirror_b = await h["b"].home.group_repo.get(sid)
    assert [e.user for e in mirror_b.seated] == ["anna", "ben", "mia"]
    assert all(e.seat_key is not None for e in mirror_b.seated)
    await h["host"].home.groups.start(
        app_id=APP, session_id=sid, actor_user_id=h["host"].uid("anna")
    )
    await net.flush()
    for name in ("b", "m"):
        assert (await h[name].home.group_repo.get(sid)).state == GROUP_PLAYING


async def test_a_two_player_table_starts_when_the_second_seat_fills(net):
    h = _h(net)
    for sh in net.space_homes.values():
        await sh.home.app_repo.uninstall(APP)
    await net.install(*net.space_homes.values(), players=(2, 2))
    sid = await _open(net)
    await _join(net, "m", "mia", sid)
    assert (await h["host"].home.group_repo.get(sid)).state == GROUP_PLAYING
    # The card says so: b sees it playing, takes no seat.
    kept = await h["b"].home.group_repo.get(sid)
    assert kept.state == GROUP_PLAYING
    listing = await h["b"].tables.list_tables(
        space_id=SPACE, actor_user_id=h["b"].uid("ben")
    )
    assert [t["state"] for t in listing["tables"]] == [CARD_PLAYING]


async def test_a_full_table_says_so(net):
    h = _h(net)
    sid = await _open(net, max_players=3)
    await _join(net, "b", "ben", sid)
    await _join(net, "m", "mia", sid)
    await h["host"].home.add_user("ann2")
    await h["host"].home.db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
        (SPACE, h["host"].uid("ann2")),
    )
    with pytest.raises(AppGroupFullError):
        await _join(net, "host", "ann2", sid)
    cards = [p for _s, t, p in net.space_events(to=h["b"].instance) if t == SPACE_TABLE]
    assert cards[-1]["state"] == CARD_FULL


async def test_the_card_count_waits_for_its_turn(net):
    h = _h(net)
    h["host"].tables._card_every = 3600.0
    sid = await _open(net)
    await _join(net, "b", "ben", sid)
    cards = [p for _s, t, p in net.space_events(to=h["m"].instance) if t == SPACE_TABLE]
    # Open went out; a second seat taken is not news worth a card yet.
    assert [c["seated"] for c in cards] == [1]


async def test_a_block_hides_the_table_and_refuses_the_join(net):
    h = _h(net)
    sid = await _open(net)
    await h["b"].home.user_repo.block(h["b"].uid("ben"), h["host"].uid("anna"))
    listing = await h["b"].tables.list_tables(
        space_id=SPACE, actor_user_id=h["b"].uid("ben")
    )
    assert listing["tables"] == []
    with pytest.raises(AppGroupCantJoinError):
        await _join(net, "b", "ben", sid)


async def test_joining_needs_the_app_here_and_on(net):
    h = _h(net)
    sid = await _open(net)
    await h["m"].home.app_repo.set_enabled(APP, enabled=False)
    with pytest.raises(AppNotEnabledError):
        await _join(net, "m", "mia", sid)
    await h["m"].home.app_repo.uninstall(APP)
    listing = await h["m"].tables.list_tables(
        space_id=SPACE, actor_user_id=h["m"].uid("mia")
    )
    assert listing["tables"][0]["app"] == {"installed": False, "enabled": False}
    with pytest.raises(AppNotFoundError):
        await _join(net, "m", "mia", sid)


# ─── Closing ──────────────────────────────────────────────────────────────


async def test_a_moderator_closes_any_table_and_players_hear_why(net):
    h = _h(net)
    sid = await _open(net, "b", "ben")  # hosted at b
    await _join(net, "host", "anna", sid)
    await h["host"].tables.close(
        space_id=SPACE, session_id=sid, actor_user_id=h["host"].uid("mo")
    )
    await net.flush()
    hub = await h["b"].home.group_repo.get(sid)
    assert (hub.state, hub.end_reason) == (GROUP_ENDED, END_CLOSED_BY_MODERATOR)
    for name, user in (("b", "ben"), ("host", "anna")):
        (row,) = h[name].home.bus.of(AppTableClosed)
        assert row.to_user_id == h[name].uid(user)
        assert row.reason == END_CLOSED_BY_MODERATOR


async def test_a_member_cant_close_someone_elses_table(net):
    h = _h(net)
    sid = await _open(net)
    with pytest.raises(AppGroupNotHostError):
        await h["b"].tables.close(
            space_id=SPACE, session_id=sid, actor_user_id=h["b"].uid("ben")
        )
    # Nor by forging a moderator's close from b's household.
    net.queue.append(
        (
            h["b"].instance,
            h["host"].instance,
            forged := {
                "space_id": SPACE,
                "kind": "session",
                "body": {
                    "app_id": APP,
                    "session_id": sid,
                    "verb": "group_close",
                    "from_user": "ben",
                    "user_id": h["b"].uid("ben"),
                    "by_role": True,
                },
            },
        )
    )
    net.types[id(forged)] = FederationEventType.SPACE_APP_SESSION
    net.space_of[id(forged)] = SPACE
    await net.flush()
    assert (await h["host"].home.group_repo.get(sid)).state == GROUP_LOBBY


async def test_the_host_closes_their_own(net):
    h = _h(net)
    sid = await _open(net)
    await h["host"].tables.close(
        space_id=SPACE, session_id=sid, actor_user_id=h["host"].uid("anna")
    )
    await net.flush()
    assert (await h["b"].home.group_repo.get(sid)).state == GROUP_ENDED
    assert (await h["b"].home.group_repo.get(sid)).end_reason == END_CANCELLED


async def test_an_app_taken_off_the_list_closes_waiting_tables_only(net):
    h = _h(net)
    waiting = await _open(net)
    playing = await _open(net, user="mo")
    await _join(net, "b", "ben", playing)
    await _join(net, "m", "mia", playing)
    await h["host"].home.groups.start(
        app_id=APP, session_id=playing, actor_user_id=h["host"].uid("mo")
    )
    await net.flush()
    for sh in net.space_homes.values():
        space = await sh.spaces.get(SPACE)
        if space is not None:
            await sh.spaces.save(
                dataclasses.replace(space, features=SpaceFeatures(allowed_apps=()))
            )
    await h["host"].tables._on_space_config(SpaceFeaturesApplied(space_id=SPACE))
    await net.flush()
    w = await h["host"].home.group_repo.get(waiting)
    assert (w.state, w.end_reason) == (GROUP_ENDED, END_APP_REMOVED)
    assert (await h["host"].home.group_repo.get(playing)).state == GROUP_PLAYING
    # No new seats at the game being played.
    with pytest.raises(SpaceAppNotAllowedError):
        await _join(net, "m", "mia", playing)


async def test_leaving_the_space_leaves_its_tables(net):
    h = _h(net)
    sid = await _open(net)
    await h["host"].tables._on_member_left(
        SpaceMemberLeft(space_id=SPACE, user_id=h["host"].uid("anna"))
    )
    await net.flush()
    hub = await h["host"].home.group_repo.get(sid)
    assert (hub.state, hub.end_reason) == (GROUP_ENDED, END_CANCELLED)


# ─── Receivers ────────────────────────────────────────────────────────────


def _queue(net, sender, to, event_type, payload):
    net.queue.append((sender.instance, to.instance, payload))
    net.sent.append((sender.instance, to.instance, payload))
    net.types[id(payload)] = event_type
    net.space_of[id(payload)] = SPACE


async def test_a_card_for_someone_without_a_writer_seat_is_dropped(net):
    h = _h(net)
    sid = await _open(net)
    hub = await h["host"].home.group_repo.get(sid)
    forged = card_wire(dataclasses.replace(hub, session_id="f" * 32), SPACE)
    forged["host_user_id"] = h["f"].uid("fred")
    _queue(net, h["f"], h["b"], FederationEventType.SPACE_APP_TABLE, forged)
    # And a card from the follower household naming the host's owner.
    other = card_wire(dataclasses.replace(hub, session_id="e" * 32), SPACE)
    _queue(net, h["f"], h["b"], FederationEventType.SPACE_APP_TABLE, other)
    await net.flush()
    assert await h["b"].home.group_repo.get("f" * 32) is None
    assert await h["b"].home.group_repo.get("e" * 32) is None


async def test_a_card_for_an_app_off_the_list_or_from_another_hub_is_dropped(net):
    h = _h(net)
    sid = await _open(net)
    hub = await h["host"].home.group_repo.get(sid)
    wire = card_wire(dataclasses.replace(hub, session_id="c" * 32), SPACE)
    wire["app_id"] = "poker"
    _queue(net, h["host"], h["b"], FederationEventType.SPACE_APP_TABLE, wire)
    # b re-announcing the host's table as its own.
    hijack = card_wire(dataclasses.replace(hub, roster_rev=9), SPACE)
    hijack["host_user_id"] = h["b"].uid("ben")
    _queue(net, h["b"], h["m"], FederationEventType.SPACE_APP_TABLE, hijack)
    await net.flush()
    assert await h["b"].home.group_repo.get("c" * 32) is None
    kept = await h["m"].home.group_repo.get(sid)
    assert kept.hub_instance == h["host"].instance and kept.roster_rev == 1


async def test_receivers_cap_new_tables_per_person_per_day(net):
    h = _h(net)
    sid = await _open(net)
    hub = await h["host"].home.group_repo.get(sid)
    for n in range(4):
        wire = card_wire(dataclasses.replace(hub, session_id=f"{n}" * 32), SPACE)
        wire["announce"] = True
        _queue(net, h["host"], h["b"], FederationEventType.SPACE_APP_TABLE, wire)
    await net.flush()
    kept = [await h["b"].home.group_repo.get(f"{n}" * 32) for n in range(4)]
    # One already came today (the real one): two more fit, the rest drop.
    assert [k is not None for k in kept] == [True, True, False, False]
    # A hub that ignores the one-a-day "let everyone know" is held to it.
    assert len(h["b"].home.bus.of(AppTableAnnounced)) == 1


async def test_a_join_needs_a_live_writer_seat_and_the_households_own_key(net):
    h = _h(net)
    sid = await _open(net)
    base = {
        "app_id": APP,
        "session_id": sid,
        "verb": "group_join",
        "from_user": "fred",
        "user_id": h["f"].uid("fred"),
        "identity_pk": h["f"].home.fed.own_identity_pk.hex(),
    }
    _queue(
        net,
        h["f"],
        h["host"],
        FederationEventType.SPACE_APP_SESSION,
        {"space_id": SPACE, "kind": "session", "body": base},
    )
    # Mia's join, but with another household's identity key.
    wrong = dict(
        base,
        from_user="mia",
        user_id=h["m"].uid("mia"),
        identity_pk=h["b"].home.fed.own_identity_pk.hex(),
    )
    _queue(
        net,
        h["m"],
        h["host"],
        FederationEventType.SPACE_APP_SESSION,
        {"space_id": SPACE, "kind": "session", "body": wrong},
    )
    await net.flush()
    hub = await h["host"].home.group_repo.get(sid)
    assert [e.user for e in hub.seated] == ["anna"]


async def test_a_space_tables_verbs_only_ride_its_space(net):
    h = _h(net)
    sid = await _open(net)
    await _join(net, "b", "ben", sid)
    # The same leave as a bare APP_SESSION (a paired peer) is dropped.
    leave = {
        "app_id": APP,
        "session_id": sid,
        "verb": "group_leave",
        "from_user": "ben",
        "reason": "left",
    }
    net.forge(h["b"].instance, h["host"].instance, leave)
    await net.flush()
    hub = await h["host"].home.group_repo.get(sid)
    assert [e.user for e in hub.seated] == ["anna", "ben"]


async def test_group_messages_ride_the_space_and_reach_unpaired_players(net):
    h = _h(net)
    sid = await _open(net)
    await _join(net, "b", "ben", sid)
    await _join(net, "m", "mia", sid)
    await h["host"].home.groups.start(
        app_id=APP, session_id=sid, actor_user_id=h["host"].uid("anna")
    )
    await net.flush()
    await h["m"].home.groups.send_message(
        app_id=APP, session_id=sid, actor_user_id=h["m"].uid("mia"), data={"go": 1}
    )
    await net.flush()
    frames = [
        f
        for uid, f in h["b"].home.ws.frames
        if uid == h["b"].uid("ben") and f["type"] == "app.group.message"
    ]
    assert [f["data"] for f in frames] == [{"go": 1}]
    kinds = [p.get("kind") for _s, t, p in net.space_events() if t == SPACE_SESSION]
    assert "message" in kinds
    # Never anything to the follower or the outsider.
    assert not net.space_events(to=h["f"].instance)
    assert not net.space_events(to=h["x"].instance)
    assert b'"go"' not in orjson.dumps(
        [p for s, t, p in net.sent if t in (h["f"].instance, h["x"].instance)]
    )


async def test_an_unknown_table_or_space_is_dropped(net):
    h = _h(net)
    _queue(
        net,
        h["host"],
        h["b"],
        FederationEventType.SPACE_APP_SESSION,
        {"space_id": SPACE, "kind": "session", "body": {"session_id": "nope"}},
    )
    _queue(
        net, h["host"], h["x"], FederationEventType.SPACE_APP_TABLE, {"space_id": SPACE}
    )
    await net.flush()
    with pytest.raises(AppGroupNotFoundError):
        await h["b"].tables.join(
            space_id=SPACE, session_id="nope", actor_user_id=h["b"].uid("ben")
        )
