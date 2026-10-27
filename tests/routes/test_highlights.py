"""HTTP smoke tests for the highlights routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from socialhome.app_keys import (
    federation_repo_key,
    federation_service_key,
    feed_service_key,
    highlight_repo_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.highlight import Highlight, HighlightFrame, HighlightFrameType


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


# ── People named for the viewer (#1073 follow-up) ───────────────────────
#
# The SPA named highlight authors, viewers and reactors from the local
# household cache only, so a remote friend read "Someone". The highlight
# now carries ``author`` and each view / reaction row the person's name,
# signed picture and household — added in the route only: the federation
# payloads (frame fan-out, view / reaction back-channel) are unchanged.


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


async def _remote_highlight(client, highlight_id: str, author: str) -> str:
    """Seed a highlight a paired household fanned in; returns its frame id."""
    now = datetime.now(UTC)
    repo = client.app[highlight_repo_key]
    await repo.save_highlight(
        Highlight(
            id=highlight_id,
            author_user_id=author,
            highlight_date=now.date().isoformat(),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
        )
    )
    frame_id = f"{highlight_id}-f1"
    await repo.save_frame(
        HighlightFrame(
            id=frame_id,
            highlight_id=highlight_id,
            sequence=1,
            frame_type=HighlightFrameType.IMAGE,
            media_url="/api/media/remote.webp",
            created_at=now.isoformat(),
        )
    )
    return frame_id


async def test_inbox_names_remote_and_unknown_authors(client):
    await _remote_amaka(client)
    await _remote_highlight(client, "h-amaka", "ru-amaka")
    await _remote_highlight(client, "h-ghost", "ru-forgotten")

    r = await client.get("/api/highlights", headers=_auth(client))
    assert r.status == 200
    rows = {row["highlight"]["id"]: row["highlight"] for row in await r.json()}
    amaka = rows["h-amaka"]["author"]
    assert amaka["display_name"] == "Amaka Okafor"
    assert amaka["household"] == "Okafor family"
    assert amaka["picture_url"].startswith(
        "api/users/ru-amaka/picture?v=cafebabecafebabe"
    )
    assert "sig=" in amaka["picture_url"]
    # Nobody here knows them: no name — never the id.
    assert rows["h-ghost"]["author"]["display_name"] is None
    assert rows["h-ghost"]["author"]["picture_url"] is None


async def test_detail_names_a_remote_author_for_a_viewer(client):
    await _remote_amaka(client)
    await _remote_highlight(client, "h-amaka", "ru-amaka")
    r = await client.get("/api/highlights/h-amaka", headers=_auth(client))
    assert r.status == 200
    body = await r.json()
    assert body["highlight"]["author"]["display_name"] == "Amaka Okafor"
    # Not the author: no viewer / reactor lists at all.
    assert "views" not in body
    assert "reactions" not in body


async def test_detail_names_remote_viewers_and_reactors_for_the_author(client):
    """The author's "Seen by" sheet: a remote viewer / reactor is named
    with a signed picture; a guardian-blocked kid by name only (§CP.F2);
    someone nobody knows gets no name — never the id."""
    await _remote_amaka(client)
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
    resp = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    created = await resp.json()
    highlight_id = created["highlight"]["id"]
    frame_id = created["frame"]["id"]
    repo = client.app[highlight_repo_key]
    for viewer in ("ru-amaka", "uid-kid", "ru-forgotten"):
        await repo.mark_viewed(frame_id, viewer)
    await repo.set_reaction(frame_id, "ru-amaka", "🔥")

    r = await client.get(f"/api/highlights/{highlight_id}", headers=_auth(client))
    assert r.status == 200
    body = await r.json()
    assert body["highlight"]["author"]["display_name"]
    views = {v["viewer_user_id"]: v for v in body["views"][frame_id]}
    assert views["ru-amaka"]["display_name"] == "Amaka Okafor"
    assert views["ru-amaka"]["household"] == "Okafor family"
    assert "sig=" in views["ru-amaka"]["picture_url"]
    assert views["ru-amaka"]["viewed_at"]
    assert views["uid-kid"]["display_name"] == "Kid"
    assert views["uid-kid"]["picture_url"] is None
    assert views["ru-forgotten"]["display_name"] is None
    [reaction] = body["reactions"][frame_id]
    assert reaction["reactor_user_id"] == "ru-amaka"
    assert reaction["emoji"] == "🔥"
    assert reaction["display_name"] == "Amaka Okafor"
    assert "sig=" in reaction["picture_url"]


async def test_back_channel_payloads_carry_no_names(client, monkeypatch):
    """Viewing / reacting to a remote friend's frame sends the author's
    household exactly the wire fields — never what this household knows
    about anyone."""
    await _remote_amaka(client)
    frame_id = await _remote_highlight(client, "h-amaka", "ru-amaka")
    sent: list[dict] = []
    fed = client.app[federation_service_key]

    async def _capture(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append({"to": to_instance_id, "type": event_type, "payload": payload})

    # ``FederationService`` is slotted — patch the class for this test.
    monkeypatch.setattr(type(fed), "send_event", _capture)
    r = await client.post(
        f"/api/highlights/frames/{frame_id}/view", headers=_auth(client)
    )
    assert r.status == 200
    r = await client.put(
        f"/api/highlights/frames/{frame_id}/reaction",
        json={"emoji": "🔥"},
        headers=_auth(client),
    )
    assert r.status == 200
    # Reading the highlight back named adds nothing to what went out.
    r = await client.get("/api/highlights/h-amaka", headers=_auth(client))
    assert (await r.json())["highlight"]["author"]["display_name"]

    by_type = {s["type"]: s for s in sent}
    viewed = by_type[FederationEventType.HIGHLIGHT_FRAME_VIEWED]
    assert viewed["to"] == "peer-o"
    assert set(viewed["payload"]) == {
        "highlight_id",
        "frame_id",
        "viewer_user_id",
        "author_user_id",
        "occurred_at",
    }
    reacted = by_type[FederationEventType.HIGHLIGHT_FRAME_REACTED]
    assert reacted["to"] == "peer-o"
    assert set(reacted["payload"]) == {
        "highlight_id",
        "frame_id",
        "reactor_user_id",
        "author_user_id",
        "emoji",
        "occurred_at",
    }


# ─── Audience gate (GET /api/highlights/{id} + frame sub-resources) ──────


async def _local_user(client, username: str) -> tuple[str, dict]:
    """Seed a second local member with their own token."""
    uid = f"uid-{username}"
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, state) "
        "VALUES(?, ?, ?, 'active')",
        (username, uid, username.title()),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) "
        "VALUES(?, ?, 't', ?)",
        (f"t-{username}", uid, sha256_token_hash(f"tok-{username}")),
    )
    return uid, {"Authorization": f"Bearer tok-{username}"}


async def _highlight_for(client, audience: list[str]) -> tuple[str, str]:
    """The admin posts a highlight only ``audience`` may see."""
    r = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/x.webp",
            "audience_kind": "users",
            "audience": audience,
        },
        headers=_auth(client),
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    return body["highlight"]["id"], body["frame"]["id"]


async def _not_found(client, headers: dict) -> dict:
    r = await client.get("/api/highlights/no-such-highlight", headers=headers)
    assert r.status == 404
    return await r.json()


async def test_detail_outside_the_audience_is_the_same_404_as_unknown(client):
    """Unguessable ids are not authorization: a member outside a
    ``users`` audience gets exactly the unknown-id 404."""
    bob_uid, bob = await _local_user(client, "bob")
    _, carol = await _local_user(client, "carol")
    highlight_id, _ = await _highlight_for(client, [bob_uid])

    url = f"/api/highlights/{highlight_id}"
    assert (await client.get(url, headers=bob)).status == 200
    hidden = await client.get(url, headers=carol)
    assert hidden.status == 404
    assert await hidden.json() == await _not_found(client, carol)


async def test_detail_hides_a_blocked_or_guardian_blocked_author(client):
    """The inbox's block rules hold on the detail read too."""
    bob_uid, bob = await _local_user(client, "bob")
    kid_uid, kid = await _local_user(client, "kid")
    await client._db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        (bob_uid, client._uid),
    )
    await client._db.enqueue(
        "UPDATE users SET child_protection_enabled=1 WHERE user_id=?", (kid_uid,)
    )
    await client._db.enqueue(
        "INSERT INTO cp_minor_blocks(minor_user_id, blocked_user_id, blocked_by)"
        " VALUES(?, ?, ?)",
        (kid_uid, client._uid, client._uid),
    )
    highlight_id, _ = await _highlight_for(client, [bob_uid, kid_uid])
    for headers in (bob, kid):
        r = await client.get(f"/api/highlights/{highlight_id}", headers=headers)
        assert r.status == 404


async def test_detail_of_an_expired_highlight_is_404(client):
    await _remote_amaka(client)
    await _remote_highlight(client, "h-old", "ru-amaka")
    await client._db.enqueue(
        "UPDATE highlights SET expires_at=? WHERE id='h-old'",
        ((datetime.now(UTC) - timedelta(hours=1)).isoformat(),),
    )
    r = await client.get("/api/highlights/h-old", headers=_auth(client))
    assert r.status == 404


async def test_frame_sub_resources_outside_the_audience_are_404(client):
    """View, reaction, DM reply, delete and share answer a member outside
    the audience the same 404 as an unknown id — and record nothing."""
    bob_uid, _ = await _local_user(client, "bob")
    _, carol = await _local_user(client, "carol")
    highlight_id, frame_id = await _highlight_for(client, [bob_uid])
    calls = [
        ("post", f"/api/highlights/frames/{frame_id}/view", {}),
        ("put", f"/api/highlights/frames/{frame_id}/reaction", {"emoji": "🔥"}),
        ("delete", f"/api/highlights/frames/{frame_id}/reaction", None),
        (
            "post",
            f"/api/highlights/frames/{frame_id}/dm-reply",
            {"conversation_id": "c1", "content": "hi"},
        ),
        ("delete", f"/api/highlights/frames/{frame_id}", None),
        ("delete", f"/api/highlights/{highlight_id}", None),
        ("post", f"/api/highlights/{highlight_id}/share", {"scope": "household"}),
    ]
    for method, url, body in calls:
        r = await getattr(client, method)(url, json=body, headers=carol)
        assert r.status == 404, (method, url, r.status)
        unknown = url.replace(frame_id, "no-such-frame").replace(
            highlight_id, "no-such-highlight"
        )
        u = await getattr(client, method)(unknown, json=body, headers=carol)
        assert u.status == 404, (method, unknown)
        assert await r.json() == await u.json(), (method, url)
    repo = client.app[highlight_repo_key]
    assert await repo.list_views_for_frame(frame_id) == []
    assert await repo.list_reactions_for_frame(frame_id) == []
    assert await repo.get_frame(frame_id) is not None


async def _share(client, highlight_id: str, body: dict) -> str:
    r = await client.post(
        f"/api/highlights/{highlight_id}/share", json=body, headers=_auth(client)
    )
    assert r.status == 201, await r.text()
    return (await r.json())["post_id"]


async def _space_with(client, seats: dict[str, str]) -> str:
    r = await client.post(
        "/api/spaces",
        json={"name": "Trip Group", "space_type": "private"},
        headers=_auth(client),
    )
    assert r.status == 201, await r.text()
    space_id = (await r.json())["id"]
    for uid, role in seats.items():
        await client._db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            (space_id, uid, role),
        )
    return space_id


async def test_space_share_opens_the_highlight_to_seated_readers(client):
    """A highlight shared into a space reads for every seat — a follower
    (subscriber) included — through that post; not without it, and not
    for someone with no seat."""
    member_uid, member = await _local_user(client, "member")
    follower_uid, follower = await _local_user(client, "follower")
    _, outsider = await _local_user(client, "outsider")
    space_id = await _space_with(
        client, {member_uid: "member", follower_uid: "subscriber"}
    )
    highlight_id, frame_id = await _highlight_for(client, [])
    post_id = await _share(
        client, highlight_id, {"scope": "space", "space_id": space_id}
    )
    url = f"/api/highlights/{highlight_id}"
    for headers in (member, follower):
        assert (await client.get(url, headers=headers)).status == 404
        r = await client.get(f"{url}?via_post={post_id}", headers=headers)
        assert r.status == 200, await r.text()
        body = await r.json()
        assert body["frames"][0]["id"] == frame_id
        assert "views" not in body
    r = await client.get(f"{url}?via_post={post_id}", headers=outsider)
    assert r.status == 404
    assert await r.json() == await _not_found(client, outsider)
    # The share opens the frame sub-resources too.
    r = await client.post(
        f"/api/highlights/frames/{frame_id}/view?via_post={post_id}", headers=member
    )
    assert r.status == 200
    views = await client.app[highlight_repo_key].list_views_for_frame(frame_id)
    assert [v.viewer_user_id for v in views] == [member_uid]


async def test_household_share_opens_the_highlight_to_members(client):
    _, carol = await _local_user(client, "carol")
    highlight_id, _ = await _highlight_for(client, [])
    post_id = await _share(client, highlight_id, {"scope": "household"})
    r = await client.get(
        f"/api/highlights/{highlight_id}?via_post={post_id}", headers=carol
    )
    assert r.status == 200, await r.text()


async def test_share_path_refuses_forged_deleted_and_mismatched_posts(client):
    """Only a live share post by the highlight's own author opens it, and
    the author's block rules still apply through it."""
    carol_uid, carol = await _local_user(client, "carol")
    bob_uid, bob = await _local_user(client, "bob")
    highlight_id, _ = await _highlight_for(client, [])
    feed = client.app[feed_service_key]
    forged = await feed.create_post(
        author_user_id=carol_uid,
        type="highlight_share",
        content=None,
        linked_highlight_id=highlight_id,
    )
    url = f"/api/highlights/{highlight_id}"
    for post_id in (forged.id, "no-such-post"):
        r = await client.get(f"{url}?via_post={post_id}", headers=bob)
        assert r.status == 404, post_id
    # A share of the author's OTHER highlight does not open this one.
    now = datetime.now(UTC)
    await client.app[highlight_repo_key].save_highlight(
        Highlight(
            id="h-other",
            author_user_id=client._uid,
            highlight_date=(now - timedelta(days=1)).date().isoformat(),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(),
        )
    )
    other = await feed.create_post(
        author_user_id=client._uid,
        type="highlight_share",
        content=None,
        linked_highlight_id="h-other",
    )
    r = await client.get(f"{url}?via_post={other.id}", headers=bob)
    assert r.status == 404
    real = await _share(client, highlight_id, {"scope": "household"})
    assert (await client.get(f"{url}?via_post={real}", headers=bob)).status == 200
    # Blocking the author closes the share path too.
    await client._db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?, ?)",
        (bob_uid, client._uid),
    )
    assert (await client.get(f"{url}?via_post={real}", headers=bob)).status == 404
    # A deleted share post no longer opens it.
    r = await client.delete(f"/api/feed/posts/{real}", headers=_auth(client))
    assert r.status in (200, 204), await r.text()
    assert (await client.get(f"{url}?via_post={real}", headers=carol)).status == 404


async def test_a_frame_for_other_people_than_today_is_refused(client):
    """A frame for fewer people never joins today's all-paired highlight:
    409 ``HIGHLIGHT_AUDIENCE_MISMATCH`` naming today's audience."""
    first = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/a.webp",
            "audience_kind": "all_paired",
        },
        headers=_auth(client),
    )
    assert first.status == 201, await first.text()
    resp = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/b.webp",
            "audience_kind": "users",
            "audience": ["u-anna"],
        },
        headers=_auth(client),
    )
    assert resp.status == 409
    err = (await resp.json())["error"]
    assert err["code"] == "HIGHLIGHT_AUDIENCE_MISMATCH"
    assert err["params"]["audience_kind"] == "all_paired"
    # A frame that names no audience joins today's highlight.
    joined = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/c.webp"},
        headers=_auth(client),
    )
    assert joined.status == 201
    assert (await joined.json())["frame"]["sequence"] == 2


async def test_audience_ids_without_a_kind_are_refused(client):
    """``audience`` without ``audience_kind`` would be ignored and the frame
    join a wider highlight: 422, nothing created."""
    resp = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/a.webp",
            "audience": ["u-anna"],
        },
        headers=_auth(client),
    )
    assert resp.status == 422
    assert (await resp.json())["error"]["code"] == "VALIDATION_ERROR"
    r = await client.get("/api/highlights", headers=_auth(client))
    assert await r.json() == []


# ─── Who else was picked (audience vs audience_label) ────────────────────


async def _shapes(
    client, highlight_id: str, headers: dict, *, via_post: str | None = None
) -> list[dict]:
    """The highlight object as ``headers``' owner gets it: from the inbox
    (unless reading through a share) and from the detail read."""
    shapes = []
    if via_post is None:
        r = await client.get("/api/highlights", headers=headers)
        assert r.status == 200
        shapes += [
            row["highlight"]
            for row in await r.json()
            if row["highlight"]["id"] == highlight_id
        ]
        assert len(shapes) == 1
    q = f"?via_post={via_post}" if via_post else ""
    r = await client.get(f"/api/highlights/{highlight_id}{q}", headers=headers)
    assert r.status == 200, await r.text()
    shapes.append((await r.json())["highlight"])
    return shapes


async def test_viewers_get_a_coarse_label_never_the_picked_ids(client):
    """A ``users`` highlight names its people to the author only. Each
    viewer gets ``audience_label`` — on the inbox, the detail and through
    a share card — and no ``audience`` list at all."""
    bob_uid, bob = await _local_user(client, "bob")
    carol_uid, carol = await _local_user(client, "carol")
    _, dave = await _local_user(client, "dave")
    highlight_id, _ = await _highlight_for(client, [bob_uid, carol_uid])
    post_id = await _share(client, highlight_id, {"scope": "household"})

    viewer_shapes = [
        *await _shapes(client, highlight_id, bob),
        *await _shapes(client, highlight_id, carol),
        *await _shapes(client, highlight_id, dave, via_post=post_id),
    ]
    for h in viewer_shapes:
        assert "audience" not in h
        assert h["audience_label"] == "people"
        assert bob_uid not in str(h)
        assert carol_uid not in str(h)

    # The author still sees exactly whom they picked.
    for h in await _shapes(client, highlight_id, _auth(client)):
        assert sorted(h["audience"]) == sorted([bob_uid, carol_uid])
        assert h["audience_label"] == "people"


async def test_the_frame_post_answer_names_the_audience_to_its_author(client):
    bob_uid, _ = await _local_user(client, "bob")
    r = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/x.webp",
            "audience_kind": "users",
            "audience": [bob_uid],
        },
        headers=_auth(client),
    )
    assert r.status == 201
    assert (await r.json())["highlight"]["audience"] == [bob_uid]


async def test_a_highlight_for_one_person_reads_just_you(client):
    bob_uid, bob = await _local_user(client, "bob")
    highlight_id, _ = await _highlight_for(client, [bob_uid])
    for h in await _shapes(client, highlight_id, bob):
        assert h["audience_label"] == "just_you"
        assert "audience" not in h


async def test_a_paired_households_highlight_reads_paired(client):
    await _remote_amaka(client)
    await _remote_highlight(client, "h-amaka", "ru-amaka")
    for h in await _shapes(client, "h-amaka", _auth(client)):
        assert h["audience_label"] == "paired"
        assert "audience" not in h


# ─── What the Today rows need (seen_by, video poster) ─────────────────────


async def test_the_inbox_tells_only_the_author_how_many_watched(client):
    """``seen_by`` is the author's "seen by N": people, not frame views,
    and absent from every viewer's row."""
    bob_uid, bob = await _local_user(client, "bob")
    carol_uid, carol = await _local_user(client, "carol")
    highlight_id, frame_id = await _highlight_for(client, [bob_uid, carol_uid])

    async def _row(headers: dict) -> dict:
        r = await client.get("/api/highlights", headers=headers)
        assert r.status == 200
        return next(x for x in await r.json() if x["highlight"]["id"] == highlight_id)

    assert (await _row(_auth(client)))["seen_by"] == 0
    for headers in (bob, carol, bob):
        r = await client.post(
            f"/api/highlights/frames/{frame_id}/view", headers=headers
        )
        assert r.status in (200, 204), await r.text()
    assert (await _row(_auth(client)))["seen_by"] == 2
    assert "seen_by" not in await _row(bob)


async def test_a_video_frame_carries_a_signed_poster(client):
    """Rows show a still of a video, not a 🎬: the ``.webp`` sibling of
    the transcoded ``.webm``, signed like ``media_url``. Image frames
    carry none."""
    _hid, frame_id, fn = await _create_video_frame(client)
    r = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/pic.webp"},
        headers=_auth(client),
    )
    image_frame_id = (await r.json())["frame"]["id"]
    frames = {f["id"]: f for f in await _list_frames(client)}
    poster = frames[frame_id]["thumbnail_url"]
    assert poster.split("?", 1)[0].endswith(fn.replace(".webm", ".webp"))
    assert "sig=" in poster
    assert "thumbnail_url" not in frames[image_frame_id]


# ─── Watching: your own reaction per frame ────────────────────────────────


async def test_the_detail_tells_each_viewer_their_own_reaction(client):
    """``my_reaction`` per frame is the caller's own emoji — so the
    viewer's tray shows it — never someone else's, and it follows a
    change and a clear."""
    bob_uid, bob = await _local_user(client, "bob")
    carol_uid, carol = await _local_user(client, "carol")
    highlight_id, frame_id = await _highlight_for(client, [bob_uid, carol_uid])

    async def _mine(headers: dict) -> str | None:
        r = await client.get(f"/api/highlights/{highlight_id}", headers=headers)
        assert r.status == 200, await r.text()
        (frame,) = (await r.json())["frames"]
        return frame["my_reaction"]

    assert await _mine(bob) is None
    url = f"/api/highlights/frames/{frame_id}/reaction"
    assert (await client.put(url, json={"emoji": "❤️"}, headers=bob)).status == 200
    assert await _mine(bob) == "❤️"
    assert await _mine(carol) is None
    assert await _mine(_auth(client)) is None
    assert (await client.put(url, json={"emoji": "🔥"}, headers=bob)).status == 200
    assert await _mine(bob) == "🔥"
    assert (await client.delete(url, headers=bob)).status == 200
    assert await _mine(bob) is None


# ─── PATCH /api/highlights/{id}/audience (change for all of today) ───────


async def _patch_audience(client, highlight_id: str, body: dict, headers=None):
    return await client.patch(
        f"/api/highlights/{highlight_id}/audience",
        json=body,
        headers=headers or _auth(client),
    )


async def test_the_author_changes_todays_audience(client):
    """Narrowing to one housemate: she reads it, another member no longer
    does — on every read path — and later frames join with the new
    audience."""
    bob_uid, bob = await _local_user(client, "bob")
    _, carol = await _local_user(client, "carol")
    first = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/a.webp",
            "audience_kind": "users",
            "audience": [bob_uid, "uid-carol"],
        },
        headers=_auth(client),
    )
    body = await first.json()
    highlight_id, frame_id = body["highlight"]["id"], body["frame"]["id"]
    assert (
        await client.get(f"/api/highlights/{highlight_id}", headers=carol)
    ).status == 200

    resp = await _patch_audience(
        client, highlight_id, {"audience_kind": "users", "audience": [bob_uid]}
    )
    assert resp.status == 200, await resp.text()
    out = (await resp.json())["highlight"]
    assert out["audience_kind"] == "users"
    assert out["audience"] == [bob_uid]

    assert (
        await client.get(f"/api/highlights/{highlight_id}", headers=bob)
    ).status == 200
    assert await (await client.get("/api/highlights", headers=carol)).json() == []
    for method, url, payload in (
        ("get", f"/api/highlights/{highlight_id}", None),
        ("post", f"/api/highlights/frames/{frame_id}/view", {}),
        ("put", f"/api/highlights/frames/{frame_id}/reaction", {"emoji": "🔥"}),
    ):
        r = await getattr(client, method)(url, json=payload, headers=carol)
        assert r.status == 404, (method, url, r.status)

    joined = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/b.webp",
            "audience_kind": "users",
            "audience": [bob_uid],
        },
        headers=_auth(client),
    )
    assert joined.status == 201
    assert (await joined.json())["frame"]["sequence"] == 2


async def test_only_the_author_changes_the_audience_http(client):
    bob_uid, bob = await _local_user(client, "bob")
    _, carol = await _local_user(client, "carol")
    highlight_id, _ = await _highlight_for(client, [bob_uid])
    wider = {"audience_kind": "all_paired", "audience": []}
    # Bob reads it: 403. Carol doesn't: the unknown-id 404.
    assert (await _patch_audience(client, highlight_id, wider, bob)).status == 403
    hidden = await _patch_audience(client, highlight_id, wider, carol)
    assert hidden.status == 404
    assert await hidden.json() == await _not_found(client, carol)
    unknown = await _patch_audience(client, "no-such-highlight", wider)
    assert unknown.status == 404
    detail = await (
        await client.get(f"/api/highlights/{highlight_id}", headers=_auth(client))
    ).json()
    assert detail["highlight"]["audience"] == [bob_uid]


async def test_an_earlier_days_audience_is_not_changed(client):
    bob_uid, _ = await _local_user(client, "bob")
    highlight_id, _ = await _highlight_for(client, [bob_uid])
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    await client._db.enqueue(
        "UPDATE highlights SET highlight_date=? WHERE id=?", (yesterday, highlight_id)
    )
    resp = await _patch_audience(
        client, highlight_id, {"audience_kind": "all_paired", "audience": []}
    )
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "HIGHLIGHT_NOT_TODAY"


async def test_change_audience_validates_the_body(client):
    bob_uid, _ = await _local_user(client, "bob")
    highlight_id, _ = await _highlight_for(client, [bob_uid])
    for body, status, code in (
        ({}, 400, "BAD_REQUEST"),
        ({"audience_kind": "everyone"}, 400, "BAD_REQUEST"),
        ({"audience_kind": "users", "audience": "u-1"}, 400, "BAD_REQUEST"),
        ({"audience_kind": "users", "audience": []}, 422, "HIGHLIGHT_AUDIENCE_EMPTY"),
    ):
        r = await _patch_audience(client, highlight_id, body)
        assert r.status == status, (body, r.status)
        assert (await r.json())["error"]["code"] == code


async def test_change_audience_needs_auth(client):
    r = await client.patch(
        "/api/highlights/h-1/audience", json={"audience_kind": "all_paired"}
    )
    assert r.status == 401


async def _pair(client, *peers: tuple[str, int]) -> None:
    fed_repo = client.app[federation_repo_key]
    for iid, version in peers:
        await fed_repo.save_instance(
            RemoteInstance(
                id=iid,
                display_name=iid,
                remote_identity_pk="aa" * 32,
                key_self_to_remote="k1",
                key_remote_to_self="k2",
                remote_inbox_url=f"https://{iid}/wh",
                local_inbox_id=f"wh-{iid}",
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.MANUAL,
                proto_version=version,
            )
        )


async def test_change_audience_sends_federation_to_dropped_and_new_households(
    client, monkeypatch
):
    """Wired end to end: all paired → one household drops the other."""
    await _pair(client, ("peer-bob", 70), ("peer-dora", 70))
    sent: list[tuple[str, FederationEventType]] = []

    async def _capture(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type))

    # ``FederationService`` is slotted — patch the class for this test.
    monkeypatch.setattr(
        type(client.app[federation_service_key]), "send_event", _capture
    )
    first = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/a.webp",
            "audience_kind": "all_paired",
        },
        headers=_auth(client),
    )
    highlight_id = (await first.json())["highlight"]["id"]
    sent.clear()
    resp = await _patch_audience(
        client, highlight_id, {"audience_kind": "households", "audience": ["peer-bob"]}
    )
    assert resp.status == 200
    assert sorted(sent) == sorted(
        [
            ("peer-bob", FederationEventType.HIGHLIGHT_CREATED),
            ("peer-dora", FederationEventType.HIGHLIGHT_DELETED),
        ]
    )


async def test_narrowing_away_from_an_older_household_is_409(client, monkeypatch):
    """A household below v_70 keeps no record of a removal: taking the
    highlight away from it waits (409 HIGHLIGHT_AUDIENCE_PEER_TOO_OLD,
    nothing changed, nothing sent); widening to it is never refused."""
    await _pair(client, ("peer-bob", 70), ("peer-dora", 69))
    sent: list[tuple[str, FederationEventType]] = []

    async def _capture(_self, *, to_instance_id, event_type, payload, **_kw):
        sent.append((to_instance_id, event_type))

    monkeypatch.setattr(
        type(client.app[federation_service_key]), "send_event", _capture
    )
    first = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/a.webp",
            "audience_kind": "households",
            "audience": ["peer-bob"],
        },
        headers=_auth(client),
    )
    highlight_id = (await first.json())["highlight"]["id"]
    resp = await _patch_audience(client, highlight_id, {"audience_kind": "all_paired"})
    assert resp.status == 200
    sent.clear()
    resp = await _patch_audience(
        client, highlight_id, {"audience_kind": "households", "audience": ["peer-bob"]}
    )
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "HIGHLIGHT_AUDIENCE_PEER_TOO_OLD"
    assert sent == []
    detail = await client.get(f"/api/highlights/{highlight_id}", headers=_auth(client))
    assert (await detail.json())["highlight"]["audience_kind"] == "all_paired"


async def test_changing_the_audience_is_rate_limited_per_author(client):
    """Six changes an hour per author: each one can send the day's frames
    to every newly added household, so the PATCH has its own bucket."""
    first = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/a.webp"},
        headers=_auth(client),
    )
    highlight_id = (await first.json())["highlight"]["id"]
    kinds = ["households", "all_paired"] * 3
    for kind in kinds:
        resp = await _patch_audience(
            client, highlight_id, {"audience_kind": kind, "audience": ["peer-x"]}
        )
        assert resp.status == 200, kind
    resp = await _patch_audience(client, highlight_id, {"audience_kind": "all_paired"})
    assert resp.status == 429
    # Reading it is not limited by the change bucket.
    detail = await client.get(f"/api/highlights/{highlight_id}", headers=_auth(client))
    assert detail.status == 200


async def test_the_author_reads_where_else_todays_highlight_opens(client):
    """``GET …/audience`` (author only): the audience, whether it has a public
    link, and the live shares — the household feed and each space by name —
    so the change sheet says plainly who can still open it."""
    h = _auth(client)
    r = await client.post(
        "/api/spaces",
        json={"name": "Trip Group", "space_type": "private"},
        headers=h,
    )
    space_id = (await r.json())["id"]
    r = await client.post(
        "/api/highlights/frames",
        json={
            "frame_type": "image",
            "media_url": "/api/media/x.webp",
            "audience_kind": "households",
        },
        headers=h,
    )
    highlight_id = (await r.json())["highlight"]["id"]
    resp = await client.get(f"/api/highlights/{highlight_id}/audience", headers=h)
    assert resp.status == 200
    assert await resp.json() == {
        "audience_kind": "households",
        "audience": [],
        "published": False,
        "shared": {"household": False, "spaces": []},
    }
    for body in (
        {"scope": "household"},
        {"scope": "space", "space_id": space_id},
    ):
        r = await client.post(
            f"/api/highlights/{highlight_id}/share", json=body, headers=h
        )
        assert r.status in (201, 202), await r.text()
    resp = await client.get(f"/api/highlights/{highlight_id}/audience", headers=h)
    body = await resp.json()
    assert body["shared"] == {
        "household": True,
        "spaces": [{"id": space_id, "name": "Trip Group"}],
    }


async def test_only_the_author_reads_where_it_opens(client):
    """A housemate who reads it gets 403; an unknown id the usual 404."""
    _, anna = await _local_user(client, "anna")
    r = await client.post(
        "/api/highlights/frames",
        json={"frame_type": "image", "media_url": "/api/media/x.webp"},
        headers=_auth(client),
    )
    highlight_id = (await r.json())["highlight"]["id"]
    resp = await client.get(f"/api/highlights/{highlight_id}/audience", headers=anna)
    assert resp.status == 403
    resp = await client.get("/api/highlights/nope/audience", headers=_auth(client))
    assert resp.status == 404
