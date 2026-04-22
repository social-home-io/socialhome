"""Tests for sealed-sender envelope encryption (§4.3)."""

from __future__ import annotations

import os

import pytest

from socialhome.federation.sealed_sender import (
    SealedEnvelope,
    seal_envelope,
    unseal_envelope,
)


# ─── seal / unseal roundtrip ─────────────────────────────────────────────


def test_seal_unseal_roundtrip():
    key = os.urandom(32)
    env = seal_envelope(
        space_id="sp-1",
        epoch=3,
        sender_instance_id="instance-alpha",
        payload_json='{"text": "hello space"}',
        space_content_key=key,
    )
    out = unseal_envelope(env, space_content_key=key)
    assert out.sender_instance_id == "instance-alpha"
    assert out.payload == {"text": "hello space"}


def test_to_dict_roundtrips_via_from_dict():
    key = os.urandom(32)
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key,
    )
    d = env.to_dict()
    assert d["sealed"] is True
    assert SealedEnvelope.from_dict(d) == env


# ─── Privacy invariants (the whole point) ────────────────────────────────


def test_sender_id_not_present_in_wire_format():
    """A GFS that only sees the wire format must not be able to read sender."""
    key = os.urandom(32)
    sender = "instance-alpha-very-distinctive"
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=sender,
        payload_json="{}",
        space_content_key=key,
    )
    wire_str = repr(env.to_dict())
    assert sender not in wire_str
    # Encrypted blob is base64url + ":", obviously contains no plaintext.
    assert sender not in env.encrypted_sender


def test_payload_text_not_present_in_wire():
    key = os.urandom(32)
    secret = "super-secret-message-content"
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json=f'{{"content": "{secret}"}}',
        space_content_key=key,
    )
    assert secret not in env.encrypted_payload
    assert secret not in repr(env.to_dict())


def test_routing_fields_remain_plaintext():
    """space_id + epoch must be plaintext for GFS routing."""
    key = os.urandom(32)
    env = seal_envelope(
        space_id="sp-public-routing-ok",
        epoch=42,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key,
    )
    d = env.to_dict()
    assert d["space_id"] == "sp-public-routing-ok"
    assert d["epoch"] == 42


# ─── Tampering detection ────────────────────────────────────────────────


def test_tampered_sender_ct_fails_decrypt():
    key = os.urandom(32)
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key,
    )
    bad = SealedEnvelope(
        space_id=env.space_id,
        epoch=env.epoch,
        encrypted_sender=env.encrypted_sender[:-2] + "AA",
        encrypted_payload=env.encrypted_payload,
    )
    with pytest.raises(Exception):
        unseal_envelope(bad, space_content_key=key)


def test_wrong_key_fails_decrypt():
    key1 = os.urandom(32)
    key2 = os.urandom(32)
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key1,
    )
    with pytest.raises(Exception):
        unseal_envelope(env, space_content_key=key2)


def test_tampered_aad_via_space_id_fails():
    """AAD binds the ciphertext to (space_id, epoch). Mutating either fails."""
    key = os.urandom(32)
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key,
    )
    altered = SealedEnvelope(
        space_id="sp-DIFFERENT",  # AAD changed
        epoch=env.epoch,
        encrypted_sender=env.encrypted_sender,
        encrypted_payload=env.encrypted_payload,
    )
    with pytest.raises(Exception):
        unseal_envelope(altered, space_content_key=key)


def test_tampered_aad_via_epoch_fails():
    key = os.urandom(32)
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key,
    )
    altered = SealedEnvelope(
        space_id=env.space_id,
        epoch=99,  # AAD changed
        encrypted_sender=env.encrypted_sender,
        encrypted_payload=env.encrypted_payload,
    )
    with pytest.raises(Exception):
        unseal_envelope(altered, space_content_key=key)


# ─── Validation guards ──────────────────────────────────────────────────


def test_seal_rejects_wrong_key_size():
    with pytest.raises(ValueError):
        seal_envelope(
            space_id="sp-1",
            epoch=0,
            sender_instance_id="x",
            payload_json="{}",
            space_content_key=b"too-short",
        )


def test_unseal_rejects_wrong_key_size():
    key = os.urandom(32)
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key,
    )
    with pytest.raises(ValueError):
        unseal_envelope(env, space_content_key=b"short")


def test_from_dict_rejects_unsealed_envelope():
    with pytest.raises(ValueError):
        SealedEnvelope.from_dict({"space_id": "x"})


def test_from_dict_rejects_malformed():
    with pytest.raises(ValueError):
        SealedEnvelope.from_dict({"sealed": True})


def test_unseal_rejects_malformed_wire():
    bad = SealedEnvelope(
        space_id="sp-1",
        epoch=0,
        encrypted_sender="no-colon-here",
        encrypted_payload="also-bad",
    )
    with pytest.raises(ValueError):
        unseal_envelope(bad, space_content_key=os.urandom(32))
