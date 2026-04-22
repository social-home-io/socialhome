"""Extra coverage for GalleryService — items + media pipeline + cover."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from socialhome.config import Config
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.gallery import GalleryAlbum, GalleryItem
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gallery_service import (
    ALBUMS_PER_SPACE,
    CAPTION_MAX,
    GalleryNotFoundError,
    GalleryPermissionError,
    GalleryService,
    _with_cover,
)


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
    for username, uid, admin in [
        ("alice", "a-id", 1),
        ("bob", "b-id", 0),
    ]:
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin)"
            " VALUES(?,?,?,?)",
            (username, uid, username.title(), admin),
        )
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-1', 'X', ?, 'alice', ?)",
        (iid, "ab" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-1', 'a-id', 'owner')",
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-1', 'b-id', 'member')",
    )
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "t.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
    )
    repo = SqliteGalleryRepo(db)
    svc = GalleryService(repo, SqliteSpaceRepo(db), EventBus(), cfg)
    yield svc, repo
    await db.shutdown()


def _png_bytes() -> bytes:
    """Return a tiny valid PNG."""
    img = Image.new("RGB", (4, 4), (200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ─── Update: cover_item_id wrong album rejected ──────────────────────────


async def test_update_cover_item_must_belong_to_album(env):
    svc, repo = env
    a1 = await svc.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="A1",
    )
    a2 = await svc.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="A2",
    )
    # Insert an item into a2.
    await repo.create_item(
        GalleryItem(
            id="it-x",
            album_id=a2.id,
            uploaded_by="a-id",
            item_type="photo",
            url="/api/media/x.webp",
            thumbnail_url="/api/media/x-thumb.jpg",
            width=10,
            height=10,
        )
    )
    with pytest.raises(ValueError, match="Cover item"):
        await svc.update_album(
            a1.id,
            actor_user_id="a-id",
            cover_item_id="it-x",
        )


async def test_update_album_too_long_name_422(env):
    svc, _ = env
    a = await svc.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="X",
    )
    with pytest.raises(ValueError):
        await svc.update_album(
            a.id,
            actor_user_id="a-id",
            name="x" * 200,
        )


async def test_update_album_too_long_description_422(env):
    svc, _ = env
    a = await svc.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="X",
    )
    with pytest.raises(ValueError):
        await svc.update_album(
            a.id,
            actor_user_id="a-id",
            description="x" * 600,
        )


async def test_update_unknown_album_raises(env):
    svc, _ = env
    with pytest.raises(GalleryNotFoundError):
        await svc.update_album(
            "missing",
            actor_user_id="a-id",
            name="X",
        )


# ─── Items ───────────────────────────────────────────────────────────────


async def test_list_items_unknown_album_raises(env):
    svc, _ = env
    with pytest.raises(GalleryNotFoundError):
        await svc.list_items("missing", actor_user_id="a-id")


async def test_list_items_non_member_403(env):
    svc, _ = env
    a = await svc.create_album(
        space_id="sp-1",
        owner_user_id="a-id",
        name="X",
    )
    with pytest.raises(GalleryPermissionError):
        await svc.list_items(a.id, actor_user_id="hostile-id")


async def test_upload_unknown_album_raises(env):
    svc, _ = env
    with pytest.raises(GalleryNotFoundError):
        await svc.upload_item(
            "missing",
            data=b"x",
            content_type="image/png",
            caption=None,
            uploader_user_id="a-id",
        )


async def test_upload_empty_data_422(env):
    svc, _ = env
    a = await svc.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="X",
    )
    with pytest.raises(ValueError):
        await svc.upload_item(
            a.id,
            data=b"",
            content_type="image/png",
            caption=None,
            uploader_user_id="a-id",
        )


async def test_upload_too_long_caption_422(env):
    svc, _ = env
    a = await svc.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="X",
    )
    with pytest.raises(ValueError):
        await svc.upload_item(
            a.id,
            data=b"\x89PNG",
            content_type="image/png",
            caption="x" * (CAPTION_MAX + 1),
            uploader_user_id="a-id",
        )


async def test_upload_photo_full_pipeline(env):
    """Smoke-test the photo path — Pillow processes a real PNG."""
    svc, repo = env
    a = await svc.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="X",
    )
    item = await svc.upload_item(
        a.id,
        data=_png_bytes(),
        content_type="image/png",
        caption="hello",
        uploader_user_id="a-id",
    )
    assert item.item_type == "photo"
    assert item.url.startswith("/api/media/")
    assert item.thumbnail_url.startswith("/api/media/")
    refreshed = await repo.get_album(a.id)
    assert refreshed.item_count == 1


# ─── delete_item permissions ────────────────────────────────────────────


async def test_delete_item_uploader_succeeds(env):
    svc, _ = env
    a = await svc.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Bobs",
    )
    item = await svc.upload_item(
        a.id,
        data=_png_bytes(),
        content_type="image/png",
        caption=None,
        uploader_user_id="b-id",
    )
    await svc.delete_item(item.id, actor_user_id="b-id")


async def test_delete_item_non_uploader_non_admin_403(env):
    svc, _ = env
    a = await svc.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Bobs",
    )
    item = await svc.upload_item(
        a.id,
        data=_png_bytes(),
        content_type="image/png",
        caption=None,
        uploader_user_id="b-id",
    )
    with pytest.raises(GalleryPermissionError):
        await svc.delete_item(item.id, actor_user_id="other-id")


async def test_delete_item_space_admin_succeeds(env):
    svc, _ = env
    a = await svc.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Bobs",
    )
    item = await svc.upload_item(
        a.id,
        data=_png_bytes(),
        content_type="image/png",
        caption=None,
        uploader_user_id="b-id",
    )
    # alice is space owner → counts as admin.
    await svc.delete_item(item.id, actor_user_id="a-id")


async def test_delete_unknown_item_silent(env):
    svc, _ = env
    await svc.delete_item("missing", actor_user_id="a-id")  # no raise


# ─── delete_album permission via space admin ─────────────────────────────


async def test_delete_album_space_admin_succeeds(env):
    svc, _ = env
    a = await svc.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Bobs",
    )
    # alice (space owner) deletes bob's album.
    await svc.delete_album(a.id, actor_user_id="a-id")


async def test_delete_album_random_user_403(env):
    svc, _ = env
    a = await svc.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Bobs",
    )
    with pytest.raises(GalleryPermissionError):
        await svc.delete_album(a.id, actor_user_id="hostile-id")


# ─── Cover URL resolution ────────────────────────────────────────────────


async def test_get_album_resolves_cover_from_explicit_item(env):
    svc, repo = env
    a = await svc.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="X",
    )
    item = await svc.upload_item(
        a.id,
        data=_png_bytes(),
        content_type="image/png",
        caption=None,
        uploader_user_id="a-id",
    )
    await svc.update_album(
        a.id,
        actor_user_id="a-id",
        cover_item_id=item.id,
    )
    got = await svc.get_album(a.id, actor_user_id="a-id")
    assert got.cover_url is not None
    assert got.cover_url.startswith("/api/media/")


async def test_get_album_falls_back_to_first_item_thumbnail(env):
    svc, _ = env
    a = await svc.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="X",
    )
    await svc.upload_item(
        a.id,
        data=_png_bytes(),
        content_type="image/png",
        caption=None,
        uploader_user_id="a-id",
    )
    got = await svc.get_album(a.id, actor_user_id="a-id")
    # No explicit cover set → first item thumbnail used.
    assert got.cover_url is not None


async def test_get_unknown_album_raises(env):
    svc, _ = env
    with pytest.raises(GalleryNotFoundError):
        await svc.get_album("missing", actor_user_id="a-id")


# ─── Retention ───────────────────────────────────────────────────────────


async def test_set_retention_unknown_album_raises(env):
    svc, _ = env
    with pytest.raises(GalleryNotFoundError):
        await svc.set_retention_exempt(
            "missing",
            True,
            actor_user_id="a-id",
        )


# ─── Cap enforcement ────────────────────────────────────────────────────


async def test_cap_enforced_at_albums_per_space(env):
    """Beyond ALBUMS_PER_SPACE → ValueError."""
    svc, repo = env
    # Inject ALBUMS_PER_SPACE rows directly to skip the per-call setup cost.
    for i in range(ALBUMS_PER_SPACE):
        await repo.create_album(
            GalleryAlbum(
                id=f"alb-{i}",
                space_id="sp-1",
                owner_user_id="a-id",
                name=f"A{i}",
            )
        )
    with pytest.raises(ValueError, match="album limit"):
        await svc.create_album(
            space_id="sp-1",
            owner_user_id="a-id",
            name="One too many",
        )


# ─── _with_cover helper ────────────────────────────────────────────────


def test_with_cover_replaces_cover_url():
    a = GalleryAlbum(
        id="a",
        space_id=None,
        owner_user_id="x",
        name="N",
        cover_url=None,
    )
    out = _with_cover(a, "/api/media/cover.jpg")
    assert out.cover_url == "/api/media/cover.jpg"
    # Original unchanged (frozen dataclass).
    assert a.cover_url is None
