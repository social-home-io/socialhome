"""Tests for the fail-soft media-file unlink helper."""

import pathlib

import pytest

from socialhome.media.cleanup import (
    BATCH_REFERENCE_THRESHOLD,
    media_basename,
    unlink_media,
    unlink_unreferenced,
)


def test_media_basename_strips_prefix_and_query():
    assert media_basename("api/media/a.webp") == "a.webp"
    assert media_basename("/api/media/a.webp?v=3") == "a.webp"
    assert media_basename("bare.webp") == "bare.webp"


def test_media_basename_rejects_empty_and_traversal():
    assert media_basename(None) is None
    assert media_basename("") is None
    assert media_basename("api/media/..") is None


pytestmark = pytest.mark.asyncio


async def _touch(d: pathlib.Path, name: str) -> pathlib.Path:
    p = d / name
    p.write_bytes(b"x")
    return p


async def test_removes_existing_file(tmp_path):
    p = await _touch(tmp_path, "abc.webp")
    assert await unlink_media(tmp_path, "api/media/abc.webp") is True
    assert not p.exists()


async def test_tolerates_leading_slash_and_query(tmp_path):
    await _touch(tmp_path, "abc.webp")
    assert await unlink_media(tmp_path, "/api/media/abc.webp?v=2") is True
    assert not (tmp_path / "abc.webp").exists()


async def test_missing_file_is_noop(tmp_path):
    assert await unlink_media(tmp_path, "api/media/gone.webp") is False


async def test_none_and_empty_url(tmp_path):
    assert await unlink_media(tmp_path, None) is False
    assert await unlink_media(tmp_path, "") is False


async def test_no_path_traversal(tmp_path):
    # A file outside media_dir must never be reached — only the basename
    # is used, so this resolves to media_dir/passwd (which doesn't exist).
    outside = tmp_path.parent / "secret.txt"
    outside.write_bytes(b"keep me")
    assert await unlink_media(tmp_path, "api/media/../../secret.txt") is False
    assert outside.exists()


class _Refs:
    def __init__(self, *live: str) -> None:
        self.live = set(live)
        self.asked: list[str] = []

    async def referenced_basenames(self) -> set[str]:
        return set(self.live)

    async def is_referenced(self, basename: str) -> bool:
        self.asked.append(basename)
        return basename in self.live


async def test_unlink_unreferenced_keeps_a_file_another_row_still_uses(tmp_path):
    shared = await _touch(tmp_path, "shared.webp")
    own = await _touch(tmp_path, "own.webp")
    refs = _Refs("shared.webp")
    removed = await unlink_unreferenced(
        tmp_path, refs, ["api/media/shared.webp", "/api/media/own.webp?v=1", None]
    )
    assert removed == 1
    assert shared.exists()
    assert not own.exists()
    assert refs.asked == ["shared.webp", "own.webp"]


async def test_unlink_unreferenced_keeps_files_without_a_reference_check(tmp_path):
    """No way to tell whether another row uses the file → keep it (the
    orphan sweep reclaims it once nothing does)."""
    p = await _touch(tmp_path, "abc.webp")
    assert await unlink_unreferenced(tmp_path, None, ["api/media/abc.webp"]) == 0
    assert p.exists()


async def test_unlink_unreferenced_without_a_media_dir_is_a_noop():
    assert await unlink_unreferenced(None, _Refs(), ["api/media/abc.webp"]) == 0


async def test_unlink_unreferenced_ignores_duplicates_and_bad_names(tmp_path):
    await _touch(tmp_path, "a.webp")
    refs = _Refs()
    removed = await unlink_unreferenced(
        tmp_path, refs, ["api/media/a.webp", "a.webp", "", "api/media/.."]
    )
    assert removed == 1
    assert refs.asked == ["a.webp"]


async def test_unlink_unreferenced_reads_the_reference_set_once_for_a_batch(tmp_path):
    names = [f"f{i}.webp" for i in range(BATCH_REFERENCE_THRESHOLD + 2)]
    for n in names:
        await _touch(tmp_path, n)
    refs = _Refs(names[0])
    removed = await unlink_unreferenced(
        tmp_path, refs, [f"api/media/{n}" for n in names]
    )
    assert removed == len(names) - 1
    assert refs.asked == []  # no per-file query
    assert (tmp_path / names[0]).exists()


async def test_unlink_unreferenced_keeps_files_when_the_check_fails(tmp_path):
    p = await _touch(tmp_path, "abc.webp")

    class _Broken(_Refs):
        async def is_referenced(self, basename: str) -> bool:
            raise RuntimeError("db gone")

    assert await unlink_unreferenced(tmp_path, _Broken(), ["api/media/abc.webp"]) == 0
    assert p.exists()
