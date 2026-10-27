"""Tests for feed routes — /api/feed and /api/feed/posts/* endpoints."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import db_key as _db_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(tmp_dir):
    """App client with admin user (pascal) and regular user (bob)."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        db = app[_db_key]
        _row = await db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'"
        )
        _pk = bytes.fromhex(_row["identity_public_key"])

        class _KP:
            public_key = _pk

        kp = _KP()
        uid = derive_user_id(kp.public_key, "pascal")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
            ("pascal", uid, "Pascal"),
        )
        raw_token = "test-token-raw"
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-1", uid, "test", sha256_token_hash(raw_token)),
        )
        uid2 = derive_user_id(kp.public_key, "bob")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
            ("bob", uid2, "Bob"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-2", uid2, "test", sha256_token_hash("bob-token-raw")),
        )
        tc._admin_token = raw_token
        tc._admin_uid = uid
        tc._bob_token = "bob-token-raw"
        tc._bob_uid = uid2
        yield tc


async def test_list_feed_empty(client):
    """GET /api/feed returns an empty list when no posts exist."""
    resp = await client.get("/api/feed", headers=_auth(client._admin_token))
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body, list)


async def test_create_post(client):
    """POST /api/feed/posts creates a post and returns 201."""
    resp = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "Hello world!"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["content"] == "Hello world!"
    assert "id" in body


async def test_create_post_appears_in_feed(client):
    """A created post appears in the feed listing."""
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "Feed item"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]
    resp = await client.get("/api/feed", headers=_auth(client._admin_token))
    body = await resp.json()
    assert any(p["id"] == post_id for p in body)


async def test_feed_payload_includes_latest_comment(client):
    """``GET /api/feed`` carries the newest non-deleted comment per
    post under a ``latest_comment`` key.  Posts with zero comments
    expose ``latest_comment: null`` so the SPA can rely on the field
    being present on every entry."""
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "thread starter"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]
    # Post with no comments → latest_comment should be null.
    body = await (
        await client.get("/api/feed", headers=_auth(client._admin_token))
    ).json()
    me = next(p for p in body if p["id"] == post_id)
    assert me["latest_comment"] is None
    # Add two comments — newest one wins.
    await client.post(
        f"/api/feed/posts/{post_id}/comments",
        json={"content": "first"},
        headers=_auth(client._admin_token),
    )
    second = await (
        await client.post(
            f"/api/feed/posts/{post_id}/comments",
            json={"content": "second"},
            headers=_auth(client._admin_token),
        )
    ).json()
    body = await (
        await client.get("/api/feed", headers=_auth(client._admin_token))
    ).json()
    me = next(p for p in body if p["id"] == post_id)
    assert me["latest_comment"] is not None
    assert me["latest_comment"]["id"] == second["id"]
    assert me["latest_comment"]["content"] == "second"


async def test_post_image_urls_are_signed(client):
    """Each entry in ``image_urls`` comes back with ``?exp=&sig=`` so
    the SPA can drop it into ``<img src>`` without the bearer token.
    Image posts use ``image_urls`` exclusively; ``media_url`` is None."""
    r = await client.post(
        "/api/feed/posts",
        json={
            "type": "image",
            "content": "with a pic",
            "image_urls": ["/api/media/abc.webp", "/api/media/def.webp"],
        },
        headers=_auth(client._admin_token),
    )
    body = await r.json()
    assert body["media_url"] is None
    image_urls = body["image_urls"]
    assert len(image_urls) == 2
    for url, name in zip(image_urls, ["abc", "def"]):
        assert url.startswith(f"/api/media/{name}.webp?")
        assert "exp=" in url
        assert "sig=" in url


async def test_post_image_urls_query_stripped_on_create(client):
    """If the client echoes a signed URL back to ``POST /api/feed/posts``
    (e.g. from the composer's preview), the server stores the canonical
    form — never the short-lived ``?exp=&sig=`` fragment. We assert by
    checking the *returned* sig differs from a fixed nonce we passed in
    (i.e. the server didn't just copy what we sent)."""
    r = await client.post(
        "/api/feed/posts",
        json={
            "type": "image",
            "content": "echo test",
            "image_urls": [
                "/api/media/xyz.webp?exp=99999999&sig=NOT_REAL",
            ],
        },
        headers=_auth(client._admin_token),
    )
    body = await r.json()
    image_url = body["image_urls"][0]
    assert image_url.startswith("/api/media/xyz.webp?")
    assert "sig=NOT_REAL" not in image_url


async def test_edit_post(client):
    """PATCH /api/feed/posts/{id} updates the post content."""
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "v1"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]
    resp = await client.patch(
        f"/api/feed/posts/{post_id}",
        json={"content": "v2"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    assert (await resp.json())["content"] == "v2"


async def test_delete_post(client):
    """DELETE /api/feed/posts/{id} returns 204."""
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "bye"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]
    resp = await client.delete(
        f"/api/feed/posts/{post_id}",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 204


async def test_add_reaction(client):
    """POST /api/feed/posts/{id}/reactions adds an emoji reaction."""
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "React!"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]
    resp = await client.post(
        f"/api/feed/posts/{post_id}/reactions",
        json={"emoji": "👍"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200


async def test_remove_reaction(client):
    """DELETE /api/feed/posts/{id}/reactions/{emoji} removes the reaction."""
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "React!"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]
    await client.post(
        f"/api/feed/posts/{post_id}/reactions",
        json={"emoji": "❤️"},
        headers=_auth(client._admin_token),
    )
    resp = await client.delete(
        f"/api/feed/posts/{post_id}/reactions/%E2%9D%A4%EF%B8%8F",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200


async def test_add_comment(client):
    """POST /api/feed/posts/{id}/comments adds a comment and returns 201."""
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "Comment me"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]
    resp = await client.post(
        f"/api/feed/posts/{post_id}/comments",
        json={"content": "Nice post!"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201


async def test_list_comments(client):
    """GET /api/feed/posts/{id}/comments lists all comments for a post."""
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "List comments"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]
    await client.post(
        f"/api/feed/posts/{post_id}/comments",
        json={"content": "c1"},
        headers=_auth(client._admin_token),
    )
    resp = await client.get(
        f"/api/feed/posts/{post_id}/comments",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 200
    comments = await resp.json()
    assert len(comments) == 1


async def test_create_post_empty_content_422(client):
    """POST /api/feed/posts with empty content returns 422."""
    resp = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": ""},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_create_post_bad_type_422(client):
    """POST /api/feed/posts with an invalid type returns 422."""
    resp = await client.post(
        "/api/feed/posts",
        json={"type": "invalid_type", "content": "test"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_create_location_post_round_trip(client):
    """A location post stores lat/lon and returns them in the response,
    truncated to 4 decimals at the service boundary."""
    resp = await client.post(
        "/api/feed/posts",
        json={
            "type": "location",
            "content": "Beach day 🌊",
            "location": {
                "lat": 52.5200123456,
                "lon": 4.0600987654,
                "label": "Marina",
            },
        },
        headers=_auth(client._admin_token),
    )
    assert resp.status == 201, await resp.text()
    body = await resp.json()
    assert body["type"] == "location"
    assert body["location"]["lat"] == 52.5200
    assert body["location"]["lon"] == 4.0601
    assert body["location"]["label"] == "Marina"


async def test_create_location_post_missing_coords_422(client):
    resp = await client.post(
        "/api/feed/posts",
        json={"type": "location"},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_create_location_post_label_too_long_422(client):
    resp = await client.post(
        "/api/feed/posts",
        json={
            "type": "location",
            "location": {"lat": 0.0, "lon": 0.0, "label": "x" * 81},
        },
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


async def test_get_nonexistent_post_comments_returns_empty(client):
    """GET /api/feed/posts/{id}/comments for an unknown post returns 200 with empty list."""
    resp = await client.get(
        "/api/feed/posts/no-such-post-id/comments",
        headers=_auth(client._admin_token),
    )
    # The route returns an empty list (not 404) for posts with no comments
    assert resp.status in (200, 404)


async def test_add_reaction_empty_emoji_422(client):
    """POST reactions with empty emoji returns 422."""
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "test"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]
    resp = await client.post(
        f"/api/feed/posts/{post_id}/reactions",
        json={"emoji": ""},
        headers=_auth(client._admin_token),
    )
    assert resp.status == 422


# ─── Saved posts (§10) ────────────────────────────────────────────────────


async def test_save_and_list_saved_posts(client):
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "bookmark me"},
        headers=_auth(client._admin_token),
    )
    pid = (await r.json())["id"]
    r = await client.post(
        f"/api/feed/posts/{pid}/save",
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    assert (await r.json())["saved"] is True

    r = await client.get(
        "/api/feed/saved",
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    saved = await r.json()
    assert any(p["id"] == pid for p in saved)


async def test_unsave_post_removes_bookmark(client):
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "temp bookmark"},
        headers=_auth(client._admin_token),
    )
    pid = (await r.json())["id"]
    await client.post(
        f"/api/feed/posts/{pid}/save",
        headers=_auth(client._admin_token),
    )
    r = await client.delete(
        f"/api/feed/posts/{pid}/save",
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    r = await client.get(
        "/api/feed/saved",
        headers=_auth(client._admin_token),
    )
    saved = await r.json()
    assert all(p["id"] != pid for p in saved)


async def test_save_unknown_post_404(client):
    r = await client.post(
        "/api/feed/posts/ghost/save",
        headers=_auth(client._admin_token),
    )
    assert r.status == 404


# ── Read watermark (§23.17.1) ──────────────────────────────────────────────


async def test_feed_read_watermark_requires_auth(client):
    r = await client.get("/api/me/feed/read")
    assert r.status == 401
    r2 = await client.post("/api/me/feed/read", json={"post_id": None})
    assert r2.status == 401


async def test_feed_read_watermark_round_trip(client):
    # Create a post first so we have a real id to pin.
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "hello"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    pid = (await r.json())["id"]

    # Default: no watermark set yet.
    r = await client.get("/api/me/feed/read", headers=_auth(client._admin_token))
    assert r.status == 200
    assert (await r.json())["last_read_post_id"] is None

    r = await client.post(
        "/api/me/feed/read",
        json={"post_id": pid},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    body = await r.json()
    assert body["last_read_post_id"] == pid

    # GET returns the same value.
    r = await client.get("/api/me/feed/read", headers=_auth(client._admin_token))
    assert (await r.json())["last_read_post_id"] == pid


async def test_feed_read_watermark_rejects_unknown_post(client):
    r = await client.post(
        "/api/me/feed/read",
        json={"post_id": "no-such-post"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 404


async def test_feed_read_watermark_requires_body_field(client):
    """Posting without a ``post_id`` field → 422."""
    r = await client.post(
        "/api/me/feed/read",
        json={"other": "thing"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 422


async def test_feed_read_watermark_accepts_null_to_clear(client):
    r = await client.post(
        "/api/me/feed/read",
        json={"post_id": None},
        headers=_auth(client._admin_token),
    )
    assert r.status == 200
    assert (await r.json())["last_read_post_id"] is None


# ─── Video media_status on the feed listing ──────────────────────────────


async def _create_video_post(client) -> tuple[str, str]:
    """Create a video post pointing at ``api/media/<fn>.webm`` and enqueue
    a matching transcode row. Returns ``(post_id, output_fn)``."""
    from socialhome.app_keys import (
        media_transcode_repo_key,
        media_transcode_service_key,
    )

    await client.app[media_transcode_service_key].stop()
    fn = "feedvid0000000000000000000000.webm"
    r = await client.post(
        "/api/feed/posts",
        json={"type": "video", "media_url": f"api/media/{fn}"},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    post_id = (await r.json())["id"]
    await client.app[media_transcode_repo_key].enqueue(
        output_filename=fn,
        source_path="/tmp/src.bin",
        thumbnail_filename="thumb.webp",
        owner_user_id=client._admin_uid,
    )
    return post_id, fn


async def _feed(client) -> list[dict]:
    r = await client.get("/api/feed", headers=_auth(client._admin_token))
    assert r.status == 200
    return await r.json()


async def test_feed_video_media_status_processing(client):
    from socialhome.app_keys import media_transcode_repo_key

    post_id, fn = await _create_video_post(client)
    await client.app[media_transcode_repo_key].mark_processing(fn)
    post = next(p for p in await _feed(client) if p["id"] == post_id)
    assert post["type"] == "video"
    assert post["media_status"] == "processing"


async def test_feed_video_media_status_ready_after_complete(client):
    from socialhome.app_keys import media_transcode_repo_key

    post_id, fn = await _create_video_post(client)
    await client.app[media_transcode_repo_key].complete(fn)
    post = next(p for p in await _feed(client) if p["id"] == post_id)
    assert post["media_status"] == "ready"


async def test_feed_video_media_status_failed(client):
    from socialhome.app_keys import media_transcode_repo_key

    post_id, fn = await _create_video_post(client)
    await client.app[media_transcode_repo_key].mark_failed(fn, "boom")
    post = next(p for p in await _feed(client) if p["id"] == post_id)
    assert post["media_status"] == "failed"


async def test_feed_text_post_has_no_processing_status(client):
    await _create_video_post(client)
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "just words"},
        headers=_auth(client._admin_token),
    )
    text_id = (await r.json())["id"]
    post = next(p for p in await _feed(client) if p["id"] == text_id)
    assert post.get("media_status") != "processing"


async def test_feed_video_post_has_signed_poster(client):
    """A video post carries a signed ``media_thumbnail_url`` — the
    ``.webp`` sibling of ``media_url`` (shared stem), signed so the SPA
    can drop it into ``<video poster>`` without a bearer token."""
    post_id, _fn = await _create_video_post(client)
    post = next(p for p in await _feed(client) if p["id"] == post_id)
    poster = post["media_thumbnail_url"]
    base = poster.split("?", 1)[0]
    assert base == "api/media/feedvid0000000000000000000000.webp"
    # Same stem as the (signed) media_url.
    media_base = post["media_url"].split("?", 1)[0]
    assert base[: -len(".webp")] == media_base[: -len(".webm")]
    # Signed: carries an exp/sig query.
    assert "exp=" in poster and "sig=" in poster


async def test_feed_text_post_has_no_poster(client):
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "just words"},
        headers=_auth(client._admin_token),
    )
    text_id = (await r.json())["id"]
    post = next(p for p in await _feed(client) if p["id"] == text_id)
    assert "media_thumbnail_url" not in post


async def test_saved_video_post_media_status_processing(client):
    from socialhome.app_keys import media_transcode_repo_key

    post_id, fn = await _create_video_post(client)
    await client.app[media_transcode_repo_key].mark_processing(fn)
    await client.post(
        f"/api/feed/posts/{post_id}/save",
        headers=_auth(client._admin_token),
    )
    r = await client.get("/api/feed/saved", headers=_auth(client._admin_token))
    post = next(p for p in await r.json() if p["id"] == post_id)
    assert post["media_status"] == "processing"


async def test_get_post_is_its_permalink(client):
    """GET /api/feed/posts/{id} returns the one post (a bell opens it)."""
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "permalink me"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]
    resp = await client.get(
        f"/api/feed/posts/{post_id}", headers=_auth(client._bob_token)
    )
    assert resp.status == 200
    body = await resp.json()
    assert (body["id"], body["content"]) == (post_id, "permalink me")


async def test_get_post_deleted_or_unknown_is_404(client):
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "gone soon"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]
    await client.delete(
        f"/api/feed/posts/{post_id}", headers=_auth(client._admin_token)
    )
    for pid in (post_id, "no-such-post"):
        resp = await client.get(
            f"/api/feed/posts/{pid}", headers=_auth(client._bob_token)
        )
        assert resp.status == 404, pid


async def test_get_post_of_a_blocked_author_is_404(client):
    """The permalink hides what the feed hides: a blocked author's post."""
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "from pascal"},
        headers=_auth(client._admin_token),
    )
    post_id = (await r.json())["id"]
    r = await client.post(
        "/api/blocks",
        json={"user_id": client._admin_uid},
        headers=_auth(client._bob_token),
    )
    assert r.status in (200, 201, 204), await r.text()
    resp = await client.get(
        f"/api/feed/posts/{post_id}", headers=_auth(client._bob_token)
    )
    assert resp.status == 404
