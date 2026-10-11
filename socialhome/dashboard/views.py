"""Household dashboard pages — login, setup link, logout, home.

Every view is server-rendered HTML; the session guard in
:mod:`socialhome.dashboard.app` has already decided whether the request
may reach a non-public view.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from aiohttp import web

from ..rate_limiter import client_bucket
from ..services.dashboard_settings_service import DashboardSettingsService
from .auth import COOKIE_NAME, KEEP_TTL_S, DashboardAuth
from .keys import AUTH_KEY, HOUSEHOLD_NAME_KEY, RENDERER_KEY, SETTINGS_KEY
from .render import DashboardRenderer

log = logging.getLogger(__name__)

__all__ = [
    "STATIC_DIR",
    "EnterView",
    "HealthView",
    "HomeView",
    "LoginView",
    "LogoutView",
    "clear_session_cookie",
    "html_response",
    "redirect",
    "register_routes",
    "safe_next",
    "set_session_cookie",
]

STATIC_DIR = Path(__file__).parent / "static"

_MAX_NEXT_LEN = 2048


def safe_next(raw: object) -> str:
    """``raw`` if it is a same-site path, else ``"/"`` (open-redirect guard).

    Accepted: printable ASCII starting with exactly one ``/`` and holding
    no backslash — so no scheme (``https:``), no protocol-relative
    ``//host``, no ``/\\host`` (which browsers treat as ``//host``) and
    no CR/LF header splitting.
    """
    if not isinstance(raw, str) or not raw or len(raw) > _MAX_NEXT_LEN:
        return "/"
    if raw[0] != "/" or raw.startswith("//") or "\\" in raw:
        return "/"
    if any(not 0x20 < ord(c) < 0x7F for c in raw):
        return "/"
    return raw


def html_response(
    text: str, *, status: int = 200, headers: dict[str, str] | None = None
) -> web.Response:
    return web.Response(
        text=text,
        status=status,
        content_type="text/html",
        charset="utf-8",
        headers=headers,
    )


def redirect(location: str, *, status: int = 303) -> web.Response:
    return web.Response(status=status, headers={"Location": location})


def set_session_cookie(
    response: web.StreamResponse, request: web.BaseRequest, value: str, *, keep: bool
) -> None:
    """Attach the session cookie: persistent (one year) only when ``keep``."""
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=KEEP_TTL_S if keep else None,
        path="/",
        secure=request.secure,
        httponly=True,
        samesite="Strict",
    )


def clear_session_cookie(
    response: web.StreamResponse, request: web.BaseRequest
) -> None:
    response.del_cookie(
        COOKIE_NAME,
        path="/",
        secure=request.secure,
        httponly=True,
        samesite="Strict",
    )


class _DashboardView(web.View):
    @property
    def auth(self) -> DashboardAuth:
        return self.request.app[AUTH_KEY]

    @property
    def settings(self) -> DashboardSettingsService:
        return self.request.app[SETTINGS_KEY]

    @property
    def renderer(self) -> DashboardRenderer:
        return self.request.app[RENDERER_KEY]

    @property
    def client_key(self) -> str:
        # The socket peer only — never X-Forwarded-For, which anyone can set.
        return client_bucket(self.request.remote)

    async def render(
        self, template: str, *, status: int = 200, **ctx: object
    ) -> web.Response:
        locale = (await self.settings.get()).locale
        household = await self.request.app[HOUSEHOLD_NAME_KEY]()
        text = self.renderer.render(template, locale=locale, household=household, **ctx)
        return html_response(text, status=status)


class LoginView(_DashboardView):
    """``GET`` shows the password form, ``POST`` checks it."""

    async def _form(
        self,
        *,
        status: int,
        next_path: str,
        keep: bool = True,
        error_key: str | None = None,
        show_form: bool = True,
        **fmt: object,
    ) -> web.Response:
        error: str | None = None
        if error_key is not None:
            locale = (await self.settings.get()).locale
            error = self.renderer.gettext(error_key, locale, **fmt)
        return await self.render(
            "login.html.j2",
            status=status,
            next_path=next_path,
            keep=keep,
            error=error,
            show_form=show_form,
        )

    async def get(self) -> web.Response:
        next_path = safe_next(self.request.query.get("next"))
        claims = await self.auth.verify(self.request.cookies.get(COOKIE_NAME))
        if claims is not None:
            return redirect(next_path)
        if (await self.settings.get()).password_set_at is None:
            return await self._form(
                status=503,
                next_path=next_path,
                error_key="dashboard.login.not_set_up",
                show_form=False,
            )
        return await self._form(status=200, next_path=next_path)

    async def post(self) -> web.Response:
        form = await self.request.post()
        next_path = safe_next(form.get("next"))
        keep = form.get("keep") is not None
        key = self.client_key

        if (await self.settings.get()).password_set_at is None:
            return await self._form(
                status=503,
                next_path=next_path,
                error_key="dashboard.login.not_set_up",
                show_form=False,
            )

        # Check and count the attempt with no ``await`` in between, and
        # *before* the slow password check (scrypt in a thread): a burst
        # of concurrent guesses must not all slip past the throttle while
        # earlier ones are still being verified. A success clears it.
        remaining = self.auth.throttle_remaining(key)
        if remaining:
            response = await self._form(
                status=429,
                next_path=next_path,
                keep=keep,
                error_key="dashboard.login.too_many_tries",
                minutes=math.ceil(remaining / 60),
            )
            response.headers["Retry-After"] = str(remaining)
            return response
        self.auth.record_failure(key)
        password = form.get("password")
        if not isinstance(password, str) or not await self.settings.verify_password(
            password
        ):
            log.info("household dashboard: wrong password from %s", key)
            return await self._form(
                status=401,
                next_path=next_path,
                keep=keep,
                error_key="dashboard.login.wrong_password",
            )

        self.auth.record_success(key)
        response = redirect(next_path)
        set_session_cookie(
            response, self.request, await self.auth.issue(keep), keep=keep
        )
        return response


class EnterView(_DashboardView):
    """``GET /enter?t=<token>`` — the one-time tablet setup link (QR)."""

    async def get(self) -> web.Response:
        key = self.client_key
        if self.auth.throttle_remaining(key):
            # Don't even try the token — a locked-out client can't spend it.
            return redirect("/login")
        # Counted up front, like a login attempt (see ``LoginView.post``).
        self.auth.record_failure(key)
        token = self.request.query.get("t", "")
        if not token or not await self.settings.consume_setup_token(token):
            return redirect("/login")
        self.auth.record_success(key)
        response = redirect("/")
        set_session_cookie(
            response, self.request, await self.auth.issue(True), keep=True
        )
        return response


class LogoutView(_DashboardView):
    """``POST /logout`` — forget this tablet's session."""

    async def post(self) -> web.Response:
        response = redirect("/login")
        clear_session_cookie(response, self.request)
        return response


class HomeView(_DashboardView):
    """``GET /`` — placeholder until the real overview ships."""

    async def get(self) -> web.Response:
        return await self.render("placeholder.html.j2")


class HealthView(web.View):
    async def get(self) -> web.Response:
        return web.Response(text="ok")


def register_routes(app: web.Application) -> None:
    app.router.add_view("/", HomeView)
    app.router.add_view("/login", LoginView)
    app.router.add_view("/enter", EnterView)
    app.router.add_view("/logout", LogoutView)
    app.router.add_view("/healthz", HealthView)
    app.router.add_static("/static/", STATIC_DIR)
