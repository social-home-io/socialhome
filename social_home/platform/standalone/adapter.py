"""Standalone platform adapter (§platform/standalone).

Authenticates requests using SHA-256-hashed bearer tokens stored in
``platform_tokens``. Users, tokens, and instance configuration are managed
entirely within the local SQLite database — no external calls are made.

Audio transcription and AI data generation raise
:class:`NotImplementedError` in v1.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, AsyncIterable, Mapping

import aiohttp

from ... import app_keys as K
from ..adapter import ExternalUser, InstanceConfig, _extract_bearer

if TYPE_CHECKING:
    from aiohttp import web

    from ...config import Config
    from ...db import AsyncDatabase

log = logging.getLogger(__name__)


def _sha256(token: str) -> str:
    """Return the hex SHA-256 digest of the raw token string."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class StandaloneAdapter:
    """Platform adapter backed entirely by the local SQLite database.

    :param db: Open :class:`~social_home.db.AsyncDatabase` instance.
    :param config: Runtime :class:`~social_home.config.Config`.
    :param options: Raw ``[standalone]`` TOML section. Reserved for
        future per-adapter settings; unused in v1.
    """

    __slots__ = ("_db", "_config", "_options", "_session")

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

    # ── Authentication ────────────────────────────────────────────────────

    async def authenticate(self, request: "web.Request") -> ExternalUser | None:
        """Extract a bearer token from the request and delegate to :meth:`authenticate_bearer`."""
        token = _extract_bearer(request)
        if not token:
            return None
        return await self.authenticate_bearer(token)

    async def authenticate_bearer(self, token: str) -> ExternalUser | None:
        """Validate ``token`` by SHA-256 hashing and looking it up in ``platform_tokens``.

        Joins ``platform_tokens`` with ``platform_users`` so a single query
        returns the full user record. Tokens past their ``expires_at`` (when
        set) are rejected.
        """
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

        # Check expiry (stored as ISO-8601 UTC text, nullable).
        if row["expires_at"] is not None:
            try:
                expires = datetime.fromisoformat(row["expires_at"])
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires:
                    return None
            except ValueError:
                # Unparseable expiry — treat as expired.
                return None

        return ExternalUser(
            username=row["username"],
            display_name=row["display_name"],
            picture_url=row["picture_url"],
            is_admin=bool(row["is_admin"]),
            email=row["email"],
        )

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
        only as SHA-256 hashes in ``platform_tokens``.
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
        await self._db.enqueue(
            "INSERT INTO platform_tokens(token_id, username, token_hash) VALUES(?,?,?)",
            (token_id, username, _sha256(raw)),
        )
        # ``label`` is accepted for forward-compatibility but ignored
        # until the platform_tokens table gains a label column.
        _ = label
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

    async def list_external_users(self) -> list[ExternalUser]:
        """Return all rows from ``platform_users``."""
        rows = await self._db.fetchall("SELECT * FROM platform_users")
        return [self._row_to_user(r) for r in rows]

    async def get_external_user(self, username: str) -> ExternalUser | None:
        """Return the user with ``username`` from ``platform_users``, or ``None``."""
        row = await self._db.fetchone(
            "SELECT * FROM platform_users WHERE username = ?",
            (username,),
        )
        if row is None:
            return None
        return self._row_to_user(row)

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

    # ── Push notifications ────────────────────────────────────────────────

    async def send_push(
        self,
        user: ExternalUser,
        title: str,
        message: str,
        data: dict | None = None,
    ) -> None:
        """POST push payload to ``platform_users.notify_endpoint``. No-op if absent.

        Best-effort — all errors are swallowed and logged at DEBUG level.
        """
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

    # ── Lifecycle hooks ────────────────────────────────────────────────────

    async def on_startup(self, app: "web.Application") -> None:  # noqa: RUF029
        """Pick up the shared aiohttp session for ``send_push``."""
        if self._session is None:
            self._session = app[K.http_session_key]

    async def on_cleanup(self, app: "web.Application") -> None:  # noqa: RUF029
        """No-op — the shared session is owned by :mod:`social_home.app`."""

    def get_extra_services(self) -> dict:
        """Standalone provides no extra services."""
        return {}

    def get_extra_routes(self) -> list[tuple[str, type]]:
        """Standalone provides no extra routes."""
        return []

    @property
    def supports_bearer_token_auth(self) -> bool:
        """Standalone supports bearer-token authentication."""
        return True

    async def fire_event(self, event_type: str, data: dict) -> bool:
        """No-op — standalone has no external event bus."""
        return False

    # ── Not implemented in v1 ─────────────────────────────────────────────

    @property
    def supports_stt(self) -> bool:
        """Standalone has no first-party STT backend in v1."""
        return False

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        language: str = "en",
    ) -> str:
        raise NotImplementedError(
            "StandaloneAdapter does not support audio transcription in v1"
        )

    async def stream_transcribe_audio(
        self,
        audio_stream: AsyncIterable[bytes],
        *,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> str:
        raise NotImplementedError(
            "StandaloneAdapter does not support audio transcription in v1"
        )

    async def generate_ai_data(
        self,
        *,
        task_name: str,
        instructions: str,
    ) -> str:
        raise NotImplementedError(
            "StandaloneAdapter does not support AI data generation in v1"
        )

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

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_user(row) -> ExternalUser:
        """Convert a ``platform_users`` row to an :class:`ExternalUser`."""
        return ExternalUser(
            username=row["username"],
            display_name=row["display_name"],
            picture_url=row["picture_url"],
            is_admin=bool(row["is_admin"]),
            email=row["email"],
        )
