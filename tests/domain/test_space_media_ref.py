"""The ``media_refs`` descriptor (:mod:`socialhome.domain.space_media_ref`)."""

from __future__ import annotations

import logging

import pytest

from socialhome.domain.space_media_ref import (
    MEDIA_REFS_FIELD,
    SpaceMediaRef,
    media_refs_to_wire,
    parse_media_refs,
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
