"""An ``UNPAIR`` tears down exactly one pairing: the signer's own (§11, §24.11).

Unpairing is a trust decision with no confirmation step on the receiving
side, so the receiver must act only on an envelope that the paired peer
itself signed — verified against the identity key pinned at pairing — and
only ever drop *that* peer's row. A forged envelope (another household's
key, or a stranger's), or a genuine one naming a third household in its
payload, must leave every other pairing intact.
"""

from __future__ import annotations

from datetime import datetime, timezone

import orjson
import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import PeerUnpaired
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure import EventBus, KeyManager
from socialhome.repositories import SqliteFederationRepo, SqliteOutboxRepo
from socialhome.repositories.dm_routing_repo import SqliteDmRoutingRepo
from socialhome.services.federation_inbound import PairingInboundHandlers
from socialhome.services.peer_unpair_service import PeerUnpairService

pytestmark = pytest.mark.security

_SESSION_KEY = b"\x21" * 32


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    own_kp = generate_identity_keypair()
    own_iid = derive_instance_id(own_kp.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (own_iid, own_kp.private_key.hex(), own_kp.public_key.hex(), "aa" * 32),
    )
    fed_repo = SqliteFederationRepo(db)
    outbox = SqliteOutboxRepo(db)
    bus = EventBus()
    key_mgr = KeyManager.from_data_dir(tmp_dir)
    svc = FederationService(
        db=db,
        federation_repo=fed_repo,
        outbox_repo=outbox,
        key_manager=key_mgr,
        bus=bus,
        own_instance_id=own_iid,
        own_identity_seed=own_kp.private_key,
        own_identity_pk=own_kp.public_key,
    )
    await svc.warm_replay_cache()
    PairingInboundHandlers(
        bus=bus,
        federation_repo=fed_repo,
        peer_unpair=PeerUnpairService(
            bus=bus,
            federation=svc,
            federation_repo=fed_repo,
            outbox_repo=outbox,
            routing_repo=SqliteDmRoutingRepo(db),
        ),
    ).attach_to(svc)
    unpaired: list[str] = []

    async def _on_unpaired(e: PeerUnpaired) -> None:
        unpaired.append(e.instance_id)

    bus.subscribe(PeerUnpaired, _on_unpaired)

    wrapped = key_mgr.encrypt(_SESSION_KEY)
    peers = {}
    for name in ("a", "c"):
        kp = generate_identity_keypair()
        iid = derive_instance_id(kp.public_key)
        await fed_repo.save_instance(
            RemoteInstance(
                id=iid,
                display_name=name,
                remote_identity_pk=kp.public_key.hex(),
                key_self_to_remote=wrapped,
                key_remote_to_self=wrapped,
                remote_inbox_url=f"https://{name}.invalid/wh",
                local_inbox_id=f"wh-{name}",
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.MANUAL,
            )
        )
        peers[name] = (kp, iid)
    yield {
        "svc": svc,
        "repo": fed_repo,
        "own_iid": own_iid,
        "peers": peers,
        "unpaired": unpaired,
    }
    await db.shutdown()


def _unpair_envelope(
    *, signer_seed: bytes, from_instance: str, to_instance: str, payload: dict
) -> bytes:
    signer = FederationEncoder(signer_seed, sig_suite="ed25519")
    envelope: dict = {
        "msg_id": f"unpair-{from_instance[:8]}-{signer_seed[:4].hex()}",
        "event_type": FederationEventType.UNPAIR.value,
        "from_instance": from_instance,
        "to_instance": to_instance,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "encrypted_payload": signer.encrypt_payload(
            orjson.dumps(payload).decode(), _SESSION_KEY
        ),
        "space_id": None,
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    envelope["signatures"] = signer.sign_envelope_all(
        orjson.dumps(envelope), suite="ed25519"
    )
    return orjson.dumps(envelope)


async def test_unpair_signed_by_the_peer_removes_only_that_peer(env):
    a_kp, a_id = env["peers"]["a"]
    _, c_id = env["peers"]["c"]
    body = _unpair_envelope(
        signer_seed=a_kp.private_key,
        from_instance=a_id,
        to_instance=env["own_iid"],
        payload={},
    )
    assert await env["svc"].handle_inbound_envelope("wh-a", body) == {"status": "ok"}
    assert await env["repo"].get_instance(a_id) is None
    assert await env["repo"].get_instance(c_id) is not None
    assert env["unpaired"] == [a_id]


async def test_unpair_signed_by_another_paired_peer_is_rejected(env):
    """C (a real, paired household) forges A's UNPAIR."""
    _, a_id = env["peers"]["a"]
    c_kp, _ = env["peers"]["c"]
    body = _unpair_envelope(
        signer_seed=c_kp.private_key,
        from_instance=a_id,
        to_instance=env["own_iid"],
        payload={},
    )
    with pytest.raises(ValueError, match="Invalid envelope signature"):
        await env["svc"].handle_inbound_envelope("wh-a", body)
    assert await env["repo"].get_instance(a_id) is not None
    assert env["unpaired"] == []


async def test_unpair_signed_by_a_stranger_is_rejected(env):
    _, a_id = env["peers"]["a"]
    stranger = generate_identity_keypair()
    body = _unpair_envelope(
        signer_seed=stranger.private_key,
        from_instance=a_id,
        to_instance=env["own_iid"],
        payload={},
    )
    with pytest.raises(ValueError, match="Invalid envelope signature"):
        await env["svc"].handle_inbound_envelope("wh-a", body)
    assert await env["repo"].get_instance(a_id) is not None
    assert env["unpaired"] == []


async def test_genuine_unpair_naming_a_third_household_drops_only_the_signer(env):
    """C's own, validly signed UNPAIR cannot carry A's teardown in its body."""
    _, a_id = env["peers"]["a"]
    c_kp, c_id = env["peers"]["c"]
    body = _unpair_envelope(
        signer_seed=c_kp.private_key,
        from_instance=c_id,
        to_instance=env["own_iid"],
        payload={"instance_id": a_id, "from_instance": a_id},
    )
    assert await env["svc"].handle_inbound_envelope("wh-c", body) == {"status": "ok"}
    assert await env["repo"].get_instance(c_id) is None
    assert await env["repo"].get_instance(a_id) is not None
    assert env["unpaired"] == [c_id]
