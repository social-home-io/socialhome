"""AppDrawService — the gift draw end to end over real households (v_78).

Every household runs the real group and draw services on its own SQLite
database; :func:`tests.app_draw_network.settle` carries the sealed
``APP_SESSION`` payloads between them. Tamper and privacy checks live in
``tests/protocol/test_app_engine_draw.py``.
"""

from __future__ import annotations

import copy
import logging
import os

import pytest

from socialhome.app_engines.draw import crypto as dc
from socialhome.app_engines.draw.domain import (
    AppDrawHostElsewhereError,
    AppDrawNoResultError,
    AppDrawNotHostError,
    AppDrawPairsError,
    AppDrawSettingsError,
    AppDrawStateError,
    AppDrawTooFewError,
    AppDrawWishTooLongError,
    AppDrawWishUpdatesError,
    DrawWireError,
)
from socialhome.app_engines.draw.service import (
    AppDrawService,
    _int,
    _parse_people,
    _raw,
    _raw_list,
    _Round,
    check_list,
    complaint_holds,
    complaint_verdict,
)
from socialhome.crypto import b64url_decode, b64url_encode, sign_ed25519
from socialhome.domain.events import AppDrawChanged
from socialhome.domain.federation import FederationEvent, FederationEventType
from tests.app_draw_network import APP, draw_table, engine_step, sent_steps, settle
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
        frames = [
            f
            for _u, f in home.ws.frames
            if f.get("type") == "app.engine" and f.get("engine") == "draw"
        ]
        assert frames
        blob = repr(frames)
        for name, household in results.values():
            assert name not in blob and household not in blob
        for f in frames:
            assert set(f["state"]) >= {"state", "has_result"}
            assert "name" not in f["state"] and "recipient" not in f["state"]


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
    assert rows[-1].change == "handover"
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
    S, commits, links, _tb, _tc = check_list(rnd, body)
    assert len(S) == rnd.n and set(commits) == set(rnd.order)
    assert len(links) == rnd.h and links[-1] == [dc.entry_hash(s) for s in S]

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
        lambda b: b["chain"][0]["hashes"].reverse(),
        lambda b: b["chain"][0]["hashes"].__setitem__(0, b["chain"][-1]["hashes"][0]),
        lambda b: b["chain"][-1]["hashes"].pop(),
        lambda b: b["chain"][0].__setitem__("sig", b["chain"][-1]["sig"]),
        lambda b: b["chain"][0].__setitem__("instance", b["chain"][-1]["instance"]),
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
    seed_ben = b64url_decode(b_st["p"]["seeds"]["ben"])
    ben = dc.person_keys(seed_ben)
    S = [b64url_decode(s) for s in hub_st["S"]]
    batches = {x["instance"]: x for x in hub_st["list_body"]["batches"]}
    mixer_pubs = [b64url_decode(x) for x in hub_st["mixer_pubs"]]
    links = [[b64url_decode(x) for x in link["hashes"]] for link in hub_st["chain"]]
    cards = [b64url_decode(c) for c in hub_st["all_cards"]]
    id_seed = net.homes["b"].fed.own_identity_seed

    def complaint(kind, *, user="ben", inst="b", seed=seed_ben, sign_seed=None):
        sig = sign_ed25519(
            sign_seed or id_seed,
            dc.complaint_message(
                rnd.t("chain" if kind == "missing" else "reveals"),
                inst,
                kind,
                user,
                seed,
            ),
        )
        return {
            "kind": kind,
            "instance": inst,
            "user": user,
            "seed": b64url_encode(seed),
            "sig": b64url_encode(sig),
        }

    kw = {
        "S": S,
        "batches": batches,
        "links": links,
        "mixer_pubs": mixer_pubs,
        "cards": cards,
    }
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
    # A seed the household never committed to proves nothing.
    assert complaint_holds(rnd, complaint("missing", seed=b"\x07" * 32), **kw) is False
    # His key really missing from S — the last mixer's signed output has
    # another in its place: holds, and names that mixer (unless it is
    # Ben's own household's: then it proves nothing against anyone).
    gone = [s for s in S if s != ben.box_pub] + [dc.new_keypair()[1]]
    gone_links = links[:-1] + [[dc.entry_hash(s) for s in gone]]
    verdict = complaint_verdict(
        rnd, complaint("missing"), **{**kw, "S": gone, "links": gone_links}
    )
    last = rnd.order[-1]
    assert verdict == (None if last == "b" else [last])
    # S without him but a chain that still carries him: proves nothing.
    assert not complaint_holds(rnd, complaint("missing"), **{**kw, "S": gone})
    # His card gone from the cards: holds as "cards", not without cards.
    no_card = [c for c in cards if dc.open_card(ben.box_priv, c, ctx=rnd.ctx) is None]
    assert complaint_holds(rnd, complaint("cards"), **{**kw, "cards": no_card})
    assert not complaint_holds(rnd, complaint("cards"), **{**kw, "cards": None})
    assert not complaint_holds(rnd, {"kind": "missing"}, **kw)


async def test_a_mixer_mixes_once_per_round(done, caplog):
    net, sid, st = done
    caplog.set_level(logging.INFO)
    rnd = _Round(st["hub"])
    last = rnd.order[-1]
    home = net.homes[last]
    mixed = (await home.draw._load(sid))["p"]["mixed"]
    prev = st["hub"]["chain"][-2]
    before = (
        st["hub"]["chain"][-3]["hashes"]
        if rnd.h > 2
        else [h for b in st["hub"]["list_body"]["batches"] for h in b["hashes"]]
    )
    prev_in = b64url_encode(dc.list_digest([b64url_decode(x) for x in before]))
    # The relay hands the last mixer a different list for the same turn.
    other = [
        b64url_encode(dc.new_keypair()[1] + b"\x00" * dc.SEAL_OVERHEAD)
        for _ in range(rnd.n)
    ]
    net.forge(
        "hub",
        last,
        engine_step(
            sid,
            "mix",
            gen=rnd.gen,
            round_no=rnd.round,
            k=rnd.h - 1,
            list=other,
            prev={"instance": prev["instance"], "in": prev_in, "sig": prev["sig"]},
            batches=st["hub"]["batches_pub"],
        ),
    )
    await settle(net)
    assert "not the previous mixer's output" in caplog.text
    assert (await home.draw._load(sid))["p"]["mixed"] == mixed


async def test_a_draw_that_gives_up_and_a_fresh_one(net, monkeypatch):
    """The last round allowed breaks (a mixer drops an entry): the hub gives
    up with the proof, every household checks it and takes the end — and
    names the mixer — then the organiser draws afresh (the next draw)."""
    monkeypatch.setattr("socialhome.app_engines.draw.service.DRAW_ROUNDS_MAX", 1)
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    real_mix = dc.mix
    real_make = AppDrawService._make_onions
    boxes: dict = {}
    cheated = []

    def make(self, rnd, pubs):
        onions, seeds, commits = real_make(self, rnd, pubs)
        boxes["order"] = rnd.order
        boxes[self._own] = {
            dc.person_keys(b64url_decode(x)).box_pub for x in seeds.values()
        }
        return onions, seeds, commits

    def dropping_mix(priv, entries, *, ctx, k, h, rand=os.urandom):
        out = real_mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)
        if k == h - 1 and not cheated:
            # Another household's person (not the mixer's own, not the
            # hub's — whose steps stay local): a proof that names the mixer.
            victim = next(
                i for i in boxes["order"] if i not in ("hub", boxes["order"][-1])
            )
            j = next(j for j, o in enumerate(out) if o in boxes[victim])
            cheated.append(victim)
            out[j] = dc.new_keypair()[1]
        return out

    monkeypatch.setattr(AppDrawService, "_make_onions", make)
    monkeypatch.setattr(dc, "mix", dropping_mix)
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    st = await hub.draw._load(sid)
    fails = sent_steps(net, "fail")
    assert [f["complaint"]["instance"] for f in fails[:1]] == cheated
    ends = sent_steps(net, "end")
    assert ends and all(e["because"]["kind"] == "complaint" for e in ends)
    culprit = st["hub"]["order"][-1]
    for home, u in _everyone(net):
        status = await home.draw.status(
            app_id=APP, session_id=sid, actor_user_id=home.uid(u)
        )
        assert (status["state"], status["reason"]) == ("failed", "rules")
        seats = [p["seat"] for p in st["hub"]["people"] if p["instance"] == culprit]
        assert status["blamed"] == [{"seats": seats}]
    # The organiser tries again: a fresh draw (the next generation).
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    results = await _results(net, sid)
    _circle_ok(results)
    status = await hub.draw.status(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
    )
    assert status["state"] == "ready" and status["gen"] == 2


async def test_an_end_after_the_reveal_is_refused(done, caplog):
    """HIGH 1 (second review): once the names are revealed no household
    takes an ``end`` (so no "first" draw can follow it)."""
    net, sid, st = done
    caplog.set_level(logging.INFO)
    hub = net.homes["hub"]
    await hub.draw._hub_end(await hub.group_repo.get(sid), st, "rules")
    await settle(net)
    assert "an end after the reveal" in caplog.text
    for home, u in _everyone(net):
        if home is hub:
            continue
        status = await home.draw.status(
            app_id=APP, session_id=sid, actor_user_id=home.uid(u)
        )
        assert (status["state"], status["reason"]) == ("ready", None)


async def test_malformed_inbound_is_dropped(done, caplog):
    net, sid, _st = done
    caplog.set_level(logging.INFO)
    b = net.homes["b"]
    table = await b.group_repo.get(sid)
    for payload in (
        engine_step(sid, "start", gen=0),
        engine_step(sid, "start", round_no=13),
        engine_step(sid, "mixers", gen=9),
        engine_step(sid, "key"),  # b is no hub
        engine_step(sid, "end", reason="nope"),
        engine_step(sid, "wish", relayed=True, sealed="x"),
        {**engine_step(sid, "start"), "engine_suite": "x25519"},
        {**engine_step(sid, "start"), "engine": "deal"},
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
    assert caplog.text.count("dropped") >= 8
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


async def test_after_the_reveal_too_few_left_keeps_the_names(tmp_path):
    """After the reveal a leave changes nothing but the leaver's giver: the
    others keep their names (no "end" after the reveal), and Draw again
    needs three people seated."""
    net = Network(tmp_path)
    try:
        sid = await draw_table(net, {"hub": ["anna"], "b": ["ben"], "c": ["cleo"]})
        hub = net.homes["hub"]
        await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
        await settle(net)
        c = net.homes["c"]
        await c.groups.leave(app_id=APP, session_id=sid, actor_user_id=c.uid("cleo"))
        await settle(net)
        assert not sent_steps(net, "end")
        status = await hub.draw.status(
            app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
        )
        assert (status["state"], status["has_result"]) == ("ready", True)
        with pytest.raises(AppDrawTooFewError):
            await hub.draw.start(
                app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), again=True
            )
    finally:
        await net.close()


# ─── Hardening from the security review ───────────────────────────────────


async def test_a_complaint_is_checked_on_the_households_own_copy(done):
    """A restart's proof is the complaint alone: each household checks it
    on its own copy of the round (the list, the mixer keys, the committed
    cards) — the hub can't hand it doctored evidence; without its own copy
    a household doesn't take the round; a missing key after the cards is
    refused."""
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
    complaint = {"kind": "complaint", "complaint": {"kind": "partner"}}
    with pytest.raises(DrawWireError, match="the complaint doesn't hold"):
        await b.draw._justify(
            table, st_b, hub["gen"], hub["round"] + 1, people, complaint
        )
    with pytest.raises(DrawWireError, match="a missing key told after the cards"):
        await b.draw._justify(
            table,
            st_b,
            hub["gen"],
            hub["round"] + 1,
            people,
            {"kind": "complaint", "complaint": {"kind": "missing"}},
        )
    no_list = copy.deepcopy(st_b)
    no_list["p"].pop("S")
    with pytest.raises(DrawWireError, match="no list of ours"):
        await b.draw._justify(
            table, no_list, hub["gen"], hub["round"] + 1, people, complaint
        )


async def test_a_missing_key_said_after_the_cards_is_refused(done, caplog):
    net, sid, st = done
    caplog.set_level(logging.INFO)
    hub = net.homes["hub"]
    st["hub"]["phase"] = "opening"
    await hub.draw._save(sid, st)
    seed = (await net.homes["b"].draw._load(sid))["p"]["seeds"]["ben"]
    net.forge(
        "b",
        "hub",
        engine_step(
            sid,
            "fail",
            gen=st["hub"]["gen"],
            round_no=st["hub"]["round"],
            complaint={
                "kind": "missing",
                "instance": "b",
                "user": "ben",
                "seed": seed,
                "sig": "x",
            },
        ),
    )
    await settle(net)
    assert "a missing key told too late" in caplog.text
    after = await hub.draw._load(sid)
    assert (after["hub"]["round"], after["hub"]["blamed"]) == (st["hub"]["round"], [])


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
    assert (await home.draw._load(sid))["final"]["handovers"]
    net.queue.extend(held)
    await settle(net)
    sheet = await home.draw.result(
        app_id=APP, session_id=sid, actor_user_id=home.uid(giver[1])
    )
    assert (sheet["name"], sheet["household"]) == before[leaver]
    assert not (await home.draw._load(sid))["final"]["handovers"]


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


async def test_configure_is_a_patch(net):
    """Only the fields given change: adding a pair keeps the name, budget
    and day (socialhome-apps#32's real run); ``null`` clears one."""
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    ids = {"app_id": APP, "session_id": sid, "actor_user_id": hub.uid("anna")}
    await hub.draw.configure(
        **ids,
        settings={"name": "Familie Müller", "budget": "20 €", "day": "2026-12-24"},
    )
    view = await hub.draw.configure(**ids, settings={"pairs": [[1, 2]]})
    assert view["settings"] == {
        "name": "Familie Müller",
        "budget": "20 €",
        "day": "2026-12-24",
        "pairs": [[1, 2]],
    }
    view = await hub.draw.configure(**ids, settings={"day": None, "pairs": None})
    assert (view["settings"]["day"], view["settings"]["pairs"]) == (None, [])
    assert view["settings"]["name"] == "Familie Müller"
    with pytest.raises(AppDrawSettingsError):
        await hub.draw.configure(**ids, settings={"day": "soon"})
    assert (await hub.draw.status(**ids))["settings"]["name"] == "Familie Müller"


# ─── Late joins until names are drawn ─────────────────────────────────────


async def test_late_joiners_sit_down_until_names_are_drawn(net):
    """An app with late joiners takes people until Draw names: after a
    Start a late joiner still sits down and is drawn; Draw names ends
    late joins — an unanswered invite is withdrawn and nobody new sits."""
    people = {"hub": ["anna"], "b": ["ben"], "c": ["cleo"], "d": ["dev", "dora"]}
    sid = await draw_table(
        net, people, late_join=True, waiting=(("d", "dev"), ("d", "dora"))
    )
    hub, d = net.homes["hub"], net.homes["d"]
    anna = hub.uid("anna")
    await hub.groups.start(app_id=APP, session_id=sid, actor_user_id=anna)
    await settle(net)
    await d.groups.join(app_id=APP, session_id=sid, actor_user_id=d.uid("dev"))
    await settle(net)
    table = await hub.group_repo.get(sid)
    assert table.state == "playing" and table.entry("d", "dev").state == "seated"
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=anna)
    await settle(net)
    table = await hub.group_repo.get(sid)
    assert table.late_join is False
    assert table.entry("d", "dora").state == "declined"
    # The households' copies hear it: no free seats offered any more.
    assert (await d.group_repo.get(sid)).late_join is False
    assert await d.app_repo.get_pending_session(APP, d.uid("dora"), sid) is None
    # Dev was drawn with the rest.
    res = await d.draw.result(app_id=APP, session_id=sid, actor_user_id=d.uid("dev"))
    assert res["name"] in {"Anna", "Ben", "Cleo"}


async def test_a_refused_draw_leaves_late_joins_open(net):
    sid = await draw_table(net, PEOPLE, late_join=True, waiting=(("d", "dev"),))
    hub = net.homes["hub"]
    ids = {"app_id": APP, "session_id": sid, "actor_user_id": hub.uid("anna")}
    await hub.draw.configure(**ids, settings={"pairs": [[1, 2], [3, 4]]})
    with pytest.raises(AppDrawPairsError):
        await hub.draw.start(**ids)
    table = await hub.group_repo.get(sid)
    assert table.late_join is True and table.entry("d", "dev").state == "invited"


async def test_without_late_joiners_draw_names_changes_nothing_for_joins(net):
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    table = await hub.group_repo.get(sid)
    assert table.state == "playing" and table.late_join is False
