"""The ``media_refs`` descriptor (:mod:`socialhome.domain.space_media_ref`)."""

from __future__ import annotations

import logging

import pytest

from socialhome.domain.link_preview import LinkPreview
from socialhome.domain.post import FileMeta, Post, PostType
from socialhome.domain.space_media_ref import (
    MAX_DESCRIPTOR_ENTRIES,
    MEDIA_REF_RESOURCES,
    MEDIA_REFS_FIELD,
    SpaceMediaLocalization,
    SpaceMediaRef,
    media_refs_to_wire,
    parse_media_refs,
    post_media_urls,
    record_media_urls,
)
from socialhome.moment_public_media import MAX_BLOB_BYTES
from socialhome.space_media_seal import SPACE_MEDIA_SUITE_AESGCM256_PADDED

SUITE = SPACE_MEDIA_SUITE_AESGCM256_PADDED
BID = "a" * 64


def _ref(name: str = "x.webp", **kw) -> SpaceMediaRef:
    args = {
        "filename": name,
        "space_id": "sp",
        "blob_id": BID,
        "suite": SUITE,
        "epoch": 2,
        "size": 65_564,
        "scope": "space",
        "addr_suite": "sm1",
    }
    args.update(kw)
    return SpaceMediaRef(**args)


def test_field_name() -> None:
    assert MEDIA_REFS_FIELD == "media_refs"


def test_wire_round_trip() -> None:
    refs = [_ref("x.webp"), _ref("y.webp", blob_id="b" * 64)]
    wire = media_refs_to_wire(refs)
    common = {"suite": SUITE, "epoch": 2, "size": 65_564, "scope": "space"}
    assert wire == {
        "x.webp": {"blob_id": BID, "addr_suite": "sm1", **common},
        "y.webp": {"blob_id": "b" * 64, "addr_suite": "sm1", **common},
    }
    parsed = parse_media_refs(wire, space_id="sp", allowed={"x.webp", "y.webp"})
    assert sorted(parsed, key=lambda r: r.filename) == refs


def test_not_a_dict_is_empty() -> None:
    assert parse_media_refs(None, space_id="sp", allowed={"x.webp"}) == []
    assert parse_media_refs([1], space_id="sp", allowed={"x.webp"}) == []


def test_only_the_items_own_files() -> None:
    """A descriptor may only name files the item itself references — never
    another file of this household."""
    wire = media_refs_to_wire([_ref("x.webp"), _ref("secret.webp")])
    parsed = parse_media_refs(wire, space_id="sp", allowed={"x.webp", "y.webp"})
    assert [r.filename for r in parsed] == ["x.webp"]


def test_bounded_by_the_items_media_count() -> None:
    wire = {f"f{i}.webp": _ref(f"f{i}.webp").to_wire() for i in range(50)}
    # More entries than the item has files: refused whole, never walked.
    assert parse_media_refs(wire, space_id="sp", allowed={"f1.webp", "f2.webp"}) == []
    two = {k: wire[k] for k in ("f1.webp", "f2.webp")}
    assert (
        len(parse_media_refs(two, space_id="sp", allowed={"f1.webp", "f2.webp"})) == 2
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"blob_id": "nothex"},
        {"blob_id": "A" * 64},
        {"epoch": -1},
        {"epoch": True},
        {"epoch": "2"},
        {"size": 0},
        {"size": MAX_BLOB_BYTES + 1},
        {"size": 1.5},
    ],
)
def test_malformed_entry_skipped(bad: dict) -> None:
    entry = {**_ref().to_wire(), **bad}
    assert parse_media_refs({"x.webp": entry}, space_id="sp", allowed={"x.webp"}) == []


def test_extra_keys_skipped() -> None:
    entry = {**_ref().to_wire(), "url": "https://gfs.example/x"}
    assert parse_media_refs({"x.webp": entry}, space_id="sp", allowed={"x.webp"}) == []


def test_unknown_suite_refused_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    entry = {**_ref().to_wire(), "suite": "rot13"}
    with caplog.at_level(logging.WARNING):
        assert (
            parse_media_refs({"x.webp": entry}, space_id="sp", allowed={"x.webp"}) == []
        )
    assert "suite" in caplog.text


def test_dataclass_is_frozen() -> None:
    ref = _ref()
    with pytest.raises(AttributeError):
        ref.epoch = 3  # type: ignore[misc]


def test_the_scope_rides_and_a_channel_scope_is_kept() -> None:
    """The address is bound to the scope it was uploaded under: the reader
    learns the KIND from the descriptor and checks it with the scope id it
    knows itself (the space id, or its own channel id)."""
    entry = _ref(scope="channel").to_wire()
    (ref,) = parse_media_refs({"x.webp": entry}, space_id="sp", allowed={"x.webp"})
    assert ref.scope == "channel"
    assert "scope_id" not in entry  # never named on the wire


@pytest.mark.parametrize("bad", [{"scope": "galaxy"}, {"scope": None}])
def test_unknown_scope_skipped(bad: dict) -> None:
    entry = {**_ref().to_wire(), **bad}
    assert parse_media_refs({"x.webp": entry}, space_id="sp", allowed={"x.webp"}) == []


def test_unknown_address_suite_refused_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = {**_ref().to_wire(), "addr_suite": "sm9"}
    with caplog.at_level(logging.WARNING):
        assert (
            parse_media_refs({"x.webp": entry}, space_id="sp", allowed={"x.webp"}) == []
        )
    assert "address suite" in caplog.text


# ── One list of an item's files, for the sender and every receiver ──────


def test_record_media_urls_per_resource() -> None:
    record = {
        "media_url": "api/media/m.webp",
        "image_urls": ["api/media/a.webp", "api/media/b.webp", 3],
        "file_meta": {"url": "api/media/doc.pdf"},
        "link_preview": {"thumbnail_url": "api/media/card.webp"},
    }
    assert record_media_urls("posts", record) == [
        "api/media/m.webp",
        "api/media/a.webp",
        "api/media/b.webp",
        "api/media/doc.pdf",
        "api/media/card.webp",
    ]
    assert record_media_urls("posts", {"image_urls": "nope", "file_meta": 1}) == []
    assert record_media_urls("calendar", {"cover_url": "api/media/c.webp"}) == [
        "api/media/c.webp"
    ]
    item = {
        "kind": "item",
        "thumbnail_url": "/api/media/t.jpg",
        "url": "/api/media/f.jpg",
    }
    assert record_media_urls("gallery", item) == [
        "/api/media/t.jpg",
        "/api/media/f.jpg",
    ]
    assert record_media_urls("gallery", {"kind": "album", "cover_url": "x"}) == []
    assert record_media_urls("tasks", {"media_url": "api/media/x.webp"}) == []
    assert MEDIA_REF_RESOURCES == frozenset({"posts", "calendar", "gallery"})


def test_post_media_urls_is_the_record_rule_for_a_post() -> None:
    """Regression (#1309 review A): a post with a picture AND a link card
    used to lose all its pictures for GFS readers — the sender described
    the card's thumbnail, the receivers allowed only the pictures, and the
    larger descriptor was refused whole. Sender and receivers now take
    the item's files from this one function."""
    post = Post(
        id="p",
        author="u",
        type=PostType.IMAGE,
        content="look",
        created_at=None,
        image_urls=("api/media/a.webp",),
        link_preview=LinkPreview(
            url="https://example.org",
            title="t",
            thumbnail_url="api/media/card.webp",
        ),
        file_meta=FileMeta(
            url="api/media/doc.pdf",
            mime_type="application/pdf",
            original_name="d.pdf",
            size_bytes=3,
        ),
    )
    assert post_media_urls(post) == [
        "api/media/a.webp",
        "api/media/doc.pdf",
        "api/media/card.webp",
    ]


def test_a_picture_plus_link_card_descriptor_keeps_the_picture() -> None:
    wire = media_refs_to_wire([_ref("a.webp"), _ref("card.webp")])
    parsed = parse_media_refs(wire, space_id="sp", allowed={"a.webp", "card.webp"})
    assert {r.filename for r in parsed} == {"a.webp", "card.webp"}


def test_unknown_entries_are_dropped_not_the_whole_list() -> None:
    """A receiver that shows fewer files than the sender described (an
    older build, a follower's narrower view) keeps what it does show."""
    wire = media_refs_to_wire([_ref("a.webp"), _ref("card.webp"), _ref("x.webp")])
    parsed = parse_media_refs(wire, space_id="sp", allowed={"a.webp"})
    assert [r.filename for r in parsed] == ["a.webp"]


def test_an_oversized_descriptor_is_refused_whole() -> None:
    wire = {
        f"f{i}.webp": _ref(f"f{i}.webp").to_wire()
        for i in range(MAX_DESCRIPTOR_ENTRIES + 1)
    }
    assert parse_media_refs(wire, space_id="sp", allowed=set(wire)) == []


# ── #1309 re-review: an item's files are renamed to the reader's names ──


def test_localization_rewrites_only_the_named_files() -> None:
    loc = SpaceMediaLocalization(names={"a.webp": "sg-1.webp"})
    assert loc.url("api/media/a.webp") == "api/media/sg-1.webp"
    assert loc.url("/api/media/a.webp?v=2") == "/api/media/sg-1.webp?v=2"
    assert loc.url("api/media/b.webp") == "api/media/b.webp"
    assert loc.url(None) is None
    assert loc.url("") == ""


def test_localization_rewrites_every_file_of_a_post() -> None:
    post = Post(
        id="p",
        author="u",
        type=PostType.IMAGE,
        content="look",
        created_at=None,
        media_url="api/media/m.webp",
        image_urls=("api/media/a.webp", "api/media/keep.webp"),
        link_preview=LinkPreview(
            url="https://example.org", title="t", thumbnail_url="api/media/card.webp"
        ),
        file_meta=FileMeta(
            url="api/media/doc.pdf",
            mime_type="application/pdf",
            original_name="d.pdf",
            size_bytes=3,
        ),
    )
    loc = SpaceMediaLocalization(
        names={
            "m.webp": "sg-m.webp",
            "a.webp": "sg-a.webp",
            "card.webp": "sg-c.webp",
            "doc.pdf": "sg-d.pdf",
        }
    )
    out = loc.post(post)
    assert post_media_urls(out) == [
        "api/media/sg-m.webp",
        "api/media/sg-a.webp",
        "api/media/keep.webp",
        "api/media/sg-d.pdf",
        "api/media/sg-c.webp",
    ]
    assert out.link_preview.title == "t"
    assert out.file_meta.original_name == "d.pdf"
    # Nothing to rename: the very same object.
    assert SpaceMediaLocalization(names={}).post(post) is post
