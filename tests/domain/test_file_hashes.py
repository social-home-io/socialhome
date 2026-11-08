"""File hashes in space content (:mod:`socialhome.domain.file_hashes`, v_85)."""

from __future__ import annotations

import hashlib

import pytest

from socialhome.domain.file_hashes import (
    FILE_HASH_SUITE_SHA256,
    FILES_FIELD,
    FILES_SUITE_FIELD,
    MAX_FILE_HASH_ENTRIES,
    SUPPORTED_FILE_HASH_SUITES,
    FileHash,
    UnsupportedFileHashSuite,
    blob_hash,
    file_hashes_to_wire,
    is_space_content_media_name,
    parse_file_hashes,
    require_file_hash_suite,
    sha256_hex,
    space_content_media_name,
)
from socialhome.domain.media_constraints import SPACE_CONTENT_MEDIA_PREFIX

H1 = hashlib.sha256(b"one").hexdigest()
H2 = hashlib.sha256(b"two").hexdigest()


def _wire(files: dict, suite: object = FILE_HASH_SUITE_SHA256) -> dict:
    return {FILES_FIELD: files, FILES_SUITE_FIELD: suite}


def test_suite_constants() -> None:
    assert FILE_HASH_SUITE_SHA256 == "sha256"
    assert FILE_HASH_SUITE_SHA256 in SUPPORTED_FILE_HASH_SUITES
    assert issubclass(UnsupportedFileHashSuite, ValueError)
    assert require_file_hash_suite("sha256") == "sha256"
    with pytest.raises(UnsupportedFileHashSuite):
        require_file_hash_suite("blake3")
    with pytest.raises(UnsupportedFileHashSuite):
        require_file_hash_suite(None)


def test_round_trip() -> None:
    hashes = {"a.webp": FileHash(H1, 3), "b.mp4": FileHash(H2, 3)}
    wire = file_hashes_to_wire(hashes)
    assert wire == _wire({"a.webp": {"h": H1}, "b.mp4": {"h": H2}})
    assert {n: fh.h for n, fh in parse_file_hashes(wire).items()} == {
        "a.webp": H1,
        "b.mp4": H2,
    }


def test_empty_map_puts_nothing_on_the_wire() -> None:
    assert file_hashes_to_wire({}) == {}
    assert parse_file_hashes({}) == {}
    assert parse_file_hashes({"content": "x"}) == {}


@pytest.mark.parametrize("suite", [None, "", "blake3", 1, "SHA256"])
def test_unknown_or_missing_suite_rejects_the_whole_map(suite: object) -> None:
    payload = _wire({"a.webp": {"h": H1}}, suite)
    if suite is None:
        payload.pop(FILES_SUITE_FIELD)
    assert parse_file_hashes(payload) == {}


@pytest.mark.parametrize(
    "entry",
    [
        {"h": H1.upper()},
        {"h": H1[:-1]},
        {"h": "z" * 64},
        {"hash": H1},
        "nope",
    ],
)
def test_one_bad_entry_rejects_the_whole_map(entry: object) -> None:
    payload = _wire({"a.webp": {"h": H2}, "b.webp": entry})
    assert parse_file_hashes(payload) == {}


@pytest.mark.parametrize("name", ["", "../x", "a/b", ".hidden", "a b", "x" * 201])
def test_unsafe_names_reject_the_whole_map(name: str) -> None:
    assert parse_file_hashes(_wire({name: {"h": H1}})) == {}


def test_not_a_dict_is_rejected() -> None:
    assert parse_file_hashes(_wire(["a.webp"])) == {}  # type: ignore[arg-type]


def test_more_than_the_cap_is_rejected_whole() -> None:
    files = {f"f{i}.webp": {"h": H1} for i in range(MAX_FILE_HASH_ENTRIES + 1)}
    assert parse_file_hashes(_wire(files)) == {}
    files.popitem()
    assert len(parse_file_hashes(_wire(files))) == MAX_FILE_HASH_ENTRIES


def test_content_name_is_bound_to_space_and_hash() -> None:
    name = space_content_media_name("sp-1", H1, "abc.webp")
    assert name.startswith(SPACE_CONTENT_MEDIA_PREFIX)
    assert name.endswith(".webp")
    assert len(name) == len("sc-") + 40 + len(".webp")
    assert is_space_content_media_name(name)
    # Deterministic, independent of the author's file name (except the ext).
    assert name == space_content_media_name("sp-1", H1, "other.webp")
    # A cross-space hash never matches.
    assert name != space_content_media_name("sp-2", H1, "abc.webp")
    assert name != space_content_media_name("sp-1", H2, "abc.webp")
    assert not is_space_content_media_name("sd-abc.webp")


def test_content_name_drops_a_long_extension() -> None:
    name = space_content_media_name("s", H1, "noext")
    assert "." not in name
    assert "." not in space_content_media_name("s", H1, "a.verylongextension")


def test_sha256_hex_and_blob_hash() -> None:
    assert sha256_hex(b"one") == H1
    assert blob_hash({"h": H1, "h_suite": "sha256"}) == H1
    assert blob_hash({"h": H1}) is None  # no suite: legacy
    assert blob_hash({"h": H1, "h_suite": "md5"}) is None
    assert blob_hash({"h": "nothex", "h_suite": "sha256"}) is None
    assert blob_hash({}) is None


def test_the_wire_carries_no_size_and_ignores_one():
    """#1322 review: a size nobody can check against the bytes is not on the
    wire (the hash refuses truncation); one sent anyway is ignored."""
    wire = file_hashes_to_wire({"a.webp": FileHash(H1, 3)})
    assert wire[FILES_FIELD] == {"a.webp": {"h": H1}}
    parsed = parse_file_hashes(_wire({"a.webp": {"h": H1, "size": "junk"}}))
    assert list(parsed) == ["a.webp"] and parsed["a.webp"].h == H1
