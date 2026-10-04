"""Tests for the writer group key domain shapes (v_50)."""

from __future__ import annotations

import pytest

from socialhome.domain.writer_key import (
    SUPPORTED_WRITER_KEY_SUITES,
    WRITER_KEY_CERT_KEYS,
    WRITER_KEY_GRANT_KEYS,
    WRITER_KEY_SUITE_ED25519,
    UnsupportedWriterKeySuite,
    WriterKeyCert,
    WriterKeyGrant,
)


def _cert_wire(**over) -> dict:
    wire = {
        "writer_key_suite": "ed25519",
        "space_id": "sp-1",
        "epoch": 3,
        "writer_pk": "AAAA",
        "cert_sig": "BBBB",
    }
    wire.update(over)
    return wire


def test_suite_constants() -> None:
    assert WRITER_KEY_SUITE_ED25519 == "ed25519"
    assert WRITER_KEY_SUITE_ED25519 in SUPPORTED_WRITER_KEY_SUITES
    assert issubclass(UnsupportedWriterKeySuite, ValueError)


def test_cert_round_trip() -> None:
    cert = WriterKeyCert.from_wire(_cert_wire())
    assert cert.to_wire() == _cert_wire()
    assert set(cert.to_wire()) == WRITER_KEY_CERT_KEYS
    assert "cert_sig" not in cert.signing_body()


def test_cert_unknown_suite_string_parses() -> None:
    # The verifier rejects it; the codec does not default it away.
    assert (
        WriterKeyCert.from_wire(_cert_wire(writer_key_suite="x")).writer_key_suite
        == "x"
    )


@pytest.mark.parametrize(
    "over",
    [
        {"epoch": -1},
        {"epoch": True},
        {"epoch": "3"},
        {"space_id": ""},
        {"space_id": "x" * 129},
        {"writer_pk": ""},
        {"cert_sig": 5},
        {"writer_key_suite": ""},
        {"extra": 1},
    ],
)
def test_cert_rejects_malformed(over) -> None:
    with pytest.raises(ValueError):
        WriterKeyCert.from_wire(_cert_wire(**over))


def test_cert_rejects_missing_field() -> None:
    wire = _cert_wire()
    del wire["writer_pk"]
    with pytest.raises(ValueError):
        WriterKeyCert.from_wire(wire)
    with pytest.raises(ValueError):
        WriterKeyCert.from_wire("nope")


def _grant_wire(**over) -> dict:
    wire = {
        "writer_key_suite": "ed25519",
        "space_id": "sp-1",
        "epoch": 3,
        "writer_seed": "CCCC",
        "writer_key_cert": _cert_wire(),
    }
    wire.update(over)
    return wire


def test_grant_round_trip() -> None:
    grant = WriterKeyGrant.from_wire(_grant_wire())
    assert grant.to_wire() == _grant_wire()
    assert set(grant.to_wire()) == WRITER_KEY_GRANT_KEYS


@pytest.mark.parametrize(
    "over",
    [
        {"writer_seed": ""},
        {"writer_key_cert": {"bad": 1}},
        {"epoch": None},
        {"more": True},
    ],
)
def test_grant_rejects_malformed(over) -> None:
    with pytest.raises(ValueError):
        WriterKeyGrant.from_wire(_grant_wire(**over))
    with pytest.raises(ValueError):
        WriterKeyGrant.from_wire([])
