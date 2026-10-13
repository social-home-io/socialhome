"""Wall panels ("displays") — pairing with a short code, device tokens.

A wall panel (e.g. an ESPHome display) has no keyboard, so it pairs the
other way round from a tablet:

1. the panel calls ``POST /api/display/v1/pair`` on the dashboard port
   (anonymous, throttled per client address) and shows the 6-character
   code it gets back (:meth:`DashboardDisplayService.start_pairing`);
2. an admin types that code and a name into Admin › Dashboard
   (:meth:`DashboardDisplayService.claim`): a 256-bit bearer token is
   minted, only its SHA-256 stored (``dashboard.displays``, via
   :class:`DashboardSettingsService`), and the token itself is parked on
   the pending entry;
3. the panel polls ``GET /api/display/v1/pair/{pair_id}`` and collects
   the token — exactly once (:meth:`DashboardDisplayService.poll`).

Pending pairings live in memory only (a restart drops them — the panel
just asks for a new code), at most :data:`MAX_PENDING_PAIRINGS`, each for
:data:`PAIRING_TTL_SECONDS`. Wrong codes are throttled globally — one
bucket for every admin (:meth:`DashboardAuth.begin_pair_code`), separate
from the tablet login / PIN throttle and the login brake. Tokens are never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..dashboard.auth import DashboardAuth
from ..domain.household_dashboard import (
    MAX_DISPLAY_INFO_LENGTH,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_DISPLAYS,
    MAX_PENDING_PAIRINGS,
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    PAIRING_TTL_SECONDS,
    DashboardDisabledError,
    DashboardDisplay,
    DashboardPairingCodeInvalidError,
    DashboardPairingThrottledError,
    DashboardSettingsInvalidError,
    DisplayPairing,
    DisplayPairingStatus,
)
from .dashboard_settings_service import DashboardSettingsService, require_admin

log = logging.getLogger(__name__)

__all__ = ["SEEN_INTERVAL_S", "DashboardDisplayService"]

#: A panel's ``last_seen_at`` / firmware / model are written at most this
#: often (seconds) — a panel polls far more often than that.
SEEN_INTERVAL_S = 60

_TOKEN_BYTES = 32
_PAIR_ID_BYTES = 16
_DISPLAY_ID_BYTES = 8
#: A token as minted (``token_urlsafe(32)`` is 43 characters); anything
#: else is refused before hashing.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_CODE_CHARS = frozenset(PAIRING_CODE_ALPHABET)
#: Typed between code groups, ignored ("ABC-DEF", "abc def").
_CODE_SEPARATORS = re.compile(r"[\s-]+")
_PRINTABLE_ASCII = re.compile(r"[^ -~]+")
#: Format characters a name may keep: the zero-width (non-)joiners emoji
#: sequences need. Every other control / format character (bidi
#: overrides, BOM…) is refused.
_NAME_JOINERS = frozenset("\u200c\u200d")


@dataclass(slots=True)
class _Pending:
    """A pairing waiting for its code (then for its token pickup).
    Service-local and mutable — never leaves this module."""

    code: str
    expires_at: float
    model: str | None
    firmware: str | None
    token: str | None = field(default=None, repr=False)
    display_id: str | None = None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def clean_info(value: object) -> str | None:
    """A panel-reported model / firmware string: printable ASCII only,
    trimmed, at most :data:`MAX_DISPLAY_INFO_LENGTH`; ``None`` if empty."""
    if not isinstance(value, str):
        return None
    text = _PRINTABLE_ASCII.sub("", value).strip()[:MAX_DISPLAY_INFO_LENGTH]
    return text.strip() or None


def _clean_name(value: object) -> str:
    if not isinstance(value, str):
        raise DashboardSettingsInvalidError("name")
    name = value.strip()
    if not 0 < len(name) <= MAX_DISPLAY_NAME_LENGTH or any(
        (unicodedata.category(ch)[0] == "C" and ch not in _NAME_JOINERS)
        or unicodedata.category(ch) in ("Zl", "Zp")
        for ch in name
    ):
        raise DashboardSettingsInvalidError("name")
    return name


def _normalise_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    code = _CODE_SEPARATORS.sub("", value).upper()
    if len(code) != PAIRING_CODE_LENGTH or not set(code) <= _CODE_CHARS:
        return None
    return code


class DashboardDisplayService:
    """Pairs wall panels and checks their bearer tokens."""

    __slots__ = ("_settings", "_auth", "_clock", "_pending", "_claim_lock", "_seen")

    def __init__(
        self,
        settings: DashboardSettingsService,
        auth: DashboardAuth,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._auth = auth
        self._clock = clock
        # pair_id → pending entry; insertion-ordered (oldest first).
        self._pending: dict[str, _Pending] = {}
        # One claim at a time: a code can't be spent twice across an await.
        self._claim_lock = asyncio.Lock()
        # display id → when its "seen" was last written (clock seconds).
        self._seen: dict[str, float] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    async def _require_enabled(self) -> None:
        if not (await self._settings.get()).enabled:
            raise DashboardDisabledError()

    def _prune(self, now: float) -> None:
        for pair_id in [p for p, e in self._pending.items() if now >= e.expires_at]:
            del self._pending[pair_id]

    def _new_code(self) -> str:
        taken = {e.code for e in self._pending.values()}
        while True:
            code = "".join(
                secrets.choice(PAIRING_CODE_ALPHABET)
                for _ in range(PAIRING_CODE_LENGTH)
            )
            if code not in taken:
                return code

    # ── Panel side (anonymous, dashboard port) ──────────────────────────

    async def start_pairing(
        self, *, model: object = None, firmware: object = None
    ) -> DisplayPairing:
        """A new pairing code for a panel. The caller throttles per
        client address (:meth:`DashboardAuth.begin_pair`)."""
        await self._require_enabled()
        now = self._clock()
        self._prune(now)
        while len(self._pending) >= MAX_PENDING_PAIRINGS:
            # The oldest still waiting for its code — never one whose token
            # is parked for pickup (that would orphan a stored panel); only
            # when every entry is parked, the oldest of all.
            victim = next(
                (p for p, e in self._pending.items() if e.token is None),
                next(iter(self._pending)),
            )
            del self._pending[victim]
            log.info("household dashboard: pairing list full — dropped the oldest")
        pair_id = secrets.token_urlsafe(_PAIR_ID_BYTES)
        code = self._new_code()
        self._pending[pair_id] = _Pending(
            code=code,
            expires_at=now + PAIRING_TTL_SECONDS,
            model=clean_info(model),
            firmware=clean_info(firmware),
        )
        return DisplayPairing(
            pair_id=pair_id, code=code, expires_in=PAIRING_TTL_SECONDS
        )

    async def poll(self, pair_id: str) -> DisplayPairingStatus | None:
        """The pairing's state; ``None`` when unknown or expired. Once
        paired, the token is handed out exactly once and the entry
        dropped — and only while the panel is still paired (removed, or
        unpaired by an epoch bump, before pickup → ``None``)."""
        self._prune(self._clock())
        entry = self._pending.get(pair_id)
        if entry is None:
            return None
        if entry.token is None:
            return DisplayPairingStatus(paired=False)
        stored = {d.id for d in await self._settings.displays()}
        if self._pending.pop(pair_id, None) is None:
            return None  # a concurrent poll collected it during the await
        if entry.display_id not in stored:
            return None
        return DisplayPairingStatus(
            paired=True, token=entry.token, display_id=entry.display_id
        )

    # ── Admin side ──────────────────────────────────────────────────────

    async def claim(
        self, *, actor_is_admin: bool, code: object, name: object
    ) -> DashboardDisplay:
        """Pair the panel showing ``code`` under ``name``.

        Every pending code is compared in constant time. Wrong codes count
        against one global throttle bucket (429 once it is full, even for
        the right code).
        """
        require_admin(actor_is_admin)
        clean = _clean_name(name)
        wanted = _normalise_code(code)
        if wanted is None:
            raise DashboardPairingCodeInvalidError()
        await self._require_enabled()
        async with self._claim_lock:
            wait = self._auth.begin_pair_code()
            if wait:
                raise DashboardPairingThrottledError(wait)
            now = self._clock()
            self._prune(now)
            match: _Pending | None = None
            for entry in self._pending.values():
                same = hmac.compare_digest(entry.code.encode(), wanted.encode())
                if same and entry.token is None and match is None:
                    match = entry
            if match is None:
                log.info("household dashboard: wrong display pairing code")
                raise DashboardPairingCodeInvalidError()
            self._auth.record_pair_code_success()
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            display = DashboardDisplay(
                id=secrets.token_hex(_DISPLAY_ID_BYTES),
                name=clean,
                token_hash=_token_hash(token),
                created_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                model=match.model,
                firmware=match.firmware,
            )
            await self._settings.add_display(display)
            match.token = token
            match.display_id = display.id
            # A fresh window to collect the token.
            match.expires_at = now + PAIRING_TTL_SECONDS
        log.info("household dashboard: paired display %s", display.id)
        return display

    async def list(self, *, actor_is_admin: bool) -> tuple[DashboardDisplay, ...]:
        require_admin(actor_is_admin)
        return await self._settings.displays()

    async def rename(
        self, *, actor_is_admin: bool, display_id: str, name: object
    ) -> DashboardDisplay:
        require_admin(actor_is_admin)
        return await self._settings.rename_display(display_id, _clean_name(name))

    async def remove(self, *, actor_is_admin: bool, display_id: str) -> None:
        """Unpair a panel — its token stops working at once."""
        require_admin(actor_is_admin)
        await self._settings.remove_display(display_id)
        self._seen.pop(display_id, None)
        for pair_id in [
            p for p, e in self._pending.items() if e.display_id == display_id
        ]:
            del self._pending[pair_id]
        log.info("household dashboard: removed display %s", display_id)

    # ── Device auth ─────────────────────────────────────────────────────

    async def authenticate(self, token: object) -> DashboardDisplay | None:
        """The paired panel whose bearer token this is, else ``None``.
        Compares the SHA-256 against every panel in constant time."""
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            return None
        digest = _token_hash(token).encode("ascii")
        found: DashboardDisplay | None = None
        for display in await self._settings.displays():
            if hmac.compare_digest(display.token_hash.encode("utf-8"), digest):
                found = display
        return found

    async def seen(
        self, display: DashboardDisplay, *, firmware: object, model: object
    ) -> None:
        """Record a panel's request — written at most once per
        :data:`SEEN_INTERVAL_S` per panel."""
        now = self._clock()
        last = self._seen.get(display.id)
        if last is not None and now - last < SEEN_INTERVAL_S:
            return
        if len(self._seen) >= 2 * MAX_DISPLAYS:
            # Removed / unknown ids: forget every quiet one.
            self._seen = {
                k: t for k, t in self._seen.items() if now - t < SEEN_INTERVAL_S
            }
        self._seen[display.id] = now
        await self._settings.record_display_seen(
            display.id,
            at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
            firmware=clean_info(firmware),
            model=clean_info(model),
        )
