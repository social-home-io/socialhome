"""Username-rename federation round-trip (mutable username, §v_26).

A rename publishes :class:`UserProfileUpdated` carrying the NEW username
(:meth:`UserService.rename_username` / :meth:`apply_ha_username`).
:class:`ProfileFederationOutbound` fans that out as ``USER_UPDATED`` to every
paired peer, re-building the per-user identity binding fresh from the *live*
(renamed) user row — so for a v_26 peer the binding's signatures commit to the
NEW username while the immutable ``identity_anchor`` / ``user_id`` are
unchanged, and the assertion still verifies against the sender's instance key.

On the receiving side :meth:`FederationInboundService._on_user_updated`
updates the peer's ``remote_users.remote_username`` in place (same
``user_id`` row, no dupe) and re-verifies + keeps the stored binding.

This suite drives BOTH halves with their real implementations against real
SQLite so a stale-username regression (signing the OLD name after a rename)
fails loudly:

1. outbound after rename carries ``username='bobby'`` + a binding whose
   reconstructed assertion ``verify_user_identity_assertion`` ACCEPTS, and
   whose signed username is ``'bobby'`` (not the stale ``'bob'``);
2. feeding that payload to the inbound handler renames the peer's
   ``remote_users`` row in place (same ``user_id``, no dupe) and re-stores the
   verified binding;
3. the full round-trip (outbound-after-rename → inbound) leaves the receiver
   with the new username on the same ``user_id``.
"""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    verify_user_identity_assertion,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.user import UserIdentityAssertion
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories import (
    SqliteConversationRepo,
    SqliteSpacePostRepo,
    SqliteSpaceRepo,
    SqliteUserRepo,
)
from socialhome.services.federation_inbound_service import (
    FederationInboundService,
)
from socialhome.services.profile_federation_outbound import (
    ProfileFederationOutbound,
)
from socialhome.services.user_service import UserService


# ── Sender side ──────────────────────────────────────────────────────────


class _SenderFederation:
    """Federation stub for the sending household.

    Advertises full v_26 support (binding + anchor) for every peer, exposes
    the live instance seed / id the binding is signed with, and records every
    outbound send.
    """

    def __init__(self, *, instance_seed: bytes, instance_id: str) -> None:
        self._own_identity_seed = instance_seed
        self._own_instance_id = instance_id
        self.sent: list[tuple[str, FederationEventType, dict]] = []

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    @property
    def own_identity_seed(self) -> bytes:
        return self._own_identity_seed

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        # v_26 peer: supports every capability the binding gates on.
        return True

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sent.append((to_instance_id, event_type, payload))
        return None


class _OnePeerRepo:
    class _Peer:
        def __init__(self, instance_id: str) -> None:
            self.id = instance_id

    def __init__(self, peer_id: str) -> None:
        self._peer_id = peer_id

    async def list_social_instances(self):
        return await self.list_instances("confirmed")

    async def list_instances(self, status: str):
        assert status == "confirmed"
        return [self._Peer(self._peer_id)]


@pytest.fixture
async def sender(tmp_dir):
    """A real UserService + ProfileFederationOutbound over a KEK-wired repo.

    Provisioning mints a real per-user identity key; the outbound service
    re-reads the keypair by username at fan-out time, so a rename is reflected
    in both the federated ``username`` AND the freshly-signed binding.
    """
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "sender.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    key_manager = KeyManager.from_data_dir(tmp_dir)
    user_repo = SqliteUserRepo(db, key_manager=key_manager)
    user_svc = UserService(
        user_repo,
        bus,
        own_instance_public_key=kp.public_key,
        key_manager=key_manager,
    )
    fed = _SenderFederation(instance_seed=kp.private_key, instance_id=iid)
    out = ProfileFederationOutbound(
        bus=bus,
        federation_service=fed,
        federation_repo=_OnePeerRepo("peer-1"),
        user_repo=user_repo,
    )
    out.wire()

    class S:
        pass

    s = S()
    s.db = db
    s.bus = bus
    s.user_svc = user_svc
    s.fed = fed
    s.instance_pk = kp.public_key
    s.instance_id = iid
    yield s
    await db.shutdown()


# ── Receiver side ──────────────────────────────────────────────────────────


class _ReceiverFederation:
    def __init__(self, keys: dict[str, bytes]) -> None:
        self._keys = keys
        self.own_instance_id = "self"

    async def peer_identity_public_key(self, instance_id: str):
        return self._keys.get(instance_id)


async def _seed_peer(db: AsyncDatabase, instance_id: str, pk_hex: str) -> None:
    await db.enqueue(
        """INSERT INTO remote_instances(
               id, display_name, remote_identity_pk,
               key_self_to_remote, key_remote_to_self,
               remote_inbox_url, local_inbox_id,
               status, source, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            instance_id,
            "Sender",
            pk_hex,
            "enc",
            "enc",
            "https://peer/wh",
            "wh-" + instance_id,
            "confirmed",
            "manual",
            "2026-01-01T00:00:00+00:00",
        ),
    )


@pytest.fixture
async def receiver(tmp_dir, sender):
    """A real FederationInboundService whose pinned key for the sender is the
    sender's real instance public key — so the binding verifies end-to-end."""
    db = AsyncDatabase(tmp_dir / "receiver.db", batch_timeout_ms=10)
    await db.startup()
    await _seed_peer(db, sender.instance_id, sender.instance_pk.hex())
    bus = EventBus()
    service = FederationInboundService(
        bus=bus,
        conversation_repo=SqliteConversationRepo(db),
        space_post_repo=SqliteSpacePostRepo(db),
        space_repo=SqliteSpaceRepo(db),
        user_repo=SqliteUserRepo(db),
    )
    service._federation_service = _ReceiverFederation(
        {sender.instance_id: sender.instance_pk}
    )

    class R:
        pass

    r = R()
    r.db = db
    r.service = service
    yield r
    await db.shutdown()


def _user_updated_event(payload: dict, *, from_instance: str) -> FederationEvent:
    return FederationEvent(
        msg_id="msg-user-updated",
        event_type=FederationEventType.USER_UPDATED,
        from_instance=from_instance,
        to_instance="self",
        timestamp="2026-06-15T00:00:00+00:00",
        payload=payload,
        space_id=None,
        media_bytes=None,
    )


# ── 1. Outbound carries new username + valid binding ────────────────────────


async def test_outbound_after_rename_signs_new_username(sender):
    bob = await sender.user_svc.provision(
        username="bob", display_name="Bob", source="manual"
    )

    await sender.user_svc.rename_username("bob", "bobby")

    assert len(sender.fed.sent) == 1
    to_instance, event_type, payload = sender.fed.sent[0]
    assert to_instance == "peer-1"
    assert event_type is FederationEventType.USER_UPDATED

    # The federated username reflects the rename; user_id is unchanged.
    assert payload["username"] == "bobby"
    assert payload["user_id"] == bob.user_id

    # The v_26 binding rides along with the unchanged anchor.
    anchor = payload["identity_anchor"]
    assert anchor == bob.identity_anchor

    # The reconstructed assertion VERIFIES against the sender's instance key,
    # proving the binding was re-signed over the NEW username (not stale 'bob')
    # with the unchanged anchor / user_id.
    reconstructed = UserIdentityAssertion(
        user_id=payload["user_id"],
        instance_id=sender.instance_id,
        username=payload["username"],
        display_name=payload["display_name"],
        issued_at=payload["user_assertion_issued_at"],
        signature=payload["user_assertion_signature"],
        user_identity_public_key=payload["user_identity_public_key"],
        user_pq_public_key=None,
        user_sig_suite=payload["user_sig_suite"],
        user_signature=payload["user_signature"],
        identity_anchor=anchor,
    )
    verify_user_identity_assertion(reconstructed, sender.instance_pk)
    # The signed username is the NEW one.
    assert reconstructed.username == "bobby"


async def test_outbound_binding_with_stale_username_would_not_verify(sender):
    """Guard: if the binding were (re)built with the OLD username after a
    rename, verify would reject it — the new-username assertion above is the
    only one that passes. This pins the failure mode the fix prevents."""
    await sender.user_svc.provision(username="bob", display_name="Bob", source="manual")
    await sender.user_svc.rename_username("bob", "bobby")
    payload = sender.fed.sent[0][2]

    stale = UserIdentityAssertion(
        user_id=payload["user_id"],
        instance_id=sender.instance_id,
        username="bob",  # stale
        display_name=payload["display_name"],
        issued_at=payload["user_assertion_issued_at"],
        signature=payload["user_assertion_signature"],
        user_identity_public_key=payload["user_identity_public_key"],
        user_pq_public_key=None,
        user_sig_suite=payload["user_sig_suite"],
        user_signature=payload["user_signature"],
        identity_anchor=payload["identity_anchor"],
    )
    with pytest.raises(ValueError):
        verify_user_identity_assertion(stale, sender.instance_pk)


# ── 2. Inbound applies the rename in place ──────────────────────────────────


async def test_inbound_renames_remote_user_in_place(sender, receiver):
    # First publication establishes the remote row under the OLD username.
    bob = await sender.user_svc.provision(
        username="bob", display_name="Bob", source="manual"
    )
    # Manually emit the pre-rename USER_UPDATED by patching the profile so the
    # outbound fires once with username 'bob'.
    await sender.user_svc.patch_profile("bob", display_name="Bob")
    first_payload = sender.fed.sent[-1][2]
    assert first_payload["username"] == "bob"
    await receiver.service._on_user_updated(
        _user_updated_event(first_payload, from_instance=sender.instance_id)
    )

    row = await receiver.db.fetchone(
        "SELECT user_id, remote_username, user_identity_public_key "
        "FROM remote_users WHERE user_id=?",
        (bob.user_id,),
    )
    assert row["remote_username"] == "bob"
    assert row["user_identity_public_key"] is not None

    # Now rename and deliver the rename USER_UPDATED.
    await sender.user_svc.rename_username("bob", "bobby")
    rename_payload = sender.fed.sent[-1][2]
    await receiver.service._on_user_updated(
        _user_updated_event(rename_payload, from_instance=sender.instance_id)
    )

    # Same user_id row, renamed in place — no duplicate.
    rows = await receiver.db.fetchall(
        "SELECT user_id, remote_username, user_identity_public_key "
        "FROM remote_users WHERE user_id=?",
        (bob.user_id,),
    )
    assert len(rows) == 1
    assert rows[0]["remote_username"] == "bobby"
    # Binding re-verified + kept.
    assert rows[0]["user_identity_public_key"] is not None

    # No second row crept in under any other key.
    total = await receiver.db.fetchone("SELECT COUNT(*) AS n FROM remote_users")
    assert total["n"] == 1


# ── 3. Round-trip ───────────────────────────────────────────────────────────


async def test_round_trip_outbound_after_rename_to_inbound(sender, receiver):
    bob = await sender.user_svc.provision(
        username="bob", display_name="Bob", source="manual"
    )
    await sender.user_svc.rename_username("bob", "bobby")
    payload = sender.fed.sent[-1][2]

    await receiver.service._on_user_updated(
        _user_updated_event(payload, from_instance=sender.instance_id)
    )

    row = await receiver.db.fetchone(
        "SELECT user_id, remote_username, identity_anchor, "
        "user_identity_public_key FROM remote_users WHERE user_id=?",
        (bob.user_id,),
    )
    assert row is not None
    assert row["user_id"] == bob.user_id
    assert row["remote_username"] == "bobby"
    assert row["identity_anchor"] == bob.identity_anchor
    assert row["user_identity_public_key"] is not None
