"""UsersSyncOutbound — push the full household roster to a new peer.

On :class:`PairingConfirmed` the service sends a single
``USERS_SYNC`` envelope carrying every (visible) local user, so the
peer's ``remote_users`` mirror is populated immediately instead of
trickling in one ``USER_UPDATED`` at a time as members edit their
profiles. The receiver-side handler already exists at
:meth:`FederationInboundService._on_users_sync`.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import (
    USER_SIG_SUITE_ED25519,
    build_user_identity_assertion,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    verify_user_identity_assertion,
)
from socialhome.domain.events import PairingConfirmed
from socialhome.domain.federation import FederationEventType
from socialhome.domain.user import UserIdentityAssertion, UserStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.users_sync_outbound import UsersSyncOutbound


class _FakeFederationService:
    def __init__(
        self,
        own_instance_id: str = "own-inst",
        *,
        supports_v25: bool = False,
        instance_seed: bytes | None = None,
    ) -> None:
        self._own_instance_id = own_instance_id
        self._own_identity_seed = instance_seed or b"\x01" * 32
        self._supports_v25 = supports_v25
        self.sent: list[tuple[str, FederationEventType, dict]] = []

    @property
    def own_identity_seed(self) -> bytes:
        return self._own_identity_seed

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        # These fakes model a v_25 peer: it supports the binding (v_25) but NOT
        # the v_26 anchor, so the anchor gate must answer False.
        from socialhome.domain.federation_capabilities import FederationCapability

        if min_version >= FederationCapability.MIN_FOR_IDENTITY_ANCHOR:
            return False
        return self._supports_v25

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sent.append((to_instance_id, event_type, payload))
        return None


class _FakeUser:
    def __init__(
        self,
        user_id: str,
        username: str,
        display_name: str,
        bio: str | None = None,
        picture_hash: str | None = None,
        handle: str | None = None,
        status: UserStatus | None = None,
    ) -> None:
        self.status = status or UserStatus()
        self.user_id = user_id
        self.username = username
        self.display_name = display_name
        self.bio = bio
        self.picture_hash = picture_hash
        self.handle = handle


class _FakeUserRepo:
    def __init__(
        self,
        users: list[_FakeUser],
        keypairs: dict[str, tuple[bytes, bytes]] | None = None,
    ) -> None:
        self._users = users
        self._keypairs = keypairs or {}

    async def list_active(self):
        return list(self._users)

    async def get_user_identity_keypair(self, username: str):
        return self._keypairs.get(username)

    async def get_user_identity_anchor(self, username: str):
        return None


class _FakePictureRepo:
    """Returns ``(bytes, hash)`` for a known user_id or ``None``."""

    def __init__(self, pics: dict[str, bytes]) -> None:
        self._pics = pics

    async def get_user_picture(self, user_id: str):
        b = self._pics.get(user_id)
        if b is None:
            return None
        return b, f"hash-{user_id}"


class _FakeVisibilityRepo:
    def __init__(self) -> None:
        self._hidden: dict[str, set[str]] = {}

    def hide(self, peer: str, user_id: str) -> None:
        self._hidden.setdefault(peer, set()).add(user_id)

    async def hidden_user_ids_for_peer(self, peer: str) -> frozenset[str]:
        return frozenset(self._hidden.get(peer, set()))


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFederationService()
    user_repo = _FakeUserRepo(
        [
            _FakeUser("u-alice", "alice", "Alice", bio="hi", picture_hash=None),
            _FakeUser("u-maria", "maria", "Maria", bio=None, picture_hash="hm"),
        ]
    )
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=user_repo,
    )
    out.wire()
    return bus, fed


async def test_fanout_on_pair_confirmed_sends_one_envelope(env):
    """One USERS_SYNC envelope, all local users in the payload."""
    bus, fed = env
    await bus.publish(PairingConfirmed(instance_id="peer-new"))
    assert len(fed.sent) == 1
    to, event_type, payload = fed.sent[0]
    assert to == "peer-new"
    assert event_type is FederationEventType.USERS_SYNC
    user_ids = [u["user_id"] for u in payload["users"]]
    assert sorted(user_ids) == ["u-alice", "u-maria"]
    # Each entry carries the public-shape fields the inbound handler
    # expects (mirrors USER_UPDATED).
    alice = next(u for u in payload["users"] if u["user_id"] == "u-alice")
    assert alice == {
        "user_id": "u-alice",
        "username": "alice",
        "display_name": "Alice",
        "bio": "hi",
        "picture_hash": None,
        "handle": None,
        "status": None,
    }


async def test_skips_self_pair_confirmed(env):
    """``PairingConfirmed`` for our own instance id is a no-op — the
    own-instance hook fires on bootstrap of the local row and must
    not produce a self-addressed envelope."""
    bus, fed = env
    await bus.publish(PairingConfirmed(instance_id="own-inst"))
    assert fed.sent == []


async def test_empty_pair_confirmed_id_is_noop(env):
    bus, fed = env
    await bus.publish(PairingConfirmed(instance_id=""))
    assert fed.sent == []


async def test_no_local_users_no_send():
    """When there are no local users to advertise we don't send an
    empty envelope — keeps the wire quiet and avoids the receiver
    processing a zero-row sync."""
    bus = EventBus()
    fed = _FakeFederationService()
    user_repo = _FakeUserRepo([])
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=user_repo,
    )
    out.wire()
    await bus.publish(PairingConfirmed(instance_id="peer-new"))
    assert fed.sent == []


async def test_visibility_repo_filters_per_peer():
    """Admin's per-peer hide list keeps a hidden user out of the
    snapshot. Mirrors the ProfileFederationOutbound semantics so the
    two outbound paths stay consistent."""
    bus = EventBus()
    fed = _FakeFederationService()
    user_repo = _FakeUserRepo(
        [
            _FakeUser("u-alice", "alice", "Alice"),
            _FakeUser("u-bob", "bob", "Bob"),
        ]
    )
    vis = _FakeVisibilityRepo()
    vis.hide("peer-new", "u-bob")
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=user_repo,
        visibility_repo=vis,
    )
    out.wire()
    await bus.publish(PairingConfirmed(instance_id="peer-new"))
    payload = fed.sent[0][2]
    user_ids = [u["user_id"] for u in payload["users"]]
    assert user_ids == ["u-alice"]


async def test_visibility_repo_hides_everyone_then_no_send():
    """If every local user is hidden from the new peer, the envelope
    is suppressed entirely (same rule as the empty-roster case)."""
    bus = EventBus()
    fed = _FakeFederationService()
    user_repo = _FakeUserRepo(
        [
            _FakeUser("u-alice", "alice", "Alice"),
        ]
    )
    vis = _FakeVisibilityRepo()
    vis.hide("peer-new", "u-alice")
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=user_repo,
        visibility_repo=vis,
    )
    out.wire()
    await bus.publish(PairingConfirmed(instance_id="peer-new"))
    assert fed.sent == []


async def test_picture_bytes_attached_when_available():
    """Users with a picture_hash AND bytes in the picture repo get a
    base64 ``picture_webp_base64`` field so the peer can render the
    avatar immediately on receive."""
    bus = EventBus()
    fed = _FakeFederationService()
    user_repo = _FakeUserRepo(
        [
            _FakeUser("u-alice", "alice", "Alice", picture_hash="h-alice"),
            _FakeUser("u-bob", "bob", "Bob", picture_hash="h-bob"),
            # No hash → no lookup, no bytes attached.
            _FakeUser("u-carol", "carol", "Carol", picture_hash=None),
        ]
    )
    pics = _FakePictureRepo({"u-alice": b"\x00\x01\x02"})
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=user_repo,
        profile_picture_repo=pics,
    )
    out.wire()
    await bus.publish(PairingConfirmed(instance_id="peer-new"))
    payload = fed.sent[0][2]
    by_id = {u["user_id"]: u for u in payload["users"]}
    # Alice has both hash + bytes → base64 attached.
    assert by_id["u-alice"]["picture_webp_base64"] == base64.b64encode(
        b"\x00\x01\x02",
    ).decode("ascii")
    # Bob has a hash but no bytes stored → no base64 field, hash kept.
    assert "picture_webp_base64" not in by_id["u-bob"]
    assert by_id["u-bob"]["picture_hash"] == "h-bob"
    # Carol has no hash at all → no lookup attempted, no base64 field.
    assert "picture_webp_base64" not in by_id["u-carol"]


async def test_v25_peer_gets_user_identity_binding():
    """A v_25 peer receives the per-user identity binding fields and the
    resulting assertion verifies against the instance public key."""
    bus = EventBus()
    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    iid = derive_instance_id(instance_kp.public_key)
    uid = derive_user_id(instance_kp.public_key, "alice")
    fed = _FakeFederationService(
        own_instance_id=iid,
        supports_v25=True,
        instance_seed=instance_kp.private_key,
    )
    user_repo = _FakeUserRepo(
        [_FakeUser(uid, "alice", "Alice", bio="hi")],
        keypairs={"alice": (user_kp.public_key, user_kp.private_key)},
    )
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=user_repo,
    )
    out.wire()
    await bus.publish(PairingConfirmed(instance_id="peer-new"))

    payload = fed.sent[0][2]
    entry = payload["users"][0]
    assert entry["user_identity_public_key"] == user_kp.public_key.hex()
    assert entry["user_sig_suite"] == USER_SIG_SUITE_ED25519
    assert "user_signature" in entry
    # The full self-verifying credential rides along: instance signature +
    # issued_at let a relayed/cached copy be re-verified standalone.
    assert "user_assertion_signature" in entry
    assert "user_assertion_issued_at" in entry
    # The legacy fields are unchanged alongside the binding.
    assert entry["user_id"] == uid
    assert entry["display_name"] == "Alice"

    # The emitted entry reconstructs an assertion that verifies end-to-end.
    reconstructed = UserIdentityAssertion(
        user_id=uid,
        instance_id=iid,
        username="alice",
        display_name="Alice",
        issued_at=entry["user_assertion_issued_at"],
        signature=entry["user_assertion_signature"],
        user_identity_public_key=entry["user_identity_public_key"],
        user_pq_public_key=None,
        user_sig_suite=entry["user_sig_suite"],
        user_signature=entry["user_signature"],
    )
    verify_user_identity_assertion(reconstructed, instance_kp.public_key)

    reference = build_user_identity_assertion(
        instance_seed=instance_kp.private_key,
        user_id=uid,
        instance_id=iid,
        username="alice",
        display_name="Alice",
        issued_at="2026-06-15T00:00:00+00:00",
        user_seed=user_kp.private_key,
        user_public_key=user_kp.public_key,
        user_sig_suite=USER_SIG_SUITE_ED25519,
    )
    assert entry["user_signature"] == reference.user_signature


async def test_v24_peer_gets_legacy_shape_without_binding():
    """A sub-v_25 peer gets exactly the legacy per-user shape — no binding
    keys leak to a peer that can't validate them."""
    bus = EventBus()
    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    uid = derive_user_id(instance_kp.public_key, "alice")
    fed = _FakeFederationService(
        supports_v25=False,
        instance_seed=instance_kp.private_key,
    )
    user_repo = _FakeUserRepo(
        [_FakeUser(uid, "alice", "Alice", bio="hi")],
        keypairs={"alice": (user_kp.public_key, user_kp.private_key)},
    )
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=user_repo,
    )
    out.wire()
    await bus.publish(PairingConfirmed(instance_id="peer-new"))

    entry = fed.sent[0][2]["users"][0]
    assert entry == {
        "user_id": uid,
        "username": "alice",
        "display_name": "Alice",
        "bio": "hi",
        "picture_hash": None,
        "handle": None,
        "status": None,
    }
    assert "user_identity_public_key" not in entry
    assert "user_sig_suite" not in entry
    assert "user_signature" not in entry
    assert "user_assertion_signature" not in entry
    assert "user_assertion_issued_at" not in entry


# ─── status in the roster snapshot ──────────────────────────────────────


async def _sync_payload(users, *, visibility=None, peer="peer-new") -> dict | None:
    bus = EventBus()
    fed = _FakeFederationService()
    UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=_FakeUserRepo(users),
        visibility_repo=visibility,
    ).wire()
    await bus.publish(PairingConfirmed(instance_id=peer))
    return fed.sent[0][2] if fed.sent else None


async def test_current_status_rides_the_snapshot():
    """A user's current status ships as ``{emoji, text, expires_at}`` so
    the peer shows it right after pairing."""
    later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    payload = await _sync_payload(
        [
            _FakeUser(
                "u-alice",
                "alice",
                "Alice",
                status=UserStatus(emoji="🌴", text="On holiday", expires_at=later),
            ),
            _FakeUser("u-maria", "maria", "Maria", status=UserStatus(text="Busy")),
        ]
    )
    by_id = {u["user_id"]: u for u in payload["users"]}
    assert by_id["u-alice"]["status"] == {
        "emoji": "🌴",
        "text": "On holiday",
        "expires_at": later,
    }
    assert by_id["u-maria"]["status"] == {
        "emoji": None,
        "text": "Busy",
        "expires_at": None,
    }


async def test_expired_status_ships_as_explicit_clear():
    """A status past its deadline (not yet swept) goes out as ``null`` —
    never resurrected on the peer — and so does no status at all."""
    earlier = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    payload = await _sync_payload(
        [
            _FakeUser(
                "u-alice",
                "alice",
                "Alice",
                status=UserStatus(emoji="🤒", text="Sick", expires_at=earlier),
            ),
            _FakeUser("u-maria", "maria", "Maria"),
        ]
    )
    for entry in payload["users"]:
        assert "status" in entry
        assert entry["status"] is None


async def test_status_of_a_user_hidden_from_the_peer_never_ships():
    """Same per-pair visibility filter as ``USER_STATUS_UPDATED``: a user
    hidden from this peer is not in the snapshot, status included."""
    vis = _FakeVisibilityRepo()
    vis.hide("peer-new", "u-alice")
    payload = await _sync_payload(
        [
            _FakeUser("u-alice", "alice", "Alice", status=UserStatus(text="Secret")),
            _FakeUser("u-maria", "maria", "Maria", status=UserStatus(text="Here")),
        ],
        visibility=vis,
    )
    assert [u["user_id"] for u in payload["users"]] == ["u-maria"]
    assert "Secret" not in repr(payload)


class _FakeChildProtection:
    """``u-kid`` is protected; its guardian blocked someone on peer-x."""

    def register_gate(self, gate) -> None:
        pass

    async def guardian_blocks_household(self, user_id, instance_id) -> bool:
        return user_id == "u-kid" and instance_id == "peer-x"


@pytest.mark.parametrize(("peer", "shipped"), [("peer-x", None), ("peer-y", "x")])
async def test_protected_status_never_ships_to_a_blocked_household(peer, shipped):
    """§CP.F2: the snapshot to a household where someone the account's
    guardian blocked is homed carries no status for it (an adult's still
    ships); the profile entry itself stays."""
    bus = EventBus()
    fed = _FakeFederationService()
    out = UsersSyncOutbound(
        bus=bus,
        federation_service=fed,
        user_repo=_FakeUserRepo(
            [
                _FakeUser("u-kid", "kid", "Kid", status=UserStatus(text="x")),
                _FakeUser("u-mom", "mom", "Mom", status=UserStatus(text="y")),
            ]
        ),
    )
    out.attach_child_protection(_FakeChildProtection())  # type: ignore[arg-type]
    out.wire()
    await bus.publish(PairingConfirmed(instance_id=peer))
    by_id = {u["user_id"]: u for u in fed.sent[0][2]["users"]}
    assert (by_id["u-kid"]["status"] or {}).get("text") == shipped
    assert by_id["u-mom"]["status"]["text"] == "y"
