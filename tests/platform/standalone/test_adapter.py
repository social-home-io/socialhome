"""Integration tests for StandaloneAdapter using real SQLite (§platform/standalone).

All fixtures use a temporary on-disk database that goes through the full
migration pipeline — the same schema the production app uses.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.config import Config
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.platform.standalone.adapter import StandaloneAdapter


# ── Helpers ───────────────────────────────────────────────────────────────────


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def db(tmp_path):
    """Fully-migrated AsyncDatabase with instance_identity seeded."""
    database = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await database.startup()

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    await database.enqueue(
        "INSERT INTO instance_identity"
        "(instance_id, identity_private_key, identity_public_key, routing_secret)"
        " VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    yield database
    await database.shutdown()


@pytest.fixture
def cfg():
    """Minimal standalone Config."""
    return Config(mode="standalone", instance_name="Test Home")


@pytest.fixture
def adapter(db, cfg):
    """A StandaloneAdapter wired to the test database."""
    return StandaloneAdapter(db=db, config=cfg)


async def _add_user(
    db: AsyncDatabase,
    username: str = "alice",
    display_name: str = "Alice",
    is_admin: bool = False,
    email: str | None = None,
    notify_endpoint: str | None = None,
) -> None:
    """Insert a row into platform_users."""
    await db.enqueue(
        "INSERT INTO platform_users(username, display_name, is_admin, email, notify_endpoint)"
        " VALUES(?,?,?,?,?)",
        (username, display_name, 1 if is_admin else 0, email, notify_endpoint),
    )


async def _add_token(
    db: AsyncDatabase,
    username: str,
    raw_token: str,
    expires_at: str | None = None,
) -> None:
    """Insert a hashed token row into platform_tokens."""
    await db.enqueue(
        "INSERT INTO platform_tokens(token_id, username, token_hash, expires_at)"
        " VALUES(?,?,?,?)",
        (uuid.uuid4().hex, username, _sha256(raw_token), expires_at),
    )


# ── authenticate_bearer ───────────────────────────────────────────────────────


async def test_authenticate_bearer_valid_token(db, adapter):
    """A valid, unexpired token returns the owning ExternalUser."""
    await _add_user(db, username="alice", display_name="Alice", is_admin=False)
    raw = secrets.token_urlsafe(32)
    await _add_token(db, "alice", raw)

    user = await adapter.authenticate_bearer(raw)
    assert user is not None
    assert user.username == "alice"
    assert user.display_name == "Alice"
    assert user.is_admin is False


async def test_authenticate_bearer_unknown_token(db, adapter):
    """A token not in the database returns None."""
    await _add_user(db, username="alice", display_name="Alice")
    await _add_token(db, "alice", secrets.token_urlsafe(32))

    result = await adapter.authenticate_bearer("completely-wrong-token")
    assert result is None


async def test_authenticate_bearer_expired_token(db, adapter):
    """A token whose expires_at is in the past is rejected."""
    await _add_user(db, username="bob", display_name="Bob")
    raw = secrets.token_urlsafe(32)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _add_token(db, "bob", raw, expires_at=past)

    result = await adapter.authenticate_bearer(raw)
    assert result is None


async def test_authenticate_bearer_future_expiry_accepted(db, adapter):
    """A token whose expires_at is in the future is accepted."""
    await _add_user(db, username="carol", display_name="Carol")
    raw = secrets.token_urlsafe(32)
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    await _add_token(db, "carol", raw, expires_at=future)

    user = await adapter.authenticate_bearer(raw)
    assert user is not None
    assert user.username == "carol"


async def test_authenticate_bearer_no_expiry_accepted(db, adapter):
    """A token with NULL expires_at (never expires) is always accepted."""
    await _add_user(db, username="dave", display_name="Dave")
    raw = secrets.token_urlsafe(32)
    await _add_token(db, "dave", raw, expires_at=None)

    user = await adapter.authenticate_bearer(raw)
    assert user is not None
    assert user.username == "dave"


async def test_authenticate_bearer_is_admin_flag(db, adapter):
    """is_admin flag is correctly propagated from the database row."""
    await _add_user(
        db, username="superadmin", display_name="Super Admin", is_admin=True
    )
    raw = secrets.token_urlsafe(32)
    await _add_token(db, "superadmin", raw)

    user = await adapter.authenticate_bearer(raw)
    assert user is not None
    assert user.is_admin is True


# ── authenticate (request-level) ─────────────────────────────────────────────


class _FakeRequest:
    """Minimal stand-in for aiohttp.web.Request."""

    def __init__(
        self,
        headers: dict | None = None,
        query: dict | None = None,
    ) -> None:
        self.headers = headers or {}
        self.query = query or {}


async def test_authenticate_via_header(db, adapter):
    """authenticate() extracts a bearer token from Authorization header."""
    await _add_user(db, username="eve", display_name="Eve")
    raw = secrets.token_urlsafe(32)
    await _add_token(db, "eve", raw)

    req = _FakeRequest(headers={"Authorization": f"Bearer {raw}"})
    user = await adapter.authenticate(req)
    assert user is not None
    assert user.username == "eve"


async def test_authenticate_no_credentials(db, adapter):
    """authenticate() returns None when no token is present."""
    req = _FakeRequest()
    result = await adapter.authenticate(req)
    assert result is None


# ── list_external_users ───────────────────────────────────────────────────────


async def test_list_external_users_empty(db, adapter):
    """list_external_users returns an empty list when no users exist."""
    users = await adapter.list_external_users()
    assert users == []


async def test_list_external_users_multiple(db, adapter):
    """list_external_users returns all users from platform_users."""
    await _add_user(db, username="alice", display_name="Alice")
    await _add_user(db, username="bob", display_name="Bob")

    users = await adapter.list_external_users()
    usernames = {u.username for u in users}
    assert usernames == {"alice", "bob"}


# ── get_external_user ─────────────────────────────────────────────────────────


async def test_get_external_user_found(db, adapter):
    """get_external_user returns the correct user when they exist."""
    await _add_user(
        db,
        username="frank",
        display_name="Frank",
        email="frank@example.com",
        is_admin=False,
    )

    user = await adapter.get_external_user("frank")
    assert user is not None
    assert user.username == "frank"
    assert user.display_name == "Frank"
    assert user.email == "frank@example.com"


async def test_get_external_user_not_found(db, adapter):
    """get_external_user returns None for an unknown username."""
    result = await adapter.get_external_user("nobody")
    assert result is None


# ── get_instance_config ───────────────────────────────────────────────────────


async def test_get_instance_config_defaults_when_no_coords(db, adapter):
    """get_instance_config falls back to Config.instance_name when DB has no coords."""
    cfg = await adapter.get_instance_config()
    assert cfg.location_name == "Test Home"
    assert cfg.latitude == 0.0
    assert cfg.longitude == 0.0
    assert cfg.time_zone == "UTC"
    assert cfg.currency == "USD"


async def test_get_instance_config_reads_from_db(db, adapter):
    """get_instance_config returns DB-stored coordinates when present."""
    await db.enqueue(
        "UPDATE instance_identity SET home_lat=?, home_lon=?, home_label=? WHERE id='self'",
        (51.5074, -0.1278, "London"),
    )

    cfg = await adapter.get_instance_config()
    assert cfg.location_name == "London"
    assert cfg.latitude == 51.5074
    assert cfg.longitude == -0.1278


# ── update_location ───────────────────────────────────────────────────────────


async def test_update_location_persists(db, adapter):
    """update_location writes coords to DB and returns updated InstanceConfig."""
    result = await adapter.update_location(48.8566, 2.3522, "Paris")

    assert result.location_name == "Paris"
    assert result.latitude == 48.8566
    assert result.longitude == 2.3522

    # Verify it was actually written.
    cfg_after = await adapter.get_instance_config()
    assert cfg_after.location_name == "Paris"
    assert cfg_after.latitude == 48.8566
    assert cfg_after.longitude == 2.3522


async def test_update_location_truncates_precision(db, adapter):
    """update_location truncates coordinates to 4 decimal places."""
    result = await adapter.update_location(48.856612345, 2.352212345, "Paris Precise")

    assert result.latitude == round(48.856612345, 4)
    assert result.longitude == round(2.352212345, 4)


# ── STT (v1: unsupported) ─────────────────────────────────────────────────────


async def test_supports_stt_is_false(adapter):
    """Standalone has no STT backend in v1."""
    assert adapter.supports_stt is False


async def test_stream_transcribe_audio_raises(adapter):
    """Streaming STT raises NotImplementedError on standalone."""

    async def _audio():
        yield b"x"

    with pytest.raises(NotImplementedError):
        await adapter.stream_transcribe_audio(_audio())


# ── get_federation_base (§11) ─────────────────────────────────────────────────


async def test_get_federation_base_returns_none_when_unset(db, cfg):
    """No [standalone].external_url → None."""
    adapter = StandaloneAdapter(db=db, config=cfg)
    assert await adapter.get_federation_base() is None


async def test_get_federation_base_composes_inbox_path(db, cfg):
    """external_url gets /federation/inbox appended."""
    adapter = StandaloneAdapter(
        db=db,
        config=cfg,
        options={"external_url": "https://social.example.com"},
    )
    assert (
        await adapter.get_federation_base()
        == "https://social.example.com/federation/inbox"
    )


async def test_get_federation_base_strips_trailing_slash(db, cfg):
    """Trailing slashes on external_url don't produce double-slashes."""
    adapter = StandaloneAdapter(
        db=db,
        config=cfg,
        options={"external_url": "https://social.example.com/"},
    )
    assert (
        await adapter.get_federation_base()
        == "https://social.example.com/federation/inbox"
    )


async def test_get_federation_base_empty_string_is_none(db, cfg):
    adapter = StandaloneAdapter(
        db=db,
        config=cfg,
        options={"external_url": ""},
    )
    assert await adapter.get_federation_base() is None


# ── First-boot admin provisioning (§platform/standalone) ─────────────────────


async def test_provision_admin_seeds_user_when_empty(db, adapter):
    """An empty platform_users gets a fresh admin row + a matching users row."""
    created = await adapter.provision_admin(username="admin", password="secret123")
    assert created is True
    pu = await db.fetchone("SELECT * FROM platform_users WHERE username='admin'")
    assert pu is not None
    assert pu["is_admin"] == 1
    u = await db.fetchone("SELECT * FROM users WHERE username='admin'")
    assert u is not None
    assert u["is_admin"] == 1
    assert StandaloneAdapter._verify_password("secret123", pu["password_hash"])


async def test_provision_admin_is_idempotent(db, adapter):
    """Re-running with a populated platform_users is a no-op."""
    await _add_user(db, "alice")
    created = await adapter.provision_admin(username="admin", password="irrelevant")
    assert created is False
    rows = await db.fetchall("SELECT username FROM platform_users")
    assert {r["username"] for r in rows} == {"alice"}


async def test_provision_admin_requires_password(adapter):
    with pytest.raises(ValueError, match="non-empty password"):
        await adapter.provision_admin(username="admin", password="")


async def test_provision_admin_respects_username_override(db, adapter):
    await adapter.provision_admin(username="owner", password="pw")
    pu = await db.fetchone("SELECT * FROM platform_users WHERE username='owner'")
    assert pu is not None
    u = await db.fetchone("SELECT * FROM users WHERE username='owner'")
    assert u is not None
    assert u["user_id"] == "uid-owner"


# ── issue_bearer_token ↔ api_tokens mirror (§platform/standalone) ────────────


async def test_issue_bearer_token_mirrors_to_api_tokens(db, adapter):
    """Successful login writes the token hash to BOTH platform_tokens and api_tokens.

    Without the api_tokens mirror, BearerTokenStrategy (which joins
    users → api_tokens) wouldn't accept the freshly-issued token and
    GET /api/me would 401 immediately after login.
    """
    await adapter.provision_admin(username="admin", password="pw-1")
    raw = await adapter.issue_bearer_token("admin", "pw-1")
    assert raw is not None
    h = _sha256(raw)
    pt = await db.fetchone(
        "SELECT username FROM platform_tokens WHERE token_hash=?",
        (h,),
    )
    at = await db.fetchone(
        "SELECT user_id FROM api_tokens WHERE token_hash=?",
        (h,),
    )
    assert pt is not None and pt["username"] == "admin"
    assert at is not None and at["user_id"] == "uid-admin"


async def test_issue_bearer_token_skips_api_mirror_when_no_users_row(db, adapter):
    """Defensive: if the bootstrap didn't create the users row (out-of-band
    deployment), platform_tokens still gets the row but api_tokens is
    skipped silently rather than crashing."""
    await db.enqueue(
        "INSERT INTO platform_users(username, display_name, is_admin, password_hash) "
        "VALUES('legacy', 'Legacy', 1, ?)",
        (StandaloneAdapter.hash_password("pw"),),
    )
    raw = await adapter.issue_bearer_token("legacy", "pw")
    assert raw is not None
    h = _sha256(raw)
    pt = await db.fetchone(
        "SELECT username FROM platform_tokens WHERE token_hash=?",
        (h,),
    )
    at = await db.fetchone(
        "SELECT user_id FROM api_tokens WHERE token_hash=?",
        (h,),
    )
    assert pt is not None
    assert at is None


async def test_issue_bearer_token_invalid_credentials_returns_none(db, adapter):
    await adapter.provision_admin(username="admin", password="pw-1")
    assert await adapter.issue_bearer_token("admin", "wrong") is None
    assert await adapter.issue_bearer_token("ghost", "pw-1") is None
