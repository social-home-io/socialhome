"""A mesh-only member household's protocol version claim.

A household that is a member of a space whose host it is NOT paired with
reaches that host only over the mesh (``SPACE_ROUTED`` through a relay).
The host then holds no ``remote_instances`` row for it — by design: that
row is what the §24.11 inbox gates on, so seating one would let an unpaired
household post any event type straight into our inbox (see migration
0045). Without a row the host had no ``proto_version`` to gate on, and every
per-household credential gated on it (writer cert v_49, writer key v_50,
private channel grant v_51) failed closed for exactly these members.

The member therefore tells its host, over the mesh, which version it runs
and which identity key it holds::

    member_proto_version : int    # OURS of the member's build
    member_identity_pk   : "<64 hex>"   # its Ed25519 identity public key

The claim rides inside an origin-authenticated payload only — the inner
event of a ``SPACE_ROUTED`` envelope (``INSTANCE_CAPABILITIES_UPDATED``, the
``SPACE_PRIVATE_INVITE_ACCEPT``, the ``SPACE_INVITE_TOKEN_REDEEM``). The
inner payload is sealed end to end to the host, and the v_31 routed-origin
signature over the ciphertext proves ``path[0]`` (the claimant) wrote it, so
a relay can neither read nor alter it. The identity key needs no further
trust: an instance id IS the SHA-256 fingerprint of that key (§4.1.2), so
:func:`parse_mesh_member_claim` accepts only the key that derives to the
sender. No new key, signature or event type.

Not a crypto wire shape: nothing is signed or sealed under a new suite here
(the routed envelope's own ``origin_sig_suite`` covers the transport), so
there is no ``*_suite`` tag.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..crypto import derive_instance_id
from ..domain.federation_capabilities import OURS

#: Wire key of the claimed ``proto_version``.
MESH_CLAIM_VERSION_FIELD: str = "member_proto_version"

#: Wire key of the claimant's identity public key (hex).
MESH_CLAIM_IDENTITY_PK_FIELD: str = "member_identity_pk"

#: Sanity ceiling on a claimed version — far above any real build, low
#: enough that a bogus value can't be mistaken for one.
MAX_CLAIMED_PROTO_VERSION: int = 10_000


@dataclass(slots=True, frozen=True)
class MeshMemberClaim:
    """A parsed, derivation-checked claim."""

    proto_version: int
    identity_pk: bytes


def mesh_member_claim(own_identity_pk: bytes) -> dict:
    """The claim fields our household adds to a mesh-routed payload."""
    return {
        MESH_CLAIM_VERSION_FIELD: OURS,
        MESH_CLAIM_IDENTITY_PK_FIELD: own_identity_pk.hex(),
    }


def parse_mesh_member_claim(
    payload: dict, *, instance_id: str
) -> MeshMemberClaim | None:
    """The claim in ``payload`` from household ``instance_id``, or ``None``
    when absent or malformed: the version must be a plain int in
    ``1..MAX_CLAIMED_PROTO_VERSION`` and the key must be 32 bytes that
    derive to ``instance_id``. Never raises."""
    if not isinstance(payload, dict):
        return None
    version = payload.get(MESH_CLAIM_VERSION_FIELD)
    pk_hex = payload.get(MESH_CLAIM_IDENTITY_PK_FIELD)
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or not 1 <= version <= MAX_CLAIMED_PROTO_VERSION
    ):
        return None
    if not isinstance(pk_hex, str) or len(pk_hex) != 64:
        return None
    try:
        pk = bytes.fromhex(pk_hex)
        if derive_instance_id(pk) != instance_id:
            return None
    except ValueError:
        return None
    return MeshMemberClaim(proto_version=version, identity_pk=pk)
