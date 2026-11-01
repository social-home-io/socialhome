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
from socialhome.app_engines.draw.domain import (
    DRAW_WAIT_SECONDS,
    AppDrawHouseholdTooOldError,
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
        rnd, complaint, S=S, batches=batches, mixer_pubs=mixer_pubs, cards=None
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
        dc.handover_message(rnd.ctx, "b", leaver, dc.public_of(wrong_priv)),
    )
    sealed = dc.seal_handover(
        giver_box, dc.encode_handover("b", leaver, wrong_priv, sig), ctx=rnd.ctx
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
    fails = sent_steps(net, "fail")
    assert fails and fails[0]["complaint"]["kind"] == "missing"
    assert _rounds(net) == {1, 2}
    assert all(
        p["because"]["kind"] == "complaint"
        for p in sent_steps(net, "start")
        if p["round"] == 2
    )
    _assert_circle(await _results(net, sid))


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
