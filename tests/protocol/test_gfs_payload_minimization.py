"""§27.9: GFS-relayed events use sealed-sender encryption.

The Global Federation Server can read only routing fields
(``space_id``, ``epoch``); sender identity + payload are encrypted
under the per-epoch space content key.

Verifies the sealed-sender envelope shape and decryption invariants.
"""

from __future__ import annotations

import json
import os

import pytest

from socialhome.federation.sealed_sender import (
    seal_envelope,
    unseal_envelope,
)


pytestmark = pytest.mark.security


# ─── Sealed envelope visible-field allowlist ────────────────────────────

_ALLOWED_GFS_VISIBLE_FIELDS: frozenset[str] = frozenset(
    {
        "sealed",
        "space_id",
        "epoch",
        "encrypted_sender",
        "encrypted_payload",
    }
)


def test_sealed_envelope_only_exposes_routing():
    key = os.urandom(32)
    env = seal_envelope(
        space_id="sp-1",
        epoch=3,
        sender_instance_id="alice-inst",
        payload_json='{"content":"secret"}',
        space_content_key=key,
    )
    d = env.to_dict()
    extras = set(d.keys()) - _ALLOWED_GFS_VISIBLE_FIELDS
    assert extras == set(), f"Sealed envelope leaks unexpected fields: {sorted(extras)}"


def test_sender_id_never_appears_on_wire():
    key = os.urandom(32)
    distinctive = "instance-alpha-very-distinctive-name"
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id=distinctive,
        payload_json="{}",
        space_content_key=key,
    )
    wire = json.dumps(env.to_dict())
    assert distinctive not in wire


def test_payload_content_never_appears_on_wire():
    key = os.urandom(32)
    secret = "auction-reserve-price-USD-9999"
    env = seal_envelope(
        space_id="sp-1",
        epoch=0,
        sender_instance_id="x",
        payload_json=f'{{"reserve":"{secret}"}}',
        space_content_key=key,
    )
    wire = json.dumps(env.to_dict())
    assert secret not in wire


def test_routing_fields_remain_in_clear():
    """``space_id`` + ``epoch`` MUST be plaintext for GFS routing."""
    key = os.urandom(32)
    env = seal_envelope(
        space_id="sp-public-routing-id",
        epoch=42,
        sender_instance_id="x",
        payload_json="{}",
        space_content_key=key,
    )
    d = env.to_dict()
    assert d["space_id"] == "sp-public-routing-id"
    assert d["epoch"] == 42


def test_unseal_with_wrong_key_fails():
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


def test_recipient_decrypts_to_original_payload():
    """Round-trip: GFS forwards the bytes; recipient decrypts cleanly."""
    key = os.urandom(32)
    env = seal_envelope(
        space_id="sp-1",
        epoch=5,
        sender_instance_id="real-sender-id",
        payload_json='{"content":"hello"}',
        space_content_key=key,
    )
    out = unseal_envelope(env, space_content_key=key)
    assert out.sender_instance_id == "real-sender-id"
    assert out.payload == {"content": "hello"}
