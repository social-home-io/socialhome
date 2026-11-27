"""Space-authority signature primitives — pure, low-level, dependency-light.

A *space-authority* event is signed with the space's Ed25519 private seed
(Phase 0, ``space_repo.get_space_seed`` / ``ensure_space_seed``) rather than
any single household's key. Any household that legitimately holds the seed
(the owner, or a delegated admin per Phase 1) can emit one, and EVERY receiver
trusts it by verifying against ``spaces.identity_public_key`` — independent of
which household relayed it. That decoupling is what lets a space keep working
while the owner is offline (the "owner-offline spaces" epic).

The verifier is a pure function over the space's PUBLIC key, so the §24.11
inbound handler can authenticate the event without ever holding the seed.

This module depends ONLY on :mod:`socialhome.crypto` (Ed25519 sign/verify +
base64url helpers) so it can be imported by the content-blind GFS process
(the authority cert and join contact checks on a listing publish) WITHOUT
dragging in the HFS federation/crypto stack (AESGCM, KeyManager). Since v_96
no GFS route authorizes a space-keyed relay, notice or media upload with it:
those event types went with the space-keyed paths. The HFS-side
:mod:`socialhome.services.space_crypto_service` re-exports every name here, so
existing importers keep working unchanged.
"""

from __future__ import annotations

from .canonical_json import canonical_str
from .crypto import (
    b64url_decode,
    b64url_encode,
    sign_ed25519,
    verify_ed25519,
)

#: Suite identifier for the space-authority signature, shipped on the wire
#: alongside the signature so the algorithm can swap without breaking older
#: receivers (crypto-suite rule). Today Ed25519; Phase-2 of ``docs/crypto.md``
#: introduces a sibling ``"ed25519+mldsa65"`` (parallel PQ signature). Receivers
#: MUST reject any suite not in ``SUPPORTED_AUTHORITY_SIG_SUITES`` — never fall
#: back to a default, or a downgrade attack becomes possible.
AUTHORITY_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_AUTHORITY_SIG_SUITES: frozenset[str] = frozenset(
    {AUTHORITY_SIG_SUITE_ED25519}
)


class UnsupportedAuthoritySuite(ValueError):
    """Raised when a space-authority signature advertises a suite this build
    doesn't know. Receivers MUST reject rather than fall back to a default."""


#: v_96 — an authority-only item (a removal, an approved post, a member's
#: post the host puts on the channel for its author) sealed into a space's
#: opaque channel: the space authority signs the INNER, inside the channel
#: ciphertext — the channel frame itself carries no authority signature (the
#: GFS never sees it).
AUTHORITY_EVENT_SPACE_CHANNEL_AUTHORITY: str = "space_channel_authority"


#: Authority event-type for a sync page (``docs/protocol/sync-pages.md``): a
#: seed-holder acting as host signs its commitment to the space's state
#: (:mod:`socialhome.sync_page`). Members verify it against the space key they
#: pin. A page travels only inside a §25.6 session, never through a GFS.
AUTHORITY_EVENT_SPACE_SYNC_PAGE: str = "space_sync_page"


#: The two wire fields :func:`sign_authority_event` produces and the sender
#: merges into the event payload. The signature is computed over the payload
#: with these removed, so the verifier MUST strip the SAME two keys before
#: calling :func:`verify_authority_event` — otherwise the canonical bytes
#: differ and a legitimate signature fails. Naming them once here keeps both
#: sides in lock-step.
_AUTHORITY_SIG_FIELDS: frozenset[str] = frozenset(
    {"authority_sig", "authority_sig_suite"}
)


def strip_authority_sig_fields(payload: dict) -> dict:
    """Return a copy of ``payload`` without the two authority-signature fields.

    The signer signs over the *bare* payload (no signature fields) and merges
    ``authority_sig`` / ``authority_sig_suite`` in afterwards; the verifier
    must reconstruct those exact bare bytes by stripping them back out. Using
    this single helper on BOTH sides guarantees the canonical signing bytes
    match. Returns a new dict — the input is never mutated.
    """
    return {k: v for k, v in payload.items() if k not in _AUTHORITY_SIG_FIELDS}


def authority_signing_bytes(
    *,
    event_type: str,
    space_id: str,
    payload: dict,
) -> bytes:
    """Canonical, domain-separated message bytes for a space-authority event.

    Binds the event type + space id + the FULL payload under a versioned
    domain-separation prefix so a signature can't be lifted onto a different
    event type, a different space, or a mutated payload. ``sort_keys`` +
    compact separators make the encoding canonical — equivalent payloads
    (same keys/values, any insertion order) produce identical bytes, so
    signer and verifier always agree.
    """
    body = canonical_str(
        {"event_type": event_type, "space_id": space_id, "payload": payload}
    )
    return b"space-authority:v1:" + body.encode()


def sign_authority_event(
    *,
    event_type: str,
    space_id: str,
    payload: dict,
    space_seed: bytes,
) -> dict:
    """Sign a space-authority event with the space's Ed25519 seed.

    Returns the wire fields to merge into the outgoing event:
    ``authority_sig`` (b64url) + ``authority_sig_suite``.
    """
    sig = sign_ed25519(
        space_seed,
        authority_signing_bytes(
            event_type=event_type, space_id=space_id, payload=payload
        ),
    )
    return {
        "authority_sig": b64url_encode(sig),
        "authority_sig_suite": AUTHORITY_SIG_SUITE_ED25519,
    }


def verify_authority_event(
    *,
    event_type: str,
    space_id: str,
    payload: dict,
    authority_sig: str,
    authority_sig_suite: str,
    space_public_key: bytes,
) -> bool:
    """Verify a space-authority signature against the space's public key.

    Raises :class:`UnsupportedAuthoritySuite` for an unknown suite (no default
    fallback). Returns ``False`` for a malformed signature or a verification
    failure. Pure function — needs only the space PUBLIC key, never the seed.
    """
    if authority_sig_suite not in SUPPORTED_AUTHORITY_SIG_SUITES:
        raise UnsupportedAuthoritySuite(authority_sig_suite)
    try:
        sig = b64url_decode(authority_sig)
    except ValueError, TypeError:
        return False
    return verify_ed25519(
        space_public_key,
        authority_signing_bytes(
            event_type=event_type, space_id=space_id, payload=payload
        ),
        sig,
    )


def authority_payload_verified(
    *,
    event_type: str,
    space_id: str,
    payload: dict,
    space_public_key_hex: str,
) -> bool:
    """Whether ``payload`` carries a valid space-authority signature (its
    ``authority_sig`` / ``authority_sig_suite`` over the payload without
    them) under the pinned ``space_public_key_hex``. Fail closed: a missing
    field, an unknown suite or a bad key is ``False``."""
    sig = payload.get("authority_sig")
    suite = payload.get("authority_sig_suite")
    if not isinstance(sig, str) or not sig or not isinstance(suite, str):
        return False
    try:
        return verify_authority_event(
            event_type=event_type,
            space_id=space_id,
            payload=strip_authority_sig_fields(payload),
            authority_sig=sig,
            authority_sig_suite=suite,
            space_public_key=bytes.fromhex(space_public_key_hex),
        )
    except UnsupportedAuthoritySuite, ValueError, TypeError:
        return False
