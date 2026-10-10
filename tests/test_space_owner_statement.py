"""Tests for the owner household's ownership statement and the inviter's
seed-holder proof (migration 0091)."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    derive_instance_id,
    ed25519_public_key,
    generate_identity_keypair,
)
from socialhome.space_owner_statement import (
    MAX_STATEMENT_FUTURE_SKEW_MS,
    OWNER_STATEMENT_SUITE_ED25519,
    SUPPORTED_OWNER_STATEMENT_SUITES,
    InvalidOwnerStatement,
    UnsupportedOwnerStatementSuite,
    sign_inviter_proof,
    sign_owner_statement,
    verify_inviter_proof,
    verify_owner_statement,
)

OWNER_SEED = b"\x51" * 32
OWNER = derive_instance_id(ed25519_public_key(OWNER_SEED))
SPACE_KP = generate_identity_keypair()
SPACE_PK = SPACE_KP.public_key.hex()
NOW = 1_800_000_000_000


def _stmt(**over) -> dict:
    kw = {
        "space_id": "sp-1",
        "owner_seed": OWNER_SEED,
        "authority_pk_hex": SPACE_PK,
        "authority_key_epoch": 0,
        "issued_at_ms": NOW,
        "now_ms": NOW,
    }
    kw.update(over)
    return sign_owner_statement(**kw)


def _verify(stmt, **over):
    kw = {
        "space_id": "sp-1",
        "owner_instance_id": OWNER,
        "authority_pk_hex": SPACE_PK,
        "now_ms": NOW,
    }
    kw.update(over)
    return verify_owner_statement(stmt, **kw)


def test_suite_constants() -> None:
    assert OWNER_STATEMENT_SUITE_ED25519 == "ed25519"
    assert SUPPORTED_OWNER_STATEMENT_SUITES == frozenset({"ed25519"})
    assert issubclass(UnsupportedOwnerStatementSuite, ValueError)
    assert issubclass(InvalidOwnerStatement, ValueError)


def test_round_trip() -> None:
    stmt = _stmt()
    assert stmt["owner_instance_id"] == OWNER
    verified = _verify(stmt)
    assert verified.owner_instance_id == OWNER
    assert verified.authority_pk_hex == SPACE_PK
    assert verified.issued_at == NOW


@pytest.mark.parametrize(
    "over",
    [
        {"space_id": "sp-2"},  # another space
        {"owner_instance_id": derive_instance_id(b"\x01" * 32)},  # another owner
        {"authority_pk_hex": "ab" * 32},  # binds another key
    ],
)
def test_binding_mismatch_is_rejected(over) -> None:
    with pytest.raises(InvalidOwnerStatement):
        _verify(_stmt(), **over)


def test_a_non_owner_cannot_sign_for_the_owner() -> None:
    forged = _stmt(owner_seed=b"\x52" * 32)
    forged["owner_instance_id"] = OWNER  # claim the owner's id
    with pytest.raises(InvalidOwnerStatement):
        _verify(forged)
    forged = _stmt(owner_seed=b"\x52" * 32)
    forged["owner_instance_id"] = OWNER
    forged["owner_pk"] = ed25519_public_key(OWNER_SEED).hex()
    with pytest.raises(InvalidOwnerStatement):
        _verify(forged)


@pytest.mark.parametrize(
    "field, value",
    [
        ("issued_at", NOW + 1),
        ("authority_key_epoch", 1),
        ("owner_statement_sig", "AAAA"),
    ],
)
def test_tampering_breaks_the_signature(field, value) -> None:
    stmt = _stmt()
    stmt[field] = value
    with pytest.raises(InvalidOwnerStatement):
        _verify(stmt)


@pytest.mark.parametrize("suite", ["future", None])
def test_unknown_suite(suite) -> None:
    stmt = _stmt()
    stmt["owner_statement_sig_suite"] = suite
    with pytest.raises(UnsupportedOwnerStatementSuite):
        _verify(stmt)


def test_far_future_issued_at_is_rejected_and_never_signed() -> None:
    too_far = NOW + MAX_STATEMENT_FUTURE_SKEW_MS + 1
    far = _stmt(issued_at_ms=too_far, now_ms=too_far)
    with pytest.raises(InvalidOwnerStatement):
        _verify(far)
    _verify(_stmt(issued_at_ms=NOW + MAX_STATEMENT_FUTURE_SKEW_MS))
    with pytest.raises(ValueError):
        sign_owner_statement(
            space_id="sp-1",
            owner_seed=OWNER_SEED,
            authority_pk_hex=SPACE_PK,
            authority_key_epoch=0,
            issued_at_ms=2**63,
        )
    with pytest.raises(ValueError):
        _stmt(issued_at_ms=too_far)


@pytest.mark.parametrize(
    "bad", [None, "x", {}, {**_stmt(), "issued_at": "1"}, {**_stmt(), "owner_pk": 5}]
)
def test_malformed(bad) -> None:
    with pytest.raises(ValueError):
        _verify(bad)


# ── Inviter proof: the inviting household holds the space seed ───────────


def _proof(**over) -> dict:
    kw = {
        "space_id": "sp-1",
        "owner_instance_id": OWNER,
        "inviter_instance_id": "admin-hh",
        "space_seed": SPACE_KP.private_key,
    }
    kw.update(over)
    return sign_inviter_proof(**kw)


def _vproof(proof, **over) -> bool:
    kw = {
        "space_id": "sp-1",
        "owner_instance_id": OWNER,
        "inviter_instance_id": "admin-hh",
        "authority_pk_hex": SPACE_PK,
    }
    kw.update(over)
    return verify_inviter_proof(proof, **kw)


def test_inviter_proof_round_trip() -> None:
    assert _vproof(_proof()) is True


@pytest.mark.parametrize(
    "over",
    [
        {"space_id": "sp-2"},
        {"owner_instance_id": "someone"},
        {"inviter_instance_id": "another-hh"},  # replayed by another household
        {"authority_pk_hex": "ab" * 32},
    ],
)
def test_inviter_proof_is_bound(over) -> None:
    assert _vproof(_proof(), **over) is False


def test_inviter_proof_needs_the_seed() -> None:
    assert _vproof(_proof(space_seed=b"\x53" * 32)) is False


@pytest.mark.parametrize("bad", [None, "x", {}, {"authority_sig": 5}])
def test_inviter_proof_malformed(bad) -> None:
    assert _vproof(bad) is False


def test_inviter_proof_unknown_suite_is_false() -> None:
    proof = _proof()
    proof["authority_sig_suite"] = "future"
    assert _vproof(proof) is False
