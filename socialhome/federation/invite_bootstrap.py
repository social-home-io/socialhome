"""Bootstrap redeem of a space invite link between strangers (§D2b).

The §D2 cross-household redeem (:mod:`socialhome.federation
.invite_token_redeem`) needs the issuing household to be a CONFIRMED
federation peer, or to sit at the end of a mesh route of confirmed
peers. Someone who receives an invite *link* from a household they have
never federated with satisfies neither, and today gets "no route to
issuer — pair with them, or with one of their household's peers".

**Possession of a valid, unexpired, unexhausted invite token IS the
authorization** — the space owner minted it deliberately. This module
carries the household-to-household half of redeeming one with no
pre-existing relationship.

## Transport: an opaque blob through the connection server

The redeeming household never learns the issuer's network address, and
the issuer never learns the redeemer's: the invite blob published on the
connection server (GFS) is a PUBLIC page, so it must not carry an
``inbox_url``. Instead:

1. The redeemer **seals** the redeem request to the issuer's published
   key-wrap public key and hands the ciphertext to the GFS addressed to
   ``issuer_instance_id``.
2. The GFS pushes it to that household over its existing socket.
3. The issuer opens it, validates, and replies through the same relay,
   sealed to the redeemer's key-wrap key (which rode *inside* the
   request).

The routing envelope is **identity-free** (the #677 lesson): it names
only the recipient. The sender's identity, the token, the space, the
users, the nonce and the signature all live inside the ciphertext, so
the GFS sees a recipient id, a blob size and a timing — nothing else.

This module owns the household side only: the sealing/unsealing, the
signature discipline, and the fail-closed validation. Carrying the blob
is somebody else's job, behind :class:`RelayEnvelopeSender` —
in production :class:`socialhome.services.gfs_envelope_sender
.GfsEnvelopeSender` (``POST {gfs}/gfs/envelope`` out, the ``envelope``
frame on ``/gfs/ws`` back in).

## Why ``keywrap_seal`` and not ``routed_crypto``

:mod:`socialhome.federation.routed_crypto` negotiates a *target
ephemeral* X25519 key via an online ``SPACE_FIND_ROUTE`` round-trip —
there is no such round-trip here, and no mesh path to run it over.
:mod:`socialhome.federation.keywrap_seal` is the static-recipient
sealed box built for exactly this case: "seal to a household that is not
a paired peer, using a key learned from the GFS", complete with
:func:`~socialhome.federation.keywrap_seal.verify_keywrap_binding` to
defeat a GFS key substitution. An Ed25519 identity key cannot be used
for ECDH directly (and this codebase has no Ed25519→X25519 conversion),
so the invite blob carries the issuer's key-wrap key **bound to** the
identity key by a self-signature; the binding is verified before we ever
seal.

## Wire shapes

Outer (what the relay sees)::

    {"to_instance": "<32 lowercase base32 chars>",
     "sealed": {kem_suite, eph_pk, ciphertext}}

An instance id is what :func:`socialhome.crypto.derive_instance_id`
produces: unpadded lowercase base32 of the first 20 bytes of the
SHA-256 of the household's Ed25519 identity key — 32 characters from
``[a-z2-7]``, not hex.

Inner (the sealed plaintext — JSON, Ed25519-signed over its canonical
bytes by the sender's *identity* key, TOFU-verified by the receiver)::

    {
      "kind":        "space_invite_bootstrap_redeem",
      "sig_suite":   "ed25519",
      "invite_token": "...", "space_id": "...",
      "redeem_nonce": "<hex>", "ts": "<tz-aware ISO-8601>",
      "instance_id": "...", "identity_pk": "<hex>",
      "keywrap_pk":  "<hex>", "keywrap_sig": "<b64url>",
      "display_name": "...",
      "redeemer_user_id": "...", "redeemer_public_key": "...",
      "redeemer_display_name": "...",
      "signature":   "<hex>"
    }

The reply (``..._ack`` / ``..._deny``) mirrors it, with the issuer's
identity + key-wrap material and either ``{space_id, role, space_meta}``
or ``{reason}``.

Every cryptographic wire shape here is suite-tagged:
``sig_suite`` on the signed inner body (validated against
:data:`SUPPORTED_BOOTSTRAP_SIG_SUITES`, no default-on-missing) and
``kem_suite`` on the sealed box (owned by ``keywrap_seal``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import orjson
from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..crypto import (
    derive_instance_id,
    sign_ed25519,
    verify_ed25519,
    x25519_exchange,
)
from ..domain.federation import FederationEventType
from ..domain.space import SpacePermissionError
from ..utils.datetime import parse_iso8601_strict
from .keywrap_seal import (
    UnsupportedKemSuite,
    open_keywrap,
    seal_to_keywrap,
    verify_keywrap_binding,
)

log = logging.getLogger(__name__)


#: Signature suite this build produces + accepts on a bootstrap body.
#: Phase-2 (``docs/crypto.md``) adds ``"ed25519+mldsa65"`` as a sibling
#: constant here and in :data:`SUPPORTED_BOOTSTRAP_SIG_SUITES`; the wire
#: shape is unchanged, the body just grows the parallel signature.
BOOTSTRAP_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_BOOTSTRAP_SIG_SUITES: frozenset[str] = frozenset(
    {BOOTSTRAP_SIG_SUITE_ED25519}
)


class UnsupportedBootstrapSigSuite(ValueError):
    """Raised when a bootstrap body advertises (or omits) a signature
    suite this build doesn't know. Receivers MUST reject rather than
    fall back — otherwise the Phase-2 hybrid suite shipping alongside
    this one opens a downgrade."""


#: Hard cap on a sealed bootstrap blob. The request is a few hundred
#: bytes; the ACK carries ``space_meta`` (cover + icon bytes + roster),
#: which the §D1b snapshot already bounds well under this. Checked
#: BEFORE the unseal so a flood costs us a length compare, not an AEAD.
MAX_SEALED_BLOB_BYTES: int = 256 * 1024

#: Hard cap on the unsealed plaintext, for the same reason one hop in.
MAX_INNER_BYTES: int = 256 * 1024

#: §24.11 timestamp window, identical to the regular inbound pipeline.
TIMESTAMP_SKEW_SECONDS: int = 300

#: ``kind`` discriminators on the inner body. Taken from the event-type
#: enum so the wire strings live in exactly one place, even though these
#: bodies never ride a §24.11 envelope.
KIND_REDEEM: str = FederationEventType.SPACE_INVITE_BOOTSTRAP_REDEEM.value
KIND_REDEEM_ACK: str = FederationEventType.SPACE_INVITE_BOOTSTRAP_REDEEM_ACK.value
KIND_REDEEM_DENY: str = FederationEventType.SPACE_INVITE_BOOTSTRAP_REDEEM_DENY.value

_KNOWN_KINDS: frozenset[str] = frozenset(
    {KIND_REDEEM, KIND_REDEEM_ACK, KIND_REDEEM_DENY}
)

#: HKDF domain-separation for the space-scoped session keys derived from
#: the two households' static key-wrap keys. Distinct from every other
#: info string in the codebase so a key derived here can never collide
#: with a pairing session key or a key-wrap key.
_SESSION_INFO_REDEEMER_TO_ISSUER: bytes = b"socialhome/space_session/redeemer-to-issuer"
_SESSION_INFO_ISSUER_TO_REDEEMER: bytes = b"socialhome/space_session/issuer-to-redeemer"


#: How long a household waits out a relay's ``429`` before retrying the
#: same envelope. The relay's window is a minute wide
#: (:data:`~socialhome.global_server.envelope_relay.ENVELOPE_MAX_PER_MINUTE`)
#: and it is a *sliding* window, so capacity returns continuously rather
#: than at a tick — a few seconds is enough to get back under the line,
#: and short enough that a sync stream waiting one out is not mistaken for
#: a hung one.
RELAY_THROTTLE_COOLDOWN_S: float = 5.0


class EnvelopeRelayThrottled(SpacePermissionError):
    """The connection server answered ``429`` — come back shortly.

    The one *temporary* refusal a :class:`RelayEnvelopeSender` raises
    rather than reporting as a plain ``False``. A throttle says nothing
    about the recipient: the relay is up, the blob is well-formed, and
    the same send will work in a few seconds. Callers that can wait
    (:class:`~socialhome.federation.gfs_relay_transport.GfsRelayTransport`
    and, above it, the space-sync provider) treat it as a cooldown and
    retry the same frame; callers that cannot surface it to the user,
    which is why it subclasses
    :class:`~socialhome.domain.space.SpacePermissionError` and reaches
    ``POST /api/spaces/join`` as a 422 with a sentence worth reading
    instead of an opaque "could not deliver".
    """


class RelayEnvelopeSender(Protocol):
    """Seam between this module and whatever carries the opaque blob.

    Production wires the GFS connection (a separate task): the envelope
    goes out over the household's existing ``/gfs/ws`` socket addressed
    to ``to_instance_id``. Tests wire an in-process fake pair.

    ``gfs_url`` names the connection server to hand the blob to: the
    redeemer passes the base URL the invite blob was served from
    (:attr:`InviteBootstrapHint.gfs_url`), and the issuer passes the one
    the request arrived on, so a household paired with several servers
    answers on the one that carried the request. Empty means "any relay
    this household can use".

    Returns ``True`` when the relay accepted the blob for delivery.
    A transport failure is ``False``, never an exception. The one
    exception implementations may raise is a *configuration* refusal —
    no reachable relay, or one that cannot carry invite envelopes —
    which the redeem surfaces to the user as a named reason rather than
    a ten-second timeout.
    """

    async def send_sealed_envelope(
        self,
        *,
        to_instance_id: str,
        envelope: dict[str, Any],
        gfs_url: str = "",
    ) -> bool: ...


@dataclass(slots=True, frozen=True)
class InviteBootstrapHint:
    """What the redeeming household learned from the public invite blob.

    Served by the connection server to anyone who opens the invite link,
    so it deliberately carries **no network address** — only the
    issuer's public keys, which are useless for locating the household.

    ``keywrap_pk`` / ``keywrap_sig`` are the issuer's static X25519
    key-wrap key and its self-signature under ``identity_pk``; they are
    bound to ``instance_id`` before any seal (see
    :func:`~socialhome.federation.keywrap_seal.verify_keywrap_binding`),
    so a malicious connection server cannot substitute a key it holds.

    ``gfs_url`` is the base URL of the connection server that served
    the blob. The blob is minted per connection server (the issuer
    publishes it there), so it is also the one relay known to reach the
    issuer — the redeemer hands its sealed request to exactly that
    server rather than guessing among its own pairings.
    """

    invite_token: str
    space_id: str
    instance_id: str
    identity_pk: str
    keywrap_pk: str
    keywrap_sig: str
    proto_version: int = 1
    display_hint: str = ""
    expires_at: str | None = None
    gfs_url: str = ""


def canonical_signing_bytes(body: dict[str, Any]) -> bytes:
    """Canonical bytes of ``body`` (minus ``signature``) for Ed25519.

    Same discipline as the §11 pairing bodies
    (:func:`socialhome.federation.peer_pairing_client
    ._canonical_body_bytes`): sorted keys so both sides digest the same
    bytes regardless of dict ordering, and ``kind`` is inside the signed
    view so the signature covers the discriminator too.
    """
    signed_view = {k: v for k, v in body.items() if k != "signature"}
    return orjson.dumps(signed_view, option=orjson.OPT_SORT_KEYS)


def sign_bootstrap_body(body: dict[str, Any], *, identity_seed: bytes) -> dict:
    """Return ``body`` with ``sig_suite`` + a hex Ed25519 ``signature``."""
    signed = {**body, "sig_suite": BOOTSTRAP_SIG_SUITE_ED25519}
    signature = sign_ed25519(identity_seed, canonical_signing_bytes(signed))
    return {**signed, "signature": signature.hex()}


def seal_bootstrap_envelope(
    *,
    body: dict[str, Any],
    identity_seed: bytes,
    recipient_instance_id: str,
    recipient_identity_pk: str,
    recipient_keywrap_pk: str,
    recipient_keywrap_sig: str,
) -> dict[str, Any]:
    """Sign ``body``, seal it to the recipient, return the outer envelope.

    Runs the anti-substitution gate first: the recipient's key-wrap key
    must be bound to ``recipient_identity_pk``, which must in turn derive
    ``recipient_instance_id``. A connection server that swapped in a
    key-wrap key it controls fails here and we refuse to seal.

    Raises :class:`ValueError` on malformed key material or a failed
    binding — the caller surfaces it as a redeem failure rather than
    leaking a request to whoever holds the substituted key.
    """
    try:
        identity_pub = bytes.fromhex(recipient_identity_pk)
        keywrap_pub = bytes.fromhex(recipient_keywrap_pk)
    except ValueError as exc:
        raise ValueError(f"malformed recipient key material: {exc}") from exc
    if not verify_keywrap_binding(
        instance_id=recipient_instance_id,
        identity_pub=identity_pub,
        keywrap_pub=keywrap_pub,
        keywrap_sig=recipient_keywrap_sig,
    ):
        raise ValueError(
            "invite bootstrap: recipient key-wrap key is not bound to the "
            "advertised identity — refusing to seal (possible relay "
            "substitution)",
        )
    signed = sign_bootstrap_body(body, identity_seed=identity_seed)
    sealed = seal_to_keywrap(
        recipient_keywrap_pub=keywrap_pub,
        plaintext=json.dumps(signed).encode("utf-8"),
    )
    # IDENTITY-FREE OUTER SHAPE: only the recipient is named, and only
    # because the relay has to route on something. The sender's
    # instance_id lives inside the ciphertext.
    return {"to_instance": recipient_instance_id, "sealed": sealed}


def unseal_envelope_body(
    *,
    envelope: dict[str, Any],
    keywrap_private_key: bytes,
) -> dict[str, Any]:
    """Caps + unseal + JSON-parse one relayed blob → the inner body.

    The first three fail-closed steps of
    :func:`open_bootstrap_envelope`, split out because the relay leg
    carries two unrelated families through one socket: bootstrap bodies
    (validated by :func:`validate_bootstrap_body`) and full §24.11
    envelopes (:mod:`socialhome.federation.gfs_relay_transport`, whose
    own signature/timestamp/replay checks are the §24.11 pipeline's).
    The receiver dispatches on the body's ``kind`` marker, so it must be
    able to read that marker before choosing a validator — without
    unsealing the same blob twice.

    Raises :class:`ValueError` (or
    :class:`~socialhome.federation.keywrap_seal.UnsupportedKemSuite`) on
    any malformed or unopenable input.
    """
    sealed = envelope.get("sealed") if isinstance(envelope, dict) else None
    if not isinstance(sealed, dict):
        raise ValueError("bootstrap envelope missing sealed payload")
    ciphertext = sealed.get("ciphertext")
    if not isinstance(ciphertext, str) or not ciphertext:
        raise ValueError("bootstrap envelope missing ciphertext")
    if len(ciphertext) > MAX_SEALED_BLOB_BYTES:
        raise ValueError("bootstrap envelope too large")

    try:
        plaintext = open_keywrap(
            sealed=sealed,
            recipient_keywrap_priv=keywrap_private_key,
        )
    except UnsupportedKemSuite:
        raise
    except Exception as exc:  # InvalidTag, ValueError, …
        raise ValueError(f"bootstrap envelope does not open: {exc}") from exc

    if len(plaintext) > MAX_INNER_BYTES:
        raise ValueError("bootstrap body too large")
    try:
        body = json.loads(plaintext)
    except Exception as exc:
        raise ValueError(f"bootstrap body is not JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("bootstrap body is not an object")
    return body


def open_bootstrap_envelope(
    *,
    envelope: dict[str, Any],
    keywrap_private_key: bytes,
    expected_kinds: frozenset[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Unseal + fully validate one inbound bootstrap envelope.

    Fail-closed, cheapest check first:

    1. **Size / shape caps** on the sealed blob — before any AEAD.
    2. **Unseal** with our key-wrap private key (an unknown ``kem_suite``
       is rejected outright, never defaulted).
    3. **Shape caps** on the plaintext, JSON parse, known ``kind``.
    4. **Signature suite** against
       :data:`SUPPORTED_BOOTSTRAP_SIG_SUITES`.
    5. **Anti-tamper identity check** — §4.1.2:
       ``derive_instance_id(identity_pk)`` must equal the claimed
       ``instance_id``, so a sender cannot keep a victim's display name
       while substituting its own keypair.
    6. **Signature** over the canonical bytes, verified against the
       body's own ``identity_pk`` (TOFU — the token is the
       authorization, the key just has to be self-consistent).
    7. **Timestamp** — tz-aware and within ±
       :data:`TIMESTAMP_SKEW_SECONDS`.

    The **replay guard on ``redeem_nonce`` is the caller's job** — it
    lives on the shared :class:`socialhome.crypto.ReplayCache` the
    §24.11 pipeline already uses, which this pure module has no handle
    on.

    Returns the validated inner body. Raises :class:`ValueError` (or its
    subclasses) on every rejection.
    """
    body = unseal_envelope_body(
        envelope=envelope,
        keywrap_private_key=keywrap_private_key,
    )
    return validate_bootstrap_body(body, expected_kinds=expected_kinds, now=now)


def validate_bootstrap_body(
    body: dict[str, Any],
    *,
    expected_kinds: frozenset[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate an already-unsealed bootstrap body (steps 3-7 above)."""
    kind = body.get("kind")
    if not isinstance(kind, str) or kind not in _KNOWN_KINDS:
        raise ValueError(f"unknown bootstrap kind {kind!r}")
    if kind not in expected_kinds:
        raise ValueError(f"unexpected bootstrap kind {kind!r} on this leg")

    suite = body.get("sig_suite")
    if suite not in SUPPORTED_BOOTSTRAP_SIG_SUITES:
        raise UnsupportedBootstrapSigSuite(
            f"bootstrap body advertises unsupported sig_suite={suite!r}; "
            f"this build supports {sorted(SUPPORTED_BOOTSTRAP_SIG_SUITES)!r}",
        )

    instance_id = str(body.get("instance_id") or "")
    identity_pk = str(body.get("identity_pk") or "")
    signature = str(body.get("signature") or "")
    if not instance_id or not identity_pk or not signature:
        raise ValueError("bootstrap body missing identity / signature fields")
    try:
        identity_pub = bytes.fromhex(identity_pk)
        signature_bytes = bytes.fromhex(signature)
    except ValueError as exc:
        raise ValueError(f"malformed bootstrap identity / signature: {exc}") from exc

    # §4.1.2 anti-tamper — the claimed instance_id must be the one the
    # advertised key derives. Without this a sender could keep the
    # victim's instance_id (which the receiver keys its rows on) while
    # signing with its own keypair.
    if derive_instance_id(identity_pub) != instance_id:
        raise ValueError(
            "bootstrap instance_id does not match identity_pk",
        )

    if not verify_ed25519(
        identity_pub,
        canonical_signing_bytes(body),
        signature_bytes,
    ):
        raise ValueError("bootstrap signature verification failed")

    ts_raw = body.get("ts")
    if not isinstance(ts_raw, str) or not ts_raw:
        raise ValueError("bootstrap body missing ts")
    try:
        ts = parse_iso8601_strict(ts_raw)
    except ValueError as exc:
        raise ValueError(f"unparseable bootstrap ts: {exc}") from exc
    if ts.tzinfo is None:
        raise ValueError("bootstrap ts is not timezone-aware")
    current = now if now is not None else datetime.now(timezone.utc)
    if abs((current - ts).total_seconds()) > TIMESTAMP_SKEW_SECONDS:
        raise ValueError("bootstrap ts skew too large")

    return body


def derive_space_session_keys(
    *,
    own_keywrap_priv: bytes,
    peer_keywrap_pub: bytes,
    is_redeemer: bool,
) -> tuple[bytes, bytes]:
    """Static-static ECDH → ``(key_self_to_remote, key_remote_to_self)``.

    Both households already publish a long-lived X25519 key-wrap key
    bound to their identity key, and both verified the other's binding
    before exchanging anything — so the pair can derive matching
    directional AES-256-GCM session keys with no extra round-trip. Same
    HKDF + role-anchoring shape as
    :func:`socialhome.federation.routed_crypto.derive_directional_keys`
    and the pairing coordinator's ``_derive_directional_keys``: each
    side's send key is the other's receive key.

    No forward secrecy (the inputs are static). That is the same
    property a confirmed pairing's session keys have once derived, and
    the space *content* is protected by the per-space content key with
    its own epoch rotation, not by this layer.
    """
    shared = x25519_exchange(own_keywrap_priv, peer_keywrap_pub)

    def _derive(info: bytes) -> bytes:
        return HKDF(
            algorithm=_hashes.SHA256(),
            length=32,
            salt=None,
            info=info,
        ).derive(shared)

    key_r_to_i = _derive(_SESSION_INFO_REDEEMER_TO_ISSUER)
    key_i_to_r = _derive(_SESSION_INFO_ISSUER_TO_REDEEMER)
    if is_redeemer:
        return key_r_to_i, key_i_to_r
    return key_i_to_r, key_r_to_i


def verify_peer_keywrap(
    *,
    instance_id: str,
    identity_pk: str,
    keywrap_pk: str,
    keywrap_sig: str,
) -> bytes | None:
    """Return the peer's key-wrap pub bytes iff bound to its identity.

    Thin fail-closed wrapper over
    :func:`~socialhome.federation.keywrap_seal.verify_keywrap_binding`
    for the inbound legs, which learn the peer's key-wrap key from
    inside a sealed body rather than from the invite blob. ``None`` on
    any malformed input or failed binding.
    """
    try:
        identity_pub = bytes.fromhex(identity_pk)
        keywrap_pub = bytes.fromhex(keywrap_pk)
    except ValueError:
        return None
    if not verify_keywrap_binding(
        instance_id=instance_id,
        identity_pub=identity_pub,
        keywrap_pub=keywrap_pub,
        keywrap_sig=keywrap_sig,
    ):
        return None
    return keywrap_pub


__all__ = [
    "BOOTSTRAP_SIG_SUITE_ED25519",
    "SUPPORTED_BOOTSTRAP_SIG_SUITES",
    "UnsupportedBootstrapSigSuite",
    "MAX_SEALED_BLOB_BYTES",
    "MAX_INNER_BYTES",
    "TIMESTAMP_SKEW_SECONDS",
    "KIND_REDEEM",
    "KIND_REDEEM_ACK",
    "KIND_REDEEM_DENY",
    "EnvelopeRelayThrottled",
    "InviteBootstrapHint",
    "RELAY_THROTTLE_COOLDOWN_S",
    "RelayEnvelopeSender",
    "canonical_signing_bytes",
    "sign_bootstrap_body",
    "seal_bootstrap_envelope",
    "open_bootstrap_envelope",
    "unseal_envelope_body",
    "validate_bootstrap_body",
    "derive_space_session_keys",
    "verify_peer_keywrap",
]
