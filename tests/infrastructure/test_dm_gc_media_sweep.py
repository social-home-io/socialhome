"""Coverage for ``DmGcScheduler._sweep_media_orphans``.

The conversation-GC half of the scheduler already has tests; this
file targets the new media-orphan sweep that drops part / preview
files whose backing ``conversation_messages`` row is deleted or
absent.
"""

from __future__ import annotations

import pytest

from socialhome.infrastructure.dm_gc_scheduler import DmGcScheduler
from socialhome.repositories import SqliteConversationRepo

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def gc_with_media(db, tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    repo = SqliteConversationRepo(db)
    sched = DmGcScheduler(repo, media_dir=media_dir)
    return sched, media_dir, db


async def test_media_sweep_drops_orphan_part_files(gc_with_media):
    """A part file whose message row is gone is removed."""
    sched, media_dir, _db = gc_with_media
    # The regex requires a 16+ hex msg-id. Use a UUID-shaped value.
    orphan = "0123456789abcdef0123"
    (media_dir / f"{orphan}.part00000").write_bytes(b"x")
    (media_dir / f"{orphan}.part00001").write_bytes(b"y")
    (media_dir / f"{orphan}.preview.webp").write_bytes(b"z")
    removed = await sched._sweep_media_orphans()
    assert removed == 3
    assert not (media_dir / f"{orphan}.part00000").exists()
    assert not (media_dir / f"{orphan}.part00001").exists()
    assert not (media_dir / f"{orphan}.preview.webp").exists()


async def test_media_sweep_keeps_live_message_files(gc_with_media):
    """Part files for a live message row stay put."""
    sched, media_dir, db = gc_with_media
    live = "fedcba9876543210fedc"
    # Seed conversation + live message row.
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("c-1", "dm"),
    )
    await db.enqueue(
        """
        INSERT INTO conversation_messages(
            id, conversation_id, sender_user_id, content, type, created_at
        ) VALUES(?,?,?,?,?, datetime('now'))
        """,
        (live, "c-1", "u-1", "", "image"),
    )
    (media_dir / f"{live}.part00000").write_bytes(b"chunk")
    (media_dir / f"{live}.preview.webp").write_bytes(b"preview")
    removed = await sched._sweep_media_orphans()
    assert removed == 0
    assert (media_dir / f"{live}.part00000").is_file()
    assert (media_dir / f"{live}.preview.webp").is_file()


async def test_media_sweep_drops_files_for_deleted_message(gc_with_media):
    """A soft-deleted message gets its leftover files cleaned."""
    sched, media_dir, db = gc_with_media
    gone = "abababababababababab"
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) VALUES(?,?, datetime('now'))",
        ("c-1", "dm"),
    )
    await db.enqueue(
        """
        INSERT INTO conversation_messages(
            id, conversation_id, sender_user_id, content, type, deleted,
            created_at
        ) VALUES(?,?,?,?,?,?, datetime('now'))
        """,
        (gone, "c-1", "u-1", "", "image", 1),
    )
    (media_dir / f"{gone}.preview.webp").write_bytes(b"old")
    removed = await sched._sweep_media_orphans()
    assert removed == 1
    assert not (media_dir / f"{gone}.preview.webp").exists()


async def test_media_sweep_ignores_unrelated_filenames(gc_with_media):
    """Files that don't match the part/preview pattern are left
    alone — e.g. uploaded WebPs from feed posts."""
    sched, media_dir, _db = gc_with_media
    (media_dir / "post-asset-image.webp").write_bytes(b"a")
    (media_dir / "another.bin").write_bytes(b"b")
    removed = await sched._sweep_media_orphans()
    assert removed == 0
    assert (media_dir / "post-asset-image.webp").is_file()
    assert (media_dir / "another.bin").is_file()


async def test_media_sweep_no_media_dir_returns_zero(db):
    """A scheduler without ``media_dir`` skips the sweep cleanly."""
    sched = DmGcScheduler(SqliteConversationRepo(db), media_dir=None)
    assert await sched._sweep_media_orphans() == 0


async def test_media_sweep_missing_dir_returns_zero(db, tmp_path):
    """A media_dir that doesn't exist yet skips cleanly."""
    sched = DmGcScheduler(
        SqliteConversationRepo(db),
        media_dir=tmp_path / "does-not-exist",
    )
    assert await sched._sweep_media_orphans() == 0
