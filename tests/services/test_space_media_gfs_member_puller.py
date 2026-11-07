"""The member fallback's household side: pull wants of listed spaces this
household is a member of, anonymously
(:mod:`socialhome.services.space_media_gfs_member_puller`)."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from types import SimpleNamespace

from socialhome.crypto import ed25519_public_key
from socialhome.domain.gfs_space_media_on_demand import MemberPullRequest
from socialhome.services import space_media_gfs_member_puller as mod
from socialhome.services.space_media_gfs_member_puller import (
    SpaceMediaGfsMemberPuller,
)
from socialhome.space_media_hint import SpaceMediaLocator, seal_space_media_hint
from socialhome.writer_key import verify_writer_sig

OWN = "own-inst"
HOST = "host-inst"
WRITER = bytes(range(1, 33))
BID = "ab" * 32


@dataclass
class _Conn:
    id: str
    gfs_instance_id: str = ""
    inbox_url: str = "https://g.example"
    status: str = "active"
    registered: bool = True

    def __post_init__(self) -> None:
        self.gfs_instance_id = self.gfs_instance_id or "inst-" + self.id


class _Spaces:
    def __init__(self, spaces: dict[str, str]):
        #: space id → owner instance id
        self.spaces = spaces

    async def list_all(self):
        return [
            SimpleNamespace(id=s, owner_instance_id=o) for s, o in self.spaces.items()
        ]


class _Crypto:
    async def get_current_epoch(self, space_id):
        return 7


class _Certs:
    def __init__(self, without=()):
        self.without = set(without)

    async def own_writer_key(self, space_id, epoch):
        return None if space_id in self.without else WRITER


class _Gfs:
    def __init__(self, targets, *, holds=None, replies=None):
        #: space id → targets (None = no list known)
        self.targets = targets
        self.holds = holds if holds is not None else {}
        self.replies = list(replies or [])
        self.pulls: list[tuple[str, dict, float]] = []

    async def server_targets(self, space_id):
        return self.targets.get(space_id)

    async def space_media_member_pull_hold(self, conn):
        return self.holds.get(conn.id, 25)

    def addressee_key_for(self, conn):
        return "11" * 32

    async def pull_space_media_wants(self, conn, body, *, timeout_s):
        self.pulls.append((conn.id, body, timeout_s))
        if self.replies:
            return self.replies.pop(0)
        return 204, []


class _Responder:
    def __init__(self):
        self.answered: list = []

    async def answer_member_want(self, want, conn, *, space_id, epoch, writer_seed):
        self.answered.append((want, conn.id, space_id, epoch, writer_seed))
        return "served"


def _puller(gfs, *, spaces=None, certs=None, responder=None, start_delay=(0, 0)):
    p = SpaceMediaGfsMemberPuller(
        space_repo=_Spaces(spaces or {"s1": HOST}),
        gfs_service=gfs,
        writer_certs=certs or _Certs(),
        space_crypto=_Crypto(),
        responder=responder or _Responder(),
        rng=random.Random(1),
        start_delay=start_delay,
    )
    p.attach_federation(SimpleNamespace(own_instance_id=OWN))
    return p


def _want_wire(ticket="a" * 32):
    return {
        "ticket": ticket,
        "blob_id": BID,
        "mode": "stream",
        "hints": [
            seal_space_media_hint(
                media_key=bytes(32),
                scope="space",
                scope_id="s1",
                blob_id=BID,
                epoch=4,
                locator=SpaceMediaLocator(item_id="i", origin=HOST, filename="a.webp"),
            )
        ],
        "claim": "c" * 32,
    }


async def test_only_spaces_it_is_a_writer_member_of_on_proven_gfss():
    g1, g2, g3 = _Conn("g1"), _Conn("g2", registered=False), _Conn("g3")
    g4 = _Conn("g4", status="suspended")
    gfs = _Gfs(
        {
            "s1": [g1, g2, g3, g4],
            "mine": [g1],
            "nokey": [g1],
            # no list known: never pulled for
        },
        holds={"g3": None},
    )
    p = _puller(
        gfs,
        spaces={"s1": HOST, "mine": OWN, "nokey": HOST, "nolist": HOST},
        certs=_Certs(without={"nokey"}),
    )
    pairs = await p.eligible()
    # Not the owner (it answers as the owner); a writer key (followers have
    # none); only a registered, active connection on the owner's list whose
    # GFS proves member_pull.
    assert [(c.id, s) for c, s in pairs] == [("g1", "s1")]


async def test_at_most_max_pull_loops_pairs(monkeypatch):
    monkeypatch.setattr(mod, "MAX_PULL_LOOPS", 2)
    conns = [_Conn(f"g{i}") for i in range(4)]
    gfs = _Gfs({"s1": conns})
    p = _puller(gfs)
    picked = await p.pick()
    assert len(picked) == 2


async def test_a_pull_is_signed_by_the_writer_key_and_names_no_household():
    g1 = _Conn("g1")
    responder = _Responder()
    gfs = _Gfs({"s1": [g1]}, replies=[(200, [_want_wire()])])
    p = _puller(gfs, responder=responder)
    delay = await p.pull_once(g1, "s1")
    assert delay == 0.0  # straight back for the next want
    (conn_id, body, timeout_s) = gfs.pulls[0]
    assert conn_id == "g1" and timeout_s == 25 + mod.PULL_TIMEOUT_PAD_S
    req = MemberPullRequest.from_wire(body)
    assert (req.scope, req.scope_id, req.epoch) == ("space", "s1", 7)
    assert req.gfs_instance_id == "inst-g1" and req.gfs_key == "11" * 32
    assert verify_writer_sig(
        writer_pk=ed25519_public_key(WRITER),
        message=req.signing_bytes(),
        writer_sig=req.sig,
        suite=req.sig_suite,
    )
    assert OWN not in repr(body)
    (want, cid, space_id, epoch, seed) = responder.answered[0]
    assert (want.ticket, want.claim, cid, space_id, epoch, seed) == (
        "a" * 32,
        "c" * 32,
        "g1",
        "s1",
        7,
        WRITER,
    )
    # Every pull carries a fresh nonce (the GFS's replay guard).
    await p.pull_once(g1, "s1")
    assert gfs.pulls[1][1]["nonce"] != body["nonce"]


async def test_pull_outcomes_back_off():
    g1 = _Conn("g1")
    gfs = _Gfs(
        {"s1": [g1]},
        replies=[
            (204, []),
            (503, []),
            (429, []),
            (403, []),
            (0, []),
            (200, [{"ticket": "bad"}]),
        ],
    )
    responder = _Responder()
    p = _puller(gfs, responder=responder)
    assert 0 <= await p.pull_once(g1, "s1") <= mod.IDLE_JITTER_S[1]
    for lo, hi in (mod.BACKOFF_FULL_S, mod.BACKOFF_FULL_S):
        assert lo <= await p.pull_once(g1, "s1") <= hi
    for _ in range(2):
        lo, hi = mod.BACKOFF_REFUSED_S
        assert lo <= await p.pull_once(g1, "s1") <= hi
    # A malformed want is never answered.
    assert await p.pull_once(g1, "s1") == 0.0
    assert responder.answered == []


async def test_no_writer_key_or_no_member_pull_means_no_pull():
    g1 = _Conn("g1")
    gfs = _Gfs({"s1": [g1]}, holds={"g1": None})
    p = _puller(gfs)
    lo, hi = mod.BACKOFF_REFUSED_S
    assert lo <= await p.pull_once(g1, "s1") <= hi
    gfs = _Gfs({"s1": [g1]})
    p = _puller(gfs, certs=_Certs(without={"s1"}))
    assert lo <= await p.pull_once(g1, "s1") <= hi
    assert gfs.pulls == []


async def test_start_pulls_and_stop_ends_every_loop():
    g1 = _Conn("g1")
    gfs = _Gfs({"s1": [g1]})
    p = _puller(gfs)
    await p.start()
    for _ in range(200):
        if gfs.pulls:
            break
        await asyncio.sleep(0.01)
    assert gfs.pulls
    await p.stop()
    assert p._loops == {}
    n = len(gfs.pulls)
    await asyncio.sleep(0.05)
    assert len(gfs.pulls) == n


async def test_stop_during_the_start_delay_returns_at_once():
    gfs = _Gfs({"s1": [_Conn("g1")]})
    p = _puller(gfs, start_delay=(3600, 3600))
    await p.start()
    await asyncio.wait_for(p.stop(), timeout=1.0)
    assert gfs.pulls == []


async def test_a_pair_no_longer_eligible_stops_its_loop():
    g1 = _Conn("g1")
    gfs = _Gfs({"s1": [g1]})
    p = _puller(gfs)
    await p.refresh()
    assert set(p._loops) == {("g1", "s1")}
    gfs.targets = {"s1": []}
    await p.refresh()
    for _ in range(200):
        if not p._loops:
            break
        await asyncio.sleep(0.01)
    assert p._loops == {}
    await p.stop()


async def test_pulls_to_one_gfs_are_paced_below_its_limit(monkeypatch):
    """A burst of wants never trips the GFS's per-address limit (and its
    long back-off): pulls to one GFS stay within MAX_PULLS_PER_MINUTE."""
    monkeypatch.setattr(mod, "MAX_PULLS_PER_MINUTE", 3)
    p = _puller(_Gfs({}))
    assert [p._pace("g1") for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = p._pace("g1")
    assert 59.0 < wait <= 60.0
    # Another GFS has its own budget.
    assert p._pace("g2") == 0.0


async def test_a_loop_waits_for_the_pull_budget(monkeypatch):
    monkeypatch.setattr(mod, "MAX_PULLS_PER_MINUTE", 2)
    g1 = _Conn("g1")
    # Wants keep coming: the loop would pull again at once.
    gfs = _Gfs({"s1": [g1]}, replies=[(200, [_want_wire()])] * 10)
    p = _puller(gfs)
    await p.refresh()
    await asyncio.sleep(0.2)
    assert len(gfs.pulls) == 2
    await p.stop()
