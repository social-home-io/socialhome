"""Tests for social_home.auth (authentication strategies)."""

from __future__ import annotations

import pytest

from social_home.auth import (
    BearerTokenStrategy,
    ChainedStrategy,
    HaIngressStrategy,
    sha256_token_hash,
)


class _FakeRequest:
    """Minimal stand-in for aiohttp.web.Request used by auth strategies."""

    def __init__(self, headers: dict | None = None, query: dict | None = None):
        self.headers = headers or {}
        self.query = query or {}


@pytest.fixture
async def env(tmp_dir):
    """Auth-focused env: real DB + UserService + a provisioned user with a token."""
    from social_home.crypto import generate_identity_keypair, derive_instance_id
    from social_home.db.database import AsyncDatabase
    from social_home.infrastructure.event_bus import EventBus
    from social_home.repositories.user_repo import SqliteUserRepo
    from social_home.services.user_service import UserService

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.kp = kp
    e.iid = iid
    e.bus = EventBus()
    e.user_repo = SqliteUserRepo(db)
    e.user_svc = UserService(e.user_repo, e.bus, own_instance_public_key=kp.public_key)

    e.user = await e.user_svc.provision(username="testuser", display_name="Test User")
    _, e.raw_token = await e.user_svc.create_api_token("testuser", label="test")
    e.token_hash = sha256_token_hash(e.raw_token)

    yield e
    await db.shutdown()


async def test_bearer_strategy_header(env):
    """Bearer token in Authorization header authenticates and returns AuthContext."""
    strategy = BearerTokenStrategy(env.user_repo)
    req = _FakeRequest(headers={"Authorization": f"Bearer {env.raw_token}"})
    ctx = await strategy.authenticate(req)
    assert ctx is not None
    assert ctx.username == "testuser"
    assert ctx.auth_method == "api_token"


async def test_bearer_strategy_query(env):
    """Token in ?token= query param authenticates (simulates WebSocket)."""
    strategy = BearerTokenStrategy(env.user_repo)
    req = _FakeRequest(query={"token": env.raw_token})
    ctx = await strategy.authenticate(req)
    assert ctx is not None
    assert ctx.username == "testuser"


async def test_bearer_strategy_bad_token(env):
    """Invalid token returns None rather than raising."""
    strategy = BearerTokenStrategy(env.user_repo)
    req = _FakeRequest(headers={"Authorization": "Bearer totally-invalid-token"})
    ctx = await strategy.authenticate(req)
    assert ctx is None


async def test_bearer_strategy_no_creds(env):
    """Missing Authorization header and no query param returns None."""
    strategy = BearerTokenStrategy(env.user_repo)
    req = _FakeRequest()
    ctx = await strategy.authenticate(req)
    assert ctx is None


async def test_ha_ingress_strategy(env):
    """X-Ingress-User + X-Ingress-Token headers authenticate the user."""
    strategy = HaIngressStrategy(env.user_repo)
    req = _FakeRequest(
        headers={
            "X-Ingress-User": "testuser",
            "X-Ingress-Token": "some-supervisor-token",
        }
    )
    ctx = await strategy.authenticate(req)
    assert ctx is not None
    assert ctx.username == "testuser"
    assert ctx.auth_method == "ha_ingress"


async def test_ha_ingress_missing_headers(env):
    """Missing X-Ingress-User or X-Ingress-Token returns None."""
    strategy = HaIngressStrategy(env.user_repo)

    ctx = await strategy.authenticate(
        _FakeRequest(headers={"X-Ingress-User": "testuser"})
    )
    assert ctx is None

    ctx2 = await strategy.authenticate(_FakeRequest(headers={"X-Ingress-Token": "tok"}))
    assert ctx2 is None

    ctx3 = await strategy.authenticate(_FakeRequest())
    assert ctx3 is None


async def test_chained_strategy(env):
    """First matching strategy wins; no match across all strategies returns None."""
    bearer = BearerTokenStrategy(env.user_repo)
    ingress = HaIngressStrategy(env.user_repo)
    chained = ChainedStrategy(bearer, ingress)

    req_bearer = _FakeRequest(headers={"Authorization": f"Bearer {env.raw_token}"})
    ctx = await chained.authenticate(req_bearer)
    assert ctx is not None
    assert ctx.username == "testuser"

    req_ingress = _FakeRequest(
        headers={
            "X-Ingress-User": "testuser",
            "X-Ingress-Token": "tok",
        }
    )
    ctx2 = await chained.authenticate(req_ingress)
    assert ctx2 is not None
    assert ctx2.username == "testuser"

    ctx3 = await chained.authenticate(_FakeRequest())
    assert ctx3 is None


# ── Middleware + helpers ──────────────────────────────────────────────────


async def test_require_auth_blocks_unauthenticated():
    """require_auth middleware returns 401 for unauthenticated requests."""
    from social_home.auth import require_auth
    from unittest.mock import AsyncMock, MagicMock

    class NullStrategy:
        async def authenticate(self, request):
            return None

    middleware = require_auth(NullStrategy())
    # Build a minimal mock request
    req = MagicMock()
    req.path = "/api/protected"
    handler = AsyncMock()

    resp = await middleware(req, handler)
    assert resp.status == 401
    handler.assert_not_called()


async def test_require_auth_passes_authenticated():
    """require_auth attaches AuthContext and calls handler."""
    from social_home.auth import require_auth, AuthContext
    from social_home.domain.user import User
    from unittest.mock import AsyncMock, MagicMock

    user = User(user_id="u1", username="admin", display_name="Admin", is_admin=True)
    ctx = AuthContext.from_user(user, auth_method="test")

    class GoodStrategy:
        async def authenticate(self, request):
            return ctx

    middleware = require_auth(GoodStrategy())
    req = MagicMock()
    req.path = "/api/protected"
    req.__setitem__ = MagicMock()
    handler = AsyncMock(return_value=MagicMock(status=200))

    _resp = await middleware(req, handler)
    handler.assert_called_once()
    req.__setitem__.assert_called_with("user", ctx)


async def test_require_auth_skips_public_paths():
    """Public paths bypass authentication entirely."""
    from social_home.auth import require_auth
    from unittest.mock import AsyncMock, MagicMock

    class NullStrategy:
        async def authenticate(self, request):
            return None

    middleware = require_auth(NullStrategy())
    req = MagicMock()
    req.path = "/healthz"
    handler = AsyncMock(return_value=MagicMock(status=200))

    _resp = await middleware(req, handler)
    handler.assert_called_once()  # bypassed auth


def test_current_user_raises_without_context():
    """current_user raises RuntimeError if no auth context attached."""
    from social_home.auth import current_user
    from unittest.mock import MagicMock

    req = MagicMock()
    req.get.return_value = None
    import pytest

    with pytest.raises(RuntimeError, match="auth middleware"):
        current_user(req)


def test_current_user_returns_context():
    """current_user returns the attached AuthContext."""
    from social_home.auth import current_user, AuthContext
    from social_home.domain.user import User
    from unittest.mock import MagicMock

    user = User(user_id="u1", username="a", display_name="A", is_admin=False)
    ctx = AuthContext.from_user(user, auth_method="test")
    req = MagicMock()
    req.get.return_value = ctx
    assert current_user(req) == ctx


def test_require_admin_non_admin_raises():
    """require_admin raises HTTPForbidden for non-admin user."""
    from social_home.auth import require_admin, AuthContext
    from social_home.domain.user import User
    from unittest.mock import MagicMock
    from aiohttp import web

    user = User(user_id="u1", username="a", display_name="A", is_admin=False)
    ctx = AuthContext.from_user(user, auth_method="test")
    req = MagicMock()
    req.get.return_value = ctx
    with pytest.raises(web.HTTPForbidden):
        require_admin(req)


def test_require_admin_admin_ok():
    """require_admin returns context for admin user."""
    from social_home.auth import require_admin, AuthContext
    from social_home.domain.user import User
    from unittest.mock import MagicMock

    user = User(user_id="u1", username="a", display_name="A", is_admin=True)
    ctx = AuthContext.from_user(user, auth_method="test")
    req = MagicMock()
    req.get.return_value = ctx
    assert require_admin(req) == ctx


def test_auth_context_from_user():
    """AuthContext.from_user populates all fields."""
    from social_home.auth import AuthContext
    from social_home.domain.user import User

    user = User(user_id="u1", username="test", display_name="Test", is_admin=True)
    ctx = AuthContext.from_user(user, auth_method="session", metadata={"k": "v"})
    assert ctx.user_id == "u1"
    assert ctx.is_admin
    assert ctx.auth_method == "session"
    assert ctx.metadata == {"k": "v"}
