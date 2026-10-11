"""Household dashboard settings, password, sessions and setup tokens.

Everything lives in the ``instance_config`` key/value table under the
``dashboard.`` prefix:

* ``dashboard.settings`` — JSON of the non-secret settings (incl. people);
* ``dashboard.password_hash`` / ``dashboard.password_set_at`` — the
  scrypt hash of the household dashboard password and when it was set;
* ``dashboard.cookie_secret`` — hex of the 32-byte tablet-cookie key;
* ``dashboard.epoch`` — session epoch; bumping it signs out every tablet;
* ``dashboard.setup_token`` — JSON ``{"hash", "expires_at"}`` of the one
  pending single-use tablet setup token (only the SHA-256 is stored).

Admin-only methods take ``actor_is_admin`` and raise
:class:`PermissionError` when it is ``False``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone

from ..domain.household_dashboard import (
    IDLE_RETURN_CHOICES,
    MIN_PASSWORD_LENGTH,
    SETUP_TOKEN_TTL_SECONDS,
    SUPPORTED_DASHBOARD_LOCALES,
    DashboardPasswordRequiredError,
    DashboardPasswordTooShortError,
    DashboardPersonSettings,
    DashboardPortFixedError,
    DashboardSettings,
    DashboardSettingsInvalidError,
)
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

_COOKIE_SECRET_BYTES = 32
_MIN_PORT = 1024
_MAX_PORT = 65535
_HHMM = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_BOOL_FIELDS = frozenset({"enabled", "keep_screen_on", "show_status_text"})
_PERSON_KEYS = frozenset({"show", "picture"})


class _Invalid(Exception):
    """Internal: a value failed validation (field named by the caller)."""


def _require_admin(actor_is_admin: bool) -> None:
    if not actor_is_admin:
        raise PermissionError("Only household admins may manage the dashboard")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


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


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DashboardSettingsService:
    """Admin-managed household dashboard configuration and credentials."""

    __slots__ = ("_repo", "_fixed_port", "_main_port", "_lock")

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
        # Serialises read-modify-write on the cookie secret and setup token
        # so concurrent callers can't mint two secrets or spend one token
        # twice.
        self._lock = asyncio.Lock()

    @property
    def port_fixed(self) -> bool:
        """``True`` when the platform owns the dashboard port."""
        return self._fixed_port is not None

    # ── Settings ────────────────────────────────────────────────────────

    async def _load(self) -> DashboardSettings:
        """Stored settings as-is (the stored port, even when fixed)."""
        rows = await self._repo.get_many(KEY_PREFIX)
        settings = _settings_from_json(rows.get(KEY_SETTINGS))
        return replace(settings, password_set_at=rows.get(KEY_PASSWORD_SET_AT))

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
        _require_admin(actor_is_admin)
        if not isinstance(patch, Mapping):
            raise DashboardSettingsInvalidError("patch")
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
                _check_scalar(key, value)
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
        await self._repo.set(KEY_SETTINGS, _settings_to_json(updated))
        return await self.get()

    # ── Password ────────────────────────────────────────────────────────

    async def _has_password(self) -> bool:
        return await self._repo.get(KEY_PASSWORD_HASH) is not None

    async def set_password(self, *, actor_is_admin: bool, password: str) -> None:
        """Set the dashboard password. Signs out every tablet."""
        _require_admin(actor_is_admin)
        if not isinstance(password, str):
            raise DashboardSettingsInvalidError("password")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise DashboardPasswordTooShortError()
        hashed = await asyncio.to_thread(hash_password, password)
        await self._repo.set(KEY_PASSWORD_HASH, hashed)
        await self._repo.set(
            KEY_PASSWORD_SET_AT, datetime.now(timezone.utc).isoformat()
        )
        await self._bump_epoch()

    async def verify_password(self, password: str) -> bool:
        if not isinstance(password, str):
            return False
        stored = await self._repo.get(KEY_PASSWORD_HASH)
        if stored is None:
            return False
        return await asyncio.to_thread(verify_password, password, stored)

    # ── Sessions ────────────────────────────────────────────────────────

    async def epoch(self) -> int:
        raw = await self._repo.get(KEY_EPOCH)
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError:
            log.warning("stored dashboard epoch is not an integer — treating as 0")
            return 0

    async def _bump_epoch(self) -> None:
        await self._repo.set(KEY_EPOCH, str(await self.epoch() + 1))

    async def revoke_sessions(self, *, actor_is_admin: bool) -> None:
        """Sign out every tablet."""
        _require_admin(actor_is_admin)
        await self._bump_epoch()

    async def cookie_secret(self) -> bytes:
        """The tablet-cookie signing key, created on first use."""
        async with self._lock:
            stored = await self._repo.get(KEY_COOKIE_SECRET)
            if stored is not None:
                try:
                    secret = bytes.fromhex(stored)
                except ValueError:
                    secret = b""
                if len(secret) == _COOKIE_SECRET_BYTES:
                    return secret
                log.warning("stored dashboard cookie secret is invalid — replacing")
            secret = secrets.token_bytes(_COOKIE_SECRET_BYTES)
            await self._repo.set(KEY_COOKIE_SECRET, secret.hex())
            return secret

    # ── Setup token ─────────────────────────────────────────────────────

    async def issue_setup_token(
        self,
        *,
        actor_is_admin: bool,
        now: float | None = None,
    ) -> str:
        """Mint a single-use tablet setup token, replacing any pending one."""
        _require_admin(actor_is_admin)
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
