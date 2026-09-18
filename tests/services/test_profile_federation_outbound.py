"""ProfileFederationOutbound — fan UserProfileUpdated to paired peers.

Mirrors the shape of ``test_sticky_federation_outbound.py``: an
in-memory ``EventBus``, a fake :class:`FederationService` recording
sends, and a fake federation repo returning the list of confirmed
peers. The peer-user visibility repo is optional — the suite covers
both the default (every peer sees every user) and the explicit-filter
shape.
"""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    USER_SIG_SUITE_ED25519,
    build_user_identity_assertion,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    verify_user_identity_assertion,
)
from socialhome.domain.events import UserProfileUpdated
from socialhome.domain.federation import FederationEventType
from socialhome.domain.user import UserIdentityAssertion
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.profile_federation_outbound import (
    ProfileFederationOutbound,
)


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
    def own_instance_id(self) -> str:
        # Mirror the real FederationService's public accessor — the
        # outbound mixins read the public property, not the private attr.
        return self._own_instance_id

    @property
    def own_identity_seed(self) -> bytes:
        return self._own_identity_seed

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        # Models a v_25 peer: supports the binding (v_25) but not the v_26
        # anchor, so the anchor gate answers False.
        from socialhome.domain.federation_capabilities import FederationCapability

        if min_version >= FederationCapability.MIN_FOR_IDENTITY_ANCHOR:
            return False
        return self._supports_v25

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sent.append((to_instance_id, event_type, payload))
        return None


class _FakeUserRepo:
    def __init__(
        self,
        keypairs: dict[str, tuple[bytes, bytes]],
        *,
        handles: dict[str, str | None] | None = None,
    ) -> None:
        self._keypairs = keypairs
        self._handles = handles or {}

    async def get_user_identity_keypair(self, username: str):
        return self._keypairs.get(username)

    async def get_user_identity_anchor(self, username: str):
        return None

    async def get_by_user_id(self, user_id: str):
        # Minimal stand-in: the outbound only reads ``.handle`` to fill
        # the USER_UPDATED payload when the event itself omitted it.
        handle = self._handles.get(user_id)
        if handle is None:
            return None

        class _U:
            pass

        u = _U()
        u.handle = handle
        return u


class _Peer:
    def __init__(self, instance_id: str) -> None:
        self.id = instance_id


class _FakeFedRepo:
    def __init__(self, peers: list[str]) -> None:
        self._peers = peers

    async def list_social_instances(self):
        return await self.list_instances("confirmed")

    async def list_instances(self, status: str):
        assert status == "confirmed"
        return [_Peer(p) for p in self._peers]


class _FakeVisibilityRepo:
    """Per-peer hide list. ``hidden_user_ids_for_peer`` returns the set
    of user_ids explicitly hidden from a given peer via :meth:`hide`."""

    def __init__(self) -> None:
        self._hidden: dict[str, set[str]] = {}

    def hide(self, peer: str, user_id: str) -> None:
        self._hidden.setdefault(peer, set()).add(user_id)

    async def hidden_user_ids_for_peer(self, peer: str) -> frozenset[str]:
        return frozenset(self._hidden.get(peer, set()))


def _event(**over) -> UserProfileUpdated:
    base = dict(
        user_id="u1",
        username="alice",
        display_name="Alice",
        bio="hello",
        picture_hash="h1",
        picture_webp=None,
        handle=None,
    )
    base.update(over)
    return UserProfileUpdated(**base)


@pytest.fixture
def env():
    bus = EventBus()
    fed = _FakeFederationService()
    repo = _FakeFedRepo(["peer-1", "peer-2", "own-inst"])
    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=repo,
    )
    out.wire()
    return bus, fed


async def test_fanouts_to_every_paired_peer_excluding_self(env):
    bus, fed = env
    await bus.publish(_event())
    recipients = [r[0] for r in fed.sent]
    assert recipients == ["peer-1", "peer-2"]
    # All carry USER_UPDATED with the public payload shape.
    for to, event_type, payload in fed.sent:
        assert event_type is FederationEventType.USER_UPDATED
        assert payload == {
            "user_id": "u1",
            "username": "alice",
            "display_name": "Alice",
            "bio": "hello",
            "picture_hash": "h1",
            "handle": None,
        }


async def test_handle_from_event_rides_user_updated_payload(env):
    """A ``UserProfileUpdated`` carrying ``handle='bobby'`` puts that handle on
    every emitted ``USER_UPDATED`` payload, alongside ``display_name``."""
    bus, fed = env
    await bus.publish(_event(handle="bobby"))
    assert fed.sent  # fan-out happened
    for _to, _ev, payload in fed.sent:
        assert payload["handle"] == "bobby"
        assert payload["display_name"] == "Alice"


async def test_handle_falls_back_to_user_row_when_event_omits_it():
    """A profile edit (display_name/bio/picture) publishes
    ``UserProfileUpdated`` with ``handle=None``; the outbound back-fills the
    handle from the user row so a non-handle edit never wipes the peer's cached
    handle to NULL."""
    bus = EventBus()
    fed = _FakeFederationService()
    repo = _FakeFedRepo(["peer-1"])
    user_repo = _FakeUserRepo({}, handles={"u1": "bobby"})
    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=repo,
        user_repo=user_repo,
    )
    out.wire()
    await bus.publish(_event(handle=None))
    payload = fed.sent[0][2]
    assert payload["handle"] == "bobby"


async def test_picture_bytes_base64d_when_present(env):
    bus, fed = env
    await bus.publish(_event(picture_webp=b"\x00\x01\x02"))
    for _to, _ev, payload in fed.sent:
        # base64-encoded ``\x00\x01\x02`` is ``AAEC``.
        assert payload["picture_webp_base64"] == "AAEC"


async def test_visibility_repo_hides_user_from_specific_peer():
    bus = EventBus()
    fed = _FakeFederationService()
    repo = _FakeFedRepo(["peer-1", "peer-2"])
    vis = _FakeVisibilityRepo()
    vis.hide("peer-2", "u1")  # u1 is hidden from peer-2 only
    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=repo,
        visibility_repo=vis,
    )
    out.wire()
    await bus.publish(_event())
    recipients = [r[0] for r in fed.sent]
    assert recipients == ["peer-1"]


async def test_no_peers_no_sends():
    bus = EventBus()
    fed = _FakeFederationService()
    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=_FakeFedRepo([]),
    )
    out.wire()
    await bus.publish(_event())
    assert fed.sent == []


async def test_peer_id_missing_is_skipped():
    """A peer row without an ``id`` (defensive — shouldn't happen in
    prod but the loop defends) is silently skipped."""
    bus = EventBus()
    fed = _FakeFederationService()

    class _BrokenPeerRepo:
        async def list_social_instances(self):
            return await self.list_instances("confirmed")

        async def list_instances(self, status):
            class _NoId:
                pass

            return [_NoId(), _Peer("good")]

    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=_BrokenPeerRepo(),
    )
    out.wire()
    await bus.publish(_event())
    assert [r[0] for r in fed.sent] == ["good"]


async def test_v25_peer_gets_user_identity_binding():
    """A v_25 peer's USER_UPDATED payload carries the per-user binding fields
    and the resulting assertion verifies against the instance public key."""
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
    repo = _FakeFedRepo(["peer-1"])
    user_repo = _FakeUserRepo({"alice": (user_kp.public_key, user_kp.private_key)})
    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=repo,
        user_repo=user_repo,
    )
    out.wire()
    await bus.publish(_event(user_id=uid, username="alice", display_name="Alice"))

    payload = fed.sent[0][2]
    assert payload["user_identity_public_key"] == user_kp.public_key.hex()
    assert payload["user_sig_suite"] == USER_SIG_SUITE_ED25519
    assert "user_signature" in payload
    # Full self-verifying credential rides along (instance sig + issued_at).
    assert "user_assertion_signature" in payload
    assert "user_assertion_issued_at" in payload
    assert payload["display_name"] == "Alice"

    # The emitted payload reconstructs an assertion that verifies standalone.
    reconstructed = UserIdentityAssertion(
        user_id=uid,
        instance_id=iid,
        username="alice",
        display_name="Alice",
        issued_at=payload["user_assertion_issued_at"],
        signature=payload["user_assertion_signature"],
        user_identity_public_key=payload["user_identity_public_key"],
        user_pq_public_key=None,
        user_sig_suite=payload["user_sig_suite"],
        user_signature=payload["user_signature"],
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
    assert payload["user_signature"] == reference.user_signature


async def test_v24_peer_gets_legacy_shape_without_binding():
    """A sub-v_25 peer gets exactly the legacy USER_UPDATED shape."""
    bus = EventBus()
    instance_kp = generate_identity_keypair()
    user_kp = generate_identity_keypair()
    uid = derive_user_id(instance_kp.public_key, "alice")
    fed = _FakeFederationService(
        supports_v25=False,
        instance_seed=instance_kp.private_key,
    )
    repo = _FakeFedRepo(["peer-1"])
    user_repo = _FakeUserRepo({"alice": (user_kp.public_key, user_kp.private_key)})
    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=repo,
        user_repo=user_repo,
    )
    out.wire()
    await bus.publish(_event(user_id=uid, username="alice", display_name="Alice"))

    payload = fed.sent[0][2]
    assert payload == {
        "user_id": uid,
        "username": "alice",
        "display_name": "Alice",
        "bio": "hello",
        "picture_hash": "h1",
        "handle": None,
    }


async def test_binding_omitted_when_no_user_repo_wired():
    """Without a user_repo the service can't fetch the identity key, so it
    falls back to the legacy shape even for a v_25 peer (back-compat wiring)."""
    bus = EventBus()
    fed = _FakeFederationService(supports_v25=True)
    repo = _FakeFedRepo(["peer-1"])
    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=repo,
    )
    out.wire()
    await bus.publish(_event())
    payload = fed.sent[0][2]
    assert "user_identity_public_key" not in payload
