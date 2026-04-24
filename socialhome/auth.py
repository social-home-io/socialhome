"""Authentication + request-context helpers (§platform/adapter).

The platform abstraction layer (§19895+) decides *how* a request is
authenticated — HA mode trusts the ingress headers; standalone mode
validates a session cookie or API token. This module provides:

* :class:`AuthContext` — the typed object representing the current user
  of a request (local or via API token).
* :func:`require_auth` — an aiohttp middleware that attaches an
  ``AuthContext`` to ``request["user"]`` or returns ``401``. The concrete
  authentication strategy is injected so unit tests can wire in a fake.
* :func:`current_user` — small convenience used by handlers to fetch the
  attached context without reaching into the aiohttp mapping.

The middleware never reads from the DB directly. It calls the
:class:`AuthStrategy` it was constructed with; concrete strategies live
next to the platform adapters.
"""

from __future__ import annotations

import hashlib

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol, runtime_checkable

from aiohttp import web

from .security import error_response

if TYPE_CHECKING:
    from .domain.user import User


log = logging.getLogger(__name__)


# Paths that are allowed without authentication. Anything matching one of
# these patterns bypasses the middleware entirely. Keep the list tight —
# every additional path is an attack surface.
_DEFAULT_PUBLIC_PATHS: tuple[str, ...] = (
    "/healthz",
    "/api/pairing/accept",  # pairing handshake — uses its own auth
    "/api/auth/token",  # standalone login — issues the token
    "/federation/inbox/",  # federation inbound — envelope-signed
    "/.well-known/",
    # Bot-bridge space posts authenticate via a per-bot Bearer token that
    # is NOT an api_tokens row. The handler does its own lookup via
    # SpaceBotRepo.get_by_token_hash. DM bot-bridge posts use a normal
    # user token and go through the standard middleware.
    "/api/bot-bridge/spaces/",
)


@dataclass(slots=True, frozen=True)
class AuthContext:
    """The authenticated principal for a request.

    Always derived from a :class:`~socialhome.domain.user.User` on this
    instance. Federation envelopes are NOT represented here — they go
    through the federation service, which has its own validation pipeline.
    """

    user_id: str
    username: str
    is_admin: bool

    #: How the request was authenticated. Mostly for telemetry / logs.
    auth_method: str  # "session" | "api_token" | "ha_ingress" | "standalone"

    #: Free-form metadata carried through from the strategy (e.g. the
    #: specific token id used). Always present, possibly empty.
    metadata: dict = None  # type: ignore[assignment]

    @classmethod
    def from_user(
        cls,
        user: "User",
        *,
        auth_method: str,
        metadata: dict | None = None,
    ) -> "AuthContext":
        return cls(
            user_id=user.user_id,
            username=user.username,
            is_admin=user.is_admin,
            auth_method=auth_method,
            metadata=metadata or {},
        )


@runtime_checkable
class AuthStrategy(Protocol):
    """Strategy interface for authenticating an inbound HTTP request.

    Implementations return an :class:`AuthContext` for authenticated
    requests and ``None`` for unauthenticated ones. They must never raise
    on a bad token / missing header — the middleware converts a ``None``
    return into a ``401``.
    """

    async def authenticate(self, request: "web.Request") -> AuthContext | None: ...


def sha256_token_hash(raw_token: str) -> str:
    """SHA-256 the raw token bytes — the hash form stored in ``api_tokens``."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def require_auth(
    strategy: AuthStrategy,
    *,
    public_paths: tuple[str, ...] = _DEFAULT_PUBLIC_PATHS,
    public_path_patterns: tuple[str, ...] = (),
):
    """Build an aiohttp middleware that gates handlers on authentication.

    * ``strategy`` — the :class:`AuthStrategy` that resolves the request
      to an :class:`AuthContext` (or ``None``).
    * ``public_paths`` — prefixes that bypass the middleware (default:
      healthcheck, pairing accept, federation inboxs, .well-known).
    * ``public_path_patterns`` — regex patterns; anything matching is
      also public. Use sparingly.

    The middleware places the context at ``request["user"]`` so handlers
    can read it via :func:`current_user` without importing this module.
    """
    compiled_patterns = tuple(re.compile(p) for p in public_path_patterns)

    def _is_public(path: str) -> bool:
        if any(path.startswith(p) for p in public_paths):
            return True
        return any(p.search(path) for p in compiled_patterns)

    @web.middleware
    async def middleware(
        request: "web.Request",
        handler: Callable[["web.Request"], Awaitable["web.StreamResponse"]],
    ) -> "web.StreamResponse":
        if _is_public(request.path):
            return await handler(request)

        ctx = await strategy.authenticate(request)
        if ctx is None:
            return error_response(401, "UNAUTHORIZED", "Authentication required.")
        request["user"] = ctx
        return await handler(request)

    return middleware


def current_user(request: "web.Request") -> AuthContext:
    """Return the :class:`AuthContext` attached by :func:`require_auth`.

    Raises :class:`RuntimeError` if called before the auth middleware has
    run — the route handler registration should guarantee that only
    protected endpoints call this.
    """
    ctx = request.get("user")
    if ctx is None:
        raise RuntimeError(
            "auth middleware did not populate request['user'] — is this path public?"
        )
    return ctx


def require_admin(request: "web.Request") -> AuthContext:
    """Return the context, raising ``web.HTTPForbidden`` if not admin."""
    ctx = current_user(request)
    if not ctx.is_admin:
        raise web.HTTPForbidden(reason="admin required")
    return ctx


# ─── Concrete strategies ─────────────────────────────────────────────────


class BearerTokenStrategy:
    """Authenticate via ``Authorization: Bearer <raw_token>``.

    The raw token is SHA-256 hashed and looked up in ``api_tokens`` via
    the :class:`AbstractUserRepo`. Tokens that are revoked or expired
    return ``None`` from the repo's ``get_user_by_token_hash``.
    """

    __slots__ = ("_user_repo",)

    def __init__(self, user_repo) -> None:
        self._user_repo = user_repo

    async def authenticate(self, request: "web.Request") -> AuthContext | None:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            # Also accept ``?token=`` for WebSocket upgrades — the ws.ts
            # client uses a query-string token since the browser
            # WebSocket API can't set custom headers.
            token = request.query.get("token")
            if not token:
                return None
        else:
            token = header[len("Bearer ") :].strip()
        if not token:
            return None

        user = await self._user_repo.get_user_by_token_hash(
            sha256_token_hash(token),
        )
        if user is None:
            return None
        return AuthContext.from_user(user, auth_method="api_token")


class HaIngressStrategy:
    """Authenticate via HA's Ingress headers (``X-Ingress-User`` + token).

    The Supervisor sets ``X-Ingress-User`` to the HA username and
    ``X-Ingress-Token`` to a per-session token. The integration is
    expected to validate ``X-Ingress-Token`` against the supervisor API
    — we keep it simple and trust the username field when a non-empty
    token is present. A production deployment can strengthen this by
    plumbing through the supervisor validation callback.
    """

    __slots__ = ("_user_repo", "_validate_token")

    def __init__(
        self,
        user_repo,
        *,
        validate_token: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._user_repo = user_repo
        self._validate_token = validate_token
        if validate_token is None:
            log.warning(
                "HaIngressStrategy: token validation is disabled — "
                "X-Ingress-User header is trusted without verification. "
                "This is only safe behind the HA Supervisor ingress proxy."
            )

    async def authenticate(self, request: "web.Request") -> AuthContext | None:
        username = request.headers.get("X-Ingress-User")
        token = request.headers.get("X-Ingress-Token")
        if not username or not token:
            return None
        if self._validate_token is not None:
            if not await self._validate_token(token):
                return None
        user = await self._user_repo.get(username)
        if user is None:
            return None
        return AuthContext.from_user(user, auth_method="ha_ingress")


class ChainedStrategy:
    """Try each strategy in order — first one to return a context wins."""

    __slots__ = ("_strategies",)

    def __init__(self, *strategies: AuthStrategy) -> None:
        self._strategies = strategies

    async def authenticate(self, request: "web.Request") -> AuthContext | None:
        for s in self._strategies:
            ctx = await s.authenticate(request)
            if ctx is not None:
                return ctx
        return None
