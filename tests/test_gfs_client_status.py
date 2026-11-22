"""Tests for :mod:`socialhome.gfs_client_status` — a pending household asks
its GFS whether the operator approved it.

The household signs its question for one GFS at a coarse time (only the key
holder may learn its own status there — no registration oracle, the question
can't be replayed at another GFS, and a captured one expires); the GFS signs
its answer with its identity key, bound to the household's fresh nonce and
the WHOLE question (id, signature, suite, time), so nobody can fake an
approval, replay an old answer or tamper with the question.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from socialhome.capabilities_sig import CAPS_SIGNING_PREFIX
from socialhome.crypto import b64url_encode, generate_identity_keypair
from socialhome.gfs_client_status import (
    CLIENT_STATUS_REQUEST_MAX_AGE_S,
    CLIENT_STATUS_REQUEST_MAX_FUTURE_S,
    CLIENT_STATUS_REQUEST_PREFIX,
    CLIENT_STATUS_SIG_SUITE_ED25519,
    CLIENT_STATUS_SIGNING_PREFIX,
    CLIENT_STATUSES,
    SUPPORTED_CLIENT_STATUS_SIG_SUITES,
    StatusRequest,
    UnsupportedClientStatusSigSuite,
    client_status_signing_bytes,
    parse_status_request,
    sign_client_status,
    sign_status_request,
    status_request_signing_bytes,
    status_request_ts,
    verify_client_status,
    verify_status_request,
)
from socialhome.gfs_proof import PROOF_SIGNING_PREFIX, new_proof_nonce

HOME = "home-alpha"
GFS = generate_identity_keypair()
GFS_PK = GFS.public_key.hex()
NOW = 1_800_000_000.0
TS = status_request_ts(NOW)
Q = StatusRequest(
    instance_id=HOME,
    sig=b64url_encode(b"q" * 64),
    suite="ed25519",
    ts=TS,
)


def test_constants():
    assert CLIENT_STATUS_SIG_SUITE_ED25519 == "ed25519"
    assert SUPPORTED_CLIENT_STATUS_SIG_SUITES == frozenset({"ed25519"})
    assert CLIENT_STATUSES == frozenset({"active", "pending", "banned", "unknown"})
    assert issubclass(UnsupportedClientStatusSigSuite, ValueError)
    assert CLIENT_STATUS_REQUEST_MAX_AGE_S == 600
    assert CLIENT_STATUS_REQUEST_MAX_FUTURE_S == 300


def test_ts_is_coarse_minutes():
    """Minute resolution: the question carries no finer clock reading."""
    assert status_request_ts(NOW + 59.9) == status_request_ts(NOW)
    assert status_request_ts(NOW) % 60 == 0
    assert status_request_ts(NOW) <= NOW


def test_prefixes_are_domain_separated():
    """From each other and from everything else the GFS key signs: the
    possession proof and the capability block. None is a prefix of another."""
    prefixes = [
        CLIENT_STATUS_REQUEST_PREFIX,
        CLIENT_STATUS_SIGNING_PREFIX,
        PROOF_SIGNING_PREFIX,
        CAPS_SIGNING_PREFIX,
    ]
    for a in prefixes:
        for b in prefixes:
            if a is not b:
                assert not a.startswith(b)


def test_request_round_trip():
    kp = generate_identity_keypair()
    nonce = new_proof_nonce()
    req = sign_status_request(kp.private_key, GFS_PK, HOME, nonce, ts=TS)
    block = req.to_wire()
    assert set(block) == {"instance_id", "sig", "sig_suite", "ts"}
    assert block["ts"] == TS
    assert parse_status_request(block, now=NOW) == req
    assert verify_status_request(kp.public_key.hex(), GFS_PK, nonce, req)


def test_request_bound_to_gfs_nonce_instance_and_ts():
    """A question can't be replayed at another GFS, with another nonce, for
    another household, or with its time moved."""
    kp = generate_identity_keypair()
    nonce = new_proof_nonce()
    req = sign_status_request(kp.private_key, GFS_PK, HOME, nonce, ts=TS)
    pk = kp.public_key.hex()
    other_gfs = generate_identity_keypair().public_key.hex()
    assert not verify_status_request(pk, other_gfs, nonce, req)
    assert not verify_status_request(pk, GFS_PK, new_proof_nonce(), req)
    assert not verify_status_request(
        pk, GFS_PK, nonce, replace(req, instance_id="home-beta")
    )
    assert not verify_status_request(pk, GFS_PK, nonce, replace(req, ts=TS + 60))
    other = generate_identity_keypair().public_key.hex()
    assert not verify_status_request(other, GFS_PK, nonce, req)


def test_request_unknown_suite_raises():
    kp = generate_identity_keypair()
    nonce = new_proof_nonce()
    req = sign_status_request(kp.private_key, GFS_PK, HOME, nonce, ts=TS)
    with pytest.raises(UnsupportedClientStatusSigSuite):
        verify_status_request(
            kp.public_key.hex(), GFS_PK, nonce, replace(req, suite="rsa")
        )


_DROP = object()


def _block(**over: object) -> dict:
    block: dict = {
        "instance_id": HOME,
        "sig": b64url_encode(b"s" * 64),
        "sig_suite": "ed25519",
        "ts": TS,
    }
    block.update(over)
    return {k: v for k, v in block.items() if v is not _DROP}


@pytest.mark.parametrize(
    "value",
    [
        None,
        "x",
        [],
        {},
        _block(sig_suite=_DROP),
        _block(ts=_DROP),
        _block(instance_id=""),
        _block(instance_id=5),
        _block(instance_id="x" * 257),
        _block(sig=""),
        _block(sig="s" * 200),
        _block(sig_suite="x" * 65),
        # Finding 1 (b): an unsupported suite is refused before anything is
        # signed — never a signed ``unknown``.
        _block(sig_suite="ed448"),
        _block(sig_suite="ED25519"),
        # Finding 5: the signature must decode to exactly 64 bytes.
        _block(sig=b64url_encode(b"s" * 63)),
        _block(sig=b64url_encode(b"s" * 65)),
        _block(sig="!!not-b64!!"),
        # Finding 2: a stale, future or malformed ts.
        _block(ts=TS - CLIENT_STATUS_REQUEST_MAX_AGE_S - 60),
        _block(ts=TS + CLIENT_STATUS_REQUEST_MAX_FUTURE_S + 60),
        _block(ts="1800000000"),
        _block(ts=float(TS)),
        _block(ts=True),
    ],
)
def test_parse_request_rejects_malformed(value):
    assert parse_status_request(value, now=NOW) is None


def test_parse_request_accepts_the_window_edges():
    """Up to the max age back, up to the skew allowance ahead."""
    assert parse_status_request(
        _block(ts=int(NOW) - CLIENT_STATUS_REQUEST_MAX_AGE_S), now=NOW
    )
    assert parse_status_request(
        _block(ts=int(NOW) + CLIENT_STATUS_REQUEST_MAX_FUTURE_S), now=NOW
    )


def test_a_clock_four_minutes_fast_still_learns_its_approval():
    """Re-review residual 1: a household inside the federation pipeline's
    ±300 s skew must be able to ask too, or it stays pending for ever."""
    assert parse_status_request(_block(ts=int(NOW) + 240), now=NOW)


def test_a_captured_question_expires():
    """Finding 2: a question captured on the path is no lasting status
    oracle — past the window the GFS refuses it before signing anything."""
    kp = generate_identity_keypair()
    req = sign_status_request(
        kp.private_key, GFS_PK, HOME, new_proof_nonce(), ts=status_request_ts(NOW)
    )
    later = NOW + CLIENT_STATUS_REQUEST_MAX_AGE_S + 60
    assert parse_status_request(req.to_wire(), now=NOW) == req
    assert parse_status_request(req.to_wire(), now=later) is None


def _answer(status: str, *, nonce: str, key=GFS, request: StatusRequest = Q):
    return sign_client_status(key.private_key, nonce, request, status)


def test_status_round_trip_each_value():
    nonce = new_proof_nonce()
    for status in sorted(CLIENT_STATUSES):
        sig, suite = _answer(status, nonce=nonce)
        assert suite == "ed25519"
        assert verify_client_status(GFS_PK, nonce, Q, status, sig, suite)


def test_status_cannot_be_relabelled():
    """A signed ``pending`` (or ``unknown``) never verifies as ``active``."""
    nonce = new_proof_nonce()
    sig, suite = _answer("pending", nonce=nonce)
    assert not verify_client_status(GFS_PK, nonce, Q, "active", sig, suite)


def test_status_bound_to_nonce_instance_key_and_question():
    """An old answer can't be replayed, another household's answer can't be
    reused, another key's answer is refused — and an answer to a DIFFERENT
    question (one someone on the path swapped in to get ``unknown``) does
    not verify for the household's own question."""
    nonce = new_proof_nonce()
    swapped = replace(Q, sig=b64url_encode(b"w" * 64))
    sig, suite = _answer("unknown", nonce=nonce, request=swapped)
    assert not verify_client_status(GFS_PK, nonce, Q, "unknown", sig, suite)
    sig, suite = _answer("active", nonce=nonce)
    assert not verify_client_status(GFS_PK, new_proof_nonce(), Q, "active", sig, suite)
    assert not verify_client_status(
        GFS_PK, nonce, replace(Q, instance_id="home-beta"), "active", sig, suite
    )
    forged, _ = _answer("active", nonce=nonce, key=generate_identity_keypair())
    assert not verify_client_status(GFS_PK, nonce, Q, "active", forged, suite)


@pytest.mark.parametrize(
    "tampered",
    [
        replace(Q, suite="ed448"),
        replace(Q, suite="ED25519"),
        replace(Q, ts=TS - 60),
    ],
)
def test_an_answer_to_a_tampered_question_never_verifies(tampered):
    """Finding 1: the WHOLE question is bound into the answer — an on-path
    edit of the question's suite (or time) that got the GFS to sign
    ``unknown`` over the household's real id, nonce and signature does not
    verify as the answer to the household's own question."""
    nonce = new_proof_nonce()
    sig, suite = _answer("unknown", nonce=nonce, request=tampered)
    assert not verify_client_status(GFS_PK, nonce, Q, "unknown", sig, suite)


def test_status_verify_never_raises_on_garbage():
    nonce = new_proof_nonce()
    garbage = b64url_encode(b"x" * 64)
    assert not verify_client_status(GFS_PK, nonce, Q, "active", "!!", "ed25519")
    assert not verify_client_status("zz", nonce, Q, "active", garbage, "ed25519")
    assert not verify_client_status(GFS_PK, nonce, Q, "active", "", "ed25519")
    assert not verify_client_status(GFS_PK, nonce, Q, "approved", garbage, "ed25519")


def test_status_unknown_suite_raises():
    nonce = new_proof_nonce()
    sig, _ = _answer("active", nonce=nonce)
    with pytest.raises(UnsupportedClientStatusSigSuite):
        verify_client_status(GFS_PK, nonce, Q, "active", sig, "ed448")


def test_signing_bytes_are_domain_separated_and_bind_the_whole_question():
    nonce = new_proof_nonce()
    req = status_request_signing_bytes("AB" * 32, HOME, nonce, "ed25519", TS)
    ans = client_status_signing_bytes("AB" * 32, nonce, Q, "active", "ed25519")
    assert req.startswith(CLIENT_STATUS_REQUEST_PREFIX)
    assert ans.startswith(CLIENT_STATUS_SIGNING_PREFIX)
    assert b'"suite":"ed25519"' in req and b'"suite":"ed25519"' in ans
    assert f'"ts":{TS}'.encode() in req
    assert b'"request_suite":"ed25519"' in ans
    assert f'"request_ts":{TS}'.encode() in ans
    assert f'"request_sig":"{Q.sig}"'.encode() in ans
    # The key is lower-cased so a spelling difference can't split it.
    assert req == status_request_signing_bytes("ab" * 32, HOME, nonce, "ed25519", TS)
    assert ans == client_status_signing_bytes("ab" * 32, nonce, Q, "active", "ed25519")


def test_sign_client_status_refuses_an_unknown_status():
    with pytest.raises(ValueError):
        sign_client_status(GFS.private_key, new_proof_nonce(), Q, "approved")
