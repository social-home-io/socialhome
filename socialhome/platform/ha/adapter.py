"""Home Assistant Core platform adapter (§platform/ha).

This is the **non-supervisor** adapter — Social Home running as a
plain process talking to HA over its REST API (long-lived token from
the ``[homeassistant]`` config section). The supervisor add-on path
with Ingress lives in :mod:`socialhome.platform.haos`.

Authentication, user listing, push, STT, AI, and event firing are all
delegated to provider classes in :mod:`.providers`. The adapter wires
them in :meth:`__init__` and inherits the high-level methods
(``authenticate`` / ``list_external_users`` / ``send_push`` / ...) from
the :class:`PlatformAdapter` ABC.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from ... import app_keys as K
from ...services.ha_bridge_service import HaBridgeService
from ..federation_base import manual_federation_base
from ..adapter import (
    Capability,
    ExternalUser,
    InstanceConfig,
    PlatformAdapter,
)
from ..local_credentials import (
    LocalCredentialStore,
    hash_password as _hash_password,
)
from ..ha_home_location import persist_home_location_from_ha
from .client import HaClient, build_ha_client
from .ice_servers_sync import HaIceServerSync
from .providers import (
    HaAIProvider,
    HaAuthProvider,
    HaEventSink,
    HaPushProvider,
    HaSTTProvider,
    HaUserDirectory,
)

if TYPE_CHECKING:
    from aiohttp import web


log = logging.getLogger(__name__)


class HaAdapter(PlatformAdapter):
    """Platform adapter for HA Core (no Supervisor).

    Constructed upfront in the app factory with raw connection settings;
    the actual :class:`HaClient` is built in :meth:`on_startup` once the
    shared ``aiohttp.ClientSession`` is available on the app. Tests can
    bypass that by injecting a pre-built ``ha_client`` kwarg.
    """

    # Narrow the protocol-typed ``users`` field on the base class to
    # the HA-specific subtype so mypy lets us call ``get_owner`` /
    # ``fetch_picture_bytes`` — both genuinely HA-only operations
    # (owner is an HA concept, the picture join walks HA's
    # ``person.*`` registry). The runtime assignment in
    # ``__init__`` is the same ``HaUserDirectory`` instance.
    users: HaUserDirectory

    __slots__ = (
        "_ha_url",
        "_ha_token",
        "_data_dir",
        "_options",
        "_ha_client",
        "_ha_bridge",
        "_ice_sync",
        "_db",
        "_credentials",
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
        data_dir: str,
        options: Mapping[str, Any] | None = None,
        ha_client: HaClient | None = None,
    ) -> None:
        self._ha_url = ha_url
        self._ha_token = ha_token
        self._data_dir = data_dir
        self._options: Mapping[str, Any] = options or MappingProxyType({})
        self._ha_client: HaClient | None = ha_client
        self._ha_bridge: HaBridgeService | None = None
        self._ice_sync: HaIceServerSync | None = None
        self._db: Any | None = None
        self._credentials: LocalCredentialStore | None = None

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
            # Local password auth — the wizard sets a password for the
            # picked HA owner so the user can log in via /api/auth/token
            # in addition to X-Remote-User-Name and HA bearer tokens.
            Capability.PASSWORD_AUTH,
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

    # ── Local credential surface (mirrors StandaloneAdapter) ─────────────

    async def issue_bearer_token(
        self,
        username: str,
        password: str,
        *,
        label: str = "web",
    ) -> str | None:
        """Verify a local password and mint a bearer token.

        Available in ha mode for the owner picked during the setup
        wizard. ``None`` until ``on_startup`` wires the credential store."""
        if self._credentials is None:
            return None
        return await self._credentials.issue_bearer_token(
            username,
            password,
            label=label,
        )

    async def set_local_password(
        self,
        username: str,
        password: str,
        *,
        display_name: str | None = None,
        is_admin: bool = True,
    ) -> None:
        """Attach a local password to ``username``. Used by the ha
        setup wizard. No-op until ``on_startup``."""
        if self._credentials is None:
            raise RuntimeError(
                "HaAdapter.set_local_password called before on_startup",
            )
        await self._credentials.set_password(
            username,
            password,
            display_name=display_name,
            is_admin=is_admin,
        )

    async def change_password(self, username: str, new_password: str) -> None:
        """Rotate ``username``'s password (admin-issued reset redeem).

        Mirrors :meth:`StandaloneAdapter.change_password` so the
        password-reset route can call ``adapter.change_password`` in
        either mode. Doesn't touch ``display_name`` / ``is_admin`` —
        only the hash. Raises ``RuntimeError`` before ``on_startup``."""
        if self._credentials is None:
            raise RuntimeError(
                "HaAdapter.change_password called before on_startup",
            )
        await self._credentials.set_password(username, new_password)

    @staticmethod
    def hash_password(password: str, *, salt: bytes | None = None) -> str:
        return _hash_password(password, salt=salt)

    @property
    def _client(self) -> HaClient:
        """Return the wired :class:`HaClient`. Raises before
        :meth:`on_startup` (or before a test injects one)."""
        if self._ha_client is None:
            raise RuntimeError(
                "HaAdapter used before on_startup — no HaClient wired",
            )
        return self._ha_client

    async def authenticate_bearer(self, token: str) -> ExternalUser | None:
        """Public wrapper around the auth provider's bearer flow.

        Kept for tests / API consumers that drive the bearer flow
        without going through an HTTP request. Only tokens in the
        local credential store (``platform_tokens``) are accepted —
        see :class:`HaAuthProvider._authenticate_bearer` for the
        rationale.
        """
        if self._credentials is None:
            return None
        return await self._credentials.authenticate_bearer(token)

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

        The HA integration pushes the bare external URL (its
        ``get_url(hass, allow_internal=False, ...)`` — Nabu Casa
        Remote UI or admin-set ``external_url``). The integration
        also registers an HA Core HTTP view at
        ``/api/socialhome/inbox/{inbox_id}`` that forwards into this
        addon's ``/federation/inbox/{inbox_id}``. So the peer-facing
        inbox base is ``{pushed_url}/api/socialhome/inbox`` — that's
        what pairing's ``{base}/{secret_id}`` concatenation must hit.

        Idempotent against an integration that ever pushes the full
        path (or a future-renamed prefix) so we don't double-append.

        An admin-set value (``/api/admin/federation/external-url``) wins
        when present. That one means something different — the base at
        which *this* Social Home is directly reachable, so it carries
        Social Home's own inbox path rather than the HA-hosted forwarder
        — which is exactly why the two are stored under separate keys.
        It is the escape hatch for a deployment running without the
        integration.

        Returns ``None`` until either is set; the pairing route surfaces
        it as 422 ``NOT_CONFIGURED``.
        """
        manual = await manual_federation_base(self._db)
        if manual is not None:
            return manual
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

    async def on_startup(self, app: "web.Application") -> None:
        """Wire HaClient + HaBridge. No supervisor bootstrap here —
        that lives in :class:`~socialhome.platform.haos.HaosAdapter`."""
        session = app[K.http_session_key]
        self._db = app[K.db_key]
        self._credentials = LocalCredentialStore(self._db)
        if self._ha_client is None:
            self._ha_client = build_ha_client(
                session,
                supervisor_token="",  # ha-mode: no supervisor proxy
                ha_url=self._ha_url,
                ha_token=self._ha_token,
            )
        # WebRTC ICE-server sync — pull HA's ``web_rtc/ice_servers`` list
        # over the HA Core WS and push to FederationService. Replaces
        # the old HA-integration push endpoint.
        #
        # Started FIRST, before the bridge / timezone / home-location work
        # below: it needs nothing but the HaClient just built and the
        # federation service already in ``app``, and the federation
        # transport holds its first handshake until this pull reports in.
        # Every await it sat behind was pure added latency before TURN
        # credentials could reach a boot-time outbox drain.
        federation_service = app.get(K.federation_service_key)
        if federation_service is not None:

            async def _apply(servers: list[dict]) -> None:
                federation_service.set_ice_servers(servers)

            async def _first_attempt() -> None:
                # Release the federation transport's first-handshake ICE
                # gate once we know the outcome — including the outcomes
                # that never call ``set_ice_servers`` (HA returned nothing
                # usable; the fetch failed). Otherwise the first outbound
                # send waits out the full prime timeout.
                federation_service.mark_ice_primed()

            self._ice_sync = HaIceServerSync(
                client=self._ha_client,
                apply_callback=_apply,
                on_first_attempt=_first_attempt,
            )
            await self._ice_sync.start()
        self._ha_bridge = HaBridgeService(app[K.event_bus_key], self)
        self._ha_bridge.wire()
        # Mirror HA Core's ``time_zone`` into preferences.tz
        # once at startup, so calendar events created without an
        # explicit tz inherit the wall clock HA already owns.
        # Operator-side changes in HA propagate to SH on the next
        # restart — household timezones change rarely enough that this
        # is the right cadence. A failed read leaves the previous value
        # (initially ``'UTC'``) in place.
        household_svc = app.get(K.preferences_service_key)
        if household_svc is not None:
            try:
                cfg = await self._ha_client.get_config()
            except Exception as exc:  # pragma: no cover
                log.warning("ha_adapter: initial tz fetch failed: %s", exc)
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

    async def on_cleanup(self, app: "web.Application") -> None:  # noqa: ARG002
        """Stop the ICE-server sync loop."""
        if self._ice_sync is not None:
            await self._ice_sync.stop()
            self._ice_sync = None

    def get_extra_services(self) -> dict:
        if self._ha_bridge is not None:
            return {K.ha_bridge_service_key: self._ha_bridge}
        return {}

    async def update_location(
        self,
        latitude: float,
        longitude: float,
        location_name: str,
    ) -> InstanceConfig:
        """Return a fresh :class:`InstanceConfig` with the override
        applied. Persistence in HA mode goes through the service
        layer, not the adapter."""
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
        """Delegate to :meth:`HaUserDirectory.fetch_picture_bytes`.

        The join (auth user → user_id → ``person.*``) lives on the
        directory so the same logic serves both ``HaAdapter`` and
        ``HaosAdapter`` without duplication.
        """
        return await self.users.fetch_picture_bytes(username)


# Back-compat alias — old call sites and tests still import the
# verbose ``HomeAssistantAdapter`` name. Phase 5 bumps the factory and
# any internal references to ``HaAdapter``; external imports keep
# working through this alias.
HomeAssistantAdapter = HaAdapter
