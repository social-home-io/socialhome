"""Space writer certificates — signing + verification (v_49).

The domain shape lives in :mod:`socialhome.domain.writer_cert`; this module
signs a :class:`WriterCert` with the space AUTHORITY seed and verifies one
against the pinned space public key. Like :mod:`socialhome.authority_sig`
and :mod:`socialhome.authority_cert` it depends only on
:mod:`socialhome.crypto`, so the content-blind GFS process can import it.

Signing bytes: ``b"space-writer-cert:v1:" + canonical_json(cert minus
cert_sig)`` — sorted keys, compact separators. The prefix is distinct from
the authority-relay prefix (``space-authority:v1:``) so neither signature
can be lifted onto the other.

Verification (:func:`verify_writer_cert`) checks, in order: a supported
``cert_suite`` (no default fallback), the signature against the pinned
space authority key, ``space_id`` and ``epoch`` match, ``instance_pk``
equals the author's household identity key, and ``scope`` permits the
required action. Every failure raises — callers drop the item.
"""

from __future__ import annotations

import json
import time

from .crypto import b64url_decode, b64url_encode, sign_ed25519, verify_ed25519
from .domain.writer_cert import (
    MAX_WRITER_CERT_EPOCH,
    WRITER_SCOPES,
    WriterCert,
    scope_permits,
)

#: Suite of the space-authority signature over a writer cert. Phase-2 of
#: ``docs/crypto.md`` adds ``"ed25519+mldsa65"`` as a sibling.
WRITER_CERT_SUITE_ED25519: str = "ed25519"
SUPPORTED_WRITER_CERT_SUITES: frozenset[str] = frozenset({WRITER_CERT_SUITE_ED25519})

#: Domain-separation prefix of the signing bytes.
_WRITER_CERT_PREFIX: bytes = b"space-writer-cert:v1:"


class UnsupportedWriterCertSuite(ValueError):
    """The cert names a signature suite this build does not know. Receivers
    MUST reject — no default fallback, or a downgrade becomes possible."""


class InvalidWriterCert(ValueError):
    """The cert is malformed, bound to another space / epoch / household,
    lacks the required scope, or its signature does not verify."""


def writer_cert_signing_bytes(cert: WriterCert) -> bytes:
    """Canonical, domain-separated bytes the space authority key signs."""
    return _WRITER_CERT_PREFIX + json.dumps(
        cert.signing_body(), separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sign_writer_cert(
    *,
    space_seed: bytes,
    space_id: str,
    epoch: int,
    instance_pk: bytes,
    scope: str,
    issued_at: int | None = None,
) -> WriterCert:
    """Issue a writer cert for household ``instance_pk`` (32-byte Ed25519
    identity public key), signed with the space authority seed."""
    if scope not in WRITER_SCOPES:
        raise ValueError(f"unknown writer scope {scope!r}")
    if len(instance_pk) != 32:
        raise ValueError("instance_pk must be a 32-byte Ed25519 public key")
    if isinstance(epoch, bool) or not 0 <= int(epoch) <= MAX_WRITER_CERT_EPOCH:
        raise ValueError("epoch out of range")
    unsigned = WriterCert(
        cert_suite=WRITER_CERT_SUITE_ED25519,
        space_id=space_id,
        epoch=int(epoch),
        instance_pk=b64url_encode(instance_pk),
        scope=scope,
        issued_at=int(time.time()) if issued_at is None else int(issued_at),
        cert_sig="",
    )
    sig = sign_ed25519(space_seed, writer_cert_signing_bytes(unsigned))
    return WriterCert(
        cert_suite=unsigned.cert_suite,
        space_id=unsigned.space_id,
        epoch=unsigned.epoch,
        instance_pk=unsigned.instance_pk,
        scope=unsigned.scope,
        issued_at=unsigned.issued_at,
        cert_sig=b64url_encode(sig),
    )


def verify_writer_cert(
    cert: WriterCert,
    *,
    space_pubkey: bytes,
    space_id: str,
    epoch: int,
    author_pk: bytes,
    required_scope: str,
) -> None:
    """Verify ``cert`` authorizes ``author_pk`` to act at ``required_scope``
    in ``space_id`` during ``epoch``. Returns ``None``; raises
    :class:`UnsupportedWriterCertSuite` / :class:`InvalidWriterCert`."""
    if cert.cert_suite not in SUPPORTED_WRITER_CERT_SUITES:
        raise UnsupportedWriterCertSuite(cert.cert_suite)
    if len(space_pubkey) != 32:
        raise InvalidWriterCert("space public key is not 32 bytes")
    try:
        sig = b64url_decode(cert.cert_sig)
    except Exception as exc:
        raise InvalidWriterCert("signature is not base64url") from exc
    if not verify_ed25519(space_pubkey, writer_cert_signing_bytes(cert), sig):
        raise InvalidWriterCert("signature does not verify against the space key")
    if cert.space_id != space_id:
        raise InvalidWriterCert("cert is for another space")
    if cert.epoch != epoch:
        raise InvalidWriterCert("cert is for another epoch")
    try:
        cert_pk = b64url_decode(cert.instance_pk)
    except Exception as exc:
        raise InvalidWriterCert("instance_pk is not base64url") from exc
    if cert_pk != author_pk:
        raise InvalidWriterCert("instance_pk does not match the author")
    if required_scope not in WRITER_SCOPES or not scope_permits(
        cert.scope, required_scope
    ):
        raise InvalidWriterCert(
            f"scope {cert.scope!r} does not permit {required_scope!r}"
        )
