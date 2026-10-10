"""Tests for the owner household's signature over a space's GFS list."""

from __future__ import annotations

import dataclasses
import json

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.gfs_server import (
    GfsServer,
    SignedGfsServerList,
    UnsupportedGfsServersSigSuite,
)
from socialhome.gfs_server_list_sig import (
    gfs_server_list_signing_bytes,
    sign_gfs_server_list,
    verify_gfs_server_list,
)

_OWNER = generate_identity_keypair()
_OWNER_ID = derive_instance_id(_OWNER.public_key)
_SERVERS = (
    GfsServer(
        gfs_instance_id="gfs-a",
        inbox_url="https://gfs-a.example",
        public_key="ab" * 32,
        gfs_key_suite="ed25519",
        display_name="A",
    ),
)


def _sign(**over) -> SignedGfsServerList:
    kw = {
        "space_id": "sp-1",
        "servers": _SERVERS,
        "issued_at": 1000,
        "owner_seed": _OWNER.private_key,
    }
    kw.update(over)
    return sign_gfs_server_list(**kw)


def test_owner_signature_verifies_and_survives_the_wire() -> None:
    signed = _sign()
    assert signed.owner_pk == _OWNER.public_key.hex()
    assert verify_gfs_server_list(signed, space_id="sp-1", owner_instance_id=_OWNER_ID)
    relayed = SignedGfsServerList.from_wire(json.loads(json.dumps(signed.to_wire())))
    assert relayed == signed
    assert verify_gfs_server_list(relayed, space_id="sp-1", owner_instance_id=_OWNER_ID)
    assert relayed.servers() == (_SERVERS, 0)


def test_another_space_or_owner_fails() -> None:
    signed = _sign()
    assert not verify_gfs_server_list(
        signed, space_id="sp-2", owner_instance_id=_OWNER_ID
    )
    other = derive_instance_id(generate_identity_keypair().public_key)
    assert not verify_gfs_server_list(signed, space_id="sp-1", owner_instance_id=other)
    assert not verify_gfs_server_list(signed, space_id="sp-1", owner_instance_id="")


def test_a_non_owner_key_cannot_sign_for_the_owner() -> None:
    admin = generate_identity_keypair()
    signed = _sign(owner_seed=admin.private_key)
    assert not verify_gfs_server_list(
        signed, space_id="sp-1", owner_instance_id=_OWNER_ID
    )
    # …and claiming the owner's key does not help.
    forged = dataclasses.replace(signed, owner_pk=_OWNER.public_key.hex())
    assert not verify_gfs_server_list(
        forged, space_id="sp-1", owner_instance_id=_OWNER_ID
    )


@pytest.mark.parametrize(
    "change",
    [
        {"issued_at": 1001},
        {"servers_json": "[]"},
        {"gfs_servers_sig": "AAAA"},
        {"owner_pk": "zz"},
    ],
)
def test_any_change_breaks_the_signature(change) -> None:
    tampered = dataclasses.replace(_sign(), **change)
    assert not verify_gfs_server_list(
        tampered, space_id="sp-1", owner_instance_id=_OWNER_ID
    )


def test_unknown_suite_raises() -> None:
    tampered = dataclasses.replace(_sign(), gfs_servers_sig_suite="future")
    with pytest.raises(UnsupportedGfsServersSigSuite):
        verify_gfs_server_list(tampered, space_id="sp-1", owner_instance_id=_OWNER_ID)


def test_signing_bytes_are_domain_separated_and_bind_every_field() -> None:
    base = {
        "space_id": "s",
        "owner_instance_id": "o",
        "servers_json": "[]",
        "issued_at": 1,
        "owner_pk": "ab" * 32,
        "suite": "ed25519",
    }
    raw = gfs_server_list_signing_bytes(**base)
    assert raw.startswith(b"space-gfs-servers:v1:")
    for key, value in [
        ("space_id", "t"),
        ("owner_instance_id", "p"),
        ("servers_json", '[{"a":1}]'),
        ("issued_at", 2),
        ("owner_pk", "cd" * 32),
        ("suite", "x"),
    ]:
        assert gfs_server_list_signing_bytes(**{**base, key: value}) != raw


def test_empty_list_signs() -> None:
    signed = _sign(servers=())
    assert verify_gfs_server_list(signed, space_id="sp-1", owner_instance_id=_OWNER_ID)
    assert signed.servers() == ((), 0)
