"""Tests for :class:`SystemAlbumBridge`.

End-to-end coverage of the post-event-driven mirror: posts in →
gallery items appear in the per-scope system album; posts edited →
mirror diffed; posts deleted → mirror cleared. Uses real
:class:`GalleryService` + :class:`SqliteGalleryRepo` + a
:class:`SqliteSpaceRepo` so the partial unique index on
``gallery_albums(space_id, is_system)`` actually fires.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from socialhome.config import Config
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    PostCreated,
    PostDeleted,
    PostEdited,
    SpacePostCreated,
    SpacePostModerated,
    SpacePostSynced,
)
from socialhome.domain.post import Post, PostType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gallery_service import GalleryService
from socialhome.services.system_album_bridge import SystemAlbumBridge


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name)"
        " VALUES('alice', 'a-id', 'Alice')",
    )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'X', ?, 'alice', ?)",
        (iid, "ab" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role)"
        " VALUES('sp-1', 'a-id', 'owner')",
    )
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "t.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
    )
    repo = SqliteGalleryRepo(db)
    bus = EventBus()
    svc = GalleryService(repo, SqliteSpaceRepo(db), bus, cfg)
    bridge = SystemAlbumBridge(svc, bus)
    bridge.wire()
    yield {"svc": svc, "bus": bus, "repo": repo, "db": db}
    await db.shutdown()


# ─── Helpers ─────────────────────────────────────────────────────────────


def _post(
    *,
    pid: str | None = None,
    type_: PostType = PostType.IMAGE,
    image_urls: tuple[str, ...] = ("/api/media/photo.webp",),
    media_url: str | None = None,
    content: str | None = "x",
) -> Post:
    return Post(
        id=pid or uuid.uuid4().hex,
        author="a-id",
        type=type_,
        created_at=datetime.now(timezone.utc),
        content=content,
        image_urls=image_urls,
        media_url=media_url,
    )


# ─── Mirror create paths ─────────────────────────────────────────────────


async def test_image_post_creates_household_system_album(env):
    p = _post(image_urls=("/api/media/a.webp", "/api/media/b.webp"))
    await env["bus"].publish(PostCreated(post=p))
    sys_album = await env["repo"].get_system_album(space_id=None)
    assert sys_album is not None
    assert sys_album.is_system is True
    assert sys_album.owner_user_id is None
    items = await env["repo"].list_items_by_source_post(p.id)
    # Repo reconstructs URLs as relative (no leading slash) — see PR #291.
    assert {it.url for it in items} == {"api/media/a.webp", "api/media/b.webp"}
    assert all(it.source_post_id == p.id for it in items)


async def test_video_post_creates_one_video_item(env):
    p = _post(
        type_=PostType.VIDEO,
        image_urls=(),
        media_url="/api/media/clip.webm",
    )
    await env["bus"].publish(PostCreated(post=p))
    items = await env["repo"].list_items_by_source_post(p.id)
    assert len(items) == 1
    assert items[0].item_type == "video"
    assert items[0].url == "api/media/clip.webm"


async def test_text_post_does_not_touch_gallery(env):
    p = _post(type_=PostType.TEXT, image_urls=(), media_url=None)
    await env["bus"].publish(PostCreated(post=p))
    # No system album auto-created when nothing was mirrored.
    sys_album = await env["repo"].get_system_album(space_id=None)
    assert sys_album is None


async def test_space_post_creates_space_scoped_system_album(env):
    p = _post(image_urls=("/api/media/space.webp",))
    await env["bus"].publish(SpacePostCreated(post=p, space_id="sp-1"))
    space_album = await env["repo"].get_system_album(space_id="sp-1")
    household_album = await env["repo"].get_system_album(space_id=None)
    assert space_album is not None
    # Household album is untouched by space-scoped posts.
    assert household_album is None


async def test_a_synced_space_post_is_mirrored_too(env):
    """A post a §25.6 sync stored lands in the space's system album, and a
    re-applied post churns nothing."""
    p = _post(image_urls=("/api/media/synced.webp",))
    await env["bus"].publish(SpacePostSynced(post=p, space_id="sp-1"))
    album = await env["repo"].get_system_album(space_id="sp-1")
    assert album is not None
    first = await env["repo"].list_items_by_source_post(p.id)
    await env["bus"].publish(SpacePostSynced(post=p, space_id="sp-1"))
    again = await env["repo"].list_items_by_source_post(p.id)
    assert [i.id for i in again] == [i.id for i in first] and len(first) == 1


# ─── Edit paths ──────────────────────────────────────────────────────────


async def test_edit_adds_image(env):
    p = _post(image_urls=("/api/media/a.webp",))
    await env["bus"].publish(PostCreated(post=p))
    edited = _post(
        pid=p.id,
        image_urls=("/api/media/a.webp", "/api/media/b.webp"),
    )
    await env["bus"].publish(PostEdited(post=edited))
    items = await env["repo"].list_items_by_source_post(p.id)
    assert {it.url for it in items} == {"api/media/a.webp", "api/media/b.webp"}


async def test_edit_removes_image(env):
    p = _post(image_urls=("/api/media/a.webp", "/api/media/b.webp"))
    await env["bus"].publish(PostCreated(post=p))
    edited = _post(pid=p.id, image_urls=("/api/media/a.webp",))
    await env["bus"].publish(PostEdited(post=edited))
    items = await env["repo"].list_items_by_source_post(p.id)
    assert {it.url for it in items} == {"api/media/a.webp"}


async def test_edit_text_only_no_churn(env):
    """Edits that don't change media must not delete-then-insert rows.

    The diff guard in ``mirror_post`` reads existing rows and skips
    when the URL set already matches.
    """
    p = _post(image_urls=("/api/media/a.webp",), content="hello")
    await env["bus"].publish(PostCreated(post=p))
    items_before = await env["repo"].list_items_by_source_post(p.id)
    item_ids_before = {it.id for it in items_before}
    edited = _post(pid=p.id, image_urls=("/api/media/a.webp",), content="changed")
    await env["bus"].publish(PostEdited(post=edited))
    items_after = await env["repo"].list_items_by_source_post(p.id)
    # Same row IDs — no delete-then-insert churn.
    assert {it.id for it in items_after} == item_ids_before


# ─── Delete paths ────────────────────────────────────────────────────────


async def test_post_deleted_clears_mirror(env):
    p = _post(image_urls=("/api/media/a.webp", "/api/media/b.webp"))
    await env["bus"].publish(PostCreated(post=p))
    await env["bus"].publish(PostDeleted(post_id=p.id))
    items = await env["repo"].list_items_by_source_post(p.id)
    assert items == []
    # Album survives — only items go.
    sys_album = await env["repo"].get_system_album(space_id=None)
    assert sys_album is not None
    assert sys_album.item_count == 0


async def test_space_post_moderated_clears_mirror(env):
    p = _post(image_urls=("/api/media/space.webp",))
    await env["bus"].publish(SpacePostCreated(post=p, space_id="sp-1"))
    await env["bus"].publish(
        SpacePostModerated(
            space_id="sp-1",
            post=p,
            moderated_by="mod-id",
        )
    )
    items = await env["repo"].list_items_by_source_post(p.id)
    assert items == []


async def test_unmirror_idempotent_unknown_post(env):
    # No setup — just delete an unknown post id. Bridge must not crash.
    await env["bus"].publish(PostDeleted(post_id="never-existed"))


# ─── Race ────────────────────────────────────────────────────────────────


async def test_concurrent_first_posts_create_one_album(env):
    """Two PostCreated events arriving in parallel for a fresh scope
    must end up with a single system album, not two — the partial
    unique index is what guarantees this."""
    p1 = _post(image_urls=("/api/media/a.webp",))
    p2 = _post(image_urls=("/api/media/b.webp",))
    await asyncio.gather(
        env["bus"].publish(PostCreated(post=p1)),
        env["bus"].publish(PostCreated(post=p2)),
    )
    rows = await env["db"].fetchall(
        "SELECT COUNT(*) AS n FROM gallery_albums "
        "WHERE space_id IS NULL AND is_system=1",
    )
    assert int(rows[0]["n"]) == 1


# ─── Item count bookkeeping ──────────────────────────────────────────────


async def test_item_count_kept_in_sync(env):
    p1 = _post(image_urls=("/api/media/a.webp", "/api/media/b.webp"))
    p2 = _post(image_urls=("/api/media/c.webp",))
    await env["bus"].publish(PostCreated(post=p1))
    await env["bus"].publish(PostCreated(post=p2))
    sys_album = await env["repo"].get_system_album(space_id=None)
    assert sys_album.item_count == 3
    await env["bus"].publish(PostDeleted(post_id=p1.id))
    sys_album = await env["repo"].get_system_album(space_id=None)
    assert sys_album.item_count == 1
