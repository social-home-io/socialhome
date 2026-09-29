"""Origin signature for relayed ``MOMENT_*`` events (v_35).

A moment fans out up to three hops: the author's household sends it to
its paired peers, and each of them re-sends the same payload onward under
its *own* envelope signature. The §24.11 pipeline therefore authenticates
only the last hop. ``origin_instance_id`` names the household the moment
came from, but on a relayed delivery nothing tied that field to the
origin household itself.

The origin household now signs every ``MOMENT_CREATED`` / ``MOMENT_DELETED``
it authors with its existing Ed25519 **identity** key (no new key — the
receiver verifies against the key it already pins for that household, or
against a shipped key bound to the origin id by
:func:`~socialhome.crypto.derive_instance_id`, §4.1.2). Three sibling
fields ride inside the encrypted payload (§25.8.21)::

    origin_identity_pk : "<64 hex>"   # the origin's Ed25519 identity pub
    origin_sig         : "<b64url>"   # Ed25519 over the signing bytes
    origin_sig_suite   : "ed25519"    # suite tag; unknown value → reject

Relays forward them verbatim (they never re-sign); ``hop_count`` is the
only field a relay changes and it is deliberately outside the signature.

The signing bytes are ``b"moment-origin:v1:"`` followed by canonical JSON
(sorted keys, compact separators, UTF-8) of the event type, the suite, and
every field the receiver stores: for a create the id, author, origin,
content, media fields, parent, ``expires_at`` and ``occurred_at``; for a
delete the id, author, origin and ``occurred_at``. The event type is
inside the signed object, so a signed create can never be replayed as a
delete of the same moment, and the domain prefix keeps the bytes disjoint
from every other identity-key signature in the protocol.

**v_38 — ``no_relay``.** A moment its origin marks ``no_relay: true`` (a
protected account's, §CP.R) is for the origin's directly paired households
only: no household relays it, and a receiver refuses one that arrives from
anyone but the origin. The mark is signed — its bytes use the
``b"moment-origin:v2:"`` domain and carry ``"no_relay": true`` — so a relay
can neither strip it (the remaining v1 bytes don't verify) nor forge it. A
moment without the mark signs exactly the v1 bytes, so older receivers
verify it unchanged; the origin never sends a marked moment to a household
below v_38 at all (it might relay it).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    sign_ed25519,
    verify_ed25519,
)
from ..domain.federation import FederationEventType

#: Signature suite for the moment origin signature. Same tag vocabulary as
#: ``Encoder.sig_suite``; Phase 2 of ``docs/crypto.md`` adds
#: ``"ed25519+mldsa65"`` as a sibling constant with both signatures
#: produced + verified in parallel.
MOMENT_ORIGIN_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_MOMENT_ORIGIN_SIG_SUITES: frozenset[str] = frozenset(
    {MOMENT_ORIGIN_SIG_SUITE_ED25519}
)

#: Payload keys carrying the origin signature.
ORIGIN_SIG_FIELD = "origin_sig"
ORIGIN_SIG_SUITE_FIELD = "origin_sig_suite"
ORIGIN_IDENTITY_PK_FIELD = "origin_identity_pk"

_DOMAIN = b"moment-origin:v1:"
#: Signing domain of a ``no_relay`` moment (v_38) — disjoint from v1, so the
#: mark can't be removed or added without breaking the signature.
_DOMAIN_NO_RELAY = b"moment-origin:v2:"

#: Payload key marking a moment for direct delivery only (v_38).
NO_RELAY_FIELD = "no_relay"


def carries_no_relay(payload: dict[str, Any]) -> bool:
    """Whether *payload* claims the ``no_relay`` mark at all — any value.
    Relays and receivers treat a present key as the mark (fail closed); the
    signature decides whether it is genuine."""
    return NO_RELAY_FIELD in payload


#: The payload fields the origin signs, per event type. Everything the
#: receiver stores; never ``hop_count`` (each relay bumps it).
_SIGNED_FIELDS: dict[FederationEventType, tuple[str, ...]] = {
    FederationEventType.MOMENT_CREATED: (
        "moment_id",
        "author_user_id",
        "origin_instance_id",
        "content",
        "media_url",
        "media_type",
        "duration_ms",
        "parent_moment_id",
        "expires_at",
        "occurred_at",
    ),
    FederationEventType.MOMENT_DELETED: (
        "moment_id",
        "author_user_id",
        "origin_instance_id",
        "occurred_at",
    ),
}

#: Event types that carry an origin signature.
SIGNED_MOMENT_EVENT_TYPES: frozenset[FederationEventType] = frozenset(_SIGNED_FIELDS)


class UnsupportedMomentOriginSuite(ValueError):
    """Raised when a moment's origin signature advertises a suite this
    build does not know. Receivers MUST reject rather than verify under a
    default algorithm — stripping the tag must never downgrade the check."""


def moment_origin_signing_bytes(
    event_type: FederationEventType,
    payload: dict[str, Any],
    *,
    sig_suite: str,
) -> bytes:
    """Canonical bytes the origin household signs for ``payload``.

    Raises :class:`ValueError` for an event type that carries no origin
    signature.
    """
    fields = _SIGNED_FIELDS.get(event_type)
    if fields is None:
        raise ValueError(f"{event_type!r} carries no moment origin signature")
    signed: dict[str, Any] = {k: payload.get(k) for k in fields}
    signed["event_type"] = event_type.value
    signed["sig_suite"] = sig_suite
    domain = _DOMAIN
    if carries_no_relay(payload):
        signed[NO_RELAY_FIELD] = payload[NO_RELAY_FIELD]
        domain = _DOMAIN_NO_RELAY
    return domain + json.dumps(
        signed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_moment_origin(
    *,
    seed: bytes,
    identity_pk: bytes,
    event_type: FederationEventType,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Origin-side: return ``payload`` plus the three origin-signature fields.

    ``payload`` is not mutated.
    """
    suite = MOMENT_ORIGIN_SIG_SUITE_ED25519
    sig = sign_ed25519(
        seed,
        moment_origin_signing_bytes(event_type, payload, sig_suite=suite),
    )
    return {
        **payload,
        ORIGIN_IDENTITY_PK_FIELD: identity_pk.hex(),
        ORIGIN_SIG_SUITE_FIELD: suite,
        ORIGIN_SIG_FIELD: b64url_encode(sig),
    }


def verify_moment_origin(
    *,
    identity_pk: bytes,
    event_type: FederationEventType,
    payload: dict[str, Any],
) -> bool:
    """Receiver-side: verify ``payload``'s origin signature under ``identity_pk``.

    Raises :class:`UnsupportedMomentOriginSuite` for a suite outside
    :data:`SUPPORTED_MOMENT_ORIGIN_SIG_SUITES` — never falls back to a
    default. Returns ``False`` (never raises) for a missing, malformed or
    non-verifying signature, or an event type that carries none.
    """
    suite = str(payload.get(ORIGIN_SIG_SUITE_FIELD) or "")
    if suite not in SUPPORTED_MOMENT_ORIGIN_SIG_SUITES:
        raise UnsupportedMomentOriginSuite(
            f"moment origin signature advertises unsupported sig_suite="
            f"{suite!r}; this build supports "
            f"{sorted(SUPPORTED_MOMENT_ORIGIN_SIG_SUITES)!r}",
        )
    if event_type not in _SIGNED_FIELDS:
        return False
    try:
        sig = b64url_decode(str(payload.get(ORIGIN_SIG_FIELD) or ""))
    except ValueError, TypeError:
        return False
    return verify_ed25519(
        identity_pk,
        moment_origin_signing_bytes(event_type, payload, sig_suite=suite),
        sig,
    )


@dataclass(slots=True, frozen=True)
class OriginCheck:
    """Outcome of :func:`check_relayed_moment_origin`.

    ``accepted`` is the verdict; ``legacy`` marks an unsigned relay taken on
    the pre-signature window; ``reason`` explains a refusal for the log.
    """

    accepted: bool
    legacy: bool = False
    reason: str = ""


def check_relayed_moment_origin(
    *,
    event_type: FederationEventType,
    payload: dict[str, Any],
    origin_instance_id: str,
    pinned_pk: bytes | None,
    origin_signs: bool,
) -> OriginCheck:
    """Decide whether a *relayed* moment really comes from its claimed origin.

    ``pinned_pk`` is the identity key this household pins for the origin
    (``None`` when it holds no row for it); ``origin_signs`` is whether that
    row advertises a ``proto_version`` at or above the signing threshold.

    * **Signed:** verified against ``pinned_pk``, or — when there is no
      pinned key — against the shipped ``origin_identity_pk`` provided it
      derives to ``origin_instance_id`` (§4.1.2). A shipped key that
      disagrees with the pinned one, an unknown suite, or a signature that
      does not verify is refused.
    * **Unsigned:** accepted (``legacy=True``) only from an origin this
      household holds a row for that is known to predate signing. An
      unsigned relay naming a signing origin, or an origin with no row, is
      refused — it is indistinguishable from a forgery.
    """
    if event_type not in SIGNED_MOMENT_EVENT_TYPES:
        return OriginCheck(False, reason="event type carries no origin signature")
    if not payload.get(ORIGIN_SIG_FIELD):
        if pinned_pk is None:
            return OriginCheck(False, reason="unsigned, and the origin is unknown")
        if origin_signs:
            return OriginCheck(
                False, reason="unsigned, but the origin signs its moments"
            )
        return OriginCheck(True, legacy=True)
    shipped: bytes | None = None
    shipped_hex = payload.get(ORIGIN_IDENTITY_PK_FIELD)
    if shipped_hex:
        try:
            shipped = bytes.fromhex(str(shipped_hex))
        except ValueError:
            return OriginCheck(False, reason="malformed origin_identity_pk")
    if pinned_pk is not None:
        if shipped is not None and shipped != pinned_pk:
            return OriginCheck(
                False, reason="origin_identity_pk differs from the pinned key"
            )
        key = pinned_pk
    elif shipped is None:
        return OriginCheck(False, reason="unknown origin and no origin_identity_pk")
    else:
        try:
            derived = derive_instance_id(shipped)
        except ValueError:
            return OriginCheck(False, reason="origin_identity_pk is not 32 bytes")
        if derived != origin_instance_id:
            return OriginCheck(
                False, reason="origin_identity_pk does not derive to the origin"
            )
        key = shipped
    try:
        ok = verify_moment_origin(
            identity_pk=key, event_type=event_type, payload=payload
        )
    except UnsupportedMomentOriginSuite:
        return OriginCheck(False, reason="unsupported origin_sig_suite")
    if not ok:
        return OriginCheck(False, reason="origin signature does not verify")
    return OriginCheck(True)


__all__ = [
    "OriginCheck",
    "check_relayed_moment_origin",
    "MOMENT_ORIGIN_SIG_SUITE_ED25519",
    "NO_RELAY_FIELD",
    "carries_no_relay",
    "ORIGIN_IDENTITY_PK_FIELD",
    "ORIGIN_SIG_FIELD",
    "ORIGIN_SIG_SUITE_FIELD",
    "SIGNED_MOMENT_EVENT_TYPES",
    "SUPPORTED_MOMENT_ORIGIN_SIG_SUITES",
    "UnsupportedMomentOriginSuite",
    "moment_origin_signing_bytes",
    "sign_moment_origin",
    "verify_moment_origin",
]
