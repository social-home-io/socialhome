"""A peer we unpaired while it was offline is a tombstone, not a peer (§11).

``PeerUnpairService.unpair`` keeps an unreachable peer's
``remote_instances`` row in ``status='unpairing'`` so the outbox can keep
retrying the signed ``UNPAIR`` until it lands. That row still holds the
peer's identity key and session keys, so it must grant **no** trust:

* every normal envelope the peer sends is refused exactly like an envelope
  from a household we never knew (``No instance found`` → 404) — on the
  HTTPS inbox and on the DataChannel alike;
* only the peer's own signed ``UNPAIR`` is still accepted — it ends the
  tombstone on the spot;
* a refused envelope proves the peer is back, so the queued ``UNPAIR`` is
  pulled forward instead of waiting out its backoff;
* the row is invisible to every ordinary read (lookup, lists, fan-outs);
* a fresh pairing with the same household replaces it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import orjson
import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
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
from socialhome.repositories.dm_media_outbox_repo import SqliteDmMediaOutboxRepo
from socialhome.repositories.dm_routing_repo import SqliteDmRoutingRepo
from socialhome.repositories.space_media_outbox_repo import (
    SqliteSpaceMediaOutboxRepo,
)
from socialhome.services.federation_inbound import PairingInboundHandlers
from socialhome.services.peer_unpair_service import PeerUnpairService

pytestmark = pytest.mark.security

_SESSION_KEY = b"\x21" * 32
_FAR_FUTURE = "2999-01-01T00:00:00+00:00"


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
    unpair = PeerUnpairService(
        bus=bus,
        federation=svc,
        federation_repo=fed_repo,
        outbox_repo=outbox,
        routing_repo=SqliteDmRoutingRepo(db),
        dm_media_outbox_repo=SqliteDmMediaOutboxRepo(db),
        space_media_outbox_repo=SqliteSpaceMediaOutboxRepo(db),
    )
    PairingInboundHandlers(
        bus=bus,
        federation_repo=fed_repo,
        peer_unpair=unpair,
    ).attach_to(svc)

    wrapped = key_mgr.encrypt(_SESSION_KEY)
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    await fed_repo.save_instance(
        RemoteInstance(
            id=iid,
            display_name="c",
            remote_identity_pk=kp.public_key.hex(),
            key_self_to_remote=wrapped,
            key_remote_to_self=wrapped,
            remote_inbox_url="https://c.invalid/wh",
            local_inbox_id="wh-c",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        )
    )
    # Tombstone it the way an unpair of an offline peer does: row kept in
    # ``unpairing``, one UNPAIR queued with a far-off retry.
    await fed_repo.mark_unpairing(iid)
    await db.enqueue(
        "INSERT INTO federation_outbox(id, instance_id, event_type,"
        " payload_json, next_attempt_at) VALUES(?,?,?,?,?)",
        ("q-unpair", iid, FederationEventType.UNPAIR.value, "{}", _FAR_FUTURE),
    )
    yield {
        "db": db,
        "svc": svc,
        "repo": fed_repo,
        "outbox": outbox,
        "own_iid": own_iid,
        "kp": kp,
        "iid": iid,
        "wrapped": wrapped,
    }
    await db.shutdown()


def _envelope(
    *,
    signer_seed: bytes,
    from_instance: str,
    to_instance: str,
    event_type: FederationEventType,
    msg_id: str,
) -> bytes:
    signer = FederationEncoder(signer_seed, sig_suite="ed25519")
    envelope: dict = {
        "msg_id": msg_id,
        "event_type": event_type.value,
        "from_instance": from_instance,
        "to_instance": to_instance,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "encrypted_payload": signer.encrypt_payload(
            orjson.dumps({}).decode(), _SESSION_KEY
        ),
        "space_id": None,
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    envelope["signatures"] = signer.sign_envelope_all(
        orjson.dumps(envelope), suite="ed25519"
    )
    return orjson.dumps(envelope)


async def _next_attempt(env) -> str:
    return str(
        await env["db"].fetchval(
            "SELECT next_attempt_at FROM federation_outbox WHERE id='q-unpair'"
        )
    )


@pytest.mark.parametrize(
    "event_type",
    [
        FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
        FederationEventType.URL_UPDATED,
        FederationEventType.PRESENCE_UPDATED,
        FederationEventType.PAIRING_CONFIRM,
    ],
)
async def test_normal_event_from_tombstone_is_refused_on_the_inbox(env, event_type):
    body = _envelope(
        signer_seed=env["kp"].private_key,
        from_instance=env["iid"],
        to_instance=env["own_iid"],
        event_type=event_type,
        msg_id=f"m-{event_type.value}",
    )
    with pytest.raises(ValueError, match="No instance found"):
        await env["svc"].handle_inbound_envelope("wh-c", body)
    # Still a tombstone — nothing it sent could revive or reshape it.
    inst = await env["repo"].get_instance(env["iid"], include_unpairing=True)
    assert inst is not None and inst.status is PairingStatus.UNPAIRING


async def test_normal_event_from_tombstone_is_refused_on_the_datachannel(env):
    body = _envelope(
        signer_seed=env["kp"].private_key,
        from_instance=env["iid"],
        to_instance=env["own_iid"],
        event_type=FederationEventType.PRESENCE_UPDATED,
        msg_id="m-rtc",
    )
    with pytest.raises(ValueError, match="No instance found"):
        await env["svc"].handle_inbound_rtc(env["iid"], body)


async def test_refused_envelope_pulls_the_queued_unpair_forward(env):
    assert await _next_attempt(env) == _FAR_FUTURE
    body = _envelope(
        signer_seed=env["kp"].private_key,
        from_instance=env["iid"],
        to_instance=env["own_iid"],
        event_type=FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
        msg_id="m-nudge",
    )
    with pytest.raises(ValueError):
        await env["svc"].handle_inbound_envelope("wh-c", body)
    due = datetime.fromisoformat(await _next_attempt(env))
    assert due <= datetime.now(timezone.utc)


async def test_forged_envelope_does_not_pull_the_unpair_forward(env):
    """The nudge is gated on the signature — a stranger holding the inbox id
    cannot make us hammer the peer."""
    stranger = generate_identity_keypair()
    body = _envelope(
        signer_seed=stranger.private_key,
        from_instance=env["iid"],
        to_instance=env["own_iid"],
        event_type=FederationEventType.PRESENCE_UPDATED,
        msg_id="m-forged",
    )
    with pytest.raises(ValueError, match="Invalid envelope signature"):
        await env["svc"].handle_inbound_envelope("wh-c", body)
    assert await _next_attempt(env) == _FAR_FUTURE


async def test_the_tombstoned_peers_own_unpair_ends_the_tombstone(env):
    body = _envelope(
        signer_seed=env["kp"].private_key,
        from_instance=env["iid"],
        to_instance=env["own_iid"],
        event_type=FederationEventType.UNPAIR,
        msg_id="m-their-unpair",
    )
    assert await env["svc"].handle_inbound_envelope("wh-c", body) == {"status": "ok"}
    assert await env["repo"].get_instance(env["iid"], include_unpairing=True) is None
    assert await env["outbox"].count_pending_for(env["iid"]) == 0


async def test_tombstone_is_invisible_to_ordinary_reads(env):
    repo = env["repo"]
    assert await repo.get_instance(env["iid"]) is None
    assert await repo.get_instance_by_local_inbox_id("wh-c") is None
    assert [i.id for i in await repo.list_instances()] == []
    assert await repo.list_social_instances() == []
    assert [
        i.id for i in await repo.list_instances(status=PairingStatus.UNPAIRING.value)
    ] == [env["iid"]]
    assert not await env["svc"].is_confirmed_peer(env["iid"])


async def test_a_new_pairing_replaces_the_tombstone(env):
    """Re-pairing (QR) with that household later must work: the fresh row —
    new keys, new inbox id — wins, and its envelopes are accepted."""
    repo = env["repo"]
    await repo.save_instance(
        RemoteInstance(
            id=env["iid"],
            display_name="c again",
            remote_identity_pk=env["kp"].public_key.hex(),
            key_self_to_remote=env["wrapped"],
            key_remote_to_self=env["wrapped"],
            remote_inbox_url="https://c.invalid/wh2",
            local_inbox_id="wh-c-2",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        )
    )
    inst = await repo.get_instance(env["iid"])
    assert inst is not None
    assert inst.status is PairingStatus.CONFIRMED
    assert inst.local_inbox_id == "wh-c-2"
    assert await repo.get_instance_by_local_inbox_id("wh-c") is None

    body = _envelope(
        signer_seed=env["kp"].private_key,
        from_instance=env["iid"],
        to_instance=env["own_iid"],
        event_type=FederationEventType.PRESENCE_UPDATED,
        msg_id="m-after-repair",
    )
    assert await env["svc"].handle_inbound_envelope("wh-c-2", body) == {"status": "ok"}
