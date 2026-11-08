"""Sealing a ``fed-media-v1`` body for the GFS relay (v_87)."""

from __future__ import annotations

import pytest

from socialhome.crypto import generate_x25519_keypair
from socialhome.domain.relay_media_frame import (
    RELAY_MEDIA_MAX_BODY_BYTES,
    RELAY_MEDIA_PREFIX_BYTES,
    parse_relay_media_body,
)
from socialhome.federation import gfs_relay_media as grm
from socialhome.federation.keywrap_seal import UnsupportedKemSuite

ME = "b" * 32
OTHER = "c" * 32


@pytest.fixture
def keys():
    return generate_x25519_keypair()


def _seal(keys, body: bytes, to: str = ME) -> bytes:
    return grm.seal_relay_media(
        to_instance=to, media_body=body, recipient_keywrap_pub=keys.public_key
    )


def test_round_trip(keys):
    media = b"\x00SHM\x01" + bytes(range(256)) * 10
    out = _seal(keys, media)
    assert (
        grm.open_relay_media(
            out, own_instance_id=ME, keywrap_private_key=keys.private_key
        )
        == media
    )


def test_the_gfs_sees_only_the_recipient(keys):
    media = b"\x00SHM\x01" + b'{"from_instance":"secret-sender"}' + b"x" * 100
    out = _seal(keys, media)
    to, sealed = parse_relay_media_body(out)
    assert to == ME
    assert b"secret-sender" not in out
    assert b"SHM" not in bytes(sealed)


def test_full_chunk_fits_the_516_kib_rung_and_the_cap(keys):
    media = b"\x00SHM\x01" + b"h" * 1600 + b"c" * (512 * 1024 + 28) + b"f" * 9
    out = _seal(keys, media)
    assert len(out) <= RELAY_MEDIA_MAX_BODY_BYTES
    assert len(out) == RELAY_MEDIA_PREFIX_BYTES + grm.SEAL_OVERHEAD_BYTES + 516 * 1024


@pytest.mark.parametrize("size", [1, 1000, 70_000, 200_000])
def test_size_is_padded_to_a_bucket(keys, size):
    out = _seal(keys, b"m" * size)
    plain = len(out) - RELAY_MEDIA_PREFIX_BYTES - grm.SEAL_OVERHEAD_BYTES
    assert plain in grm.RELAY_MEDIA_SIZE_BUCKETS


def test_oversize_is_refused(keys):
    with pytest.raises(ValueError, match="does not fit"):
        _seal(keys, b"m" * (grm.RELAY_MEDIA_MAX_MEDIA_BODY_BYTES + 1))
    # The largest that fits still makes a body under the cap.
    out = _seal(keys, b"m" * grm.RELAY_MEDIA_MAX_MEDIA_BODY_BYTES)
    assert len(out) == RELAY_MEDIA_MAX_BODY_BYTES


def test_tampered_ciphertext_is_refused(keys):
    out = bytearray(_seal(keys, b"m" * 500))
    out[-1] ^= 1
    with pytest.raises(ValueError, match="does not open"):
        grm.open_relay_media(
            bytes(out), own_instance_id=ME, keywrap_private_key=keys.private_key
        )


def test_readdressed_frame_is_refused(keys):
    """A GFS that rewrites the recipient id breaks the AEAD binding."""
    out = _seal(keys, b"m" * 500)
    moved = out[:5] + OTHER.encode() + out[RELAY_MEDIA_PREFIX_BYTES:]
    with pytest.raises(ValueError, match="does not open"):
        grm.open_relay_media(
            moved, own_instance_id=OTHER, keywrap_private_key=keys.private_key
        )


def test_wrong_recipient_is_refused(keys):
    out = _seal(keys, b"m" * 500, to=OTHER)
    with pytest.raises(ValueError, match="another household"):
        grm.open_relay_media(
            out, own_instance_id=ME, keywrap_private_key=keys.private_key
        )


def test_sealed_to_someone_else_is_refused(keys):
    other = generate_x25519_keypair()
    out = _seal(other, b"m" * 500)
    with pytest.raises(ValueError, match="does not open"):
        grm.open_relay_media(
            out, own_instance_id=ME, keywrap_private_key=keys.private_key
        )


def test_unknown_kem_suite_byte_is_refused(keys):
    out = bytearray(_seal(keys, b"m" * 500))
    out[RELAY_MEDIA_PREFIX_BYTES] = 0x7F
    with pytest.raises(UnsupportedKemSuite):
        grm.open_relay_media(
            bytes(out), own_instance_id=ME, keywrap_private_key=keys.private_key
        )


def test_unknown_kem_suite_name_is_refused_on_seal(keys):
    with pytest.raises(UnsupportedKemSuite):
        grm.seal_relay_media(
            to_instance=ME,
            media_body=b"m",
            recipient_keywrap_pub=keys.public_key,
            kem_suite="x25519+mlkem768",
        )


def test_malformed_outer_body_is_refused(keys):
    with pytest.raises(ValueError, match="invalid relay media body"):
        grm.open_relay_media(
            b"{}", own_instance_id=ME, keywrap_private_key=keys.private_key
        )


def test_short_seal_is_refused(keys):
    out = _seal(keys, b"m")[: RELAY_MEDIA_PREFIX_BYTES + 10]
    with pytest.raises(ValueError, match="invalid relay media body"):
        grm.open_relay_media(
            out, own_instance_id=ME, keywrap_private_key=keys.private_key
        )


def test_the_kem_suite_byte_is_bound_into_the_aad(keys, monkeypatch):
    """A relay that rewrites the suite byte to another KNOWN id breaks the
    tag (wire v1 binds the suite, so a future second suite can't be swapped
    in on the way)."""
    out = bytearray(_seal(keys, b"m" * 500))
    monkeypatch.setitem(grm.KEM_SUITE_IDS, 0x02, grm.KEM_SUITE_IDS[0x01])
    out[RELAY_MEDIA_PREFIX_BYTES] = 0x02
    with pytest.raises(ValueError, match="does not open"):
        grm.open_relay_media(
            bytes(out), own_instance_id=ME, keywrap_private_key=keys.private_key
        )
