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

**Panels found through the platform** (Home Assistant — only when the
adapter has a :class:`~socialhome.platform.adapter.DisplayDirectory`)
pair without a code:

1. Admin › Dashboard lists them (:meth:`DashboardDisplayService.platform_panels`);
2. *Connect* (:meth:`DashboardDisplayService.connect_platform_panel`)
   mints a **claim** — 256 random bits, only its SHA-256 kept in memory,
   bound to that panel (its platform id, MAC and published public key),
   single-use, valid :data:`CLAIM_TTL_SECONDS`, at most
   :data:`MAX_PENDING_CLAIMS` (one per panel) — seals it together with
   the dashboard's address to the panel's public key
   (:mod:`socialhome.dashboard.claim_seal`) and asks the platform to hand
   the sealed value to the panel;
3. the panel opens it and spends the claim at
   ``POST /api/display/v1/pair/claim`` with its MAC and public key
   (:meth:`DashboardDisplayService.spend_claim`) and gets its token in
   the answer.

Only ciphertext crosses Home Assistant (its recorder stores every
action's data, an admin token can watch ``call_service`` live): the
claim and the address are readable by the panel's private key alone,
which never leaves the panel. The token never crosses Home Assistant at
all. Home Assistant admins are still trusted with the panel itself —
they can reflash it (ESPHome) or swap the key sensor it reports — so
Connect means "the panel Home Assistant vouches for". What is bounded
besides: a claim pairs one device once (spent by its first use, even a
wrong-MAC or wrong-key one), lives :data:`CLAIM_TTL_SECONDS`, needs the
public key it was sealed to (a claim learned elsewhere is useless
without the matching key), and dies with a "sign out all tablets" /
password change (it is bound to the session epoch) or with the removal
of the panel's display.
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..dashboard.auth import THROTTLE_MAX_FAILURES, DashboardAuth
from ..dashboard.claim_seal import InvalidPanelKey, seal_claim
from ..domain.household_dashboard import (
    CLAIM_TTL_SECONDS,
    MAX_DISPLAY_INFO_LENGTH,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_DISPLAYS,
    MAX_PENDING_CLAIMS,
    MAX_PENDING_PAIRINGS,
    PAIRING_CODE_ALPHABET,
    PAIRING_CODE_LENGTH,
    PAIRING_TTL_SECONDS,
    DashboardDisabledError,
    DashboardDisplay,
    DashboardDisplayNotFoundError,
    DashboardDisplayPinThrottledError,
    DashboardDisplayPinWrongError,
    DashboardDisplaySignedOutError,
    DashboardPairingCodeInvalidError,
    DashboardPairingThrottledError,
    DashboardPinRequiredError,
    DashboardSettingsInvalidError,
    DisplayDiscoveryUnavailableError,
    DisplayPairing,
    DisplayPairingStatus,
    DisplayPanelNeedsUpdateError,
    DisplayPanelNotFoundError,
    DisplayPanelOfflineError,
    ExternalDisplayPanel,
    normalize_mac,
    parse_panel_key,
)
from ..platform.adapter import DisplayDirectory
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
    mac: str | None = None


@dataclass(slots=True, frozen=True)
class _Claim:
    """A connect claim waiting for its panel. Service-local — never
    leaves this module; only the claim's SHA-256 is kept."""

    digest: bytes = field(repr=False)
    mac: str
    #: The panel public key (raw 32 bytes) the claim was sealed to — the
    #: panel must present it when it spends the claim.
    panel_key: bytes
    name: str
    expires_at: float
    #: The session epoch it was minted under — a bump revokes it.
    epoch: int


#: A platform panel's name when its own isn't usable as a display name.
_FALLBACK_PANEL_NAME = "Wall panel"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _claim_digest(claim: str) -> bytes:
    return hashlib.sha256(claim.encode("ascii")).digest()


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


def _panel_display_name(value: str) -> str:
    """A platform panel's name as a display name: cut to
    :data:`MAX_DISPLAY_NAME_LENGTH`, :data:`_FALLBACK_PANEL_NAME` when it
    still isn't a valid one."""
    try:
        return _clean_name(value.strip()[:MAX_DISPLAY_NAME_LENGTH])
    except DashboardSettingsInvalidError:
        return _FALLBACK_PANEL_NAME


def _normalise_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    code = _CODE_SEPARATORS.sub("", value).upper()
    if len(code) != PAIRING_CODE_LENGTH or not set(code) <= _CODE_CHARS:
        return None
    return code


class DashboardDisplayService:
    """Pairs wall panels and checks their bearer tokens."""

    __slots__ = (
        "_settings",
        "_auth",
        "_clock",
        "_directory",
        "_pending",
        "_claims",
        "_claim_lock",
        "_seen",
    )

    def __init__(
        self,
        settings: DashboardSettingsService,
        auth: DashboardAuth,
        *,
        directory: DisplayDirectory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._auth = auth
        self._clock = clock
        # The platform's panels (Home Assistant); ``None`` on standalone.
        self._directory = directory
        # platform panel id → its pending connect claim; oldest first.
        self._claims: dict[str, _Claim] = {}
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

    @property
    def claim_count(self) -> int:
        return len(self._claims)

    @property
    def can_discover(self) -> bool:
        """The platform lists wall panels (Home Assistant)."""
        return self._directory is not None

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
        self, *, model: object = None, firmware: object = None, mac: object = None
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
            mac=normalize_mac(mac),
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
                mac=match.mac,
            )
            await self._settings.add_display(display)
            match.token = token
            match.display_id = display.id
            # A fresh window to collect the token.
            match.expires_at = now + PAIRING_TTL_SECONDS
        log.info("household dashboard: paired display %s", display.id)
        return display

    # ── Panels found through the platform (Home Assistant) ──────────────

    def _require_directory(self) -> DisplayDirectory:
        if self._directory is None:
            raise DisplayDiscoveryUnavailableError()
        return self._directory

    def _prune_claims(self, now: float) -> None:
        for key in [k for k, c in self._claims.items() if now >= c.expires_at]:
            del self._claims[key]

    async def platform_panels(
        self, *, actor_is_admin: bool
    ) -> list[tuple[ExternalDisplayPanel, DashboardDisplay | None]]:
        """The panels the platform knows, each with the paired display it
        is (matched by MAC) or ``None``.
        :class:`DisplayDiscoveryUnavailableError` without a directory;
        :class:`DisplayDiscoveryFailedError` when the platform didn't
        answer."""
        require_admin(actor_is_admin)
        panels = await self._require_directory().list_panels()
        # A display still waiting to take over (``replaces``) loses to the
        # pairing that works today.
        by_mac = {
            d.mac: d
            for d in sorted(
                await self._settings.displays(), key=lambda d: not d.replaces
            )
            if d.mac
        }
        return [(p, by_mac.get(p.mac) if p.mac else None) for p in panels]

    async def connect_platform_panel(
        self, *, actor_is_admin: bool, panel_id: str, url: str
    ) -> int:
        """Send the platform panel ``panel_id`` a connect request: a fresh
        claim (replacing any it had) plus ``url``, the dashboard address it
        should spend it at. Returns how long the claim stays valid
        (seconds). A request the platform couldn't deliver takes its claim
        with it."""
        require_admin(actor_is_admin)
        directory = self._require_directory()
        await self._require_enabled()
        panel = next(
            (p for p in await directory.list_panels() if p.id == panel_id), None
        )
        if panel is None:
            raise DisplayPanelNotFoundError()
        if not panel.online:
            raise DisplayPanelOfflineError()
        panel_key = parse_panel_key(panel.panel_key)
        if panel.connect_action is None or panel.mac is None or panel_key is None:
            raise DisplayPanelNeedsUpdateError()
        claim = secrets.token_urlsafe(_TOKEN_BYTES)
        try:
            sealed = seal_claim(claim=claim, url=url, panel_pub=panel_key)
        except InvalidPanelKey as exc:
            # A key no firmware would publish (e.g. a small-order point).
            log.warning(
                "household dashboard: panel %s publishes an unusable key", panel.id
            )
            raise DisplayPanelNeedsUpdateError() from exc
        epoch = await self._settings.epoch()
        async with self._claim_lock:
            now = self._clock()
            self._prune_claims(now)
            self._claims.pop(panel.id, None)
            while len(self._claims) >= MAX_PENDING_CLAIMS:
                del self._claims[next(iter(self._claims))]
                log.info(
                    "household dashboard: connect claims full — dropped the oldest"
                )
            entry = _Claim(
                digest=_claim_digest(claim),
                mac=panel.mac,
                panel_key=panel_key,
                name=_panel_display_name(panel.name),
                expires_at=now + CLAIM_TTL_SECONDS,
                epoch=epoch,
            )
            self._claims[panel.id] = entry
        try:
            await directory.connect(panel, sealed=sealed)
        except BaseException:
            # Undelivered: nobody should be able to spend it.
            if self._claims.get(panel.id) is entry:
                del self._claims[panel.id]
            raise
        log.info("household dashboard: sent panel %s a connect request", panel.id)
        return CLAIM_TTL_SECONDS

    async def spend_claim(
        self,
        claim: object,
        *,
        mac: object,
        panel_key: object,
        model: object = None,
        firmware: object = None,
    ) -> DisplayPairingStatus | None:
        """Pair the panel spending ``claim``: a new display (named as on
        the platform) and its token, handed out here and only here.
        ``None`` — uniformly — when the claim is malformed, unknown,
        expired, already spent, or ``mac`` / ``panel_key`` (the panel's
        base64url public key) isn't the panel it was minted and sealed
        for. Every claim is compared in constant time; a matching one is
        spent by this call whatever its outcome. A display with the same
        MAC (the panel's old pairing) is replaced — not here, but on the
        new token's first authenticated use (:meth:`authenticate`), so
        whoever spends a claim the panel never sees can't break the
        panel's working pairing without using what they got. The caller
        throttles per client address."""
        await self._require_enabled()
        if not isinstance(claim, str) or _TOKEN.fullmatch(claim) is None:
            return None
        digest = _claim_digest(claim)
        async with self._claim_lock:
            now = self._clock()
            self._prune_claims(now)
            found: str | None = None
            for key, entry in self._claims.items():
                if hmac.compare_digest(entry.digest, digest) and found is None:
                    found = key
            if found is None:
                log.info("household dashboard: unknown connect claim")
                return None
            spent = self._claims.pop(found)
            presented = parse_panel_key(panel_key)
            if normalize_mac(mac) != spent.mac or not hmac.compare_digest(
                presented or b"", spent.panel_key
            ):
                log.warning(
                    "household dashboard: connect claim for panel %s spent by"
                    " another device — refused",
                    found,
                )
                return None
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            display = DashboardDisplay(
                id=secrets.token_hex(_DISPLAY_ID_BYTES),
                name=spent.name,
                token_hash=_token_hash(token),
                created_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                model=clean_info(model),
                firmware=clean_info(firmware),
                mac=spent.mac,
            )
            dropped = await self._settings.add_display(
                display, supersede_same_mac=True, expected_epoch=spent.epoch
            )
            if dropped is None:
                # Minted before "sign out all tablets" / a password change.
                log.info("household dashboard: connect claim revoked by sign-out")
                return None
            for display_id in dropped:
                self._forget(display_id)
        log.info(
            "household dashboard: panel %s connected as display %s", found, display.id
        )
        return DisplayPairingStatus(paired=True, token=token, display_id=display.id)

    async def list(self, *, actor_is_admin: bool) -> tuple[DashboardDisplay, ...]:
        require_admin(actor_is_admin)
        return await self._settings.displays()

    async def remove(self, *, actor_is_admin: bool, display_id: str) -> None:
        """Unpair a panel — its token stops working at once, and a connect
        claim or a re-connect still pending for it (same MAC) is dropped."""
        require_admin(actor_is_admin)
        mac = next(
            (d.mac for d in await self._settings.displays() if d.id == display_id),
            None,
        )
        for removed in await self._settings.remove_display(display_id):
            self._forget(removed)
        if mac is not None:
            for key in [k for k, c in self._claims.items() if c.mac == mac]:
                del self._claims[key]
        log.info("household dashboard: removed display %s", display_id)

    async def update(
        self, *, actor_is_admin: bool, display_id: str, patch: Mapping[str, object]
    ) -> DashboardDisplay:
        """Admin › Dashboard's edit of a panel: ``name`` renames it,
        ``locked: false`` lifts its privacy lock (recovery for a panel
        nobody can unlock — only the panel itself locks). Validated before
        anything is written; with neither key it is a rename (``name``
        missing → :class:`DashboardSettingsInvalidError`)."""
        require_admin(actor_is_admin)
        unlock = "locked" in patch
        if unlock and patch["locked"] is not False:
            raise DashboardSettingsInvalidError("locked")
        change: dict[str, object] = {}
        if "name" in patch or not unlock:
            change["name"] = _clean_name(patch.get("name"))
        if unlock:
            change["locked"] = False
        display = await self._settings.update_display(display_id, change)
        if unlock:
            # A rescued panel starts with fresh PIN tries.
            self._auth.record_success(
                DashboardAuth.display_pin_throttle_key(display_id), count_global=False
            )
            log.info("household dashboard: admin unlocked display %s", display_id)
        return display

    def _forget(self, display_id: str) -> None:
        self._seen.pop(display_id, None)
        for pair_id in [
            p for p, e in self._pending.items() if e.display_id == display_id
        ]:
            del self._pending[pair_id]

    # ── Privacy lock (panel side) ───────────────────────────────────────
    #
    # The tablet keeps its lock in its signed cookie; a panel's lives on
    # its stored record, so a reboot can't lift it. The PIN rules are the
    # tablet's (``views.UnlockView``): five wrong PINs in the throttle
    # window, then the next try — right or wrong — signs the panel out
    # (here: unpairs it; only an admin pairing it again helps).

    async def is_locked(self, display: DashboardDisplay) -> bool:
        """``display`` is privacy-locked: its flag is set *and* a PIN
        exists (with no PIN nothing can be locked — like a tablet's
        locked cookie, which counts as unlocked then)."""
        return display.locked and (await self._settings.get()).pin_set

    async def lock(self, display: DashboardDisplay) -> None:
        """Privacy-lock the panel. :class:`DashboardPinRequiredError`
        while no PIN is set (nothing could unlock it)."""
        if not (await self._settings.get()).pin_set:
            raise DashboardPinRequiredError()
        try:
            await self._settings.update_display(display.id, {"locked": True})
        except DashboardDisplayNotFoundError:
            raise DashboardDisplaySignedOutError() from None
        log.info("household dashboard: display %s locked", display.id)

    async def unlock(self, display: DashboardDisplay, pin: object) -> None:
        """Lift the panel's lock with the household PIN.

        A panel that isn't locked is left alone — no PIN is checked, so
        there is nothing to guess against. Otherwise the guess is counted
        before the (slow, constant-time) scrypt check; a wrong one raises
        :class:`DashboardDisplayPinWrongError` with the tries left. Once
        the panel's wrong PINs are spent, the next try unpairs it and
        raises :class:`DashboardDisplaySignedOutError`; any other
        hold-back (a full throttle table) only asks it to wait
        (:class:`DashboardDisplayPinThrottledError`).
        """
        # The stored record, not the request's snapshot: a /lock that
        # landed after this request authenticated still needs the PIN.
        stored = next(
            (d for d in await self._settings.displays() if d.id == display.id), None
        )
        if stored is None:
            raise DashboardDisplaySignedOutError()
        if not await self.is_locked(stored):
            return
        auth = self._auth
        key = DashboardAuth.display_pin_throttle_key(display.id)
        wait = auth.begin_attempt(key, count_global=False)
        if wait and auth.failure_count(key) >= THROTTLE_MAX_FAILURES:
            log.warning(
                "household dashboard: too many wrong PINs on display %s — unpaired",
                display.id,
            )
            try:
                removed = await self._settings.remove_display(display.id)
            except DashboardDisplayNotFoundError:
                removed = (display.id,)
            for display_id in removed:
                self._forget(display_id)
            raise DashboardDisplaySignedOutError()
        if wait:
            raise DashboardDisplayPinThrottledError(wait)
        if not isinstance(pin, str) or not await auth.check_pin(pin):
            log.info("household dashboard: wrong PIN on display %s", display.id)
            raise DashboardDisplayPinWrongError(
                max(0, THROTTLE_MAX_FAILURES - auth.failure_count(key))
            )
        auth.record_success(key, count_global=False)
        try:
            await self._settings.update_display(display.id, {"locked": False})
        except DashboardDisplayNotFoundError:
            raise DashboardDisplaySignedOutError() from None
        log.info("household dashboard: display %s unlocked", display.id)

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
        if found is not None and found.replaces:
            # A re-connected panel's first use: its old pairing goes now.
            taken = await self._settings.take_over(found.id)
            if taken is None:
                return None  # removed since we read it
            for display_id in taken:
                self._forget(display_id)
                log.info(
                    "household dashboard: display %s took over from %s",
                    found.id,
                    display_id,
                )
            found = next(
                (d for d in await self._settings.displays() if d.id == found.id),
                None,
            )
        return found

    async def seen(
        self,
        display: DashboardDisplay,
        *,
        firmware: object,
        model: object,
        mac: object = None,
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
            # Write-once: a self-reported MAC fills a gap (a panel paired
            # before it sent one) but never moves a known one — else any
            # paired panel could pose as another in the Home Assistant list.
            mac=normalize_mac(mac) if display.mac is None else None,
        )
