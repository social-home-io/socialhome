"""Tests for GalleryService."""

from __future__ import annotations

import pathlib

import pytest

from socialhome.config import Config
from socialhome.media.video_processor import VideoProcessor
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import GalleryAlbumUpdated
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo
from socialhome.repositories.media_transcode_repo import SqliteMediaTranscodeRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.gallery_service import (
    DESCRIPTION_MAX,
    GalleryNotFoundError,
    GalleryPermissionError,
    GalleryService,
    NAME_MAX,
)
from socialhome.services.media_transcode_service import MediaTranscodeService


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
        ("eve", "e-id", 0),
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
    # alice = owner, bob = member (no special role).
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
    transcode_repo = SqliteMediaTranscodeRepo(db)
    transcode_service = MediaTranscodeService(
        repo=transcode_repo,
        media_dir=pathlib.Path(cfg.media_path),
        processor=VideoProcessor(),
    )
    # Note: the scheduler loop is NOT started — the tests drive
    # ``flush_once`` directly (or just assert the enqueue), so a real
    # background transcode never races against the assertions.
    svc = GalleryService(
        SqliteGalleryRepo(db),
        SqliteSpaceRepo(db),
        EventBus(),
        cfg,
        media_transcode_repo=transcode_repo,
        media_transcode_service=transcode_service,
        media_refs=SqliteMediaReferenceRepo(db),
    )
    yield svc
    await db.shutdown()


# ─── Album CRUD permissions ──────────────────────────────────────────────


async def test_create_album_member_succeeds(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Trip 2026",
    )
    assert a.name == "Trip 2026"
    assert a.owner_user_id == "b-id"


async def test_create_album_non_member_403(env):
    with pytest.raises(GalleryPermissionError):
        await env.create_album(
            space_id="sp-1",
            owner_user_id="e-id",
            name="Hostile",
        )


async def test_create_household_album_no_membership_check(env):
    a = await env.create_album(
        space_id=None,
        owner_user_id="e-id",
        name="Personal",
    )
    assert a.space_id is None


async def test_create_album_empty_name_422(env):
    with pytest.raises(ValueError):
        await env.create_album(
            space_id="sp-1",
            owner_user_id="a-id",
            name="",
        )


async def test_create_album_too_long_name_422(env):
    with pytest.raises(ValueError):
        await env.create_album(
            space_id="sp-1",
            owner_user_id="a-id",
            name="x" * (NAME_MAX + 1),
        )


async def test_create_album_too_long_description_422(env):
    with pytest.raises(ValueError):
        await env.create_album(
            space_id="sp-1",
            owner_user_id="a-id",
            name="X",
            description="x" * (DESCRIPTION_MAX + 1),
        )


# ─── List + get ──────────────────────────────────────────────────────────


async def test_list_albums_member_succeeds(env):
    await env.create_album(space_id="sp-1", owner_user_id="a-id", name="A")
    await env.create_album(space_id="sp-1", owner_user_id="a-id", name="B")
    out = await env.list_albums(space_id="sp-1", actor_user_id="b-id")
    assert len(out) == 2


async def test_list_albums_non_member_403(env):
    with pytest.raises(GalleryPermissionError):
        await env.list_albums(space_id="sp-1", actor_user_id="e-id")


async def test_get_album_unknown_404(env):
    with pytest.raises(GalleryNotFoundError):
        await env.get_album("nope", actor_user_id="a-id")


# ─── Update + delete permissions ─────────────────────────────────────────


async def test_update_album_owner_succeeds(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Original",
    )
    await env.update_album(
        a.id,
        actor_user_id="b-id",
        name="Renamed",
    )
    refreshed = await env.get_album(a.id, actor_user_id="b-id")
    assert refreshed.name == "Renamed"


async def test_update_album_publishes_gallery_album_updated(env):
    """The edit is what the federation outbound fans out to the space's
    other member households (v_33) — a no-op patch publishes nothing."""
    seen: list[GalleryAlbumUpdated] = []

    async def _on(event: GalleryAlbumUpdated) -> None:
        seen.append(event)

    env._bus.subscribe(GalleryAlbumUpdated, _on)
    a = await env.create_album(space_id="sp-1", owner_user_id="b-id", name="Old")
    await env.update_album(a.id, actor_user_id="b-id")
    assert seen == []
    await env.update_album(a.id, actor_user_id="b-id", name="New")
    assert [(e.album_id, e.space_id) for e in seen] == [(a.id, "sp-1")]


async def test_update_album_space_admin_succeeds(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Bobs",
    )
    # alice = space owner → counts as admin.
    await env.update_album(
        a.id,
        actor_user_id="a-id",
        name="Admin renamed",
    )


async def test_update_album_other_user_403(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Bobs",
    )
    with pytest.raises(GalleryPermissionError):
        await env.update_album(
            a.id,
            actor_user_id="e-id",
            name="Hijack",
        )


async def test_delete_album_owner_succeeds(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="X",
    )
    await env.delete_album(a.id, actor_user_id="b-id")
    with pytest.raises(GalleryNotFoundError):
        await env.get_album(a.id, actor_user_id="a-id")


async def test_delete_unknown_album_silent(env):
    # No raise.
    await env.delete_album("missing", actor_user_id="a-id")


# ─── Retention exemption ────────────────────────────────────────────────


async def test_set_retention_exempt_owner_succeeds(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="a-id",
        name="Keep me",
    )
    await env.set_retention_exempt(a.id, True, actor_user_id="a-id")
    refreshed = await env.get_album(a.id, actor_user_id="a-id")
    assert refreshed.retention_exempt is True


async def test_set_retention_exempt_non_owner_403(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="a-id",
        name="X",
    )
    with pytest.raises(GalleryPermissionError):
        await env.set_retention_exempt(
            a.id,
            True,
            actor_user_id="b-id",
        )


# ─── System album guards ─────────────────────────────────────────────────
#
# The auto-managed "Posts" album cannot be renamed, deleted, uploaded
# to, or have its items individually removed. Five entry points reject
# system-album mutations *before* the regular owner/admin check.


async def test_ensure_system_album_idempotent(env):
    a1 = await env.ensure_system_album(space_id=None)
    a2 = await env.ensure_system_album(space_id=None)
    assert a1.id == a2.id
    assert a1.is_system is True
    assert a1.owner_user_id is None


async def test_system_album_household_and_space_isolated(env):
    household = await env.ensure_system_album(space_id=None)
    space = await env.ensure_system_album(space_id="sp-1")
    assert household.id != space.id
    assert household.space_id is None
    assert space.space_id == "sp-1"


async def test_system_album_delete_blocked(env):
    a = await env.ensure_system_album(space_id=None)
    with pytest.raises(GalleryPermissionError):
        await env.delete_album(a.id, actor_user_id="a-id")


async def test_system_album_update_blocked(env):
    a = await env.ensure_system_album(space_id=None)
    with pytest.raises(GalleryPermissionError):
        await env.update_album(a.id, actor_user_id="a-id", name="renamed")


async def test_system_album_upload_blocked(env):
    a = await env.ensure_system_album(space_id=None)
    with pytest.raises(GalleryPermissionError):
        await env.upload_item(
            a.id,
            data=b"x" * 100,
            content_type="image/jpeg",
            caption=None,
            uploader_user_id="a-id",
        )


async def test_delete_item_removes_files_from_disk(env):
    import io

    from PIL import Image

    album = await env.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="Trip",
    )
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, format="JPEG")
    item = await env.upload_item(
        album.id,
        data=buf.getvalue(),
        content_type="image/jpeg",
        caption=None,
        uploader_user_id="a-id",
    )
    media_dir = env._media_dir  # type: ignore[attr-defined]
    full = media_dir / item.url.rsplit("/", 1)[-1]
    thumb = media_dir / item.thumbnail_url.rsplit("/", 1)[-1]
    assert full.exists()
    assert thumb.exists()

    await env.delete_item(item.id, actor_user_id="a-id")
    assert not full.exists()
    assert not thumb.exists()


async def test_delete_item_keeps_files_another_item_uses(env):
    """An item naming another item's files (e.g. one synced from another
    household) is deleted without taking those files with it."""
    import dataclasses
    import io

    from PIL import Image

    album = await env.create_album(space_id=None, owner_user_id="a-id", name="T")
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, format="JPEG")
    item = await env.upload_item(
        album.id,
        data=buf.getvalue(),
        content_type="image/jpeg",
        caption=None,
        uploader_user_id="a-id",
    )
    copy = dataclasses.replace(item, id="copy-item")
    await env._repo.create_item(copy)  # type: ignore[attr-defined]
    media_dir = env._media_dir  # type: ignore[attr-defined]
    full = media_dir / item.url.rsplit("/", 1)[-1]
    thumb = media_dir / item.thumbnail_url.rsplit("/", 1)[-1]

    await env.delete_item("copy-item", actor_user_id="a-id")
    assert full.exists() and thumb.exists()
    await env.delete_item(item.id, actor_user_id="a-id")
    assert not full.exists() and not thumb.exists()


async def test_upload_video_is_async(env):
    """A video upload creates a GalleryItem row immediately (no inline
    transcode), enqueues a transcode job keyed by the item's output
    ``.webm`` filename, and the .webm/.webp don't exist on disk yet."""
    album = await env.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="Clips",
    )
    item = await env.upload_item(
        album.id,
        data=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200,
        content_type="video/mp4",
        caption="my clip",
        uploader_user_id="a-id",
    )
    assert item.item_type == "video"
    assert item.url.endswith(".webm")
    assert item.thumbnail_url.endswith(".webp")
    assert item.duration_s is None
    output_filename = item.url.rsplit("/", 1)[-1]
    thumb_filename = item.thumbnail_url.rsplit("/", 1)[-1]
    # ``.webm`` + ``.webp`` share one UUID stem so the poster is
    # derivable from the media URL server-side.
    assert output_filename[: -len(".webm")] == thumb_filename[: -len(".webp")]

    # The item row is persisted right away.
    rows = await env.list_items(album.id, actor_user_id="a-id")
    assert any(r.id == item.id for r in rows)

    # Exactly one transcode job, keyed by the .webm output filename.
    due = await env._transcode_repo.list_due()  # type: ignore[attr-defined]
    assert len(due) == 1
    assert due[0].output_filename == output_filename
    assert due[0].owner_user_id == "a-id"

    # No transcode happened inline — outputs absent.
    media_dir = env._media_dir  # type: ignore[attr-defined]
    assert not (media_dir / output_filename).exists()
    assert not (media_dir / thumb_filename).exists()


async def test_upload_video_flush_writes_files(env, monkeypatch):
    """Driving the wired transcode service's ``flush_once`` with a stub
    processor writes the output + poster and clears the job."""
    album = await env.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="Clips",
    )
    item = await env.upload_item(
        album.id,
        data=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200,
        content_type="video/mp4",
        caption=None,
        uploader_user_id="a-id",
    )
    output_filename = item.url.rsplit("/", 1)[-1]
    thumb_filename = item.thumbnail_url.rsplit("/", 1)[-1]

    svc = env._transcode_service  # type: ignore[attr-defined]

    class _StubProcessor:
        async def process(self, src, name):
            return b"WEBMDATA", "out.webm"

        async def generate_thumbnail(self, src):
            return b"WEBPDATA"

    monkeypatch.setattr(svc, "_processor", _StubProcessor())

    done = await svc.flush_once()
    assert done == 1

    media_dir = env._media_dir  # type: ignore[attr-defined]
    assert (media_dir / output_filename).read_bytes() == b"WEBMDATA"
    assert (media_dir / thumb_filename).read_bytes() == b"WEBPDATA"
    assert await env._transcode_repo.list_due() == []  # type: ignore[attr-defined]


async def test_system_album_set_retention_exempt_blocked(env):
    a = await env.ensure_system_album(space_id=None)
    with pytest.raises(GalleryPermissionError):
        await env.set_retention_exempt(a.id, False, actor_user_id="a-id")


async def test_list_albums_pins_system_album_first(env):
    # Create a regular album first, then the system album. The list
    # must return the system album at the top regardless of created_at.
    await env.create_album(
        space_id="sp-1",
        owner_user_id="a-id",
        name="Trip",
    )
    sys = await env.ensure_system_album(space_id="sp-1")
    rows = await env.list_albums(space_id="sp-1", actor_user_id="a-id")
    assert rows[0].id == sys.id
    assert rows[0].is_system is True
