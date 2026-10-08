"""HTTP tests for /api/moments/* (§Momentum)."""

from __future__ import annotations

from .conftest import _auth


async def _seed_target(client, *, user_id="uid-bob", username="bob"):
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, user_id, "Bob"),
    )


# ── Create + list ────────────────────────────────────────────────────────


async def test_create_text_moment_and_list(client):
    r = await client.post(
        "/api/moments",
        json={"content": "hello world"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["content"] == "hello world"
    assert body["author_user_id"] == client._uid
    assert body["origin_instance_id"]

    r = await client.get("/api/moments", headers=_auth(client._tok))
    assert r.status == 200
    items = await r.json()
    assert {m["id"] for m in items} == {body["id"]}
    # Inbox rows always include the engagement counts so the SPA can
    # render the Twitter-style chip row without follow-up fetches.
    assert items[0]["reaction_count"] == 0
    assert items[0]["reply_count"] == 0


async def test_inbox_includes_aggregated_counts(client):
    """Reactions + replies show up as integers on each inbox row."""
    r = await client.post(
        "/api/moments",
        json={"content": "hi"},
        headers=_auth(client._tok),
    )
    moment_id = (await r.json())["id"]
    await client.put(
        f"/api/moments/{moment_id}/reaction",
        json={"emoji": "🔥"},
        headers=_auth(client._tok),
    )
    await client.post(
        "/api/moments",
        json={"content": "yo", "parent_moment_id": moment_id},
        headers=_auth(client._tok),
    )
    items = await (await client.get("/api/moments", headers=_auth(client._tok))).json()
    parent = next(m for m in items if m["id"] == moment_id)
    assert parent["reaction_count"] == 1
    assert parent["reply_count"] == 1


async def test_create_empty_rejected(client):
    r = await client.post(
        "/api/moments",
        json={"content": "  "},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_create_with_external_media_url_is_422(client):
    """A moment's media must be a local upload (``INVALID_MEDIA_URL``)."""
    r = await client.post(
        "/api/moments",
        json={
            "content": "look",
            "media_url": "https://example.invalid/img.jpg",
            "media_type": "image",
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422, await r.text()
    assert (await r.json())["error"]["code"] == "INVALID_MEDIA_URL"


async def test_rate_limit_429_within_window(client):
    r = await client.post(
        "/api/moments",
        json={"content": "one"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    r = await client.post(
        "/api/moments",
        json={"content": "two"},
        headers=_auth(client._tok),
    )
    assert r.status == 429
    body = await r.json()
    assert body["error"]["code"] == "MOMENT_RATE_LIMIT"


async def test_video_over_15s_rejected(client):
    r = await client.post(
        "/api/moments",
        json={
            "content": "",
            "media_url": "/api/media/clip.webm",
            "media_type": "video",
            "duration_ms": 16_000,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_moment_media_url_is_signed_in_responses(client):
    """The browser drops the Authorization header on ``<img src>`` /
    ``<video src>`` requests, so moments returned to the SPA need to
    carry a signed ``?exp=&sig=`` query — otherwise the canonical
    ``/api/media/...`` URL 401s the moment the inbox renders."""
    create = await client.post(
        "/api/moments",
        json={
            "content": "look",
            "media_url": "/api/media/sunset.webp",
            "media_type": "image",
        },
        headers=_auth(client._tok),
    )
    assert create.status == 201
    create_body = await create.json()
    moment_id = create_body["id"]
    assert create_body["media_url"].startswith("/api/media/sunset.webp?"), create_body[
        "media_url"
    ]
    assert "exp=" in create_body["media_url"]
    assert "sig=" in create_body["media_url"]

    inbox = await (await client.get("/api/moments", headers=_auth(client._tok))).json()
    found = next(m for m in inbox if m["id"] == moment_id)
    assert found["media_url"].startswith("/api/media/sunset.webp?"), found["media_url"]

    detail = await (
        await client.get(f"/api/moments/{moment_id}", headers=_auth(client._tok))
    ).json()
    assert detail["moment"]["media_url"].startswith("/api/media/sunset.webp?")


async def test_moment_create_strips_inbound_signature_query(client):
    """If the SPA echoes a signed upload URL back into ``media_url`` on
    create, the route must drop ``?exp=&sig=`` before persisting so the
    moment row carries the canonical URL only — the server signs fresh
    on every read."""
    resp = await client.post(
        "/api/moments",
        json={
            "content": "ok",
            "media_url": "/api/media/sunset.webp?exp=99999999999&sig=stale",
            "media_type": "image",
        },
        headers=_auth(client._tok),
    )
    assert resp.status == 201
    out_url = (await resp.json())["media_url"]
    base = out_url.split("?", 1)[0]
    assert base == "/api/media/sunset.webp"
    # Exactly one ``?`` — the server signed afresh, didn't concatenate.
    assert out_url.count("?") == 1


# ── Replies ──────────────────────────────────────────────────────────────


async def test_reply_skips_rate_limit(client):
    r = await client.post(
        "/api/moments",
        json={"content": "root"},
        headers=_auth(client._tok),
    )
    parent_id = (await r.json())["id"]
    # Replying again is fine even within the 15-min window.
    r = await client.post(
        "/api/moments",
        json={"content": "self-reply", "parent_moment_id": parent_id},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    assert (await r.json())["parent_moment_id"] == parent_id


async def test_detail_includes_replies_and_reactions(client):
    r = await client.post(
        "/api/moments",
        json={"content": "root"},
        headers=_auth(client._tok),
    )
    parent_id = (await r.json())["id"]
    await client.post(
        "/api/moments",
        json={"content": "r", "parent_moment_id": parent_id},
        headers=_auth(client._tok),
    )
    await client.put(
        f"/api/moments/{parent_id}/reaction",
        json={"emoji": "🔥"},
        headers=_auth(client._tok),
    )
    r = await client.get(
        f"/api/moments/{parent_id}",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["moment"]["id"] == parent_id
    assert len(body["replies"]) == 1
    assert [rx["emoji"] for rx in body["reactions"]] == ["🔥"]


# ── Block visibility ─────────────────────────────────────────────────────


async def test_blocked_authors_hidden_from_inbox(client):
    """Someone the viewer blocked drops out of the moments inbox."""
    await _seed_target(client)
    # Bob posts a moment via his own user_id (we hit the repo directly
    # because there's no other auth context wired in this test).
    await client._db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, "
        "origin_instance_id, created_at, expires_at) "
        "VALUES(?,?,?,?, datetime('now'), datetime('now', '+7 day'))",
        ("m-bob", "uid-bob", "from bob", "self"),
    )
    r = await client.get("/api/moments", headers=_auth(client._tok))
    assert any(m["author_user_id"] == "uid-bob" for m in await r.json())
    # Block bob → his moment disappears.
    r = await client.post(
        "/api/blocks",
        json={"user_id": "uid-bob"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    r = await client.get("/api/moments", headers=_auth(client._tok))
    assert not any(m["author_user_id"] == "uid-bob" for m in await r.json())


# ── Report ───────────────────────────────────────────────────────────────


async def test_report_moment_creates_pending_row(client):
    r = await client.post(
        "/api/moments",
        json={"content": "x"},
        headers=_auth(client._tok),
    )
    moment_id = (await r.json())["id"]
    # The reporter must not be the same as the report's only filer (no
    # self-report duplicates), so seed a second reporter via a fresh user.
    await _seed_target(client, user_id="uid-bob", username="bob")
    r = await client.post(
        f"/api/moments/{moment_id}/report",
        json={"category": "spam", "notes": "promotion"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["status"] == "pending"


# ── Follows ──────────────────────────────────────────────────────────────


async def test_follow_unfollow_round_trip(client):
    await _seed_target(client)
    r = await client.post(
        "/api/moments/follows",
        json={"user_id": "uid-bob"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    r = await client.get("/api/moments/follows", headers=_auth(client._tok))
    body = await r.json()
    assert [f["user_id"] for f in body["follows"]] == ["uid-bob"]
    r = await client.delete(
        "/api/moments/follows/uid-bob",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await (
        await client.get(
            "/api/moments/follows",
            headers=_auth(client._tok),
        )
    ).json()
    assert body["follows"] == []


async def test_follow_self_rejected(client):
    r = await client.post(
        "/api/moments/follows",
        json={"user_id": client._uid},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_archive_returns_visible_moments(client):
    await client.post(
        "/api/moments",
        json={"content": "one"},
        headers=_auth(client._tok),
    )
    r = await client.get("/api/moments/archive", headers=_auth(client._tok))
    assert r.status == 200
    items = await r.json()
    assert len(items) == 1


async def test_archive_filters_by_tag(client):
    await client.post(
        "/api/moments",
        json={"content": "hike day #outdoors"},
        headers=_auth(client._tok),
    )
    # Service rate-limits 1 top-level moment / 15 min, so only one
    # author-side post per test. Seed a second moment via the bus.
    await client._db.enqueue(
        "UPDATE moments SET created_at=datetime('now', '-20 minutes') "
        "WHERE author_user_id=?",
        (client._uid,),
    )
    await client.post(
        "/api/moments",
        json={"content": "kitchen #cooking"},
        headers=_auth(client._tok),
    )
    r = await client.get(
        "/api/moments/archive?tag=outdoors",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    items = await r.json()
    assert {m["content"] for m in items} == {"hike day #outdoors"}


async def test_hashtags_endpoint_returns_aggregates(client):
    await client.post(
        "/api/moments",
        json={"content": "#alpha and #beta"},
        headers=_auth(client._tok),
    )
    r = await client.get("/api/moments/hashtags", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    tags = {row["tag"]: row["count"] for row in body["hashtags"]}
    assert tags == {"alpha": 1, "beta": 1}


async def test_reaction_clear_returns_null(client):
    r = await client.post(
        "/api/moments",
        json={"content": "rx"},
        headers=_auth(client._tok),
    )
    moment_id = (await r.json())["id"]
    await client.put(
        f"/api/moments/{moment_id}/reaction",
        json={"emoji": "🔥"},
        headers=_auth(client._tok),
    )
    r = await client.delete(
        f"/api/moments/{moment_id}/reaction",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json()) == {"moment_id": moment_id, "emoji": None}


async def test_reaction_empty_emoji_rejected(client):
    r = await client.post(
        "/api/moments",
        json={"content": "x"},
        headers=_auth(client._tok),
    )
    moment_id = (await r.json())["id"]
    r = await client.put(
        f"/api/moments/{moment_id}/reaction",
        json={"emoji": "  "},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_delete_moment_round_trip(client):
    r = await client.post(
        "/api/moments",
        json={"content": "byes"},
        headers=_auth(client._tok),
    )
    moment_id = (await r.json())["id"]
    r = await client.delete(
        f"/api/moments/{moment_id}",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    r = await client.get(
        f"/api/moments/{moment_id}",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_endpoints_require_auth(client):
    for path in ["/api/moments", "/api/moments/archive", "/api/moments/follows"]:
        r = await client.get(path)
        assert r.status == 401, path
    r = await client.post("/api/moments", json={"content": "x"})
    assert r.status == 401
    r = await client.post("/api/moments/follows", json={"user_id": "uid-bob"})
    assert r.status == 401


async def test_create_missing_user_id_on_follow(client):
    r = await client.post(
        "/api/moments/follows",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 400


async def test_report_missing_category_rejected(client):
    r = await client.post(
        "/api/moments",
        json={"content": "x"},
        headers=_auth(client._tok),
    )
    moment_id = (await r.json())["id"]
    r = await client.post(
        f"/api/moments/{moment_id}/report",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 422


# ── Video media_status ─────────────────────────────────────────────────────


async def _create_video_moment(client) -> tuple[str, str]:
    """Create a video moment + enqueue a matching transcode row.

    Returns ``(moment_id, output_fn)``. Stops the scheduler so the row
    stays put while the test inspects readiness.
    """
    from socialhome.app_keys import (
        media_transcode_repo_key,
        media_transcode_service_key,
    )

    await client.app[media_transcode_service_key].stop()
    fn = "momvid00000000000000000000000.webm"
    r = await client.post(
        "/api/moments",
        json={
            "content": "look at this",
            "media_url": f"api/media/{fn}",
            "media_type": "video",
            "duration_ms": 5000,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    moment_id = (await r.json())["id"]
    await client.app[media_transcode_repo_key].enqueue(
        output_filename=fn,
        source_path="/tmp/src.bin",
        thumbnail_filename="thumb.webp",
        owner_user_id=client._uid,
    )
    return moment_id, fn


async def _inbox(client) -> list[dict]:
    r = await client.get("/api/moments", headers=_auth(client._tok))
    assert r.status == 200
    return await r.json()


async def test_moment_video_media_status_processing(client):
    from socialhome.app_keys import media_transcode_repo_key

    moment_id, fn = await _create_video_moment(client)
    await client.app[media_transcode_repo_key].mark_processing(fn)
    m = next(x for x in await _inbox(client) if x["id"] == moment_id)
    assert m["media_type"] == "video"
    assert m["media_status"] == "processing"


async def test_moment_video_media_status_ready_after_complete(client):
    from socialhome.app_keys import media_transcode_repo_key

    moment_id, fn = await _create_video_moment(client)
    await client.app[media_transcode_repo_key].complete(fn)
    m = next(x for x in await _inbox(client) if x["id"] == moment_id)
    assert m["media_status"] == "ready"


async def test_moment_video_media_status_failed(client):
    from socialhome.app_keys import media_transcode_repo_key

    moment_id, fn = await _create_video_moment(client)
    await client.app[media_transcode_repo_key].mark_failed(fn, "boom")
    m = next(x for x in await _inbox(client) if x["id"] == moment_id)
    assert m["media_status"] == "failed"


async def test_moment_text_has_no_processing_status(client):
    # A reply is exempt from the per-author top-level rate limit, so it
    # can coexist with the video moment in the same inbox.
    parent_id, _fn = await _create_video_moment(client)
    r = await client.post(
        "/api/moments",
        json={"content": "plain text", "parent_moment_id": parent_id},
        headers=_auth(client._tok),
    )
    text_id = (await r.json())["id"]
    m = next(x for x in await _inbox(client) if x["id"] == text_id)
    assert m["media_type"] is None
    assert m.get("media_status") != "processing"


async def test_moment_archive_video_media_status(client):
    from socialhome.app_keys import media_transcode_repo_key

    moment_id, fn = await _create_video_moment(client)
    await client.app[media_transcode_repo_key].mark_processing(fn)
    r = await client.get("/api/moments/archive", headers=_auth(client._tok))
    assert r.status == 200
    m = next(x for x in await r.json() if x["id"] == moment_id)
    assert m["media_status"] == "processing"


async def test_moment_detail_video_media_status(client):
    from socialhome.app_keys import media_transcode_repo_key

    moment_id, fn = await _create_video_moment(client)
    await client.app[media_transcode_repo_key].mark_processing(fn)
    r = await client.get(f"/api/moments/{moment_id}", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["moment"]["media_status"] == "processing"


async def test_moment_video_has_signed_poster(client):
    moment_id, _fn = await _create_video_moment(client)
    m = next(x for x in await _inbox(client) if x["id"] == moment_id)
    poster = m["media_thumbnail_url"]
    base = poster.split("?", 1)[0]
    assert base == "api/media/momvid00000000000000000000000.webp"
    media_base = m["media_url"].split("?", 1)[0]
    assert base[: -len(".webp")] == media_base[: -len(".webm")]
    assert "exp=" in poster and "sig=" in poster


async def test_moment_text_has_no_poster(client):
    parent_id, _fn = await _create_video_moment(client)
    r = await client.post(
        "/api/moments",
        json={"content": "plain text", "parent_moment_id": parent_id},
        headers=_auth(client._tok),
    )
    text_id = (await r.json())["id"]
    m = next(x for x in await _inbox(client) if x["id"] == text_id)
    assert "media_thumbnail_url" not in m


async def test_moment_detail_video_has_signed_poster(client):
    moment_id, _fn = await _create_video_moment(client)
    r = await client.get(f"/api/moments/{moment_id}", headers=_auth(client._tok))
    body = await r.json()
    poster = body["moment"]["media_thumbnail_url"]
    assert poster.split("?", 1)[0] == "api/media/momvid00000000000000000000000.webp"
    assert "exp=" in poster and "sig=" in poster
