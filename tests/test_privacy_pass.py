"""RFC 9474 / 9577 / 9578 conformance of :mod:`socialhome.privacy_pass`.

The vectors are copied verbatim from RFC 9474 Appendix A.3
(RSABSSA-SHA384-PSS-Deterministic) and RFC 9578 Appendix A.2 (issuance
protocol 2, test vector 1) into ``tests/fixtures/privacy_pass_vectors.json``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_private_key,
)

from socialhome import privacy_pass as pp

_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "privacy_pass_vectors.json").read_text()
)
A3 = {k: bytes.fromhex(v) for k, v in _VECTORS["rfc9474_a3"].items()}
V2 = {k: bytes.fromhex(v) for k, v in _VECTORS["rfc9578_a2"].items()}


def _int(b: bytes) -> int:
    return int.from_bytes(b, "big")


@pytest.fixture(scope="module")
def a3_key() -> rsa.RSAPrivateKey:
    p, q, n, e, d = (_int(A3[k]) for k in ("p", "q", "n", "e", "d"))
    pub = rsa.RSAPublicNumbers(e, n)
    return rsa.RSAPrivateNumbers(
        p=p,
        q=q,
        d=d,
        dmp1=rsa.rsa_crt_dmp1(d, p),
        dmq1=rsa.rsa_crt_dmq1(d, q),
        iqmp=rsa.rsa_crt_iqmp(p, q),
        public_numbers=pub,
    ).private_key()


@pytest.fixture(scope="module")
def v2_key() -> rsa.RSAPrivateKey:
    key = load_pem_private_key(V2["skI"], password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    return key


# ── RFC 9474 A.3 ─────────────────────────────────────────────────────────


def test_rfc9474_a3_encode(a3_key):
    n = _int(A3["n"])
    assert A3["prepared_msg"] == A3["msg"]  # identity preparation
    assert (
        pp.emsa_pss_encode(A3["msg"], n.bit_length() - 1, A3["salt"])
        == A3["encoded_msg"]
    )


def test_rfc9474_a3_blind_sign_finalize(a3_key):
    n = _int(A3["n"])
    inv = _int(A3["inv"])
    r = pow(inv, -1, n)
    blinded, got_inv = pp.blind(a3_key.public_key(), A3["msg"], salt=A3["salt"], r=r)
    assert blinded == A3["blinded_msg"]
    assert got_inv == inv
    blind_sig = pp.blind_sign(a3_key, blinded)
    assert blind_sig == A3["blind_sig"]
    sig = pp.finalize(a3_key.public_key(), A3["msg"], blind_sig, inv)
    assert sig == A3["sig"]
    assert pp.verify(a3_key.public_key(), A3["msg"], A3["sig"])


def test_rfc9474_finalize_refuses_a_wrong_blind_sig(a3_key):
    inv = _int(A3["inv"])
    bad = bytearray(A3["blind_sig"])
    bad[-1] ^= 1
    with pytest.raises(pp.InvalidToken):
        pp.finalize(a3_key.public_key(), A3["msg"], bytes(bad), inv)
    with pytest.raises(pp.InvalidToken):
        pp.finalize(a3_key.public_key(), A3["msg"], A3["blind_sig"][1:], inv)


def test_blind_sign_refuses_bad_sizes(a3_key):
    with pytest.raises(pp.InvalidToken):
        pp.blind_sign(a3_key, b"\x00" * 10)
    with pytest.raises(pp.InvalidToken):
        pp.blind_sign(a3_key, b"\xff" * 512)  # ≥ n


# ── RFC 9578 A.2 ─────────────────────────────────────────────────────────


def test_rfc9578_key_encoding_and_id(v2_key):
    pub = v2_key.public_key()
    assert pp.encode_public_key(pub) == V2["pkI"]
    assert pp.decode_public_key(V2["pkI"]).public_numbers() == pub.public_numbers()
    assert pp.token_key_id(pub) == hashlib.sha256(V2["pkI"]).digest()


def test_rfc9578_issuance_round_trip(v2_key):
    pub = v2_key.public_key()
    request, pending = pp.token_request(
        pub,
        V2["token_challenge"],
        nonce=V2["nonce"],
        salt=V2["salt"],
        r=_int(V2["blind"]),
    )
    assert request == V2["token_request"]
    truncated, blinded = pp.parse_token_request(request)
    assert truncated == pp.token_key_id(pub)[-1]
    response = pp.blind_sign(v2_key, blinded)
    assert response == V2["token_response"]
    token = pp.finalize_token(pub, pending, response)
    assert token.encode() == V2["token"]
    decoded = pp.Token.decode(V2["token"])
    assert decoded.verify(pub)
    assert decoded.challenge_digest == hashlib.sha256(V2["token_challenge"]).digest()


def test_rfc9577_challenge_encoding():
    # The vector's challenge: type 2, issuer "issuer.example", a 32-byte
    # redemption context, origin "origin.example".
    raw = V2["token_challenge"]
    ctx = raw[2 + 2 + 14 + 1 : 2 + 2 + 14 + 1 + 32]
    assert (
        pp.token_challenge(
            b"issuer.example", redemption_context=ctx, origin_info=b"origin.example"
        )
        == raw
    )
    with pytest.raises(pp.InvalidToken):
        pp.token_challenge(b"x", redemption_context=b"short")
    with pytest.raises(pp.InvalidToken):
        pp.token_challenge(b"")


# ── Refusals ─────────────────────────────────────────────────────────────


def test_forged_and_foreign_tokens_do_not_verify(v2_key):
    pub = v2_key.public_key()
    token = pp.Token.decode(V2["token"])
    forged = pp.Token(
        token.nonce, token.challenge_digest, token.token_key_id, b"\x01" * pp.NK
    )
    assert not forged.verify(pub)
    other = pp.Token(
        b"\x02" * 32, token.challenge_digest, token.token_key_id, token.authenticator
    )
    assert not other.verify(pub)
    assert not token.verify(pp.generate_issuer_key().public_key())


def test_token_decode_refuses_bad_shapes():
    with pytest.raises(pp.InvalidToken):
        pp.Token.decode(V2["token"][:-1])
    with pytest.raises(pp.InvalidToken):
        pp.Token.decode(b"\x00\x01" + V2["token"][2:])
    with pytest.raises(pp.InvalidToken):
        pp.parse_token_request(V2["token_request"][:-1])
    with pytest.raises(pp.InvalidToken):
        pp.parse_token_request(b"\x00\x01" + V2["token_request"][2:])


def test_decode_public_key_refuses_other_encodings(v2_key):
    plain = v2_key.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    with pytest.raises(pp.InvalidToken):
        pp.decode_public_key(plain)  # rsaEncryption, not RSASSA-PSS
    with pytest.raises(pp.InvalidToken):
        pp.decode_public_key(b"junk")
    small = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    with pytest.raises(pp.InvalidToken):
        pp.decode_public_key(pp.encode_public_key(small.public_key()))


def test_suite_validation_has_no_fallback():
    pp.validate_ticket_suite(pp.TICKET_SUITE_PP_BLINDRSA)
    for bad in (None, "", "privacypass-voprf", 2):
        with pytest.raises(pp.UnsupportedTicketSuite):
            pp.validate_ticket_suite(bad)


def test_fresh_issuance_round_trip():
    key = pp.generate_issuer_key()
    pub = key.public_key()
    challenge = pp.token_challenge(b"gfs")
    request, pending = pp.token_request(pub, challenge)
    _t, blinded = pp.parse_token_request(request)
    token = pp.finalize_token(pub, pending, pp.blind_sign(key, blinded))
    assert pp.Token.decode(token.encode()).verify(pub)
    assert "redacted" in repr(pending)
