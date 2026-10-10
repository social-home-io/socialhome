"""The owner household's signature over a space's GFS list (migration 0089).

The list (:class:`socialhome.domain.gfs_server.SignedGfsServerList`) tells
member households which GFSs list the space, so members will send to those
servers. Only the space's OWNER household may say that: the list is signed
with the owner household's Ed25519 identity key, and receivers verify it with
keys they already hold — no new registry:

* ``derive_instance_id(owner_pk) == owner_instance_id`` — an instance id IS
  the hash of its identity key, so only the owner household can produce a
  list for a space it owns (same rule as :mod:`socialhome.authority_cert`);
* the signature covers the space id, the exact ``servers`` array, the
  ``issued_at`` counter, ``owner_pk`` and the suite tag, so a relaying
  household (a delegated admin's join snapshot) can carry the list but not
  change it, move it to another space, or swap the suite.

Signing bytes: ``b"space-gfs-servers:v1:" + canonical_json({space_id,
owner_instance_id, servers, issued_at, owner_pk, gfs_servers_sig_suite})``.

Freshness (``issued_at`` strictly above the stored one) is the caller's
check — this module only answers "did the owner sign exactly this".
"""

from __future__ import annotations

import binascii
import json

from .crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
    sign_ed25519,
    verify_ed25519,
)
from .domain.gfs_server import (
    GFS_SERVERS_SIG_SUITE_ED25519,
    SUPPORTED_GFS_SERVERS_SIG_SUITES,
    GfsServer,
    SignedGfsServerList,
    UnsupportedGfsServersSigSuite,
    canonical_json,
    gfs_servers_to_wire,
)

_PREFIX = b"space-gfs-servers:v1:"


def gfs_server_list_signing_bytes(
    *,
    space_id: str,
    owner_instance_id: str,
    servers_json: str,
    issued_at: int,
    owner_pk: str,
    suite: str,
) -> bytes:
    """Canonical, domain-separated bytes the owner signs."""
    body = {
        "space_id": space_id,
        "owner_instance_id": owner_instance_id,
        "servers": json.loads(servers_json),
        "issued_at": issued_at,
        "owner_pk": owner_pk,
        "gfs_servers_sig_suite": suite,
    }
    return _PREFIX + canonical_json(body).encode("utf-8")


def sign_gfs_server_list(
    *,
    space_id: str,
    servers: tuple[GfsServer, ...],
    issued_at: int,
    owner_seed: bytes,
) -> SignedGfsServerList:
    """The owner household signs *servers* for *space_id*."""
    owner_pk_bytes = ed25519_public_key(owner_seed)
    owner_pk = owner_pk_bytes.hex()
    servers_json = canonical_json(gfs_servers_to_wire(servers))
    message = gfs_server_list_signing_bytes(
        space_id=space_id,
        owner_instance_id=derive_instance_id(owner_pk_bytes),
        servers_json=servers_json,
        issued_at=issued_at,
        owner_pk=owner_pk,
        suite=GFS_SERVERS_SIG_SUITE_ED25519,
    )
    return SignedGfsServerList(
        servers_json=servers_json,
        issued_at=issued_at,
        owner_pk=owner_pk,
        gfs_servers_sig_suite=GFS_SERVERS_SIG_SUITE_ED25519,
        gfs_servers_sig=b64url_encode(sign_ed25519(owner_seed, message)),
    )


def verify_gfs_server_list(
    signed: SignedGfsServerList,
    *,
    space_id: str,
    owner_instance_id: str,
) -> bool:
    """Whether *signed* is the owner household's list for *space_id*.

    Raises :class:`UnsupportedGfsServersSigSuite` for a suite this build does
    not know (a hand-built instance; :meth:`SignedGfsServerList.from_wire`
    already refuses one). Any other failure answers ``False``.
    """
    if signed.gfs_servers_sig_suite not in SUPPORTED_GFS_SERVERS_SIG_SUITES:
        raise UnsupportedGfsServersSigSuite(
            f"unsupported gfs_servers_sig_suite {signed.gfs_servers_sig_suite!r}"
        )
    if not owner_instance_id or not space_id:
        return False
    try:
        owner_pk = bytes.fromhex(signed.owner_pk)
        sig = b64url_decode(signed.gfs_servers_sig)
    except ValueError, binascii.Error:
        return False
    if len(owner_pk) != 32 or derive_instance_id(owner_pk) != owner_instance_id:
        return False
    message = gfs_server_list_signing_bytes(
        space_id=space_id,
        owner_instance_id=owner_instance_id,
        servers_json=signed.servers_json,
        issued_at=signed.issued_at,
        owner_pk=signed.owner_pk,
        suite=signed.gfs_servers_sig_suite,
    )
    return verify_ed25519(owner_pk, message, sig)


__all__ = [
    "gfs_server_list_signing_bytes",
    "sign_gfs_server_list",
    "verify_gfs_server_list",
]
