"""SpaceMailboxService rules with in-memory stand-ins only (no GFS):
races, the release jitter, recovery of a lost mailbox (v_88 review)."""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import orjson

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
)
from socialhome.domain.space_mailbox import OwnMailbox
from socialhome.federation.gfs_mailbox import derive_mailbox_keys
from socialhome.federation.space_mailbox_entry import build_entry
from socialhome.services.space_mailbox_service import (
    ANNOUNCES_PER_DAY,
    CLAIM_JITTER_S,
    LOST_ROTATE_AFTER,
    RECOVERY_REGISTERS_PER_DAY,
    REGISTER_NO_TICKET,
    REGISTER_OK,
    REGISTER_REFUSED,
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

    async def list_member_instances(self, space_id):
        return [i for (s, i) in self.rows if s == space_id]

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
    key = _claim_key("s1", "host", OWN)
    svc._claims[key] = _Claim(keys=keys, own=OWN, peer="host")
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

        async def list_member_instances(self, space_id):
            return []

    svc = _bare_service(_Spaces(), rng=random.Random(4))
    for space in ("s1", "s2", "s3"):
        own = OwnMailbox(gen=1, gfs_instance_id="g", gfs_url="http://g")
        keys = derive_mailbox_keys(b"\1" * 32, "g", space, 1)
        svc._claims[_claim_key(space, "host", own)] = _Claim(
            keys=keys, own=own, peer="host"
        )
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


async def test_a_mailbox_the_gfs_keeps_refusing_is_replaced(monkeypatch):
    """Re-registering the same mailbox is refused again and again (not
    just short of a ticket): after ``LOST_ROTATE_AFTER`` refusals it is
    replaced by a fresh generation, the seat rows move to it and the
    counterpart was told to fall back meanwhile."""

    class _Spaces:
        def __init__(self) -> None:
            own = {"gen": 7, "gfs_id": "g", "gfs": "http://g"}
            self.rows = {
                ("s1", "host"): orjson.dumps({"peer": None, "own": own}).decode()
            }

        async def list_local_member_user_ids(self, space_id):
            return ["me"]

        async def list_space_instance_mailboxes(self):
            return [(s, i, v) for (s, i), v in self.rows.items() if v is not None]

        async def set_space_instance_mailbox(self, s, i, v):
            self.rows[(s, i)] = v

        async def list_member_instances(self, space_id):
            return [i for (s, i) in self.rows if s == space_id]

    class _Repo:
        async def list_active(self):
            return []

    spaces = _Spaces()
    svc = _bare_service(spaces, rng=random.Random(1))
    svc._gfs_repo = _Repo()  # type: ignore[assignment]
    svc._root = b"\1" * 32
    old_keys = derive_mailbox_keys(b"\1" * 32, "g", "s1", 7)
    claim = _Claim(keys=old_keys, own=OWN, peer="host")
    svc._claims[_claim_key("s1", "host", OWN)] = claim
    announced: list = []

    async def _announce(space_id, instance_id, entry):
        announced.append(entry)

    svc.attach_announcer(_announce)

    async def _register(self, conn, keys, *, fetch=True):
        refused = keys.mailbox_id == old_keys.mailbox_id
        return REGISTER_REFUSED if refused else REGISTER_OK

    monkeypatch.setattr(SpaceMailboxService, "_register", _register)
    for _ in range(LOST_ROTATE_AFTER - 1):
        assert await svc._recover(object(), claim) is False  # type: ignore[arg-type]
    assert announced == [None]  # told once, at the first refusal
    assert await svc._recover(object(), claim) is True  # type: ignore[arg-type]
    row = orjson.loads(spaces.rows[("s1", "host")])
    assert row["own"]["gen"] != 7
    new_key = ("s1", "host", "g", row["own"]["gen"])
    assert list(svc._claims) == [new_key]
    assert svc._claims[new_key].lost  # it announces itself once claimed
    await svc.stop()


class _RowSpaces:
    """Rows as the repo stores them: ``mailbox`` JSON or NULL (absent from
    the listing), plus which (space, household) pairs are seated."""

    def __init__(self, rows=None, seated=None) -> None:
        self.rows: dict[tuple[str, str], str | None] = dict(rows or {})
        self.seated: set[tuple[str, str]] = set(seated or self.rows)

    async def list_local_member_user_ids(self, space_id):
        return ["me"]

    async def list_member_instances(self, space_id):
        return [i for (s, i) in self.seated if s == space_id]

    async def set_space_instance_mailbox(self, s, i, v):
        if (s, i) in self.seated:
            self.rows[(s, i)] = v

    async def list_space_instance_mailboxes(self):
        return [(s, i, v) for (s, i), v in self.rows.items() if v is not None]


def _joiner(space="s1", gen=1):
    ident = generate_identity_keypair()
    kw = generate_x25519_keypair()
    iid = derive_instance_id(ident.public_key)
    keys = derive_mailbox_keys(b"\2" * 32, "g", space, gen, peer="host")
    entry = build_entry(
        keys,
        instance_id=iid,
        identity_seed=ident.private_key,
        gfs_url="http://g",
        proto_version=88,
        keywrap_pk=kw.public_key,
        keywrap_sig=b64url_encode(sign_ed25519(ident.private_key, kw.public_key)),
    )
    row = SimpleNamespace(
        source=InstanceSource.SPACE_SESSION,
        remote_identity_pk=ident.public_key.hex(),
        relay_via="http://g",
    )
    return iid, entry, row


def _update(sender, raw, space="s1"):
    return FederationEvent(
        msg_id="m",
        event_type=FederationEventType.SPACE_MAILBOX_UPDATED,
        from_instance=sender,
        to_instance="i",
        timestamp="2026-11-09T00:00:00+00:00",
        payload={"mailbox": raw},
        space_id=space,
    )


async def test_a_null_update_then_an_entry_is_taken_on_a_row_without_own():
    """Round-3 F1: the host minted no mailbox (own = null) and the joiner
    reported its own lost (null) — the row must stay a mailbox pair, so the
    joiner's later entry is still taken (it was wiped to NULL for good)."""
    iid, entry, row = _joiner()

    class _Repo:
        async def get_instance(self, instance_id):
            return row if instance_id == iid else None

    spaces = _RowSpaces(
        {("s1", iid): orjson.dumps({"peer": entry.to_wire(), "own": None}).decode()}
    )
    svc = _bare_service(spaces)
    svc._federation_repo = _Repo()  # type: ignore[assignment]
    await svc._load_rows()
    await svc._on_mailbox_updated(_update(iid, None))
    assert orjson.loads(spaces.rows[("s1", iid)]) == {"peer": None, "own": None}
    assert await svc.target_for(iid, "s1") is None  # fall back meanwhile
    await svc._on_mailbox_updated(_update(iid, entry.to_wire()))
    assert orjson.loads(spaces.rows[("s1", iid)])["peer"] == entry.to_wire()
    target = await svc.target_for(iid, "s1")
    assert target is not None and target.mailbox_id == entry.mailbox_id


async def test_a_late_own_waits_for_the_seat_and_keeps_the_newest_entry():
    """Round-3 F4: a host mint past its bound may finish before the seat row
    exists — nothing is written (it stays pending) until it does, and then
    only OUR half is set: the joiner's entry there now is kept."""
    iid, newer, _row = _joiner(gen=2)
    spaces = _RowSpaces(seated=set())
    svc = _bare_service(spaces)
    key = _claim_key("s1", iid, OWN)
    svc._pending[key] = svc._clock() + 1800
    assert await svc.set_own("s1", iid, OWN) is False
    assert key in svc._pending and spaces.rows == {}
    spaces.seated.add(("s1", iid))
    spaces.rows[("s1", iid)] = orjson.dumps(
        {"peer": newer.to_wire(), "own": None}
    ).decode()
    await svc._load_rows()
    assert await svc.set_own("s1", iid, OWN) is True
    row = orjson.loads(spaces.rows[("s1", iid)])
    assert row["peer"] == newer.to_wire()  # never the redeem's older entry
    assert row["own"]["gen"] == OWN.gen
    assert key not in svc._pending


async def test_a_gfs_refusing_every_claim_cannot_burn_tickets_or_storm(monkeypatch):
    """Round-3 F6: per member pair and day, re-registrations and announces
    are capped — a GFS that answers every claim with 4401 costs at most
    RECOVERY_REGISTERS_PER_DAY register attempts and ANNOUNCES_PER_DAY
    updates to the one counterpart."""
    spaces = _RowSpaces(
        {
            ("s1", "host"): orjson.dumps(
                {"peer": None, "own": {"gen": 7, "gfs_id": "g", "gfs": "http://g"}}
            ).decode()
        }
    )
    svc = _bare_service(spaces)
    svc._root = b"\1" * 32
    keys = derive_mailbox_keys(b"\1" * 32, "g", "s1", 7, peer="host")
    claim = _Claim(keys=keys, own=OWN, peer="host")
    svc._claims[_claim_key("s1", "host", OWN)] = claim
    announced: list = []
    registers: list = []

    async def _announce(space_id, instance_id, entry):
        announced.append(entry)

    async def _register(self, conn, k, *, fetch=True):
        registers.append(k.mailbox_id)
        return REGISTER_NO_TICKET  # never refused: no rotation either

    svc.attach_announcer(_announce)
    monkeypatch.setattr(SpaceMailboxService, "_register", _register)
    for _ in range(50):
        claim.lost = False  # each episode: claimed, then 4401 again
        await svc._recover(object(), claim)  # type: ignore[arg-type]
    assert len(registers) == RECOVERY_REGISTERS_PER_DAY
    assert len(announced) == ANNOUNCES_PER_DAY
    # A new day resets the budget.
    svc._clock = lambda: 10 * 86_400.0  # type: ignore[assignment]
    claim.lost = False
    await svc._recover(object(), claim)  # type: ignore[arg-type]
    assert len(registers) == RECOVERY_REGISTERS_PER_DAY + 1


async def test_remember_without_own_keeps_a_late_own():
    """Round 5: the post-commit ``remember(own=None)`` can land after a late
    ``set_own`` — it must keep that own, never null it (the sweep would then
    unregister a mailbox the joiner was already told about)."""
    iid, entry, _row = _joiner()
    spaces = _RowSpaces(seated={("s1", iid)})
    svc = _bare_service(spaces)
    assert await svc.set_own("s1", iid, OWN) is True
    assert await svc.remember("s1", iid, peer=entry, own=None) is True
    row = orjson.loads(spaces.rows[("s1", iid)])
    assert row["own"]["gen"] == OWN.gen
    assert row["peer"] == entry.to_wire()


async def test_a_pair_without_entries_is_still_marked():
    """A v_88 seat where neither side minted is written as an empty pair
    row, so the sweep's retry can find it later."""
    spaces = _RowSpaces(seated={("s1", "host")})
    svc = _bare_service(spaces)
    assert await svc.remember("s1", "host", peer=None, own=None) is True
    assert orjson.loads(spaces.rows[("s1", "host")]) == {"peer": None, "own": None}


def test_claim_connects_are_spread_out():
    """Round 5: claims reopened together (start, a GFS restart) must not
    connect — or ping — in step: each waits its own uniform(0, 60 s) first,
    and each socket gets its own heartbeat period."""
    svc = _bare_service(_RowSpaces(), rng=random.Random(3))
    delays = [svc._claim_jitter() for _ in range(20)]
    lo, hi = CLAIM_JITTER_S
    assert lo == 0.0 and hi >= 60.0
    assert all(lo <= d <= hi for d in delays) and len(set(delays)) == 20
    beats = [svc._heartbeat() for _ in range(20)]
    assert all(20.0 <= b <= 40.0 for b in beats) and len(set(beats)) == 20


async def test_reopened_claims_wait_their_own_delay(monkeypatch):
    spaces = _RowSpaces(
        {
            (f"s{n}", "host"): orjson.dumps(
                {"peer": None, "own": {"gen": n, "gfs_id": "g", "gfs": "http://g"}}
            ).decode()
            for n in range(3)
        }
    )
    svc = _bare_service(spaces, rng=random.Random(5))
    svc._root = b"\1" * 32
    seen: list = []

    async def _loop(self, claim):
        seen.append(claim.start_delay)

    monkeypatch.setattr(SpaceMailboxService, "_claim_loop", _loop)
    monkeypatch.setattr(SpaceMailboxService, "_sweep_loop", _noop)
    await svc.start()
    await asyncio.sleep(0)
    assert len(seen) == 3 and len(set(seen)) == 3
    assert all(0.0 <= d <= CLAIM_JITTER_S[1] for d in seen)
    await svc.stop()


async def _noop(self):
    return None
