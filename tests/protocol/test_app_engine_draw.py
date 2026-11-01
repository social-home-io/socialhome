"""Release-blocker protocol tests: the gift draw engine (v_78).

Marked ``@pytest.mark.security``. Over real households (real SQLite, the
real group service, the real app engine registry and draw engine; sealed
``APP_SESSION`` payloads carried by :func:`tests.app_draw_network.settle`):

* **The host household learns nothing more than any participant.** What it
  stores and every payload it sends or receives, with the random bytes
  blanked out, is the same whoever draws whom; no wish list or result of
  anyone reaches its database or logs in plaintext.
* **The first independent review's attacks** (``draw-review-1``, one test
  each, named after its PoC): the last mixer choosing the circle (H1); the
  hub peeking then re-rolling with a pre-armed "missing" proof or a
  doctored card list (H2); a re-roll by leaving after a peek, or a
  "changed" round while someone is still seated (H3); a hand-over pointing
  a giver at any card (H4); a low-order box key (M1); an invisible extra
  participant (M2); an early "complete" (L4); a framing card list (L5);
  repeated card commitments (L6); an engine an app never declared.
* **A cheating household is caught**: a swapping mixer is exposed by the
  owner's proof and the round redrawn; duplicates and oversize entries are
  refused; a complaint without proof is named and changes nothing; a new
  round without a reason, a replayed round, an unknown suite, a stranger's
  or a non-hub's step are dropped.
* **Exclusions hold**, **an offline home** is waited for and can be drawn
  without, and **older households** never take part.
"""

from __future__ import annotations

import collections
import copy
import dataclasses
import itertools
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

import orjson
import pytest

from socialhome.app_engines.draw import crypto as dc
from socialhome.app_engines.draw import service as svc_mod
from socialhome.app_engines.draw.domain import (
    DRAW_PENDING_SECONDS,
    DRAW_ROUNDS_MAX,
    DRAW_WAIT_SECONDS,
    AppDrawAgainLimitError,
    AppDrawHouseholdTooOldError,
    AppDrawPastDayError,
    DrawWireError,
)
from socialhome.app_engines.draw.service import _Round, complaint_holds
from socialhome.crypto import (
    Ed25519Keypair,
    b64url_decode,
    b64url_encode,
    ed25519_public_key,
    sign_ed25519,
)
from tests.app_draw_network import (
    APP,
    add_draw,
    draw_app,
    draw_table,
    engine_step,
    sent_steps,
    settle,
)
from tests.app_group_network import Network

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


async def _draw(net, *, pairs=None, drop=None) -> str:
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
    await settle(net, drop=drop)
    return sid


async def _results(net, sid, people=PEOPLE) -> dict[str, str]:
    out = {}
    for inst, users in people.items():
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
        if "hub" in (s, t) and p.get("verb") == "engine"
    ]


async def _hub_state(net, sid) -> dict:
    return await net.homes["hub"].draw._load(sid)


def _rounds(net) -> set[int]:
    return {p["round"] for p in sent_steps(net, "start")}


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
            # (c) — the assignment changes, the host's view mustn't.
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
            # The host holds its own person's card and seed only.
            assert set(state["final"]["results"]) == {"anna"}
            assert set(state["p"]["seeds"]) == {"anna"}
        finally:
            await net.close()
    assert len({tuple(sorted(a.items())) for a in assignments}) > 1, "runs drew alike"
    for a in assignments:
        _assert_circle(a)
    for v in views[1:]:
        assert _diff(views[0], v) == []
    for s in states[1:]:
        assert [d for d in _diff(states[0], s) if ".hub.since" not in d] == []


async def test_no_wish_list_or_result_reaches_the_hosts_disk_or_logs(net, caplog):
    caplog.set_level(logging.DEBUG)
    sid = await _draw(net)
    _assert_circle(await _results(net, sid))
    hub = net.homes["hub"]
    blob = b"".join(
        p.read_bytes()
        for p in (net.root / "hub").iterdir()
        if p.name.startswith("h.db")
    )
    assert blob
    for marker in WISH.values():
        assert marker.encode() not in blob
    row = await hub.db.fetchone(
        "SELECT state_json FROM app_engine_state WHERE session_id = ? AND engine = ?",
        (sid, "draw"),
    )
    assert set(orjson.loads(row["state_json"])) == {"v", "enc"}
    for marker in WISH.values():
        assert marker not in caplog.text
    frames = repr([f for _u, f in hub.ws.frames if f.get("type") == "app.engine"])
    for u in PEOPLE["b"] + PEOPLE["c"] + PEOPLE["d"]:
        assert WISH[u] not in frames


async def test_a_card_opens_only_for_its_giver(net):
    sid = await _draw(net)
    st = await _hub_state(net, sid)
    sealed = [b64url_decode(c) for c in st["p"]["cards"]]
    assert len(sealed) == 5 and len({len(c) for c in sealed}) == 1
    ctx = b64url_decode(st["p"]["ctx"])
    box = dc.person_keys(b64url_decode(st["p"]["seeds"]["anna"])).box_priv
    assert len(dc.try_cards(box, sealed, ctx=ctx)) == 1
    assert dc.try_cards(dc.new_keypair()[0], sealed, ctx=ctx) == []


# ─── draw-review-1: the attacks, one regression each ─────────────────────


async def test_last_mixer_chooses_the_circle(tmp_path, monkeypatch):
    """H1. The last mixer outputs S in any order it likes — the circle
    must not follow it: the same randomness everywhere, S reversed by the
    last mixer, and every person draws the same as before."""
    monkeypatch.setattr(
        "socialhome.services.app_group_service.uuid4", lambda: UUID(int=7)
    )
    real_mix = dc.mix
    results = []
    for reverse in (False, True):
        _fixed_identities(monkeypatch)

        def mixing(priv, entries, *, ctx, k, h, rand=os.urandom, rev=reverse):
            out = real_mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)
            return list(reversed(out)) if (rev and k == h - 1) else out

        monkeypatch.setattr(dc, "mix", mixing)
        net = Network(tmp_path / f"r{reverse}")
        try:
            sid = await draw_table(net, PEOPLE)
            hub = net.homes["hub"]
            await hub.draw.start(
                app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
            )
            await settle(net)
            st = await _hub_state(net, sid)
            results.append((await _results(net, sid), st["hub"]["S"]))
        finally:
            await net.close()
    (first, s1), (second, s2) = results
    assert s1 == list(reversed(s2))  # the last mixer did reorder S …
    assert first == second  # … and chose nothing by it


async def test_hub_peeks_then_rerolls_with_prearmed_missing(net, monkeypatch):
    """H2 (a). A household (here the hub's own) commits to one box but
    onions another, so it holds a "missing" proof for the committed box it
    can play when it likes. Onions derive from the seed: the proof's
    onion isn't the one it submitted, so it proves nothing — the household
    is named and nothing is redrawn. Played late (after the cards), it is
    refused outright."""
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    real_make = type(hub.draw)._make_onions

    def prearmed(self, rnd, pubs):
        onions, seeds, commits = real_make(self, rnd, pubs)
        if self._own == "hub":
            other = os.urandom(dc.SEED_BYTES)
            onions = [dc.onion_from_seed(other, pubs, ctx=rnd.ctx)]
        return onions, seeds, commits

    monkeypatch.setattr(type(hub.draw), "_make_onions", prearmed)
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    st = await _hub_state(net, sid)
    assert st["hub"]["blamed"] == ["hub"]
    assert _rounds(net) == {1}
    assert st["hub"]["phase"] == "reveal"  # stuck, not redrawn


async def test_prearmed_missing_proof_doesnt_hold(net):
    """H2 (a), the proof itself: a seed whose commitment the household
    signed but whose onion it never sent can't prove a key missing."""
    sid = await _draw(net)
    st = await _hub_state(net, sid)
    rnd = _Round(st["hub"])
    batches = {b["instance"]: b for b in st["hub"]["list_body"]["batches"]}
    mixer_pubs = [b64url_decode(x) for x in st["hub"]["mixer_pubs"]]
    S = [b64url_decode(s) for s in st["hub"]["S"]]
    links = [[b64url_decode(x) for x in link["hashes"]] for link in st["hub"]["chain"]]
    hub = net.homes["hub"]
    armed = os.urandom(dc.SEED_BYTES)
    # The household signs a commitment for the armed seed …
    batches["hub"] = {
        **batches["hub"],
        "commits": [
            ["anna", b64url_encode(dc.person_commitment(rnd.ctx, "hub", "anna", armed))]
        ],
    }
    sig = sign_ed25519(
        hub.fed.own_identity_seed,
        dc.complaint_message(rnd.ctx, "hub", "missing", "anna", armed),
    )
    complaint = {
        "kind": "missing",
        "instance": "hub",
        "user": "anna",
        "seed": b64url_encode(armed),
        "sig": b64url_encode(sig),
    }

    # … but its onion (in the batch hashes) was built from another seed.
    assert not complaint_holds(
        rnd,
        complaint,
        S=S,
        batches=batches,
        links=links,
        mixer_pubs=mixer_pubs,
        cards=None,
    )


async def test_hub_doctored_card_list_is_refused_and_frames_nobody(net, caplog):
    """H2 (b) / L5. The hub hands one household a card list with a card
    swapped out: the list isn't what every household committed to, so the
    household opens nothing, complains about nothing, and nobody is
    named — the round waits."""
    caplog.set_level(logging.INFO)

    def doctor(sender, to, payload):
        if payload.get("step") == "cards_all" and to == "c":
            payload["cards"] = [b64url_encode(b"\x03" * dc.SEALED_CARD_BYTES)] + list(
                payload["cards"][1:]
            )
        return False

    sid = await _draw(net, drop=doctor)
    c_state = await net.homes["c"].draw._load(sid)
    assert "results" not in c_state["p"] and not c_state["p"].get("complained")
    assert not sent_steps(net, "fail") and not sent_steps(net, "redo")
    assert "cards aren't the committed ones" in caplog.text
    st = await _hub_state(net, sid)
    assert st["hub"]["blamed"] == [] and st["hub"]["phase"] == "opening"


async def test_hub_cannot_slip_a_card_past_the_commitments(net, caplog):
    """H2 (b). Every household commits to its card hashes before any card
    is sent: a card that isn't the committed one (sealed after peeking) is
    refused."""
    caplog.set_level(logging.INFO)

    def swap(sender, to, payload):
        if payload.get("step") == "cards" and sender == "b":
            payload["cards"] = [b64url_encode(os.urandom(dc.SEALED_CARD_BYTES))] + list(
                payload["cards"][1:]
            )
        return False

    sid = await _draw(net, drop=swap)
    st = await _hub_state(net, sid)
    assert "b" not in st["hub"]["cards"] and st["hub"]["phase"] == "cards"
    assert "cards aren't the ones committed to" in caplog.text
    # Commitments went out before any card did.
    order = [
        p["step"] for p in (p for _s, _t, p in net.sent if p.get("verb") == "engine")
    ]
    assert order.index("commits") < order.index("cards")


async def test_household_reroll_by_leaving_after_peeking(net):
    """H3 (a). A household opens its cards (peeks), then one of its people
    leaves: no new round — the round completes, the leaver hands over."""
    held = []

    def hold_b_done(sender, to, payload):
        if sender == "b" and payload.get("step") == "done":
            held.append((sender, to, payload))
            return True
        return False

    sid = await _draw(net, drop=hold_b_done)
    assert held  # b peeked (opened its cards) and holds its done back
    b = net.homes["b"]
    await b.groups.leave(app_id=APP, session_id=sid, actor_user_id=b.uid("bea"))
    await settle(net, drop=hold_b_done)
    assert _rounds(net) == {1}  # no re-roll
    net.queue.extend(held)
    await settle(net)
    assert _rounds(net) == {1}
    st = await _hub_state(net, sid)
    assert st["hub"]["phase"] == "complete"
    # Bea's giver now gives to whoever Bea drew.
    left = {k: v for k, v in PEOPLE.items()}
    left["b"] = ["ben"]
    results = await _results(net, sid, left)
    assert "bea" not in results.values()
    assert sorted(results.values()) == sorted(results)


async def test_hub_reroll_with_changed_while_person_still_seated(net):
    """H3 (b). The hub sends a "changed" round dropping someone who is
    still seated: every household's own copy shows them there, so none
    starts it."""
    sid = await _draw(net)
    st = await _hub_state(net, sid)
    hub = st["hub"]
    st["hub"]["phase"] = "keys"  # (as if still running)
    for inst in ("b", "c", "d"):
        s = await net.homes[inst].draw._load(sid)
        s["last"]["complete"] = False
        await net.homes[inst].draw._save(sid, s)
    people = [p for p in hub["people"] if p["user"] != "cleo"]
    for to in ("b", "d"):
        net.forge(
            "hub",
            to,
            engine_step(
                sid,
                "start",
                gen=hub["gen"],
                round_no=hub["round"] + 1,
                people=people,
                identities={k: v for k, v in hub["identities"].items() if k != "c"},
                settings=hub["settings"],
                because={"kind": "changed"},
            ),
        )
    await settle(net)
    for inst in ("b", "d"):
        s = await net.homes[inst].draw._load(sid)
        assert s["p"]["round"] == hub["round"]  # not started
        assert s.get("pending_start")  # waits for its own copy to show it
    assert not [p for p in sent_steps(net, "key") if p["round"] == hub["round"] + 1]


async def test_handover_can_redirect_to_any_card(net):
    """H4. A household hands over with another box than the one the giver
    gives to (its other person's), so the giver would get that person's
    recipient: the giver checks the box against the one it holds and
    refuses."""
    sid = await _draw(net)
    before = await _results(net, sid)
    where = {u: inst for inst, users in PEOPLE.items() for u in users}
    leaver, giver = next(
        (r, g) for g, r in before.items() if r in ("ben", "bea") and where[g] != "b"
    )
    other = "bea" if leaver == "ben" else "ben"
    g_state = await net.homes[where[giver]].draw._load(sid)
    rnd = _Round(g_state["p"])
    b_state = await net.homes["b"].draw._load(sid)
    wrong_priv = dc.person_keys(b64url_decode(b_state["p"]["seeds"][other])).box_priv
    C = [b64url_decode(x) for x in b_state["p"]["C"]]
    leaver_pub = dc.person_keys(b64url_decode(b_state["p"]["seeds"][leaver])).box_pub
    giver_box = C[(C.index(leaver_pub) - 1) % len(C)]
    sig = sign_ed25519(
        net.homes["b"].fed.own_identity_seed,
        dc.handover_message(rnd.ctx, "b", leaver, [dc.public_of(wrong_priv)]),
    )
    sealed = dc.seal_handover(
        giver_box, dc.encode_handover("b", leaver, [wrong_priv], sig), ctx=rnd.ctx
    )
    net.forge(
        "b",
        "hub",
        engine_step(
            sid,
            "handover",
            gen=rnd.gen,
            round_no=rnd.round,
            sealed=b64url_encode(sealed),
        ),
    )
    await settle(net)
    assert (await _results(net, sid))[giver] == leaver


async def test_low_order_box_voids_publicly_and_blames_its_owner(net, monkeypatch):
    """M1. A household slips a key nobody can seal to into S: every
    household sees it in S, opens its seeds, the owner's opening doesn't
    rebuild its onion — it is named, and a fresh round follows."""
    sid = await draw_table(net, PEOPLE)
    c = net.homes["c"]
    real_make = type(c.draw)._make_onions

    def bad_once(self, rnd, pubs):
        onions, seeds, commits = real_make(self, rnd, pubs)
        if self._own == "c" and rnd.round == 1:
            onions = [dc.build_onion(b"\x00" * 32, pubs, ctx=rnd.ctx)]
        return onions, seeds, commits

    monkeypatch.setattr(type(c.draw), "_make_onions", bad_once)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    # b, c and d open over the network (the hub's own opening is local).
    assert {s for s, _t, p in net.sent if p.get("step") == "open"} == {"b", "c", "d"}
    assert [p for p in sent_steps(net, "start") if p["because"]["kind"] == "void"]
    st = await _hub_state(net, sid)
    assert st["hub"]["blamed"] == ["c"]
    status = await hub.draw.status(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
    )
    assert status["blamed"] == [{"seats": [4]}]
    _assert_circle(await _results(net, sid))


async def test_hub_adds_invisible_sybil_participant(net):
    """M2. The hub's start names a person nobody at the table sees: every
    household's people list must be exactly its own copy's seated people,
    so none starts it."""
    sid = await draw_table(net, PEOPLE)
    hub_home = net.homes["hub"]
    await hub_home.add_user("ghost")
    hub_home2 = hub_home
    table = await hub_home2.group_repo.get(sid)
    people = [
        {"seat": e.seat, "instance": e.instance, "user": e.user, "name": e.name}
        for e in table.roster
        if e.state == "seated"
    ] + [{"seat": 99, "instance": "hub", "user": "ghost", "name": "Ghost"}]
    ids = await hub_home.draw._identities(table, {"hub", "b", "c", "d"})
    for to in ("b", "c", "d"):
        net.forge(
            "hub",
            to,
            engine_step(
                sid,
                "start",
                people=people,
                identities=ids,
                settings={"name": "", "budget": "", "day": None, "pairs": []},
                because={"kind": "first"},
            ),
        )
    await settle(net)
    assert not sent_steps(net, "key")
    for inst in ("b", "c", "d"):
        assert "p" not in await net.homes[inst].draw._load(sid)


async def test_hub_cannot_complete_a_round_early(net):
    """L4. A "complete" without every household's signed done is refused:
    a hub can't freeze a household on a result the round never agreed."""

    def hold_done(sender, to, payload):
        return payload.get("step") == "done" and sender == "c"

    sid = await _draw(net, drop=hold_done)
    st = await _hub_state(net, sid)
    rnd = _Round(st["hub"])
    dones = copy.deepcopy(st["hub"]["done"])
    dones["c"] = {"count": 1, "sig": b64url_encode(b"\x00" * 64)}
    net.forge(
        "hub",
        "b",
        engine_step(sid, "complete", gen=rnd.gen, round_no=rnd.round, dones=dones),
    )
    await settle(net, drop=hold_done)
    b_state = await net.homes["b"].draw._load(sid)
    assert b_state["last"]["complete"] is False and "final" not in b_state


async def test_duplicate_card_commitments_dont_stall(net):
    """L6. A household commits to hashes another already committed to: the
    hub refuses that word (its state stays as it was) and the round goes on
    once the honest commitment arrives."""
    held = []

    def hold_c_commit(sender, to, payload):
        if sender == "c" and payload.get("step") == "commit" and not held:
            held.append((sender, to, payload))
            return True
        return False

    sid = await _draw(net, drop=hold_c_commit)
    st = await _hub_state(net, sid)
    rnd = _Round(st["hub"])
    b_hashes = [b64url_decode(x) for x in st["hub"]["commits"]["b"]["hashes"]][:1]
    sig = sign_ed25519(
        net.homes["c"].fed.own_identity_seed,
        dc.card_commit_message(rnd.ctx, "c", b_hashes),
    )
    net.forge(
        "c",
        "hub",
        engine_step(
            sid,
            "commit",
            gen=rnd.gen,
            round_no=rnd.round,
            hashes=[b64url_encode(x) for x in b_hashes],
            sig=b64url_encode(sig),
        ),
    )
    await settle(net, drop=hold_c_commit)
    assert "c" not in (await _hub_state(net, sid))["hub"]["commits"]
    net.queue.extend(held)
    await settle(net)
    _assert_circle(await _results(net, sid))


async def test_engine_steps_only_for_an_app_that_declares_it(net, caplog):
    """Engine dispatch: a household takes an ``engine`` step only when its
    own installed copy of the table's app declares that engine."""
    caplog.set_level(logging.INFO)
    sid = await draw_table(net, PEOPLE)
    b = net.homes["b"]
    await b.app_repo.update_installed(draw_app(engines=()))
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    assert not [p for s, _t, p in net.sent if s == "b" and p.get("step") == "key"]
    assert "for an app that doesn't declare it dropped" in caplog.text
    assert "p" not in await b.draw._load(sid)


# ─── draw-review-2: the attacks, one regression each ─────────────────────


def _seats(st: dict, instances) -> list[dict]:
    """``blamed`` as the app sees it, for *instances*."""
    people = st["hub"]["people"]
    return [
        {"seats": [p["seat"] for p in people if p["instance"] == i]} for i in instances
    ]


async def _everyone_names(net, sid, instances) -> None:
    """Every person at the table sees the same households named — each
    household checked the proof itself."""
    hub_st = await _hub_state(net, sid)
    want = _seats(hub_st, instances)
    for inst, users in PEOPLE.items():
        home = net.homes[inst]
        for u in users:
            status = await home.draw.status(
                app_id=APP, session_id=sid, actor_user_id=home.uid(u)
            )
            assert sorted(status["blamed"] or [], key=str) == sorted(want, key=str), (
                inst,
                u,
            )


async def test_hub_rerolls_after_peeking_with_end_then_first(net):
    """NEW-H1 (a). The hub holds one household's done back, reads its own
    person's card (it has the results), then sends ``end {rules}`` — a lie:
    no rule failed — and a ``first`` draw. No household takes the end (its
    own copy shows no reason for one), so none takes the restart."""

    def hold_d_done(s, _t, p):
        return s == "d" and p.get("step") == "done"

    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net, drop=hold_d_done)
    st = await _hub_state(net, sid)
    assert st["hub"]["phase"] == "opening" and st["p"]["results"]["anna"]
    table = await hub.group_repo.get(sid)
    async with hub.draw._lock(sid):
        st = await hub.draw._load(sid)
        await hub.draw._hub_end(table, st, "rules")
    await settle(net, drop=hold_d_done)
    for inst in ("b", "c", "d"):
        st_i = await net.homes[inst].draw._load(sid)
        assert not st_i.get("ended") and st_i.get("pending_end"), inst
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net, drop=hold_d_done)
    assert [p for p in sent_steps(net, "start") if p["gen"] == 2]
    for inst in ("b", "c", "d"):
        st_i = await net.homes[inst].draw._load(sid)
        assert st_i["p"]["gen"] == 1, inst  # nobody took the re-roll
    assert not [p for p in sent_steps(net, "key") if p["gen"] == 2]


async def test_hub_rerolls_after_complete_past_draw_again_rules(net, caplog):
    """NEW-H1 (b). After a completed draw the hub sends ``end`` then
    ``first``: refused — after the reveal no end is taken, and a new draw is
    only the organiser's visible Draw again (its count, its day)."""
    caplog.set_level(logging.INFO)
    sid = await _draw(net)
    first = await _results(net, sid)
    hub = net.homes["hub"]
    table = await hub.group_repo.get(sid)
    async with hub.draw._lock(sid):
        st = await hub.draw._load(sid)
        await hub.draw._hub_end(table, st, "rules")
    await settle(net)
    assert "an end after the reveal" in caplog.text
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    for inst in ("b", "c", "d"):
        st_i = await net.homes[inst].draw._load(sid)
        assert st_i["p"]["gen"] == 1 and not st_i.get("ended"), inst
    assert "not the first draw" in caplog.text
    others = {u: r for u, r in (await _results(net, sid)).items() if u != "anna"}
    assert others == {u: r for u, r in first.items() if u != "anna"}


async def test_a_restart_after_an_end_counts_as_draw_again(net):
    """A ``first`` draw after a real end is checked like Draw again: never
    past the exchange day (the hub refuses too) and within the count."""
    sid = await _draw(net)
    b = net.homes["b"]
    st_b = await b.draw._load(sid)
    st_b["last"]["complete"] = False
    st_b["ended"] = "rules"
    past = (datetime.now(UTC) - timedelta(days=3)).date().isoformat()
    st_b["p"]["settings"] = {**st_b["p"]["settings"], "day": past}
    table = await b.group_repo.get(sid)
    rnd = _Round(st_b["p"])
    with pytest.raises(DrawWireError, match="past the exchange day"):
        await b.draw._justify(table, st_b, 2, 1, rnd.people, {"kind": "first"})
    st_b["p"]["settings"]["day"] = None
    assert await b.draw._justify(table, st_b, 2, 1, rnd.people, {"kind": "first"}) == []
    hub = net.homes["hub"]
    st = await _hub_state(net, sid)
    st["hub"].update(phase="ended", ended="rules")
    st["hub"]["settings"] = {**st["hub"]["settings"], "day": past}
    await hub.draw._save(sid, st)
    with pytest.raises(AppDrawPastDayError):
        await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    st["hub"]["settings"]["day"] = None
    st["hub"]["gen"] = 11
    await hub.draw._save(sid, st)
    with pytest.raises(AppDrawAgainLimitError):
        await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))


async def test_rev1_card_frames_the_honest_giver(net, monkeypatch):
    """NEW-H2 / MED 3. Household b seals Ben's card with ``rev`` 1 (validly
    signed). The giver and anyone checking its complaint use one test (a
    round's card is ``rev`` 0): the complaint holds, every household opens
    its box keys, and b — who sealed it — is named by everyone; a fresh
    round follows."""
    real = svc_mod.AppDrawService._card_for

    async def card_for(self, rnd, user, *, wish, rev):
        if self._own == "b" and user == "ben" and rnd.round == 1:
            rev = 1
        return await real(self, rnd, user, wish=wish, rev=rev)

    monkeypatch.setattr(svc_mod.AppDrawService, "_card_for", card_for)
    sid = await _draw(net)
    # (The giver may be the hub's own person: its steps stay local.)
    voids = [p for p in sent_steps(net, "start") if p["because"]["kind"] == "void"]
    assert voids and voids[0]["because"]["why"] == "cards"
    assert voids[0]["because"]["complaint"]["kind"] == "cards"
    # Box keys opened, never a seed.
    for p in sent_steps(net, "open"):
        assert set(p["opening"]) == {"boxes", "sig"}
    st = await _hub_state(net, sid)
    assert st["hub"]["blamed"] == ["b"]
    await _everyone_names(net, sid, ["b"])
    _assert_circle(await _results(net, sid))


async def test_a_card_sealed_to_the_wrong_box_names_its_household(net, monkeypatch):
    """NEW-H2 (c). Household c seals Cleo's card to a key nobody holds:
    her giver's box opens nothing, the complaint holds, the box keys show
    whose card belonged there — c is named by everyone."""
    real = svc_mod.AppDrawService._make_cards

    async def misplaced(self, rnd, cur, C, wishes):
        cards = await real(self, rnd, cur, C, wishes)
        if self._own == "c" and rnd.round == 1:
            card = await self._card_for(rnd, "cleo", wish="", rev=0)
            cards = [dc.seal_card(dc.new_keypair()[1], card, ctx=rnd.ctx)]
        return cards

    monkeypatch.setattr(svc_mod.AppDrawService, "_make_cards", misplaced)
    sid = await _draw(net)
    voids = [p for p in sent_steps(net, "start") if p["because"]["kind"] == "void"]
    assert {(v["round"], v["because"]["why"]) for v in voids} == {(2, "cards")}
    st = await _hub_state(net, sid)
    assert st["hub"]["blamed"] == ["c"]
    await _everyone_names(net, sid, ["c"])
    _assert_circle(await _results(net, sid))


async def test_mixer_injects_low_order_point_nobody_blamed(net, monkeypatch):
    """NEW-H2 (b). The last mixer puts a key nobody can seal to in place of
    one entry of its own output — every round. Every household opens its
    seeds; each seed rebuilds its person's entry at every step of the mix
    and the signed per-entry chain shows whose output lacks it: that mixer
    is named, by everyone, every round, until the draw gives up."""
    monkeypatch.setattr("socialhome.app_engines.draw.service.DRAW_ROUNDS_MAX", 3)
    real_mix = dc.mix

    def evil(priv, entries, *, ctx, k, h, rand=os.urandom):
        out = real_mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)
        if k == h - 1:
            out[0] = b"\x00" * 32
        return out

    monkeypatch.setattr(dc, "mix", evil)
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    st = await _hub_state(net, sid)
    lasts = []
    for rnd_no in (1, 2, 3):
        order = dc.mix_order(sid, 1, rnd_no, list(PEOPLE))
        lasts.append(order[-1])
    assert st["hub"]["ended"] == "rules"
    assert sorted(st["hub"]["blamed"]) == sorted(set(lasts))
    # Each household checked every void and the final give-up itself.
    for inst in ("b", "c", "d"):
        st_i = await net.homes[inst].draw._load(sid)
        assert st_i["ended"] == "rules", inst
        assert sorted(st_i["blamed"]) == sorted(set(lasts)), inst


async def test_hub_mixer_arms_missing_and_decides_after_all_reveals(net, monkeypatch):
    """NEW-H2 (a). The hub, as a mixer, swaps its own person's peeled entry
    for a box it holds — a genuine "missing" proof — and holds it until
    every other household revealed ``r``. The chain names the mixer whose
    output lacks the entry: the hub itself, so its complaint proves nothing
    against anyone — refused, the hub named, no fresh round."""
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    cap: dict = {}
    real_make = svc_mod.AppDrawService._make_onions

    def make(self, rnd, pubs):
        onions, seeds, commits = real_make(self, rnd, pubs)
        if self._own == "hub":
            cap.update(
                seed=svc_mod._raw(seeds["anna"]),
                pubs=pubs,
                ctx=rnd.ctx,
                k=rnd.order.index("hub"),
                h=rnd.h,
            )
        return onions, seeds, commits

    real_mix = dc.mix

    def evil(priv, entries, *, ctx, k, h, rand=os.urandom):
        out = real_mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)
        if cap and k == cap["k"] and ctx == cap["ctx"] and not cap.get("swapped"):
            mine = dc.onion_layers(cap["seed"], cap["pubs"], ctx=ctx)[k + 1]
            x = dc.new_keypair()[1]
            for j in reversed(range(k + 1, h)):
                x = dc.seal(cap["pubs"][j], x, ctx=ctx, label=f"layer|{j}")
            out[out.index(mine)] = x
            cap["swapped"] = True
        return out

    monkeypatch.setattr(svc_mod.AppDrawService, "_make_onions", make)
    monkeypatch.setattr(dc, "mix", evil)
    seen: dict = {}
    real_c = svc_mod.AppDrawService._hub_complaint

    async def stash(self, table, st, rnd, sender, step, p):
        if sender == "hub" and "stash" not in seen:
            seen["stash"] = p  # held until every other r is in
            return None
        return await real_c(self, table, st, rnd, sender, step, p)

    monkeypatch.setattr(svc_mod.AppDrawService, "_hub_complaint", stash)
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    assert cap.get("swapped")
    st = await _hub_state(net, sid)
    assert sorted(st["hub"]["reveals"]) == ["b", "c", "d"]
    table = await hub.group_repo.get(sid)
    await hub.draw._step(table, "hub", seen["stash"])
    await settle(net)
    st = await _hub_state(net, sid)
    assert st["hub"]["blamed"] == ["hub"]
    assert _rounds(net) == {1}
    for inst in ("b", "c", "d"):
        assert (await net.homes[inst].draw._load(sid))["p"]["round"] == 1, inst


async def test_a_mixer_that_drops_anothers_entry_is_named_by_everyone(net, monkeypatch):
    """The last mixer swaps b's person's entry for a box of its own: b's
    "missing" proof holds, the chain names the mixer, every household names
    it, and a fresh round draws everyone."""
    real_make = svc_mod.AppDrawService._make_onions
    boxes: dict = {}

    def make(self, rnd, pubs):
        onions, seeds, commits = real_make(self, rnd, pubs)
        if self._own == "b":
            boxes["b"] = {
                dc.person_keys(svc_mod._raw(s)).box_pub for s in seeds.values()
            }
            boxes["round"] = rnd.round
        return onions, seeds, commits

    real_mix = dc.mix

    def swap(priv, entries, *, ctx, k, h, rand=os.urandom):
        out = real_mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)
        if k == h - 1 and boxes.get("round") == 1 and not boxes.get("done"):
            i = next(i for i, o in enumerate(out) if o in boxes["b"])
            out[i] = dc.new_keypair()[1]
            boxes["done"] = True
        return out

    monkeypatch.setattr(svc_mod.AppDrawService, "_make_onions", make)
    monkeypatch.setattr(dc, "mix", swap)
    sid = await _draw(net)
    order = dc.mix_order(sid, 1, 1, list(PEOPLE))
    if order[-1] == "b":
        pytest.skip("b mixes last for this table id")
    fails = sent_steps(net, "fail")
    assert fails and fails[0]["complaint"]["instance"] == "b"
    assert _rounds(net) == {1, 2}
    st = await _hub_state(net, sid)
    assert st["hub"]["blamed"] == [order[-1]]
    await _everyone_names(net, sid, [order[-1]])
    _assert_circle(await _results(net, sid))


async def test_a_mixer_that_outputs_garbage_is_named_not_the_next_one(net, monkeypatch):
    """LOW (waiting_for / blamed name only proven misbehaviour). The first
    mixer outputs an entry nobody can peel: the next mixer can't mix, says
    so with its seeds, every household opens its seeds, the chain shows the
    first mixer's output lacks a person — it is named, not the one stuck."""
    real_mix = dc.mix

    def garbage(priv, entries, *, ctx, k, h, rand=os.urandom):
        out = real_mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)
        if k == 0 and not getattr(garbage, "done", 0):
            out[0] = os.urandom(len(out[0]))
            garbage.done = 1  # type: ignore[attr-defined]
        return out

    monkeypatch.setattr(dc, "mix", garbage)
    sid = await _draw(net)
    order = dc.mix_order(sid, 1, 1, list(PEOPLE))
    opens = sent_steps(net, "open")
    stuck = [p for p in opens if p["opening"].get("stuck") is not None]
    assert stuck and stuck[0]["opening"]["stuck"] == 1
    voids = [p for p in sent_steps(net, "start") if p["because"]["kind"] == "void"]
    assert voids and voids[0]["because"]["why"] == "stuck"
    st = await _hub_state(net, sid)
    assert st["hub"]["blamed"] == [order[0]]
    await _everyone_names(net, sid, [order[0]])
    _assert_circle(await _results(net, sid))


async def test_a_mixer_that_says_stuck_falsely_names_itself(net, monkeypatch):
    """A mixer whose input is every honest entry claims it can't peel: the
    seeds show every entry reached it intact — it is the one named."""
    order = dc.mix_order
    real_mix = dc.mix
    said: list = []

    def liar(priv, entries, *, ctx, k, h, rand=os.urandom):
        if k == 1 and not said:
            said.append(True)
            raise dc.DrawCryptoError("seal doesn't open")
        return real_mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)

    monkeypatch.setattr(dc, "mix", liar)
    sid = await _draw(net)
    st = await _hub_state(net, sid)
    liar_home = order(sid, 1, 1, list(PEOPLE))[1]
    assert st["hub"]["blamed"] == [liar_home]
    await _everyone_names(net, sid, [liar_home])
    _assert_circle(await _results(net, sid))


async def test_a_stuck_void_without_the_mixers_word_opens_nothing(net):
    """A hub can't make households open their seeds by claiming a mixer is
    stuck: the void must carry that mixer's signed word."""

    def hold_mix(s, t, p):
        return p.get("step") == "mix"

    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net, drop=hold_mix)
    st = await _hub_state(net, sid)
    rnd = _Round(st["hub"])
    claim = {"seeds": {}, "sig": b64url_encode(b"\x00" * 64), "stuck": 0}
    for to in ("b", "c", "d"):
        net.forge(
            "hub",
            to,
            engine_step(
                sid,
                "void",
                gen=rnd.gen,
                round_no=rnd.round,
                why="stuck",
                k=0,
                claim=claim,
                batches=st["hub"]["batches_pub"],
                chain=[],
            ),
        )
    await settle(net, drop=hold_mix)
    assert not sent_steps(net, "open")


@pytest.mark.parametrize("first_to_leave", ["later", "earlier"])
async def test_chained_hand_overs_reach_the_next_one_still_seated(net, first_to_leave):
    """MED 4. In the circle X → Y → Z → W, Z and then Y leave (or Y, then
    Z): X now gives to W — never to someone who left — and W has a giver.
    Each leaver passes every box key handed to it, as a chain the giver
    checks against the circle."""
    sid = await _draw(net)
    before = await _results(net, sid)  # giver -> recipient (lower-case names)
    giver_of = {r: g for g, r in before.items()}
    where = {u: inst for inst, users in PEOPLE.items() for u in users}
    # Y and Z not on the hub (its organiser stays) — X and W stay.
    for z in before:
        y = giver_of[z]
        x, w = giver_of[y], before[z]
        if "anna" not in (y, z) and len({x, y, z, w}) == 4:
            break
    else:  # pragma: no cover — 5 people always have such a run
        raise AssertionError("no run")
    order = (z, y) if first_to_leave == "later" else (y, z)
    for leaver in order:
        home = net.homes[where[leaver]]
        await home.groups.leave(
            app_id=APP, session_id=sid, actor_user_id=home.uid(leaver)
        )
        await settle(net)
    still = {
        inst: [u for u in users if u not in (y, z)] for inst, users in PEOPLE.items()
    }
    still = {k: v for k, v in still.items() if v}
    after = await _results(net, sid, still)
    assert after[x] == w
    assert y not in after.values() and z not in after.values()
    _assert_circle(after)


async def test_kept_hand_overs_and_starts_expire(net, monkeypatch):
    """LOW. What a household keeps until its own copy of the table shows it
    (a hand-over from someone still seated here, a start, an end) is
    dropped after DRAW_PENDING_SECONDS."""
    sid = await _draw(net)
    b = net.homes["b"]
    st = await b.draw._load(sid)
    st["final"]["handovers"] = [
        {"sealed": b64url_encode(os.urandom(dc.SEALED_HANDOVER_BYTES)), "at": _ago(0)}
    ]
    st["pending_start"] = {"p": {"gen": 2, "round": 1}, "at": _ago(2)}
    await b.draw._save(sid, st)
    later = datetime.now(UTC) + timedelta(seconds=DRAW_PENDING_SECONDS + 60)
    monkeypatch.setattr("socialhome.app_engines.draw.service._now", lambda: later)
    await b.draw._table_changed(sid)
    st = await b.draw._load(sid)
    assert st["final"]["handovers"] == [] and "pending_start" not in st


def _ago(hours: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


async def test_small_tables_with_a_pair_spread_evenly_and_never_give_up(
    tmp_path, monkeypatch
):
    """LOW (spread). Four people, one pair: one circle in three fits, so a
    draw takes a few rounds — and the two circles that fit come out about
    equally often. A box names nobody, so the circle can't steer around a
    pair; the round cap is what keeps it from giving up:
    (2/3)^DRAW_ROUNDS_MAX < 1e-7."""
    assert (2 / 3) ** DRAW_ROUNDS_MAX < 1e-7
    people = {"a": ["anna"], "b": ["ben", "lena"], "c": ["cleo"]}
    counts: collections.Counter = collections.Counter()
    for i in range(40):
        net = Network(tmp_path / f"r{i}")
        try:
            sid = await draw_table(net, people, hub="a")
            hub = net.homes["a"]
            seats = {e.user: e.seat for e in (await hub.group_repo.get(sid)).roster}
            await hub.draw.configure(
                app_id=APP,
                session_id=sid,
                actor_user_id=hub.uid("anna"),
                settings={"pairs": [[seats["ben"], seats["lena"]]]},
            )
            await hub.draw.start(
                app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
            )
            await settle(net)
            res = await _results(net, sid, people)
            assert res["ben"] != "lena" and res["lena"] != "ben"
            _assert_circle(res)
            counts[tuple(sorted(res.items()))] += 1
        finally:
            await net.close()
    assert len(counts) == 2
    assert min(counts.values()) >= 6  # 40 draws, p = 1/2 each: < 6 is 4.4 σ


# ─── A cheating household is caught ───────────────────────────────────────


async def test_a_duplicated_entry_is_refused(net, monkeypatch):
    real_mix = dc.mix

    def duplicating_mix(priv, entries, *, ctx, k, h, rand=os.urandom):
        out = real_mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)
        out[1] = out[0]
        return out

    monkeypatch.setattr(dc, "mix", duplicating_mix)
    sid = await _draw(net)
    assert not sent_steps(net, "list")
    hub = net.homes["hub"]
    status = await hub.draw.status(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
    )
    assert status["state"] == "drawing" and status["has_result"] is False


async def test_a_dropped_onion_on_the_way_stalls_rather_than_leaks(net):
    def drop(sender, to, payload):
        # The relay passes the first mixer a list with one onion missing.
        if payload.get("step") == "mix" and payload.get("k") == 0:
            payload["batches"][0]["onions"] = payload["batches"][0]["onions"][:-1]
        return False

    await _draw(net, drop=drop)
    assert not sent_steps(net, "mixed")


async def test_wrong_suite_strangers_replays_and_non_hub_steps_are_dropped(net, caplog):
    caplog.set_level(logging.INFO)
    sid = await draw_table(net, PEOPLE)
    stranger = await net.household("e")
    await stranger.add_user("eve")
    await stranger.app_repo.install(draw_app())
    add_draw(stranger)
    await net.pair(net.homes["hub"], stranger)
    await net.pair(net.homes["c"], stranger)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    held: list[tuple[str, str, dict]] = []

    def hold(sender, to, payload):
        step = payload.get("step")
        if (sender, step) == ("b", "key") or (to, step) == ("c", "start"):
            held.append((sender, to, payload))
            return True
        return False

    await settle(net, drop=hold)
    key_from_b = next(p for s, _t, p in held if s == "b")
    start_to_c = next(p for _s, t, p in held if t == "c")
    net.forge("e", "hub", dict(key_from_b))  # a stranger's (copied) key
    net.forge("b", "hub", {**key_from_b, "engine_suite": "x25519"})  # unknown suite
    net.forge("b", "hub", {**key_from_b, "round": 2})  # another round
    net.forge("e", "c", dict(start_to_c))  # a stranger posing as the hub
    net.forge("b", "c", dict(start_to_c))  # a participant posing as the hub
    await settle(net)
    st = await _hub_state(net, sid)
    assert "b" not in st["hub"]["keys"] and "e" not in st["hub"]["keys"]
    assert "p" not in await net.homes["c"].draw._load(sid)
    assert "not a participant household" in caplog.text
    assert "not from the hub" in caplog.text
    assert "engine_suite 'x25519'" in caplog.text
    for item in held:
        net.queue.append(item)
    await settle(net)
    _assert_circle(await _results(net, sid))


async def test_an_oversize_onion_is_refused(net):
    def grow(sender, to, payload):
        if payload.get("step") == "onions" and sender == "c":
            payload["onions"] = [o + "AAAA" for o in payload["onions"]]
        return False

    sid = await _draw(net, drop=grow)
    assert "c" not in (await _hub_state(net, sid))["hub"]["batches"]


async def test_a_complaint_without_proof_is_named_and_changes_nothing(net):
    sid = await _draw(net)
    before = await _results(net, sid)
    b = net.homes["b"]
    st_b = await b.draw._load(sid)
    seed = b64url_decode(st_b["p"]["seeds"]["ben"])
    st = await _hub_state(net, sid)
    rnd = _Round(st["hub"])
    # Ben's household claims he drew a partner (there are no pairs) — a
    # real seed, a real signature, a false claim.
    sig = sign_ed25519(
        b.fed.own_identity_seed,
        dc.complaint_message(rnd.ctx, "b", "partner", "ben", seed),
    )
    st["hub"]["phase"] = "opening"
    st["hub"]["done"].pop("b")
    await net.homes["hub"].draw._save(sid, st)
    net.forge(
        "b",
        "hub",
        engine_step(
            sid,
            "redo",
            gen=rnd.gen,
            round_no=rnd.round,
            complaint={
                "kind": "partner",
                "instance": "b",
                "user": "ben",
                "seed": b64url_encode(seed),
                "sig": b64url_encode(sig),
            },
        ),
    )
    await settle(net)
    after = await _hub_state(net, sid)
    assert after["hub"]["blamed"] == ["b"]
    assert after["hub"]["round"] == rnd.round
    status = await net.homes["hub"].draw.status(
        app_id=APP, session_id=sid, actor_user_id=net.homes["hub"].uid("anna")
    )
    assert status["blamed"] == [{"seats": [2, 3]}]
    assert await _results(net, sid) == before


async def test_a_new_round_without_its_reason_is_refused(net):
    """The organiser's household can't re-roll on demand: a start for the
    next round that says "changed" with the same people, "first" again, a
    complaint that doesn't hold or a void that isn't is refused."""
    sid = await _draw(net)
    hub = (await _hub_state(net, sid))["hub"]
    for because in (
        {"kind": "changed"},
        {"kind": "first"},
        {"kind": "complaint", "complaint": {"kind": "missing"}, "list": {}},
        {"kind": "void", "list": hub["list_body"]},
        {"kind": "whatever"},
    ):
        for gen, rnd in ((hub["gen"], hub["round"] + 1), (1, 1)):
            net.forge(
                "hub",
                "b",
                engine_step(
                    sid,
                    "start",
                    gen=gen,
                    round_no=rnd,
                    people=hub["people"],
                    identities=hub["identities"],
                    settings=hub["settings"],
                    because=because,
                ),
            )
    await settle(net)
    st_b = await net.homes["b"].draw._load(sid)
    assert (st_b["p"]["gen"], st_b["p"]["round"]) == (hub["gen"], hub["round"])
    assert st_b["last"]["complete"] is True


# ─── Exclusions, offline homes, older homes ───────────────────────────────


async def test_pairs_redo_with_proof_until_the_circle_fits(tmp_path, monkeypatch):
    """ben(2)–bea(3) may not draw each other: about half of the rounds hit
    the pair and are redrawn on the giver's proof. Over a few draws the
    redo path really runs, and every result keeps the pair apart."""
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
            redos = sent_steps(net, "redo")
            for p in redos:
                assert p["complaint"]["kind"] == "partner"
                assert p["complaint"]["instance"] == "b"
            assert len(_rounds(net)) == len(redos) + 1
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
    monkeypatch.setattr("socialhome.app_engines.draw.service._now", lambda: later)
    status = await hub.draw.status(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
    )
    assert status["waiting_for"] == [{"seats": [5]}]
    # "Draw without them": the organiser takes Dev's seat out.
    await hub.groups.remove(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), seat=5
    )
    await settle(net, drop=lambda s, t, p: "d" in (s, t))
    left = {k: v for k, v in PEOPLE.items() if k != "d"}
    results = await _results(net, sid, left)
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
        net.homes["b"].fed.versions["c"] = 77
        net.forge("c", "b", engine_step(sid, "start"))
        await settle(net)
        assert "p" not in await net.homes["b"].draw._load(sid)
    finally:
        await net.close()


async def test_every_draw_step_rides_the_one_sealed_engine_verb(net):
    await _draw(net)
    steps = [p for _s, _t, p in net.sent if p.get("verb") == "engine"]
    assert steps
    for p in steps:
        assert net.types[id(p)].value == "app_session"
        assert p["engine"] == "draw"
        assert p["engine_suite"] == dc.DRAW_SUITE_X25519_AESGCM
        assert isinstance(p["gen"], int) and isinstance(p["round"], int)
    assert not [
        p for _s, _t, p in net.sent if str(p.get("verb", "")).startswith("draw_")
    ]


def test_the_draw_app_declares_the_engine():
    app = draw_app()
    assert app.manifest.engines == ("draw",)
    assert dataclasses.replace(app.manifest, engines=()).engines == ()


def _fixed_identities(monkeypatch) -> None:
    """Household identity keys from a counter (same households, same keys,
    run after run) — so two runs differ only where a test makes them."""
    counter = itertools.count(1)

    def keypair():
        seed = bytes([next(counter)]) * 32
        return Ed25519Keypair(private_key=seed, public_key=ed25519_public_key(seed))

    monkeypatch.setattr("tests.app_group_network.generate_identity_keypair", keypair)
