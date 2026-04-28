"""Standalone platform adapter (§platform/standalone).

Authenticates requests using SHA-256-hashed bearer tokens stored in
``platform_tokens``. Users, tokens, and instance configuration are managed
entirely within the local SQLite database — no external calls are made.

Audio transcription and AI data generation are not supported (the
adapter exposes ``stt = None`` / ``ai = None`` and the base
:class:`PlatformAdapter` raises :class:`NotImplementedError`).

This module composes mode-specific Provider classes
(:class:`StandaloneAuthProvider`, :class:`StandaloneUserDirectory`,
:class:`StandalonePushProvider`) into a :class:`StandaloneAdapter`
that satisfies the :class:`PlatformAdapter` ABC. See
``socialhome/platform/adapter.py`` for the design pattern overview.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

import aiohttp

from ... import app_keys as K
from ..adapter import (
    Capability,
    ExternalUser,
    InstanceConfig,
    NoopEventSink,
    PlatformAdapter,
    _extract_bearer,
)

if TYPE_CHECKING:
    from aiohttp import web

    from ...config import Config
    from ...db import AsyncDatabase

log = logging.getLogger(__name__)


def _sha256(token: str) -> str:
    """Return the hex SHA-256 digest of the raw token string."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Providers ────────────────────────────────────────────────────────────────


class StandaloneAuthProvider:
    """Resolve a request via ``Authorization: Bearer`` against
    ``platform_tokens`` joined to ``platform_users``."""

    __slots__ = ("_db",)

    def __init__(self, db: "AsyncDatabase") -> None:
        self._db = db

    async def authenticate(
        self,
        request: "web.Request",
    ) -> ExternalUser | None:
        token = _extract_bearer(request)
        if not token:
            return None
        return await self._authenticate_bearer(token)

    async def _authenticate_bearer(self, token: str) -> ExternalUser | None:
        token_hash = _sha256(token)
        row = await self._db.fetchone(
            """
            SELECT
                u.username,
                u.display_name,
                u.picture_url,
                u.is_admin,
                u.email,
                t.expires_at
            FROM platform_tokens t
            JOIN platform_users u ON u.username = t.username
            WHERE t.token_hash = ?
            """,
            (token_hash,),
        )
        if row is None:
            return None
        if row["expires_at"] is not None:
            try:
                expires = datetime.fromisoformat(row["expires_at"])
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires:
                    return None
            except ValueError:
                return None
        return ExternalUser(
            username=row["username"],
            display_name=row["display_name"],
            picture_url=row["picture_url"],
            is_admin=bool(row["is_admin"]),
            email=row["email"],
        )


class StandaloneUserDirectory:
    """List / get / enable / disable principals stored in ``platform_users``."""

    __slots__ = ("_db",)

    def __init__(self, db: "AsyncDatabase") -> None:
        self._db = db

    async def list_users(self) -> list[ExternalUser]:
        rows = await self._db.fetchall("SELECT * FROM platform_users")
        return [_row_to_user(r) for r in rows]

    async def get(self, username: str) -> ExternalUser | None:
        row = await self._db.fetchone(
            "SELECT * FROM platform_users WHERE username = ?",
            (username,),
        )
        return _row_to_user(row) if row else None

    async def is_enabled(self, username: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM platform_users WHERE username=?",
            (username,),
        )
        return row is not None

    async def enable(
        self,
        username: str,
        *,
        password: str | None = None,
    ) -> ExternalUser:
        """Create (or re-activate) ``username``. Standalone requires a
        password — caller validates against the capability set."""
        if not password:
            raise ValueError(
                "standalone mode requires a password when enabling a user",
            )
        pw_hash = StandaloneAdapter.hash_password(password)
        await self._db.enqueue(
            """
            INSERT INTO platform_users(
                username, display_name, is_admin, password_hash
            ) VALUES(?, ?, 0, ?)
            ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash
            """,
            (username, username, pw_hash),
        )
        existing = await self.get(username)
        assert existing is not None
        return existing

    async def disable(self, username: str) -> None:
        await self._db.enqueue(
            "DELETE FROM platform_users WHERE username=?",
            (username,),
        )


class StandalonePushProvider:
    """POST a payload to ``platform_users.notify_endpoint``. Best-effort."""

    __slots__ = ("_db", "_session")

    def __init__(
        self,
        db: "AsyncDatabase",
        session: aiohttp.ClientSession | None,
    ) -> None:
        self._db = db
        self._session = session

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def send(
        self,
        user: ExternalUser,
        title: str,
        message: str,
        data: dict | None = None,
    ) -> None:
        row = await self._db.fetchone(
            "SELECT notify_endpoint FROM platform_users WHERE username = ?",
            (user.username,),
        )
        if row is None or not row["notify_endpoint"]:
            return
        endpoint: str = row["notify_endpoint"]
        payload: dict = {"title": title, "message": message}
        if data:
            payload["data"] = data
        session = self._session
        if session is None:
            log.debug(
                "standalone: send_push to %r skipped — no shared HTTP session wired",
                user.username,
            )
            return
        try:
            async with session.post(endpoint, json=payload) as resp:
                if resp.status not in (200, 201, 204):
                    log.debug(
                        "standalone: send_push to %r returned %d",
                        user.username,
                        resp.status,
                    )
        except aiohttp.ClientError as exc:
            log.debug(
                "standalone: send_push to %r failed: %s",
                user.username,
                exc,
            )


def _row_to_user(row: Mapping[str, Any]) -> ExternalUser:
    """Convert a ``platform_users`` row to an :class:`ExternalUser`."""
    return ExternalUser(
        username=row["username"],
        display_name=row["display_name"],
        picture_url=row["picture_url"],
        is_admin=bool(row["is_admin"]),
        email=row["email"],
    )


# ── Adapter ──────────────────────────────────────────────────────────────────


class StandaloneAdapter(PlatformAdapter):
    """Platform adapter backed entirely by the local SQLite database.

    :param db: Open :class:`~socialhome.db.AsyncDatabase` instance.
    :param config: Runtime :class:`~socialhome.config.Config`.
    :param options: Raw ``[standalone]`` TOML section.
    """

    __slots__ = (
        "_db",
        "_config",
        "_options",
        "_session",
        "auth",
        "users",
        "push",
        "stt",
        "ai",
        "events",
    )

    def __init__(
        self,
        db: "AsyncDatabase",
        config: "Config",
        options: Mapping[str, Any] | None = None,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._options: Mapping[str, Any] = options or MappingProxyType({})
        self._session: aiohttp.ClientSession | None = session

        # Compose providers — each owns one slice of behaviour.
        self.auth = StandaloneAuthProvider(db)
        self.users = StandaloneUserDirectory(db)
        self.push = StandalonePushProvider(db, session)
        self.stt = None  # standalone has no STT backend in v1
        self.ai = None  # standalone has no AI backend in v1
        self.events = NoopEventSink()

    @property
    def capabilities(self) -> frozenset[Capability]:
        # Standalone supports password auth + push (when notify_endpoint
        # is configured per-user). No ingress, no STT, no AI, no
        # HA-person directory.
        return frozenset({Capability.PASSWORD_AUTH, Capability.PUSH})

    # ── Authentication ────────────────────────────────────────────────────

    async def authenticate_bearer(self, token: str) -> ExternalUser | None:
        """Validate ``token`` by SHA-256 hashing it against ``platform_tokens``.

        Public method retained on the adapter so existing test fixtures and
        tools that drive the bearer flow without going through an HTTP
        request can still call it. Internally delegates to the
        :class:`StandaloneAuthProvider`."""
        return await self.auth._authenticate_bearer(token)

    # ── Password-based token issuance (§auth/token) ───────────────────────

    async def issue_bearer_token(
        self,
        username: str,
        password: str,
        *,
        label: str = "web",
    ) -> str | None:
        """Verify credentials and mint a fresh bearer token (§POST /api/auth/token).

        Returns the raw token string the client must present as
        ``Authorization: Bearer <token>`` on subsequent requests, or
        ``None`` if the credentials are invalid. Tokens are stored
        only as SHA-256 hashes — once in ``platform_tokens`` (the
        platform-layer session log) and once in ``api_tokens`` keyed on
        the matching ``users`` row so the application's
        :class:`BearerTokenStrategy` can resolve them. Without the
        ``api_tokens`` mirror, ``GET /api/me`` would 401 immediately
        after a successful login — the standalone session token would
        never reach the auth middleware.
        """
        row = await self._db.fetchone(
            "SELECT password_hash FROM platform_users WHERE username=?",
            (username,),
        )
        if row is None or not row["password_hash"]:
            return None
        stored: str = row["password_hash"]
        if not self._verify_password(password, stored):
            return None

        raw = secrets.token_urlsafe(32)
        token_id = secrets.token_urlsafe(16)
        token_hash = _sha256(raw)
        await self._db.enqueue(
            "INSERT INTO platform_tokens(token_id, username, token_hash) VALUES(?,?,?)",
            (token_id, username, token_hash),
        )
        # Mirror into ``api_tokens`` so the application-layer bearer
        # strategy (which joins users → api_tokens) accepts this token.
        # Skips silently when the matching ``users`` row is absent —
        # that's a deployment misconfiguration rather than a runtime
        # error and the platform_tokens row is still useful for audit.
        user_row = await self._db.fetchone(
            "SELECT user_id FROM users WHERE username=?",
            (username,),
        )
        if user_row is not None and user_row["user_id"]:
            await self._db.enqueue(
                """
                INSERT INTO api_tokens(token_id, user_id, label, token_hash)
                VALUES(?, ?, ?, ?)
                """,
                (token_id, user_row["user_id"], label, token_hash),
            )
        return raw

    @staticmethod
    def hash_password(password: str, *, salt: bytes | None = None) -> str:
        """Return a scrypt hash in ``scrypt$<N>$<r>$<p>$<salt_hex>$<hash_hex>`` form.

        Stdlib-only — ``hashlib.scrypt`` is available on every Python 3.14
        build we target. Parameters are chosen to be secure for a household
        server (N=2^15, r=8, p=1 — ~100ms on commodity hardware).
        """
        # N*r*128 stays ≤16 MB so we sit comfortably under OpenSSL's
        # default EVP memory limit (~32 MB). ~50 ms on commodity hardware.
        n = 2**14
        r = 8
        p = 1
        if salt is None:
            salt = os.urandom(16)
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32
        )
        return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"

    @staticmethod
    def _verify_password(password: str, stored: str) -> bool:
        if not stored.startswith("scrypt$"):
            return False
        try:
            _, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$", 5)
            n, r, p = int(n_s), int(r_s), int(p_s)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
            dk = hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt,
                n=n,
                r=r,
                p=p,
                dklen=len(expected),
            )
            return hmac.compare_digest(dk, expected)
        except ValueError, KeyError:
            return False

    # ── User listing ──────────────────────────────────────────────────────
    # ``list_external_users`` / ``get_external_user`` come from the
    # :class:`PlatformAdapter` ABC and delegate to ``self.users``.

    # ── Instance config ───────────────────────────────────────────────────

    async def get_instance_config(self) -> InstanceConfig:
        """Read location from ``instance_identity``, with fallback to config defaults."""
        row = await self._db.fetchone(
            "SELECT home_lat, home_lon, home_label FROM instance_identity WHERE id='self'",
        )

        if row and row["home_lat"] is not None and row["home_lon"] is not None:
            lat = float(row["home_lat"])
            lon = float(row["home_lon"])
            label = row["home_label"] or self._config.instance_name
        else:
            lat = 0.0
            lon = 0.0
            label = self._config.instance_name

        return InstanceConfig(
            location_name=label,
            latitude=lat,
            longitude=lon,
            time_zone="UTC",
            currency="USD",
        )

    # ── Federation inbox base URL (§11) ───────────────────────────────────

    async def get_federation_base(self) -> str | None:
        """Return ``[standalone].external_url`` + ``/federation/inbox``.

        ``[standalone].external_url`` is the publicly-reachable base the
        admin has configured — "https://social.example.com". We append
        the inbox path so the coordinator can build per-peer URLs by
        concatenating the peer's ``local_inbox_id``.

        Returns ``None`` when the option is unset; the pairing route
        converts that to a 422 ``NOT_CONFIGURED`` so the admin knows to
        set the URL before issuing a QR.
        """
        raw = self._options.get("external_url") if self._options else None
        if not raw:
            return None
        base = str(raw).rstrip("/")
        if not base:
            return None
        return f"{base}/federation/inbox"

    # ── Push notifications ────────────────────────────────────────────────
    # ``send_push`` comes from the :class:`PlatformAdapter` ABC and
    # delegates to ``self.push`` (StandalonePushProvider).

    # ── Lifecycle hooks ────────────────────────────────────────────────────

    async def on_startup(self, app: "web.Application") -> None:
        """Standalone-mode startup wiring.

        Picks up the shared aiohttp session and forwards it to
        :class:`StandalonePushProvider` for outbound push.

        First-boot admin provisioning lives in :meth:`provision_admin`,
        which is called from the ``POST /api/setup/standalone`` route.
        When ``[standalone].admin_password`` (or ``SH_ADMIN_PASSWORD``)
        is set, the same code path runs here so headless deployments
        come up with a usable login without a wizard click-through.
        Without that override the wizard is the only path — fresh DBs
        boot with no admin and the SPA redirects to ``/setup``.
        """
        if self._session is None:
            self._session = app[K.http_session_key]
        if self.push is not None and isinstance(self.push, StandalonePushProvider):
            self.push.attach_session(self._session)
        if self._config.admin_password:
            await self.provision_admin(
                username=self._config.admin_username,
                password=self._config.admin_password,
            )

    async def provision_admin(
        self,
        *,
        username: str,
        password: str,
        display_name: str = "Admin",
    ) -> bool:
        """Seed the first admin in ``platform_users`` + ``users``.

        Idempotent: returns ``False`` when an admin already exists
        (either platform-side or domain-side). Returns ``True`` on
        first-boot success. Used by both the headless env-var path
        (``on_startup``) and the ``POST /api/setup/standalone`` route.
        """
        username = (username or "admin").strip() or "admin"
        if not password:
            raise ValueError("provision_admin requires a non-empty password")

        existing = await self._db.fetchone(
            "SELECT 1 FROM platform_users LIMIT 1",
        )
        if existing is not None:
            return False
        existing_user = await self._db.fetchone(
            "SELECT 1 FROM users WHERE username=?",
            (username,),
        )
        if existing_user is not None:
            return False

        pw_hash = self.hash_password(password)
        await self._db.enqueue(
            """
            INSERT INTO platform_users(username, display_name, is_admin, password_hash)
            VALUES(?, ?, 1, ?)
            ON CONFLICT(username) DO NOTHING
            """,
            (username, display_name, pw_hash),
        )
        user_id = f"uid-{username}"
        await self._db.enqueue(
            """
            INSERT INTO users(username, user_id, display_name, is_admin)
            VALUES(?, ?, ?, 1)
            ON CONFLICT(username) DO UPDATE SET is_admin=1
            """,
            (username, user_id, display_name),
        )
        return True

    async def on_cleanup(self, app: "web.Application") -> None:  # noqa: RUF029
        """No-op — the shared session is owned by :mod:`socialhome.app`."""

    def get_extra_services(self) -> dict:
        """Standalone provides no extra services."""
        return {}

    def get_extra_routes(self) -> list[tuple[str, type]]:
        """Standalone provides no extra routes."""
        return []

    # ``supports_bearer_token_auth``, ``supports_stt``,
    # ``transcribe_audio`` / ``stream_transcribe_audio`` /
    # ``generate_ai_data``, ``fire_event`` all come from the
    # :class:`PlatformAdapter` ABC and route through ``capabilities`` /
    # ``self.stt`` / ``self.ai`` / ``self.events``.

    # ── Location override ─────────────────────────────────────────────────

    async def update_location(
        self,
        latitude: float,
        longitude: float,
        location_name: str,
    ) -> InstanceConfig:
        """Persist a location override to ``instance_identity`` and return updated config."""
        lat = round(float(latitude), 4)
        lon = round(float(longitude), 4)

        await self._db.enqueue(
            """
            UPDATE instance_identity
               SET home_lat = ?, home_lon = ?, home_label = ?
             WHERE id = 'self'
            """,
            (lat, lon, location_name),
        )

        return InstanceConfig(
            location_name=location_name,
            latitude=lat,
            longitude=lon,
            time_zone="UTC",
            currency="USD",
        )

    # ``_row_to_user`` was a private staticmethod used by the old
    # in-class user listing methods; that logic now lives in the
    # module-level ``_row_to_user`` helper consumed by
    # :class:`StandaloneUserDirectory`. Kept as a class attribute alias
    # for any external caller that imported it via the class.
    _row_to_user = staticmethod(_row_to_user)
