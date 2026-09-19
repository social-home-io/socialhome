"""End-to-end encryption for ``SPACE_ROUTED`` inner payloads.

Mirrors the project's existing pairing-coordinator key-derivation
shape (see :func:`socialhome.federation.auto_pair_coordinator
._derive_session_keys`): X25519 ECDH → HKDF-SHA256 to two
**directional** 32-byte keys, one for each direction of the
exchange. Symmetric AEAD is AES-256-GCM, matching every other
confidentiality surface in the codebase (federation envelopes,
space content, sealed sender). The AAD binds the ciphertext to its
routing context (``route_id`` + ``inner_event_type``) so a ciphertext
intercepted from one ``SPACE_ROUTED`` envelope can't be cut-and-
pasted into another — same idea as the ``space_id`` AAD on space
content (see :mod:`socialhome.services.space_crypto_service`).

## Threat model (recap from ``docs/crypto.md``)

* **Confidentiality** — relays on the discovered path see routing
  metadata (``route_id``, ``path``, ``position``, ``inner_event_type``)
  but **not** ``inner_payload``. They hold neither ephemeral private
  half so they can't recover the shared secret.
* **Authenticity** — the outer ``SPACE_ROUTED`` envelope is signed by
  each hop along the standard §24.11 federation pipeline, so a relay
  swapping the AEAD ciphertext mid-path is caught by the outer
  signature verify on the next hop. The AES-GCM tag is an additional
  belt-and-suspenders against ciphertext mutation by a relay that
  somehow bypasses the §24.11 verify.
* **No forward secrecy beyond the discovery window** — once the
  target's ephemeral private half expires from cache
  (``DEFAULT_TARGET_EPH_TTL_S``, default 300 s), subsequent redeems
  force a fresh discovery + fresh ephemerals. This is deliberate and
  must not be relaxed by refreshing the key on use: the bound is
  "mint + TTL", not "last use + TTL".
  The origin-side route-cache TTL is strictly SHORTER (by
  ``route_discovery.ROUTE_CACHE_SAFETY_MARGIN_S``) so the origin's
  window always closes first. Ordering matters and used to be wrong —
  if the origin can outlive the target, it keeps sealing under a
  private half the target has already dropped, and the target discards
  every envelope in silence (#648).
* **PQ migration** — same shape as pairing's X25519 today; Phase 2
  of ``docs/crypto.md`` swaps in X25519+ML-KEM-768 hybrid here too.
  No wire change required — only the ``derive_directional_keys``
  body grows a parallel ML-KEM half.

## Key exchange flow

1. **Target**, when ``SPACE_FIND_ROUTE`` reaches it as the target:
   generate ephemeral X25519 keypair, ship pub in
   ``SPACE_ROUTE_FOUND``, cache priv keyed on pub (TTL).

2. **Origin**, on ``SPACE_ROUTE_FOUND``: cache the target's eph_pub
   alongside the discovered path. Per outbound ``SPACE_ROUTED``:
   generate fresh origin ephemeral, derive directional keys via
   ``DH(origin_priv, target_pub)`` + HKDF, AES-GCM-seal the inner
   payload using the **origin→target** key with
   ``AAD = route_id||inner_event_type``.

3. **Target**, on ``SPACE_ROUTED`` unwrap: look up cached priv by
   ``target_eph_pk``, derive the same directional keys, decrypt with
   the **origin→target** key.

4. **ACK (target → origin)**: the **target→origin** key is the
   second HKDF output from the same ECDH — separate from the
   origin→target key so a captured forward ciphertext can't be
   replayed as a reply. Different AAD too (``route_id||"ack"``).

## Wire shape (the dict embedded in ``SPACE_ROUTED.payload.sealed``)

```python
{
    "kem_suite":     "x25519",        # algo for the key agreement
    "origin_eph_pk": "<32 b64url>",   # sender's ephemeral pub
    "target_eph_pk": "<32 b64url>",   # target's ephemeral pub (echo)
    "nonce":         "<12 b64url>",   # AES-GCM nonce
    "ciphertext":    "<aead b64url>", # AES-GCM ct||tag
}
```

The ``target_eph_pk`` echo lets the target find its cached private
half AND lets the origin (on the ACK return) prove it derived the
correct shared secret.

``kem_suite`` mirrors the ``sig_suite`` mechanism (see
``docs/crypto.md`` § "The suite contract") so the Phase-2 PQ
migration is a wire-additive change: when ``x25519+mlkem768`` lands
the pubkey fields carry the concatenated X25519 + ML-KEM material
and the seal does a double KEM + HKDF over the combined secret.
Receivers reject ``kem_suite`` values they don't know — the floor
right now is the single value ``x25519``.
"""

from __future__ import annotations

import hashlib
import os
import time

from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..crypto import (
    X25519Keypair,
    b64url_decode,
    b64url_encode,
    generate_x25519_keypair,
    sign_ed25519,
    verify_ed25519,
    x25519_exchange,
)


#: Default TTL on the target's cached ephemeral private half, measured
#: from the moment the key is MINTED (never extended on use — that would
#: trade away the forward-secrecy bound documented above). Must stay
#: strictly greater than the origin's route-cache TTL, which is derived
#: from this value in ``route_discovery.ROUTE_CACHE_TTL_S``.
DEFAULT_TARGET_EPH_TTL_S: float = 300.0

#: KEM suite this build implements. PQ migration (Phase 2 of
#: ``docs/crypto.md``) layers ML-KEM-768 on top of X25519 → the value
#: would become ``"x25519+mlkem768"`` and the wire-format gains a
#: longer concatenated pubkey; same envelope shape otherwise.
KEM_SUITE_X25519: str = "x25519"
SUPPORTED_KEM_SUITES: frozenset[str] = frozenset({KEM_SUITE_X25519})


class UnsupportedKemSuite(ValueError):
    """Raised when an inbound sealed payload advertises a KEM suite
    this build doesn't know. Receivers MUST reject rather than fall
    back to a weaker suite — otherwise a downgrade attack becomes
    possible once Phase-2 hybrid lands."""


#: Signature suite for the ``SPACE_ROUTE_STALE`` nack — the target's
#: Ed25519 identity key signs "this ephemeral pub is dead for route X".
#: Mirrors ``Encoder.sig_suite`` on the envelope; Phase 2 of
#: ``docs/crypto.md`` adds ``"ed25519+mldsa65"`` as a sibling constant
#: with both signatures produced + verified in parallel. Same wire
#: shape either way — the tag is what lets the algorithm swap.
ROUTE_STALE_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_ROUTE_STALE_SIG_SUITES: frozenset[str] = frozenset(
    {ROUTE_STALE_SIG_SUITE_ED25519}
)


class UnsupportedRouteStaleSuite(ValueError):
    """Raised when an inbound ``SPACE_ROUTE_STALE`` advertises a
    signature suite this build doesn't know. Receivers MUST reject
    rather than verify under a default algorithm — otherwise a peer
    stripping the tag downgrades the check once Phase-2 hybrid lands."""


#: Signature suite for the **origin authentication** field set that
#: rides alongside a sealed ``SPACE_ROUTED`` payload (v_31, #692). The
#: household at ``path[0]`` signs the routing claim plus the sealed
#: material with its Ed25519 identity key, so the receiver stops having
#: to take the relay-supplied ``path[0]`` on faith. Same tag vocabulary
#: as :data:`ROUTE_STALE_SIG_SUITE_ED25519` / ``Encoder.sig_suite``;
#: Phase 2 of ``docs/crypto.md`` adds ``"ed25519+mldsa65"`` as a sibling
#: constant with both signatures produced + verified in parallel.
ROUTED_ORIGIN_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_ROUTED_ORIGIN_SIG_SUITES: frozenset[str] = frozenset(
    {ROUTED_ORIGIN_SIG_SUITE_ED25519}
)


class UnsupportedRoutedOriginSuite(ValueError):
    """Raised when a ``SPACE_ROUTED`` origin-authentication field set
    advertises a signature suite this build doesn't know. Receivers MUST
    reject rather than verify under a default algorithm — a peer that
    could strip the tag down to a weaker default would walk straight
    back into #692."""


def _require_known_suite(sealed: dict[str, str]) -> None:
    suite = sealed.get("kem_suite", KEM_SUITE_X25519)
    if suite not in SUPPORTED_KEM_SUITES:
        raise UnsupportedKemSuite(
            f"sealed payload advertises unsupported kem_suite={suite!r}; "
            f"this build supports {sorted(SUPPORTED_KEM_SUITES)!r}",
        )


def generate_ephemeral_keypair() -> tuple[str, str]:
    """Return ``(priv_b64url, pub_b64url)`` — a fresh X25519 keypair."""
    kp: X25519Keypair = generate_x25519_keypair()
    return b64url_encode(kp.private_key), b64url_encode(kp.public_key)


def derive_directional_keys(
    *,
    my_priv_b64: str,
    peer_pub_b64: str,
    is_origin: bool,
) -> tuple[bytes, bytes]:
    """ECDH → HKDF → ``(send_key, recv_key)`` for this side.

    Both sides produce the same X25519 shared secret. HKDF then
    derives two 32-byte directional keys with role-anchored info
    strings — exactly mirrors
    :func:`socialhome.federation.auto_pair_coordinator
    ._derive_session_keys` so the same property holds: each side's
    ``send_key`` equals the peer's ``recv_key``. Without this gating
    both sides would label their keys identically and every envelope
    would decrypt with the wrong key.

    Returns ``(send_key, recv_key)``. Caller uses ``send_key`` for
    outbound and ``recv_key`` for inbound.
    """
    shared = x25519_exchange(
        b64url_decode(my_priv_b64),
        b64url_decode(peer_pub_b64),
    )

    def _derive(info: bytes) -> bytes:
        return HKDF(
            algorithm=_hashes.SHA256(),
            length=32,
            salt=None,
            info=info,
        ).derive(shared)

    key_o_to_t = _derive(b"socialhome/space_routed/origin-to-target")
    key_t_to_o = _derive(b"socialhome/space_routed/target-to-origin")
    if is_origin:
        return key_o_to_t, key_t_to_o
    return key_t_to_o, key_o_to_t


def _aad(route_id: str, inner_event_type: str, *, ack: bool = False) -> bytes:
    """Additional-authenticated-data binding the ciphertext to its
    routing context.

    Mirrors the ``space_id`` AAD pattern from
    :mod:`socialhome.services.space_crypto_service` — a ciphertext
    intercepted from one ``SPACE_ROUTED`` envelope can't be
    cut-and-pasted into another with a different ``route_id`` or a
    different ``inner_event_type`` because the AEAD tag won't verify.
    ``ack=True`` further partitions forward vs. reply on the same
    shared secret.
    """
    suffix = b"|ack" if ack else b""
    return (
        b"socialhome/space_routed/v1|"
        + route_id.encode("utf-8")
        + b"|"
        + inner_event_type.encode("utf-8")
        + suffix
    )


def seal_inner_payload(
    *,
    inner_payload_json: str,
    origin_eph_priv_b64: str,
    origin_eph_pub_b64: str,
    target_eph_pub_b64: str,
    route_id: str,
    inner_event_type: str,
) -> dict[str, str]:
    """Origin → target seal of an inner payload.

    Derives the origin→target directional key from
    ``DH(origin_priv, target_pub)`` + HKDF and AES-GCM-encrypts with
    AAD bound to ``(route_id, inner_event_type)``. Returns the
    wire-shape dict the :class:`SpaceRoutedHandler` embeds inside
    its envelope.
    """
    send_key, _recv = derive_directional_keys(
        my_priv_b64=origin_eph_priv_b64,
        peer_pub_b64=target_eph_pub_b64,
        is_origin=True,
    )
    nonce = _fresh_nonce()
    aesgcm = AESGCM(send_key)
    ct = aesgcm.encrypt(
        nonce,
        inner_payload_json.encode("utf-8"),
        _aad(route_id, inner_event_type),
    )
    return {
        "kem_suite": KEM_SUITE_X25519,
        "origin_eph_pk": origin_eph_pub_b64,
        "target_eph_pk": target_eph_pub_b64,
        "nonce": b64url_encode(nonce),
        "ciphertext": b64url_encode(ct),
    }


def unseal_inner_payload(
    *,
    sealed: dict[str, str],
    target_eph_priv_b64: str,
    route_id: str,
    inner_event_type: str,
) -> str:
    """Target → origin unseal.

    Caller looks up its cached ephemeral private half via
    ``sealed["target_eph_pk"]`` and passes it in. Derives the
    matching origin→target key (this side is the target, so it's
    its ``recv_key``) and AES-GCM-decrypts with the same AAD.

    Raises :class:`ValueError` on wire-format problems and
    :class:`cryptography.exceptions.InvalidTag` on a failed AEAD tag.
    """
    _require_known_suite(sealed)
    try:
        origin_pub = sealed["origin_eph_pk"]
        nonce_b64 = sealed["nonce"]
        ct_b64 = sealed["ciphertext"]
    except KeyError as exc:
        raise ValueError(f"sealed payload missing field: {exc}") from exc
    # We're the target → origin→target key is our recv direction.
    _send, recv_key = derive_directional_keys(
        my_priv_b64=target_eph_priv_b64,
        peer_pub_b64=origin_pub,
        is_origin=False,
    )
    aesgcm = AESGCM(recv_key)
    nonce = b64url_decode(nonce_b64)
    ct = b64url_decode(ct_b64)
    return aesgcm.decrypt(
        nonce,
        ct,
        _aad(route_id, inner_event_type),
    ).decode("utf-8")


def seal_reply_payload(
    *,
    inner_payload_json: str,
    target_eph_priv_b64: str,
    target_eph_pub_b64: str,
    origin_eph_pub_b64: str,
    route_id: str,
    inner_event_type: str,
) -> dict[str, str]:
    """Target → origin seal for the ACK leg.

    Reuses the same ECDH shared secret as the forward direction but
    pulls the **target→origin** directional key (the second HKDF
    output) and tags the AAD with ``ack`` so a captured forward
    ciphertext can't be replayed as a reply.
    """
    _send, _recv = derive_directional_keys(
        my_priv_b64=target_eph_priv_b64,
        peer_pub_b64=origin_eph_pub_b64,
        is_origin=False,
    )
    # The target's *send* direction is target→origin.
    send_key = _send
    nonce = _fresh_nonce()
    aesgcm = AESGCM(send_key)
    ct = aesgcm.encrypt(
        nonce,
        inner_payload_json.encode("utf-8"),
        _aad(route_id, inner_event_type, ack=True),
    )
    return {
        "kem_suite": KEM_SUITE_X25519,
        "origin_eph_pk": origin_eph_pub_b64,
        "target_eph_pk": target_eph_pub_b64,
        "nonce": b64url_encode(nonce),
        "ciphertext": b64url_encode(ct),
    }


def unseal_reply_payload(
    *,
    sealed: dict[str, str],
    origin_eph_priv_b64: str,
    route_id: str,
    inner_event_type: str,
) -> str:
    """Origin-side unseal of the ACK leg.

    Origin caches its ephemeral private half keyed on ``route_id``
    during ``send_routed``; when the ACK arrives via
    ``SPACE_ROUTED`` along the reverse path it looks up that priv
    and recovers the target→origin AEAD key.
    """
    _require_known_suite(sealed)
    try:
        target_pub = sealed["target_eph_pk"]
        nonce_b64 = sealed["nonce"]
        ct_b64 = sealed["ciphertext"]
    except KeyError as exc:
        raise ValueError(f"sealed payload missing field: {exc}") from exc
    # We're the origin → target→origin key is our recv direction.
    _send, recv_key = derive_directional_keys(
        my_priv_b64=origin_eph_priv_b64,
        peer_pub_b64=target_pub,
        is_origin=True,
    )
    aesgcm = AESGCM(recv_key)
    nonce = b64url_decode(nonce_b64)
    ct = b64url_decode(ct_b64)
    return aesgcm.decrypt(
        nonce,
        ct,
        _aad(route_id, inner_event_type, ack=True),
    ).decode("utf-8")


def route_stale_signing_bytes(route_id: str, stale_eph_pk_b64: str) -> bytes:
    """Canonical bytes the target signs to declare its ephemeral X25519
    pub DEAD for THIS route.

    Domain-separated (``space-route-stale:v1:``) and route-scoped so a
    signature can't be lifted onto another ``route_id`` (replay) and
    can't collide with any other Ed25519 signature this identity
    produces — in particular NOT with the ``space-route-found:v1:``
    bytes in :mod:`socialhome.federation.route_discovery`, which bind
    the SAME pub as *live*. Without the distinct tag a captured
    ROUTE_FOUND signature over ``(id, pub)`` would verify as a
    route-stale nack and let a relay tear down a healthy route.
    """
    return (
        b"space-route-stale:v1:" + route_id.encode() + b":" + stale_eph_pk_b64.encode()
    )


def sign_route_stale(*, seed: bytes, route_id: str, stale_eph_pk_b64: str) -> str:
    """Target-side: sign :func:`route_stale_signing_bytes` with the
    instance identity seed. Returns the b64url-encoded signature."""
    sig = sign_ed25519(seed, route_stale_signing_bytes(route_id, stale_eph_pk_b64))
    return b64url_encode(sig)


def verify_route_stale(
    *,
    identity_pk: bytes,
    route_id: str,
    stale_eph_pk_b64: str,
    sig_b64: str,
    sig_suite: str,
) -> bool:
    """Origin-side: verify a ``SPACE_ROUTE_STALE`` nack signature.

    Raises :class:`UnsupportedRouteStaleSuite` for any ``sig_suite``
    outside :data:`SUPPORTED_ROUTE_STALE_SIG_SUITES` — never falls back
    to a default algorithm. Returns ``False`` (never raises) for a
    malformed, wrong-length, or non-verifying signature, matching
    :func:`socialhome.crypto.verify_ed25519`'s posture.
    """
    if sig_suite not in SUPPORTED_ROUTE_STALE_SIG_SUITES:
        raise UnsupportedRouteStaleSuite(
            f"route-stale nack advertises unsupported sig_suite={sig_suite!r}; "
            f"this build supports {sorted(SUPPORTED_ROUTE_STALE_SIG_SUITES)!r}",
        )
    try:
        sig = b64url_decode(sig_b64)
    except ValueError, TypeError:
        return False
    return verify_ed25519(
        identity_pk,
        route_stale_signing_bytes(route_id, stale_eph_pk_b64),
        sig,
    )


def routed_origin_signing_bytes(
    *,
    route_id: str,
    direction: str,
    path: list[str],
    inner_event_type: str,
    sealed: dict[str, str],
) -> bytes:
    """Canonical bytes the household at ``path[0]`` signs over a
    ``SPACE_ROUTED`` envelope it authored (v_31, #692).

    Binds the whole routing claim — which leg, which route_id, which
    source-route (and therefore which origin AND which target), which
    inner event type — to the sealed material that carries the content.
    A relay that re-seals the payload under its own ephemeral (the only
    thing an anonymous X25519 seal lets it do) changes the material
    digest, so the origin's signature no longer verifies; a relay that
    lifts a genuine signature onto a different route, leg, or target
    changes the prefix, so it doesn't verify either.

    The digest covers **public** bytes only — the relay already sees
    every field it hashes. That is deliberate: signing the *plaintext*
    (or its digest) would hand a relay holding a guess at a low-entropy
    inner payload a verification oracle to confirm it with, which the
    §25.8.21 encryption-first rule rules out. Binding the ciphertext is
    equivalent for authenticity: the AEAD tag already binds ciphertext
    to plaintext under a key the forger doesn't share with the target.

    Domain-separated (``space-routed-origin:v1:``) so a signature can
    never be confused with ``space-route-found:v1:`` (which binds an
    ephemeral pub as live) or ``space-route-stale:v1:`` (which binds the
    same pub as dead).
    """
    material = "|".join(
        [
            str(sealed.get("kem_suite") or ""),
            str(sealed.get("origin_eph_pk") or ""),
            str(sealed.get("target_eph_pk") or ""),
            str(sealed.get("nonce") or ""),
            str(sealed.get("ciphertext") or ""),
        ]
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    return (
        b"space-routed-origin:v1:"
        + direction.encode("utf-8")
        + b":"
        + route_id.encode("utf-8")
        + b":"
        + "|".join(path).encode("utf-8")
        + b":"
        + inner_event_type.encode("utf-8")
        + b":"
        + digest.encode("ascii")
    )


def sign_routed_origin(
    *,
    seed: bytes,
    route_id: str,
    direction: str,
    path: list[str],
    inner_event_type: str,
    sealed: dict[str, str],
) -> str:
    """Author-side: sign :func:`routed_origin_signing_bytes` with the
    instance identity seed. Returns the b64url-encoded signature."""
    sig = sign_ed25519(
        seed,
        routed_origin_signing_bytes(
            route_id=route_id,
            direction=direction,
            path=path,
            inner_event_type=inner_event_type,
            sealed=sealed,
        ),
    )
    return b64url_encode(sig)


def verify_routed_origin(
    *,
    identity_pk: bytes,
    route_id: str,
    direction: str,
    path: list[str],
    inner_event_type: str,
    sealed: dict[str, str],
    sig_b64: str,
    sig_suite: str,
) -> bool:
    """Receiver-side: verify the origin-authentication signature.

    Raises :class:`UnsupportedRoutedOriginSuite` for any ``sig_suite``
    outside :data:`SUPPORTED_ROUTED_ORIGIN_SIG_SUITES` — never falls back
    to a default algorithm. Returns ``False`` (never raises) for a
    malformed, wrong-length, or non-verifying signature, matching
    :func:`socialhome.crypto.verify_ed25519`'s posture.
    """
    if sig_suite not in SUPPORTED_ROUTED_ORIGIN_SIG_SUITES:
        raise UnsupportedRoutedOriginSuite(
            f"SPACE_ROUTED origin auth advertises unsupported "
            f"sig_suite={sig_suite!r}; this build supports "
            f"{sorted(SUPPORTED_ROUTED_ORIGIN_SIG_SUITES)!r}",
        )
    try:
        sig = b64url_decode(sig_b64)
    except ValueError, TypeError:
        return False
    return verify_ed25519(
        identity_pk,
        routed_origin_signing_bytes(
            route_id=route_id,
            direction=direction,
            path=path,
            inner_event_type=inner_event_type,
            sealed=sealed,
        ),
        sig,
    )


def _fresh_nonce() -> bytes:
    """12-byte AES-GCM nonce drawn from the OS CSPRNG."""
    return os.urandom(12)


def expired(stored_at: float, ttl_s: float) -> bool:
    """Whether ``stored_at`` (monotonic seconds) is past its TTL."""
    return (time.monotonic() - stored_at) > ttl_s


__all__ = [
    "DEFAULT_TARGET_EPH_TTL_S",
    "ROUTED_ORIGIN_SIG_SUITE_ED25519",
    "ROUTE_STALE_SIG_SUITE_ED25519",
    "SUPPORTED_ROUTED_ORIGIN_SIG_SUITES",
    "SUPPORTED_ROUTE_STALE_SIG_SUITES",
    "UnsupportedRouteStaleSuite",
    "UnsupportedRoutedOriginSuite",
    "derive_directional_keys",
    "expired",
    "generate_ephemeral_keypair",
    "route_stale_signing_bytes",
    "routed_origin_signing_bytes",
    "seal_inner_payload",
    "seal_reply_payload",
    "sign_route_stale",
    "sign_routed_origin",
    "unseal_inner_payload",
    "unseal_reply_payload",
    "verify_route_stale",
    "verify_routed_origin",
]
# ``X25519PrivateKey`` re-imported only so test fixtures that monkey-
# patch this module's namespace (rare) can pull it through the public
# API surface; remove if the test rewrite lands without using it.
_ = X25519PrivateKey
