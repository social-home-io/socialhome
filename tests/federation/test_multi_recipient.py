"""Tests for :mod:`socialhome.federation.multi_recipient` (v_90).

Real crypto end to end: the key derivation, the signed inner envelope, the
outer seal and every refusal a receiver must make.
"""

from __future__ import annotations

import os

import orjson
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    generate_identity_keypair,
)
from socialhome.domain.gfs_mailbox import MULTI_MAX_RECIPIENTS
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.gfs_relay_transport import (
    RELAY_SIZE_BUCKETS,
    is_relay_envelope_body,
)
from socialhome.federation.multi_recipient import (
    MR_SUITE_AESGCM_256,
    MULTI_TO_INSTANCE,
    SUPPORTED_MR_SUITES,
    UnsupportedMrSuite,
    build_multi_envelope,
    decrypt_mr_payload,
    derive_mr_key,
    is_multi_sealed,
    is_valid_kid,
    multi_audience_includes,
    open_multi,
    recipient_tag,
    seal_multi,
)

CONTENT_KEY = b"\x11" * 32
RECIPIENTS = ("b" * 32, "c" * 32)


def _encoder() -> tuple[FederationEncoder, bytes]:
    kp = generate_identity_keypair()
    return FederationEncoder(kp.private_key), kp.public_key


def _envelope(payload: str = '{"text":"hi"}') -> tuple[dict, bytes, bytes]:
    encoder, pk = _encoder()
    _, key = derive_mr_key(CONTENT_KEY)
    env = build_multi_envelope(
        encoder=encoder,
        own_instance_id="a" * 32,
        event_type="space_post_created",
        payload_json=payload,
        space_id="sp-1",
        key=key,
        recipients=RECIPIENTS,
    )
    return env, key, pk


def test_suite_constants():
    assert MR_SUITE_AESGCM_256 == "mr-aesgcm-256"
    assert frozenset({MR_SUITE_AESGCM_256}) == SUPPORTED_MR_SUITES
    assert MULTI_TO_INSTANCE == "*"
    assert issubclass(UnsupportedMrSuite, ValueError)


def test_kid_is_stable_per_key_and_differs_between_keys():
    kid_a, key_a = derive_mr_key(CONTENT_KEY)
    kid_a2, key_a2 = derive_mr_key(CONTENT_KEY)
    kid_b, key_b = derive_mr_key(b"\x22" * 32)
    assert (kid_a, key_a) == (kid_a2, key_a2)
    assert kid_a != kid_b and key_a != key_b
    assert len(b64url_decode(kid_a)) == 16
    assert len(key_a) == 32
    # Neither is the content key itself.
    assert key_a != CONTENT_KEY
    assert b64url_decode(kid_a) not in CONTENT_KEY


def test_the_inner_envelope_is_signed_in_the_canonical_order():
    env, key, pk = _envelope()
    assert env["to_instance"].startswith(MULTI_TO_INSTANCE + ":")
    assert env["sig_suite"] == "ed25519"
    assert env["space_id"] == "sp-1"
    assert list(env) == [
        "msg_id",
        "event_type",
        "from_instance",
        "to_instance",
        "timestamp",
        "encrypted_payload",
        "space_id",
        "proto_version",
        "sig_suite",
        "signatures",
    ]
    signed = orjson.dumps({k: v for k, v in env.items() if k != "signatures"})
    encoder, _ = _encoder()
    assert encoder.verify_signatures_all(
        signed,
        suite="ed25519",
        signatures=env["signatures"],
        ed_public_key=pk,
        pq_public_key=None,
    )
    assert decrypt_mr_payload(env["encrypted_payload"], key) == '{"text":"hi"}'


def test_round_trip():
    env, key, _ = _envelope()
    kid, _ = derive_mr_key(CONTENT_KEY)
    sealed = seal_multi(env, kid=kid, key=key)
    assert set(sealed) == {"mr_suite", "kid", "ciphertext"}
    assert sealed["mr_suite"] == MR_SUITE_AESGCM_256
    assert sealed["kid"] == kid
    assert is_multi_sealed(sealed)
    body = open_multi(sealed, key)
    assert is_relay_envelope_body(body)
    assert body["envelope"] == env


def test_the_seal_names_nothing_but_the_kid():
    env, key, _ = _envelope()
    kid, _ = derive_mr_key(CONTENT_KEY)
    wire = orjson.dumps(seal_multi(env, kid=kid, key=key))
    # Long markers only: a short one can turn up in random b64url.
    for secret in (b"a" * 32, b"space_post_created", b'"space_id"'):
        assert secret not in wire


def test_the_plaintext_is_padded_to_a_bucket():
    kid, key = derive_mr_key(CONTENT_KEY)
    sizes = set()
    for n in (1, 200, 3000):
        env, _, _ = _envelope('{"t":"' + "x" * n + '"}')
        sealed = seal_multi(env, kid=kid, key=key)
        raw = b64url_decode(sealed["ciphertext"])
        bucket = len(raw) - 12 - 16
        assert bucket in RELAY_SIZE_BUCKETS
        sizes.add(bucket)
    assert len(sizes) >= 2


def test_an_oversize_envelope_is_refused():
    kid, key = derive_mr_key(CONTENT_KEY)
    env, _, _ = _envelope('{"t":"' + "x" * (RELAY_SIZE_BUCKETS[-1] + 10) + '"}')
    with pytest.raises(ValueError, match="bucket"):
        seal_multi(env, kid=kid, key=key)


def test_a_tampered_ciphertext_does_not_open():
    env, key, _ = _envelope()
    kid, _ = derive_mr_key(CONTENT_KEY)
    sealed = seal_multi(env, kid=kid, key=key)
    raw = bytearray(b64url_decode(sealed["ciphertext"]))
    raw[20] ^= 0x01
    with pytest.raises(ValueError):
        open_multi({**sealed, "ciphertext": b64url_encode(bytes(raw))}, key)


def test_a_relabelled_kid_does_not_open():
    """The kid is bound as associated data."""
    env, key, _ = _envelope()
    kid, _ = derive_mr_key(CONTENT_KEY)
    other_kid, _ = derive_mr_key(b"\x33" * 32)
    sealed = seal_multi(env, kid=kid, key=key)
    with pytest.raises(ValueError):
        open_multi({**sealed, "kid": other_kid}, key)


def test_the_wrong_key_does_not_open():
    env, key, _ = _envelope()
    kid, _ = derive_mr_key(CONTENT_KEY)
    sealed = seal_multi(env, kid=kid, key=key)
    _, other = derive_mr_key(b"\x44" * 32)
    with pytest.raises(ValueError):
        open_multi(sealed, other)
    with pytest.raises(ValueError):
        decrypt_mr_payload(env["encrypted_payload"], other)


def test_an_unknown_suite_is_refused_before_anything_else():
    env, key, _ = _envelope()
    kid, _ = derive_mr_key(CONTENT_KEY)
    sealed = seal_multi(env, kid=kid, key=key)
    with pytest.raises(UnsupportedMrSuite):
        open_multi({**sealed, "mr_suite": "mr-chacha"}, key)
    # Even with garbage elsewhere, the suite is judged first.
    with pytest.raises(UnsupportedMrSuite):
        open_multi({"mr_suite": "nope", "kid": 1, "ciphertext": None}, key)
    with pytest.raises(UnsupportedMrSuite):
        open_multi({"kid": kid, "ciphertext": sealed["ciphertext"]}, key)


@pytest.mark.parametrize(
    "sealed",
    [
        {"mr_suite": MR_SUITE_AESGCM_256, "kid": 5, "ciphertext": "AA"},
        {"mr_suite": MR_SUITE_AESGCM_256, "kid": "k", "ciphertext": ""},
        {"mr_suite": MR_SUITE_AESGCM_256, "kid": "k", "ciphertext": "!!!"},
        {"mr_suite": MR_SUITE_AESGCM_256, "kid": "k", "ciphertext": "AAAA"},
        {"mr_suite": MR_SUITE_AESGCM_256, "kid": "k", "ciphertext": "A" * 400_000},
    ],
)
def test_malformed_seals_are_refused(sealed):
    with pytest.raises(ValueError):
        open_multi(sealed, b"\x00" * 32)


def test_a_seal_whose_plaintext_is_not_an_object_is_refused():
    kid, key = derive_mr_key(CONTENT_KEY)
    nonce = os.urandom(12)
    aad = b"sh-mr-seal:v1\n" + MR_SUITE_AESGCM_256.encode() + b"\n" + kid.encode()
    ct = AESGCM(key).encrypt(nonce, b"[1, 2]", aad)
    sealed = {
        "mr_suite": MR_SUITE_AESGCM_256,
        "kid": kid,
        "ciphertext": b64url_encode(nonce + ct),
    }
    with pytest.raises(ValueError, match="object"):
        open_multi(sealed, key)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "x",
        {"kem_suite": "x25519", "eph_pk": "A", "ciphertext": "A"},
        {"mr_suite": MR_SUITE_AESGCM_256},
    ],
)
def test_is_multi_sealed_only_for_an_mr_shape(value):
    assert is_multi_sealed(value) is (isinstance(value, dict) and "mr_suite" in value)


def test_decrypt_mr_payload_refuses_a_malformed_string():
    with pytest.raises(ValueError):
        decrypt_mr_payload("no-colon", b"\x00" * 32)


# ─── Audience binding ────────────────────────────────────────────────────


def test_recipient_tag_is_a_6_char_hmac_of_the_instance_id():
    _, key = derive_mr_key(CONTENT_KEY)
    tag = recipient_tag(key, "b" * 32)
    assert len(tag) == 6
    assert len(b64url_decode(tag)) == 4
    assert tag == recipient_tag(key, "b" * 32)
    assert tag != recipient_tag(key, "c" * 32)
    _, other = derive_mr_key(b"\x22" * 32)
    assert tag != recipient_tag(other, "b" * 32)


def test_the_envelope_names_one_sorted_tag_per_recipient():
    env, key, _ = _envelope()
    tags = sorted(recipient_tag(key, r) for r in RECIPIENTS)
    assert env["to_instance"] == "*:" + ".".join(tags)
    # The instance ids themselves never appear.
    for r in RECIPIENTS:
        assert r not in env["to_instance"]


def test_the_audience_includes_the_listed_recipients_only():
    env, key, _ = _envelope()
    for r in RECIPIENTS:
        assert multi_audience_includes(env["to_instance"], key, r)
    assert not multi_audience_includes(env["to_instance"], key, "d" * 32)
    _, other = derive_mr_key(b"\x22" * 32)
    assert not multi_audience_includes(env["to_instance"], other, RECIPIENTS[0])


def _to(*tags: str) -> str:
    return "*:" + ".".join(tags)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda good: None,
        lambda good: 5,
        lambda good: "*",
        lambda good: "*:",
        lambda good: good[1:],
        lambda good: "*;" + good[2:],
        lambda good: good + ".",
        lambda good: good + ".short",
        lambda good: good + ".!!!!!!!!!!!",
        lambda good: good + "." + good[2:],  # duplicate tag
        lambda good: _to(*reversed(good[2:].split("."))),  # unsorted
        lambda good: _to(*(["A" * 6] * 0), *[f"{i:06d}" for i in range(501)]),
    ],
)
def test_a_malformed_audience_is_refused(mangle):
    env, key, _ = _envelope()
    good = env["to_instance"]
    assert multi_audience_includes(good, key, RECIPIENTS[0])
    assert not multi_audience_includes(mangle(good), key, RECIPIENTS[0])


@pytest.mark.parametrize("recipients", [(), tuple(f"{i:032d}" for i in range(501))])
def test_build_refuses_an_empty_or_oversize_audience(recipients):
    encoder, _ = _encoder()
    _, key = derive_mr_key(CONTENT_KEY)
    with pytest.raises(ValueError, match="recipient"):
        build_multi_envelope(
            encoder=encoder,
            own_instance_id="a" * 32,
            event_type="space_post_created",
            payload_json="{}",
            space_id="sp-1",
            key=key,
            recipients=recipients,
        )


def test_the_largest_audience_still_seals():
    encoder, _ = _encoder()
    kid, key = derive_mr_key(CONTENT_KEY)
    recipients = [f"{i:032d}" for i in range(MULTI_MAX_RECIPIENTS)]
    env = build_multi_envelope(
        encoder=encoder,
        own_instance_id="a" * 32,
        event_type="space_post_created",
        payload_json="{}",
        space_id="sp-1",
        key=key,
        recipients=recipients,
    )
    assert multi_audience_includes(env["to_instance"], key, recipients[-1])
    assert open_multi(seal_multi(env, kid=kid, key=key), key)["envelope"] == env


@pytest.mark.parametrize(
    "kid", [None, 5, "", "k", "A" * 21, "A" * 23, "A" * 21 + "=", "A" * 21 + "!"]
)
def test_is_valid_kid_refuses_any_other_shape(kid):
    assert not is_valid_kid(kid)


def test_is_valid_kid_takes_a_derived_kid():
    kid, _ = derive_mr_key(CONTENT_KEY)
    assert is_valid_kid(kid)
