"""Tests for :class:`PairingCancelService` — cancelling a pairing that was
never confirmed (§11)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

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
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.pairing_cancel_service import (
    PAIRING_CANCEL_REASON,
    PairingCancelService,
)


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
        #: Run inside ``delete_pending_instance`` before it decides — lets a
        #: test confirm (or drop) the row at the worst possible moment.
        self.before_delete = None

    async def get_instance(self, iid, *, include_unpairing=False):
        inst = self.instances.get(iid)
        if inst is None:
            return None
        if inst.status is PairingStatus.UNPAIRING and not include_unpairing:
            return None
        return inst

    async def pop_pairings_for_inbox(self, local_inbox_id):
        self._log.append(("pop_pairings", local_inbox_id))
        tokens = [t for t, i in self.sessions.items() if i == local_inbox_id]
        for t in tokens:
            del self.sessions[t]
        return tokens

    async def delete_pending_instance(self, iid):
        if self.before_delete is not None:
            self.before_delete()
        self._log.append(("delete_pending", iid))
        inst = self.instances.get(iid)
        if inst is None or inst.status not in (
            PairingStatus.PENDING_SENT,
            PairingStatus.PENDING_RECEIVED,
        ):
            return False
        del self.instances[iid]
        return True


class _Federation:
    def __init__(self, repo: _Repo, log: list) -> None:
        self._repo = repo
        self._log = log
        self.sent: list[dict] = []
        self.result = "ok"  # ok | fail | hang | raise

    async def send_event(self, *, to_instance_id, event_type, payload):
        # The row (keys, inbox) must still exist when the abort is sealed.
        assert to_instance_id in self._repo.instances
        self._log.append(("send", to_instance_id))
        self.sent.append(
            {"to": to_instance_id, "event_type": event_type, "payload": payload}
        )
        if self.result == "hang":
            await asyncio.sleep(10)
        if self.result == "raise":
            raise RuntimeError("boom")
        return DeliveryResult(
            instance_id=to_instance_id,
            ok=self.result == "ok",
            error=None if self.result == "ok" else "queued",
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
    fed = _Federation(repo, log)
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


async def test_qr_half_pair_closes_the_session_tells_the_peer_then_forgets(env):
    env.repo.instances["peer-q"] = _qr_pending()
    env.repo.sessions["tok-q"] = "wh-peer-q"
    env.repo.sessions["tok-other"] = "wh-other"

    assert await env.svc.cancel_pending("peer-q") is True

    # The session goes first (nothing can finish the handshake from then
    # on), the abort is sealed while the row still holds the keys, and the
    # row goes last.
    assert env.log == [
        ("pop_pairings", "wh-peer-q"),
        ("forget_auto_pair", "peer-q"),
        ("send", "peer-q"),
        ("delete_pending", "peer-q"),
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


async def test_confirmed_meanwhile_is_a_conflict_and_stays(env):
    env.repo.instances["peer-q"] = _qr_pending()
    env.repo.sessions["tok-q"] = "wh-peer-q"

    def _confirm() -> None:
        env.repo.instances["peer-q"] = replace(
            env.repo.instances["peer-q"], status=PairingStatus.CONFIRMED
        )

    env.repo.before_delete = _confirm
    with pytest.raises(PairingAlreadyConfirmedError):
        await env.svc.cancel_pending("peer-q")

    assert env.repo.instances["peer-q"].status is PairingStatus.CONFIRMED
    assert env.published == []


async def test_gone_meanwhile_still_completes(env):
    """The peer's own abort (or the expiry sweep) took the row first: the
    pairing is cancelled either way."""
    env.repo.instances["peer-q"] = _qr_pending()
    env.repo.before_delete = lambda: env.repo.instances.pop("peer-q")

    assert await env.svc.cancel_pending("peer-q") is True
    assert [e.instance_id for e in env.published] == ["peer-q"]


async def test_works_without_the_auto_pair_coordinator(env):
    svc = PairingCancelService(
        bus=EventBus(),
        federation=env.fed,
        federation_repo=env.repo,
    )
    env.repo.instances["peer-q"] = _qr_pending()
    assert await svc.cancel_pending("peer-q") is True
    assert "peer-q" not in env.repo.instances
