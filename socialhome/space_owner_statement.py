"""Who owns a space — the owner household's signed statement (migration 0093).

Space ids are random (``SpaceService.create_space``), never derived from a
key, so a stub's owner is otherwise just whatever the inviting household
said. When a household is invited by somebody OTHER than the owner (a
delegated admin), it seats the stub under the claimed owner only when two
signatures, over keys it can bind first-hand, say so:

* the **owner statement** — the owner household's Ed25519 identity key signs
  ``{space_id, owner_instance_id, owner_pk, authority_pk,
  authority_key_epoch, issued_at, owner_statement_sig_suite}``, with
  ``derive_instance_id(owner_pk) == owner_instance_id`` (an instance id IS
  the hash of its identity key). Nobody but the owner can name the owner;
  it binds the exact space authority key the snapshot pins. Honest members
  relay the owner's latest statement in their own snapshots.
* the **inviter proof** — the inviting household signs
  ``{space_id, owner_instance_id, inviter_instance_id, authority_pk}`` with
  the space AUTHORITY seed (:mod:`socialhome.authority_sig`, event type
  ``space_inviter_proof``): only the owner and delegated admins hold it, and
  the inviter's own id is bound in, so a plain member that relays the
  owner's statement cannot pass itself off as a delegated admin, and nobody
  can replay another household's proof.

Without both, the join falls back to seating the stub under the inviting
household itself (the pre-0093 behaviour): no inviting-admin trust.

Signing bytes of the statement: ``b"space-owner-statement:v1:"`` +
canonical JSON (sorted keys, compact) of the statement minus its signature.
``issued_at`` is unix milliseconds; a value more than
:data:`MAX_STATEMENT_FUTURE_SKEW_MS` ahead of the receiver's clock is
refused (and never signed), so a runaway value cannot lock members out of
later statements.

Depends only on :mod:`socialhome.crypto` and :mod:`socialhome.authority_sig`.
"""

from __future__ import annotations

import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .authority_sig import (
    UnsupportedAuthoritySuite,
    sign_authority_event,
    strip_authority_sig_fields,
    verify_authority_event,
)
from .crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
    sign_ed25519,
    verify_ed25519,
)

OWNER_STATEMENT_SUITE_ED25519: str = "ed25519"
SUPPORTED_OWNER_STATEMENT_SUITES: frozenset[str] = frozenset(
    {OWNER_STATEMENT_SUITE_ED25519}
)

#: How far ahead of the receiver's clock ``issued_at`` may be.
MAX_STATEMENT_FUTURE_SKEW_MS: int = 24 * 60 * 60 * 1000

#: How old a statement may be and still be taken. A statement names the
#: authority key of its moment; an admin revoked by a rotation still holds
#: the OLD key and the owner's old statement for it, so an old statement
#: must expire. The owner re-signs on every rotation and every
#: :data:`STATEMENT_REFRESH_MS` (well inside this window) and members relay
#: the newest.
MAX_STATEMENT_AGE_MS: int = 30 * 24 * 60 * 60 * 1000

#: How often the owner household re-broadcasts a fresh statement.
STATEMENT_REFRESH_MS: int = 7 * 24 * 60 * 60 * 1000

MAX_STATEMENT_INT: int = 2**63 - 1

OWNER_STATEMENT_FIELDS: frozenset[str] = frozenset(
    {
        "space_id",
        "owner_instance_id",
        "owner_pk",
        "authority_pk",
        "authority_key_epoch",
        "issued_at",
        "owner_statement_sig_suite",
        "owner_statement_sig",
    }
)

#: The authority-event type of the inviter proof.
INVITER_PROOF_EVENT: str = "space_inviter_proof"

_PREFIX = b"space-owner-statement:v1:"


class UnsupportedOwnerStatementSuite(ValueError):
    """The statement names a signature suite this build does not know (or
    none). Rejected — never defaulted."""


class InvalidOwnerStatement(ValueError):
    """Malformed, bound to another space / owner / key, out of the time
    window, or its signature does not verify."""


@dataclass(slots=True, frozen=True)
class VerifiedOwnerStatement:
    """What a verified statement establishes."""

    space_id: str
    owner_instance_id: str
    owner_pk_hex: str
    authority_pk_hex: str
    authority_key_epoch: int
    issued_at: int


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def owner_statement_signing_bytes(statement: dict) -> bytes:
    body = {k: v for k, v in statement.items() if k != "owner_statement_sig"}
    return _PREFIX + json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def sign_owner_statement(
    *,
    space_id: str,
    owner_seed: bytes,
    authority_pk_hex: str,
    authority_key_epoch: int,
    issued_at_ms: int | None = None,
    now_ms: int | None = None,
) -> dict:
    """The owner household signs that it owns *space_id* under the authority
    key *authority_pk_hex*. Raises :class:`ValueError` for an ``issued_at``
    more than a day ahead of now (receivers would refuse it)."""
    now = _now_ms() if now_ms is None else now_ms
    issued_at = now if issued_at_ms is None else issued_at_ms
    if (
        not _is_int(issued_at)
        or issued_at < 0
        or issued_at > MAX_STATEMENT_INT
        or issued_at > now + MAX_STATEMENT_FUTURE_SKEW_MS
    ):
        raise ValueError("issued_at out of range")
    if not _is_int(authority_key_epoch) or authority_key_epoch < 0:
        raise ValueError("authority_key_epoch out of range")
    owner_pk = ed25519_public_key(owner_seed)
    statement: dict = {
        "space_id": space_id,
        "owner_instance_id": derive_instance_id(owner_pk),
        "owner_pk": owner_pk.hex(),
        "authority_pk": authority_pk_hex.lower(),
        "authority_key_epoch": authority_key_epoch,
        "issued_at": issued_at,
        "owner_statement_sig_suite": OWNER_STATEMENT_SUITE_ED25519,
    }
    statement["owner_statement_sig"] = b64url_encode(
        sign_ed25519(owner_seed, owner_statement_signing_bytes(statement))
    )
    return statement


def _hex32(value: object, field: str) -> bytes:
    if not isinstance(value, str):
        raise InvalidOwnerStatement(f"{field} is not a string")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise InvalidOwnerStatement(f"{field} is not hex") from exc
    if len(raw) != 32:
        raise InvalidOwnerStatement(f"{field} is not 32 bytes")
    return raw


def verify_owner_statement(
    statement: object,
    *,
    space_id: str,
    owner_instance_id: str,
    authority_pk_hex: str,
    authority_key_epoch: int | None = None,
    now_ms: int | None = None,
    max_age_ms: int | None = MAX_STATEMENT_AGE_MS,
) -> VerifiedOwnerStatement:
    """Verify that *owner_instance_id* signed that it owns *space_id* under
    *authority_pk_hex* (at *authority_key_epoch*, when given), no older than
    *max_age_ms*. Raises :class:`UnsupportedOwnerStatementSuite` or
    :class:`InvalidOwnerStatement`; ordering against a stored statement is
    the caller's decision."""
    if not isinstance(statement, dict):
        raise InvalidOwnerStatement("statement is not an object")
    if statement.get("owner_statement_sig_suite") not in (
        SUPPORTED_OWNER_STATEMENT_SUITES
    ):
        raise UnsupportedOwnerStatementSuite(
            str(statement.get("owner_statement_sig_suite"))
        )
    missing = OWNER_STATEMENT_FIELDS - set(statement)
    if missing:
        raise InvalidOwnerStatement(f"statement is missing {sorted(missing)}")
    if statement["space_id"] != space_id:
        raise InvalidOwnerStatement("statement is for another space")
    if not owner_instance_id or statement["owner_instance_id"] != owner_instance_id:
        raise InvalidOwnerStatement("statement names another owner")
    owner_pk = _hex32(statement["owner_pk"], "owner_pk")
    _hex32(statement["authority_pk"], "authority_pk")
    if derive_instance_id(owner_pk) != owner_instance_id:
        raise InvalidOwnerStatement("owner_pk does not derive to the owner id")
    if str(statement["authority_pk"]).lower() != str(authority_pk_hex or "").lower():
        raise InvalidOwnerStatement("statement binds another authority key")
    epoch = statement["authority_key_epoch"]
    if not _is_int(epoch) or epoch < 0 or epoch > MAX_STATEMENT_INT:
        raise InvalidOwnerStatement("authority_key_epoch out of range")
    issued_at = statement["issued_at"]
    if not _is_int(issued_at) or issued_at < 0:
        raise InvalidOwnerStatement("issued_at out of range")
    now = _now_ms() if now_ms is None else now_ms
    if issued_at > now + MAX_STATEMENT_FUTURE_SKEW_MS:
        raise InvalidOwnerStatement("issued_at is too far in the future")
    if max_age_ms is not None and issued_at < now - max_age_ms:
        raise InvalidOwnerStatement("statement is too old")
    if authority_key_epoch is not None and epoch != authority_key_epoch:
        raise InvalidOwnerStatement("statement names another key epoch")
    sig_text = statement["owner_statement_sig"]
    if not isinstance(sig_text, str):
        raise InvalidOwnerStatement("owner_statement_sig is not a string")
    try:
        sig = b64url_decode(sig_text)
    except (ValueError, binascii.Error) as exc:
        raise InvalidOwnerStatement("owner_statement_sig is not base64url") from exc
    if not verify_ed25519(owner_pk, owner_statement_signing_bytes(statement), sig):
        raise InvalidOwnerStatement("statement signature does not verify")
    return VerifiedOwnerStatement(
        space_id=space_id,
        owner_instance_id=owner_instance_id,
        owner_pk_hex=str(statement["owner_pk"]).lower(),
        authority_pk_hex=str(statement["authority_pk"]).lower(),
        authority_key_epoch=epoch,
        issued_at=issued_at,
    )


def _proof_body(
    *,
    space_id: str,
    owner_instance_id: str,
    inviter_instance_id: str,
    authority_key_epoch: int,
) -> dict:
    return {
        "space_id": space_id,
        "owner_instance_id": owner_instance_id,
        "inviter_instance_id": inviter_instance_id,
        "authority_key_epoch": authority_key_epoch,
    }


def sign_inviter_proof(
    *,
    space_id: str,
    owner_instance_id: str,
    inviter_instance_id: str,
    space_seed: bytes,
    authority_key_epoch: int = 0,
) -> dict:
    """The inviting household proves it holds the space authority seed (of
    the key at *authority_key_epoch*)."""
    body = _proof_body(
        space_id=space_id,
        owner_instance_id=owner_instance_id,
        inviter_instance_id=inviter_instance_id,
        authority_key_epoch=authority_key_epoch,
    )
    body["authority_pk"] = ed25519_public_key(space_seed).hex()
    body.update(
        sign_authority_event(
            event_type=INVITER_PROOF_EVENT,
            space_id=space_id,
            payload=dict(body),
            space_seed=space_seed,
        )
    )
    return body


def verify_inviter_proof(
    proof: object,
    *,
    space_id: str,
    owner_instance_id: str,
    inviter_instance_id: str,
    authority_pk_hex: str,
    authority_key_epoch: int = 0,
) -> bool:
    """Whether *inviter_instance_id* proved it holds the seed of
    *authority_pk_hex* (at *authority_key_epoch*) for *space_id* owned by
    *owner_instance_id*. An unknown suite or any malformed field answers
    ``False``."""
    if not isinstance(proof, dict):
        return False
    expected = _proof_body(
        space_id=space_id,
        owner_instance_id=owner_instance_id,
        inviter_instance_id=inviter_instance_id,
        authority_key_epoch=authority_key_epoch,
    )
    expected["authority_pk"] = str(authority_pk_hex or "").lower()
    if strip_authority_sig_fields(proof) != expected:
        return False
    sig = proof.get("authority_sig")
    suite = proof.get("authority_sig_suite")
    if not isinstance(sig, str) or not isinstance(suite, str):
        return False
    try:
        return verify_authority_event(
            event_type=INVITER_PROOF_EVENT,
            space_id=space_id,
            payload=expected,
            authority_sig=sig,
            authority_sig_suite=suite,
            space_public_key=bytes.fromhex(expected["authority_pk"]),
        )
    except UnsupportedAuthoritySuite, ValueError:
        return False


__all__ = [
    "INVITER_PROOF_EVENT",
    "MAX_STATEMENT_AGE_MS",
    "STATEMENT_REFRESH_MS",
    "MAX_STATEMENT_FUTURE_SKEW_MS",
    "OWNER_STATEMENT_FIELDS",
    "OWNER_STATEMENT_SUITE_ED25519",
    "SUPPORTED_OWNER_STATEMENT_SUITES",
    "InvalidOwnerStatement",
    "UnsupportedOwnerStatementSuite",
    "VerifiedOwnerStatement",
    "owner_statement_signing_bytes",
    "sign_inviter_proof",
    "sign_owner_statement",
    "verify_inviter_proof",
    "verify_owner_statement",
]
