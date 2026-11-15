"""Tests for space-page version identity (v_48, host-sequenced pages)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from socialhome.domain.page import Page
from socialhome.domain.page_version import (
    SCALAR_CONFLICT,
    PageConflictSide,
    is_version_hash,
    merge_scalar,
    page_record,
    side_to_wire,
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


def _page() -> Page:
    return Page(
        id="p",
        title="T",
        content="body",
        created_by="u",
        created_at="2026-01-01",
        updated_at="2026-01-02",
        space_id="s",
        seq=3,
        pending_base_seq=2,
    )


def test_page_record_is_the_row_its_hash_and_every_side():
    side = PageConflictSide(
        hash=version_hash("T", "other"), title="T", content="other", by="b", at="t"
    )
    record = page_record(_page(), [side])
    assert "pending_base_seq" not in record  # this household's own marker
    assert record["version_hash"] == version_hash("T", "body")
    assert record["conflict"] == [side_to_wire(side)]
    assert record["seq"] == 3 and record["content"] == "body"


def test_measuring_a_page_counts_ids_stamps_and_counters_at_their_widest():
    """Whoever measures (the saving household, the host, a receiver) gets
    one number for the same words, whatever its stamps say — and never less
    than what travels."""
    plain = _page()
    stamped = replace(
        plain,
        created_at="2026-11-15T09:00:00.123456+00:00",
        updated_at="2026-11-15 09:00:00",
        last_editor_user_id="u-" + "a" * 32,
        seq=2**53,
    )
    measured = {
        len(json.dumps(page_record(p, [], measure=True), ensure_ascii=False))
        for p in (plain, stamped)
    }
    assert len(measured) == 1
    full = len(json.dumps(page_record(stamped, []), ensure_ascii=False))
    assert full < measured.pop()
