"""The sender-side file hasher (:mod:`socialhome.services.media_file_hasher`)."""

from __future__ import annotations

import hashlib
import os
import pathlib

from socialhome.domain.file_hashes import (
    FILE_HASH_SUITE_SHA256,
    FILES_FIELD,
    FILES_SUITE_FIELD,
    MAX_FILE_HASH_ENTRIES,
    FileHash,
)
from socialhome.services.media_file_hasher import MediaFileHasher


def _put(media: pathlib.Path, name: str, data: bytes) -> str:
    (media / name).write_bytes(data)
    return f"api/media/{name}"


async def test_hashes_each_file_once_by_its_name(tmp_path: pathlib.Path) -> None:
    a = _put(tmp_path, "a.webp", b"aaa")
    b = _put(tmp_path, "b.webp", b"bbbb")
    hasher = MediaFileHasher(tmp_path)
    got = await hasher.hashes_for([a, "/" + a + "?sig=1", b, None, ""])
    assert got == {
        "a.webp": FileHash(hashlib.sha256(b"aaa").hexdigest(), 3),
        "b.webp": FileHash(hashlib.sha256(b"bbbb").hexdigest(), 4),
    }


async def test_missing_and_unsafe_names_are_skipped(tmp_path: pathlib.Path) -> None:
    hasher = MediaFileHasher(tmp_path)
    assert await hasher.hashes_for(["api/media/nope.webp", "api/media/../x"]) == {}
    assert await hasher.hash_name(".hidden") is None


async def test_a_large_file_hashes_in_parts(tmp_path: pathlib.Path) -> None:
    data = os.urandom(3 * 1024 * 1024 + 17)
    _put(tmp_path, "v.mp4", data)
    got = await MediaFileHasher(tmp_path).hash_name("v.mp4")
    assert got == FileHash(hashlib.sha256(data).hexdigest(), len(data))


async def test_a_rewritten_file_is_hashed_again(tmp_path: pathlib.Path) -> None:
    _put(tmp_path, "a.webp", b"one")
    hasher = MediaFileHasher(tmp_path)
    first = await hasher.hash_name("a.webp")
    path = tmp_path / "a.webp"
    path.write_bytes(b"other")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    second = await hasher.hash_name("a.webp")
    assert first != second
    assert second == FileHash(hashlib.sha256(b"other").hexdigest(), 5)


async def test_at_most_the_map_cap(tmp_path: pathlib.Path) -> None:
    urls = [_put(tmp_path, f"f{i}.webp", bytes([i])) for i in range(20)]
    got = await MediaFileHasher(tmp_path).hashes_for(urls)
    assert len(got) == MAX_FILE_HASH_ENTRIES
    assert list(got) == [f"f{i}.webp" for i in range(MAX_FILE_HASH_ENTRIES)]


async def test_with_hashes_adds_the_map_to_a_new_dict(tmp_path: pathlib.Path) -> None:
    a = _put(tmp_path, "a.webp", b"aaa")
    payload = {"id": "p1", "media_url": a}
    out = await MediaFileHasher(tmp_path).with_hashes(payload, [a])
    assert payload == {"id": "p1", "media_url": a}
    assert out[FILES_SUITE_FIELD] == FILE_HASH_SUITE_SHA256
    assert out[FILES_FIELD] == {"a.webp": {"h": hashlib.sha256(b"aaa").hexdigest()}}
    # Nothing to hash: the payload unchanged.
    assert await MediaFileHasher(tmp_path).with_hashes(payload, []) == payload


async def test_with_hashes_without_a_hasher_is_a_no_op() -> None:
    assert await MediaFileHasher.attach(None, {"a": 1}, ["api/media/x"]) == {"a": 1}
