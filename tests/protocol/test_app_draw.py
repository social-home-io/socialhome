"""Release-blocker protocol tests: the gift draw (v_78).

Marked ``@pytest.mark.security``. Over real households (real SQLite, the
real group and draw services; sealed ``APP_SESSION`` payloads carried by
:func:`tests.app_draw_network.settle`):

* **The host household learns nothing more than any participant.** What it
  stores and every payload it sends or receives, with the random bytes
  (keys, ciphertext, signatures) blanked out, is the same whoever draws
  whom; no wish list or result of anyone reaches its database or logs in
  plaintext.
* **A cheating household is caught.** A mixer that swaps an entry is
  exposed by the owner's proof and the round is redrawn; a duplicate or an
  oversize entry is refused; a complaint without proof is named to the
  organiser and changes nothing; a new round without a reason, a replayed
  round, an unknown suite, a stranger's or a non-hub's verb are dropped.
* **Exclusions hold** (a redo carries its proof), **an offline home** is
  waited for and can be drawn without, and **older households** never take
  part.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

import orjson
import pytest

from socialhome.crypto import b64url_decode, b64url_encode, sign_ed25519
from socialhome.domain.app_draw import (
    DRAW_WAIT_SECONDS,
    AppDrawHouseholdTooOldError,
)
from socialhome.federation import draw_crypto as dc
from tests.app_draw_network import APP, add_draw, draw_table, settle
from tests.app_group_network import Network, quiz_app

pytestmark = pytest.mark.security

PEOPLE = {
    "hub": ["anna"],
    "b": ["ben", "bea"],
    "c": ["cleo"],
    "d": ["dev"],
}
WISH = {u: f"WISH-{u.upper()}-7f3a" for users in PEOPLE.values() for u in users}


@pytest.fixture
async def net(tmp_path, monkeypatch):
    # One table id for every run: the mixing order depends on it, so two
    # runs differ only in what the households drew at random.
    monkeypatch.setattr(
        "socialhome.services.app_group_service.uuid4", lambda: UUID(int=7)
    )
    n = Network(tmp_path)
    yield n
    await n.close()


async def _draw(net, *, pairs=None) -> str:
    sid = await draw_table(net, PEOPLE)
    for inst, users in PEOPLE.items():
        home = net.homes[inst]
        for u in users:
            await home.draw.set_wish(
                app_id=APP, session_id=sid, actor_user_id=home.uid(u), text=WISH[u]
            )
    hub = net.homes["hub"]
    if pairs:
        await hub.draw.configure(
            app_id=APP,
            session_id=sid,
            actor_user_id=hub.uid("anna"),
            settings={"pairs": pairs},
        )
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    return sid


async def _results(net, sid) -> dict[str, str]:
    out = {}
    for inst, users in PEOPLE.items():
        home = net.homes[inst]
        for u in users:
            res = await home.draw.result(
                app_id=APP, session_id=sid, actor_user_id=home.uid(u)
            )
            out[u] = res["name"].lower()
    return out


def _assert_circle(results: dict[str, str]) -> None:
    assert sorted(results.values()) == sorted(results)
    assert all(g != r for g, r in results.items())
    seen, cur = set(), next(iter(results))
    while cur not in seen:
        seen.add(cur)
        cur = results[cur]
    assert seen == set(results)


_BLOB = re.compile(r"[A-Za-z0-9_-]{16,}")


def _shape(value):
    """*value* with every random-looking string (keys, ciphertext,
    signatures, digests) replaced by its length."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(v) for v in value]
    if isinstance(value, str) and _BLOB.fullmatch(value):
        return f"<{len(value)}>"
    return value


def _diff(a, b, path="") -> list[str]:
    """Where *a* and *b* differ (paths), for a readable failure."""
    if type(a) is not type(b):
        return [path]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            out += _diff(a.get(k), b.get(k), f"{path}.{k}")
        return out
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return [f"{path}[len]"]
        return [
            d
            for i, (x, y) in enumerate(zip(a, b, strict=True))
            for d in _diff(x, y, f"{path}[{i}]")
        ]
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def _hub_view(net) -> list:
    """Every draw payload the hub sent or received, in order."""
    return [
        (s, t, _shape(p))
        for s, t, p in net.sent
        if "hub" in (s, t) and str(p.get("verb", "")).startswith("draw_")
    ]


async def _hub_state(net, sid) -> dict:
    return await net.homes["hub"].draw._load(sid)


# ─── What the host household learns ───────────────────────────────────────


async def test_the_host_view_is_the_same_whoever_draws_whom(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "socialhome.services.app_group_service.uuid4", lambda: UUID(int=7)
    )
    views, states, assignments = [], [], []
    for run, c_seed in enumerate((1, 2, 3, 4)):
        net = Network(tmp_path / f"run{run}")
        try:
            sid = await draw_table(net, PEOPLE)
            # Every household draws the same randomness each run but one
            # mixer (c) — the assignment changes, the host's view mustn't.
            add_draw(net.homes["c"], seed=c_seed)
            hub = net.homes["hub"]
            await hub.draw.start(
                app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
            )
            await settle(net)
            assignments.append(await _results(net, sid))
            views.append(_hub_view(net))
            state = await _hub_state(net, sid)
            states.append(_shape(state))
            # The host holds its own person's card only.
            own = state["final"]["results"]
            assert set(own) == {"anna"}
            assert set(state["p"]["boxes"]) == {"anna"}
        finally:
            await net.close()
    assert len({tuple(sorted(a.items())) for a in assignments}) > 1, "runs drew alike"
    for a in assignments:
        _assert_circle(a)
    # Identical but for the host's own person's result (its position).
    for v in views[1:]:
        assert _diff(views[0], v) == []
    for s in states[1:]:
        assert [
            d
            for d in _diff(states[0], s)
            if ".anna.j" not in d and ".hub.since" not in d
        ] == []


async def test_no_wish_list_or_result_reaches_the_hosts_disk_or_logs(net, caplog):
    caplog.set_level(logging.DEBUG)
    sid = await _draw(net)
    results = await _results(net, sid)
    _assert_circle(results)
    hub = net.homes["hub"]
    # The database and its write-ahead log, as they lie on disk.
    blob = b"".join(
        p.read_bytes()
        for p in (net.root / "hub").iterdir()
        if p.name.startswith("h.db")
    )
    assert blob
    for marker in WISH.values():
        assert marker.encode() not in blob
    row = await hub.db.fetchone(
        "SELECT draw_json FROM app_group_sessions WHERE session_id = ?", (sid,)
    )
    assert set(orjson.loads(row["draw_json"])) == {"v", "enc"}
    for marker in WISH.values():
        assert marker not in caplog.text
    # The app frames carry the state, never a name drawn.
    frames = repr([f for _u, f in hub.ws.frames if f.get("type") == "app.draw"])
    for u in PEOPLE["b"] + PEOPLE["c"] + PEOPLE["d"]:
        assert WISH[u] not in frames


async def test_a_card_opens_only_for_its_giver(net):
    sid = await _draw(net)
    hub_state = await _hub_state(net, sid)
    sealed = [b64url_decode(c) for c in hub_state["p"]["cards"]]
    assert len(sealed) == 5 and len({len(c) for c in sealed}) == 1
    ctx = b64url_decode(hub_state["p"]["ctx"])
    box = b64url_decode(hub_state["p"]["boxes"]["anna"]["priv"])
    assert len(dc.try_cards(box, sealed, ctx=ctx)) == 1
    # The host's own key is no key to anyone else's card.
    other = dc.new_keypair()[0]
    assert dc.try_cards(other, sealed, ctx=ctx) == []


# ─── A cheating household is caught ───────────────────────────────────────


async def test_a_mixer_that_swaps_an_entry_is_exposed_and_the_round_redrawn(
    net, monkeypatch
):
    real_mix = dc.mix
    cheated = []

    def cheating_mix(priv, entries, *, ctx, k, h, rand=os.urandom):
        out = real_mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)
        if k == h - 1 and not cheated:
            cheated.append(out[0])
            out[0] = dc.new_keypair()[1]  # a box key of the mixer's own making
        return out

    monkeypatch.setattr(dc, "mix", cheating_mix)
    sid = await _draw(net)
    assert cheated
    fails = [p for _s, _t, p in net.sent if p.get("verb") == "draw_fail"]
    assert fails and fails[0]["complaint"]["kind"] == "missing"
    starts = [p for _s, _t, p in net.sent if p.get("verb") == "draw_start"]
    assert {p["round"] for p in starts} == {1, 2}
    assert all(p["because"]["kind"] == "complaint" for p in starts if p["round"] == 2)
    _assert_circle(await _results(net, sid))


async def test_a_duplicated_entry_is_refused(net, monkeypatch):
    real_mix = dc.mix

    def duplicating_mix(priv, entries, *, ctx, k, h, rand=os.urandom):
        out = real_mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)
        out[1] = out[0]
        return out

    monkeypatch.setattr(dc, "mix", duplicating_mix)
    sid = await _draw(net)
    assert not [p for _s, _t, p in net.sent if p.get("verb") == "draw_list"]
    hub = net.homes["hub"]
    status = await hub.draw.status(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
    )
    assert status["state"] == "drawing" and status["has_result"] is False


async def test_a_dropped_onion_on_the_way_stalls_rather_than_leaks(net):
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))

    def drop(sender, to, payload):
        # The relay passes the first mixer a list with one onion missing.
        if payload.get("verb") == "draw_mix" and payload.get("k") == 0:
            payload["batches"][0]["onions"] = payload["batches"][0]["onions"][:-1]
        return False

    await settle(net, drop=drop)
    assert not [p for _s, _t, p in net.sent if p.get("verb") == "draw_mixed"]


async def test_wrong_suite_strangers_replays_and_non_hub_verbs_are_dropped(net, caplog):
    caplog.set_level(logging.INFO)
    sid = await draw_table(net, PEOPLE)
    stranger = await net.household("e")
    await stranger.add_user("eve")
    add_draw(stranger)
    await net.pair(net.homes["hub"], stranger)
    await net.pair(net.homes["c"], stranger)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    held: list[tuple[str, str, dict]] = []

    def hold(sender, to, payload):
        # Hold back b's key and the hub's start for c.
        verb = payload.get("verb")
        if (sender, verb) == ("b", "draw_key") or (to, verb) == ("c", "draw_start"):
            held.append((sender, to, payload))
            return True
        return False

    await settle(net, drop=hold)
    key_from_b = next(p for s, _t, p in held if s == "b")
    start_to_c = next(p for _s, t, p in held if t == "c")
    net.forge("e", "hub", dict(key_from_b))  # a stranger's (copied) key
    net.forge("b", "hub", {**key_from_b, "draw_suite": "x25519"})  # unknown suite
    net.forge("b", "hub", {**key_from_b, "round": 2})  # another round
    net.forge("e", "c", dict(start_to_c))  # a stranger posing as the hub
    net.forge("b", "c", dict(start_to_c))  # a participant posing as the hub
    await settle(net)
    st = await _hub_state(net, sid)
    assert "b" not in st["hub"]["keys"] and "e" not in st["hub"]["keys"]
    assert "p" not in await net.homes["c"].draw._load(sid)  # c took no start
    assert "not a participant household" in caplog.text
    assert "not from the hub" in caplog.text
    assert "draw_suite 'x25519'" in caplog.text
    # The real messages still run the draw to its end.
    for item in held:
        net.queue.append(item)
    await settle(net)
    _assert_circle(await _results(net, sid))


async def test_an_oversize_onion_is_refused(net):
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))

    def grow(sender, to, payload):
        if payload.get("verb") == "draw_onions" and sender == "c":
            payload["onions"] = [o + "AAAA" for o in payload["onions"]]
        return False

    await settle(net, drop=grow)
    st = await _hub_state(net, sid)
    assert "c" not in st["hub"]["batches"]


async def test_a_complaint_without_proof_is_named_and_changes_nothing(net):
    sid = await _draw(net)
    before = await _results(net, sid)
    st_b = await net.homes["b"].draw._load(sid)
    box = st_b["p"]["boxes"]["ben"]
    hub_state = await _hub_state(net, sid)
    rid = {"gen": hub_state["hub"]["gen"], "round": hub_state["hub"]["round"]}
    # Ben's household claims he drew a partner (there are no pairs) —
    # a real box key, a real signature, a false claim.
    ctx = b64url_decode(st_b["p"]["ctx"])
    sig = sign_ed25519(
        net.homes["b"].fed.own_identity_seed,
        dc.complaint_message(
            ctx,
            "b",
            "partner",
            "ben",
            b64url_decode(box["priv"]),
            b64url_decode(box["nonce"]),
        ),
    )
    # Back to the opening phase so the hub would act on a valid one.
    hub_state["hub"]["phase"] = "opening"
    await net.homes["hub"].draw._save(sid, hub_state)
    net.forge(
        "b",
        "hub",
        {
            "app_id": APP,
            "session_id": sid,
            "verb": "draw_redo",
            "draw_suite": dc.DRAW_SUITE_X25519_AESGCM,
            **rid,
            "complaint": {
                "kind": "partner",
                "instance": "b",
                "user": "ben",
                "box_priv": box["priv"],
                "nonce": box["nonce"],
                "sig": b64url_encode(sig),
            },
        },
    )
    await settle(net)
    st = await _hub_state(net, sid)
    assert st["hub"]["false"] == ["b"]
    assert st["hub"]["round"] == rid["round"]
    status = await net.homes["hub"].draw.status(
        app_id=APP, session_id=sid, actor_user_id=net.homes["hub"].uid("anna")
    )
    assert status["false_complaints"] == [{"seats": [2, 3]}]
    assert await _results(net, sid) == before


async def test_a_new_round_without_its_reason_is_refused(net):
    """The organiser's household can't re-roll on demand: a start for the
    next round that says "changed" with the same people, "first" again, or
    a complaint that doesn't hold is refused by every participant."""
    sid = await _draw(net)
    hub_state = await _hub_state(net, sid)
    hub = hub_state["hub"]
    people = hub["people"]
    for because in (
        {"kind": "changed"},
        {"kind": "first"},
        {"kind": "complaint", "complaint": {"kind": "missing"}, "list": {}},
        {"kind": "whatever"},
    ):
        for gen, rnd in ((hub["gen"], hub["round"] + 1), (1, 1)):
            net.forge(
                "hub",
                "b",
                {
                    "app_id": APP,
                    "session_id": sid,
                    "verb": "draw_start",
                    "draw_suite": dc.DRAW_SUITE_X25519_AESGCM,
                    "gen": gen,
                    "round": rnd,
                    "people": people,
                    "identities": hub["identities"],
                    "settings": hub["settings"],
                    "because": because,
                },
            )
    await settle(net)
    st_b = await net.homes["b"].draw._load(sid)
    assert (st_b["p"]["gen"], st_b["p"]["round"]) == (hub["gen"], hub["round"])
    assert st_b["last"]["complete"] is True


# ─── Exclusions, offline homes, older homes ───────────────────────────────


async def test_pairs_redo_with_proof_until_the_circle_fits(tmp_path, monkeypatch):
    """ben(2)–bea(3) may not draw each other: with five people about half
    of the rounds hit the pair and are redrawn on the giver's proof. Over
    a few draws the redo path really runs (not just lucky first rounds),
    and every result keeps the pair apart."""
    monkeypatch.setattr(
        "socialhome.services.app_group_service.uuid4", lambda: UUID(int=7)
    )
    total_redos = 0
    for shift in range(6):
        net = Network(tmp_path / f"s{shift}")
        try:
            sid = await draw_table(net, PEOPLE)
            for i, home in enumerate(net.homes.values()):
                add_draw(home, seed=100 * shift + i)
            hub = net.homes["hub"]
            await hub.draw.configure(
                app_id=APP,
                session_id=sid,
                actor_user_id=hub.uid("anna"),
                settings={"pairs": [[2, 3]]},
            )
            await hub.draw.start(
                app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
            )
            await settle(net)
            results = await _results(net, sid)
            _assert_circle(results)
            assert results["ben"] != "bea" and results["bea"] != "ben"
            redos = [p for _s, _t, p in net.sent if p.get("verb") == "draw_redo"]
            for p in redos:
                assert p["complaint"]["kind"] == "partner"
                assert p["complaint"]["instance"] == "b"
            rounds = {
                p["round"] for _s, _t, p in net.sent if p.get("verb") == "draw_start"
            }
            assert len(rounds) == len(redos) + 1
            total_redos += len(redos)
        finally:
            await net.close()
    assert total_redos > 0


async def test_an_offline_home_is_waited_for_then_drawn_without(net, monkeypatch):
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net, drop=lambda s, t, p: "d" in (s, t))
    status = await hub.draw.status(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
    )
    assert status["state"] == "drawing" and status["waiting_for"] is None
    later = datetime.now(UTC) + timedelta(seconds=DRAW_WAIT_SECONDS + 5)
    monkeypatch.setattr("socialhome.services.app_draw_service._now", lambda: later)
    status = await hub.draw.status(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
    )
    assert status["waiting_for"] == [{"seats": [5]}]
    # "Draw without them": the organiser takes Dev's seat out.
    await hub.groups.remove(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), seat=5
    )
    await settle(net, drop=lambda s, t, p: "d" in (s, t))
    results = {}
    for inst in ("hub", "b", "c"):
        home = net.homes[inst]
        for u in PEOPLE[inst]:
            res = await home.draw.result(
                app_id=APP, session_id=sid, actor_user_id=home.uid(u)
            )
            results[u] = res["name"].lower()
    _assert_circle(results)
    assert "dev" not in results.values()


async def test_a_household_below_v78_never_takes_part(tmp_path):
    net = Network(tmp_path)
    try:
        sid = await draw_table(net, {"hub": ["anna"], "b": ["ben"], "c": ["cleo"]})
        hub = net.homes["hub"]
        hub.fed.versions["c"] = 77
        with pytest.raises(AppDrawHouseholdTooOldError):
            await hub.draw.start(
                app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
            )
        # And a draw verb from it is dropped.
        net.homes["b"].fed.versions["c"] = 77
        net.forge(
            "c",
            "b",
            {
                "app_id": APP,
                "session_id": sid,
                "verb": "draw_start",
                "draw_suite": dc.DRAW_SUITE_X25519_AESGCM,
                "gen": 1,
                "round": 1,
            },
        )
        await settle(net)
        assert "p" not in await net.homes["b"].draw._load(sid)
    finally:
        await net.close()


async def test_every_draw_verb_rides_the_sealed_app_session(net):
    await _draw(net)
    draw = [p for _s, _t, p in net.sent if str(p.get("verb", "")).startswith("draw_")]
    assert draw
    for p in draw:
        assert net.types[id(p)].value == "app_session"
        assert p["draw_suite"] == dc.DRAW_SUITE_X25519_AESGCM
        assert isinstance(p["gen"], int) and isinstance(p["round"], int)


def test_quiz_app_takes_thirty():
    assert quiz_app(players=(3, 30)).manifest.players_max == 30
