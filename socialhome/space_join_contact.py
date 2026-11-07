"""A global space's join contact — where an "Ask to join" is sealed to (v_84).

The owner household publishes, inside its signed GFS listing, the keys a
household that never paired with it needs to seal a join request to it:
its instance id, identity key, key-wrap key and the key-wrap key's
self-signature. The set is signed with the **space authority key** — the
key the GFS already pins for the space (it authorizes ``/gfs/publish``) and
that the listing serves as ``identity_public_key`` — so a GFS cannot swap
the keys without breaking a signature it cannot make.

Pure and dependency-light (only :mod:`socialhome.authority_sig`): the GFS
checks the shape and the signature against its pin before it serves a
contact; the household checks it again, plus the key-wrap binding, against
the authority key it pinned for the space on first sight.

Wire shape (one object, ``join_contact`` on the listing)::

    {"instance_id": "...", "identity_pk": "<hex>", "keywrap_pk": "<hex>",
     "kem_suite": "x25519", "keywrap_sig": "<b64url>",
     "issued_at": "<tz-aware ISO 8601>",
     "authority_sig": "<b64url>", "authority_sig_suite": "ed25519"}

``issued_at`` is signed with the rest: a reader keeps the newest contact it
saw for a space and never goes back to an older one, so a GFS cannot replay
a contact from before the owner changed its key-wrap key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .authority_sig import (
    UnsupportedAuthoritySuite,
    sign_authority_event,
    strip_authority_sig_fields,
    verify_authority_event,
)

#: Authority event type the contact is signed under (never a relay type).
AUTHORITY_EVENT_SPACE_JOIN_CONTACT: str = "space_join_contact"

#: KEM suites a contact's key-wrap key may carry (crypto-suite rule).
JOIN_CONTACT_KEM_SUITE_X25519: str = "x25519"
SUPPORTED_JOIN_CONTACT_KEM_SUITES: frozenset[str] = frozenset(
    {JOIN_CONTACT_KEM_SUITE_X25519}
)

#: The signed fields, in no particular order (the signature is over their
#: canonical JSON).
CONTACT_FIELDS: tuple[str, ...] = (
    "instance_id",
    "identity_pk",
    "keywrap_pk",
    "kem_suite",
    "keywrap_sig",
    "issued_at",
)

#: Upper bound on any one field (hex keys are 64, a signature ~86).
_MAX_FIELD_CHARS = 200


def sign_join_contact(
    *,
    space_id: str,
    instance_id: str,
    identity_pk: str,
    keywrap_pk: str,
    keywrap_sig: str,
    space_seed: bytes,
    kem_suite: str = JOIN_CONTACT_KEM_SUITE_X25519,
    issued_at: str | None = None,
) -> dict[str, str]:
    """The owner's contact for *space_id*, signed with the space seed."""
    payload = {
        "instance_id": instance_id,
        "identity_pk": identity_pk,
        "keywrap_pk": keywrap_pk,
        "kem_suite": kem_suite,
        "keywrap_sig": keywrap_sig,
        "issued_at": issued_at or datetime.now(UTC).isoformat(),
    }
    sig = sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_JOIN_CONTACT,
        space_id=space_id,
        payload=payload,
        space_seed=space_seed,
    )
    return {**payload, **sig}


def parse_join_contact(raw: object) -> dict[str, str] | None:
    """A contact's exact wire shape (strings, bounded, no extra keys), or
    ``None``. Says nothing about its signature."""
    if not isinstance(raw, dict):
        return None
    allowed = set(CONTACT_FIELDS) | {"authority_sig", "authority_sig_suite"}
    if set(raw) != allowed:
        return None
    out: dict[str, str] = {}
    for key in allowed:
        value = raw[key]
        if not isinstance(value, str) or not value or len(value) > _MAX_FIELD_CHARS:
            return None
        out[key] = value
    if out["kem_suite"] not in SUPPORTED_JOIN_CONTACT_KEM_SUITES:
        return None
    if contact_issued_at(out) is None:
        return None
    return out


def contact_issued_at(contact: dict[str, str]) -> datetime | None:
    """The contact's signed ``issued_at`` (tz-aware), or ``None``."""
    try:
        ts = datetime.fromisoformat(contact.get("issued_at", ""))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


def verify_join_contact(
    contact: dict[str, Any], *, space_id: str, space_public_key_hex: str
) -> bool:
    """Does *contact* carry a valid space-authority signature for
    *space_id* under ``space_public_key_hex``? ``False`` for any malformed
    input or an unknown suite (never a default)."""
    parsed = parse_join_contact(contact)
    if parsed is None:
        return False
    try:
        space_pk = bytes.fromhex(space_public_key_hex)
        return verify_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_JOIN_CONTACT,
            space_id=space_id,
            payload=strip_authority_sig_fields(parsed),
            authority_sig=parsed["authority_sig"],
            authority_sig_suite=parsed["authority_sig_suite"],
            space_public_key=space_pk,
        )
    except UnsupportedAuthoritySuite, ValueError:
        return False


__all__ = [
    "AUTHORITY_EVENT_SPACE_JOIN_CONTACT",
    "CONTACT_FIELDS",
    "JOIN_CONTACT_KEM_SUITE_X25519",
    "SUPPORTED_JOIN_CONTACT_KEM_SUITES",
    "contact_issued_at",
    "parse_join_contact",
    "sign_join_contact",
    "verify_join_contact",
]
