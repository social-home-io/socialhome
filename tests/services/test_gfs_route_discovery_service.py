"""Unit tests for :mod:`socialhome.services.gfs_route_discovery_service`."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
    GfsConnection,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.federation.event_dispatch_registry import EventDispatchRegistry
from socialhome.services import gfs_route_discovery_service as mod
from socialhome.services.gfs_relay_inbound import RELAY_DELIVERED_VIA
from socialhome.services.gfs_route_discovery_service import (
    MAX_PENDING_PROBES,
    PENDING_PROBE_TTL_S,
    PROBE_ANSWER_MIN_INTERVAL_S,
    PROBE_PEER_MIN_INTERVAL_S,
    ROUTE_MAX_AGE,
    GfsRouteDiscoveryService,
)

PEER = "b" * 32
OTHER = "c" * 32
URL_X = "https://x.example"
URL_Y = "https://y.example"


def _conn(cid: str, url: str, status: str = "active") -> GfsConnection:
    return GfsConnection(
        id=cid,
        gfs_instance_id=f"gfs-{cid}",
        display_name=cid,
        public_key="aa" * 32,
        inbox_url=url,
        status=status,
        paired_at="2026-01-01T00:00:00+00:00",
    )


def _peer(iid: str = PEER, **kw) -> RemoteInstance:
    base = RemoteInstance(
        id=iid,
        display_name="peer",
        remote_identity_pk="cc" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="",
        local_inbox_id=f"inbox-{iid[:4]}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        proto_version=FederationCapability.MIN_FOR_GFS_RELAY_ROUTES,
        remote_keywrap_pk="dd" * 32,
        gfs_relay=True,
    )
    return dataclasses.replace(base, **kw)


class _Fed:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[dict] = []
        self._event_registry = EventDispatchRegistry()
        self.repo: _FedRepo | None = None

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        assert self.repo is not None
        peer = await self.repo.get_instance(instance_id)
        return peer is not None and peer.proto_version >= min_version

    async def send_event_via_gfs(self, **kw) -> DeliveryResult:
        self.sent.append(kw)
        return DeliveryResult(
            instance_id=kw["to_instance_id"],
            ok=self.ok,
            error=None if self.ok else "gfs_relay_failed",
            via="gfs_relay",
        )


class _FedRepo:
    def __init__(self, *peers: RemoteInstance) -> None:
        self.peers = {p.id: p for p in peers}
        self.routes: dict[tuple[str, str], str] = {}
        self.cutoffs: list[str] = []

    async def get_instance(self, iid: str):
        return self.peers.get(iid)

    async def list_instances(self, *, source=None, status=None):
        return [p for p in self.peers.values() if status in (None, p.status.value)]

    async def upsert_gfs_route(self, iid: str, cid: str, *, now: str) -> None:
        self.routes[(iid, cid)] = now

    async def delete_gfs_routes_older_than(self, cutoff: str) -> int:
        self.cutoffs.append(cutoff)
        stale = [k for k, v in self.routes.items() if v < cutoff]
        for k in stale:
            del self.routes[k]
        return len(stale)


class _GfsRepo:
    def __init__(self, *conns: GfsConnection) -> None:
        self.conns = list(conns)

    async def list_active(self):
        return list(self.conns)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _svc(
    *peers: RemoteInstance,
    conns=(("a-x", URL_X), ("a-y", URL_Y)),
    ok: bool = True,
    supported=None,
):
    fed = _Fed(ok=ok)
    repo = _FedRepo(*peers)
    fed.repo = repo
    gfs_repo = _GfsRepo(*(_conn(c, u) for c, u in conns))
    clock = _Clock()

    async def _supported(conn: GfsConnection) -> bool:
        return True if supported is None else supported(conn)

    svc = GfsRouteDiscoveryService(
        federation=fed,
        federation_repo=repo,
        gfs_connection_repo=gfs_repo,
        envelope_relay_supported=_supported,
        clock=clock,
        now_iso=lambda: "2026-10-08T12:00:00+00:00",
    )
    return svc, fed, repo, gfs_repo, clock


def _event(et: FederationEventType, payload, *, sender: str = PEER):
    return FederationEvent(
        msg_id="m",
        event_type=et,
        from_instance=sender,
        to_instance="a" * 32,
        timestamp="2026-10-08T12:00:00+00:00",
        payload=payload,
    )


async def _deliver(handler, event, *, via: str | None):
    token = RELAY_DELIVERED_VIA.set(via)
    try:
        await handler(event)
    finally:
        RELAY_DELIVERED_VIA.reset(token)


# ── probing ────────────────────────────────────────────────────────────


async def test_probe_goes_through_each_own_relay_connection_with_only_a_nonce():
    svc, fed, *_ = _svc(_peer())

    sent = await svc.probe_peer(PEER)

    assert sent == 2
    assert [s["gfs_url"] for s in fed.sent] == [URL_X, URL_Y]
    for s in fed.sent:
        assert s["event_type"] is FederationEventType.GFS_RELAY_PROBE
        assert set(s["payload"]) == {"nonce"}
        assert len(s["payload"]["nonce"]) >= 22
    assert fed.sent[0]["payload"] != fed.sent[1]["payload"]
    assert svc.pending_count == 2


async def test_probe_skips_inactive_and_non_relay_servers():
    svc, fed, _repo, gfs_repo, _ = _svc(
        _peer(),
        supported=lambda conn: conn.id != "a-x",
    )
    gfs_repo.conns.append(_conn("a-z", "https://z.example", status="pending"))
    gfs_repo.conns.append(_conn("a-w", ""))

    assert await svc.probe_peer(PEER) == 1
    assert [s["gfs_url"] for s in fed.sent] == [URL_Y]


async def test_a_failing_capability_check_skips_only_that_server():
    def _boom(conn):
        if conn.id == "a-x":
            raise RuntimeError("info down")
        return True

    svc, fed, *_ = _svc(_peer(), supported=_boom)

    assert await svc.probe_peer(PEER) == 1
    assert [s["gfs_url"] for s in fed.sent] == [URL_Y]


@pytest.mark.parametrize(
    "changes",
    [
        {"gfs_relay": False},
        {"remote_keywrap_pk": None},
        {"remote_keywrap_pk": ""},
        {"proto_version": FederationCapability.MIN_FOR_GFS_RELAY_ROUTES - 1},
        {"status": PairingStatus.PENDING_SENT},
        {"source": InstanceSource.SPACE_SESSION},
    ],
    ids=[
        "not-opted-in",
        "no-keywrap",
        "empty-keywrap",
        "pre-v53",
        "not-confirmed",
        "link-joined",
    ],
)
async def test_an_ineligible_peer_is_never_probed(changes):
    svc, fed, *_ = _svc(_peer(**changes))

    assert await svc.probe_peer(PEER) == 0
    assert fed.sent == []
    assert svc.pending_count == 0


async def test_an_unknown_peer_is_not_probed():
    svc, fed, *_ = _svc()
    assert await svc.probe_peer(PEER) == 0
    assert fed.sent == []


async def test_no_relay_connection_means_no_probe_and_no_throttle_spent():
    svc, fed, _repo, gfs_repo, _ = _svc(_peer())
    gfs_repo.conns.clear()
    assert await svc.probe_peer(PEER) == 0
    gfs_repo.conns.append(_conn("a-y", URL_Y))
    assert await svc.probe_peer(PEER) == 1


async def test_probes_to_one_peer_are_rate_capped():
    svc, fed, _repo, _gfs, clock = _svc(_peer())

    assert await svc.probe_peer(PEER) == 2
    clock.t += PROBE_PEER_MIN_INTERVAL_S - 1
    assert await svc.probe_peer(PEER) == 0
    clock.t += 2
    assert await svc.probe_peer(PEER) == 2
    assert len(fed.sent) == 4


async def test_a_probe_the_relay_did_not_accept_is_forgotten():
    svc, fed, *_ = _svc(_peer(), ok=False)

    assert await svc.probe_peer(PEER) == 0
    assert len(fed.sent) == 2
    assert svc.pending_count == 0


async def test_probe_all_probes_every_eligible_confirmed_peer():
    svc, fed, *_ = _svc(
        _peer(PEER),
        _peer(OTHER, gfs_relay=False),
        _peer("d" * 32),
    )
    assert await svc.probe_all() == 4
    assert {s["to_instance_id"] for s in fed.sent} == {PEER, "d" * 32}


async def test_pending_probes_are_capped_oldest_first(monkeypatch):
    monkeypatch.setattr(mod, "MAX_PENDING_PROBES", 3)
    svc, fed, _repo, _gfs, clock = _svc(_peer(), _peer(OTHER))

    await svc.probe_peer(PEER)
    first_nonce = fed.sent[0]["payload"]["nonce"]
    await svc.probe_peer(OTHER)

    assert svc.pending_count == 3
    # The oldest probe was evicted: its ack is now unknown.
    await _deliver(
        svc._on_probe_ack,
        _event(FederationEventType.GFS_RELAY_PROBE_ACK, {"nonce": first_nonce}),
        via="a-x",
    )
    assert _repo.routes == {}


async def test_expired_pending_probes_are_pruned_on_the_next_probe():
    svc, fed, _repo, _gfs, clock = _svc(_peer(), _peer(OTHER))
    await svc.probe_peer(PEER)
    clock.t += PENDING_PROBE_TTL_S + 1
    await svc.probe_peer(OTHER)
    assert svc.pending_count == 2
    assert MAX_PENDING_PROBES >= 2


# ── receiving a probe ──────────────────────────────────────────────────


async def test_a_relayed_probe_records_the_route_and_acks_via_the_same_server():
    svc, fed, repo, *_ = _svc(_peer(), conns=(("b-y", URL_Y), ("b-z", "https://z")))

    await _deliver(
        svc._on_probe,
        _event(FederationEventType.GFS_RELAY_PROBE, {"nonce": "n" * 22}),
        via="b-y",
    )

    assert repo.routes == {(PEER, "b-y"): "2026-10-08T12:00:00+00:00"}
    assert fed.sent == [
        {
            "to_instance_id": PEER,
            "event_type": FederationEventType.GFS_RELAY_PROBE_ACK,
            "payload": {"nonce": "n" * 22},
            "gfs_url": URL_Y,
        }
    ]


async def test_a_probe_that_did_not_arrive_over_the_relay_is_ignored():
    svc, fed, repo, *_ = _svc(_peer())

    await _deliver(
        svc._on_probe,
        _event(FederationEventType.GFS_RELAY_PROBE, {"nonce": "n" * 22}),
        via=None,
    )

    assert repo.routes == {}
    assert fed.sent == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"nonce": 7},
        {"nonce": "short"},
        {"nonce": "x" * 65},
        {"nonce": "a b" * 8},
        [],
    ],
)
async def test_a_malformed_probe_is_ignored(payload):
    svc, fed, repo, *_ = _svc(_peer())

    await _deliver(
        svc._on_probe,
        _event(FederationEventType.GFS_RELAY_PROBE, payload),
        via="a-x",
    )

    assert repo.routes == {}
    assert fed.sent == []


async def test_answers_to_one_peer_over_one_server_are_rate_capped():
    svc, fed, repo, _gfs, clock = _svc(_peer())
    probe = _event(FederationEventType.GFS_RELAY_PROBE, {"nonce": "n" * 22})

    await _deliver(svc._on_probe, probe, via="a-x")
    await _deliver(svc._on_probe, probe, via="a-x")
    # Another server is its own budget.
    await _deliver(svc._on_probe, probe, via="a-y")
    clock.t += PROBE_ANSWER_MIN_INTERVAL_S
    await _deliver(svc._on_probe, probe, via="a-x")

    assert [s["gfs_url"] for s in fed.sent] == [URL_X, URL_Y, URL_X]


async def test_the_answer_throttle_is_bounded(monkeypatch):
    monkeypatch.setattr(mod, "MAX_ANSWER_ENTRIES", 2)
    svc, fed, repo, _gfs, clock = _svc(_peer())
    probe = _event(FederationEventType.GFS_RELAY_PROBE, {"nonce": "n" * 22})

    await _deliver(svc._on_probe, probe, via="a-x")
    await _deliver(svc._on_probe, probe, via="a-y")
    clock.t += 1
    await _deliver(svc._on_probe, probe, via="a-q")

    assert len(svc._last_answer_at) <= 2
    clock.t += PROBE_ANSWER_MIN_INTERVAL_S
    await _deliver(svc._on_probe, probe, via="a-r")
    assert len(svc._last_answer_at) <= 2


async def test_a_probe_over_a_vanished_connection_records_but_cannot_answer():
    svc, fed, repo, *_ = _svc(_peer())

    await _deliver(
        svc._on_probe,
        _event(FederationEventType.GFS_RELAY_PROBE, {"nonce": "n" * 22}),
        via="gone",
    )

    assert (PEER, "gone") in repo.routes
    assert fed.sent == []


async def test_an_ack_the_relay_refuses_is_only_logged():
    svc, fed, repo, *_ = _svc(_peer(), ok=False)

    await _deliver(
        svc._on_probe,
        _event(FederationEventType.GFS_RELAY_PROBE, {"nonce": "n" * 22}),
        via="a-x",
    )

    assert len(fed.sent) == 1
    assert (PEER, "a-x") in repo.routes


# ── receiving an ack ───────────────────────────────────────────────────


async def _probed(svc, fed) -> dict[str, str]:
    """Probe PEER; return {connection id: nonce}."""
    await svc.probe_peer(PEER)
    by_url = {s["gfs_url"]: s["payload"]["nonce"] for s in fed.sent}
    return {"a-x": by_url[URL_X], "a-y": by_url[URL_Y]}


async def test_an_ack_over_the_probed_server_confirms_the_route():
    svc, fed, repo, *_ = _svc(_peer())
    nonces = await _probed(svc, fed)

    await _deliver(
        svc._on_probe_ack,
        _event(FederationEventType.GFS_RELAY_PROBE_ACK, {"nonce": nonces["a-y"]}),
        via="a-y",
    )

    assert repo.routes == {(PEER, "a-y"): "2026-10-08T12:00:00+00:00"}
    assert svc.pending_count == 1


async def test_an_ack_is_accepted_once():
    svc, fed, repo, *_ = _svc(_peer())
    nonces = await _probed(svc, fed)
    ack = _event(FederationEventType.GFS_RELAY_PROBE_ACK, {"nonce": nonces["a-y"]})

    await _deliver(svc._on_probe_ack, ack, via="a-y")
    repo.routes.clear()
    await _deliver(svc._on_probe_ack, ack, via="a-y")

    assert repo.routes == {}


async def test_an_ack_via_a_different_server_than_probed_is_ignored():
    svc, fed, repo, *_ = _svc(_peer())
    nonces = await _probed(svc, fed)

    await _deliver(
        svc._on_probe_ack,
        _event(FederationEventType.GFS_RELAY_PROBE_ACK, {"nonce": nonces["a-x"]}),
        via="a-y",
    )
    await _deliver(
        svc._on_probe_ack,
        _event(FederationEventType.GFS_RELAY_PROBE_ACK, {"nonce": nonces["a-x"]}),
        via=None,
    )

    assert repo.routes == {}
    assert svc.pending_count == 2


async def test_an_ack_from_a_different_peer_is_ignored():
    svc, fed, repo, *_ = _svc(_peer(), _peer(OTHER))
    nonces = await _probed(svc, fed)

    await _deliver(
        svc._on_probe_ack,
        _event(
            FederationEventType.GFS_RELAY_PROBE_ACK,
            {"nonce": nonces["a-x"]},
            sender=OTHER,
        ),
        via="a-x",
    )

    assert repo.routes == {}
    # The genuine ack still lands afterwards.
    await _deliver(
        svc._on_probe_ack,
        _event(FederationEventType.GFS_RELAY_PROBE_ACK, {"nonce": nonces["a-x"]}),
        via="a-x",
    )
    assert (PEER, "a-x") in repo.routes


async def test_an_ack_for_an_unknown_nonce_is_ignored():
    svc, fed, repo, *_ = _svc(_peer())
    await _probed(svc, fed)

    await _deliver(
        svc._on_probe_ack,
        _event(FederationEventType.GFS_RELAY_PROBE_ACK, {"nonce": "z" * 22}),
        via="a-x",
    )
    await _deliver(
        svc._on_probe_ack,
        _event(FederationEventType.GFS_RELAY_PROBE_ACK, {"nonce": None}),
        via="a-x",
    )

    assert repo.routes == {}


async def test_an_ack_after_the_ttl_is_ignored_and_forgotten():
    svc, fed, repo, _gfs, clock = _svc(_peer())
    nonces = await _probed(svc, fed)
    clock.t += PENDING_PROBE_TTL_S + 1

    await _deliver(
        svc._on_probe_ack,
        _event(FederationEventType.GFS_RELAY_PROBE_ACK, {"nonce": nonces["a-x"]}),
        via="a-x",
    )

    assert repo.routes == {}
    assert svc.pending_count == 1


# ── expiry + wiring ────────────────────────────────────────────────────


async def test_expiry_drops_routes_not_refreshed_for_three_intervals():
    svc, _fed, repo, *_ = _svc(_peer())
    now = datetime.now(timezone.utc)
    repo.routes[(PEER, "old")] = (now - ROUTE_MAX_AGE - timedelta(hours=1)).isoformat()
    repo.routes[(PEER, "fresh")] = (now - timedelta(hours=1)).isoformat()

    assert await svc.expire_stale_routes() == 1
    assert list(repo.routes) == [(PEER, "fresh")]
    assert ROUTE_MAX_AGE == timedelta(hours=72)


async def test_expiry_with_nothing_stale_is_quiet():
    svc, _fed, repo, *_ = _svc(_peer())
    assert await svc.expire_stale_routes() == 0


async def test_attach_to_registers_both_handlers():
    svc, fed, *_ = _svc()
    svc.attach_to(fed)
    reg = fed._event_registry
    assert reg.handler_count(FederationEventType.GFS_RELAY_PROBE) == 1
    assert reg.handler_count(FederationEventType.GFS_RELAY_PROBE_ACK) == 1


def test_the_default_clock_stamps_tz_aware_utc():
    stamp = mod._utc_now_iso()
    assert stamp.endswith("+00:00")
