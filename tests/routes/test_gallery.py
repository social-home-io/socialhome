"""HTTP tests for gallery routes."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from urllib.parse import quote

import aiohttp
import pytest

from socialhome.app_keys import config_key, gallery_repo_key
from socialhome.domain.gallery import GalleryItem
from tests.video_clips import make_clip

from .conftest import _auth


async def _make_space(client, *, sid: str = "sp-1") -> None:
    db = client._db
    # ``feature_gallery`` defaults to 0 in the 0001 schema but real
    # spaces created through SpaceService get 1 (the dataclass default).
    # Set it explicitly so the new route-level gate doesn't 403.
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, feature_gallery)"
        " VALUES(?, 'X', 'inst', 'admin', ?, 1)",
        (sid, "ab" * 32),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'owner')",
        (sid, client._uid),
    )


# ─── Authentication ──────────────────────────────────────────────────────


async def test_list_household_albums_requires_auth(client):
    r = await client.get("/api/gallery/albums")
    assert r.status == 401


async def test_list_space_albums_requires_auth(client):
    r = await client.get("/api/spaces/sp-1/gallery/albums")
    assert r.status == 401


async def test_create_album_requires_auth(client):
    r = await client.post("/api/gallery/albums", json={"name": "X"})
    assert r.status == 401


# ─── Default album (an upload with no album chosen) ──────────────────────


async def test_default_album_requires_auth(client):
    assert (await client.get("/api/gallery/default-album")).status == 401
    r = await client.post("/api/gallery/default-album", json={"name": "Photos"})
    assert r.status == 401


async def test_default_album_is_null_until_made(client):
    r = await client.get("/api/gallery/default-album", headers=_auth(client._tok))
    assert r.status == 200
    assert await r.json() == {"album": None}


async def test_default_album_is_made_once_then_found(client):
    h = _auth(client._tok)
    r = await client.post(
        "/api/gallery/default-album", json={"name": "Fotos"}, headers=h
    )
    assert r.status == 201
    made = await r.json()
    assert made["name"] == "Fotos"
    assert made["space_id"] is None
    assert made["is_system"] is False
    r = await client.post(
        "/api/gallery/default-album", json={"name": "Photos"}, headers=h
    )
    assert r.status == 200
    assert (await r.json())["id"] == made["id"]
    r = await client.get("/api/gallery/default-album", headers=h)
    assert (await r.json())["album"]["id"] == made["id"]
    # An ordinary album: it shows in the list.
    r = await client.get("/api/gallery/albums", headers=h)
    assert made["id"] in {a["id"] for a in await r.json()}


async def test_default_album_needs_a_name(client):
    r = await client.post(
        "/api/gallery/default-album", json={"name": ""}, headers=_auth(client._tok)
    )
    assert r.status == 422


# ─── Household album CRUD ───────────────────────────────────────────────


async def test_household_albums_initially_empty(client):
    r = await client.get("/api/gallery/albums", headers=_auth(client._tok))
    assert r.status == 200
    assert (await r.json()) == []


async def test_create_household_album(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Personal photos"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["name"] == "Personal photos"
    assert body["space_id"] is None


async def test_create_album_empty_name_422(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": ""},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_create_album_bad_json_400(client):
    r = await client.post(
        "/api/gallery/albums",
        data="bad",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


# ─── Space album CRUD ───────────────────────────────────────────────────


async def test_create_space_album(client):
    await _make_space(client)
    r = await client.post(
        "/api/spaces/sp-1/gallery/albums",
        json={"name": "Trip 2026", "description": "A trip"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["space_id"] == "sp-1"


async def test_list_space_albums_non_member_403(client):
    db = client._db
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-other', 'X', 'inst', 'someone', ?)",
        ("ab" * 32,),
    )
    r = await client.get(
        "/api/spaces/sp-other/gallery/albums",
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ─── Get / update / delete album ────────────────────────────────────────


async def test_get_album_unknown_404(client):
    r = await client.get(
        "/api/gallery/albums/missing",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_get_album_existing(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "X"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.get(
        f"/api/gallery/albums/{aid}",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["id"] == aid


async def test_update_album_renames(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Original"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.patch(
        f"/api/gallery/albums/{aid}",
        json={"name": "Renamed"},
        headers=_auth(client._tok),
    )
    assert r.status == 204
    r = await client.get(
        f"/api/gallery/albums/{aid}",
        headers=_auth(client._tok),
    )
    assert (await r.json())["name"] == "Renamed"


async def test_update_album_bad_json_400(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "X"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.patch(
        f"/api/gallery/albums/{aid}",
        data="bad",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


async def test_delete_album_returns_204(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Bye"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.delete(
        f"/api/gallery/albums/{aid}",
        headers=_auth(client._tok),
    )
    assert r.status == 204


async def test_set_retention_exempt(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Keep me"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.post(
        f"/api/gallery/albums/{aid}/retention",
        json={"retention_exempt": True},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["retention_exempt"] is True


# ─── Items ──────────────────────────────────────────────────────────────


async def test_list_items_unknown_album_404(client):
    r = await client.get(
        "/api/gallery/albums/missing/items",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_list_items_empty_album_returns_array(client):
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Empty"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    r = await client.get(
        f"/api/gallery/albums/{aid}/items",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json()) == []


async def test_upload_video_item_returns_processing(client):
    """POST a video to an album — the response is 201 with the new item
    serialised and ``media_status='processing'`` (async transcode), and
    the gallery scheduler is stopped first so it can't race."""
    from socialhome.app_keys import media_transcode_service_key

    # Stop the background loop so the fake mp4 bytes aren't transcoded
    # (and the row rescheduled) before we assert the item's shape.
    await client.app[media_transcode_service_key].stop()

    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Clips"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]

    boundary = "----gallery-boundary"
    body = make_clip(2)
    parts = [
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="file"; filename="clip.mp4"',
        b"Content-Type: video/mp4",
        b"",
        body,
        f"--{boundary}--".encode(),
        b"",
    ]
    payload = b"\r\n".join(parts)
    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=payload,
        headers={
            **_auth(client._tok),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    assert r.status == 201
    item = await r.json()
    assert item["item_type"] == "video"
    # ``url`` is signed (``...webm?exp=&sig=``); assert on the path part.
    assert ".webm" in item["url"].split("?", 1)[0]
    assert item["url"].split("?", 1)[0].endswith(".webm")
    assert item["media_status"] == "processing"


async def test_delete_item_unknown_204(client):
    r = await client.delete(
        "/api/gallery/items/missing",
        headers=_auth(client._tok),
    )
    assert r.status == 204


async def _upload_video(client) -> tuple[str, str]:
    """Upload a fake mp4 to a fresh album. Returns ``(album_id, output_fn)``.

    Stops the transcode scheduler first so the enqueued row stays in
    ``pending`` (it won't be transcoded + the row deleted) while the
    test inspects readiness.
    """
    from socialhome.app_keys import media_transcode_service_key

    await client.app[media_transcode_service_key].stop()
    r = await client.post(
        "/api/gallery/albums",
        json={"name": "Clips"},
        headers=_auth(client._tok),
    )
    aid = (await r.json())["id"]
    boundary = "----gallery-boundary"
    body = make_clip(2)
    parts = [
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="file"; filename="clip.mp4"',
        b"Content-Type: video/mp4",
        b"",
        body,
        f"--{boundary}--".encode(),
        b"",
    ]
    payload = b"\r\n".join(parts)
    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=payload,
        headers={
            **_auth(client._tok),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    assert r.status == 201
    item = await r.json()
    output_fn = item["url"].split("?", 1)[0].rsplit("/", 1)[-1]
    return aid, output_fn


async def _list_items(client, album_id: str) -> list[dict]:
    r = await client.get(
        f"/api/gallery/albums/{album_id}/items",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    return await r.json()


async def test_gallery_list_video_media_status_processing(client):
    from socialhome.app_keys import media_transcode_repo_key

    aid, fn = await _upload_video(client)
    # The upload enqueued a 'pending' transcode row → list reports it.
    await client.app[media_transcode_repo_key].mark_processing(fn)
    items = await _list_items(client, aid)
    vids = [i for i in items if i["item_type"] == "video"]
    assert len(vids) == 1
    assert vids[0]["media_status"] == "processing"


async def test_gallery_list_video_media_status_ready_after_complete(client):
    from socialhome.app_keys import media_transcode_repo_key

    aid, fn = await _upload_video(client)
    # Completing the transcode deletes the row → absent → 'ready'.
    await client.app[media_transcode_repo_key].complete(fn)
    items = await _list_items(client, aid)
    vids = [i for i in items if i["item_type"] == "video"]
    assert vids[0]["media_status"] == "ready"


async def test_gallery_list_video_media_status_failed(client):
    from socialhome.app_keys import media_transcode_repo_key

    aid, fn = await _upload_video(client)
    await client.app[media_transcode_repo_key].mark_failed(fn, "boom")
    items = await _list_items(client, aid)
    vids = [i for i in items if i["item_type"] == "video"]
    assert vids[0]["media_status"] == "failed"


async def test_gallery_list_photo_has_no_processing_status(client):
    import io

    from PIL import Image

    aid, _fn = await _upload_video(client)
    # Add a real photo (valid PNG bytes) to the same album.
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (50, 100, 200)).save(buf, "PNG")
    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=buf.getvalue(),
        headers={**_auth(client._tok), "Content-Type": "image/png"},
    )
    assert r.status == 201
    items = await _list_items(client, aid)
    photos = [i for i in items if i["item_type"] != "video"]
    assert photos
    for p in photos:
        assert p.get("media_status") != "processing"


# ─── Household videos: 5 minutes, streamed to disk, "ready in" ────────────


async def _video_album(client) -> str:
    from socialhome.app_keys import media_transcode_service_key

    # Keep the queue still so the assertions see it as uploaded.
    await client.app[media_transcode_service_key].stop()
    r = await client.post(
        "/api/gallery/albums", json={"name": "Clips"}, headers=_auth(client._tok)
    )
    return (await r.json())["id"]


async def _post_video(client, aid: str, clip: bytes, *, raw: bool = False):
    if raw:
        return await client.post(
            f"/api/gallery/albums/{aid}/items",
            data=clip,
            headers={**_auth(client._tok), "Content-Type": "video/mp4"},
        )
    form = aiohttp.FormData()
    form.add_field("file", clip, filename="clip.mp4", content_type="video/mp4")
    return await client.post(
        f"/api/gallery/albums/{aid}/items", data=form, headers=_auth(client._tok)
    )


async def test_upload_video_over_five_minutes_is_422_with_its_length(client):
    aid = await _video_album(client)
    r = await _post_video(client, aid, make_clip(432))
    assert r.status == 422
    err = (await r.json())["error"]
    assert err["code"] == "VIDEO_TOO_LONG"
    assert err["params"] == {"duration_s": 432, "max_s": 300}


async def test_upload_video_of_five_minutes_is_201_with_its_real_length(client):
    aid = await _video_album(client)
    r = await _post_video(client, aid, make_clip(300, size=(48, 64)))
    assert r.status == 201, await r.text()
    item = await r.json()
    assert item["duration_s"] == pytest.approx(300.0, abs=0.5)
    assert (item["width"], item["height"]) == (48, 64)
    assert item["media_status"] == "processing"
    # Queued: a rough "ready in" for the processing tile.
    assert item["media_eta_s"] >= 1


async def test_raw_video_body_streams_to_disk(client):
    aid = await _video_album(client)
    r = await _post_video(client, aid, make_clip(2), raw=True)
    assert r.status == 201, await r.text()
    assert (await r.json())["item_type"] == "video"


async def test_unreadable_video_is_422_video_unreadable(client):
    aid = await _video_album(client)
    r = await _post_video(client, aid, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200)
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "VIDEO_UNREADABLE"


async def test_video_over_the_household_cap_is_413(client, monkeypatch):
    monkeypatch.setattr(
        "socialhome.services.gallery_service.GALLERY_VIDEO_MAX_UPLOAD_BYTES",
        2 * 1024 * 1024,
    )
    aid = await _video_album(client)

    async def gen():
        # Chunked (no Content-Length): only the streaming cap can refuse it.
        for _ in range(3):
            yield b"\x00" * (1024 * 1024)

    r = await client.post(
        f"/api/gallery/albums/{aid}/items",
        data=gen(),
        headers={**_auth(client._tok), "Content-Type": "video/mp4"},
    )
    assert r.status == 413
    err = (await r.json())["error"]
    assert err["code"] == "PAYLOAD_TOO_LARGE"
    assert err["params"] == {"max_mb": 2}
    media = pathlib.Path(client.app[config_key].media_path) / "transcode_src"
    assert not media.exists() or list(media.iterdir()) == []


async def test_list_shows_ready_in_for_a_processing_video(client):
    aid, _fn = await _upload_video(client)
    items = await _list_items(client, aid)
    vid = next(i for i in items if i["item_type"] == "video")
    assert vid["media_status"] == "processing"
    assert isinstance(vid["media_eta_s"], int) and vid["media_eta_s"] >= 1
# ─── In-album date order (owner decision D1) ─────────────────────────────


async def _seed(client, album_id: str, rows) -> None:
    """Items straight into the repo: ``(id, taken_at, created_at)``."""

    repo = client.app[gallery_repo_key]
    for item_id, taken, created in rows:
        await repo.create_item(
            GalleryItem(
                id=item_id,
                album_id=album_id,
                uploaded_by=client._uid,
                item_type="photo",
                url=f"api/media/{item_id}.webp",
                thumbnail_url=f"api/media/{item_id}-t.webp",
                width=10,
                height=10,
                taken_at=taken,
                created_at=created,
            )
        )


async def _new_album(client, name: str = "Summer") -> str:
    r = await client.post(
        "/api/gallery/albums", json={"name": name}, headers=_auth(client._tok)
    )
    assert r.status == 201
    return (await r.json())["id"]


_DATED = [
    ("a", "2024-07-12", "2026-10-01T10:00:00+00:00"),
    ("b", "2024-07-12", "2026-10-01T10:00:00+00:00"),
    ("c", "2025-01-02", "2026-09-01T10:00:00+00:00"),
    ("d", None, "2026-08-15T10:00:00+00:00"),  # no date taken: upload day
    ("e", "2019-03-03", "2026-10-02T10:00:00+00:00"),
]


async def test_items_by_date_taken_page_without_skip_or_repeat(client):
    h = _auth(client._tok)
    aid = await _new_album(client)
    await _seed(client, aid, _DATED)
    for sort, want in (
        ("taken", ["d", "c", "b", "a", "e"]),
        ("taken_asc", ["e", "a", "b", "c", "d"]),
    ):
        seen: list[str] = []
        cursor = ""
        while True:
            r = await client.get(
                f"/api/gallery/albums/{aid}/items?sort={sort}&limit=2{cursor}",
                headers=h,
            )
            assert r.status == 200
            page = await r.json()
            seen += [i["id"] for i in page]
            if len(page) < 2:
                break
            last = page[-1]
            cursor = (
                f"&after_day={last['day']}&after={quote(last['created_at'])}"
                f"&after_id={last['id']}"
            )
        assert seen == want, sort
    r = await client.get(f"/api/gallery/albums/{aid}/items?sort=taken", headers=h)
    days = {i["id"]: i["day"] for i in await r.json()}
    assert days["d"] == "2026-08-15" and days["a"] == "2024-07-12"


async def test_items_sort_and_cursor_are_checked(client):
    h = _auth(client._tok)
    aid = await _new_album(client)
    r = await client.get(f"/api/gallery/albums/{aid}/items?sort=random", headers=h)
    assert r.status == 422
    r = await client.get(
        f"/api/gallery/albums/{aid}/items?sort=taken&after_day=2024-01-01", headers=h
    )
    assert r.status == 422
    # The upload order ignores a date cursor.
    r = await client.get(
        f"/api/gallery/albums/{aid}/items?after_day=2024-01-01", headers=h
    )
    assert r.status == 200


async def test_albums_list_newest_activity_first(client):
    h = _auth(client._tok)
    older = await _new_album(client, "Older")
    newer = await _new_album(client, "Newer")
    await _seed(client, older, [("x", None, "2099-01-01T00:00:00+00:00")])
    r = await client.get("/api/gallery/albums", headers=h)
    rows = await r.json()
    assert [a["id"] for a in rows if not a["is_system"]][:2] == [older, newer]
    assert rows[0]["activity_at"] == "2099-01-01T00:00:00+00:00"
    assert rows[0]["is_default"] is False and rows[0]["default_name"] is False


async def test_default_album_shows_in_each_viewers_language_until_renamed(client):
    h = _auth(client._tok)
    r = await client.post(
        "/api/gallery/default-album", json={"name": "Fotos"}, headers=h
    )
    made = await r.json()
    assert (made["is_default"], made["default_name"]) == (True, True)
    r = await client.patch(
        f"/api/gallery/albums/{made['id']}", json={"name": "Familie"}, headers=h
    )
    assert r.status == 204
    r = await client.get(f"/api/gallery/albums/{made['id']}", headers=h)
    got = await r.json()
    assert (got["name"], got["is_default"], got["default_name"]) == (
        "Familie",
        True,
        False,
    )


# ─── On this day (owner decision D14) ────────────────────────────────────


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _years_ago(n: int) -> str:
    today = datetime.now(UTC).date()
    try:
        return today.replace(year=today.year - n).isoformat()
    except ValueError:  # 29 February
        return today.replace(year=today.year - n, day=28).isoformat()


async def test_on_this_day_requires_auth(client):
    assert (await client.get("/api/gallery/on-this-day")).status == 401
    r = await client.patch("/api/gallery/on-this-day", json={"enabled": False})
    assert r.status == 401


async def test_on_this_day_without_a_memory(client):
    r = await client.get("/api/gallery/on-this-day", headers=_auth(client._tok))
    assert r.status == 200
    assert await r.json() == {
        "card": None,
        "settings": {
            "enabled": True,
            "hidden_today": False,
            "excluded_album_ids": [],
        },
    }


async def test_on_this_day_card_hide_and_turn_off(client):
    h = _auth(client._tok)
    aid = await _new_album(client, "Autumn break")
    day = _years_ago(2)
    await _seed(
        client,
        aid,
        [(f"m-{n}", day, "2026-01-01T00:00:00+00:00") for n in range(4)],
    )
    r = await client.get(f"/api/gallery/on-this-day?date={_today()}", headers=h)
    card = (await r.json())["card"]
    assert card["album"]["id"] == aid
    assert card["album"]["name"] == "Autumn break"
    assert (card["date"], card["years_ago"], card["count"]) == (day, 2, 4)
    assert [i["id"] for i in card["items"]] == ["m-0", "m-1", "m-2"]
    assert card["items"][0]["thumbnail_url"].startswith("api/media/m-0-t.webp")

    # ✕ — hidden for today.
    r = await client.patch(
        "/api/gallery/on-this-day",
        json={"hide_today": True, "date": _today()},
        headers=h,
    )
    assert r.status == 200
    assert (await r.json())["settings"]["hidden_today"] is True
    r = await client.get(f"/api/gallery/on-this-day?date={_today()}", headers=h)
    body = await r.json()
    assert body["card"] is None and body["settings"]["hidden_today"] is True
    r = await client.patch(
        "/api/gallery/on-this-day", json={"hide_today": False}, headers=h
    )
    assert (await r.json())["settings"]["hidden_today"] is False

    # Don't show this album again — and back.
    r = await client.patch(
        "/api/gallery/on-this-day", json={"exclude_album_id": aid}, headers=h
    )
    assert (await r.json())["settings"]["excluded_album_ids"] == [aid]
    r = await client.get("/api/gallery/on-this-day", headers=h)
    assert (await r.json())["card"] is None
    await client.patch(
        "/api/gallery/on-this-day", json={"include_all": True}, headers=h
    )

    # Turned off.
    r = await client.patch(
        "/api/gallery/on-this-day", json={"enabled": False}, headers=h
    )
    assert (await r.json())["settings"]["enabled"] is False
    r = await client.get("/api/gallery/on-this-day", headers=h)
    body = await r.json()
    assert body["card"] is None and body["settings"]["enabled"] is False


async def test_on_this_day_checks_its_input(client):
    h = _auth(client._tok)
    for query in ("date=yesterday", "date=1999-01-01"):
        r = await client.get(f"/api/gallery/on-this-day?{query}", headers=h)
        assert r.status == 422, query
    for body in (
        {"enabled": "no"},
        {"hide_today": 1},
        {"date": 20261101},
        {"exclude_album_id": 5},
        {"include_album_id": ""},
    ):
        r = await client.patch("/api/gallery/on-this-day", json=body, headers=h)
        assert r.status == 422, body
    r = await client.patch(
        "/api/gallery/on-this-day", json={"exclude_album_id": "nope"}, headers=h
    )
    assert r.status == 404


async def test_on_this_day_follows_the_gallery_switch(client):
    h = _auth(client._tok)
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": {"feat_gallery": False}},
        headers=h,
    )
    assert r.status == 200
    assert (await client.get("/api/gallery/on-this-day", headers=h)).status == 403
    r = await client.patch("/api/gallery/on-this-day", json={}, headers=h)
    assert r.status == 403


async def test_a_space_album_lists_by_date_taken_too(client):
    """The date order and its cursor are the same for a space album; its
    rows are never the household's default album."""
    h = _auth(client._tok)
    await _make_space(client)
    r = await client.post(
        "/api/spaces/sp-1/gallery/albums", json={"name": "Trip"}, headers=h
    )
    album = await r.json()
    assert (album["is_default"], album["default_name"]) == (False, False)
    await _seed(client, album["id"], _DATED)
    seen: list[str] = []
    cursor = ""
    while True:
        r = await client.get(
            f"/api/gallery/albums/{album['id']}/items?sort=taken_asc&limit=2{cursor}",
            headers=h,
        )
        assert r.status == 200
        page = await r.json()
        seen += [i["id"] for i in page]
        if len(page) < 2:
            break
        last = page[-1]
        cursor = (
            f"&after_day={last['day']}&after={quote(last['created_at'])}"
            f"&after_id={last['id']}"
        )
    assert seen == ["e", "a", "b", "c", "d"]
    r = await client.get("/api/spaces/sp-1/gallery/albums", headers=h)
    assert (await r.json())[0]["activity_at"] == "2026-10-02T10:00:00+00:00"


async def test_on_this_day_never_uses_a_space_album(client):
    h = _auth(client._tok)
    await _make_space(client)
    r = await client.post(
        "/api/spaces/sp-1/gallery/albums", json={"name": "Trip"}, headers=h
    )
    day = _years_ago(1)
    await _seed(
        client, (await r.json())["id"], [("s-1", day, "2026-01-01T00:00:00+00:00")]
    )
    r = await client.get(f"/api/gallery/on-this-day?date={_today()}", headers=h)
    assert (await r.json())["card"] is None
