"""HAOS / Supervisor add-on platform adapter (§platform/haos).

Social Home running as a Home Assistant add-on. Talks to HA Core
through the Supervisor proxy at ``http://supervisor/core/api`` and
trusts the Supervisor-injected ``X-Remote-User-Name`` header for inbound
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
from ..ha_home_location import persist_home_location_from_ha
from ..ha.client import HaClient, build_ha_client
from ..ha.ice_servers_sync import HaIceServerSync
from ..ha.providers import (
    HaAIProvider,
    HaEventSink,
    HaPushProvider,
    HaSTTProvider,
    HaUserDirectory,
)
from .bootstrap import HaBootstrap
from .supervisor import SupervisorClient

if TYPE_CHECKING:
    from aiohttp import web


log = logging.getLogger(__name__)


class HaIngressAuthProvider:
    """Trust the Supervisor-injected ``X-Remote-User-Name`` header.

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
        self,
        request: "web.Request",
    ) -> ExternalUser | None:
        # Gate on the canonical "came through HA Core's ingress proxy"
        # marker first. HA Core sets ``X-Hass-Source: core.ingress`` by
        # direct assignment on every ingress hop
        # (``homeassistant/components/hassio/ingress.py``), overwriting
        # any browser-supplied value — so its presence on the request
        # proves the user already passed through Core's authentication
        # and Supervisor's session check. Without it we have no business
        # trusting ``X-Remote-User-Name``.
        if request.headers.get("X-Hass-Source") != "core.ingress":
            return None
        ingress_user = request.headers.get("X-Remote-User-Name")
        if not ingress_user:
            return None
        return await self._adapter.users.get(ingress_user)


class HaosAdapter(PlatformAdapter):
    # ``users`` is narrowed to ``HaUserDirectory`` here (vs the
    # platform-agnostic ``UserDirectory`` protocol the base class
    # exposes) so mypy lets us call ``get_owner`` / ``fetch_picture_bytes``
    # — both HA-only concepts that don't belong on the shared
    # protocol. Identical instance assigned at runtime.
    users: HaUserDirectory

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
        "_ice_sync",
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
        self._ice_sync: HaIceServerSync | None = None
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
    def provides_ice_servers(self) -> bool:
        """``True`` — :class:`HaIceServerSync` pulls HA Core's
        ``web_rtc/ice_servers`` (Nabu Casa Cloud TURN credentials
        included) shortly after startup, so the federation transport
        holds its first handshake until that list lands rather than
        building a STUN-only peer that can never relay."""
        return True

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
        """Return the externally-reachable federation inbox base URL.

        The HA integration pushes the bare external URL (Nabu Casa
        Remote UI or admin-set ``external_url``) and registers an HA
        Core HTTP view at ``/api/socialhome/inbox/{inbox_id}`` that
        forwards into the addon. Peers therefore POST to
        ``{pushed_url}/api/socialhome/inbox/{inbox_id}``, so the
        adapter has to splice that path onto the pushed base before
        the pairing coordinator appends a per-peer secret. Idempotent
        against an integration that ever pushes the full path so we
        don't double-append.
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
        base = raw.rstrip("/")
        if base.endswith("/api/socialhome/inbox"):
            return base
        return f"{base}/api/socialhome/inbox"

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
        # WebRTC ICE-server sync — pull HA's ``web_rtc/ice_servers`` list
        # over the HA Core WS and push to FederationService. Replaces
        # the old HA-integration push endpoint (which only fired on
        # ``EVENT_CORE_CONFIG_UPDATE``, missing Nabu Casa Cloud's
        # runtime registrations). One initial fetch + daily refresh —
        # cadence matches the Nabu Casa Cloud TURN credential TTL.
        #
        # Started FIRST, before HaBootstrap / avatar / timezone /
        # home-location work below: it needs nothing but the HaClient just
        # built and the federation service already in ``app``, and the
        # federation transport holds its first handshake until this pull
        # reports in. Every await it sat behind was pure added latency
        # before the Cloudflare TURN credentials could reach a boot-time
        # outbox drain — the outage this ordering fixes.
        federation_service = app.get(K.federation_service_key)
        if federation_service is not None:

            async def _apply(servers: list[dict]) -> None:
                # ``set_ice_servers`` is sync (just rebinds an attribute
                # + clears suppression); wrap in an async shim so
                # HaIceServerSync's apply_callback contract holds.
                federation_service.set_ice_servers(servers)

            async def _first_attempt() -> None:
                # Release the federation transport's first-handshake ICE
                # gate once we know the outcome — including the two that
                # never call ``set_ice_servers`` (HA returned nothing
                # usable — no cloud subscription, an ordinary install;
                # or the fetch failed outright).
                federation_service.mark_ice_primed()

            self._ice_sync = HaIceServerSync(
                client=self._ha_client,
                apply_callback=_apply,
                on_first_attempt=_first_attempt,
            )
            await self._ice_sync.start()
        if self._supervisor_client is None:
            self._supervisor_client = SupervisorClient(
                session,
                self._supervisor_url,
                self._supervisor_token,
            )
        await HaBootstrap(
            db=self._db,
            users=self.users,
            supervisor=self._supervisor_client,
            data_dir=self._data_dir,
            user_service=app.get(K.user_service_key),
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
        # Read HA Core's time_zone once at startup and mirror it into
        # preferences.tz so calendar events created without an
        # explicit tz inherit HA's wall clock. Operator-side changes
        # in HA propagate to SH on the next add-on restart — household
        # timezones change rarely enough that this is the right
        # cadence. A failed initial fetch leaves the previous value
        # in place (defaults to ``'UTC'`` at install).
        household_svc = app.get(K.preferences_service_key)
        if household_svc is not None:
            try:
                cfg = await self._ha_client.get_config()
            except Exception as exc:  # pragma: no cover
                log.warning("haos_adapter: initial tz fetch failed: %s", exc)
            else:
                tz = (cfg or {}).get("time_zone")
                if isinstance(tz, str) and tz.strip():
                    await household_svc.set_tz_from_ha(tz.strip())
        # §25 — read HA's home coordinates from /api/config and persist them
        # to instance_identity. Publishes LocalHomeLocationUpdated on change
        # so the federation service can broadcast to confirmed peers.
        instance_cfg = await self.get_instance_config()
        await persist_home_location_from_ha(
            db=self._db,
            bus=app[K.event_bus_key],
            latitude=instance_cfg.latitude,
            longitude=instance_cfg.longitude,
        )

    async def _sync_admin_picture_from_ha(
        self,
        app: "web.Application",
    ) -> None:
        user_service = app.get(K.user_service_key)
        user_repo = app.get(K.user_repo_key)
        if user_service is None or user_repo is None:
            return
        owner = await self.users.get_owner()
        if owner is None:
            return
        local = await user_repo.get(owner.username)
        if local is None:
            return
        # Prefer the id stored on the SH row (set at provisioning time
        # by ``HaBootstrap`` / the wizard) so this sync doesn't repeat
        # the username → id WS lookup. Fall back to the ExternalUser
        # if a legacy row predates ``external_id`` — the directory
        # still resolves by username in that case.
        ha_user_id = local.external_id or owner.external_id
        if ha_user_id:
            bytes_ = await self.users.fetch_picture_bytes_by_id(ha_user_id)
        else:
            bytes_ = await self.users.fetch_picture_bytes(owner.username)
        if not bytes_:
            return
        await user_service.set_picture(local.user_id, bytes_)

    async def on_cleanup(self, app: "web.Application") -> None:  # noqa: ARG002
        """Stop the daily ICE-server sync. The HaClient session is
        owned by the app's shared aiohttp ClientSession and torn
        down by its own cleanup hook."""
        if self._ice_sync is not None:
            await self._ice_sync.stop()
            self._ice_sync = None

    def get_extra_services(self) -> dict:
        if self._ha_bridge is not None:
            return {K.ha_bridge_service_key: self._ha_bridge}
        return {}

    async def fetch_entity_picture_bytes(
        self,
        username: str,
    ) -> bytes | None:
        """Delegate to :meth:`HaUserDirectory.fetch_picture_bytes`.

        The auth-user → user_id → ``person.*`` join lives on the
        directory so both HA-flavoured adapters share one
        implementation.
        """
        return await self.users.fetch_picture_bytes(username)
