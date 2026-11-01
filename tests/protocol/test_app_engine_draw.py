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
import random
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

import orjson
import pytest

from socialhome.app_engines.draw import crypto as dc
from socialhome.app_engines.draw import service as svc_mod
from socialhome.app_engines.draw.domain import (
    DRAW_CARD_REDRAWS_MAX,
    DRAW_PARTNER_REDRAWS_MAX,
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
from socialhome.federation.gfs_relay_transport import RELAY_MAX_ENVELOPE_BYTES
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
                # (the cards are out: a removal now is a Draw again)
                gen=hub["gen"] + 1,
                round_no=1,
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
    assert not [p for p in sent_steps(net, "key") if p["gen"] == hub["gen"] + 1]


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
        dc.card_commit_message(rnd.t("reveals"), "c", b_hashes),
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


async def _no_one_named_card_redraw(net, sid) -> None:
    """A card complaint held: a visible redraw (round 2, because the
    complaint) that names nobody and opened nothing — counted by every
    household."""
    redo = [p for p in sent_steps(net, "start") if p["round"] == 2]
    assert redo and redo[0]["because"]["kind"] == "complaint"
    assert redo[0]["because"]["complaint"]["kind"] == "cards"
    assert set(redo[0]["because"]) == {"kind", "complaint"}  # nothing more
    assert not sent_steps(net, "open")  # nobody opened anything
    st = await _hub_state(net, sid)
    assert st["hub"]["blamed"] == [] and st["hub"]["card_redraws"] == 1
    for inst in PEOPLE:
        s_i = await net.homes[inst].draw._load(sid)
        assert not s_i.get("blamed") and s_i["card_redraws"] == 1, inst
    _assert_circle(await _results(net, sid))


async def test_rev1_card_frames_the_honest_giver(net, monkeypatch):
    """NEW-H2 / MED 3 (review 2). Household b seals a card with ``rev`` 1
    (validly signed). The giver and anyone checking its complaint use one
    test (a round's card is ``rev`` 0): the complaint holds on the giver's
    own box alone, so the honest giver is never framed. Since review 5 a
    card complaint names nobody (nothing is opened to find out who): a
    visible, counted redraw."""
    real_make = svc_mod.AppDrawService._make_cards

    async def make(self, rnd, cur, C, wishes):
        if self._own != "b" or rnd.round != 1:
            return await real_make(self, rnd, cur, C, wishes)
        plain = []
        for i, user in enumerate(sorted(cur["seeds"])):
            to = C[dc.circle_targets(C.index(self._box_pub(cur, user)), rnd.n)[1]]
            card = await self._card_for(rnd, user, wish="", rev=1 if i == 0 else 0)
            plain.append(dc.seal_card(to, card, ctx=rnd.ctx))
        return plain

    monkeypatch.setattr(svc_mod.AppDrawService, "_make_cards", make)
    sid = await _draw(net)
    await _no_one_named_card_redraw(net, sid)


async def test_a_card_sealed_to_the_wrong_box_names_its_household(net, monkeypatch):
    """NEW-H2 (c). Household c seals Cleo's card to a key nobody holds: her
    giver's box opens nothing — proven by the giver's own seed. After the
    list nothing is ever opened (review 5): a visible redraw naming nobody,
    counted against the draw, never against the complainer."""
    real = svc_mod.AppDrawService._make_cards

    async def misplaced(self, rnd, cur, C, wishes):
        cards = await real(self, rnd, cur, C, wishes)
        if self._own == "c" and rnd.round == 1:
            card = await self._card_for(rnd, "cleo", wish="", rev=0)
            cards = [dc.seal_card(dc.new_keypair()[1], card, ctx=rnd.ctx)]
        return cards

    monkeypatch.setattr(svc_mod.AppDrawService, "_make_cards", misplaced)
    sid = await _draw(net)
    await _no_one_named_card_redraw(net, sid)


async def test_mixer_injects_low_order_point_nobody_blamed(net, monkeypatch):
    """NEW-H2 (b) (review 2) and LOW 6 (review 3). The last mixer puts a
    key nobody can seal to in place of one entry of its own output — every
    round. Every household opens its seeds; the signed per-entry chain shows
    whose output lacks the entry: that mixer is named, by everyone. Once the
    same household broke a second round, no more rounds: the draw waits for
    the organiser ("Draw without them"), who sees it at once."""
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
    lasts: list[str] = []
    for rnd_no in range(1, 20):
        last = dc.mix_order(sid, 1, rnd_no, list(PEOPLE))[-1]
        lasts.append(last)
        if lasts.count(last) == 2:
            break
    assert st["hub"]["phase"] == "held" and st["hub"]["held"] == [lasts[-1]]
    assert st["hub"]["round"] == len(lasts)
    assert sorted(st["hub"]["blamed"]) == sorted(set(lasts))
    # Each household checked every void itself and names the same.
    for inst in ("b", "c", "d"):
        st_i = await net.homes[inst].draw._load(sid)
        assert sorted(st_i["blamed"]) == sorted(set(lasts[:-1])), inst
    status = await hub.draw.status(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
    )
    assert status["waiting_for"] == _seats(st, [lasts[-1]])
    # The organiser draws without them: a fresh round, the cheat gone.
    monkeypatch.setattr(dc, "mix", real_mix)
    for e in (await hub.group_repo.get(sid)).roster:
        if e.instance == lasts[-1] and e.state == "seated":
            await hub.groups.remove(
                app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), seat=e.seat
            )
            await settle(net)
    left = {k: v for k, v in PEOPLE.items() if k != lasts[-1]}
    if lasts[-1] != "hub":
        _assert_circle(await _results(net, sid, left))


async def test_every_household_refuses_a_round_after_a_second_break(net, monkeypatch):
    """R4 LOW 4. Each household counts who broke rounds of a draw itself: a
    hub that ignores the hold and starts the next round anyway finds every
    household refusing it."""
    real_mix = dc.mix

    def evil(priv, entries, *, ctx, k, h, rand=os.urandom):
        out = real_mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)
        if k == h - 1:
            out[0] = b"\x00" * 32
        return out

    async def no_hold(self, table, st, rnd, because, culprits):
        self._blame(st["hub"], culprits)
        await self._next_round(
            table, st, people=rnd.people, names=rnd.names, because=because
        )

    monkeypatch.setattr(dc, "mix", evil)
    monkeypatch.setattr(svc_mod.AppDrawService, "_redraw", no_hold)
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    lasts: list[str] = []
    for rnd_no in range(1, 20):
        last = dc.mix_order(sid, 1, rnd_no, list(PEOPLE))[-1]
        lasts.append(last)
        if lasts.count(last) == 2:
            break
    # Round len(lasts) broke a second time by the same household; the hub
    # started round len(lasts) + 1 anyway — nobody took it.
    st = await _hub_state(net, sid)
    assert st["hub"]["round"] == len(lasts) + 1
    for inst in ("b", "c", "d"):
        s_i = await net.homes[inst].draw._load(sid)
        assert s_i["p"]["round"] == len(lasts), inst


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
    assert order[-1] != "b"  # (the fixed table id: b doesn't mix last)
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


@pytest.mark.parametrize("same_home", [True, False])
async def test_small_tables_with_a_pair_spread_evenly_and_never_give_up(
    tmp_path, monkeypatch, same_home
):
    """LOW (spread). Four people, one pair: one circle in three fits, so a
    draw takes a few rounds — and the two circles that fit come out about
    equally often. A box names nobody, so the circle can't steer around a
    pair; the caps are what keep it from giving up: (2/3)^DRAW_ROUNDS_MAX
    < 1e-7, and the pairs' own redraw cap (2/3)^DRAW_PARTNER_REDRAWS_MAX
    < 1e-5 (R6: genuine draws never reach it — pinned below). The pair is
    in one household (told before any card) or in two (after them)."""
    assert (2 / 3) ** DRAW_ROUNDS_MAX < 1e-7
    assert (2 / 3) ** DRAW_PARTNER_REDRAWS_MAX < 1e-5
    people = (
        {"a": ["anna"], "b": ["ben", "lena"], "c": ["cleo"]}
        if same_home
        else {"a": ["anna"], "b": ["ben"], "c": ["lena"], "d": ["cleo"]}
    )
    counts: collections.Counter = collections.Counter()
    most = 0
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
            st = await hub.draw._load(sid)
            most = max(most, st["hub"]["partner_redraws"])
            kinds = {p["complaint"]["kind"] for p in sent_steps(net, "redo")}
            assert kinds <= ({"couple"} if same_home else {"partner"})
        finally:
            await net.close()
    assert len(counts) == 2
    assert most < DRAW_PARTNER_REDRAWS_MAX
    assert min(counts.values()) >= 6  # 40 draws, p = 1/2 each: < 6 is 4.4 σ


# ─── draw-review-3: the attacks, one regression each ─────────────────────


async def _late_card_void(net, sid) -> None:
    """The hub sends every household a card "void" with a valid complaint
    about its own person's box — the old path that opened every box key."""
    hub = net.homes["hub"]
    table = await hub.group_repo.get(sid)
    st = await hub.draw._load(sid)
    rnd = svc_mod._Round(st["p"])
    msg = hub.draw._complaint(rnd, st["p"], "cards", "anna")
    for inst in ("b", "c", "d"):
        await hub.draw._send(
            table,
            inst,
            "void",
            hub.draw._rid(rnd, why="cards", complaint=msg["complaint"]),
        )


async def test_hub_learns_whole_completed_draw_with_a_late_card_void(net, caplog):
    """R3 HIGH 1. After a completed draw the hub sends a card "void": no
    household opens anything — a finished draw is never openable (no void
    once the list is out; card complaints open nothing)."""
    caplog.set_level(logging.INFO)
    sid = await _draw(net)
    before = {inst: (await net.homes[inst].draw._load(sid))["final"] for inst in PEOPLE}
    await _late_card_void(net, sid)
    await settle(net)
    assert "a void after the list" in caplog.text
    assert not sent_steps(net, "open")
    for inst in ("b", "c", "d"):
        st_i = await net.homes[inst].draw._load(sid)
        assert st_i["last"]["complete"] and st_i["final"] == before[inst], inst


async def test_hub_voids_then_completes_anyway(net, caplog):
    """R3 HIGH 1 (variant). The hub holds ``complete``, sends a card void,
    then releases ``complete``: the void opens nothing, and a household
    that opened a round never takes its ``complete``."""
    caplog.set_level(logging.INFO)
    hold: list = []

    def drop_complete(s, t, p):
        if p.get("step") == "complete" and t != "hub":
            hold.append((s, t, p))
            return True
        return False

    sid = await _draw(net, drop=drop_complete)
    assert hold
    await _late_card_void(net, sid)
    await settle(net)
    assert "a void after the list" in caplog.text
    assert not sent_steps(net, "open")
    # Had a household opened this round, it would never take the complete.
    b = net.homes["b"]
    st_b = await b.draw._load(sid)
    st_b["p"]["void_answer"] = ["open", {"opening": {}}]
    await b.draw._save(sid, st_b)
    net.queue.extend(hold)
    await settle(net)
    st_b = await b.draw._load(sid)
    assert st_b["last"]["complete"] is False and "final" not in st_b
    for inst in ("c", "d"):
        assert (await net.homes[inst].draw._load(sid))["last"]["complete"], inst


PEOPLE_B = {"hub": ["anna"], "b": ["ben", "bea"], "c": ["cleo"], "d": ["dev"]}


async def test_hub_frames_an_honest_household_with_a_junk_opening(net, monkeypatch):
    """R3 HIGH 2. The hub says "stuck" at its own mixing turn, then relays
    a junk opening for c: an opening without its household's signature
    proves nothing — no household names c, none takes the restart."""
    real_on_mix = svc_mod.AppDrawService._on_mix
    real_mix = dc.mix
    done: dict = {}

    async def on_mix(self, table, st, rnd, p):
        if self._own == "hub" and not done:
            done["x"] = True

            def stuck(*a, **kw):
                raise dc.DrawCryptoError("lie")

            monkeypatch.setattr(dc, "mix", stuck)
            try:
                return await real_on_mix(self, table, st, rnd, p)
            finally:
                monkeypatch.setattr(dc, "mix", real_mix)
        return await real_on_mix(self, table, st, rnd, p)

    monkeypatch.setattr(svc_mod.AppDrawService, "_on_mix", on_mix)
    real_ev = svc_mod.AppDrawService._void_evidence

    def evidence(hub):
        ev = real_ev(hub)
        ev["openings"] = {**ev["openings"], "c": {"seeds": {}, "sig": "AA"}}
        return ev

    monkeypatch.setattr(
        svc_mod.AppDrawService, "_void_evidence", staticmethod(evidence)
    )
    sid = await draw_table(net, PEOPLE_B)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    assert done
    for inst in ("b", "c", "d"):
        st_i = await net.homes[inst].draw._load(sid)
        assert "c" not in st_i.get("blamed", []), inst
        assert st_i["p"]["round"] == 1, inst  # the framed restart refused


async def test_the_hub_drops_an_unsigned_opening(net, caplog, monkeypatch):
    """R3 HIGH 2. The hub checks an opening's signature before it keeps
    it: a junk one is dropped (the household shows as waited for)."""
    caplog.set_level(logging.INFO)

    def junk(s, _t, p):
        if s == "c" and p.get("step") == "open":
            p["opening"] = {"seeds": {}, "sig": b64url_encode(b"\x00" * 64)}
        return False

    real_mix = dc.mix

    def stuck_once(priv, entries, *, ctx, k, h, rand=os.urandom):
        if k == 1 and not getattr(stuck_once, "done", 0):
            stuck_once.done = 1  # type: ignore[attr-defined]
            raise dc.DrawCryptoError("seal doesn't open")
        return real_mix(priv, entries, ctx=ctx, k=k, h=h, rand=rand)

    monkeypatch.setattr(dc, "mix", stuck_once)
    sid = await _draw(net, drop=junk)
    st = await _hub_state(net, sid)
    assert "an opening without its household's word" in caplog.text
    assert st["hub"]["round"] == 1  # waits for c's real opening
    assert "c" not in st["hub"].get("opens", {})
    assert "c" not in st["hub"]["blamed"]


async def test_a_household_checks_its_own_opening_is_passed_on_unchanged(net):
    """R3 HIGH 2. A void's restart must carry this household's own opening
    exactly as it sent it."""
    sid = await _draw(net)
    b = net.homes["b"]
    st_b = copy.deepcopy(await b.draw._load(sid))
    st_b["last"]["complete"] = False
    rnd = _Round(st_b["p"])
    table = await b.group_repo.get(sid)
    sent = b.draw._opening(rnd, st_b["p"], rnd.t("chain"))
    st_b["p"]["void_answer"] = ["open", sent]
    other = b.draw._opening(rnd, st_b["p"], rnd.t("chain"), stuck=0)
    with pytest.raises(DrawWireError, match="not our opening"):
        await b.draw._justify(
            table,
            st_b,
            rnd.gen,
            rnd.round + 1,
            rnd.people,
            {"kind": "void", "why": "unusable", "openings": {"b": other["opening"]}},
        )


async def test_handover_chain_skips_a_still_seated_person(net):
    """R3 LOW 4. In X → L → M → W, L's household hands over the chain
    [L, M] while M is still seated (a lie: M never left): every key but the
    last must belong to someone gone, so the giver keeps it — and when L
    really leaves, X gives to M (not W, and M keeps a giver)."""
    sid = await _draw(net)
    before = await _results(net, sid)
    giver_of = {r: g for g, r in before.items()}
    where = {u: inst for inst, users in PEOPLE.items() for u in users}
    leaver = next(u for u in before if u != "anna" and giver_of[u] != "anna")
    x, m = giver_of[leaver], before[leaver]
    w = before[m]

    async def seed(user):
        st = await net.homes[where[user]].draw._load(sid)
        return b64url_decode(st["p"]["seeds"][user])

    l_keys = dc.person_keys(await seed(leaver))
    m_keys = dc.person_keys(await seed(m))
    st_l = await net.homes[where[leaver]].draw._load(sid)
    rnd = _Round(st_l["p"])
    C = [b64url_decode(c) for c in st_l["p"]["C"]]
    giver_box = C[(C.index(l_keys.box_pub) - 1) % len(C)]
    home = net.homes[where[leaver]]
    sig = sign_ed25519(
        home.fed.own_identity_seed,
        dc.handover_message(
            rnd.ctx, where[leaver], leaver, [l_keys.box_pub, m_keys.box_pub]
        ),
    )
    sealed = dc.seal_handover(
        giver_box,
        dc.encode_handover(
            where[leaver], leaver, [l_keys.box_priv, m_keys.box_priv], sig
        ),
        ctx=rnd.ctx,
    )
    net.forge(
        where[leaver],
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
    await home.groups.leave(app_id=APP, session_id=sid, actor_user_id=home.uid(leaver))
    await settle(net)
    still = {inst: [u for u in users if u != leaver] for inst, users in PEOPLE.items()}
    after = await _results(net, sid, {k: v for k, v in still.items() if v})
    assert after[x] == m != w
    _assert_circle(after)


def test_worst_case_draw_messages_fit_the_gfs_relay_cap():
    """R3 LOW 5. 30 people in 30 households, the longest names: the
    largest draw messages — the ``list`` with its per-entry chain, every
    card, a stuck mix's void (the batches and chain so far), and a
    restart / give-up (its proof is the complaint or the openings only:
    nothing every household already holds is sent again) — stay well under
    the GFS relay's envelope cap once sealed and base64'd."""

    def b(n: int) -> str:
        return b64url_encode(os.urandom(n))

    n = h = 30
    inst = [("x" * 60) + f"{i:04d}" for i in range(h)]
    user = [("u" * 60) + f"{i:04d}" for i in range(n)]
    people = [
        {"seat": i + 1, "instance": inst[i], "user": user[i], "name": "N" * 80}
        for i in range(n)
    ]
    batches = [
        {
            "instance": inst[i],
            "hashes": [b(32)],
            "commits": [[user[i], b(32)]],
            "r_commit": b(32),
            "sig": b(64),
        }
        for i in range(h)
    ]
    chain = [
        {"instance": inst[k], "hashes": [b(32) for _ in range(n)], "sig": b(64)}
        for k in range(h)
    ]
    head = {
        "app_id": "a" * 64,
        "session_id": "s" * 36,
        "verb": "engine",
        "engine": "draw",
        "engine_suite": dc.DRAW_SUITE_X25519_AESGCM,
        "gen": 11,
        "round": DRAW_ROUNDS_MAX,
    }
    seeds_opening = {
        "seeds": {user[0]: b(32)},
        "sig": b(64),
        "stuck": 29,
    }
    complaint = {
        "kind": "cards",
        "instance": inst[0],
        "user": user[0],
        "seed": b(32),
        "sig": b(64),
    }
    messages = {
        "list": {
            "step": "list",
            "S": [b(32) for _ in range(n)],
            "batches": batches,
            "chain": chain,
        },
        "cards_all": {"step": "cards_all", "cards": [b(1072) for _ in range(n)]},
        "void": {
            "step": "void",
            "why": "stuck",
            "k": 29,
            "claim": seeds_opening,
            "batches": batches,
            "chain": chain[:29],
        },
        "start (stuck void)": {
            "step": "start",
            "people": people,
            "identities": {i: "ab" * 32 for i in inst},
            "settings": {
                "name": "N" * 80,
                "budget": "B" * 40,
                "day": None,
                "pairs": [[1, 2]] * 10,
            },
            "because": {
                "kind": "void",
                "why": "stuck",
                "k": 29,
                "openings": {i: seeds_opening for i in inst},
                "batches": batches,
                "chain": chain[:29],
            },
        },
        "end (complaint)": {
            "step": "end",
            "reason": "rules",
            "people": people,
            "because": {"kind": "complaint", "complaint": complaint},
        },
        "mix k=0 (every onion)": {
            "step": "mix",
            "k": 0,
            "batches": [
                {**bt, "onions": [b(dc.onion_size(h))], "hashes": None}
                for bt in batches
            ],
        },
        "end (stuck void)": {
            "step": "end",
            "reason": "rules",
            "people": people,
            "because": {
                "kind": "void",
                "why": "stuck",
                "k": 29,
                "openings": {i: seeds_opening for i in inst},
                "batches": batches,
                "chain": chain[:29],
            },
        },
        "mix k>0 (list + every batch)": {
            "step": "mix",
            "k": 1,
            "list": [b(dc.onion_size(h - 1)) for _ in range(n)],
            "prev": {"instance": inst[0], "in": b(32), "sig": b(64)},
            "batches": batches,
        },
    }
    for name, body in messages.items():
        raw = len(orjson.dumps({**head, **body}))
        sealed_b64 = (raw + 512) * 4 // 3  # seal + envelope, base64
        assert sealed_b64 < RELAY_MAX_ENVELOPE_BYTES * 0.6, (name, sealed_b64)


# ─── draw-review-4 ────────────────────────────────────────────────────────

FAMILY = {
    "hub": ["h1"],
    "a": ["a1", "a2", "a3", "a4"],
    "b": ["b1"],
    "c": ["c1"],
    "d": ["d1"],
    "e": ["e1"],
}


async def test_household_with_adjacent_people_peeks_then_rerolls_unnamed(
    net, monkeypatch
):
    """R4 HIGH 2 under the owner's decision (review 5). Household a, when
    one of its people gives to another of its people, seals that card to a
    junk key, peeks at its cards and lets the "no card" complaint redraw the
    round. Nothing is opened after the list, so nobody is named — but every
    such redraw is visible and counted, and after DRAW_CARD_REDRAWS_MAX the
    draw stops (next test)."""
    armed: list = []
    real_make = svc_mod.AppDrawService._make_cards

    async def make(self, rnd, cur, C, wishes):
        if self._own != "a" or armed:
            return await real_make(self, rnd, cur, C, wishes)
        mine = {self._box_pub(cur, u) for u in cur["seeds"]}
        out = []
        for user in sorted(cur["seeds"]):
            j = C.index(self._box_pub(cur, user))
            to = C[dc.circle_targets(j, rnd.n)[1]]
            card = await self._card_for(rnd, user, wish="", rev=0)
            if to in mine and not armed:
                armed.append((rnd.gen, rnd.round))
                to = dc.new_keypair()[1]
            out.append(dc.seal_card(to, card, ctx=rnd.ctx))
        return out

    monkeypatch.setattr(svc_mod.AppDrawService, "_make_cards", make)
    sid = await draw_table(net, FAMILY)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("h1"))
    await settle(net)
    for _ in range(9):
        if armed:
            break
        await hub.draw.start(
            app_id=APP, session_id=sid, actor_user_id=hub.uid("h1"), again=True
        )
        await settle(net)
    assert armed
    gen, rnd_no = armed[0]
    redo = [
        p
        for p in sent_steps(net, "start")
        if (p["gen"], p["round"]) == (gen, rnd_no + 1)
    ]
    assert redo and redo[0]["because"]["complaint"]["kind"] == "cards"
    assert not sent_steps(net, "open")
    for inst in FAMILY:
        s_i = await net.homes[inst].draw._load(sid)
        assert not s_i.get("blamed") and s_i["last"]["complete"], inst
        assert s_i["card_redraws"] == 1, inst


SMALL = {"hub": ["anna"], "b": ["ben"], "c": ["cleo"], "d": ["dev"]}


async def test_one_household_kills_every_round_unnamed(net, monkeypatch):
    """R4 MED 3 under the owner's decision. Household c seals its card to a
    fresh junk key every round: each card complaint is a visible redraw
    naming nobody; after DRAW_CARD_REDRAWS_MAX the draw stops with a plain
    state ("Someone's home kept breaking the draw") that every household
    checked itself — not 40 rounds."""
    real_make = svc_mod.AppDrawService._make_cards

    async def make(self, rnd, cur, C, wishes):
        if self._own != "c":
            return await real_make(self, rnd, cur, C, wishes)
        card = await self._card_for(rnd, "cleo", wish="", rev=0)
        return [dc.seal_card(dc.new_keypair()[1], card, ctx=rnd.ctx)]

    monkeypatch.setattr(svc_mod.AppDrawService, "_make_cards", make)
    seen: dict = {}
    real_c = svc_mod.AppDrawService._hub_complaint

    async def spy(self, table, st, rnd, sender, step, p):
        seen["complaint"] = p.get("complaint")
        return await real_c(self, table, st, rnd, sender, step, p)

    monkeypatch.setattr(svc_mod.AppDrawService, "_hub_complaint", spy)
    sid = await draw_table(net, SMALL)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    st = await _hub_state(net, sid)
    assert (st["hub"]["phase"], st["hub"]["ended"]) == ("ended", "broken")
    assert st["hub"]["round"] == DRAW_CARD_REDRAWS_MAX + 1
    for inst in SMALL:
        s_i = await net.homes[inst].draw._load(sid)
        assert s_i.get("ended") == "broken" and not s_i.get("blamed"), inst
        home = net.homes[inst]
        status = await home.draw.status(
            app_id=APP, session_id=sid, actor_user_id=home.uid(SMALL[inst][0])
        )
        assert (status["state"], status["reason"]) == ("failed", "broken")
    # A hub that starts another round anyway: no household takes it.
    async with hub.draw._lock(sid):
        st = await hub.draw._load(sid)
        last = st["hub"]
        last.update(phase="opening")
        table = await hub.group_repo.get(sid)
        rnd = _Round(last)
        fake = {"kind": "complaint", "complaint": seen["complaint"]}
        await hub.draw._next_round(
            table, st, people=rnd.people, names=rnd.names, because=fake
        )
    await settle(net)
    for inst in ("b", "c", "d"):
        s_i = await net.homes[inst].draw._load(sid)
        assert s_i["p"]["round"] == DRAW_CARD_REDRAWS_MAX + 1, inst


async def test_a_removal_after_the_cards_counts_as_a_draw_again(net, monkeypatch):
    """R4 LOW 5. The organiser removes a household once the circle is known
    (the cards are out; people may have read theirs): the fresh round is a
    new draw — counted as a Draw again, visible to all."""

    def hold_d(s, t, p):
        return "d" in (s, t) and p.get("step") in ("done", "cards_all", "complete")

    sid = await _draw(net, drop=hold_d)
    st = await _hub_state(net, sid)
    assert st["hub"]["phase"] == "opening"
    hub = net.homes["hub"]
    await hub.groups.remove(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), seat=5
    )
    await settle(net, drop=lambda s, t, p: "d" in (s, t))
    starts = [p for p in sent_steps(net, "start") if p["because"]["kind"] == "changed"]
    assert starts and {(p["gen"], p["round"]) for p in starts} == {(2, 1)}
    left = {k: v for k, v in PEOPLE.items() if k != "d"}
    _assert_circle(await _results(net, sid, left))
    status = await hub.draw.status(
        app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
    )
    assert status["again"] == 1


# ─── Accepted limit (owner decision 2026-11-01): a household's server ────
# These tests PIN a known, documented limit (docs/protocol/app-engines.md,
# "What holds"): from its own people's results and their circle positions,
# a household's server works out some pairs touching its own people, and
# colluding households pool what they know. They pass while the limit
# stands; a change that closes it will fail them on purpose.

LIMIT_PEOPLE = {
    "hub": ["anna", "alf", "amy", "abe"],
    "b": ["ben", "bea"],
    "c": ["cleo"],
    "d": ["dev"],
    "e": ["eve"],
}


def _infer(st: dict, inst: str, extra: list | None = None) -> dict:
    """The pairs with a giver in another household that household *inst*
    (pooling with *extra* states) works out from its own stored state."""
    known: dict[int, tuple[str, str]] = {}
    for st_i, inst_i in [(st, inst), *(extra or [])]:
        cur = st_i["p"]
        C = [svc_mod._raw(x) for x in cur["C"]]
        n = len(C)
        for u in cur["seeds"]:
            j = C.index(svc_mod.AppDrawService._box_pub(cur, u))
            known[j] = (inst_i, u)
            card = dc.decode_card(svc_mod._raw(st_i["final"]["results"][u]["card"]))
            known[(j + 1) % n] = (card.instance, card.user)
    mine = {inst} | {i for _s, i in (extra or [])}
    return {
        who: known[(j + 1) % n]
        for j, who in known.items()
        if who[0] not in mine and (j + 1) % n in known
    }


async def _truth(net, sid, people) -> dict:
    out = {}
    for inst, users in people.items():
        st = await net.homes[inst].draw._load(sid)
        for u in users:
            card = dc.decode_card(svc_mod._raw(st["final"]["results"][u]["card"]))
            out[(inst, u)] = (card.instance, card.user)
    return out


async def _draw_until(net, sid, people, found) -> tuple[dict, dict]:
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    for _ in range(10):
        sts = {i: await net.homes[i].draw._load(sid) for i in people}
        if found(sts):
            break
        await hub.draw.start(
            app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), again=True
        )
        await settle(net)
    return sts, await _truth(net, sid, people)


async def test_accepted_limit_a_household_learns_who_gives_to_its_own_person(net):
    sid = await draw_table(net, LIMIT_PEOPLE)
    sts, truth = await _draw_until(
        net, sid, LIMIT_PEOPLE, lambda sts: any(_infer(s, i) for i, s in sts.items())
    )
    total = 0
    for inst, st in sts.items():
        for giver, rec in _infer(st, inst).items():
            assert truth[giver] == rec and giver[0] != inst
            total += 1
    assert total >= 1


async def test_accepted_limit_colluding_households_pool_pairs(net):
    sid = await draw_table(net, LIMIT_PEOPLE)
    single = [i for i, users in LIMIT_PEOPLE.items() if len(users) == 1]

    def pooled(sts):
        out = {}
        for a in single:
            for b in single:
                if a < b:
                    got = _infer(sts[a], a, [(sts[b], b)])
                    alone = {**_infer(sts[a], a), **_infer(sts[b], b)}
                    out.update({g: r for g, r in got.items() if g not in alone})
        return out

    sts, truth = await _draw_until(net, sid, LIMIT_PEOPLE, pooled)
    extra = pooled(sts)
    assert extra and all(truth[g] == r for g, r in extra.items())


def test_accepted_limit_how_often_a_household_learns_a_pair():
    """The chance (circle uniform) that a household with m of n people
    learns at least one pair whose giver is elsewhere — the table in
    docs/protocol/app-engines.md."""
    rng = random.Random(4)
    want = {
        (6, 2): 0.40,
        (8, 3): 0.57,
        (12, 4): 0.64,
        (12, 2): 0.18,
        (20, 4): 0.47,
        (30, 5): 0.50,
    }
    for (n, m), p in want.items():
        hits = 0
        T = 20000
        for _ in range(T):
            own = set(rng.sample(range(n), m))
            if any((j + 2) % n in own and (j + 1) % n not in own for j in own):
                hits += 1
        assert abs(hits / T - p) < 0.03, (n, m, hits / T)


# ─── draw-review-5 (owner: simplify — nothing opened after the list) ─────


async def test_hub_splits_void_and_complete_and_learns_victims_final_pairs(net, caplog):
    """R5 HIGH 1. The hub holds ``complete`` back, sends it to one household
    and a card void (with a real complaint of its own person) to the others,
    hoping they open where they sealed their cards. There is no such void:
    after the list nothing is ever opened (and never after one's own
    ``done``) — no household opens anything; the round stays as it was."""
    caplog.set_level(logging.INFO)
    hold: list = []

    def drop_complete(s, t, p):
        if p.get("step") == "complete" and t != "hub":
            hold.append((s, t, p))
            return True
        return False

    sid = await _draw(net, drop=drop_complete)
    hub = net.homes["hub"]
    st = await hub.draw._load(sid)
    rnd = _Round(st["p"])
    complaint = hub.draw._complaint(rnd, st["p"], "cards", "anna")["complaint"]
    for to in ("b", "d"):
        for why in ("cards", "unusable", "stuck"):
            net.forge(
                "hub",
                to,
                engine_step(
                    sid,
                    "void",
                    gen=rnd.gen,
                    round_no=rnd.round,
                    why=why,
                    complaint=complaint,
                    k=0,
                ),
            )
    net.queue.extend(x for x in hold if x[1] == "c")
    await settle(net)
    assert "a void after the list" in caplog.text
    assert not sent_steps(net, "open")
    assert (await net.homes["c"].draw._load(sid))["last"]["complete"]


async def test_hub_equivocates_its_r_commit_and_frames_an_honest_household(
    net, monkeypatch
):
    """R5 HIGH 2. The hub signs a second batch with another ``r`` commitment
    and hands it (and the matching ``r``) to household c only, so c draws
    another circle. Card commitments and ``done`` carry the circle each
    household drew: c's commitment doesn't match the round's — the round
    stalls; nobody is named, nothing is opened."""
    r_alt = b"\x07" * dc.SEED_BYTES
    box: dict = {}
    real_send = svc_mod.AppDrawService._send
    real_to_all = svc_mod.AppDrawService._to_all

    async def send(self, table, to, step, body):
        if self._own == "hub" and to == "c" and step in ("list", "reveals"):
            body = copy.deepcopy(body)
            ctx = box["ctx"]
            if step == "list":
                for b in body["batches"]:
                    if b["instance"] == "hub":
                        rc = dc.r_commitment(ctx, "hub", r_alt)
                        b["r_commit"] = svc_mod._b64(rc)
                        hs = [svc_mod._raw(h) for h in b["hashes"]]
                        cm = [(u, svc_mod._raw(c)) for u, c in b["commits"]]
                        b["sig"] = self._sign(dc.batch_message(ctx, "hub", hs, cm, rc))
            else:
                body["reveals"]["hub"] = svc_mod._b64(r_alt)
        return await real_send(self, table, to, step, body)

    async def to_all(self, table, rnd, step, body):
        box["ctx"] = rnd.ctx
        return await real_to_all(self, table, rnd, step, body)

    monkeypatch.setattr(svc_mod.AppDrawService, "_send", send)
    monkeypatch.setattr(svc_mod.AppDrawService, "_to_all", to_all)
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    st = await _hub_state(net, sid)
    # (c's copy of the batches differs: the mixers' signatures over the
    # transcript don't verify there — c stalls at the list.)
    assert st["hub"]["phase"] in ("reveal", "commit")
    assert "c" not in st["hub"]["reveals"] and "c" not in st["hub"]["commits"]
    assert not sent_steps(net, "void") and not sent_steps(net, "open")
    for inst in PEOPLE:
        s_i = await net.homes[inst].draw._load(sid)
        assert not s_i.get("blamed") and not s_i.get("broke"), inst


async def test_hub_peeks_at_held_cards_then_drops_a_household_without_draw_again(
    net,
):
    """R5 LOW 3. The hub holds every sealed card before anyone else, keeps
    ``cards_all`` back, and — a whole household gone — sends a same-draw
    "changed" round. Every household that committed its cards (the circle
    is known) takes a removal only as a Draw again: nobody starts it."""
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    no_cards = lambda s, t, p: p.get("step") == "cards_all"
    await settle(net, drop=no_cards)
    st = await _hub_state(net, sid)
    assert st["hub"].get("all_cards") and st["hub"]["phase"] == "opening"
    d = net.homes["d"]
    await d.groups.leave(app_id=APP, session_id=sid, actor_user_id=d.uid("dev"))
    await settle(net, drop=lambda s, t, p: p.get("step") in ("cards_all", "start"))
    hubst = st["hub"]
    people = [p for p in hubst["people"] if p["instance"] != "d"]
    for to in ("b", "c"):
        net.forge(
            "hub",
            to,
            engine_step(
                sid,
                "start",
                gen=hubst["gen"],
                round_no=hubst["round"] + 1,
                people=people,
                identities={k: v for k, v in hubst["identities"].items() if k != "d"},
                settings=hubst["settings"],
                because={"kind": "changed"},
            ),
        )
    await settle(net, drop=no_cards)
    for inst in ("b", "c"):
        s_i = await net.homes[inst].draw._load(sid)
        assert (s_i["p"]["gen"], s_i["p"]["round"]) == (1, 1), inst


# ─── draw-review-6: the round's transcript closes equivocation ───────────


def _equivocate_mixer_key(monkeypatch, plan, *, victims=None):
    """The hub signs a DIFFERENT mixer key of its own for each other
    household (or only for *victims*)."""
    real_to_all = svc_mod.AppDrawService._to_all

    async def to_all(self, table, rnd, step, body):
        if self._own != "hub" or step != "mixers":
            return await real_to_all(self, table, rnd, step, body)
        st = await self._load(table.session_id)
        plan["hub_priv"] = svc_mod._raw(st["p"]["mixer_priv"])
        plan["privs"] = {}
        slot = rnd.order.index("hub")
        for inst in rnd.order:
            keys = [dict(k) for k in body["keys"]]
            if inst != "hub" and (victims is None or inst in victims):
                priv, pub = dc.new_keypair()
                plan["privs"][inst] = priv
                keys[slot]["pub"] = svc_mod._b64(pub)
                keys[slot]["sig"] = self._sign(dc.key_message(rnd.ctx, "hub", pub))
            await self._send(table, inst, step, self._rid(rnd, keys=keys))

    monkeypatch.setattr(svc_mod.AppDrawService, "_to_all", to_all)


async def _nobody_named_and_nothing_opened(net, sid, people) -> None:
    for inst in people:
        s_i = await net.homes[inst].draw._load(sid)
        assert not s_i.get("blamed") and not s_i.get("broke"), inst
        assert not (s_i.get("last") or {}).get("complete"), inst
    st = await _hub_state(net, sid)
    assert st["hub"]["blamed"] == [] and st["hub"]["phase"] != "held"
    assert not sent_steps(net, "open")


async def test_hub_equivocates_its_mixer_key_and_learns_every_final_pair(
    net, monkeypatch
):
    """R6 HIGH 1 (privacy). The hub hands each household its own hub mixer
    key, hoping — as the last mixer — to tell each entry's household. Every
    batch signs the transcript of the mixer keys as ITS household holds
    them: the first mixer (and every household at the list) finds batches
    signed over other keys — the round stalls before any ``r`` is out, so
    no circle exists to map; nobody is named, nothing is opened."""
    plan: dict = {}
    _equivocate_mixer_key(monkeypatch, plan)
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    st = await _hub_state(net, sid)
    assert st["hub"]["phase"] in ("onions", "mixing")
    assert not sent_steps(net, "list") and not sent_steps(net, "reveal")
    await _nobody_named_and_nothing_opened(net, sid, PEOPLE)


async def test_hub_equivocates_its_mixer_key_and_frames_an_honest_household(
    net, monkeypatch
):
    """R6 HIGH 1 (framing). The hub gives only household c another hub key,
    then says "stuck" at its own mixing turn. c's batch was signed over c's
    transcript of the keys: it doesn't verify on anyone else's — the round
    stalls; c is never named."""
    plan: dict = {}
    _equivocate_mixer_key(monkeypatch, plan, victims={"c"})
    real_mix = dc.mix

    def mix(mixer_priv, entries, **kw):
        if mixer_priv == plan.get("hub_priv"):
            raise dc.DrawCryptoError("hub says: stuck")
        return real_mix(mixer_priv, entries, **kw)

    monkeypatch.setattr(dc, "mix", mix)
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    await _nobody_named_and_nothing_opened(net, sid, PEOPLE)


def _equivocator(monkeypatch, what: str) -> None:
    """The hub hands household c — only c — another value of the round's
    public data, re-signing what it can sign itself (its own key, its own
    batch, its own link)."""
    real_send = svc_mod.AppDrawService._send

    async def send(self, table, to, step, body):
        if self._own != "hub" or to != "c":
            return await real_send(self, table, to, step, body)
        body = copy.deepcopy(body)
        hub_st = (await self._load(table.session_id))["hub"]
        ctx = svc_mod._raw(hub_st["ctx"])
        rnd = _Round(hub_st)
        if what == "people" and step == "start":
            body["settings"] = {**body["settings"], "name": "Another draw"}
        elif what == "mixer key" and step == "mixers":
            slot = rnd.order.index("hub")
            _priv, pub = dc.new_keypair()
            body["keys"][slot]["pub"] = svc_mod._b64(pub)
            body["keys"][slot]["sig"] = self._sign(dc.key_message(ctx, "hub", pub))
        elif what in ("r_commit", "batch") and step == "list":
            for b in body["batches"]:
                if b["instance"] == "hub":
                    if what == "r_commit":
                        b["r_commit"] = svc_mod._b64(
                            dc.r_commitment(ctx, "hub", b"\x07" * 32)
                        )
                    else:
                        b["commits"] = [
                            [u, svc_mod._b64(b"\x05" * 32)] for u, _c in b["commits"]
                        ]
                    b["sig"] = self._sign(
                        dc.batch_message(
                            rnd.t("keys"),
                            "hub",
                            [svc_mod._raw(x) for x in b["hashes"]],
                            [(u, svc_mod._raw(c)) for u, c in b["commits"]],
                            svc_mod._raw(b["r_commit"]),
                        )
                    )
        elif what == "chain link" and step == "list":
            k = rnd.order.index("hub")
            link = body["chain"][k]
            link["hashes"] = list(reversed(link["hashes"]))
            prev = (
                dc.list_digest(
                    [svc_mod._raw(x) for x in body["chain"][k - 1]["hashes"]]
                )
                if k
                else dc.list_digest(
                    [svc_mod._raw(x) for bt in body["batches"] for x in bt["hashes"]]
                )
            )
            out = dc.list_digest([svc_mod._raw(x) for x in link["hashes"]])
            link["sig"] = self._sign(
                dc.mix_message(rnd.t("batches"), k, "hub", prev, out)
            )
        elif what == "reveal" and step == "reveals":
            body["reveals"]["hub"] = svc_mod._b64(b"\x07" * 32)
        return await real_send(self, table, to, step, body)

    monkeypatch.setattr(svc_mod.AppDrawService, "_send", send)


@pytest.mark.parametrize(
    "what", ["people", "mixer key", "r_commit", "batch", "chain link", "reveal"]
)
async def test_any_equivocation_stalls_and_names_nobody(net, monkeypatch, what):
    """R6 (the whole class). Whatever public value of the round the hub
    hands one household differently, that household's transcript differs:
    the signatures it checks — or the ones checked against it — don't
    verify. The round stalls; nobody is named; nothing is opened; no
    household gets a result."""
    _equivocator(monkeypatch, what)
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    await _nobody_named_and_nothing_opened(net, sid, PEOPLE)
    assert "results" not in (await net.homes["c"].draw._load(sid)).get("p", {})


async def test_couple_household_forces_uncounted_partner_redraws(net, monkeypatch):
    """R6 HIGH/MED 2. Household b holds a couple (Ben, Bea) and Bob. When
    Ben gives to Bob, b seals Bea's card (its own, valid) to Ben's box
    instead of Bob's, so Ben "drew his partner". A couple within one
    household is told before any card exists; after the cards a partner
    complaint about a card the complainer's own household signed proves
    nothing — refused, and b is named to the organiser; no redraw."""
    people = {
        "hub": ["anna"],
        "b": ["ben", "bea", "bob"],
        "c": ["cleo"],
        "d": ["dev"],
    }
    forced: list = []
    real_make = svc_mod.AppDrawService._make_cards

    async def make(self, rnd, cur, C, wishes):
        if self._own != "b" or forced:
            return await real_make(self, rnd, cur, C, wishes)
        n = rnd.n
        pos = {u: C.index(self._box_pub(cur, u)) for u in cur["seeds"]}
        out = []
        for user in sorted(cur["seeds"]):
            _g, sealed_to = dc.circle_targets(pos[user], n)
            who = user
            if user == "bob" and (pos["bob"] - pos["ben"]) % n == 1:
                who = "bea"  # Bob's giver (Ben) gets Bea's card
                forced.append(rnd.round)
            card = await self._card_for(rnd, who, wish="", rev=0)
            out.append(dc.seal_card(C[sealed_to], card, ctx=rnd.ctx))
        return out

    monkeypatch.setattr(svc_mod.AppDrawService, "_make_cards", make)
    sid = await draw_table(net, people)
    hub = net.homes["hub"]
    seats = {e.user: e.seat for e in (await hub.group_repo.get(sid)).roster}
    await hub.draw.configure(
        app_id=APP,
        session_id=sid,
        actor_user_id=hub.uid("anna"),
        settings={"pairs": [[seats["ben"], seats["bea"]]]},
    )
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    for _ in range(10):
        if forced:
            break
        await hub.draw.start(
            app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), again=True
        )
        await settle(net)
    assert forced
    st = await _hub_state(net, sid)
    partner_redos = [
        p for p in sent_steps(net, "redo") if p["complaint"]["kind"] == "partner"
    ]
    assert partner_redos and st["hub"]["blamed"] == ["b"]
    assert st["hub"]["phase"] == "opening"  # no redraw: the round waits
    assert not [
        p
        for p in sent_steps(net, "start")
        if p["round"] == forced[0] + 1
        and p["because"]["kind"] == "complaint"
        and p["because"]["complaint"]["kind"] == "partner"
    ]


async def test_a_couple_in_one_household_is_told_before_any_card(net):
    """R6 HIGH/MED 2. Ben and Bea (household b) may not draw each other:
    when the circle puts them side by side, b says so right after the
    reveal — with both seeds — before any card exists; the redraw is
    counted with the pairs' redraws."""
    seen = 0
    sid = await draw_table(net, PEOPLE)
    hub = net.homes["hub"]
    for attempt in range(12):
        if attempt == 0:
            await hub.draw.configure(
                app_id=APP,
                session_id=sid,
                actor_user_id=hub.uid("anna"),
                settings={"pairs": [[2, 3]]},
            )
            await hub.draw.start(
                app_id=APP, session_id=sid, actor_user_id=hub.uid("anna")
            )
        else:
            await hub.draw.start(
                app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), again=True
            )
        await settle(net)
        couples = [
            p for p in sent_steps(net, "redo") if p["complaint"]["kind"] == "couple"
        ]
        if couples:
            seen = len(couples)
            break
    assert seen
    c = couples[0]
    # Told instead of the commitment: b had sealed no card for that round.
    assert not [
        p
        for s, _t, p in net.sent
        if s == "b"
        and p.get("step") == "commit"
        and (p["gen"], p["round"]) == (c["gen"], c["round"])
    ]
    st = await _hub_state(net, sid)
    assert st["hub"]["blamed"] == []
    _assert_circle(await _results(net, sid))


# ─── draw-review-7 ────────────────────────────────────────────────────────


async def test_hub_peeks_at_cards_then_rerolls_with_its_own_couple(net, monkeypatch):
    """R7 MEDIUM. The hub's household holds a couple (Anna, Amy). When the
    circle puts them side by side it keeps quiet, commits its cards like
    everyone, opens its own people's cards from what the others sent (the
    peek), withholds ``cards_all`` and restarts with the couple complaint.
    A couple is told INSTEAD of committing: no household that holds every
    card commitment (the complainer's own among them) takes it."""
    people = {"hub": ["anna", "amy"], "b": ["ben"], "c": ["cleo"], "d": ["dev"]}
    plan: dict = {"peeked": []}
    real_couple = svc_mod.AppDrawService._own_couple
    real_cards = svc_mod.AppDrawService._hub_cards

    def own_couple(self, rnd, cur, C):
        got = real_couple(self, rnd, cur, C)
        if self._own == "hub" and got is not None and "couple" not in plan:
            plan["couple"] = (rnd.gen, rnd.round, got)
            return None  # keep quiet: commit cards as if nothing
        return got

    async def hub_cards(self, table, st, rnd, sender, p):
        hub = st["hub"]
        c = plan.get("couple")
        if c is None or (c[0], c[1]) != (rnd.gen, rnd.round) or plan.get("done"):
            return await real_cards(self, table, st, rnd, sender, p)
        hub["cards"][sender] = p["cards"]
        if len(hub["cards"]) < rnd.h:
            await self._save(table.session_id, st)
            return
        every = [svc_mod._raw(x) for cs in hub["cards"].values() for x in cs]
        cur = st["p"]
        for user in cur["seeds"]:  # the peek: our people's recipients
            got = dc.try_cards(self._box_priv(cur, user), every, ctx=rnd.ctx)
            plan["peeked"].append((user, dc.decode_card(got[0]).user))
        plan["done"] = True
        msg = self._complaint(rnd, cur, "couple", *c[2])
        await self._redraw(
            table, st, rnd, {"kind": "complaint", "complaint": msg["complaint"]}, []
        )

    monkeypatch.setattr(svc_mod.AppDrawService, "_own_couple", own_couple)
    monkeypatch.setattr(svc_mod.AppDrawService, "_hub_cards", hub_cards)
    sid = await draw_table(net, people)
    hub = net.homes["hub"]
    seats = {e.user: e.seat for e in (await hub.group_repo.get(sid)).roster}
    await hub.draw.configure(
        app_id=APP,
        session_id=sid,
        actor_user_id=hub.uid("anna"),
        settings={"pairs": [[seats["anna"], seats["amy"]]]},
    )
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    for _ in range(10):
        if plan.get("done"):
            break
        await hub.draw.start(
            app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), again=True
        )
        await settle(net)
    assert plan.get("done") and len(plan["peeked"]) == 2
    gen, rno, _c = plan["couple"]
    for inst in ("b", "c", "d"):
        assert not [
            p
            for s, _t, p in net.sent
            if s == inst
            and p.get("step") == "key"
            and (p["gen"], p["round"]) == (gen, rno + 1)
        ], inst  # nobody took the re-roll
        st = await net.homes[inst].draw._load(sid)
        assert (st["p"]["gen"], st["p"]["round"]) == (gen, rno), inst


async def test_hub_changes_the_settings_mid_draw(net, monkeypatch):
    """R7 LOW. The organiser can't change the settings while a draw runs,
    and neither can the hub: the next round of the same draw must carry the
    settings of the one before (the pairs only for the seats still in).
    Here the hub drops the pair and changes the budget and day at a redraw:
    no household takes it."""
    people = {"hub": ["anna"], "b": ["ben", "bea"], "c": ["cleo"], "d": ["dev"]}
    real_redraw = svc_mod.AppDrawService._redraw
    tampered: list = []

    async def redraw(self, table, st, rnd, because, culprits):
        if self._own == "hub" and not tampered:
            st["settings"] = {
                "name": "x",
                "budget": "999 EUR",
                "day": "2099-12-24",
                "pairs": [],
            }
            tampered.append((rnd.gen, rnd.round))
        return await real_redraw(self, table, st, rnd, because, culprits)

    monkeypatch.setattr(svc_mod.AppDrawService, "_redraw", redraw)
    sid = await draw_table(net, people)
    hub = net.homes["hub"]
    seats = {e.user: e.seat for e in (await hub.group_repo.get(sid)).roster}
    await hub.draw.configure(
        app_id=APP,
        session_id=sid,
        actor_user_id=hub.uid("anna"),
        settings={"pairs": [[seats["ben"], seats["bea"]]], "budget": "20 EUR"},
    )
    await hub.draw.start(app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"))
    await settle(net)
    for _ in range(10):
        if tampered:
            break
        await hub.draw.start(
            app_id=APP, session_id=sid, actor_user_id=hub.uid("anna"), again=True
        )
        await settle(net)
    assert tampered
    gen, rno = tampered[0]
    for inst in ("b", "c", "d"):
        st = await net.homes[inst].draw._load(sid)
        assert (st["p"]["gen"], st["p"]["round"]) == (gen, rno), inst
        assert st["p"]["settings"]["budget"] == "20 EUR", inst
        assert not st["last"]["complete"], inst


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
                # Ben and Bea are both b's: told before any card exists.
                assert p["complaint"]["kind"] == "couple"
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
