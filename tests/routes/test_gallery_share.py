"""Integration: sharing household photos out as a copy (gallery D8) over
the real routes — ``GET /api/gallery/share/targets`` and
``POST /api/gallery/share`` to the feed, a space (post / album) and a chat."""

from __future__ import annotations

import io

from PIL import Image

from socialhome import app_keys as K
from socialhome.auth import sha256_token_hash

from .conftest import _auth


def _jpeg(color: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, format="JPEG")
    return buf.getvalue()


async def _add_user(client, username: str) -> tuple[str, str]:
    uid, tok = f"uid-{username}", f"tok-{username}"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (username, uid, username.title()),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"t-{username}", uid, "t", sha256_token_hash(tok)),
    )
    return uid, tok


async def _album(client, name: str = "Summer", tok: str | None = None) -> str:
    r = await client.post(
        "/api/gallery/albums", json={"name": name}, headers=_auth(tok or client._tok)
    )
    assert r.status == 201
    return (await r.json())["id"]


async def _upload(client, album_id: str, color=(200, 40, 40), tok: str | None = None):
    r = await client.post(
        f"/api/gallery/albums/{album_id}/items",
        data=_jpeg(color),
        headers={**_auth(tok or client._tok), "Content-Type": "image/jpeg"},
    )
    assert r.status == 201
    return await r.json()


async def _space(client, name: str = "Grandparents", *, gallery: bool = True) -> str:
    r = await client.post(
        "/api/spaces", json={"name": name}, headers=_auth(client._tok)
    )
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    await client._db.enqueue(
        "UPDATE spaces SET feature_gallery=? WHERE id=?", (int(gallery), sid)
    )
    return sid


async def _share(client, body: dict, tok: str | None = None):
    return await client.post(
        "/api/gallery/share", json=body, headers=_auth(tok or client._tok)
    )


async def test_targets_list_feed_spaces_and_chats_with_limits(client):
    await _add_user(client, "oma")
    sid = await _space(client)
    r = await client.post(
        "/api/conversations/dm", json={"username": "oma"}, headers=_auth(client._tok)
    )
    assert r.status == 201
    conv = (await r.json())["id"]

    r = await client.get("/api/gallery/share/targets", headers=_auth(client._tok))

    assert r.status == 200
    body = await r.json()
    assert body["feed"] is True
    [space] = body["spaces"]
    assert space == {
        "id": sid,
        "name": "Grandparents",
        "emoji": space["emoji"],
        "space_type": space["space_type"],
        "households": 1,
        "post": True,
        "post_review": False,
        "album": True,
    }
    assert body["chats"] == [
        {"id": conv, "name": "Oma", "group": False, "households": 1}
    ]
    assert body["limits"] == {
        "max_items": 50,
        "post_items": 5,
        "chat_items": 5,
        "video_seconds": 60,
    }
    assert (await client.get("/api/gallery/share/targets")).status == 401


async def test_share_to_the_feed_makes_one_image_post_of_copies(client):
    a = await _album(client)
    one, two = await _upload(client, a, (1, 2, 3)), await _upload(client, a, (9, 8, 7))

    r = await _share(
        client,
        {"item_ids": [one["id"], two["id"]], "target": "feed", "text": "For Oma"},
    )

    assert r.status == 200
    assert await r.json() == {
        "shared": 2,
        "skipped": [],
        "trimming": 0,
        "queued": False,
    }
    feed = await (await client.get("/api/feed", headers=_auth(client._tok))).json()
    [post] = [p for p in feed if p.get("image_urls")]
    assert post["content"] == "For Oma"
    assert len(post["image_urls"]) == 2
    # Copies: never the gallery's own files.
    names = {u.split("?")[0].rsplit("/", 1)[-1] for u in post["image_urls"]}
    sources = {i["url"].split("?")[0].rsplit("/", 1)[-1] for i in (one, two)}
    assert names.isdisjoint(sources)


async def test_share_into_a_space_album_and_as_a_space_post(client):
    a = await _album(client)
    item = await _upload(client, a)
    sid = await _space(client)
    r = await client.post(
        f"/api/spaces/{sid}/gallery/albums",
        json={"name": "Family reunion"},
        headers=_auth(client._tok),
    )
    space_album = (await r.json())["id"]

    r = await _share(
        client,
        {
            "item_ids": [item["id"]],
            "target": "space_album",
            "space_id": sid,
            "album_id": space_album,
        },
    )
    assert r.status == 200 and (await r.json())["shared"] == 1
    r = await client.get(
        f"/api/gallery/albums/{space_album}/items", headers=_auth(client._tok)
    )
    [copy] = await r.json()
    assert copy["id"] != item["id"]
    assert copy["item_type"] == "photo"

    r = await _share(
        client,
        {
            "item_ids": [item["id"]],
            "target": "space_album",
            "space_id": sid,
            "new_album_name": "Oma's birthday",
        },
    )
    assert r.status == 200
    albums = await (
        await client.get(
            f"/api/spaces/{sid}/gallery/albums", headers=_auth(client._tok)
        )
    ).json()
    assert "Oma's birthday" in {al["name"] for al in albums}

    r = await _share(
        client, {"item_ids": [item["id"]], "target": "space_post", "space_id": sid}
    )
    assert r.status == 200 and (await r.json())["shared"] == 1
    r = await client.get(f"/api/spaces/{sid}/feed", headers=_auth(client._tok))
    body = await r.json()
    posts = body if isinstance(body, list) else body.get("posts", body.get("items"))
    assert [len(p["image_urls"]) for p in posts] == [1]


async def test_share_to_a_chat_sends_one_message_per_photo(client):
    await _add_user(client, "oma")
    r = await client.post(
        "/api/conversations/dm", json={"username": "oma"}, headers=_auth(client._tok)
    )
    conv = (await r.json())["id"]
    a = await _album(client)
    items = [await _upload(client, a, (i * 90, 9, 9)) for i in range(2)]

    r = await _share(
        client,
        {
            "item_ids": [i["id"] for i in items],
            "target": "chat",
            "conversation_id": conv,
        },
    )

    assert r.status == 200 and (await r.json())["shared"] == 2
    r = await client.get(
        f"/api/conversations/{conv}/messages", headers=_auth(client._tok)
    )
    msgs = await r.json()
    assert sorted(m["type"] for m in msgs) == ["image", "image"]


async def test_over_a_targets_limit_or_bad_input_is_refused(client):
    a = await _album(client)
    ids = [(await _upload(client, a, (i * 40, 0, 0)))["id"] for i in range(6)]
    assert (await _share(client, {"item_ids": ids, "target": "feed"})).status == 422
    assert (await _share(client, {"item_ids": ids, "target": "moon"})).status == 422
    assert (await _share(client, {"item_ids": "x", "target": "feed"})).status == 422
    r = await _share(
        client, {"item_ids": ids[:1], "target": "space_post", "space_id": "nope"}
    )
    assert r.status == 403
    r = await _share(
        client, {"item_ids": ids[:1], "target": "chat", "conversation_id": "nope"}
    )
    assert r.status == 404
    r = await client.post("/api/gallery/share", json={"item_ids": ids[:1]})
    assert r.status == 401


async def test_a_private_albums_photo_is_shared_only_by_its_uploader(client):
    _uid, tok = await _add_user(client, "lena")
    private = await _album(client, "Gifts", tok=tok)
    item = await _upload(client, private, tok=tok)
    await client.app[K.gallery_repo_key].set_album_audience(
        private, "chosen", frozenset({client._uid})
    )

    r = await _share(client, {"item_ids": [item["id"]], "target": "feed"})
    assert await r.json() == {
        "shared": 0,
        "skipped": [{"item_id": item["id"], "reason": "private"}],
        "trimming": 0,
        "queued": False,
    }
    r = await _share(client, {"item_ids": [item["id"]], "target": "feed"}, tok=tok)
    assert (await r.json())["shared"] == 1


async def test_a_subscriber_gets_no_space_target_and_cannot_share_there(client):
    uid, tok = await _add_user(client, "kid")
    sid = await _space(client)
    await client.app[K.space_service_key].add_member(
        sid, actor_username="admin", user_id=uid, role="subscriber"
    )
    a = await _album(client)
    item = await _upload(client, a, tok=tok)
    r = await client.get("/api/gallery/share/targets", headers=_auth(tok))
    assert (await r.json())["spaces"] == []
    r = await _share(
        client,
        {"item_ids": [item["id"]], "target": "space_post", "space_id": sid},
        tok=tok,
    )
    assert r.status == 403


async def test_the_uploader_is_told_in_the_app(client):
    _uid, tok = await _add_user(client, "lena")
    a = await _album(client)
    items = [await _upload(client, a, (i * 90, 1, 1), tok=tok) for i in range(2)]
    mine = await _upload(client, a, (5, 5, 5))

    r = await _share(
        client,
        {"item_ids": [i["id"] for i in (*items, mine)], "target": "feed"},
    )
    assert (await r.json())["shared"] == 3

    await client.app[K.notification_delivery_key].drain()
    r = await client.get("/api/notifications", headers=_auth(tok))
    rows = [n for n in await r.json() if n["type"] == "gallery_shared"]
    assert [n["title"] for n in rows] == ["Admin shared 2 of your photos in the feed"]
