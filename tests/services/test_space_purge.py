"""Tests for the shared space hard-delete helper (``space_purge``)."""

from __future__ import annotations

import pathlib

import pytest

from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.space_purge import purge_space_and_media

pytestmark = pytest.mark.asyncio


class _FakeRefs:
    """Files other rows (outside the purged space) still reference."""

    def __init__(self, *live: str, space: tuple[str, ...] = ()) -> None:
        self._live = set(live)
        self.space: list[str] = list(space)

    async def referenced_basenames(self) -> set[str]:
        return set(self._live)

    async def is_referenced(self, basename: str) -> bool:
        return basename in self._live

    async def space_files(self, space_id: str) -> list[str]:
        return list(self.space)


class _FakeSpaceRepo:
    def __init__(self) -> None:
        self.purged: list[str] = []

    async def purge(self, space_id: str) -> None:
        self.purged.append(space_id)


async def test_purge_drops_rows_and_unlinks_files(tmp_path: pathlib.Path):
    # Two real files; one referenced by a post, one by the gallery.
    (tmp_path / "post.webp").write_bytes(b"P")
    (tmp_path / "gallery.webp").write_bytes(b"G")
    (tmp_path / "unrelated.webp").write_bytes(b"U")
    space_repo = _FakeSpaceRepo()
    removed = await purge_space_and_media(
        space_repo=space_repo,
        media_dir=tmp_path,
        media_refs=_FakeRefs(
            space=(
                "post.webp",
                "gallery.webp",
            )
        ),
        space_id="sp-1",
    )
    assert space_repo.purged == ["sp-1"]  # rows dropped (cascade)
    assert removed == 2
    assert not (tmp_path / "post.webp").exists()
    assert not (tmp_path / "gallery.webp").exists()
    assert (tmp_path / "unrelated.webp").exists()  # untouched


async def test_purge_without_media_dir_still_drops_rows():
    space_repo = _FakeSpaceRepo()
    removed = await purge_space_and_media(
        space_repo=space_repo,
        media_dir=None,
        space_id="sp-9",
    )
    assert space_repo.purged == ["sp-9"]
    assert removed == 0


async def test_purge_tolerates_missing_files(tmp_path: pathlib.Path):
    space_repo = _FakeSpaceRepo()
    removed = await purge_space_and_media(
        space_repo=space_repo,
        media_dir=tmp_path,
        media_refs=_FakeRefs(space=("gone.webp",)),
        space_id="sp-2",
    )
    assert space_repo.purged == ["sp-2"]
    assert removed == 0  # missing file → no-op, no raise


async def test_purge_keeps_files_other_rows_still_reference(tmp_path: pathlib.Path):
    """A purged space's rows may name files that rows elsewhere use; those
    stay on disk."""
    (tmp_path / "borrowed.webp").write_bytes(b"B")
    (tmp_path / "own.webp").write_bytes(b"O")
    removed = await purge_space_and_media(
        space_repo=_FakeSpaceRepo(),
        media_dir=tmp_path,
        media_refs=_FakeRefs(
            "borrowed.webp",
            space=(
                "borrowed.webp",
                "own.webp",
            ),
        ),
        space_id="sp-3",
    )
    assert removed == 1
    assert (tmp_path / "borrowed.webp").exists()
    assert not (tmp_path / "own.webp").exists()


async def test_purge_without_a_reference_check_keeps_files(tmp_path: pathlib.Path):
    (tmp_path / "post.webp").write_bytes(b"P")
    removed = await purge_space_and_media(
        space_repo=_FakeSpaceRepo(),
        media_dir=tmp_path,
        space_id="sp-4",
    )
    assert removed == 0
    assert (tmp_path / "post.webp").exists()


async def test_purge_frees_every_registry_file_of_the_space(tmp_path: pathlib.Path):
    """A post's attachment, a link card, a calendar cover or a page's file
    (``media_refs.space_files`` — the registry's columns) goes with the
    space, unless another row names it."""
    for name in ("doc.pdf", "card.webp", "cover.webp", "kept.webp"):
        (tmp_path / name).write_bytes(b"x")
    refs = _FakeRefs("kept.webp")
    refs.space = ["doc.pdf", "card.webp", "cover.webp", "kept.webp"]
    removed = await purge_space_and_media(
        space_repo=_FakeSpaceRepo(),
        media_dir=tmp_path,
        media_refs=refs,
        space_id="sp-5",
    )
    assert removed == 3
    assert (tmp_path / "kept.webp").exists()
    assert not (tmp_path / "doc.pdf").exists()


_DM = "dm-" + "ab" * 20 + ".webp"


async def test_purge_never_frees_a_reserved_name_a_legacy_row_holds(db, tmp_path):
    """A legacy row of the purged space naming a DM's (``dm-``) file — in
    a post, a comment, a gallery item or a listing — never takes that file
    with the space (real repos + the registry's ``space_files``); the
    space's own file still goes."""
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name) VALUES('u1', 'u1', 'U')"
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp1', 'S', 'self', 'u1', 'k')"
    )
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, media_url,"
        " image_urls_json) VALUES('p1', 'sp1', 'u1', 'image', ?, ?)",
        (f"api/media/{_DM}", f'["api/media/{_DM}", "api/media/own.webp"]'),
    )
    await db.enqueue(
        "INSERT INTO space_post_comments(id, post_id, author, media_url)"
        " VALUES('c1', 'p1', 'u1', ?)",
        (f"api/media/{_DM}",),
    )
    await db.enqueue(
        "INSERT INTO gallery_albums(id, space_id, name) VALUES('al', 'sp1', 'A')"
    )
    await db.enqueue(
        "INSERT INTO gallery_items(id, album_id, uploaded_by, item_type, filename,"
        " thumbnail_filename, width, height) VALUES('g1', 'al', 'u1', 'photo',"
        " ?, ?, 1, 1)",
        (_DM, _DM),
    )
    await db.enqueue(
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id, mode, title,"
        " end_time, currency, image_urls_json) VALUES('p1', 'sp1', 'u1', 'fixed',"
        " 'T', '2099-01-01', 'EUR', ?)",
        (f'["api/media/{_DM}"]',),
    )
    (tmp_path / _DM).write_bytes(b"dm")
    (tmp_path / "own.webp").write_bytes(b"own")
    removed = await purge_space_and_media(
        space_repo=SqliteSpaceRepo(db),
        media_dir=tmp_path,
        media_refs=SqliteMediaReferenceRepo(db),
        space_id="sp1",
    )
    assert (tmp_path / _DM).exists()
    assert not (tmp_path / "own.webp").exists()
    assert removed == 1
