"""Household dashboard pages — login, setup link, logout, lock, and the
boards: Home (its Overview · Messages · Notes · Map tabs, :mod:`.tabs`),
a person, their ``/fragment/…`` refreshes, avatars and the Map tab's
tiles — plus ``POST /ack``, the "Got it" on a household-chat mention
(the dashboard's one data write).

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
from typing import cast
from urllib.parse import quote, urlsplit

from aiohttp import web

from ..domain.household_dashboard import (
    HERE_SHOWN,
    DashboardPerson,
    HomeSnapshot,
    PersonSnapshot,
)
from ..rate_limiter import client_bucket
from ..services.dashboard_settings_service import DashboardSettingsService
from ..services.map_tile_service import (
    MAP_ATTRIBUTION_TEXT,
    TileCoordinateError,
    TileUnavailableError,
)
from .auth import (
    COOKIE_NAME,
    KEEP_TTL_S,
    REVOKED_LOCKED,
    REVOKED_SIGNED_OUT,
    StaleSessionError,
    THROTTLE_MAX_FAILURES,
    DashboardAuth,
)
from .clock import household_clock, resolve_zone
from .boards import board_base, home_board, model_fields, person_board
from .messages import MentionCard
from .keys import (
    AUTH_KEY,
    CLAIMS_KEY,
    HOUSEHOLD_NAME_KEY,
    HOUSEHOLD_THEME_KEY,
    HOUSEHOLD_TZ_KEY,
    RENDERER_KEY,
    SETTINGS_KEY,
    SNAPSHOTS_KEY,
    TILES_KEY,
)
from .policy import build_dashboard_csp
from .presenter import (
    ColorBook,
    Fmt,
    home_colors,
    person_colors,
    theme_css,
    view_key,
)
from .render import DashboardRenderer
from .tabs import (
    TAB_MAP,
    TAB_MESSAGES,
    TAB_NOTES,
    TAB_OVERVIEW,
    home_tab,
    home_tabs,
)

log = logging.getLogger(__name__)

__all__ = [
    "ACK_ID_MAX",
    "ack_id",
    "AVATAR_CACHE",
    "HERE_SHOWN",
    "HOME_FRAGMENT",
    "LOCKED_FRAGMENT",
    "LOCKED_HEADER",
    "PIN_WAIT",
    "PIN_WRONG",
    "SIGNED_OUT_PATH",
    "AckView",
    "AvatarView",
    "EnterView",
    "HealthView",
    "HomeFragmentView",
    "HomeMapFragmentView",
    "HomeMapView",
    "HomeTabFragmentView",
    "HomeTabView",
    "HomeView",
    "LockView",
    "LoginView",
    "LogoutView",
    "PersonFragmentView",
    "PersonView",
    "StaticView",
    "TILE_CACHE",
    "TileView",
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


#: Avatars may be stored but must be revalidated on every use, so each
#: load passes the session guard again (423 locked, 404 once hidden).
AVATAR_CACHE = "private, no-cache"

#: A map tile may be stored by the tablet only (it says which area the
#: wall shows) and is revalidated on every use, so the lock (423) wins
#: at once; the ``ETag`` makes that a 304 from the shared tile cache.
TILE_CACHE = "private, no-cache"

#: Longest ``message_id`` / ``person_id`` ``POST /ack`` accepts.
ACK_ID_MAX = 128

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
    own_color: str | None = None,
    picture: bool = False,
    tab: str | None = None,
) -> _Board:
    """The context every dashboard board (Home, a person) shares: rail,
    clock, night mode, the inline colour style and its view key
    (:func:`~.boards.board_base`). ``picture`` (a picture-view person) is
    part of the key too: it changes the rail and the night view, which a
    swap can't — and so is Home's ``tab``, so a swap never crosses tabs."""
    app = request.app
    settings = await app[SETTINGS_KEY].get()
    t = app[RENDERER_KEY].translator(settings.locale)
    style = colors.css() + theme_css(await _theme_primary(app))
    base = board_base(snapshot, people=people, colors=colors, t=t, own_color=own_color)
    night = base.night
    # Everything outside <main> a fragment swap can't change: the rail's
    # faces, <html lang>, the lock button, the data-* the script reads.
    rail = [(p.user_id, p.display_name, p.initials, p.picture_hash) for p in people]
    zone = snapshot.now.tzinfo
    assert zone is not None
    context: dict[str, object] = {
        "household": await app[HOUSEHOLD_NAME_KEY](),
        "f": base.fmt,
        "people": people,
        "current": current,
        "show_rail": base.show_rail,
        "rail_many": base.rail_many,
        "pin_set": settings.pin_set,
        "clock": household_clock(snapshot.now.timestamp(), zone, t),
        "night": night,
        "night_item": base.night_item,
        "style": style,
        "view_key": view_key(
            style,
            night,
            rail,
            current,
            settings.locale,
            settings.pin_set,
            settings.idle_return_minutes,
            settings.keep_screen_on,
            picture,
            tab,
        ),
        "fragment_url": fragment_url,
        # A person page and Home's Messages / Notes tabs go back to
        # Overview when nobody touches the tablet.
        "idle_minutes": (
            settings.idle_return_minutes
            if current or tab not in (None, TAB_OVERVIEW)
            else 0
        ),
        "keep_awake": settings.keep_screen_on,
        "view_template": template,
        # The picture view's model (``picture.py``), set by the person
        # board for a picture-view person.
        "picture": None,
        # Household-chat rows (``messages.py``): Home and the picture view,
        # never at night. New ones just swap in with the fragment — every
        # sender is a shown person, whose colour is in the style already.
        "messages": [],
        # Mentions: the rail's badges (every person mention waiting, only
        # where there is a rail), the marker the script reads after a swap
        # (badges + chime), Home's ``@here`` banners and the person page's
        # own. Home and the person board add what they render themselves —
        # the script chimes only for what this page shows.
        "badges": base.badges,
        "mention_keys": " ".join(c.key for c in base.badged),
        "mention_sound": settings.mention_sound,
        "here_cards": [],
        "my_mentions": [],
        # Home's tab bar (empty with only Overview, and on person pages).
        "tab": tab,
        "tabs": (),
        "tab_new": 0,
    }
    return _Board(locale=settings.locale, context=context, style=style)


_HOME_TEMPLATES = {
    TAB_OVERVIEW: "_home.html.j2",
    TAB_MESSAGES: "_home_messages.html.j2",
    TAB_NOTES: "_home_notes.html.j2",
    TAB_MAP: "_home_map.html.j2",
}


async def _home_board(request: web.Request, tab: str = TAB_OVERVIEW) -> _Board | None:
    """Home on ``tab`` — ``None`` when that tab doesn't show (:mod:`.tabs`);
    the content is :func:`~.boards.home_board`'s."""
    snap = await request.app[SNAPSHOTS_KEY].home()
    if tab not in {t.key for t in home_tabs(snap)}:
        return None
    board = await _board(
        request,
        snapshot=snap,
        people=snap.people,
        colors=home_colors(snap),
        current=None,
        fragment_url=home_tab(tab).fragment,
        template=_HOME_TEMPLATES[tab],
        tab=tab,
    )
    model = home_board(snap, cast(Fmt, board.context["f"]), tab)
    assert model is not None  # the tab shows (checked above, same snapshot)
    board.context.update(
        snap=snap,
        tabs=model.tabs,
        tab_new=model.tab_new,
        here_cards=model.here_cards,
    )
    _add_keys(board, model.here_cards)
    if model.chat_days is not None:
        board.context["chat_days"] = model.chat_days
    elif model.notes is not None:
        board.context.update(notes=model.notes, notes_beyond=model.notes_beyond)
    elif tab == TAB_MAP:
        board.context.update(map=model.map, attribution=MAP_ATTRIBUTION_TEXT)
    else:
        assert model.overview is not None
        board.context.update(model_fields(model.overview))
    return board


async def _person_board(request: web.Request) -> _Board | None:
    user_id = request.match_info["user_id"]
    page = await request.app[SNAPSHOTS_KEY].person(user_id)
    if page is None:
        return None
    board = await _board(
        request,
        snapshot=page,
        people=page.people or (page.person,),
        colors=person_colors(page),
        current=page.person.user_id,
        own_color=page.person.color,
        fragment_url=f"/fragment/p/{quote(page.person.user_id, safe='')}",
        template="_person.html.j2",
        picture=page.person.picture_view,
    )
    model = person_board(page, cast(Fmt, board.context["f"]))
    board.context.update(model_fields(model))
    if not board.context["show_rail"]:
        # No rail, so no badge listed them yet.
        _add_keys(board, model.my_mentions)
    return board


def _add_keys(board: _Board, cards: list[MentionCard]) -> None:
    """List ``cards`` (rendered on this page) first among the mention keys
    the script chimes for."""
    keys = [c.key for c in cards]
    rest = cast(str, board.context["mention_keys"])
    board.context["mention_keys"] = " ".join([*keys, *([rest] if rest else [])])


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


class _PageView(_BoardView):
    """A board page: ``HEAD`` is the ``GET`` (same status, headers and
    cookie renewal) — aiohttp sends it without the body."""

    async def head(self) -> web.Response:
        return await self.get()  # type: ignore[attr-defined]


class HomeView(_PageView):
    """``GET /`` — the household overview (or night mode)."""

    async def get(self) -> web.Response:
        board = await _home_board(self.request)
        assert board is not None  # Overview always shows
        return await self.page(board)


class HomeFragmentView(_BoardView):
    """``GET /fragment/home`` — Home's ``<main>`` content, for live refresh."""

    async def get(self) -> web.Response:
        board = await _home_board(self.request)
        assert board is not None  # Overview always shows
        return await self.fragment(board)


class HomeTabView(_PageView):
    """``GET /messages`` and ``GET /notes`` — Home's other tabs.

    ``404`` (the plain not-found page) while the tab doesn't show; at night
    ``303`` to ``/``, so every wake lands on Overview."""

    async def get(self) -> web.Response:
        board = await _home_board(self.request, self.request.match_info["tab"])
        if board is None:
            locale = (await self.settings.get()).locale
            return await self.render("not_found.html.j2", locale=locale, status=404)
        if board.context["night"]:
            return redirect("/")
        return await self.page(board)


class HomeTabFragmentView(_BoardView):
    """``GET /fragment/home/messages`` and ``/fragment/home/notes`` — a
    tab's ``<main>`` content; a plain ``404`` once the tab is gone (the
    script goes to Overview). At night its view key changes, so the page
    reloads — into the ``303``."""

    async def get(self) -> web.Response:
        board = await _home_board(self.request, self.request.match_info["tab"])
        if board is None:
            return web.Response(status=404, text="Not Found")
        return await self.fragment(board)


class HomeMapView(_PageView):
    """``GET /map`` — Home's Map tab: who is where (:mod:`.map_view`).

    ``303`` to ``/`` while nobody is located — sharing flips at runtime,
    so a wall must never land on a not-found page — and at night."""

    async def get(self) -> web.Response:
        board = await _home_board(self.request, TAB_MAP)
        if board is None or board.context["night"]:
            return redirect("/")
        return await self.page(board)


class HomeMapFragmentView(_BoardView):
    """``GET /fragment/home/map`` — the Map tab's ``<main>`` content; a
    ``303`` to ``/`` like the page (the script follows it to Overview)."""

    async def get(self) -> web.Response:
        board = await _home_board(self.request, TAB_MAP)
        if board is None or board.context["night"]:
            return redirect("/")
        return await self.fragment(board)


class TileView(_DashboardView):
    """``GET /tiles/{z}/{x}/{y}`` — one map tile for the Map tab, from the
    main app's shared tile proxy (same upstream, same cache), so the
    tablet's browser never fetches from a third party (CSP
    ``img-src 'self'``).

    The session guard has already required an unlocked session (``401``
    / ``423``). Each session has a tile budget
    (:meth:`DashboardAuth.begin_tile`, ``429``). ``404`` for coordinates
    off the grid (or no tile source), ``503`` when the upstream fails and
    nothing is cached — the tablet keeps the empty square."""

    async def get(self) -> web.Response:
        tiles = self.request.app.get(TILES_KEY)
        if tiles is None:
            raise web.HTTPNotFound()
        wait = self.auth.begin_tile(self.request[CLAIMS_KEY])
        if wait:
            return web.Response(
                status=429,
                text="Too Many Requests",
                headers={"Retry-After": str(wait)},
            )
        info = self.request.match_info
        try:
            tile = await tiles.fetch(int(info["z"]), int(info["x"]), int(info["y"]))
        except TileCoordinateError:
            raise web.HTTPNotFound() from None
        except TileUnavailableError:
            return web.Response(status=503, text="Service Unavailable")
        etag = '"' + hashlib.sha256(tile.body).hexdigest()[:32] + '"'
        headers = {"Cache-Control": TILE_CACHE, "ETag": etag}
        if self.request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        return web.Response(
            body=tile.body, content_type=tile.content_type, headers=headers
        )


class PersonView(_PageView):
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
    without a picture (the page shows initials). ``private, no-cache``:
    every load revalidates by ``ETag`` (the picture hash) through the
    session guard, so a lock or a hidden person takes effect at once.
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
            "Cache-Control": AVATAR_CACHE,
        }
        if self.request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        return web.Response(body=body, content_type=image_type(body), headers=headers)


async def _signed_out_response(request: web.Request) -> web.Response:
    """Off to the login form with the "signed out" note, cookie cleared."""
    response = redirect(SIGNED_OUT_PATH)
    await clear_session_cookie(response, request)
    return response


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
        reason = self.auth.revocation(claims)
        if reason == REVOKED_SIGNED_OUT:
            # Signed out while this request was in flight (the cookie key
            # was replaced): no fresh cookie under the new key.
            return await _signed_out_response(self.request)
        if reason is not None:
            # A concurrent ``/lock`` of the same session got here first:
            # one lock (one new session) per unlocked session, so a copied
            # cookie can't fan out into many PIN budgets / revocations.
            return response
        # Revoked before the new cookie — ``revoke`` records the entry
        # before its first await while the list has room, so a concurrent
        # ``/lock`` of this session sees it above (or here: ``False``).
        if not await self.auth.revoke(claims, REVOKED_LOCKED):
            if self.auth.revocation(claims) == REVOKED_SIGNED_OUT:
                return await _signed_out_response(self.request)
            return response
        try:
            locked = await self.auth.issue(claims.keep, locked=True, continuing=claims)
        except StaleSessionError:
            return await _signed_out_response(self.request)
        await set_session_cookie(response, self.request, locked, keep=claims.keep)
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
            await self.auth.revoke(claims, REVOKED_SIGNED_OUT)
            return await _signed_out_response(self.request)
        if wait:
            response = redirect(f"/?pin={PIN_WAIT}")
            response.headers["Retry-After"] = str(wait)
            return response
        pin = form.get("pin")
        if not isinstance(pin, str) or not await self.auth.check_pin(pin):
            log.info("household dashboard: wrong PIN from %s", self.client_key)
            return redirect(f"/?pin={PIN_WRONG}")
        self.auth.record_success(key, count_global=False)
        if self.auth.revocation(claims) == REVOKED_SIGNED_OUT:
            # Signed out while the PIN was being checked (the cookie key
            # was replaced): a right PIN doesn't bring the session back.
            return await _signed_out_response(self.request)
        try:
            unlocked = await self.auth.issue(
                claims.keep, locked=False, continuing=claims
            )
        except StaleSessionError:
            return await _signed_out_response(self.request)
        response = redirect("/")
        await set_session_cookie(response, self.request, unlocked, keep=claims.keep)
        return response


def ack_id(value: object) -> str | None:
    """A "Got it" id (tablet form field or panel JSON) if it is a
    plausible id, else ``None``."""
    if isinstance(value, str) and 0 < len(value) <= ACK_ID_MAX:
        return value
    return None


class AckView(_GoHomeOnGet):
    """``POST /ack`` (``message_id``, optional ``person_id``) — "Got it"
    on a household-chat mention: an ``@here`` banner on Home (no
    ``person_id``) or a person's own mention on their page.

    The dashboard's one data write, so nothing the tablet sends is
    trusted: :meth:`HouseholdDashboardService.acknowledge` re-derives
    that the wall shows exactly that mention right now (opt-in, chat on,
    not night, shown unblocked sender, within the hour, really ``@here`` /
    really naming that shown person) — anything else is the plain
    not-found page (``404``), never saying why. The session guard has
    already required a session (``401``), a same-origin ``POST``
    (``403``) and an unlocked tablet (``423``). Writes are capped per
    session (:meth:`DashboardAuth.begin_ack`, ``429``). Post/Redirect/Get
    back to Home or to that person's page — never anywhere the form
    names.
    """

    async def post(self) -> web.Response:
        wait = self.auth.begin_ack(self.request[CLAIMS_KEY])
        if wait:
            return web.Response(
                status=429,
                text="Too Many Requests",
                headers={"Retry-After": str(wait)},
            )
        form = await self.request.post()
        message_id = ack_id(form.get("message_id"))
        raw_person = form.get("person_id") or None
        person_id = ack_id(raw_person)
        snapshots = self.request.app[SNAPSHOTS_KEY]
        if (
            message_id is None
            or (raw_person is not None and person_id is None)
            or not await snapshots.acknowledge(message_id, person_id=person_id)
        ):
            locale = (await self.settings.get()).locale
            return await self.render("not_found.html.j2", locale=locale, status=404)
        if person_id is None:
            return redirect("/")
        return redirect(f"/p/{quote(person_id, safe='')}")


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
    app.router.add_view(f"/{{tab:{TAB_MESSAGES}|{TAB_NOTES}}}", HomeTabView)
    app.router.add_view(f"/{TAB_MAP}", HomeMapView)
    app.router.add_view("/p/{user_id}", PersonView)
    app.router.add_view("/fragment/home", HomeFragmentView)
    app.router.add_view(
        f"/fragment/home/{{tab:{TAB_MESSAGES}|{TAB_NOTES}}}", HomeTabFragmentView
    )
    app.router.add_view(f"/fragment/home/{TAB_MAP}", HomeMapFragmentView)
    app.router.add_view("/fragment/p/{user_id}", PersonFragmentView)
    app.router.add_view(r"/tiles/{z:\d{1,2}}/{x:\d{1,7}}/{y:\d{1,7}}", TileView)
    app.router.add_view("/avatar/{user_id}", AvatarView)
    app.router.add_view("/login", LoginView)
    app.router.add_view("/enter", EnterView)
    app.router.add_view("/logout", LogoutView)
    app.router.add_view("/lock", LockView)
    app.router.add_view("/unlock", UnlockView)
    app.router.add_view("/ack", AckView)
    app.router.add_view("/healthz", HealthView)
    app.router.add_view("/static/{name}", StaticView)
