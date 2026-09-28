"""user_identity_binding — derive the per-user identity-binding wire fields.

The helper is shared by the two outbound services that publish user info
to peers (``users_sync_outbound`` and ``profile_federation_outbound``). It
emits the ``user_identity_public_key`` / ``user_sig_suite`` /
``user_signature`` binding fields ONLY for peers that advertise v_25, and
only for users who actually have a minted identity key; everything else
gets the empty dict so the legacy payload shape is unchanged.
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
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.user import UserIdentityAssertion
from socialhome.services.user_identity_binding import user_identity_binding_fields


class _FakeFederation:
    def __init__(
        self,
        instance_seed: bytes,
        instance_pk: bytes,
        *,
        supports: bool,
        max_version: int = 999,
    ):
        self._own_identity_seed = instance_seed
        self._own_identity_pk = instance_pk
        self._own_instance_id = derive_instance_id(instance_pk)
        self._supports = supports
        # When ``supports`` is True, gate per-min_version: a peer "supports"
        # a capability only if its ``max_version`` is at or above the
        # requested threshold. Lets one fake model a v_25 peer (binds but no
        # anchor) vs a v_26 peer (binds with anchor).
        self._max_version = max_version
        self.support_calls: list[tuple[str, int]] = []

    @property
    def own_identity_seed(self) -> bytes:
        return self._own_identity_seed

    @property
    def own_instance_id(self) -> str:
        return self._own_instance_id

    async def peer_supports(self, instance_id: str, *, min_version: int) -> bool:
        self.support_calls.append((instance_id, min_version))
        if not self._supports:
            return False
        return self._max_version >= min_version


class _FakeUserRepo:
    def __init__(
        self,
        keypairs: dict[str, tuple[bytes, bytes]],
        anchors: dict[str, str] | None = None,
    ):
        self._keypairs = keypairs
        self._anchors = anchors or {}

    async def get_user_identity_keypair(self, username: str):
        return self._keypairs.get(username)

    async def get_user_identity_anchor(self, username: str):
        return self._anchors.get(username)


@pytest.fixture
def instance_kp():
    return generate_identity_keypair()


@pytest.fixture
def user_kp():
    return generate_identity_keypair()


async def test_v25_peer_gets_verifiable_binding(instance_kp, user_kp):
    fed = _FakeFederation(
        instance_kp.private_key, instance_kp.public_key, supports=True
    )
    iid = derive_instance_id(instance_kp.public_key)
    uid = derive_user_id(instance_kp.public_key, "alice")
    repo = _FakeUserRepo({"alice": (user_kp.public_key, user_kp.private_key)})

    fields = await user_identity_binding_fields(
        federation_service=fed,
        user_repo=repo,
        peer_instance_id="peer-1",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )

    # The entry now carries the FULL self-verifying assertion: the three
    # issued_at-independent binding fields PLUS the instance signature
    # (``user_assertion_signature``) and the assertion's ``issued_at``
    # (``user_assertion_issued_at``), so a relayed/cached copy can be
    # re-verified outside the original signed envelope.
    assert set(fields) == {
        "user_identity_public_key",
        "user_sig_suite",
        "user_signature",
        "user_assertion_signature",
        "user_assertion_issued_at",
    }
    assert fields["user_identity_public_key"] == user_kp.public_key.hex()
    assert fields["user_sig_suite"] == USER_SIG_SUITE_ED25519
    # No anchor configured on the repo, so no ``identity_anchor`` field even
    # though this peer would support it.
    assert "identity_anchor" not in fields
    # Both gates are consulted: the v_25 binding gate then the v_26 anchor gate.
    assert fed.support_calls == [
        ("peer-1", FederationCapability.MIN_FOR_USER_IDENTITY_KEY),
        ("peer-1", FederationCapability.MIN_FOR_IDENTITY_ANCHOR),
    ]

    # The emitted fields reconstruct an assertion that verifies end-to-end
    # against the issuing instance's public key — proving the entry is a
    # standalone, self-verifying portable credential.
    reconstructed = UserIdentityAssertion(
        user_id=uid,
        instance_id=iid,
        username="alice",
        display_name="Alice",
        issued_at=fields["user_assertion_issued_at"],
        signature=fields["user_assertion_signature"],
        user_identity_public_key=fields["user_identity_public_key"],
        user_pq_public_key=None,
        user_sig_suite=fields["user_sig_suite"],
        user_signature=fields["user_signature"],
    )
    verify_user_identity_assertion(reconstructed, instance_kp.public_key)

    # The three binding fields are issued_at-independent, so they match a
    # reference assertion regardless of when it was minted.
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
    assert fields["user_identity_public_key"] == reference.user_identity_public_key
    assert fields["user_sig_suite"] == reference.user_sig_suite
    assert fields["user_signature"] == reference.user_signature


async def test_v26_peer_gets_anchor_and_verifies(instance_kp, user_kp):
    """A v_26 peer gets the Phase-1 binding PLUS ``identity_anchor``, and the
    reconstructed assertion (user_id derived from the anchor) verifies."""
    anchor = "11111111-2222-3333-4444-555555555555"
    fed = _FakeFederation(
        instance_kp.private_key, instance_kp.public_key, supports=True, max_version=26
    )
    iid = derive_instance_id(instance_kp.public_key)
    # A uuid-anchored user: user_id derives from the anchor, not the username.
    uid = derive_user_id(instance_kp.public_key, anchor)
    repo = _FakeUserRepo(
        {"alice": (user_kp.public_key, user_kp.private_key)},
        anchors={"alice": anchor},
    )

    fields = await user_identity_binding_fields(
        federation_service=fed,
        user_repo=repo,
        peer_instance_id="peer-26",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )

    assert set(fields) == {
        "user_identity_public_key",
        "user_sig_suite",
        "user_signature",
        "user_assertion_signature",
        "user_assertion_issued_at",
        "identity_anchor",
    }
    assert fields["identity_anchor"] == anchor
    # Both gates were checked: v_25 (binding) then v_26 (anchor).
    assert fed.support_calls == [
        ("peer-26", FederationCapability.MIN_FOR_USER_IDENTITY_KEY),
        ("peer-26", FederationCapability.MIN_FOR_IDENTITY_ANCHOR),
    ]

    # The reconstructed assertion — with the anchor — verifies end-to-end, and
    # its user_id matches the anchor-derived id (not the username-derived one).
    reconstructed = UserIdentityAssertion(
        user_id=uid,
        instance_id=iid,
        username="alice",
        display_name="Alice",
        issued_at=fields["user_assertion_issued_at"],
        signature=fields["user_assertion_signature"],
        user_identity_public_key=fields["user_identity_public_key"],
        user_pq_public_key=None,
        user_sig_suite=fields["user_sig_suite"],
        user_signature=fields["user_signature"],
        identity_anchor=fields["identity_anchor"],
    )
    verify_user_identity_assertion(reconstructed, instance_kp.public_key)


async def test_v25_peer_gets_binding_without_anchor(instance_kp, user_kp):
    """A peer that supports USER_IDENTITY_KEY (v_25) but NOT IDENTITY_ANCHOR
    (v_26) gets the Phase-1 binding with NO ``identity_anchor`` on the wire,
    and the assertion it reconstructs (user_id derived from the username)
    verifies."""
    anchor = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    fed = _FakeFederation(
        instance_kp.private_key, instance_kp.public_key, supports=True, max_version=25
    )
    iid = derive_instance_id(instance_kp.public_key)
    # v_25 receiver derives user_id from the username — so the sender must
    # publish the username-derived id and the anchor-free binding.
    uid = derive_user_id(instance_kp.public_key, "alice")
    repo = _FakeUserRepo(
        {"alice": (user_kp.public_key, user_kp.private_key)},
        anchors={"alice": anchor},
    )

    fields = await user_identity_binding_fields(
        federation_service=fed,
        user_repo=repo,
        peer_instance_id="peer-25",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )

    assert "identity_anchor" not in fields
    assert set(fields) == {
        "user_identity_public_key",
        "user_sig_suite",
        "user_signature",
        "user_assertion_signature",
        "user_assertion_issued_at",
    }
    # The v_26 anchor gate was still consulted (and declined).
    assert fed.support_calls == [
        ("peer-25", FederationCapability.MIN_FOR_USER_IDENTITY_KEY),
        ("peer-25", FederationCapability.MIN_FOR_IDENTITY_ANCHOR),
    ]
    # The anchor-free assertion verifies against the username-derived user_id.
    reconstructed = UserIdentityAssertion(
        user_id=uid,
        instance_id=iid,
        username="alice",
        display_name="Alice",
        issued_at=fields["user_assertion_issued_at"],
        signature=fields["user_assertion_signature"],
        user_identity_public_key=fields["user_identity_public_key"],
        user_pq_public_key=None,
        user_sig_suite=fields["user_sig_suite"],
        user_signature=fields["user_signature"],
    )
    verify_user_identity_assertion(reconstructed, instance_kp.public_key)


async def test_sub_v25_peer_gets_no_binding(instance_kp, user_kp):
    fed = _FakeFederation(
        instance_kp.private_key, instance_kp.public_key, supports=False
    )
    uid = derive_user_id(instance_kp.public_key, "alice")
    repo = _FakeUserRepo({"alice": (user_kp.public_key, user_kp.private_key)})

    fields = await user_identity_binding_fields(
        federation_service=fed,
        user_repo=repo,
        peer_instance_id="peer-1",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )
    assert fields == {}


async def test_v25_peer_no_minted_key_gets_no_binding(instance_kp):
    fed = _FakeFederation(
        instance_kp.private_key, instance_kp.public_key, supports=True
    )
    uid = derive_user_id(instance_kp.public_key, "alice")
    repo = _FakeUserRepo({})  # no keypair for alice

    fields = await user_identity_binding_fields(
        federation_service=fed,
        user_repo=repo,
        peer_instance_id="peer-1",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )
    assert fields == {}


@pytest.mark.parametrize("supports", [False, True], ids=["pre-v25", "no-key"])
async def test_without_a_binding_the_plain_anchor_still_travels(instance_kp, supports):
    """With no binding to carry it (a peer whose version isn't known yet, or a
    user without a minted key), the anchor still rides along on its own so the
    receiver can re-derive a first-seen ``user_id`` from the sender's key."""
    fed = _FakeFederation(
        instance_kp.private_key, instance_kp.public_key, supports=supports
    )
    uid = derive_user_id(instance_kp.public_key, "anchor-uuid")
    repo = _FakeUserRepo({}, anchors={"alice": "anchor-uuid"})

    fields = await user_identity_binding_fields(
        federation_service=fed,
        user_repo=repo,
        peer_instance_id="peer-1",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )
    assert fields == {"identity_anchor": "anchor-uuid"}


async def test_no_user_repo_gets_no_binding(instance_kp):
    fed = _FakeFederation(
        instance_kp.private_key, instance_kp.public_key, supports=True
    )
    uid = derive_user_id(instance_kp.public_key, "alice")

    fields = await user_identity_binding_fields(
        federation_service=fed,
        user_repo=None,
        peer_instance_id="peer-1",
        user_id=uid,
        username="alice",
        display_name="Alice",
    )
    assert fields == {}
