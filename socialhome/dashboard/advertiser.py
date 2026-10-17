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

The announcement runs in a task of its own: :meth:`DisplayAdvertiser.advertise`
only asks for a port and returns (probing a fresh name takes about a
second, which the dashboard's listener lock must not wait for), and the
task brings the responder to the last state asked for — so a later
:meth:`~DisplayAdvertiser.withdraw` or port change can't be overtaken by an
announce still in flight. While announced, it rescans the LAN addresses
every :data:`RESCAN_INTERVAL_S` (DHCP renumbering, an interface coming
up) and follows a change in place — the record re-announced with the new
addresses, the responder's sockets moved onto them — so a panel never
sees the service vanish; a failed or address-less announcement is
retried on the same beat. This is a request-driven reconciler rather than
a periodic job, so it doesn't use the ``_stop: asyncio.Event`` scheduler
shape: a stop is a request the task drains (goodbyes sent, sockets
closed), never a bare ``cancel()``.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
from collections.abc import Iterable

import ifaddr
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

log = logging.getLogger(__name__)

__all__ = [
    "DISPLAY_API_PATH",
    "RESCAN_INTERVAL_S",
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
#: hold a stop (which waits for the announcement task) for long.
_REGISTER_TIMEOUT_S = 10.0
#: How often the LAN addresses are rescanned while announcing (and a
#: failed announcement retried).
RESCAN_INTERVAL_S = 60.0

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
    (:class:`~socialhome.dashboard.server.DashboardServer` calls them);
    neither ever raises. :meth:`advertise` returns at once — a background
    task announces — while :meth:`withdraw` waits until the goodbye went
    out and the sockets are closed. The identity seed arrives after the
    identity bootstrap via :meth:`attach_identity`; until then nothing is
    announced.
    """

    __slots__ = (
        "_addresses",
        "_applied",
        "_host",
        "_info",
        "_name",
        "_noted_loopback",
        "_progress",
        "_requested",
        "_scanned",
        "_short_id",
        "_task",
        "_wake",
        "_want",
        "_warned",
        "_zc",
    )

    def __init__(self, *, host: str) -> None:
        self._host = host
        self._short_id: str | None = None
        self._zc: AsyncZeroconf | None = None
        # The addresses the responder serves, the scan they came from (a
        # retry may serve fewer — see :meth:`_open`) and the instance name
        # won by probing (kept across a restart of the responder).
        self._addresses: list[str] = []
        self._scanned: list[str] = []
        self._name: str | None = None
        self._info: ServiceInfo | None = None
        self._warned = False
        self._noted_loopback = False
        # The last port asked for (``None``: withdrawn) and how many
        # requests were made / have been carried out.
        self._want: int | None = None
        self._requested = 0
        self._applied = 0
        self._progress = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def attach_identity(self, identity_seed: bytes) -> None:
        self._short_id = display_short_id(identity_seed)

    @property
    def advertising_port(self) -> int | None:
        """The port currently announced, or ``None``."""
        return self._info.port if self._info is not None else None

    async def advertise(self, port: int) -> None:
        """Ask for the dashboard to be announced on ``port`` (moving off
        any old port). Returns at once; :meth:`settled` waits for it."""
        self._request(port)

    async def withdraw(self) -> None:
        """Stop announcing (goodbye packets) and release the sockets —
        after any announcement still in flight. Returns once done."""
        await self._until(self._request(None))

    async def settled(self) -> None:
        """Wait until the last :meth:`advertise` / :meth:`withdraw` has
        been carried out (announced, failed or withdrawn)."""
        await self._until(self._requested)

    # ── The announcement task ──────────────────────────────────────────

    def _request(self, port: int | None) -> int:
        self._want = port
        self._requested += 1
        self._wake.set()
        if self._task is None:
            if port is None and self._zc is None:
                self._done(self._requested)  # nothing to withdraw
            else:
                self._task = asyncio.get_running_loop().create_task(
                    self._run(), name="dashboard-mdns"
                )
        return self._requested

    async def _until(self, generation: int) -> None:
        while self._applied < generation:
            await self._progress.wait()

    def _done(self, generation: int) -> None:
        self._applied = max(self._applied, generation)
        progress, self._progress = self._progress, asyncio.Event()
        progress.set()

    async def _run(self) -> None:
        rescan = False
        try:
            while True:
                self._wake.clear()
                generation = self._requested
                await self._reconcile(rescan=rescan)
                rescan = False
                self._done(generation)
                if self._requested != generation:
                    continue  # asked again meanwhile
                if self._want is None and self._zc is None:
                    return
                # A loopback bind has nothing to follow: wait for requests.
                beat = None if _is_loopback_bind(self._host) else RESCAN_INTERVAL_S
                try:
                    async with asyncio.timeout(beat):
                        await self._wake.wait()
                except TimeoutError:
                    rescan = True
        finally:
            self._task = None
            # Never leave a waiter hanging (cancelled at shutdown).
            self._done(self._requested)

    async def _reconcile(self, *, rescan: bool) -> None:
        """Bring the responder in line with ``_want``; never raises."""
        port = self._want
        if port is None:
            await self._close()
            return
        short_id = self._short_id
        if short_id is None:
            log.debug("dashboard mDNS: no identity yet — not announcing")
            return
        try:
            async with asyncio.timeout(_REGISTER_TIMEOUT_S):
                announced = await self._announce(port, short_id, rescan=rescan)
        except Exception as exc:  # fail-soft by contract
            log.debug(
                "DisplayAdvertiser._reconcile: fail-soft error ignored", exc_info=True
            )
            self._warn(f"could not announce the dashboard on the network: {exc!r}")
            await self._close()
            return
        if announced:
            self._warned = False

    async def _close(self) -> None:
        zc, self._zc, self._info = self._zc, None, None
        self._addresses, self._scanned = [], []
        if zc is None:
            return
        try:
            await zc.async_close()
        except Exception as exc:  # fail-soft by contract
            log.debug("dashboard mDNS: close failed: %r", exc, exc_info=True)

    async def _scan(self) -> list[str]:
        if _is_specific_ip(self._host):
            return lan_addresses(self._host, ())  # the adapters don't matter
        adapters = await asyncio.to_thread(ifaddr.get_adapters)
        return lan_addresses(self._host, adapters)

    async def _announce(self, port: int, short_id: str, *, rescan: bool) -> bool:
        """Announce on ``port``; ``False`` when there's no LAN to announce on."""
        if self._zc is None or self._info is None:
            return await self._start(port, short_id)
        if rescan:
            addresses = await self._scan()
            # Against the last *scan*: after a retry on fewer addresses
            # (:meth:`_open`) the same scan is no change, not a flap.
            if set(addresses) != set(self._scanned):
                return await self._follow(port, short_id, addresses)
        if self._info.port != port:
            # Port change: re-publish the SRV under the registered name
            # (``allow_name_change`` may have renamed it) — no goodbye, no
            # second probe, so a panel never sees the service vanish.
            info = _service_info(port, short_id, self._addresses, name=self._info.name)
            _reap(await self._zc.async_update_service(info))
            self._info = info
            log.info("dashboard announcement moved to port %d", port)
        return True

    async def _follow(self, port: int, short_id: str, addresses: list[str]) -> bool:
        """The LAN addresses changed under a live announcement: re-announce
        the record with the new set (cache-flush, so panels drop the old
        addresses) and move the responder's sockets onto it — no goodbye,
        no new probe."""
        assert self._zc is not None and self._info is not None
        if not addresses:
            await self._close()
            self._warn(
                "the host lost its LAN address — the dashboard announcement "
                "is withdrawn until it is back"
            )
            return False
        info = _service_info(port, short_id, addresses, name=self._name)
        _reap(await self._zc.async_update_service(info))
        self._info = info
        await self._zc.async_update_interfaces(
            interfaces=list(addresses), ip_version=_ip_version(addresses)
        )
        self._addresses, self._scanned = addresses, addresses
        log.info("dashboard announcement now on %s", ", ".join(addresses))
        return True

    async def _start(self, port: int, short_id: str) -> bool:
        scanned = await self._scan()
        if not scanned:
            if _is_loopback_bind(self._host):
                # Deliberate (e.g. behind a reverse proxy) — nothing to say.
                if not self._noted_loopback:
                    log.info("dashboard mDNS: listening on loopback — not announcing")
                    self._noted_loopback = True
            else:
                self._warn(
                    "no LAN address to announce the dashboard on "
                    f"(listening on {self._host!r}) — panels need its address typed in"
                )
            return False
        zc, addresses = await self._open(scanned)
        self._zc, self._addresses, self._scanned = zc, addresses, scanned
        # Under the name already won, if any: a responder restarted after a
        # failure keeps a conflict-renamed name.
        info = _service_info(port, short_id, addresses, name=self._name)
        _reap(await zc.async_register_service(info, allow_name_change=True))
        self._info, self._name = info, info.name
        log.info("dashboard announced on the network as %r (port %d)", info.name, port)
        return True

    async def _open(self, addresses: list[str]) -> tuple[AsyncZeroconf, list[str]]:
        """A responder on ``addresses``. An address can vanish between the
        scan and the socket setup (an IPv6 privacy address expiring, DHCP):
        then one retry, on a fresh scan — or, if that is the same set, its
        IPv4 part."""
        # Socket setup is synchronous but brief; zeroconf must be built on
        # the loop it serves, so it can't move to a worker thread.
        try:
            return AsyncZeroconf(interfaces=addresses), addresses
        except Exception as exc:
            retry = _fallback(addresses, await self._scan())
            if retry is None:
                raise
            log.log(
                # Once per failure streak: a retry that keeps failing logs
                # its WARNING once, not this line every beat.
                logging.DEBUG if self._warned else logging.INFO,
                "dashboard mDNS: could not open %s (%r) — retrying on %s",
                ", ".join(addresses),
                exc,
                ", ".join(retry),
            )
            return AsyncZeroconf(interfaces=retry), retry

    def _warn(self, message: str) -> None:
        if not self._warned:
            log.warning("dashboard mDNS: %s", message)
            self._warned = True


def _fallback(failed: list[str], current: list[str]) -> list[str] | None:
    """What to retry on after a responder failed to open on ``failed``:
    the freshly scanned set if it differs, else its IPv4 part if that
    differs, else nothing."""
    if current and set(current) != set(failed):
        return current
    v4 = [a for a in current if ipaddress.ip_address(a).version == 4]
    if v4 and set(v4) != set(failed):
        return v4
    return None


def _ip_version(addresses: list[str]) -> IPVersion:
    if any(ipaddress.ip_address(a).version == 6 for a in addresses):
        return IPVersion.All
    return IPVersion.V4Only


def _is_specific_ip(host: str) -> bool:
    """``host`` is one IP address (not a wildcard, not a host name)."""
    try:
        return not ipaddress.ip_address(host.strip().strip("[]")).is_unspecified
    except ValueError:
        return False


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
