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
  (``X-Remote-User-Name`` header).

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
* :class:`ZoneSource` — the platform's named places (Home Assistant's
  ``zone.*`` entities), mirrored into the household's zones.
* :class:`HomeLocationSource` — the platform's own home location (Home
  Assistant's core config), mirrored read-only as the household's home.
* :class:`DisplayDirectory` — wall panels the platform knows (Home
  Assistant's ESPHome devices running the Social Home dashboard
  firmware), and a way to send one a connect request.

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
from collections.abc import AsyncIterable
from dataclasses import dataclass
from enum import StrEnum
from typing import (
    TYPE_CHECKING,
    Protocol,
    runtime_checkable,
)

from .federation_base import public_base_from_federation_base

if TYPE_CHECKING:
    from aiohttp import web

    from ..domain.household_dashboard import ExternalDisplayPanel
    from ..domain.household_zone import ExternalHouseholdZone

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
class DashboardHostPort:
    """Where the platform publishes the household dashboard on the host.

    Three states:

    * ``port`` set — published on that host port (``mapped``);
    * ``port=None, declared=True`` — the platform could publish it but
      the user hasn't turned it on (Home Assistant App: the port left
      empty under the App's Network settings) — the admin can fix this;
    * ``port=None, declared=False`` — the platform doesn't offer the port
      at all (an App release whose manifest doesn't declare it) — nothing
      the admin can do.
    """

    port: int | None
    declared: bool = True

    @property
    def mapped(self) -> bool:
        return self.port is not None


@dataclass(slots=True, frozen=True)
class ExternalUser:
    """A user as seen by the platform layer.

    In ha / haos mode this maps to an HA auth account (the WS
    ``config/auth/list`` row); in standalone mode it maps to a row in
    ``platform_users``.

    ``external_id`` is the provider's stable identifier for the
    account — the 32-hex HA ``user_id`` for ``source='ha'`` rows,
    ``None`` for standalone — so SH can join back to provider-side
    resources (HA ``person.*`` entities via ``attributes.user_id``,
    future zone / device-tracker bridges) without re-running the
    username → id lookup.
    """

    username: str
    display_name: str
    picture_url: str | None
    is_admin: bool
    email: str | None = None
    external_id: str | None = None


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
    #: Adapter has a :class:`ZoneSource` — the household's zones can be
    #: synced from the platform (Admin › Zones shows the sync toggle).
    ZONE_SYNC = "zone_sync"
    #: The process shares the host's network, so an mDNS announcement
    #: reaches the LAN — the household dashboard announces itself to wall
    #: panels (``_socialhome-dash._tcp``). Off for the Home Assistant
    #: App: its container sits on the Supervisor's bridge network
    #: (no ``host_network``), where multicast never reaches the LAN.
    MDNS_ADVERTISE = "mdns_advertise"
    #: Adapter has a :class:`DisplayDirectory` — wall panels adopted in
    #: Home Assistant show under Admin › Dashboard › Wall panels with a
    #: Connect button (ha + haos; standalone has no platform to ask).
    HA_DISPLAY_DISCOVERY = "ha_display_discovery"
    #: Adapter has a :class:`HomeLocationSource` — the household's home
    #: location comes from the platform (Home Assistant's core config) and
    #: Admin › Settings shows it read-only; without it the household sets
    #: its home itself (standalone).
    HOME_LOCATION_SYNC = "home_location_sync"
    #: Adapter forwards events to the platform's automation engine
    #: (``HaBridgeService`` → Home Assistant's event bus), so Settings →
    #: Notifications offers the per-person opt-in to send one's
    #: notifications there (D14; ha + haos).
    AUTOMATION_EVENTS = "automation_events"


# ── Provider Protocols ───────────────────────────────────────────────────────


@runtime_checkable
class AuthProvider(Protocol):
    """Resolve an HTTP request to an :class:`ExternalUser`.

    Mode-specific implementations:

    * Standalone: SHA-256 lookup in ``platform_tokens`` / ``api_tokens``.
    * HA Core: ``X-Remote-User-Name`` header (when behind a proxy) +
      bearer-token validation against the HA REST API.
    * HAOS: ``X-Remote-User-Name`` (Supervisor-injected) + bearer fallback
      for token-API consumers.

    Returning ``None`` always means "401 unauthenticated" — never raise.
    """

    async def authenticate(
        self,
        request: web.Request,
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

    async def list_notify_targets(self) -> list[dict]:
        """User-selectable notify targets as ``[{"entity_id", "name"}]``.

        Empty list when the platform exposes no selectable targets.
        """
        ...


@runtime_checkable
class STTProvider(Protocol):
    """Speech-to-text: buffered + streaming.

    Streaming is the primary path; buffered wraps it for callers that
    have the full audio in memory. PCM16 little-endian at
    ``sample_rate`` Hz, ``channels`` channels (default 16 kHz mono —
    HA Whisper's defaults).
    """

    async def transcribe(
        self,
        audio: bytes,
        language: str = "en",
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


@runtime_checkable
class ZoneSource(Protocol):
    """The platform's named places, read for the household-zone sync.

    HA / HAOS: the ``zone.*`` entities from ``GET /api/states``
    (passive zones left out; ``zone.home`` is the Home zone).
    Standalone: none (``PlatformAdapter.zones`` is ``None``).
    """

    async def list_zones(self) -> list[ExternalHouseholdZone]:
        """Every zone the platform has right now — ``[]`` when it truly
        has none. Raises
        :class:`~socialhome.domain.household_zone.ZoneSourceUnavailableError`
        when the platform couldn't be read, so a sync never mistakes a
        failed call for an empty list."""
        ...


@runtime_checkable
class HomeLocationSource(Protocol):
    """The platform's own home location — the household's home when the
    platform has one.

    HA / HAOS: ``latitude`` / ``longitude`` of HA Core's ``GET /api/config``
    (the same point ``zone.home`` is drawn around). Standalone: none
    (``PlatformAdapter.home_location`` is ``None``) — the household sets
    its home in Admin › Settings.
    """

    async def get_home_location(self) -> tuple[float, float] | None:
        """``(lat, lon)`` at 4 dp, or ``None`` when the platform has no
        usable home right now (none set, or it couldn't be read — the
        caller then keeps the home it has)."""
        ...


@runtime_checkable
class DisplayDirectory(Protocol):
    """Wall panels the platform knows about, and how to reach them.

    HA / HAOS: ESPHome devices in Home Assistant's device registry whose
    firmware is the Social Home dashboard (``esphome.project``
    ``social-home-io.esphome-dashboard``); a connect request is their
    ``connect_social_home`` ESPHome action, called through HA.
    Standalone: none (``PlatformAdapter.displays`` is ``None``).
    """

    async def list_panels(self) -> list[ExternalDisplayPanel]:
        """Every panel the platform has — ``[]`` when it has none.
        Raises
        :class:`~socialhome.domain.household_dashboard.DisplayDiscoveryFailedError`
        when the platform couldn't be read."""
        ...

    async def connect(self, panel: ExternalDisplayPanel, *, sealed: str) -> None:
        """Ask ``panel`` to pair itself: deliver ``sealed`` — its claim
        and the dashboard's address, sealed to ``panel.panel_key``
        (:mod:`socialhome.dashboard.claim_seal`). Raises
        :class:`~socialhome.domain.household_dashboard.DisplayDiscoveryFailedError`
        when the platform didn't deliver it. ``sealed`` may end up in the
        platform's history (HA's recorder) — it is ciphertext only the
        panel opens."""
        ...


class NoopEventSink:
    """Default :class:`ExternalEventSink` for adapters without an
    upstream event bus. Returns ``False`` for every call so callers
    learn the event was not published, but raises nothing."""

    async def fire(self, event_type: str, data: dict) -> bool:
        return False


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
    #: Platform zones for the household-zone sync; ``None`` = the platform
    #: has none (adapters that set it also declare ``Capability.ZONE_SYNC``).
    zones: ZoneSource | None = None
    #: The platform's home location; ``None`` = the household sets its own
    #: (adapters that set it also declare ``Capability.HOME_LOCATION_SYNC``).
    home_location: HomeLocationSource | None = None
    #: Wall panels the platform knows; ``None`` = it knows none (adapters
    #: that set it also declare ``Capability.HA_DISPLAY_DISCOVERY``).
    displays: DisplayDirectory | None = None

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

    async def get_public_base_url(self) -> str | None:
        """Return the origin external clients reach this Social Home at.

        Used for URLs that leave the SPA (iCal feed links, the API-token
        base) — never ``document.baseURI``, which under ingress is a
        path only a signed-in Home Assistant browser can load. Derived
        from :meth:`get_federation_base`, so it is set exactly when the
        deployment has a direct public address; ``None`` when the only
        public route is the Home Assistant inbox forwarder (or nothing).
        """
        return public_base_from_federation_base(await self.get_federation_base())

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
    async def on_startup(self, app: web.Application) -> None:
        """Wire platform-specific services + run any first-boot bootstrap."""
        ...

    @abc.abstractmethod
    async def on_cleanup(self, app: web.Application) -> None:
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
        self,
        request: web.Request,
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
        self,
        audio_bytes: bytes,
        language: str = "en",
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
            task_name=task_name,
            instructions=instructions,
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

    @property
    def provides_ice_servers(self) -> bool:
        """``True`` when this platform pushes an ICE-server list
        asynchronously after startup, so the federation transport should
        wait briefly before its first handshake.

        The HA-backed adapters pull HA Core's ``web_rtc/ice_servers``
        (Nabu Casa Cloud TURN credentials among them) over the HA
        WebSocket once the app is up — after the boot-time outbox drain
        has already started building peers. A peer built STUN-only in
        that window can never relay, so the transport holds its first
        offerer handshake until the list lands.

        Deliberately NOT a :class:`Capability`: that set is serialised to
        the SPA by ``GET /api/instance/config``, and this is a backend
        startup-ordering fact with nothing for the UI to render.
        Platforms that will never push a list leave this ``False``, and
        ``create_app`` releases the gate immediately on their behalf.

        The admin ICE-servers diagnostic reports the same fact as
        ``pulls_from_home_assistant``: a pulled list replaces the
        config-derived one, so an operator's TOML TURN settings are not
        what federation ends up using.
        """
        return False

    @property
    def honours_manual_federation_base(self) -> bool:
        """``True`` when :meth:`get_federation_base` takes the admin-set
        base (``PUT /api/admin/federation/external-url``) into account.

        The Home Assistant App returns ``False``: it sits behind ingress
        with no address of its own that peers could reach, so peers POST
        to the inbox forwarder the companion integration registers inside
        Home Assistant, and the integration owns that URL. A hand-typed
        base there would never take effect, so the admin API refuses to
        store one rather than report a change that didn't happen.

        Deliberately NOT a :class:`Capability` — it is a backend fact;
        the external-URL admin API surfaces it as ``manual_supported``.
        """
        return True

    @property
    def fixed_dashboard_port(self) -> int | None:
        """The port the household dashboard must listen on, or ``None``
        when the admin may choose it.

        The Home Assistant App returns the container port: HA Supervisor
        owns the host port mapping, so changing the port inside the
        container would only break the mapping. Standalone and ``ha``
        installs leave this ``None``.

        Deliberately NOT a :class:`Capability` — it is a backend fact;
        the dashboard admin API surfaces it as ``port_fixed``.
        """
        return None

    async def dashboard_host_port(self) -> DashboardHostPort | None:
        """The host port the dashboard's listen port is published on.

        ``None`` (the default) means "not applicable or unknown": the
        dashboard is reached on the port it listens on. The Home Assistant
        App returns the port HA Supervisor maps to the container port —
        ``DashboardHostPort(None)`` when the user hasn't mapped one,
        ``DashboardHostPort(None, declared=False)`` when the App release
        doesn't declare the port — and degrades to ``None`` (logged at
        WARNING) when Supervisor can't be asked. The dashboard admin API
        surfaces it as ``host_port_mapped`` (``null`` unless declared).
        """
        return None

    async def dashboard_lan_host(self) -> str | None:
        """The LAN address (an IP literal, IPv4 or unbracketed IPv6) wall
        tablets should reach the dashboard under, or ``None`` when the
        platform doesn't know one.

        Pairs with :meth:`dashboard_host_port`: together they build the
        tablet URL when the admin hasn't set an address. ``None`` (the
        default) leaves the admin page guessing from the host the admin's
        browser used. The Home Assistant App returns the HA host's primary
        RFC 1918 IPv4 from Supervisor's ``GET /network/info`` — the admin may
        be browsing through a Nabu Casa remote URL no tablet can use — and
        degrades to ``None`` (logged at WARNING) when Supervisor can't
        answer.
        """
        return None


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


def _extract_bearer(request: web.Request) -> str | None:
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
