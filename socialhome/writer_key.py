"""Space writer group key — derivation, certs and signatures (v_50).

The domain shapes live in :mod:`socialhome.domain.writer_key`. Like
:mod:`socialhome.writer_cert` this module depends only on
:mod:`socialhome.crypto` (and ``cryptography``'s HKDF), so the content-blind
GFS process can import it.

**Derivation.** One writer key per (space, content epoch), derived from the
space AUTHORITY seed::

    writer_seed = HKDF-SHA256(ikm=space_seed, salt=b"socialhome-writer-key:v1",
                              info=b"<space_id>:<epoch>", length=32)

So every household holding the seed (the owner, a delegated admin) derives
the SAME key for an epoch without coordinating — two seed holders never pin
competing keys — and seed holders store nothing. HKDF is one-way: a member
holding the writer key of epoch N learns nothing about the authority seed or
any other epoch's key. An authority-key rotation (v_44) changes the seed and
so every writer key with it; the GFS forgets its writer-key pins on that
re-pin, and the rotation bundle carries the new grant.

**Why a new key** (CLAUDE.md "no new key" rule): the content key is held by
every reader, including GFS followers, so it cannot authorize a write; the
authority seed must not spread to every publisher; the household identity key
is exactly what strict mode withholds from the connection server.

Signing bytes (each with its own domain-separation prefix, so no signature
can be lifted onto another statement):

* writer key cert: ``b"space-writer-key-cert:v1:" + canonical_json(cert minus
  cert_sig)``, signed with the space AUTHORITY seed;
* anonymous publish: ``b"gfs-member-publish-anon:v1:" + canonical_json(body
  minus writer_sig)``, signed with the WRITER key (see
  :class:`~socialhome.domain.gfs_member_publish.MemberPublishAnonRequest`).
"""

from __future__ import annotations

import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .crypto import (
    b64url_decode,
    b64url_encode,
    ed25519_public_key,
    sign_ed25519,
    verify_ed25519,
)
from .domain.writer_cert import MAX_WRITER_CERT_EPOCH
from .domain.writer_key import (
    SUPPORTED_WRITER_KEY_SUITES,
    WRITER_KEY_SUITE_ED25519,
    UnsupportedWriterKeySuite,
    WriterKeyCert,
    WriterKeyGrant,
)

_DERIVE_SALT: bytes = b"socialhome-writer-key:v1"
_CERT_PREFIX: bytes = b"space-writer-key-cert:v1:"


class InvalidWriterKey(ValueError):
    """A writer key cert / grant / signature is malformed, bound to another
    space or epoch, or does not verify."""


def derive_writer_seed(space_seed: bytes, space_id: str, epoch: int) -> bytes:
    """The 32-byte Ed25519 seed of ``space_id``'s writer key at ``epoch``
    (see the module docstring). Deterministic for every seed holder."""
    if len(space_seed) != 32:
        raise ValueError("space seed must be 32 bytes")
    if isinstance(epoch, bool) or not 0 <= int(epoch) <= MAX_WRITER_CERT_EPOCH:
        raise ValueError("epoch out of range")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_DERIVE_SALT,
        info=f"{space_id}:{int(epoch)}".encode("utf-8"),
    ).derive(space_seed)


def writer_key_cert_signing_bytes(cert: WriterKeyCert) -> bytes:
    """Canonical, domain-separated bytes the space authority seed signs."""
    return _CERT_PREFIX + json.dumps(
        cert.signing_body(), separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sign_writer_key_cert(
    *, space_seed: bytes, space_id: str, epoch: int, writer_pk: bytes
) -> WriterKeyCert:
    """Pin ``writer_pk`` as ``space_id``'s writer key at ``epoch``, signed
    with the space authority seed."""
    if len(writer_pk) != 32:
        raise ValueError("writer_pk must be a 32-byte Ed25519 public key")
    unsigned = WriterKeyCert(
        writer_key_suite=WRITER_KEY_SUITE_ED25519,
        space_id=space_id,
        epoch=int(epoch),
        writer_pk=b64url_encode(writer_pk),
        cert_sig="",
    )
    sig = sign_ed25519(space_seed, writer_key_cert_signing_bytes(unsigned))
    return WriterKeyCert(
        writer_key_suite=unsigned.writer_key_suite,
        space_id=unsigned.space_id,
        epoch=unsigned.epoch,
        writer_pk=unsigned.writer_pk,
        cert_sig=b64url_encode(sig),
    )


def verify_writer_key_cert(
    cert: WriterKeyCert, *, space_pubkey: bytes, space_id: str, epoch: int
) -> bytes:
    """Verify ``cert`` pins a writer key for ``space_id`` at ``epoch`` under
    the space authority key ``space_pubkey``. Returns the 32-byte writer
    public key; raises :class:`UnsupportedWriterKeySuite` /
    :class:`InvalidWriterKey`."""
    if cert.writer_key_suite not in SUPPORTED_WRITER_KEY_SUITES:
        raise UnsupportedWriterKeySuite(cert.writer_key_suite)
    if len(space_pubkey) != 32:
        raise InvalidWriterKey("space public key is not 32 bytes")
    try:
        sig = b64url_decode(cert.cert_sig)
        writer_pk = b64url_decode(cert.writer_pk)
    except Exception as exc:
        raise InvalidWriterKey("writer key cert is not base64url") from exc
    if not verify_ed25519(space_pubkey, writer_key_cert_signing_bytes(cert), sig):
        raise InvalidWriterKey("writer key cert does not verify against the space key")
    if cert.space_id != space_id:
        raise InvalidWriterKey("writer key cert is for another space")
    if cert.epoch != epoch:
        raise InvalidWriterKey("writer key cert is for another epoch")
    if len(writer_pk) != 32:
        raise InvalidWriterKey("writer_pk is not 32 bytes")
    return writer_pk


def issue_writer_key_grant(
    *, space_seed: bytes, space_id: str, epoch: int
) -> WriterKeyGrant:
    """Derive ``space_id``'s writer key at ``epoch`` and wrap it, with its
    authority cert, as the grant a publish-capable household is delivered."""
    seed = derive_writer_seed(space_seed, space_id, epoch)
    cert = sign_writer_key_cert(
        space_seed=space_seed,
        space_id=space_id,
        epoch=epoch,
        writer_pk=ed25519_public_key(seed),
    )
    return WriterKeyGrant(
        writer_key_suite=WRITER_KEY_SUITE_ED25519,
        space_id=space_id,
        epoch=int(epoch),
        writer_seed=b64url_encode(seed),
        writer_key_cert=cert,
    )


def verify_writer_key_grant(
    grant: WriterKeyGrant, *, space_pubkey: bytes, space_id: str
) -> bytes:
    """Verify a delivered grant: a supported suite, its cert under the space
    authority key for this space and the grant's epoch, and the seed's public
    half equal to the pinned ``writer_pk``. Returns the 32-byte seed."""
    if grant.writer_key_suite not in SUPPORTED_WRITER_KEY_SUITES:
        raise UnsupportedWriterKeySuite(grant.writer_key_suite)
    if grant.space_id != space_id:
        raise InvalidWriterKey("writer key grant is for another space")
    writer_pk = verify_writer_key_cert(
        grant.writer_key_cert,
        space_pubkey=space_pubkey,
        space_id=space_id,
        epoch=grant.epoch,
    )
    try:
        seed = b64url_decode(grant.writer_seed)
    except Exception as exc:
        raise InvalidWriterKey("writer seed is not base64url") from exc
    if len(seed) != 32 or ed25519_public_key(seed) != writer_pk:
        raise InvalidWriterKey("writer seed does not match the pinned writer key")
    return seed


def sign_with_writer_key(writer_seed: bytes, message: bytes) -> str:
    """b64url Ed25519 signature of ``message`` under the writer key."""
    return b64url_encode(sign_ed25519(writer_seed, message))


def verify_writer_sig(
    *, writer_pk: bytes, message: bytes, writer_sig: str, suite: str
) -> bool:
    """Whether ``writer_sig`` (suite ``suite``) signs ``message`` under
    ``writer_pk``. Raises :class:`UnsupportedWriterKeySuite` for an unknown
    suite; ``False`` for a malformed or failing signature."""
    if suite not in SUPPORTED_WRITER_KEY_SUITES:
        raise UnsupportedWriterKeySuite(suite)
    try:
        sig = b64url_decode(writer_sig)
    except Exception:
        return False
    return verify_ed25519(writer_pk, message, sig)
