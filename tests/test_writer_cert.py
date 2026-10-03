"""Tests for writer-cert signing + verification (v_49)."""

from __future__ import annotations

import dataclasses
import os

import pytest

from socialhome.authority_sig import (
    authority_signing_bytes,
    sign_authority_event,
)
from socialhome.crypto import b64url_decode, b64url_encode, ed25519_public_key
from socialhome.domain.writer_cert import (
    WRITER_SCOPE_COMMENT,
    WRITER_SCOPE_WRITE,
    WriterCert,
)
from socialhome.writer_cert import (
    SUPPORTED_WRITER_CERT_SUITES,
    WRITER_CERT_SUITE_ED25519,
    InvalidWriterCert,
    UnsupportedWriterCertSuite,
    sign_writer_cert,
    verify_writer_cert,
    writer_cert_signing_bytes,
)

SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)
AUTHOR_PK = ed25519_public_key(os.urandom(32))


def _issue(**over) -> WriterCert:
    kw = dict(
        space_seed=SPACE_SEED,
        space_id="sp-1",
        epoch=4,
        instance_pk=AUTHOR_PK,
        scope=WRITER_SCOPE_WRITE,
    )
    kw.update(over)
    return sign_writer_cert(**kw)


def _verify(cert, **over) -> None:
    kw = dict(
        space_pubkey=SPACE_PK,
        space_id="sp-1",
        epoch=4,
        author_pk=AUTHOR_PK,
        required_scope=WRITER_SCOPE_WRITE,
    )
    kw.update(over)
    verify_writer_cert(cert, **kw)


def test_suite_constants():
    assert WRITER_CERT_SUITE_ED25519 == "ed25519"
    assert SUPPORTED_WRITER_CERT_SUITES == frozenset({"ed25519"})
    assert issubclass(UnsupportedWriterCertSuite, ValueError)
    assert issubclass(InvalidWriterCert, ValueError)


def test_issue_and_verify_round_trip():
    cert = _issue()
    assert cert.cert_suite == WRITER_CERT_SUITE_ED25519
    assert b64url_decode(cert.instance_pk) == AUTHOR_PK
    assert cert.issued_at > 0
    _verify(cert)
    # Also survives the wire.
    _verify(WriterCert.from_wire(cert.to_wire()))


def test_write_scope_permits_comment():
    _verify(_issue(), required_scope=WRITER_SCOPE_COMMENT)


def test_comment_scope_cannot_post():
    cert = _issue(scope=WRITER_SCOPE_COMMENT)
    _verify(cert, required_scope=WRITER_SCOPE_COMMENT)
    with pytest.raises(InvalidWriterCert, match="scope"):
        _verify(cert, required_scope=WRITER_SCOPE_WRITE)


def test_unknown_suite_rejected_without_fallback():
    cert = dataclasses.replace(_issue(), cert_suite="ed25519+mldsa65")
    with pytest.raises(UnsupportedWriterCertSuite):
        _verify(cert)


def test_bad_signature():
    cert = _issue()
    sig = bytearray(b64url_decode(cert.cert_sig))
    sig[0] ^= 1
    with pytest.raises(InvalidWriterCert, match="signature"):
        _verify(dataclasses.replace(cert, cert_sig=b64url_encode(bytes(sig))))
    with pytest.raises(InvalidWriterCert, match="signature"):
        _verify(dataclasses.replace(cert, cert_sig="***"))


def test_tampered_scope_breaks_signature():
    cert = _issue(scope=WRITER_SCOPE_COMMENT)
    with pytest.raises(InvalidWriterCert, match="signature"):
        _verify(dataclasses.replace(cert, scope=WRITER_SCOPE_WRITE))


def test_signed_by_non_authority_key():
    cert = _issue(space_seed=os.urandom(32))
    with pytest.raises(InvalidWriterCert, match="signature"):
        _verify(cert)


def test_wrong_space():
    with pytest.raises(InvalidWriterCert, match="space"):
        _verify(_issue(space_id="sp-other"))


def test_wrong_epoch():
    with pytest.raises(InvalidWriterCert, match="epoch"):
        _verify(_issue(epoch=3))


def test_author_pk_mismatch():
    with pytest.raises(InvalidWriterCert, match="instance_pk"):
        _verify(_issue(), author_pk=ed25519_public_key(os.urandom(32)))


def test_malformed_instance_pk():
    cert = dataclasses.replace(_issue(), instance_pk="!!")
    with pytest.raises(InvalidWriterCert):
        _verify(cert)


def test_bad_space_pubkey():
    with pytest.raises(InvalidWriterCert):
        _verify(_issue(), space_pubkey=b"short")


def test_unknown_required_scope():
    with pytest.raises(InvalidWriterCert, match="scope"):
        _verify(_issue(), required_scope="admin")


def test_issue_rejects_bad_scope_and_pk():
    with pytest.raises(ValueError):
        _issue(scope="admin")
    with pytest.raises(ValueError):
        _issue(instance_pk=b"short")
    with pytest.raises(ValueError):
        _issue(epoch=-1)


def test_signing_bytes_domain_separated():
    cert = _issue()
    msg = writer_cert_signing_bytes(cert)
    assert msg.startswith(b"space-writer-cert:v1:")
    assert not msg.startswith(b"space-authority:")
    assert b"cert_sig" not in msg


def test_authority_relay_signature_cannot_pose_as_cert():
    """An authority-relay signature over the cert body never verifies as a
    cert (distinct domain prefix)."""
    cert = _issue()
    relay_sig = sign_authority_event(
        event_type="space_post_public",
        space_id="sp-1",
        payload=cert.signing_body(),
        space_seed=SPACE_SEED,
    )["authority_sig"]
    assert authority_signing_bytes(
        event_type="space_post_public",
        space_id="sp-1",
        payload=cert.signing_body(),
    ) != writer_cert_signing_bytes(cert)
    with pytest.raises(InvalidWriterCert, match="signature"):
        _verify(dataclasses.replace(cert, cert_sig=relay_sig))
