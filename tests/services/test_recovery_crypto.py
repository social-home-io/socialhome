"""Tests for the Recovery Kit pure-crypto codec (``recovery_crypto``).

Covers round-trip, the AAD header binding (tamper any clear field → auth
failure), suite-allow-list enforcement (no default fallback), version
checks, malformed input, and the no-plaintext-leak property.
"""

from __future__ import annotations

import orjson
import pytest

from socialhome.crypto import b64url_decode, b64url_encode
from socialhome.services import recovery_crypto as rc

PAYLOAD = b"trust-layer-blob: SECRET-MARKER-12345 \x00\x01\x02 binary"
PASSPHRASE = "correct horse battery staple"
INSTANCE_ID = "qbfdx7k2n3p6r8t1v4w9"
CREATED_AT = "2026-06-14T12:00:00+00:00"


def _seal() -> bytes:
    return rc.seal_kit(
        PAYLOAD, PASSPHRASE, instance_id=INSTANCE_ID, created_at=CREATED_AT
    )


def _mutate_header(kit_bytes: bytes, **changes: object) -> bytes:
    obj = orjson.loads(kit_bytes)
    obj.update(changes)
    return orjson.dumps(obj)


def test_round_trip_returns_payload_and_header() -> None:
    kit = _seal()
    header, payload = rc.unseal_kit(kit, PASSPHRASE)
    assert payload == PAYLOAD
    assert header["kit_version"] == rc.RECOVERY_KIT_VERSION
    assert header["kdf_suite"] == rc.RECOVERY_KDF_SUITE_SCRYPT
    assert header["aead_suite"] == rc.RECOVERY_AEAD_SUITE_AESGCM
    assert header["instance_id"] == INSTANCE_ID
    assert header["created_at"] == CREATED_AT


def test_read_kit_header_without_passphrase() -> None:
    kit = _seal()
    header = rc.read_kit_header(kit)
    assert header["instance_id"] == INSTANCE_ID
    assert header["created_at"] == CREATED_AT
    assert header["kdf_suite"] == rc.RECOVERY_KDF_SUITE_SCRYPT
    assert header["aead_suite"] == rc.RECOVERY_AEAD_SUITE_AESGCM
    assert header["kit_version"] == rc.RECOVERY_KIT_VERSION


def test_wrong_passphrase_raises() -> None:
    kit = _seal()
    with pytest.raises(rc.RecoveryKitError):
        rc.unseal_kit(kit, "wrong passphrase")


def test_tamper_instance_id_breaks_aad() -> None:
    kit = _mutate_header(_seal(), instance_id="evil-instance-id")
    with pytest.raises(rc.RecoveryKitError):
        rc.unseal_kit(kit, PASSPHRASE)


def test_tamper_created_at_breaks_aad() -> None:
    kit = _mutate_header(_seal(), created_at="2000-01-01T00:00:00+00:00")
    with pytest.raises(rc.RecoveryKitError):
        rc.unseal_kit(kit, PASSPHRASE)


def test_tamper_ciphertext_byte_raises() -> None:
    obj = orjson.loads(_seal())
    ct = bytearray(b64url_decode(obj["ciphertext"]))
    ct[0] ^= 0x01
    obj["ciphertext"] = b64url_encode(bytes(ct))
    with pytest.raises(rc.RecoveryKitError):
        rc.unseal_kit(orjson.dumps(obj), PASSPHRASE)


def test_unknown_kdf_suite_raises_unsupported() -> None:
    kit = _mutate_header(_seal(), kdf_suite="argon2id-bogus")
    with pytest.raises(rc.UnsupportedRecoverySuite):
        rc.unseal_kit(kit, PASSPHRASE)
    with pytest.raises(rc.UnsupportedRecoverySuite):
        rc.read_kit_header(kit)


def test_unknown_aead_suite_raises_unsupported() -> None:
    kit = _mutate_header(_seal(), aead_suite="chacha20poly1305-bogus")
    with pytest.raises(rc.UnsupportedRecoverySuite):
        rc.unseal_kit(kit, PASSPHRASE)
    with pytest.raises(rc.UnsupportedRecoverySuite):
        rc.read_kit_header(kit)


def test_non_string_suite_raises_unsupported_not_typeerror() -> None:
    # A JSON list/object for a suite field is unhashable; the membership
    # check must not crash with an unhandled TypeError on untrusted bytes.
    for field in ("kdf_suite", "aead_suite"):
        for bogus in ([rc.RECOVERY_KDF_SUITE_SCRYPT], {"x": 1}):
            kit = _mutate_header(_seal(), **{field: bogus})
            with pytest.raises(rc.UnsupportedRecoverySuite):
                rc.unseal_kit(kit, PASSPHRASE)
            with pytest.raises(rc.UnsupportedRecoverySuite):
                rc.read_kit_header(kit)


def test_wrong_version_raises() -> None:
    kit = _mutate_header(_seal(), kit_version=999)
    with pytest.raises(rc.RecoveryKitError):
        rc.unseal_kit(kit, PASSPHRASE)
    with pytest.raises(rc.RecoveryKitError):
        rc.read_kit_header(kit)


def test_malformed_not_json_raises() -> None:
    with pytest.raises(rc.RecoveryKitError):
        rc.unseal_kit(b"this is not json{{{", PASSPHRASE)
    with pytest.raises(rc.RecoveryKitError):
        rc.read_kit_header(b"this is not json{{{")


def test_malformed_json_array_not_object_raises() -> None:
    with pytest.raises(rc.RecoveryKitError):
        rc.read_kit_header(orjson.dumps([1, 2, 3]))
    with pytest.raises(rc.RecoveryKitError):
        rc.unseal_kit(orjson.dumps([1, 2, 3]), PASSPHRASE)


def test_short_nonce_decrypt_error_raises() -> None:
    # A valid-b64 but wrong-length nonce makes AESGCM.decrypt raise a plain
    # ValueError (not InvalidTag) — that branch must also map to RecoveryKitError.
    obj = orjson.loads(_seal())
    obj["nonce"] = b64url_encode(b"\x00" * 4)  # too short for GCM
    with pytest.raises(rc.RecoveryKitError):
        rc.unseal_kit(orjson.dumps(obj), PASSPHRASE)


def test_malformed_missing_fields_raises() -> None:
    kit = orjson.dumps({"kit_version": rc.RECOVERY_KIT_VERSION})
    with pytest.raises(rc.RecoveryKitError):
        rc.read_kit_header(kit)
    with pytest.raises(rc.RecoveryKitError):
        rc.unseal_kit(kit, PASSPHRASE)


def test_malformed_bad_b64_raises() -> None:
    obj = orjson.loads(_seal())
    obj["ciphertext"] = "!!!not-base64!!!"
    with pytest.raises(rc.RecoveryKitError):
        rc.unseal_kit(orjson.dumps(obj), PASSPHRASE)


def test_empty_payload_round_trips() -> None:
    kit = rc.seal_kit(b"", PASSPHRASE, instance_id=INSTANCE_ID, created_at=CREATED_AT)
    header, payload = rc.unseal_kit(kit, PASSPHRASE)
    assert payload == b""
    assert header["instance_id"] == INSTANCE_ID


def test_no_plaintext_leak() -> None:
    kit = _seal()
    assert b"SECRET-MARKER-12345" not in kit
