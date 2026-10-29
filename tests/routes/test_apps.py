"""HTTP tests for /api/apps, /api/apps/catalog, and /api/apps/{app_id}."""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiohttp
import orjson
import pytest

from socialhome.app_keys import (
    app_federation_service_key,
    app_service_key,
    instance_id_key,
    notification_delivery_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.domain.apps import (
    AppAgeRestrictedError,
    AppAlreadyInstalledError,
    AppBundleTooLargeError,
    AppCatalogEntry,
    AppContactNotFoundError,
    AppDownloadError,
    AppIntegrityError,
    AppManifest,
    AppNotEnabledError,
    AppNotFoundError,
    AppPendingSession,
    InstalledApp,
)
from socialhome.repositories.app_repo import SqliteAppRepo
from socialhome.services.app_federation_service import AppFederationService
from socialhome.services.app_service import AppService

from .conftest import _auth, client_with_stubs

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_installed(
    app_id: str = "com.example.hello",
    enabled: bool = True,
) -> InstalledApp:
    manifest = AppManifest(entry="index.js", icon="icon.png", capabilities=("read",))
    return InstalledApp(
        app_id=app_id,
        name="Hello App",
        version="1.0.0",
        enabled=enabled,
        manifest=manifest,
        bundle_path=f"apps/{app_id}/1.0.0",
        bundle_sha256="abc123",
        source_url="https://example.com/hello.tgz",
        installed_by="admin",
        installed_at="2026-06-01T00:00:00+00:00",
    )


def _make_catalog_entry(app_id: str = "com.example.new") -> AppCatalogEntry:
    return AppCatalogEntry(
        app_id=app_id,
        name="New App",
        latest_version="2.0.0",
        description="A test app",
        icon_url="https://example.com/icon.png",
        capabilities=("read", "write"),
        bundle_url="https://example.com/new.tgz",
        bundle_sha256="def456",
    )


async def _seed_member(db, username: str = "bob", user_id: str = "bob-id") -> str:
    """Seed a non-admin user and return their raw token."""
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (username, user_id, username.capitalize()),
    )
    raw = f"{username}-raw-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"tok-{username}", user_id, "t", sha256_token_hash(raw)),
    )
    return raw


# ── GET /api/apps ─────────────────────────────────────────────────────────


async def test_list_apps_empty_as_admin(client):
    r = await client.get("/api/apps", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body == {"apps": []}


async def test_list_apps_empty_as_member(client):
    """Non-admins can list apps too, but only enabled ones (empty → empty)."""
    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps", headers=_auth(member_tok))
    assert r.status == 200
    body = await r.json()
    assert body == {"apps": []}


async def test_list_apps_requires_auth(client):
    r = await client.get("/api/apps")
    assert r.status == 401


async def test_list_apps_admin_sees_disabled(client, monkeypatch):
    """Admin sees all apps including disabled ones via list_visible."""
    disabled_app = _make_installed(enabled=False)
    monkeypatch.setattr(
        AppService, "list_visible", AsyncMock(return_value=[disabled_app])
    )

    r = await client.get("/api/apps", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert len(body["apps"]) == 1
    assert body["apps"][0]["enabled"] is False


async def test_list_apps_member_filters_disabled(client, monkeypatch):
    """Non-admin only sees apps list_visible returns (enabled + age-allowed)."""
    enabled_app = _make_installed(app_id="com.example.enabled", enabled=True)
    # list_visible already filters disabled — only enabled_app is returned
    monkeypatch.setattr(
        AppService,
        "list_visible",
        AsyncMock(return_value=[enabled_app]),
    )

    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps", headers=_auth(member_tok))
    assert r.status == 200
    body = await r.json()
    ids = [a["app_id"] for a in body["apps"]]
    assert "com.example.enabled" in ids
    assert "com.example.disabled" not in ids


# ── POST /api/apps ────────────────────────────────────────────────────────


async def test_install_requires_admin(client):
    """Non-admin POST → 403."""
    member_tok = await _seed_member(client._db)
    r = await client.post(
        "/api/apps",
        json={"app_id": "com.example.hello"},
        headers=_auth(member_tok),
    )
    assert r.status == 403


async def test_install_requires_auth(client):
    r = await client.post("/api/apps", json={"app_id": "com.example.hello"})
    assert r.status == 401


async def test_install_rejects_missing_app_id(client):
    r = await client.post("/api/apps", json={}, headers=_auth(client._tok))
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_install_rejects_empty_app_id(client):
    r = await client.post("/api/apps", json={"app_id": ""}, headers=_auth(client._tok))
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_install_rejects_non_string_app_id(client):
    r = await client.post("/api/apps", json={"app_id": 123}, headers=_auth(client._tok))
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_install_not_found_returns_404(client, monkeypatch):
    monkeypatch.setattr(
        AppService,
        "install",
        AsyncMock(side_effect=AppNotFoundError("not in catalog")),
    )
    r = await client.post(
        "/api/apps",
        json={"app_id": "com.example.missing"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_install_already_installed_returns_409(client, monkeypatch):
    monkeypatch.setattr(
        AppService,
        "install",
        AsyncMock(side_effect=AppAlreadyInstalledError("already there")),
    )
    r = await client.post(
        "/api/apps",
        json={"app_id": "com.example.hello"},
        headers=_auth(client._tok),
    )
    assert r.status == 409
    body = await r.json()
    assert body["error"]["code"] == "CONFLICT"


async def test_install_integrity_error_returns_400(client, monkeypatch):
    monkeypatch.setattr(
        AppService,
        "install",
        AsyncMock(side_effect=AppIntegrityError("sha256 mismatch")),
    )
    r = await client.post(
        "/api/apps",
        json={"app_id": "com.example.hello"},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (AppBundleTooLargeError(), 422, "APP_BUNDLE_TOO_LARGE"),
        (AppDownloadError(), 502, "APP_DOWNLOAD_FAILED"),
    ],
)
async def test_install_oversized_or_undownloadable_bundle_has_a_clear_code(
    client, monkeypatch, exc, status, code
):
    """The admin UI turns these codes into a translated line (apiErrors.ts)
    instead of a generic failure or a 500."""
    monkeypatch.setattr(AppService, "install", AsyncMock(side_effect=exc))
    r = await client.post(
        "/api/apps", json={"app_id": "chess"}, headers=_auth(client._tok)
    )
    assert r.status == status
    assert (await r.json())["error"]["code"] == code


async def test_install_success_returns_201(client, monkeypatch):
    app = _make_installed()
    monkeypatch.setattr(AppService, "install", AsyncMock(return_value=app))

    r = await client.post(
        "/api/apps",
        json={"app_id": "com.example.hello"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body["app_id"] == "com.example.hello"
    assert body["name"] == "Hello App"
    assert body["version"] == "1.0.0"
    assert body["enabled"] is True
    assert body["capabilities"] == ["read"]
    assert body["icon"] == "icon.png"


# ── GET /api/apps/catalog ────────────────────────────────────────────────


async def test_catalog_is_admin_only(client):
    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps/catalog", headers=_auth(member_tok))
    assert r.status == 403


async def test_catalog_requires_auth(client):
    r = await client.get("/api/apps/catalog")
    assert r.status == 401


async def test_admin_can_list_catalog_empty(client, monkeypatch):
    """When no catalog is configured, browse_catalog returns an empty list."""
    monkeypatch.setattr(AppService, "browse_catalog", AsyncMock(return_value=[]))
    r = await client.get("/api/apps/catalog", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body == {"apps": [], "enabled": True, "checked_at": None, "stale": False}


async def test_catalog_turned_off_answers_disabled_not_an_error(client):
    """``apps_catalog_url = ""`` (no catalog service): browse says the
    catalog is off, update checks find nothing (refresh included) and an
    install is refused with a stable code — never a 500."""
    client.app[app_service_key]._catalog = None
    r = await client.get("/api/apps/catalog", headers=_auth(client._tok))
    assert r.status == 200
    assert await r.json() == {
        "apps": [],
        "enabled": False,
        "checked_at": None,
        "stale": False,
    }
    r = await client.get("/api/apps/updates?refresh=1", headers=_auth(client._tok))
    assert r.status == 200
    assert await r.json() == {"updates": [], "checked_at": None, "stale": False}
    r = await client.post(
        "/api/apps", json={"app_id": "chess"}, headers=_auth(client._tok)
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "APP_CATALOG_DISABLED"


async def test_admin_can_list_catalog_with_entry(client, monkeypatch):
    entry = _make_catalog_entry()
    monkeypatch.setattr(AppService, "browse_catalog", AsyncMock(return_value=[entry]))

    r = await client.get("/api/apps/catalog", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert len(body["apps"]) == 1
    item = body["apps"][0]
    assert item["app_id"] == "com.example.new"
    assert item["name"] == "New App"
    assert item["latest_version"] == "2.0.0"
    assert item["description"] == "A test app"
    assert item["icon_url"] == "https://example.com/icon.png"
    assert item["capabilities"] == ["read", "write"]


# ── GET /api/apps/{app_id} ───────────────────────────────────────────────


async def test_detail_404_for_unknown(client):
    r = await client.get("/api/apps/nope", headers=_auth(client._tok))
    assert r.status == 404
    body = await r.json()
    assert body["error"]["code"] == "NOT_FOUND"


async def test_detail_requires_auth(client):
    r = await client.get("/api/apps/some-app")
    assert r.status == 401


async def test_detail_member_404_for_disabled(client, monkeypatch):
    disabled_app = _make_installed(enabled=False)
    monkeypatch.setattr(AppService, "get", AsyncMock(return_value=disabled_app))

    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps/com.example.hello", headers=_auth(member_tok))
    assert r.status == 404


async def test_detail_admin_sees_disabled(client, monkeypatch):
    disabled_app = _make_installed(enabled=False)
    monkeypatch.setattr(AppService, "get", AsyncMock(return_value=disabled_app))

    r = await client.get("/api/apps/com.example.hello", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["enabled"] is False


async def test_detail_member_sees_enabled(client, monkeypatch):
    enabled_app = _make_installed(enabled=True)
    monkeypatch.setattr(AppService, "get", AsyncMock(return_value=enabled_app))

    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps/com.example.hello", headers=_auth(member_tok))
    assert r.status == 200
    body = await r.json()
    assert body["enabled"] is True


async def test_detail_minor_gets_404_for_age_restricted_app(client, monkeypatch):
    """An under-age protected minor GETs a restricted app → 404 (not 403).

    The route must not confirm the app's existence to a minor — returning 403
    would reveal that the app exists, which is a privacy / enumeration leak.
    """
    enabled_app = _make_installed(enabled=True)
    monkeypatch.setattr(AppService, "get", AsyncMock(return_value=enabled_app))
    monkeypatch.setattr(
        AppService,
        "assert_age_allowed",
        AsyncMock(
            side_effect=AppAgeRestrictedError("This app is restricted to ages 13+.")
        ),
    )

    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps/com.example.hello", headers=_auth(member_tok))
    assert r.status == 404
    body = await r.json()
    assert body["error"]["code"] == "NOT_FOUND"


async def test_detail_admin_sees_age_restricted_app(client, monkeypatch):
    """An admin can always GET a restricted app — the age gate is non-admin only."""
    enabled_app = _make_installed(enabled=True)
    monkeypatch.setattr(AppService, "get", AsyncMock(return_value=enabled_app))
    # assert_age_allowed should NOT be called for admin, so configure it to
    # raise to verify it's not called.
    age_mock = AsyncMock(side_effect=AppAgeRestrictedError("should not be called"))
    monkeypatch.setattr(AppService, "assert_age_allowed", age_mock)

    r = await client.get("/api/apps/com.example.hello", headers=_auth(client._tok))
    assert r.status == 200
    age_mock.assert_not_awaited()


# ── PATCH /api/apps/{app_id} ──────────────────────────────────────────────


async def test_patch_requires_admin(client):
    member_tok = await _seed_member(client._db)
    r = await client.patch(
        "/api/apps/com.example.hello",
        json={"enabled": False},
        headers=_auth(member_tok),
    )
    assert r.status == 403


async def test_patch_requires_auth(client):
    r = await client.patch("/api/apps/com.example.hello", json={"enabled": False})
    assert r.status == 401


async def test_patch_rejects_non_bool_enabled(client):
    r = await client.patch(
        "/api/apps/com.example.hello",
        json={"enabled": "yes"},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_patch_missing_fields_returns_400(client):
    """PATCH with neither enabled nor min_age returns 400."""
    r = await client.patch(
        "/api/apps/com.example.hello",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_patch_not_found_returns_404(client, monkeypatch):
    monkeypatch.setattr(
        AppService,
        "set_enabled",
        AsyncMock(side_effect=AppNotFoundError("not installed")),
    )
    r = await client.patch(
        "/api/apps/com.example.missing",
        json={"enabled": True},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_patch_set_enabled_success(client, monkeypatch):
    updated = _make_installed(enabled=False)
    monkeypatch.setattr(AppService, "set_enabled", AsyncMock(return_value=updated))

    r = await client.patch(
        "/api/apps/com.example.hello",
        json={"enabled": False},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["enabled"] is False


# ── DELETE /api/apps/{app_id} ─────────────────────────────────────────────


async def test_delete_requires_admin(client):
    member_tok = await _seed_member(client._db)
    r = await client.delete(
        "/api/apps/com.example.hello",
        headers=_auth(member_tok),
    )
    assert r.status == 403


async def test_delete_requires_auth(client):
    r = await client.delete("/api/apps/com.example.hello")
    assert r.status == 401


async def test_delete_not_found_returns_404(client, monkeypatch):
    monkeypatch.setattr(
        AppService,
        "uninstall",
        AsyncMock(side_effect=AppNotFoundError("not installed")),
    )
    r = await client.delete(
        "/api/apps/com.example.missing",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_delete_success_returns_ok(client, monkeypatch):
    monkeypatch.setattr(AppService, "uninstall", AsyncMock(return_value=None))

    r = await client.delete(
        "/api/apps/com.example.hello",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"status": "ok"}


# ── Store helpers ─────────────────────────────────────────────────────────


def _make_installed_no_installer(
    app_id: str = "com.example.hello",
    enabled: bool = True,
) -> InstalledApp:
    """Like ``_make_installed`` but with ``installed_by=None`` to avoid FK issues."""
    manifest = AppManifest(entry="index.js", icon="icon.png", capabilities=("read",))
    return InstalledApp(
        app_id=app_id,
        name="Hello App",
        version="1.0.0",
        enabled=enabled,
        manifest=manifest,
        bundle_path=f"apps/{app_id}/1.0.0",
        bundle_sha256="abc123",
        source_url="https://example.com/hello.tgz",
        installed_by=None,
        installed_at="2026-06-01T00:00:00+00:00",
    )


async def _seed_installed_enabled_app(db, app_id: str = "com.example.hello") -> None:
    """Insert an installed+enabled app row directly via SqliteAppRepo.

    Uses ``installed_by=None`` to avoid a FK constraint on the users table
    (the installer user may not have been seeded yet at call time).
    """
    repo = SqliteAppRepo(db)
    app = _make_installed_no_installer(app_id=app_id, enabled=True)
    await repo.install(app)


async def _seed_second_user(
    db, username: str = "carol", user_id: str = "carol-id"
) -> str:
    """Seed a second non-admin user and return their raw token."""
    return await _seed_member(db, username=username, user_id=user_id)


# ── GET /api/apps/{app_id}/store ────────────────────────────────────────


async def test_store_list_empty_for_new_user(client):
    """GET /store returns empty items dict when no keys have been set."""
    await _seed_installed_enabled_app(client._db)
    r = await client.get(
        "/api/apps/com.example.hello/store",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"items": {}}


async def test_store_list_requires_auth(client):
    await _seed_installed_enabled_app(client._db)
    r = await client.get("/api/apps/com.example.hello/store")
    assert r.status == 401


# ── PUT + GET /api/apps/{app_id}/store/{key} ────────────────────────────


async def test_store_put_then_get(client):
    """PUT a value then GET it back → 200 with the same value."""
    await _seed_installed_enabled_app(client._db)

    value = {"turn": "w"}
    r = await client.put(
        "/api/apps/com.example.hello/store/game1",
        json={"value": value},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    put_body = await r.json()
    assert put_body == {"key": "game1", "value": value}

    r2 = await client.get(
        "/api/apps/com.example.hello/store/game1",
        headers=_auth(client._tok),
    )
    assert r2.status == 200
    get_body = await r2.json()
    assert get_body == {"key": "game1", "value": value}


async def test_store_get_missing_key_404(client):
    """GET a key that was never set → 404 NOT_FOUND."""
    await _seed_installed_enabled_app(client._db)
    r = await client.get(
        "/api/apps/com.example.hello/store/no-such-key",
        headers=_auth(client._tok),
    )
    assert r.status == 404
    body = await r.json()
    assert body["error"]["code"] == "NOT_FOUND"


async def test_store_on_uninstalled_app_404(client):
    """Store op on an app_id not installed → 404 via AppNotFoundError."""
    r = await client.get(
        "/api/apps/com.example.not-installed/store/key",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_store_list_scoped_to_caller(client):
    """User A's PUT does NOT appear in user B's GET /store.

    We seed a second user (carol) and verify that after user A (admin) PUTs
    a key, user B's store list is empty — store data is per-user-scoped.
    """
    await _seed_installed_enabled_app(client._db)

    # User A (admin) puts a key
    r = await client.put(
        "/api/apps/com.example.hello/store/shared-key",
        json={"value": "user-a-data"},
        headers=_auth(client._tok),
    )
    assert r.status == 200

    # User B (carol) lists their own store — must be empty
    carol_tok = await _seed_second_user(client._db)
    r2 = await client.get(
        "/api/apps/com.example.hello/store",
        headers=_auth(carol_tok),
    )
    assert r2.status == 200
    body = await r2.json()
    # carol's store has no keys — user A's data is invisible
    assert body == {"items": {}}


async def test_store_value_too_large_413(client):
    """PUT a value exceeding the byte quota → 413 QUOTA_EXCEEDED."""
    await _seed_installed_enabled_app(client._db)

    big_value = {"x": "a" * 70000}
    r = await client.put(
        "/api/apps/com.example.hello/store/big-key",
        json={"value": big_value},
        headers=_auth(client._tok),
    )
    assert r.status == 413
    body = await r.json()
    assert body["error"]["code"] == "QUOTA_EXCEEDED"


async def test_store_delete(client):
    """PUT then DELETE then GET → 404 NOT_FOUND."""
    await _seed_installed_enabled_app(client._db)

    # PUT the key
    await client.put(
        "/api/apps/com.example.hello/store/temp-key",
        json={"value": "to-be-deleted"},
        headers=_auth(client._tok),
    )

    # DELETE
    r_del = await client.delete(
        "/api/apps/com.example.hello/store/temp-key",
        headers=_auth(client._tok),
    )
    assert r_del.status == 200
    body = await r_del.json()
    assert body == {"status": "ok"}

    # GET after delete → 404
    r_get = await client.get(
        "/api/apps/com.example.hello/store/temp-key",
        headers=_auth(client._tok),
    )
    assert r_get.status == 404


async def test_store_on_disabled_app_403(client, monkeypatch):
    """Store op on a disabled app → 403 APP_NOT_ENABLED (AppNotEnabledError)."""
    await _seed_installed_enabled_app(client._db)
    monkeypatch.setattr(
        AppService,
        "store_get",
        AsyncMock(side_effect=AppNotEnabledError("App disabled")),
    )
    r = await client.get(
        "/api/apps/com.example.hello/store/any-key",
        headers=_auth(client._tok),
    )
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "APP_NOT_ENABLED"


async def test_store_put_missing_value_field_400(client):
    """PUT without a 'value' field → 400 UNPROCESSABLE."""
    await _seed_installed_enabled_app(client._db)
    r = await client.put(
        "/api/apps/com.example.hello/store/some-key",
        json={"not_value": 42},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_store_preserves_sensitive_like_keys(client):
    """PUT a value dict whose keys overlap SENSITIVE_FIELDS — GET must round-trip intact.

    ``sanitise_for_api`` strips keys such as ``signature`` and ``endpoint`` from
    any nested dict.  App KV values are opaque user data — stripping them silently
    corrupts reads.  The store views must bypass sanitisation and pass the value
    through untouched (web.json_response instead of self._json).
    """
    await _seed_installed_enabled_app(client._db)

    # Use two SENSITIVE_FIELDS members so the test is robust.
    payload = {
        "signature": "abc123",
        "endpoint": "https://push.example.com",
        "score": 5,
    }

    r_put = await client.put(
        "/api/apps/com.example.hello/store/prefs",
        json={"value": payload},
        headers=_auth(client._tok),
    )
    assert r_put.status == 200
    put_body = await r_put.json()
    assert put_body["value"] == payload, (
        "PUT response must not strip sensitive-like keys from the value"
    )

    r_get = await client.get(
        "/api/apps/com.example.hello/store/prefs",
        headers=_auth(client._tok),
    )
    assert r_get.status == 200
    get_body = await r_get.json()
    assert get_body["value"] == payload, (
        "GET response must not strip sensitive-like keys from the value"
    )

    # Verify the collection view also preserves the full value.
    r_list = await client.get(
        "/api/apps/com.example.hello/store",
        headers=_auth(client._tok),
    )
    assert r_list.status == 200
    list_body = await r_list.json()
    assert list_body["items"]["prefs"] == payload, (
        "GET /store (collection) must not strip sensitive-like keys from values"
    )


async def test_store_key_too_long_413(client):
    """PUT to a key longer than 256 chars → 413 QUOTA_EXCEEDED."""
    await _seed_installed_enabled_app(client._db)
    long_key = "k" * 257
    r = await client.put(
        f"/api/apps/com.example.hello/store/{long_key}",
        json={"value": {"ok": True}},
        headers=_auth(client._tok),
    )
    assert r.status == 413
    body = await r.json()
    assert body["error"]["code"] == "QUOTA_EXCEEDED"


# ── App federation routes ─────────────────────────────────────────────────
#
# AppFederationService is wired in Task 6.  For these route tests we inject a
# stub AsyncMock directly into the test app dict so the views can resolve the
# service key without a real federation layer.


@pytest.fixture
def fed_svc():
    """A stub AppFederationService; see the module's ``client``."""
    return AsyncMock(spec=AppFederationService)


@pytest.fixture
async def client(request, make_client):
    """``client`` starts with :func:`fed_svc` swapped in when a test asks
    for it."""
    return await client_with_stubs(
        request, make_client, {"fed_svc": app_federation_service_key}
    )


# ── GET /api/apps/{app_id}/peers ──────────────────────────────────────────


async def test_peers_requires_auth(client):
    r = await client.get("/api/apps/com.example.hello/peers")
    assert r.status == 401


async def test_peers_returns_peer_list(client, fed_svc):
    peers = [
        {"instance_id": "peer.example.com", "display_name": "Example Peer"},
        {"instance_id": "other.example.net", "display_name": "Other"},
    ]
    fed_svc.list_peers.return_value = peers

    r = await client.get(
        "/api/apps/com.example.hello/peers",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"peers": peers}
    fed_svc.list_peers.assert_awaited_once()


async def test_peers_member_can_list(client, fed_svc):
    """Non-admin members can also list peers."""
    fed_svc.list_peers.return_value = []
    member_tok = await _seed_member(client._db)

    r = await client.get(
        "/api/apps/com.example.hello/peers",
        headers=_auth(member_tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"peers": []}


# ── GET /api/apps/{app_id}/contacts ──────────────────────────────────────


async def test_contacts_requires_auth(client):
    r = await client.get("/api/apps/com.example.hello/contacts")
    assert r.status == 401


async def test_get_contacts_returns_contact_shape(client, fed_svc):
    contacts = [
        {
            "instance_id": "peer.example.com",
            "user_ref": "alice@peer.example.com",
            "display_name": "Alice",
            "is_local": False,
            "online": True,
        },
        {
            "instance_id": "local",
            "user_ref": "bob@local",
            "display_name": "Bob",
            "is_local": True,
            "online": False,
        },
    ]
    fed_svc.list_contacts.return_value = contacts

    r = await client.get(
        "/api/apps/com.example.hello/contacts",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert isinstance(body["contacts"], list)
    for c in body["contacts"]:
        assert set(c.keys()) == {
            "instance_id",
            "user_ref",
            "display_name",
            "is_local",
            "online",
        }
    fed_svc.list_contacts.assert_awaited_once()


async def test_contacts_member_can_list(client, fed_svc):
    """Non-admin members can also list contacts."""
    fed_svc.list_contacts.return_value = []
    member_tok = await _seed_member(client._db)

    r = await client.get(
        "/api/apps/com.example.hello/contacts",
        headers=_auth(member_tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"contacts": []}


# ── POST /api/apps/{app_id}/sessions ─────────────────────────────────────


async def test_sessions_requires_auth(client):
    r = await client.post(
        "/api/apps/com.example.hello/sessions",
        json={"peer_instance_id": "peer.example.com"},
    )
    assert r.status == 401


async def test_sessions_returns_session_id_201(client, fed_svc):
    """A ``target`` body opens a person-addressed session and returns 201."""
    fed_svc.open_session.return_value = "deadbeef01234567"

    target = {
        "instance_id": "peer.example.com",
        "user_ref": "bob",
        "is_local": False,
    }
    r = await client.post(
        "/api/apps/com.example.hello/sessions",
        json={"target": target},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body == {"session_id": "deadbeef01234567"}
    fed_svc.open_session.assert_awaited_once_with(
        app_id="com.example.hello",
        target=target,
        actor_user_id=client._uid,
    )


async def test_sessions_legacy_peer_instance_id_still_works(client, fed_svc):
    """A legacy ``peer_instance_id`` body maps to a remote household target."""
    fed_svc.open_session.return_value = "cafebabe89abcdef"

    r = await client.post(
        "/api/apps/com.example.hello/sessions",
        json={"peer_instance_id": "peer.example.com"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    body = await r.json()
    assert body == {"session_id": "cafebabe89abcdef"}
    fed_svc.open_session.assert_awaited_once_with(
        app_id="com.example.hello",
        target={
            "instance_id": "peer.example.com",
            "user_ref": "",
            "is_local": False,
        },
        actor_user_id=client._uid,
    )


async def test_sessions_rejects_malformed_target(client, fed_svc):
    """A target missing is_local / user_ref is rejected with 400."""
    r = await client.post(
        "/api/apps/com.example.hello/sessions",
        json={"target": {"instance_id": "peer.example.com"}},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_sessions_rejects_missing_peer(client, fed_svc):
    r = await client.post(
        "/api/apps/com.example.hello/sessions",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


# ── POST /api/apps/{app_id}/sessions/{session_id}/accept ─────────────────


async def test_accept_requires_auth(client):
    r = await client.post("/api/apps/com.example.hello/sessions/s1/accept")
    assert r.status == 401


async def test_accept_returns_the_invite_scoped_to_the_caller(client, fed_svc):
    """The invitee is the authed user — never a request param."""
    fed_svc.accept_session.return_value = AppPendingSession(
        app_id="com.example.hello",
        user_id=client._uid,
        session_id="sess-1",
        from_instance="peer.example.com",
        from_user="alice",
        payload={"verb": "open"},
        created_at="2026-06-04T12:00:00+00:00",
    )
    r = await client.post(
        "/api/apps/com.example.hello/sessions/sess-1/accept",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == {
        "session": {
            "session_id": "sess-1",
            "from_instance": "peer.example.com",
            "from_user": "alice",
            "payload": {"verb": "open"},
        }
    }
    fed_svc.accept_session.assert_awaited_once_with(
        app_id="com.example.hello", session_id="sess-1", actor_user_id=client._uid
    )


async def test_the_blanket_pending_sessions_drain_is_gone(client):
    """Opening an app accepts nothing: there is no endpoint that takes in
    every waiting invite at once."""
    r = await client.get(
        "/api/apps/com.example.hello/pending-sessions", headers=_auth(client._tok)
    )
    assert r.status == 404


async def test_sessions_rejects_empty_peer(client, fed_svc):
    r = await client.post(
        "/api/apps/com.example.hello/sessions",
        json={"peer_instance_id": ""},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_sessions_app_not_found_404(client, fed_svc):
    fed_svc.open_session.side_effect = AppNotFoundError("not installed")

    r = await client.post(
        "/api/apps/com.example.missing/sessions",
        json={"peer_instance_id": "peer.example.com"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_sessions_app_not_enabled_403(client, fed_svc):
    fed_svc.open_session.side_effect = AppNotEnabledError("disabled")

    r = await client.post(
        "/api/apps/com.example.hello/sessions",
        json={"peer_instance_id": "peer.example.com"},
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ── POST /api/apps/{app_id}/messages ─────────────────────────────────────


async def test_messages_requires_auth(client):
    r = await client.post(
        "/api/apps/com.example.hello/messages",
        json={
            "session_id": "s1",
            "peer_instance_id": "peer.example.com",
            "payload": {},
        },
    )
    assert r.status == 401


async def test_messages_returns_ok(client, fed_svc):
    """A ``target`` body sends a person-addressed message and returns ok."""
    fed_svc.send_message.return_value = None

    target = {
        "instance_id": "peer.example.com",
        "user_ref": "bob",
        "is_local": False,
    }
    r = await client.post(
        "/api/apps/com.example.hello/messages",
        json={
            "session_id": "sess-abc",
            "target": target,
            "payload": {"move": "e4"},
        },
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"ok": True}
    fed_svc.send_message.assert_awaited_once_with(
        app_id="com.example.hello",
        target=target,
        session_id="sess-abc",
        payload={"move": "e4"},
        actor_user_id=client._uid,
    )


async def test_messages_legacy_peer_instance_id_still_works(client, fed_svc):
    """A legacy ``peer_instance_id`` body maps to a household-addressed target."""
    fed_svc.send_message.return_value = None

    r = await client.post(
        "/api/apps/com.example.hello/messages",
        json={
            "session_id": "sess-abc",
            "peer_instance_id": "peer.example.com",
            "payload": {"move": "e4"},
        },
        headers=_auth(client._tok),
    )
    assert r.status == 200
    fed_svc.send_message.assert_awaited_once_with(
        app_id="com.example.hello",
        target={
            "instance_id": "peer.example.com",
            "user_ref": "",
            "is_local": False,
        },
        session_id="sess-abc",
        payload={"move": "e4"},
        actor_user_id=client._uid,
    )


async def test_messages_rejects_malformed_target(client, fed_svc):
    """A target missing is_local / user_ref is rejected with 400."""
    r = await client.post(
        "/api/apps/com.example.hello/messages",
        json={
            "session_id": "s1",
            "target": {"instance_id": "peer.example.com"},
            "payload": {},
        },
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_sessions_non_contact_target_forbidden(client, fed_svc):
    """Opening a session with a non-contact target → 403 FORBIDDEN."""
    fed_svc.open_session.side_effect = AppContactNotFoundError("not a contact")

    r = await client.post(
        "/api/apps/com.example.hello/sessions",
        json={
            "target": {
                "instance_id": "peer.example.com",
                "user_ref": "stranger",
                "is_local": False,
            }
        },
        headers=_auth(client._tok),
    )
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "FORBIDDEN"


async def test_messages_non_contact_target_forbidden(client, fed_svc):
    """Sending a message to a non-contact target → 403 FORBIDDEN."""
    fed_svc.send_message.side_effect = AppContactNotFoundError("not a contact")

    r = await client.post(
        "/api/apps/com.example.hello/messages",
        json={
            "session_id": "s1",
            "target": {
                "instance_id": "peer.example.com",
                "user_ref": "stranger",
                "is_local": False,
            },
            "payload": {"move": "e4"},
        },
        headers=_auth(client._tok),
    )
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "FORBIDDEN"


async def test_messages_rejects_missing_session_id(client, fed_svc):
    r = await client.post(
        "/api/apps/com.example.hello/messages",
        json={"peer_instance_id": "peer.example.com", "payload": {}},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_messages_rejects_missing_peer(client, fed_svc):
    r = await client.post(
        "/api/apps/com.example.hello/messages",
        json={"session_id": "s1", "payload": {}},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_messages_rejects_missing_payload(client, fed_svc):
    r = await client.post(
        "/api/apps/com.example.hello/messages",
        json={"session_id": "s1", "peer_instance_id": "peer.example.com"},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_messages_rejects_oversized_payload(client, fed_svc):
    # Build a payload that exceeds 256 KiB when JSON-encoded.
    big_payload = {"data": "x" * (256 * 1024 + 1)}

    r = await client.post(
        "/api/apps/com.example.hello/messages",
        json={
            "session_id": "s1",
            "peer_instance_id": "peer.example.com",
            "payload": big_payload,
        },
        headers=_auth(client._tok),
    )
    assert r.status == 413
    body = await r.json()
    assert body["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_messages_app_not_found_404(client, fed_svc):
    fed_svc.send_message.side_effect = AppNotFoundError("not installed")

    r = await client.post(
        "/api/apps/com.example.missing/messages",
        json={"session_id": "s1", "peer_instance_id": "p.example.com", "payload": {}},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_messages_app_not_enabled_403(client, fed_svc):
    fed_svc.send_message.side_effect = AppNotEnabledError("disabled")

    r = await client.post(
        "/api/apps/com.example.hello/messages",
        json={"session_id": "s1", "peer_instance_id": "p.example.com", "payload": {}},
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ── Age gate route tests ──────────────────────────────────────────────────────


async def test_patch_set_min_age_admin_success(client, monkeypatch):
    """PATCH {min_age: 13} as admin → 200 with min_age in response."""
    updated = _make_installed()
    # Use a version that carries min_age=13
    updated_with_age = InstalledApp(
        app_id=updated.app_id,
        name=updated.name,
        version=updated.version,
        enabled=updated.enabled,
        manifest=updated.manifest,
        bundle_path=updated.bundle_path,
        bundle_sha256=updated.bundle_sha256,
        source_url=updated.source_url,
        installed_by=updated.installed_by,
        installed_at=updated.installed_at,
        min_age=13,
    )
    monkeypatch.setattr(
        AppService, "set_min_age", AsyncMock(return_value=updated_with_age)
    )

    r = await client.patch(
        "/api/apps/com.example.hello",
        json={"min_age": 13},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["min_age"] == 13


async def test_patch_set_min_age_non_admin_403(client):
    """PATCH {min_age: 13} as member → 403."""
    member_tok = await _seed_member(client._db)
    r = await client.patch(
        "/api/apps/com.example.hello",
        json={"min_age": 13},
        headers=_auth(member_tok),
    )
    assert r.status == 403


async def test_patch_set_min_age_invalid_value_422(client, monkeypatch):
    """PATCH {min_age: 15} (invalid) → 422 via ValueError in BaseView._iter."""
    monkeypatch.setattr(
        AppService,
        "set_min_age",
        AsyncMock(side_effect=ValueError("min_age must be one of [0, 13, 16, 18]")),
    )
    r = await client.patch(
        "/api/apps/com.example.hello",
        json={"min_age": 15},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_patch_set_min_age_non_integer_400(client):
    """PATCH {min_age: 'adult'} → 400 UNPROCESSABLE (not an integer)."""
    r = await client.patch(
        "/api/apps/com.example.hello",
        json={"min_age": "adult"},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_serialize_includes_min_age(client, monkeypatch):
    """GET /api/apps serialized response includes min_age field."""
    app_with_age = InstalledApp(
        app_id="com.example.hello",
        name="Hello App",
        version="1.0.0",
        enabled=True,
        manifest=AppManifest(entry="index.js", icon="icon.png", capabilities=("read",)),
        bundle_path="apps/com.example.hello/1.0.0",
        bundle_sha256="abc123",
        source_url="https://example.com/hello.tgz",
        installed_by="admin",
        installed_at="2026-06-01T00:00:00+00:00",
        min_age=16,
    )
    monkeypatch.setattr(
        AppService,
        "list_visible",
        AsyncMock(return_value=[app_with_age]),
    )
    r = await client.get("/api/apps", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert len(body["apps"]) == 1
    assert body["apps"][0]["min_age"] == 16


async def test_list_apps_minor_filtered_by_age(client, monkeypatch):
    """GET /api/apps as under-age minor omits the restricted app (via list_visible)."""
    unrestricted = _make_installed(app_id="com.example.free")
    # Mock list_visible to simulate it already filtered the restricted app
    monkeypatch.setattr(
        AppService,
        "list_visible",
        AsyncMock(return_value=[unrestricted]),
    )
    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps", headers=_auth(member_tok))
    assert r.status == 200
    body = await r.json()
    ids = [a["app_id"] for a in body["apps"]]
    assert "com.example.free" in ids
    assert "com.example.hello" not in ids  # filtered by age gate


async def test_age_restricted_error_returns_403(client, monkeypatch):
    """AppAgeRestrictedError from any service method → 403 APP_AGE_RESTRICTED."""
    monkeypatch.setattr(
        AppService,
        "store_get",
        AsyncMock(
            side_effect=AppAgeRestrictedError("This app is restricted to ages 13+.")
        ),
    )
    await _seed_installed_enabled_app(client._db)
    r = await client.get(
        "/api/apps/com.example.hello/store/key",
        headers=_auth(client._tok),
    )
    assert r.status == 403
    body = await r.json()
    assert body["error"]["code"] == "APP_AGE_RESTRICTED"


# ── GET /api/apps/updates ─────────────────────────────────────────────────


async def test_updates_requires_auth(client):
    r = await client.get("/api/apps/updates")
    assert r.status == 401


async def test_updates_member_gets_200_force_false(client, monkeypatch):
    """Non-admin member → 200; list_updates called with force=False."""
    updates = [
        {
            "app_id": "com.example.hello",
            "name": "Hello App",
            "current_version": "1.0.0",
            "latest_version": "2.0.0",
        }
    ]
    mock = AsyncMock(return_value=updates)
    monkeypatch.setattr(AppService, "list_updates", mock)

    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps/updates", headers=_auth(member_tok))
    assert r.status == 200
    body = await r.json()
    assert body == {"updates": updates, "checked_at": None, "stale": False}
    mock.assert_awaited_once_with(force=False)


async def test_updates_member_refresh_param_ignored(client, monkeypatch):
    """Non-admin member with ?refresh=1 still calls list_updates(force=False)."""
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr(AppService, "list_updates", mock)

    member_tok = await _seed_member(client._db)
    r = await client.get("/api/apps/updates?refresh=1", headers=_auth(member_tok))
    assert r.status == 200
    mock.assert_awaited_once_with(force=False)


async def test_updates_admin_refresh_forces_network_fetch(client, monkeypatch):
    """Admin with ?refresh=1 calls list_updates(force=True)."""
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr(AppService, "list_updates", mock)

    r = await client.get("/api/apps/updates?refresh=1", headers=_auth(client._tok))
    assert r.status == 200
    mock.assert_awaited_once_with(force=True)


async def test_updates_admin_no_refresh_uses_cache(client, monkeypatch):
    """Admin without ?refresh → list_updates(force=False) (uses cache)."""
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr(AppService, "list_updates", mock)

    r = await client.get("/api/apps/updates", headers=_auth(client._tok))
    assert r.status == 200
    mock.assert_awaited_once_with(force=False)


class _CatalogResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self) -> None:
        return None

    async def text(self) -> str:
        return orjson.dumps(self._payload).decode()


class _CatalogHost:
    """A catalog host that answers until ``down`` is set."""

    def __init__(self) -> None:
        self.down = False

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, **kw):
        if self.down:
            raise aiohttp.ClientConnectionError("catalog host down")
        return _CatalogResp(
            {
                "apps": [
                    {
                        "app_id": "com.example.new",
                        "name": "New App",
                        "latest_version": "2026.10.28",
                        "description": "A test app",
                        "bundle_url": "https://example.com/new.tgz",
                        "bundle_sha256": "ab" * 32,
                    }
                ]
            }
        )


async def _catalog_down_after_one_answer(client) -> None:
    """Point the real catalog service at a host that answered once and is
    now down."""
    catalog = client.app[app_service_key]._catalog
    host = _CatalogHost()
    catalog._session_factory = host
    await catalog.fetch_catalog(force=True)
    host.down = True


async def test_updates_check_against_a_down_catalog_says_stale_not_500(
    client, monkeypatch
):
    """The check failed: the answer says so (``stale`` + when the catalog
    last answered) instead of an empty "all up to date" or a 500."""
    await _catalog_down_after_one_answer(client)
    installed = InstalledApp(
        app_id="com.example.new",
        name="New App",
        version="2026.6.4",
        enabled=True,
        manifest=AppManifest(entry="index.html", icon=None, capabilities=()),
        bundle_path="com.example.new/2026.6.4",
        bundle_sha256="ab" * 32,
        source_url="https://example.com/new.tgz",
        installed_by=None,
        installed_at="2026-06-04T00:00:00+00:00",
    )
    repo = client.app[app_service_key]._repo
    monkeypatch.setattr(
        type(repo), "list_installed", AsyncMock(return_value=[installed])
    )
    r = await client.get("/api/apps/updates?refresh=1", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["stale"] is True
    assert body["checked_at"] is not None
    # What the catalog said when it last answered still counts.
    assert [u["latest_version"] for u in body["updates"]] == ["2026.10.28"]


async def test_catalog_browse_against_a_down_catalog_serves_the_held_copy(client):
    await _catalog_down_after_one_answer(client)
    catalog = client.app[app_service_key]._catalog
    catalog._cache_ts = None  # expired: a plain read goes to the network
    r = await client.get("/api/apps/catalog", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["stale"] is True
    assert body["checked_at"] is not None
    assert [a["app_id"] for a in body["apps"]] == ["com.example.new"]


async def test_catalog_never_reached_is_a_coded_503_not_a_500(client):
    host = _CatalogHost()
    host.down = True
    client.app[app_service_key]._catalog._session_factory = host
    r = await client.get("/api/apps/catalog", headers=_auth(client._tok))
    assert r.status == 503
    assert (await r.json())["error"]["code"] == "APP_CATALOG_UNREACHABLE"


async def test_catalog_refresh_retries_past_the_pause_after_a_failure(client):
    """The Catalog tab's Retry (``?refresh=1``) reaches the network again;
    a plain read inside the pause answers from what is held."""
    host = _CatalogHost()
    host.down = True
    client.app[app_service_key]._catalog._session_factory = host
    r = await client.get("/api/apps/catalog", headers=_auth(client._tok))
    assert r.status == 503
    host.down = False
    r = await client.get("/api/apps/catalog", headers=_auth(client._tok))
    assert r.status == 503  # still inside the retry pause: no network
    r = await client.get("/api/apps/catalog?refresh=1", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert body["stale"] is False
    assert [a["app_id"] for a in body["apps"]] == ["com.example.new"]


# ── POST /api/apps/{app_id}/update ───────────────────────────────────────


async def test_update_app_requires_auth(client):
    r = await client.post("/api/apps/com.example.hello/update")
    assert r.status == 401


async def test_update_app_requires_admin(client, monkeypatch):
    """Non-admin POST → 403 before update_app is called."""
    mock = AsyncMock(return_value=_make_installed())
    monkeypatch.setattr(AppService, "update_app", mock)

    member_tok = await _seed_member(client._db)
    r = await client.post(
        "/api/apps/com.example.hello/update",
        headers=_auth(member_tok),
    )
    assert r.status == 403
    mock.assert_not_awaited()


async def test_update_app_admin_success_returns_serialized_app(client, monkeypatch):
    """Admin POST → 200 with the serialized InstalledApp."""
    app = _make_installed()
    mock = AsyncMock(return_value=app)
    monkeypatch.setattr(AppService, "update_app", mock)

    r = await client.post(
        "/api/apps/com.example.hello/update",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["app_id"] == "com.example.hello"
    assert body["name"] == "Hello App"
    assert body["version"] == "1.0.0"
    assert body["enabled"] is True
    assert body["capabilities"] == ["read"]
    assert body["icon"] == "icon.png"
    mock.assert_awaited_once_with("com.example.hello", actor_is_admin=True)


async def test_update_app_not_found_returns_404(client, monkeypatch):
    monkeypatch.setattr(
        AppService,
        "update_app",
        AsyncMock(side_effect=AppNotFoundError("not installed")),
    )
    r = await client.post(
        "/api/apps/com.example.missing/update",
        headers=_auth(client._tok),
    )
    assert r.status == 404
    body = await r.json()
    assert body["error"]["code"] == "NOT_FOUND"


async def test_update_app_integrity_error_returns_400(client, monkeypatch):
    monkeypatch.setattr(
        AppService,
        "update_app",
        AsyncMock(side_effect=AppIntegrityError("sha256 mismatch")),
    )
    r = await client.post(
        "/api/apps/com.example.hello/update",
        headers=_auth(client._tok),
    )
    assert r.status == 400
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


# ── E2E: bus wired into AppFederationService (challenge → notification) ───


async def test_local_challenge_creates_notification(client):
    """POST /api/apps/{id}/sessions with a local target fires an AppChallengeReceived
    event on the bus, which notification_service translates into a bell row for the
    challenged user.  This test is intentionally a failing test until app.py passes
    bus= to AppFederationService.
    """
    await _seed_installed_enabled_app(client._db)
    carol_tok = await _seed_member(client._db, username="carol", user_id="carol-id")

    own_instance_id = client.app[instance_id_key]

    target = {
        "instance_id": own_instance_id,
        "user_ref": "carol-id",
        "is_local": True,
    }
    r = await client.post(
        "/api/apps/com.example.hello/sessions",
        json={"target": target},
        headers=_auth(client._tok),
    )
    assert r.status == 201

    # Carol should now have an app_challenge notification row (written by
    # the delivery worker, after the request returns).
    await client.app[notification_delivery_key].drain()
    notif_r = await client.get(
        "/api/notifications",
        headers=_auth(carol_tok),
    )
    assert notif_r.status == 200
    notifs = await notif_r.json()
    types = [n["type"] for n in notifs]
    assert "app_challenge" in types, (
        f"expected app_challenge notification, got: {notifs}"
    )


# ── Game invites wait in "Needs you" until they end ───────────────────────


async def _challenge_carol(client) -> tuple[str, str, dict]:
    """The admin challenges Carol (on this household): her token, the
    session id and the target."""
    await _seed_installed_enabled_app(client._db)
    carol_tok = await _seed_member(client._db, username="carol", user_id="carol-id")
    target = {
        "instance_id": client.app[instance_id_key],
        "user_ref": "carol-id",
        "is_local": True,
    }
    r = await client.post(
        "/api/apps/com.example.hello/sessions",
        json={"target": target},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    session_id = (await r.json())["session_id"]
    await client.app[notification_delivery_key].drain()
    return carol_tok, session_id, target


async def _needs_you(client, tok: str) -> list[dict]:
    await client.app[notification_delivery_key].drain()
    r = await client.get("/api/notifications/needs-you", headers=_auth(tok))
    assert r.status == 200
    return (await r.json())["items"]


async def test_a_game_invite_waits_in_needs_you_until_declined(client):
    carol_tok, session_id, _ = await _challenge_carol(client)
    (item,) = await _needs_you(client, carol_tok)
    assert item["type"] == "app_challenge"
    assert item["object_key"] == f"app_session:com.example.hello/{session_id}"
    assert item["link_url"] == "/apps/com.example.hello"

    url = f"/api/apps/com.example.hello/sessions/{session_id}/decline"
    r = await client.post(url, headers=_auth(carol_tok))
    assert r.status == 200
    assert await _needs_you(client, carol_tok) == []
    rows = await (
        await client.get("/api/notifications", headers=_auth(carol_tok))
    ).json()
    (row,) = [n for n in rows if n["type"] == "app_challenge"]
    assert row["params"]["handled"] == {"outcome": "declined", "by": None}

    # A second tap (another device) hears it was handled.
    r = await client.post(url, headers=_auth(carol_tok))
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "APP_CHALLENGE_HANDLED"


async def test_only_the_invitee_can_decline_a_game_invite(client):
    _, session_id, _ = await _challenge_carol(client)
    r = await client.post(
        f"/api/apps/com.example.hello/sessions/{session_id}/decline",
        headers=_auth(client._tok),  # the challenger
    )
    assert r.status == 409


async def test_decline_requires_auth(client):
    r = await client.post("/api/apps/com.example.hello/sessions/s1/decline")
    assert r.status == 401


async def _challenge_carol_again(client) -> str:
    """The admin sends Carol another invite: its session id."""
    r = await client.post(
        "/api/apps/com.example.hello/sessions",
        json={
            "target": {
                "instance_id": client.app[instance_id_key],
                "user_ref": "carol-id",
                "is_local": True,
            }
        },
        headers=_auth(client._tok),
    )
    assert r.status == 201
    await client.app[notification_delivery_key].drain()
    return (await r.json())["session_id"]


async def test_opening_the_app_accepts_nothing(client):
    carol_tok, session_id, _ = await _challenge_carol(client)
    r = await client.get(
        "/api/apps/com.example.hello/runtime", headers=_auth(carol_tok)
    )
    assert r.status == 200
    (item,) = await _needs_you(client, carol_tok)
    assert item["object_key"] == f"app_session:com.example.hello/{session_id}"


async def test_play_takes_in_only_that_invite(client):
    carol_tok, first, _ = await _challenge_carol(client)
    second = await _challenge_carol_again(client)
    r = await client.post(
        f"/api/apps/com.example.hello/sessions/{first}/accept",
        headers=_auth(carol_tok),
    )
    assert r.status == 200
    session = (await r.json())["session"]
    assert session["session_id"] == first
    assert session["from_user"] == client._uid
    assert session["payload"]["verb"] == "open"
    # The other invite still waits for Carol in Needs you.
    (item,) = await _needs_you(client, carol_tok)
    assert item["object_key"] == f"app_session:com.example.hello/{second}"
    # Playing it again (a second device) hears it was handled.
    r = await client.post(
        f"/api/apps/com.example.hello/sessions/{first}/accept",
        headers=_auth(carol_tok),
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "APP_CHALLENGE_HANDLED"


async def test_the_accepted_invite_reads_accepted(client):
    carol_tok, session_id, _ = await _challenge_carol(client)
    r = await client.post(
        f"/api/apps/com.example.hello/sessions/{session_id}/accept",
        headers=_auth(carol_tok),
    )
    assert r.status == 200
    await client.app[notification_delivery_key].drain()
    r = await client.get("/api/notifications", headers=_auth(carol_tok))
    (row,) = [n for n in await r.json() if n["type"] == "app_challenge"]
    assert row["params"]["handled"] == {"outcome": "accepted", "by": None}


async def test_only_the_invitee_can_play_a_game_invite(client):
    carol_tok, session_id, _ = await _challenge_carol(client)
    r = await client.post(
        f"/api/apps/com.example.hello/sessions/{session_id}/accept",
        headers=_auth(client._tok),  # the challenger
    )
    assert r.status == 409
    assert len(await _needs_you(client, carol_tok)) == 1


async def test_the_challenger_cancels_a_game_invite(client):
    carol_tok, session_id, target = await _challenge_carol(client)
    r = await client.post(
        f"/api/apps/com.example.hello/sessions/{session_id}/cancel",
        json={"target": target},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await _needs_you(client, carol_tok) == []


async def test_cancel_needs_a_target(client):
    r = await client.post(
        "/api/apps/com.example.hello/sessions/s1/cancel",
        json={"target": {"instance_id": "x"}},
        headers=_auth(client._tok),
    )
    assert r.status == 400
    assert (await r.json())["error"]["code"] == "UNPROCESSABLE"


# ── POST /api/apps/{app_id}/held — the mailbox (D11) ─────────────────────


async def test_held_requires_auth(client):
    r = await client.post("/api/apps/com.example.hello/held")
    assert r.status == 401


async def test_held_hands_over_the_callers_mailbox(client, fed_svc):
    """The mailbox is the authed user's — never a request param."""
    fed_svc.take_held.return_value = [
        AppPendingSession(
            app_id="com.example.hello",
            user_id=client._uid,
            session_id="sess-1",
            from_instance="peer.example.com",
            from_user="alice",
            payload={"verb": "open"},
            created_at="2026-06-04T12:00:00+00:00",
            state="active",
            held={"kind": "session", "payload": {"verb": "close", "reason": "x"}},
        )
    ]
    r = await client.post(
        "/api/apps/com.example.hello/held", headers=_auth(client._tok)
    )
    assert r.status == 200
    assert await r.json() == {
        "messages": [
            {
                "session_id": "sess-1",
                "from_instance": "peer.example.com",
                "from_user": "alice",
                "kind": "session",
                "payload": {"verb": "close", "reason": "x"},
            }
        ]
    }
    fed_svc.take_held.assert_awaited_once_with(
        app_id="com.example.hello", actor_user_id=client._uid
    )


async def test_held_for_a_turned_off_app_is_403(client, fed_svc):
    fed_svc.take_held.side_effect = AppNotEnabledError("com.example.hello")
    r = await client.post(
        "/api/apps/com.example.hello/held", headers=_auth(client._tok)
    )
    assert r.status == 403


async def _held(client, tok: str) -> list[dict]:
    r = await client.post("/api/apps/com.example.hello/held", headers=_auth(tok))
    assert r.status == 200
    return (await r.json())["messages"]


async def test_a_move_waits_for_a_closed_app_and_arrives_once(client):
    """L#1 on one household: the admin moves while Carol's app is closed —
    her next open gets the latest move, the one after gets nothing."""
    carol_tok, session_id, target = await _challenge_carol(client)
    r = await client.post(
        f"/api/apps/com.example.hello/sessions/{session_id}/accept",
        headers=_auth(carol_tok),
    )
    assert r.status == 200
    for san in ("e4", "Nf3"):
        r = await client.post(
            "/api/apps/com.example.hello/messages",
            json={"session_id": session_id, "target": target, "payload": {"san": san}},
            headers=_auth(client._tok),
        )
        assert r.status == 200
    (msg,) = await _held(client, carol_tok)
    assert msg["session_id"] == session_id
    assert msg["kind"] == "message"
    assert msg["payload"] == {"san": "Nf3"}
    assert msg["from_user"] == client._uid
    assert await _held(client, carol_tok) == []


async def test_a_decline_waits_for_the_challenger_with_a_bell_row(client):
    """L#2 on one household: Carol declines while the admin's app is
    closed — a bell row, and the close waits for the admin's app."""
    carol_tok, session_id, _ = await _challenge_carol(client)
    r = await client.post(
        f"/api/apps/com.example.hello/sessions/{session_id}/decline",
        headers=_auth(carol_tok),
    )
    assert r.status == 200
    await client.app[notification_delivery_key].drain()
    rows = await (
        await client.get("/api/notifications", headers=_auth(client._tok))
    ).json()
    (row,) = [n for n in rows if n["type"] == "app_challenge_declined"]
    assert row["object_key"] == f"app_session:com.example.hello/{session_id}"
    assert row["link_url"] == "/apps/com.example.hello"
    (msg,) = await _held(client, client._tok)
    assert msg["kind"] == "session"
    assert msg["payload"]["reason"] == "declined"


async def test_app_messages_have_their_own_rate_limit_bucket(client):
    """C#31: a quick game's moves no longer share the 60/min ``/api/apps``
    bucket with the store writes around them."""
    _carol_tok, session_id, target = await _challenge_carol(client)
    body = {"session_id": session_id, "target": target, "payload": {"san": "e4"}}
    for _ in range(70):
        r = await client.post(
            "/api/apps/com.example.hello/messages",
            json=body,
            headers=_auth(client._tok),
        )
        assert r.status == 200
    r = await client.get("/api/apps", headers=_auth(client._tok))
    assert r.status == 200
