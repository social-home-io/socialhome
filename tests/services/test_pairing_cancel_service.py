"""Tests for :class:`PairingCancelService` — cancelling a pairing that was
never confirmed (§11).

The first half drives the service against in-memory fakes; the second half
races it against a real confirm (our own SAS entry, the peer's signed
peer-confirm) over a real SQLite database: whichever commits first wins,
a cancel never leaves a row behind, and a refused cancel never told the
peer anything.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import PeerUnpaired
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEventType,
    InstanceSource,
    PairingAlreadyConfirmedError,
    PairingStatus,
    PendingPairingNotFoundError,
    RemoteInstance,
)
from socialhome.federation.pairing_coordinator import PairingCoordinator
from socialhome.federation.peer_pairing_client import sign_peer_body
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.services.pairing_cancel_service import (
    PAIRING_CANCEL_REASON,
    PairingCancelService,
)

_PENDING = (PairingStatus.PENDING_SENT, PairingStatus.PENDING_RECEIVED)


def _qr_pending(iid: str = "peer-q") -> RemoteInstance:
    """A QR half-pair: keys + inbox are on the row from the scan / the
    peer-accept, the SAS has not been entered yet."""
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=f"https://{iid}/wh",
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.PENDING_RECEIVED,
        source=InstanceSource.MANUAL,
    )


def _auto_pending(iid: str = "peer-a") -> RemoteInstance:
    """A trust-relay request we sent: a provisional row with no key."""
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="",
        key_self_to_remote="",
        key_remote_to_self="",
        remote_inbox_url="",
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.PENDING_SENT,
        source=InstanceSource.MANUAL,
        relay_via="peer-b",
    )


class _Repo:
    def __init__(self, log: list) -> None:
        self.instances: dict[str, RemoteInstance] = {}
        self.sessions: dict[str, str] = {}  # token → own_local_inbox_id
        self._log = log
        #: Run inside ``cancel_pending_instance`` before it decides — lets a
        #: test confirm (or drop) the row at the worst possible moment.
        self.before_cancel = None

    async def get_instance(self, iid, *, include_unpairing=False):
        inst = self.instances.get(iid)
        if inst is None:
            return None
        if inst.status is PairingStatus.UNPAIRING and not include_unpairing:
            return None
        return inst

    async def cancel_pending_instance(self, iid):
        if self.before_cancel is not None:
            self.before_cancel()
        self._log.append(("cancel_txn", iid))
        inst = self.instances.get(iid)
        if inst is None or inst.status not in _PENDING:
            return None
        tokens = [t for t, i in self.sessions.items() if i == inst.local_inbox_id]
        for t in tokens:
            del self.sessions[t]
        del self.instances[iid]
        return tokens


class _Federation:
    def __init__(self, log: list) -> None:
        self._log = log
        self.sent: list[dict] = []
        self.result = "ok"  # ok | fail | hang | raise

    async def send_event_once(self, *, instance, event_type, payload):
        self._log.append(("send", instance.id))
        self.sent.append(
            {"to": instance.id, "event_type": event_type, "payload": payload}
        )
        if self.result == "hang":
            await asyncio.sleep(10)
        if self.result == "raise":
            raise RuntimeError("boom")
        return DeliveryResult(
            instance_id=instance.id,
            ok=self.result == "ok",
            error=None if self.result == "ok" else "unreachable",
        )


class _AutoPair:
    def __init__(self, log: list) -> None:
        self._log = log

    def forget_requests_to(self, target_instance_id):
        self._log.append(("forget_auto_pair", target_instance_id))
        return 1


@pytest.fixture
def env():
    log: list = []
    repo = _Repo(log)
    fed = _Federation(log)
    bus = EventBus()
    published: list[PeerUnpaired] = []
    bus.subscribe(PeerUnpaired, published.append)
    svc = PairingCancelService(
        bus=bus,
        federation=fed,
        federation_repo=repo,
        auto_pair=_AutoPair(log),
        notify_timeout_s=0.05,
    )

    class Env:
        pass

    e = Env()
    e.log, e.repo, e.fed, e.svc, e.published = log, repo, fed, svc, published
    return e


async def test_unknown_household_is_not_found(env):
    with pytest.raises(PendingPairingNotFoundError):
        await env.svc.cancel_pending("nope")
    assert env.fed.sent == [] and env.published == []


async def test_confirmed_pairing_is_refused_untouched(env):
    env.repo.instances["peer-c"] = replace(
        _qr_pending("peer-c"), status=PairingStatus.CONFIRMED
    )
    env.repo.sessions["tok"] = "wh-peer-c"
    with pytest.raises(PairingAlreadyConfirmedError):
        await env.svc.cancel_pending("peer-c")
    assert "peer-c" in env.repo.instances
    assert env.repo.sessions == {"tok": "wh-peer-c"}
    assert env.fed.sent == [] and env.published == []


async def test_unpair_tombstone_is_not_found(env):
    env.repo.instances["peer-t"] = replace(
        _qr_pending("peer-t"), status=PairingStatus.UNPAIRING
    )
    with pytest.raises(PendingPairingNotFoundError):
        await env.svc.cancel_pending("peer-t")
    assert "peer-t" in env.repo.instances


async def test_qr_half_pair_is_forgotten_first_and_then_the_peer_is_told(env):
    env.repo.instances["peer-q"] = _qr_pending()
    env.repo.sessions["tok-q"] = "wh-peer-q"
    env.repo.sessions["tok-other"] = "wh-other"

    assert await env.svc.cancel_pending("peer-q") is True

    # The transaction (sessions + row) commits before anything leaves this
    # household — so a cancel that loses a race has told the peer nothing.
    assert env.log == [
        ("cancel_txn", "peer-q"),
        ("forget_auto_pair", "peer-q"),
        ("send", "peer-q"),
    ]
    assert env.fed.sent == [
        {
            "to": "peer-q",
            "event_type": FederationEventType.PAIRING_ABORT,
            "payload": {"token": "tok-q", "reason": PAIRING_CANCEL_REASON},
        }
    ]
    assert env.repo.sessions == {"tok-other": "wh-other"}
    assert "peer-q" not in env.repo.instances
    assert [e.instance_id for e in env.published] == ["peer-q"]


async def test_half_pair_without_a_session_still_tells_the_peer(env):
    """The peer's own handler drops its half-pair on the signer alone."""
    env.repo.instances["peer-q"] = _qr_pending()
    assert await env.svc.cancel_pending("peer-q") is True
    assert env.fed.sent[0]["payload"] == {"reason": PAIRING_CANCEL_REASON}


@pytest.mark.parametrize("outcome", ["fail", "hang", "raise"])
async def test_unreachable_peer_never_blocks_the_cancel(env, outcome):
    """Fail-soft: no tombstone, no retry — the peer's own half expires with
    its session — and the cancel still completes."""
    env.fed.result = outcome
    env.repo.instances["peer-q"] = _qr_pending()
    env.repo.sessions["tok-q"] = "wh-peer-q"

    assert await env.svc.cancel_pending("peer-q") is False

    assert "peer-q" not in env.repo.instances
    assert env.repo.sessions == {}
    assert [e.instance_id for e in env.published] == ["peer-q"]


async def test_trust_relay_request_is_dropped_without_a_message(env):
    """A request we sent through a trust relay has no key to the target:
    nothing is sent, the in-memory request is forgotten (a late approval
    is ignored) and the provisional row goes."""
    env.repo.instances["peer-a"] = _auto_pending()

    assert await env.svc.cancel_pending("peer-a") is False

    assert env.fed.sent == []
    assert ("forget_auto_pair", "peer-a") in env.log
    assert "peer-a" not in env.repo.instances
    assert [e.instance_id for e in env.published] == ["peer-a"]


async def test_confirmed_meanwhile_is_a_conflict_and_nothing_was_sent(env):
    env.repo.instances["peer-q"] = _qr_pending()
    env.repo.sessions["tok-q"] = "wh-peer-q"

    def _confirm() -> None:
        env.repo.instances["peer-q"] = replace(
            env.repo.instances["peer-q"], status=PairingStatus.CONFIRMED
        )

    env.repo.before_cancel = _confirm
    with pytest.raises(PairingAlreadyConfirmedError):
        await env.svc.cancel_pending("peer-q")

    assert env.repo.instances["peer-q"].status is PairingStatus.CONFIRMED
    assert env.fed.sent == []
    assert env.published == []


async def test_gone_meanwhile_is_not_found_and_publishes_nothing(env):
    """Another cancel (or the peer's own abort) took the row first: this
    one reports 404 and does not announce the removal a second time."""
    env.repo.instances["peer-q"] = _qr_pending()
    env.repo.before_cancel = lambda: env.repo.instances.pop("peer-q")

    with pytest.raises(PendingPairingNotFoundError):
        await env.svc.cancel_pending("peer-q")
    assert env.fed.sent == [] and env.published == []


async def test_double_cancel_publishes_once(env):
    env.repo.instances["peer-q"] = _qr_pending()
    results = await asyncio.gather(
        env.svc.cancel_pending("peer-q"),
        env.svc.cancel_pending("peer-q"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, PendingPairingNotFoundError) for r in results) == 1
    assert [e.instance_id for e in env.published] == ["peer-q"]
    assert len(env.fed.sent) == 1


async def test_works_without_the_auto_pair_coordinator(env):
    svc = PairingCancelService(
        bus=EventBus(),
        federation=env.fed,
        federation_repo=env.repo,
    )
    env.repo.instances["peer-q"] = _qr_pending()
    assert await svc.cancel_pending("peer-q") is True
    assert "peer-q" not in env.repo.instances


# ─── Races against a real confirm, real SQLite ────────────────────────────


class _SlowFed:
    """``send_event_once`` that yields *ticks* times before answering —
    widens the window in which a confirm can interleave."""

    def __init__(self, ticks: int) -> None:
        self.ticks = ticks
        self.sent: list = []

    async def send_event_once(self, *, instance, event_type, payload):
        self.sent.append(event_type)
        for _ in range(self.ticks):
            await asyncio.sleep(0)
        return DeliveryResult(instance_id=instance.id, ok=False, error="unreachable")


@pytest.fixture
async def race(tmp_dir):
    db = AsyncDatabase(tmp_dir / "race.db", batch_timeout_ms=10)
    await db.startup()
    repo = SqliteFederationRepo(db)
    own = generate_identity_keypair()
    coord = PairingCoordinator(repo, KeyManager(b"k" * 32), own.public_key)
    peer = generate_identity_keypair()
    accepted = await coord.accept(
        {
            "token": "tok-race",
            "identity_pk": peer.public_key.hex(),
            "dh_pk": generate_x25519_keypair().public_key.hex(),
            "inbox_url": "https://peer.example/federation/inbox/abc",
        },
        "https://me.example/federation/inbox",
    )

    class Race:
        pass

    r = Race()
    r.repo, r.coord, r.accepted = repo, coord, accepted
    r.peer, r.peer_id = peer, derive_instance_id(peer.public_key)
    yield r
    await db.shutdown()


async def _check_outcome(race, fed, cancel_outcome, confirm_ok: bool) -> None:
    final = await race.repo.get_instance(race.peer_id)
    if cancel_outcome == "cancelled":
        # No resurrection: a successful cancel leaves nothing behind, and
        # the confirm that lost the race failed.
        assert final is None
        assert not confirm_ok
    else:
        # The confirm won: the pairing is live and the peer was never
        # told to abort it.
        assert cancel_outcome == "conflict"
        assert confirm_ok
        assert final is not None and final.status is PairingStatus.CONFIRMED
        assert fed.sent == []


async def _cancel(svc, iid) -> str:
    try:
        await svc.cancel_pending(iid)
        return "cancelled"
    except PairingAlreadyConfirmedError:
        return "conflict"


@pytest.mark.parametrize("ticks", [0, 1, 3, 8])
@pytest.mark.parametrize("first", ["confirm", "cancel"])
async def test_own_sas_entry_racing_a_cancel_never_resurrects(race, ticks, first):
    fed = _SlowFed(ticks)
    svc = PairingCancelService(
        bus=EventBus(), federation=fed, federation_repo=race.repo
    )

    async def confirm() -> bool:
        try:
            await race.coord.confirm("tok-race", race.accepted["verification_code"])
            return True
        except ValueError:
            return False

    if first == "confirm":
        confirm_ok, outcome = await asyncio.gather(
            confirm(), _cancel(svc, race.peer_id)
        )
    else:
        outcome, confirm_ok = await asyncio.gather(
            _cancel(svc, race.peer_id), confirm()
        )
    await _check_outcome(race, fed, outcome, confirm_ok)


@pytest.mark.parametrize("ticks", [0, 1, 3, 8])
@pytest.mark.parametrize("first", ["peer", "cancel"])
async def test_peer_confirm_racing_a_cancel_never_resurrects(race, ticks, first):
    fed = _SlowFed(ticks)
    svc = PairingCancelService(
        bus=EventBus(), federation=fed, federation_repo=race.repo
    )
    body = sign_peer_body(
        {"token": "tok-race", "instance_id": race.peer_id},
        own_identity_seed=race.peer.private_key,
    )

    async def peer_confirm() -> bool:
        try:
            await race.coord.handle_peer_confirm(
                body, expected_local_inbox_id=race.accepted["local_inbox_id"]
            )
            return True
        except ValueError:
            return False

    if first == "peer":
        confirm_ok, outcome = await asyncio.gather(
            peer_confirm(), _cancel(svc, race.peer_id)
        )
    else:
        outcome, confirm_ok = await asyncio.gather(
            _cancel(svc, race.peer_id), peer_confirm()
        )
    await _check_outcome(race, fed, outcome, confirm_ok)
