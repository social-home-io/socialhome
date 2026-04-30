"""Tests for the /api/setup/* first-boot wizard endpoints."""

from __future__ import annotations

from dataclasses import replace

from socialhome.app import create_app
from socialhome.app_keys import (
    config_key,
    household_features_service_key,
    platform_adapter_key,
    setup_service_key,
)
from socialhome.app_keys import db_key as _db_key
from socialhome.config import Config
from socialhome.platform.adapter import Capability, ExternalUser


async def _build_standalone_app(aiohttp_client, tmp_dir):
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "t.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    tc = await aiohttp_client(app)
    tc._app = app
    return tc


# ── Standalone ──────────────────────────────────────────────────────────────


async def test_standalone_setup_seeds_admin_and_returns_token(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.post(
        "/api/setup/standalone",
        json={"username": "owner", "password": "hunter2"},
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    assert isinstance(body["token"], str) and len(body["token"]) > 20
    db = tc._app[_db_key]
    pu = await db.fetchone(
        "SELECT * FROM platform_users WHERE username='owner'",
    )
    assert pu is not None and pu["is_admin"] == 1
    assert await tc._app[setup_service_key].is_required() is False


async def test_standalone_setup_requires_username_and_password(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.post("/api/setup/standalone", json={"username": "x"})
    assert r.status == 422


async def test_standalone_setup_persists_household_name(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.post(
        "/api/setup/standalone",
        json={
            "username": "owner",
            "password": "hunter2",
            "household_name": "The Rivendells",
        },
    )
    assert r.status == 201, await r.text()
    feats = await tc._app[household_features_service_key].get()
    assert feats.household_name == "The Rivendells"


async def test_standalone_setup_blank_household_name_keeps_default(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.post(
        "/api/setup/standalone",
        json={
            "username": "owner",
            "password": "hunter2",
            "household_name": "   ",
        },
    )
    assert r.status == 201
    feats = await tc._app[household_features_service_key].get()
    assert feats.household_name == "Home"


async def test_standalone_setup_household_name_too_long_422(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.post(
        "/api/setup/standalone",
        json={
            "username": "owner",
            "password": "hunter2",
            "household_name": "x" * 200,
        },
    )
    assert r.status == 422
    # Setup must NOT have been marked complete on validation failure.
    assert await tc._app[setup_service_key].is_required() is True


async def test_standalone_setup_locked_after_completion(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r1 = await tc.post(
        "/api/setup/standalone",
        json={"username": "owner", "password": "pw"},
    )
    assert r1.status == 201
    r2 = await tc.post(
        "/api/setup/standalone",
        json={"username": "owner2", "password": "pw"},
    )
    assert r2.status == 409
    body = await r2.json()
    assert body["error"]["code"] == "ALREADY_COMPLETE"


# ── ha (mode mismatch + happy path) ─────────────────────────────────────────


async def test_ha_owner_setup_mode_mismatch_in_standalone(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.post(
        "/api/setup/ha/owner",
        json={"username": "alice"},
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "WRONG_MODE"


async def test_haos_complete_setup_mode_mismatch_in_standalone(
    aiohttp_client,
    tmp_dir,
):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.post("/api/setup/haos/complete")
    assert r.status == 409


async def test_ha_persons_mode_mismatch_in_standalone(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.get("/api/setup/ha/persons")
    assert r.status == 409


# ── ha (happy path with swapped adapter) ────────────────────────────────────


class _FakeUserDirectory:
    def __init__(self, persons):
        self._by_user = {p.username: p for p in persons}

    async def list_users(self):
        return list(self._by_user.values())

    async def get(self, username):
        return self._by_user.get(username)


class _FakeHaAdapter:
    """Stand-in for HaAdapter sufficient for the ha-mode setup route."""

    def __init__(self, persons):
        self.users = _FakeUserDirectory(persons)
        self.passwords: dict[str, str] = {}

    async def list_external_users(self):
        return await self.users.list_users()

    async def set_local_password(
        self,
        username,
        password,
        *,
        display_name=None,
        is_admin=False,
    ):
        self.passwords[username] = password

    async def issue_bearer_token(self, username, password, *, label="web"):
        return f"token-{username}"


def _swap_to_ha(app, persons, *, mode="ha"):
    app[platform_adapter_key] = _FakeHaAdapter(persons)
    app[config_key] = replace(app[config_key], mode=mode)


async def test_ha_persons_lists_via_adapter(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    _swap_to_ha(
        tc._app,
        [
            ExternalUser("alice", "Alice", None, is_admin=False),
            ExternalUser("bob", "Bob", "https://x/b.png", is_admin=False),
        ],
    )
    r = await tc.get("/api/setup/ha/persons")
    assert r.status == 200, await r.text()
    body = await r.json()
    by_name = {p["username"]: p for p in body["persons"]}
    assert by_name["alice"]["display_name"] == "Alice"
    assert by_name["bob"]["picture_url"] == "https://x/b.png"


async def test_ha_owner_setup_persists_household_name(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    _swap_to_ha(tc._app, [ExternalUser("alice", "Alice", None, is_admin=False)])
    r = await tc.post(
        "/api/setup/ha/owner",
        json={
            "username": "alice",
            "password": "hunter22",
            "household_name": "Casa Vizeli",
        },
    )
    assert r.status == 201, await r.text()
    feats = await tc._app[household_features_service_key].get()
    assert feats.household_name == "Casa Vizeli"


async def test_ha_owner_setup_happy_path(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    _swap_to_ha(
        tc._app,
        [ExternalUser("alice", "Alice", None, is_admin=False)],
    )
    r = await tc.post(
        "/api/setup/ha/owner",
        json={"username": "alice", "password": "hunter22"},
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    assert body["token"] == "token-alice"
    adapter = tc._app[platform_adapter_key]
    assert adapter.passwords["alice"] == "hunter22"
    assert await tc._app[setup_service_key].is_required() is False
    # Mirror row in users table is admin.
    db = tc._app[_db_key]
    user = await db.fetchone("SELECT is_admin FROM users WHERE username='alice'")
    assert user is not None and user["is_admin"] == 1


async def test_ha_owner_setup_requires_password(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    _swap_to_ha(tc._app, [ExternalUser("alice", "Alice", None, is_admin=False)])
    r = await tc.post("/api/setup/ha/owner", json={"username": "alice"})
    assert r.status == 422


async def test_ha_owner_unknown_username_422(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    _swap_to_ha(tc._app, [ExternalUser("alice", "Alice", None, is_admin=False)])
    r = await tc.post(
        "/api/setup/ha/owner",
        json={"username": "ghost", "password": "hunter22"},
    )
    assert r.status == 422


# ── haos (happy path) ───────────────────────────────────────────────────────


class _FakeSupervisorClient:
    def __init__(self, owner: str | None = "owner"):
        self._owner = owner

    async def get_owner_username(self):
        return self._owner


class _FakeHaosAdapter(_FakeHaAdapter):
    @property
    def capabilities(self):
        return frozenset({Capability.INGRESS, Capability.HA_PERSON_DIRECTORY})


async def test_haos_complete_happy_path(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter(
        [ExternalUser("owner", "The Owner", None, is_admin=False)],
    )
    adapter._supervisor_client = _FakeSupervisorClient("owner")
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r = await tc.post("/api/setup/haos/complete")
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["username"] == "owner"
    assert await tc._app[setup_service_key].is_required() is False


async def test_haos_complete_persists_household_name(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter(
        [ExternalUser("owner", "The Owner", None, is_admin=False)],
    )
    adapter._supervisor_client = _FakeSupervisorClient("owner")
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r = await tc.post(
        "/api/setup/haos/complete",
        json={"household_name": "Hearth"},
    )
    assert r.status == 200, await r.text()
    feats = await tc._app[household_features_service_key].get()
    assert feats.household_name == "Hearth"


async def test_haos_complete_household_name_too_long_422(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter(
        [ExternalUser("owner", "The Owner", None, is_admin=False)],
    )
    adapter._supervisor_client = _FakeSupervisorClient("owner")
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r = await tc.post(
        "/api/setup/haos/complete",
        json={"household_name": "x" * 200},
    )
    assert r.status == 422
    assert await tc._app[setup_service_key].is_required() is True


async def test_haos_complete_no_owner_returns_422(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter([])
    adapter._supervisor_client = _FakeSupervisorClient(None)
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r = await tc.post("/api/setup/haos/complete")
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "NO_OWNER"


async def test_haos_complete_owner_not_in_persons_422(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter([])  # supervisor returns 'owner' but no person.* entity
    adapter._supervisor_client = _FakeSupervisorClient("owner")
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r = await tc.post("/api/setup/haos/complete")
    assert r.status == 422


async def test_haos_complete_missing_supervisor_client_503(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter([])
    # No _supervisor_client wired — route surfaces 503.
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r = await tc.post("/api/setup/haos/complete")
    assert r.status == 503


async def test_setup_locked_after_haos_completion(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter(
        [ExternalUser("owner", "Owner", None, is_admin=False)],
    )
    adapter._supervisor_client = _FakeSupervisorClient("owner")
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r1 = await tc.post("/api/setup/haos/complete")
    assert r1.status == 200
    r2 = await tc.post("/api/setup/haos/complete")
    assert r2.status == 409
