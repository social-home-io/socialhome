"""Encrypted public-moment media: one blob per file, padded, content-addressed."""

from __future__ import annotations

import hashlib
import os

import pytest
from cryptography.exceptions import InvalidTag

from socialhome import moment_public_media as mpm
from socialhome.moment_public_media import (
    MAX_BLOB_BYTES,
    MAX_MEDIA_BYTES,
    MEDIA_SUITE_AESGCM256_PADDED,
    PAD_BUCKETS,
    SUPPORTED_MEDIA_SUITES,
    UnsupportedMediaSuite,
    blob_id_of,
    is_blob_id,
    open_media,
    padded_size,
    require_known_media_suite,
    seal_media,
)

ITEM = os.urandom(32)


def _seal(data: bytes, **kw) -> bytes:
    args = {
        "item_key": ITEM,
        "author_user_id": "alice",
        "moment_id": "m1",
        "role": "media",
    }
    args.update(kw)
    return seal_media(data=data, **args)


def test_round_trip_and_the_blob_is_content_addressed():
    data = b"RIFF" + os.urandom(5000)
    blob = _seal(data)
    assert data not in blob
    bid = blob_id_of(blob)
    assert bid == hashlib.sha256(blob).hexdigest() and is_blob_id(bid)
    out = open_media(
        blob=blob, item_key=ITEM, author_user_id="alice", moment_id="m1", role="media"
    )
    assert out == data


@pytest.mark.parametrize(
    ("size", "bucket"),
    [
        (0, PAD_BUCKETS[0]),
        (1, PAD_BUCKETS[0]),
        (PAD_BUCKETS[0] - 4, PAD_BUCKETS[0]),
        (PAD_BUCKETS[0] - 3, PAD_BUCKETS[1]),
        (MAX_MEDIA_BYTES, PAD_BUCKETS[-1]),
    ],
)
def test_sizes_pad_to_buckets(size, bucket):
    assert padded_size(size) == bucket
    if size <= 70_000:
        assert len(_seal(b"x" * size)) == bucket + 12 + 16


def test_two_files_in_one_bucket_have_the_same_blob_size():
    assert len(_seal(b"a" * 10)) == len(_seal(b"b" * 50_000))


def test_too_large_is_refused():
    with pytest.raises(ValueError):
        padded_size(MAX_MEDIA_BYTES + 1)
    assert MAX_BLOB_BYTES == PAD_BUCKETS[-1] + 12 + 16


@pytest.mark.parametrize(
    "kw",
    [{"author_user_id": "mallory"}, {"moment_id": "m2"}, {"role": "poster"}],
)
def test_a_blob_moved_to_another_moment_author_or_role_fails(kw):
    blob = _seal(b"hello")
    args = {
        "item_key": ITEM,
        "author_user_id": "alice",
        "moment_id": "m1",
        "role": "media",
    }
    args.update(kw)
    with pytest.raises(InvalidTag):
        open_media(blob=blob, **args)


def test_wrong_key_fails():
    with pytest.raises(InvalidTag):
        open_media(
            blob=_seal(b"x"),
            item_key=os.urandom(32),
            author_user_id="alice",
            moment_id="m1",
            role="media",
        )


@pytest.mark.parametrize("blob", [b"", b"x" * 20, b"x" * (PAD_BUCKETS[0] + 1)])
def test_malformed_blobs_raise(blob):
    with pytest.raises((ValueError, InvalidTag)):
        open_media(
            blob=blob,
            item_key=ITEM,
            author_user_id="alice",
            moment_id="m1",
            role="media",
        )


def test_a_lying_length_header_is_refused():
    """The length prefix sits inside the AEAD, but a sealer could still write
    one past the padding — refused, never read out of bounds."""
    blob = mpm._seal_padded(
        key=mpm._media_key(ITEM, "media"),
        aad=mpm._aad("alice", "m1", "media"),
        plaintext=(10**9).to_bytes(4, "big") + b"\0" * (PAD_BUCKETS[0] - 4),
    )
    with pytest.raises(ValueError):
        open_media(
            blob=blob,
            item_key=ITEM,
            author_user_id="alice",
            moment_id="m1",
            role="media",
        )


def test_unknown_roles_and_suites_are_refused():
    with pytest.raises(ValueError):
        _seal(b"x", role="thumbnail")
    assert SUPPORTED_MEDIA_SUITES == {MEDIA_SUITE_AESGCM256_PADDED}
    for bad in ("aesgcm-256", None, ""):
        with pytest.raises(UnsupportedMediaSuite):
            require_known_media_suite(bad)
    assert require_known_media_suite(MEDIA_SUITE_AESGCM256_PADDED)


@pytest.mark.parametrize("value", ["", "ab", "g" * 64, "A" * 64, None, 5])
def test_blob_ids_are_64_lowercase_hex(value):
    assert not is_blob_id(value)
