"""Tests for socialhome.platform.haos.bootstrap."""

from __future__ import annotations

import os

import pytest

from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import UserProfileUpdated
from socialhome.infrastructure.event_bus import EventBus
from socialhome.platform.haos.bootstrap import (
    BOOTSTRAP_FLAG,
    INTEGRATION_TOKEN_FILENAME,
    INTEGRATION_TOKEN_LABEL,
    HaBootstrap,
)
from socialhome.platform.haos.supervisor import AddonInfo
from socialhome.repositories.instance_config_repo import SqliteInstanceConfigRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.user_service import UserService


# ─── Fakes ───────────────────────────────────────────────────────────────


_DEFAULT_ADDON_INFO = AddonInfo(hostname="local-social-home", ingress_port=8099)


# Sentinel that means "the test didn't override self_info" — distinct
# from ``None`` (which the test uses to assert the missing-data branch).
_UNSET = object()


class _FakeSupervisor:
    """In-process :class:`SupervisorClient` substitute for tests.

    Only the discovery-push + addon-info shape lives here now; user
    discovery moved to :class:`_FakeUsers` (mirroring the production
    split where HA Core's WS is the single source of identity).
    """

    def __init__(
        self,
        *,
        fail_discovery: bool = False,
        self_info: AddonInfo | None = _UNSET,  # type: ignore[assignment]
    ) -> None:
        self.fail_discovery = fail_discovery
        # ``None`` here means "Supervisor said no" — the bootstrap
        # logs and skips the push instead of guessing.
        self.self_info: AddonInfo | None = (
            _DEFAULT_ADDON_INFO if self_info is _UNSET else self_info
        )
        self.pushed_payloads: list[dict] = []

    async def get_self_info(self) -> AddonInfo | None:
        return self.self_info

    async def push_discovery(self, payload: dict) -> bool:
        self.pushed_payloads.append(payload)
        return not self.fail_discovery


class _FakeUsers:
    """In-process :class:`HaUserDirectory` substitute for bootstrap tests.

    Only the methods the bootstrap actually calls live here —
    ``get_owner()``. Anything else would be dead weight.
    """

    def __init__(
        self,
        *,
        owner_username: str | None = "ha_owner",
        owner_display_name: str = "Social Home Test",
        owner_external_id: str | None = "ha-id-stable",
    ) -> None:
        self._owner_username = owner_username
        self._owner_display_name = owner_display_name
        self._owner_external_id = owner_external_id

    async def get_owner(self):
        if self._owner_username is None:
            return None
        from socialhome.platform.adapter import ExternalUser

        return ExternalUser(
            username=self._owner_username,
            display_name=self._owner_display_name,
            picture_url=None,
            is_admin=False,
            email=None,
            external_id=self._owner_external_id,
        )


def _make_bootstrap(
    env,
    *,
    users: _FakeUsers | None = None,
    supervisor: _FakeSupervisor | None = None,
) -> HaBootstrap:
    """Tiny factory so individual tests stay focused on what they
    actually exercise — caller passes only the collaborator they
    want non-default."""
    return HaBootstrap(
        db=env.db,
        instance_config=SqliteInstanceConfigRepo(env.db),
        users=users or _FakeUsers(),
        supervisor=supervisor or _FakeSupervisor(),
        data_dir=env.data_dir,
        user_service=env.user_svc,
    )


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
async def env(tmp_dir):
    """DB with instance_identity seeded + a data_dir for the token file."""
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "boot.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    data_dir = tmp_dir / "data"
    data_dir.mkdir()

    bus = EventBus()
    user_svc = UserService(
        SqliteUserRepo(db),
        bus,
        own_instance_public_key=kp.public_key,
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.data_dir = str(data_dir)
    e.kp = kp
    e.iid = iid
    e.bus = bus
    e.user_svc = user_svc
    yield e
    await db.shutdown()


# ─── Individual step helpers ─────────────────────────────────────────────


async def test_provision_admin_idempotent(env):
    """Admin provisioned once; second call is a no-op (row count stays at 1)."""
    bs = _make_bootstrap(env)

    await bs._provision_admin(
        username="ha_owner",
        display_name="HA Owner",
        external_id="ha-id-1",
    )
    row = await env.db.fetchone(
        "SELECT user_id, is_admin, display_name, external_id, source"
        " FROM users WHERE username=?",
        ("ha_owner",),
    )
    assert row is not None
    assert row["is_admin"] == 1
    # ``display_name`` is now the HA "name" field, not the username
    # (#297). The previous version fell back to username for both.
    assert row["display_name"] == "HA Owner"
    # ``external_id`` carries the HA user_id so downstream joins
    # (picture lifter, future presence bridge) don't re-run the
    # username→id lookup.
    assert row["external_id"] == "ha-id-1"
    assert row["source"] == "ha"

    # A subsequent call refreshes ``external_id`` (HA-side rotation)
    # and re-asserts is_admin without inserting a second row.
    await bs._provision_admin(
        username="ha_owner",
        display_name="HA Owner",
        external_id="ha-id-2",
    )
    count = await env.db.fetchval(
        "SELECT COUNT(*) FROM users WHERE username=?",
        ("ha_owner",),
        default=0,
    )
    assert count == 1
    refreshed = await env.db.fetchone(
        "SELECT external_id FROM users WHERE username=?",
        ("ha_owner",),
    )
    assert refreshed["external_id"] == "ha-id-2"


async def test_provision_admin_is_username_anchored(env):
    """HAOS owners are username-anchored: ``identity_anchor == username`` and
    ``user_id == derive_user_id(pk, username)``. Unlike a standalone uuid
    anchor, this keeps the deterministic ``user_id`` stable across the
    idempotent re-mirroring the bootstrap does on every boot (legacy-style,
    works on all peers).
    """
    bs = _make_bootstrap(env)
    expected_uid = derive_user_id(env.kp.public_key, "ha_owner")

    await bs._provision_admin(
        username="ha_owner",
        display_name="HA Owner",
        external_id="ha-id-1",
    )

    row = await env.db.fetchone(
        "SELECT user_id, identity_anchor, handle FROM users WHERE username=?",
        ("ha_owner",),
    )
    assert row is not None
    assert row["identity_anchor"] == "ha_owner"
    assert row["user_id"] == expected_uid
    # The public ``@handle`` seeds from the username so the row is never
    # NULL-handle (the §public-handle editor pre-fills + lets the user save).
    assert row["handle"] == "ha_owner"

    # Idempotent re-mirror: a second provision (HA-side re-poll) must leave
    # the deterministic user_id + the anchor untouched.
    await bs._provision_admin(
        username="ha_owner",
        display_name="HA Owner",
        external_id="ha-id-2",
    )
    again = await env.db.fetchone(
        "SELECT user_id, identity_anchor FROM users WHERE username=?",
        ("ha_owner",),
    )
    assert again["user_id"] == expected_uid
    assert again["identity_anchor"] == "ha_owner"


async def test_run_user_is_username_anchored_across_reruns(env):
    """End-to-end: running bootstrap twice leaves user_id + identity_anchor
    unchanged (the bootstrap re-runs the mirror on every boot)."""
    users = _FakeUsers(owner_username="ha_admin")
    expected_uid = derive_user_id(env.kp.public_key, "ha_admin")

    await _make_bootstrap(env, users=users).run()
    first = await env.db.fetchone(
        "SELECT user_id, identity_anchor FROM users WHERE username=?",
        ("ha_admin",),
    )
    assert first["identity_anchor"] == "ha_admin"
    assert first["user_id"] == expected_uid

    await _make_bootstrap(env, users=users).run()
    second = await env.db.fetchone(
        "SELECT user_id, identity_anchor FROM users WHERE username=?",
        ("ha_admin",),
    )
    assert second["user_id"] == first["user_id"]
    assert second["identity_anchor"] == first["identity_anchor"]


async def test_config_flag_helpers(env):
    """_is_done / _mark_done round-trip through instance_config."""
    bs = _make_bootstrap(env)

    assert await bs._is_done() is False
    await bs._mark_done()
    assert await bs._is_done() is True


async def test_generate_integration_token_writes_file(env):
    """Token is persisted in api_tokens and written to disk (mode 0600)."""
    bs = _make_bootstrap(env)
    await bs._provision_admin(
        username="ha_owner",
        display_name="HA Owner",
        external_id="ha-id-1",
    )

    await bs._generate_integration_token("ha_owner")

    row = await env.db.fetchone(
        "SELECT token_id FROM api_tokens WHERE label=?",
        (INTEGRATION_TOKEN_LABEL,),
    )
    assert row is not None

    token_path = os.path.join(env.data_dir, INTEGRATION_TOKEN_FILENAME)
    assert os.path.exists(token_path)
    with open(token_path) as f:
        raw = f.read().strip()
    assert len(raw) > 20

    mode = os.stat(token_path).st_mode & 0o777
    assert mode == 0o600

    # Idempotent — second call does not create a new row.
    await bs._generate_integration_token("ha_owner")
    count = await env.db.fetchval(
        "SELECT COUNT(*) FROM api_tokens WHERE label=?",
        (INTEGRATION_TOKEN_LABEL,),
        default=0,
    )
    assert count == 1


# ─── run() end-to-end ────────────────────────────────────────────────────


async def test_run_provisions_admin_and_pushes_discovery(env):
    """First boot provisions the owner, mints a token, pushes discovery."""
    sv = _FakeSupervisor()
    users = _FakeUsers(owner_username="ha_admin", owner_display_name="HA Admin")
    bs = _make_bootstrap(env, users=users, supervisor=sv)

    await bs.run()

    # Admin provisioned with the HA display name (not the username).
    row = await env.db.fetchone(
        "SELECT is_admin, display_name FROM users WHERE username=?",
        ("ha_admin",),
    )
    assert row is not None and row["is_admin"] == 1
    assert row["display_name"] == "HA Admin"

    # Token persisted
    tokens = await env.db.fetchall(
        "SELECT label FROM api_tokens WHERE label=?",
        (INTEGRATION_TOKEN_LABEL,),
    )
    assert len(tokens) == 1

    # Token file exists (so discovery could read it)
    token_file = os.path.join(env.data_dir, INTEGRATION_TOKEN_FILENAME)
    assert os.path.exists(token_file)

    # Flag set
    assert await bs._is_done() is True

    # Discovery pushed with the freshly-minted token, plus the
    # add-on's reachable hostname + port read from /addons/self/info.
    assert len(sv.pushed_payloads) == 1
    payload = sv.pushed_payloads[0]
    assert payload["service"] == "socialhome"
    assert set(payload["config"].keys()) == {"host", "port", "token"}
    assert payload["config"]["host"] == "local-social-home"
    assert payload["config"]["port"] == 8099
    with open(token_file) as f:
        assert payload["config"]["token"] == f.read().strip()


async def test_run_is_idempotent(env):
    """Second run skips provisioning but still pushes discovery."""
    sv = _FakeSupervisor()
    users = _FakeUsers(owner_username="ha_admin")
    bs = _make_bootstrap(env, users=users, supervisor=sv)
    await bs.run()
    # Second time around: still pushes discovery, does not duplicate users.
    await _make_bootstrap(env, users=users, supervisor=sv).run()

    assert await env.db.fetchval("SELECT COUNT(*) FROM users") == 1
    assert (
        await env.db.fetchval(
            "SELECT COUNT(*) FROM api_tokens WHERE label=?",
            (INTEGRATION_TOKEN_LABEL,),
        )
        == 1
    )
    # Discovery pushed twice (once per run).
    assert len(sv.pushed_payloads) == 2


async def test_run_no_owner_skips_provisioning(env):
    """If the directory returns no owner, bootstrap skips provisioning entirely."""
    sv = _FakeSupervisor()
    bs = _make_bootstrap(
        env,
        users=_FakeUsers(owner_username=None),
        supervisor=sv,
    )

    await bs.run()

    assert await env.db.fetchval("SELECT COUNT(*) FROM users") == 0
    assert await bs._is_done() is False
    # No token file, so discovery push is skipped.
    assert sv.pushed_payloads == []


async def test_run_discovery_failure_does_not_raise(env):
    """A discovery push failure is logged, not raised."""
    sv = _FakeSupervisor(fail_discovery=True)
    bs = _make_bootstrap(env, supervisor=sv)
    # Should complete without raising even though push_discovery reports failure.
    await bs.run()
    # Still provisioned the owner regardless.
    assert await env.db.fetchval("SELECT COUNT(*) FROM users") == 1


async def test_run_discovery_skipped_when_token_file_missing(env):
    """With the bootstrap flag already set, if the token file is absent, discovery is skipped cleanly."""
    sv = _FakeSupervisor()
    bs = _make_bootstrap(env, supervisor=sv)
    await bs._mark_done()

    await bs.run()
    assert sv.pushed_payloads == []


async def test_run_discovery_skipped_when_self_info_unavailable(env):
    """If ``/addons/self/info`` returns no hostname/port we don't push
    a half-formed payload — the integration would fail at ``_validate``."""
    sv = _FakeSupervisor(self_info=None)
    bs = _make_bootstrap(env, supervisor=sv)
    await bs.run()
    # Owner was still provisioned + token was minted — we just
    # didn't advertise it this boot. A later boot with a working
    # Supervisor will push.
    assert await env.db.fetchval("SELECT COUNT(*) FROM users") == 1
    assert sv.pushed_payloads == []


async def test_bootstrap_flag_constant():
    """BOOTSTRAP_FLAG matches the historical migration key."""
    assert BOOTSTRAP_FLAG == "ha_bootstrap_done"


# ─── HA-side person rename follows the username (match by external_id) ──────


async def test_run_follows_ha_rename(env):
    """Seed an HA owner, then re-run with the HA person renamed (same
    external_id): the local row is RENAMED (matched by external_id, not a
    new row), a child row cascades, and a profile event federates."""
    seen: list = []
    env.bus.subscribe(UserProfileUpdated, lambda ev: seen.append(ev))

    # First boot — provisions `oldname` (external_id ha-1).
    await _make_bootstrap(
        env, users=_FakeUsers(owner_username="oldname", owner_external_id="ha-1")
    ).run()
    uid = await env.db.fetchval(
        "SELECT user_id FROM users WHERE username=?", ("oldname",)
    )
    assert uid is not None
    # Seed a child row that must cascade with the rename.
    await env.db.enqueue(
        "INSERT INTO presence(username, entity_id, state) VALUES(?,?,?)",
        ("oldname", "person.x", "home"),
    )
    seen.clear()

    # Second boot — HA renamed the person to `newname` (same external_id).
    await _make_bootstrap(
        env, users=_FakeUsers(owner_username="newname", owner_external_id="ha-1")
    ).run()

    assert (
        await env.db.fetchval(
            "SELECT COUNT(*) FROM users WHERE username=?", ("oldname",)
        )
        == 0
    )
    row = await env.db.fetchone(
        "SELECT user_id, source, external_id, is_admin FROM users WHERE username=?",
        ("newname",),
    )
    assert row is not None
    assert row["user_id"] == uid  # same row, renamed — user_id immutable
    assert row["source"] == "ha"
    assert row["external_id"] == "ha-1"
    assert row["is_admin"] == 1
    # Exactly one row for this external_id.
    assert (
        await env.db.fetchval(
            "SELECT COUNT(*) FROM users WHERE external_id=?", ("ha-1",), default=0
        )
        == 1
    )
    # Child row cascaded.
    pres = await env.db.fetchone(
        "SELECT username FROM presence WHERE entity_id=?", ("person.x",)
    )
    assert pres["username"] == "newname"
    # Rename federated.
    assert any(ev.username == "newname" and ev.user_id == uid for ev in seen)


async def test_run_rename_then_idempotent(env):
    """After a rename-follow, a further re-run with the same HA name is a
    no-op: no dupe row, no extra profile event."""
    await _make_bootstrap(
        env, users=_FakeUsers(owner_username="newname", owner_external_id="ha-1")
    ).run()

    seen: list = []
    env.bus.subscribe(UserProfileUpdated, lambda ev: seen.append(ev))
    await _make_bootstrap(
        env, users=_FakeUsers(owner_username="newname", owner_external_id="ha-1")
    ).run()

    assert (
        await env.db.fetchval(
            "SELECT COUNT(*) FROM users WHERE external_id=?", ("ha-1",), default=0
        )
        == 1
    )
    assert seen == []


async def test_run_first_provision_unchanged(env):
    """Unknown external_id → INSERT path: identity_anchor=username,
    source='ha', external_id preserved."""
    await _make_bootstrap(
        env, users=_FakeUsers(owner_username="freshuser", owner_external_id="ha-new")
    ).run()

    row = await env.db.fetchone(
        "SELECT identity_anchor, source, external_id FROM users WHERE username=?",
        ("freshuser",),
    )
    assert row is not None
    assert row["identity_anchor"] == "freshuser"
    assert row["source"] == "ha"
    assert row["external_id"] == "ha-new"


async def test_run_invalid_ha_name_keeps_old_username(env):
    """If the renamed HA person's name fails validation, the old username
    is kept (no crash, no rename, no event)."""
    await _make_bootstrap(
        env, users=_FakeUsers(owner_username="goodname", owner_external_id="ha-1")
    ).run()

    seen: list = []
    env.bus.subscribe(UserProfileUpdated, lambda ev: seen.append(ev))
    # "admin" is reserved → invalid username.
    await _make_bootstrap(
        env, users=_FakeUsers(owner_username="admin", owner_external_id="ha-1")
    ).run()

    # Old username kept; no row under the invalid name.
    assert (
        await env.db.fetchval(
            "SELECT COUNT(*) FROM users WHERE username=?", ("goodname",), default=0
        )
        == 1
    )
    assert (
        await env.db.fetchval(
            "SELECT COUNT(*) FROM users WHERE username=?", ("admin",), default=0
        )
        == 0
    )
    assert seen == []
