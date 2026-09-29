"""Integration tests for link previews: ``POST /api/link-preview`` and the
server-built preview on household / space post create.

The outbound fetch is mocked at its boundary (``OutboundFetcher.fetch``) —
no network; the guard itself is covered in ``tests/test_outbound_fetch.py``.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from PIL import Image

from socialhome.outbound_fetch import FetchResult, OutboundFetcher, OutboundFetchRefused

pytestmark = pytest.mark.integration

PAGE = b"""<html><head>
<meta property="og:title" content="Story title">
<meta property="og:description" content="What it is about">
<meta property="og:site_name" content="Example">
<meta property="og:image" content="https://example.com/card.png">
</head></html>"""


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 30), "green").save(buf, format="PNG")
    return buf.getvalue()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def fetched():
    calls: list[str] = []

    async def fake_fetch(self, url, *, accept, max_bytes, truncate=False):
        calls.append(url)
        if url == "https://example.com/story":
            return FetchResult(url, "text/html", "utf-8", PAGE, False)
        if url == "https://example.com/card.png":
            return FetchResult(url, "image/png", None, _png(), False)
        raise OutboundFetchRefused("status", "404")

    with patch.object(OutboundFetcher, "fetch", fake_fetch):
        yield calls


async def test_composer_preview(client, fetched):
    r = await client.post(
        "/api/link-preview",
        json={"url": "https://example.com/story"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    p = body["preview"]
    assert p["url"] == "https://example.com/story"
    assert p["title"] == "Story title"
    assert p["description"] == "What it is about"
    assert p["site_name"] == "Example"
    assert p["thumbnail_url"].startswith("api/media/")
    assert "sig=" in p["thumbnail_url"]  # signed for <img src>
    # The stored image is served by the ordinary media route.
    img = await client.get(p["thumbnail_url"])
    assert img.status == 200


async def test_composer_preview_no_card(client, fetched):
    r = await client.post(
        "/api/link-preview",
        json={"url": "https://nothing.example/"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json()) == {"preview": None}


@pytest.mark.parametrize("body", [{}, {"url": ""}, {"url": 5}, ["x"]])
async def test_composer_preview_validation(client, fetched, body):
    r = await client.post("/api/link-preview", json=body, headers=_auth(client._tok))
    assert r.status == 422
    assert fetched == []


async def test_composer_preview_requires_auth(client, fetched):
    r = await client.post(
        "/api/link-preview", json={"url": "https://example.com/story"}
    )
    assert r.status == 401
    assert fetched == []


async def test_admin_can_turn_previews_off(client, fetched):
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": {"allow_link_preview": False}},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["allow_link_preview"] is False
    r = await client.post(
        "/api/link-preview",
        json={"url": "https://example.com/story"},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "see https://example.com/story"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    assert (await r.json())["link_preview"] is None
    assert fetched == []


async def test_feed_post_carries_server_built_preview(client, fetched):
    r = await client.post(
        "/api/feed/posts",
        json={
            "type": "text",
            "content": "read this https://example.com/story",
            # A client cannot inject card fields — ignored.
            "link_preview": {"url": "https://evil.example/", "title": "Fake"},
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    post = await r.json()
    assert post["link_preview"]["title"] == "Story title"
    assert post["link_preview"]["url"] == "https://example.com/story"
    feed = await (await client.get("/api/feed", headers=_auth(client._tok))).json()
    card = feed[0]["link_preview"]
    assert card["title"] == "Story title"
    assert "sig=" in card["thumbnail_url"]


async def test_feed_post_opt_out(client, fetched):
    r = await client.post(
        "/api/feed/posts",
        json={
            "type": "text",
            "content": "read this https://example.com/story",
            "no_link_preview": True,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["link_preview"] is None
    assert body["no_link_preview"] is True
    assert fetched == []


async def test_composer_warms_cache_for_submit(client, fetched):
    await client.post(
        "/api/link-preview",
        json={"url": "https://example.com/story"},
        headers=_auth(client._tok),
    )
    before = len(fetched)
    r = await client.post(
        "/api/feed/posts",
        json={"type": "text", "content": "https://example.com/story"},
        headers=_auth(client._tok),
    )
    assert (await r.json())["link_preview"]["title"] == "Story title"
    assert len(fetched) == before  # reused, not fetched again


async def test_space_post_carries_preview(client, fetched):
    r = await client.post(
        "/api/spaces",
        json={"name": "Links", "emoji": "🔗"},
        headers=_auth(client._tok),
    )
    sid = (await r.json())["id"]
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={"type": "text", "content": "look https://example.com/story"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    r = await client.post(
        f"/api/spaces/{sid}/posts",
        json={
            "type": "text",
            "content": "no card https://example.com/story",
            "no_link_preview": True,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    feed = await (
        await client.get(f"/api/spaces/{sid}/feed", headers=_auth(client._tok))
    ).json()
    by_content = {p["content"]: p for p in feed}
    card = by_content["look https://example.com/story"]["link_preview"]
    assert card["title"] == "Story title"
    assert "sig=" in card["thumbnail_url"]
    assert by_content["no card https://example.com/story"]["link_preview"] is None
