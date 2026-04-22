"""Tests for socialhome.platform.ha.bootstrap."""

from __future__ import annotations

import os

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.platform.ha.bootstrap import (
    BOOTSTRAP_FLAG,
    INTEGRATION_TOKEN_FILENAME,
    INTEGRATION_TOKEN_LABEL,
    HaBootstrap,
)


# ─── Fakes ───────────────────────────────────────────────────────────────


class _FakeSupervisor:
    """In-process :class:`SupervisorClient` substitute for tests."""

    def __init__(
        self,
        *,
        owner_username: str | None = "ha_admin",
        fail_discovery: bool = False,
    ) -> None:
        self.owner_username = owner_username
        self.fail_discovery = fail_discovery
        self.pushed_payloads: list[dict] = []

    async def get_owner_username(self) -> str | None:
        return self.owner_username

    async def push_discovery(self, payload: dict) -> bool:
        self.pushed_payloads.append(payload)
        return not self.fail_discovery


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

    class Env:
        pass

    e = Env()
    e.db = db
    e.data_dir = str(data_dir)
    e.kp = kp
    e.iid = iid
    yield e
    await db.shutdown()


# ─── Individual step helpers ─────────────────────────────────────────────


async def test_provision_admin_idempotent(env):
    """Admin provisioned once; second call is a no-op (row count stays at 1)."""
    bs = HaBootstrap(env.db, _FakeSupervisor(), env.data_dir)

    await bs._provision_admin("ha_owner")
    row = await env.db.fetchone(
        "SELECT user_id, is_admin FROM users WHERE username=?",
        ("ha_owner",),
    )
    assert row is not None
    assert row["is_admin"] == 1

    await bs._provision_admin("ha_owner")
    count = await env.db.fetchval(
        "SELECT COUNT(*) FROM users WHERE username=?",
        ("ha_owner",),
        default=0,
    )
    assert count == 1


async def test_config_flag_helpers(env):
    """_is_done / _mark_done round-trip through instance_config."""
    bs = HaBootstrap(env.db, _FakeSupervisor(), env.data_dir)

    assert await bs._is_done() is False
    await bs._mark_done()
    assert await bs._is_done() is True


async def test_generate_integration_token_writes_file(env):
    """Token is persisted in api_tokens and written to disk (mode 0600)."""
    bs = HaBootstrap(env.db, _FakeSupervisor(), env.data_dir)
    await bs._provision_admin("ha_owner")

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
    sv = _FakeSupervisor(owner_username="ha_admin")
    bs = HaBootstrap(env.db, sv, env.data_dir)

    await bs.run()

    # Admin provisioned
    row = await env.db.fetchone(
        "SELECT is_admin FROM users WHERE username=?",
        ("ha_admin",),
    )
    assert row is not None and row["is_admin"] == 1

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

    # Discovery pushed with the freshly-minted token — no url field.
    assert len(sv.pushed_payloads) == 1
    payload = sv.pushed_payloads[0]
    assert payload["service"] == "socialhome"
    assert set(payload["config"].keys()) == {"token"}
    with open(token_file) as f:
        assert payload["config"]["token"] == f.read().strip()


async def test_run_is_idempotent(env):
    """Second run skips provisioning but still pushes discovery."""
    sv = _FakeSupervisor(owner_username="ha_admin")
    bs = HaBootstrap(env.db, sv, env.data_dir)
    await bs.run()
    # Second time around: still pushes discovery, does not duplicate users.
    await HaBootstrap(env.db, sv, env.data_dir).run()

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
    """If supervisor returns no owner, bootstrap skips provisioning entirely."""
    sv = _FakeSupervisor(owner_username=None)
    bs = HaBootstrap(env.db, sv, env.data_dir)

    await bs.run()

    assert await env.db.fetchval("SELECT COUNT(*) FROM users") == 0
    assert await bs._is_done() is False
    # No token file, so discovery push is skipped.
    assert sv.pushed_payloads == []


async def test_run_discovery_failure_does_not_raise(env):
    """A discovery push failure is logged, not raised."""
    sv = _FakeSupervisor(owner_username="ha_admin", fail_discovery=True)
    bs = HaBootstrap(env.db, sv, env.data_dir)
    # Should complete without raising even though push_discovery reports failure.
    await bs.run()
    # Still provisioned the owner regardless.
    assert await env.db.fetchval("SELECT COUNT(*) FROM users") == 1


async def test_run_discovery_skipped_when_token_file_missing(env):
    """With the bootstrap flag already set, if the token file is absent, discovery is skipped cleanly."""
    sv = _FakeSupervisor(owner_username="ha_admin")
    bs = HaBootstrap(env.db, sv, env.data_dir)
    await bs._mark_done()

    await bs.run()
    assert sv.pushed_payloads == []


async def test_bootstrap_flag_constant():
    """BOOTSTRAP_FLAG matches the historical migration key."""
    assert BOOTSTRAP_FLAG == "ha_bootstrap_done"
