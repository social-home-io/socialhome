"""AppDrawService — the gift draw end to end over real households (v_78).

Every household runs the real group and draw services on its own SQLite
database; :func:`tests.app_draw_network.settle` carries the sealed
``APP_SESSION`` payloads between them. Tamper and privacy checks live in
``tests/protocol/test_app_draw.py``.
"""

from __future__ import annotations

import copy
import logging

import pytest

from socialhome.crypto import b64url_decode, b64url_encode, sign_ed25519
from socialhome.domain.app_draw import (
    AppDrawHostElsewhereError,
    AppDrawNoResultError,
    AppDrawNotHostError,
    AppDrawPairsError,
    AppDrawStateError,
    AppDrawTooFewError,
    AppDrawWishTooLongError,
    AppDrawWishUpdatesError,
    DrawWireError,
)
from socialhome.domain.events import AppDrawChanged
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.federation import draw_crypto as dc
from socialhome.services.app_draw_service import (
    _int,
    _parse_people,
    _raw,
    _raw_list,
    _Round,
    check_list,
    complaint_holds,
)
from tests.app_draw_network import APP, draw_table, settle
from tests.app_group_network import Network

PEOPLE = {
    "hub": ["anna", "olaf"],
    "b": ["ben", "bea"],
    "c": ["cleo"],
    "d": ["dev"],
}


@pytest.fixture
async def net(tmp_path):
    n = Network(tmp_path)
    yield n
    await n.close()


def _everyone(net):
    for inst, users in PEOPLE.items():
        for u in users:
            yield net.homes[inst], u


async def _results(net, sid) -> dict[tuple[str, str], tuple[str, str]]:
    """Giver → recipient, as each person's own result sheet says."""
    out = {}
    for home, u in _everyone(net):
        res = await home.draw.result(
            app_id=APP, session_id=sid, actor_user_id=home.uid(u)
        )
        out[(home.instance, u)] = (res["name"], res["household"])
    return out


def _circle_ok(results: dict) -> None:
    names = {u.title(): (inst, u) for inst, users in PEOPLE.items() for u in users}
    givers = {}
    for giver, (name, _hh) in results.items():
        to = names[name]
        assert to != giver, "someone drew themselves"
        givers.setdefault(to, []).append(giver)
    assert set(givers) == set(results), "someone gets no gift"
    assert all(len(g) == 1 for g in givers.values()), "someone gets two gifts"
    # One circle (a single cycle through everyone).
    start = next(iter(results))
    seen, cur = set(), start
    while cur not in seen:
        seen.add(cur)
        cur = names[results[cur][0]]
    assert seen == set(results)


async def test_a_draw_over_four_households_gives_everyone_one_circle(net):
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.configure(
        app_id=APP,
        session_id=sid,
        actor_user_id=hub.uid("anna"),
        settings={
            "name": "Familie Müller 2026",
            "budget": "about 20 €",
            "day": "2026-12-24",
        },
    )
    b = net.homes["b"]
    await b.draw.set_wish(
        app_id=APP,
        session_id=sid,
        actor_user_id=b.uid("ben"),
        text="A book about birds",
    )
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    results = await _results(net, sid)
    _circle_ok(results)
    # Ben's giver sees his wish list, the budget and the day.
    giver = next(g for g, (n, _h) in results.items() if n == "Ben")
    home = net.homes[giver[0]]
    sheet = await home.draw.result(
        app_id=APP, session_id=sid, actor_user_id=home.uid(giver[1])
    )
    assert sheet["wish"] == "A book about birds"
    assert (sheet["budget"], sheet["day"], sheet["draw_name"]) == (
        "about 20 €",
        "2026-12-24",
        "Familie Müller 2026",
    )
    assert sheet["household"] == "Home b"
    for home, u in _everyone(net):
        status = await home.draw.status(
            app_id=APP, session_id=sid, actor_user_id=home.uid(u)
        )
        assert status["state"] == "ready" and status["has_result"] is True
        rows = [e for e in home.bus.of(AppDrawChanged) if e.to_user_id == home.uid(u)]
        assert [e.change for e in rows] == ["ready"]


async def test_the_app_frame_never_carries_a_result(net):
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    results = await _results(net, sid)
    for home in net.homes.values():
        frames = [f for _u, f in home.ws.frames if f.get("type") == "app.draw"]
        assert frames
        blob = repr(frames)
        for name, household in results.values():
            assert name not in blob and household not in blob
        for f in frames:
            assert set(f["draw"]) >= {"state", "has_result"}
            assert "name" not in f["draw"] and "recipient" not in f["draw"]


async def test_exclusions_hold_and_rounds_redo_with_proof(net):
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    # anna (seat 1) & olaf (seat 2) and ben (3) & bea (4) are couples.
    await hub.draw.configure(
        app_id=APP,
        session_id=sid,
        actor_user_id=hub.uid("anna"),
        settings={"pairs": [[1, 2], [3, 4]]},
    )
    for _ in range(3):
        if (
            await hub.draw.status(
                app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
            )
        )["gen"]:
            await hub.draw.start(
                app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), again=True
            )
        else:
            await hub.draw.start(
                app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
            )
        await settle(net)
        results = await _results(net, sid)
        _circle_ok(results)
        assert results[("hub", "anna")][0] != "Olaf"
        assert results[("hub", "olaf")][0] != "Anna"
        assert results[("b", "ben")][0] != "Bea"
        assert results[("b", "bea")][0] != "Ben"


async def test_only_the_organiser_on_the_host_household_runs_the_draw(net):
    sid = await draw_table(net, PEOPLE)
    b, hub = net.homes["b"], net.homes["hub"]
    with pytest.raises(AppDrawNotHostError):
        await b.draw.start(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    with pytest.raises(AppDrawNotHostError):
        await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("olaf"))
    with pytest.raises(AppDrawNotHostError):
        await b.draw.configure(
            app_id=APP, session_id=sid, actor_user_id=b.uid("ben"), settings={}
        )
    with pytest.raises(AppDrawStateError):
        await hub.draw.start(
            app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), again=True
        )
    assert AppDrawHostElsewhereError.status == 409


async def test_pairs_must_fit(net):
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    with pytest.raises(AppDrawPairsError):
        await hub.draw.configure(
            app_id=APP,
            session_id=sid,
            actor_user_id=hub.uid("anna"),
            settings={"pairs": [[1, 99]]},
        )
    # Three pairs for six people is more than a third.
    await hub.draw.configure(
        app_id=APP,
        session_id=sid,
        actor_user_id=hub.uid("anna"),
        settings={"pairs": [[1, 2], [3, 4], [5, 6]]},
    )
    with pytest.raises(AppDrawPairsError):
        await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))


async def test_too_few_people(net):
    sid = await draw_table(net, {"hub": ["anna"], "b": ["ben"], "c": ["cleo"]})
    c = net.homes["c"]
    await c.groups.leave(app_id=APP, session_id=sid, actor_user_id=c.uid("cleo"))
    await settle(net)
    hub = net.homes["hub"]
    with pytest.raises(AppDrawTooFewError):
        await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))


async def test_no_result_before_the_draw(net):
    sid = await draw_table(net, PEOPLE)
    b = net.homes["b"]
    with pytest.raises(AppDrawNoResultError):
        await b.draw.result(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    status = await b.draw.status(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    assert status["state"] == "gathering"


async def test_wish_list_bounds_and_updates_after_the_reveal(net):
    sid = await draw_table(net, PEOPLE)
    hub, b = net.homes["hub"], net.homes["b"]
    with pytest.raises(AppDrawWishTooLongError):
        await b.draw.set_wish(
            app_id=APP, session_id=sid, actor_user_id=b.uid("ben"), text="x" * 501
        )
    with pytest.raises(AppDrawWishTooLongError):
        # 500 characters, but far past the 1 KiB card in UTF-8.
        await b.draw.set_wish(
            app_id=APP, session_id=sid, actor_user_id=b.uid("ben"), text="😀" * 400
        )
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    giver = next(g for g, (n, _h) in (await _results(net, sid)).items() if n == "Ben")
    home = net.homes[giver[0]]
    for i in range(5):
        await b.draw.set_wish(
            app_id=APP, session_id=sid, actor_user_id=b.uid("ben"), text=f"socks #{i}"
        )
        await settle(net)
        sheet = await home.draw.result(
            app_id=APP, session_id=sid, actor_user_id=home.uid(giver[1])
        )
        assert sheet["wish"] == f"socks #{i}" and sheet["wish_updated"] is True
    with pytest.raises(AppDrawWishUpdatesError):
        await b.draw.set_wish(
            app_id=APP, session_id=sid, actor_user_id=b.uid("ben"), text="more"
        )
    changes = [
        e.change
        for e in home.bus.of(AppDrawChanged)
        if e.to_user_id == home.uid(giver[1])
    ]
    assert changes.count("wish") == 5


async def test_a_leaver_after_the_reveal_hands_their_recipient_to_their_giver(net):
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    before = await _results(net, sid)
    leaver = ("c", "cleo")
    leaver_to = before[leaver]
    giver = next(g for g, (n, _h) in before.items() if n == "Cleo")
    c = net.homes["c"]
    await c.groups.leave(app_id=APP, session_id=sid, actor_user_id=c.uid("cleo"))
    await settle(net)
    home = net.homes[giver[0]]
    sheet = await home.draw.result(
        app_id=APP, session_id=sid, actor_user_id=home.uid(giver[1])
    )
    assert (sheet["name"], sheet["household"]) == leaver_to
    assert sheet["handed_over_by"] == "Cleo"
    rows = [
        e for e in home.bus.of(AppDrawChanged) if e.to_user_id == home.uid(giver[1])
    ]
    assert rows[-1].change == "handover" and rows[-1].name == "Cleo"
    # The leaver's recipient updates their wish list: it reaches the new giver.
    to_inst, to_user = next(
        (i, u) for i, us in PEOPLE.items() for u in us if u.title() == leaver_to[0]
    )
    to_home = net.homes[to_inst]
    await to_home.draw.set_wish(
        app_id=APP, session_id=sid, actor_user_id=to_home.uid(to_user), text="new wish"
    )
    await settle(net)
    sheet = await home.draw.result(
        app_id=APP, session_id=sid, actor_user_id=home.uid(giver[1])
    )
    assert sheet["wish"] == "new wish"


async def test_someone_leaving_mid_round_restarts_without_them(net):
    sid = await draw_table(net, PEOPLE)
    hub, d = net.homes["hub"], net.homes["d"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    # d leaves before the round is through (its key is still on the way).
    await d.groups.leave(app_id=APP, session_id=sid, actor_user_id=d.uid("dev"))
    await settle(net)
    for home, u in _everyone(net):
        if (home.instance, u) == ("d", "dev"):
            continue
        res = await home.draw.result(
            app_id=APP, session_id=sid, actor_user_id=home.uid(u)
        )
        assert res["name"] != "Dev"


# ─── What every receiver checks ───────────────────────────────────────────


def test_wire_helpers_refuse_what_is_out_of_shape():
    for bad in (None, "", "!!!", "A" * 5000):
        with pytest.raises(DrawWireError):
            _raw(bad, 32)
    with pytest.raises(DrawWireError):
        _raw(b64url_encode(b"x" * 31), 32)
    with pytest.raises(DrawWireError):
        _raw_list([b64url_encode(b"x" * 32)] * 2, 2, 32, "S")  # repeats
    with pytest.raises(DrawWireError):
        _raw_list("nope", 1, 32, "S")
    for bad in (True, -1, 1000, "1", None):
        with pytest.raises(DrawWireError):
            _int(bad, 0, 999, "k")
    good = [
        {"seat": i, "instance": "h", "user": f"u{i}", "name": "N"} for i in (1, 2, 3)
    ]
    assert len(_parse_people(good)[0]) == 3
    for bad in (
        good[:2],
        [*good, {"seat": 3, "instance": "h", "user": "x"}],  # a seat twice
        [*good[:2], {"seat": 4, "instance": "h", "user": "u1"}],  # a person twice
        [*good[:2], "x"],
        [*good[:2], {"seat": 4, "instance": "h" * 80, "user": "u"}],
        [{"seat": i, "instance": "h", "user": f"u{i}"} for i in range(1, 32)],
    ):
        with pytest.raises(DrawWireError):
            _parse_people(bad)


@pytest.fixture
async def done(net):
    """A finished draw: the network, the table id and the hub's state."""
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    return net, sid, await hub.draw._load(sid)


async def test_check_list_refuses_every_tampering(done):
    _net, _sid, st = done
    rnd = _Round(st["hub"])
    body = st["hub"]["list_body"]
    S, commits = check_list(rnd, body)
    assert len(S) == rnd.n and set(commits) == set(rnd.order)

    def broken(fn):
        b = copy.deepcopy(body)
        fn(b)
        return b

    S0 = body["S"][0]
    for fn in (
        lambda b: b["S"].pop(),
        lambda b: b["S"].__setitem__(1, S0),
        lambda b: b["S"].__setitem__(0, b64url_encode(b"\x09" * 32)),
        lambda b: b["batches"].pop(),
        lambda b: b["batches"].__setitem__(0, b["batches"][1]),
        lambda b: b["batches"][0].__setitem__("instance", "zzz"),
        lambda b: b["batches"][0].__setitem__("sig", b64url_encode(b"\x00" * 64)),
        lambda b: (
            b["batches"][0]["commits"].reverse()
            or b["batches"][0]["commits"].append(["x", S0])
        ),
        lambda b: b["chain"].pop(),
        lambda b: b["chain"].reverse(),
        lambda b: b["chain"][-1].__setitem__("in", b["chain"][0]["in"]),
        lambda b: b["chain"][0].__setitem__("sig", b["chain"][-1]["sig"]),
        lambda b: b.__setitem__("batches", "x"),
        lambda b: b.__setitem__("chain", None),
    ):
        with pytest.raises((DrawWireError, dc.DrawCryptoError)):
            check_list(rnd, broken(fn))


async def test_complaint_holds_only_with_its_proof(done):
    net, sid, st = done
    hub_st = st["hub"]
    rnd = _Round(hub_st)
    b_st = await net.homes["b"].draw._load(sid)
    box = b_st["p"]["boxes"]["ben"]
    S = [b64url_decode(s) for s in hub_st["S"]]
    commits = {x["instance"]: x["commits"] for x in hub_st["list_body"]["batches"]}
    cards = [b64url_decode(c) for c in hub_st["all_cards"]]
    seed = net.homes["b"].fed.own_identity_seed

    def complaint(kind, *, user="ben", inst="b", priv=None, nonce=None, sign_seed=None):
        p = b64url_decode(priv or box["priv"])
        n = b64url_decode(nonce or box["nonce"])
        sig = sign_ed25519(
            sign_seed or seed, dc.complaint_message(rnd.ctx, inst, kind, user, p, n)
        )
        return {
            "kind": kind,
            "instance": inst,
            "user": user,
            "box_priv": b64url_encode(p),
            "nonce": b64url_encode(n),
            "sig": b64url_encode(sig),
        }

    kw = {"S": S, "commits_all": commits, "cards": cards}
    # Ben's key IS in S once and his card is fine: nothing holds.
    for kind in ("missing", "cards", "partner"):
        assert complaint_holds(rnd, complaint(kind), **kw) is False
    assert complaint_holds(rnd, complaint("bogus"), **kw) is False
    assert complaint_holds(rnd, complaint("missing", user="olaf"), **kw) is False
    assert complaint_holds(rnd, complaint("missing", inst="zzz"), **kw) is False
    other_seed = net.homes["c"].fed.own_identity_seed
    assert (
        complaint_holds(rnd, complaint("missing", sign_seed=other_seed), **kw) is False
    )
    fresh = b64url_encode(dc.new_keypair()[0])
    assert complaint_holds(rnd, complaint("missing", priv=fresh), **kw) is False
    # His key really missing from S: holds.
    gone = [s for s in S if s != dc.public_of(b64url_decode(box["priv"]))]
    gone.append(dc.new_keypair()[1])
    assert complaint_holds(
        rnd, complaint("missing"), S=gone, commits_all=commits, cards=cards
    )
    # His card gone from the cards: holds as "cards", not without the cards.
    mine = dc.try_cards(b64url_decode(box["priv"]), cards, ctx=rnd.ctx)
    assert len(mine) == 1
    no_card = [
        c
        for c in cards
        if dc.open_card(b64url_decode(box["priv"]), c, ctx=rnd.ctx) is None
    ]
    assert complaint_holds(
        rnd, complaint("cards"), S=S, commits_all=commits, cards=no_card
    )
    assert not complaint_holds(
        rnd, complaint("cards"), S=S, commits_all=commits, cards=None
    )
    assert not complaint_holds(rnd, {"kind": "missing"}, **kw)


async def test_a_mixer_mixes_once_per_round(done, caplog):
    net, sid, st = done
    caplog.set_level(logging.INFO)
    rnd = _Round(st["hub"])
    last = rnd.order[-1]
    home = net.homes[last]
    mixed = (await home.draw._load(sid))["p"]["mixed"]
    prev = st["hub"]["chain"][-2] if rnd.h > 1 else None
    assert prev is not None
    # The relay hands the last mixer a different list for the same turn.
    other = [
        b64url_encode(dc.new_keypair()[1] + b"\x00" * dc.SEAL_OVERHEAD)
        for _ in range(rnd.n)
    ]
    net.forge(
        "hub",
        last,
        {
            "app_id": APP,
            "session_id": sid,
            "verb": "draw_mix",
            "draw_suite": dc.DRAW_SUITE_X25519_AESGCM,
            "gen": rnd.gen,
            "round": rnd.round,
            "k": rnd.h - 1,
            "list": other,
            "prev": {
                "instance": prev["instance"],
                "in": prev["in"],
                "sig": prev["sig"],
            },
        },
    )
    await settle(net)
    assert "not the previous mixer's output" in caplog.text
    assert (await home.draw._load(sid))["p"]["mixed"] == mixed


async def test_a_draw_that_gives_up_and_a_fresh_one(done, monkeypatch):
    net, sid, st = done
    hub = net.homes["hub"]
    await hub.draw._hub_end(await hub.group_repo.get(sid), st, "rules")
    await settle(net)
    for home, u in _everyone(net):
        status = await home.draw.status(
            app_id=APP, session_id=sid, actor_user_id=home.uid(u)
        )
        assert (status["state"], status["reason"]) == ("failed", "rules")
    # The organiser tries again: a fresh draw (the next generation).
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    results = await _results(net, sid)
    _circle_ok(results)
    status = await hub.draw.status(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
    )
    assert status["state"] == "ready" and status["gen"] == 2


async def test_malformed_inbound_is_dropped(done, caplog):
    net, sid, _st = done
    caplog.set_level(logging.INFO)
    b = net.homes["b"]
    table = await b.group_repo.get(sid)
    base = {"app_id": APP, "session_id": sid, "draw_suite": dc.DRAW_SUITE_X25519_AESGCM}
    for payload in (
        {**base, "verb": "draw_start", "gen": 0, "round": 1},
        {**base, "verb": "draw_start", "gen": 1, "round": 13},
        {**base, "verb": "draw_mixers", "gen": 9, "round": 1},
        {**base, "verb": "draw_key", "gen": 1, "round": 1},  # b is no hub
        {**base, "verb": "draw_end", "gen": 1, "round": 1, "reason": "nope"},
        {
            **base,
            "verb": "draw_wish",
            "gen": 1,
            "round": 1,
            "relayed": True,
            "sealed": "x",
        },
    ):
        await b.draw.on_inbound(
            FederationEvent(
                msg_id="m",
                event_type=FederationEventType.APP_SESSION,
                from_instance="hub",
                to_instance="b",
                timestamp="2026-10-31T12:00:00+00:00",
                payload=payload,
            ),
            table,
        )
    assert caplog.text.count("dropped") >= 5
    status = await b.draw.status(app_id=APP, session_id=sid, actor_user_id=b.uid("ben"))
    assert status["state"] == "ready"


async def test_the_table_ending_ends_the_draw(net):
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await hub.groups.close(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    st = await hub.draw._load(sid)
    assert st["hub"]["phase"] == "ended"


async def test_after_the_reveal_too_few_left_ends_it(tmp_path):
    net = Network(tmp_path)
    try:
        sid = await draw_table(net, {"hub": ["anna"], "b": ["ben"], "c": ["cleo"]})
        hub = net.homes["hub"]
        await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
        await settle(net)
        c = net.homes["c"]
        await c.groups.leave(app_id=APP, session_id=sid, actor_user_id=c.uid("cleo"))
        await settle(net)
        status = await hub.draw.status(
            app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
        )
        assert (status["state"], status["reason"]) == ("failed", "too_few")
    finally:
        await net.close()


# ─── Hardening from the security review ───────────────────────────────────


async def test_a_redo_proof_must_be_about_the_cards_this_household_saw(done):
    """A relay can't drop or swap a card in a complaint's evidence to make
    it hold: each household compares the proof with its own copy."""
    net, sid, st = done
    b = net.homes["b"]
    st_b = copy.deepcopy(await b.draw._load(sid))
    st_b["last"]["complete"] = False
    hub = st["hub"]
    people = [
        dc.Person(seat=p["seat"], instance=p["instance"], user=p["user"])
        for p in hub["people"]
    ]
    table = await b.group_repo.get(sid)
    swapped = list(hub["all_cards"])
    swapped[0], swapped[1] = swapped[1], swapped[0]
    for evidence, why in (
        ({"cards": swapped}, "other cards than ours"),
        ({}, "other cards than ours"),
        (
            {"complaint": {"kind": "missing"}, "cards": hub["all_cards"]},
            "a missing key told after the cards went out",
        ),
    ):
        because = {
            "kind": "complaint",
            "complaint": {"kind": "cards"},
            "list": hub["list_body"],
            **evidence,
        }
        with pytest.raises(DrawWireError, match=why):
            b.draw._justify(table, st_b, hub["gen"], hub["round"] + 1, people, because)


async def test_a_missing_key_said_after_the_cards_is_refused(done, caplog):
    net, sid, st = done
    caplog.set_level(logging.INFO)
    hub = net.homes["hub"]
    st["hub"]["phase"] = "opening"
    await hub.draw._save(sid, st)
    box = (await net.homes["b"].draw._load(sid))["p"]["boxes"]["ben"]
    net.forge(
        "b",
        "hub",
        {
            "app_id": APP,
            "session_id": sid,
            "verb": "draw_fail",
            "draw_suite": dc.DRAW_SUITE_X25519_AESGCM,
            "gen": st["hub"]["gen"],
            "round": st["hub"]["round"],
            "complaint": {"kind": "missing", "instance": "b", "user": "ben", **box},
        },
    )
    await settle(net)
    assert "a missing key told too late" in caplog.text
    after = await hub.draw._load(sid)
    assert (after["hub"]["round"], after["hub"]["false"]) == (st["hub"]["round"], [])


async def test_a_hand_over_waits_until_the_leaver_shows_as_gone(net):
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    before = await _results(net, sid)
    where = {u.title(): (inst, u) for inst, users in PEOPLE.items() for u in users}
    # A giver on another household than the hub's and the leaver's.
    giver, leaver = next(
        (g, where[n])
        for g, (n, _h) in before.items()
        if g[0] != "hub" and where[n][0] not in (g[0], "hub")
    )
    home = net.homes[giver[0]]
    c = net.homes[leaver[0]]
    held = []

    def hold_rosters(sender, to, payload):
        # The giver's household hasn't heard Cleo left yet.
        if to == giver[0] and str(payload.get("verb", "")).startswith("group_"):
            held.append((sender, to, payload))
            return True
        return False

    await c.groups.leave(app_id=APP, session_id=sid, actor_user_id=c.uid(leaver[1]))
    await settle(net, drop=hold_rosters)
    sheet = await home.draw.result(
        app_id=APP, session_id=sid, actor_user_id=home.uid(giver[1])
    )
    assert sheet["name"] == leaver[1].title()  # not yet: still seated here
    assert (await home.draw._load(sid)).get("pending")
    net.queue.extend(held)
    await settle(net)
    sheet = await home.draw.result(
        app_id=APP, session_id=sid, actor_user_id=home.uid(giver[1])
    )
    assert (sheet["name"], sheet["household"]) == before[leaver]
    assert not (await home.draw._load(sid)).get("pending")


async def test_identity_hints_from_the_start_are_passed_on(done, monkeypatch):
    """A household not paired with another takes its key from the start
    message — only as a hint the group service accepts when it derives to
    that household's id."""
    net, sid, _st = done
    b = net.homes["b"]
    seen = {}

    async def fake(_self, instance, hint=None):
        seen[instance] = hint
        return b"\x01" * 32

    monkeypatch.setattr(type(b.groups), "identity_pk_of", fake)
    table = await b.group_repo.get(sid)
    out = await b.draw._identities(table, {"b", "c"}, hints={"c": "ab" * 32})
    assert seen == {"c": "ab" * 32}
    assert out == {"b": b.fed.own_identity_pk.hex(), "c": "01" * 32}
