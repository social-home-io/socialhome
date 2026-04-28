"""Platform abstraction layer — adapter ABC + provider Protocols + value types.

The application talks exclusively through :class:`PlatformAdapter`. Three
concrete adapters live alongside this module:

* :class:`~socialhome.platform.standalone.StandaloneAdapter` — local SQLite
  user / token store, no external integration.
* :class:`~socialhome.platform.ha.HaAdapter` — Home Assistant Core /
  custom-integration mode (no Supervisor). Bearer-token auth against
  the HA REST API.
* :class:`~socialhome.platform.haos.HaosAdapter` — HA OS / Supervisor
  add-on. Auto-login through the Supervisor ingress proxy
  (``X-Ingress-User`` header).

The concrete adapter is selected by
:func:`socialhome.platform.build_platform_adapter` using ``Config.mode``.

## Design pattern: Adapter + Provider composition

The ABC :class:`PlatformAdapter` does not contain integration logic
itself — instead it composes small **Provider Protocols** for the
slices of behaviour that vary across modes:

* :class:`AuthProvider` — request → :class:`AuthContext` resolution.
* :class:`UserDirectory` — list / get / enable / disable principals.
* :class:`PushProvider` — Web Push / native push delivery.
* :class:`STTProvider` — speech-to-text (buffered + streaming).
* :class:`AIProvider` — text generation via an external service.
* :class:`ExternalEventSink` — fire-and-forget event publication.

Each adapter wires the right concrete provider in its constructor. The
adapter's high-level methods are thin pass-throughs. To add a new
platform you write a new adapter class plus the providers it needs;
route handlers and services consume the adapter through the Provider
interfaces — never via ``isinstance`` checks on the concrete adapter.

The :class:`Capability` set on each adapter drives the SPA's
``/api/instance/config`` payload so the UI can render mode-aware
affordances without re-implementing the mode branching everywhere.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from enum import StrEnum
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

    In ha / haos mode this maps to a ``person.*`` entity; in standalone
    mode it maps to a row in ``platform_users``.
    """

    username: str
    display_name: str
    picture_url: str | None
    is_admin: bool
    email: str | None = None


# ── Capability ───────────────────────────────────────────────────────────────


class Capability(StrEnum):
    """Mode-specific feature flags exposed to the SPA via
    ``GET /api/instance/config``.

    Adapters declare the union of capabilities they support; route
    handlers and the SPA consult the set instead of branching on
    ``config.mode`` directly.
    """

    #: Adapter has a working :class:`PushProvider`.
    PUSH = "push"
    #: Adapter has a working :class:`STTProvider`.
    STT = "stt"
    #: Adapter has a working :class:`AIProvider`.
    AI = "ai"
    #: Requests authenticate via an upstream ingress proxy
    #: (Supervisor on HAOS) — no password or token form needed.
    INGRESS = "ingress"
    #: Adapter accepts username/password via ``POST /api/auth/token``.
    PASSWORD_AUTH = "password_auth"
    #: User directory is the HA ``person.*`` registry rather than a
    #: locally-managed table; user-management UI shows enable/disable
    #: rather than create/delete.
    HA_PERSON_DIRECTORY = "ha_person_directory"


# ── Provider Protocols ───────────────────────────────────────────────────────


@runtime_checkable
class AuthProvider(Protocol):
    """Resolve an HTTP request to an :class:`ExternalUser`.

    Mode-specific implementations:

    * Standalone: SHA-256 lookup in ``platform_tokens`` / ``api_tokens``.
    * HA Core: ``X-Ingress-User`` header (when behind a proxy) +
      bearer-token validation against the HA REST API.
    * HAOS: ``X-Ingress-User`` (Supervisor-injected) + bearer fallback
      for token-API consumers.

    Returning ``None`` always means "401 unauthenticated" — never raise.
    """

    async def authenticate(
        self, request: "web.Request",
    ) -> ExternalUser | None: ...


@runtime_checkable
class UserDirectory(Protocol):
    """Enumerate + look up principals on this platform.

    Steady-state user management uses the same surface across modes;
    capabilities decide whether passwords are involved (standalone /
    ha) or skipped (haos — ingress handles auth).
    """

    async def list_users(self) -> list[ExternalUser]: ...

    async def get(self, username: str) -> ExternalUser | None: ...

    async def is_enabled(self, username: str) -> bool: ...

    async def enable(
        self,
        username: str,
        *,
        password: str | None = None,
    ) -> ExternalUser:
        """Provision (or re-activate) ``username`` as a Social Home user.

        Standalone + ha: ``password`` is required and stored as a
        scrypt hash. Haos: ``password`` MUST be ``None`` (ingress
        auto-logs the user in; storing a password would be a footgun).
        Implementations validate accordingly and raise :class:`ValueError`
        on mismatch.
        """
        ...

    async def disable(self, username: str) -> None: ...


@runtime_checkable
class PushProvider(Protocol):
    """Deliver a title-only push notification to ``user``.

    Best-effort — never raises. Per §25.3 we only put the title on
    the wire; the body is fetched in-app via the notification row.
    """

    async def send(
        self,
        user: ExternalUser,
        title: str,
        message: str,
        data: dict | None = None,
    ) -> None: ...


@runtime_checkable
class STTProvider(Protocol):
    """Speech-to-text: buffered + streaming.

    Streaming is the primary path; buffered wraps it for callers that
    have the full audio in memory. PCM16 little-endian at
    ``sample_rate`` Hz, ``channels`` channels (default 16 kHz mono —
    HA Whisper's defaults).
    """

    async def transcribe(
        self, audio: bytes, language: str = "en",
    ) -> str: ...

    async def stream_transcribe(
        self,
        stream: AsyncIterable[bytes],
        *,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> str: ...


@runtime_checkable
class AIProvider(Protocol):
    """Run an AI text-generation task and return the generated data.

    Mirrors HA's ``ai_task.generate_data`` action. Inline attachments
    (image data URLs) ride in ``instructions`` because REST service
    calls don't accept binary payloads.
    """

    async def generate_data(
        self,
        *,
        task_name: str,
        instructions: str,
    ) -> str: ...


@runtime_checkable
class ExternalEventSink(Protocol):
    """Fire-and-forget event publication to the host platform.

    HA / HAOS: ``POST /api/events/{event_type}`` so HA automations can
    listen. Standalone: returns ``False`` (no external bus).
    """

    async def fire(self, event_type: str, data: dict) -> bool: ...


# ── ABC ──────────────────────────────────────────────────────────────────────


class PlatformAdapter(abc.ABC):
    """Platform-neutral interface used by all services and route handlers.

    Concrete adapters compose :class:`AuthProvider`,
    :class:`UserDirectory`, optional :class:`PushProvider` /
    :class:`STTProvider` / :class:`AIProvider`, and an
    :class:`ExternalEventSink`. The high-level methods on this ABC
    are thin pass-throughs so the existing call sites continue to
    work unchanged.

    Implementations must be safe to call concurrently — they are
    shared across all request handlers without synchronisation.

    Add a new platform by writing a new adapter class + the providers
    it needs. Never branch on ``config.mode`` in service / route
    code; query :attr:`capabilities` instead.
    """

    # ── Composed providers (concrete adapters set these in __init__) ──

    auth: AuthProvider
    users: UserDirectory
    push: PushProvider | None
    stt: STTProvider | None
    ai: AIProvider | None
    events: ExternalEventSink

    @property
    @abc.abstractmethod
    def capabilities(self) -> frozenset[Capability]:
        """Capability set for ``GET /api/instance/config``.

        Determined at construction; immutable for the lifetime of the
        process.
        """
        ...

    # ── Adapter-level methods (mode-specific, not provider-shaped) ──

    @abc.abstractmethod
    async def get_instance_config(self) -> InstanceConfig:
        """Return current instance location / regional configuration."""
        ...

    @abc.abstractmethod
    async def get_federation_base(self) -> str | None:
        """Return the externally-reachable scheme+host+path prefix peers
        POST federation envelopes to, or ``None`` if unconfigured.

        The full per-peer URL is composed as
        ``f"{base}/{local_inbox_id}"``. ``None`` is a hard error for
        the pairing route (surfaced as 422 ``NOT_CONFIGURED``).
        """
        ...

    @abc.abstractmethod
    async def update_location(
        self,
        latitude: float,
        longitude: float,
        location_name: str,
    ) -> InstanceConfig:
        """Persist a location override and return the updated config."""
        ...

    @abc.abstractmethod
    async def on_startup(self, app: "web.Application") -> None:
        """Wire platform-specific services + run any first-boot bootstrap."""
        ...

    @abc.abstractmethod
    async def on_cleanup(self, app: "web.Application") -> None:
        """Tear down platform-specific resources."""
        ...

    def get_extra_services(self) -> dict:
        """Extra ``app[key] = service`` pairs the adapter provides.

        Default empty; HA-mode adapters override to wire HaBridgeService
        etc.
        """
        return {}

    def get_extra_routes(self) -> list[tuple[str, type]]:
        """Extra ``(url_path, ViewClass)`` tuples the adapter provides.

        Default empty.
        """
        return []

    # ── Pass-through delegations ─────────────────────────────────────

    async def authenticate(
        self, request: "web.Request",
    ) -> ExternalUser | None:
        """Delegate to :attr:`auth`. Kept on the adapter so existing
        call sites don't need to migrate to ``adapter.auth.authenticate``
        in the same change."""
        return await self.auth.authenticate(request)

    async def list_external_users(self) -> list[ExternalUser]:
        return await self.users.list_users()

    async def get_external_user(self, username: str) -> ExternalUser | None:
        return await self.users.get(username)

    async def send_push(
        self,
        user: ExternalUser,
        title: str,
        message: str,
        data: dict | None = None,
    ) -> None:
        if self.push is None:
            return
        await self.push.send(user, title, message, data)

    async def transcribe_audio(
        self, audio_bytes: bytes, language: str = "en",
    ) -> str:
        if self.stt is None:
            raise NotImplementedError(
                f"{type(self).__name__} does not support audio transcription",
            )
        return await self.stt.transcribe(audio_bytes, language)

    async def stream_transcribe_audio(
        self,
        audio_stream: AsyncIterable[bytes],
        *,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> str:
        if self.stt is None:
            raise NotImplementedError(
                f"{type(self).__name__} does not support audio transcription",
            )
        return await self.stt.stream_transcribe(
            audio_stream,
            language=language,
            sample_rate=sample_rate,
            channels=channels,
        )

    async def generate_ai_data(
        self,
        *,
        task_name: str,
        instructions: str,
    ) -> str:
        if self.ai is None:
            raise NotImplementedError(
                f"{type(self).__name__} does not support AI data generation",
            )
        return await self.ai.generate_data(
            task_name=task_name, instructions=instructions,
        )

    async def fire_event(self, event_type: str, data: dict) -> bool:
        return await self.events.fire(event_type, data)

    # ── Capability shortcuts (back-compat with `supports_*` properties) ──

    @property
    def supports_stt(self) -> bool:
        return Capability.STT in self.capabilities

    @property
    def supports_bearer_token_auth(self) -> bool:
        return Capability.PASSWORD_AUTH in self.capabilities


# ── Back-compat alias ────────────────────────────────────────────────────────


#: Older modules import :class:`AbstractPlatformAdapter`. Kept as an
#: alias so the rename to :class:`PlatformAdapter` lands without a
#: cross-cutting find-and-replace. New code should use
#: :class:`PlatformAdapter`.
AbstractPlatformAdapter = PlatformAdapter


# Maps CLI ``mode`` string to the TOML section name. Kept explicit so the
# section key ("homeassistant") can differ from the mode ("ha") — the mode
# is the short operational label, the section is the user-facing TOML tag.
#: Used by :func:`socialhome.platform.build_platform_adapter`.
_PLATFORM_SECTION: dict[str, str] = {
    "ha": "homeassistant",
    "haos": "homeassistant",
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
