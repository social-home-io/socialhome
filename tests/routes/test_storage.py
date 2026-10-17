"""HTTP tests for /api/storage/usage."""

from __future__ import annotations

import pathlib

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


async def test_storage_usage_reports_media_dir_bytes(client):
    # Usage reflects actual bytes on disk under the media dir (where every
    # uploaded blob lands), not post metadata — so a photo/gallery/DM file
    # counts even though it isn't a FILE-type post.
    media = pathlib.Path(client._cfg.media_path)
    (media / "gallery").mkdir(parents=True, exist_ok=True)
    (media / "avatar.webp").write_bytes(b"\0" * 1234)
    (media / "gallery" / "photo.jpg").write_bytes(b"\0" * 766)
    r = await client.get("/api/storage/usage", headers=_auth(client._tok))
    body = await r.json()
    assert body["used_bytes"] == 2000
