"""Tests for DisplayAdvertiser — the dashboard's mDNS announcement.

Zeroconf and the interface scan are mocked at the module boundary: no
multicast socket is ever opened here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from unittest.mock import patch

import ifaddr
import pytest
from zeroconf import service_type_name

from socialhome.dashboard import advertiser as adv_mod
from socialhome.dashboard.advertiser import (
    SERVICE_TYPE,
    DisplayAdvertiser,
    display_short_id,
    lan_addresses,
)

SEED = bytes(range(32))


class FakeZeroconf:
    """Stand-in for :class:`zeroconf.asyncio.AsyncZeroconf`."""

    instances: list[FakeZeroconf] = []
    fail_init: Exception | None = None
    fail_register: Exception | None = None
    fail_update: Exception | None = None

    def __init__(self, interfaces=None, **_kw) -> None:
        if FakeZeroconf.fail_init is not None:
            raise FakeZeroconf.fail_init
        self.interfaces = interfaces
        self.registered: list = []
        self.updated: list = []
        self.unregistered: list = []
        self.closed = False
        FakeZeroconf.instances.append(self)

    async def async_register_service(self, info, **kw):
        if FakeZeroconf.fail_register is not None:
            raise FakeZeroconf.fail_register
        self.registered.append((info, kw))

    async def async_update_service(self, info):
        if FakeZeroconf.fail_update is not None:
            raise FakeZeroconf.fail_update
        self.updated.append(info)

    async def async_unregister_service(self, info):
        self.unregistered.append(info)

    async def async_close(self) -> None:
        self.closed = True


def _adapter(name: str, *ips: str | tuple[str, int, int]) -> ifaddr.Adapter:
    return ifaddr.Adapter(name, name, [ifaddr.IP(ip, 24, name) for ip in ips])


ADAPTERS = [
    _adapter("lo", "127.0.0.1", ("::1", 0, 0)),
    _adapter("eth0", "192.168.1.20", ("fe80::1", 0, 2), ("fd00::20", 0, 2)),
    _adapter("docker0", "172.17.0.1"),
    _adapter("br-1234abcd", "172.18.0.1"),
    _adapter("veth99", "169.254.3.3"),
    _adapter("wlan0", "169.254.7.7", "10.0.0.5"),
]


@pytest.fixture(autouse=True)
def fake_zeroconf():
    FakeZeroconf.instances = []
    FakeZeroconf.fail_init = None
    FakeZeroconf.fail_register = None
    FakeZeroconf.fail_update = None
    with (
        patch.object(adv_mod, "AsyncZeroconf", FakeZeroconf),
        patch.object(adv_mod.ifaddr, "get_adapters", return_value=ADAPTERS),
    ):
        yield FakeZeroconf


def _advertiser(host: str = "0.0.0.0") -> DisplayAdvertiser:
    a = DisplayAdvertiser(host=host)
    a.attach_identity(SEED)
    return a


# ── short id / addresses ───────────────────────────────────────────────


def test_short_id_is_a_domain_separated_hash_of_the_secret_seed():
    short = display_short_id(SEED)
    assert len(short) == 6
    assert short == display_short_id(SEED)
    assert short != hashlib.sha256(SEED).hexdigest()[:6]
    assert short != display_short_id(bytes(32))


def test_lan_addresses_wildcard_v4_skips_loopback_link_local_and_bridges():
    assert lan_addresses("0.0.0.0", ADAPTERS) == ["192.168.1.20", "10.0.0.5"]


def test_lan_addresses_dual_stack_wildcard_adds_routable_ipv6():
    assert lan_addresses("::", ADAPTERS) == ["192.168.1.20", "10.0.0.5", "fd00::20"]
    assert lan_addresses("", ADAPTERS) == ["192.168.1.20", "10.0.0.5", "fd00::20"]


def test_lan_addresses_specific_ip_is_used_as_is():
    assert lan_addresses("10.0.0.5", ADAPTERS) == ["10.0.0.5"]


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "169.254.7.7"])
def test_lan_addresses_loopback_listener_is_not_advertised(host):
    assert lan_addresses(host, ADAPTERS) == []


def test_lan_addresses_hostname_falls_back_to_lan_v4():
    assert lan_addresses("myhost.lan", ADAPTERS) == ["192.168.1.20", "10.0.0.5"]


def test_service_type_passes_zeroconf_validation():
    """DNS-SD caps the service name at 15 bytes (RFC 6335 §5.1); the fake
    responder below doesn't check it, real registration does — a
    ``_socialhome-display`` type was refused with ``BadTypeInNameException``."""
    assert service_type_name(SERVICE_TYPE) == SERVICE_TYPE
    short = display_short_id(SEED)
    instance = f"Social Home {short}.{SERVICE_TYPE}"
    assert service_type_name(instance, strict=False) == SERVICE_TYPE


# ── advertise / withdraw ───────────────────────────────────────────────


async def test_advertise_registers_service_with_minimal_txt(fake_zeroconf):
    a = _advertiser()
    await a.advertise(8100)

    [zc] = fake_zeroconf.instances
    assert zc.interfaces == ["192.168.1.20", "10.0.0.5"]
    [(info, kw)] = zc.registered
    short = display_short_id(SEED)
    assert info.type == SERVICE_TYPE == "_socialhome-dash._tcp.local."
    assert info.name == f"Social Home {short}.{SERVICE_TYPE}"
    assert info.server == f"socialhome-{short}.local."
    assert info.port == 8100
    assert info.parsed_addresses() == ["192.168.1.20", "10.0.0.5"]
    assert info.properties == {
        b"api": b"1",
        b"id": short.encode(),
        b"path": b"/api/display/v1/",
    }
    assert kw == {"allow_name_change": True}
    assert a.advertising_port == 8100


async def test_advertise_same_port_is_a_noop(fake_zeroconf):
    a = _advertiser()
    await a.advertise(8100)
    await a.advertise(8100)
    [zc] = fake_zeroconf.instances
    assert len(zc.registered) == 1


async def test_port_change_updates_the_registered_service(fake_zeroconf):
    a = _advertiser()
    await a.advertise(8100)
    [zc] = fake_zeroconf.instances
    [(first, _)] = zc.registered
    first.name = f"Social Home {display_short_id(SEED)} (2).{SERVICE_TYPE}"

    await a.advertise(8200)

    assert len(fake_zeroconf.instances) == 1
    assert len(zc.registered) == 1  # no second probe
    assert zc.unregistered == []  # no goodbye
    [moved] = zc.updated
    assert moved.port == 8200
    assert moved.name == first.name  # keeps a conflict-renamed name
    assert moved.properties == first.properties
    assert a.advertising_port == 8200


async def test_failed_port_change_withdraws(fake_zeroconf, caplog):
    a = _advertiser()
    await a.advertise(8100)
    fake_zeroconf.fail_update = OSError("socket gone")
    with caplog.at_level(logging.WARNING, logger=adv_mod.__name__):
        await a.advertise(8200)
    [zc] = fake_zeroconf.instances
    assert zc.closed
    assert a.advertising_port is None
    assert "socket gone" in caplog.text


async def test_register_timeout_is_fail_soft(fake_zeroconf, caplog):
    started = asyncio.Event()

    async def hang(self, info, **kw):
        started.set()
        await asyncio.Event().wait()

    a = _advertiser()
    with (
        patch.object(adv_mod, "_REGISTER_TIMEOUT_S", 0.05),
        patch.object(FakeZeroconf, "async_register_service", hang),
        caplog.at_level(logging.WARNING, logger=adv_mod.__name__),
    ):
        await a.advertise(8100)
    assert started.is_set()
    [zc] = fake_zeroconf.instances
    assert zc.closed
    assert a.advertising_port is None
    assert "TimeoutError" in caplog.text


async def test_withdraw_closes_responder(fake_zeroconf):
    a = _advertiser()
    await a.advertise(8100)
    await a.withdraw()

    [zc] = fake_zeroconf.instances
    assert zc.closed
    assert a.advertising_port is None
    # A later advertise starts a fresh responder.
    await a.advertise(8100)
    assert len(fake_zeroconf.instances) == 2


async def test_withdraw_when_idle_is_safe():
    await _advertiser().withdraw()


async def test_no_identity_yet_skips(fake_zeroconf):
    a = DisplayAdvertiser(host="0.0.0.0")
    await a.advertise(8100)
    assert fake_zeroconf.instances == []
    assert a.advertising_port is None


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
async def test_loopback_listener_skips_without_warning(fake_zeroconf, caplog, host):
    a = _advertiser(host=host)
    with caplog.at_level(logging.INFO, logger=adv_mod.__name__):
        await a.advertise(8100)
    assert fake_zeroconf.instances == []
    assert [r.levelno for r in caplog.records] == [logging.INFO]


async def test_no_lan_address_warns_once(fake_zeroconf, caplog):
    a = _advertiser()
    with patch.object(adv_mod.ifaddr, "get_adapters", return_value=[ADAPTERS[0]]):
        with caplog.at_level(logging.WARNING, logger=adv_mod.__name__):
            await a.advertise(8100)
            await a.advertise(8200)
    assert fake_zeroconf.instances == []
    assert len(caplog.records) == 1


async def test_multicast_failure_is_fail_soft_and_warns_once(fake_zeroconf, caplog):
    fake_zeroconf.fail_init = OSError("multicast unavailable")
    a = _advertiser()
    with caplog.at_level(logging.WARNING, logger=adv_mod.__name__):
        await a.advertise(8100)
        await a.advertise(8200)
    assert a.advertising_port is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "multicast unavailable" in warnings[0].getMessage()


async def test_register_failure_closes_responder(fake_zeroconf, caplog):
    fake_zeroconf.fail_register = RuntimeError("name conflict")
    a = _advertiser()
    with caplog.at_level(logging.WARNING, logger=adv_mod.__name__):
        await a.advertise(8100)
    [zc] = fake_zeroconf.instances
    assert zc.closed
    assert a.advertising_port is None

    # Recovery: the next attempt succeeds and warns again on a later failure.
    fake_zeroconf.fail_register = None
    await a.advertise(8100)
    assert a.advertising_port == 8100
    await a.withdraw()
    fake_zeroconf.fail_register = RuntimeError("again")
    with caplog.at_level(logging.WARNING, logger=adv_mod.__name__):
        await a.advertise(8100)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


async def test_warning_rearms_after_success(fake_zeroconf, caplog):
    a = _advertiser()
    fake_zeroconf.fail_init = OSError("first")
    with caplog.at_level(logging.WARNING, logger=adv_mod.__name__):
        await a.advertise(8100)
        fake_zeroconf.fail_init = None
        await a.advertise(8100)
        await a.withdraw()
        fake_zeroconf.fail_init = OSError("second")
        await a.advertise(8100)
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(messages) == 2


async def test_broadcast_future_outcome_is_retrieved(fake_zeroconf):
    loop = asyncio.get_running_loop()
    broadcast = loop.create_future()

    async def register(self, info, **kw):
        self.registered.append((info, kw))
        return broadcast

    a = _advertiser()
    with patch.object(FakeZeroconf, "async_register_service", register):
        await a.advertise(8100)
    broadcast.set_exception(RuntimeError("closed mid-broadcast"))
    await asyncio.sleep(0)
    assert broadcast._log_traceback is False  # retrieved by the callback


async def test_withdraw_failure_is_fail_soft(fake_zeroconf):
    a = _advertiser()
    await a.advertise(8100)
    [zc] = fake_zeroconf.instances

    async def boom() -> None:
        raise OSError("socket gone")

    zc.async_close = boom
    await a.withdraw()
    assert a.advertising_port is None


async def test_hostname_bind_without_lan_address_warns(fake_zeroconf, caplog):
    a = _advertiser(host="myhost.lan")
    with patch.object(adv_mod.ifaddr, "get_adapters", return_value=[ADAPTERS[0]]):
        with caplog.at_level(logging.WARNING, logger=adv_mod.__name__):
            await a.advertise(8100)
    assert fake_zeroconf.instances == []
    assert "myhost.lan" in caplog.text
