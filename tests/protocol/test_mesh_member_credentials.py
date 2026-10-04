"""§27.9 release blocker: a member household the host reaches only over the
mesh gets its per-household credentials, sealed end to end — and only on an
origin-authenticated version claim.

Topology (the federation demo's c / b / d): the host ``c`` and the member
``d`` are not paired; ``b`` is paired with both and relays ``SPACE_ROUTED``.
``c`` therefore holds no ``remote_instances`` row for ``d``.

* ``d``'s version + identity-key claim travels d → b → c inside the sealed,
  origin-signed inner payload; ``b`` never reads it. ``c`` records it
  (migration 0078) and from then on issues ``d``'s writer cert under the
  claimed key, which travels c → b → d sealed to ``d`` alone.
* A relay that re-seals a claim of its own under ``d``'s name is dropped by
  the v_31 origin check; a claim naming a key that does not derive to the
  sender is ignored; a member whose version is unknown or too old gets no
  credential at all (fail closed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import pytest

from socialhome.crypto import b64url_encode
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.federation_capabilities import OURS, FederationCapability
from socialhome.domain.writer_cert import WRITER_SCOPE_WRITE, WriterCert
from socialhome.federation import routed_crypto
from socialhome.federation.mesh_member_claim import mesh_member_claim
from socialhome.services.space_writer_cert_service import SpaceWriterCertService
from socialhome.writer_cert import verify_writer_cert
from tests.federation.test_peer_supports import _MeshRepo, _mesh_service
from tests.federation.test_routed_envelope import (
    _build_identity_chain,
    _mint_target_eph,
)
from tests.services.test_space_writer_cert_service import (
    SEED,
    SPACE_PK,
    _Keys,
    _Remote,
    _remote,
    _space,
    _Spaces,
)

pytestmark = pytest.mark.security


async def _settle() -> None:
    for _ in range(12):
        await asyncio.sleep(0)


def _mesh_topology():
    """``[d, b, c]`` with identity-derived ids; ``c`` holds no row (and no
    pinned key) for ``d`` and vice versa — only ``b`` is paired with both."""
    d, b, c = _build_identity_chain(3)
    for x, y in ((c, d), (d, c)):
        x.repo._instances.pop(y.instance_id)
        x.fed.identity_pks.pop(y.instance_id)
        x.fed.peers.pop(y.instance_id)
    return d, b, c


def _wire(node, to: str) -> str:
    return json.dumps([s["payload"] for s in node.fed.sent if s["to"] == to])


def _host(c_id: str, d_id: str, repo: _MeshRepo, *, space=None):
    """The host's real cert service over a federation stand-in whose
    version / key gates are the real ``FederationService`` methods."""
    fed = _mesh_service(repo)
    fed._own_instance_id = c_id

    async def _no_pin(_iid):
        return None

    fed.peer_identity_public_key = _no_pin
    certs = SpaceWriterCertService(
        space_repo=_Spaces(space or _space(), SEED),
        remote_member_repo=_Remote([_remote(d_id, "member")]),
        space_key_repo=_Keys(),
        own_instance_id=c_id,
        own_identity_pk=os.urandom(32),
    )
    certs.attach_federation(fed)  # type: ignore[arg-type]
    return fed, certs


async def test_mesh_member_gets_its_cert_sealed_end_to_end():
    d, b, c = _mesh_topology()
    d_pk = d.fed.identity.public_key

    # 1. d → b → c: the version claim, sealed to c and origin-signed by d.
    await d.handler.send_routed(
        path=[d.instance_id, b.instance_id, c.instance_id],
        target_eph_pk_b64=_mint_target_eph(c),
        inner_event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
        inner_payload={"proto_version": OURS, **mesh_member_claim(d_pk)},
    )
    await _settle()
    relay_view = _wire(d, b.instance_id) + _wire(b, c.instance_id)
    assert "member_proto_version" not in relay_view
    assert "member_identity_pk" not in relay_view
    assert len(c.dispatched) == 1
    claim_event = c.dispatched[0]
    assert claim_event.from_instance == d.instance_id
    assert claim_event.routed_path is not None

    # 2. c records it: d is a seated member household with no row.
    repo = _MeshRepo(seated={d.instance_id})
    fed, certs = _host(c.instance_id, d.instance_id, repo)
    assert await fed.record_mesh_member_claim(claim_event)
    assert await fed.space_member_supports(
        d.instance_id, min_version=FederationCapability.MIN_FOR_PRIVATE_CHANNELS
    )

    # 3. c issues d's OWN cert under the claimed key (per-peer fan-out hook,
    #    the same one the rekey / roster snapshot / redeem ACK use) …
    payload = await certs.peer_payload_hook("sp-1")(d.instance_id, {"space_id": "sp-1"})
    cert_wire = payload["writer_cert"]
    assert WriterCert.from_wire(cert_wire).instance_pk == b64url_encode(d_pk)

    # 4. … and it travels c → b → d sealed to d: b never sees it.
    await c.handler.send_routed(
        path=[c.instance_id, b.instance_id, d.instance_id],
        target_eph_pk_b64=_mint_target_eph(d),
        inner_event_type=FederationEventType.SPACE_ROSTER_SNAPSHOT,
        inner_payload=payload,
    )
    await _settle()
    relay_view = _wire(c, b.instance_id) + _wire(b, d.instance_id)
    assert "writer_cert" not in relay_view
    assert cert_wire["cert_sig"] not in relay_view
    assert cert_wire["instance_pk"] not in relay_view
    assert len(d.dispatched) == 1
    got = WriterCert.from_wire(d.dispatched[0].payload["writer_cert"])
    verify_writer_cert(
        got,
        space_pubkey=SPACE_PK,
        space_id="sp-1",
        epoch=2,
        author_pk=d_pk,
        required_scope=WRITER_SCOPE_WRITE,
    )


async def test_mesh_member_in_a_strict_space_gets_the_writer_key_too():
    d, _b, c = _mesh_topology()
    repo = _MeshRepo(
        seated={d.instance_id},
        claims={d.instance_id: (OURS, d.fed.identity.public_key.hex())},
    )
    _fed, certs = _host(
        c.instance_id, d.instance_id, repo, space=_space(gfs_publish_mode="strict")
    )
    out = await certs.peer_payload_hook("sp-1")(d.instance_id, {})
    assert "writer_cert" in out and "writer_key" in out


@pytest.mark.parametrize(
    "claimed", [None, FederationCapability.MIN_FOR_MEMBER_GFS_PUBLISH - 1]
)
async def test_mesh_member_of_unknown_or_old_version_gets_nothing(claimed):
    d, _b, c = _mesh_topology()
    claims = (
        {}
        if claimed is None
        else {d.instance_id: (claimed, d.fed.identity.public_key.hex())}
    )
    repo = _MeshRepo(seated={d.instance_id}, claims=claims)
    _fed, certs = _host(
        c.instance_id, d.instance_id, repo, space=_space(gfs_publish_mode="strict")
    )
    assert await certs.cert_for_peer("sp-1", d.instance_id) is None
    out = await certs.peer_payload_hook("sp-1")(d.instance_id, {})
    assert "writer_cert" not in out and "writer_key" not in out


async def test_relay_cannot_forge_a_claim_in_the_members_name(caplog):
    """The relay re-seals a claim of its own (a version d never ran) to the
    host's ephemeral key, keeping d's routing fields and origin signature:
    the v_31 origin signature no longer matches the ciphertext, so the
    inner event never reaches a handler and nothing is recorded."""
    caplog.set_level(logging.WARNING)
    d, b, c = _mesh_topology()
    b.fed.peers.pop(c.instance_id)  # capture b's forward instead of delivering
    target_pub = _mint_target_eph(c)
    await d.handler.send_routed(
        path=[d.instance_id, b.instance_id, c.instance_id],
        target_eph_pk_b64=target_pub,
        inner_event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
        inner_payload={
            "proto_version": OURS,
            **mesh_member_claim(d.fed.identity.public_key),
        },
    )
    await _settle()
    genuine = next(s["payload"] for s in b.fed.sent if s["to"] == c.instance_id)
    forged_claim = {
        "proto_version": 9999,
        "member_proto_version": 9999,
        "member_identity_pk": d.fed.identity.public_key.hex(),
    }
    eph_priv, eph_pub = routed_crypto.generate_ephemeral_keypair()
    resealed = routed_crypto.seal_inner_payload(
        inner_payload_json=json.dumps(forged_claim),
        origin_eph_priv_b64=eph_priv,
        origin_eph_pub_b64=eph_pub,
        target_eph_pub_b64=target_pub,
        route_id=genuine["route_id"],
        inner_event_type=genuine["inner_event_type"],
    )
    forged_sealed = {**genuine["sealed"], **resealed}
    for key in ("origin_identity_pk", "origin_sig", "origin_sig_suite"):
        forged_sealed[key] = genuine["sealed"][key]
    await c.handler._on_routed(
        FederationEvent(
            msg_id="forged",
            event_type=FederationEventType.SPACE_ROUTED,
            from_instance=b.instance_id,
            to_instance=c.instance_id,
            timestamp="2026-10-04T00:00:00Z",
            payload={**genuine, "sealed": forged_sealed},
        )
    )
    await _settle()
    assert c.dispatched == []
    assert "origin signature does not verify" in caplog.text


async def test_claim_naming_a_key_that_is_not_the_senders_is_ignored():
    """b (a genuine mesh origin in its own right) claims a version for d's
    id… it can only send under its own id, and a claim carrying any key
    other than the sender's own is refused: nothing recorded for anyone."""
    d, b, c = _mesh_topology()
    # b sends under its OWN id but names d's key in the claim.
    c.repo._instances.pop(b.instance_id)
    c.fed.identity_pks.pop(b.instance_id)
    await b.handler.send_routed(
        path=[b.instance_id, c.instance_id],
        target_eph_pk_b64=_mint_target_eph(c),
        inner_event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
        inner_payload={
            "proto_version": OURS,
            **mesh_member_claim(d.fed.identity.public_key),
        },
    )
    await _settle()
    assert len(c.dispatched) == 1
    repo = _MeshRepo(seated={b.instance_id, d.instance_id})
    fed = _mesh_service(repo)
    assert not await fed.record_mesh_member_claim(c.dispatched[0])
    assert repo.claims == {}
