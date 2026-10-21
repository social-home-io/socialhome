"""The public-moment wire crypto: ratchet, item keys, sealed bodies, sealed
keys, signatures (v_62)."""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from socialhome.crypto import b64url_encode, generate_identity_keypair
from socialhome.moment_public_seal import (
    ENVELOPE_FIELDS,
    KEY_SUITE_AESGCM_256,
    MAX_CIPHERTEXT_CHARS,
    MAX_RATCHET_STEPS,
    MOMENT_SEAL_SUITE_AESGCM256,
    RATCHET_STEP_S,
    SUPPORTED_MOMENT_SEAL_SUITES,
    UnsupportedMomentSealSuite,
    is_wire_int,
    item_key,
    key_at_rest_context,
    key_plaintext,
    open_body,
    open_with_item_key,
    parse_key_plaintext,
    ratchet,
    require_known_key_suite,
    require_known_suite,
    seal_body,
    sign_wire,
    step_for,
    verify_wire,
)
from socialhome.services.space_crypto_service import (
    KEY_SUITE_AESGCM_256 as SPACE_KEY_SUITE,
)

KEY = os.urandom(32)
BODY = {"op": "create", "content": "Sunset at the lake"}


def _envelope(
    key: bytes = KEY,
    *,
    author: str = "alice",
    gen: int = 1_700_000_000,
    step: int = 2,
    moment_id: str = "m-lake-1",
) -> dict:
    return {
        "author_user_id": author,
        "instance_id": "inst-a",
        **seal_body(
            step_key=key,
            author_user_id=author,
            key_gen=gen,
            key_step=step,
            moment_id=moment_id,
            body=BODY,
        ),
    }


def test_round_trip_and_no_content_on_the_wire():
    env = _envelope()
    assert set(env) | {"signature"} == ENVELOPE_FIELDS
    assert env["seal_suite"] == MOMENT_SEAL_SUITE_AESGCM256
    assert "Sunset" not in str(env)
    assert open_body(step_key=KEY, envelope=env) == BODY


def test_the_item_key_opens_that_moment_and_no_other():
    """A share link carries one moment's item key: it opens that moment, and
    neither another moment nor the step key can be had from it."""
    env = _envelope()
    assert open_with_item_key(key=item_key(KEY, "m-lake-1"), envelope=env) == BODY
    other = _envelope(moment_id="m-lake-2")
    with pytest.raises(InvalidTag):
        open_with_item_key(key=item_key(KEY, "m-lake-1"), envelope=other)
    assert item_key(KEY, "m-lake-1") != KEY


def test_two_seals_of_one_body_differ():
    assert _envelope()["ciphertext"] != _envelope()["ciphertext"]


def test_wrong_key_fails():
    with pytest.raises(InvalidTag):
        open_body(step_key=os.urandom(32), envelope=_envelope())


@pytest.mark.parametrize(
    ("field", "value"),
    [("author_user_id", "mallory"), ("key_gen", 1), ("key_step", 3)],
)
def test_moving_a_ciphertext_to_another_author_or_key_id_fails(field, value):
    env = {**_envelope(), field: value}
    with pytest.raises(InvalidTag):
        open_with_item_key(key=item_key(KEY, "m-lake-1"), envelope=env)


def test_moving_a_ciphertext_to_another_moment_id_fails():
    env = {**_envelope(), "moment_id": "m-other"}
    with pytest.raises(InvalidTag):
        open_with_item_key(key=item_key(KEY, "m-lake-1"), envelope=env)


@pytest.mark.parametrize("suite", ["aesgcm-128", "", None, "AESGCM-256"])
def test_unknown_suite_is_refused_never_defaulted(suite):
    env = {**_envelope(), "seal_suite": suite}
    with pytest.raises(UnsupportedMomentSealSuite):
        open_body(step_key=KEY, envelope=env)
    with pytest.raises(UnsupportedMomentSealSuite):
        require_known_suite(suite)
    with pytest.raises(UnsupportedMomentSealSuite):
        require_known_key_suite(suite)


def test_suites_are_pinned():
    assert SUPPORTED_MOMENT_SEAL_SUITES == {MOMENT_SEAL_SUITE_AESGCM256}
    # The delivered key wears the space content-key tag.
    assert KEY_SUITE_AESGCM_256 == SPACE_KEY_SUITE


@pytest.mark.parametrize(
    "patch",
    [
        {"author_user_id": ""},
        {"moment_id": ""},
        {"moment_id": "m" * 201},
        {"key_gen": -1},
        {"key_step": True},
        {"key_gen": "7"},
        {"ciphertext": "nocolon"},
        {"ciphertext": "x" * (MAX_CIPHERTEXT_CHARS + 1)},
        {"ciphertext": b64url_encode(b"short") + ":" + b64url_encode(b"x" * 20)},
    ],
)
def test_malformed_envelopes_raise_value_error(patch):
    with pytest.raises(ValueError):
        open_with_item_key(key=KEY, envelope={**_envelope(), **patch})


def test_open_body_needs_a_moment_id():
    with pytest.raises(ValueError):
        open_body(step_key=KEY, envelope={**_envelope(), "moment_id": None})


def test_seal_refuses_a_short_key_or_bad_moment_id():
    with pytest.raises(ValueError):
        seal_body(
            step_key=b"x" * 16,
            author_user_id="a",
            key_gen=1,
            key_step=0,
            moment_id="m",
            body={},
        )
    with pytest.raises(ValueError):
        seal_body(
            step_key=KEY,
            author_user_id="a",
            key_gen=1,
            key_step=0,
            moment_id="",
            body={},
        )


def test_sealed_body_must_be_an_object():
    env = _envelope()
    nonce = os.urandom(12)
    ct = AESGCM(item_key(KEY, "m-lake-1")).encrypt(
        nonce,
        b"[1]",
        b"socialhome/moment-public/v2|aesgcm-256|alice|1700000000|2|m-lake-1",
    )
    env["ciphertext"] = f"{b64url_encode(nonce)}:{b64url_encode(ct)}"
    with pytest.raises(TypeError):
        open_body(step_key=KEY, envelope=env)


def test_the_ratchet_is_one_way_and_bounded():
    one = ratchet(KEY)
    assert one != KEY and ratchet(KEY, 2) == ratchet(one)
    assert ratchet(KEY, 0) == KEY
    with pytest.raises(ValueError):
        ratchet(KEY, MAX_RATCHET_STEPS + 1)
    with pytest.raises(ValueError):
        ratchet(KEY, -1)


def test_step_for_counts_days_of_a_generation():
    assert step_for(1000, 1000) == 0
    assert step_for(1000, 1000 + RATCHET_STEP_S - 1) == 0
    assert step_for(1000, 1000 + 3 * RATCHET_STEP_S) == 3
    assert step_for(1000, 10) == 0


def test_is_wire_int():
    assert is_wire_int(0) and is_wire_int(7)
    assert not is_wire_int(True) and not is_wire_int(-1) and not is_wire_int("1")


def test_key_plaintext_round_trip():
    raw = key_plaintext(author_user_id="alice", key_gen=3, key_step=1, to="b", key=KEY)
    assert (
        parse_key_plaintext(raw, author_user_id="alice", key_gen=3, key_step=1, to="b")
        == KEY
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"author_user_id": "bob", "key_gen": 3, "key_step": 1, "to": "b"},
        {"author_user_id": "alice", "key_gen": 4, "key_step": 1, "to": "b"},
        {"author_user_id": "alice", "key_gen": 3, "key_step": 2, "to": "b"},
        {"author_user_id": "alice", "key_gen": 3, "key_step": 1, "to": "c"},
    ],
)
def test_a_sealed_key_lifted_into_another_delivery_is_refused(kwargs):
    raw = key_plaintext(author_user_id="alice", key_gen=3, key_step=1, to="b", key=KEY)
    with pytest.raises(ValueError):
        parse_key_plaintext(raw, **kwargs)


def test_a_sealed_key_with_an_unknown_suite_is_refused():
    raw = key_plaintext(
        author_user_id="a", key_gen=1, key_step=0, to="b", key=KEY
    ).replace(b"aesgcm-256", b"aesgcm-999")
    with pytest.raises(UnsupportedMomentSealSuite):
        parse_key_plaintext(raw, author_user_id="a", key_gen=1, key_step=0, to="b")


@pytest.mark.parametrize("raw", [b"[]", b'{"key_suite":"aesgcm-256"}'])
def test_malformed_sealed_keys_are_refused(raw):
    with pytest.raises((ValueError, TypeError)):
        parse_key_plaintext(raw, author_user_id="a", key_gen=1, key_step=0, to="b")


def test_at_rest_context_binds_author_generation_and_role():
    assert key_at_rest_context("a", 1, own=True) != key_at_rest_context(
        "a", 1, own=False
    )
    assert key_at_rest_context("a", 1, own=True) != key_at_rest_context(
        "a", 2, own=True
    )


def test_sign_and_verify_wire():
    kp = generate_identity_keypair()
    signed = sign_wire(kp.private_key, {"a": 1, "signature": "old"})
    assert verify_wire(signed, kp.public_key.hex())
    assert not verify_wire({**signed, "a": 2}, kp.public_key.hex())
    other = generate_identity_keypair()
    assert not verify_wire(signed, other.public_key.hex())
    assert not verify_wire({"a": 1}, kp.public_key.hex())
    assert not verify_wire(signed, None)
    assert not verify_wire(signed, "not-hex")
