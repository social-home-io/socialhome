"""Tests for space-page version identity (v_48, host-sequenced pages)."""

from __future__ import annotations

import hashlib
import json

import pytest

from socialhome.domain.page_version import (
    SCALAR_CONFLICT,
    is_version_hash,
    merge_scalar,
    version_hash,
)


def test_version_hash_is_sha256_over_canonical_json():
    blob = json.dumps(
        {"content": "body", "cover": "/c.webp", "title": "T"},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert version_hash("T", "body", "/c.webp") == (
        "sha256:" + hashlib.sha256(blob.encode()).hexdigest()
    )


def test_version_hash_covers_title_content_and_cover():
    assert version_hash("a", "b") != version_hash("b", "a")
    assert version_hash("T", "x") != version_hash("T", "y")
    assert version_hash("T", "x", "/1.webp") != version_hash("T", "x", "/2.webp")
    assert version_hash("T", "x", None) == version_hash("T", "x", "")
    assert version_hash("T", "") == version_hash("T", None)  # type: ignore[arg-type]


def test_version_hash_is_well_formed_and_unicode_stable():
    h = version_hash("Ünïcode ✓", "ß\n\nparagraph")
    assert is_version_hash(h)
    assert h == version_hash("Ünïcode ✓", "ß\n\nparagraph")


@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        "",
        "sha256:" + "a" * 63,
        "sha256:" + "A" * 64,
        "sha512:" + "a" * 64,
        "a" * 64,
        "sha256:" + "a" * 64 + "\n",
    ],
)
def test_is_version_hash_rejects_malformed(value):
    assert not is_version_hash(value)


@pytest.mark.parametrize(
    ("base", "mine", "theirs", "out"),
    [
        ("t", "t", "t", "t"),
        ("t", "mine", "t", "mine"),
        ("t", "t", "theirs", "theirs"),
        ("t", "same", "same", "same"),
        (None, None, "/c", "/c"),
        ("t", "beta", "alpha", SCALAR_CONFLICT),
    ],
)
def test_merge_scalar(base, mine, theirs, out):
    assert (
        merge_scalar(base, mine, theirs) is out
        or merge_scalar(base, mine, theirs) == out
    )
