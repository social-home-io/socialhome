"""HAOS / Supervisor add-on platform adapter (§platform/haos).

Social Home running as a Home Assistant add-on. Talks to HA Core
through the Supervisor proxy at ``http://supervisor/core/api`` and
trusts the Supervisor-injected ``X-Ingress-User`` header for inbound
auth (no separate password — the proxy already authenticated the
HA user before forwarding).

The adapter shares its REST-talking providers (HaUserDirectory /
HaPushProvider / HaSTTProvider / HaAIProvider / HaEventSink) with
:class:`socialhome.platform.ha.HaAdapter`. Two pieces differ:

* **Auth** — :class:`HaIngressAuthProvider` instead of
  :class:`HaAuthProvider`. The Supervisor proxy guarantees the
  header is set; we never accept a bearer fallback because that
  would let an attacker inside the addon container authenticate
  bypassing ingress.
* **First-boot** — :class:`HaBootstrap` reads the HA owner from
  ``http://supervisor/auth/list``, provisions them as the SH admin,
  and pushes a discovery payload so the HA integration finds us.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from ... import app_keys as K
from ...services.ha_bridge_service import HaBridgeService
from ..adapter import (
    Capability,
    ExternalUser,
    InstanceConfig,
    PlatformAdapter,
)
from ..ha.client import HaClient, build_ha_client
from ..ha.providers import (
    HaAIProvider,
    HaEventSink,
    HaPushProvider,
    HaSTTProvider,
    HaUserDirectory,
    _state_to_user,
)
from .bootstrap import HaBootstrap
from .supervisor import SupervisorClient

if TYPE_CHECKING:
    from aiohttp import web


log = logging.getLogger(__name__)


class HaIngressAuthProvider:
    """Trust the Supervisor-injected ``X-Ingress-User`` header.

    HAOS routes every web request through the Supervisor's ingress
    proxy, which authenticates the HA user via Home Assistant's own
    auth pipeline before forwarding. By the time the request reaches
    the addon the principal is already established — we read the
    header and look up the matching ``person.*`` entity.

    Unlike :class:`HaAuthProvider`, we do NOT fall back to a bearer
    token from the request. A bearer would let a process inside the
    addon container authenticate bypassing ingress; the haos invariant
    is that ingress is the only entry point.
    """

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HaosAdapter") -> None:
        self._adapter = adapter

    async def authenticate(
        self, request: "web.Request",
    ) -> ExternalUser | None:
        ingress_user = request.headers.get("X-Ingress-User")
        if not ingress_user:
            return None
        return await self._adapter.users.get(ingress_user)


class HaosAdapter(PlatformAdapter):
    """Platform adapter for HA add-on (Supervisor + Ingress).

    Constructed upfront in the app factory; the actual :class:`HaClient`
    and :class:`SupervisorClient` are built in :meth:`on_startup` once
    the shared aiohttp session is available. Tests can inject pre-built
    clients via the ``ha_client`` / ``supervisor_client`` kwargs.
    """

    __slots__ = (
        "_supervisor_url",
        "_supervisor_token",
        "_data_dir",
        "_options",
        "_ha_client",
        "_supervisor_client",
        "_ha_bridge",
        "_db",
        "auth", "users", "push", "stt", "ai", "events",
    )

    def __init__(
        self,
        *,
        supervisor_url: str,
        supervisor_token: str,
        data_dir: str,
        options: Mapping[str, Any] | None = None,
        ha_client: HaClient | None = None,
        supervisor_client: SupervisorClient | None = None,
    ) -> None:
        self._supervisor_url = supervisor_url
        self._supervisor_token = supervisor_token
        self._data_dir = data_dir
        self._options: Mapping[str, Any] = options or MappingProxyType({})
        self._ha_client: HaClient | None = ha_client
        self._supervisor_client: SupervisorClient | None = supervisor_client
        self._ha_bridge: HaBridgeService | None = None
        self._db: Any | None = None

        # HAOS-specific auth — ingress header trust, no bearer fallback.
        self.auth = HaIngressAuthProvider(self)
        # Shared HA providers; they take an adapter ref and read
        # ``self._client`` / ``self._options`` lazily.
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
            Capability.INGRESS,  # always — that's the haos invariant
        }
        if self._options.get("stt_entity_id"):
            caps.add(Capability.STT)
        return frozenset(caps)

    @property
    def _client(self) -> HaClient:
        if self._ha_client is None:
            raise RuntimeError(
                "HaosAdapter used before on_startup — no HaClient wired",
            )
        return self._ha_client

    async def get_instance_config(self) -> InstanceConfig:
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

    async def get_federation_base(self) -> str | None:
        """Return the base URL the HA integration last advertised
        (cached in ``instance_config['ha_federation_base']`` by the
        ``federation.set_base`` WS command). Returns ``None`` until
        the integration has pushed it."""
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

    async def update_location(
        self,
        latitude: float,
        longitude: float,
        location_name: str,
    ) -> InstanceConfig:
        """Return a fresh :class:`InstanceConfig` with the override
        applied. HAOS doesn't persist; HA's own config is the source
        of truth for time_zone / currency."""
        base = await self.get_instance_config()
        return InstanceConfig(
            location_name=location_name,
            latitude=round(float(latitude), 4),
            longitude=round(float(longitude), 4),
            time_zone=base.time_zone,
            currency=base.currency,
        )

    async def on_startup(self, app: "web.Application") -> None:
        """Wire HaClient + SupervisorClient + HaBridge; run the one-time
        :class:`HaBootstrap` so the HA owner becomes the SH admin and
        the integration discovery record is published."""
        session = app[K.http_session_key]
        self._db = app[K.db_key]
        if self._ha_client is None:
            self._ha_client = build_ha_client(
                session,
                supervisor_token=self._supervisor_token,
                ha_url="",  # supervisor proxy handles routing
                ha_token="",
            )
        if self._supervisor_client is None:
            self._supervisor_client = SupervisorClient(
                session,
                self._supervisor_url,
                self._supervisor_token,
            )
        await HaBootstrap(
            db=self._db,
            supervisor_client=self._supervisor_client,
            data_dir=self._data_dir,
        ).run()
        # Best-effort: pull the newly-provisioned admin's HA avatar
        # into the profile picture cache. Failures only log.
        try:
            await self._sync_admin_picture_from_ha(app)
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "haos_adapter: admin picture sync failed: %s",
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
        """No-op."""

    def get_extra_services(self) -> dict:
        if self._ha_bridge is not None:
            return {K.ha_bridge_service_key: self._ha_bridge}
        return {}

    async def fetch_entity_picture_bytes(
        self,
        username: str,
    ) -> bytes | None:
        state = await self._client.get_state(f"person.{username}")
        if state is None:
            return None
        attrs: dict = state.get("attributes", {}) or {}
        entity_picture = attrs.get("entity_picture")
        if not entity_picture:
            return None
        return await self._client.fetch_path_bytes(entity_picture)

    _state_to_user = staticmethod(_state_to_user)
