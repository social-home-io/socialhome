"""§Momentum-public no-redistribute integration scenario.

Wires the real :class:`MomentPublicInbound` and
:class:`MomentFederationOutbound` against a SQLite ``moments`` table
and asserts the contract end-to-end:

* When the GFS pushes ``incoming_public_moment``, the recipient
  persists the row with ``received_via='gfs'`` and republishes a
  local :class:`MomentCreated`.
* The federation outbound subscriber sees the bus event but
  short-circuits the relay path because the row is GFS-received.
* A direct call to :meth:`relay_inbound` with the same envelope
  also skips fan-out — the no-redistribute rule applies on both
  the bus and explicit-relay paths.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.crypto import b64url_encode, sign_ed25519
from socialhome.domain.events import MomentCreated
from socialhome.domain.federation import FederationEventType, RemoteInstance
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicFollowRepo,
)
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.services.moment_federation_outbound import (
    MomentFederationOutbound,
)
from socialhome.services.moment_public_inbound import MomentPublicInbound

_AUTHOR_SEED = b"\x11" * 32


def _author_pk_hex() -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    sk = Ed25519PrivateKey.from_private_bytes(_AUTHOR_SEED)
    return sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _signed_envelope(payload: dict) -> dict:
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    signed = dict(payload)
    signed["signature"] = b64url_encode(sign_ed25519(_AUTHOR_SEED, canonical))
    return signed


def _peer(iid: str) -> RemoteInstance:
    inst = MagicMock(spec=RemoteInstance)
    inst.id = iid
    return inst


@pytest.fixture
async def env(db):
    """Wire the real recipient stack: bus + repos + outbound + inbound."""
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u-self','alice','Alice','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections("
        "id, gfs_instance_id, display_name, public_key, inbox_url, "
        "status, paired_at) "
        "VALUES('g1','gfs-1','GFS One','ff'*32,'https://gfs1.example','active', datetime('now'))"
    )
    moment_repo = SqliteMomentRepo(db)
    follow_repo = SqliteMomentPublicFollowRepo(db)
    await follow_repo.upsert(
        follower_user_id="u-self",
        followed_user_id="u-remote",
        gfs_id="g1",
        followed_instance_pk=_author_pk_hex(),
        followed_username="bob",
        followed_display_name="Bob",
    )
    bus = EventBus()
    federation = MagicMock()
    federation.own_instance_id = "self"
    federation.send_event = AsyncMock()
    federation_repo = MagicMock()
    federation_repo.list_social_instances = AsyncMock(
        return_value=[_peer("peer-c"), _peer("peer-d")],
    )
    user_repo = MagicMock()
    user_repo.get_instance_for_user = AsyncMock(return_value="inst-remote")
    out = MomentFederationOutbound(
        bus=bus,
        federation_service=federation,
        federation_repo=federation_repo,
        user_repo=user_repo,
    )
    out.wire()
    inbound = MomentPublicInbound(
        bus=bus, moment_repo=moment_repo, follow_repo=follow_repo
    )
    return {
        "bus": bus,
        "moments": moment_repo,
        "federation": federation,
        "outbound": out,
        "inbound": inbound,
    }


async def test_gfs_public_moment_persists_and_does_not_relay(env):
    """End-to-end: a GFS-pushed moment lands locally tagged
    ``received_via='gfs'`` and the federation outbound never sees a
    reason to fan it out."""
    captured: list[MomentCreated] = []
    env["bus"].subscribe(MomentCreated, lambda e: captured.append(e))

    envelope = _signed_envelope(
        {
            "moment_id": "m-public",
            "author_user_id": "u-remote",
            "author_username": "bob",
            "author_display_name": "Bob",
            "content": "hello world",
            "media_url": None,
            "media_type": None,
            "duration_ms": None,
            "parent_moment_id": None,
            "origin_instance_id": "inst-remote",
            "created_at": "2026-05-06T12:00:00Z",
            "expires_at": "2026-05-07T12:00:00Z",
        }
    )
    await env["inbound"].handle(
        {"type": "incoming_public_moment", "payload": envelope}, gfs_id="g1"
    )

    # Row persisted with the no-redistribute marker.
    saved = await env["moments"].get("m-public")
    assert saved is not None
    assert saved.received_via == "gfs"
    assert saved.received_via_gfs_id == "g1"

    # The bus event fired (so the inbox + realtime fire), but the
    # federation outbound did NOT fan out — the moment came from a
    # remote author so the bus path skips, and the explicit relay
    # path is not invoked here.
    assert len(captured) == 1
    env["federation"].send_event.assert_not_called()


async def test_explicit_relay_call_with_gfs_payload_does_not_fan(env):
    """Direct ``relay_inbound`` call with a ``received_via='gfs'``
    payload short-circuits the relay path — covers the contract on
    the explicit-relay side too."""
    payload = {
        "moment_id": "m-public",
        "author_user_id": "u-remote",
        "origin_instance_id": "inst-remote",
        "hop_count": 1,
        "received_via": "gfs",
    }
    await env["outbound"].relay_inbound(
        event_type=FederationEventType.MOMENT_CREATED,
        payload=payload,
        from_instance="peer-sender",
    )
    env["federation"].send_event.assert_not_called()
