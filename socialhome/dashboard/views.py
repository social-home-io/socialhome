"""Household dashboard pages — login, setup link, logout, lock, and the
boards: Home, a person, their ``/fragment/…`` refreshes and avatars.

Every view is server-rendered HTML; the session guard in
:mod:`socialhome.dashboard.app` has already decided whether the request
may reach a non-public view (and answers every content route of a
privacy-locked tablet with :func:`render_lock_screen` itself).

A board page carries one inline ``<style>`` — the validated colours of
:mod:`.presenter` — admitted by hash in that response's CSP. A fragment
can't add style rules to the page it lands in, so it carries the page's
*view key* (style, night mode, rail); ``dashboard.js`` reloads the whole
page when a fragment's key differs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from aiohttp import web

from ..domain.household_dashboard import (
    SECTION_TIMETABLE,
    DashboardAgendaItem,
    DashboardEvent,
    DashboardPerson,
    HomeSnapshot,
    PersonSnapshot,
)
from ..rate_limiter import client_bucket
from ..services.dashboard_settings_service import DashboardSettingsService
from .auth import (
    COOKIE_NAME,
    KEEP_TTL_S,
    REVOKED_LOCKED,
    REVOKED_SIGNED_OUT,
    THROTTLE_MAX_FAILURES,
    DashboardAuth,
)
from .clock import household_clock, resolve_zone
from .keys import (
    AUTH_KEY,
    CLAIMS_KEY,
    HOUSEHOLD_NAME_KEY,
    HOUSEHOLD_THEME_KEY,
    HOUSEHOLD_TZ_KEY,
    RENDERER_KEY,
    SETTINGS_KEY,
    SNAPSHOTS_KEY,
)
from .policy import build_dashboard_csp
from .presenter import (
    MAX_LANES,
    SHOPPING_SHOWN,
    STICKIES_SHOWN,
    ColorBook,
    Fmt,
    build_timeline,
    home_colors,
    night_item,
    person_colors,
    presence_groups,
    school_changes,
    school_now,
    theme_css,
    view_key,
)
from .render import DashboardRenderer

log = logging.getLogger(__name__)

__all__ = [
    "AVATAR_MAX_AGE_S",
    "HOME_FRAGMENT",
    "LOCKED_FRAGMENT",
    "LOCKED_HEADER",
    "PIN_WAIT",
    "PIN_WRONG",
    "SIGNED_OUT_PATH",
    "AvatarView",
    "EnterView",
    "HealthView",
    "HomeFragmentView",
    "HomeView",
    "LockView",
    "LoginView",
    "LogoutView",
    "PersonFragmentView",
    "PersonView",
    "StaticView",
    "UnlockView",
    "clear_session_cookie",
    "cookie_secure",
    "html_response",
    "image_type",
    "redirect",
    "register_routes",
    "render_lock_screen",
    "safe_next",
    "set_session_cookie",
]

#: What a locked tablet gets for ``/fragment/…`` (fetched by page JS): a
#: ``200`` with this header and body, so the script can reload into the
#: lock screen instead of painting a fragment.
LOCKED_HEADER = "X-Dashboard-Locked"
LOCKED_FRAGMENT = '<div data-dashboard-locked="1"></div>'

#: Where a tablet lands after too many wrong PINs.
SIGNED_OUT_PATH = "/login?signed_out=1"

#: ``/?pin=…`` flags the lock screen reads after ``POST /unlock``.
PIN_WRONG = "wrong"
PIN_WAIT = "wait"

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


async def render_lock_screen(request: web.Request) -> web.Response:
    """The privacy lock screen: household name, clock, date, PIN form.

    Takes nothing about any person — so nothing about one can leak. The
    outcome of the last ``POST /unlock`` arrives as a flag (Post/Redirect/
    Get, so a reload never re-sends a PIN): ``?pin=wrong`` shows "Wrong
    PIN"; ``?pin=wait`` shows "Too many tries" with ``429`` and
    ``Retry-After`` while this tablet must still wait.
    """
    app = request.app
    auth = app[AUTH_KEY]
    renderer = app[RENDERER_KEY]
    locale = (await app[SETTINGS_KEY].get()).locale
    zone = await asyncio.to_thread(resolve_zone, await app[HOUSEHOLD_TZ_KEY]())
    t = renderer.translator(locale)
    status, headers, error = 200, None, None
    flag = request.query.get("pin")
    if flag == PIN_WRONG:
        error = t("dashboard.lock.wrong_pin")
    elif flag == PIN_WAIT:
        key = DashboardAuth.pin_throttle_key(request[CLAIMS_KEY])
        wait = auth.attempt_wait(key, count_global=False)
        if wait:
            status, headers = 429, {"Retry-After": str(wait)}
            error = t("dashboard.login.too_many_tries", minutes=math.ceil(wait / 60))
    text = renderer.render(
        "lock.html.j2",
        locale=locale,
        household=await app[HOUSEHOLD_NAME_KEY](),
        clock=household_clock(auth.now(), zone, t),
        error=error,
    )
    return html_response(text, status=status, headers=headers)


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
        notice_key: str | None = None,
        show_form: bool = True,
        **fmt: object,
    ) -> web.Response:
        error = (
            None
            if error_key is None
            else self.renderer.gettext(error_key, locale, **fmt)
        )
        notice = (
            None if notice_key is None else self.renderer.gettext(notice_key, locale)
        )
        return await self.render(
            "login.html.j2",
            locale=locale,
            status=status,
            next_path=next_path,
            keep=keep,
            error=error,
            notice=notice,
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
        signed_out = self.request.query.get("signed_out") == "1"
        return await self.login_form(
            locale=settings.locale,
            status=200,
            next_path=next_path,
            notice_key="dashboard.lock.signed_out" if signed_out else None,
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


#: A shown person's picture may sit in the tablet's cache this long — short,
#: so hiding someone (or changing their picture) shows within minutes.
AVATAR_MAX_AGE_S = 300

#: Events listed under "Tomorrow" on a person page.
TOMORROW_SHOWN = 3

#: ``/fragment/…`` paths for live refresh (``dashboard.js``).
HOME_FRAGMENT = "/fragment/home"

_SAFE_ETAG = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def image_type(body: bytes) -> str:
    """The image MIME type of stored picture bytes (profile pictures are
    stored as WebP; older rows may be PNG / JPEG / GIF)."""
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


async def _theme_primary(app: web.Application) -> str | None:
    """The household theme colour, or ``None`` when it can't be read —
    the dashboard then keeps its built-in accent."""
    try:
        return (await app[HOUSEHOLD_THEME_KEY]()).primary_color
    except Exception:
        log.warning("household dashboard: theme read failed", exc_info=True)
        return None


@dataclass(slots=True, frozen=True)
class _Board:
    """A rendered-to-be board: its locale, template context and the
    inline style text the page's CSP must admit."""

    locale: str
    context: dict[str, object]
    style: str


async def _board(
    request: web.Request,
    *,
    snapshot: HomeSnapshot | PersonSnapshot,
    people: tuple[DashboardPerson, ...],
    colors: ColorBook,
    current: str | None,
    fragment_url: str,
    template: str,
    extra: dict[str, object],
) -> _Board:
    """The context every dashboard board (Home, a person) shares: rail,
    clock, night mode, the inline colour style and its view key."""
    app = request.app
    settings = await app[SETTINGS_KEY].get()
    t = app[RENDERER_KEY].translator(settings.locale)
    style = colors.css() + theme_css(await _theme_primary(app))
    night = snapshot.is_night
    rail_ids = ",".join(p.user_id for p in people)
    fmt = Fmt(now=snapshot.now, today=snapshot.today, t=t, people=people, colors=colors)
    zone = snapshot.now.tzinfo
    assert zone is not None
    context: dict[str, object] = {
        "household": await app[HOUSEHOLD_NAME_KEY](),
        "f": fmt,
        "people": people,
        "current": current,
        "show_rail": len(people) > 1,
        "rail_many": len(people) > MAX_LANES,
        "pin_set": settings.pin_set,
        "clock": household_clock(snapshot.now.timestamp(), zone, t),
        "night": night,
        "night_item": night_item(snapshot) if night else None,
        "style": style,
        "view_key": view_key(style, night, rail_ids, current),
        "fragment_url": fragment_url,
        "idle_minutes": settings.idle_return_minutes if current else 0,
        "keep_awake": settings.keep_screen_on,
        "view_template": template,
        **extra,
    }
    return _Board(locale=settings.locale, context=context, style=style)


async def _home_board(request: web.Request) -> _Board:
    snap = await request.app[SNAPSHOTS_KEY].home()
    colors = home_colors(snap)
    board = await _board(
        request,
        snapshot=snap,
        people=snap.people,
        colors=colors,
        current=None,
        fragment_url=HOME_FRAGMENT,
        template="_home.html.j2",
        extra={},
    )
    fmt = board.context["f"]
    assert isinstance(fmt, Fmt)
    board.context.update(
        snap=snap,
        timeline=build_timeline(snap, fmt),
        presence=presence_groups(snap),
        at_school=(
            school_now(snap.school, snap.now)
            if SECTION_TIMETABLE in snap.sections
            else []
        ),
        changes=(
            school_changes(snap.school) if SECTION_TIMETABLE in snap.sections else []
        ),
        shopping_shown=snap.shopping[:SHOPPING_SHOWN],
        shopping_more=max(0, snap.shopping_total - SHOPPING_SHOWN),
        stickies=snap.stickies[:STICKIES_SHOWN],
        open_chores=sum(1 for c in snap.chores_today if not c.done),
    )
    return board


async def _person_board(request: web.Request) -> _Board | None:
    user_id = request.match_info["user_id"]
    page = await request.app[SNAPSHOTS_KEY].person(user_id)
    if page is None:
        return None
    people = page.people or (page.person,)
    school_lessons = page.school.lessons if page.school is not None else ()
    events_today = (
        tuple(i for i in page.today_items if isinstance(i, DashboardEvent))
        if page.school is not None
        else page.today_items
    )
    return await _board(
        request,
        snapshot=page,
        people=people,
        colors=person_colors(page),
        current=page.person.user_id,
        fragment_url=f"/fragment/p/{quote(page.person.user_id, safe='')}",
        template="_person.html.j2",
        extra={
            "page": page,
            "school_lessons": school_lessons,
            "today_list": events_today,
            "tomorrow_list": _tomorrow(page),
        },
    )


def _tomorrow(page: PersonSnapshot) -> list[DashboardAgendaItem]:
    """Tomorrow on a person page: the first few events, plus the first
    lesson (school is said once, not lesson by lesson), in time order."""
    events = [i for i in page.tomorrow_items if isinstance(i, DashboardEvent)]
    items: list[DashboardAgendaItem] = list(events[:TOMORROW_SHOWN])
    if page.school_tomorrow_first is not None:
        items.append(page.school_tomorrow_first)
    items.sort(
        key=lambda i: (
            0 if isinstance(i, DashboardEvent) and i.all_day else 1,
            i.start,
        )
    )
    return items


class _BoardView(_DashboardView):
    async def page(self, board: _Board) -> web.Response:
        text = self.renderer.render(
            "board.html.j2", locale=board.locale, **board.context
        )
        return html_response(
            text,
            headers={"Content-Security-Policy": build_dashboard_csp([board.style])},
        )

    async def fragment(self, board: _Board) -> web.Response:
        text = self.renderer.render(
            "fragment.html.j2", locale=board.locale, **board.context
        )
        return html_response(text)


class HomeView(_BoardView):
    """``GET /`` — the household overview (or night mode)."""

    async def get(self) -> web.Response:
        return await self.page(await _home_board(self.request))


class HomeFragmentView(_BoardView):
    """``GET /fragment/home`` — Home's ``<main>`` content, for live refresh."""

    async def get(self) -> web.Response:
        return await self.fragment(await _home_board(self.request))


class PersonView(_BoardView):
    """``GET /p/{user_id}`` — one shown person's page; ``404`` for anyone
    unknown, inactive or hidden (the same page either way)."""

    async def get(self) -> web.Response:
        board = await _person_board(self.request)
        if board is None:
            locale = (await self.settings.get()).locale
            return await self.render("not_found.html.j2", locale=locale, status=404)
        return await self.page(board)


class PersonFragmentView(_BoardView):
    """``GET /fragment/p/{user_id}`` — the person page's ``<main>``
    content; a plain ``404`` once they are hidden (the script goes Home)."""

    async def get(self) -> web.Response:
        board = await _person_board(self.request)
        if board is None:
            return web.Response(status=404, text="Not Found")
        return await self.fragment(board)


class AvatarView(_DashboardView):
    """``GET /avatar/{user_id}`` — a shown person's profile picture.

    ``404`` for anyone hidden, inactive or unknown, and for a person
    without a picture (the page shows initials). Cached privately for a
    few minutes and revalidated by ``ETag`` (the picture hash).
    """

    async def get(self) -> web.Response:
        found = await self.request.app[SNAPSHOTS_KEY].avatar(
            self.request.match_info["user_id"]
        )
        if found is None:
            raise web.HTTPNotFound()
        body, picture_hash = found
        tag = (
            picture_hash
            if _SAFE_ETAG.fullmatch(picture_hash)
            else hashlib.sha256(body).hexdigest()[:32]
        )
        etag = f'"{tag}"'
        headers = {
            "ETag": etag,
            "Cache-Control": f"private, max-age={AVATAR_MAX_AGE_S}",
        }
        if self.request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        return web.Response(body=body, content_type=image_type(body), headers=headers)


class _GoHomeOnGet(_DashboardView):
    """``GET`` (a reload, a typed URL) of a form target just goes home —
    nothing is ever re-POSTed."""

    async def get(self) -> web.Response:
        return redirect("/")


class LockView(_GoHomeOnGet):
    """``POST /lock`` — privacy-lock this tablet (needs a session).

    The lock is a claim in this tablet's signed cookie, so it is per
    tablet and survives reloads and browser restarts. The locked cookie
    starts a new session (its id keys the PIN throttle), and the incoming
    unlocked session is revoked as ``locked``: a slow response still
    carrying it can't lift the lock. An already-locked session changes
    nothing — no new cookie, so no fresh PIN budget, and no revocation
    entry. ``409`` when no PIN is set: nothing could unlock it (the
    button isn't shown then).
    """

    async def post(self) -> web.Response:
        if not (await self.settings.get()).pin_set:
            return web.Response(status=409, text="No dashboard PIN is set")
        claims = self.request[CLAIMS_KEY]
        response = redirect("/")
        if claims.locked:
            return response
        await set_session_cookie(
            response,
            self.request,
            await self.auth.issue(claims.keep, locked=True),
            keep=claims.keep,
        )
        self.auth.revoke(claims, REVOKED_LOCKED)
        return response


class UnlockView(_GoHomeOnGet):
    """``POST /unlock`` (form field ``pin``) — lift this tablet's lock.

    Only a valid *and* locked session is checked against the PIN; an
    unlocked one just goes home. Guesses are throttled per tablet
    (:meth:`DashboardAuth.pin_throttle_key`). When this tablet's own five
    wrong PINs in 15 minutes are spent, the next try signs it out and
    revokes the cookie — then only the household password helps. Any
    other hold-back (a full throttle table) only asks it to wait.
    Every outcome is a redirect (Post/Redirect/Get).
    """

    async def post(self) -> web.Response:
        claims = self.request[CLAIMS_KEY]
        if not claims.locked:
            return redirect("/")
        form = await self.request.post()
        key = DashboardAuth.pin_throttle_key(claims)
        # Counted before the slow scrypt check — see ``begin_attempt``.
        wait = self.auth.begin_attempt(key, count_global=False)
        if wait and self.auth.failure_count(key) >= THROTTLE_MAX_FAILURES:
            log.warning(
                "household dashboard: too many wrong PINs from %s — tablet signed out",
                self.client_key,
            )
            self.auth.revoke(claims, REVOKED_SIGNED_OUT)
            response = redirect(SIGNED_OUT_PATH)
            await clear_session_cookie(response, self.request)
            return response
        if wait:
            response = redirect(f"/?pin={PIN_WAIT}")
            response.headers["Retry-After"] = str(wait)
            return response
        pin = form.get("pin")
        if not isinstance(pin, str) or not await self.auth.check_pin(pin):
            log.info("household dashboard: wrong PIN from %s", self.client_key)
            return redirect(f"/?pin={PIN_WRONG}")
        self.auth.record_success(key, count_global=False)
        response = redirect("/")
        await set_session_cookie(
            response,
            self.request,
            await self.auth.issue(claims.keep, locked=False),
            keep=claims.keep,
        )
        return response


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
    app.router.add_view("/p/{user_id}", PersonView)
    app.router.add_view("/fragment/home", HomeFragmentView)
    app.router.add_view("/fragment/p/{user_id}", PersonFragmentView)
    app.router.add_view("/avatar/{user_id}", AvatarView)
    app.router.add_view("/login", LoginView)
    app.router.add_view("/enter", EnterView)
    app.router.add_view("/logout", LogoutView)
    app.router.add_view("/lock", LockView)
    app.router.add_view("/unlock", UnlockView)
    app.router.add_view("/healthz", HealthView)
    app.router.add_view("/static/{name}", StaticView)
