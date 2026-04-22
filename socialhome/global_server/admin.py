"""GFS admin auth — bcrypt password + session cookie + middleware (§24.9).

The GFS admin portal is a single-operator surface. One bcrypt-hashed
password lives in ``server_config('admin_password_hash')`` (seeded via
``socialhome-global-server --set-password`` which upserts it atomically).
On successful login a 256-bit random session token is persisted with a
one-hour expiry and returned as an ``HttpOnly; Secure; SameSite=Strict``
cookie. Every ``/admin/api/*`` route passes through :func:`build_admin_middleware`
which validates the cookie against the ``admin_sessions`` table.

Brute-force: 5 failed attempts per IP in a rolling 15-minute window →
HTTP 429 with ``Retry-After: 900``.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import TYPE_CHECKING

import bcrypt
from aiohttp import web

if TYPE_CHECKING:
    from .repositories import AbstractGfsAdminRepo


log = logging.getLogger(__name__)


#: Session lifetime (spec §24.9 — 1 hour).
SESSION_TTL_SECONDS: int = 3600
#: Brute-force window (spec §24.9 — 5 attempts / 15 minutes).
BRUTE_FORCE_WINDOW_SECONDS: int = 15 * 60
BRUTE_FORCE_MAX_ATTEMPTS: int = 5
#: Cookie name.
SESSION_COOKIE: str = "gfs_session"


# ─── Password helpers ───────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Return a bcrypt hash as an ASCII string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time compare via bcrypt."""
    if not password or not stored_hash:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            stored_hash.encode("ascii"),
        )
    except ValueError, TypeError:
        return False


# ─── AdminAuth service ─────────────────────────────────────────────────


class AdminAuth:
    """Admin login / logout / session-check gate for the GFS portal."""

    __slots__ = ("_admin_repo",)

    def __init__(self, admin_repo: "AbstractGfsAdminRepo") -> None:
        self._admin_repo = admin_repo

    async def login(
        self,
        password: str,
        client_ip: str,
    ) -> tuple[str | None, str]:
        """Attempt a login.

        Returns ``(token, status)`` where ``status`` is one of
        ``"ok" | "locked" | "invalid" | "disabled"``.
        """
        # Admin disabled if no hash configured.
        stored = await self._admin_repo.get_config("admin_password_hash")
        if not stored:
            return None, "disabled"

        now = int(time.time())
        # Brute-force check.
        since = now - BRUTE_FORCE_WINDOW_SECONDS
        fails = await self._admin_repo.count_failed_attempts(
            client_ip,
            since=since,
        )
        if fails >= BRUTE_FORCE_MAX_ATTEMPTS:
            log.warning(
                "GFS admin login rate-limited for ip=%s (%d recent failures)",
                client_ip,
                fails,
            )
            return None, "locked"

        if not verify_password(password, stored):
            await self._admin_repo.record_login_attempt(client_ip)
            return None, "invalid"

        token = secrets.token_urlsafe(32)
        await self._admin_repo.create_session(
            token,
            expires_at=now + SESSION_TTL_SECONDS,
        )
        return token, "ok"

    async def check(self, token: str | None) -> bool:
        """Return ``True`` iff ``token`` is a valid unexpired session."""
        if not token:
            return False
        session = await self._admin_repo.get_session(token)
        if session is None:
            return False
        if session.expires_at < int(time.time()):
            # Expired — purge and reject.
            await self._admin_repo.delete_session(token)
            return False
        return True

    async def logout(self, token: str | None) -> None:
        if token:
            await self._admin_repo.delete_session(token)


# ─── aiohttp middleware ─────────────────────────────────────────────────


def build_admin_middleware(auth: AdminAuth):
    """Return an aiohttp middleware that guards ``/admin/api/*``.

    The static ``/admin`` HTML page is NOT gated — a logged-out user
    still gets the page so the login form renders. Every JSON API call
    under ``/admin/api/`` demands a valid session cookie.
    """

    @web.middleware
    async def _admin_middleware(request: web.Request, handler):
        path = request.rel_url.path
        if not path.startswith("/admin/api/"):
            return await handler(request)

        cookie = request.cookies.get(SESSION_COOKIE)
        ok = await auth.check(cookie)
        if not ok:
            return web.json_response(
                {"error": "unauthorized"},
                status=401,
            )
        return await handler(request)

    return _admin_middleware


# ─── Route handlers ─────────────────────────────────────────────────────


def _client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        # Use the leftmost entry — the original client.
        return fwd.split(",")[0].strip()
    peer = request.transport.get_extra_info("peername") if request.transport else None
    if peer:
        return str(peer[0])
    return "unknown"


async def handle_login(request: web.Request) -> web.Response:
    """``POST /admin/login`` — body ``{"password": "..."}``. Sets the cookie."""
    auth: AdminAuth = request.app["admin_auth"]
    try:
        body = await request.json()
        password = str(body.get("password") or "")
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")

    token, status = await auth.login(password, _client_ip(request))
    if status == "locked":
        resp = web.json_response(
            {"error": "rate_limited"},
            status=429,
        )
        resp.headers["Retry-After"] = str(BRUTE_FORCE_WINDOW_SECONDS)
        return resp
    if status == "disabled":
        return web.json_response(
            {
                "error": "admin_disabled",
                "detail": "Set an admin password via `socialhome-global-server --set-password`",
            },
            status=503,
        )
    if status != "ok" or token is None:
        return web.json_response({"error": "invalid_credentials"}, status=401)

    resp = web.json_response({"status": "ok"})
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        path="/admin",
        httponly=True,
        secure=request.secure,
        samesite="Strict",
    )
    return resp


async def handle_logout(request: web.Request) -> web.Response:
    auth: AdminAuth = request.app["admin_auth"]
    token = request.cookies.get(SESSION_COOKIE)
    await auth.logout(token)
    resp = web.json_response({"status": "ok"})
    resp.del_cookie(SESSION_COOKIE, path="/admin")
    return resp
