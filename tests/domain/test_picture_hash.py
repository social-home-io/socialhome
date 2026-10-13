"""Tests for :mod:`socialhome.domain.picture_hash`."""

from __future__ import annotations

import pytest

from socialhome.domain.picture_hash import (
    PICTURE_HASH_LEN,
    PICTURE_HASH_RE,
    clean_picture_hash,
    compute_picture_hash,
)
from socialhome.repositories.profile_picture_repo import (
    compute_picture_hash as repo_compute,
)


def test_compute_picture_hash_is_the_16_hex_sha256_prefix():
    # sha256(b"abc") = ba7816bf8f01cfea414140de5dae2223...
    assert compute_picture_hash(b"abc") == "ba7816bf8f01cfea"
    assert len(compute_picture_hash(b"")) == PICTURE_HASH_LEN


def test_computed_hash_always_passes_the_format_check():
    for blob in (b"", b"abc", b"\x00" * 1024, bytes(range(256))):
        digest = compute_picture_hash(blob)
        assert PICTURE_HASH_RE.fullmatch(digest)
        assert clean_picture_hash(digest) == digest


def test_repo_reexport_is_the_same_function():
    assert repo_compute is compute_picture_hash


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "ab&sig=evil",
        "../x",
        "0123456789abcdef/",
        "0123456789abcde#",
        '0123456789abcd"x',
        "0123456789ABCDEF",  # uppercase is never what we compute
        "0123456789abcdef0",  # 17 chars
        "0123456789abcde",  # 15 chars
        "0123456789abcdef\n",  # trailing newline — $ would let it through
        "g123456789abcdef",
        "a" * 10_000,
        123,
        b"0123456789abcdef",
        ["0123456789abcdef"],
        {"h": "0123456789abcdef"},
    ],
)
def test_hostile_or_malformed_values_are_dropped(value):
    assert clean_picture_hash(value) is None


def test_valid_hash_is_kept_verbatim():
    assert clean_picture_hash("0123456789abcdef") == "0123456789abcdef"
