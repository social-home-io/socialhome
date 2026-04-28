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
from socialhome.repositories.presence_repo import SqlitePresenceRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.space_zone_repo import SqliteSpaceZoneRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_location_outbound import SpaceLocationOutbound


class _FakeWS:
    """Captures every WS broadcast for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    async def broadcast_to_users(self, user_ids: list[str], payload: dict) -> int:
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
    zone_repo = SqliteSpaceZoneRepo(db)
    presence_repo = SqlitePresenceRepo(db)

    async def _make_space(
        space_id: str,
        *,
        feature_location: bool = True,
        location_mode: str = "gps",
    ) -> Space:
        space = Space(
            id=space_id,
            name=f"Space {space_id}",
            owner_instance_id=iid,
            owner_username="alice",
            identity_public_key=kp.public_key.hex(),
            config_sequence=1,
            features=SpaceFeatures(
                location=feature_location,
                location_mode=location_mode,  # type: ignore[arg-type]
            ),
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
        space_zone_repo=zone_repo,
        user_repo=user_repo,
        presence_repo=presence_repo,
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
    e.zone_repo = zone_repo
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
    assert data["mode"] == "gps"
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
    assert call["payload"]["mode"] == "gps"
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
        sp.id,
        env.federation._own_instance_id,
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
        space_zone_repo=env.zone_repo,
        user_repo=SqliteUserRepo(env.db),
        presence_repo=SqlitePresenceRepo(env.db),
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
        call["encrypted_payload"],
        session_key,
    )
    parsed = json.loads(plaintext)
    assert parsed["mode"] == "gps"
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


# ─── zone_only mode (§23.8.6 + §23.8.7) ─────────────────────────────────


async def _seed_zone(
    env, space_id: str, *, name: str, lat: float, lon: float, radius_m: int = 200
) -> str:
    from socialhome.domain.space import SpaceZone

    zid = f"z_{name.lower().replace(' ', '_')}"
    zone = SpaceZone(
        id=zid,
        space_id=space_id,
        name=name,
        latitude=lat,
        longitude=lon,
        radius_m=radius_m,
        color="#3b82f6",
        created_by=env.alice_uid,
        created_at="2026-04-28T00:00:00+00:00",
        updated_at="2026-04-28T00:00:00+00:00",
    )
    await env.zone_repo.upsert(zone)
    return zid


async def test_zone_only_mode_publishes_zone_label_no_gps(env):
    """`zone_only` mode: payload carries the matched zone label and
    NO raw coordinates on either the local WS frame or the federation
    envelope. Pins the privacy invariant of the new tier."""
    sp = await env.make_space("sp_zone", location_mode="zone_only")
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-28T00:00:00+00:00",
            location_share_enabled=True,
        ),
    )
    await env.space_repo.add_space_instance(sp.id, "remote_zone")
    await _seed_zone(env, sp.id, name="Office", lat=47.3769, lon=8.5417)

    await env.bus.publish(
        PresenceUpdated(
            username="alice",
            state="zone",
            zone_name="Office",  # HA zone — must NOT leak to space
            latitude=47.3769,
            longitude=8.5417,
            gps_accuracy_m=10.0,
            updated_at="2026-04-28T12:00:00+00:00",
        ),
    )

    [(_user_ids, frame)] = env.ws.calls
    data = frame["data"]
    assert data["mode"] == "zone_only"
    assert data["space_id"] == sp.id
    assert data["user_id"] == env.alice_uid
    assert data["zone_id"] == "z_office"
    assert data["zone_name"] == "Office"
    assert data["updated_at"] == "2026-04-28T12:00:00+00:00"
    # No GPS on the wire.
    assert "lat" not in data
    assert "lon" not in data
    assert "accuracy_m" not in data

    # Federation envelope mirrors the WS shape — same payload.
    [call] = env.federation.calls
    assert call["payload"]["mode"] == "zone_only"
    assert call["payload"]["zone_id"] == "z_office"
    assert "lat" not in call["payload"]
    assert "lon" not in call["payload"]


async def test_zone_only_mode_skips_when_no_zone_matches(env):
    """GPS outside every space zone → silent skip. No WS frame, no
    federation call. The originating instance does not leak
    presence-without-location.
    """
    sp = await env.make_space("sp_zone_silent", location_mode="zone_only")
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-28T00:00:00+00:00",
            location_share_enabled=True,
        ),
    )
    await env.space_repo.add_space_instance(sp.id, "remote_silent")
    # Zone is far away from the published coordinates.
    await _seed_zone(env, sp.id, name="Faraway", lat=0.0, lon=0.0, radius_m=100)

    await env.bus.publish(
        PresenceUpdated(
            username="alice",
            state="zone",
            latitude=47.3769,
            longitude=8.5417,
            gps_accuracy_m=10.0,
        ),
    )

    assert env.ws.calls == []
    assert env.federation.calls == []


async def test_zone_only_mode_picks_closest_overlapping_zone(env):
    """When GPS sits inside two overlapping zones, pick the closer
    centre. Mirrors the deterministic client-side behaviour in
    SpaceLocationCard.matchZoneName."""
    sp = await env.make_space("sp_zone_overlap", location_mode="zone_only")
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-28T00:00:00+00:00",
            location_share_enabled=True,
        ),
    )
    # Outer zone at (47.4, 8.5), 5 km radius
    await _seed_zone(
        env,
        sp.id,
        name="District",
        lat=47.4,
        lon=8.5,
        radius_m=5000,
    )
    # Inner zone at (47.3769, 8.5417), 200 m radius — dead-on the GPS
    await _seed_zone(
        env,
        sp.id,
        name="Office",
        lat=47.3769,
        lon=8.5417,
        radius_m=200,
    )

    await env.bus.publish(
        PresenceUpdated(
            username="alice",
            state="zone",
            latitude=47.3770,
            longitude=8.5417,
            gps_accuracy_m=10.0,
        ),
    )

    [(_user_ids, frame)] = env.ws.calls
    assert frame["data"]["zone_name"] == "Office"


async def test_zone_only_decrypted_envelope_has_no_gps(env, tmp_dir):
    """Decrypt-side invariant for zone_only mode: the encrypted
    envelope plaintext carries the zone label only — no `lat`/`lon`
    survive the haversine-and-strip path."""
    kp = generate_identity_keypair()
    encoder = FederationEncoder(kp.private_key)
    session_key = os.urandom(32)
    real_fed = _FakeFederation(encoder=encoder, session_key=session_key)

    bus = EventBus()
    ws = _FakeWS()
    sp = await env.make_space(
        "sp_zone_decrypt",
        location_mode="zone_only",
    )
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-28T00:00:00+00:00",
            location_share_enabled=True,
        ),
    )
    await env.space_repo.add_space_instance(sp.id, "remote_zone_decrypt")
    await _seed_zone(env, sp.id, name="Office", lat=47.3769, lon=8.5417)

    from socialhome.repositories.user_repo import SqliteUserRepo

    outbound = SpaceLocationOutbound(
        bus=bus,
        ws=ws,
        federation_service=real_fed,  # type: ignore[arg-type]
        space_repo=env.space_repo,
        space_zone_repo=env.zone_repo,
        user_repo=SqliteUserRepo(env.db),
        presence_repo=SqlitePresenceRepo(env.db),
    )
    outbound.wire()

    await bus.publish(
        PresenceUpdated(
            username="alice",
            state="zone",
            zone_name="Office",
            latitude=47.3769,
            longitude=8.5417,
            gps_accuracy_m=10.0,
            updated_at="2026-04-28T12:00:00+00:00",
        ),
    )

    [call] = real_fed.calls
    assert "lat" not in call["encrypted_payload"]
    assert "lon" not in call["encrypted_payload"]
    plaintext = encoder.decrypt_payload(
        call["encrypted_payload"],
        session_key,
    )
    parsed = json.loads(plaintext)
    assert parsed["mode"] == "zone_only"
    assert parsed["zone_id"] == "z_office"
    assert parsed["zone_name"] == "Office"
    assert "lat" not in parsed
    assert "lon" not in parsed
    assert "accuracy_m" not in parsed


async def test_mode_changed_event_refires_presence_for_space_only(env):
    """Publishing :class:`SpaceLocationModeChanged` triggers a fresh
    fan-out for THIS space's opted-in members, so receivers see the
    new tier within seconds rather than waiting for the next HA push."""
    from socialhome.domain.events import SpaceLocationModeChanged
    from socialhome.domain.presence import LocationUpdate
    from socialhome.repositories.presence_repo import SqlitePresenceRepo
    from socialhome.services.presence_service import PresenceService

    sp = await env.make_space("sp_refire", location_mode="zone_only")
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-28T00:00:00+00:00",
            location_share_enabled=True,
        ),
    )
    await env.space_repo.add_space_instance(sp.id, "remote_refire")
    # Seed a zone matching the GPS we'll persist below.
    await _seed_zone(env, sp.id, name="Office", lat=47.3769, lon=8.5417)

    # Persist a presence row WITHOUT going through the bus (i.e.
    # don't fire PresenceUpdated) — we want to verify the
    # mode-change handler reads the row directly.
    presence_repo = SqlitePresenceRepo(env.db)
    presence_svc = PresenceService(presence_repo)  # no bus
    await presence_svc.update_location(
        LocationUpdate(
            username="alice",
            state="zone",
            zone_name="Office",
            latitude=47.3769,
            longitude=8.5417,
            gps_accuracy_m=10.0,
        ),
    )
    # The env outbound is subscribed to the fixture's bus. Reset
    # capture state so we only see what the mode-change publishes.
    env.ws.calls.clear()
    env.federation.calls.clear()

    await env.bus.publish(
        SpaceLocationModeChanged(space_id=sp.id, new_mode="zone_only"),
    )

    [(_user_ids, frame)] = env.ws.calls
    assert frame["data"]["mode"] == "zone_only"
    assert frame["data"]["zone_name"] == "Office"
    [call] = env.federation.calls
    assert call["payload"]["mode"] == "zone_only"


async def test_mode_changed_skipped_when_feature_off(env):
    """Mode-changed event on a space whose feature_location is OFF is
    a no-op (defensive — feature_location must be ON for any space
    presence to fire)."""
    from socialhome.domain.events import SpaceLocationModeChanged

    sp = await env.make_space("sp_off_refire", feature_location=False)
    await env.space_repo.save_member(
        SpaceMember(
            space_id=sp.id,
            user_id=env.alice_uid,
            role="member",
            joined_at="2026-04-28T00:00:00+00:00",
            location_share_enabled=True,
        ),
    )

    await env.bus.publish(
        SpaceLocationModeChanged(space_id=sp.id, new_mode="gps"),
    )

    assert env.ws.calls == []
    assert env.federation.calls == []
