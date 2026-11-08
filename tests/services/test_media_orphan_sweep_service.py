"""Tests for the media orphan sweep — the safety-critical 'keep' rules."""

import os
import time

import pytest

from socialhome.domain.file_hashes import space_content_media_name
from socialhome.media.cleanup import unlink_unreferenced
from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo
from socialhome.services import dm_media_sync_service as dm
from socialhome.services import space_media_sync_service as space
from socialhome.services.media_orphan_sweep_service import (
    GRACE_SECONDS,
    PARTIAL_GRACE_SECONDS,
    MediaOrphanSweepService,
)

pytestmark = pytest.mark.asyncio


class _FakeRefRepo:
    def __init__(self, names):
        self._names = set(names)

    async def referenced_basenames(self):
        return set(self._names)


class _FakeTranscodeRepo:
    def __init__(self, active):
        self._active = set(active)

    async def active_source_paths(self):
        return set(self._active)


def _write(dir_, name, *, age_seconds=0):
    p = dir_ / name
    p.write_bytes(b"x")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(p, (old, old))
    return p


async def test_deletes_only_old_unreferenced_files(tmp_path):
    referenced = _write(tmp_path, "keep-me.webp", age_seconds=2 * GRACE_SECONDS)
    fresh_orphan = _write(tmp_path, "fresh.webp")  # mtime ~now
    old_orphan = _write(tmp_path, "old.webp", age_seconds=2 * GRACE_SECONDS)
    # DM intermediates (owned by dm_gc) — old + unreferenced but skip-patterned.
    preview = _write(tmp_path, "abc123.preview.webp", age_seconds=2 * GRACE_SECONDS)
    part = _write(tmp_path, "abc123.part00001", age_seconds=2 * GRACE_SECONDS)
    assembled = _write(tmp_path, "abc123.assembled.webp", age_seconds=2 * GRACE_SECONDS)
    # A staging subdirectory must never be touched.
    partial = tmp_path / ".partial"
    partial.mkdir()
    (partial / "chunk").write_bytes(b"y")

    svc = MediaOrphanSweepService(
        media_dir=tmp_path,
        reference_repo=_FakeRefRepo({"keep-me.webp"}),
    )
    removed = await svc.sweep_once()

    assert removed == 1
    assert not old_orphan.exists()  # the only thing deleted
    assert referenced.exists()  # referenced → kept
    assert fresh_orphan.exists()  # within grace → kept
    assert preview.exists() and part.exists() and assembled.exists()  # skip-pattern
    assert (partial / "chunk").exists()  # subdir skipped


async def test_missing_media_dir_is_noop(tmp_path):
    svc = MediaOrphanSweepService(
        media_dir=tmp_path / "does-not-exist",
        reference_repo=_FakeRefRepo(set()),
    )
    assert await svc.sweep_once() == 0


async def test_empty_reference_set_still_respects_grace(tmp_path):
    fresh = _write(tmp_path, "fresh.webp")
    old = _write(tmp_path, "old.webp", age_seconds=2 * GRACE_SECONDS)
    svc = MediaOrphanSweepService(
        media_dir=tmp_path, reference_repo=_FakeRefRepo(set())
    )
    removed = await svc.sweep_once()
    assert removed == 1
    assert fresh.exists()
    assert not old.exists()


async def test_transcode_src_sweep_noop_without_repo(tmp_path):
    src_dir = tmp_path / "transcode_src"
    src_dir.mkdir()
    _write(src_dir, "stale.bin", age_seconds=2 * GRACE_SECONDS)
    svc = MediaOrphanSweepService(
        media_dir=tmp_path, reference_repo=_FakeRefRepo(set())
    )
    assert await svc.sweep_transcode_src_once() == 0
    assert (src_dir / "stale.bin").exists()


async def test_transcode_src_sweep_missing_dir_is_noop(tmp_path):
    svc = MediaOrphanSweepService(
        media_dir=tmp_path,
        reference_repo=_FakeRefRepo(set()),
        media_transcode_repo=_FakeTranscodeRepo(set()),
    )
    assert await svc.sweep_transcode_src_once() == 0


async def test_transcode_src_sweep_keeps_referenced_and_young(tmp_path):
    src_dir = tmp_path / "transcode_src"
    src_dir.mkdir()
    referenced = _write(src_dir, "live.bin", age_seconds=2 * GRACE_SECONDS)
    old_orphan = _write(src_dir, "leaked.bin", age_seconds=2 * GRACE_SECONDS)
    young_orphan = _write(src_dir, "fresh.bin")  # within grace

    svc = MediaOrphanSweepService(
        media_dir=tmp_path,
        reference_repo=_FakeRefRepo(set()),
        media_transcode_repo=_FakeTranscodeRepo({str(referenced)}),
    )
    removed = await svc.sweep_transcode_src_once()

    assert removed == 1
    assert referenced.exists()  # still referenced by a job row
    assert young_orphan.exists()  # within grace
    assert not old_orphan.exists()  # leaked + old → reaped


async def test_top_level_sweep_ignores_transcode_src_subdir(tmp_path):
    # The subdir itself (and an old orphan inside it) must survive the
    # top-level pass — only sweep_transcode_src_once touches that dir.
    src_dir = tmp_path / "transcode_src"
    src_dir.mkdir()
    inside = _write(src_dir, "leaked.bin", age_seconds=2 * GRACE_SECONDS)
    old_top = _write(tmp_path, "old.webp", age_seconds=2 * GRACE_SECONDS)

    svc = MediaOrphanSweepService(
        media_dir=tmp_path,
        reference_repo=_FakeRefRepo(set()),
        media_transcode_repo=_FakeTranscodeRepo(set()),
    )
    removed = await svc.sweep_once()

    # Only the top-level orphan was removed by sweep_once.
    assert removed == 1
    assert not old_top.exists()
    assert src_dir.is_dir()
    assert inside.exists()


# ── .partial/ — chunk sets that never complete ─────────────────────────


def _key_dir(media, key, *, entries, age_seconds=0):
    d = media / ".partial" / key
    d.mkdir(parents=True)
    old = time.time() - age_seconds
    for name in entries:
        f = d / name
        f.write_bytes(b"z")
        if age_seconds:
            os.utime(f, (old, old))
    if age_seconds:
        os.utime(d, (old, old))
    return d


async def test_partial_sweep_removes_only_abandoned_sets(tmp_path):
    """A set nothing has touched past the grace (a retracted DM, a sender
    that gave up after MAX_ATTEMPTS, an attempt with another chunk count,
    a crash) goes; a set still receiving chunks stays."""
    old = _key_dir(
        tmp_path,
        "a" * 32,
        entries=["000000", "000003", "assembling-dead", "part.tmp-dead"],
        age_seconds=PARTIAL_GRACE_SECONDS + 60,
    )
    fresh = _key_dir(tmp_path, "b" * 32, entries=["000000"])
    # Old parts but one chunk just landed: still in progress.
    busy = _key_dir(
        tmp_path, "c" * 32, entries=["000000"], age_seconds=PARTIAL_GRACE_SECONDS * 2
    )
    (busy / "000001").write_bytes(b"new")
    svc = MediaOrphanSweepService(media_dir=tmp_path, reference_repo=_FakeRefRepo([]))
    assert await svc.sweep_partial_once() == 1
    assert not old.exists()
    assert fresh.exists() and busy.exists()
    assert (tmp_path / ".partial").is_dir()


async def test_partial_sweep_is_bounded_per_pass(tmp_path):
    for i in range(5):
        _key_dir(
            tmp_path,
            f"{i:032d}",
            entries=["000000"],
            age_seconds=PARTIAL_GRACE_SECONDS + 60,
        )
    svc = MediaOrphanSweepService(media_dir=tmp_path, reference_repo=_FakeRefRepo([]))
    assert await svc.sweep_partial_once(limit=2) == 2
    assert len(list((tmp_path / ".partial").iterdir())) == 3
    assert await svc.sweep_partial_once(limit=10) == 3


async def test_partial_sweep_missing_dir_is_noop(tmp_path):
    svc = MediaOrphanSweepService(media_dir=tmp_path, reference_repo=_FakeRefRepo([]))
    assert await svc.sweep_partial_once() == 0


async def test_partial_grace_outlasts_the_senders_retry_horizon():
    """A set must never be swept while its sender may still retry: six
    attempts with backoff capped at 30 min is well under a day."""
    for mod in (dm, space):
        horizon = mod.MAX_ATTEMPTS * mod.BACKOFF_CAP_SECONDS * 2
        assert PARTIAL_GRACE_SECONDS > horizon


def _outside(tmp_path, *, age_seconds):
    """A directory outside the media dir holding a file worth keeping."""
    out = tmp_path / "outside"
    out.mkdir()
    keep = out / "precious.txt"
    keep.write_bytes(b"not media")
    old = time.time() - age_seconds
    os.utime(keep, (old, old))
    os.utime(out, (old, old))
    return out, keep


async def test_partial_sweep_never_follows_a_symlinked_set(tmp_path):
    """Regression: a symlink in ``.partial`` pointing outside made the sweep
    list the target and delete what was there. The link itself goes; its
    target and everything in it stay."""
    media = tmp_path / "media"
    out, keep = _outside(tmp_path, age_seconds=PARTIAL_GRACE_SECONDS * 2)
    partial = media / ".partial"
    partial.mkdir(parents=True)
    link = partial / ("d" * 32)
    link.symlink_to(out, target_is_directory=True)
    old = time.time() - PARTIAL_GRACE_SECONDS * 2
    os.utime(link, (old, old), follow_symlinks=False)
    svc = MediaOrphanSweepService(media_dir=media, reference_repo=_FakeRefRepo([]))
    assert await svc.sweep_partial_once() == 1
    assert not link.is_symlink() and not link.exists()
    assert keep.read_bytes() == b"not media"


async def test_partial_sweep_ages_a_fresh_symlink_by_the_link_itself(tmp_path):
    """The age is the link's own mtime (``lstat``), never the target's —
    a fresh link to an old directory is not abandoned yet."""
    media = tmp_path / "media"
    out, keep = _outside(tmp_path, age_seconds=PARTIAL_GRACE_SECONDS * 2)
    partial = media / ".partial"
    partial.mkdir(parents=True)
    link = partial / ("e" * 32)
    link.symlink_to(out, target_is_directory=True)
    svc = MediaOrphanSweepService(media_dir=media, reference_repo=_FakeRefRepo([]))
    assert await svc.sweep_partial_once() == 0
    assert link.is_symlink()
    assert keep.exists()


async def test_partial_sweep_never_descends_into_a_symlink_inside_a_set(tmp_path):
    """A symlink inside an abandoned set is unlinked, not followed."""
    media = tmp_path / "media"
    out, keep = _outside(tmp_path, age_seconds=PARTIAL_GRACE_SECONDS * 2)
    d = _key_dir(
        media, "f" * 32, entries=["000000"], age_seconds=PARTIAL_GRACE_SECONDS + 60
    )
    inner = d / "000001"
    inner.symlink_to(out, target_is_directory=True)
    old = time.time() - PARTIAL_GRACE_SECONDS * 2
    os.utime(inner, (old, old), follow_symlinks=False)
    os.utime(d, (old, old))
    svc = MediaOrphanSweepService(media_dir=media, reference_repo=_FakeRefRepo([]))
    assert await svc.sweep_partial_once() == 1
    assert not d.exists()
    assert keep.read_bytes() == b"not media"


async def test_partial_sweep_skips_a_symlinked_partial_root(tmp_path):
    """``.partial`` itself a symlink: nothing behind it is touched."""
    media = tmp_path / "media"
    media.mkdir()
    out, keep = _outside(tmp_path, age_seconds=PARTIAL_GRACE_SECONDS * 2)
    (media / ".partial").symlink_to(out, target_is_directory=True)
    svc = MediaOrphanSweepService(media_dir=media, reference_repo=_FakeRefRepo([]))
    assert await svc.sweep_partial_once() == 0
    assert keep.read_bytes() == b"not media"


async def test_partial_sweep_does_not_count_a_set_it_could_not_remove(tmp_path):
    """Review F3: a planted sub-directory keeps its set; the sweep must not
    report it removed on every pass."""
    media = tmp_path / "media"
    d = _key_dir(
        media, "9" * 32, entries=["000000"], age_seconds=PARTIAL_GRACE_SECONDS + 60
    )
    (d / "nested").mkdir()
    old = time.time() - PARTIAL_GRACE_SECONDS * 2
    os.utime(d / "nested", (old, old))
    os.utime(d, (old, old))
    svc = MediaOrphanSweepService(media_dir=media, reference_repo=_FakeRefRepo([]))
    assert await svc.sweep_partial_once() == 0
    assert (d / "nested").is_dir()


# ── v_85: content-addressed space files ──────────────────────────────────


async def test_content_addressed_files_are_kept_while_referenced(db, tmp_path):
    """A content-addressed (``sc-``) file is shared by every item of its
    space that shows the same bytes: kept while any row references it,
    swept once orphaned past the 24 h grace."""
    shared = space_content_media_name("sp-1", "a" * 64, "a.webp")
    orphan = space_content_media_name("sp-1", "b" * 64, "b.webp")
    young = space_content_media_name("sp-1", "c" * 64, "c.webp")
    for name, age in ((shared, 2), (orphan, 2), (young, 0)):
        _write(tmp_path, name, age_seconds=age * GRACE_SECONDS)
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'S', 'host', 'anna', ?)",
        ("00" * 32,),
    )
    for post_id in ("p-1", "p-2"):
        await db.enqueue(
            "INSERT INTO space_posts(id, space_id, author, type, content,"
            " image_urls_json) VALUES(?, 'sp-1', 'u', 'image', '', ?)",
            (post_id, f'["api/media/{shared}"]'),
        )
    refs = SqliteMediaReferenceRepo(db)
    # One of the two posts goes: its delete path keeps the shared file.
    await db.enqueue("DELETE FROM space_posts WHERE id='p-1'", ())
    await unlink_unreferenced(tmp_path, refs, [f"api/media/{shared}"])
    assert (tmp_path / shared).exists()
    svc = MediaOrphanSweepService(media_dir=tmp_path, reference_repo=refs)
    assert await svc.sweep_once() == 1
    assert (tmp_path / shared).exists()
    assert not (tmp_path / orphan).exists()
    assert (tmp_path / young).exists()  # inside the grace window
    # The last post goes: the file goes with it.
    await db.enqueue("DELETE FROM space_posts WHERE id='p-2'", ())
    await unlink_unreferenced(tmp_path, refs, [f"api/media/{shared}"])
    assert not (tmp_path / shared).exists()
