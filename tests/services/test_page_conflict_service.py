"""Tests for host-sequenced space pages (§4.4.4.1, v_48): the bounded
paragraph diff3, the host sequencer, the member mirror, the wire parsers,
and conflict resolution bodies."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    PageConflictEmitted,
    PageCreated,
    PageProposalSettled,
    PageUpdated,
)
from socialhome.domain.page import PageVersion
from socialhome.domain.page_version import PageConflictSide, version_hash
from socialhome.repositories.page_repo import (
    PageNotFoundError,
    SqlitePageRepo,
    new_page,
)
from socialhome.services import page_conflict_service as mod
from socialhome.services.page_conflict_service import (
    MAX_CONFLICT_SIDES,
    CanonicalVersion,
    PageConflictService,
    PageConflictStaleError,
    PageMode,
    Proposal,
    Sequenced,
    SequenceOutcome,
    canonical_extras,
    canonical_from_wire,
    diff3_merge,
    proposal_from_wire,
    side_to_wire,
)


def _body(*paras: str) -> str:
    return "\n\n".join(paras)


# ─── Bounded diff3 ───────────────────────────────────────────────────────


def test_diff3_trivial_cases():
    assert diff3_merge("a", "a", "a") == mod.MergeResult("a", False)
    assert diff3_merge("a", "b", "a").content == "b"
    assert diff3_merge("a", "a", "b").content == "b"
    assert diff3_merge("a", "x", "x").content == "x"


def test_diff3_top_insert_and_bottom_edit_is_clean():
    base = _body("p1", "p2", "p3")
    r = diff3_merge(base, _body("intro", "p1", "p2", "p3"), _body("p1", "p2", "p3x"))
    assert not r.has_conflict
    assert r.content == _body("intro", "p1", "p2", "p3x")


def test_diff3_disjoint_deletions_keep_the_middle():
    base = _body("p1", "p2", "p3")
    r = diff3_merge(base, _body("p2", "p3"), _body("p1", "p2"))
    assert (r.content, r.has_conflict) == ("p2", False)


def test_diff3_same_point_insertions_go_current_first():
    base = _body("p1", "p2")
    r = diff3_merge(base, _body("p1", "cur", "p2"), _body("p1", "prop", "p2"))
    assert r.content == _body("p1", "cur", "prop", "p2")
    r = diff3_merge(base, _body("p1", "p2", "cur"), _body("p1", "p2", "prop"))
    assert r.content == _body("p1", "p2", "cur", "prop")


def test_diff3_same_paragraph_changed_differently_conflicts():
    r = diff3_merge(_body("p1", "p2"), _body("a", "p2"), _body("b", "p2"))
    assert r.has_conflict and r.content == ""


def test_diff3_deletion_vs_edit_conflicts():
    assert diff3_merge(_body("k", "d"), "k", _body("k", "d2")).has_conflict


def test_diff3_identical_overlapping_change_once():
    base = _body("p1", "p2")
    r = diff3_merge(base, _body("x", "p2", "c"), _body("x", "p2"))
    assert r.content == _body("x", "p2", "c")


def test_diff3_caps(monkeypatch):
    monkeypatch.setattr(mod, "MERGE_MAX_BYTES", 10)
    assert diff3_merge("p1", _body("p1", "a"), _body("b", "p1") + "x" * 20).has_conflict
    monkeypatch.setattr(mod, "MERGE_MAX_BYTES", 128 * 1024)
    monkeypatch.setattr(mod, "MERGE_MAX_PARAGRAPHS", 2)
    assert diff3_merge(
        _body("p1", "p2"), _body("x", "p1", "p2"), _body("p1", "p2", "y")
    ).has_conflict


def test_diff3_over_the_ops_budget_conflicts(monkeypatch):
    monkeypatch.setattr(mod, "OPS_BUDGET", 50)
    base = _body(*[f"p{i}" for i in range(40)])
    cur = _body(*[f"c{i}" if i % 2 else f"p{i}" for i in range(40)])
    prop = _body(*[f"p{i}" for i in range(40)], "tail")
    assert diff3_merge(base, cur, prop).has_conflict


def test_myers_matches_a_known_script():
    moves = mod._myers_moves([1, 2, 3], [1, 3, 4], 1000)
    assert moves.count("=") == 2
    assert moves.count("-") == 1 and moves.count("+") == 1


def test_bounded_hunks_cover_every_change():
    ids: dict[str, int] = {}
    base = ["a", "b", "c", "d"]
    other = ["a", "x", "c", "d", "e"]
    assert mod._bounded_hunks(base, other, ids) == [(1, 2, ("x",)), (4, 4, ("e",))]


async def test_the_adversarial_merge_is_fast_and_never_stalls_the_loop():
    """CPU DoS (probe14 C2): ``["a"]*2000`` vs ``["a","b"]*1000`` finishes
    in < 250 ms, and the event loop never lags > 100 ms meanwhile."""
    base = _body(*(["a"] * 2000))
    theirs = _body(*(["a", "b"] * 1000))
    mine = _body(*(["z"] + ["a"] * 1999))
    # Within every size cap: it is the diff budget that must stop it.
    assert max(len(x.split("\n\n")) for x in (base, theirs, mine)) == 2000
    with pytest.raises(mod._OverBudget):
        mod._bounded_hunks(base.split("\n\n"), theirs.split("\n\n"), {})
    stop = asyncio.Event()

    async def ticker() -> float:
        worst = 0.0
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.001)
            now = time.perf_counter()
            worst = max(worst, now - last)
            last = now
        return worst

    tick = asyncio.create_task(ticker())
    await asyncio.sleep(0.01)
    started = time.perf_counter()
    result = await asyncio.to_thread(diff3_merge, base, mine, theirs)
    elapsed = time.perf_counter() - started
    stop.set()
    worst = await tick
    assert result.has_conflict  # over budget → a conflict side, not a merge
    assert elapsed < 0.25, elapsed
    assert worst < 0.1, worst


# ─── Wire parsers ────────────────────────────────────────────────────────

_H = "sha256:" + "a" * 64


def test_proposal_from_wire():
    p = proposal_from_wire(
        {
            "title": "T",
            "content": "c",
            "base_seq": 3,
            "base_hash": _H,
            "resolves": [_H, _H],
        }
    )
    assert p is not None and (p.base_seq, p.base_hash, p.resolves) == (3, _H, (_H,))
    legacy = proposal_from_wire({"title": "T", "content": "c"})
    assert legacy is not None and legacy.base_seq is None
    for bad in (
        {"base_seq": -1},
        {"base_seq": "3"},
        {"base_seq": True},
        {"base_seq": 1, "base_hash": "nope"},
        {"resolves": "x"},
        {"resolves": [_H] * 6},
        {"resolves": ["nope"]},
    ):
        assert proposal_from_wire({"title": "T", "content": "c", **bad}) is None, bad


def test_canonical_round_trip():
    side = PageConflictSide(
        hash=version_hash("T", "s"),
        title="T",
        content="s",
        by="u2",
        at="t1",
        base_seq=2,
    )
    page = replace(
        new_page(title="T", content="c", created_by="u1", space_id="sp"), seq=5
    )
    seq_ = Sequenced(proposer_instance="iid", proposal_hash=_H, outcome="applied")
    wire = {"title": "T", "content": "c", **canonical_extras(page, [side], seq_)}
    v = canonical_from_wire(wire)
    assert v is not None
    assert v.seq == 5 and v.sequenced == seq_ and v.has_state
    assert v.conflict == (side,)
    assert side_to_wire(side)["side_id"] == side.hash


@pytest.mark.parametrize(
    "bad",
    [
        {"seq": -1},
        {"seq": "1"},
        {"seq": 1, "conflict": "x"},
        {"seq": 1, "conflict": [{"title": 1}]},
        {"seq": 1, "conflict": [{}] * (MAX_CONFLICT_SIDES + 1)},
        {"seq": 1, "sequenced": {"outcome": "maybe"}},
        {
            "seq": 1,
            "sequenced": {
                "outcome": "refused",
                "proposer_instance": "i",
                "proposal_hash": _H,
                "reason": "bogus",
            },
        },
    ],
)
def test_canonical_from_wire_refuses_malformed(bad):
    assert canonical_from_wire({"title": "T", **bad}) is None


def test_a_refusal_without_state():
    v = canonical_from_wire(
        {
            "seq": 0,
            "sequenced": {
                "proposer_instance": "i",
                "proposal_hash": _H,
                "outcome": "refused",
                "reason": "gone",
            },
        }
    )
    assert v is not None and not v.has_state and v.sequenced.reason == "gone"


# ─── Engine fixture ──────────────────────────────────────────────────────

SID = "sp-1"
BASE = _body("p1", "p2", "p3")


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> None:
        self.events.append(event)

    def of(self, kind) -> list:
        return [e for e in self.events if isinstance(e, kind)]


class _Fed:
    """``peer_supports`` + captured targeted sends."""

    def __init__(self, version: int = 48) -> None:
        self.version = version
        self.sent: list[tuple[str, object, dict]] = []

    async def peer_supports(self, instance_id, *, min_version):
        return self.version >= min_version

    async def send_with_mesh_fallback(
        self, *, to_instance_id, event_type, payload, space_id=None
    ):
        self.sent.append((to_instance_id, event_type, payload))


class _Spaces:
    def __init__(self, owner: str) -> None:
        self.owner = owner

    async def get(self, space_id):
        if space_id != SID:
            return None

        class _S:
            owner_instance_id = self.owner

        return _S()


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "pc.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key) VALUES(?, 'test', 'host', 'u1', ?)",
        (SID, "aa" * 32),
    )
    repo = SqlitePageRepo(db)
    bus = _Bus()
    svc = PageConflictService(repo, bus=bus)
    fed = _Fed()
    svc.attach_federation(fed, own_instance_id="host", space_repo=_Spaces("host"))
    page = await svc.host_create(
        new_page(title="T", content=BASE, created_by="u1", space_id=SID),
        actor_user_id="u1",
    )

    class E:
        pass

    e = E()
    e.db, e.repo, e.bus, e.svc, e.fed, e.pid = db, repo, bus, svc, fed, page.id
    yield e
    await db.shutdown()


async def _page(env):
    return await env.repo.get_space_page(env.pid, space_id=SID)


async def _propose(
    env, content, *, base_seq=1, base=BASE, by="u-a", proposer="iid-a", **kw
):
    return await env.svc.sequence(
        space_id=SID,
        page_id=env.pid,
        proposal=Proposal(
            title="T",
            content=content,
            actor_user_id=by,
            base_seq=base_seq,
            base_hash=version_hash("T", base) if base is not None else None,
            **kw,
        ),
        proposer_instance=proposer,
    )


async def _sides(env) -> list[str]:
    return [s.content for s in await env.svc.sides(SID, env.pid)]


# ─── Host: mode + create ─────────────────────────────────────────────────


async def test_mode(env):
    assert await env.svc.mode(SID) == (PageMode.HOST, "host")
    env.svc.attach_federation(env.fed, own_instance_id="me", space_repo=_Spaces("host"))
    assert await env.svc.mode(SID) == (PageMode.MEMBER, "host")
    env.fed.version = 47
    assert await env.svc.mode(SID) == (PageMode.LEGACY, "host")
    assert (await env.svc.mode("nope"))[0] is PageMode.LEGACY
    alone = PageConflictService(env.repo)
    assert (await alone.mode(SID))[0] is PageMode.HOST


async def test_host_create_is_seq_one_and_broadcast(env):
    page = await _page(env)
    assert page.seq == 1 and page.pending_base_seq is None
    (created,) = env.bus.of(PageCreated)
    assert created.canonical["seq"] == 1
    assert created.canonical["created_by"] == "u1"


# ─── Host: sequencing rules ──────────────────────────────────────────────


async def test_a_proposal_on_the_current_version_fast_forwards(env):
    r = await _propose(env, _body("p1", "p2", "p3", "p4"))
    assert r.outcome is SequenceOutcome.APPLIED
    page = await _page(env)
    assert (page.seq, page.content, page.last_editor_user_id) == (
        2,
        _body("p1", "p2", "p3", "p4"),
        "u-a",
    )
    (update,) = env.bus.of(PageUpdated)
    assert update.canonical["seq"] == 2
    assert update.canonical["sequenced"]["proposer_instance"] == "iid-a"
    versions = await env.repo.list_versions(env.pid, space_id=SID)
    assert [v.content for v in versions] == [BASE]


async def test_a_duplicate_is_acknowledged_without_a_new_seq(env):
    r = await _propose(env, BASE)
    assert r.outcome is SequenceOutcome.DUPLICATE
    assert (await _page(env)).seq == 1
    (to, _et, ack) = env.fed.sent[-1]
    assert to == "iid-a" and ack["sequenced"]["outcome"] == "applied"
    assert ack["seq"] == 1


async def test_a_base_ahead_of_the_host_is_refused(env):
    r = await _propose(env, "x", base_seq=9)
    assert (r.outcome, r.reason) == (SequenceOutcome.REFUSED, "bad_base")
    assert env.fed.sent[-1][2]["sequenced"]["reason"] == "bad_base"


async def test_an_older_base_merges(env):
    await _propose(env, _body("p1x", "p2", "p3"), by="u-a")
    r = await _propose(env, _body("p1", "p2", "p3x"), by="u-b", proposer="iid-b")
    assert r.outcome is SequenceOutcome.APPLIED
    page = await _page(env)
    assert page.content == _body("p1x", "p2", "p3x") and page.seq == 3
    # The merged-in proposal is kept in history: its author's next draft
    # (made on top of it) finds its base.
    contents = [v.content for v in await env.repo.list_versions(env.pid, space_id=SID)]
    assert _body("p1", "p2", "p3x") in contents


async def test_an_overlap_becomes_a_side_and_never_blocks(env):
    await _propose(env, _body("a", "p2", "p3"), by="u-a")
    r = await _propose(env, _body("b", "p2", "p3"), by="u-b", proposer="iid-b")
    assert r.outcome is SequenceOutcome.SIDE
    assert await _sides(env) == [_body("b", "p2", "p3")]
    page = await _page(env)
    assert page.content == _body("a", "p2", "p3") and page.seq == 3
    assert env.bus.of(PageConflictEmitted)[-1].federated is True
    # The conflict never blocks an edit.
    await env.svc.commit_local(
        space_id=SID, page_id=env.pid, actor_user_id="u1", patch={"content": "later"}
    )
    page = await _page(env)
    assert page.content == "later" and page.seq == 4
    assert await _sides(env) == [_body("b", "p2", "p3")]


async def test_an_unknown_base_becomes_a_side(env):
    r = await _propose(env, "x", base_seq=0, base="never here")
    assert r.outcome is SequenceOutcome.SIDE


async def test_a_title_clash_becomes_a_side(env):
    await env.svc.commit_local(
        space_id=SID, page_id=env.pid, actor_user_id="u1", patch={"title": "Host title"}
    )
    r = await env.svc.sequence(
        space_id=SID,
        page_id=env.pid,
        proposal=Proposal(
            title="Member title",
            content=BASE,
            actor_user_id="u-a",
            base_seq=1,
            base_hash=version_hash("T", BASE),
        ),
        proposer_instance="iid-a",
    )
    assert r.outcome is SequenceOutcome.SIDE


async def test_one_side_per_user_and_the_caps(env):
    await _propose(env, _body("h", "p2", "p3"), by="u-h")
    await _propose(env, _body("a1", "p2", "p3"), by="u-a")
    await _propose(env, _body("a2", "p2", "p3"), by="u-a")
    assert await _sides(env) == [_body("a2", "p2", "p3")]
    for user in ("u-b", "u-c", "u-d"):
        await _propose(env, _body(user, "p2", "p3"), by=user, proposer=f"iid-{user}")
    sides = await _sides(env)
    assert len(sides) == MAX_CONFLICT_SIDES
    # Overflow moved the oldest to history — never refused, never lost.
    assert _body("a2", "p2", "p3") not in sides
    kept = [v.content for v in await env.repo.list_versions(env.pid, space_id=SID)]
    assert _body("a2", "p2", "p3") in kept and _body("a1", "p2", "p3") in kept


async def test_sides_are_capped_in_bytes(env, monkeypatch):
    monkeypatch.setattr(mod, "SIDES_MAX_BYTES", 30)
    await _propose(env, _body("h", "p2", "p3"), by="u-h")
    await _propose(env, _body("a" * 20, "p2", "p3"), by="u-a")
    await _propose(env, _body("b" * 20, "p2", "p3"), by="u-b", proposer="iid-b")
    assert await _sides(env) == [_body("b" * 20, "p2", "p3")]


async def test_resolves_retire_the_sides(env):
    await _propose(env, _body("a", "p2", "p3"), by="u-a")
    await _propose(env, _body("b", "p2", "p3"), by="u-b", proposer="iid-b")
    (side,) = await env.svc.sides(SID, env.pid)
    page = await env.svc.commit_local(
        space_id=SID,
        page_id=env.pid,
        actor_user_id="u1",
        patch={},
        resolves=[side.hash],
    )
    assert await _sides(env) == [] and page.seq == 4
    kept = [v.content for v in await env.repo.list_versions(env.pid, space_id=SID)]
    assert _body("b", "p2", "p3") in kept


async def test_a_resolution_keeping_a_side_makes_it_current(env):
    await _propose(env, _body("a", "p2", "p3"), by="u-a")
    await _propose(env, _body("b", "p2", "p3"), by="u-b", proposer="iid-b")
    (side,) = await env.svc.sides(SID, env.pid)
    r = await env.svc.sequence(
        space_id=SID,
        page_id=env.pid,
        proposal=Proposal(
            title="T",
            content=side.content,
            actor_user_id="u-c",
            base_seq=3,
            base_hash=version_hash("T", _body("a", "p2", "p3")),
            resolves=(side.hash,),
        ),
        proposer_instance="iid-c",
    )
    assert r.outcome is SequenceOutcome.APPLIED
    assert (await _page(env)).content == side.content
    assert await _sides(env) == []


async def test_proposals_are_rate_limited(env, monkeypatch):
    monkeypatch.setattr(mod, "PROPOSALS_PER_MINUTE", 1)
    await _propose(env, "one")
    r = await _propose(env, "two", base_seq=2, base="one")
    assert (r.outcome, r.reason) == (SequenceOutcome.REFUSED, "rate_limited")
    assert env.fed.sent[-1][2]["sequenced"]["reason"] == "rate_limited"


async def test_a_proposed_create(env):
    r = await env.svc.sequence(
        space_id=SID,
        page_id="pg-new",
        proposal=Proposal(
            title="N", content="c", actor_user_id="u-a", base_seq=0, created_by="u-a"
        ),
        proposer_instance="iid-a",
    )
    assert r.created and r.page.seq == 1
    assert env.bus.of(PageCreated)[-1].page_id == "pg-new"
    gone = await env.svc.sequence(
        space_id=SID,
        page_id="pg-gone",
        proposal=Proposal(title="N", content="c", actor_user_id="u-a", base_seq=4),
        proposer_instance="iid-a",
    )
    assert (gone.outcome, gone.reason) == (SequenceOutcome.REFUSED, "gone")
    (_to, _et, ack) = env.fed.sent[-1]
    assert ack["seq"] == 0 and ack["sequenced"]["reason"] == "gone"


async def test_no_ack_to_a_v47_proposer(env):
    env.fed.version = 47
    await _propose(env, BASE)
    assert env.fed.sent == []


async def test_the_page_lock_serialises(env):
    lock = env.svc.lock_for(SID, env.pid)
    assert env.svc.lock_for(SID, env.pid) is lock
    async with lock:
        task = asyncio.create_task(_propose(env, "x"))
        await asyncio.sleep(0.05)
        assert not task.done()
    assert (await task).outcome is SequenceOutcome.APPLIED


async def test_commit_local_missing_page(env):
    with pytest.raises(PageNotFoundError):
        await env.svc.commit_local(
            space_id=SID, page_id="nope", actor_user_id="u", patch={}
        )


# ─── Member: drafts + mirror ─────────────────────────────────────────────


@pytest.fixture
async def member(env):
    """The same database seen as a MEMBER household (host elsewhere)."""
    env.svc.attach_federation(env.fed, own_instance_id="me", space_repo=_Spaces("host"))
    return env


def _version(page, content, seq, *, sides=(), sequenced=None, title="T"):
    return CanonicalVersion(
        seq=seq,
        title=title,
        content=content,
        created_by=page.created_by,
        updated_at="2026-10-03T00:00:00+00:00",
        last_editor_user_id="u-h",
        conflict=tuple(sides),
        sequenced=sequenced,
    )


async def _draft(env, content):
    page = await _page(env)
    async with env.svc.lock_for(SID, env.pid):
        return await env.svc.member_draft(
            page, replace(page, content=content), space_id=SID, actor_user_id="u-me"
        )


async def test_a_draft_keeps_its_base_and_proposes_it(member):
    env = member
    await _draft(env, "draft 1")
    await _draft(env, "draft 2")
    page = await _page(env)
    assert (page.seq, page.pending_base_seq) == (1, 1)
    proposal = await env.svc.proposal_for(SID, env.pid)
    assert proposal.content == "draft 2" and proposal.base_seq == 1
    assert proposal.base_hash == version_hash("T", BASE)


async def test_mirror_newer_without_a_draft_applies(member):
    env = member
    page = await _page(env)
    assert await env.svc.mirror(
        space_id=SID, page_id=env.pid, version=_version(page, "v2", 2)
    )
    page = await _page(env)
    assert (page.content, page.seq) == ("v2", 2)
    assert [v.content for v in await env.repo.list_versions(env.pid, space_id=SID)] == [
        BASE
    ]
    # Older or equal: ignored.
    assert not await env.svc.mirror(
        space_id=SID, page_id=env.pid, version=_version(page, "old", 1)
    )
    assert not await env.svc.mirror(
        space_id=SID, page_id=env.pid, version=_version(page, "same", 2)
    )
    assert (await _page(env)).content == "v2"


async def test_mirror_keeps_a_draft_until_its_ack(member):
    env = member
    await _draft(env, "mine")
    page = await _page(env)
    await env.svc.mirror(
        space_id=SID, page_id=env.pid, version=_version(page, "other", 2)
    )
    page = await _page(env)
    assert (page.content, page.seq, page.pending_base_seq) == ("mine", 2, 1)
    ack = Sequenced(
        proposer_instance="me",
        proposal_hash=version_hash("T", "mine"),
        outcome="applied",
    )
    await env.svc.mirror(
        space_id=SID,
        page_id=env.pid,
        version=_version(page, "merged", 3, sequenced=ack),
    )
    page = await _page(env)
    assert (page.content, page.seq, page.pending_base_seq) == ("merged", 3, None)
    assert env.bus.of(PageProposalSettled)[-1].outcome == "applied"
    assert await env.repo.get_draft_base(env.pid, space_id=SID) is None


async def test_mirror_settles_an_ack_at_the_same_seq(member):
    env = member
    await _draft(env, BASE + "x")
    page = await _page(env)
    ack = Sequenced(
        proposer_instance="me",
        proposal_hash=version_hash("T", BASE + "x"),
        outcome="applied",
    )
    await env.svc.mirror(
        space_id=SID, page_id=env.pid, version=_version(page, BASE, 1, sequenced=ack)
    )
    page = await _page(env)
    assert page.pending_base_seq is None and page.content == BASE


async def test_mirror_replaces_the_conflict_list_exactly(member):
    env = member
    page = await _page(env)
    side = PageConflictSide(
        hash=version_hash("T", "s"),
        title="T",
        content="s",
        by="u-b",
        at="2026-10-03T00:00:00.000001+00:00",
    )
    await env.svc.mirror(
        space_id=SID, page_id=env.pid, version=_version(page, "v2", 2, sides=[side])
    )
    assert await env.svc.sides(SID, env.pid) == [side]
    assert env.bus.of(PageConflictEmitted)[-1].federated is True
    await env.svc.mirror(space_id=SID, page_id=env.pid, version=_version(page, "v3", 3))
    assert await env.svc.sides(SID, env.pid) == []


@pytest.mark.parametrize(
    ("reason", "content", "pending"),
    [
        ("access", BASE, None),
        ("bad_base", BASE, None),
        ("gone", "mine", None),
        ("rate_limited", "mine", 1),
    ],
)
async def test_mirror_refusals(member, reason, content, pending):
    env = member
    await _draft(env, "mine")
    page = await _page(env)
    refused = Sequenced(
        proposer_instance="me",
        proposal_hash=version_hash("T", "mine"),
        outcome="refused",
        reason=reason,
    )
    await env.svc.mirror(
        space_id=SID,
        page_id=env.pid,
        version=_version(page, BASE, 1, sequenced=refused),
    )
    page = await _page(env)
    assert (page.content, page.pending_base_seq) == (content, pending)
    settled = env.bus.of(PageProposalSettled)[-1]
    assert (settled.outcome, settled.reason) == ("refused", reason)


async def test_a_stateless_refusal_restores_the_draft_base(member):
    env = member
    await _draft(env, "mine")
    refused = Sequenced(
        proposer_instance="me",
        proposal_hash=version_hash("T", "mine"),
        outcome="refused",
        reason="access",
    )
    await env.svc.mirror(
        space_id=SID,
        page_id=env.pid,
        version=CanonicalVersion(seq=0, sequenced=refused, has_state=False),
    )
    page = await _page(env)
    assert (page.content, page.pending_base_seq) == (BASE, None)


async def test_rebase_draft_on_what_we_sent(member):
    env = member
    await _draft(env, "one")
    sent = await env.svc.proposal_for(SID, env.pid)
    await _draft(env, "two")
    await env.svc.rebase_draft(SID, env.pid, sent=sent, seq=2)
    page = await _page(env)
    assert page.pending_base_seq == 2
    proposal = await env.svc.proposal_for(SID, env.pid)
    assert proposal.base_hash == version_hash("T", "one") and proposal.base_seq == 2


async def test_mirror_creates_a_page_from_the_host(member):
    env = member
    page = await _page(env)
    version = replace(_version(page, "new body", 1), created_by="u-x")
    assert await env.svc.mirror(space_id=SID, page_id="pg-mirror", version=version)
    created = await env.repo.get_space_page("pg-mirror", space_id=SID)
    assert (created.content, created.seq) == ("new body", 1)
    assert not await env.svc.mirror(
        space_id=SID,
        page_id="pg-other",
        version=CanonicalVersion(seq=1, has_state=False),
    )


async def test_member_create_is_a_seq_zero_draft(member):
    env = member
    page = await env.svc.member_create(
        new_page(title="N", content="c", created_by="u-me", space_id=SID)
    )
    assert (page.seq, page.pending_base_seq) == (0, 0)
    proposal = await env.svc.proposal_for(SID, page.id)
    assert (
        proposal.base_seq == 0
        and proposal.created_by == "u-me"
        and proposal.base_hash is None
    )


async def test_legacy_apply_keeps_the_old_body(env):
    page = await _page(env)
    await env.svc.legacy_apply(page, replace(page, content="lww"), space_id=SID)
    assert (await _page(env)).content == "lww"
    assert [v.content for v in await env.repo.list_versions(env.pid, space_id=SID)] == [
        BASE
    ]


# ─── Resolution bodies ───────────────────────────────────────────────────


async def test_resolution_body(env):
    page = await _page(env)
    side = PageConflictSide(
        hash=version_hash("T", "s"), title="S", content="s", by="u", at="t"
    )
    rb = env.svc.resolution_body
    assert await rb(
        page, [side], resolution="side", side=side.hash, merged_content=None
    ) == ("S", "s", None)
    assert await rb(
        page, [side], resolution="mine", side=None, merged_content=None
    ) == ("T", BASE, None)
    assert await rb(
        page, [side], resolution="theirs", side=None, merged_content=None
    ) == ("S", "s", None)
    assert await rb(
        page, [side], resolution="merged_content", side=None, merged_content="m"
    ) == ("T", "m", None)
    current = version_hash("T", BASE)
    assert await rb(
        page, [side], resolution="side", side=current, merged_content=None
    ) == ("T", BASE, None)
    with pytest.raises(PageConflictStaleError):
        await rb(page, [side], resolution="side", side=_H, merged_content=None)
    with pytest.raises(ValueError):
        await rb(
            page, [side], resolution="merged_content", side=None, merged_content=""
        )
    assert mod.RESOLUTIONS == ("side", "merged_content", "mine", "theirs")


async def test_history_rows_are_hashed_newest_first(env):
    for n, content in enumerate(("a", "b"), start=10):
        await env.repo.save_version(
            PageVersion(
                id=uuid.uuid4().hex,
                page_id=env.pid,
                version=n,
                title="T",
                content=content,
                edited_by="u",
                edited_at="t",
                space_id=SID,
            )
        )
    hist = await env.svc._history(SID, env.pid)
    assert [h for h, _v in hist] == [version_hash("T", "b"), version_hash("T", "a")]
