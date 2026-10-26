"""Unit tests for :mod:`socialhome.services.moment_reaction_counts` (v_69).

In-memory fakes only; the end-to-end rules against the real app, SQLite and
the GFS are in ``tests/protocol/test_moment_reaction_counts.py``."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.events import MomentReactionTotalsUpdated
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.moment import Moment, MomentReactionSummary
from socialhome.moment_counts import sign_counts
from socialhome.services.moment_reaction_counts import (
    GFS_CHECKPOINTS_S,
    GFS_MAX_TRIES,
    GFS_RETRY_S,
    MESH_CHECKPOINTS_S,
    MESH_SENDS_PER_TICK,
    MomentReactionCounts,
    due_checkpoint,
)

FET = FederationEventType
OWN_KEY = generate_identity_keypair()
OWN = derive_instance_id(OWN_KEY.public_key)
ORIGIN_KEY = generate_identity_keypair()
ORIGIN = derive_instance_id(ORIGIN_KEY.public_key)
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _ms(offset_s: float) -> int:
    return int((T0.timestamp() + offset_s) * 1000)


def _moment(mid="m-1", *, via="self", public=False, days=7, author="u-anna"):
    return Moment(
        id=mid,
        author_user_id=author,
        content="x",
        media_url=None,
        media_type=None,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id=OWN if via == "self" else ORIGIN,
        created_at=T0.isoformat(),
        expires_at=(T0 + timedelta(days=days)).isoformat(),
        is_public=public,
        received_via=via,
    )


class _Repo:
    def __init__(self) -> None:
        self.own: list[Moment] = []
        self.held: dict[str, Moment] = {}
        self.tallies: dict[str, dict[str, int]] = {}
        self.summaries: dict[str, MomentReactionSummary] = {}

    async def list_own_live_since(self, since_iso):
        return list(self.own)

    async def reaction_tallies(self, ids):
        return {i: self.tallies[i] for i in ids if i in self.tallies}

    async def get(self, mid, include_deleted=False):
        return self.held.get(mid)

    async def get_reaction_summaries(self, ids):
        return {i: self.summaries[i] for i in ids if i in self.summaries}

    async def set_reaction_summary(
        self, mid, summary, *, author_user_id, origin_instance_id
    ):
        m = self.held.get(mid)
        if m is None or m.author_user_id != author_user_id:
            return False
        old = self.summaries.get(mid)
        if old is not None and old.rev >= summary.rev:
            return False
        self.summaries[mid] = summary
        return True


class _Users:
    async def get_by_user_id(self, user_id):
        return object() if user_id == "u-anna" else None


class _Fed:
    own_instance_id = OWN
    own_identity_seed = OWN_KEY.private_key
    own_identity_pk = OWN_KEY.public_key

    def __init__(self) -> None:
        self.pinned: dict[str, bytes] = {}

    async def peer_identity_public_key(self, instance_id):
        return self.pinned.get(instance_id)


class _Outbound:
    def __init__(self) -> None:
        self.fanned: list[dict] = []
        self.relayed: list[dict] = []
        #: (moment id, origin) whose create this household relayed.
        self.relayed_creates: set[tuple[str, str]] = set()
        self.fail = False

    async def fan_counts(self, payload, *, author_user_id):
        if self.fail:
            raise RuntimeError("outbox down")
        self.fanned.append(payload)
        return 1

    async def relay_inbound(self, **kw):
        self.relayed.append(kw)
        return 1

    def relayed_create(self, moment_id, origin):
        return (moment_id, origin) in self.relayed_creates


class _Gfs:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int, dict, int]] = []
        self.skips: list[frozenset[str]] = []
        #: GFS ids that answer 2xx / refuse; ``boom`` raises.
        self.ok = frozenset({"g1"})
        self.refusing: frozenset[str] = frozenset()
        self.boom = False

    async def send_counts(self, moment, *, rev, counts, total, skip=frozenset()):
        if self.boom:
            raise RuntimeError("GFS session gone")
        self.sent.append((moment.id, rev, counts, total))
        self.skips.append(skip)
        return self.ok - skip, self.refusing - skip


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event):
        self.events.append(event)


@pytest.fixture
def svc():
    repo, out, bus = _Repo(), _Outbound(), _Bus()
    clock = [_ms(0)]
    counts = MomentReactionCounts(
        bus=bus,
        federation_service=_Fed(),
        moment_repo=repo,
        user_repo=_Users(),
        outbound=out,
        clock_ms=lambda: clock[0],
    )
    counts._started_ms = _ms(-1)
    return counts, repo, out, bus, clock


# ── Checkpoints ───────────────────────────────────────────────────────────


def test_checkpoints_are_the_agreed_ones():
    assert MESH_CHECKPOINTS_S == (600, 21_600, 259_200)
    assert GFS_CHECKPOINTS_S == (600, 10_800, 86_400)


@pytest.mark.parametrize(
    "now,expires_days,want",
    [
        (599, 7, None),
        (600, 7, 0),
        (21_599, 7, 0),
        (21_600, 7, 1),
        (259_200, 7, 2),
        (259_200, 3, 1),  # the +3 d one only while followers keep it
        (259_200, 2, 1),
    ],
)
def test_due_checkpoint_mesh(now, expires_days, want):
    assert (
        due_checkpoint(
            MESH_CHECKPOINTS_S,
            created_s=0,
            expires_s=expires_days * 86_400,
            now_s=now,
        )
        == want
    )


def test_due_checkpoint_gfs_last_only_within_the_window():
    assert (
        due_checkpoint(GFS_CHECKPOINTS_S, created_s=0, expires_s=86_400, now_s=90_000)
        == 1
    )
    assert (
        due_checkpoint(GFS_CHECKPOINTS_S, created_s=0, expires_s=90_000, now_s=90_000)
        == 2
    )


async def test_mesh_and_gfs_checkpoints_only_on_change(svc):
    counts, repo, out, _bus, clock = svc
    gfs = _Gfs()
    counts.attach_gfs(gfs)
    repo.own = [_moment(public=True)]
    repo.tallies["m-1"] = {"🔥": 2}
    clock[0] = _ms(601)
    assert await counts.tick() == 2  # mesh + gfs at +10 min
    assert [p["total"] for p in out.fanned] == [2]
    assert [s[3] for s in gfs.sent] == [2]
    clock[0] = _ms(3 * 3600 + 1)
    assert await counts.tick() == 0  # gfs +3 h, unchanged
    repo.tallies["m-1"] = {"🔥": 2, "👏": 1}
    clock[0] = _ms(6 * 3600 + 1)
    assert await counts.tick() == 1  # mesh +6 h — changed since its last send
    clock[0] = _ms(86_400 + 1)
    assert await counts.tick() == 1  # gfs +24 h — changed since its last send
    assert gfs.sent[-1][3] == 3
    revs = [p["rev"] for p in out.fanned] + [s[1] for s in gfs.sent]
    assert len(set(revs)) == len(revs)  # every rev is new


async def test_a_private_moment_goes_over_the_mesh_only(svc):
    counts, repo, _out, _bus, clock = svc
    gfs = _Gfs()
    counts.attach_gfs(gfs)
    repo.own = [_moment(public=False)]
    repo.tallies["m-1"] = {"🔥": 1}
    clock[0] = _ms(601)
    assert await counts.tick() == 1
    assert gfs.sent == []


async def test_a_moment_without_reactions_sends_nothing(svc):
    counts, repo, out, _bus, clock = svc
    repo.own = [_moment()]
    clock[0] = _ms(601)
    assert await counts.tick() == 0
    assert out.fanned == []


async def test_gone_moments_are_forgotten(svc):
    counts, repo, _out, _bus, clock = svc
    repo.own = [_moment()]
    repo.tallies["m-1"] = {"🔥": 1}
    clock[0] = _ms(601)
    await counts.tick()
    assert counts._done
    repo.own = []
    await counts.tick()
    assert counts._done == {} and counts._sent == {}


async def test_revs_grow_within_one_millisecond(svc):
    counts, *_ = svc
    assert counts._next_rev() < counts._next_rev()


async def test_start_and_stop_run_the_loop(svc):
    counts, repo, out, _bus, clock = svc
    counts._tick_s = 0.01
    repo.own = [_moment()]
    repo.tallies["m-1"] = {"🔥": 1}
    clock[0] = _ms(601)
    await counts.start()
    await counts.start()  # idempotent
    for _ in range(100):
        if out.fanned:
            break
        await asyncio.sleep(0.01)
    await counts.stop()
    assert out.fanned and counts._task is None


# ── Receiver ──────────────────────────────────────────────────────────────


def _event(payload, *, from_instance="peer-a") -> FederationEvent:
    return FederationEvent(
        msg_id="m",
        event_type=FET.MOMENT_REACTIONS_COUNTED,
        from_instance=from_instance,
        to_instance=OWN,
        timestamp=T0.isoformat(),
        payload=payload,
    )


def _payload(rev, **over):
    return sign_counts(
        seed=ORIGIN_KEY.private_key,
        identity_pk=ORIGIN_KEY.public_key,
        moment_id=over.pop("moment_id", "m-far"),
        author_user_id=over.pop("author", "u-noor"),
        origin_instance_id=ORIGIN,
        rev=rev,
        counts={"🔥": 1},
        total=1,
    )


async def test_receiver_keeps_newer_totals_publishes_and_relays(svc):
    counts, repo, out, bus, clock = svc
    repo.held["m-far"] = replace(
        _moment("m-far", via="household", author="u-noor"), hop_count=2
    )
    clock[0] = _ms(1000)
    await counts._on_counted(_event(_payload(_ms(900))))
    assert repo.summaries["m-far"].total == 1
    assert isinstance(bus.events[0], MomentReactionTotalsUpdated)
    assert bus.events[0].received_via == "household"
    # The held row's hop keeps the hint from a protected account (§CP.R).
    assert bus.events[0].hop_count == 2
    assert out.relayed[0]["holds_moment"] is True
    await counts._on_counted(_event(_payload(_ms(800))))  # older
    assert repo.summaries["m-far"].rev == _ms(900)
    assert len(bus.events) == 1


async def test_receiver_refuses_its_own_origin_and_local_authors(svc):
    counts, _repo, out, bus, clock = svc
    clock[0] = _ms(1000)
    own = sign_counts(
        seed=OWN_KEY.private_key,
        identity_pk=OWN_KEY.public_key,
        moment_id="m-1",
        author_user_id="u-x",
        origin_instance_id=OWN,
        rev=_ms(900),
        counts={},
        total=0,
    )
    await counts._on_counted(_event(own))
    await counts._on_counted(_event(_payload(_ms(900), author="u-anna")))
    await counts._on_counted(_event({"moment_id": "m"}))  # malformed
    assert out.relayed == [] and bus.events == []


async def test_receiver_budget_counts_new_revs_not_copies(svc):
    counts, repo, out, _bus, clock = svc
    repo.held["m-far"] = _moment("m-far", via="household", author="u-noor")
    clock[0] = _ms(1000)
    p = _payload(_ms(100))
    for peer in ("a", "b", "c", "d", "e", "f"):  # six copies of one rev
        await counts._on_counted(_event(p, from_instance=peer))
    assert len(out.relayed) == 6  # each copy is offered to the relay
    for i in range(1, 6):
        await counts._on_counted(_event(_payload(_ms(100 + i))))
    assert repo.summaries["m-far"].rev == _ms(103)  # 4 revs a day at most


async def test_apply_totals_refuses_malformed_counts(svc):
    counts, repo, *_ = svc
    repo.held["m-far"] = _moment("m-far", via="household", author="u-noor")
    assert not await counts.apply_totals(
        moment_id="m-far",
        author_user_id="u-noor",
        origin_instance_id=ORIGIN,
        rev=1,
        counts={"🔥": 5},
        total=1,
    )


# ── Adversarial review of #1184 ───────────────────────────────────────────


async def test_mesh_sends_are_paced_and_a_restarts_catch_up_goes_last(svc):
    counts, repo, out, _bus, clock = svc
    counts._started_ms = _ms(3600)  # restarted an hour after T0
    # Twelve moments made before the restart (catch-up), and one after.
    repo.own = [_moment(f"m-old-{i}") for i in range(12)]
    repo.own.append(
        replace(_moment("m-new"), created_at=(T0 + timedelta(hours=2)).isoformat())
    )
    for m in repo.own:
        repo.tallies[m.id] = {"🔥": 1}
    clock[0] = _ms(2 * 3600 + 601)
    assert await counts.tick() == MESH_SENDS_PER_TICK
    assert out.fanned[0]["moment_id"] == "m-new"  # a live checkpoint first
    assert await counts.tick() == 13 - MESH_SENDS_PER_TICK
    assert await counts.tick() == 0


async def test_a_failed_mesh_send_stays_due(svc):
    counts, repo, out, _bus, clock = svc
    repo.own = [_moment()]
    repo.tallies["m-1"] = {"🔥": 1}
    clock[0] = _ms(601)
    out.fail = True
    assert await counts.tick() == 0
    assert counts._done == {}
    out.fail = False
    assert await counts.tick() == 1


async def test_a_refusing_gfs_is_retried_then_left_to_the_next_checkpoint(svc):
    counts, repo, _out, _bus, clock = svc
    gfs = _Gfs()
    gfs.ok, gfs.refusing = frozenset({"g1"}), frozenset({"g2"})
    counts.attach_gfs(gfs)
    repo.own = [_moment(public=True)]
    repo.tallies["m-1"] = {"🔥": 1}
    clock[0] = _ms(601)
    assert await counts.tick() == 2  # mesh + g1
    for n in range(1, GFS_MAX_TRIES):
        clock[0] += GFS_RETRY_S * 1000
        await counts.tick()
        assert gfs.skips[n] == frozenset({"g1"})  # only g2 again
    assert len(gfs.sent) == GFS_MAX_TRIES
    clock[0] += GFS_RETRY_S * 1000
    await counts.tick()
    assert len(gfs.sent) == GFS_MAX_TRIES  # given up for this checkpoint
    # Unchanged, yet not every GFS had it: the next checkpoint goes to all.
    gfs.refusing = frozenset()
    gfs.ok = frozenset({"g1", "g2"})
    clock[0] = _ms(3 * 3600 + 1)
    await counts.tick()
    assert gfs.skips[-1] == frozenset()


async def test_a_gfs_send_that_raises_is_retried(svc):
    counts, repo, _out, _bus, clock = svc
    gfs = _Gfs()
    gfs.boom = True
    counts.attach_gfs(gfs)
    repo.own = [_moment(public=True)]
    repo.tallies["m-1"] = {"🔥": 1}
    clock[0] = _ms(601)
    assert await counts.tick() == 1  # the mesh one only
    gfs.boom = False
    clock[0] += GFS_RETRY_S * 1000
    assert await counts.tick() == 1
    assert len(gfs.sent) == 1


async def test_receiver_refuses_before_any_budget(svc):
    counts, repo, out, _bus, clock = svc
    repo.held["m-far"] = _moment("m-far", via="household", author="u-noor")
    clock[0] = _ms(30 * 3600)
    await counts._on_counted(_event(_payload(_ms(0))))  # 30 h old
    await counts._on_counted(_event(_payload(_ms(30 * 3600), moment_id="m-else")))
    assert out.relayed == [] and counts._seen_revs == {}
    assert counts._limiter.has_room(f"o:{ORIGIN}", limit=1, window_s=60)


async def test_a_relay_only_household_passes_on_newer_revs_only(svc):
    counts, _repo, out, _bus, clock = svc
    out.relayed_creates.add(("m-far", ORIGIN))
    clock[0] = _ms(1000)
    await counts._on_counted(_event(_payload(_ms(900))))
    await counts._on_counted(_event(_payload(_ms(900)), from_instance="peer-b"))
    assert len(out.relayed) == 2  # a copy of the rev taken: offered, free
    counts._seen_revs.clear()  # as after a restart
    await counts._on_counted(_event(_payload(_ms(900))))
    await counts._on_counted(_event(_payload(_ms(800))))
    assert len(out.relayed) == 2  # not newer than the last passed on
    await counts._on_counted(_event(_payload(_ms(950))))
    assert len(out.relayed) == 3
