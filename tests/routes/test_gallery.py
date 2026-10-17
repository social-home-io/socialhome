"""HTTP tests for gallery routes."""

from __future__ import annotations

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
    body = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200
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
    body = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200
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
