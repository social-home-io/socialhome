"""Tests for socialhome.platform.haos.supervisor."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

from socialhome.platform.haos.supervisor import (
    AddonInfo,
    SupervisorClient,
    primary_lan_ipv4,
)

# pytest-homeassistant-custom-component (a transitive dev dep when this
# repo's venv is shared with the ha-integration repo) installs a
# socket-blocking guard; the aiohttp TestClient below needs a real
# port. CI doesn't install that plugin so this is a no-op there.
try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass


@pytest.fixture
async def sv_server(aiohttp_server):
    """In-process Supervisor fake. The discovery + addon-info endpoints
    are all this client talks to now — user discovery moved to HA Core's
    WS ``config/auth/list`` (see #297 / #298)."""
    captured: dict = {"discovery": []}

    async def discovery(request: web.Request) -> web.Response:
        body = await request.json()
        captured["discovery"].append(body)
        captured["discovery_header"] = request.headers.get("Authorization")
        return web.json_response({"result": "ok"})

    async def self_info(request: web.Request) -> web.Response:
        captured["self_info_header"] = request.headers.get("Authorization")
        return web.json_response(
            captured.get("self_info_response")
            or {
                "result": "ok",
                "data": {
                    "slug": "local_social_home",
                    "hostname": "local-social-home",
                    "ingress_port": 8099,
                    "ip_address": "172.30.33.0",
                },
            }
        )

    async def network_info(request: web.Request) -> web.Response:
        captured["network_info_header"] = request.headers.get("Authorization")
        status = captured.get("network_info_status", 200)
        return web.json_response(
            captured.get("network_info_response")
            or {"result": "ok", "data": {"interfaces": [_iface()]}},
            status=status,
        )

    app = web.Application()
    app.router.add_post("/discovery", discovery)
    app.router.add_get("/addons/self/info", self_info)
    app.router.add_get("/network/info", network_info)
    server = await aiohttp_server(app)
    return server, captured


@pytest.fixture
async def client(sv_server):
    import aiohttp

    server, _ = sv_server
    async with aiohttp.ClientSession() as session:
        yield SupervisorClient(
            session, str(server.make_url("")).rstrip("/"), "sv-token"
        )


async def test_push_discovery_true_on_2xx(client, sv_server):
    _, captured = sv_server
    ok = await client.push_discovery(
        {"service": "socialhome", "config": {"token": "abc"}},
    )
    assert ok is True
    assert captured["discovery"] == [
        {"service": "socialhome", "config": {"token": "abc"}},
    ]
    assert captured["discovery_header"] == "Bearer sv-token"


async def test_get_self_info_returns_typed_addon_info(client, sv_server):
    _, captured = sv_server
    info = await client.get_self_info()
    assert info == AddonInfo(hostname="local-social-home", ingress_port=8099)
    assert captured["self_info_header"] == "Bearer sv-token"


async def test_get_self_info_returns_none_when_response_missing_fields(
    client, sv_server
):
    """Partial responses (missing ``hostname`` or ``ingress_port``) surface as
    ``None`` rather than as a half-populated dataclass — the bootstrap
    treats ``None`` as "skip discovery this boot"."""
    _, captured = sv_server
    captured["self_info_response"] = {
        "result": "ok",
        "data": {"slug": "local_social_home", "hostname": "local-social-home"},
    }
    assert await client.get_self_info() is None


async def test_get_self_info_returns_none_when_supervisor_unreachable():
    """A bad URL surfaces as ``None`` (no exception escapes)."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        # Port 1 is reserved and refused on every sane host; the
        # client wraps the connection error into a warning + ``None``.
        sv = SupervisorClient(session, "http://127.0.0.1:1", "sv-token")
        assert await sv.get_self_info() is None


def test_addon_info_from_response_rejects_partial_payloads():
    """The parser returns ``None`` for any missing required field."""
    assert AddonInfo.from_response(None) is None
    assert AddonInfo.from_response({}) is None
    assert AddonInfo.from_response({"hostname": "h"}) is None
    assert AddonInfo.from_response({"ingress_port": 8099}) is None
    # Both present -> parses.
    info = AddonInfo.from_response({"hostname": "h", "ingress_port": "8099"})
    assert info == AddonInfo(hostname="h", ingress_port=8099)


# ── network map (household dashboard host port) ─────────────────────────
# Shapes from supervisor/api/apps.py ``APIApps.info_data`` (``network`` =
# ``App.ports``: container port → host port int, or null when unpublished).


def _info(**extra) -> AddonInfo:
    info = AddonInfo.from_response({"hostname": "h", "ingress_port": 8099, **extra})
    assert info is not None
    return info


def test_addon_info_reads_a_mapped_host_port():
    assert _info(network={"8100/tcp": 18100}).host_port("8100/tcp") == 18100


def test_addon_info_null_host_port_is_unpublished():
    assert _info(network={"8100/tcp": None}).host_port("8100/tcp") is None


def test_addon_info_missing_network_or_key_is_unpublished():
    assert _info().network is None
    assert _info().host_port("8100/tcp") is None
    assert _info(network={"1883/tcp": 1883}).host_port("8100/tcp") is None
    assert _info(network="junk").host_port("8100/tcp") is None


@pytest.mark.parametrize("value", [0, -1, True, "8100", 1.5])
def test_addon_info_malformed_host_port_is_unpublished(value):
    assert _info(network={"8100/tcp": value}).host_port("8100/tcp") is None


def test_addon_info_declares():
    """Key present (even ``null``) = the manifest declares the port; a
    missing key or no ``network`` at all = it doesn't."""
    assert _info(network={"8100/tcp": 8100}).declares("8100/tcp") is True
    assert _info(network={"8100/tcp": None}).declares("8100/tcp") is True
    assert _info(network={"1883/tcp": 1883}).declares("8100/tcp") is False
    assert _info(network=None).declares("8100/tcp") is False
    assert _info().declares("8100/tcp") is False
    assert _info(host_network=True).declares("8100/tcp") is True


def test_addon_info_host_network_publishes_the_container_port():
    info = _info(host_network=True, network=None)
    assert info.host_port("8100/tcp") == 8100
    assert info.host_port("nonsense") is None


async def test_get_self_info_parses_the_network_map(client, sv_server):
    _, captured = sv_server
    captured["self_info_response"] = {
        "result": "ok",
        "data": {
            "hostname": "local-social-home",
            "ingress_port": 8099,
            "network": {"8100/tcp": 8200},
            "host_network": False,
        },
    }
    info = await client.get_self_info()
    assert info is not None
    assert info.host_port("8100/tcp") == 8200


@pytest.mark.parametrize(
    "body", [{"result": "ok"}, {"result": "ok", "data": "x"}, {"data": []}]
)
async def test_get_self_info_none_without_a_data_object(
    client, sv_server, body, caplog
):
    _, captured = sv_server
    captured["self_info_response"] = body
    with caplog.at_level("WARNING"):
        assert await client.get_self_info() is None
    assert "no data object" in caplog.text


async def test_get_self_info_none_on_a_malformed_ingress_port(
    client, sv_server, caplog
):
    _, captured = sv_server
    captured["self_info_response"] = {
        "data": {"hostname": "h", "ingress_port": "eighty"}
    }
    with caplog.at_level("WARNING"):
        assert await client.get_self_info() is None
    assert "malformed" in caplog.text


# ─── /network/info + primary_lan_ipv4 ───────────────────────────────────────


def _iface(
    *,
    name: str = "eth0",
    primary: bool = True,
    connected: bool = True,
    enabled: bool = True,
    address: list | None = None,
) -> dict:
    """One ``interfaces[]`` entry shaped like Supervisor's
    ``interface_struct_v1`` / ``ip4config_struct``."""
    return {
        "interface": name,
        "type": "ethernet",
        "enabled": enabled,
        "connected": connected,
        "primary": primary,
        "ipv4": {
            "method": "auto",
            "address": ["192.168.1.10/24"] if address is None else address,
            "gateway": "192.168.1.1",
            "nameservers": ["192.168.1.1"],
            "ready": True,
        },
        "ipv6": {"method": "auto", "address": ["fd00::10/64"]},
    }


def test_primary_lan_ipv4_reads_the_primary_interface():
    data = {
        "interfaces": [
            _iface(name="wlan0", primary=False, address=["10.0.0.7/24"]),
            _iface(name="eth0", primary=True, address=["192.168.1.10/24"]),
        ]
    }
    assert primary_lan_ipv4(data) == "192.168.1.10"


def test_primary_lan_ipv4_falls_back_to_a_connected_non_primary():
    data = {
        "interfaces": [
            _iface(name="eth0", primary=True, connected=False),
            _iface(name="wlan0", primary=False, address=["10.0.0.7/24"]),
        ]
    }
    assert primary_lan_ipv4(data) == "10.0.0.7"


def test_primary_lan_ipv4_skips_disconnected_and_disabled():
    data = {
        "interfaces": [
            _iface(name="eth0", connected=False),
            _iface(name="eth1", primary=False, enabled=False),
        ]
    }
    assert primary_lan_ipv4(data) is None


def test_primary_lan_ipv4_skips_loopback_link_local_and_public():
    data = {
        "interfaces": [
            _iface(
                address=[
                    "127.0.0.1/8",
                    "169.254.3.4/16",
                    "8.8.8.8/24",
                    "not-an-ip",
                    17,
                    "172.16.5.9/16",
                ]
            )
        ]
    }
    assert primary_lan_ipv4(data) == "172.16.5.9"


def test_primary_lan_ipv4_skips_private_but_non_lan_ranges():
    """``is_private`` ranges no tablet can reach are not a LAN address."""
    data = {
        "interfaces": [
            _iface(
                address=[
                    "0.0.0.1/8",
                    "198.18.0.5/15",
                    "192.0.2.7/24",
                    "240.0.0.9/4",
                    "100.64.1.2/10",
                    "10.1.2.3/8",
                ]
            )
        ]
    }
    assert primary_lan_ipv4(data) == "10.1.2.3"


def test_primary_lan_ipv4_public_only_is_none():
    assert primary_lan_ipv4({"interfaces": [_iface(address=["8.8.8.8/24"])]}) is None


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"interfaces": None},
        {"interfaces": "eth0"},
        {"interfaces": ["eth0", None]},
        {"interfaces": [{"primary": True, "connected": True, "enabled": True}]},
        {
            "interfaces": [
                {"primary": True, "connected": True, "enabled": True, "ipv4": None}
            ]
        },
        {
            "interfaces": [
                {
                    "primary": True,
                    "connected": True,
                    "enabled": True,
                    "ipv4": {"address": "192.168.1.10/24"},
                }
            ]
        },
        {"interfaces": [_iface(address=[])]},
    ],
    ids=[
        "none",
        "empty",
        "interfaces-null",
        "interfaces-str",
        "entries-not-dicts",
        "no-ipv4",
        "ipv4-null",
        "address-not-list",
        "no-address",
    ],
)
def test_primary_lan_ipv4_tolerates_malformed_payloads(data):
    assert primary_lan_ipv4(data) is None


def test_primary_lan_ipv4_connected_must_be_true_not_truthy():
    """A string ``"false"`` must not count as connected."""
    iface = _iface()
    iface["connected"] = "false"
    assert primary_lan_ipv4({"interfaces": [iface]}) is None


async def test_get_network_info_unwraps_the_data_object(client, sv_server):
    _, captured = sv_server
    data = await client.get_network_info()
    assert data == {"interfaces": [_iface()]}
    assert captured["network_info_header"] == "Bearer sv-token"
    assert primary_lan_ipv4(data) == "192.168.1.10"


async def test_get_network_info_none_on_http_error(client, sv_server, caplog):
    _, captured = sv_server
    captured["network_info_status"] = 403
    captured["network_info_response"] = {"result": "error", "message": "no"}
    with caplog.at_level("WARNING"):
        assert await client.get_network_info() is None
    assert "/network/info failed" in caplog.text


@pytest.mark.parametrize(
    "body", [{"result": "ok"}, {"result": "ok", "data": "x"}, {"data": []}]
)
async def test_get_network_info_none_without_a_data_object(
    client, sv_server, body, caplog
):
    _, captured = sv_server
    captured["network_info_response"] = body
    with caplog.at_level("WARNING"):
        assert await client.get_network_info() is None
    assert "no data object" in caplog.text


async def test_get_network_info_none_when_supervisor_unreachable(caplog):
    async with aiohttp.ClientSession() as session:
        sv = SupervisorClient(session, "http://127.0.0.1:1", "sv-token")
        with caplog.at_level("WARNING"):
            assert await sv.get_network_info() is None
    assert "/network/info failed" in caplog.text
