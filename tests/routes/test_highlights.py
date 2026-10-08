"""HTTP smoke tests for the highlights routes."""

from __future__ import annotations


def _auth(client) -> dict:
    return {"Authorization": f"Bearer {client._tok}"}


async def test_create_frame_then_list(client):
    """POST /api/highlights/frames creates a highlight; GET /api/highlights returns it."""
    resp = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/x.webp",
            "caption_text": "hello",
            "caption_emoji": "🌅",
        },
        headers=_auth(client),
    )
    assert resp.status == 201, await resp.text()
    body = await resp.json()
    assert body["highlight"]["author_user_id"] == client._uid
    assert body["frame"]["sequence"] == 1
    assert body["frame"]["caption_emoji"] == "🌅"

    listed = await client.get("/api/highlights", headers=_auth(client))
    assert listed.status == 200
    rows = await listed.json()
    assert len(rows) == 1
    assert len(rows[0]["frames"]) == 1


async def test_frame_media_url_is_signed_in_responses(client):
    """The browser drops the Authorization header on ``<img src>`` /
    ``<video src>`` requests, so highlight frames returned to the SPA
    need to carry a signed ``?exp=&sig=`` query — otherwise the canonical
    ``/api/media/...`` URL 401s the moment the viewer opens."""
    create = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    assert create.status == 201
    create_body = await create.json()
    created_url = create_body["frame"]["media_url"]
    highlight_id = create_body["highlight"]["id"]
    assert created_url.startswith("/api/media/x.webp?"), created_url
    assert "exp=" in created_url and "sig=" in created_url

    rows = await (await client.get("/api/highlights", headers=_auth(client))).json()
    list_url = rows[0]["frames"][0]["media_url"]
    assert list_url.startswith("/api/media/x.webp?"), list_url
    assert "exp=" in list_url and "sig=" in list_url

    detail = await (
        await client.get(
            f"/api/highlights/{highlight_id}",
            headers=_auth(client),
        )
    ).json()
    detail_url = detail["frames"][0]["media_url"]
    assert detail_url.startswith("/api/media/x.webp?"), detail_url
    assert "exp=" in detail_url and "sig=" in detail_url


async def test_frame_create_strips_inbound_signature_query(client):
    """If the SPA echoes a signed upload URL back into ``media_url`` on
    create, the route must drop ``?exp=&sig=`` before persisting so the
    frame row carries the canonical URL only — the server signs fresh
    on every read."""
    resp = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/x.webp?exp=99999999999&sig=stale",
        },
        headers=_auth(client),
    )
    assert resp.status == 201, await resp.text()
    # The response is signed fresh; assert the file path under the
    # signature stayed canonical (no double-quoted ``?exp=...?exp=...``).
    out_url = (await resp.json())["frame"]["media_url"]
    base = out_url.split("?", 1)[0]
    assert base == "/api/media/x.webp"
    # Exactly one ``?`` — sig was minted afresh, not concatenated.
    assert out_url.count("?") == 1


async def test_react_and_clear(client):
    """PUT /reaction sets, DELETE /reaction clears."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    assert resp.status == 201
    body = await resp.json()
    frame_id = body["frame"]["id"]
    # Authors don't accumulate views on their own highlights, but reactions
    # are still recorded — we just want to exercise the endpoints.
    r = await client.put(
        f"/api/highlights/frames/{frame_id}/reaction",
        json={"emoji": "🔥"},
        headers=_auth(client),
    )
    assert r.status == 200
    r = await client.delete(
        f"/api/highlights/frames/{frame_id}/reaction",
        headers=_auth(client),
    )
    assert r.status == 200


async def test_share_to_household_feed(client):
    """POST /api/highlights/{id}/share creates a highlight_share post."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    body = await resp.json()
    highlight_id = body["highlight"]["id"]
    s = await client.post(
        f"/api/highlights/{highlight_id}/share",
        json={"scope": "household", "note": "look at this"},
        headers=_auth(client),
    )
    assert s.status == 201, await s.text()
    j = await s.json()
    assert j["highlight_id"] == highlight_id
    assert j["post_id"]


async def test_delete_highlight(client):
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    highlight_id = (await resp.json())["highlight"]["id"]
    d = await client.delete(f"/api/highlights/{highlight_id}", headers=_auth(client))
    assert d.status == 204
    listed = await client.get("/api/highlights", headers=_auth(client))
    rows = await listed.json()
    assert rows == []


async def test_create_frame_validates_inputs(client):
    """Bad bodies surface 400; bad enum values surface 400 too."""
    # Missing media_url
    r = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image"},
        headers=_auth(client),
    )
    assert r.status == 400

    # Invalid frame_type
    r = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "audio", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    assert r.status == 400

    # Invalid audience_kind
    r = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/x.webp",
            "audience_kind": "everyone",
        },
        headers=_auth(client),
    )
    assert r.status == 400

    # audience must be a list
    r = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/x.webp",
            "audience": "not-a-list",
        },
        headers=_auth(client),
    )
    assert r.status == 400


async def test_create_frame_with_external_media_url_is_422(client):
    """A third-party ``media_url`` is refused with the coded envelope —
    viewers would fetch it (IP leak) and receivers drop it, so the
    highlight would never federate. Nothing is created."""
    r = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "https://example.invalid/img.jpg"},
        headers=_auth(client),
    )
    assert r.status == 422, await r.text()
    body = await r.json()
    assert body["error"]["code"] == "INVALID_MEDIA_URL"
    assert body["error"]["params"] == {"field": "media_url"}
    assert "example.invalid" not in str(body)

    listed = await client.get("/api/highlights", headers=_auth(client))
    assert await listed.json() == []


async def test_get_highlight_with_views_and_reactions(client):
    """Author GET returns inline views + reactions per frame."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    body = await resp.json()
    highlight_id = body["highlight"]["id"]
    frame_id = body["frame"]["id"]

    # React on the frame so the author gets a non-empty reactions block.
    r = await client.put(
        f"/api/highlights/frames/{frame_id}/reaction",
        json={"emoji": "🔥"},
        headers=_auth(client),
    )
    assert r.status == 200

    g = await client.get(f"/api/highlights/{highlight_id}", headers=_auth(client))
    assert g.status == 200
    detail = await g.json()
    assert detail["highlight"]["id"] == highlight_id
    assert "views" in detail
    assert "reactions" in detail
    assert frame_id in detail["reactions"]


async def test_get_unknown_highlight_returns_404(client):
    g = await client.get("/api/highlights/missing-id", headers=_auth(client))
    assert g.status == 404


async def test_reaction_validation(client):
    """Empty / missing emoji on PUT returns 400."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    frame_id = (await resp.json())["frame"]["id"]
    r = await client.put(
        f"/api/highlights/frames/{frame_id}/reaction",
        json={"emoji": "  "},
        headers=_auth(client),
    )
    assert r.status == 400
    r = await client.put(
        f"/api/highlights/frames/{frame_id}/reaction",
        json={},
        headers=_auth(client),
    )
    assert r.status == 400


async def test_view_marks_frame(client):
    """``POST /frames/{id}/view`` returns 200."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    frame_id = (await resp.json())["frame"]["id"]
    r = await client.post(
        f"/api/highlights/frames/{frame_id}/view",
        json={},
        headers=_auth(client),
    )
    assert r.status == 200


async def test_share_validates_scope(client):
    """``share`` must specify a known scope."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    highlight_id = (await resp.json())["highlight"]["id"]
    r = await client.post(
        f"/api/highlights/{highlight_id}/share",
        json={"scope": "neighbour"},
        headers=_auth(client),
    )
    assert r.status == 400


async def test_dm_reply_validates_body(client):
    """``dm-reply`` validates conversation_id + content."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    frame_id = (await resp.json())["frame"]["id"]
    # Missing conversation_id.
    r = await client.post(
        f"/api/highlights/frames/{frame_id}/dm-reply",
        json={"content": "lol"},
        headers=_auth(client),
    )
    assert r.status == 400
    # Non-string content.
    r = await client.post(
        f"/api/highlights/frames/{frame_id}/dm-reply",
        json={"conversation_id": "abc", "content": 123},
        headers=_auth(client),
    )
    assert r.status == 400


async def test_dm_reply_round_trip(client):
    """End-to-end: post a frame → reply via DM with the frame snapshot."""
    h = _auth(client)
    # Seed a peer user + open a 1:1 DM with them.
    await client._db.enqueue(
        "INSERT OR IGNORE INTO users(username, user_id, display_name, state) "
        "VALUES('bob', 'uid-bob', 'Bob', 'active')",
    )
    r = await client.post("/api/conversations/dm", json={"username": "bob"}, headers=h)
    assert r.status == 201
    conv_id = (await r.json())["id"]

    # Author posts a highlight frame, then replies-via-DM to it.
    r = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/x.webp",
            "caption_text": "from the trip",
            "caption_emoji": "🏖",
        },
        headers=h,
    )
    frame_id = (await r.json())["frame"]["id"]
    r = await client.post(
        f"/api/highlights/frames/{frame_id}/dm-reply",
        json={"conversation_id": conv_id, "content": "lol nice"},
        headers=h,
    )
    assert r.status == 201, await r.text()
    assert (await r.json())["message_id"]


async def test_share_to_space_round_trip(client):
    """Share a highlight into a space feed."""
    h = _auth(client)
    # Create a space first via the API so the user is its owner.
    r = await client.post(
        "/api/spaces",
        json={"name": "Trip Group", "space_type": "private"},
        headers=h,
    )
    assert r.status == 201, await r.text()
    space_id = (await r.json())["id"]
    r = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=h,
    )
    highlight_id = (await r.json())["highlight"]["id"]
    r = await client.post(
        f"/api/highlights/{highlight_id}/share",
        json={"scope": "space", "space_id": space_id, "note": "look"},
        headers=h,
    )
    # 201 (created) or 202 (queued for moderation) is acceptable.
    assert r.status in (201, 202), await r.text()


async def test_delete_frame_endpoint(client):
    """Author can DELETE a single frame."""
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    body = await resp.json()
    frame_id = body["frame"]["id"]
    r = await client.delete(
        f"/api/highlights/frames/{frame_id}",
        headers=_auth(client),
    )
    assert r.status == 204


# ── Video frame media_status ───────────────────────────────────────────────


async def _create_video_frame(client) -> tuple[str, str, str]:
    """Create a video highlight frame + a matching transcode row.

    Returns ``(highlight_id, frame_id, output_fn)``. Stops the scheduler
    so the row stays put while the test inspects readiness.
    """
    from socialhome.app_keys import (
        media_transcode_repo_key,
        media_transcode_service_key,
    )

    await client.app[media_transcode_service_key].stop()
    fn = "hlvid000000000000000000000000.webm"
    r = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "video", "media_url": f"api/media/{fn}"},
        headers=_auth(client),
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    highlight_id = body["highlight"]["id"]
    frame_id = body["frame"]["id"]
    await client.app[media_transcode_repo_key].enqueue(
        output_filename=fn,
        source_path="/tmp/src.bin",
        thumbnail_filename="thumb.webp",
        owner_user_id=client._uid,
    )
    return highlight_id, frame_id, fn


async def _list_frames(client) -> list[dict]:
    r = await client.get("/api/highlights", headers=_auth(client))
    assert r.status == 200
    rows = await r.json()
    out: list[dict] = []
    for row in rows:
        out.extend(row["frames"])
    return out


async def test_highlight_video_frame_media_status_processing(client):
    from socialhome.app_keys import media_transcode_repo_key

    _hid, frame_id, fn = await _create_video_frame(client)
    await client.app[media_transcode_repo_key].mark_processing(fn)
    f = next(x for x in await _list_frames(client) if x["id"] == frame_id)
    assert f["frame_type"] == "video"
    assert f["media_status"] == "processing"


async def test_highlight_video_frame_media_status_ready_after_complete(client):
    from socialhome.app_keys import media_transcode_repo_key

    _hid, frame_id, fn = await _create_video_frame(client)
    await client.app[media_transcode_repo_key].complete(fn)
    f = next(x for x in await _list_frames(client) if x["id"] == frame_id)
    assert f["media_status"] == "ready"


async def test_highlight_video_frame_media_status_failed(client):
    from socialhome.app_keys import media_transcode_repo_key

    _hid, frame_id, fn = await _create_video_frame(client)
    await client.app[media_transcode_repo_key].mark_failed(fn, "boom")
    f = next(x for x in await _list_frames(client) if x["id"] == frame_id)
    assert f["media_status"] == "failed"


async def test_highlight_image_frame_has_no_processing_status(client):
    _hid, _frame_id, _fn = await _create_video_frame(client)
    r = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/pic.webp"},
        headers=_auth(client),
    )
    assert r.status == 201
    image_frame_id = (await r.json())["frame"]["id"]
    f = next(x for x in await _list_frames(client) if x["id"] == image_frame_id)
    assert f.get("media_status") != "processing"


async def test_highlight_detail_video_frame_media_status(client):
    from socialhome.app_keys import media_transcode_repo_key

    highlight_id, frame_id, fn = await _create_video_frame(client)
    await client.app[media_transcode_repo_key].mark_processing(fn)
    r = await client.get(f"/api/highlights/{highlight_id}", headers=_auth(client))
    assert r.status == 200
    body = await r.json()
    f = next(x for x in body["frames"] if x["id"] == frame_id)
    assert f["media_status"] == "processing"
