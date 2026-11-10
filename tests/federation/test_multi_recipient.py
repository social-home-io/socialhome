"""Tests for :mod:`socialhome.federation.multi_recipient` (v_91).

Real crypto end to end: the per-recipient wrap of the body key, the signed
inner envelope, the body seal and every refusal a receiver must make.
"""

from __future__ import annotations

import hashlib
import os

import orjson
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.domain.gfs_mailbox import MULTI_MAX_RECIPIENTS
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.gfs_relay_transport import (
    RELAY_SIZE_BUCKETS,
    is_relay_envelope_body,
)
from socialhome.federation.keywrap_seal import UnsupportedKemSuite, seal_to_keywrap
from socialhome.federation.multi_recipient import (
    MR_SUITE_WRAP_AESGCM_256,
    MULTI_TO_INSTANCE,
    SUPPORTED_MR_SUITES,
    UnsupportedMrSuite,
    build_multi_envelope,
    is_multi_sealed,
    multi_audience_includes,
    new_mr_key,
    open_multi,
    recipient_tag,
    seal_multi,
    wrap_mr_key,
)

RECIPIENTS = ("b" * 32, "c" * 32)
MAILBOX = generate_x25519_keypair()


def _encoder() -> tuple[FederationEncoder, bytes]:
    kp = generate_identity_keypair()
    return FederationEncoder(kp.private_key), kp.public_key


def _envelope(
    payload: str = '{"text":"hi"}', key: bytes | None = None
) -> tuple[dict, bytes, bytes]:
    encoder, pk = _encoder()
    key = key or new_mr_key()
    env = build_multi_envelope(
        encoder=encoder,
        own_instance_id="a" * 32,
        event_type="space_post_created",
        payload_json=payload,
        space_id="sp-1",
        recipients=RECIPIENTS,
        msg_id="m-1",
    )
    return env, key, pk


def _frame(env: dict, key: bytes, kem_pk: bytes = MAILBOX.public_key) -> dict:
    """What the GFS pushes to one mailbox: the body plus that mailbox's wrap."""
    return {**seal_multi(env, key=key), "wrap": wrap_mr_key(key, kem_pk)}


def test_suite_constants():
    assert MR_SUITE_WRAP_AESGCM_256 == "mr-wrap-aesgcm-256"
    assert frozenset({MR_SUITE_WRAP_AESGCM_256}) == SUPPORTED_MR_SUITES
    assert MULTI_TO_INSTANCE == "*"
    assert issubclass(UnsupportedMrSuite, ValueError)


def test_each_upload_gets_a_fresh_key():
    assert len(new_mr_key()) == 32
    assert new_mr_key() != new_mr_key()


def test_the_inner_envelope_is_signed_in_the_canonical_order():
    env, _, pk = _envelope()
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
    # As-is: the whole envelope rides inside the body sealed under K.
    assert env["encrypted_payload"] == '{"text":"hi"}'


def test_round_trip_through_the_mailbox_key():
    env, key, _ = _envelope()
    body = seal_multi(env, key=key)
    assert set(body) == {"mr_suite", "ciphertext"}
    assert body["mr_suite"] == MR_SUITE_WRAP_AESGCM_256
    wrap = wrap_mr_key(key, MAILBOX.public_key)
    assert set(wrap) == {"kem_suite", "eph_pk", "ciphertext"}
    sealed = {**body, "wrap": wrap}
    assert is_multi_sealed(sealed)
    opened = open_multi(sealed, MAILBOX.private_key)
    assert is_relay_envelope_body(opened)
    assert opened["envelope"] == env


def test_every_recipient_opens_the_same_body_with_its_own_key():
    env, key, _ = _envelope()
    body = seal_multi(env, key=key)
    for mailbox in (generate_x25519_keypair(), generate_x25519_keypair()):
        sealed = {**body, "wrap": wrap_mr_key(key, mailbox.public_key)}
        assert open_multi(sealed, mailbox.private_key)["envelope"] == env


def test_another_mailboxs_key_does_not_open_the_wrap():
    env, key, _ = _envelope()
    other = generate_x25519_keypair()
    with pytest.raises(ValueError, match="wrap"):
        open_multi(_frame(env, key), other.private_key)


def test_the_wire_names_nothing():
    env, key, _ = _envelope()
    wire = orjson.dumps(_frame(env, key))
    # Long markers only: a short one can turn up in random b64url.
    for secret in (b"a" * 32, b"space_post_created", b'"space_id"', b"sp-1"):
        assert secret not in wire


def test_the_plaintext_is_padded_to_a_bucket():
    key = new_mr_key()
    sizes = set()
    for n in (1, 200, 6000):
        env, _, _ = _envelope('{"t":"' + "x" * n + '"}', key)
        raw = b64url_decode(seal_multi(env, key=key)["ciphertext"])
        bucket = len(raw) - 12 - 16
        assert bucket in RELAY_SIZE_BUCKETS
        sizes.add(bucket)
    assert len(sizes) >= 2


def test_an_oversize_envelope_is_refused():
    env, key, _ = _envelope('{"t":"' + "x" * (RELAY_SIZE_BUCKETS[-1] + 10) + '"}')
    with pytest.raises(ValueError, match="bucket"):
        seal_multi(env, key=key)


def test_a_tampered_body_does_not_open():
    env, key, _ = _envelope()
    sealed = _frame(env, key)
    raw = bytearray(b64url_decode(sealed["ciphertext"]))
    raw[20] ^= 0x01
    with pytest.raises(ValueError):
        open_multi(
            {**sealed, "ciphertext": b64url_encode(bytes(raw))}, MAILBOX.private_key
        )


def test_a_body_under_another_key_does_not_open():
    """A wrap of K never opens a body sealed under some other key."""
    env, key, _ = _envelope()
    sealed = {
        **seal_multi(env, key=new_mr_key()),
        "wrap": wrap_mr_key(key, MAILBOX.public_key),
    }
    with pytest.raises(ValueError, match="body"):
        open_multi(sealed, MAILBOX.private_key)


def test_an_unknown_suite_is_refused_before_anything_else():
    env, key, _ = _envelope()
    sealed = _frame(env, key)
    with pytest.raises(UnsupportedMrSuite):
        open_multi({**sealed, "mr_suite": "mr-aesgcm-256"}, MAILBOX.private_key)
    # Even with garbage elsewhere, the suite is judged first.
    with pytest.raises(UnsupportedMrSuite):
        open_multi({"mr_suite": "nope", "wrap": 1, "ciphertext": None}, b"")
    with pytest.raises(UnsupportedMrSuite):
        open_multi({k: v for k, v in sealed.items() if k != "mr_suite"}, b"")


def test_an_unknown_wrap_suite_is_refused():
    env, key, _ = _envelope()
    sealed = _frame(env, key)
    for suite in ("x25519+mlkem768", None):
        wrap = {**sealed["wrap"], "kem_suite": suite}
        with pytest.raises(UnsupportedKemSuite):
            open_multi({**sealed, "wrap": wrap}, MAILBOX.private_key)


def test_a_wrap_that_holds_no_32_byte_key_is_refused():
    env, key, _ = _envelope()
    wrap = seal_to_keywrap(recipient_keywrap_pub=MAILBOX.public_key, plaintext=key[:16])
    with pytest.raises(ValueError, match="32-byte"):
        open_multi({**seal_multi(env, key=key), "wrap": wrap}, MAILBOX.private_key)


@pytest.mark.parametrize(
    "sealed",
    [
        {"mr_suite": MR_SUITE_WRAP_AESGCM_256, "ciphertext": "AA"},
        {"mr_suite": MR_SUITE_WRAP_AESGCM_256, "wrap": "x", "ciphertext": "AA"},
        {"mr_suite": MR_SUITE_WRAP_AESGCM_256, "wrap": {}, "ciphertext": ""},
        {"mr_suite": MR_SUITE_WRAP_AESGCM_256, "wrap": {}, "ciphertext": "A" * 400_000},
        {
            "mr_suite": MR_SUITE_WRAP_AESGCM_256,
            "wrap": {"kem_suite": "x25519"},
            "ciphertext": "AA",
        },
    ],
)
def test_malformed_frames_are_refused(sealed):
    with pytest.raises(ValueError):
        open_multi(sealed, MAILBOX.private_key)


@pytest.mark.parametrize("ciphertext", ["!!!", "AAAA"])
def test_a_malformed_body_ciphertext_is_refused(ciphertext):
    key = new_mr_key()
    sealed = {
        "mr_suite": MR_SUITE_WRAP_AESGCM_256,
        "ciphertext": ciphertext,
        "wrap": wrap_mr_key(key, MAILBOX.public_key),
    }
    with pytest.raises(ValueError):
        open_multi(sealed, MAILBOX.private_key)


def test_a_body_whose_plaintext_is_not_an_object_is_refused():
    key = new_mr_key()
    nonce = os.urandom(12)
    aad = b"sh-mr-body:v1\n" + MR_SUITE_WRAP_AESGCM_256.encode()
    ct = AESGCM(key).encrypt(nonce, b"[1, 2]", aad)
    sealed = {
        "mr_suite": MR_SUITE_WRAP_AESGCM_256,
        "ciphertext": b64url_encode(nonce + ct),
        "wrap": wrap_mr_key(key, MAILBOX.public_key),
    }
    with pytest.raises(ValueError, match="object"):
        open_multi(sealed, MAILBOX.private_key)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "x",
        {"kem_suite": "x25519", "eph_pk": "A", "ciphertext": "A"},
        {"mr_suite": MR_SUITE_WRAP_AESGCM_256},
    ],
)
def test_is_multi_sealed_only_for_an_mr_shape(value):
    assert is_multi_sealed(value) is (isinstance(value, dict) and "mr_suite" in value)


# ─── Audience binding ────────────────────────────────────────────────────


def test_recipient_tag_is_a_6_char_unkeyed_sha256_of_the_instance_id():
    tag = recipient_tag("b" * 32)
    assert len(tag) == 6
    assert tag == b64url_encode(
        hashlib.sha256(b"sh-mr-to:v1\n" + b"b" * 32).digest()[:4]
    )
    assert tag != recipient_tag("c" * 32)


def test_the_envelope_names_one_sorted_tag_per_recipient():
    env, _key, _ = _envelope()
    tags = sorted(recipient_tag(r) for r in RECIPIENTS)
    assert env["to_instance"] == "*:" + ".".join(tags)
    # The instance ids themselves never appear.
    for r in RECIPIENTS:
        assert r not in env["to_instance"]


def test_the_audience_includes_the_listed_recipients_only():
    env, _key, _ = _envelope()
    for r in RECIPIENTS:
        assert multi_audience_includes(env["to_instance"], r)
    assert not multi_audience_includes(env["to_instance"], "d" * 32)


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
        lambda good: _to(*[f"{i:06d}" for i in range(501)]),
    ],
)
def test_a_malformed_audience_is_refused(mangle):
    env, _key, _ = _envelope()
    good = env["to_instance"]
    assert multi_audience_includes(good, RECIPIENTS[0])
    assert not multi_audience_includes(mangle(good), RECIPIENTS[0])


@pytest.mark.parametrize("recipients", [(), tuple(f"{i:032d}" for i in range(501))])
def test_build_refuses_an_empty_or_oversize_audience(recipients):
    encoder, _ = _encoder()
    with pytest.raises(ValueError, match="recipient"):
        build_multi_envelope(
            encoder=encoder,
            own_instance_id="a" * 32,
            event_type="space_post_created",
            payload_json="{}",
            space_id="sp-1",
            recipients=recipients,
            msg_id="m-1",
        )


def test_the_largest_audience_still_seals():
    encoder, _ = _encoder()
    key = new_mr_key()
    recipients = [f"{i:032d}" for i in range(MULTI_MAX_RECIPIENTS)]
    env = build_multi_envelope(
        encoder=encoder,
        own_instance_id="a" * 32,
        event_type="space_post_created",
        payload_json="{}",
        space_id="sp-1",
        recipients=recipients,
        msg_id="m-1",
    )
    assert multi_audience_includes(env["to_instance"], recipients[-1])
    assert open_multi(_frame(env, key), MAILBOX.private_key)["envelope"] == env
