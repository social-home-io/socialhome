"""Signed reaction totals of a moment (v_69, owner decision 10).

Only a moment's author household holds every reaction to it — paired,
2–3-hop (v_67) and GFS followers' (v_68) all reach it — so only it can say
how many people reacted. It re-broadcasts the **totals**: per-emoji counts
(the :data:`~socialhome.domain.moment.MOMENT_COUNTS_MAX_EMOJIS` most used)
and the total over every emoji. Never who reacted.

On the mesh the totals ride ``MOMENT_REACTIONS_COUNTED`` (hop-encrypted like
every federation payload, §25.8.21) and are relayed like a
``MOMENT_CREATED``, so every household down the 3-hop mesh must be able to
check them against the author's household — a relay must not be able to
inflate or zero a count. The origin signs them with its existing Ed25519
**identity** key (no new key — the receiver verifies against the key it
pins for that household, or a shipped key bound to the origin id by
:func:`~socialhome.crypto.derive_instance_id`, §4.1.2, exactly as the
moment's own origin signature)::

    origin_identity_pk : "<64 hex>"   # the origin's Ed25519 identity pub
    counts_sig         : "<b64url>"   # Ed25519 over the signing bytes
    counts_sig_suite   : "ed25519"    # suite tag; unknown value → reject

The signing bytes are ``b"moment-counts:v1:"`` + canonical JSON
(:mod:`socialhome.canonical_json`) of every field but ``hop_count`` (each
relay bumps it) and ``counts_sig`` itself: ``moment_id``,
``author_user_id``, ``origin_instance_id``, ``rev``, ``counts``, ``total``,
``origin_identity_pk``, ``counts_sig_suite`` and — for a protected
account's moment, which goes to paired households only — ``no_relay``. The
domain prefix keeps the bytes disjoint from every other identity-key
signature in the protocol (``moment-origin:``, ``moment-reaction:`` …).

Import-light on purpose, like :mod:`socialhome.moment_origin`.
"""

from __future__ import annotations

from typing import Any

from .canonical_json import canonical_utf8_bytes
from .crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    sign_ed25519,
    verify_ed25519,
)
from .domain.moment import reaction_counts_valid

#: Signature suite of the totals signature. Same tag vocabulary as
#: ``Encoder.sig_suite``; Phase 2 of ``docs/crypto.md`` adds
#: ``"ed25519+mldsa65"`` as a sibling constant (both signatures, in parallel).
COUNTS_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_COUNTS_SIG_SUITES: frozenset[str] = frozenset({COUNTS_SIG_SUITE_ED25519})

#: Payload keys of the signature.
COUNTS_SIG_FIELD = "counts_sig"
COUNTS_SIG_SUITE_FIELD = "counts_sig_suite"
ORIGIN_IDENTITY_PK_FIELD = "origin_identity_pk"
#: A protected account's moment (§CP.R): its totals go to paired households
#: only and are never relayed. Signed, so a relay can't strip it.
NO_RELAY_FIELD = "no_relay"

_DOMAIN = b"moment-counts:v1:"

#: Every field of a ``MOMENT_REACTIONS_COUNTED`` payload (``no_relay`` only
#: when set). A payload naming anything else is refused.
COUNTS_FIELDS: frozenset[str] = frozenset(
    {
        "moment_id",
        "author_user_id",
        "origin_instance_id",
        "rev",
        "counts",
        "total",
        "hop_count",
        ORIGIN_IDENTITY_PK_FIELD,
        COUNTS_SIG_SUITE_FIELD,
        COUNTS_SIG_FIELD,
    }
)

#: Longest id-shaped field (moment, user, instance id).
MAX_COUNTS_ID_CHARS: int = 256

#: Oldest a totals ``rev`` may be (now − ``rev``, ms) and still be kept or
#: relayed: a day of outbox retries for an offline peer plus clock skew.
#: Anything older is a replay — refused before any budget is touched.
MAX_COUNTS_REV_AGE_MS: int = 25 * 3600 * 1000

#: Fields outside the signature.
_UNSIGNED = frozenset({"hop_count", COUNTS_SIG_FIELD})


class UnsupportedCountsSigSuite(ValueError):
    """A totals signature advertises a suite this build does not know.
    Receivers MUST reject rather than verify under a default algorithm."""


def counts_signing_bytes(payload: dict[str, Any]) -> bytes:
    """Canonical bytes the origin household signs for *payload*."""
    signed = {k: v for k, v in payload.items() if k not in _UNSIGNED}
    return _DOMAIN + canonical_utf8_bytes(signed)


def sign_counts(
    *,
    seed: bytes,
    identity_pk: bytes,
    moment_id: str,
    author_user_id: str,
    origin_instance_id: str,
    rev: int,
    counts: dict[str, int],
    total: int,
    no_relay: bool = False,
) -> dict[str, Any]:
    """Origin side: the ``MOMENT_REACTIONS_COUNTED`` payload at hop 1."""
    payload: dict[str, Any] = {
        "moment_id": moment_id,
        "author_user_id": author_user_id,
        "origin_instance_id": origin_instance_id,
        "rev": rev,
        "counts": dict(counts),
        "total": total,
        ORIGIN_IDENTITY_PK_FIELD: identity_pk.hex(),
        COUNTS_SIG_SUITE_FIELD: COUNTS_SIG_SUITE_ED25519,
    }
    if no_relay:
        payload[NO_RELAY_FIELD] = True
    payload[COUNTS_SIG_FIELD] = b64url_encode(
        sign_ed25519(seed, counts_signing_bytes(payload))
    )
    payload["hop_count"] = 1
    return payload


def verify_counts(*, identity_pk: bytes, payload: dict[str, Any]) -> bool:
    """Whether *payload*'s totals signature verifies under *identity_pk*.

    Raises :class:`UnsupportedCountsSigSuite` for a suite outside
    :data:`SUPPORTED_COUNTS_SIG_SUITES` — never verified under a default.
    Never raises otherwise."""
    suite = payload.get(COUNTS_SIG_SUITE_FIELD)
    if not isinstance(suite, str) or suite not in SUPPORTED_COUNTS_SIG_SUITES:
        raise UnsupportedCountsSigSuite(
            f"moment totals advertise unsupported counts_sig_suite={suite!r}; "
            f"this build supports {sorted(SUPPORTED_COUNTS_SIG_SUITES)!r}"
        )
    try:
        sig = b64url_decode(str(payload.get(COUNTS_SIG_FIELD) or ""))
    except ValueError, TypeError:
        return False
    return verify_ed25519(identity_pk, counts_signing_bytes(payload), sig)


def counts_shape_refusal(payload: object) -> str | None:
    """Why *payload* is not a well-formed ``MOMENT_REACTIONS_COUNTED``
    payload, or ``None``. Exactly :data:`COUNTS_FIELDS` (plus a ``no_relay:
    true``), ids non-empty strings of at most :data:`MAX_COUNTS_ID_CHARS`,
    ``rev`` and ``hop_count`` positive integers, the counts valid
    (:func:`~socialhome.domain.moment.reaction_counts_valid`)."""
    if not isinstance(payload, dict):
        return "not an object"
    keys = set(payload)
    if NO_RELAY_FIELD in keys:
        if payload[NO_RELAY_FIELD] is not True:
            return "malformed no_relay"
        keys.discard(NO_RELAY_FIELD)
    if keys != COUNTS_FIELDS:
        return "unexpected or missing fields"
    for key in (
        "moment_id",
        "author_user_id",
        "origin_instance_id",
        ORIGIN_IDENTITY_PK_FIELD,
        COUNTS_SIG_SUITE_FIELD,
        COUNTS_SIG_FIELD,
    ):
        value = payload[key]
        if not isinstance(value, str) or not 0 < len(value) <= MAX_COUNTS_ID_CHARS:
            return f"malformed {key}"
    for key in ("rev", "hop_count"):
        value = payload[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return f"malformed {key}"
    if not reaction_counts_valid(payload["counts"], payload["total"]):
        return "malformed counts"
    return None


def counts_origin_refusal(
    payload: dict[str, Any], *, pinned_pk: bytes | None
) -> str | None:
    """Why *payload*'s totals don't come from its claimed origin household,
    or ``None`` when they do. *pinned_pk* is the identity key this household
    pins for ``origin_instance_id`` (``None`` when it holds no row for it).

    Always signed — there is no legacy window (v_69 is new). Verified
    against the pinned key, or — with none pinned — against the shipped
    ``origin_identity_pk`` provided it derives to ``origin_instance_id``. A
    shipped key that disagrees with the pinned one, an unknown suite, or a
    signature that doesn't verify is refused. Never raises."""
    try:
        shipped = bytes.fromhex(str(payload.get(ORIGIN_IDENTITY_PK_FIELD) or ""))
    except ValueError:
        return "malformed origin_identity_pk"
    if pinned_pk is not None:
        if shipped != pinned_pk:
            return "origin_identity_pk differs from the pinned key"
    else:
        try:
            derived = derive_instance_id(shipped)
        except ValueError:
            return "origin_identity_pk is not 32 bytes"
        if derived != payload.get("origin_instance_id"):
            return "origin_identity_pk does not derive to the origin"
    try:
        ok = verify_counts(identity_pk=shipped, payload=payload)
    except UnsupportedCountsSigSuite:
        return "unsupported counts_sig_suite"
    return None if ok else "totals signature does not verify"


__all__ = [
    "COUNTS_FIELDS",
    "COUNTS_SIG_FIELD",
    "COUNTS_SIG_SUITE_ED25519",
    "COUNTS_SIG_SUITE_FIELD",
    "MAX_COUNTS_ID_CHARS",
    "MAX_COUNTS_REV_AGE_MS",
    "NO_RELAY_FIELD",
    "ORIGIN_IDENTITY_PK_FIELD",
    "SUPPORTED_COUNTS_SIG_SUITES",
    "UnsupportedCountsSigSuite",
    "counts_origin_refusal",
    "counts_shape_refusal",
    "counts_signing_bytes",
    "sign_counts",
    "verify_counts",
]
