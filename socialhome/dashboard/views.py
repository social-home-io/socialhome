"""Household dashboard pages — login, setup link, logout, home.

Every view is server-rendered HTML; the session guard in
:mod:`socialhome.dashboard.app` has already decided whether the request
may reach a non-public view.
"""

from __future__ import annotations

import logging
import math
from urllib.parse import urlsplit

from aiohttp import web

from ..rate_limiter import client_bucket
from ..services.dashboard_settings_service import DashboardSettingsService
from .auth import COOKIE_NAME, KEEP_TTL_S, DashboardAuth
from .keys import AUTH_KEY, HOUSEHOLD_NAME_KEY, RENDERER_KEY, SETTINGS_KEY
from .render import DashboardRenderer

log = logging.getLogger(__name__)

__all__ = [
    "EnterView",
    "HealthView",
    "HomeView",
    "LoginView",
    "LogoutView",
    "StaticView",
    "clear_session_cookie",
    "cookie_secure",
    "html_response",
    "redirect",
    "register_routes",
    "safe_next",
    "set_session_cookie",
]

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


async def cookie_secure(request: web.Request) -> bool:
    """Mark the cookie ``Secure``? Yes over TLS — and also for a request
    to the admin-set ``https://`` address: behind a TLS-terminating reverse
    proxy (which keeps ``Host``) the request reaches us as plain HTTP, but
    the browser is on HTTPS. A tablet opening the LAN address directly is
    on plain HTTP, where a ``Secure`` cookie would be dropped and login
    would loop — so the address only counts when the request names it."""
    if request.secure:
        return True
    address = (await request.app[SETTINGS_KEY].get()).address
    if address is None or not address.startswith("https://"):
        return False
    return _hostname(request.host) == _hostname(urlsplit(address).netloc)


def _hostname(netloc: str) -> str:
    """The host part of ``netloc`` (no port), lower-case, no root dot."""
    try:
        name = urlsplit(f"//{netloc}").hostname or ""
    except ValueError:
        return ""
    return name.rstrip(".")


async def set_session_cookie(
    response: web.StreamResponse, request: web.Request, value: str, *, keep: bool
) -> None:
    """Attach the session cookie: persistent (one year) only when ``keep``."""
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=KEEP_TTL_S if keep else None,
        path="/",
        secure=await cookie_secure(request),
        httponly=True,
        samesite="Strict",
    )


async def clear_session_cookie(
    response: web.StreamResponse, request: web.Request
) -> None:
    response.del_cookie(
        COOKIE_NAME,
        path="/",
        secure=await cookie_secure(request),
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
        self, template: str, *, locale: str, status: int = 200, **ctx: object
    ) -> web.Response:
        household = await self.request.app[HOUSEHOLD_NAME_KEY]()
        text = self.renderer.render(template, locale=locale, household=household, **ctx)
        return html_response(text, status=status)

    async def login_form(
        self,
        *,
        locale: str,
        status: int,
        next_path: str = "/",
        keep: bool = True,
        error_key: str | None = None,
        show_form: bool = True,
        **fmt: object,
    ) -> web.Response:
        error = (
            None
            if error_key is None
            else self.renderer.gettext(error_key, locale, **fmt)
        )
        return await self.render(
            "login.html.j2",
            locale=locale,
            status=status,
            next_path=next_path,
            keep=keep,
            error=error,
            show_form=show_form,
        )

    async def too_many_tries(
        self, *, locale: str, wait: int, next_path: str = "/", keep: bool = True
    ) -> web.Response:
        response = await self.login_form(
            locale=locale,
            status=429,
            next_path=next_path,
            keep=keep,
            error_key="dashboard.login.too_many_tries",
            minutes=math.ceil(wait / 60),
        )
        response.headers["Retry-After"] = str(wait)
        return response


class LoginView(_DashboardView):
    """``GET`` shows the password form, ``POST`` checks it."""

    async def get(self) -> web.Response:
        next_path = safe_next(self.request.query.get("next"))
        claims = await self.auth.verify(self.request.cookies.get(COOKIE_NAME))
        if claims is not None:
            return redirect(next_path)
        settings = await self.settings.get()
        if settings.password_set_at is None:
            return await self.login_form(
                locale=settings.locale,
                status=503,
                next_path=next_path,
                error_key="dashboard.login.not_set_up",
                show_form=False,
            )
        return await self.login_form(
            locale=settings.locale, status=200, next_path=next_path
        )

    async def post(self) -> web.Response:
        form = await self.request.post()
        next_path = safe_next(form.get("next"))
        keep = form.get("keep") is not None
        key = self.client_key
        settings = await self.settings.get()
        locale = settings.locale

        if settings.password_set_at is None:
            return await self.login_form(
                locale=locale,
                status=503,
                next_path=next_path,
                error_key="dashboard.login.not_set_up",
                show_form=False,
            )

        # Checked and counted with no ``await`` in between, before the slow
        # password check — see :meth:`DashboardAuth.begin_attempt`.
        wait = self.auth.begin_attempt(key)
        if wait:
            return await self.too_many_tries(
                locale=locale, wait=wait, next_path=next_path, keep=keep
            )
        password = form.get("password")
        if not isinstance(password, str) or not await self.auth.check_password(
            password
        ):
            log.info("household dashboard: wrong password from %s", key)
            return await self.login_form(
                locale=locale,
                status=401,
                next_path=next_path,
                keep=keep,
                error_key="dashboard.login.wrong_password",
            )

        self.auth.record_success(key)
        response = redirect(next_path)
        await set_session_cookie(
            response, self.request, await self.auth.issue(keep), keep=keep
        )
        return response


class EnterView(_DashboardView):
    """The one-time tablet setup link (QR): ``/enter?t=<token>``.

    ``GET`` only shows a confirm button — link previewers and prefetchers
    fetch links too, and must not spend the token. The button's
    same-origin form ``POST``s the token, which signs the tablet in.
    """

    async def get(self) -> web.Response:
        token = self.request.query.get("t", "")
        if not token:
            return redirect("/login")
        locale = (await self.settings.get()).locale
        return await self.render("enter.html.j2", locale=locale, token=token)

    async def post(self) -> web.Response:
        form = await self.request.post()
        token = form.get("t")
        if not isinstance(token, str) or not token:
            return redirect("/login")
        key = self.client_key
        # Per-client limit only: see ``DashboardAuth.begin_attempt``.
        wait = self.auth.begin_attempt(key, count_global=False)
        if wait:
            # Don't even try the token — a locked-out client can't spend it.
            locale = (await self.settings.get()).locale
            return await self.too_many_tries(locale=locale, wait=wait)
        if not await self.settings.consume_setup_token(token):
            return redirect("/login")
        self.auth.record_success(key, count_global=False)
        response = redirect("/")
        await set_session_cookie(
            response, self.request, await self.auth.issue(True), keep=True
        )
        return response


class LogoutView(_DashboardView):
    """``POST /logout`` — forget this tablet's session."""

    async def post(self) -> web.Response:
        response = redirect("/login")
        await clear_session_cookie(response, self.request)
        return response


class HomeView(_DashboardView):
    """``GET /`` — placeholder until the real overview ships."""

    async def get(self) -> web.Response:
        locale = (await self.settings.get()).locale
        return await self.render("placeholder.html.j2", locale=locale)


class HealthView(web.View):
    async def get(self) -> web.Response:
        return web.Response(text="ok")


class StaticView(_DashboardView):
    """``/static/<name>`` from the renderer's preloaded assets.

    No filesystem access per request (and so no traversal); revalidated
    by ``ETag`` so an upgrade's new stylesheet is picked up at once.
    """

    async def get(self) -> web.Response:
        asset = self.renderer.static_asset(self.request.match_info["name"])
        if asset is None:
            raise web.HTTPNotFound()
        headers = {"ETag": asset.etag, "Cache-Control": "no-cache"}
        if self.request.headers.get("If-None-Match") == asset.etag:
            return web.Response(status=304, headers=headers)
        return web.Response(
            body=asset.body, content_type=asset.content_type, headers=headers
        )

    async def head(self) -> web.Response:
        # aiohttp sends the GET response's headers without its body.
        return await self.get()


def register_routes(app: web.Application) -> None:
    app.router.add_view("/", HomeView)
    app.router.add_view("/login", LoginView)
    app.router.add_view("/enter", EnterView)
    app.router.add_view("/logout", LogoutView)
    app.router.add_view("/healthz", HealthView)
    app.router.add_view("/static/{name}", StaticView)
