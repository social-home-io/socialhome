"""Tests for the §D2b invite-bootstrap sealed envelope.

Covers the fail-closed ladder a stranger's redeem envelope has to climb:
size caps → unseal → known kind → suite tag → ``derive_instance_id``
anti-tamper → Ed25519 signature → timestamp skew. Each rung has its own
test so a regression names itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.federation.invite_bootstrap import (
    BOOTSTRAP_SIG_SUITE_ED25519,
    KIND_REDEEM,
    KIND_REDEEM_ACK,
    MAX_SEALED_BLOB_BYTES,
    SUPPORTED_BOOTSTRAP_SIG_SUITES,
    InviteBootstrapHint,
    UnsupportedBootstrapSigSuite,
    canonical_signing_bytes,
    derive_space_session_keys,
    open_bootstrap_envelope,
    seal_bootstrap_envelope,
    sign_bootstrap_body,
    verify_peer_keywrap,
)
from socialhome.federation.keywrap_seal import seal_to_keywrap


class _Household:
    """Identity + key-wrap material for one side of the exchange."""

    def __init__(self) -> None:
        ident = generate_identity_keypair()
        self.seed = ident.private_key
        self.identity_pk = ident.public_key
        self.instance_id = derive_instance_id(ident.public_key)
        kw = generate_x25519_keypair()
        self.keywrap_priv = kw.private_key
        self.keywrap_pub = kw.public_key
        self.keywrap_sig = b64url_encode(sign_ed25519(self.seed, kw.public_key))

    @property
    def identity_pk_hex(self) -> str:
        return self.identity_pk.hex()

    @property
    def keywrap_pk_hex(self) -> str:
        return self.keywrap_pub.hex()


@pytest.fixture
def redeemer() -> _Household:
    return _Household()


@pytest.fixture
def issuer() -> _Household:
    return _Household()


def _redeem_body(redeemer: _Household, **over) -> dict:
    body = {
        "kind": KIND_REDEEM,
        "invite_token": "a" * 32,
        "space_id": "space-1",
        "redeem_nonce": "b" * 32,
        "ts": datetime.now(timezone.utc).isoformat(),
        "instance_id": redeemer.instance_id,
        "identity_pk": redeemer.identity_pk_hex,
        "keywrap_pk": redeemer.keywrap_pk_hex,
        "keywrap_sig": redeemer.keywrap_sig,
        "display_name": "Redeemer household",
        "redeemer_user_id": "u-1",
        "redeemer_public_key": "cafe",
        "redeemer_display_name": "Ada",
    }
    body.update(over)
    return body


def _seal(redeemer: _Household, issuer: _Household, body: dict) -> dict:
    return seal_bootstrap_envelope(
        body=body,
        identity_seed=redeemer.seed,
        recipient_instance_id=issuer.instance_id,
        recipient_identity_pk=issuer.identity_pk_hex,
        recipient_keywrap_pk=issuer.keywrap_pk_hex,
        recipient_keywrap_sig=issuer.keywrap_sig,
    )


# ── happy path ────────────────────────────────────────────────────────


def test_round_trip_returns_validated_body(redeemer, issuer):
    envelope = _seal(redeemer, issuer, _redeem_body(redeemer))
    body = open_bootstrap_envelope(
        envelope=envelope,
        keywrap_private_key=issuer.keywrap_priv,
        expected_kinds=frozenset({KIND_REDEEM}),
    )
    assert body["invite_token"] == "a" * 32
    assert body["instance_id"] == redeemer.instance_id
    assert body["sig_suite"] == BOOTSTRAP_SIG_SUITE_ED25519


def test_outer_envelope_is_identity_free(redeemer, issuer):
    """The relay must learn nothing but the recipient (#677)."""
    envelope = _seal(redeemer, issuer, _redeem_body(redeemer))
    assert set(envelope) == {"to_instance", "sealed"}
    assert envelope["to_instance"] == issuer.instance_id
    flat = json.dumps(envelope)
    assert redeemer.instance_id not in flat
    assert redeemer.identity_pk_hex not in flat
    assert "a" * 32 not in flat  # the token
    assert "space-1" not in flat
    assert "Ada" not in flat


def test_suite_constant_is_in_supported_set():
    assert BOOTSTRAP_SIG_SUITE_ED25519 in SUPPORTED_BOOTSTRAP_SIG_SUITES


# ── fail-closed ladder ────────────────────────────────────────────────


def test_missing_sealed_payload_rejected(issuer):
    with pytest.raises(ValueError, match="missing sealed payload"):
        open_bootstrap_envelope(
            envelope={"to_instance": issuer.instance_id},
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_oversized_blob_rejected_before_unseal(issuer):
    envelope = {
        "to_instance": issuer.instance_id,
        "sealed": {
            "kem_suite": "x25519",
            "eph_pk": "x",
            "ciphertext": "z" * (MAX_SEALED_BLOB_BYTES + 1),
        },
    }
    with pytest.raises(ValueError, match="too large"):
        open_bootstrap_envelope(
            envelope=envelope,
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_sealed_to_another_household_does_not_open(redeemer, issuer):
    stranger = _Household()
    envelope = _seal(redeemer, issuer, _redeem_body(redeemer))
    with pytest.raises(ValueError, match="does not open"):
        open_bootstrap_envelope(
            envelope=envelope,
            keywrap_private_key=stranger.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_unknown_kind_rejected(redeemer, issuer):
    body = sign_bootstrap_body(
        _redeem_body(redeemer, kind="totally_made_up"),
        identity_seed=redeemer.seed,
    )
    envelope = {
        "to_instance": issuer.instance_id,
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=issuer.keywrap_pub,
            plaintext=json.dumps(body).encode(),
        ),
    }
    with pytest.raises(ValueError, match="unknown bootstrap kind"):
        open_bootstrap_envelope(
            envelope=envelope,
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_wrong_leg_kind_rejected(redeemer, issuer):
    """An ACK body replayed onto the request leg is refused."""
    envelope = _seal(redeemer, issuer, _redeem_body(redeemer))
    with pytest.raises(ValueError, match="unexpected bootstrap kind"):
        open_bootstrap_envelope(
            envelope=envelope,
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM_ACK}),
        )


def test_unknown_sig_suite_rejected_with_no_fallback(redeemer, issuer):
    body = _redeem_body(redeemer)
    body["sig_suite"] = "ed25519+quantumhandwave"
    body["signature"] = sign_ed25519(redeemer.seed, canonical_signing_bytes(body)).hex()
    envelope = {
        "to_instance": issuer.instance_id,
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=issuer.keywrap_pub,
            plaintext=json.dumps(body).encode(),
        ),
    }
    with pytest.raises(UnsupportedBootstrapSigSuite):
        open_bootstrap_envelope(
            envelope=envelope,
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_missing_sig_suite_rejected(redeemer, issuer):
    body = _redeem_body(redeemer)
    body["signature"] = sign_ed25519(redeemer.seed, canonical_signing_bytes(body)).hex()
    envelope = {
        "to_instance": issuer.instance_id,
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=issuer.keywrap_pub,
            plaintext=json.dumps(body).encode(),
        ),
    }
    with pytest.raises(UnsupportedBootstrapSigSuite):
        open_bootstrap_envelope(
            envelope=envelope,
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_spoofed_instance_id_rejected_by_anti_tamper(redeemer, issuer):
    """§4.1.2 — claiming a victim's instance_id while signing with your
    own keypair must not get past the identity check."""
    victim = _Household()
    body = _redeem_body(redeemer, instance_id=victim.instance_id)
    signed = sign_bootstrap_body(body, identity_seed=redeemer.seed)
    envelope = {
        "to_instance": issuer.instance_id,
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=issuer.keywrap_pub,
            plaintext=json.dumps(signed).encode(),
        ),
    }
    with pytest.raises(ValueError, match="does not match identity_pk"):
        open_bootstrap_envelope(
            envelope=envelope,
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_tampered_field_fails_signature(redeemer, issuer):
    signed = sign_bootstrap_body(_redeem_body(redeemer), identity_seed=redeemer.seed)
    signed["invite_token"] = "z" * 32  # swap the token after signing
    envelope = {
        "to_instance": issuer.instance_id,
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=issuer.keywrap_pub,
            plaintext=json.dumps(signed).encode(),
        ),
    }
    with pytest.raises(ValueError, match="signature verification failed"):
        open_bootstrap_envelope(
            envelope=envelope,
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_signature_by_a_different_key_rejected(redeemer, issuer):
    attacker = _Household()
    body = _redeem_body(redeemer)
    body["sig_suite"] = BOOTSTRAP_SIG_SUITE_ED25519
    body["signature"] = sign_ed25519(attacker.seed, canonical_signing_bytes(body)).hex()
    envelope = {
        "to_instance": issuer.instance_id,
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=issuer.keywrap_pub,
            plaintext=json.dumps(body).encode(),
        ),
    }
    with pytest.raises(ValueError, match="signature verification failed"):
        open_bootstrap_envelope(
            envelope=envelope,
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


@pytest.mark.parametrize("delta_s", [-3600, 3600])
def test_timestamp_outside_window_rejected(redeemer, issuer, delta_s):
    stale = (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()
    envelope = _seal(redeemer, issuer, _redeem_body(redeemer, ts=stale))
    with pytest.raises(ValueError, match="skew too large"):
        open_bootstrap_envelope(
            envelope=envelope,
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_naive_timestamp_rejected(redeemer, issuer):
    naive = datetime.now().replace(tzinfo=None).isoformat()
    envelope = _seal(redeemer, issuer, _redeem_body(redeemer, ts=naive))
    with pytest.raises(ValueError, match="not timezone-aware"):
        open_bootstrap_envelope(
            envelope=envelope,
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


# ── anti-substitution gate on the seal side ───────────────────────────


def test_seal_refuses_substituted_keywrap_key(redeemer, issuer):
    """A relay swapping in a key-wrap key it holds must not get sealed to."""
    attacker_kw = generate_x25519_keypair()
    with pytest.raises(ValueError, match="not bound to the advertised identity"):
        seal_bootstrap_envelope(
            body=_redeem_body(redeemer),
            identity_seed=redeemer.seed,
            recipient_instance_id=issuer.instance_id,
            recipient_identity_pk=issuer.identity_pk_hex,
            recipient_keywrap_pk=attacker_kw.public_key.hex(),
            recipient_keywrap_sig=issuer.keywrap_sig,
        )


def test_seal_refuses_identity_that_does_not_derive_the_instance_id(redeemer, issuer):
    other = _Household()
    with pytest.raises(ValueError, match="not bound to the advertised identity"):
        seal_bootstrap_envelope(
            body=_redeem_body(redeemer),
            identity_seed=redeemer.seed,
            recipient_instance_id=other.instance_id,
            recipient_identity_pk=issuer.identity_pk_hex,
            recipient_keywrap_pk=issuer.keywrap_pk_hex,
            recipient_keywrap_sig=issuer.keywrap_sig,
        )


def test_verify_peer_keywrap_accepts_bound_material(redeemer):
    assert (
        verify_peer_keywrap(
            instance_id=redeemer.instance_id,
            identity_pk=redeemer.identity_pk_hex,
            keywrap_pk=redeemer.keywrap_pk_hex,
            keywrap_sig=redeemer.keywrap_sig,
        )
        == redeemer.keywrap_pub
    )


def test_verify_peer_keywrap_rejects_unbound_material(redeemer):
    attacker_kw = generate_x25519_keypair()
    assert (
        verify_peer_keywrap(
            instance_id=redeemer.instance_id,
            identity_pk=redeemer.identity_pk_hex,
            keywrap_pk=attacker_kw.public_key.hex(),
            keywrap_sig=redeemer.keywrap_sig,
        )
        is None
    )


def test_verify_peer_keywrap_rejects_garbage_hex(redeemer):
    assert (
        verify_peer_keywrap(
            instance_id=redeemer.instance_id,
            identity_pk="nothex",
            keywrap_pk=redeemer.keywrap_pk_hex,
            keywrap_sig=redeemer.keywrap_sig,
        )
        is None
    )


# ── session-key derivation ────────────────────────────────────────────


def test_session_keys_mirror_across_the_pair(redeemer, issuer):
    r_send, r_recv = derive_space_session_keys(
        own_keywrap_priv=redeemer.keywrap_priv,
        peer_keywrap_pub=issuer.keywrap_pub,
        is_redeemer=True,
    )
    i_send, i_recv = derive_space_session_keys(
        own_keywrap_priv=issuer.keywrap_priv,
        peer_keywrap_pub=redeemer.keywrap_pub,
        is_redeemer=False,
    )
    assert r_send == i_recv
    assert i_send == r_recv
    assert r_send != r_recv
    assert len(r_send) == 32


def test_session_keys_are_domain_separated_from_a_third_party(redeemer, issuer):
    stranger = _Household()
    r_send, _ = derive_space_session_keys(
        own_keywrap_priv=redeemer.keywrap_priv,
        peer_keywrap_pub=issuer.keywrap_pub,
        is_redeemer=True,
    )
    other, _ = derive_space_session_keys(
        own_keywrap_priv=redeemer.keywrap_priv,
        peer_keywrap_pub=stranger.keywrap_pub,
        is_redeemer=True,
    )
    assert r_send != other


# ── the hint DTO ──────────────────────────────────────────────────────


def test_hint_defaults_to_the_most_conservative_proto_version():
    hint = InviteBootstrapHint(
        invite_token="t",
        space_id="s",
        instance_id="i",
        identity_pk="p",
        keywrap_pk="k",
        keywrap_sig="g",
    )
    assert hint.proto_version == 1
    assert hint.expires_at is None


# ── malformed-input rungs ─────────────────────────────────────────────


def test_seal_rejects_non_hex_recipient_material(redeemer, issuer):
    with pytest.raises(ValueError, match="malformed recipient key material"):
        seal_bootstrap_envelope(
            body=_redeem_body(redeemer),
            identity_seed=redeemer.seed,
            recipient_instance_id=issuer.instance_id,
            recipient_identity_pk="zzz",
            recipient_keywrap_pk=issuer.keywrap_pk_hex,
            recipient_keywrap_sig=issuer.keywrap_sig,
        )


def test_missing_ciphertext_rejected(issuer):
    with pytest.raises(ValueError, match="missing ciphertext"):
        open_bootstrap_envelope(
            envelope={"sealed": {"kem_suite": "x25519", "eph_pk": "x"}},
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def _sealed(issuer, payload: bytes) -> dict:
    return {
        "to_instance": issuer.instance_id,
        "sealed": seal_to_keywrap(
            recipient_keywrap_pub=issuer.keywrap_pub,
            plaintext=payload,
        ),
    }


def test_non_json_body_rejected(issuer):
    with pytest.raises(ValueError, match="not JSON"):
        open_bootstrap_envelope(
            envelope=_sealed(issuer, b"not json at all"),
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_non_object_body_rejected(issuer):
    with pytest.raises(ValueError, match="not an object"):
        open_bootstrap_envelope(
            envelope=_sealed(issuer, b"[1, 2, 3]"),
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_missing_identity_fields_rejected(redeemer, issuer):
    body = _redeem_body(redeemer)
    body.pop("identity_pk")
    body["sig_suite"] = BOOTSTRAP_SIG_SUITE_ED25519
    body["signature"] = "aa"
    with pytest.raises(ValueError, match="missing identity / signature"):
        open_bootstrap_envelope(
            envelope=_sealed(issuer, json.dumps(body).encode()),
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_non_hex_identity_rejected(redeemer, issuer):
    body = _redeem_body(redeemer, identity_pk="nothex")
    body["sig_suite"] = BOOTSTRAP_SIG_SUITE_ED25519
    body["signature"] = "aa"
    with pytest.raises(ValueError, match="malformed bootstrap identity"):
        open_bootstrap_envelope(
            envelope=_sealed(issuer, json.dumps(body).encode()),
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_missing_ts_rejected(redeemer, issuer):
    body = _redeem_body(redeemer)
    body.pop("ts")
    signed = sign_bootstrap_body(body, identity_seed=redeemer.seed)
    with pytest.raises(ValueError, match="missing ts"):
        open_bootstrap_envelope(
            envelope=_sealed(issuer, json.dumps(signed).encode()),
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_unparseable_ts_rejected(redeemer, issuer):
    signed = sign_bootstrap_body(
        _redeem_body(redeemer, ts="whenever"),
        identity_seed=redeemer.seed,
    )
    with pytest.raises(ValueError, match="unparseable bootstrap ts"):
        open_bootstrap_envelope(
            envelope=_sealed(issuer, json.dumps(signed).encode()),
            keywrap_private_key=issuer.keywrap_priv,
            expected_kinds=frozenset({KIND_REDEEM}),
        )


def test_verify_peer_keywrap_rejects_empty_signature(redeemer):
    assert (
        verify_peer_keywrap(
            instance_id=redeemer.instance_id,
            identity_pk=redeemer.identity_pk_hex,
            keywrap_pk=redeemer.keywrap_pk_hex,
            keywrap_sig="",
        )
        is None
    )
