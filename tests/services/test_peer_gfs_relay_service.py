"""Tests for :class:`PeerGfsRelayService` — the per-connection GFS
fallback switch (v_54)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import PeerCapabilitiesAdvertised
from socialhome.domain.federation import (
    GfsConnection,
    GfsRelayNotAllowedError,
    GfsRelayPeerNotFoundError,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services.peer_gfs_relay_service import PeerGfsRelayService


class _Wire:
    def __init__(self, *, fail_send: bool = False, fail_probe: bool = False):
        self.keywrap_to: list[str] = []
        self.probed: list[str] = []
        self.probe_kwargs: list[dict] = []
        self._fail_send = fail_send
        self._fail_probe = fail_probe

    async def send_keywrap(self, instance_id: str) -> bool:
        self.keywrap_to.append(instance_id)
        if self._fail_send:
            raise RuntimeError("peer unreachable")
        return True

    async def probe(self, instance_id: str, **kw) -> int:
        self.probed.append(instance_id)
        self.probe_kwargs.append(kw)
        if self._fail_probe:
            raise RuntimeError("gfs down")
        return 1


def _peer(iid: str = "peer-1", **kw) -> RemoteInstance:
    base = RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://peer.example/inbox",
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        proto_version=54,
    )
    return replace(base, **kw)


@pytest.fixture
async def env(tmp_path):
    db = AsyncDatabase(tmp_path / "t.db", batch_timeout_ms=10)
    await db.startup()
    repo = SqliteFederationRepo(db)
    gfs_repo = SqliteGfsConnectionRepo(db)
    await gfs_repo.save(
        GfsConnection(
            id="conn-1",
            gfs_instance_id="gfs-1",
            display_name="GFS",
            public_key="ab" * 32,
            inbox_url="https://gfs.example",
            status="active",
            paired_at="2026-01-01T00:00:00+00:00",
        )
    )
    yield db, repo
    await db.shutdown()


def _service(repo, wire: _Wire, bus: EventBus | None = None) -> PeerGfsRelayService:
    return PeerGfsRelayService(
        federation_repo=repo,
        send_keywrap=wire.send_keywrap,
        probe_peer=wire.probe,
        bus=bus,
    )


async def test_switching_on_opts_in_then_sends_the_key_then_probes(env):
    _db, repo = env
    await repo.save_instance(_peer())
    wire = _Wire()

    row = await _service(repo, wire).set_gfs_relay("peer-1", enabled=True)

    assert row.gfs_relay is True
    assert (await repo.get_instance("peer-1")).gfs_relay is True
    assert wire.keywrap_to == ["peer-1"]
    assert wire.probed == ["peer-1"]
    # The admin's probe never opens the per-peer throttle window: the
    # other household's probe, once it switches on, must get our probe-back.
    assert wire.probe_kwargs == [{"hold_throttle": False}]


async def test_switching_on_again_repeats_the_hand_over(env):
    """A second "on" is the admin's retry, not a no-op."""
    _db, repo = env
    await repo.save_instance(_peer())
    wire = _Wire()
    svc = _service(repo, wire)

    await svc.set_gfs_relay("peer-1", enabled=True)
    await svc.set_gfs_relay("peer-1", enabled=True)

    assert wire.keywrap_to == ["peer-1", "peer-1"]


async def test_switching_off_clears_the_opt_in_and_every_route(env):
    _db, repo = env
    await repo.save_instance(_peer())
    await repo.set_gfs_relay("peer-1", enabled=True)
    await repo.upsert_gfs_route("peer-1", "conn-1", now="2026-10-08T00:00:00+00:00")
    await repo.save_instance(_peer("peer-2"))
    await repo.upsert_gfs_route("peer-2", "conn-1", now="2026-10-08T00:00:00+00:00")
    wire = _Wire()

    row = await _service(repo, wire).set_gfs_relay("peer-1", enabled=False)

    assert row.gfs_relay is False
    assert await repo.list_gfs_routes("peer-1") == []
    # Another peer's routes are untouched.
    assert len(await repo.list_gfs_routes("peer-2")) == 1
    assert wire.keywrap_to == [] and wire.probed == []


async def test_unknown_peer_is_refused(env):
    _db, repo = env
    with pytest.raises(GfsRelayPeerNotFoundError):
        await _service(repo, _Wire()).set_gfs_relay("nobody", enabled=True)


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": InstanceSource.SPACE_SESSION, "remote_inbox_url": ""},
        {"status": PairingStatus.PENDING_SENT},
    ],
    ids=["link-joined", "pending"],
)
async def test_only_a_confirmed_direct_pair_can_be_switched(env, overrides):
    _db, repo = env
    await repo.save_instance(_peer(**overrides))
    wire = _Wire()

    with pytest.raises(GfsRelayNotAllowedError):
        await _service(repo, wire).set_gfs_relay("peer-1", enabled=True)

    assert (await repo.get_instance("peer-1")).gfs_relay is False
    assert wire.keywrap_to == [] and wire.probed == []


async def test_a_failed_key_send_still_saves_the_switch_and_probes(env, caplog):
    _db, repo = env
    await repo.save_instance(_peer())
    wire = _Wire(fail_send=True)

    row = await _service(repo, wire).set_gfs_relay("peer-1", enabled=True)

    assert row.gfs_relay is True
    assert wire.probed == ["peer-1"]
    assert "could not send our key-wrap key" in caplog.text


async def test_a_failed_probe_never_fails_the_switch(env, caplog):
    _db, repo = env
    await repo.save_instance(_peer())

    row = await _service(repo, _Wire(fail_probe=True)).set_gfs_relay(
        "peer-1", enabled=True
    )

    assert row.gfs_relay is True
    assert "route probe for peer-1 failed" in caplog.text


async def test_a_newly_learned_peer_key_triggers_a_probe(env):
    _db, repo = env
    bus = EventBus()
    wire = _Wire()
    _service(repo, wire, bus).wire()

    await bus.publish(PeerCapabilitiesAdvertised(instance_id="peer-1"))
    assert wire.probed == []

    await bus.publish(
        PeerCapabilitiesAdvertised(instance_id="peer-1", keywrap_learned=True)
    )
    assert wire.probed == ["peer-1"]
    # A peer-driven trigger keeps the throttle.
    assert wire.probe_kwargs == [{}]


async def test_wire_without_a_bus_is_a_no_op(env):
    _db, repo = env
    _service(repo, _Wire()).wire()
