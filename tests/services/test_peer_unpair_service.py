"""Tests for :class:`PeerUnpairService` — the one teardown path for a
pairing, whichever side ends it (§11)."""

from __future__ import annotations

import asyncio
import time

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
from socialhome.services.peer_unpair_service import PeerUnpairService


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

    async def get_instance(self, iid):
        return self.instances.get(iid)

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
        self.result = "ok"  # "ok" | "fail" | "hang" | "raise"

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


@pytest.mark.parametrize("outcome", ["fail", "raise"])
async def test_undeliverable_unpair_still_removes_locally(env, outcome):
    env["repo"].instances["peer-b"] = _inst("peer-b")
    env["outbox"].rows["peer-b"] = ["queued-unpair"]
    env["fed"].result = outcome

    assert await env["svc"].unpair("peer-b") is False

    assert "peer-b" not in env["repo"].instances
    # The UNPAIR send_event just queued can never be redelivered once the
    # row is gone — it is purged with the rest.
    assert env["outbox"].rows == {}
    assert [e.instance_id for e in env["published"]] == ["peer-b"]


async def test_hanging_peer_does_not_block_unpair(env):
    env["repo"].instances["peer-c"] = _inst("peer-c")
    env["fed"].result = "hang"

    started = time.monotonic()
    assert await env["svc"].unpair("peer-c") is False
    assert time.monotonic() - started < 1.0

    assert "peer-c" not in env["repo"].instances
    assert [e.instance_id for e in env["published"]] == ["peer-c"]


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
