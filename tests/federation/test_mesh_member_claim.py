"""Tests for the mesh-only member's version claim
(:mod:`socialhome.federation.mesh_member_claim`)."""

from __future__ import annotations

import os

import pytest

from socialhome.crypto import derive_instance_id, ed25519_public_key
from socialhome.domain.federation_capabilities import OURS
from socialhome.federation.mesh_member_claim import (
    MESH_CLAIM_IDENTITY_PK_FIELD,
    MESH_CLAIM_VERSION_FIELD,
    MeshMemberClaim,
    mesh_member_claim,
    parse_mesh_member_claim,
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
