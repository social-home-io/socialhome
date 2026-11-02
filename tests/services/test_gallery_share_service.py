"""GalleryShareService — a copy of household photos to the feed, a space
or a chat (gallery D8), always in the stored format: WebP ≤ 2560 px,
WebM ≤ 1920 px, a video kept to its first minute or left out."""

from __future__ import annotations

import asyncio
import io
import pathlib

import av
import orjson
import pytest
from PIL import Image

from socialhome import app_keys as K
from socialhome.domain.events import GalleryItemsShared, MediaTranscodeFailed
from socialhome.domain.gallery_share import (
    SHARE_MAX_ITEMS,
    LongVideo,
    ShareSkip,
    ShareTarget,
)
from socialhome.domain.media_constraints import (
    IMAGE_MAX_DIMENSION,
    VIDEO_MAX_DIMENSION,
    VIDEO_WEBM_MAGIC,
)
from socialhome.media.video_processor import VideoProcessor
from socialhome.services.gallery_service import GalleryPermissionError
from tests.routes.conftest import _authed_client
from tests.video_clips import make_clip

try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """The app's test server opens a port."""

except ImportError:  # pragma: no cover - CI path
    pass


class _Env:
    pass


@pytest.fixture
async def env(aiohttp_client, tmp_dir):
    client = await _authed_client(aiohttp_client, tmp_dir, None)
    app = client.app
    e = _Env()
    e.client, e.db, e.admin = client, client._db, client._uid
    e.share = app[K.gallery_share_service_key]
    e.gallery = app[K.gallery_service_key]
    e.repo = app[K.gallery_repo_key]
    e.spaces = app[K.space_service_key]
    e.dms = app[K.dm_service_key]
    e.transcode = app[K.media_transcode_service_key]
    e.bus = app[K.event_bus_key]
    e.media = pathlib.Path(client._cfg.media_path)
    e.shared: list[GalleryItemsShared] = []

    async def _seen(ev: GalleryItemsShared) -> None:
        e.shared.append(ev)

    e.bus.subscribe(GalleryItemsShared, _seen)
    await e.db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin)"
        " VALUES('lena','uid-lena','Lena',0)"
    )
    return e


def _jpeg(color=(200, 40, 40), size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


async def _album(env, name="Summer", owner=None):
    return await env.gallery.create_album(
        space_id=None, owner_user_id=owner or env.admin, name=name
    )


async def _photo(env, album, color=(200, 40, 40), by=None, size=(64, 48)):
    up = await env.gallery.upload_item(
        album.id,
        data=_jpeg(color, size),
        content_type="image/jpeg",
        caption=None,
        uploader_user_id=by or env.admin,
    )
    return up.item


async def _video(env, album, seconds, size=(32, 16)):
    up = await env.gallery.upload_item(
        album.id,
        data=make_clip(seconds, size=size),
        content_type="video/mp4",
        caption=None,
        uploader_user_id=env.admin,
    )
    await _converted(env, up.item.url)
    return up.item


async def _space(env, *, gallery=True):
    space = await env.spaces.create_space(owner_username="admin", name="Oma")
    await env.db.enqueue(
        "UPDATE spaces SET feature_gallery=? WHERE id=?", (int(gallery), space.id)
    )
    return space


async def _converted(env, url: str) -> None:
    """Wait for the app's own transcode worker to write *url*'s WebM."""
    name = url.split("?")[0].rsplit("/", 1)[-1]
    transcodes = env.client.app[K.media_transcode_repo_key]
    for _ in range(600):
        if (env.media / name).is_file() and not await transcodes.status_for([name]):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{name} was never converted")


def _post_urls(rows) -> list[str]:
    out: list[str] = []
    for r in rows:
        if r["media_url"]:
            out.append(r["media_url"])
        out.extend(orjson.loads(r["image_urls_json"] or "[]"))
    return out


def _file(env, url: str) -> pathlib.Path:
    return env.media / url.split("?")[0].rsplit("/", 1)[-1]


def _assert_stored_webp(path: pathlib.Path) -> None:
    with Image.open(path) as img:
        assert img.format == "WEBP"
        assert max(img.size) <= IMAGE_MAX_DIMENSION


def _assert_stored_webm(path: pathlib.Path, *, max_seconds: float) -> None:
    assert path.read_bytes()[:4] == VIDEO_WEBM_MAGIC
    with av.open(str(path)) as c:
        assert c.format.name.startswith("matroska,webm")
        v = c.streams.video[0]
        assert v.codec_context.name == "vp9"
        assert max(v.codec_context.width, v.codec_context.height) <= VIDEO_MAX_DIMENSION
        assert c.duration is not None and c.duration / 1_000_000 <= max_seconds + 1


# ─── The stored format for every target ──────────────────────────────────


@pytest.mark.parametrize("target", list(ShareTarget))
async def test_every_target_gets_webp_within_2560_and_webm_within_1920(env, target):
    album = await _album(env)
    photo = await _photo(env, album, size=(3000, 2000))
    video = await _video(env, album, 2, size=(2560, 1440))
    space = await _space(env)
    space_album = await env.gallery.create_album(
        space_id=space.id, owner_user_id=env.admin, name="Reunion"
    )
    conv = await env.dms.create_dm(creator_username="admin", other_username="lena")
    kw = {
        ShareTarget.FEED: {},
        ShareTarget.SPACE_POST: {"space_id": space.id},
        ShareTarget.SPACE_ALBUM: {"space_id": space.id, "album_id": space_album.id},
        ShareTarget.CHAT: {"conversation_id": conv.id},
    }[target]

    result = await env.share.share(
        [photo.id, video.id], actor_user_id=env.admin, target=target, **kw
    )

    assert result.shared == 2 and not result.skipped
    if target is ShareTarget.FEED:
        rows = await env.db.fetchall(
            "SELECT media_url, image_urls_json FROM feed_posts"
        )
        urls = _post_urls(rows)
    elif target is ShareTarget.SPACE_POST:
        rows = await env.db.fetchall(
            "SELECT media_url, image_urls_json FROM space_posts WHERE space_id=?",
            (space.id,),
        )
        urls = _post_urls(rows)
    elif target is ShareTarget.SPACE_ALBUM:
        items = await env.repo.list_items(space_album.id, limit=10)
        urls = [i.url for i in items] + [i.thumbnail_url for i in items]
    else:
        rows = await env.db.fetchall(
            "SELECT media_url FROM conversation_messages WHERE conversation_id=?",
            (conv.id,),
        )
        urls = [r["media_url"] for r in rows]
    names = {u for u in urls if u.startswith("api/media/")}
    webps = [n for n in names if n.endswith(".webp")]
    webms = [n for n in names if n.endswith(".webm")]
    assert webps and len(webms) == 1
    for n in webps:
        _assert_stored_webp(_file(env, n))
    _assert_stored_webm(_file(env, webms[0]), max_seconds=2)
    # Copies, never the gallery's own files.
    assert not names & {photo.url, video.url, photo.thumbnail_url}


async def test_a_stored_photo_that_is_not_webp_is_reencoded(env):
    """Belt and braces: a stored file that somehow isn't a WebP ≤ 2560
    goes through the ImageProcessor; a WebP is copied byte for byte."""
    album = await _album(env)
    good = await _photo(env, album, (10, 20, 30))
    bad = await _photo(env, album, (90, 80, 70))
    big = io.BytesIO()
    Image.new("RGB", (3200, 1800), (1, 2, 3)).save(big, format="PNG")
    _file(env, bad.url).write_bytes(big.getvalue())

    await env.share.share(
        [good.id, bad.id], actor_user_id=env.admin, target=ShareTarget.FEED
    )

    urls = _post_urls(
        await env.db.fetchall("SELECT media_url, image_urls_json FROM feed_posts")
    )
    copies = [_file(env, u) for u in urls]
    for path in copies:
        _assert_stored_webp(path)
    assert _file(env, good.url).read_bytes() in {p.read_bytes() for p in copies}


# ─── Videos longer than a minute ─────────────────────────────────────────


async def test_a_long_video_is_left_out_or_cut_to_its_first_minute(env):
    album = await _album(env)
    long = await _video(env, album, 70)
    space = await _space(env)
    space_album = await env.gallery.create_album(
        space_id=space.id, owner_user_id=env.admin, name="Clips"
    )
    kw = {"space_id": space.id, "album_id": space_album.id}

    skipped = await env.share.share(
        [long.id], actor_user_id=env.admin, target=ShareTarget.SPACE_ALBUM, **kw
    )
    assert skipped.shared == 0
    assert [(s.item_id, s.reason) for s in skipped.skipped] == [
        (long.id, ShareSkip.TOO_LONG)
    ]

    trimmed = await env.share.share(
        [long.id],
        actor_user_id=env.admin,
        target=ShareTarget.SPACE_ALBUM,
        long_videos=LongVideo.TRIM,
        **kw,
    )
    assert (trimmed.shared, trimmed.trimming) == (1, 1)
    [copy] = await env.repo.list_items(space_album.id, limit=5)
    assert copy.duration_s == 60
    await _converted(env, copy.url)
    _assert_stored_webm(_file(env, copy.url), max_seconds=60)
    probe = await VideoProcessor().probe_file(_file(env, copy.url))
    assert probe.duration_s is not None and probe.duration_s <= 61
    _assert_stored_webp(_file(env, copy.thumbnail_url))


async def test_a_trimmed_chat_video_is_sent_once_its_minute_is_ready(env):
    album = await _album(env)
    long = await _video(env, album, 70)
    conv = await env.dms.create_dm(creator_username="admin", other_username="lena")

    result = await env.share.share(
        [long.id],
        actor_user_id=env.admin,
        target=ShareTarget.CHAT,
        conversation_id=conv.id,
        long_videos=LongVideo.TRIM,
        text="Happy birthday",
    )

    assert (result.shared, result.trimming) == (1, 1)
    msgs = await env.db.fetchall(
        "SELECT type, content FROM conversation_messages WHERE conversation_id=?",
        (conv.id,),
    )
    assert [(m["type"], m["content"]) for m in msgs] == [("text", "Happy birthday")]
    for _ in range(200):
        msgs = await env.db.fetchall(
            "SELECT 1 FROM conversation_messages WHERE conversation_id=?"
            " AND type='video'",
            (conv.id,),
        )
        if msgs:
            break
        await asyncio.sleep(0.05)
    msgs = await env.db.fetchall(
        "SELECT type, media_url FROM conversation_messages WHERE conversation_id=?"
        " AND type='video'",
        (conv.id,),
    )
    [video] = msgs
    _assert_stored_webm(_file(env, video["media_url"]), max_seconds=60)


async def test_a_failed_trim_for_a_chat_sends_nothing(env):
    env.share._pending_chat["x.webm"] = object()
    await env.bus.publish(
        MediaTranscodeFailed(output_filename="x.webm", owner_user_id=None)
    )
    assert env.share._pending_chat == {}


async def test_a_video_still_converting_is_not_shared(env):
    album = await _album(env)
    up = await env.gallery.upload_item(
        album.id,
        data=make_clip(2),
        content_type="video/mp4",
        caption=None,
        uploader_user_id=env.admin,
    )
    result = await env.share.share(
        [up.item.id], actor_user_id=env.admin, target=ShareTarget.FEED
    )
    assert [s.reason for s in result.skipped] == [ShareSkip.PROCESSING]


# ─── Who may share what ──────────────────────────────────────────────────


async def test_unknown_space_and_private_items_are_skipped_with_why(env):
    album = await _album(env)
    mine = await _photo(env, album)
    space = await _space(env)
    space_album = await env.gallery.create_album(
        space_id=space.id, owner_user_id=env.admin, name="Theirs"
    )
    in_space = await _photo(env, space_album, (1, 1, 1))
    lenas_album = await _album(env, "Gifts", owner="uid-lena")
    lenas = await _photo(env, lenas_album, (2, 2, 2), by="uid-lena")
    await env.repo.set_album_audience(lenas_album.id, "chosen", frozenset({env.admin}))

    result = await env.share.share(
        [mine.id, in_space.id, lenas.id, "nope"],
        actor_user_id=env.admin,
        target=ShareTarget.FEED,
    )

    assert result.shared == 1
    assert {(s.item_id, s.reason) for s in result.skipped} == {
        (in_space.id, ShareSkip.NOT_HOUSEHOLD),
        (lenas.id, ShareSkip.PRIVATE),
        ("nope", ShareSkip.NOT_FOUND),
    }
    # The private album's own uploader shares it.
    by_lena = await env.share.share(
        [lenas.id], actor_user_id="uid-lena", target=ShareTarget.FEED
    )
    assert by_lena.shared == 1


async def test_a_private_album_nobody_chose_you_for_answers_like_unknown(env):
    lenas_album = await _album(env, "Diary", owner="uid-lena")
    lenas = await _photo(env, lenas_album, (3, 3, 3), by="uid-lena")
    await env.repo.set_album_audience(lenas_album.id, "chosen", frozenset())
    result = await env.share.share(
        [lenas.id], actor_user_id=env.admin, target=ShareTarget.FEED
    )
    assert [s.reason for s in result.skipped] == [ShareSkip.NOT_FOUND]


async def test_limits_and_unpostable_targets_are_refused_before_any_copy(env):
    album = await _album(env)
    photo = await _photo(env, album)
    before = set(env.media.iterdir())
    with pytest.raises(ValueError):
        await env.share.share(
            [f"id-{i}" for i in range(6)],
            actor_user_id=env.admin,
            target=ShareTarget.FEED,
        )
    with pytest.raises(ValueError):
        await env.share.share(
            [f"id-{i}" for i in range(SHARE_MAX_ITEMS + 1)],
            actor_user_id=env.admin,
            target=ShareTarget.SPACE_ALBUM,
            space_id="x",
        )
    space = await _space(env, gallery=False)
    with pytest.raises(GalleryPermissionError):
        await env.share.share(
            [photo.id],
            actor_user_id=env.admin,
            target=ShareTarget.SPACE_ALBUM,
            space_id=space.id,
            new_album_name="New",
        )
    with pytest.raises(GalleryPermissionError):
        await env.share.share(
            [photo.id],
            actor_user_id="uid-lena",
            target=ShareTarget.SPACE_POST,
            space_id=space.id,
        )
    with pytest.raises(ValueError):
        await env.share.share(
            [photo.id], actor_user_id=env.admin, target=ShareTarget.SPACE_POST
        )
    with pytest.raises(ValueError):
        await env.share.share(
            [photo.id],
            actor_user_id=env.admin,
            target=ShareTarget.FEED,
            text="x" * 2001,
        )
    assert set(env.media.iterdir()) == before


async def test_a_failed_delivery_leaves_no_copy_behind(env, monkeypatch):
    album = await _album(env)
    photo = await _photo(env, album)
    before = set(env.media.iterdir())

    async def _boom(self, **kw):
        raise RuntimeError("feed down")

    monkeypatch.setattr(type(env.share._feed), "create_post", _boom)
    with pytest.raises(RuntimeError):
        await env.share.share(
            [photo.id], actor_user_id=env.admin, target=ShareTarget.FEED
        )
    assert set(env.media.iterdir()) == before


async def test_a_moderated_space_holds_the_post_for_review(env):
    space = await _space(env)
    await env.spaces.add_member(space.id, actor_username="admin", user_id="uid-lena")
    await env.db.enqueue(
        "UPDATE spaces SET posts_access='moderated' WHERE id=?", (space.id,)
    )
    album = await _album(env)
    photo = await _photo(env, album)
    targets = await env.share.targets(actor_user_id="uid-lena")
    [offered] = targets.spaces
    assert (offered.post, offered.post_review) == (True, True)
    result = await env.share.share(
        [photo.id],
        actor_user_id="uid-lena",
        target=ShareTarget.SPACE_POST,
        space_id=space.id,
    )
    assert (result.shared, result.queued) == (1, True)


# ─── Targets ─────────────────────────────────────────────────────────────


async def test_targets_leave_out_read_only_seats_archived_spaces_and_textonly_chats(
    env,
):
    open_space = await _space(env)
    follower = await env.spaces.create_space(owner_username="admin", name="Club")
    await env.spaces.add_member(
        follower.id, actor_username="admin", user_id="uid-lena", role="subscriber"
    )
    await env.spaces.add_member(
        open_space.id, actor_username="admin", user_id="uid-lena"
    )
    gone = await env.spaces.create_space(owner_username="admin", name="Old")
    await env.spaces.add_member(gone.id, actor_username="admin", user_id="uid-lena")
    await env.db.enqueue("UPDATE spaces SET archived=1 WHERE id=?", (gone.id,))
    dm = await env.dms.create_dm(creator_username="lena", other_username="admin")

    targets = await env.share.targets(actor_user_id="uid-lena")

    assert [s.space_id for s in targets.spaces] == [open_space.id]
    assert [(c.conversation_id, c.name) for c in targets.chats] == [(dm.id, "Admin")]
    assert targets.to_dict()["limits"]["max_items"] == SHARE_MAX_ITEMS


async def test_a_gallery_off_postless_space_is_not_a_target(env):
    space = await _space(env, gallery=False)
    await env.db.enqueue(
        "UPDATE spaces SET posts_access='admin_only' WHERE id=?", (space.id,)
    )
    await env.spaces.add_member(space.id, actor_username="admin", user_id="uid-lena")
    assert (await env.share.targets(actor_user_id="uid-lena")).spaces == ()


# ─── The uploader is told ────────────────────────────────────────────────


async def test_one_event_per_uploader_and_never_for_ones_own(env):
    album = await _album(env)
    mine = await _photo(env, album, (9, 9, 9))
    lenas = [await _photo(env, album, (i * 60, 5, 5), by="uid-lena") for i in range(2)]
    space = await _space(env)

    await env.share.share(
        [mine.id, *(p.id for p in lenas)],
        actor_user_id=env.admin,
        target=ShareTarget.SPACE_POST,
        space_id=space.id,
    )

    [ev] = env.shared
    assert (ev.uploader, ev.sharer, ev.photos, ev.videos, ev.target, ev.space_id) == (
        "uid-lena",
        env.admin,
        2,
        0,
        "space",
        space.id,
    )
    assert ev.album_id == album.id


async def test_a_new_space_album_gets_thumbnails_and_only_the_sharers_words(env):
    album = await _album(env)
    mine = await _photo(env, album, (9, 9, 9))
    lenas = await _photo(env, album, (90, 9, 9), by="uid-lena")
    await env.gallery.update_item(mine.id, actor_user_id=env.admin, caption="Our lake")
    await env.gallery.update_item(lenas.id, actor_user_id="uid-lena", caption="Mine!")
    space = await _space(env)

    result = await env.share.share(
        [mine.id, lenas.id],
        actor_user_id=env.admin,
        target=ShareTarget.SPACE_ALBUM,
        space_id=space.id,
        new_album_name="  Lake 2026 ",
    )

    assert result.shared == 2
    [made] = [
        a
        for a in await env.repo.list_albums(space.id, limit=10)
        if a.name == "Lake 2026"
    ]
    copies = await env.repo.list_items(made.id, limit=10)
    assert {c.uploaded_by for c in copies} == {env.admin}
    assert sorted(c.caption or "" for c in copies) == ["", "Our lake"]
    for c in copies:
        assert c.thumbnail_url != c.url
        _assert_stored_webp(_file(env, c.thumbnail_url))
