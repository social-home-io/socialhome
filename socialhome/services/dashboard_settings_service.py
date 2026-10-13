"""Household dashboard settings, password, sessions and setup tokens.

Everything lives in the ``instance_config`` key/value table under the
``dashboard.`` prefix:

* ``dashboard.settings`` — JSON of the non-secret settings (incl. people);
* ``dashboard.password_hash`` / ``dashboard.password_set_at`` — the
  scrypt hash of the household dashboard password and when it was set;
* ``dashboard.cookie_secret`` — the 32-byte tablet-cookie key, wrapped
  under the instance KEK (:class:`KeyManager`, attached after startup);
* ``dashboard.epoch`` — session epoch; bumping it signs out every tablet;
* ``dashboard.pin_hash`` / ``dashboard.pin_set_at`` — the scrypt hash of
  the tablets' privacy-lock PIN and when it was set (the timestamp alone
  tells the dashboard a PIN exists, so its per-request settings read
  never touches the hash);
* ``dashboard.setup_token`` — JSON ``{"hash", "expires_at"}`` of the one
  pending single-use tablet setup token (only the SHA-256 is stored);
* ``dashboard.displays`` — JSON list of the paired wall panels
  (:class:`DashboardDisplay`; only each bearer token's SHA-256 is
  stored). Every epoch bump ("sign out all tablets", a new password)
  empties it in the same write — panels are unpaired with the tablets.
  The pairing flow itself lives in
  :mod:`socialhome.services.dashboard_display_service`; this service
  only stores the list, under the same lock as the epoch.

Admin-only methods take ``actor_is_admin`` and raise
:class:`PermissionError` when it is ``False``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..domain.household_dashboard import (
    IDLE_RETURN_CHOICES,
    MAX_DISPLAYS,
    MAX_ADDRESS_LENGTH,
    MAX_PIN_LENGTH,
    MIN_PASSWORD_LENGTH,
    MIN_PIN_LENGTH,
    SETUP_TOKEN_TTL_SECONDS,
    SUPPORTED_DASHBOARD_LOCALES,
    DashboardDisplay,
    DashboardDisplayLimitError,
    DashboardDisplayNotFoundError,
    DashboardPasswordRequiredError,
    DashboardPasswordTooShortError,
    DashboardPersonSettings,
    DashboardPinInvalidError,
    DashboardPortFixedError,
    DashboardSettings,
    DashboardSettingsInvalidError,
)
from ..infrastructure.key_manager import KeyManager, KeyManagerError
from ..platform.local_credentials import hash_password, verify_password
from ..repositories.instance_config_repo import AbstractInstanceConfigRepo

log = logging.getLogger(__name__)

KEY_PREFIX = "dashboard."
KEY_SETTINGS = "dashboard.settings"
KEY_PASSWORD_HASH = "dashboard.password_hash"
KEY_PASSWORD_SET_AT = "dashboard.password_set_at"
KEY_COOKIE_SECRET = "dashboard.cookie_secret"
KEY_EPOCH = "dashboard.epoch"
KEY_SETUP_TOKEN = "dashboard.setup_token"
KEY_PIN_HASH = "dashboard.pin_hash"
KEY_PIN_SET_AT = "dashboard.pin_set_at"
KEY_DISPLAYS = "dashboard.displays"
#: The stored value of "no panels paired".
_NO_DISPLAYS = "[]"

_COOKIE_SECRET_BYTES = 32
_COOKIE_SECRET_AAD = KEY_COOKIE_SECRET.encode("ascii")
_MIN_PORT = 1024
_MAX_PORT = 65535
# ``[0-9]``, not ``\d`` — ``\d`` also matches other scripts' digits.
_PIN = re.compile(rf"[0-9]{{{MIN_PIN_LENGTH},{MAX_PIN_LENGTH}}}")
_HHMM = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_BOOL_FIELDS = frozenset(
    {
        "enabled",
        "keep_screen_on",
        "show_status_text",
        "show_household_messages",
        "mention_sound",
    }
)
_PERSON_KEYS = frozenset({"show", "picture"})
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Printable ASCII without space, ``\\``, ``?``, ``#`` and ``@`` — no query,
# fragment or userinfo can hide in an accepted address.
_ADDRESS_CHARS = re.compile(r"[!-~]+")
_ADDRESS_FORBIDDEN = frozenset("\\?#@")


class _Invalid(Exception):
    """Internal: a value failed validation (field named by the caller)."""


def require_admin(actor_is_admin: bool) -> None:
    """Refuse a dashboard management call from a non-admin (403 via BaseView)."""
    if not actor_is_admin:
        raise PermissionError("Only household admins may manage the dashboard")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_address(value: object) -> str | None:
    """Normalise an admin-set dashboard address; raise :class:`_Invalid`.

    ``None`` / ``""`` clear it. Otherwise ``http(s)://host[:port]`` with an
    optional trailing ``/`` (stripped) — no path, query, fragment or
    userinfo. Scheme and host are lower-cased.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_ADDRESS_LENGTH:
        raise _Invalid
    if _ADDRESS_CHARS.fullmatch(value) is None or not _ADDRESS_FORBIDDEN.isdisjoint(
        value
    ):
        raise _Invalid
    try:
        parts = urlsplit(value)
        # ``.port`` raises on a non-numeric or out-of-range port.
        if parts.port == 0:
            raise _Invalid
    except ValueError:
        raise _Invalid from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or parts.path not in ("", "/"):
        raise _Invalid
    netloc = parts.netloc.lower()
    host = parts.hostname
    if not host or netloc.endswith(":"):
        raise _Invalid
    if netloc.startswith("["):
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise _Invalid from None
    elif not all(_HOST_LABEL.fullmatch(label) for label in host.split(".")):
        raise _Invalid
    return f"{scheme}://{netloc}"


def _check_scalar(field: str, value: object) -> object:
    """Validate one non-people setting; raise :class:`_Invalid` if bad.

    The port is range-checked only — the clash with the main port is an
    update-time rule (:meth:`DashboardSettingsService.update`).
    """
    if field in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise _Invalid
    elif field == "port":
        if not _is_int(value) or not _MIN_PORT <= value <= _MAX_PORT:  # type: ignore[operator]
            raise _Invalid
    elif field == "locale":
        if not isinstance(value, str) or value not in SUPPORTED_DASHBOARD_LOCALES:
            raise _Invalid
    elif field == "idle_return_minutes":
        if not _is_int(value) or value not in IDLE_RETURN_CHOICES:
            raise _Invalid
    elif field in ("night_start", "night_end"):
        if not isinstance(value, str) or _HHMM.fullmatch(value) is None:
            raise _Invalid
    elif field == "address":
        return _check_address(value)
    else:
        raise _Invalid
    return value


_SCALAR_FIELDS = (
    "enabled",
    "port",
    "locale",
    "idle_return_minutes",
    "night_start",
    "night_end",
    "keep_screen_on",
    "show_status_text",
    "show_household_messages",
    "mention_sound",
    "address",
)


def _check_person(
    raw: object, base: DashboardPersonSettings
) -> DashboardPersonSettings:
    """Merge one ``{"show", "picture"}`` patch over ``base``."""
    if not isinstance(raw, Mapping) or not set(raw) <= _PERSON_KEYS:
        raise _Invalid
    show = raw.get("show", base.show)
    picture = raw.get("picture", base.picture)
    if not isinstance(show, bool):
        raise _Invalid
    if picture is not None and not isinstance(picture, bool):
        raise _Invalid
    return DashboardPersonSettings(show=show, picture=picture)


def _merge_people(
    raw: object, current: Mapping[str, DashboardPersonSettings]
) -> dict[str, DashboardPersonSettings]:
    if not isinstance(raw, Mapping):
        raise _Invalid
    merged = dict(current)
    for user_id, entry in raw.items():
        if not isinstance(user_id, str) or not user_id:
            raise _Invalid
        if entry is None:
            # ``null`` drops the entry — the person is back on defaults.
            merged.pop(user_id, None)
            continue
        merged[user_id] = _check_person(
            entry, current.get(user_id, DashboardPersonSettings())
        )
    return merged


def _settings_to_json(settings: DashboardSettings) -> str:
    data: dict[str, object] = {f: getattr(settings, f) for f in _SCALAR_FIELDS}
    data["people"] = {
        uid: {"show": p.show, "picture": p.picture} for uid, p in settings.people
    }
    return json.dumps(data, sort_keys=True)


def _settings_from_json(raw: str | None) -> DashboardSettings:
    """Decode stored settings leniently: corrupt JSON → defaults, an
    invalid field → that field's default (each logged as a warning)."""
    if raw is None:
        return DashboardSettings()
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("stored dashboard settings are not valid JSON — using defaults")
        return DashboardSettings()
    if not isinstance(data, dict):
        log.warning("stored dashboard settings are not an object — using defaults")
        return DashboardSettings()
    fields: dict[str, object] = {}
    for name in _SCALAR_FIELDS:
        if name not in data:
            continue
        try:
            fields[name] = _check_scalar(name, data[name])
        except _Invalid:
            log.warning("stored dashboard setting %s is invalid — using default", name)
    people: list[tuple[str, DashboardPersonSettings]] = []
    raw_people = data.get("people", {})
    if isinstance(raw_people, dict):
        for uid, entry in raw_people.items():
            try:
                people.append((uid, _check_person(entry, DashboardPersonSettings())))
            except _Invalid:
                log.warning("stored dashboard person entry is invalid — skipped")
    else:
        log.warning("stored dashboard people are not an object — ignored")
    return DashboardSettings(**fields, people=tuple(people))  # type: ignore[arg-type]


_DISPLAY_STR_FIELDS = ("id", "name", "token_hash", "created_at")
_DISPLAY_OPT_FIELDS = ("last_seen_at", "firmware", "model")


def _display_to_dict(d: DashboardDisplay) -> dict[str, object]:
    return {
        "id": d.id,
        "name": d.name,
        "token_hash": d.token_hash,
        "created_at": d.created_at,
        "last_seen_at": d.last_seen_at,
        "firmware": d.firmware,
        "model": d.model,
        "locked": d.locked,
    }


def _display_from_dict(raw: object) -> DashboardDisplay | None:
    if not isinstance(raw, dict):
        return None
    for name in _DISPLAY_STR_FIELDS:
        if not isinstance(raw.get(name), str) or not raw[name]:
            return None
    for name in _DISPLAY_OPT_FIELDS:
        if raw.get(name) is not None and not isinstance(raw[name], str):
            return None
    locked = raw.get("locked", False)
    if not isinstance(locked, bool):
        return None
    return DashboardDisplay(
        id=raw["id"],
        name=raw["name"],
        token_hash=raw["token_hash"],
        created_at=raw["created_at"],
        last_seen_at=raw.get("last_seen_at"),
        firmware=raw.get("firmware"),
        model=raw.get("model"),
        locked=locked,
    )


def _displays_from_json(raw: str | None) -> tuple[DashboardDisplay, ...]:
    """Decode the stored panels. Corrupt storage fails closed (no panel
    authenticates); a bad entry is skipped — each logged as a warning."""
    if raw is None:
        return ()
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("stored dashboard displays are not valid JSON — none paired")
        return ()
    if not isinstance(data, list):
        log.warning("stored dashboard displays are not a list — none paired")
        return ()
    displays: list[DashboardDisplay] = []
    for entry in data:
        display = _display_from_dict(entry)
        if display is None:
            log.warning("stored dashboard display entry is invalid — skipped")
            continue
        displays.append(display)
    return tuple(displays)


def _displays_to_json(displays: tuple[DashboardDisplay, ...]) -> str:
    return json.dumps([_display_to_dict(d) for d in displays])


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DashboardSettingsService:
    """Admin-managed household dashboard configuration and credentials."""

    __slots__ = (
        "_repo",
        "_fixed_port",
        "_main_port",
        "_lock",
        "_key_manager",
        "_cookie_secret",
        "_cached",
        "_generation",
        "_displays",
    )

    def __init__(
        self,
        repo: AbstractInstanceConfigRepo,
        *,
        fixed_port: int | None,
        main_port: int,
    ) -> None:
        self._repo = repo
        self._fixed_port = fixed_port
        self._main_port = main_port
        # Serialises every read-modify-write (settings, epoch, password,
        # cookie secret, setup token) so concurrent callers can't lose an
        # update, mint two secrets or spend one token twice. Locked public
        # methods only call the unlocked ``_*_unlocked`` helpers.
        self._lock = asyncio.Lock()
        self._key_manager: KeyManager | None = None
        # The unwrapped cookie key, cached after the first load / creation:
        # every dashboard request verifies a cookie, and it shouldn't take
        # the lock + a KEK decrypt each time. Only this service writes the
        # stored key, so the cache can't go stale behind its back.
        self._cookie_secret: bytes | None = None
        # The stored settings (+ password timestamp) after the first load:
        # the dashboard reads them on every request (locale, Host
        # allow-list). Every write path replaces or drops the cache and
        # bumps ``_generation``, so a read that raced a write is never
        # cached (only this service writes these keys).
        self._cached: DashboardSettings | None = None
        self._generation = 0
        # The paired panels after the first load — a panel's every request
        # looks its token up here. Read and written only under ``_lock``.
        self._displays: tuple[DashboardDisplay, ...] | None = None

    def attach_key_manager(self, key_manager: KeyManager) -> None:
        """Late-wire the instance KEK — it only exists after startup."""
        self._key_manager = key_manager
        self._cookie_secret = None

    @property
    def port_fixed(self) -> bool:
        """``True`` when the platform owns the dashboard port."""
        return self._fixed_port is not None

    # ── Settings ────────────────────────────────────────────────────────

    async def _load(self) -> DashboardSettings:
        """Stored settings as-is (the stored port, even when fixed).

        Reads only the three non-secret keys — never a hash, the cookie
        key or setup token — and only until the first load is cached.
        """
        cached = self._cached
        if cached is not None:
            return cached
        generation = self._generation
        settings = _settings_from_json(await self._repo.get(KEY_SETTINGS))
        settings = replace(
            settings,
            password_set_at=await self._repo.get(KEY_PASSWORD_SET_AT),
            pin_set=await self._repo.get(KEY_PIN_SET_AT) is not None,
        )
        if generation == self._generation:
            self._cached = settings
        return settings

    def _wrote(self, settings: DashboardSettings | None) -> None:
        """Record a write: cache ``settings`` (or drop the cache)."""
        self._generation += 1
        self._cached = settings

    async def snapshot(self) -> DashboardSettings:
        """The stored settings, to hand back to :meth:`restore` — unlike
        :meth:`get`, a platform-fixed port is not substituted, so a
        restore never writes it into storage."""
        return await self._load()

    async def get(self) -> DashboardSettings:
        """Current settings; a platform-fixed port always wins."""
        settings = await self._load()
        if self._fixed_port is not None:
            settings = replace(settings, port=self._fixed_port)
        return settings

    async def update(
        self,
        *,
        actor_is_admin: bool,
        patch: Mapping[str, object],
    ) -> DashboardSettings:
        """Validate and apply a partial settings ``patch``.

        Every key is checked strictly; the first bad one raises
        :class:`DashboardSettingsInvalidError` naming it (unknown keys
        are reported as ``unknown`` — the submitted key is never echoed)
        and nothing is written.
        """
        require_admin(actor_is_admin)
        if not isinstance(patch, Mapping):
            raise DashboardSettingsInvalidError("patch")
        async with self._lock:
            await self._update_unlocked(patch)
        return await self.get()

    async def _update_unlocked(self, patch: Mapping[str, object]) -> None:
        current = await self._load()
        changes: dict[str, object] = {}
        for key, value in patch.items():
            if key == "people":
                try:
                    merged = _merge_people(value, current.people_map())
                except _Invalid:
                    raise DashboardSettingsInvalidError("people") from None
                changes["people"] = tuple(merged.items())
                continue
            if key not in _SCALAR_FIELDS:
                raise DashboardSettingsInvalidError("unknown")
            try:
                value = _check_scalar(key, value)
            except _Invalid:
                raise DashboardSettingsInvalidError(key) from None
            if key == "port":
                if self._fixed_port is not None:
                    if value != self._fixed_port:
                        raise DashboardPortFixedError()
                    continue
                if value == self._main_port:
                    raise DashboardSettingsInvalidError("port")
            changes[key] = value
        if changes.get("enabled") is True and not await self._has_password():
            raise DashboardPasswordRequiredError()
        updated = replace(current, **changes)  # type: ignore[arg-type]
        if (
            updated.enabled
            and self._fixed_port is None
            and updated.port == self._main_port
        ):
            # Checked on the merged state: enabling with a stored (or
            # default) port that clashes with the main app is refused too.
            raise DashboardSettingsInvalidError("port")
        self._wrote(None)
        await self._repo.set(KEY_SETTINGS, _settings_to_json(updated))
        self._wrote(updated)

    async def restore(
        self, *, actor_is_admin: bool, previous: DashboardSettings
    ) -> None:
        """Put ``previous`` (from :meth:`snapshot`) back as the stored
        settings — undoes a change the dashboard listener couldn't follow
        (port taken). The password timestamp is not part of it."""
        require_admin(actor_is_admin)
        async with self._lock:
            self._wrote(None)
            await self._repo.set(KEY_SETTINGS, _settings_to_json(previous))
            self._wrote(None)

    # ── Password ────────────────────────────────────────────────────────

    async def _has_password(self) -> bool:
        return await self._repo.get(KEY_PASSWORD_HASH) is not None

    async def set_password(self, *, actor_is_admin: bool, password: str) -> None:
        """Set the dashboard password. Signs out every tablet."""
        require_admin(actor_is_admin)
        if not isinstance(password, str):
            raise DashboardSettingsInvalidError("password")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise DashboardPasswordTooShortError()
        hashed = await asyncio.to_thread(hash_password, password)
        async with self._lock:
            # Hash, timestamp and new epoch land together — a crash can't
            # leave a new password with old tablets still signed in.
            self._wrote(None)
            await self._repo.set_many(
                {
                    KEY_PASSWORD_HASH: hashed,
                    KEY_PASSWORD_SET_AT: datetime.now(timezone.utc).isoformat(),
                    KEY_EPOCH: str(await self._epoch_unlocked() + 1),
                    KEY_DISPLAYS: _NO_DISPLAYS,
                }
            )
            self._displays = ()
            self._wrote(None)

    async def verify_password(self, password: str) -> bool:
        if not isinstance(password, str):
            return False
        stored = await self._repo.get(KEY_PASSWORD_HASH)
        if stored is None:
            return False
        return await asyncio.to_thread(verify_password, password, stored)

    # ── Privacy-lock PIN ────────────────────────────────────────────────

    async def set_pin(self, *, actor_is_admin: bool, pin: str) -> None:
        """Set (or change) the PIN that unlocks a privacy-locked tablet.

        Exactly 4–6 ASCII digits, else :class:`DashboardPinInvalidError`.
        Separate from the household password and signs nobody out.
        """
        require_admin(actor_is_admin)
        if not isinstance(pin, str) or _PIN.fullmatch(pin) is None:
            raise DashboardPinInvalidError()
        hashed = await asyncio.to_thread(hash_password, pin)
        async with self._lock:
            self._wrote(None)
            await self._repo.set_many(
                {
                    KEY_PIN_HASH: hashed,
                    KEY_PIN_SET_AT: datetime.now(timezone.utc).isoformat(),
                }
            )
            self._wrote(None)

    async def delete_pin(self, *, actor_is_admin: bool) -> None:
        """Remove the PIN — locked tablets then count as unlocked.

        The marker goes first: a crash in between leaves "no PIN" (what
        was asked for), never "PIN set" with no hash able to unlock.
        """
        require_admin(actor_is_admin)
        async with self._lock:
            self._wrote(None)
            await self._repo.delete(KEY_PIN_SET_AT)
            await self._repo.delete(KEY_PIN_HASH)
            self._wrote(None)

    async def verify_pin(self, pin: str) -> bool:
        """``True`` when ``pin`` matches the stored PIN (constant-time
        scrypt compare, off the event loop). ``False`` when none is set."""
        if not isinstance(pin, str) or _PIN.fullmatch(pin) is None:
            return False
        stored = await self._repo.get(KEY_PIN_HASH)
        if stored is None:
            return False
        return await asyncio.to_thread(verify_password, pin, stored)

    # ── Sessions ────────────────────────────────────────────────────────

    async def epoch(self) -> int:
        """Current session epoch (``0`` before the first bump)."""
        async with self._lock:
            return await self._epoch_unlocked()

    async def _epoch_unlocked(self) -> int:
        raw = await self._repo.get(KEY_EPOCH)
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError:
            # Never fall back to 0: cookies minted at small epochs would
            # validate again. Reseed far above any epoch reached by bumps.
            fresh = (1 << 61) | secrets.randbits(61)
            log.warning("stored dashboard epoch is corrupt — reseeding")
            await self._repo.set(KEY_EPOCH, str(fresh))
            return fresh

    async def revoke_sessions(self, *, actor_is_admin: bool) -> None:
        """Sign out every tablet and unpair every wall panel (one write)."""
        require_admin(actor_is_admin)
        async with self._lock:
            await self._repo.set_many(
                {
                    KEY_EPOCH: str(await self._epoch_unlocked() + 1),
                    KEY_DISPLAYS: _NO_DISPLAYS,
                }
            )
            self._displays = ()

    async def cookie_secret(self) -> bytes:
        """The tablet-cookie signing key, created on first use.

        Stored KEK-wrapped. Raises :class:`RuntimeError` before
        :meth:`attach_key_manager` — the key is never kept in plaintext.
        A value that won't unwrap (other KEK, corrupt) is replaced, which
        signs every tablet out.
        """
        cached = self._cookie_secret
        if cached is not None:
            return cached
        km = self._key_manager
        if km is None:
            raise RuntimeError("dashboard cookie secret needs the instance KEK")
        async with self._lock:
            if self._cookie_secret is not None:
                return self._cookie_secret
            stored = await self._repo.get(KEY_COOKIE_SECRET)
            if stored is not None:
                try:
                    secret = km.decrypt(stored, associated_data=_COOKIE_SECRET_AAD)
                except KeyManagerError:
                    secret = b""
                if len(secret) == _COOKIE_SECRET_BYTES:
                    self._cookie_secret = secret
                    return secret
                log.warning("stored dashboard cookie secret is invalid — replacing")
            secret = secrets.token_bytes(_COOKIE_SECRET_BYTES)
            await self._repo.set(
                KEY_COOKIE_SECRET,
                km.encrypt(secret, associated_data=_COOKIE_SECRET_AAD),
            )
            self._cookie_secret = secret
            return secret

    # ── Setup token ─────────────────────────────────────────────────────

    async def issue_setup_token(
        self,
        *,
        actor_is_admin: bool,
        now: float | None = None,
    ) -> str:
        """Mint a single-use tablet setup token, replacing any pending one."""
        require_admin(actor_is_admin)
        if not await self._has_password():
            raise DashboardPasswordRequiredError()
        issued_at = time.time() if now is None else now
        raw = secrets.token_urlsafe(32)
        record = {
            "hash": _token_hash(raw),
            "expires_at": issued_at + SETUP_TOKEN_TTL_SECONDS,
        }
        async with self._lock:
            await self._repo.set(KEY_SETUP_TOKEN, json.dumps(record))
        return raw

    async def consume_setup_token(self, raw: str, *, now: float | None = None) -> bool:
        """``True`` exactly once for the pending, unexpired token."""
        if not isinstance(raw, str):
            return False
        at = time.time() if now is None else now
        async with self._lock:
            stored = await self._repo.get(KEY_SETUP_TOKEN)
            if stored is None:
                return False
            try:
                record = json.loads(stored)
                expected = record["hash"]
                expires_at = float(record["expires_at"])
                if not isinstance(expected, str):
                    raise TypeError
            except ValueError, TypeError, KeyError:
                log.warning("stored dashboard setup token is corrupt — discarded")
                await self._repo.delete(KEY_SETUP_TOKEN)
                return False
            if at >= expires_at:
                await self._repo.delete(KEY_SETUP_TOKEN)
                return False
            if not hmac.compare_digest(
                _token_hash(raw).encode("ascii"), expected.encode("utf-8")
            ):
                return False
            await self._repo.delete(KEY_SETUP_TOKEN)
            return True

    # ── Wall panels (displays) ──────────────────────────────────────────
    #
    # Storage only, unguarded: the admin gate and the pairing rules live
    # in :class:`~socialhome.services.dashboard_display_service.DashboardDisplayService`,
    # the one caller. Under ``_lock`` with the epoch, so an epoch bump's
    # wipe can't be undone by a write that read the list before it.

    async def _displays_unlocked(self) -> tuple[DashboardDisplay, ...]:
        if self._displays is None:
            self._displays = _displays_from_json(await self._repo.get(KEY_DISPLAYS))
        return self._displays

    async def _save_displays_unlocked(
        self, displays: tuple[DashboardDisplay, ...]
    ) -> None:
        self._displays = None
        await self._repo.set(KEY_DISPLAYS, _displays_to_json(displays))
        self._displays = displays

    async def displays(self) -> tuple[DashboardDisplay, ...]:
        """Every paired wall panel, oldest first."""
        async with self._lock:
            return await self._displays_unlocked()

    async def add_display(self, display: DashboardDisplay) -> None:
        """Store a newly paired panel; :class:`DashboardDisplayLimitError`
        at :data:`MAX_DISPLAYS`."""
        async with self._lock:
            current = await self._displays_unlocked()
            if len(current) >= MAX_DISPLAYS:
                raise DashboardDisplayLimitError()
            await self._save_displays_unlocked((*current, display))

    async def _replace_display_unlocked(
        self, display_id: str, change: Mapping[str, object]
    ) -> DashboardDisplay | None:
        current = await self._displays_unlocked()
        for i, d in enumerate(current):
            if d.id == display_id:
                updated = replace(d, **change)  # type: ignore[arg-type]
                await self._save_displays_unlocked(
                    (*current[:i], updated, *current[i + 1 :])
                )
                return updated
        return None

    async def rename_display(self, display_id: str, name: str) -> DashboardDisplay:
        async with self._lock:
            updated = await self._replace_display_unlocked(display_id, {"name": name})
        if updated is None:
            raise DashboardDisplayNotFoundError()
        return updated

    async def remove_display(self, display_id: str) -> None:
        async with self._lock:
            current = await self._displays_unlocked()
            kept = tuple(d for d in current if d.id != display_id)
            if len(kept) == len(current):
                raise DashboardDisplayNotFoundError()
            await self._save_displays_unlocked(kept)

    async def record_display_seen(
        self,
        display_id: str,
        *,
        at: str,
        firmware: str | None,
        model: str | None,
    ) -> None:
        """Note a panel's request: ``last_seen_at`` and, when the panel
        sent them, its firmware / model. A removed panel is ignored."""
        change: dict[str, object] = {"last_seen_at": at}
        if firmware is not None:
            change["firmware"] = firmware
        if model is not None:
            change["model"] = model
        async with self._lock:
            await self._replace_display_unlocked(display_id, change)
