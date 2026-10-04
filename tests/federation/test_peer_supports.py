"""Tests for ``FederationService.peer_supports(min_version=N)``.

Concrete demonstration of the version-gating discipline laid out in
CLAUDE.md ('Federation protocol versioning'): a v2 sender consults
``peer_supports`` before including a v2-only field, so a v1 peer
never sees the field.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from socialhome.crypto import derive_instance_id, ed25519_public_key
from socialhome.domain.events import PeerProtoVersionRaised
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import OURS, FederationCapability
from socialhome.federation.federation_service import FederationService
from socialhome.federation.mesh_member_claim import mesh_member_claim


def _make_service(peer):
    """Build a tiny FederationService stand-in that exposes the same
    ``peer_supports`` method without touching the dozen other deps
    its full constructor needs."""
    from socialhome.federation.federation_service import FederationService

    repo = SimpleNamespace(get_instance=AsyncMock(return_value=peer))
    svc = SimpleNamespace(
        _federation_repo=repo,
        peer_supports=FederationService.peer_supports,
    )

    async def call(instance_id, *, min_version):
        return await FederationService.peer_supports(
            svc, instance_id, min_version=min_version
        )

    return call


def _peer(instance_id: str, *, proto_version: int) -> RemoteInstance:
    return RemoteInstance(
        id=instance_id,
        display_name=instance_id,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://example.test/x",
        local_inbox_id="local",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        proto_version=proto_version,
    )


@pytest.mark.asyncio
async def test_peer_at_version_supports_that_version_and_below():
    """v2 peer supports v2 and v1; not v3."""
    call = _make_service(_peer("p", proto_version=2))
    assert await call("p", min_version=1) is True
    assert await call("p", min_version=2) is True
    assert await call("p", min_version=3) is False


@pytest.mark.asyncio
async def test_peer_at_v1_does_not_support_v2_field():
    """The classic case the gate exists for: v1 peer ⇒ skip the v2 field."""
    call = _make_service(_peer("p", proto_version=1))
    assert await call("p", min_version=2) is False


@pytest.mark.asyncio
async def test_unknown_peer_does_not_support_anything():
    """A peer not in ``remote_instances`` returns ``False`` so the
    sender omits the optional field rather than guessing."""
    call = _make_service(None)
    assert await call("nope", min_version=1) is False
    assert await call("nope", min_version=2) is False


@pytest.mark.asyncio
async def test_repo_failure_is_fail_soft():
    """Repo lookup raising → ``False``. We never crash the outbound
    fan-out because the version check could not be resolved — sending
    the legacy shape is always safer."""
    from socialhome.federation.federation_service import FederationService

    repo = SimpleNamespace(
        get_instance=AsyncMock(side_effect=RuntimeError("db down")),
    )
    svc = SimpleNamespace(_federation_repo=repo)
    result = await FederationService.peer_supports(svc, "p", min_version=2)
    assert result is False


# ── Mesh-only space members (migration 0078) ──────────────────────────────

MESH_PK = ed25519_public_key(os.urandom(32))
MESH = derive_instance_id(MESH_PK)


class _MeshRepo:
    """``remote_instances`` rows + the household's ``space_instances`` claim."""

    def __init__(self, *, rows=None, seated=(), claims=None):
        self.rows = dict(rows or {})
        self.seated = set(seated)
        self.claims: dict[str, tuple[int, str | None]] = dict(claims or {})

    async def get_instance(self, instance_id):
        return self.rows.get(instance_id)

    async def get_space_member_version(self, instance_id):
        if instance_id not in self.seated:
            return None
        return self.claims.get(instance_id, (0, None))

    async def record_space_member_version(self, instance_id, version, pk_hex):
        if instance_id not in self.seated:
            return None
        before = self.claims.get(instance_id, (0, None))[0]
        self.claims[instance_id] = (max(before, version), pk_hex)
        return before


class _Bus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


def _mesh_service(repo):
    svc = SimpleNamespace(_federation_repo=repo, _own_instance_id="host", _bus=_Bus())

    def bind(name):
        fn = getattr(FederationService, name)

        async def call(*args, **kwargs):
            return await fn(svc, *args, **kwargs)

        return call

    svc.record_mesh_member_claim = bind("record_mesh_member_claim")
    svc.space_member_supports = bind("space_member_supports")
    svc.mesh_member_identity_pk = bind("mesh_member_identity_pk")
    return svc


def _routed_event(payload, *, origin=MESH, routed=True):
    return FederationEvent(
        msg_id="m",
        event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
        from_instance=origin,
        to_instance="host",
        timestamp="t",
        payload=payload,
        routed_path=[origin, "relay", "host"] if routed else None,
    )


@pytest.mark.asyncio
async def test_unknown_mesh_member_supports_nothing():
    """Fail closed: no row and no recorded claim → no version."""
    svc = _mesh_service(_MeshRepo(seated={MESH}))
    assert await svc.space_member_supports(MESH, min_version=1) is False
    svc = _mesh_service(_MeshRepo())
    assert await svc.space_member_supports(MESH, min_version=1) is False
    assert await svc.mesh_member_identity_pk(MESH) is None


@pytest.mark.asyncio
async def test_routed_claim_from_a_seated_mesh_member_is_recorded():
    svc = _mesh_service(_MeshRepo(seated={MESH}))
    ok = await svc.record_mesh_member_claim(_routed_event(mesh_member_claim(MESH_PK)))
    assert ok is True
    assert await svc.space_member_supports(
        MESH, min_version=FederationCapability.MIN_FOR_PRIVATE_CHANNELS
    )
    assert not await svc.space_member_supports(MESH, min_version=OURS + 1)
    assert await svc.mesh_member_identity_pk(MESH) == MESH_PK
    # The household learned a version → the existing catch-up fires (the
    # host re-sends its roster snapshot, which carries the credentials).
    assert svc._bus.events == [
        PeerProtoVersionRaised(
            instance_id=MESH,
            old_version=0,
            new_version=OURS,
            occurred_at=svc._bus.events[0].occurred_at,
        )
    ]


@pytest.mark.asyncio
async def test_same_claim_again_publishes_nothing_new():
    svc = _mesh_service(_MeshRepo(seated={MESH}))
    await svc.record_mesh_member_claim(_routed_event(mesh_member_claim(MESH_PK)))
    await svc.record_mesh_member_claim(_routed_event(mesh_member_claim(MESH_PK)))
    assert len(svc._bus.events) == 1


@pytest.mark.asyncio
async def test_claim_is_a_high_water_mark():
    svc = _mesh_service(_MeshRepo(seated={MESH}, claims={MESH: (60, MESH_PK.hex())}))
    await svc.record_mesh_member_claim(_routed_event(mesh_member_claim(MESH_PK)))
    assert await svc.space_member_supports(MESH, min_version=60)
    assert svc._bus.events == []


@pytest.mark.asyncio
async def test_direct_delivery_claim_is_ignored():
    """Only an origin-authenticated mesh unwrap may carry a claim."""
    svc = _mesh_service(_MeshRepo(seated={MESH}))
    ok = await svc.record_mesh_member_claim(
        _routed_event(mesh_member_claim(MESH_PK), routed=False)
    )
    assert ok is False
    assert not await svc.space_member_supports(MESH, min_version=1)


@pytest.mark.asyncio
async def test_forged_claim_naming_someone_elses_key_is_ignored():
    """A claim whose key does not derive to the authenticated origin — a
    household vouching for a version under another household's id — is
    dropped, and nothing is recorded."""
    forger_pk = ed25519_public_key(os.urandom(32))
    repo = _MeshRepo(seated={MESH})
    svc = _mesh_service(repo)
    ok = await svc.record_mesh_member_claim(_routed_event(mesh_member_claim(forger_pk)))
    assert ok is False
    assert repo.claims == {}
    assert not await svc.space_member_supports(MESH, min_version=1)


@pytest.mark.asyncio
async def test_claim_from_a_non_member_records_nothing():
    """A claim never creates membership."""
    repo = _MeshRepo()
    svc = _mesh_service(repo)
    assert not await svc.record_mesh_member_claim(
        _routed_event(mesh_member_claim(MESH_PK))
    )
    assert repo.claims == {} and svc._bus.events == []


@pytest.mark.asyncio
async def test_paired_household_keeps_its_row_version():
    """A household we hold a row for is judged by that row only — its
    advertised version arrives on the direct path."""
    row = _peer(MESH, proto_version=40)
    repo = _MeshRepo(rows={MESH: row}, seated={MESH})
    svc = _mesh_service(repo)
    assert not await svc.record_mesh_member_claim(
        _routed_event(mesh_member_claim(MESH_PK))
    )
    assert repo.claims == {}
    assert await svc.space_member_supports(MESH, min_version=40)
    assert not await svc.space_member_supports(MESH, min_version=41)
    assert await svc.mesh_member_identity_pk(MESH) is None


@pytest.mark.asyncio
async def test_payload_without_claim_is_ignored():
    svc = _mesh_service(_MeshRepo(seated={MESH}))
    assert not await svc.record_mesh_member_claim(_routed_event({"proto_version": 51}))


@pytest.mark.asyncio
async def test_stored_key_that_does_not_derive_is_not_returned():
    """Defence in depth on read: a corrupted stored key is never used."""
    other = ed25519_public_key(os.urandom(32))
    svc = _mesh_service(_MeshRepo(seated={MESH}, claims={MESH: (51, other.hex())}))
    assert await svc.mesh_member_identity_pk(MESH) is None


@pytest.mark.asyncio
async def test_claim_without_notify_records_but_publishes_nothing():
    svc = _mesh_service(_MeshRepo(seated={MESH}))
    assert await svc.record_mesh_member_claim(
        _routed_event(mesh_member_claim(MESH_PK)), notify=False
    )
    assert await svc.space_member_supports(MESH, min_version=OURS)
    assert svc._bus.events == []
