"""SpaceMailboxService rules with in-memory stand-ins only (no GFS):
races, the release jitter, recovery of a lost mailbox (v_88 review)."""

from __future__ import annotations

import asyncio
import random

import orjson

from socialhome.domain.space_mailbox import OwnMailbox
from socialhome.federation.gfs_mailbox import derive_mailbox_keys
from socialhome.services.space_mailbox_service import (
    RELEASE_DELAY_S,
    SpaceMailboxService,
    _Claim,
    _claim_key,
)

OWN = OwnMailbox(gen=7, gfs_instance_id="g", gfs_url="http://g")


class _RacingSpaces:
    """``list_local_member_user_ids`` lets a seat write (``remember``) slip
    in while ``release_unused`` is between its row read and its decision."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str | None] = {("s1", "host"): None}
        self.svc: SpaceMailboxService | None = None
        self.fired = False
        self.racer: asyncio.Task | None = None

    async def list_local_member_user_ids(self, space_id):
        if not self.fired:
            self.fired = True
            assert self.svc is not None
            # A concurrent seat write — it must wait for the release, not
            # interleave with it.
            self.racer = asyncio.create_task(
                self.svc.remember("s1", "host", peer=None, own=OWN)
            )
            await asyncio.sleep(0)
        return ["me"]

    async def set_space_instance_mailbox(self, s, i, v):
        if (s, i) in self.rows:
            self.rows[(s, i)] = v

    async def list_space_instance_mailboxes(self):
        return [(s, i, v) for (s, i), v in self.rows.items() if v is not None]


def _bare_service(spaces, **kw) -> SpaceMailboxService:
    return SpaceMailboxService(
        space_repo=spaces,
        instance_config=None,  # type: ignore[arg-type]
        key_manager=None,  # type: ignore[arg-type]
        gfs_repo=None,  # type: ignore[arg-type]
        gfs_service=None,  # type: ignore[arg-type]
        instance_id="i",
        identity_seed=b"\0" * 32,
        keywrap_public_key=b"\0" * 32,
        keywrap_sig="x",
        **kw,
    )


async def test_a_seat_written_during_a_sweep_keeps_its_mailbox(monkeypatch):
    """Regression (review of #1333): the sweep read the rows, awaited, then
    read ``_pending`` — a ``remember`` in between ended the pending state
    against a stale row snapshot and the live mailbox was unregistered."""
    spaces = _RacingSpaces()
    # Another row so the in-use loop awaits.
    spaces.rows[("s0", "x")] = orjson.dumps({"peer": None, "own": None}).decode()
    svc = _bare_service(spaces)
    spaces.svc = svc
    keys = derive_mailbox_keys(b"\1" * 32, "g", "s1", 7)
    key = _claim_key("s1", OWN)
    svc._claims[key] = _Claim(keys=keys, own=OWN)
    svc._pending[key] = svc._clock() + 1800
    dropped: list = []

    async def _drop(self, claim, *, delay=None):
        dropped.append(claim)

    monkeypatch.setattr(SpaceMailboxService, "_release_claim", _drop)
    assert await svc.release_unused() == 0
    assert spaces.racer is not None
    await spaces.racer
    assert dropped == []
    assert key in svc._claims
    assert spaces.rows[("s1", "host")] is not None  # the seat holds it


async def test_each_release_waits_its_own_random_delay(monkeypatch):
    """One kick / ban / dissolve releases several mailboxes; unregistering
    them back to back from one address would link them. Each goes on its
    own, after an independent random wait of minutes."""

    class _Spaces:
        async def list_local_member_user_ids(self, space_id):
            return []

        async def list_space_instance_mailboxes(self):
            return []

        async def set_space_instance_mailbox(self, *a):
            return None

    svc = _bare_service(_Spaces(), rng=random.Random(4))
    for space in ("s1", "s2", "s3"):
        own = OwnMailbox(gen=1, gfs_instance_id="g", gfs_url="http://g")
        keys = derive_mailbox_keys(b"\1" * 32, "g", space, 1)
        svc._claims[_claim_key(space, own)] = _Claim(keys=keys, own=own)
    delays: list[float] = []

    async def _release(self, claim, *, delay):
        delays.append(delay)

    monkeypatch.setattr(SpaceMailboxService, "_release_claim", _release)
    assert await svc.release_unused() == 3
    await asyncio.sleep(0)
    for _ in range(50):
        if len(delays) == 3:
            break
        await asyncio.sleep(0.01)
    assert len(delays) == 3
    lo, hi = RELEASE_DELAY_S
    assert all(lo <= d <= hi for d in delays)
    assert lo >= 60.0  # minutes, not milliseconds
    assert len(set(delays)) == 3  # independent
    assert svc.open_claims() == 0
    await svc.stop()
