"""Tests for the mesh-only member's version claim
(:mod:`socialhome.federation.mesh_member_claim`)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from socialhome.crypto import derive_instance_id, ed25519_public_key
from socialhome.domain.federation_capabilities import OURS
from socialhome.federation.mesh_member_claim import (
    MESH_CLAIM_IDENTITY_PK_FIELD,
    MESH_CLAIM_VERSION_FIELD,
    MeshMemberClaim,
    mesh_member_claim,
    parse_mesh_member_claim,
    sender_identity_pk,
)

PK = ed25519_public_key(os.urandom(32))
IID = derive_instance_id(PK)


def test_claim_carries_our_version_and_identity_key():
    claim = mesh_member_claim(PK)
    assert claim == {
        MESH_CLAIM_VERSION_FIELD: OURS,
        MESH_CLAIM_IDENTITY_PK_FIELD: PK.hex(),
    }


def test_parse_round_trip():
    parsed = parse_mesh_member_claim(mesh_member_claim(PK), instance_id=IID)
    assert parsed == MeshMemberClaim(proto_version=OURS, identity_pk=PK)


def test_parse_ignores_unrelated_fields():
    payload = {"space_id": "sp", **mesh_member_claim(PK)}
    assert parse_mesh_member_claim(payload, instance_id=IID) is not None


def test_no_claim_is_none():
    assert parse_mesh_member_claim({"space_id": "sp"}, instance_id=IID) is None


def test_key_that_does_not_derive_to_the_sender_is_refused():
    """A claim names the sender's own identity key: an instance id IS the
    fingerprint of that key (§4.1.2), so a relay or anyone else cannot
    attach a key of their own."""
    other = ed25519_public_key(os.urandom(32))
    payload = mesh_member_claim(other)
    assert parse_mesh_member_claim(payload, instance_id=IID) is None


@pytest.mark.parametrize(
    "version",
    [None, "51", 51.0, True, False, 0, -3, 10_001],
)
def test_malformed_version_is_refused(version):
    payload = {
        MESH_CLAIM_VERSION_FIELD: version,
        MESH_CLAIM_IDENTITY_PK_FIELD: PK.hex(),
    }
    assert parse_mesh_member_claim(payload, instance_id=IID) is None


@pytest.mark.parametrize("pk", [None, 7, "zz" * 32, "ab" * 31, "ab" * 33])
def test_malformed_identity_key_is_refused(pk):
    payload = {MESH_CLAIM_VERSION_FIELD: OURS, MESH_CLAIM_IDENTITY_PK_FIELD: pk}
    assert parse_mesh_member_claim(payload, instance_id=IID) is None


def test_non_dict_payload_is_none():
    assert parse_mesh_member_claim(None, instance_id=IID) is None  # type: ignore[arg-type]


class _Fed:
    def __init__(self, pinned: dict[str, bytes]) -> None:
        self.pinned = pinned

    async def peer_identity_public_key(self, instance_id):
        return self.pinned.get(instance_id)


def _ev(payload, *, routed: bool, sender: str = IID):
    return SimpleNamespace(
        from_instance=sender,
        payload=payload,
        routed_path=[sender, "us"] if routed else None,
    )


async def test_sender_key_is_the_pinned_one_first():
    pinned = ed25519_public_key(os.urandom(32))
    fed = _Fed({IID: pinned})
    got = await sender_identity_pk(fed, _ev(mesh_member_claim(PK), routed=True))
    assert got == pinned


async def test_sender_key_of_a_mesh_only_household_is_its_routed_claim():
    got = await sender_identity_pk(_Fed({}), _ev(mesh_member_claim(PK), routed=True))
    assert got == PK


@pytest.mark.parametrize(
    ("payload", "routed"),
    [
        (mesh_member_claim(PK), False),  # not origin-authenticated
        ({}, True),  # no claim
        (mesh_member_claim(ed25519_public_key(os.urandom(32))), True),  # not ours
    ],
    ids=["unrouted", "no-claim", "foreign-key"],
)
async def test_sender_key_is_none_without_a_pin_or_a_valid_claim(payload, routed):
    assert await sender_identity_pk(_Fed({}), _ev(payload, routed=routed)) is None


async def test_sender_key_without_federation_is_none():
    assert (
        await sender_identity_pk(None, _ev(mesh_member_claim(PK), routed=True)) is None
    )
