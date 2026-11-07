"""The ``media_refs`` descriptor (:mod:`socialhome.domain.space_media_ref`)."""

from __future__ import annotations

import logging

import pytest

from socialhome.domain.link_preview import LinkPreview
from socialhome.domain.post import FileMeta, Post, PostType
from socialhome.domain.space_media_ref import (
    MAX_DESCRIPTOR_ENTRIES,
    MAX_PAGE_DESCRIPTOR_ENTRIES,
    MAX_PAGE_FILES,
    MEDIA_REF_RESOURCES,
    MEDIA_REFS_FIELD,
    SpaceMediaLocalization,
    SpaceMediaRef,
    media_refs_to_wire,
    page_body_media_refs,
    page_file_count,
    page_media_urls,
    parse_media_refs,
    post_media_urls,
    record_media_urls,
    rewrite_page_body_media,
    space_page_media_name,
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
    assert MEDIA_REF_RESOURCES == frozenset({"posts", "calendar", "gallery", "pages"})


# ── A page's files: its cover and the pictures its body shows ───────────

BODY = (
    "Intro ![a](api/media/a.webp) and ![b](/api/media/b.webp?exp=1&sig=x)\n"
    "a [link](https://example.org/api/media/x.webp) stays text, "
    "so does path/api/media/y.webp; again ![a2](api/media/a.webp)"
)


def test_a_page_body_lists_only_its_own_local_pictures() -> None:
    """``api/media/<file>`` (leading ``/`` and query kept) — never the same
    path inside another URL or path."""
    assert page_body_media_refs(BODY) == [
        "api/media/a.webp",
        "/api/media/b.webp?exp=1&sig=x",
        "api/media/a.webp",
    ]
    assert page_body_media_refs(None) == []
    assert page_body_media_refs(3) == []


def test_a_page_body_rewrite_touches_only_its_pictures() -> None:
    out = rewrite_page_body_media(BODY, lambda u: u.replace("media/", "M/"))
    assert out == (
        "Intro ![a](api/M/a.webp) and ![b](/api/M/b.webp?exp=1&sig=x)\n"
        "a [link](https://example.org/api/media/x.webp) stays text, "
        "so does path/api/media/y.webp; again ![a2](api/M/a.webp)"
    )
    assert rewrite_page_body_media("![r](api/media/r.webp)", lambda u: "") == "![r]()"


def test_a_pages_files_are_its_cover_then_body_pictures_once_each() -> None:
    assert page_media_urls("api/media/c.webp", BODY) == [
        "api/media/c.webp",
        "api/media/a.webp",
        "/api/media/b.webp",
    ]
    assert page_media_urls(None, "") == []
    record = {"cover_image_url": "api/media/c.webp", "content": BODY}
    assert record_media_urls("pages", record) == page_media_urls(
        "api/media/c.webp", BODY
    )


def test_a_pages_descriptor_never_exceeds_its_entry_cap() -> None:
    """A receiver refuses a descriptor past the cap whole: a page with
    more pictures describes its first ones only (one file once, however
    it is spelled)."""
    body = " ".join(f"![p](api/media/p{i}.webp)" for i in range(30))
    body = "![dup](/api/media/p0.webp) " + body
    urls = record_media_urls("pages", {"cover_image_url": None, "content": body})
    assert len(urls) == MAX_DESCRIPTOR_ENTRIES
    assert urls[0] == "/api/media/p0.webp"
    assert "api/media/p0.webp" not in urls


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


def test_a_page_scoped_name_binds_space_and_file() -> None:
    """v_82: where a member keeps a page's file — fixed length, the
    extension kept, a different name per space and per file, never the
    name the page shows."""
    a = space_page_media_name("sp-1", "sd-" + "1" * 40 + ".webp")
    assert a.startswith("sp-") and a.endswith(".webp") and len(a) == 48
    assert a != space_page_media_name("sp-2", "sd-" + "1" * 40 + ".webp")
    assert a != space_page_media_name("sp-1", "other.webp")
    assert space_page_media_name("sp-1", "noext").startswith("sp-")


def test_a_pages_file_count_counts_each_file_once() -> None:
    body = "![a](api/media/a.webp) ![a2](/api/media/a.webp?x=1) ![b](api/media/b.webp)"
    assert page_file_count("api/media/c.webp", body) == 3
    assert page_file_count("api/media/a.webp", body) == 2
    assert page_file_count(None, None) == 0
    assert MAX_PAGE_DESCRIPTOR_ENTRIES == MAX_PAGE_FILES * 4
