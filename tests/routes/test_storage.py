"""HTTP tests for /api/storage/usage."""

from __future__ import annotations

import pathlib

import orjson
import pytest

from socialhome.app_keys import instance_config_repo_key

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


async def test_the_synced_space_files_storage_is_set_with_the_quota(client):
    """Owner 2026-11-28 — "Storage for synced space files" lives with the
    household's storage settings: reported with the usage (default 2 GiB
    per space) and set by an admin through the same endpoint (0 = only
    what is opened), without touching the household quota."""
    r = await client.get("/api/storage/usage", headers=_auth(client._tok))
    body = await r.json()
    assert body["space_sync_bytes"] == 2 * 1024 * 1024 * 1024
    quota = body["quota_bytes"]
    r = await client.put(
        "/api/admin/storage/quota",
        json={"space_sync_bytes": 0},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["space_sync_bytes"] == 0
    assert body["quota_bytes"] == quota
    r = await client.put(
        "/api/admin/storage/quota",
        json={"space_sync_bytes": -1},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    r = await client.put(
        "/api/admin/storage/quota", json={}, headers=_auth(client._tok)
    )
    assert r.status == 422


async def test_the_storage_settings_are_kept_in_the_instance_config(client):
    """Owner 2026-11-28 (I5): an admin's storage settings — the quota, the
    synced files per space and their total cap — are kept in the instance
    config, so a restart starts from them."""
    r = await client.put(
        "/api/admin/storage/quota",
        json={
            "quota_bytes": 7_000_000,
            "space_sync_bytes": 1000,
            "space_sync_total_bytes": 5000,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["space_sync_total_bytes"] == 5000
    saved = await client.server.app[instance_config_repo_key].get("storage.settings")
    assert orjson.loads(saved) == {
        "quota_bytes": 7_000_000,
        "space_sync_bytes": 1000,
        "space_sync_total_bytes": 5000,
    }


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"quota_bytes": True},
        {"space_sync_bytes": 2.5},
        {"space_sync_total_bytes": "7"},
        {"space_sync_total_bytes": -1},
    ],
)
async def test_storage_settings_take_whole_numbers_only(client, body):
    r = await client.put(
        "/api/admin/storage/quota", json=body, headers=_auth(client._tok)
    )
    assert r.status == 422


async def test_only_an_admin_changes_the_storage_settings(client):
    """Fable M7: a non-admin gets 403 and nothing changes."""
    await client._db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?", (client._uid,)
    )
    r = await client.put(
        "/api/admin/storage/quota",
        json={"space_sync_bytes": 0},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    r = await client.get("/api/storage/usage", headers=_auth(client._tok))
    assert (await r.json())["space_sync_bytes"] == 2 * 1024 * 1024 * 1024
