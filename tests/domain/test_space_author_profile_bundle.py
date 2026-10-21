"""A new follower's author-profile bundle (v_61): names first, pictures
after, every part padded to a size bucket."""

from __future__ import annotations

import json

import pytest

from socialhome.domain import space_author_profile_bundle as b
from socialhome.domain.space_author_profile import PROFILE_PICTURE_FIELD
from socialhome.domain.space_item import ITEM_SIZE_BUCKETS, PAD_FIELD


def _item(i: int, *, picture: str | None = None, pad: int = 0) -> dict:
    inner = {
        "item_type": "author_profile",
        "author_user_id": f"u{i}",
        "content": f"n{i}" + "x" * pad,
    }
    if picture is not None:
        inner[PROFILE_PICTURE_FIELD] = picture
    return inner


def _entry(i: int, **kw) -> b.BundleEntry:
    return b.BundleEntry(epoch=i % 3, item=_item(i, **kw), picture_hash=f"{i:016x}")


def _parse(raw: bytes) -> dict:
    return json.loads(raw)


def test_the_first_part_carries_every_name_and_no_picture_bytes():
    entries = [_entry(i, picture="A" * 20_000) for i in range(5)]
    parts = b.build_bundle_parts("sp", entries)
    first = _parse(parts[0])
    assert [x["item"]["author_user_id"] for x in first["items"]] == [
        f"u{i}" for i in range(5)
    ]
    assert all(PROFILE_PICTURE_FIELD not in x["item"] for x in first["items"])
    assert [x["epoch"] for x in first["items"]] == [i % 3 for i in range(5)]
    assert first["pictures"] == []


def test_pictures_follow_in_later_parts_by_author_and_signed_hash():
    entries = [_entry(i, picture=f"PIC{i}") for i in range(3)]
    parts = b.build_bundle_parts("sp", entries)
    pics = [p for raw in parts[1:] for p in _parse(raw)["pictures"]]
    assert pics == [
        {"author": f"u{i}", "hash": f"{i:016x}", "picture": f"PIC{i}"} for i in range(3)
    ]
    assert all(_parse(raw)["items"] == [] for raw in parts[1:])


def test_an_author_without_a_picture_sends_no_picture_part():
    parts = b.build_bundle_parts("sp", [_entry(1), _entry(2)])
    assert len(parts) == 1


def test_every_part_is_padded_to_a_bucket():
    entries = [_entry(i, picture="P" * (i * 3000 + 10)) for i in range(12)]
    parts = b.build_bundle_parts("sp", entries)
    assert len(parts) > 1
    for raw in parts:
        assert len(raw) in ITEM_SIZE_BUCKETS
        assert PAD_FIELD in _parse(raw)
        assert len(raw) <= b.BUNDLE_PART_MAX_BYTES


def test_parts_number_themselves():
    entries = [_entry(i, picture="P" * 30_000) for i in range(10)]
    parts = [_parse(raw) for raw in b.build_bundle_parts("sp", entries)]
    assert [p["part"] for p in parts] == list(range(len(parts)))
    assert {p["parts"] for p in parts} == {len(parts)}
    assert {p["space_id"] for p in parts} == {"sp"}


def test_at_most_the_author_cap():
    entries = [_entry(i) for i in range(b.MAX_BUNDLE_AUTHORS + 10)]
    first = _parse(b.build_bundle_parts("sp", entries)[0])
    assert len(first["items"]) == b.MAX_BUNDLE_AUTHORS


def test_names_that_do_not_fit_one_part_are_left_out():
    entries = [_entry(i, pad=3_000) for i in range(60)]
    parts = b.build_bundle_parts("sp", entries)
    first = _parse(parts[0])
    assert 0 < len(first["items"]) < b.MAX_BUNDLE_AUTHORS
    assert len(parts[0]) <= b.BUNDLE_PART_MAX_BYTES


def test_nothing_in_nothing_out():
    assert b.build_bundle_parts("sp", []) == []


def test_round_trip():
    entries = [_entry(i, picture=f"PIC{i}") for i in range(2)]
    parsed = [
        b.parse_bundle_plaintext(raw) for raw in b.build_bundle_parts("sp", entries)
    ]
    assert parsed[0] is not None
    assert parsed[0].space_id == "sp"
    assert [(e, i["author_user_id"]) for e, i in parsed[0].items] == [
        (0, "u0"),
        (1, "u1"),
    ]
    pics = [p for part in parsed if part is not None for p in part.pictures]
    assert pics == [("u0", f"{0:016x}", "PIC0"), ("u1", f"{1:016x}", "PIC1")]


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        [],
        {"space_id": "", "items": [], "pictures": []},
        {"space_id": "sp", "items": {}, "pictures": []},
        {"space_id": "sp", "items": [], "pictures": {}},
        {"space_id": "sp", "items": [{"epoch": True, "item": {}}], "pictures": []},
        {"space_id": "sp", "items": [{"epoch": -1, "item": {}}], "pictures": []},
        {"space_id": "sp", "items": [{"epoch": 1, "item": []}], "pictures": []},
        {
            "space_id": "sp",
            "items": [{"epoch": 1, "item": {}}] * (b.MAX_BUNDLE_AUTHORS + 1),
            "pictures": [],
        },
        {
            "space_id": "sp",
            "items": [],
            "pictures": [{"author": "u", "hash": "h", "picture": "p"}]
            * (b.MAX_BUNDLE_AUTHORS + 1),
        },
        {"space_id": "sp", "items": [], "pictures": [{"author": 1, "hash": "h"}]},
    ],
)
def test_malformed_plaintext_is_refused(body):
    raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    assert b.parse_bundle_plaintext(raw) is None


def test_padding_that_is_not_signed_never_rides_in_a_bundle():
    """Review I5: only the signed shape is forwarded; an oversized item is
    left out rather than crowding the other names out of part 0."""
    padded = {**_item(1), "junk": "x" * 100_000}
    huge = _item(2, pad=5_000)  # over MAX_PROFILE_ITEM_BYTES even when kept
    parts = b.build_bundle_parts(
        "sp",
        [
            b.BundleEntry(epoch=0, item=padded, picture_hash=None),
            b.BundleEntry(epoch=0, item=huge, picture_hash=None),
            _entry(3),
        ],
    )
    [first] = [_parse(raw) for raw in parts]
    assert [x["item"]["author_user_id"] for x in first["items"]] == ["u1", "u3"]
    assert "junk" not in first["items"][0]["item"]
