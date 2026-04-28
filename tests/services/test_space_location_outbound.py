"""Tests for ``socialhome.services.space_location_outbound`` (§23.8.6).

These tests pin the GPS-only invariant: HA-defined zone names never
appear in any space-bound payload, regardless of whether they're on
the household ``presence.updated`` frame upstream. Concretely:

* For every space where the user has ``location_share_enabled = 1``
  and the space has ``feature_location = 1``, the outbound publishes
  a local WS frame **and** a sealed federation event — both
  GPS-only.
* When the household ``PresenceUpdated`` carries ``zone_name``
  (always the case on a transition into a known zone), the
  space-bound payload **must not** include it.
* When ``lat`` or ``lon`` is ``None`` (accuracy gate fired), neither
  fan-out runs.
* Spaces where the member has not opted in do not receive any
  payload at all.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import PresenceUpdated
from socialhome.domain.federation import FederationEventType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceType,
)
from socialhome.federation.encoder import FederationEncoder
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_location_outbound import SpaceLocationOutbound


class _FakeWS:
    """Captures every WS broadcast for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    async def broadcast_to_users(
        self, user_ids: list[str], payload: dict
    ) -> int:
        self.calls.append((list(user_ids), payload))
        return len(user_ids)


class _FakeFederation:
    """Captures every send_event for assertion.

    When ``encoder`` + ``session_key`` are wired the fake also
    encrypts the JSON payload with the real :class:`FederationEncoder`
    before stashing it, so tests can decrypt and assert on the actual
    sealed envelope payload — not just the dict the caller passed.
    """

    _own_instance_id = "self_instance"

    def __init__(
        self,
        *,
        encoder: FederationEncoder | None = None,
        session_key: bytes | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._encoder = encoder
        self._session_key = session_key

    async def send_event(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str | None = None,
    ) -> None:
        encrypted: str | None = None
        if self._encoder is not None and self._session_key is not None:
            encrypted = self._encoder.encrypt_payload(
                json.dumps(payload),
                self._session_key,
            )
        self.calls.append(
            {
                "to_instance_id": to_instance_id,
                "event_type": event_type,
                "payload": payload,
                "encrypted_payload": encrypted,
                "space_id": space_id,
            },
        )


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    alice_uid = derive_user_id(kp.public_key, "alice")
    bob_uid = derive_user_id(kp.public_key, "bob")
    for username, uid, name in (
        ("alice", alice_uid, "Alice"),
        ("bob", bob_uid, "Bob"),
    ):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, uid, name),
        )

    space_repo = SqliteSpaceRepo(db)

    async def _make_space(
        space_id: str, *, feature_location: bool = True
    ) -> Space:
        space = Space(
            id=space_id,
            name=f"Space {space_id}",
            owner_instance_id=iid,
            owner_username="alice",
            identity_public_key=kp.public_key.hex(),
            config_sequence=1,
            features=SpaceFeatures(location=feature_location),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
        await space_repo.save(space)
        return space

    bus = EventBus()
    ws = _FakeWS()
    federation = _FakeFederation()
    user_repo = SqliteUserRepo(db)

    outbound = SpaceLocationOutbound(
        bus=bus,
        ws=ws,
        federation_service=federation,  # type: ignore[arg-type]
        space_repo=space_repo,
        user_repo=user_repo,
    )
    outbound.wire()

    class E:
        pass

    e = E()
    e.db = db
    e.bus = bus
    e.ws = ws
    e.federation = federation
    e.alice_uid = alice_uid
    e.bob_uid = bob_uid
    e.space_repo = space_repo
    e.make_space = _make_space
    yield e
    await db.shutdown()


# ─── Happy path ─────────────────────────────────────────────────────────


async def test_publishes_gps_only_local_frame(env):
    sp = await env.make_space("sp_office")
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-27T00:00:00+00:00",
            location_share_enabled=True,
        ),
    )
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.bob_uid,
            role="member",
            joined_at="2026-04-27T00:00:00+00:00",
        ),
    )

    await env.bus.publish(
        PresenceUpdated(
            username="alice",
            state="zone",
            zone_name="Office",  # household-only — must NOT appear below
            latitude=47.3769,
            longitude=8.5417,
            gps_accuracy_m=12.0,
            updated_at="2026-04-27T12:00:00+00:00",
        ),
    )

    [(user_ids, frame)] = env.ws.calls
    assert sorted(user_ids) == sorted([env.alice_uid, env.bob_uid])
    assert frame["type"] == "space_location_updated"
    data = frame["data"]
    assert data["space_id"] == sp.id
    assert data["user_id"] == env.alice_uid
    assert data["lat"] == 47.3769
    assert data["lon"] == 8.5417
    assert data["accuracy_m"] == 12.0
    assert data["updated_at"] == "2026-04-27T12:00:00+00:00"
    assert "zone_name" not in data
    assert "state" not in data


async def test_no_zone_name_in_federation_payload(env):
    sp = await env.make_space("sp_office")
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-27T00:00:00+00:00",
            location_share_enabled=True,
        ),
    )
    # Wire a remote member instance so federation has a peer to fan to.
    await env.space_repo.add_space_instance(sp.id, "remote_instance_a")

    await env.bus.publish(
        PresenceUpdated(
            username="alice",
            state="zone",
            zone_name="Office",
            latitude=47.3769,
            longitude=8.5417,
            gps_accuracy_m=12.0,
            updated_at="2026-04-27T12:00:00+00:00",
        ),
    )

    [call] = env.federation.calls
    assert call["event_type"] == FederationEventType.SPACE_LOCATION_UPDATED
    assert call["space_id"] == sp.id
    assert call["to_instance_id"] == "remote_instance_a"
    assert call["payload"]["lat"] == 47.3769
    assert call["payload"]["lon"] == 8.5417
    assert call["payload"]["accuracy_m"] == 12.0
    assert "zone_name" not in call["payload"]
    assert "state" not in call["payload"]


# ─── Negative paths ─────────────────────────────────────────────────────


async def test_skipped_when_member_not_opted_in(env):
    sp = await env.make_space("sp_office")
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-27T00:00:00+00:00",
            location_share_enabled=False,  # opted out
        ),
    )

    await env.bus.publish(
        PresenceUpdated(
            username="alice",
            state="zone",
            zone_name="Office",
            latitude=47.3769,
            longitude=8.5417,
            gps_accuracy_m=12.0,
        ),
    )
    assert env.ws.calls == []
    assert env.federation.calls == []


async def test_skipped_when_feature_location_off(env):
    sp = await env.make_space("sp_off", feature_location=False)
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-27T00:00:00+00:00",
            location_share_enabled=True,
        ),
    )

    await env.bus.publish(
        PresenceUpdated(
            username="alice",
            state="zone",
            zone_name="Office",
            latitude=47.3769,
            longitude=8.5417,
            gps_accuracy_m=12.0,
        ),
    )
    assert env.ws.calls == []
    assert env.federation.calls == []


async def test_skipped_when_lat_lon_none(env):
    """Accuracy gate fired upstream — no GPS to broadcast."""
    sp = await env.make_space("sp_office")
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-27T00:00:00+00:00",
            location_share_enabled=True,
        ),
    )

    await env.bus.publish(
        PresenceUpdated(
            username="alice",
            state="zone",
            zone_name="Office",
            latitude=None,
            longitude=None,
            gps_accuracy_m=None,
        ),
    )
    assert env.ws.calls == []
    assert env.federation.calls == []


async def test_skips_own_instance_in_federation(env):
    sp = await env.make_space("sp_office")
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-27T00:00:00+00:00",
            location_share_enabled=True,
        ),
    )
    # Record both our own instance and a remote one as space members.
    await env.space_repo.add_space_instance(
        sp.id, env.federation._own_instance_id,
    )
    await env.space_repo.add_space_instance(sp.id, "remote_instance_b")

    await env.bus.publish(
        PresenceUpdated(
            username="alice",
            state="zone",
            latitude=47.3769,
            longitude=8.5417,
            gps_accuracy_m=12.0,
        ),
    )
    [call] = env.federation.calls
    assert call["to_instance_id"] == "remote_instance_b"


async def test_no_zone_name_in_decrypted_federation_envelope(env, tmp_dir):
    """§27: decrypt the outbound `SPACE_LOCATION_UPDATED` envelope and
    assert `zone_name` is absent from the *plaintext* of the encrypted
    payload — not just the dict at the call site.

    Pins the GPS-only invariant at the actual sealed envelope boundary,
    which is where remote member instances see the bytes. If a future
    change leaks `zone_name` into the encrypter, this test catches it
    even if the call-site dict still looked clean.
    """
    # Fresh outbound wired against a real FederationEncoder so the
    # fake's send_event runs the real encrypt path.
    kp = generate_identity_keypair()
    encoder = FederationEncoder(kp.private_key)
    session_key = os.urandom(32)
    real_fed = _FakeFederation(encoder=encoder, session_key=session_key)

    # Re-wire a brand-new outbound on the same db / bus / ws so the
    # earlier outbound (subscribed in the env fixture) doesn't double-fire.
    bus = EventBus()
    ws = _FakeWS()
    sp = await env.make_space("sp_decrypt_assert")
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-28T00:00:00+00:00",
            location_share_enabled=True,
        ),
    )
    await env.space_repo.add_space_instance(sp.id, "remote_decrypt")

    from socialhome.repositories.user_repo import SqliteUserRepo
    outbound = SpaceLocationOutbound(
        bus=bus,
        ws=ws,
        federation_service=real_fed,  # type: ignore[arg-type]
        space_repo=env.space_repo,
        user_repo=SqliteUserRepo(env.db),
    )
    outbound.wire()

    await bus.publish(
        PresenceUpdated(
            username="alice",
            state="zone",
            zone_name="Office",  # household-only — must NOT appear after decrypt
            latitude=47.3769,
            longitude=8.5417,
            gps_accuracy_m=12.0,
            updated_at="2026-04-28T12:00:00+00:00",
        ),
    )

    [call] = real_fed.calls
    assert call["event_type"] == FederationEventType.SPACE_LOCATION_UPDATED
    assert call["encrypted_payload"] is not None
    # The literal byte stream that goes on the wire must not even
    # *contain* the ASCII string "zone_name" (encrypted noise has zero
    # chance of producing that string — this catches an accidental
    # routing-field leak).
    assert "zone_name" not in call["encrypted_payload"]
    # And the decrypted plaintext is structurally GPS-only.
    plaintext = encoder.decrypt_payload(
        call["encrypted_payload"], session_key,
    )
    parsed = json.loads(plaintext)
    assert "zone_name" not in parsed
    assert "state" not in parsed
    assert parsed["lat"] == 47.3769
    assert parsed["lon"] == 8.5417
    assert parsed["accuracy_m"] == 12.0


async def test_publishes_to_each_opted_in_space(env):
    sp_a = await env.make_space("sp_a")
    sp_b = await env.make_space("sp_b")
    sp_off = await env.make_space("sp_off", feature_location=False)
    for sp in (sp_a, sp_b, sp_off):
        await env.space_repo.save_member(
            SpaceMember(
                space_id=sp.id,
                user_id=env.alice_uid,
                role="member",
                joined_at="2026-04-27T00:00:00+00:00",
                location_share_enabled=True,
            ),
        )

    await env.bus.publish(
        PresenceUpdated(
            username="alice",
            state="zone",
            latitude=1.0,
            longitude=2.0,
            gps_accuracy_m=15.0,
        ),
    )
    seen = sorted(call[1]["data"]["space_id"] for call in env.ws.calls)
    assert seen == ["sp_a", "sp_b"]
