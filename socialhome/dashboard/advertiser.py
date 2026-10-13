"""mDNS announcement of the household dashboard, for wall panels.

While the dashboard listens, :class:`DisplayAdvertiser` announces it on
the home network as a ``_socialhome-dash._tcp`` service so a wall
panel (ESPHome firmware, a tablet app) can find it without anyone typing
an address.

What a LAN neighbour learns is deliberately little: *a* Social Home
dashboard is at this address and port. The instance name, host name and
``id`` TXT key carry a 6-hex hash of the instance id (stable across
restarts, but not a prefix of the federation identity), and the TXT
record holds only the display-API version and path — no household name,
no member data, no software version.

Fail-soft by design: multicast can be unavailable (a container on a
bridge network, a firewall) and the dashboard must keep serving anyway.
Failures log one WARNING until the next success.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
from collections.abc import Iterable

import ifaddr
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

log = logging.getLogger(__name__)

__all__ = [
    "DISPLAY_API_PATH",
    "SERVICE_TYPE",
    "DisplayAdvertiser",
    "display_short_id",
    "lan_addresses",
]

#: The DNS-SD service type wall panels browse for.
SERVICE_TYPE = "_socialhome-dash._tcp.local."
#: Major version of the display JSON API the TXT record points at.
DISPLAY_API_VERSION = "1"
#: Path prefix of the display JSON API on the dashboard port.
DISPLAY_API_PATH = "/api/display/v1/"

#: Bound on probing + first announcement, so a stuck network stack can't
#: hold the dashboard's listener lock.
_REGISTER_TIMEOUT_S = 10.0

#: Interface-name prefixes of container / VM / VPN links. A wall panel is
#: never on one of these, and announcing there leaks the service into
#: networks it doesn't belong to.
_VIRTUAL_IFACE_PREFIXES = (
    "docker",
    "br-",
    "veth",
    "virbr",
    "vnet",
    "cni",
    "flannel",
    "cali",
    "podman",
    "lxc",
    "lxd",
    "hassio",
    "tun",
    "tap",
    "wg",
    "zt",
    "tailscale",
    "vboxnet",
    "vmnet",
    "incusbr",
    "ppp",
    "ipsec",
    "utun",
    "cilium",
    "weave",
    "kube",
)

_SHORT_ID_DOMAIN = b"socialhome-dashboard-mdns\x00"

_WILDCARD_V4 = "0.0.0.0"
_WILDCARD_V6 = "::"


def display_short_id(identity_seed: bytes) -> str:
    """A 6-hex id for this household's dashboard on the LAN.

    Derived from the instance's secret identity seed (domain-separated),
    not from the instance id: the instance id is known to paired
    households, the GFS and invite links, so any function of it would let
    them recognise this household on a shared network. Stable across
    restarts, unique enough to tell two households' panels apart, and
    24 bits of a hash reveal nothing about the seed.
    """
    return hashlib.sha256(_SHORT_ID_DOMAIN + identity_seed).hexdigest()[:6]


def _routable(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (addr.is_loopback or addr.is_link_local or addr.is_unspecified)


def lan_addresses(listen_host: str, adapters: Iterable[ifaddr.Adapter]) -> list[str]:
    """The addresses to announce for a dashboard bound to ``listen_host``.

    A specific IP is used as-is (unless it is loopback / link-local —
    nothing on the LAN can reach that). A wildcard picks every routable
    address of the host's physical interfaces: IPv4 for ``0.0.0.0``,
    IPv4 + IPv6 for ``::`` / empty. Loopback, link-local and container /
    VPN interfaces are skipped. A host name falls back to the IPv4 set.
    """
    host = listen_host.strip().strip("[]")
    if host == "localhost":
        return []
    want_v6 = host in ("", _WILDCARD_V6)
    if host not in ("", _WILDCARD_V4, _WILDCARD_V6):
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            pass  # a host name — announce the LAN IPv4 set
        else:
            return [str(addr)] if _routable(addr) else []
    v4: list[str] = []
    v6: list[str] = []
    for adapter in adapters:
        if adapter.name.lower().startswith(_VIRTUAL_IFACE_PREFIXES):
            continue
        for ip in adapter.ips:
            if ip.is_IPv6:
                if not want_v6:
                    continue
                raw = ip.ip[0] if isinstance(ip.ip, tuple) else ip.ip
                addr6 = ipaddress.IPv6Address(raw.split("%", 1)[0])
                if _routable(addr6):
                    v6.append(str(addr6))
            else:
                addr4 = ipaddress.IPv4Address(ip.ip)
                if _routable(addr4):
                    v4.append(str(addr4))
    return list(dict.fromkeys(v4 + v6))


class DisplayAdvertiser:
    """Announces the dashboard port over mDNS while it listens.

    :meth:`advertise` / :meth:`withdraw` follow the listener
    (:class:`~socialhome.dashboard.server.DashboardServer` calls them,
    serialised under its lock); neither ever raises. The identity seed
    arrives after the identity bootstrap via :meth:`attach_identity`;
    until then nothing is announced.
    """

    __slots__ = ("_host", "_short_id", "_zc", "_addresses", "_info", "_warned")

    def __init__(self, *, host: str) -> None:
        self._host = host
        self._short_id: str | None = None
        self._zc: AsyncZeroconf | None = None
        self._addresses: list[str] = []
        self._info: ServiceInfo | None = None
        self._warned = False

    def attach_identity(self, identity_seed: bytes) -> None:
        self._short_id = display_short_id(identity_seed)

    @property
    def advertising_port(self) -> int | None:
        """The port currently announced, or ``None``."""
        return self._info.port if self._info is not None else None

    async def advertise(self, port: int) -> None:
        """Announce the dashboard on ``port`` (moving off any old port)."""
        short_id = self._short_id
        if short_id is None:
            log.debug("dashboard mDNS: no identity yet — not announcing")
            return
        if self.advertising_port == port:
            return
        try:
            async with asyncio.timeout(_REGISTER_TIMEOUT_S):
                announced = await self._announce(port, short_id)
        except Exception as exc:  # fail-soft by contract
            self._warn(f"could not announce the dashboard on the network: {exc!r}")
            await self.withdraw()
            return
        if announced:
            self._warned = False

    async def withdraw(self) -> None:
        """Stop announcing (goodbye packets) and release the sockets."""
        zc, self._zc, self._info = self._zc, None, None
        if zc is None:
            return
        try:
            await zc.async_close()
        except Exception as exc:  # fail-soft by contract
            log.debug("dashboard mDNS: close failed: %r", exc)

    async def _announce(self, port: int, short_id: str) -> bool:
        """Announce on ``port``; ``False`` when there's no LAN to announce on."""
        if self._zc is not None and self._info is not None:
            # Port change: re-publish the SRV under the registered name
            # (``allow_name_change`` may have renamed it) — no goodbye, no
            # second probe, so a panel never sees the service vanish.
            info = _service_info(port, short_id, self._addresses, name=self._info.name)
            _reap(await self._zc.async_update_service(info))
            self._info = info
            log.info("dashboard announcement moved to port %d", port)
            return True
        adapters = await asyncio.to_thread(ifaddr.get_adapters)
        addresses = lan_addresses(self._host, adapters)
        if not addresses:
            if _is_loopback_bind(self._host):
                # Deliberate (e.g. behind a reverse proxy) — nothing to say.
                log.info("dashboard mDNS: listening on loopback — not announcing")
            else:
                self._warn(
                    "no LAN address to announce the dashboard on "
                    f"(listening on {self._host!r}) — panels need its address typed in"
                )
            return False
        # Socket setup is synchronous but brief; zeroconf must be built on
        # the loop it serves, so it can't move to a worker thread.
        self._zc = AsyncZeroconf(interfaces=addresses)
        self._addresses = addresses
        info = _service_info(port, short_id, addresses)
        _reap(await self._zc.async_register_service(info, allow_name_change=True))
        self._info = info
        log.info("dashboard announced on the network as %r (port %d)", info.name, port)
        return True

    def _warn(self, message: str) -> None:
        if not self._warned:
            log.warning("dashboard mDNS: %s", message)
            self._warned = True


def _is_loopback_bind(host: str) -> bool:
    host = host.strip().strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _reap(broadcast: object) -> None:
    """Retrieve the outcome of zeroconf's background announcement future,
    so a responder closed mid-broadcast logs no "exception never
    retrieved"."""
    if isinstance(broadcast, asyncio.Future):
        broadcast.add_done_callback(lambda f: None if f.cancelled() else f.exception())


def _service_info(
    port: int, short: str, addresses: list[str], *, name: str | None = None
) -> ServiceInfo:
    return ServiceInfo(
        SERVICE_TYPE,
        name or f"Social Home {short}.{SERVICE_TYPE}",
        port=port,
        properties={
            "api": DISPLAY_API_VERSION,
            "id": short,
            "path": DISPLAY_API_PATH,
        },
        server=f"socialhome-{short}.local.",
        parsed_addresses=addresses,
    )
