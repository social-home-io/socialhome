"""A new follower's author-profile bundle (v_61): bounds and wire shape."""

from __future__ import annotations

import json

from socialhome.domain import space_author_profile_bundle as b


def _item(i: int, pad: int = 0) -> dict:
    return {"item_type": "author_profile", "n": i, "pad": "x" * pad}


def test_a_small_cast_is_one_part():
    items = [_item(i) for i in range(3)]
    assert b.split_bundle(items) == [items]


def test_parts_stay_under_the_part_cap_and_keep_order():
    items = [_item(i, pad=50_000) for i in range(8)]
    parts = b.split_bundle(items)
    assert len(parts) > 1
    assert [x for p in parts for x in p] == items
    for part in parts:
        size = sum(len(json.dumps(x, separators=(",", ":"))) for x in part)
        assert size <= b.BUNDLE_PART_MAX_BYTES


def test_at_most_the_author_cap():
    items = [_item(i) for i in range(b.MAX_BUNDLE_AUTHORS + 10)]
    assert sum(len(p) for p in b.split_bundle(items)) == b.MAX_BUNDLE_AUTHORS


def test_the_total_cap_holds():
    items = [_item(i, pad=150_000) for i in range(b.MAX_BUNDLE_AUTHORS)]
    parts = b.split_bundle(items)
    total = sum(len(json.dumps(x, separators=(",", ":"))) for p in parts for x in p)
    assert total <= b.MAX_BUNDLE_BYTES
    assert sum(len(p) for p in parts) < len(items)


def test_an_item_too_big_for_any_part_is_left_out():
    big = _item(0, pad=b.BUNDLE_PART_MAX_BYTES + 1)
    small = _item(1)
    assert b.split_bundle([big, small]) == [[small]]


def test_nothing_in_nothing_out():
    assert b.split_bundle([]) == []


def test_plaintext_round_trip():
    raw = b.bundle_plaintext(space_id="sp", epoch=4, part=0, parts=1, items=[_item(1)])
    assert b.parse_bundle_plaintext(raw) == ("sp", 4, [_item(1)])


def test_malformed_plaintext_is_refused():
    for raw in (
        b"not json",
        b"[]",
        json.dumps({"space_id": "", "epoch": 1, "items": []}).encode(),
        json.dumps({"space_id": "sp", "epoch": True, "items": []}).encode(),
        json.dumps({"space_id": "sp", "epoch": -1, "items": []}).encode(),
        json.dumps({"space_id": "sp", "epoch": 1, "items": {}}).encode(),
        json.dumps(
            {"space_id": "sp", "epoch": 1, "items": [{}] * (b.MAX_BUNDLE_AUTHORS + 1)}
        ).encode(),
    ):
        assert b.parse_bundle_plaintext(raw) is None
