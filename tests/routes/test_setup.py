"""Tests for the /api/setup/* first-boot wizard endpoints."""

from __future__ import annotations

from dataclasses import replace

from socialhome.app import create_app
from socialhome.app_keys import (
    config_key,
    federation_repo_key,
    preferences_service_key,
    platform_adapter_key,
    setup_service_key,
)
from socialhome.app_keys import db_key as _db_key
from socialhome.config import Config
from socialhome.crypto import derive_user_id
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
    feats = await tc._app[preferences_service_key].get_household()
    assert feats.household_name == "The Rivendells"


async def test_standalone_setup_household_name_reaches_federated_identity(
    aiohttp_client,
    tmp_dir,
):
    """Regression: the setup name must reach the FEDERATED identity field.

    The federated identity (``instance_identity.display_name``, carried in
    the pairing QR + shown to peers) used to stay the bootstrap default
    while only the local preference got the name — so peers saw "My Home".
    """
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    r = await tc.post(
        "/api/setup/standalone",
        json={
            "username": "owner",
            "password": "hunter2",
            "household_name": "Casa Vizeli",
        },
    )
    assert r.status == 201, await r.text()
    identity = await tc._app[federation_repo_key].get_local_identity()
    assert identity is not None
    assert identity["display_name"] == "Casa Vizeli"


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
    feats = await tc._app[preferences_service_key].get_household()
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

    async def get_owner(self):
        # First admin-flagged user, mirroring HaUserDirectory's "first
        # row with is_owner=True" walk. Tests that need a specific
        # owner pass that user first in the list.
        for user in self._by_user.values():
            if user.is_admin:
                return user
        return None


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
    feats = await tc._app[preferences_service_key].get_household()
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
    user = await db.fetchone(
        "SELECT is_admin, handle FROM users WHERE username='alice'"
    )
    assert user is not None and user["is_admin"] == 1
    # The public ``@handle`` seeds from the username so the row is never
    # NULL-handle (the §public-handle editor pre-fills + lets the user save).
    assert user["handle"] == "alice"


async def test_ha_owner_setup_derives_cryptographic_user_id(aiohttp_client, tmp_dir):
    """The mirrored admin row gets a derived ``user_id``, not ``uid-<name>``.

    Regression (#standalone-admin-user-id): a synthetic id can never satisfy
    the public-space relay's per-author self-cert
    (``derive_user_id(author_pk, anchor) == author_user_id``), so every post
    the household admin wrote was dropped by subscribers.
    """
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    _swap_to_ha(tc._app, [ExternalUser("alice", "Alice", None, is_admin=False)])
    r = await tc.post(
        "/api/setup/ha/owner",
        json={"username": "alice", "password": "hunter22"},
    )
    assert r.status == 201, await r.text()
    db = tc._app[_db_key]
    user = await db.fetchone(
        "SELECT user_id, identity_anchor FROM users WHERE username='alice'"
    )
    assert user is not None
    identity = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    assert identity is not None
    expected = derive_user_id(bytes.fromhex(identity["identity_public_key"]), "alice")
    assert user["user_id"] != "uid-alice"
    assert user["user_id"] == expected
    assert user["identity_anchor"] == "alice"


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
# Both ``haos/complete`` and ``ha/owner`` now resolve the owner via
# ``adapter.users.get_owner()`` (HA Core WS ``config/auth/list``); the
# supervisor REST ``/auth/list`` is no longer touched by the wizard
# (#297 / #298).


class _FakeHaosAdapter(_FakeHaAdapter):
    @property
    def capabilities(self):
        return frozenset({Capability.INGRESS, Capability.HA_PERSON_DIRECTORY})


async def test_haos_complete_happy_path(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter(
        [ExternalUser("owner", "The Owner", None, is_admin=True)],
    )
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
        [ExternalUser("owner", "The Owner", None, is_admin=True)],
    )
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r = await tc.post(
        "/api/setup/haos/complete",
        json={"household_name": "Hearth"},
    )
    assert r.status == 200, await r.text()
    feats = await tc._app[preferences_service_key].get_household()
    assert feats.household_name == "Hearth"


async def test_haos_complete_household_name_too_long_422(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter(
        [ExternalUser("owner", "The Owner", None, is_admin=True)],
    )
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r = await tc.post(
        "/api/setup/haos/complete",
        json={"household_name": "x" * 200},
    )
    assert r.status == 422
    assert await tc._app[setup_service_key].is_required() is True


async def test_haos_complete_no_owner_returns_422(aiohttp_client, tmp_dir):
    """No HA user with ``is_owner=True`` → wizard 422s cleanly."""
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter([])
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r = await tc.post("/api/setup/haos/complete")
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "NO_OWNER"


async def test_haos_complete_persists_display_name(aiohttp_client, tmp_dir):
    """Display name comes straight from the auth list — instances whose
    owner has a display name that differs from their auth username
    provision cleanly (#297)."""
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter(
        [
            ExternalUser(
                "socialhome",
                "Social Home Test",
                None,
                is_admin=True,
            ),
        ],
    )
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r = await tc.post("/api/setup/haos/complete")
    assert r.status == 200, await r.text()
    db = tc._app[_db_key]
    row = await db.fetchone(
        "SELECT username, display_name, handle FROM users WHERE username=?",
        ("socialhome",),
    )
    assert row is not None
    assert row["username"] == "socialhome"
    assert row["display_name"] == "Social Home Test"
    # The public ``@handle`` seeds from the username so the row is never
    # NULL-handle (the §public-handle editor pre-fills + lets the user save).
    assert row["handle"] == "socialhome"


async def test_haos_complete_stamps_source_ha_and_external_id(
    aiohttp_client,
    tmp_dir,
):
    """Regression: the wizard MUST persist ``source='ha'`` + ``external_id``
    on the admin row so the HA Users admin panel's sync toggle shows
    "Active" for the household owner.

    Earlier builds constructed a fresh ``ExternalUser`` inside
    :class:`HaosCompleteSetupView` and dropped ``external_id`` on the
    floor — the row landed with ``source='manual'`` and the toggle
    rendered as "Not added" against the user who was clearly already
    provisioned.
    """
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter(
        [
            ExternalUser(
                "socialhome",
                "Social Home",
                None,
                is_admin=True,
                external_id="abc123def" + "0" * 23,
            ),
        ],
    )
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r = await tc.post("/api/setup/haos/complete")
    assert r.status == 200, await r.text()

    db = tc._app[_db_key]
    row = await db.fetchone(
        "SELECT source, external_id FROM users WHERE username=?",
        ("socialhome",),
    )
    assert row is not None
    assert row["source"] == "ha"
    assert row["external_id"] == "abc123def" + "0" * 23


async def test_setup_locked_after_haos_completion(aiohttp_client, tmp_dir):
    tc = await _build_standalone_app(aiohttp_client, tmp_dir)
    adapter = _FakeHaosAdapter(
        [ExternalUser("owner", "Owner", None, is_admin=True)],
    )
    tc._app[platform_adapter_key] = adapter
    tc._app[config_key] = replace(tc._app[config_key], mode="haos")
    r1 = await tc.post("/api/setup/haos/complete")
    assert r1.status == 200
    r2 = await tc.post("/api/setup/haos/complete")
    assert r2.status == 409
