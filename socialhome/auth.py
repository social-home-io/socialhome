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

from .app_keys import media_signer_key
from .security import error_response

if TYPE_CHECKING:
    from .domain.user import User
    from .media_signer import MediaUrlSigner


log = logging.getLogger(__name__)


# Paths that are allowed without authentication. Anything matching one of
# these patterns bypasses the middleware entirely. Keep the list tight —
# every additional path is an attack surface.
_DEFAULT_PUBLIC_PATHS: tuple[str, ...] = (
    "/healthz",
    "/api/pairing/accept",  # pairing handshake — uses its own auth
    "/api/auth/token",  # standalone login — issues the token
    "/api/auth/redeem-password-reset",  # admin-issued reset → new password
    # First-boot wizard — the SPA hits these before it has a token.
    # The setup_service gate (`is_required`) inside each handler stops
    # them being usable after first boot, so leaving them public is safe.
    "/api/instance/config",
    "/api/setup/",
    "/federation/inbox/",  # federation inbound — envelope-signed
    "/.well-known/",
    # Bot-bridge space posts authenticate via a per-bot Bearer token that
    # is NOT an api_tokens row. The handler does its own lookup via
    # SpaceBotRepo.get_by_token_hash. DM bot-bridge posts use a normal
    # user token and go through the standard middleware.
    "/api/bot-bridge/spaces/",
    # SPA entry points — the browser fetches these before the user has
    # a token. ``routes.spa.mount_spa`` registers the matching handlers;
    # ``/`` itself is opened via the ``^/$`` regex below (the prefix
    # matcher would otherwise treat ``/`` as public-everything).
    "/manifest.json",
    "/sw.js",
    "/assets/",
)

#: Phase F — iCal subscription feeds carry a per-(user, space) token in
#: the query string because most desktop calendar clients refresh
#: without OAuth. The route handler validates the token; this regex
#: lets the auth middleware skip the standard Bearer-token requirement.
#:
#: ``^/$`` matches just the root URL — the SPA index. Using a regex
#: instead of adding ``/`` to :data:`_DEFAULT_PUBLIC_PATHS` keeps the
#: prefix matcher from turning every URL public.
_DEFAULT_PUBLIC_PATH_PATTERNS: tuple[str, ...] = (
    r"^/api/spaces/[^/]+/calendar/export\.ics$",
    # The pasteable code for one invite link. The TOKEN is the
    # credential — whoever holds it can already redeem the link — so
    # there is no session to require, and the handler answers a uniform
    # 404 for anything that is not a live link of ours. A regex, not a
    # ``/api/invite-links/`` prefix, so nothing else under that path
    # could ever become public by being added later.
    r"^/api/invite-links/[^/]+/code$",
    # App bundle files bypass bearer-auth; the view self-authorizes via a
    # prefix HMAC signature (entry load) or a path-scoped cookie (sub-resources).
    # ``/runtime`` is intentionally NOT public — it requires a bearer token.
    r"^/api/apps/[^/]+/bundle/",
    r"^/$",
    # SPA catchall — any non-``/api``, non-``/healthz``,
    # non-``/federation``, non-``/.well-known``, non-``/assets``,
    # non-``/manifest.json``, non-``/sw.js`` path is owned by the
    # SPA and renders ``index.html`` (see ``routes.spa.SpaCatchallView``).
    # ``/{name}.{ext}`` paths the SPA might own — e.g. a hypothetical
    # ``/robots.txt`` — are intentionally NOT matched so a missing
    # asset surfaces as 404 instead of an HTML shell.
    r"^/(?!api/|healthz$|federation/|\.well-known/|assets/|manifest\.json$|sw\.js$|favicon\.svg$)[^.]+$",
    # ``/favicon.svg`` is the only dotted root-level asset we serve from
    # the SPA bundle; the catchall regex above excludes paths containing
    # dots, so list the file explicitly here.
    r"^/favicon\.svg$",
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
    public_path_patterns: tuple[str, ...] = _DEFAULT_PUBLIC_PATH_PATTERNS,
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
    """Authenticate via HA's Ingress headers (``X-Remote-User-Name``).

    The HA Supervisor sits in front of us as a reverse proxy and
    validates the ingress session token *before* forwarding the
    request to our container. By the time bytes reach this handler
    Supervisor has already authenticated the user — we trust the
    ``X-Remote-User-Name`` header it stamps in. Supervisor sets it
    from authenticated session state and explicitly **strips** any
    client-supplied ``X-Remote-User-*`` header on the inbound path
    (see ``supervisor/api/ingress.py``), so the value cannot be
    spoofed by the browser. This matches the convention every other
    HA add-on follows (node-red, vscode, file-editor, ESPHome…).

    The threat model is "Supervisor proxied this request" — if an
    operator misconfigures the add-on so its internal port is reachable
    bypassing Supervisor, ``X-Remote-User-Name`` becomes spoofable, but
    the same is true for every HA add-on. Closing that hole is a
    Supervisor-level concern, not an add-on-level concern.
    """

    __slots__ = ("_user_repo",)

    def __init__(self, user_repo) -> None:
        self._user_repo = user_repo

    async def authenticate(self, request: "web.Request") -> AuthContext | None:
        # Only trust ``X-Remote-User-Name`` when HA Core's ingress
        # proxy is in the path. ``X-Hass-Source: core.ingress`` is set
        # by direct assignment in ``homeassistant/components/hassio/
        # ingress.py``, overwriting any client-supplied value, so its
        # presence proves the request went through Core's authentication.
        if request.headers.get("X-Hass-Source") != "core.ingress":
            return None
        username = request.headers.get("X-Remote-User-Name")
        if not username:
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


# Sentinel user_id stamped onto :class:`AuthContext` instances minted by
# :class:`SignedMediaStrategy`. The signed URL is a capability, not a user
# session — anything that downstream tries to query users by
# ``user_id == "__signed_url__"`` should miss the user table cleanly. Routes
# that serve signed media (the GET handlers under ``/api/media``,
# ``/api/users/{id}/picture``, ``/api/spaces/{id}/members/{user_id}/picture``)
# only call ``self.user`` for the auth check and never read ``user_id``.
SIGNED_URL_PRINCIPAL: str = "__signed_url__"

# Path families that can be reached via a signed URL. The pattern matches
# the canonical resource path only — query string is ignored. Any path that
# matches AND carries valid ``?exp=&sig=`` query params is authorised by
# :class:`SignedMediaStrategy`.
#
# Every media pattern below is a *per-resource* capability: the signature
# covers the full path, so a leaked URL exposes exactly one file.
# ``/api/map/tiles`` is deliberately broader — its coordinates live in the
# query string, which the HMAC does not cover, so one signature authorises
# every tile. That is the only workable shape (Leaflet substitutes
# ``{z}/{x}/{y}`` client-side and can never request a per-tile signature)
# and it is safe: the capability is "fetch public map tiles through this
# instance", which exposes no user data whatsoever.
_SIGNED_PATH_PATTERNS: tuple[str, ...] = (
    r"^/api/media/[^/]+$",
    r"^/api/users/[^/]+/picture$",
    r"^/api/spaces/[^/]+/members/[^/]+/picture$",
    r"^/api/spaces/[^/]+/cover$",
    r"^/api/spaces/[^/]+/icon$",
    r"^/api/map/tiles$",
)


class SignedMediaStrategy:
    """Authenticate browser-loaded media via short-lived ``?exp=&sig=``.

    Reads the :class:`MediaUrlSigner` from ``request.app[media_signer_key]``
    so the strategy can be instantiated at app-build time even though the
    signer itself is created later in ``_on_startup`` (it depends on the
    instance identity seed). If the signer isn't ready yet, this strategy
    declines and the chain falls back to bearer auth.
    """

    __slots__ = ("_compiled_patterns",)

    def __init__(self) -> None:
        self._compiled_patterns = tuple(re.compile(p) for p in _SIGNED_PATH_PATTERNS)

    async def authenticate(self, request: "web.Request") -> AuthContext | None:
        if not any(p.match(request.path) for p in self._compiled_patterns):
            return None
        signer: "MediaUrlSigner | None" = request.app.get(media_signer_key)
        if signer is None:
            return None
        exp = request.query.get("exp", "")
        sig = request.query.get("sig", "")
        if not exp or not sig:
            return None
        if not signer.verify(request.path, exp, sig):
            return None
        return AuthContext(
            user_id=SIGNED_URL_PRINCIPAL,
            username=SIGNED_URL_PRINCIPAL,
            is_admin=False,
            auth_method="signed_url",
            metadata={},
        )
