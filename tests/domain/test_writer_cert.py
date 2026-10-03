"""Tests for the WriterCert domain dataclass + wire codec."""

from __future__ import annotations

import dataclasses

import pytest

from socialhome.domain.writer_cert import (
    WRITER_SCOPE_COMMENT,
    WRITER_SCOPE_WRITE,
    WRITER_SCOPES,
    WriterCert,
    scope_permits,
    strongest_scope,
)


def _cert(**over) -> WriterCert:
    base = dict(
        cert_suite="ed25519",
        space_id="sp-1",
        epoch=3,
        instance_pk="A" * 43,
        scope=WRITER_SCOPE_WRITE,
        issued_at=1_700_000_000,
        cert_sig="sig",
    )
    base.update(over)
    return WriterCert(**base)


def test_frozen():
    cert = _cert()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cert.scope = WRITER_SCOPE_COMMENT  # type: ignore[misc]


def test_wire_round_trip():
    cert = _cert()
    wire = cert.to_wire()
    assert wire == {
        "cert_suite": "ed25519",
        "space_id": "sp-1",
        "epoch": 3,
        "instance_pk": "A" * 43,
        "scope": "write",
        "issued_at": 1_700_000_000,
        "cert_sig": "sig",
    }
    assert WriterCert.from_wire(wire) == cert


def test_signing_body_excludes_signature():
    body = _cert().signing_body()
    assert "cert_sig" not in body
    assert body["cert_suite"] == "ed25519"
    assert set(body) == {
        "cert_suite",
        "space_id",
        "epoch",
        "instance_pk",
        "scope",
        "issued_at",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"cert_suite": 1},
        {"space_id": ""},
        {"epoch": "3"},
        {"epoch": True},
        {"epoch": -1},
        {"instance_pk": ""},
        {"scope": "admin"},
        {"issued_at": "now"},
        {"cert_sig": None},
    ],
)
def test_from_wire_rejects_malformed(mutation):
    wire = _cert().to_wire()
    wire.update(mutation)
    with pytest.raises(ValueError):
        WriterCert.from_wire(wire)


def test_from_wire_rejects_missing_field_and_non_dict():
    wire = _cert().to_wire()
    del wire["scope"]
    with pytest.raises(ValueError):
        WriterCert.from_wire(wire)
    with pytest.raises(ValueError):
        WriterCert.from_wire("nope")


def test_scope_permits():
    assert scope_permits(WRITER_SCOPE_WRITE, WRITER_SCOPE_WRITE)
    assert scope_permits(WRITER_SCOPE_WRITE, WRITER_SCOPE_COMMENT)
    assert scope_permits(WRITER_SCOPE_COMMENT, WRITER_SCOPE_COMMENT)
    assert not scope_permits(WRITER_SCOPE_COMMENT, WRITER_SCOPE_WRITE)
    assert not scope_permits("bogus", WRITER_SCOPE_COMMENT)
    assert not scope_permits(WRITER_SCOPE_WRITE, "bogus")


def test_strongest_scope():
    assert WRITER_SCOPES == frozenset({"write", "comment"})
    assert strongest_scope([]) is None
    assert strongest_scope([WRITER_SCOPE_COMMENT]) == WRITER_SCOPE_COMMENT
    assert (
        strongest_scope([WRITER_SCOPE_COMMENT, WRITER_SCOPE_WRITE])
        == WRITER_SCOPE_WRITE
    )
