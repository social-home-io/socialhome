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
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, AsyncIterable, Mapping

from ... import app_keys as K
from ...services.ha_bridge_service import HaBridgeService
from ..adapter import ExternalUser, InstanceConfig, _extract_bearer
from .bootstrap import HaBootstrap
from .client import HaClient, build_ha_client
from .supervisor import SupervisorClient

if TYPE_CHECKING:
    from aiohttp import web


log = logging.getLogger(__name__)


class HomeAssistantAdapter:
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

    # ── Authentication ────────────────────────────────────────────────────

    async def authenticate(self, request: "web.Request") -> ExternalUser | None:
        """Authenticate via ``X-Ingress-User`` header, falling back to bearer.

        When the HA Supervisor routes a request through Ingress it injects
        ``X-Ingress-User`` with the HA username. If that header is absent the
        method falls back to bearer-token validation.
        """
        ingress_user = request.headers.get("X-Ingress-User")
        if ingress_user:
            return await self.get_external_user(ingress_user)

        token = _extract_bearer(request)
        if token:
            return await self.authenticate_bearer(token)

        return None

    async def authenticate_bearer(self, token: str) -> ExternalUser | None:
        """Validate ``token`` against ``GET {ha_url}/api/``.

        A valid HA token returns ``200`` with ``{"message": "API running."}``.
        Any non-200 response means the token is invalid or insufficient.
        """
        data = await self._client.verify_token(token)
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

    # ── User listing ──────────────────────────────────────────────────────

    async def list_external_users(self) -> list[ExternalUser]:
        """Return all ``person.*`` entities from HA states as users."""
        states = await self._client.get_states()
        users: list[ExternalUser] = []
        for state in states:
            entity_id: str = state.get("entity_id", "")
            if not entity_id.startswith("person."):
                continue
            users.append(self._state_to_user(state))
        return users

    async def get_external_user(self, username: str) -> ExternalUser | None:
        """Fetch ``person.<username>`` from HA and convert to :class:`ExternalUser`."""
        state = await self._client.get_state(f"person.{username}")
        if state is None:
            return None
        return self._state_to_user(state)

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

    # ── Push notifications ────────────────────────────────────────────────

    async def send_push(
        self,
        user: ExternalUser,
        title: str,
        message: str,
        data: dict | None = None,
    ) -> None:
        """POST to ``notify.mobile_app_{username}``. Best-effort — swallows errors."""
        body: dict = {"title": title, "message": message}
        if data:
            body["data"] = data
        await self._client.call_service(
            "notify",
            f"mobile_app_{user.username}",
            body,
        )

    # ── Home Assistant bridge (event firing for automations) ──────────────

    async def fire_event(self, event_type: str, data: dict | None = None) -> bool:
        """POST to ``/api/events/{event_type}`` so HA automations can react.

        ``event_type`` is namespaced — Social Home publishes under
        ``socialhome.*`` (e.g. ``socialhome.post_created``,
        ``socialhome.task_assigned``).  Returns ``True`` on 2xx, ``False``
        otherwise. Best-effort — never raises.
        """
        return await self._client.fire_event(event_type, data)

    # ── Lifecycle hooks ────────────────────────────────────────────────────

    async def on_startup(self, app: "web.Application") -> None:
        """Wire the HA client + bridge; run bootstrap when running as add-on."""
        session = app[K.http_session_key]
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
        """No-op — nothing to tear down for the HA adapter yet."""

    def get_extra_services(self) -> dict:
        """Return the HA bridge service keyed on its app key."""
        if self._ha_bridge is not None:
            return {K.ha_bridge_service_key: self._ha_bridge}
        return {}

    def get_extra_routes(self) -> list[tuple[str, type]]:
        """HA adapter provides no extra routes."""
        return []

    @property
    def supports_bearer_token_auth(self) -> bool:
        """HA adapter does not support standalone bearer-token auth."""
        return False

    # ── Speech-to-text (HA STT platform) ──────────────────────────────────

    @property
    def supports_stt(self) -> bool:
        """STT is available when ``[homeassistant].stt_entity_id`` is set."""
        return bool(self._options.get("stt_entity_id"))

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        language: str = "en",
    ) -> str:
        """Buffered transcription — delegates to :meth:`stream_transcribe_audio`.

        Kept for callers that already have the full clip in memory.
        """

        async def _single_chunk() -> AsyncIterable[bytes]:
            if audio_bytes:
                yield audio_bytes

        return await self.stream_transcribe_audio(
            _single_chunk(),
            language=language,
        )

    async def stream_transcribe_audio(
        self,
        audio_stream: AsyncIterable[bytes],
        *,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> str:
        """POST a chunked PCM16 stream to ``/api/stt/{stt_entity_id}``.

        Returns the transcript on ``result=="success"`` and an empty
        string otherwise (mirrors :meth:`generate_ai_data` — STT is a
        best-effort helper, not a critical path).
        """
        entity_id = self._options.get("stt_entity_id")
        if not entity_id:
            raise NotImplementedError(
                "HomeAssistantAdapter: no [homeassistant].stt_entity_id "
                "configured — set it to an HA STT entity id (e.g. "
                "'stt.home_assistant_cloud') to enable transcription."
            )

        payload = await self._client.stream_stt(
            entity_id,
            audio_stream,
            language=language,
            sample_rate=sample_rate,
            channels=channels,
        )
        if not isinstance(payload, dict) or payload.get("result") != "success":
            return ""
        text = payload.get("text") or ""
        return text if isinstance(text, str) else str(text)

    async def generate_ai_data(
        self,
        *,
        task_name: str,
        instructions: str,
    ) -> str:
        """POST to HA's ``ai_task.generate_data`` action.

        Requests ``?return_response`` so HA includes the task's
        ``service_response.data`` in the reply. When
        ``options["ai_task_entity_id"]`` is set we pin the task to that
        HA AI-task entity (e.g. ``ai_task.openai``); otherwise HA picks
        the default. Returns the ``data`` field as a string, or empty
        string on any non-2xx / network error.
        """
        body: dict = {"task_name": task_name, "instructions": instructions}
        entity_id = self._options.get("ai_task_entity_id")
        if entity_id:
            body["entity_id"] = entity_id
        payload = await self._client.call_service(
            "ai_task",
            "generate_data",
            body,
            return_response=True,
        )
        if payload is None:
            return ""
        # HA returns {"changed_states": [...], "service_response": {...}}
        # when ?return_response is set. The task's generated text lives
        # at service_response.data (string or dict when 'structure' set).
        service_response = (payload or {}).get("service_response") or {}
        data = service_response.get("data", "")
        if isinstance(data, str):
            return data
        # Some backends return structured output; stringify for callers
        # that only expect text.
        return str(data) if data else ""

    # ── Location override ─────────────────────────────────────────────────

    async def update_location(
        self,
        latitude: float,
        longitude: float,
        location_name: str,
    ) -> InstanceConfig:
        """Store a location override in ``instance_identity``.

        HA adapter does not carry a ``db`` reference; callers that need DB
        persistence in HA mode should store via the service layer. This
        implementation returns the override wrapped in a fresh config using
        HA's time_zone and currency.
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

    @staticmethod
    def _state_to_user(state: dict) -> ExternalUser:
        """Convert a HA state dict for a ``person.*`` entity to an :class:`ExternalUser`."""
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
