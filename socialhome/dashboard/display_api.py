"""The wall-panel JSON API on the dashboard port: ``/api/display/v1/…``.

A wall panel (e.g. an ESPHome display) is not a browser: it pairs with a
short code and then authenticates every request with
``Authorization: Bearer <token>``
(:class:`~socialhome.services.dashboard_display_service.DashboardDisplayService`).

* ``POST /api/display/v1/pair`` (public) — start pairing: ``201
  {pair_id, code, expires_in}``; throttled per client address
  (:meth:`DashboardAuth.begin_pair`);
* ``GET /api/display/v1/pair/{pair_id}`` (public) — ``{"status":
  "pending"}``, or once ``{"status": "paired", "token", "display_id"}``;
  ``404`` when unknown or expired;
* ``GET /api/display/v1/whoami`` (bearer) — ``{display_id, name}``;
* ``GET /api/display/v1/home`` · ``/messages`` · ``/notes`` · ``/map`` ·
  ``/person/{user_id}`` · ``/theme`` (bearer) — the boards as JSON
  (:mod:`.display_json`, built from the same view models as the HTML
  pages, :mod:`.boards`), each with an ``ETag`` (``If-None-Match`` →
  ``304``). A tab that doesn't show is ``404 TAB_OFF``; at night the
  Messages / Notes / Map tabs are ``409 NIGHT`` (the wall goes back to
  Overview, like the tablet's ``303``);
* ``GET /api/display/v1/avatar/{user_id}?size=N`` and
  ``/emoji/{code}?size=N`` (bearer) — PNGs (:mod:`.avatars`,
  :mod:`.emoji`);
* ``GET /api/display/v1/map.png?w=&h=&scheme=`` (bearer) — the Map tab as
  one picture (:mod:`.map_png`), under the same rules as ``/map``; a poll
  of an unchanged map is a ``304`` that paints nothing (``/map``'s
  ``image.etag`` is the default picture's ``ETag``).

* ``POST /api/display/v1/lock`` · ``/unlock`` (``{pin}``) · ``/ack``
  (``{message_id, person_id?}``) (bearer) — the privacy lock (stored on
  the panel's record, :meth:`DashboardDisplayService.lock` / ``unlock``:
  the tablet's PIN rules, the 6th wrong try unpairs the panel) and "Got
  it" (the tablet's :meth:`acknowledge` rules and budget, per panel).

A panel that is locked (its record's ``locked``, while a PIN is set) gets
``423`` with the lock payload (household name, clock, PIN strings — never
a person) for everything but pairing and ``/unlock``. Requests are
budgeted per panel (:meth:`DashboardAuth.begin_display_read`, ``429`` +
``Retry-After``).

The two credentials never cross: under this prefix the session guard
reads only the bearer token (a tablet cookie opens nothing here), and on
every other path only the cookie (a panel token opens no page). With no
cookie authority under the prefix there is nothing for a cross-site
request to ride on, so the same-origin ``POST`` check skips it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping

import orjson
from aiohttp import web

from ..domain.errors import CodedError
from ..domain.household_dashboard import DashboardSettings
from ..domain.theme import HouseholdTheme
from ..rate_limiter import client_bucket
from ..security import error_response
from ..services.map_tile_service import MAP_ATTRIBUTION_TEXT
from .avatars import AVATAR_SIZES, DEFAULT_AVATAR_SIZE
from .boards import BoardBase, HomeBoard, board_base, home_board, person_board
from .clock import resolve_zone
from .display_json import (
    API_VERSION,
    board_colors,
    board_header,
    display_clock,
    home_payload,
    lock_payload,
    map_payload,
    messages_payload,
    notes_payload,
    person_payload,
    theme_payload,
)
from .emoji import CODE_PATTERN, EMOJI_SIZES, SOURCE_SIZE, is_code
from .keys import (
    AUTH_KEY,
    AVATARS_KEY,
    DISPLAY_KEY,
    DISPLAYS_KEY,
    EMOJI_KEY,
    HOUSEHOLD_NAME_KEY,
    HOUSEHOLD_THEME_KEY,
    HOUSEHOLD_TZ_KEY,
    MAP_PICTURES_KEY,
    RENDERER_KEY,
    SETTINGS_KEY,
    SNAPSHOTS_KEY,
)
from .map_png import DEFAULT_SIZE, SCHEMES, MapTilesUnavailable, clamp_size
from .map_scene import (
    MapPalette,
    MapScene,
    map_palettes,
    map_scene,
    scene_etag,
    scene_key,
)
from .presenter import home_colors, person_colors, view_key
from .tabs import TAB_MAP, TAB_MESSAGES, TAB_NOTES, TAB_OVERVIEW
from .views import ack_id

log = logging.getLogger(__name__)

__all__ = [
    "DISPLAY_API_PREFIX",
    "DISPLAY_CACHE",
    "IMAGE_PREFIXES",
    "LOCK_PATH",
    "MAP_PNG_PATH",
    "PAIR_PATH",
    "UNLOCK_PATH",
    "bearer_token",
    "guard_display_request",
    "is_display_api",
    "is_public_display_path",
    "register_display_routes",
]

DISPLAY_API_PREFIX = "/api/display/v1/"
PAIR_PATH = "/api/display/v1/pair"
LOCK_PATH = "/api/display/v1/lock"
UNLOCK_PATH = "/api/display/v1/unlock"
#: Panels report themselves in these headers (untrusted, length-capped).
FIRMWARE_HEADER = "X-Display-Firmware"
MODEL_HEADER = "X-Display-Model"

#: Every answer is revalidated by ``ETag`` (a 30 s poll is a ``304`` while
#: nothing changed); the panel keeps it, nobody in between does.
DISPLAY_CACHE = "private, no-cache"
MAP_PNG_PATH = DISPLAY_API_PREFIX + "map.png"
#: Image routes — their own, larger read budget.
IMAGE_PREFIXES = (
    DISPLAY_API_PREFIX + "avatar/",
    DISPLAY_API_PREFIX + "emoji/",
    MAP_PNG_PATH,
)
#: How long a panel waits before asking again for a map with no tiles.
TILES_RETRY_S = 60
_SAFE_TAG = re.compile(r"[A-Za-z0-9_.-]{1,128}")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def is_display_api(path: str) -> bool:
    return path.startswith(DISPLAY_API_PREFIX)


def is_public_display_path(path: str) -> bool:
    """The pairing endpoints — a panel has no token yet."""
    if path == PAIR_PATH:
        return True
    rest = path.removeprefix(PAIR_PATH + "/")
    # Exactly one non-empty segment — nothing nested below is public.
    return rest != path and bool(rest) and "/" not in rest


def bearer_token(headers: Mapping[str, str]) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header, else ``None``."""
    scheme, _, rest = headers.get("Authorization", "").strip().partition(" ")
    token = rest.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def _coded(exc: CodedError) -> web.Response:
    response = error_response(
        exc.status,
        exc.code,
        exc.detail,
        extra={"params": exc.params} if exc.params else None,
    )
    if exc.status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    retry_after = exc.params.get("retry_after")
    if exc.status == 429 and retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    return response


async def _json_object(request: web.Request) -> dict[str, object] | None:
    """The request body as a JSON object (``{}`` when empty), else ``None``."""
    raw = await request.read()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _bad_request() -> web.Response:
    return error_response(400, "BAD_REQUEST", "Expected a JSON object.")


async def guard_display_request(
    request: web.Request, handler: Handler
) -> web.StreamResponse:
    """The session guard's branch for ``/api/display/v1/…``: the pairing
    endpoints are public, everything else needs a paired panel's token."""
    if is_public_display_path(request.path):
        return await handler(request)
    displays = request.app[DISPLAYS_KEY]
    display = await displays.authenticate(bearer_token(request.headers))
    if display is None:
        response = error_response(401, "UNAUTHORIZED", "A display token is required.")
        response.headers["WWW-Authenticate"] = "Bearer"
        return response
    request[DISPLAY_KEY] = display
    await displays.seen(
        display,
        firmware=request.headers.get(FIRMWARE_HEADER),
        model=request.headers.get(MODEL_HEADER),
    )
    # Budgeted first, so a locked panel's 423s are capped too.
    wait = request.app[AUTH_KEY].begin_display_read(
        display.id, image=request.path.startswith(IMAGE_PREFIXES)
    )
    if wait:
        response = error_response(429, "RATE_LIMITED", "Too many requests.")
        response.headers["Retry-After"] = str(wait)
        return response
    # The PIN is the one way out of the lock, so ``/unlock`` passes.
    if request.path != UNLOCK_PATH and await displays.is_locked(display):
        return await _locked(request)
    return await handler(request)


async def _locked(request: web.Request, *, status: int = 423) -> web.Response:
    """``423`` for a privacy-locked panel: what the tablet lock screen
    shows (household name, clock, the PIN strings) — never a person.
    ``POST /lock`` answers the same body with ``200``."""
    app = request.app
    locale = (await app[SETTINGS_KEY].get()).locale
    zone = await asyncio.to_thread(resolve_zone, await app[HOUSEHOLD_TZ_KEY]())
    t = app[RENDERER_KEY].translator(locale)
    payload = lock_payload(
        household=await app[HOUSEHOLD_NAME_KEY](),
        locale=locale,
        clock=display_clock(app[AUTH_KEY].now(), zone, t),
        t=t,
    )
    return web.json_response(payload, status=status, dumps=_dumps)


def _dumps(value: object) -> str:
    return orjson.dumps(value).decode()


def _etag_matches(request: web.Request, etag: str) -> bool:
    """``If-None-Match`` names ``etag`` (a list, weak or not, or ``*``)."""
    header = request.headers.get("If-None-Match")
    if not header:
        return False
    tags = {t.strip().removeprefix("W/") for t in header.split(",")}
    return etag in tags or "*" in tags


def json_answer(request: web.Request, payload: object) -> web.Response:
    """``payload`` as JSON with an ``ETag`` of its bytes — ``304`` (no
    body) when the panel already has it."""
    body = orjson.dumps(payload)
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": DISPLAY_CACHE}
    if _etag_matches(request, etag):
        return web.Response(status=304, headers=headers)
    return web.Response(body=body, content_type="application/json", headers=headers)


class DisplayPairView(web.View):
    """``POST /api/display/v1/pair`` — a panel asks for a pairing code."""

    async def post(self) -> web.Response:
        # The socket peer only — never X-Forwarded-For, which anyone can set.
        wait = self.request.app[AUTH_KEY].begin_pair(client_bucket(self.request.remote))
        if wait:
            response = error_response(
                429, "RATE_LIMITED", "Too many pairing requests. Try again later."
            )
            response.headers["Retry-After"] = str(wait)
            return response
        body = await _json_object(self.request)
        if body is None:
            return _bad_request()
        try:
            pairing = await self.request.app[DISPLAYS_KEY].start_pairing(
                model=body.get("model"), firmware=body.get("firmware")
            )
        except CodedError as exc:
            return _coded(exc)
        return web.json_response(
            {
                "pair_id": pairing.pair_id,
                "code": pairing.code,
                "expires_in": pairing.expires_in,
            },
            status=201,
        )


class DisplayPairStatusView(web.View):
    """``GET /api/display/v1/pair/{pair_id}`` — the panel polls for its token."""

    async def get(self) -> web.Response:
        status = await self.request.app[DISPLAYS_KEY].poll(
            self.request.match_info["pair_id"]
        )
        if status is None:
            return error_response(404, "NOT_FOUND", "No such pairing.")
        if not status.paired:
            return web.json_response({"status": "pending"})
        return web.json_response(
            {
                "status": "paired",
                "token": status.token,
                "display_id": status.display_id,
            }
        )


class DisplayWhoamiView(web.View):
    """``GET /api/display/v1/whoami`` — which panel this token belongs to."""

    async def get(self) -> web.Response:
        display = self.request[DISPLAY_KEY]
        return web.json_response({"display_id": display.id, "name": display.name})


class DisplayLockView(web.View):
    """``POST /api/display/v1/lock`` — privacy-lock this panel: ``200``
    with the lock screen (:func:`~.display_json.lock_payload`), stored
    on the panel's record so a reboot keeps it. ``409
    DASHBOARD_PIN_REQUIRED`` while no PIN is set (nothing could unlock
    it); a locked panel gets the guard's ``423``."""

    async def post(self) -> web.Response:
        try:
            await self.request.app[DISPLAYS_KEY].lock(self.request[DISPLAY_KEY])
        except CodedError as exc:
            return _coded(exc)
        return await _locked(self.request, status=200)


class DisplayUnlockView(web.View):
    """``POST /api/display/v1/unlock`` ``{"pin": "1234"}`` — lift this
    panel's lock (the one route a locked panel reaches besides the lock
    screen). ``200 {"locked": false}`` (also when it wasn't locked —
    nothing is checked then); ``403 DISPLAY_PIN_WRONG`` with
    ``params.remaining``; once those are spent the next try unpairs the
    panel: ``401 DISPLAY_SIGNED_OUT``
    (:meth:`DashboardDisplayService.unlock`)."""

    async def post(self) -> web.Response:
        body = await _json_object(self.request)
        if body is None:
            return _bad_request()
        try:
            await self.request.app[DISPLAYS_KEY].unlock(
                self.request[DISPLAY_KEY], body.get("pin")
            )
        except CodedError as exc:
            return _coded(exc)
        return web.json_response({"api_version": API_VERSION, "locked": False})


class DisplayAckView(web.View):
    """``POST /api/display/v1/ack`` ``{"message_id", "person_id"?}`` —
    "Got it" on a mention the panel shows (each mention's ``ack``).

    The tablet's ``POST /ack`` rules: nothing the panel sends is trusted —
    :meth:`HouseholdDashboardService.acknowledge` re-derives that the
    wall shows exactly that mention right now (opt-in, chat on, not
    night, shown sender, really ``@here`` / really naming that shown
    person); anything else is ``404 NOT_FOUND``, never saying why. A
    locked panel gets the guard's ``423``. Capped per panel
    (:meth:`DashboardAuth.begin_display_ack`, ``429`` + ``Retry-After``).
    """

    async def post(self) -> web.Response:
        app = self.request.app
        wait = app[AUTH_KEY].begin_display_ack(self.request[DISPLAY_KEY].id)
        if wait:
            response = error_response(429, "RATE_LIMITED", "Too many requests.")
            response.headers["Retry-After"] = str(wait)
            return response
        body = await _json_object(self.request)
        if body is None:
            return _bad_request()
        message_id = ack_id(body.get("message_id"))
        raw_person = body.get("person_id")
        person_id = ack_id(raw_person)
        if (
            message_id is None
            or (raw_person is not None and person_id is None)
            or not await app[SNAPSHOTS_KEY].acknowledge(message_id, person_id=person_id)
        ):
            return error_response(404, "NOT_FOUND", "Nothing to acknowledge.")
        return web.json_response({"api_version": API_VERSION, "acknowledged": True})


async def _theme(app: web.Application) -> HouseholdTheme | None:
    """The household theme, or ``None`` when it can't be read — the panel
    then keeps the built-in accent (like the tablet)."""
    try:
        return await app[HOUSEHOLD_THEME_KEY]()
    except Exception:
        log.warning("household dashboard: theme read failed", exc_info=True)
        return None


class _DisplayBoardView(web.View):
    """Shared by the board endpoints: settings, translator, header."""

    async def _setup(self) -> tuple[DashboardSettings, Callable[..., str]]:
        settings = await self.request.app[SETTINGS_KEY].get()
        return settings, self.request.app[RENDERER_KEY].translator(settings.locale)

    async def _header(
        self,
        *,
        view: str,
        base: BoardBase,
        sections: frozenset[str],
        settings: DashboardSettings,
        current: str | None = None,
        picture: bool = False,
        tab: str | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        app = self.request.app
        now = base.fmt.now
        zone = now.tzinfo
        assert zone is not None
        clock = display_clock(now.timestamp(), zone, base.fmt.t)
        theme = await _theme(app)
        rail = [
            (p.user_id, p.display_name, p.initials, p.picture_hash, p.color)
            for p in base.people
        ]
        key = view_key(
            "display-v1",
            base.night,
            rail,
            current,
            settings.locale,
            settings.pin_set,
            settings.idle_return_minutes,
            settings.keep_screen_on,
            picture,
            tab,
            None if theme is None else theme.primary_color,
        )
        header = board_header(
            view=view,
            base=base,
            sections=sections,
            settings=settings,
            household=await app[HOUSEHOLD_NAME_KEY](),
            clock=clock,
            view_key=key,
            current=current,
        )
        return header, clock

    async def _board(
        self, tab: str
    ) -> tuple[DashboardSettings, BoardBase, HomeBoard] | web.Response:
        """Home on ``tab`` — or the ``409 NIGHT`` / ``404 TAB_OFF`` answer."""
        settings, t = await self._setup()
        snap = await self.request.app[SNAPSHOTS_KEY].home()
        base = board_base(snap, people=snap.people, colors=home_colors(snap), t=t)
        # Night first: at night every other tab is the same 409, whether or
        # not it would show by day.
        if tab != TAB_OVERVIEW and base.night:
            return error_response(409, "NIGHT", "Night mode shows Overview only.")
        board = home_board(snap, base.fmt, tab)
        if board is None:
            return error_response(404, "TAB_OFF", "This tab isn't shown.")
        return settings, base, board

    async def _map_picture(
        self, board: HomeBoard, base: BoardBase
    ) -> tuple[MapScene, dict[str, MapPalette], str] | None:
        """The Map tab's picture scene, both schemes' colours and its key —
        ``None`` when no tile source is wired (no picture then)."""
        app = self.request.app
        pictures = app.get(MAP_PICTURES_KEY)
        if pictures is None or board.map is None:
            return None
        scene = map_scene(board.map, t=base.fmt.t, attribution=MAP_ATTRIBUTION_TEXT)
        css = app[RENDERER_KEY].static_asset("dashboard.css")
        colors = board_colors(
            "" if css is None else css.body.decode("utf-8"), await _theme(app)
        )
        palettes = map_palettes(colors)
        key = scene_key(scene, palettes=palettes, tiles=pictures.source_tag)
        return scene, palettes, key

    async def _home(self, tab: str) -> web.Response:
        found = await self._board(tab)
        if isinstance(found, web.Response):
            return found
        settings, base, board = found
        snap = board.snap
        header, clock = await self._header(
            view=tab, base=base, sections=snap.sections, settings=settings, tab=tab
        )
        if tab == TAB_MESSAGES:
            payload = messages_payload(header, board, base, clock)
        elif tab == TAB_NOTES:
            payload = notes_payload(header, board, base, clock)
        elif tab == TAB_MAP:
            picture = await self._map_picture(board, base)
            image = (
                None
                if picture is None
                else {
                    "url": MAP_PNG_PATH,
                    "width": DEFAULT_SIZE[0],
                    "height": DEFAULT_SIZE[1],
                    "etag": scene_etag(picture[2], *DEFAULT_SIZE, "light"),
                }
            )
            payload = map_payload(
                header, board, base, clock, MAP_ATTRIBUTION_TEXT, image
            )
        else:
            payload = home_payload(header, board, base, clock)
        return json_answer(self.request, payload)


class DisplayHomeView(_DisplayBoardView):
    """``GET /api/display/v1/home`` — Home's Overview."""

    async def get(self) -> web.Response:
        return await self._home(TAB_OVERVIEW)


class DisplayTabView(_DisplayBoardView):
    """``GET /api/display/v1/{messages,notes,map}`` — Home's other tabs."""

    async def get(self) -> web.Response:
        return await self._home(self.request.match_info["tab"])


def _dimension(request: web.Request, name: str) -> int | None:
    """``?name=`` as an int, ``None`` when absent; ``ValueError`` when it
    is not a plain number of at most five digits."""
    raw = request.query.get(name)
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdigit() or len(raw) > 5:
        raise ValueError(name)
    return int(raw)


class DisplayMapPictureView(_DisplayBoardView):
    """``GET /api/display/v1/map.png?w=&h=&scheme=`` — Home's Map tab as
    one PNG (:mod:`.map_png`): ``w`` × ``h`` held to 200–1920 × 200–1200
    (default: the firmware's map area), ``scheme`` ``light`` / ``dark``.

    The same rules as ``/map``: ``409 NIGHT``, ``404 TAB_OFF`` (nobody
    located, presence off), ``423`` when locked (the guard); hidden people
    are never in the read model. ``ETag`` → ``304`` without painting; a
    picture is painted only when nothing kept matches, at most
    :data:`~.auth.MAP_RENDER_MAX` a minute per panel (``429``). ``503``
    when no tile at all can be had."""

    async def get(self) -> web.Response:
        try:
            width, height = clamp_size(
                _dimension(self.request, "w"), _dimension(self.request, "h")
            )
        except ValueError:
            return error_response(400, "BAD_SIZE", "w and h must be whole numbers.")
        scheme = self.request.query.get("scheme", "light")
        if scheme not in SCHEMES:
            return error_response(
                400,
                "BAD_SCHEME",
                "Unsupported scheme.",
                extra={"params": {"allowed": sorted(SCHEMES)}},
            )
        found = await self._board(TAB_MAP)
        if isinstance(found, web.Response):
            return found
        _, base, board = found
        app = self.request.app
        picture = await self._map_picture(board, base)
        if picture is None:
            return _no_tiles()
        scene, palettes, key = picture
        etag = scene_etag(key, width, height, scheme)
        if _etag_matches(self.request, etag):
            return _png(self.request, b"", etag)
        pictures = app[MAP_PICTURES_KEY]
        kept = pictures.cached(key, width, height, scheme)
        if kept is None:
            wait = app[AUTH_KEY].begin_map_render(self.request[DISPLAY_KEY].id)
            if wait:
                response = error_response(429, "RATE_LIMITED", "Too many map pictures.")
                response.headers["Retry-After"] = str(wait)
                return response
            try:
                kept = await pictures.render(
                    scene,
                    key,
                    w=width,
                    h=height,
                    scheme=scheme,
                    palette=palettes[scheme],
                )
            except MapTilesUnavailable:
                return _no_tiles()
        return _png(self.request, kept.png, kept.etag)


def _no_tiles() -> web.Response:
    response = error_response(
        503, "TILES_UNAVAILABLE", "The map tiles can't be loaded right now."
    )
    response.headers["Retry-After"] = str(TILES_RETRY_S)
    return response


class DisplayPersonView(_DisplayBoardView):
    """``GET /api/display/v1/person/{user_id}`` — ``404`` for anyone
    unknown, inactive or hidden (the same answer either way)."""

    async def get(self) -> web.Response:
        settings, t = await self._setup()
        page = await self.request.app[SNAPSHOTS_KEY].person(
            self.request.match_info["user_id"]
        )
        if page is None:
            return error_response(404, "NOT_FOUND", "No such person.")
        base = board_base(
            page,
            people=page.people or (page.person,),
            colors=person_colors(page),
            t=t,
            own_color=page.person.color,
        )
        board = person_board(page, base.fmt)
        header, clock = await self._header(
            view="person",
            base=base,
            sections=page.sections,
            settings=settings,
            current=page.person.user_id,
            picture=page.person.picture_view,
        )
        return json_answer(self.request, person_payload(header, board, base, clock))


class DisplayThemeView(_DisplayBoardView):
    """``GET /api/display/v1/theme`` — colours, font, locale, strings."""

    async def get(self) -> web.Response:
        settings, t = await self._setup()
        css = self.request.app[RENDERER_KEY].static_asset("dashboard.css")
        payload = theme_payload(
            css="" if css is None else css.body.decode("utf-8"),
            theme=await _theme(self.request.app),
            locale=settings.locale,
            t=t,
        )
        return json_answer(self.request, payload)


def _size(request: web.Request, allowed: frozenset[int], default: int) -> int | None:
    raw = request.query.get("size")
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdigit() or int(raw) not in allowed:
        return None
    return int(raw)


def _png(request: web.Request, body: bytes, etag: str) -> web.Response:
    headers = {"ETag": etag, "Cache-Control": DISPLAY_CACHE}
    if _etag_matches(request, etag):
        return web.Response(status=304, headers=headers)
    return web.Response(body=body, content_type="image/png", headers=headers)


def _bad_size(allowed: frozenset[int]) -> web.Response:
    return error_response(
        400,
        "BAD_SIZE",
        "Unsupported size.",
        extra={"params": {"allowed": sorted(allowed)}},
    )


class DisplayAvatarView(web.View):
    """``GET /api/display/v1/avatar/{user_id}?size=N`` — a shown person's
    picture as an ``N`` × ``N`` PNG; ``404`` for anyone hidden, inactive
    or unknown and for a person without one (the same rule as the
    tablet's ``/avatar/…``, asked on every request)."""

    async def get(self) -> web.Response:
        size = _size(self.request, AVATAR_SIZES, DEFAULT_AVATAR_SIZE)
        if size is None:
            return _bad_size(AVATAR_SIZES)
        found = await self.request.app[SNAPSHOTS_KEY].avatar(
            self.request.match_info["user_id"]
        )
        if found is None:
            return error_response(404, "NOT_FOUND", "No picture.")
        body, picture_hash = found
        tag = (
            picture_hash
            if _SAFE_TAG.fullmatch(picture_hash)
            else hashlib.sha256(body).hexdigest()[:32]
        )
        etag = f'"{tag}-{size}"'
        if _etag_matches(self.request, etag):
            return _png(self.request, b"", etag)
        try:
            png = await self.request.app[AVATARS_KEY].png(body, tag, size)
        except Exception:
            log.warning("household dashboard: avatar transcode failed", exc_info=True)
            return error_response(404, "NOT_FOUND", "No picture.")
        return _png(self.request, png, etag)


class DisplayEmojiView(web.View):
    """``GET /api/display/v1/emoji/{code}?size=N`` — a bundled Twemoji
    PNG (:mod:`.emoji`); one the bundle lacks is 📍."""

    async def get(self) -> web.Response:
        size = _size(self.request, EMOJI_SIZES, SOURCE_SIZE)
        if size is None:
            return _bad_size(EMOJI_SIZES)
        code = self.request.match_info["code"]
        if not is_code(code):
            return error_response(404, "NOT_FOUND", "No such emoji.")
        images = self.request.app[EMOJI_KEY]
        resolved = await images.resolve(code)
        etag = f'"{resolved}-{size}"'
        if _etag_matches(self.request, etag):
            return _png(self.request, b"", etag)
        return _png(self.request, await images.png(resolved, size), etag)


def register_display_routes(app: web.Application) -> None:
    app.router.add_view(PAIR_PATH, DisplayPairView)
    app.router.add_view(
        PAIR_PATH + "/{pair_id:[A-Za-z0-9_-]{1,64}}", DisplayPairStatusView
    )
    app.router.add_view(DISPLAY_API_PREFIX + "whoami", DisplayWhoamiView)
    app.router.add_view(LOCK_PATH, DisplayLockView)
    app.router.add_view(UNLOCK_PATH, DisplayUnlockView)
    app.router.add_view(DISPLAY_API_PREFIX + "ack", DisplayAckView)
    app.router.add_view(DISPLAY_API_PREFIX + "home", DisplayHomeView)
    app.router.add_view(
        DISPLAY_API_PREFIX + f"{{tab:{TAB_MESSAGES}|{TAB_NOTES}|{TAB_MAP}}}",
        DisplayTabView,
    )
    app.router.add_view(MAP_PNG_PATH, DisplayMapPictureView)
    app.router.add_view(DISPLAY_API_PREFIX + "person/{user_id}", DisplayPersonView)
    app.router.add_view(DISPLAY_API_PREFIX + "theme", DisplayThemeView)
    app.router.add_view(DISPLAY_API_PREFIX + "avatar/{user_id}", DisplayAvatarView)
    app.router.add_view(
        DISPLAY_API_PREFIX + f"emoji/{{code:{CODE_PATTERN}}}", DisplayEmojiView
    )
