"""Tests for GalleryService."""

from __future__ import annotations

import asyncio
import hashlib
import io
import pathlib
from datetime import UTC, datetime, timedelta

import av
import pytest
from PIL import Image
from pillow_heif import register_heif_opener

from socialhome.config import Config
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.errors import (
    DownloadTooLargeError,
    PayloadTooLargeError,
    TranscodeQueueFullError,
    VideoTooLongError,
    VideoUnreadableError,
)
from socialhome.domain.events import (
    GalleryAlbumDeleted,
    GalleryAlbumUpdated,
    GalleryItemDeleted,
    GalleryItemRestored,
    GalleryItemUpdated,
    GalleryPrivateAlbumTrashed,
    UserDeprovisioned,
)
from socialhome.domain.gallery import (
    DOWNLOAD_MAX_ITEMS,
    GALLERY_MAX_UPLOAD_BYTES,
    GALLERY_VIDEO_MAX_SECONDS,
    GALLERY_VIDEO_MAX_UPLOAD_BYTES,
    GalleryItem,
    UploadOutcome,
)
from socialhome.domain.media_constraints import (
    IMAGE_MAX_DIMENSION,
    IMAGE_WEBP_QUALITY,
    THUMBNAIL_PX,
    VIDEO_MAX_DIMENSION,
    VIDEO_MAX_DURATION_SECONDS,
)
from socialhome.domain.post import Post, PostType
from socialhome.domain.preferences import FeatureDisabledError
from socialhome.domain.space import SpaceMember, SubscriberReadOnlyError
from socialhome.federation.owner_bound_id import (
    GALLERY_ALBUM_KIND,
    GALLERY_ITEM_KIND,
    OwnerBinding,
    check_owner_bound_id,
    is_owner_bound,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.media.video_processor import VideoProcessor
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.repositories.instance_config_repo import SqliteInstanceConfigRepo
from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo
from socialhome.repositories.media_transcode_repo import SqliteMediaTranscodeRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.gallery_service import (
    DEFAULT_ALBUM_KEY,
    DESCRIPTION_MAX,
    NAME_MAX,
    AlbumAudienceError,
    ChildUploadOffError,
    GalleryNotFoundError,
    GalleryPermissionError,
    GalleryService,
    _extract_exif_date_sync,
)
from socialhome.services.media_transcode_service import MediaTranscodeService
from tests.video_clips import make_clip


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
        " identity_public_key, feature_gallery) VALUES('sp-1', 'X', ?, 'alice', ?, 1)",
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
        user_repo=SqliteUserRepo(db),
        instance_config=SqliteInstanceConfigRepo(db),
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


async def test_a_space_album_id_commits_to_its_creator(env):
    """A space album federates, so its id is owner-bound: it verifies for
    its creator in its space and for nobody else."""
    a = await env.create_album(space_id="sp-1", owner_user_id="b-id", name="Trip")
    assert (
        check_owner_bound_id(
            GALLERY_ALBUM_KIND, a.id, space_id="sp-1", owner_user_id="b-id"
        )
        is OwnerBinding.VALID
    )
    assert (
        check_owner_bound_id(
            GALLERY_ALBUM_KIND, a.id, space_id="sp-1", owner_user_id="a-id"
        )
        is OwnerBinding.MISMATCH
    )


async def test_a_household_album_keeps_a_plain_id(env):
    """A household album never leaves the household — nothing to bind."""
    a = await env.create_album(space_id=None, owner_user_id="b-id", name="Home")
    assert not is_owner_bound(a.id)


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


async def test_a_space_with_its_gallery_off_answers_by_id_like_its_space_routes(env):
    """By-id album reads and writes honour the space's ``gallery`` toggle;
    a household album has no space toggle and is untouched."""
    album = await env.create_album(space_id="sp-1", owner_user_id="b-id", name="A")
    home = await env.create_album(space_id=None, owner_user_id="b-id", name="H")
    await env._space_repo._db.enqueue(
        "UPDATE spaces SET feature_gallery = 0 WHERE id='sp-1'"
    )
    with pytest.raises(FeatureDisabledError):
        await env.get_album(album.id, actor_user_id="b-id")
    with pytest.raises(FeatureDisabledError):
        await env.list_items(album.id, actor_user_id="b-id")
    with pytest.raises(FeatureDisabledError):
        await env.update_album(album.id, actor_user_id="b-id", name="B")
    with pytest.raises(FeatureDisabledError):
        await env.delete_album(album.id, actor_user_id="b-id")
    # An outsider still learns nothing: the unknown id's 404 comes first.
    with pytest.raises(GalleryNotFoundError):
        await env.get_album(album.id, actor_user_id="e-id")
    assert (await env.get_album(home.id, actor_user_id="b-id")).id == home.id


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


async def test_update_album_by_an_outsider_is_the_unknown_ids_404(env):
    a = await env.create_album(
        space_id="sp-1",
        owner_user_id="b-id",
        name="Bobs",
    )
    with pytest.raises(GalleryNotFoundError):
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


async def test_delete_album_publishes_its_owner(env):
    """The owner rides the delete event (v_34) so the federated delete can
    name it for a receiver that does not hold the album yet."""
    seen: list[GalleryAlbumDeleted] = []

    async def _on(event: GalleryAlbumDeleted) -> None:
        seen.append(event)

    env._bus.subscribe(GalleryAlbumDeleted, _on)
    a = await env.create_album(space_id="sp-1", owner_user_id="b-id", name="X")
    await env.delete_album(a.id, actor_user_id="a-id")  # the space owner
    assert [(e.album_id, e.owner_id) for e in seen] == [(a.id, "b-id")]


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


async def test_a_space_upload_id_commits_to_its_uploader(env):
    """v_36: a space upload federates, so its id is owner-bound to the
    uploader in the album's space; a household upload keeps a plain id."""
    import io

    from PIL import Image

    space_album = await env.create_album(
        space_id="sp-1", owner_user_id="b-id", name="Trip"
    )
    home_album = await env.create_album(space_id=None, owner_user_id="a-id", name="H")
    ids = []
    for album_id, uploader in ((space_album.id, "a-id"), (home_album.id, "a-id")):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="JPEG")
        item = (
            await env.upload_item(
                album_id,
                data=buf.getvalue(),
                content_type="image/jpeg",
                caption=None,
                uploader_user_id=uploader,
            )
        ).item
        ids.append(item.id)
    assert (
        check_owner_bound_id(
            GALLERY_ITEM_KIND, ids[0], space_id="sp-1", owner_user_id="a-id"
        )
        is OwnerBinding.VALID
    )
    assert (
        check_owner_bound_id(
            GALLERY_ITEM_KIND, ids[0], space_id="sp-1", owner_user_id="b-id"
        )
        is OwnerBinding.MISMATCH
    )
    assert not is_owner_bound(ids[1])


async def test_a_deleted_household_photo_keeps_its_files_until_the_purge(env):
    """A household delete is a move to the trash: the files stay for the
    30 days a restore needs them, then the purge removes them."""
    import io

    from PIL import Image

    album = await env.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="Trip",
    )
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, format="JPEG")
    item = (
        await env.upload_item(
            album.id,
            data=buf.getvalue(),
            content_type="image/jpeg",
            caption=None,
            uploader_user_id="a-id",
        )
    ).item
    media_dir = env._media_dir  # type: ignore[attr-defined]
    full = media_dir / item.url.rsplit("/", 1)[-1]
    thumb = media_dir / item.thumbnail_url.rsplit("/", 1)[-1]
    assert full.exists()
    assert thumb.exists()

    await env.delete_item(item.id, actor_user_id="a-id")
    assert full.exists() and thumb.exists()
    await env.purge_expired_trash(now=datetime.now(UTC) + timedelta(days=31))
    assert not full.exists()
    assert not thumb.exists()


async def test_a_deleted_album_s_files_go_with_the_trash_purge(env):
    """An album delete sends its photos to the trash (files kept); the
    purge takes the files unless another row still names them."""
    import dataclasses
    import io

    from PIL import Image

    album = await env.create_album(space_id=None, owner_user_id="a-id", name="T")
    other = await env.create_album(space_id=None, owner_user_id="a-id", name="O")
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, format="JPEG")
    item = (
        await env.upload_item(
            album.id,
            data=buf.getvalue(),
            content_type="image/jpeg",
            caption=None,
            uploader_user_id="a-id",
        )
    ).item
    buf2 = io.BytesIO()
    Image.new("RGB", (32, 32), (200, 20, 20)).save(buf2, format="JPEG")
    kept = (
        await env.upload_item(
            album.id,
            data=buf2.getvalue(),
            content_type="image/jpeg",
            caption=None,
            uploader_user_id="a-id",
        )
    ).item
    await env._repo.create_item(  # type: ignore[attr-defined]
        dataclasses.replace(kept, id="elsewhere", album_id=other.id)
    )
    media_dir = env._media_dir  # type: ignore[attr-defined]
    gone = [media_dir / u.rsplit("/", 1)[-1] for u in (item.url, item.thumbnail_url)]
    stay = [media_dir / u.rsplit("/", 1)[-1] for u in (kept.url, kept.thumbnail_url)]
    assert all(p.exists() for p in gone + stay)

    await env.delete_album(album.id, actor_user_id="a-id")
    assert all(p.exists() for p in gone + stay)
    await env.purge_expired_trash(now=datetime.now(UTC) + timedelta(days=31))
    assert not any(p.exists() for p in gone)
    assert all(p.exists() for p in stay)


async def test_a_cover_renders_only_when_the_item_is_in_that_album(env):
    """A cover id may name an item that has not arrived yet (a federated
    edit can overtake the upload) — or, once it lands, one filed in some
    other album. Only an item of this album is rendered as its cover."""
    import dataclasses
    import io

    from PIL import Image

    album = await env.create_album(space_id=None, owner_user_id="a-id", name="T")
    other = await env.create_album(space_id=None, owner_user_id="a-id", name="O")
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, format="JPEG")
    own = (
        await env.upload_item(
            album.id,
            data=buf.getvalue(),
            content_type="image/jpeg",
            caption=None,
            uploader_user_id="a-id",
        )
    ).item
    foreign = dataclasses.replace(
        own, id="foreign", album_id=other.id, thumbnail_url="api/media/foreign.webp"
    )
    await env._repo.create_item(foreign)  # type: ignore[attr-defined]
    await env._repo.update_album(album.id, {"cover_item_id": "foreign"})  # type: ignore[attr-defined]
    got = await env.get_album(album.id, actor_user_id="a-id")
    assert got.cover_url == own.thumbnail_url


async def test_delete_item_keeps_files_another_item_uses(env):
    """An item naming another item's files (e.g. one synced from another
    household) is deleted without taking those files with it."""
    import dataclasses
    import io

    from PIL import Image

    album = await env.create_album(space_id=None, owner_user_id="a-id", name="T")
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, format="JPEG")
    item = (
        await env.upload_item(
            album.id,
            data=buf.getvalue(),
            content_type="image/jpeg",
            caption=None,
            uploader_user_id="a-id",
        )
    ).item
    copy = dataclasses.replace(item, id="copy-item")
    await env._repo.create_item(copy)  # type: ignore[attr-defined]
    media_dir = env._media_dir  # type: ignore[attr-defined]
    full = media_dir / item.url.rsplit("/", 1)[-1]
    thumb = media_dir / item.thumbnail_url.rsplit("/", 1)[-1]

    await env.delete_item("copy-item", actor_user_id="a-id")
    await env.purge_trashed_item("copy-item", actor_user_id="a-id")
    assert full.exists() and thumb.exists()
    await env.delete_item(item.id, actor_user_id="a-id")
    await env.purge_trashed_item(item.id, actor_user_id="a-id")
    assert not full.exists() and not thumb.exists()


async def test_upload_video_is_async(env):
    """A video upload creates a GalleryItem row immediately (no inline
    transcode), enqueues a transcode job keyed by the item's output
    ``.webm`` filename, and the .webm/.webp don't exist on disk yet. The
    item already carries the video's real length, stored size and the
    day the file says it was recorded."""
    album = await env.create_album(
        space_id=None,
        owner_user_id="a-id",
        name="Clips",
    )
    item = (
        await env.upload_item(
            album.id,
            data=make_clip(
                3,
                size=(640, 360),
                metadata={"creation_time": "2026-05-17T16:20:00.000000Z"},
            ),
            content_type="video/mp4",
            caption="my clip",
            uploader_user_id="a-id",
        )
    ).item
    assert item.item_type == "video"
    assert item.url.endswith(".webm")
    assert item.thumbnail_url.endswith(".webp")
    assert item.duration_s == pytest.approx(3.0, abs=0.1)
    assert (item.width, item.height) == (640, 360)
    assert item.taken_at == "2026-05-17"
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
    assert due[0].duration_s == pytest.approx(3.0, abs=0.1)

    # No transcode happened inline — outputs absent.
    media_dir = env._media_dir  # type: ignore[attr-defined]
    assert not (media_dir / output_filename).exists()
    assert not (media_dir / thumb_filename).exists()


async def test_upload_video_flush_writes_webm_and_webp_poster(env):
    """The queued video is converted to the one stored format: a VP9/Opus
    WebM no bigger than ``VIDEO_MAX_DIMENSION`` on its longest edge, and
    a WebP poster — whatever the phone recorded."""
    album = await env.create_album(space_id=None, owner_user_id="a-id", name="Clips")
    item = (
        await env.upload_item(
            album.id,
            data=make_clip(2, size=(2560, 1440), audio=True),
            content_type="video/mp4",
            caption=None,
            uploader_user_id="a-id",
        )
    ).item
    assert (item.width, item.height) == (1920, 1080)
    output_filename = item.url.rsplit("/", 1)[-1]
    thumb_filename = item.thumbnail_url.rsplit("/", 1)[-1]

    done = await env._transcode_service.flush_once()  # type: ignore[attr-defined]
    assert done == 1

    media_dir = env._media_dir  # type: ignore[attr-defined]
    with av.open(str(media_dir / output_filename)) as c:
        assert c.format.name.startswith("matroska,webm")
        video = c.streams.video[0]
        assert video.codec_context.name == "vp9"
        assert max(video.codec_context.width, video.codec_context.height) <= (
            VIDEO_MAX_DIMENSION
        )
        assert (video.codec_context.width, video.codec_context.height) == (1920, 1080)
        assert [a.codec_context.name for a in c.streams.audio] == ["opus"]
    with Image.open(media_dir / thumb_filename) as poster:
        assert poster.format == "WEBP"
    assert await env._transcode_repo.list_due() == []  # type: ignore[attr-defined]
    # The temp source is gone.
    assert list((media_dir / "transcode_src").iterdir()) == []


async def test_household_video_of_five_minutes_is_kept_whole(env):
    """The household gallery keeps videos up to 5 minutes (owner D4): a
    5:00 video is converted whole, not cut at the protocol's 60 s."""
    album = await env.create_album(space_id=None, owner_user_id="a-id", name="Party")
    item = (
        await env.upload_item(
            album.id,
            data=make_clip(GALLERY_VIDEO_MAX_SECONDS),
            content_type="video/mp4",
            caption=None,
            uploader_user_id="a-id",
        )
    ).item
    assert item.duration_s == pytest.approx(300.0, abs=0.5)

    assert await env._transcode_service.flush_once() == 1  # type: ignore[attr-defined]
    out = env._media_dir / item.url.rsplit("/", 1)[-1]  # type: ignore[attr-defined]
    with av.open(str(out)) as c:
        assert c.duration / av.time_base == pytest.approx(300.0, abs=1.5)


async def test_household_video_over_five_minutes_is_refused_not_cut(env):
    """Longer than 5 minutes: refused with its length — nothing stored,
    nothing queued, the uploaded file removed."""
    album = await env.create_album(space_id=None, owner_user_id="a-id", name="Party")
    with pytest.raises(VideoTooLongError) as caught:
        await env.upload_item(
            album.id,
            data=make_clip(432),  # 7:12
            content_type="video/mp4",
            caption=None,
            uploader_user_id="a-id",
        )
    assert caught.value.code == "VIDEO_TOO_LONG"
    assert caught.value.params == {"duration_s": 432, "max_s": 300}
    assert await env.list_items(album.id, actor_user_id="a-id") == []
    assert await env._transcode_repo.count_queued() == 0  # type: ignore[attr-defined]
    stash_dir = env._media_dir / "transcode_src"  # type: ignore[attr-defined]
    assert list(stash_dir.iterdir()) == []


async def test_one_second_past_five_minutes_is_refused(env):
    album = await env.create_album(space_id=None, owner_user_id="a-id", name="Party")
    with pytest.raises(VideoTooLongError):
        await env.upload_item(
            album.id,
            data=make_clip(GALLERY_VIDEO_MAX_SECONDS + 1),
            content_type="video/mp4",
            caption=None,
            uploader_user_id="a-id",
        )


async def test_space_video_keeps_the_protocol_60s_limit(env):
    """A space album's videos go to member households: the 60 s protocol
    limit stays (cut there as before — the share choice is not here)."""
    album = await env.create_album(space_id="sp-1", owner_user_id="a-id", name="Trip")
    limits = await env.upload_limits(album.id, uploader_user_id="a-id")
    assert limits.video_seconds == VIDEO_MAX_DURATION_SECONDS == 60
    assert limits.video_bytes == GALLERY_MAX_UPLOAD_BYTES
    item = (
        await env.upload_item(
            album.id,
            data=make_clip(90),
            content_type="video/mp4",
            caption=None,
            uploader_user_id="a-id",
        )
    ).item
    assert item.duration_s == 60.0
    assert await env._transcode_service.flush_once() == 1  # type: ignore[attr-defined]
    out = env._media_dir / item.url.rsplit("/", 1)[-1]  # type: ignore[attr-defined]
    with av.open(str(out)) as c:
        assert c.duration / av.time_base == pytest.approx(60.0, abs=1.5)


async def test_household_upload_limits(env):
    album = await env.create_album(space_id=None, owner_user_id="a-id", name="Home")
    limits = await env.upload_limits(album.id, uploader_user_id="b-id")
    assert limits.video_seconds == GALLERY_VIDEO_MAX_SECONDS == 300
    assert limits.video_bytes == GALLERY_VIDEO_MAX_UPLOAD_BYTES
    assert limits.photo_bytes == GALLERY_MAX_UPLOAD_BYTES
    system = await env.ensure_system_album(space_id=None)
    with pytest.raises(GalleryPermissionError):
        await env.upload_limits(system.id, uploader_user_id="a-id")


async def test_portrait_phone_video_is_stored_upright(env):
    """A phone records portrait as landscape + a rotation; the stored
    size (and the WebM) is the picture as shown."""
    album = await env.create_album(space_id=None, owner_user_id="a-id", name="Home")
    item = (
        await env.upload_item(
            album.id,
            data=make_clip(2, size=(64, 32), rotation=90, split_colour=True),
            content_type="video/quicktime",
            caption=None,
            uploader_user_id="a-id",
        )
    ).item
    assert (item.width, item.height) == (32, 64)
    assert await env._transcode_service.flush_once() == 1  # type: ignore[attr-defined]
    out = env._media_dir / item.url.rsplit("/", 1)[-1]  # type: ignore[attr-defined]
    with av.open(str(out)) as c:
        frame = next(c.decode(video=0)).to_image()
    assert frame.size == (32, 64)
    # Turned counter-clockwise: the left (red) half is now at the bottom.
    assert frame.getpixel((16, 56))[0] > 200
    assert frame.getpixel((16, 8))[2] > 200


async def test_unreadable_video_is_422_and_leaves_nothing(env):
    album = await env.create_album(space_id=None, owner_user_id="a-id", name="Home")
    with pytest.raises(VideoUnreadableError):
        await env.upload_item(
            album.id,
            data=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200,
            content_type="video/mp4",
            caption=None,
            uploader_user_id="a-id",
        )
    stash_dir = env._media_dir / "transcode_src"  # type: ignore[attr-defined]
    assert list(stash_dir.iterdir()) == []
    assert await env._transcode_repo.count_queued() == 0  # type: ignore[attr-defined]


async def test_same_video_twice_is_already_here(env):
    album = await env.create_album(space_id=None, owner_user_id="a-id", name="Home")
    clip = make_clip(2)
    first = await env.upload_item(
        album.id,
        data=clip,
        content_type="video/mp4",
        caption=None,
        uploader_user_id="a-id",
    )
    again = await env.upload_item(
        album.id,
        data=clip,
        content_type="video/mp4",
        caption=None,
        uploader_user_id="a-id",
    )
    assert again.outcome is UploadOutcome.ALREADY_HERE
    assert again.item.id == first.item.id
    assert await env._transcode_repo.count_queued() == 1  # type: ignore[attr-defined]
    # Only the first upload's source waits for its conversion.
    stash_dir = env._media_dir / "transcode_src"  # type: ignore[attr-defined]
    assert len(list(stash_dir.iterdir())) == 1


async def test_a_full_conversion_queue_refuses_more_videos(env, monkeypatch):
    monkeypatch.setattr(
        "socialhome.services.gallery_service.GALLERY_VIDEO_MAX_QUEUED", 1
    )
    album = await env.create_album(space_id=None, owner_user_id="a-id", name="Home")
    await env.upload_item(
        album.id,
        data=make_clip(2),
        content_type="video/mp4",
        caption=None,
        uploader_user_id="a-id",
    )
    with pytest.raises(TranscodeQueueFullError):
        await env.upload_item(
            album.id,
            data=make_clip(3),
            content_type="video/mp4",
            caption=None,
            uploader_user_id="a-id",
        )
    stash_dir = env._media_dir / "transcode_src"  # type: ignore[attr-defined]
    assert len(list(stash_dir.iterdir())) == 1


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


async def test_stash_upload_streams_to_disk_and_hashes(env):
    stash = await env.stash_upload(_chunks(b"ab" * 1000, b"cd"), max_bytes=10_000)
    assert stash.size == 2002
    assert stash.sha256 == hashlib.sha256(b"ab" * 1000 + b"cd").hexdigest()
    assert pathlib.Path(stash.path).read_bytes() == b"ab" * 1000 + b"cd"
    assert pathlib.Path(stash.path).parent.name == "transcode_src"


async def test_stash_upload_over_the_cap_is_413_and_removed(env):
    with pytest.raises(PayloadTooLargeError):
        await env.stash_upload(_chunks(b"x" * 600, b"y" * 600), max_bytes=1000)
    stash_dir = env._media_dir / "transcode_src"  # type: ignore[attr-defined]
    assert list(stash_dir.iterdir()) == []


async def test_upload_video_to_a_hidden_album_removes_the_stash(env):
    stash = await env.stash_upload(_chunks(make_clip(2)), max_bytes=10_000_000)
    with pytest.raises(GalleryNotFoundError):
        await env.upload_video(
            "no-such-album", stash=stash, caption=None, uploader_user_id="a-id"
        )
    assert not pathlib.Path(stash.path).exists()


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


# ─── v_41 moderator: content authority over others' gallery content ──────


async def _with_moderator(env):
    """eve becomes a space moderator in sp-1."""
    await env._space_repo.save_member(  # type: ignore[attr-defined]
        SpaceMember(
            space_id="sp-1",
            user_id="e-id",
            role="moderator",
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )


async def _bobs_item(env):
    import io

    from PIL import Image

    album = await env.create_album(space_id="sp-1", owner_user_id="b-id", name="B")
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="JPEG")
    item = (
        await env.upload_item(
            album.id,
            data=buf.getvalue(),
            content_type="image/jpeg",
            caption=None,
            uploader_user_id="b-id",
        )
    ).item
    return album, item


async def test_a_moderator_deletes_others_items_but_not_their_albums(env):
    """M4 — item-level moderation is content authority; renaming or
    deleting somebody else's WHOLE album is settings authority (or being
    its owner)."""
    await _with_moderator(env)
    album, item = await _bobs_item(env)
    with pytest.raises(GalleryPermissionError):
        await env.update_album(album.id, actor_user_id="e-id", name="Tidied")
    with pytest.raises(GalleryPermissionError):
        await env.delete_album(album.id, actor_user_id="e-id")
    await env.delete_item(item.id, actor_user_id="e-id")
    # The owner (settings authority) and the album's owner still can.
    await env.update_album(album.id, actor_user_id="a-id", name="Renamed")
    await env.delete_album(album.id, actor_user_id="b-id")
    with pytest.raises(GalleryNotFoundError):
        await env.get_album(album.id, actor_user_id="a-id")


async def test_a_moderator_cannot_change_retention_on_others_albums(env):
    """Retention is a space setting — settings authority only."""
    await _with_moderator(env)
    album, _item = await _bobs_item(env)
    with pytest.raises(GalleryPermissionError):
        await env.set_retention_exempt(album.id, True, actor_user_id="e-id")
    # The owner (settings authority) still can.
    await env.set_retention_exempt(album.id, True, actor_user_id="a-id")


async def test_a_member_cannot_delete_others_items(env):
    await env._space_repo.save_member(  # type: ignore[attr-defined]
        SpaceMember(
            space_id="sp-1",
            user_id="e-id",
            role="member",
            joined_at="2026-01-01T00:00:00+00:00",
        )
    )
    _album, item = await _bobs_item(env)
    with pytest.raises(GalleryPermissionError):
        await env.delete_item(item.id, actor_user_id="e-id")


async def test_a_member_who_is_not_the_owner_still_gets_403_on_rename(env):
    """The by-id 404 is for outsiders only: a seated member who doesn't
    own the album keeps the owner/admin 403."""
    await env._space_repo.save_member(
        SpaceMember(space_id="sp-1", user_id="e-id", role="member", joined_at="")
    )
    a = await env.create_album(space_id="sp-1", owner_user_id="b-id", name="Bobs")
    with pytest.raises(GalleryPermissionError):
        await env.update_album(a.id, actor_user_id="e-id", name="Hijack")


async def test_a_follower_creates_no_album_and_uploads_nothing(env):
    await env._space_repo.save_member(
        SpaceMember(space_id="sp-1", user_id="e-id", role="subscriber", joined_at="")
    )
    with pytest.raises(SubscriberReadOnlyError):
        await env.create_album(space_id="sp-1", owner_user_id="e-id", name="Mine")
    a = await env.create_album(space_id="sp-1", owner_user_id="b-id", name="Bobs")
    with pytest.raises(SubscriberReadOnlyError):
        await env.upload_item(
            a.id,
            data=b"x",
            content_type="image/png",
            caption=None,
            uploader_user_id="e-id",
        )
    # A follower still reads the space gallery.
    assert (await env.get_album(a.id, actor_user_id="e-id")).id == a.id


# ─── EXIF "taken at" date ────────────────────────────────────────────────

#: EXIF tag ids, spelled out so the tests read like the EXIF spec.
_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_DATETIME = 0x0132
_DATETIME_ORIGINAL = 0x9003
_DATETIME_DIGITIZED = 0x9004
_OFFSET_TIME_ORIGINAL = 0x9011


def _image_bytes(
    *,
    fmt: str = "JPEG",
    exif_ifd: dict[int, str] | None = None,
    base: dict[int, str] | None = None,
    gps: bool = False,
) -> bytes:
    """A real image file whose EXIF carries the given tags.

    ``exif_ifd`` lands in the Exif sub-IFD (0x8769) — where cameras and
    phones write DateTimeOriginal — and ``base`` in IFD0.
    """
    exif = Image.Exif()
    for tag, value in (base or {}).items():
        exif[tag] = value
    if exif_ifd:
        sub = exif.get_ifd(_EXIF_IFD)
        for tag, value in exif_ifd.items():
            sub[tag] = value
    if gps:
        exif.get_ifd(_GPS_IFD).update({1: "N", 2: (52.0, 22.0, 1.0)})
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, format=fmt, exif=exif.tobytes())
    return buf.getvalue()


def test_a_camera_jpeg_dates_from_the_exif_sub_ifd():
    data = _image_bytes(exif_ifd={_DATETIME_ORIGINAL: "2024:05:17 21:45:03"})
    # The tag really sits in the sub-IFD, not in IFD0 — the camera shape.
    with Image.open(io.BytesIO(data)) as img:
        assert _DATETIME_ORIGINAL not in img.getexif()
    assert _extract_exif_date_sync(data) == "2024-05-17"


def test_a_heic_photo_dates_from_the_exif_sub_ifd():
    data = _image_bytes(
        fmt="HEIF", exif_ifd={_DATETIME_ORIGINAL: "2023:12:31 08:00:00"}
    )
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "HEIF"
    assert _extract_exif_date_sync(data) == "2023-12-31"


def test_digitized_answers_when_original_is_missing():
    data = _image_bytes(exif_ifd={_DATETIME_DIGITIZED: "2022:02:03 10:11:12"})
    assert _extract_exif_date_sync(data) == "2022-02-03"


def test_original_wins_over_digitized_and_the_file_date():
    data = _image_bytes(
        exif_ifd={
            _DATETIME_ORIGINAL: "2021:06:01 12:00:00",
            _DATETIME_DIGITIZED: "2021:06:02 12:00:00",
        },
        base={_DATETIME: "2025:01:01 00:00:00"},
    )
    assert _extract_exif_date_sync(data) == "2021-06-01"


def test_the_ifd0_file_date_is_the_last_fallback():
    data = _image_bytes(base={_DATETIME: "2020:07:04 09:30:00"})
    assert _extract_exif_date_sync(data) == "2020-07-04"


def test_the_camera_day_is_kept_whatever_its_offset():
    """A late-evening shot stays on the photographer's calendar day: the
    offset would move it to the next day in UTC, so it is not applied."""
    data = _image_bytes(
        exif_ifd={
            _DATETIME_ORIGINAL: "2024:05:17 23:30:00",
            _OFFSET_TIME_ORIGINAL: "-07:00",
        }
    )
    assert _extract_exif_date_sync(data) == "2024-05-17"


def test_no_exif_gives_no_date():
    assert _extract_exif_date_sync(_image_bytes()) is None


@pytest.mark.parametrize(
    "value",
    ["0000:00:00 00:00:00", "    :  :     :  :  ", "2024:13:40 10:00:00", "garbage"],
)
def test_a_malformed_date_gives_no_date(value):
    data = _image_bytes(exif_ifd={_DATETIME_ORIGINAL: value})
    assert _extract_exif_date_sync(data) is None


def test_a_malformed_original_falls_through_to_a_valid_digitized():
    data = _image_bytes(
        exif_ifd={
            _DATETIME_ORIGINAL: "0000:00:00 00:00:00",
            _DATETIME_DIGITIZED: "2019:09:09 09:09:09",
        }
    )
    assert _extract_exif_date_sync(data) == "2019-09-09"


def test_bytes_that_are_no_image_give_no_date():
    assert _extract_exif_date_sync(b"not an image") is None


async def test_an_uploaded_camera_photo_keeps_its_day_and_no_exif(env):
    """End to end: the day is read before ImageProcessor strips EXIF; the
    stored files carry no EXIF at all (no time, no GPS)."""
    album = await env.create_album(space_id=None, owner_user_id="a-id", name="Trip")
    data = _image_bytes(exif_ifd={_DATETIME_ORIGINAL: "2024:05:17 21:45:03"}, gps=True)
    item = (
        await env.upload_item(
            album.id,
            data=data,
            content_type="image/jpeg",
            caption=None,
            uploader_user_id="a-id",
        )
    ).item
    assert item.taken_at == "2024-05-17"
    media_dir = pathlib.Path(env._media_dir)
    for url in (item.url, item.thumbnail_url):
        stored = (media_dir / url.rsplit("/", 1)[1]).read_bytes()
        with Image.open(io.BytesIO(stored)) as img:
            assert not img.getexif()
            assert "exif" not in img.info


# ─── Household albums: who may delete and manage (UX review #4) ─────────
#
# alice is the household admin, bob and eve are members. Before the fix any
# household member could delete any household photo, and a household admin
# could not tidy someone else's album.


def _jpeg(color: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    """A small JPEG; a different ``color`` is a different file (the
    household duplicate check matches exact bytes)."""
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buf, format="JPEG")
    return buf.getvalue()


async def _bobs_household_item(env, *, uploader: str = "b-id"):
    album = await env.create_album(space_id=None, owner_user_id="b-id", name="Bob's")
    item = (
        await env.upload_item(
            album.id,
            data=_jpeg(),
            content_type="image/jpeg",
            caption=None,
            uploader_user_id=uploader,
        )
    ).item
    return album, item


async def test_a_household_member_cannot_delete_someone_elses_photo(env):
    album, item = await _bobs_household_item(env)
    with pytest.raises(GalleryPermissionError):
        await env.delete_item(item.id, actor_user_id="e-id")
    assert [i.id for i in await env.list_items(album.id, actor_user_id="b-id")] == [
        item.id
    ]


async def test_the_uploader_deletes_their_own_household_photo(env):
    album, item = await _bobs_household_item(env, uploader="e-id")
    await env.delete_item(item.id, actor_user_id="e-id")
    assert await env.list_items(album.id, actor_user_id="b-id") == []


async def test_the_album_owner_cannot_delete_someone_elses_photo(env):
    """D6: one by one, a photo is its uploader's (or an admin's) — the
    album's owner deletes the album as a whole instead."""
    album, item = await _bobs_household_item(env, uploader="e-id")
    with pytest.raises(GalleryPermissionError):
        await env.delete_item(item.id, actor_user_id="b-id")
    assert [i.id for i in await env.list_items(album.id, actor_user_id="b-id")] == [
        item.id
    ]
    await env.delete_album(album.id, actor_user_id="b-id")
    # Its uploader brings the photo back (and the album with it).
    await env.restore_item(item.id, actor_user_id="e-id")
    assert [i.id for i in await env.list_items(album.id, actor_user_id="b-id")] == [
        item.id
    ]


async def test_a_household_admin_deletes_any_household_photo(env):
    album, item = await _bobs_household_item(env, uploader="e-id")
    await env.delete_item(item.id, actor_user_id="a-id")
    assert await env.list_items(album.id, actor_user_id="b-id") == []


async def test_a_household_admin_manages_someone_elses_household_album(env):
    album, _item = await _bobs_household_item(env)
    await env.update_album(album.id, actor_user_id="a-id", name="Tidied")
    await env.set_retention_exempt(album.id, True, actor_user_id="a-id")
    assert (await env.get_album(album.id, actor_user_id="b-id")).name == "Tidied"
    await env.delete_album(album.id, actor_user_id="a-id")
    with pytest.raises(GalleryNotFoundError):
        await env.get_album(album.id, actor_user_id="b-id")


async def test_a_household_member_cannot_manage_someone_elses_household_album(env):
    album, _item = await _bobs_household_item(env)
    with pytest.raises(GalleryPermissionError):
        await env.update_album(album.id, actor_user_id="e-id", name="Hijack")
    with pytest.raises(GalleryPermissionError):
        await env.set_retention_exempt(album.id, True, actor_user_id="e-id")
    with pytest.raises(GalleryPermissionError):
        await env.delete_album(album.id, actor_user_id="e-id")
    assert (await env.get_album(album.id, actor_user_id="b-id")).name == "Bob's"


async def test_a_household_admin_gets_no_say_over_a_space_album(env):
    """A space album follows the space's roles: a household admin who is a
    plain member of the space is no space admin."""
    await env._space_repo.save_member(
        SpaceMember(space_id="sp-1", user_id="a-id", role="member", joined_at="")
    )
    album, item = await _bobs_item(env)
    with pytest.raises(GalleryPermissionError):
        await env.update_album(album.id, actor_user_id="a-id", name="Hijack")
    with pytest.raises(GalleryPermissionError):
        await env.delete_item(item.id, actor_user_id="a-id")


async def test_a_deactivated_admin_is_no_household_admin(env):
    _album, item = await _bobs_household_item(env)
    await env._users._db.enqueue(  # type: ignore[union-attr]
        "UPDATE users SET state='inactive' WHERE user_id='a-id'"
    )
    with pytest.raises(GalleryPermissionError):
        await env.delete_item(item.id, actor_user_id="a-id")


async def test_without_a_user_repo_nobody_is_a_household_admin(env):
    _album, item = await _bobs_household_item(env)
    env._users = None
    with pytest.raises(GalleryPermissionError):
        await env.delete_item(item.id, actor_user_id="a-id")


async def test_a_protected_account_deletes_only_its_own_uploads(env):
    """Live walk: Mia (protected, 9) deleted her mother's photo with a 204.
    A protected account removes only what it uploaded — even in its own
    album, and not by deleting an album that holds someone else's photo."""
    await env._users._db.enqueue(  # type: ignore[union-attr]
        "UPDATE users SET child_protection_enabled=1 WHERE user_id='e-id'"
    )
    album = await env.create_album(space_id=None, owner_user_id="e-id", name="Mia's")
    bobs = (
        await env.upload_item(
            album.id,
            data=_jpeg(),
            content_type="image/jpeg",
            caption=None,
            uploader_user_id="b-id",
        )
    ).item
    own = (
        await env.upload_item(
            album.id,
            data=_jpeg((10, 200, 10)),
            content_type="image/jpeg",
            caption=None,
            uploader_user_id="e-id",
        )
    ).item
    with pytest.raises(GalleryPermissionError):
        await env.delete_item(bobs.id, actor_user_id="e-id")
    with pytest.raises(GalleryPermissionError):
        await env.delete_album(album.id, actor_user_id="e-id")
    await env.delete_item(own.id, actor_user_id="e-id")
    # Once only her own photos are left, the album is hers to delete.
    await env.delete_item(bobs.id, actor_user_id="b-id")
    await env.delete_album(album.id, actor_user_id="e-id")
    with pytest.raises(GalleryNotFoundError):
        await env.get_album(album.id, actor_user_id="b-id")


# ─── System "Posts" album: blocks and thumbnails (UX review #11, #18) ────


def _photo_post(pid: str, author: str, *names: str) -> Post:
    return Post(
        id=pid,
        author=author,
        type=PostType.IMAGE,
        created_at=datetime.now(UTC),
        content="x",
        image_urls=tuple(f"/api/media/{n}" for n in names),
    )


async def _store_photo(env, name: str, size=(1600, 1200)) -> None:
    media = env._media_dir
    media.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (10, 90, 160)).save(media / name, format="WEBP")


async def test_the_posts_album_hides_people_the_viewer_blocked(env):
    """The feed hides a blocked author's posts; the album that mirrors the
    feed hides them too. A regular album is not the feed's mirror."""
    await env.mirror_post(_photo_post("p-bob", "b-id", "bob.webp"), None)
    await env.mirror_post(_photo_post("p-eve", "e-id", "eve.webp"), None)
    posts = await env.ensure_system_album(None)
    regular, bobs_upload = await _bobs_household_item(env)
    await env._users._db.enqueue(  # type: ignore[union-attr]
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id)"
        " VALUES('a-id', 'b-id')"
    )
    alice = await env.list_items(posts.id, actor_user_id="a-id")
    assert {i.uploaded_by for i in alice} == {"e-id"}
    eve = await env.list_items(posts.id, actor_user_id="e-id")
    assert {i.uploaded_by for i in eve} == {"b-id", "e-id"}
    mine = await env.list_items(regular.id, actor_user_id="a-id")
    assert [i.id for i in mine] == [bobs_upload.id]
    # The album card counts what each viewer sees, not every row.
    assert (await env.get_album(posts.id, actor_user_id="a-id")).item_count == 1
    assert (await env.get_album(posts.id, actor_user_id="e-id")).item_count == 2
    (listed,) = [
        a
        for a in await env.list_albums(space_id=None, actor_user_id="a-id")
        if a.is_system
    ]
    assert listed.item_count == 1
    assert (await env.get_album(regular.id, actor_user_id="a-id")).item_count == 1


async def test_a_mirrored_photo_gets_a_real_thumbnail(env):
    """The grid used the post's full 2560 px file as each tile."""
    await _store_photo(env, "big.webp")
    await env.mirror_post(_photo_post("p-1", "a-id", "big.webp"), None)
    (item,) = await env._repo.list_items_by_source_post("p-1")
    assert item.url == "api/media/big.webp"
    assert item.thumbnail_url != item.url
    thumb = env._media_dir / item.thumbnail_url.rsplit("/", 1)[-1]
    with Image.open(thumb) as img:
        assert max(img.size) <= 512
    # Deleting the post drops the thumbnail it made, never the post's file.
    await env.unmirror_post("p-1")
    assert not thumb.exists()
    assert (env._media_dir / "big.webp").exists()


async def test_a_mirrored_photo_not_on_disk_yet_falls_back_to_its_file(env):
    await env.mirror_post(_photo_post("p-2", "a-id", "later.webp"), None)
    (item,) = await env._repo.list_items_by_source_post("p-2")
    assert item.thumbnail_url == item.url


async def test_a_mirrored_video_uses_its_poster(env):
    post = Post(
        id="p-v",
        author="a-id",
        type=PostType.VIDEO,
        created_at=datetime.now(UTC),
        content="x",
        media_url="/api/media/clip.webm",
    )
    await env.mirror_post(post, None)
    (item,) = await env._repo.list_items_by_source_post("p-v")
    assert item.thumbnail_url == "api/media/clip.webp"
    await env.unmirror_post("p-v")


async def test_an_edit_replaces_the_thumbnails_it_made(env):
    await _store_photo(env, "one.webp")
    await _store_photo(env, "two.webp")
    await env.mirror_post(_photo_post("p-3", "a-id", "one.webp"), None)
    (first,) = await env._repo.list_items_by_source_post("p-3")
    await env.mirror_post(_photo_post("p-3", "a-id", "two.webp"), None)
    (second,) = await env._repo.list_items_by_source_post("p-3")
    assert second.url == "api/media/two.webp"
    assert not (env._media_dir / first.thumbnail_url.rsplit("/", 1)[-1]).exists()
    assert (env._media_dir / second.thumbnail_url.rsplit("/", 1)[-1]).exists()


async def test_list_items_pages_newest_first_through_the_service(env):
    album = await env.create_album(space_id=None, owner_user_id="a-id", name="Lots")
    ids = []
    for n in range(5):
        it = (
            await env.upload_item(
                album.id,
                # Five different files: the same bytes would be one photo.
                data=_jpeg((n * 50, 40, 40)),
                content_type="image/jpeg",
                caption=None,
                uploader_user_id="a-id",
            )
        ).item
        ids.append(it.id)
    first = await env.list_items(album.id, actor_user_id="a-id", limit=3)
    rest = await env.list_items(
        album.id,
        actor_user_id="a-id",
        limit=3,
        before=first[-1].created_at,
        before_id=first[-1].id,
    )
    assert [i.id for i in first + rest] == list(reversed(ids))


# ─── Library model (migration 0139): duplicates, trash, leaving ─────────


async def _household_album(env, owner: str = "a-id", name: str = "Summer"):
    return await env.create_album(space_id=None, owner_user_id=owner, name=name)


async def _upload(env, album_id: str, data: bytes, who: str = "a-id"):
    return await env.upload_item(
        album_id,
        data=data,
        content_type="image/jpeg",
        caption=None,
        uploader_user_id=who,
    )


def _stored_files(env) -> set[str]:
    media_dir = env._media_dir  # type: ignore[attr-defined]
    return {p.name for p in media_dir.iterdir() if p.is_file()}


def _published(env, kind: type) -> list:
    seen: list = []

    async def _grab(event) -> None:
        seen.append(event)

    env._bus.subscribe(kind, _grab)  # type: ignore[attr-defined]
    return seen


async def test_the_same_photo_uploaded_twice_is_already_here(env):
    """An exact re-upload stores nothing: the same item comes back, no
    new row, no new file — whichever album it was picked for."""
    album = await _household_album(env)
    other = await _household_album(env, owner="b-id", name="Bob's")
    first = await _upload(env, album.id, _jpeg())
    files = _stored_files(env)

    again = await _upload(env, other.id, _jpeg(), who="b-id")

    assert first.outcome is UploadOutcome.CREATED
    assert again.outcome is UploadOutcome.ALREADY_HERE
    assert again.item.id == first.item.id
    assert _stored_files(env) == files
    assert (await env.get_album(other.id, actor_user_id="b-id")).item_count == 0


async def test_the_digest_is_of_the_uploaded_bytes_and_the_bytes_are_not_kept(env):
    """``content_sha256`` hashes the upload before conversion (every stored
    file is a fresh WebP re-encode); only the WebP and its thumbnail are
    written, never the uploaded JPEG."""
    album = await _household_album(env)
    data = _jpeg()
    up = await _upload(env, album.id, data)
    row = await env._repo._db.fetchone(  # type: ignore[attr-defined]
        "SELECT content_sha256 FROM gallery_items WHERE id=?", (up.item.id,)
    )
    assert row[0] == hashlib.sha256(data).hexdigest()
    media_dir = env._media_dir  # type: ignore[attr-defined]
    stored = [(media_dir / n).read_bytes() for n in _stored_files(env)]
    assert data not in stored
    assert all(b[:4] == b"RIFF" and b[8:12] == b"WEBP" for b in stored)


async def test_a_photo_in_a_private_album_is_no_duplicate_for_others(env):
    """The duplicate answer must not reveal a private album's photo: for
    someone outside its audience the upload is simply stored again."""
    private = await _household_album(env, name="Gifts")
    hidden = await _upload(env, private.id, _jpeg())
    assert await env._repo.set_album_audience(private.id, "chosen")  # type: ignore[attr-defined]
    mine = await _household_album(env, owner="b-id", name="Bob's")

    up = await _upload(env, mine.id, _jpeg(), who="b-id")

    assert up.outcome is UploadOutcome.CREATED
    assert up.item.id != hidden.item.id


async def test_a_private_album_s_audience_sees_the_duplicate(env):
    private = await _household_album(env, name="Gifts")
    first = await _upload(env, private.id, _jpeg())
    await env._repo.set_album_audience(private.id, "chosen", frozenset({"b-id"}))  # type: ignore[attr-defined]
    mine = await _household_album(env, owner="b-id", name="Bob's")

    up = await _upload(env, mine.id, _jpeg(), who="b-id")

    assert up.outcome is UploadOutcome.ALREADY_HERE
    assert up.item.id == first.item.id


async def test_uploading_a_trashed_photo_again_brings_it_back(env):
    album = await _household_album(env)
    first = await _upload(env, album.id, _jpeg())
    await env.delete_item(first.item.id, actor_user_id="a-id")
    restored = _published(env, GalleryItemRestored)

    again = await _upload(env, album.id, _jpeg())

    assert again.outcome is UploadOutcome.RESTORED
    assert again.item.id == first.item.id
    assert await env._repo.get_item(first.item.id) is not None  # type: ignore[attr-defined]
    assert [e.item_id for e in restored] == [first.item.id]
    assert await env.list_trash(actor_user_id="a-id") == []


async def test_a_space_upload_is_never_checked_for_duplicates(env):
    album = await env.create_album(space_id="sp-1", owner_user_id="a-id", name="T")
    one = await _upload(env, album.id, _jpeg())
    two = await _upload(env, album.id, _jpeg())
    assert one.item.id != two.item.id
    rows = await env._repo._db.fetchall(  # type: ignore[attr-defined]
        "SELECT content_sha256 FROM gallery_items WHERE album_id=?", (album.id,)
    )
    assert [r[0] for r in rows] == [None, None]


async def test_a_deleted_household_photo_waits_in_the_trash(env):
    album = await _household_album(env)
    up = await _upload(env, album.id, _jpeg())
    deleted = _published(env, GalleryItemDeleted)

    await env.delete_item(up.item.id, actor_user_id="a-id")

    assert [e.item_id for e in deleted] == [up.item.id]
    assert await env.list_items(album.id, actor_user_id="a-id") == []
    assert (await env.get_album(album.id, actor_user_id="a-id")).item_count == 0
    (trashed,) = await env.list_trash(actor_user_id="b-id")
    assert trashed.item.id == up.item.id
    assert trashed.deleted_by == "a-id"
    assert trashed.album_name == "Summer"
    assert not trashed.album_in_trash
    # Deleting it again is a no-op, not a second event.
    await env.delete_item(up.item.id, actor_user_id="a-id")
    assert len(deleted) == 1

    back = await env.restore_item(up.item.id, actor_user_id="a-id")
    assert back.id == up.item.id
    assert [i.id for i in await env.list_items(album.id, actor_user_id="a-id")] == [
        up.item.id
    ]
    assert (await env.get_album(album.id, actor_user_id="a-id")).item_count == 1


async def test_a_deleted_album_comes_back_with_a_restored_photo(env):
    album = await _household_album(env)
    one = await _upload(env, album.id, _jpeg())
    two = await _upload(env, album.id, _jpeg((10, 10, 200)))

    await env.delete_album(album.id, actor_user_id="a-id")

    with pytest.raises(GalleryNotFoundError):
        await env.get_album(album.id, actor_user_id="a-id")
    trash = await env.list_trash(actor_user_id="a-id")
    assert {t.item.id for t in trash} == {one.item.id, two.item.id}
    assert all(t.album_in_trash for t in trash)

    await env.restore_item(one.item.id, actor_user_id="a-id")

    back = await env.get_album(album.id, actor_user_id="a-id")
    assert back.name == "Summer" and back.item_count == 1
    assert [t.item.id for t in await env.list_trash(actor_user_id="a-id")] == [
        two.item.id
    ]


async def test_only_who_may_delete_may_restore_or_purge(env):
    album = await _household_album(env, owner="b-id", name="Bob's")
    up = await _upload(env, album.id, _jpeg(), who="b-id")
    await env.delete_item(up.item.id, actor_user_id="b-id")

    with pytest.raises(GalleryPermissionError):  # eve: not uploader/owner/admin
        await env.restore_item(up.item.id, actor_user_id="e-id")
    with pytest.raises(GalleryPermissionError):
        await env.purge_trashed_item(up.item.id, actor_user_id="e-id")
    with pytest.raises(GalleryNotFoundError):
        await env.restore_item("nope", actor_user_id="a-id")
    # A household admin may.
    await env.restore_item(up.item.id, actor_user_id="a-id")


async def test_a_protected_account_restores_only_its_own(env):
    await env._users._db.enqueue(  # type: ignore[union-attr]
        "UPDATE users SET child_protection_enabled=1 WHERE user_id='e-id'"
    )
    album = await _household_album(env, owner="e-id", name="Mia's")
    up = await _upload(env, album.id, _jpeg(), who="b-id")
    await env.delete_item(up.item.id, actor_user_id="b-id")
    with pytest.raises(GalleryPermissionError):
        await env.restore_item(up.item.id, actor_user_id="e-id")


async def test_a_private_album_s_trash_is_hidden_from_others(env):
    private = await _household_album(env, name="Gifts")
    up = await _upload(env, private.id, _jpeg())
    await env._repo.set_album_audience(private.id, "chosen")  # type: ignore[attr-defined]
    await env.delete_item(up.item.id, actor_user_id="a-id")

    assert await env.list_trash(actor_user_id="b-id") == []
    with pytest.raises(GalleryNotFoundError):
        await env.restore_item(up.item.id, actor_user_id="b-id")
    assert [t.item.id for t in await env.list_trash(actor_user_id="a-id")] == [
        up.item.id
    ]


async def test_the_purge_waits_thirty_days(env):
    album = await _household_album(env)
    up = await _upload(env, album.id, _jpeg())
    await env.delete_album(album.id, actor_user_id="a-id")
    now = datetime.now(UTC)

    assert await env.purge_expired_trash(now=now + timedelta(days=29)) == 0
    assert len(await env.list_trash(actor_user_id="a-id")) == 1

    assert await env.purge_expired_trash(now=now + timedelta(days=31)) == 2
    assert await env.list_trash(actor_user_id="a-id") == []
    assert await env._repo.get_trashed_item(up.item.id) is None  # type: ignore[attr-defined]
    row = await env._repo._db.fetchone(  # type: ignore[attr-defined]
        "SELECT COUNT(*) FROM gallery_albums WHERE id=?", (album.id,)
    )
    assert row[0] == 0  # the emptied album went with its last photo


async def test_the_purge_works_in_bounded_batches(env):
    album = await _household_album(env)
    for shade in range(3):
        up = await _upload(env, album.id, _jpeg((shade * 60, 0, 0)))
        await env.delete_item(up.item.id, actor_user_id="a-id")
    later = datetime.now(UTC) + timedelta(days=31)

    # One photo per batch: its file and thumbnail.
    assert await env.purge_expired_trash(now=later, batch=1, max_batches=1) == 2
    assert len(await env.list_trash(actor_user_id="a-id")) == 2
    assert await env.purge_expired_trash(now=later, batch=1) == 4
    assert await env.list_trash(actor_user_id="a-id") == []


async def test_a_space_delete_has_no_trash(env):
    album = await env.create_album(space_id="sp-1", owner_user_id="a-id", name="T")
    up = await _upload(env, album.id, _jpeg())
    await env.delete_item(up.item.id, actor_user_id="a-id")
    assert await env.list_trash(actor_user_id="a-id") == []
    assert await env._repo.is_item_deleted(up.item.id, space_id="sp-1")  # type: ignore[attr-defined]


async def test_leaving_the_household_keeps_photos_and_hands_albums_to_an_admin(env):
    """Guard: removing a person never removes their photos; the household
    albums they owned pass to an admin — except a private one, which an
    admin must not gain a view of: it stays Bob's while someone else sees
    it, and goes to the trash when nobody else does (r3)."""
    env.wire()
    shared = await _household_album(env, owner="b-id", name="Bob's")
    private = await _household_album(env, owner="b-id", name="Bob's secret")
    await env._repo.set_album_audience(private.id, "chosen", frozenset({"a-id"}))  # type: ignore[attr-defined]
    alone = await _household_album(env, owner="b-id", name="Only Bob")
    await env._repo.set_album_audience(alone.id, "chosen")  # type: ignore[attr-defined]
    in_space = await env.create_album(space_id="sp-1", owner_user_id="b-id", name="S")
    up = await _upload(env, shared.id, _jpeg(), who="b-id")

    await env._users.soft_delete("bob")  # type: ignore[attr-defined]
    await env._bus.publish(UserDeprovisioned(user_id="b-id", username="bob"))  # type: ignore[attr-defined]

    assert (
        await env.get_album(shared.id, actor_user_id="a-id")
    ).owner_user_id == "a-id"
    assert (
        await env.get_album(private.id, actor_user_id="b-id")
    ).owner_user_id == "b-id"
    assert (
        await env.get_album(in_space.id, actor_user_id="a-id")
    ).owner_user_id == "b-id"
    assert await env._repo.get_trashed_album(alone.id) is not None  # type: ignore[attr-defined]
    (kept,) = await env.list_items(shared.id, actor_user_id="a-id")
    assert kept.id == up.item.id and kept.uploaded_by == "b-id"


async def test_with_no_admin_left_the_albums_stay(env):
    env.wire()
    album = await _household_album(env, owner="a-id")
    await env._users._db.enqueue("UPDATE users SET is_admin=0")  # type: ignore[attr-defined]
    await env._bus.publish(UserDeprovisioned(user_id="a-id", username="alice"))  # type: ignore[attr-defined]
    assert (await env.get_album(album.id, actor_user_id="a-id")).owner_user_id == "a-id"


# ─── Stored format (domain/media_constraints.py) ──────────────────────────


async def test_a_household_photo_is_stored_as_webp_within_2560(env):
    """Owner rule: everything in the gallery is converted to the defined
    format and resolution — WebP, longest side ≤ IMAGE_MAX_DIMENSION, and a
    WebP thumbnail ≤ THUMBNAIL_PX. Never the uploaded original."""
    album = await _household_album(env)
    buf = io.BytesIO()
    Image.new("RGB", (4000, 3000), (30, 90, 160)).save(buf, format="JPEG")
    up = await _upload(env, album.id, buf.getvalue())
    media_dir = env._media_dir  # type: ignore[attr-defined]
    for url, cap in (
        (up.item.url, IMAGE_MAX_DIMENSION),
        (up.item.thumbnail_url, THUMBNAIL_PX),
    ):
        with Image.open(media_dir / url.rsplit("/", 1)[-1]) as img:
            assert img.format == "WEBP"
            assert max(img.size) <= cap
    assert max(up.item.width, up.item.height) == IMAGE_MAX_DIMENSION


async def test_a_household_video_is_stored_as_webm_within_1920(env):
    """A video is transcoded to VP9/Opus WebM, longest side ≤
    VIDEO_MAX_DIMENSION, with a WebP poster — the real processor on a
    2560-wide source."""
    clip = io.BytesIO()
    out = av.open(clip, mode="w", format="webm")
    try:
        stream = out.add_stream("vp9", rate=1)
        stream.width, stream.height, stream.pix_fmt = 2560, 1440, "yuv420p"
        frame = av.VideoFrame.from_image(Image.new("RGB", (2560, 1440), (9, 9, 99)))
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    finally:
        out.close()
    album = await _household_album(env)
    up = await env.upload_item(
        album.id,
        data=clip.getvalue(),
        content_type="video/webm",
        caption=None,
        uploader_user_id="a-id",
    )
    assert up.item.url.endswith(".webm")
    assert await env._transcode_service.flush_once() == 1  # type: ignore[attr-defined]

    media_dir = env._media_dir  # type: ignore[attr-defined]
    with av.open(str(media_dir / up.item.url.rsplit("/", 1)[-1])) as container:
        assert "webm" in container.format.name
        video = container.streams.video[0]
        assert video.codec_context.name == "vp9"
        longest = max(video.codec_context.width, video.codec_context.height)
        assert longest <= VIDEO_MAX_DIMENSION
    with Image.open(media_dir / up.item.thumbnail_url.rsplit("/", 1)[-1]) as poster:
        assert poster.format == "WEBP"


# ─── Default album (upload without choosing an album) ────────────────────


async def test_no_default_album_until_the_first_upload_without_one(env):
    assert await env.get_default_album(actor_user_id="b-id") is None


async def test_the_default_album_is_made_once_and_then_reused(env):
    first, created = await env.ensure_default_album(actor_user_id="b-id", name="Fotos")
    assert created is True
    assert first.name == "Fotos"
    assert first.space_id is None
    assert first.owner_user_id == "b-id"
    # Someone else, in another language, gets the same album.
    again, created_again = await env.ensure_default_album(
        actor_user_id="a-id", name="Photos"
    )
    assert created_again is False
    assert again.id == first.id
    assert again.name == "Fotos"
    shown = await env.get_default_album(actor_user_id="e-id")
    assert shown is not None and shown.id == first.id


async def test_two_first_uploads_at_once_make_one_default_album(env):
    results = await asyncio.gather(
        env.ensure_default_album(actor_user_id="a-id", name="Photos"),
        env.ensure_default_album(actor_user_id="b-id", name="Fotos"),
    )
    assert results[0][0].id == results[1][0].id
    assert sorted(created for _, created in results) == [False, True]


async def test_a_deleted_default_album_is_made_afresh(env):
    first, _ = await env.ensure_default_album(actor_user_id="a-id", name="Photos")
    await env.delete_album(first.id, actor_user_id="a-id")
    assert await env.get_default_album(actor_user_id="a-id") is None
    second, created = await env.ensure_default_album(actor_user_id="b-id", name="Fotos")
    assert created is True
    assert second.id != first.id


async def test_a_default_key_naming_a_space_album_is_ignored(env):
    space_album = await env.create_album(
        space_id="sp-1", owner_user_id="a-id", name="Space"
    )
    await env._instance_config.set(DEFAULT_ALBUM_KEY, space_album.id)
    assert await env.get_default_album(actor_user_id="a-id") is None
    album, created = await env.ensure_default_album(actor_user_id="a-id", name="Photos")
    assert created is True
    assert album.space_id is None


async def test_the_default_album_keeps_its_default_name_until_renamed(env):
    """Each viewer sees the default album under their own word for
    "Photos" (``default_name``) — not the first uploader's — until
    someone names it; then everybody sees that name."""
    album, _ = await env.ensure_default_album(actor_user_id="b-id", name="Fotos")
    other = await env.create_album(space_id=None, owner_user_id="a-id", name="Summer")
    assert (album.is_default, album.default_name) == (True, True)
    listed = {
        a.id: a for a in await env.list_albums(space_id=None, actor_user_id="e-id")
    }
    assert (listed[album.id].is_default, listed[album.id].default_name) == (True, True)
    assert (listed[other.id].is_default, listed[other.id].default_name) == (
        False,
        False,
    )
    # Renaming another album, or editing only the description, changes nothing.
    await env.update_album(other.id, actor_user_id="a-id", name="Sommer")
    await env.update_album(album.id, actor_user_id="b-id", description="All of us")
    shown = await env.get_album(album.id, actor_user_id="e-id")
    assert shown.default_name is True
    await env.update_album(album.id, actor_user_id="b-id", name="Familie")
    shown = await env.get_album(album.id, actor_user_id="e-id")
    assert (shown.name, shown.is_default, shown.default_name) == (
        "Familie",
        True,
        False,
    )
    # Deleted, the next default album is in each viewer's language again.
    await env.delete_album(album.id, actor_user_id="b-id")
    fresh, _ = await env.ensure_default_album(actor_user_id="a-id", name="Photos")
    assert fresh.default_name is True


async def test_space_albums_are_never_the_default(env):
    await env.ensure_default_album(actor_user_id="a-id", name="Photos")
    space_album = await env.create_album(
        space_id="sp-1", owner_user_id="a-id", name="Space"
    )
    shown = await env.get_album(space_album.id, actor_user_id="a-id")
    assert not shown.is_default and not shown.default_name


async def test_the_default_album_needs_a_name(env):
    with pytest.raises(ValueError):
        await env.ensure_default_album(actor_user_id="a-id", name="  ")


async def test_without_the_config_repo_there_is_no_default(tmp_dir):
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "x.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
    )
    svc = GalleryService(None, None, EventBus(), cfg)  # type: ignore[arg-type]
    assert await svc.get_default_album(actor_user_id="a-id") is None
    with pytest.raises(RuntimeError):
        await svc.ensure_default_album(actor_user_id="a-id", name="Photos")


# ─── Storage format: an upload into the default album, HEIC, quality ─────
# Owner rule: images WebP ≤ IMAGE_MAX_DIMENSION at IMAGE_WEBP_QUALITY,
# videos VP9/Opus WebM ≤ VIDEO_MAX_DIMENSION, thumbnails / posters WebP ≤
# THUMBNAIL_PX. No originals, no other formats, whatever the upload was.


def _stored(svc: GalleryService, url: str) -> bytes:
    return (svc._media_dir / url.removeprefix("api/media/")).read_bytes()


def _assert_webp_within(data: bytes, max_px: int) -> tuple[int, int]:
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    img = Image.open(io.BytesIO(data))
    assert img.format == "WEBP"
    assert max(img.size) <= max_px
    return img.size


async def test_a_large_phone_photo_is_stored_as_webp_at_the_defined_size(env):
    album, _ = await env.ensure_default_album(actor_user_id="b-id", name="Photos")
    img = Image.new("RGB", (4032, 3024), (200, 120, 40))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    item = (
        await env.upload_item(
            album.id,
            data=buf.getvalue(),
            content_type="image/jpeg",
            caption=None,
            uploader_user_id="b-id",
        )
    ).item
    size = _assert_webp_within(_stored(env, item.url), IMAGE_MAX_DIMENSION)
    assert size == (IMAGE_MAX_DIMENSION, 1920)
    assert (item.width, item.height) == size
    _assert_webp_within(_stored(env, item.thumbnail_url), THUMBNAIL_PX)
    # Only the web copy and its thumbnail — never the original bytes.
    names = {p.name for p in env._media_dir.iterdir() if p.is_file()}
    assert names == {
        item.url.removeprefix("api/media/"),
        item.thumbnail_url.removeprefix("api/media/"),
    }


async def test_the_webp_quality_is_the_defined_one(env, monkeypatch):
    seen: list[int] = []
    real_save = Image.Image.save

    def spy(self, fp, format=None, **params):
        if format == "WEBP" and self.size[0] > 600:
            seen.append(params.get("quality"))
        return real_save(self, fp, format, **params)

    monkeypatch.setattr(Image.Image, "save", spy)
    album, _ = await env.ensure_default_album(actor_user_id="b-id", name="Photos")
    buf = io.BytesIO()
    Image.new("RGB", (1200, 800), (10, 20, 30)).save(buf, "PNG")
    await env.upload_item(
        album.id,
        data=buf.getvalue(),
        content_type="image/png",
        caption=None,
        uploader_user_id="b-id",
    )
    assert seen == [IMAGE_WEBP_QUALITY]


async def test_a_heic_photo_is_stored_as_webp(env):
    register_heif_opener()
    album, _ = await env.ensure_default_album(actor_user_id="b-id", name="Photos")
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (90, 160, 30)).save(buf, "HEIF")
    item = (
        await env.upload_item(
            album.id,
            data=buf.getvalue(),
            content_type="image/heic",
            caption=None,
            uploader_user_id="b-id",
        )
    ).item
    assert item.item_type == "photo"
    assert _assert_webp_within(_stored(env, item.url), IMAGE_MAX_DIMENSION) == (
        64,
        48,
    )


# ─── "Neu" dot (D9) ──────────────────────────────────────────────────────


async def _add(env, album_id: str, who: str, item_id: str, *, ago_s: int = 0) -> None:
    at = (datetime.now(UTC) - timedelta(seconds=ago_s)).isoformat()
    await env._repo.create_item(  # type: ignore[attr-defined]
        GalleryItem(
            id=item_id,
            album_id=album_id,
            uploaded_by=who,
            item_type="photo",
            url=f"/api/media/{item_id}.webp",
            thumbnail_url=f"/api/media/{item_id}-t.webp",
            width=10,
            height=10,
            created_at=at,
        )
    )


async def _dots(env, user_id: str, space_id: str | None = None) -> dict[str, bool]:
    albums = await env.list_albums(space_id=space_id, actor_user_id=user_id)
    return {a.name: a.has_new for a in albums}


async def test_the_new_dot_shows_others_photos_until_the_album_is_opened(env):
    trip = await env.create_album(space_id=None, owner_user_id="a-id", name="Trip")
    await env.create_album(space_id=None, owner_user_id="a-id", name="Old")
    await _add(env, trip.id, "a-id", "old-1", ago_s=3600)
    # The first look only starts the clock: nothing is new on day one.
    assert await _dots(env, "b-id") == {"Trip": False, "Old": False}
    await asyncio.sleep(0.01)
    await _add(env, trip.id, "a-id", "new-1")
    await _add(env, trip.id, "b-id", "own-1")
    assert await _dots(env, "b-id") == {"Trip": True, "Old": False}
    assert (await env.get_album(trip.id, actor_user_id="b-id")).has_new
    await env.mark_album_seen(trip.id, actor_user_id="b-id")
    assert await _dots(env, "b-id") == {"Trip": False, "Old": False}
    assert not (await env.get_album(trip.id, actor_user_id="b-id")).has_new
    # Bob's own uploads never make a dot for him.
    await asyncio.sleep(0.01)
    await _add(env, trip.id, "b-id", "own-2")
    assert await _dots(env, "b-id") == {"Trip": False, "Old": False}


async def test_the_new_dot_shows_on_space_albums_too(env):
    album = await env.create_album(space_id="sp-1", owner_user_id="a-id", name="Oma")
    assert await _dots(env, "b-id", "sp-1") == {"Oma": False}
    await asyncio.sleep(0.01)
    await _add(env, album.id, "a-id", "sp-new")
    assert await _dots(env, "b-id", "sp-1") == {"Oma": True}
    await env.mark_album_seen(album.id, actor_user_id="b-id")
    assert await _dots(env, "b-id", "sp-1") == {"Oma": False}


async def test_marking_an_album_seen_needs_a_seat(env):
    album = await env.create_album(space_id="sp-1", owner_user_id="a-id", name="Oma")
    with pytest.raises(GalleryNotFoundError):
        await env.mark_album_seen(album.id, actor_user_id="e-id")
    with pytest.raises(GalleryNotFoundError):
        await env.mark_album_seen("nope", actor_user_id="b-id")


# ─── Managing (PR 5): edit, move, bulk, album restore, empty trash ────────


async def test_caption_and_day_taken_are_edited_by_the_uploader(env):
    album = await _household_album(env, owner="a-id")
    up = await _upload(env, album.id, _jpeg(), who="b-id")
    seen = _published(env, GalleryItemUpdated)

    item = await env.update_item(
        up.item.id, actor_user_id="b-id", caption="  Lake  ", taken_at="2026-07-12"
    )

    assert item.caption == "Lake"
    assert item.taken_at == "2026-07-12"
    assert [(e.item_id, e.from_album_id) for e in seen] == [(up.item.id, None)]
    cleared = await env.update_item(
        up.item.id, actor_user_id="b-id", caption="", taken_at=None
    )
    assert cleared.caption is None
    assert cleared.taken_at is None


async def test_only_who_may_delete_a_photo_may_edit_it(env):
    album = await _household_album(env, owner="a-id")
    up = await _upload(env, album.id, _jpeg(), who="a-id")
    with pytest.raises(GalleryPermissionError):
        await env.update_item(up.item.id, actor_user_id="e-id", caption="mine now")
    assert (
        await env.update_item(up.item.id, actor_user_id="a-id", caption="ok")
    ).caption == "ok"


@pytest.mark.parametrize(
    "bad", ["12.07.2026", "2026-13-01", "1700-01-01", "2999-01-01", "2026-7-1"]
)
async def test_a_day_taken_must_be_a_real_day(env, bad):
    album = await _household_album(env)
    up = await _upload(env, album.id, _jpeg())
    with pytest.raises(ValueError):
        await env.update_item(up.item.id, actor_user_id="a-id", taken_at=bad)


async def test_a_long_caption_is_refused(env):
    album = await _household_album(env)
    up = await _upload(env, album.id, _jpeg())
    with pytest.raises(ValueError):
        await env.update_item(up.item.id, actor_user_id="a-id", caption="x" * 301)


async def test_a_photo_moves_to_another_album_and_both_counts_follow(env):
    src = await _household_album(env, name="Summer")
    dst = await _household_album(env, owner="b-id", name="Lake")
    up = await _upload(env, src.id, _jpeg())
    await env.update_album(src.id, actor_user_id="a-id", cover_item_id=up.item.id)
    seen = _published(env, GalleryItemUpdated)

    moved = await env.update_item(up.item.id, actor_user_id="a-id", album_id=dst.id)

    assert moved.album_id == dst.id
    src_now = await env._repo.get_album(src.id)  # type: ignore[attr-defined]
    assert src_now.item_count == 0
    assert src_now.cover_item_id is None  # it no longer lives there
    assert (await env._repo.get_album(dst.id)).item_count == 1  # type: ignore[attr-defined]
    assert [(e.album_id, e.from_album_id) for e in seen] == [(dst.id, src.id)]


async def test_nothing_moves_into_the_posts_album_or_a_space(env):
    album = await _household_album(env)
    up = await _upload(env, album.id, _jpeg())
    system = await env.ensure_system_album(None)
    space_album = await env.create_album(
        space_id="sp-1", owner_user_id="a-id", name="Trip"
    )
    with pytest.raises(GalleryPermissionError):
        await env.update_item(up.item.id, actor_user_id="a-id", album_id=system.id)
    with pytest.raises(GalleryNotFoundError):
        await env.update_item(up.item.id, actor_user_id="a-id", album_id=space_album.id)


async def test_a_space_photo_is_not_edited_here(env):
    album = await env.create_album(space_id="sp-1", owner_user_id="a-id", name="Trip")
    up = await _upload(env, album.id, _jpeg())
    with pytest.raises(GalleryPermissionError):
        await env.update_item(up.item.id, actor_user_id="a-id", caption="x")


async def test_a_bulk_delete_does_what_it_may_and_names_the_rest(env):
    album = await _household_album(env, owner="b-id", name="Bob's")
    mine = await _upload(env, album.id, _jpeg((1, 2, 3)), who="e-id")
    bobs = await _upload(env, album.id, _jpeg((4, 5, 6)), who="b-id")

    done, refused = await env.apply_to_items(
        "delete", [mine.item.id, bobs.item.id, mine.item.id], actor_user_id="e-id"
    )

    assert done == [mine.item.id]
    assert refused == [bobs.item.id]
    trash = await env.list_trash(actor_user_id="e-id")
    assert [t.item.id for t in trash] == [mine.item.id]

    back, _ = await env.apply_to_items("restore", [mine.item.id], actor_user_id="e-id")
    assert back == [mine.item.id]
    assert await env.list_trash(actor_user_id="e-id") == []


async def test_a_bulk_move_and_its_bounds(env):
    src = await _household_album(env, name="A")
    dst = await _household_album(env, name="B")
    up = await _upload(env, src.id, _jpeg())
    done, refused = await env.apply_to_items(
        "move", [up.item.id, "nope"], actor_user_id="a-id", album_id=dst.id
    )
    assert (done, refused) == ([up.item.id], ["nope"])
    with pytest.raises(ValueError):
        await env.apply_to_items("move", [up.item.id], actor_user_id="a-id")
    with pytest.raises(ValueError):
        await env.apply_to_items("explode", [up.item.id], actor_user_id="a-id")
    with pytest.raises(ValueError):
        await env.apply_to_items("delete", [], actor_user_id="a-id")
    with pytest.raises(ValueError):
        await env.apply_to_items(
            "delete", [f"i{n}" for n in range(501)], actor_user_id="a-id"
        )


async def test_an_album_comes_back_with_the_photos_deleted_with_it(env):
    album = await _household_album(env, owner="b-id", name="Bob's")
    earlier = await _upload(env, album.id, _jpeg((1, 1, 1)), who="b-id")
    kept = await _upload(env, album.id, _jpeg((2, 2, 2)), who="b-id")
    await env.delete_item(earlier.item.id, actor_user_id="b-id")
    await asyncio.sleep(1.1)  # a later trash stamp than the album's
    await env.delete_album(album.id, actor_user_id="b-id")
    with pytest.raises(GalleryPermissionError):
        await env.restore_album(album.id, actor_user_id="e-id")

    assert await env.restore_album(album.id, actor_user_id="b-id") == 1

    back = await env.get_album(album.id, actor_user_id="b-id")
    assert back.item_count == 1
    trash = await env.list_trash(actor_user_id="b-id")
    assert [t.item.id for t in trash] == [earlier.item.id]  # deleted on its own
    assert kept.item.id not in {t.item.id for t in trash}
    with pytest.raises(GalleryNotFoundError):
        await env.restore_album(album.id, actor_user_id="b-id")


async def test_only_an_admin_empties_the_trash(env):
    album = await _household_album(env)
    up = await _upload(env, album.id, _jpeg())
    await env.delete_item(up.item.id, actor_user_id="a-id")
    with pytest.raises(GalleryPermissionError):
        await env.empty_trash(actor_user_id="b-id")
    assert await env.empty_trash(actor_user_id="a-id") == 1
    assert await env.list_trash(actor_user_id="a-id") == []
    assert not _stored_files(env)


# ─── Downloads: the stored WebP / WebM, human names ──────────────────────


async def test_a_download_hands_out_the_stored_webp_with_a_human_name(env):
    album = await _household_album(env, name="Sommer 2026")
    one = await _upload(env, album.id, _jpeg((1, 1, 1)))
    two = await _upload(env, album.id, _jpeg((2, 2, 2)))
    await env.update_item(one.item.id, actor_user_id="a-id", taken_at="2026-07-12")
    await env.update_item(two.item.id, actor_user_id="a-id", taken_at="2026-07-12")

    files = await env.download_files(
        [one.item.id, two.item.id, "unknown"], actor_user_id="b-id"
    )

    assert [f.name for f in files] == [
        "Sommer 2026 – 2026-07-12 – 1.webp",
        "Sommer 2026 – 2026-07-12 – 2.webp",
    ]
    for f in files:
        data = pathlib.Path(f.path).read_bytes()
        assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"  # the stored copy
        assert f.size == len(data)
        with Image.open(io.BytesIO(data)) as img:
            assert max(img.size) <= IMAGE_MAX_DIMENSION


async def test_a_space_download_needs_a_seat(env):
    album = await env.create_album(space_id="sp-1", owner_user_id="a-id", name="Trip")
    up = await _upload(env, album.id, _jpeg())
    assert await env.download_files([up.item.id], actor_user_id="e-id") == []
    assert len(await env.download_files([up.item.id], actor_user_id="b-id")) == 1


async def test_the_posts_album_downloads_under_the_readers_word(env):
    await _store_photo(env, "pic.webp")
    await env.mirror_post(_photo_post("p1", "a-id", "pic.webp"), None)
    system = await env.ensure_system_album(None)
    (item,) = await env.list_items(system.id, actor_user_id="a-id")
    (f,) = await env.download_files(
        [item.id], actor_user_id="a-id", system_album_label="Aus dem Feed"
    )
    assert f.name.startswith("Aus dem Feed – ")
    assert f.name.endswith(".webp")


async def test_too_many_photos_for_one_download(env, monkeypatch):
    with pytest.raises(DownloadTooLargeError):
        await env.download_files(
            [f"i{n}" for n in range(DOWNLOAD_MAX_ITEMS + 1)], actor_user_id="a-id"
        )
    with pytest.raises(ValueError):
        await env.download_files([], actor_user_id="a-id")
    album = await _household_album(env)
    up = await _upload(env, album.id, _jpeg())
    monkeypatch.setattr("socialhome.services.gallery_service.DOWNLOAD_MAX_BYTES", 10)
    with pytest.raises(DownloadTooLargeError):
        await env.download_files([up.item.id], actor_user_id="a-id")


async def test_someones_photos_for_the_leaving_household_zip(env):
    album = await _household_album(env, owner="b-id", name="Bob's")
    await _upload(env, album.id, _jpeg((1, 1, 1)), who="b-id")
    await _upload(env, album.id, _jpeg((2, 2, 2)), who="e-id")
    other = await _household_album(env, owner="b-id", name="Lake")
    await _upload(env, other.id, _jpeg((3, 3, 3)), who="b-id")

    name, items, _ = await env.person_uploads("bob", actor_user_id="a-id")
    assert name == "Bob"
    assert len(items) == 2  # his own, not Eve's

    with pytest.raises(GalleryPermissionError):
        await env.person_uploads("bob", actor_user_id="e-id")
    with pytest.raises(GalleryNotFoundError):
        await env.person_uploads("nobody", actor_user_id="a-id")
    _name, files = await env.person_download_files("bob", actor_user_id="b-id")
    assert sorted(f.name for f in files) == sorted(
        n for n in (f.name for f in files) if n.startswith(("Bob's – ", "Lake – "))
    )
    assert len(files) == 2


# ─── Private albums (D7) and children (D6) ────────────────────────────────


async def _private(env, owner="a-id", people=("b-id",), name="Gifts"):
    return await env.create_album(
        space_id=None,
        owner_user_id=owner,
        name=name,
        audience="chosen",
        user_ids=frozenset(people),
    )


async def test_a_private_album_is_listed_only_to_its_people(env):
    gifts = await _private(env, owner="b-id", people=())
    for who, seen in (("b-id", True), ("e-id", False)):
        ids = {a.id for a in await env.list_albums(space_id=None, actor_user_id=who)}
        assert (gifts.id in ids) is seen
    # alice is an admin: one locked tile, nothing to read on it.
    (tile,) = [
        a
        for a in await env.list_albums(space_id=None, actor_user_id="a-id")
        if a.id == gifts.id
    ]
    assert tile.locked and tile.name == "" and tile.cover_url is None


async def test_a_private_album_starts_with_valid_people_only(env):
    with pytest.raises(AlbumAudienceError) as unknown:
        await _private(env, people=("nobody",))
    assert unknown.value.params["reason"] == "unknown_person"
    with pytest.raises(AlbumAudienceError) as many:
        await _private(env, people=tuple(f"u{i}" for i in range(60)))
    assert many.value.params["reason"] == "too_many"
    with pytest.raises(ValueError):
        await env.create_album(
            space_id="sp-1", owner_user_id="a-id", name="S", audience="chosen"
        )
    with pytest.raises(ValueError):
        await env.create_album(
            space_id=None, owner_user_id="a-id", name="S", audience="nope"
        )


async def test_a_guardian_blocked_person_is_never_chosen(env):
    db = env._repo._db  # type: ignore[attr-defined]
    await db.enqueue("UPDATE users SET child_protection_enabled=1 WHERE user_id='b-id'")
    await db.enqueue(
        "INSERT INTO cp_minor_blocks(minor_user_id, blocked_user_id, blocked_by)"
        " VALUES('b-id', 'e-id', 'a-id')"
    )
    with pytest.raises(AlbumAudienceError) as blocked:
        await _private(env, owner="b-id", people=("e-id",))
    assert blocked.value.params == {"reason": "blocked", "user_id": "e-id"}


async def test_the_default_album_stays_everyones(env):
    default, _ = await env.ensure_default_album(actor_user_id="a-id", name="Photos")
    with pytest.raises(AlbumAudienceError) as err:
        await env.set_album_audience(
            default.id, actor_user_id="a-id", audience="chosen"
        )
    assert err.value.params["reason"] == "default_album"


async def test_only_the_owner_sets_the_audience(env):
    gifts = await _private(env, people=("b-id",))
    with pytest.raises(GalleryPermissionError):
        await env.set_album_audience(
            gifts.id, actor_user_id="b-id", audience="everyone"
        )
    with pytest.raises(ValueError):
        await env.set_album_audience(gifts.id, actor_user_id="a-id", audience="x")
    space = await env.create_album(space_id="sp-1", owner_user_id="a-id", name="S")
    with pytest.raises(GalleryPermissionError):
        await env.set_album_audience(space.id, actor_user_id="a-id", audience="chosen")
    with pytest.raises(GalleryNotFoundError):
        await env.get_album_audience(space.id, actor_user_id="a-id")


async def test_an_empty_album_turns_private_and_back(env):
    album = await _household_album(env)
    got = await env.set_album_audience(
        album.id, actor_user_id="a-id", audience="chosen", user_ids=frozenset({"b-id"})
    )
    assert got.viewers == {"a-id", "b-id"}
    await _upload(env, album.id, _jpeg((1, 50, 1)))  # alice's own photo
    back = await env.set_album_audience(
        album.id, actor_user_id="a-id", audience="everyone"
    )
    assert back.viewers is None
    # Everyone → everyone with photos in it is a no-op, not a refusal.
    await env.set_album_audience(album.id, actor_user_id="a-id", audience="everyone")


async def test_people_can_be_added_to_a_private_album(env):
    gifts = await _private(env, people=("b-id",))
    await _upload(env, gifts.id, _jpeg((2, 50, 1)), who="b-id")
    got = await env.set_album_audience(
        gifts.id,
        actor_user_id="a-id",
        audience="chosen",
        user_ids=frozenset({"b-id", "e-id"}),
    )
    assert got.user_ids == {"b-id", "e-id"}


async def test_moves_between_private_albums(env):
    """Same people: anyone inside may move. More people: only the uploader.
    Fewer: nobody."""
    small = await _private(env, people=("b-id",), name="Small")
    same = await _private(env, people=("b-id",), name="Same")
    wide = await _private(env, people=("b-id", "e-id"), name="Wide")
    bobs = (await _upload(env, small.id, _jpeg((3, 50, 1)), who="b-id")).item
    await env.update_item(bobs.id, actor_user_id="a-id", album_id=same.id)
    with pytest.raises(AlbumAudienceError) as err:
        await env.update_item(bobs.id, actor_user_id="a-id", album_id=wide.id)
    assert err.value.params["reason"] == "not_uploader"
    moved = await env.update_item(bobs.id, actor_user_id="b-id", album_id=wide.id)
    assert moved.album_id == wide.id
    with pytest.raises(AlbumAudienceError) as back:
        await env.update_item(bobs.id, actor_user_id="b-id", album_id=small.id)
    assert back.value.params["reason"] == "would_hide"
    # Out into everyone's album: the uploader may (it is like sharing).
    everyone = await _household_album(env, name="All")
    out = await env.update_item(bobs.id, actor_user_id="b-id", album_id=everyone.id)
    assert out.album_id == everyone.id


async def test_the_gallery_settings_and_who_may_add(env):
    assert (await env.get_settings()).children_may_upload is True
    with pytest.raises(GalleryPermissionError):
        await env.set_settings(actor_user_id="b-id", children_may_upload=False)
    assert await env.may_add("b-id")
    await env._repo._db.enqueue(  # type: ignore[attr-defined]
        "UPDATE users SET child_protection_enabled=1 WHERE user_id='b-id'"
    )
    off = await env.set_settings(actor_user_id="a-id", children_may_upload=False)
    assert off.children_may_upload is False
    assert not await env.may_add("b-id") and await env.may_add("e-id")
    with pytest.raises(ChildUploadOffError):
        await _household_album(env, owner="b-id")
    # A space album keeps the space's own rules.
    await env.create_album(space_id="sp-1", owner_user_id="b-id", name="S")
    on = await env.set_settings(actor_user_id="a-id")
    assert on.children_may_upload is False


async def test_settings_without_an_instance_config(env):
    env._instance_config = None  # type: ignore[attr-defined]
    assert (await env.get_settings()).children_may_upload is True
    with pytest.raises(RuntimeError):
        await env.set_settings(actor_user_id="a-id", children_may_upload=True)


async def test_an_admin_trashes_a_private_album_and_its_owner_is_told(env):
    seen: list = []
    env._bus.subscribe(GalleryPrivateAlbumTrashed, seen.append)  # type: ignore[attr-defined]
    gifts = await _private(env, owner="b-id", people=())
    await _upload(env, gifts.id, _jpeg((4, 50, 1)), who="b-id")
    # eve is no admin: like an unknown id.
    await env.delete_album(gifts.id, actor_user_id="e-id")
    assert await env.get_album(gifts.id, actor_user_id="b-id")
    await env.delete_album(gifts.id, actor_user_id="a-id")
    (event,) = seen
    assert (event.owner_id, event.admin_user_id, event.count) == ("b-id", "a-id", 1)
    (tile,) = await env.list_locked_trash(actor_user_id="a-id")
    assert tile.album_id == gifts.id and tile.count == 1 and tile.album_in_trash
    assert await env.list_locked_trash(actor_user_id="e-id") == []
    # The owner deleting their own private album tells nobody.
    mine = await _private(env, owner="b-id", people=(), name="Mine")
    await env.delete_album(mine.id, actor_user_id="b-id")
    assert len(seen) == 1


async def test_a_private_file_is_refused_to_outsiders(env):
    gifts = await _private(env, owner="b-id", people=())
    item = (await _upload(env, gifts.id, _jpeg((5, 50, 1)), who="b-id")).item
    name = item.url.rsplit("/", 1)[-1]
    assert not await env.may_open_file(name, user_id="e-id")
    assert not await env.may_open_file(name, user_id="a-id")
    assert await env.may_open_file(name, user_id="b-id")
    assert await env.may_open_file("other.webp", user_id="e-id")
