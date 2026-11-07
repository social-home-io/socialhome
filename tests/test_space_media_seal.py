"""Space media sealed for the GFS cache (:mod:`socialhome.space_media_seal`)."""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from socialhome.moment_public_media import MAX_BLOB_BYTES, PAD_BUCKETS, blob_id_of
from socialhome.space_media_seal import (
    MAX_SPACE_MEDIA_BYTES,
    SPACE_MEDIA_SUITE_AESGCM256_PADDED,
    SUPPORTED_SPACE_MEDIA_SUITES,
    UnsupportedSpaceMediaSuite,
    derive_space_media_key,
    open_space_media,
    require_space_media_suite,
    seal_space_media,
    space_media_nonce,
)

CONTENT_KEY = bytes(range(32))
KEY = derive_space_media_key(CONTENT_KEY)
SPACE = "sp-1"
NAME = "0123abcd.webp"


def _seal(data: bytes, **kw) -> bytes:
    args = {"media_key": KEY, "space_id": SPACE, "epoch": 3, "filename": NAME}
    args |= {"item_id": "post-1", "origin": "inst-a"}
    args.update(kw)
    return seal_space_media(data=data, **args)


def _open(blob: bytes, **kw) -> bytes:
    args = {"media_key": KEY, "space_id": SPACE, "epoch": 3, "filename": NAME}
    args |= {"item_id": "post-1", "origin": "inst-a"}
    args.update(kw)
    return open_space_media(blob=blob, **args)


def _nonce(name: str, data: bytes, *, item_id="post-1", origin="inst-a") -> bytes:
    return space_media_nonce(KEY, name, data, item_id=item_id, origin=origin)


def test_round_trip() -> None:
    data = os.urandom(5000)
    assert _open(_seal(data)) == data


def test_suite_is_tagged_and_unknown_is_refused() -> None:
    assert SPACE_MEDIA_SUITE_AESGCM256_PADDED in SUPPORTED_SPACE_MEDIA_SUITES
    assert require_space_media_suite(SPACE_MEDIA_SUITE_AESGCM256_PADDED)
    with pytest.raises(UnsupportedSpaceMediaSuite):
        require_space_media_suite("aesgcm-128")
    with pytest.raises(UnsupportedSpaceMediaSuite):
        require_space_media_suite(None)
    assert issubclass(UnsupportedSpaceMediaSuite, ValueError)


def test_key_is_derived_not_the_content_key() -> None:
    assert len(KEY) == 32
    assert KEY != CONTENT_KEY
    assert derive_space_media_key(CONTENT_KEY) == KEY
    assert derive_space_media_key(bytes(32)) != KEY


def test_deterministic_same_file_same_blob() -> None:
    """A re-seal (re-upload after the cache dropped the blob) yields the
    byte-identical blob, so readers' descriptors stay valid."""
    data = os.urandom(70_000)
    first, second = _seal(data), _seal(data)
    assert first == second
    assert blob_id_of(first) == blob_id_of(second)


def test_different_content_same_name_different_nonce() -> None:
    a, b = os.urandom(100), os.urandom(100)
    assert _nonce(NAME, a) != _nonce(NAME, b)
    assert len(_nonce(NAME, a)) == 12
    assert _seal(a)[:12] != _seal(b)[:12]


def test_nonce_depends_on_name() -> None:
    data = b"x" * 10
    assert _nonce("a.webp", data) != _nonce("b.webp", data)


def test_nonce_depends_on_the_item_and_the_sealing_household() -> None:
    """#1309 re-review: the AAD names the item and the household that sealed
    it, so the nonce must too — one nonce under one key with two AADs would
    be GCM nonce reuse."""
    data = b"x" * 10
    base = _nonce(NAME, data)
    assert _nonce(NAME, data, item_id="post-2") != base
    assert _nonce(NAME, data, origin="inst-m") != base


@pytest.mark.parametrize(
    "tamper",
    [
        {"space_id": "sp-2"},
        {"epoch": 4},
        {"filename": "other.webp"},
        {"item_id": "post-2"},
        {"origin": "inst-m"},
        {"media_key": derive_space_media_key(bytes(32))},
    ],
)
def test_tamper_refused(tamper: dict) -> None:
    blob = _seal(b"hello world")
    with pytest.raises(InvalidTag):
        _open(blob, **tamper)


def test_flipped_byte_refused() -> None:
    blob = bytearray(_seal(b"hello world"))
    blob[20] ^= 1
    with pytest.raises(InvalidTag):
        _open(bytes(blob))


def test_padded_to_a_bucket() -> None:
    for size in (0, 1, 4000, 70_000, 300_000, 1_500_000):
        blob = _seal(b"\1" * size)
        assert len(blob) - 28 in PAD_BUCKETS
        assert len(blob) <= MAX_BLOB_BYTES


def test_largest_file_fits_and_one_more_does_not() -> None:
    assert MAX_SPACE_MEDIA_BYTES == 8 * 1024 * 1024
    blob = _seal(b"\0" * MAX_SPACE_MEDIA_BYTES)
    assert len(blob) == MAX_BLOB_BYTES
    with pytest.raises(ValueError):
        _seal(b"\0" * (MAX_SPACE_MEDIA_BYTES + 1))


def test_malformed_blob_sizes_refused() -> None:
    with pytest.raises(ValueError):
        _open(b"short")
    with pytest.raises(ValueError):
        _open(os.urandom(PAD_BUCKETS[0] + 28 + 1))
