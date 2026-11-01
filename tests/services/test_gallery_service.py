"""Tests for GalleryService."""

from __future__ import annotations

import io
import pathlib

import pytest
from PIL import Image

from socialhome.config import Config
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import GalleryAlbumDeleted, GalleryAlbumUpdated
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
from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo
from socialhome.repositories.media_transcode_repo import SqliteMediaTranscodeRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.gallery_service import (
    DESCRIPTION_MAX,
    NAME_MAX,
    GalleryNotFoundError,
    GalleryPermissionError,
    GalleryService,
    _extract_exif_date_sync,
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
        item = await env.upload_item(
            album_id,
            data=buf.getvalue(),
            content_type="image/jpeg",
            caption=None,
            uploader_user_id=uploader,
        )
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


async def test_delete_album_removes_its_items_files_from_disk(env):
    """The cascade takes the item rows; the files go with them unless
    another row still names them."""
    import dataclasses
    import io

    from PIL import Image

    album = await env.create_album(space_id=None, owner_user_id="a-id", name="T")
    other = await env.create_album(space_id=None, owner_user_id="a-id", name="O")
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, format="JPEG")
    item = await env.upload_item(
        album.id,
        data=buf.getvalue(),
        content_type="image/jpeg",
        caption=None,
        uploader_user_id="a-id",
    )
    buf2 = io.BytesIO()
    Image.new("RGB", (32, 32), (200, 20, 20)).save(buf2, format="JPEG")
    kept = await env.upload_item(
        album.id,
        data=buf2.getvalue(),
        content_type="image/jpeg",
        caption=None,
        uploader_user_id="a-id",
    )
    await env._repo.create_item(  # type: ignore[attr-defined]
        dataclasses.replace(kept, id="elsewhere", album_id=other.id)
    )
    media_dir = env._media_dir  # type: ignore[attr-defined]
    gone = [media_dir / u.rsplit("/", 1)[-1] for u in (item.url, item.thumbnail_url)]
    stay = [media_dir / u.rsplit("/", 1)[-1] for u in (kept.url, kept.thumbnail_url)]
    assert all(p.exists() for p in gone + stay)

    await env.delete_album(album.id, actor_user_id="a-id")
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
    own = await env.upload_item(
        album.id,
        data=buf.getvalue(),
        content_type="image/jpeg",
        caption=None,
        uploader_user_id="a-id",
    )
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
    item = await env.upload_item(
        album.id,
        data=buf.getvalue(),
        content_type="image/jpeg",
        caption=None,
        uploader_user_id="b-id",
    )
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
    item = await env.upload_item(
        album.id,
        data=data,
        content_type="image/jpeg",
        caption=None,
        uploader_user_id="a-id",
    )
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


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (200, 40, 40)).save(buf, format="JPEG")
    return buf.getvalue()


async def _bobs_household_item(env, *, uploader: str = "b-id"):
    album = await env.create_album(space_id=None, owner_user_id="b-id", name="Bob's")
    item = await env.upload_item(
        album.id,
        data=_jpeg(),
        content_type="image/jpeg",
        caption=None,
        uploader_user_id=uploader,
    )
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


async def test_the_album_owner_deletes_any_photo_in_their_album(env):
    album, item = await _bobs_household_item(env, uploader="e-id")
    await env.delete_item(item.id, actor_user_id="b-id")
    assert await env.list_items(album.id, actor_user_id="b-id") == []


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
    bobs = await env.upload_item(
        album.id,
        data=_jpeg(),
        content_type="image/jpeg",
        caption=None,
        uploader_user_id="b-id",
    )
    own = await env.upload_item(
        album.id,
        data=_jpeg(),
        content_type="image/jpeg",
        caption=None,
        uploader_user_id="e-id",
    )
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
