"""HTTP tests for /api/storage/usage."""

from __future__ import annotations

import json


from .conftest import _auth


async def test_storage_usage_requires_auth(client):
    r = await client.get("/api/storage/usage")
    assert r.status == 401


async def test_storage_usage_zero_initially(client):
    r = await client.get("/api/storage/usage", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["used_bytes"] == 0
    assert body["quota_bytes"] > 0
    assert body["available_bytes"] == body["quota_bytes"]


async def test_storage_usage_reports_after_file_post(client):
    db = client._db
    meta = json.dumps(
        {
            "url": "/m/1",
            "mime_type": "image/png",
            "original_name": "x.png",
            "size_bytes": 1234,
        }
    )
    await db.enqueue(
        "INSERT INTO feed_posts(id, author, type, content, file_meta_json)"
        " VALUES('p1', ?, 'file', '', ?)",
        (client._uid, meta),
    )
    r = await client.get("/api/storage/usage", headers=_auth(client._tok))
    body = await r.json()
    assert body["used_bytes"] == 1234
