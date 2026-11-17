"""Tests for :class:`PendingPairings` — where a pending pairing lives.

In the ``remote_instances`` row, as ever — or, when that row is a live
space seat, beside it in memory so the seat keeps carrying the space until
the people have confirmed.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
    is_paired,
    is_space_seat,
)
from socialhome.federation.pending_pairing import (
    STAGED_MAX,
    PendingPairings,
    TooManyPendingPairingsError,
)

PEER = "peer-1"


def _inst(
    status: PairingStatus,
    *,
    source: InstanceSource = InstanceSource.MANUAL,
    iid: str = PEER,
    inbox: str = "pair-inbox",
    keyed: bool = True,
) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="ab" * 32 if keyed else "",
        key_self_to_remote="k1" if keyed else "",
        key_remote_to_self="k2" if keyed else "",
        remote_inbox_url="https://peer/inbox/x" if keyed else "",
        local_inbox_id=inbox,
        status=status,
        source=source,
    )


def _seat(iid: str = PEER) -> RemoteInstance:
    return _inst(
        PairingStatus.CONFIRMED,
        source=InstanceSource.SPACE_SESSION,
        iid=iid,
        inbox="seat-inbox",
    )


class _Repo:
    def __init__(self) -> None:
        self.rows: dict[str, RemoteInstance] = {}
        self.calls: list[tuple] = []
        self.upgrade_ok = True

    async def get_instance(self, iid, *, include_unpairing=False):
        return self.rows.get(iid)

    async def save_instance(self, inst):
        self.calls.append(("save", inst.id))
        self.rows[inst.id] = inst
        return inst

    async def promote_pending_instance(self, inst, *, token=None):
        self.calls.append(("promote", inst.id, token))
        row = self.rows.get(inst.id)
        if row is None or row.status is PairingStatus.CONFIRMED:
            return False
        self.rows[inst.id] = inst
        return True

    async def upgrade_seat_instance(self, inst, *, token=None):
        self.calls.append(("upgrade", inst.id, token))
        if not self.upgrade_ok:
            return False
        self.rows[inst.id] = inst
        return True

    async def upsert_gfs_route(self, instance_id, gfs_connection_id, *, now):
        self.calls.append(("route", instance_id, gfs_connection_id))

    async def cancel_pairing_sessions(self, *, local_inbox_id, peer_identity_pk):
        self.calls.append(("sessions", local_inbox_id, peer_identity_pk))
        return ["tok"]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def repo():
    return _Repo()


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def pending(repo, clock):
    return PendingPairings(repo, clock=clock)


# ── The two predicates ──────────────────────────────────────────────────


def test_a_seat_is_not_a_pairing():
    seat = _seat()
    assert is_space_seat(seat) and not is_paired(seat)
    pair = _inst(PairingStatus.CONFIRMED)
    assert is_paired(pair) and not is_space_seat(pair)
    for status in (
        PairingStatus.PENDING_SENT,
        PairingStatus.PENDING_RECEIVED,
        PairingStatus.UNPAIRING,
    ):
        for source in InstanceSource:
            row = _inst(status, source=source)
            assert not is_paired(row) and not is_space_seat(row)
    assert not is_paired(None) and not is_space_seat(None)


# ── save ────────────────────────────────────────────────────────────────


async def test_without_a_seat_the_pending_row_is_written_as_ever(pending, repo):
    half = _inst(PairingStatus.PENDING_RECEIVED)
    assert await pending.save(half, ttl_s=300) is False
    assert repo.rows[PEER] is half
    assert pending.staged(PEER) is None


async def test_over_a_pending_row_it_is_written_as_ever(pending, repo):
    repo.rows[PEER] = _inst(PairingStatus.PENDING_SENT, keyed=False)
    half = _inst(PairingStatus.PENDING_RECEIVED)
    assert await pending.save(half, ttl_s=300) is False
    assert repo.rows[PEER] is half


async def test_over_a_seat_the_pending_half_is_kept_beside_it(pending, repo):
    seat = repo.rows[PEER] = _seat()
    half = _inst(PairingStatus.PENDING_RECEIVED)
    assert await pending.save(half, ttl_s=300) is True
    # The seat row is untouched: it keeps carrying the space.
    assert repo.rows[PEER] is seat and repo.calls == []
    assert pending.staged(PEER) is half
    assert pending.list_staged() == [half]


async def test_only_a_pending_instance_can_be_saved(pending):
    with pytest.raises(ValueError, match="pending"):
        await pending.save(_inst(PairingStatus.CONFIRMED), ttl_s=300)


async def test_a_staged_half_expires_with_its_ttl(pending, repo, clock):
    repo.rows[PEER] = _seat()
    await pending.save(_inst(PairingStatus.PENDING_SENT, inbox="appr"), ttl_s=300)
    clock.now += 299
    assert pending.staged(PEER) is not None
    assert pending.keyed_for_inbox("appr") is not None
    clock.now += 2
    assert pending.staged(PEER) is None
    assert pending.list_staged() == []
    assert pending.keyed_for_inbox("appr") is None


async def test_a_new_handshake_replaces_the_staged_one(pending, repo):
    repo.rows[PEER] = _seat()
    await pending.save(_inst(PairingStatus.PENDING_RECEIVED, inbox="old"), ttl_s=300)
    newer = _inst(PairingStatus.PENDING_SENT, inbox="new")
    await pending.save(newer, ttl_s=300)
    assert pending.list_staged() == [newer]


async def test_a_row_write_drops_a_stale_staged_half(pending, repo):
    """The seat ended while a half was staged; the next handshake is an
    ordinary row again and must not leave the old half behind it."""
    repo.rows[PEER] = _seat()
    await pending.save(_inst(PairingStatus.PENDING_RECEIVED), ttl_s=300)
    del repo.rows[PEER]
    await pending.save(_inst(PairingStatus.PENDING_SENT, keyed=False), ttl_s=300)
    assert pending.staged(PEER) is None


async def test_the_store_is_bounded(pending, repo):
    for n in range(STAGED_MAX):
        repo.rows[f"p{n}"] = _seat(f"p{n}")
        await pending.save(_inst(PairingStatus.PENDING_SENT, iid=f"p{n}"), ttl_s=300)
    repo.rows["one-more"] = _seat("one-more")
    with pytest.raises(TooManyPendingPairingsError):
        await pending.save(_inst(PairingStatus.PENDING_SENT, iid="one-more"), ttl_s=300)
    # Replacing one already there is not growth.
    await pending.save(_inst(PairingStatus.PENDING_SENT, iid="p0"), ttl_s=300)


async def test_an_expired_half_makes_room(pending, repo, clock):
    for n in range(STAGED_MAX):
        repo.rows[f"p{n}"] = _seat(f"p{n}")
        await pending.save(_inst(PairingStatus.PENDING_SENT, iid=f"p{n}"), ttl_s=10)
    clock.now += 11
    repo.rows["later"] = _seat("later")
    assert await pending.save(_inst(PairingStatus.PENDING_SENT, iid="later"), ttl_s=10)


# ── get: the pairing's view of a household ──────────────────────────────


async def test_get_hides_a_seat(pending, repo):
    """A seat is not a pairing: the handshake sees "nothing yet"."""
    repo.rows[PEER] = _seat()
    assert await pending.get(PEER) is None


async def test_get_returns_the_staged_half_over_the_seat(pending, repo):
    repo.rows[PEER] = _seat()
    half = _inst(PairingStatus.PENDING_RECEIVED)
    await pending.save(half, ttl_s=300)
    assert await pending.get(PEER) is half


async def test_get_returns_any_other_row(pending, repo):
    assert await pending.get(PEER) is None
    for row in (_inst(PairingStatus.PENDING_SENT), _inst(PairingStatus.CONFIRMED)):
        repo.rows[PEER] = row
        assert await pending.get(PEER) is row


# ── promote ─────────────────────────────────────────────────────────────


async def test_promote_of_a_row_goes_through_the_row(pending, repo):
    half = repo.rows[PEER] = _inst(PairingStatus.PENDING_RECEIVED)
    confirmed = replace(half, status=PairingStatus.CONFIRMED)
    assert await pending.promote(confirmed, token="tok")
    assert repo.calls == [("promote", PEER, "tok")]


async def test_promote_of_a_staged_half_replaces_the_seat(pending, repo):
    repo.rows[PEER] = _seat()
    half = _inst(PairingStatus.PENDING_RECEIVED)
    await pending.save(half, ttl_s=300, gfs_route="gfs-1")
    confirmed = replace(half, status=PairingStatus.CONFIRMED)
    assert await pending.promote(confirmed, token="tok")
    assert repo.calls == [("upgrade", PEER, "tok"), ("route", PEER, "gfs-1")]
    assert repo.rows[PEER] is confirmed
    assert pending.staged(PEER) is None


async def test_a_refused_upgrade_seeds_no_route_and_forgets_the_half(pending, repo):
    repo.rows[PEER] = _seat()
    half = _inst(PairingStatus.PENDING_RECEIVED)
    await pending.save(half, ttl_s=300, gfs_route="gfs-1")
    repo.upgrade_ok = False
    assert not await pending.promote(replace(half, status=PairingStatus.CONFIRMED))
    assert repo.calls == [("upgrade", PEER, None)]
    assert pending.staged(PEER) is None


async def test_an_expired_half_cannot_be_promoted(pending, repo, clock):
    seat = repo.rows[PEER] = _seat()
    half = _inst(PairingStatus.PENDING_RECEIVED)
    await pending.save(half, ttl_s=300)
    clock.now += 301
    assert not await pending.promote(replace(half, status=PairingStatus.CONFIRMED))
    # It fell through to the row path, which refuses a confirmed row.
    assert repo.rows[PEER] is seat


async def test_promote_must_carry_the_staged_handshake(pending, repo):
    """Another handshake's keys never ride a staged half's confirmation."""
    seat = repo.rows[PEER] = _seat()
    await pending.save(_inst(PairingStatus.PENDING_RECEIVED), ttl_s=300)
    other = _inst(PairingStatus.CONFIRMED, inbox="another-handshake")
    assert not await pending.promote(other)
    assert repo.rows[PEER] is seat
    assert repo.calls == []


# ── the inbox lookup ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status", [PairingStatus.PENDING_SENT, PairingStatus.PENDING_RECEIVED]
)
async def test_a_keyed_staged_half_resolves_by_its_inbox_id(pending, repo, status):
    """An approval and a pairing code's half both resolve; what either may
    then be sent is the pipeline's staged-half gate."""
    repo.rows[PEER] = _seat()
    half = _inst(status, inbox="pair")
    await pending.save(half, ttl_s=300)
    assert pending.keyed_for_inbox("pair") is half
    assert pending.keyed_for_inbox("") is None
    assert pending.keyed_for_inbox("other") is None
    # Never by the seat's own inbox id.
    assert pending.keyed_for_inbox("seat-inbox") is None


async def test_our_own_keyless_request_never_resolves_by_inbox_id(pending, repo):
    repo.rows[PEER] = _seat()
    await pending.save(
        _inst(PairingStatus.PENDING_SENT, inbox="req", keyed=False), ttl_s=300
    )
    assert pending.keyed_for_inbox("req") is None


# ── cancel ──────────────────────────────────────────────────────────────


async def test_cancel_of_a_staged_half_takes_its_sessions_not_the_seat(pending, repo):
    seat = repo.rows[PEER] = _seat()
    half = _inst(PairingStatus.PENDING_RECEIVED)
    await pending.save(half, ttl_s=300)
    assert await pending.cancel(PEER) == (half, ["tok"])
    assert repo.calls == [("sessions", "pair-inbox", "ab" * 32)]
    assert repo.rows[PEER] is seat
    assert pending.staged(PEER) is None


async def test_cancel_without_a_staged_half_is_none(pending, repo):
    repo.rows[PEER] = _inst(PairingStatus.PENDING_RECEIVED)
    assert await pending.cancel(PEER) is None
    assert repo.calls == []


async def test_a_failed_session_delete_keeps_the_half_for_a_retry(pending, repo):
    repo.rows[PEER] = _seat()
    half = _inst(PairingStatus.PENDING_RECEIVED)
    await pending.save(half, ttl_s=300)

    async def _boom(**_kw):
        raise RuntimeError("db down")

    repo.cancel_pairing_sessions = _boom
    with pytest.raises(RuntimeError):
        await pending.cancel(PEER)
    assert pending.staged(PEER) is half


# ── promote: what never confirms, and what an error leaves behind ────────


@pytest.mark.parametrize("staged", [True, False])
async def test_a_pairing_without_keys_is_never_confirmed(pending, repo, staged):
    """Our own keyless request, promoted as if it were the handshake the
    admin just verified, would leave a confirmed row nobody can talk to —
    over a seat, in place of the seat."""
    request = _inst(PairingStatus.PENDING_SENT, keyed=False)
    if staged:
        seat = repo.rows[PEER] = _seat()
        await pending.save(request, ttl_s=300)
    else:
        seat = repo.rows[PEER] = request
    assert not await pending.promote(replace(request, status=PairingStatus.CONFIRMED))
    assert repo.rows[PEER] is seat and repo.calls == []


async def test_a_failed_upgrade_write_keeps_the_half_for_a_retry(pending, repo):
    repo.rows[PEER] = _seat()
    half = _inst(PairingStatus.PENDING_SENT)
    await pending.save(half, ttl_s=300)

    async def _boom(inst, *, token=None):
        raise RuntimeError("db down")

    repo.upgrade_seat_instance = _boom
    with pytest.raises(RuntimeError):
        await pending.promote(replace(half, status=PairingStatus.CONFIRMED))
    assert pending.staged(PEER) is half


async def test_a_failed_route_seed_never_fails_the_confirmation(pending, repo):
    repo.rows[PEER] = _seat()
    half = _inst(PairingStatus.PENDING_RECEIVED)
    await pending.save(half, ttl_s=300, gfs_route="gfs-1")

    async def _boom(*_a, **_kw):
        raise RuntimeError("db down")

    repo.upsert_gfs_route = _boom
    confirmed = replace(half, status=PairingStatus.CONFIRMED)
    assert await pending.promote(confirmed)
    assert repo.rows[PEER] is confirmed and pending.staged(PEER) is None


# ── one winner: a confirm and a cancel of the same half ──────────────────


class _SlowRepo(_Repo):
    """Holds the upgrade write open until the test lets it finish."""

    def __init__(self) -> None:
        super().__init__()
        self.writing = asyncio.Event()
        self.release = asyncio.Event()

    async def upgrade_seat_instance(self, inst, *, token=None):
        self.writing.set()
        await self.release.wait()
        return await super().upgrade_seat_instance(inst, token=token)


async def test_a_cancel_during_the_confirm_loses(clock):
    """The row path decides this in one transaction — whichever commits
    first wins, the other changes nothing. A half kept beside a seat has no
    token to consume (an approval, a request), so the store decides it: a
    cancel that arrives while the confirm is writing finds nothing left."""
    repo = _SlowRepo()
    pending = PendingPairings(repo, clock=clock)
    repo.rows[PEER] = _seat()
    half = _inst(PairingStatus.PENDING_SENT)
    await pending.save(half, ttl_s=300)
    confirmed = replace(half, status=PairingStatus.CONFIRMED)

    promote = asyncio.create_task(pending.promote(confirmed))
    await repo.writing.wait()
    cancel = asyncio.create_task(pending.cancel(PEER))
    await asyncio.sleep(0)
    repo.release.set()

    assert await promote is True
    assert await cancel is None
    assert repo.rows[PEER] is confirmed
    assert ("sessions", "pair-inbox", "ab" * 32) not in repo.calls


async def test_a_handshake_started_during_a_confirm_is_not_swept_away(clock):
    """The confirm's own clean-up must not drop a half that a NEW
    handshake staged while the write was in flight."""
    repo = _SlowRepo()
    pending = PendingPairings(repo, clock=clock)
    repo.rows[PEER] = _seat()
    half = _inst(PairingStatus.PENDING_SENT, inbox="first")
    await pending.save(half, ttl_s=300)
    repo.upgrade_ok = False  # the write will be refused

    promote = asyncio.create_task(
        pending.promote(replace(half, status=PairingStatus.CONFIRMED))
    )
    await repo.writing.wait()
    newer = _inst(PairingStatus.PENDING_RECEIVED, inbox="second")
    save = asyncio.create_task(pending.save(newer, ttl_s=300))
    await asyncio.sleep(0)
    repo.release.set()

    assert await promote is False
    assert await save is True
    assert pending.staged(PEER) is newer
