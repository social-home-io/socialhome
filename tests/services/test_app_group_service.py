"""AppGroupService — group tables (v_76) across three households.

Households (``tests/app_group_network.py``): **hub** (Anna, who opens the
tables, and Ada), **b** (Ben), **c** (Cleo); the hub is paired with b and
c, b and c are not paired with each other. Real SQLite repos per
household, the real app bridge and group service, a fake federation that
delivers the sealed ``APP_SESSION`` payloads in order.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.app_group import (
    END_CANCELLED,
    END_EXPIRED,
    END_HOST_GONE,
    GROUP_ENDED,
    GROUP_LOBBY,
    GROUP_OPEN_TABLES_MAX,
    GROUP_PLAYING,
    SEAT_DECLINED,
    SEAT_LEFT,
    SEAT_REMOVED,
    SEAT_SEATED,
    AppGroupCantJoinError,
    AppGroupClosedError,
    AppGroupHouseholdTooOldError,
    AppGroupLimitError,
    AppGroupNotFoundError,
    AppGroupNotHostError,
    AppGroupSessionError,
    AppGroupSizeError,
    AppGroupsNotSupportedError,
    AppGroupTooFewError,
)
from socialhome.domain.apps import INVITES_OPEN_MAX, AppInviteLimitError
from socialhome.domain.events import (
    AppChallengeEnded,
    AppChallengeReceived,
    AppUninstalled,
)
from tests.app_group_network import APP, Network, quiz_app


@pytest.fixture
async def net(tmp_path):
    n = Network(tmp_path)
    hub = await n.household("hub")
    b = await n.household("b")
    c = await n.household("c")
    old = await n.household("old", version=75)
    await hub.add_user("anna")
    await hub.add_user("ada")
    await b.add_user("ben")
    await c.add_user("cleo")
    await old.add_user("olga")
    for home in (hub, b, c, old):
        await home.app_repo.install(quiz_app())
    await n.pair(hub, b)
    await n.pair(hub, c)
    await n.pair(hub, old)
    yield n
    await n.close()


def _homes(net):
    return net.homes["hub"], net.homes["b"], net.homes["c"]


async def _open(net, *, people=("ada", "ben", "cleo"), **kw) -> str:
    hub, b, c = _homes(net)
    who = {"ada": (hub, "ada"), "ben": (b, "ben"), "cleo": (c, "cleo")}
    targets = [hub.target(*who[p]) for p in people]
    view = await hub.groups.open_table(
        app_id=APP, actor_user_id=hub.uid("anna"), targets=targets, **kw
    )
    await net.flush()
    return view["session_id"]


async def _view(home, user, sid):
    return await home.groups.get_table(
        app_id=APP, session_id=sid, actor_user_id=home.uid(user)
    )


# ─── Opening and inviting ─────────────────────────────────────────────────


async def test_open_invites_each_person_and_names_nobody_else(net):
    hub, b, _c = _homes(net)
    sid = await _open(net)
    host = await _view(hub, "anna", sid)
    assert host["state"] == GROUP_LOBBY
    assert host["is_host"] and host["me"] == 1 and host["host"] == 1
    assert host["invited"] == 3
    assert [p["name"] for p in host["players"]] == ["Anna"]
    # Ada (same household) waits in Needs you; nothing travels for her.
    ada = await _view(hub, "ada", sid)
    assert ada["my_state"] == "invited" and ada["me"] is None
    # Ben and Cleo each got one invite naming only the seated host.
    invites = [p for _s, _t, p in net.sent if p["verb"] == "group_invite"]
    assert sorted(p["to_user"] for p in invites) == ["ben", "cleo"]
    for p in invites:
        assert [e["user"] for e in p["table"]["roster"]] == ["anna"]
        assert p["others"] == 2
    ben = await _view(b, "ben", sid)
    assert ben["my_state"] == "invited" and ben["invited"] is None
    got = b.bus.of(AppChallengeReceived)
    assert got[0].to_user_id == b.uid("ben")
    assert got[0].from_display == "Anna · Home hub"
    assert hub.bus.of(AppChallengeReceived)[0].to_user_id == hub.uid("ada")


async def test_open_refuses_what_doesnt_fit(net):
    hub, b, c = _homes(net)
    anna = hub.uid("anna")
    with pytest.raises(AppGroupSizeError):  # host + 1 < the app's 3
        await hub.groups.open_table(
            app_id=APP, actor_user_id=anna, targets=[hub.target(b, "ben")]
        )
    with pytest.raises(AppGroupSizeError):  # max past the sheet's 10
        await hub.groups.open_table(
            app_id=APP,
            actor_user_id=anna,
            targets=[hub.target(b, "ben"), hub.target(c, "cleo")],
            max_players=11,
        )
    with pytest.raises(AppGroupHouseholdTooOldError):
        await hub.groups.open_table(
            app_id=APP,
            actor_user_id=anna,
            targets=[hub.target(b, "ben"), hub.target(net.homes["old"], "olga")],
        )
    await hub.app_repo.update_installed(quiz_app(tables=False))
    with pytest.raises(AppGroupsNotSupportedError):
        await hub.groups.open_table(
            app_id=APP,
            actor_user_id=anna,
            targets=[hub.target(b, "ben"), hub.target(c, "cleo")],
        )


async def test_the_invite_sheet_says_who_can_have_a_seat(net):
    hub = net.homes["hub"]
    people = await hub.app_fed.list_invitable(app_id=APP, self_user_id=hub.uid("anna"))
    groups = {p["user_ref"]: p["groups"] for p in people}
    assert groups == {hub.uid("ada"): True, "ben": True, "cleo": True, "olga": False}


async def test_tables_count_against_the_invite_caps_per_person(net):
    hub, _b, _c = _homes(net)
    anna = hub.uid("anna")
    await _open(net)
    counts = await hub.app_fed.invite_counts(app_id=APP, user_id=anna)
    assert counts["open"] == 3 and counts["today"] == 3
    # Three tables of three: nine open invites, one more table passes 10.
    await _open(net)
    await _open(net)
    with pytest.raises(AppInviteLimitError):
        await _open(net, people=("ben", "cleo"))
    assert counts["max_open"] == INVITES_OPEN_MAX


async def test_open_tables_per_person_are_capped(net):
    hub = net.homes["hub"]
    for _ in range(GROUP_OPEN_TABLES_MAX):
        sid = await _open(net, people=("ada", "ben"))
        # Answered invites stop counting towards the open-invite cap.
        await hub.groups.decline(
            app_id=APP, session_id=sid, actor_user_id=hub.uid("ada")
        )
    with pytest.raises(AppGroupLimitError):
        await _open(net, people=("ada", "ben"))


# ─── Joining, starting ────────────────────────────────────────────────────


async def test_join_seats_and_every_household_sees_the_roster(net):
    hub, b, c = _homes(net)
    sid = await _open(net)
    view = await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    assert view["my_state"] == "joining"
    await net.flush()
    assert (await _view(b, "ben", sid))["me"] == 2
    await c.groups.join(app_id=APP, session_id=sid, actor_user_id=c.uid("cleo"))
    await net.flush()
    for home, user in ((hub, "anna"), (b, "ben"), (c, "cleo")):
        v = await _view(home, user, sid)
        assert [(p["seat"], p["name"]) for p in v["players"]] == [
            (1, "Anna"),
            (2, "Ben"),
            (3, "Cleo"),
        ]
    ended = b.bus.of(AppChallengeEnded)
    assert [(e.user_id, e.outcome) for e in ended] == [(b.uid("ben"), "accepted")]
    # Who sat: one seat answer each, rosters to the seated households.
    assert net.verbs(to="b").count("group_seat") == 1
    assert "group_roster" in net.verbs(to="b")


async def test_start_needs_the_fewest_players_and_withdraws_the_rest(net):
    hub, b, c = _homes(net)
    sid = await _open(net)
    anna = hub.uid("anna")
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    with pytest.raises(AppGroupTooFewError):
        await hub.groups.start(app_id=APP, session_id=sid, actor_user_id=anna)
    with pytest.raises(AppGroupNotHostError):
        await b.groups.start(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await c.groups.join(app_id=APP, session_id=sid, actor_user_id=c.uid("cleo"))
    await net.flush()
    view = await hub.groups.start(app_id=APP, session_id=sid, actor_user_id=anna)
    await net.flush()
    assert view["state"] == GROUP_PLAYING
    assert (await _view(c, "cleo", sid))["state"] == GROUP_PLAYING
    # Ada never answered: her invite is withdrawn.
    assert await hub.app_repo.get_pending_session(APP, hub.uid("ada"), sid) is None
    assert ("withdrawn",) in {(e.outcome,) for e in hub.bus.of(AppChallengeEnded)}
    with pytest.raises(AppGroupClosedError):
        await hub.groups.start(app_id=APP, session_id=sid, actor_user_id=anna)


async def test_a_two_player_table_starts_when_the_second_seat_fills(net):
    hub, b, _c = _homes(net)
    await hub.app_repo.update_installed(quiz_app(players=(2, 2)))
    await b.app_repo.update_installed(quiz_app(players=(2, 2)))
    sid = await _open(net, people=("ben",))
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    assert (await _view(hub, "anna", sid))["state"] == GROUP_PLAYING
    assert (await _view(b, "ben", sid))["state"] == GROUP_PLAYING


async def test_a_local_invitee_joins_on_the_hub_itself(net):
    hub, _b, _c = _homes(net)
    sid = await _open(net)
    view = await hub.groups.join(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("ada")
    )
    assert view["my_state"] == SEAT_SEATED and view["me"] == 2
    with pytest.raises(AppGroupClosedError):  # already in
        await hub.groups.join(app_id=APP, session_id=sid, actor_user_id=hub.uid("ada"))


# ─── Declining, leaving, removing, closing ────────────────────────────────


async def test_decline_tells_the_hub_but_not_the_players(net):
    hub, _b, c = _homes(net)
    sid = await _open(net)
    await c.groups.decline(app_id=APP, session_id=sid, actor_user_id=c.uid("cleo"))
    await net.flush()
    table = await hub.group_repo.get(sid)
    assert table.entry("c", "cleo").state == SEAT_DECLINED
    assert await c.app_repo.get_pending_session(APP, c.uid("cleo"), sid) is None
    assert net.verbs(to="b") == ["group_invite"]
    with pytest.raises(AppGroupNotFoundError):
        await _view(c, "cleo", sid)


async def test_the_host_leaving_a_lobby_closes_it_for_everyone(net):
    hub, b, c = _homes(net)
    sid = await _open(net)
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    await hub.groups.leave(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await net.flush()
    ben = await _view(b, "ben", sid)
    assert (ben["state"], ben["end_reason"]) == (GROUP_ENDED, END_CANCELLED)
    # Cleo's invite (never answered) ends with it.
    assert await c.app_repo.get_pending_session(APP, c.uid("cleo"), sid) is None
    assert (await c.group_repo.get(sid)).state == GROUP_ENDED


async def test_after_start_the_host_hands_over_and_the_new_host_removes(net):
    hub, b, c = _homes(net)
    sid = await _open(net)
    for home, user in ((b, "ben"), (c, "cleo"), (hub, "ada")):
        await home.groups.join(app_id=APP, session_id=sid, actor_user_id=home.uid(user))
        await net.flush()
    await hub.groups.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await net.flush()
    await hub.groups.leave(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await net.flush()
    # Ben sat down first: he hosts now; the hub household keeps relaying.
    ben = await _view(b, "ben", sid)
    assert ben["is_host"] and ben["host"] == 2 and ben["state"] == GROUP_PLAYING
    table = await hub.group_repo.get(sid)
    assert (table.hub_instance, table.host_instance) == ("hub", "b")
    # Ben (on b) removes Cleo: a command to the hub, which removes her.
    with pytest.raises(AppGroupNotHostError):
        await c.groups.remove(
            app_id=APP, session_id=sid, actor_user_id=c.uid("cleo"), seat=2
        )
    await b.groups.remove(
        app_id=APP, session_id=sid, actor_user_id=b.uid("ben"), seat=3
    )
    await net.flush()
    assert (await hub.group_repo.get(sid)).at_seat(3).state == SEAT_REMOVED
    assert await c.app_repo.get_pending_session(APP, c.uid("cleo"), sid) is None
    assert any(
        f.get("change") == SEAT_REMOVED
        for uid, f in c.ws.frames
        if uid == c.uid("cleo")
    )
    assert (await _view(b, "ben", sid))["players"][2]["state"] == SEAT_REMOVED


async def test_leave_before_start_frees_the_seat(net):
    hub, b, _c = _homes(net)
    sid = await _open(net)
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    await b.groups.leave(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    table = await hub.group_repo.get(sid)
    assert table.entry("b", "ben").state == SEAT_LEFT
    assert [e.user for e in table.seated] == ["anna"]


async def test_the_host_closes_the_table(net):
    hub, b, _c = _homes(net)
    sid = await _open(net)
    with pytest.raises(AppGroupNotHostError):
        await b.groups.close(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await hub.groups.close(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await net.flush()
    assert (await b.group_repo.get(sid)).end_reason == END_CANCELLED
    assert await b.app_repo.get_pending_session(APP, b.uid("ben"), sid) is None
    with pytest.raises(AppGroupClosedError):
        await hub.groups.close(
            app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
        )


# ─── Blocks and children (each household's own rules) ─────────────────────


async def test_an_invite_from_a_host_one_blocked_is_hidden(net):
    _hub, b, _c = _homes(net)
    await b.user_repo.block(b.uid("ben"), "r-hub-anna")
    sid = await _open(net)
    assert await b.app_repo.get_pending_session(APP, b.uid("ben"), sid) is None
    assert await b.group_repo.get(sid) is None


async def test_someone_one_blocked_sitting_down_takes_one_out(net):
    hub, b, _c = _homes(net)
    await hub.user_repo.block(hub.uid("ada"), "r-b-ben")
    sid = await _open(net)
    await hub.groups.join(app_id=APP, session_id=sid, actor_user_id=hub.uid("ada"))
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    table = await hub.group_repo.get(sid)
    assert table.entry("hub", "ada").state == SEAT_LEFT
    assert table.entry("b", "ben").state == SEAT_SEATED
    told = [f for uid, f in hub.ws.frames if uid == hub.uid("ada") and "change" in f]
    assert told[-1]["reason"] == "cant_play"
    # Ben's household sees an ordinary leave, no reason.
    ada = next(p for p in (await _view(b, "ben", sid))["players"] if p["name"] == "Ada")
    assert ada["state"] == SEAT_LEFT


async def test_join_refused_without_a_reason_when_blocked(net):
    hub, b, _c = _homes(net)
    sid = await _open(net)
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    await hub.user_repo.block(hub.uid("ada"), "r-b-ben")
    with pytest.raises(AppGroupCantJoinError):
        await hub.groups.join(app_id=APP, session_id=sid, actor_user_id=hub.uid("ada"))
    assert (await hub.group_repo.get(sid)).entry("hub", "ada").state != SEAT_SEATED


async def test_a_child_sits_only_with_households_it_may_message(net):
    hub, b, c = _homes(net)
    c.cp.protected.add(c.uid("cleo"))
    c.cp.direct_peers.discard("b")  # c and b were never paired
    sid = await _open(net)
    # Ben sits first: Cleo's invite is hidden on her household — nothing
    # goes back to the hub (no decline follows Ben sitting down).
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    assert await c.app_repo.get_pending_session(APP, c.uid("cleo"), sid) is None
    assert (await hub.group_repo.get(sid)).entry("c", "cleo").state == "invited"
    assert "group_leave" not in net.verbs(to="hub")
    with pytest.raises(AppGroupNotFoundError):
        await c.groups.join(app_id=APP, session_id=sid, actor_user_id=c.uid("cleo"))


async def test_a_child_already_seated_leaves_when_a_stranger_sits_down(net):
    hub, b, c = _homes(net)
    c.cp.protected.add(c.uid("cleo"))
    c.cp.direct_peers.discard("b")
    sid = await _open(net)
    await c.groups.join(app_id=APP, session_id=sid, actor_user_id=c.uid("cleo"))
    await net.flush()
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    assert (await hub.group_repo.get(sid)).entry("c", "cleo").state == SEAT_LEFT
    assert await c.app_repo.get_pending_session(APP, c.uid("cleo"), sid) is None


async def test_a_guardian_block_takes_the_child_out(net):
    hub, b, _c = _homes(net)
    sid = await _open(net)
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    await hub.groups.join(app_id=APP, session_id=sid, actor_user_id=hub.uid("ada"))
    await net.flush()
    hub.cp.blocks.add(frozenset({hub.uid("ada"), "r-b-ben"}))
    await hub.groups.on_guardian_block(hub.uid("ada"), "r-b-ben")
    await net.flush()
    assert (await hub.group_repo.get(sid)).entry("hub", "ada").state == SEAT_LEFT


# ─── The 1:1 paths leave group seats alone ────────────────────────────────


async def test_one_to_one_routes_refuse_a_group_seat(net):
    _hub, b, _c = _homes(net)
    sid = await _open(net)
    ben = b.uid("ben")
    with pytest.raises(AppGroupSessionError):
        await b.app_fed.accept_session(app_id=APP, session_id=sid, actor_user_id=ben)
    with pytest.raises(AppGroupSessionError):
        await b.app_fed.decline_session(app_id=APP, session_id=sid, actor_user_id=ben)
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=ben)
    await net.flush()
    with pytest.raises(AppGroupSessionError):
        await b.app_fed.send_message(
            app_id=APP,
            target={"instance_id": "hub", "user_ref": "anna", "is_local": False},
            session_id=sid,
            payload={"move": 1},
            actor_user_id=ben,
        )
    assert await b.app_fed.list_contacts(app_id=APP, self_user_id=ben) == []


async def test_a_one_to_one_close_from_the_host_doesnt_end_a_group_invite(net):
    hub, b, _c = _homes(net)
    sid = await _open(net)
    await hub.app_fed._send_close(
        app_id=APP,
        session_id=sid,
        actor_user_id=hub.uid("anna"),
        to_instance="b",
        to_user="ben",
        reason="withdrawn",
    )
    await net.flush()
    assert await b.app_repo.get_pending_session(APP, b.uid("ben"), sid) is not None


# ─── Housekeeping ─────────────────────────────────────────────────────────


async def test_an_expired_lobby_ends_everywhere(net):
    hub, b, _c = _homes(net)
    sid = await _open(net)
    past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    await hub.db.enqueue(
        "UPDATE app_group_sessions SET expires_at = ? WHERE session_id = ?", (past, sid)
    )
    await hub.groups.sweep()
    await net.flush()
    table = await b.group_repo.get(sid)
    assert (table.state, table.end_reason) == (GROUP_ENDED, END_EXPIRED)
    ended = b.bus.of(AppChallengeEnded)
    assert [(e.user_id, e.outcome) for e in ended] == [(b.uid("ben"), "expired")]


async def test_a_copy_ends_on_its_own_when_the_hub_is_gone(net):
    _hub, b, _c = _homes(net)
    sid = await _open(net)
    past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    await b.db.enqueue(
        "UPDATE app_group_sessions SET expires_at = ? WHERE session_id = ?", (past, sid)
    )
    await b.groups.sweep()
    assert (await b.group_repo.get(sid)).end_reason == END_EXPIRED
    assert await b.app_repo.get_pending_session(APP, b.uid("ben"), sid) is None


async def test_ended_tables_are_forgotten_a_week_later(net):
    hub, _b, _c = _homes(net)
    sid = await _open(net)
    await hub.groups.close(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    await hub.db.enqueue(
        "UPDATE app_group_sessions SET updated_at = ? WHERE session_id = ?", (old, sid)
    )
    assert await hub.groups.sweep() == 1
    assert await hub.group_repo.get(sid) is None


async def test_uninstall_at_the_hub_ends_the_table(net):
    hub, b, _c = _homes(net)
    sid = await _open(net)
    await hub.app_repo.uninstall(APP)
    await hub.groups.on_app_uninstalled(AppUninstalled(app_id=APP))
    await net.flush()
    assert (await b.group_repo.get(sid)).end_reason == END_HOST_GONE
    assert await hub.group_repo.get(sid) is None


async def test_uninstall_at_a_player_leaves_the_table(net):
    hub, b, _c = _homes(net)
    sid = await _open(net)
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    await b.app_repo.uninstall(APP)
    await b.groups.on_app_uninstalled(AppUninstalled(app_id=APP))
    await net.flush()
    assert (await hub.group_repo.get(sid)).entry("b", "ben").state == SEAT_LEFT
    assert await b.group_repo.get(sid) is None


async def test_rosters_coalesce_and_stop_sends_nothing_more(net, tmp_path):
    hub, b, c = _homes(net)
    hub.groups._roster_every = 60.0
    sid = await _open(net)
    await b.groups.join(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    await net.flush()
    rosters = net.verbs().count("group_roster")
    await c.groups.join(app_id=APP, session_id=sid, actor_user_id=c.uid("cleo"))
    await hub.groups.join(app_id=APP, session_id=sid, actor_user_id=hub.uid("ada"))
    for _ in range(5):
        await net.flush()
    # Inside the 60 s window: the later joins wait for one coalesced roster.
    assert net.verbs().count("group_roster") == rosters
    assert sid in hub.groups._roster_due
    await hub.groups.stop()
    assert net.verbs().count("group_roster") == rosters


async def test_list_tables_lists_the_callers_places(net):
    _hub, b, _c = _homes(net)
    sid = await _open(net)
    listed = await b.groups.list_tables(app_id=APP, actor_user_id=b.uid("ben"))
    assert [g["session_id"] for g in listed] == [sid]
    assert listed[0]["players"][0] == {
        "seat": 1,
        "name": "Anna",
        "household": None,
        "is_local": False,
        "state": SEAT_SEATED,
    }
