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
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

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

from .utils.datetime import parse_iso8601_strict

if TYPE_CHECKING:
    from .domain.user import UserIdentityAssertion


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


def sign_user_assertion(
    seed: bytes,
    *,
    user_id: str,
    instance_id: str,
    username: str,
    display_name: str,
    issued_at: str,
) -> str:
    """Produce the base64url Ed25519 signature for a user identity assertion."""
    payload = user_assertion_signed_bytes(
        user_id=user_id,
        instance_id=instance_id,
        username=username,
        display_name=display_name,
        issued_at=issued_at,
    )
    return b64url_encode(sign_ed25519(seed, payload))


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
    * the Ed25519 signature is invalid;
    * the assertion is older than ``max_age`` or is dated in the future.
    """
    expected_instance_id = derive_instance_id(sender_instance_public_key)
    if assertion.instance_id != expected_instance_id:
        raise ValueError("instance_id does not match sender public key")

    expected_user_id = derive_user_id(sender_instance_public_key, assertion.username)
    if assertion.user_id != expected_user_id:
        raise ValueError("user_id does not match instance public key + username")

    payload = user_assertion_signed_bytes(
        user_id=assertion.user_id,
        instance_id=assertion.instance_id,
        username=assertion.username,
        display_name=assertion.display_name,
        issued_at=assertion.issued_at,
    )
    if not verify_ed25519(
        sender_instance_public_key,
        payload,
        b64url_decode(assertion.signature),
    ):
        raise ValueError("Invalid user identity assertion signature")

    issued = parse_iso8601_strict(assertion.issued_at)
    current = now if now is not None else datetime.now(timezone.utc)
    if abs((current - issued).total_seconds()) > max_age.total_seconds():
        raise ValueError("User identity assertion is expired or future-dated")


# ─── Replay cache (§24.11 validation pipeline) ────────────────────────────


class ReplayCache:
    """A bounded-window replay cache keyed by ``(msg_id, from_instance)``.

    Federation envelopes carry a unique ``msg_id`` per sender instance.
    Any key seen within the configured ``window`` is rejected as a
    replay. Entries older than ``window`` are pruned lazily on each
    check and on ``prune()``.

    **Why scope by sender?** UUID collisions between two instances are
    astronomically unlikely, but scoping the cache key by
    ``from_instance`` removes the edge case entirely and makes per-peer
    rate-limiting trivial to bolt on later. The ``seen()`` API accepts
    the sender for forward compatibility; callers that don't have it
    (tests, in-memory diagnostics) can pass ``from_instance=""``.
    """

    def __init__(self, window: timedelta = timedelta(hours=1)) -> None:
        self._window = window
        self._seen: dict[tuple[str, str], datetime] = {}

    def seen(
        self,
        msg_id: str,
        *,
        from_instance: str = "",
        now: datetime | None = None,
    ) -> bool:
        """Return ``True`` if this ``(msg_id, from_instance)`` was seen
        within the window.

        If not seen, records it and returns ``False`` so callers can use
        this as an atomic check-and-insert primitive.
        """
        current = now if now is not None else datetime.now(timezone.utc)
        self._prune(current)
        key = (from_instance, msg_id)
        if key in self._seen:
            return True
        self._seen[key] = current
        return False

    def prune(self, *, now: datetime | None = None) -> None:
        self._prune(now if now is not None else datetime.now(timezone.utc))

    def load(self, entries: list[tuple[str, str]]) -> None:
        """Warm the cache from persisted ``(msg_id, received_at)`` rows.

        Pre-scoping entries (``("", msg_id)``) are preserved for back-compat
        with existing persisted rows from before the scoping change.
        """
        for msg_id, received_at in entries:
            try:
                self._seen[("", msg_id)] = parse_iso8601_strict(received_at)
            except ValueError:
                continue

    def load_scoped(
        self,
        entries: list[tuple[str, str, str]],
    ) -> None:
        """Warm the cache from ``(from_instance, msg_id, received_at)``
        rows. Used by callers that persist replay keys with sender
        scoping."""
        for from_instance, msg_id, received_at in entries:
            try:
                self._seen[(from_instance, msg_id)] = parse_iso8601_strict(received_at)
            except ValueError:
                continue

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._seen)

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._window
        stale = [k for k, t in self._seen.items() if t < cutoff]
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
