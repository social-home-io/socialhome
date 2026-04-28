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
from .client import HaClient, build_ha_client
from .providers import (
    HaAIProvider,
    HaAuthProvider,
    HaEventSink,
    HaPushProvider,
    HaSTTProvider,
    HaUserDirectory,
    _state_to_user,
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

    __slots__ = (
        "_ha_url",
        "_ha_token",
        "_data_dir",
        "_options",
        "_ha_client",
        "_ha_bridge",
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
            # in addition to X-Ingress-User and HA bearer tokens.
            Capability.PASSWORD_AUTH,
        }
        if self._options.get("stt_entity_id"):
            caps.add(Capability.STT)
        return frozenset(caps)

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
        without going through an HTTP request. Tries the local
        credential store (wizard-set passwords) first, then HA's own
        verify_token for long-lived access tokens."""
        if self._credentials is not None:
            local = await self._credentials.authenticate_bearer(token)
            if local is not None:
                return local
        data = await self._client.verify_token(token)
        if data is None:
            return None
        username = data.get("username") or "ha_api_user"
        return ExternalUser(
            username=username,
            display_name=username,
            picture_url=None,
            is_admin=True,
        )

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
        """Return the base URL the HA integration last advertised.

        The adapter never guesses — the integration knows the
        externally-reachable URL (admin-set ``external_url`` or Nabu
        Casa Remote UI) and pushes it via the ``federation.set_base``
        WS command. Returns ``None`` until that has happened; the
        pairing route surfaces it as 422 ``NOT_CONFIGURED``.
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
        self._ha_bridge = HaBridgeService(app[K.event_bus_key], self)
        self._ha_bridge.wire()

    async def on_cleanup(self, app: "web.Application") -> None:  # noqa: RUF029
        """No-op."""

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
        """Resolve + download the ``person.<username>`` ``entity_picture``.

        Returns the raw bytes so the caller (e.g.
        :meth:`UserService.set_picture`) can re-run them through the
        ImageProcessor pipeline. ``None`` when the person has no
        picture or HA is unreachable.
        """
        state = await self._client.get_state(f"person.{username}")
        if state is None:
            return None
        attrs: dict = state.get("attributes", {}) or {}
        entity_picture = attrs.get("entity_picture")
        if not entity_picture:
            return None
        return await self._client.fetch_path_bytes(entity_picture)

    # Module-level ``_state_to_user`` is the canonical helper; preserve
    # the historical class-attribute access path.
    _state_to_user = staticmethod(_state_to_user)


# Back-compat alias — old call sites and tests still import the
# verbose ``HomeAssistantAdapter`` name. Phase 5 bumps the factory and
# any internal references to ``HaAdapter``; external imports keep
# working through this alias.
HomeAssistantAdapter = HaAdapter
