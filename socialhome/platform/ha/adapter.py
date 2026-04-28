"""Home Assistant platform adapter (§platform/ha).

Authenticates requests using HA Ingress headers or a bearer token validated
against the HA REST API. User and instance data are proxied from HA's REST
API via :class:`~.client.HaClient`. Push notifications are delivered via
``notify.mobile_app_<username>``.

AI data generation is delegated to Home Assistant's ``ai_task.generate_data``
action; the ``[homeassistant]`` TOML section may set ``ai_task_entity_id``
to pin a specific HA AI task entity. Speech-to-text streams to the
``stt_entity_id`` entity.

When Social Home runs as a Home Assistant add-on (``SUPERVISOR_TOKEN``
present in the environment) the adapter talks to HA Core through the
Supervisor proxy at ``http://supervisor/core/api``, and a
:class:`~.supervisor.SupervisorClient` drives the one-time
:class:`~.bootstrap.HaBootstrap` on startup.

This module composes Provider classes (:class:`HaAuthProvider`,
:class:`HaUserDirectory`, :class:`HaPushProvider`, :class:`HaSTTProvider`,
:class:`HaAIProvider`, :class:`HaEventSink`) into a
:class:`HomeAssistantAdapter` that satisfies the :class:`PlatformAdapter`
ABC. Phase 4 of the platform-adapter-v2 refactor extracts the
Supervisor-specific behaviour into a separate :class:`HaosAdapter`.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, AsyncIterable, Mapping

from ... import app_keys as K
from ...services.ha_bridge_service import HaBridgeService
from ..adapter import (
    Capability,
    ExternalUser,
    InstanceConfig,
    PlatformAdapter,
    _extract_bearer,
)
from .bootstrap import HaBootstrap
from .client import HaClient, build_ha_client
from .supervisor import SupervisorClient

if TYPE_CHECKING:
    from aiohttp import web


log = logging.getLogger(__name__)


# ── Providers ────────────────────────────────────────────────────────────────


class HaAuthProvider:
    """Resolve a request via ``X-Ingress-User`` header or HA bearer token.

    HAOS routes through Supervisor's ingress proxy which injects
    ``X-Ingress-User``. When that header is absent we fall back to
    bearer-token validation against the HA REST API — the long-lived
    token in the integration's config or a `?token=` query string from
    the WS client.
    """

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HomeAssistantAdapter") -> None:
        self._adapter = adapter

    async def authenticate(
        self,
        request: "web.Request",
    ) -> ExternalUser | None:
        ingress_user = request.headers.get("X-Ingress-User")
        if ingress_user:
            return await self._adapter.users.get(ingress_user)
        token = _extract_bearer(request)
        if token:
            return await self._authenticate_bearer(token)
        return None

    async def _authenticate_bearer(self, token: str) -> ExternalUser | None:
        data = await self._adapter._client.verify_token(token)
        if data is None:
            return None
        # A valid HA token does not carry a specific username — return a
        # minimal sentinel user so callers know authentication succeeded.
        # In production the Ingress path is preferred; bearer is for API use.
        username = data.get("username") or "ha_api_user"
        return ExternalUser(
            username=username,
            display_name=username,
            picture_url=None,
            is_admin=True,
        )


class HaUserDirectory:
    """List / get principals from the HA ``person.*`` entity registry.

    HA mode tracks users in HA itself; the directory is read-only as
    far as enable/disable/passwords go — provisioning happens elsewhere
    (the HA users sync routes in :mod:`socialhome.routes.ha_users`).
    """

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HomeAssistantAdapter") -> None:
        self._adapter = adapter

    async def list_users(self) -> list[ExternalUser]:
        states = await self._adapter._client.get_states()
        users: list[ExternalUser] = []
        for state in states:
            entity_id: str = state.get("entity_id", "")
            if not entity_id.startswith("person."):
                continue
            users.append(_state_to_user(state))
        return users

    async def get(self, username: str) -> ExternalUser | None:
        state = await self._adapter._client.get_state(f"person.{username}")
        return _state_to_user(state) if state else None

    async def is_enabled(self, username: str) -> bool:
        # In HA mode "enabled" means there's a corresponding ``person.*``
        # entity — same as ``get(...) is not None``.
        return (await self.get(username)) is not None

    async def enable(
        self,
        username: str,
        *,
        password: str | None = None,
    ) -> ExternalUser:
        """HA mode does not provision HA persons from the SH side.

        The SH-side enable flow (set Social Home password for an existing
        HA person) is handled by the steady-state user-management routes,
        not the directory. Raised here to make misuse loud.
        """
        raise NotImplementedError(
            "HA-mode enable goes through ha_users routes; the directory "
            "is read-only as far as HA persons are concerned",
        )

    async def disable(self, username: str) -> None:
        raise NotImplementedError(
            "HA-mode disable goes through ha_users routes",
        )


class HaPushProvider:
    """Deliver via ``notify.mobile_app_{username}`` HA service call."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HomeAssistantAdapter") -> None:
        self._adapter = adapter

    async def send(
        self,
        user: ExternalUser,
        title: str,
        message: str,
        data: dict | None = None,
    ) -> None:
        body: dict = {"title": title, "message": message}
        if data:
            body["data"] = data
        await self._adapter._client.call_service(
            "notify",
            f"mobile_app_{user.username}",
            body,
        )


class HaSTTProvider:
    """Stream PCM16 audio to ``/api/stt/{stt_entity_id}``."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HomeAssistantAdapter") -> None:
        self._adapter = adapter

    async def transcribe(
        self,
        audio: bytes,
        language: str = "en",
    ) -> str:
        async def _single_chunk() -> AsyncIterable[bytes]:
            if audio:
                yield audio

        return await self.stream_transcribe(_single_chunk(), language=language)

    async def stream_transcribe(
        self,
        stream: AsyncIterable[bytes],
        *,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> str:
        entity_id = self._adapter._options.get("stt_entity_id")
        if not entity_id:
            raise NotImplementedError(
                "HomeAssistantAdapter: no [homeassistant].stt_entity_id "
                "configured — set it to an HA STT entity id (e.g. "
                "'stt.home_assistant_cloud') to enable transcription."
            )
        payload = await self._adapter._client.stream_stt(
            entity_id,
            stream,
            language=language,
            sample_rate=sample_rate,
            channels=channels,
        )
        if not isinstance(payload, dict) or payload.get("result") != "success":
            return ""
        text = payload.get("text") or ""
        return text if isinstance(text, str) else str(text)


class HaAIProvider:
    """Run HA's ``ai_task.generate_data`` action."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HomeAssistantAdapter") -> None:
        self._adapter = adapter

    async def generate_data(
        self,
        *,
        task_name: str,
        instructions: str,
    ) -> str:
        body: dict = {"task_name": task_name, "instructions": instructions}
        entity_id = self._adapter._options.get("ai_task_entity_id")
        if entity_id:
            body["entity_id"] = entity_id
        payload = await self._adapter._client.call_service(
            "ai_task",
            "generate_data",
            body,
            return_response=True,
        )
        if payload is None:
            return ""
        service_response = (payload or {}).get("service_response") or {}
        data = service_response.get("data", "")
        if isinstance(data, str):
            return data
        return str(data) if data else ""


class HaEventSink:
    """``POST /api/events/{event_type}`` so HA automations can subscribe."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HomeAssistantAdapter") -> None:
        self._adapter = adapter

    async def fire(self, event_type: str, data: dict) -> bool:
        return await self._adapter._client.fire_event(event_type, data)


def _state_to_user(state: dict) -> ExternalUser:
    """Convert an HA ``person.*`` state dict to :class:`ExternalUser`."""
    entity_id: str = state.get("entity_id", "")
    username = entity_id.removeprefix("person.")
    attrs: dict = state.get("attributes", {})
    return ExternalUser(
        username=username,
        display_name=attrs.get("friendly_name") or username,
        picture_url=attrs.get("entity_picture"),
        is_admin=False,
        email=None,
    )


# ── Adapter ──────────────────────────────────────────────────────────────────


class HomeAssistantAdapter(PlatformAdapter):
    """Platform adapter that delegates to the Home Assistant REST API.

    Constructed upfront in the app factory with raw connection settings;
    the actual :class:`HaClient` / :class:`SupervisorClient` are built in
    :meth:`on_startup` once the shared ``aiohttp.ClientSession`` is
    available on the app. Tests can bypass that by injecting pre-built
    ``ha_client`` / ``supervisor_client`` kwargs.
    """

    __slots__ = (
        "_ha_url",
        "_ha_token",
        "_supervisor_url",
        "_supervisor_token",
        "_data_dir",
        "_options",
        "_ha_client",
        "_supervisor_client",
        "_ha_bridge",
        "_db",
        "auth",
        "users",
        "push",
        "stt",
        "ai",
        "events",
    )

    def __init__(
        self,
        *,
        ha_url: str,
        ha_token: str,
        supervisor_url: str,
        supervisor_token: str,
        data_dir: str,
        options: Mapping[str, Any] | None = None,
        ha_client: HaClient | None = None,
        supervisor_client: SupervisorClient | None = None,
    ) -> None:
        self._ha_url = ha_url
        self._ha_token = ha_token
        self._supervisor_url = supervisor_url
        self._supervisor_token = supervisor_token
        self._data_dir = data_dir
        self._options: Mapping[str, Any] = options or MappingProxyType({})
        self._ha_client: HaClient | None = ha_client
        self._supervisor_client: SupervisorClient | None = supervisor_client
        self._ha_bridge: HaBridgeService | None = None
        # ``_db`` is wired in :meth:`on_startup`; used to read the
        # HA-integration-pushed federation base URL from ``instance_config``.
        self._db: Any | None = None

        # Compose providers. Each holds a back-reference to the adapter
        # so they can lazily access ``self._client`` (only available after
        # :meth:`on_startup`) and ``self._options``.
        self.auth = HaAuthProvider(self)
        self.users = HaUserDirectory(self)
        self.push = HaPushProvider(self)
        self.stt = HaSTTProvider(self)
        self.ai = HaAIProvider(self)
        self.events = HaEventSink(self)

    @property
    def capabilities(self) -> frozenset[Capability]:
        caps = {
            Capability.PUSH,
            Capability.AI,
            Capability.HA_PERSON_DIRECTORY,
        }
        if self._options.get("stt_entity_id"):
            caps.add(Capability.STT)
        if self._supervisor_token:
            caps.add(Capability.INGRESS)
        return frozenset(caps)

    @property
    def _client(self) -> HaClient:
        """Return the wired :class:`HaClient`.

        Raises ``RuntimeError`` if accessed before :meth:`on_startup` (or a
        test that constructed the adapter without injecting a client).
        """
        if self._ha_client is None:
            raise RuntimeError(
                "HomeAssistantAdapter used before on_startup — no HaClient wired",
            )
        return self._ha_client

    # ── Authentication (back-compat shim) ─────────────────────────────────

    async def authenticate_bearer(self, token: str) -> ExternalUser | None:
        """Public wrapper around :meth:`HaAuthProvider._authenticate_bearer`
        kept for tests / API consumers that drive the bearer flow without
        going through an HTTP request."""
        return await self.auth._authenticate_bearer(token)

    # ── Instance config ───────────────────────────────────────────────────

    async def get_instance_config(self) -> InstanceConfig:
        """Fetch location, time zone, and currency from ``GET {ha_url}/api/config``."""
        cfg = await self._client.get_config()
        if cfg is None:
            return InstanceConfig(
                location_name="Home",
                latitude=0.0,
                longitude=0.0,
                time_zone="UTC",
                currency="USD",
            )
        return InstanceConfig(
            location_name=cfg.get("location_name", "Home"),
            latitude=float(cfg.get("latitude", 0.0)),
            longitude=float(cfg.get("longitude", 0.0)),
            time_zone=cfg.get("time_zone", "UTC"),
            currency=cfg.get("currency", "USD"),
        )

    # ── Federation inbox base URL (§11) ───────────────────────────────────

    async def get_federation_base(self) -> str | None:
        """Return the base URL the HA integration last advertised.

        The addon never guesses this — the HA integration knows the
        externally-reachable URL (admin-set ``external_url`` or Nabu
        Casa Remote UI) and pushes it to the addon via the
        ``federation.set_base`` WS command. The value is cached in
        ``instance_config['ha_federation_base']``.

        Returns ``None`` before the integration has pushed a base; the
        pairing route surfaces that as 422 ``NOT_CONFIGURED`` so the
        admin knows the HA integration isn't ready yet.
        """
        if self._db is None:
            return None
        row = await self._db.fetchone(
            "SELECT value FROM instance_config WHERE key=?",
            ("ha_federation_base",),
        )
        if row is None:
            return None
        raw = str(row["value"] or "").strip()
        if not raw:
            return None
        return raw.rstrip("/")

    # ── Lifecycle hooks ────────────────────────────────────────────────────

    async def on_startup(self, app: "web.Application") -> None:
        """Wire the HA client + bridge; run bootstrap when running as add-on."""
        session = app[K.http_session_key]
        self._db = app[K.db_key]
        if self._ha_client is None:
            self._ha_client = build_ha_client(
                session,
                supervisor_token=self._supervisor_token,
                ha_url=self._ha_url,
                ha_token=self._ha_token,
            )
        if self._supervisor_token and self._supervisor_client is None:
            self._supervisor_client = SupervisorClient(
                session,
                self._supervisor_url,
                self._supervisor_token,
            )
        if self._supervisor_client is not None:
            await HaBootstrap(
                db=app[K.db_key],
                supervisor_client=self._supervisor_client,
                data_dir=self._data_dir,
            ).run()
            # Best-effort: pull the newly-provisioned admin's HA avatar
            # into the profile picture cache. Safe to run every boot —
            # set_picture is idempotent by hash. Failures only log.
            try:
                await self._sync_admin_picture_from_ha(app)
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "ha_adapter: admin picture sync failed: %s",
                    exc,
                )
        self._ha_bridge = HaBridgeService(app[K.event_bus_key], self)
        self._ha_bridge.wire()

    async def _sync_admin_picture_from_ha(
        self,
        app: "web.Application",
    ) -> None:
        owner = await self._supervisor_client.get_owner_username()  # type: ignore[union-attr]
        if not owner:
            return
        bytes_ = await self.fetch_entity_picture_bytes(owner)
        if not bytes_:
            return
        user_service = app.get(K.user_service_key)
        user_repo = app.get(K.user_repo_key)
        if user_service is None or user_repo is None:
            return
        local = await user_repo.get(owner)
        if local is None:
            return
        await user_service.set_picture(local.user_id, bytes_)

    async def on_cleanup(self, app: "web.Application") -> None:  # noqa: RUF029
        """No-op — the HA adapter holds nothing the base lifecycle does
        not already release."""

    def get_extra_services(self) -> dict:
        """Return the HA bridge service keyed on its app key."""
        if self._ha_bridge is not None:
            return {K.ha_bridge_service_key: self._ha_bridge}
        return {}

    # ``get_extra_routes`` inherits the empty default from PlatformAdapter.

    # ── Location override ─────────────────────────────────────────────────

    async def update_location(
        self,
        latitude: float,
        longitude: float,
        location_name: str,
    ) -> InstanceConfig:
        """Store a location override in :class:`InstanceConfig`.

        HA adapter does not carry a ``db`` reference for arbitrary
        writes; callers that need DB persistence in HA mode should
        store via the service layer. This implementation returns the
        override wrapped in a fresh config using HA's time_zone and
        currency.
        """
        base = await self.get_instance_config()
        return InstanceConfig(
            location_name=location_name,
            latitude=round(float(latitude), 4),
            longitude=round(float(longitude), 4),
            time_zone=base.time_zone,
            currency=base.currency,
        )

    async def fetch_entity_picture_bytes(
        self,
        username: str,
    ) -> bytes | None:
        """Resolve + download the ``person.<username>`` ``entity_picture``.

        Returns the raw bytes so the caller (e.g. ``UserService.set_picture``)
        can re-run them through the ImageProcessor pipeline. ``None`` when
        the person has no picture or HA is unreachable.
        """
        state = await self._client.get_state(f"person.{username}")
        if state is None:
            return None
        attrs: dict = state.get("attributes", {}) or {}
        entity_picture = attrs.get("entity_picture")
        if not entity_picture:
            return None
        return await self._client.fetch_path_bytes(entity_picture)

    # ── Internals ─────────────────────────────────────────────────────────

    # Module-level ``_state_to_user`` is the canonical helper; the class
    # attribute alias preserves any historical caller that referenced it
    # via the class.
    _state_to_user = staticmethod(_state_to_user)
