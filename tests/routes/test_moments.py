"""HTTP tests for /api/moments/* (§Momentum)."""

from __future__ import annotations

from socialhome.app_keys import federation_repo_key, federation_service_key
from socialhome.auth import sha256_token_hash
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.moment_origin import (
    ORIGIN_IDENTITY_PK_FIELD,
    ORIGIN_SIG_FIELD,
    ORIGIN_SIG_SUITE_FIELD,
)

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


async def _follows_by_id(client) -> dict[str, dict]:
    r = await client.get("/api/moments/follows", headers=_auth(client._tok))
    assert r.status == 200
    return {f["user_id"]: f for f in (await r.json())["follows"]}


async def _follow(client, user_id: str) -> None:
    r = await client.post(
        "/api/moments/follows",
        json={"user_id": user_id},
        headers=_auth(client._tok),
    )
    assert r.status == 201, await r.text()


async def test_follows_name_local_remote_and_unknown_people(client):
    """Regression (B1, Friends/Profile review): Settings › Privacy named
    followed people from the local roster only, so a remote person showed
    as a raw id and the viewer's nickname was ignored. Each row now carries
    name, signed picture, household and the viewer's nickname."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.set_instance_display_name("Vizeli")
    await fed_repo.save_instance(
        RemoteInstance(
            id="peer-o",
            display_name="Okafor family",
            remote_identity_pk="aa" * 32,
            key_self_to_remote="enc",
            key_remote_to_self="enc",
            remote_inbox_url="https://peer-o.example/wh",
            local_inbox_id="wh-peer-o",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        )
    )
    await client._db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username, "
        " display_name, picture_hash) VALUES(?,?,?,?,?)",
        ("ru-amaka", "peer-o", "amaka", "Amaka Okafor", "cafebabecafebabe"),
    )
    await _seed_target(client)
    put = await client.put(
        "/api/aliases/users/uid-bob",
        json={"alias": "Brother"},
        headers=_auth(client._tok),
    )
    assert put.status in (200, 201), await put.text()
    for uid in ("uid-bob", "ru-amaka", "ru-forgotten"):
        await _follow(client, uid)

    rows = await _follows_by_id(client)
    assert rows["uid-bob"]["display_name"] == "Bob"
    assert rows["uid-bob"]["personal_alias"] == "Brother"
    assert rows["uid-bob"]["household"] == "Vizeli"
    assert rows["uid-bob"]["created_at"]

    amaka = rows["ru-amaka"]
    assert amaka["display_name"] == "Amaka Okafor"
    assert amaka["household"] == "Okafor family"
    assert amaka["personal_alias"] is None
    assert amaka["picture_url"].startswith(
        "api/users/ru-amaka/picture?v=cafebabecafebabe"
    )
    assert "sig=" in amaka["picture_url"]

    gone = rows["ru-forgotten"]
    assert gone["display_name"] is None
    assert gone["household"] is None
    assert gone["picture_url"] is None


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


# ── Authors named for the viewer (#1073 follow-up) ──────────────────────
#
# The SPA named moment authors from the local household cache only, so a
# remote friend's moment read "Someone". Each moment now carries
# ``author`` — the name this household holds, signed picture, household
# and the viewer's nickname — added in the route only: the federation
# payload a moment travels in is unchanged.


def _peer_o() -> RemoteInstance:
    return RemoteInstance(
        id="peer-o",
        display_name="Okafor family",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://peer-o.example/wh",
        local_inbox_id="wh-peer-o",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


async def _remote_amaka(client) -> None:
    await client.app[federation_repo_key].save_instance(_peer_o())
    await client._db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username, "
        " display_name, picture_hash) VALUES(?,?,?,?,?)",
        ("ru-amaka", "peer-o", "amaka", "Amaka Okafor", "cafebabecafebabe"),
    )


async def _seed_moment(
    client, moment_id: str, author: str, *, parent: str | None = None
) -> None:
    await client._db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, parent_moment_id, "
        "origin_instance_id, created_at, expires_at) "
        "VALUES(?,?,?,?,?, datetime('now'), datetime('now', '+7 day'))",
        (moment_id, author, f"from {author}", parent, "peer-o"),
    )


async def test_moments_name_remote_and_unknown_authors(client):
    await _remote_amaka(client)
    await _seed_moment(client, "m-amaka", "ru-amaka")
    await _seed_moment(client, "m-ghost", "ru-forgotten")

    for path in ("/api/moments", "/api/moments/archive"):
        r = await client.get(path, headers=_auth(client._tok))
        assert r.status == 200
        rows = {m["id"]: m for m in await r.json()}
        amaka = rows["m-amaka"]["author"]
        assert amaka["display_name"] == "Amaka Okafor"
        assert amaka["household"] == "Okafor family"
        assert amaka["picture_url"].startswith(
            "api/users/ru-amaka/picture?v=cafebabecafebabe"
        )
        assert "sig=" in amaka["picture_url"]
        # Nobody here knows them: no name — never the id.
        ghost = rows["m-ghost"]["author"]
        assert ghost["display_name"] is None
        assert ghost["picture_url"] is None


async def test_moment_detail_names_author_and_reply_authors(client):
    await _remote_amaka(client)
    await _seed_moment(client, "m-amaka", "ru-amaka")
    r = await client.post(
        "/api/moments",
        json={"content": "nice", "parent_moment_id": "m-amaka"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    r = await client.get("/api/moments/m-amaka", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["moment"]["author"]["display_name"] == "Amaka Okafor"
    assert "sig=" in body["moment"]["author"]["picture_url"]
    [reply] = body["replies"]
    assert reply["author_user_id"] == client._uid
    assert reply["author"]["display_name"]


async def test_moment_reply_across_a_guardian_block_is_name_only(client):
    """A protected kid whose guardian blocked the viewer: the kid's reply
    names them but carries no picture (§CP.F2)."""
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, picture_hash,"
        " child_protection_enabled) VALUES(?,?,?,?,1)",
        ("kid", "uid-kid", "Kid", "ffff0000ffff0000"),
    )
    await client._db.enqueue(
        "INSERT INTO cp_minor_blocks(minor_user_id, blocked_user_id, blocked_by)"
        " VALUES(?,?,?)",
        ("uid-kid", client._uid, client._uid),
    )
    r = await client.post(
        "/api/moments", json={"content": "root"}, headers=_auth(client._tok)
    )
    root = (await r.json())["id"]
    await _seed_moment(client, "m-kid", "uid-kid", parent=root)

    r = await client.get(f"/api/moments/{root}", headers=_auth(client._tok))
    [reply] = (await r.json())["replies"]
    assert reply["author"]["display_name"] == "Kid"
    assert reply["author"]["picture_url"] is None


async def test_moment_federation_payload_carries_no_names(client, monkeypatch):
    """Names are a route-layer view: the MOMENT_CREATED payload a paired
    household receives keeps exactly its wire fields."""
    await _remote_amaka(client)
    sent: list[dict] = []
    fed = client.app[federation_service_key]

    async def _capture(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append({"to": to_instance_id, "type": event_type, "payload": payload})

    # ``FederationService`` is slotted — patch the class for this test.
    monkeypatch.setattr(type(fed), "send_event", _capture)
    r = await client.post(
        "/api/moments", json={"content": "hello"}, headers=_auth(client._tok)
    )
    assert r.status == 201
    moment_id = (await r.json())["id"]
    # Reading it back named adds nothing to what went out.
    inbox = await client.get("/api/moments", headers=_auth(client._tok))
    assert any(m["id"] == moment_id and m["author"] for m in await inbox.json())

    created = [s for s in sent if s["type"] == FederationEventType.MOMENT_CREATED]
    assert [s["to"] for s in created] == ["peer-o"]
    payload = created[0]["payload"]
    assert set(payload) == {
        "moment_id",
        "author_user_id",
        "content",
        "media_url",
        "media_type",
        "duration_ms",
        "parent_moment_id",
        "origin_instance_id",
        "expires_at",
        "occurred_at",
        "hop_count",
        ORIGIN_IDENTITY_PK_FIELD,
        ORIGIN_SIG_SUITE_FIELD,
        ORIGIN_SIG_FIELD,
    }


async def _seed_gfs_follow(
    client,
    *,
    author: str = "u-gfs-noor",
    display_name: str = "Noor Haddad",
    bio: str | None = "Beekeeper. Posts the bees.",
    digest: str | None = "0123456789abcdef",
) -> None:
    await client._db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
        " public_key, inbox_url, status, paired_at) VALUES('g1', 'gfs-1',"
        " 'GFS One', ?, 'https://gfs1.example', 'active', datetime('now'))",
        ("aa" * 32,),
    )
    await client._db.enqueue(
        "INSERT INTO moment_public_follows(follower_user_id, followed_user_id,"
        " gfs_id, followed_instance_pk, followed_username, followed_display_name,"
        " followed_bio, followed_picture_digest) VALUES(?,?,?,?,?,?,?,?)",
        (client._uid, author, "g1", "bb" * 32, "noor", display_name, bio, digest),
    )
    await client._db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id,"
        " created_at, expires_at, is_public, received_via, received_via_gfs_id)"
        " VALUES('m-gfs', ?, 'bees!', 'inst-noor', datetime('now'),"
        " datetime('now', '+7 day'), 1, 'gfs', 'g1')",
        (author,),
    )


async def test_gfs_moment_names_its_author_with_bio_and_picture(client):
    """An author followed only through a GFS reads by the name, bio and
    GFS-mirrored picture their household signed — not "Someone"."""
    await _seed_gfs_follow(client)
    for path in ("/api/moments", "/api/moments/archive", "/api/moments/m-gfs"):
        r = await client.get(path, headers=_auth(client._tok))
        assert r.status == 200
        body = await r.json()
        row = (
            body["moment"]
            if "moment" in body
            else next(m for m in body if m["id"] == "m-gfs")
        )
        author = row["author"]
        assert author["display_name"] == "Noor Haddad"
        assert author["bio"] == "Beekeeper. Posts the bees."
        assert author["household"] is None
        assert author["picture_url"].startswith(
            "api/gfs/g1/moments/users/u-gfs-noor/picture?v=0123456789abcdef"
        )
        assert "sig=" in author["picture_url"]


async def test_gfs_author_without_a_name_is_never_shown_as_an_id(client):
    await _seed_gfs_follow(client, display_name="u-gfs-noor", bio=None, digest=None)
    await client._db.enqueue("UPDATE moment_public_follows SET followed_username=''")
    r = await client.get("/api/moments", headers=_auth(client._tok))
    [row] = [m for m in await r.json() if m["id"] == "m-gfs"]
    assert row["author"]["display_name"] is None
    assert row["author"]["picture_url"] is None
    assert "bio" not in row["author"]


async def test_household_moments_carry_no_bio(client):
    await _remote_amaka(client)
    await _seed_moment(client, "m-amaka", "ru-amaka")
    r = await client.get("/api/moments", headers=_auth(client._tok))
    [row] = [m for m in await r.json() if m["id"] == "m-amaka"]
    assert "bio" not in row["author"]


# ── Audience gate on GET /api/moments/{id} (security) ───────────────────


async def _moment_row(
    client,
    moment_id: str,
    author: str,
    *,
    age_hours: float = 1,
    expires_in_hours: float = 24 * 6,
    hop_count: int = 1,
) -> None:
    await client._db.enqueue(
        "INSERT INTO moments(id, author_user_id, content, origin_instance_id, "
        "created_at, expires_at, hop_count) VALUES(?,?,?,?, "
        "datetime('now', ?), datetime('now', ?), ?)",
        (
            moment_id,
            author,
            "hello",
            "peer-o",
            f"-{age_hours * 3600:.0f} seconds",
            f"{expires_in_hours * 3600:+.0f} seconds",
            hop_count,
        ),
    )


async def _unknown_404(client) -> tuple[int, dict]:
    r = await client.get("/api/moments/m-never-existed", headers=_auth(client._tok))
    return r.status, await r.json()


async def _get(client, moment_id: str) -> tuple[int, dict]:
    r = await client.get(f"/api/moments/{moment_id}", headers=_auth(client._tok))
    return r.status, await r.json()


async def test_a_readable_moment_opens(client):
    await _moment_row(client, "m-ok", "uid-bob")
    status, body = await _get(client, "m-ok")
    assert status == 200 and body["moment"]["id"] == "m-ok"


async def test_a_blocked_authors_moment_is_the_unknown_id_404(client):
    await _moment_row(client, "m-blocked", "uid-bob")
    await client._db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?,?)",
        (client._uid, "uid-bob"),
    )
    assert await _get(client, "m-blocked") == await _unknown_404(client)
    status, body = await _unknown_404(client)
    assert status == 404 and "m-never-existed" not in str(body)


async def test_a_guardian_blocked_authors_moment_is_hidden(client):
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, child_protection_enabled)"
        " VALUES('kid', 'uid-kid', 'Kid', 1)"
    )
    await client._db.enqueue(
        "INSERT INTO cp_minor_blocks(minor_user_id, blocked_user_id, blocked_by)"
        " VALUES(?,?,?)",
        ("uid-kid", client._uid, client._uid),
    )
    await _moment_row(client, "m-kid", "uid-kid")
    assert await _get(client, "m-kid") == await _unknown_404(client)


async def test_an_old_moment_opens_only_for_a_follower_or_its_author(client):
    await _moment_row(client, "m-old", "uid-bob", age_hours=30)
    assert await _get(client, "m-old") == await _unknown_404(client)
    await client._db.enqueue(
        "INSERT INTO user_follows(follower_user_id, followed_user_id) VALUES(?,?)",
        (client._uid, "uid-bob"),
    )
    assert (await _get(client, "m-old"))[0] == 200
    await _moment_row(client, "m-mine-old", client._uid, age_hours=40)
    assert (await _get(client, "m-mine-old"))[0] == 200


async def test_an_expired_moment_is_hidden_even_from_its_author(client):
    await _moment_row(client, "m-gone", client._uid, expires_in_hours=-1)
    assert await _get(client, "m-gone") == await _unknown_404(client)


async def test_beyond_the_viewers_max_hops_is_hidden(client):
    await client._db.enqueue(
        "UPDATE users SET preferences_json=? WHERE user_id=?",
        ('{"moments": {"max_hops": 1}}', client._uid),
    )
    await _moment_row(client, "m-far", "uid-bob", hop_count=2)
    assert await _get(client, "m-far") == await _unknown_404(client)


async def test_every_moment_action_for_a_non_reader_is_the_same_404(client):
    """React, clear, delete and reply on a moment the caller can't read get
    the unknown-id 404 and change nothing."""
    await _moment_row(client, "m-hidden", "uid-bob")
    await client._db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?,?)",
        (client._uid, "uid-bob"),
    )
    h = _auth(client._tok)
    expected = (await _unknown_404(client))[1]
    for method, path, body in (
        ("PUT", "/api/moments/m-hidden/reaction", {"emoji": "❤️"}),
        ("DELETE", "/api/moments/m-hidden/reaction", None),
        ("POST", "/api/moments", {"content": "re", "parent_moment_id": "m-hidden"}),
    ):
        r = await client.request(method, path, json=body, headers=h)
        assert r.status == 404, (method, path)
        assert await r.json() == expected
    row = await client._db.fetchone(
        "SELECT COUNT(*) AS n FROM moment_reactions WHERE moment_id='m-hidden'"
    )
    assert row["n"] == 0
    replies = await client._db.fetchone(
        "SELECT COUNT(*) AS n FROM moments WHERE parent_moment_id='m-hidden'"
    )
    assert replies["n"] == 0


async def test_a_member_deletes_only_what_they_can_read_and_wrote(client):
    """A non-admin: an unreadable moment is the unknown-id 404; a readable
    one by someone else is 403; their own goes."""
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES('eve','uid-eve','Eve')"
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-eve", "uid-eve", "t", sha256_token_hash("eve-tok")),
    )
    await client._db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?,?)",
        ("uid-eve", "uid-bob"),
    )
    await _moment_row(client, "m-bob", "uid-bob")
    await _moment_row(client, "m-admin", client._uid)
    await _moment_row(client, "m-eve", "uid-eve")
    eve = _auth("eve-tok")
    expected = (await _unknown_404(client))[1]
    r = await client.delete("/api/moments/m-bob", headers=eve)
    assert r.status == 404 and await r.json() == expected
    assert (await client.delete("/api/moments/m-admin", headers=eve)).status == 403
    assert (await client.delete("/api/moments/m-eve", headers=eve)).status == 200


# ── A reply never goes further than the moment it answers ───────────────


async def test_a_reply_to_a_household_moment_is_stored_not_public(client):
    r = await client.post(
        "/api/moments", json={"content": "household only"}, headers=_auth(client._tok)
    )
    root_id = (await r.json())["id"]
    # A stale composer that pre-ticks "share publicly" on the reply.
    r = await client.post(
        "/api/moments",
        json={"content": "reply", "parent_moment_id": root_id, "is_public": True},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    reply = await r.json()
    assert reply["is_public"] is False
    r = await client.get(f"/api/moments/{reply['id']}", headers=_auth(client._tok))
    assert (await r.json())["moment"]["is_public"] is False


async def test_a_reply_to_a_public_moment_may_be_public(client):
    r = await client.post(
        "/api/moments",
        json={"content": "public root", "is_public": True},
        headers=_auth(client._tok),
    )
    root = await r.json()
    assert root["is_public"] is True
    r = await client.post(
        "/api/moments",
        json={"content": "reply", "parent_moment_id": root["id"], "is_public": True},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    assert (await r.json())["is_public"] is True


# ── Protected accounts never see GFS-delivered moments (§CP.R) ──────────


async def _kid_and_gfs_moment(client) -> dict:
    """A protected member (Kid) and the admin in one household; the admin
    follows a stranger's public moments, so a GFS moment (with a hashtag
    and a GFS reply) sits household-wide next to a household moment."""
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, child_protection_enabled)"
        " VALUES('kid', 'uid-kid', 'Kid', 1)"
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-kid", "uid-kid", "t", sha256_token_hash("kid-tok")),
    )
    await client._db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
        " inbox_url, status, paired_at) VALUES('g1', 'gfs-1', 'G', 'aa',"
        " 'https://g.example', 'active', datetime('now'))"
    )
    for mid, via, content, parent in (
        ("m-house", "household", "family dinner #pasta", None),
        ("m-gfs", "gfs", "stranger moment #bees", None),
        ("m-gfs-reply", "gfs", "stranger reply", "m-house"),
    ):
        await client._db.enqueue(
            "INSERT INTO moments(id, author_user_id, content, parent_moment_id,"
            " origin_instance_id, created_at, expires_at, is_public, received_via,"
            " received_via_gfs_id) VALUES(?,?,?,?, 'inst-x', datetime('now'),"
            " datetime('now', '+1 day'), ?, ?, ?)",
            (
                mid,
                "u-stranger" if via == "gfs" else "uid-bob",
                content,
                parent,
                1 if via == "gfs" else 0,
                via,
                "g1" if via == "gfs" else None,
            ),
        )
    for mid, tag in (("m-house", "pasta"), ("m-gfs", "bees")):
        await client._db.enqueue(
            "INSERT INTO moment_hashtags(moment_id, tag) VALUES(?, ?)", (mid, tag)
        )
    return {"kid": _auth("kid-tok"), "adult": _auth(client._tok)}


async def _ids(client, path, headers) -> set[str]:
    r = await client.get(path, headers=headers)
    assert r.status == 200
    return {m["id"] for m in await r.json()}


async def test_a_protected_member_never_sees_a_gfs_moment(client):
    h = await _kid_and_gfs_moment(client)
    for path in ("/api/moments", "/api/moments/archive"):
        assert "m-gfs" in await _ids(client, path, h["adult"])
        kid = await _ids(client, path, h["kid"])
        assert "m-gfs" not in kid and "m-gfs-reply" not in kid
        assert "m-house" in kid
    # Detail: the unknown-id 404, byte for byte.
    r = await client.get("/api/moments/m-gfs", headers=h["kid"])
    unknown = await client.get("/api/moments/m-never", headers=h["kid"])
    assert r.status == 404 and await r.json() == await unknown.json()
    assert (await client.get("/api/moments/m-gfs", headers=h["adult"])).status == 200
    # Replies inside a household moment: the GFS reply is left out.
    r = await client.get("/api/moments/m-house", headers=h["kid"])
    assert [x["id"] for x in (await r.json())["replies"]] == []
    r = await client.get("/api/moments/m-house", headers=h["adult"])
    assert [x["id"] for x in (await r.json())["replies"]] == ["m-gfs-reply"]
    # Counts: the trending tags leave the GFS moment out.
    r = await client.get("/api/moments/hashtags", headers=h["kid"])
    assert {t["tag"] for t in (await r.json())["hashtags"]} == {"pasta"}
    r = await client.get("/api/moments/hashtags", headers=h["adult"])
    assert {t["tag"] for t in (await r.json())["hashtags"]} == {"pasta", "bees"}
    # Acting on it is the same 404.
    r = await client.put(
        "/api/moments/m-gfs/reaction", json={"emoji": "❤️"}, headers=h["kid"]
    )
    assert r.status == 404
