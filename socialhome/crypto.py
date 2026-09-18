"""Cryptographic primitives for Social Home.

Covers:

* Deterministic derivation of ``instance_id`` / ``user_id`` from Ed25519 public
  keys (§4.1.2 / §4.1.3).
* Ed25519 keypair generation / signing / verification helpers.
* X25519 (ECDH) helpers used for pairing and per-space key exchange.
* A signed ``UserIdentityAssertion`` encoder / verifier (§4.1.4).
* An in-memory replay-protection cache used by the federation service.

Anything that needs a ``secrets`` or ``os.urandom`` source of randomness lives
here so it is easy to audit. No network or database I/O.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives import serialization

from .domain.move_link import MoveLink
from .domain.user import UserIdentityAssertion
from .utils.datetime import parse_iso8601_strict


# ─── Base helpers ──────────────────────────────────────────────────────────


def b64url_encode(data: bytes) -> str:
    """URL-safe base64 without trailing ``=`` padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Inverse of :func:`b64url_encode`. Tolerates missing ``=`` padding."""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


# ─── Identifier derivation (§4.1.2 / §4.1.3) ──────────────────────────────


def derive_instance_id(identity_public_key_bytes: bytes) -> str:
    """Derive a stable, verifiable instance identifier from an Ed25519 public key.

    The ID is the lowercase base32-encoded SHA-256 of the key, truncated to the
    first 20 bytes (160 bits of collision resistance) and stripped of the
    base32 padding. That produces a 32-character identifier, e.g.
    ``qbfdx7k2n3p6r8t1v4w9y0zh``.

    See §4.1.2 of the spec.
    """
    if len(identity_public_key_bytes) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    digest = hashlib.sha256(identity_public_key_bytes).digest()
    return base64.b32encode(digest[:20]).decode("ascii").lower().rstrip("=")


def derive_space_id(space_public_key_bytes: bytes) -> str:
    """Derive a stable, verifiable space identifier (§4.3).

    Uses the same construction as :func:`derive_instance_id` — spaces and
    instances share a single id namespace because both are public-key
    fingerprints. Future mesh routing can then key on ``space_id``
    without a directory.
    """
    if len(space_public_key_bytes) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    digest = hashlib.sha256(space_public_key_bytes).digest()
    return base64.b32encode(digest[:20]).decode("ascii").lower().rstrip("=")


def generate_space_keypair() -> "Ed25519Keypair":
    """Generate a fresh Ed25519 keypair for a new space (§4.3.2).

    Same algorithm as :func:`generate_identity_keypair` — a separate
    function exists so call sites read clearly. The returned tuple is
    ``(private_seed, public_key)``, both 32 bytes.
    """
    return generate_identity_keypair()


def derive_user_id(instance_public_key_bytes: bytes, username: str) -> str:
    """Derive a globally unique, cryptographically bound user identifier.

    Uses a NUL-byte separator between the key and username to prevent any
    length-extension confusion (e.g. a key ending in ``'a'`` concatenated with
    username ``'bc'`` must not collide with a key ending in ``'ab'`` +
    username ``'c'``).

    See §4.1.3 of the spec.
    """
    if len(instance_public_key_bytes) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    if not username:
        raise ValueError("username must be non-empty")
    payload = instance_public_key_bytes + b"\x00" + username.encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return base64.b32encode(digest[:20]).decode("ascii").lower().rstrip("=")


# ─── Ed25519 keypair lifecycle ────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Ed25519Keypair:
    """Raw bytes of an Ed25519 keypair.

    ``private_key`` is the 32-byte seed (not the expanded 64-byte secret key).
    ``public_key`` is the 32-byte public key bytes.
    """

    private_key: bytes  # 32-byte seed
    public_key: bytes  # 32-byte public key


def generate_identity_keypair() -> Ed25519Keypair:
    """Generate a new long-term Ed25519 identity keypair."""
    sk = Ed25519PrivateKey.generate()
    seed = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pk = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return Ed25519Keypair(private_key=seed, public_key=pk)


def sign_ed25519(seed: bytes, message: bytes) -> bytes:
    """Sign ``message`` with the Ed25519 private key seed.

    Returns the raw 64-byte signature.
    """
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    return sk.sign(message)


def verify_ed25519(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Return ``True`` iff ``signature`` is a valid Ed25519 signature."""
    if len(public_key) != 32:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except InvalidSignature:
        return False
    except ValueError:
        return False
    return True


# ─── X25519 (ECDH) ────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class X25519Keypair:
    private_key: bytes  # 32 bytes
    public_key: bytes  # 32 bytes


def generate_x25519_keypair() -> X25519Keypair:
    sk = X25519PrivateKey.generate()
    priv = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return X25519Keypair(private_key=priv, public_key=pub)


def x25519_exchange(private_key: bytes, peer_public_key: bytes) -> bytes:
    """Perform an X25519 Diffie–Hellman exchange and return the raw shared secret."""
    if len(private_key) != 32 or len(peer_public_key) != 32:
        raise ValueError("X25519 keys must be 32 bytes")
    sk = X25519PrivateKey.from_private_bytes(private_key)
    pk = X25519PublicKey.from_public_bytes(peer_public_key)
    return sk.exchange(pk)


# ─── User-identity signature suite tag (independent user identity) ─────────


USER_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_USER_SIG_SUITES: frozenset[str] = frozenset({USER_SIG_SUITE_ED25519})


class UnsupportedUserSigSuite(ValueError):
    """Raised when a user-identity assertion advertises a `user_sig_suite` this
    build doesn't know. Receivers MUST reject rather than fall back to a default
    (downgrade protection once the Phase-2 hybrid `ed25519+mldsa65` lands). PQ
    migration grows the frozenset + ships the parallel signature; wire shape
    unchanged."""


def validate_user_sig_suite(suite: str) -> None:
    if not isinstance(suite, str) or suite not in SUPPORTED_USER_SIG_SUITES:
        raise UnsupportedUserSigSuite(
            f"user_sig_suite={suite!r} not recognised; expected one of "
            f"{SUPPORTED_USER_SIG_SUITES}",
        )


# ─── Move-out link signature suite tag (move-out link) ─────────────────────


MOVE_LINK_SUITE_ED25519: str = "ed25519"
SUPPORTED_MOVE_LINK_SUITES: frozenset[str] = frozenset({MOVE_LINK_SUITE_ED25519})


class UnsupportedMoveLinkSuite(ValueError):
    """Raised when a move-out link advertises a `suite` this build doesn't know.
    Receivers MUST reject rather than fall back to a default (downgrade
    protection once the Phase-2 hybrid `ed25519+mldsa65` lands). PQ migration
    grows the frozenset + ships the parallel signature; wire shape unchanged."""


def validate_move_link_suite(suite: str) -> None:
    if not isinstance(suite, str) or suite not in SUPPORTED_MOVE_LINK_SUITES:
        raise UnsupportedMoveLinkSuite(
            f"move-link suite={suite!r} not recognised; expected one of "
            f"{SUPPORTED_MOVE_LINK_SUITES}",
        )


# ─── User self-signature (independent user identity Phase 1) ───────────────


def user_identity_signed_bytes(
    *,
    user_id: str,
    instance_id: str,
    username: str,
    user_public_key: bytes,
    user_sig_suite: str,
    identity_anchor: str | None = None,
) -> bytes:
    """Canonical bytes the USER self-signature covers. Length-prefixed (4-byte
    big-endian length per field via ``_lv``), mirroring
    ``user_assertion_signed_bytes`` — so a NUL byte in any field (usernames are
    not charset-restricted) cannot shift field boundaries and collide two
    different field tuples onto identical signed bytes. Binds user_id, instance,
    username, user pubkey, suite, under the ``sh/user-identity/v1`` domain
    prefix.

    When ``identity_anchor`` is present, the bytes are **extended** with the
    anchor under a distinct ``identity-anchor`` domain tag so the user self-sig
    commits to the anchor too. The ``_lv``-prefixed tag can't collide with the
    legacy tail (whose final field is the suite value), so the absent-anchor
    case stays byte-identical to today (legacy compat)."""
    base = (
        _lv(b"sh/user-identity/v1")
        + _lv(user_id.encode("utf-8"))
        + _lv(instance_id.encode("utf-8"))
        + _lv(username.encode("utf-8"))
        + _lv(user_public_key)
        + _lv(user_sig_suite.encode("utf-8"))
    )
    if identity_anchor is None:
        return base
    return base + _lv(b"identity-anchor") + _lv(identity_anchor.encode("utf-8"))


def sign_user_self(user_seed: bytes, message: bytes) -> bytes:
    return sign_ed25519(user_seed, message)


def verify_user_self(user_public_key: bytes, message: bytes, signature: bytes) -> bool:
    return verify_ed25519(user_public_key, message, signature)


# ─── Move-out link signed bytes (move-out link) ────────────────────────────


def move_link_user_signed_bytes(
    *,
    old_user_id: str,
    new_user_id: str,
    user_public_key: bytes,
    new_instance_public_key: bytes,
    issued_at: str,
    suite: str,
) -> bytes:
    """Canonical bytes the USER (``P``) signature on a move-out link covers.

    Length-prefixed (4-byte big-endian length per field via ``_lv``) so a NUL
    byte in any field can't shift field boundaries and collide two different
    field tuples onto identical signed bytes. Committed under the
    ``sh/move-out/user/v1`` domain tag — distinct from the release tag so the
    two move-link signatures can never be cross-replayed."""
    return (
        _lv(b"sh/move-out/user/v1")
        + _lv(old_user_id.encode("utf-8"))
        + _lv(new_user_id.encode("utf-8"))
        + _lv(user_public_key)
        + _lv(new_instance_public_key)
        + _lv(issued_at.encode("utf-8"))
        + _lv(suite.encode("utf-8"))
    )


def move_link_release_signed_bytes(
    *,
    old_user_id: str,
    new_user_id: str,
    user_public_key: bytes,
    new_instance_public_key: bytes,
    issued_at: str,
    suite: str,
) -> bytes:
    """Canonical bytes the RELEASE (old-home instance) signature covers.

    Same field set / length-prefixing as
    :func:`move_link_user_signed_bytes`, but under the distinct
    ``sh/move-out/release/v1`` domain tag — so the release and user signatures
    can never be cross-replayed. By committing to ``new_user_id`` +
    ``new_instance_public_key`` it is the destination-pin: the releasing
    household vouches for *this specific* destination."""
    return (
        _lv(b"sh/move-out/release/v1")
        + _lv(old_user_id.encode("utf-8"))
        + _lv(new_user_id.encode("utf-8"))
        + _lv(user_public_key)
        + _lv(new_instance_public_key)
        + _lv(issued_at.encode("utf-8"))
        + _lv(suite.encode("utf-8"))
    )


# ─── Move-out link build + verify (move-out link) ──────────────────────────


# A move-link is a DURABLE fact: its security comes from the dual signatures +
# destination pinning + the monotonic issued_at guard, NOT from freshness. When
# the caller opts out of age-checking (``max_age is None``), we still need the
# embedded assertion's signature/binding checks to run, so we pass this
# effectively-infinite window so expiry never trips.
_DURABLE_MAX_AGE = timedelta(days=365_000)


class MoveLinkError(ValueError):
    """Base for every move-out link verification failure."""


class MoveLinkUserSigInvalid(MoveLinkError):
    """The USER (``P``) consent signature on the link did not verify."""


class MoveLinkReleaseSigInvalid(MoveLinkError):
    """The old-home RELEASE consent signature on the link did not verify."""


class MoveLinkReleaseDestinationMismatch(MoveLinkError):
    """The release signature was issued for a different destination than the
    one carried by this link (the destination-pin was violated).

    Defined for explicit future use; today a release issued over a different
    destination simply fails to verify against the recomputed bytes and surfaces
    as :class:`MoveLinkReleaseSigInvalid`."""


class MoveLinkBindingInvalid(MoveLinkError):
    """A structural binding failed: the receiver's stored P↔old_id binding did
    not match the link's ``P``, or the embedded new-home P↔new_id assertion was
    invalid / inconsistent with the link's ``P``."""


def build_move_link(
    *,
    user_seed: bytes,
    user_public_key: bytes,
    old_home_instance_seed: bytes,
    old_user_id: str,
    old_instance_id: str,
    new_instance_public_key: bytes,
    new_home_assertion: UserIdentityAssertion,
    issued_at: str,
    suite: str = MOVE_LINK_SUITE_ED25519,
) -> MoveLink:
    """Assemble a dual-signed :class:`MoveLink`.

    Produces the USER consent signature (by ``P`` = ``user_seed``) and the
    destination-pinned RELEASE signature (by the old home's instance key =
    ``old_home_instance_seed``) over the canonical move-link bytes, both
    committing to ``new_user_id`` (taken from ``new_home_assertion``) +
    ``new_instance_public_key``. The two pubkeys are hex-encoded on the wire.
    """
    validate_move_link_suite(suite)
    new_user_id = new_home_assertion.user_id

    user_sig = b64url_encode(
        sign_user_self(
            user_seed,
            move_link_user_signed_bytes(
                old_user_id=old_user_id,
                new_user_id=new_user_id,
                user_public_key=user_public_key,
                new_instance_public_key=new_instance_public_key,
                issued_at=issued_at,
                suite=suite,
            ),
        )
    )
    # The RELEASE signature is a raw Ed25519 INSTANCE signature over arbitrary
    # bytes — the same primitive sign_user_assertion uses internally.
    release_sig = b64url_encode(
        sign_ed25519(
            old_home_instance_seed,
            move_link_release_signed_bytes(
                old_user_id=old_user_id,
                new_user_id=new_user_id,
                user_public_key=user_public_key,
                new_instance_public_key=new_instance_public_key,
                issued_at=issued_at,
                suite=suite,
            ),
        )
    )

    return MoveLink(
        suite=suite,
        user_public_key=user_public_key.hex(),
        old_user_id=old_user_id,
        old_instance_id=old_instance_id,
        issued_at=issued_at,
        new_instance_public_key=new_instance_public_key.hex(),
        new_home_assertion=new_home_assertion,
        user_signature=user_sig,
        release_signature=release_sig,
    )


def verify_move_link(
    link: MoveLink,
    *,
    old_home_pinned_pk: bytes,
    stored_old_user_pubkey: bytes,
    now: datetime | None = None,
    max_age: timedelta | None = None,
) -> None:
    """Verify a move-out link under DUAL CONSENT + both P-bindings.

    Raises a specific :class:`MoveLinkError` subclass on each failed leg;
    returns ``None`` on success. ``old_home_pinned_pk`` is the old home's
    instance pubkey the receiver already pins; ``stored_old_user_pubkey`` is the
    portable ``P`` the receiver already stored for ``old_user_id``.

    Freshness is caller-controlled: a move-link is a DURABLE record, so the
    default ``max_age=None`` performs NO age rejection anywhere (signatures and
    all bindings still verify — only the age gates are skipped). Pass a
    ``timedelta`` to bound freshness; that same bound is propagated to the
    embedded new-home assertion check so the caller's policy reaches every leg.
    """
    # 1. Suite — no default fallback (downgrade protection).
    validate_move_link_suite(link.suite)

    # 2. Decode the relayed key material.
    try:
        link_pubkey = bytes.fromhex(link.user_public_key)
        new_inst_pk = bytes.fromhex(link.new_instance_public_key)
    except ValueError as exc:
        raise MoveLinkBindingInvalid("malformed move-link key material") from exc
    if len(link_pubkey) != 32 or len(new_inst_pk) != 32:
        raise MoveLinkBindingInvalid("malformed move-link key material")

    # 3. Binding #3 — P↔old_id (the binding the receiver already stores) and the
    #    pinned old-home instance key must match the link's old_instance_id.
    if link_pubkey != stored_old_user_pubkey:
        raise MoveLinkBindingInvalid("link P does not match stored old-home P")
    if len(old_home_pinned_pk) != 32:
        raise MoveLinkBindingInvalid("malformed old-home pinned key")
    if derive_instance_id(old_home_pinned_pk) != link.old_instance_id:
        raise MoveLinkBindingInvalid(
            "pinned old-home key does not match link.old_instance_id"
        )

    # 4. Binding #4 — P↔new_id, verified against the relayed new-home instance
    #    pubkey, and the embedded assertion must bind the SAME P.
    if derive_instance_id(new_inst_pk) != link.new_home_assertion.instance_id:
        raise MoveLinkBindingInvalid(
            "new-home instance key does not match the embedded assertion"
        )
    try:
        verify_user_identity_assertion(
            link.new_home_assertion,
            new_inst_pk,
            now=now,
            # Propagate the caller's freshness policy: when age-checking is off
            # (max_age is None), use a sentinel-large window so the assertion's
            # signature/binding checks still run but expiry never trips.
            max_age=_DURABLE_MAX_AGE if max_age is None else max_age,
        )
    except (ValueError, UnsupportedUserSigSuite) as exc:
        raise MoveLinkBindingInvalid(f"new-home assertion invalid: {exc}") from exc
    if (link.new_home_assertion.user_identity_public_key or "").lower() != (
        link.user_public_key.lower()
    ):
        raise MoveLinkBindingInvalid("new-home assertion binds a different P")

    # 5. Destination user_id is sourced from the verified embedded binding.
    new_user_id = link.new_home_assertion.user_id

    # 6. USER consent — the person (holding P) authorised this move.
    try:
        user_sig = b64url_decode(link.user_signature)
    except (ValueError, binascii.Error) as exc:
        raise MoveLinkUserSigInvalid("malformed user signature encoding") from exc
    if not verify_user_self(
        link_pubkey,
        move_link_user_signed_bytes(
            old_user_id=link.old_user_id,
            new_user_id=new_user_id,
            user_public_key=link_pubkey,
            new_instance_public_key=new_inst_pk,
            issued_at=link.issued_at,
            suite=link.suite,
        ),
        user_sig,
    ):
        raise MoveLinkUserSigInvalid("move-link user consent signature invalid")

    # 7. RELEASE consent — the old home vouched for THIS destination. A release
    #    issued over a different destination won't verify against these
    #    recomputed bytes (the destination-pin).
    try:
        release_sig = b64url_decode(link.release_signature)
    except (ValueError, binascii.Error) as exc:
        raise MoveLinkReleaseSigInvalid("malformed release signature encoding") from exc
    if not verify_ed25519(
        old_home_pinned_pk,
        move_link_release_signed_bytes(
            old_user_id=link.old_user_id,
            new_user_id=new_user_id,
            user_public_key=link_pubkey,
            new_instance_public_key=new_inst_pk,
            issued_at=link.issued_at,
            suite=link.suite,
        ),
        release_sig,
    ):
        raise MoveLinkReleaseSigInvalid("move-link release consent signature invalid")

    # 8. Freshness — caller-controlled. A move-link is a durable record, so when
    #    max_age is None (the default) we do NOT age-check at all. Otherwise
    #    reject a link older than max_age (one-directional: a future-dated link
    #    is fine, the new-home assertion gate above already bounds clock skew).
    if max_age is not None:
        issued = parse_iso8601_strict(link.issued_at)
        current = now if now is not None else datetime.now(timezone.utc)
        if (current - issued).total_seconds() > max_age.total_seconds():
            raise MoveLinkError("move-link is older than max_age")


# ─── UserIdentityAssertion encoding (§4.1.4) ──────────────────────────────


def _lv(b: bytes) -> bytes:
    """Length-prefix a byte string with a 4-byte big-endian length."""
    return len(b).to_bytes(4, "big") + b


def user_assertion_signed_bytes(
    user_id: str,
    instance_id: str,
    username: str,
    display_name: str,
    issued_at: str,
) -> bytes:
    """Canonical byte encoding of a ``UserIdentityAssertion`` for signing.

    ``picture_hash`` is intentionally excluded — it is mutable and
    not security-critical (§4.1.4).
    """
    return (
        b"\x01"
        + _lv(user_id.encode("utf-8"))
        + _lv(instance_id.encode("utf-8"))
        + _lv(username.encode("utf-8"))
        + _lv(display_name.encode("utf-8"))
        + _lv(issued_at.encode("utf-8"))
    )


def instance_assertion_signed_bytes(
    *,
    user_id: str,
    instance_id: str,
    username: str,
    display_name: str,
    issued_at: str,
    user_identity_public_key: bytes | None,
    user_sig_suite: str | None,
    identity_anchor: str | None = None,
) -> bytes:
    """Bytes the INSTANCE signature covers.

    For a legacy assertion (no user binding, no anchor) this is **exactly** the
    legacy :func:`user_assertion_signed_bytes` — byte-for-byte backward
    compatible with existing v24 assertions.

    For a binding-bearing assertion (``user_identity_public_key`` present) the
    legacy bytes are **extended** with the user pubkey + suite under a
    ``user-binding`` domain separator, so the household vouches for the
    *specific* user key. This closes the transplant flaw: an attacker who
    swaps in their own user pubkey can no longer re-use the victim's instance
    signature, because that signature now commits to the user key.

    When ``identity_anchor`` is present it is appended under a distinct
    ``identity-anchor`` domain tag so the instance signature also commits to
    the anchor ``user_id`` derives from — a swapped anchor breaks the instance
    sig, not just the verifier's derivation check. Both ``_lv``-prefixed domain
    tags can't collide with the legacy tail (whose final field is the
    ``issued_at`` value) nor with each other, so the extensions are unambiguous
    and the absent-both case stays byte-identical to the legacy bytes.
    """
    out = user_assertion_signed_bytes(
        user_id,
        instance_id,
        username,
        display_name,
        issued_at,
    )
    if user_identity_public_key is not None:
        out += (
            _lv(b"user-binding")
            + _lv(user_identity_public_key)
            + _lv((user_sig_suite or USER_SIG_SUITE_ED25519).encode("utf-8"))
        )
    if identity_anchor is not None:
        out += _lv(b"identity-anchor") + _lv(identity_anchor.encode("utf-8"))
    return out


def sign_user_assertion(
    seed: bytes,
    *,
    user_id: str,
    instance_id: str,
    username: str,
    display_name: str,
    issued_at: str,
    user_identity_public_key: bytes | None = None,
    user_sig_suite: str | None = None,
    identity_anchor: str | None = None,
) -> str:
    """Produce the base64url Ed25519 INSTANCE signature for a user identity
    assertion.

    When ``user_identity_public_key`` is supplied the signed bytes are extended
    to commit to the user binding (see :func:`instance_assertion_signed_bytes`);
    when ``identity_anchor`` is supplied they are also extended to commit to the
    anchor. Otherwise the legacy bytes are signed verbatim.
    """
    payload = instance_assertion_signed_bytes(
        user_id=user_id,
        instance_id=instance_id,
        username=username,
        display_name=display_name,
        issued_at=issued_at,
        user_identity_public_key=user_identity_public_key,
        user_sig_suite=user_sig_suite,
        identity_anchor=identity_anchor,
    )
    return b64url_encode(sign_ed25519(seed, payload))


def build_user_identity_assertion(
    *,
    instance_seed: bytes,
    user_id: str,
    instance_id: str,
    username: str,
    display_name: str,
    issued_at: str,
    picture_hash: str | None = None,
    public_key: str | None = None,
    public_key_version: int = 0,
    user_seed: bytes | None = None,
    user_public_key: bytes | None = None,
    user_sig_suite: str = USER_SIG_SUITE_ED25519,
    identity_anchor: str | None = None,
) -> UserIdentityAssertion:
    """Build a signed :class:`UserIdentityAssertion`.

    The INSTANCE signature (``signature``) is always produced from
    ``instance_seed`` exactly as the legacy path did. When **both**
    ``user_seed`` and ``user_public_key`` are supplied, a second USER
    self-signature is attached, binding the user's own identity key into the
    assertion (proving "the user holds their identity key", independent of the
    hosting instance). When either is absent the legacy assertion is produced
    verbatim — all ``user_*`` binding fields stay ``None``.
    """
    user_identity_public_key: str | None = None
    suite: str | None = None
    user_signature: str | None = None
    binding_pubkey: bytes | None = None
    if user_seed is not None and user_public_key is not None:
        validate_user_sig_suite(user_sig_suite)
        user_identity_public_key = user_public_key.hex()
        suite = user_sig_suite
        binding_pubkey = user_public_key
        body = user_identity_signed_bytes(
            user_id=user_id,
            instance_id=instance_id,
            username=username,
            user_public_key=user_public_key,
            user_sig_suite=user_sig_suite,
            identity_anchor=identity_anchor,
        )
        user_signature = b64url_encode(sign_user_self(user_seed, body))

    # The INSTANCE signature commits to the user binding when one is present
    # (binding_pubkey != None) so a transplanted user key can't re-use it; a
    # legacy assertion (binding_pubkey == None) signs the legacy bytes verbatim.
    signature = sign_user_assertion(
        instance_seed,
        user_id=user_id,
        instance_id=instance_id,
        username=username,
        display_name=display_name,
        issued_at=issued_at,
        user_identity_public_key=binding_pubkey,
        user_sig_suite=suite,
        identity_anchor=identity_anchor,
    )

    return UserIdentityAssertion(
        user_id=user_id,
        instance_id=instance_id,
        username=username,
        display_name=display_name,
        issued_at=issued_at,
        signature=signature,
        picture_hash=picture_hash,
        public_key=public_key,
        public_key_version=public_key_version,
        user_identity_public_key=user_identity_public_key,
        user_pq_public_key=None,  # reserved for the Phase-2 PQ hybrid
        user_sig_suite=suite,
        user_signature=user_signature,
        identity_anchor=identity_anchor,
    )


def verify_user_identity_assertion(
    assertion: "UserIdentityAssertion",
    sender_instance_public_key: bytes,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=24),
) -> None:
    """Validate a :class:`UserIdentityAssertion`.

    Raises :class:`ValueError` with a human-readable message on any failure:

    * instance_id does not match the sender's public key;
    * user_id does not match (public key + username);
    * the Ed25519 INSTANCE signature is invalid;
    * a present user-identity binding fails its USER self-signature;
    * the assertion is older than ``max_age`` or is dated in the future.

    When ``assertion.user_identity_public_key`` is present, the USER self-
    signature is additionally verified against that key — proving the user
    holds their own identity key, independent of the hosting instance. A
    present binding with an unknown ``user_sig_suite`` raises
    :class:`UnsupportedUserSigSuite` (no default fallback). When the binding
    is absent the function behaves exactly as the legacy instance-sig-only
    path.
    """
    expected_instance_id = derive_instance_id(sender_instance_public_key)
    if assertion.instance_id != expected_instance_id:
        raise ValueError("instance_id does not match sender public key")

    # ``user_id`` derives from the immutable ``identity_anchor`` (uuid for new
    # users) when present, else falls back to ``username`` for legacy rows.
    derivation_input = (
        assertion.identity_anchor
        if assertion.identity_anchor is not None
        else assertion.username
    )
    if assertion.user_id != derive_user_id(
        sender_instance_public_key, derivation_input
    ):
        raise ValueError("user_id does not match instance public key + identity anchor")

    # Parse + validate the per-user identity binding up front (when present) so
    # the INSTANCE signature can be verified over bytes that COMMIT to the user
    # pubkey. Absence of a pubkey == legacy payload: instance sig covers the
    # legacy bytes exactly, no user self-sig.
    user_pubkey: bytes | None = None
    user_self_sig: str | None = None
    suite: str | None = None
    if assertion.user_identity_public_key is not None:
        # A first-revision payload may omit the suite; default to ed25519 (the
        # documented tripwire). An explicit unknown suite must NOT fall back —
        # validate_user_sig_suite raises UnsupportedUserSigSuite.
        suite = assertion.user_sig_suite or USER_SIG_SUITE_ED25519
        validate_user_sig_suite(suite)
        if assertion.user_signature is None:
            raise ValueError("user identity binding missing user_signature")
        user_self_sig = assertion.user_signature
        try:
            user_pubkey = bytes.fromhex(assertion.user_identity_public_key)
        except ValueError as exc:
            raise ValueError("malformed user identity binding") from exc

    payload = instance_assertion_signed_bytes(
        user_id=assertion.user_id,
        instance_id=assertion.instance_id,
        username=assertion.username,
        display_name=assertion.display_name,
        issued_at=assertion.issued_at,
        user_identity_public_key=user_pubkey,
        user_sig_suite=suite,
        identity_anchor=assertion.identity_anchor,
    )
    if not verify_ed25519(
        sender_instance_public_key,
        payload,
        b64url_decode(assertion.signature),
    ):
        raise ValueError("Invalid user identity assertion signature")

    # The instance sig (above) now vouches for the specific user key when a
    # binding is present; the user self-sig additionally proves possession.
    if user_pubkey is not None and user_self_sig is not None:
        body = user_identity_signed_bytes(
            user_id=assertion.user_id,
            instance_id=assertion.instance_id,
            username=assertion.username,
            user_public_key=user_pubkey,
            user_sig_suite=suite or USER_SIG_SUITE_ED25519,
            identity_anchor=assertion.identity_anchor,
        )
        if not verify_user_self(
            user_pubkey,
            body,
            b64url_decode(user_self_sig),
        ):
            raise ValueError("Invalid user identity binding self-signature")

    issued = parse_iso8601_strict(assertion.issued_at)
    current = now if now is not None else datetime.now(timezone.utc)
    if abs((current - issued).total_seconds()) > max_age.total_seconds():
        raise ValueError("User identity assertion is expired or future-dated")


# ─── Replay cache (§24.11 validation pipeline) ────────────────────────────


#: Replay-dedup retention window. MUST exceed the federation outbox's max
#: redelivery interval (BACKOFF_SECONDS ceiling 14400s × 1.30 jitter ≈ 5.2 h):
#: outbox redelivery now re-signs each retry with a fresh timestamp, so a
#: retry of a delivery whose 2xx ack was lost passes the §24.11 timestamp
#: gate and relies SOLELY on this replay cache to be deduped. If the window
#: were shorter than the retry cadence the receiver would apply the event
#: twice. 24 h gives generous margin over the ~5.2 h ceiling.
#:
#: The binding constraint is now the §D2b connection-server relay. That
#: relay holds an envelope for an offline household for up to 24 h and the
#: receiver's timestamp step widens to
#: :data:`~socialhome.federation.inbound_validator
#: .RELAY_TIMESTAMP_SKEW_SECONDS` (24 h + 300 s) to accept it on arrival.
#: A timestamp window is only ever as safe as the replay memory behind it:
#: anywhere the two diverge, a captured envelope replayed inside the gap
#: is accepted twice. So this retention is held at **25 h** — strictly
#: above that skew budget — and
#: ``tests/federation/test_inbound_validator.py`` asserts the inequality
#: so a later "tidy-up" back to 24 h fails loudly instead of quietly
#: reopening the hole.
REPLAY_CACHE_WINDOW: timedelta = timedelta(hours=25)


class ReplayCache:
    """A bounded-window replay cache keyed by ``msg_id``.

    Federation envelopes carry a globally-unique ``msg_id`` (``uuid4``).
    Any ``msg_id`` seen within the configured ``window`` is rejected as a
    replay. Entries older than ``window`` are pruned lazily on each check
    and on ``prune()``.

    Production constructs this with :data:`REPLAY_CACHE_WINDOW` (24 h),
    which must outlast the federation outbox's max jittered redelivery
    interval (~5.2 h): redeliveries are re-signed with a fresh timestamp,
    so a retry of a lost-ack delivery passes the §24.11 timestamp gate and
    this cache is the only thing left to dedupe it.

    **Why key by ``msg_id`` alone?** The durable source of truth,
    ``federation_replay_cache``, is keyed by ``msg_id`` (its PRIMARY KEY)
    and so is the documented design (``docs/architecture.md`` →
    "Replay cache"). Keying the in-memory cache the same way keeps the two
    consistent — critically, an entry warmed at startup via :meth:`load`
    must dedupe the inbound check, which calls ``seen(msg_id,
    from_instance=<verified signer>)``. An earlier ``(from_instance,
    msg_id)`` tuple key meant warmed rows (loaded as ``("", msg_id)`` since
    the table has no sender column) never matched a scoped runtime check,
    so a replay arriving after a restart slipped through. ``seen()`` still
    accepts ``from_instance`` for caller context (the inbound error message,
    a future per-peer feature) but it is **not** part of the dedup key. A
    ``uuid4`` collision across senders is ~2⁻¹²² — negligible vs the cost of
    a key the durable layer can't enforce.
    """

    def __init__(self, window: timedelta = timedelta(hours=1)) -> None:
        # The 1 h default is for ad-hoc / test construction only; production
        # passes :data:`REPLAY_CACHE_WINDOW` (24 h) explicitly so the live
        # window outlasts the outbox's re-signed redelivery cadence.
        self._window = window
        self._seen: dict[str, datetime] = {}

    def seen(
        self,
        msg_id: str,
        *,
        from_instance: str = "",
        now: datetime | None = None,
    ) -> bool:
        """Return ``True`` if this ``msg_id`` was seen within the window.

        If not seen, records it and returns ``False`` so callers can use
        this as an atomic check-and-insert primitive. ``from_instance`` is
        accepted for caller context but is not part of the dedup key (see
        the class docstring) — the durable replay table is ``msg_id``-keyed.
        """
        current = now if now is not None else datetime.now(timezone.utc)
        self._prune(current)
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = current
        return False

    def prune(self, *, now: datetime | None = None) -> None:
        self._prune(now if now is not None else datetime.now(timezone.utc))

    def load(self, entries: list[tuple[str, str]]) -> None:
        """Warm the cache from persisted ``(msg_id, received_at)`` rows.

        Keyed by ``msg_id`` to match both the durable
        ``federation_replay_cache`` (PK = ``msg_id``) and runtime
        :meth:`seen`, so a warmed entry dedupes a post-restart inbound check
        regardless of the ``from_instance`` that check supplies.
        """
        for msg_id, received_at in entries:
            try:
                self._seen[msg_id] = parse_iso8601_strict(received_at)
            except ValueError:
                continue

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._seen)

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._window

        # Persisted ``received_at`` rows are written via SQLite's
        # ``datetime('now')`` (naive UTC), so :meth:`load` warms the
        # cache with offset-naive entries while ``seen()`` records
        # offset-aware ones. Normalise to aware UTC before comparing
        # so the prune step doesn't ``TypeError`` on a freshly-warmed
        # cache.
        def _aware(t: datetime) -> datetime:
            return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)

        stale = [k for k, t in self._seen.items() if _aware(t) < cutoff]
        for k in stale:
            del self._seen[k]


# ─── Misc primitives ──────────────────────────────────────────────────────


def generate_routing_secret() -> str:
    """32 random bytes, hex-encoded. Used to keyed-hash relay path selection.

    Never transmitted. See §4.1.8.
    """
    return os.urandom(32).hex()


def keyed_hash(secret_hex: str, data: bytes) -> bytes:
    """HMAC-SHA256 used for relay-path selection."""
    return hmac.new(bytes.fromhex(secret_hex), data, hashlib.sha256).digest()


def random_token(nbytes: int = 32) -> str:
    """URL-safe random token with sufficient entropy for auth / invite use."""
    return secrets.token_urlsafe(nbytes)


def sha256_hex(data: bytes | str) -> str:
    """Convenience: lowercase hex SHA-256 of bytes or utf-8 text."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()
