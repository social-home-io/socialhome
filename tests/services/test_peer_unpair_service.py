"""Tests for :class:`PeerUnpairService` — the one teardown path for a
pairing, whichever side ends it (§11)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.events import PeerUnpaired
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.peer_unpair_service import (
    UNPAIR_RETRY_MAX_AGE,
    PeerUnpairService,
)


def _inst(iid: str) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=f"https://{iid}/wh",
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


class _Log(list):
    """Shared call log so tests can assert cross-collaborator ordering."""


class _FakeFederationRepo:
    def __init__(self, log: _Log) -> None:
        self.instances: dict[str, RemoteInstance] = {}
        self._log = log

    async def get_instance(self, iid, *, include_unpairing=False):
        inst = self.instances.get(iid)
        if inst is None:
            return None
        if inst.status is PairingStatus.UNPAIRING and not include_unpairing:
            return None
        return inst

    async def list_instances(self, *, source=None, status=None):
        return [i for i in self.instances.values() if i.status.value == status]

    async def mark_unpairing(self, iid):
        self._log.append(("mark_unpairing", iid))
        inst = self.instances[iid]
        self.instances[iid] = replace(inst, status=PairingStatus.UNPAIRING)

    async def delete_instance(self, iid):
        self._log.append(("delete_instance", iid))
        self.instances.pop(iid, None)


class _FakeOutboxRepo:
    def __init__(self, log: _Log) -> None:
        self.rows: dict[str, list[str]] = {}
        self._log = log

    async def delete_for_instance(self, iid):
        self._log.append(("outbox_purge", iid))
        self.rows.pop(iid, None)

    async def count_pending_for(self, iid):
        return len(self.rows.get(iid, []))


class _FakeRoutingRepo:
    def __init__(self, log: _Log) -> None:
        self.discovered_via: dict[str, list[str]] = {}
        self._log = log

    async def forget_discovered_via(self, iid):
        self._log.append(("forget_discovered_via", iid))
        self.discovered_via.pop(iid, None)


class _FakeFederation:
    """``send_event`` stand-in: records the call + whether the peer's row
    (keys, inbox) still existed at that moment."""

    def __init__(self, repo: _FakeFederationRepo, log: _Log) -> None:
        self._repo = repo
        self._log = log
        self.sent: list[dict] = []
        self.queued: list[dict] = []
        self.result = "ok"  # "ok" | "fail" | "hang" | "raise"
        self.queue_ok = True
        self._outbox: _FakeOutboxRepo | None = None
        #: Awaited inside ``queue_event`` before the row lands — lets a test
        #: interleave another coroutine at the worst possible point.
        self.before_queue = None

    async def queue_event(self, *, to_instance_id, event_type, payload, expires_at):
        inst = await self._repo.get_instance(to_instance_id, include_unpairing=True)
        self._log.append(("queue_event", to_instance_id))
        if self.before_queue is not None:
            await self.before_queue()
        self.queued.append(
            {
                "to": to_instance_id,
                "event_type": event_type,
                "payload": payload,
                "expires_at": expires_at,
                "status": inst.status if inst else None,
            }
        )
        if not self.queue_ok:
            return None
        assert self._outbox is not None
        self._outbox.rows.setdefault(to_instance_id, []).append("queued-unpair")
        return "msg-1"

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self._log.append(("send_event", to_instance_id))
        self.sent.append(
            {
                "to": to_instance_id,
                "event_type": event_type,
                "payload": payload,
                "row_present": to_instance_id in self._repo.instances,
            }
        )
        if self.result == "hang":
            await asyncio.sleep(3600)
        if self.result == "raise":
            raise RuntimeError("boom")
        return DeliveryResult(
            instance_id=to_instance_id,
            ok=self.result == "ok",
            error=None if self.result == "ok" else "queued",
        )


@pytest.fixture
def env():
    log = _Log()
    bus = EventBus()
    repo = _FakeFederationRepo(log)
    outbox = _FakeOutboxRepo(log)
    routing = _FakeRoutingRepo(log)
    fed = _FakeFederation(repo, log)
    fed._outbox = outbox
    svc = PeerUnpairService(
        bus=bus,
        federation=fed,  # type: ignore[arg-type]
        federation_repo=repo,  # type: ignore[arg-type]
        outbox_repo=outbox,  # type: ignore[arg-type]
        routing_repo=routing,  # type: ignore[arg-type]
        notify_timeout_s=0.05,
    )
    published: list[PeerUnpaired] = []
    bus.subscribe(PeerUnpaired, published.append)
    return {
        "svc": svc,
        "repo": repo,
        "outbox": outbox,
        "routing": routing,
        "fed": fed,
        "log": log,
        "published": published,
    }


async def test_unpair_sends_unpair_before_forgetting(env):
    env["repo"].instances["peer-a"] = _inst("peer-a")
    env["outbox"].rows["peer-a"] = ["m1"]
    env["routing"].discovered_via["peer-a"] = ["peer-x"]

    assert await env["svc"].unpair("peer-a") is True

    assert env["fed"].sent == [
        {
            "to": "peer-a",
            "event_type": FederationEventType.UNPAIR,
            "payload": {},
            "row_present": True,
        }
    ]
    assert [c for c, _ in env["log"]] == [
        "send_event",
        "outbox_purge",
        "forget_discovered_via",
        "delete_instance",
    ]
    assert "peer-a" not in env["repo"].instances
    assert env["outbox"].rows == {}
    assert env["routing"].discovered_via == {}
    assert [e.instance_id for e in env["published"]] == ["peer-a"]


async def test_unpair_unknown_peer_returns_none_and_sends_nothing(env):
    assert await env["svc"].unpair("nope") is None
    assert env["fed"].sent == []
    assert env["log"] == []
    assert env["published"] == []


@pytest.mark.parametrize("outcome", ["fail", "raise", "hang"])
async def test_undeliverable_unpair_leaves_a_tombstone_that_retries(env, outcome):
    """An offline peer must still learn it was unpaired: the row stays as an
    ``unpairing`` tombstone (the outbox needs its keys + inbox URL) and one
    fresh UNPAIR is queued with a bounded lifetime."""
    env["repo"].instances["peer-b"] = _inst("peer-b")
    env["outbox"].rows["peer-b"] = ["queued-post", "queued-unpair-from-send"]
    env["routing"].discovered_via["peer-b"] = ["peer-x"]
    env["fed"].result = outcome

    started = time.monotonic()
    assert await env["svc"].unpair("peer-b") is False
    assert time.monotonic() - started < 1.0

    # Order: close trust first, then drop the old queue, then queue the
    # one envelope the tombstone exists for.
    calls = [c for c, _ in env["log"]]
    assert calls[:1] == ["send_event"]
    assert calls.index("mark_unpairing") < calls.index("outbox_purge")
    assert calls.index("outbox_purge") < calls.index("queue_event")
    assert "delete_instance" not in calls

    inst = env["repo"].instances["peer-b"]
    assert inst.status is PairingStatus.UNPAIRING
    assert env["outbox"].rows == {"peer-b": ["queued-unpair"]}
    assert env["routing"].discovered_via == {}
    (queued,) = env["fed"].queued
    assert queued["event_type"] is FederationEventType.UNPAIR
    assert queued["payload"] == {}
    assert queued["status"] is PairingStatus.UNPAIRING
    expires = datetime.fromisoformat(queued["expires_at"])
    expected = datetime.now(timezone.utc) + UNPAIR_RETRY_MAX_AGE
    assert abs((expires - expected).total_seconds()) < 60
    # The UI drops the connection now — the tombstone is not a connection.
    assert [e.instance_id for e in env["published"]] == ["peer-b"]


async def test_unqueueable_unpair_forgets_immediately(env):
    """No session key to seal with → nothing could ever be delivered, so a
    tombstone would only linger: forget on the spot."""
    env["repo"].instances["peer-b"] = _inst("peer-b")
    env["fed"].result = "fail"
    env["fed"].queue_ok = False

    assert await env["svc"].unpair("peer-b") is False

    assert "peer-b" not in env["repo"].instances
    assert env["outbox"].rows == {}
    assert [e.instance_id for e in env["published"]] == ["peer-b"]


async def test_unpair_of_a_tombstone_is_unknown(env):
    env["repo"].instances["peer-t"] = replace(
        _inst("peer-t"), status=PairingStatus.UNPAIRING
    )
    assert await env["svc"].unpair("peer-t") is None
    assert env["fed"].sent == []


async def test_finish_purges_a_tombstone_once_unpair_is_delivered(env):
    env["repo"].instances["peer-t"] = replace(
        _inst("peer-t"), status=PairingStatus.UNPAIRING
    )
    env["outbox"].rows["peer-t"] = ["queued-unpair"]

    await env["svc"].finish_unpair("peer-t")

    assert "peer-t" not in env["repo"].instances
    assert env["outbox"].rows == {}
    # Already announced when the tombstone was made — not twice.
    assert env["published"] == []


async def test_finish_never_touches_a_live_pairing(env):
    """A stale UNPAIR outcome after a re-pair must not tear the new pair down."""
    env["repo"].instances["peer-a"] = _inst("peer-a")
    env["outbox"].rows["peer-a"] = ["new-post"]

    await env["svc"].finish_unpair("peer-a")
    await env["svc"].finish_unpair("gone")

    assert "peer-a" in env["repo"].instances
    assert env["outbox"].rows == {"peer-a": ["new-post"]}


async def test_sweep_purges_tombstones_whose_unpair_is_gone(env):
    """The UNPAIR expired (max age) or was dropped: the tombstone goes too.
    One that still has its UNPAIR queued is kept."""
    for iid in ("peer-done", "peer-waiting"):
        env["repo"].instances[iid] = replace(_inst(iid), status=PairingStatus.UNPAIRING)
    env["repo"].instances["peer-live"] = _inst("peer-live")
    env["outbox"].rows["peer-waiting"] = ["queued-unpair"]

    assert await env["svc"].sweep_tombstones() == 1

    assert set(env["repo"].instances) == {"peer-waiting", "peer-live"}


async def test_sweep_never_purges_a_tombstone_still_being_made(env):
    """Race: the hourly sweep can run between the status flip and the
    UNPAIR landing in the outbox. At that instant the tombstone has
    nothing queued, and purging it would mean the peer is never told."""
    env["repo"].instances["peer-a"] = _inst("peer-a")
    env["fed"].result = "fail"
    swept: list[int] = []

    async def _sweep_now():
        swept.append(await env["svc"].sweep_tombstones())

    env["fed"].before_queue = _sweep_now

    assert await env["svc"].unpair("peer-a") is False

    assert swept == [0]
    tomb = env["repo"].instances["peer-a"]
    assert tomb.status is PairingStatus.UNPAIRING
    assert env["outbox"].rows == {"peer-a": ["queued-unpair"]}
    # Once it is made, the sweep treats it like any other tombstone.
    assert await env["svc"].sweep_tombstones() == 0
    env["outbox"].rows.clear()
    assert await env["svc"].sweep_tombstones() == 1


def test_retry_max_age_is_thirty_days():
    assert UNPAIR_RETRY_MAX_AGE == timedelta(days=30)


async def test_forget_cleans_up_without_notifying(env):
    """The inbound path: the peer already knows — never echo an UNPAIR."""
    env["repo"].instances["peer-d"] = _inst("peer-d")
    env["outbox"].rows["peer-d"] = ["m1", "m2"]
    env["routing"].discovered_via["peer-d"] = ["peer-y"]

    await env["svc"].forget("peer-d")

    assert env["fed"].sent == []
    assert "peer-d" not in env["repo"].instances
    assert env["outbox"].rows == {}
    assert env["routing"].discovered_via == {}
    assert [e.instance_id for e in env["published"]] == ["peer-d"]
