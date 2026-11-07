"""Held space files: stored per (sender, file), adopted write-once, swept."""

from __future__ import annotations

import os

import orjson
import pytest

from socialhome.domain.space_media_sender import HeldSpaceBlob
from socialhome.services.held_space_media import (
    BLOB_NAME,
    HELD_PREFIX,
    META_NAME,
    HeldSpaceMediaStore,
    held_key,
)
from socialhome.services.media_orphan_sweep_service import (
    PARTIAL_GRACE_SECONDS,
    MediaOrphanSweepService,
)


def _held(sender: str = "peer-a", filename: str = "f.webp") -> HeldSpaceBlob:
    return HeldSpaceBlob(
        key=held_key(sender, filename),
        sender=sender,
        space_id="sp-a",
        correlation_id="post-1",
        filename=filename,
    )


async def _hold(store: HeldSpaceMediaStore, held: HeldSpaceBlob, body: bytes):
    path = await store.prepare(held.sender, held.filename)
    path.write_bytes(body)
    await store.record(held)


def test_held_key_binds_sender_and_file():
    assert held_key("a", "f").startswith(HELD_PREFIX)
    assert held_key("a", "f") != held_key("b", "f")
    assert held_key("a", "f") != held_key("a", "g")


async def test_record_then_adopt_moves_the_bytes(tmp_path):
    store = HeldSpaceMediaStore(tmp_path)
    held = _held()
    await _hold(store, held, b"BYTES")
    assert await store.held_for("sp-a") == [held]
    assert await store.adopt(held, tmp_path / "f.webp")
    assert (tmp_path / "f.webp").read_bytes() == b"BYTES"
    assert await store.held_for("sp-a") == []
    assert not (tmp_path / ".partial" / held.key).exists()


async def test_adopt_never_replaces_an_existing_file(tmp_path):
    store = HeldSpaceMediaStore(tmp_path)
    held = _held()
    await _hold(store, held, b"HELD")
    (tmp_path / "f.webp").write_bytes(b"FIRST")
    assert not await store.adopt(held, tmp_path / "f.webp")
    assert (tmp_path / "f.webp").read_bytes() == b"FIRST"
    assert await store.held_for("sp-a") == []


async def test_adopt_of_a_swept_copy_just_forgets_it(tmp_path):
    store = HeldSpaceMediaStore(tmp_path)
    held = _held()
    await _hold(store, held, b"HELD")
    (tmp_path / ".partial" / held.key / BLOB_NAME).unlink()
    assert not await store.adopt(held, tmp_path / "f.webp")
    assert not (tmp_path / "f.webp").exists()
    assert await store.held_for("sp-a") == []


async def test_two_households_copies_never_share_a_folder(tmp_path):
    store = HeldSpaceMediaStore(tmp_path)
    a, b = _held("peer-a"), _held("peer-b")
    await _hold(store, a, b"A")
    await _hold(store, b, b"B")
    assert sorted(h.sender for h in await store.held_for("sp-a")) == [
        "peer-a",
        "peer-b",
    ]
    await store.drop(b)
    assert await store.held_for("sp-a") == [a]


async def test_a_fresh_store_reloads_what_is_held(tmp_path):
    await _hold(HeldSpaceMediaStore(tmp_path), _held(), b"HELD")
    # Junk next to it: an incomplete chunk set, a folder whose record does
    # not match its name, a record that is not JSON.
    (tmp_path / ".partial" / "abc123").mkdir()
    bad = tmp_path / ".partial" / f"{HELD_PREFIX}wrong"
    bad.mkdir()
    (bad / META_NAME).write_bytes(orjson.dumps(_held(filename="o.webp").to_dict()))
    broken = tmp_path / ".partial" / held_key("peer-z", "z.webp")
    broken.mkdir()
    (broken / META_NAME).write_bytes(b"{not json")
    assert await HeldSpaceMediaStore(tmp_path).held_for("sp-a") == [_held()]


async def test_nothing_held_and_no_partial_folder(tmp_path):
    assert await HeldSpaceMediaStore(tmp_path).held_for("sp-a") == []


async def test_a_symlinked_partial_root_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    media = tmp_path / "media"
    media.mkdir()
    (media / ".partial").symlink_to(elsewhere)
    store = HeldSpaceMediaStore(media)
    with pytest.raises(OSError):
        await store.prepare("peer-a", "f.webp")
    assert await store.held_for("sp-a") == []
    assert list(elsewhere.iterdir()) == []


async def test_the_orphan_sweep_ages_out_a_held_file(tmp_path):
    """Reuses the 3-day ``.partial`` sweep — no store of its own."""
    held = _held()
    await _hold(HeldSpaceMediaStore(tmp_path), held, b"HELD")
    folder = tmp_path / ".partial" / held.key
    old = 1_000_000.0
    for path in (folder, *folder.iterdir()):
        os.utime(path, (old, old), follow_symlinks=False)
    sweep = MediaOrphanSweepService(media_dir=tmp_path, reference_repo=None)  # type: ignore[arg-type]
    assert await sweep.sweep_partial_once(now=old + PARTIAL_GRACE_SECONDS + 1) == 1
    assert not folder.exists()
