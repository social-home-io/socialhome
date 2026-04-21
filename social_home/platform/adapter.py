"""Platform abstraction layer — adapter interface + shared value types.

The application talks exclusively through :class:`AbstractPlatformAdapter`.
In HA mode (:mod:`.ha_adapter`) requests are authenticated via HA Ingress
headers or a bearer token validated against the HA REST API; user/instance
data is proxied from HA. In standalone mode (:mod:`.standalone`) users and
tokens are managed directly in the local SQLite database.

The concrete adapter is selected by
:func:`social_home.platform.build_platform_adapter` using the ``mode`` field
from :class:`~social_home.config.Config`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    AsyncIterable,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:
    from aiohttp import web

log = logging.getLogger(__name__)


# ── Value types ──────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class InstanceConfig:
    """Immutable snapshot of the instance's physical / regional settings."""

    location_name: str
    latitude: float
    longitude: float
    time_zone: str
    currency: str


@dataclass(slots=True, frozen=True)
class ExternalUser:
    """A user as seen by the platform layer.

    In HA mode this maps to a ``person.*`` entity; in standalone mode it maps
    to a row in ``platform_users``.
    """

    username: str
    display_name: str
    picture_url: str | None
    is_admin: bool
    email: str | None = None


# ── Protocol ─────────────────────────────────────────────────────────────────


@runtime_checkable
class AbstractPlatformAdapter(Protocol):
    """Platform-neutral interface used by all services and route handlers.

    Implementations must be safe to call concurrently — they are shared
    across all request handlers without synchronisation.
    """

    async def authenticate(self, request: "web.Request") -> ExternalUser | None:
        """Resolve an inbound HTTP request to an :class:`ExternalUser`.

        Returns ``None`` if the request cannot be authenticated.
        """
        ...

    async def authenticate_bearer(self, token: str) -> ExternalUser | None:
        """Validate a raw bearer token and return the owning user.

        Returns ``None`` for unknown, expired, or revoked tokens.
        """
        ...

    async def list_external_users(self) -> list[ExternalUser]:
        """Return all users known to the platform."""
        ...

    async def get_external_user(self, username: str) -> ExternalUser | None:
        """Look up a single user by username. Returns ``None`` if not found."""
        ...

    async def get_instance_config(self) -> InstanceConfig:
        """Return current instance location / regional configuration."""
        ...

    async def send_push(
        self,
        user: ExternalUser,
        title: str,
        message: str,
        data: dict | None = None,
    ) -> None:
        """Send a push notification to ``user``. Best-effort — never raises."""
        ...

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        language: str = "en",
    ) -> str:
        """Transcribe ``audio_bytes`` (buffered) to text.

        Convenience wrapper around :meth:`stream_transcribe_audio` for
        callers that already have the full audio in memory. Adapters
        without STT support raise :class:`NotImplementedError`.
        """
        ...

    async def stream_transcribe_audio(
        self,
        audio_stream: AsyncIterable[bytes],
        *,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> str:
        """Stream PCM16 little-endian audio to the platform's STT and return text.

        The audio is expected to be raw PCM16 little-endian at
        ``sample_rate`` Hz with ``channels`` channels (default 16 kHz mono,
        matching HA's Whisper defaults). Implementations forward the
        iterable directly to the upstream HTTP body so bytes never need
        to be buffered. Adapters without STT support raise
        :class:`NotImplementedError`.
        """
        ...

    @property
    def supports_stt(self) -> bool:
        """Whether this adapter can service :meth:`stream_transcribe_audio`.

        When ``False`` the UI hides the mic button and the WS route at
        ``/api/stt/stream`` closes with an error frame on connect.
        """
        ...

    async def generate_ai_data(
        self,
        *,
        task_name: str,
        instructions: str,
    ) -> str:
        """Run an AI task and return its generated ``data`` field as text.

        Mirrors the Home Assistant ``ai_task.generate_data`` action. The
        ``instructions`` string may embed inline attachments (e.g. image
        data URLs) because the REST service call does not accept raw
        binary payloads. Adapters without AI support raise
        :class:`NotImplementedError`.
        """
        ...

    async def update_location(
        self,
        latitude: float,
        longitude: float,
        location_name: str,
    ) -> InstanceConfig:
        """Persist a location override and return the updated config."""
        ...

    # ── Lifecycle hooks ──────────────────────────────────────────────────

    async def on_startup(self, app: "web.Application") -> None:
        """Called during ``on_startup`` — adapters wire platform-specific services."""
        ...

    async def on_cleanup(self, app: "web.Application") -> None:
        """Called during ``on_cleanup`` — adapters tear down platform-specific resources."""
        ...

    def get_extra_services(self) -> dict:
        """Return a dict of extra app-key → service pairs the adapter provides."""
        ...

    def get_extra_routes(self) -> list[tuple[str, type]]:
        """Return a list of ``(url_path, ViewClass)`` tuples for adapter-specific routes."""
        ...

    @property
    def supports_bearer_token_auth(self) -> bool:
        """Whether this adapter supports standalone bearer-token authentication."""
        ...

    async def fire_event(self, event_type: str, data: dict) -> bool:
        """Fire a platform event. Returns ``True`` on success, ``False`` otherwise."""
        ...


# Maps CLI ``mode`` string to the TOML section name. Kept explicit so the
# section key ("homeassistant") can differ from the mode ("ha") — the mode
# is the short operational label, the section is the user-facing TOML tag.
#: Used by :func:`social_home.platform.build_platform_adapter`.
_PLATFORM_SECTION: dict[str, str] = {
    "ha": "homeassistant",
    "standalone": "standalone",
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_bearer(request: "web.Request") -> str | None:
    """Extract a raw bearer token from a request.

    Checks, in order:
    1. ``Authorization: Bearer <token>`` header.
    2. ``?token=<token>`` query parameter (used by WebSocket clients that
       cannot set custom headers).

    Returns the raw token string or ``None``.
    """
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        bearer = header[len("Bearer ") :].strip()
        if bearer:
            return bearer

    query_token = request.query.get("token")
    if query_token:
        return query_token

    return None
