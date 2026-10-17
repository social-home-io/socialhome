"""Home Assistant Supervisor API client.

A thin wrapper for the Supervisor-only endpoints used by
:class:`HaBootstrap` when Social Home runs as a HA add-on:

* ``GET /addons/self/info`` — read our own add-on metadata so the
  discovery payload can advertise a reachable ``host`` + ``port`` for
  the integration. The Supervisor rewrites underscores in the slug
  to dashes when it assigns Docker DNS names, so handing that
  hostname over directly spares the integration the substitution
  dance.
* ``GET /network/info`` — the host's network interfaces, so Admin ›
  Dashboard can show tablets the Home Assistant host's LAN address
  (:func:`primary_lan_ipv4`) rather than the name the admin's browser
  used (a Nabu Casa remote URL is useless to a wall tablet).
* ``POST /discovery`` — register the add-on with HA's discovery integration
  so the official ``socialhome`` HA integration can pick us up automatically.

User discovery (the owner account, the picker for the wizard, the
ingress-header → identity resolution) goes through HA Core's WS
``config/auth/list`` instead — see
:meth:`socialhome.platform.ha.client.HaClient.list_auth_users`.
The Supervisor's REST ``/auth/list`` was a second source of identity
data with a strictly smaller payload (no ``id``, no
``credentials``) and the same envelope; keeping it forked invited
the kind of slug-vs-username gotchas issue #297 documents.

The Supervisor sets ``SUPERVISOR_URL`` / ``SUPERVISOR_TOKEN`` in the
add-on environment.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import aiohttp

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AddonInfo:
    """Subset of ``GET /addons/self/info`` that we actually consume.

    The Supervisor response carries 40+ fields; we only need the
    DNS hostname and the ingress port to build the discovery
    payload, plus the published-port map for the household dashboard's
    tablet URL. Modelling just those keeps the surface easy to type
    and easy to fake in tests without re-creating the whole
    upstream schema.

    Supervisor contract for ``network`` / ``host_network`` (verified
    against home-assistant/supervisor ``main`` @ 8f21f7a3, 2026-10-11):

    * ``supervisor/api/__init__.py`` routes ``GET /addons/{app}/info`` (the
      legacy alias of ``/apps/{app}/info``) to ``APIApps.info_data_v1`` →
      ``supervisor/api/apps.py`` ``APIApps.info_data``, which sets
      ``ATTR_NETWORK`` (``"network"``) to ``app.ports`` and
      ``ATTR_HOST_NETWORK`` (``"host_network"``) to ``app.host_network``.
    * ``supervisor/apps/app.py`` ``App.ports`` is ``dict[str, int | None] |
      None``: keys are the manifest's container ports (``"8100/tcp"``),
      values the user's host-port override merged over the manifest
      default — ``None`` means "not published" (a manifest default of
      ``null`` the user never set). The whole field is ``None`` when the
      manifest declares no ports.
    * ``supervisor/docker/app.py`` ``DockerApp.ports`` drops falsy host
      ports when the container is created (``DockerApp.run``) and publishes
      nothing under ``host_network`` — there the container port *is* the
      host port.
    * ``supervisor/api/apps.py`` ``APIApps.options`` only persists a new
      ``network`` map; it does not restart the App. The container's
      published ports change when the App is next started (HA's frontend
      asks the user to restart it after a network change).
    """

    hostname: str
    ingress_port: int
    network: Mapping[str, int | None] | None = None
    host_network: bool = False

    @classmethod
    def from_response(cls, data: dict | None) -> AddonInfo | None:
        """Return an :class:`AddonInfo` or ``None`` if either required
        field is missing from the Supervisor's response."""
        if not data:
            return None
        hostname = data.get("hostname")
        port = data.get("ingress_port")
        if not hostname or port is None:
            return None
        return cls(
            hostname=str(hostname),
            ingress_port=int(port),
            network=_parse_network(data.get("network")),
            host_network=data.get("host_network") is True,
        )

    def declares(self, container_port: str) -> bool:
        """Does the App manifest declare ``container_port``? A key with a
        ``null`` host port is declared (the user just hasn't turned it
        on); a missing key or no ``network`` map at all is not."""
        if self.host_network:
            return True
        return self.network is not None and container_port in self.network

    def host_port(self, container_port: str) -> int | None:
        """The host port ``container_port`` (e.g. ``"8100/tcp"``) is
        published on, or ``None`` when it isn't published."""
        if self.host_network:
            number, _, _ = container_port.partition("/")
            return int(number) if number.isdigit() else None
        if self.network is None:
            return None
        return self.network.get(container_port)


def _parse_network(raw: object) -> Mapping[str, int | None] | None:
    """The ``network`` map with anything but a positive int host port read
    as "not published" — a malformed entry must not invent a port."""
    if not isinstance(raw, dict):
        return None
    parsed: dict[str, int | None] = {}
    for key, value in raw.items():
        ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
        parsed[str(key)] = value if ok else None
    return MappingProxyType(parsed)


#: The RFC 1918 home-network ranges. ``ip.is_private`` is broader — it
#: also counts ``0.0.0.0/8``, the documentation and benchmark ranges and
#: ``240.0.0.0/4`` — none of which a wall tablet can reach.
_LAN_NETWORKS = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)


def _lan_ipv4(iface: Mapping) -> str | None:
    """The first RFC 1918 IPv4 address on ``iface`` (prefix stripped), or
    ``None`` — never a loopback, link-local, reserved or public address."""
    ipv4 = iface.get("ipv4")
    if not isinstance(ipv4, Mapping):
        return None
    addresses = ipv4.get("address")
    if not isinstance(addresses, list):
        return None
    for raw in addresses:
        if not isinstance(raw, str):
            continue
        try:
            ip = ipaddress.ip_interface(raw).ip
        except ValueError:
            continue
        if any(ip in net for net in _LAN_NETWORKS):
            return str(ip)
    return None


def primary_lan_ipv4(data: Mapping | None) -> str | None:
    """The Home Assistant host's LAN IPv4 from ``GET /network/info``'s
    ``data`` object, or ``None``.

    Prefers the interface Supervisor marks ``primary`` (the one holding
    the default route), then any other connected interface; an interface
    counts only when ``enabled`` and ``connected`` are literally ``true``.
    Only private addresses qualify — a wall tablet on the LAN can't use a
    loopback, link-local or public one. A malformed payload is ``None``.

    Supervisor contract (verified against home-assistant/supervisor
    ``main``, 2026-10-17):

    * ``supervisor/api/__init__.py`` ``_register_network`` routes
      ``GET /network/info`` to ``APINetwork.info_v1``, which wraps its
      answer in the usual ``{"result": "ok", "data": {...}}`` envelope.
    * ``supervisor/api/middleware/security.py``: the default App role may
      call any path matching ``^(?:|/.+/info)$`` — so ``hassio_api: true``
      is enough, no ``hassio_role`` bump.
    * ``supervisor/api/network.py`` ``interface_struct_v1`` /
      ``ip4config_struct``: ``data.interfaces[]`` items carry
      ``interface``, ``type``, ``enabled``, ``connected``, ``primary`` and
      ``ipv4: {method, address: ["192.168.1.10/24", ...], gateway,
      nameservers, ready}`` (``supervisor/const.py``: ``ATTR_INTERFACES``
      = ``"interfaces"``, ``ATTR_PRIMARY`` = ``"primary"``,
      ``ATTR_CONNECTED`` = ``"connected"``, ``ATTR_ENABLED`` =
      ``"enabled"``, ``ATTR_IPV4`` = ``"ipv4"``, ``ATTR_ADDRESS`` =
      ``"address"``).
    """
    if not isinstance(data, Mapping):
        return None
    interfaces = data.get("interfaces")
    if not isinstance(interfaces, list):
        return None
    up = [
        iface
        for iface in interfaces
        if isinstance(iface, Mapping)
        and iface.get("enabled") is True
        and iface.get("connected") is True
    ]
    # Primary first; ``sorted`` is stable, so Supervisor's order breaks ties.
    for iface in sorted(up, key=lambda i: i.get("primary") is not True):
        address = _lan_ipv4(iface)
        if address is not None:
            return address
    return None


class SupervisorClient:
    """HTTP client for the Supervisor API (never talks to HA Core)."""

    __slots__ = ("_base_url", "_session", "_token")

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def get_self_info(self) -> AddonInfo | None:
        """Return ``GET /addons/self/info`` as a typed :class:`AddonInfo`,
        or ``None`` if the call failed / the response was missing
        the fields :class:`HaBootstrap._push_discovery` needs.

        ``AddonInfo.hostname`` has already had ``_`` replaced with
        ``-`` by the Supervisor (Docker DNS doesn't accept
        underscores), so the integration can use it verbatim
        without further substitution.
        """
        try:
            async with self._session.get(
                f"{self._base_url}/addons/self/info",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            # ValueError covers a body that isn't JSON.
            log.warning("supervisor: /addons/self/info failed: %s", exc)
            return None
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            log.warning("supervisor: /addons/self/info returned no data object")
            return None
        try:
            return AddonInfo.from_response(data["data"])
        except (TypeError, ValueError) as exc:
            log.warning("supervisor: /addons/self/info is malformed: %s", exc)
            return None

    async def get_network_info(self) -> dict | None:
        """Return ``GET /network/info``'s ``data`` object (see
        :func:`primary_lan_ipv4` for the contract), or ``None`` if the
        call failed or the answer carried no data object (logged at
        WARNING)."""
        try:
            async with self._session.get(
                f"{self._base_url}/network/info",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                body = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            # ValueError covers a body that isn't JSON.
            log.warning("supervisor: /network/info failed: %s", exc)
            return None
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            log.warning("supervisor: /network/info returned no data object")
            return None
        data: dict = body["data"]
        return data

    async def push_discovery(self, payload: dict) -> bool:
        """POST ``/discovery`` — returns ``True`` on 2xx.

        The payload should carry the integration token plus the
        ``host`` + ``port`` the HA integration needs to reach us.
        :class:`HaBootstrap` builds it.
        """
        try:
            async with self._session.post(
                f"{self._base_url}/discovery",
                headers=self._headers(),
                json=payload,
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                log.warning(
                    "supervisor: discovery push returned HTTP %d",
                    resp.status,
                )
                return False
        except aiohttp.ClientError as exc:
            log.warning("supervisor: discovery push failed: %s", exc)
            return False


__all__ = ["AddonInfo", "SupervisorClient", "primary_lan_ipv4"]
