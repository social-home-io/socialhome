"""Move-out link domain type (move-out link, §move-out).

A :class:`MoveLink` is a verifiable claim that a person — identified by a
portable per-user Ed25519 pubkey ``P`` (shipped in independent-user-identity
Phase 1) — moved from ``old_user_id@old_instance_id`` to a new home. The new
home's P↔new_id binding rides inside the embedded
:class:`~socialhome.domain.user.UserIdentityAssertion` (so ``new_user_id`` /
``new_instance_id`` come from that assertion, not from a duplicated field on
the link). ``user_id`` stays household-scoped — a move is NOT a re-keying of
the cryptographic id.

Two signatures bind the link:

* :attr:`user_signature` — by ``P``, proving the person authorised the move
  (canonical bytes: :func:`socialhome.crypto.move_link_user_signed_bytes`).
* :attr:`release_signature` — by the *old* home's instance key, proving the
  releasing household vouches for the destination (canonical bytes:
  :func:`socialhome.crypto.move_link_release_signed_bytes`, which commits to
  ``new_user_id`` + ``new_instance_public_key`` — the destination-pin).

Verification (``build_move_link`` / ``verify_move_link``) is a later task and
lives in the service/crypto layer, not here — this module is the pure wire
dataclass only.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import WrongTypeError
from .user import UserIdentityAssertion


@dataclass(slots=True, frozen=True)
class MoveLink:
    """A signed move-out claim (pure domain dataclass — no I/O)."""

    suite: str  # move-link signature suite tag, e.g. "ed25519"
    user_public_key: str  # hex Ed25519 portable user pubkey P
    old_user_id: str
    old_instance_id: str
    issued_at: str  # ISO-8601 UTC (tz-aware)
    # Hex instance pubkey of the new home, relayed by the old home; the
    # destination-pin both signatures commit to.
    new_instance_public_key: str
    # The new home's P↔new_id binding: an assertion carrying new_user_id (==
    # user_id), new_instance_id (== instance_id) and identity_anchor, signed
    # by the new home's instance key.
    new_home_assertion: UserIdentityAssertion
    user_signature: str  # base64url Ed25519 signature by P
    release_signature: str  # base64url Ed25519 signature by old home instance key

    @property
    def new_user_id(self) -> str:
        """The destination user_id — sourced from the embedded binding."""
        return self.new_home_assertion.user_id

    @property
    def new_instance_id(self) -> str:
        """The destination instance_id — sourced from the embedded binding."""
        return self.new_home_assertion.instance_id

    def to_wire_dict(self) -> dict[str, object]:
        """Serialise to the federation wire shape; the embedded assertion
        serialises via its own :meth:`UserIdentityAssertion.to_wire_dict`."""
        return {
            "suite": self.suite,
            "user_public_key": self.user_public_key,
            "old_user_id": self.old_user_id,
            "old_instance_id": self.old_instance_id,
            "issued_at": self.issued_at,
            "new_instance_public_key": self.new_instance_public_key,
            "new_home_assertion": self.new_home_assertion.to_wire_dict(),
            "user_signature": self.user_signature,
            "release_signature": self.release_signature,
        }

    @classmethod
    def from_wire_dict(cls, data: dict[str, object]) -> MoveLink:
        """Reconstruct from the wire shape produced by :meth:`to_wire_dict`."""
        assertion_data = data["new_home_assertion"]
        if not isinstance(assertion_data, dict):
            raise WrongTypeError("new_home_assertion must be a wire dict")
        return cls(
            suite=str(data["suite"]),
            user_public_key=str(data["user_public_key"]),
            old_user_id=str(data["old_user_id"]),
            old_instance_id=str(data["old_instance_id"]),
            issued_at=str(data["issued_at"]),
            new_instance_public_key=str(data["new_instance_public_key"]),
            new_home_assertion=UserIdentityAssertion.from_wire_dict(assertion_data),
            user_signature=str(data["user_signature"]),
            release_signature=str(data["release_signature"]),
        )
