"""Tests for socialhome.platform.haos.adapter — HaosAdapter surface."""

from __future__ import annotations

import pytest

from socialhome.domain.household_dashboard import DEFAULT_DASHBOARD_PORT
from socialhome.platform.adapter import Capability, DashboardHostPort
from socialhome.platform.ha.displays import HaDisplayDirectory
from socialhome.platform.ha.zones import HaZoneSource
from socialhome.platform.ha_home_location import HaHomeLocationSource
from socialhome.platform.haos import adapter as haos_adapter_module
from socialhome.platform.haos.adapter import HaosAdapter
from socialhome.platform.haos.supervisor import AddonInfo
from socialhome.repositories.instance_config_repo import SqliteInstanceConfigRepo

# ─── Fake HaClient (mirrors the one in tests/platform/ha/test_adapter.py) ──


class _FakeHaClient:
    def __init__(
        self,
        *,
        states: list[dict] | None = None,
        state_by_entity: dict[str, dict] | None = None,
        config_response: dict | None = None,
        auth_users: list[dict] | None = None,
        path_bytes: dict[str, bytes] | None = None,
    ) -> None:
        self._states = states or []
        self._state_by_entity = state_by_entity or {}
        self._config_response = config_response
        self._auth_users = auth_users or []
        self._path_bytes = path_bytes or {}
        self.calls: list[tuple] = []

    async def list_auth_users(self):
        self.calls.append(("list_auth_users",))
        return self._auth_users

    async def verify_token(self, token):
        self.calls.append(("verify_token", token))

    async def get_states(self):
        self.calls.append(("get_states",))
        return self._states

    async def get_state(self, entity_id):
        self.calls.append(("get_state", entity_id))
        return self._state_by_entity.get(entity_id)

    async def get_config(self):
        self.calls.append(("get_config",))
        return self._config_response

    async def call_service(self, *a, **kw):
        return None

    async def fire_event(self, *a, **kw):
        return True

    async def stream_stt(self, *a, **kw):
        return None

    async def fetch_path_bytes(self, path, *a, **kw):
        self.calls.append(("fetch_path_bytes", path))
        return self._path_bytes.get(path)


class _FakeRequest:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query = query or {}


def _build_haos_adapter(*, ha_client=None, options=None, supervisor_client=None):
    return HaosAdapter(
        supervisor_url="http://supervisor",
        supervisor_token="t",
        data_dir="/tmp/x",
        options=options,
        ha_client=ha_client or _FakeHaClient(),
        supervisor_client=supervisor_client,
    )


class _FakeSupervisor:
    """``get_self_info`` answers in turn from ``infos`` (an exception
    instance is raised)."""

    def __init__(self, *infos):
        self._infos = list(infos)
        self.calls = 0

    async def get_self_info(self):
        self.calls += 1
        item = self._infos.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeNetworkSupervisor:
    """``get_network_info`` answers in turn from ``answers`` (an exception
    instance is raised)."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls = 0

    async def get_network_info(self):
        self.calls += 1
        item = self._answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _network(address: str) -> dict:
    return {
        "interfaces": [
            {
                "interface": "eth0",
                "enabled": True,
                "connected": True,
                "primary": True,
                "ipv4": {"address": [f"{address}/24"]},
            }
        ]
    }


def _addon_info(**data):
    return AddonInfo.from_response({"hostname": "h", "ingress_port": 8099, **data})


# ─── Capabilities + provider wiring ────────────────────────────────────────


async def test_capabilities_always_include_ingress():
    adapter = _build_haos_adapter()
    caps = adapter.capabilities
    assert Capability.INGRESS in caps
    assert Capability.HA_PERSON_DIRECTORY in caps
    assert Capability.PUSH in caps


async def test_capabilities_omit_mdns_advertise():
    """The Home Assistant App container is on the Supervisor's bridge
    network (no ``host_network``) — an mDNS announcement can't reach the
    LAN, so the dashboard doesn't try."""
    assert Capability.MDNS_ADVERTISE not in _build_haos_adapter().capabilities


async def test_zone_source_wired_with_zone_sync_capability():
    """Same HA zone source as ha mode — both talk to HA Core."""
    adapter = _build_haos_adapter()
    assert isinstance(adapter.zones, HaZoneSource)
    assert Capability.ZONE_SYNC in adapter.capabilities


async def test_home_location_source_wired_with_its_capability():
    """Same HA home as ha mode — the admin setting shows it read-only."""
    adapter = _build_haos_adapter()
    assert isinstance(adapter.home_location, HaHomeLocationSource)
    assert Capability.HOME_LOCATION_SYNC in adapter.capabilities


async def test_capabilities_add_stt_when_entity_configured():
    adapter = _build_haos_adapter(options={"stt_entity_id": "stt.cloud"})
    assert Capability.STT in adapter.capabilities


async def test_capabilities_omit_stt_without_entity_id():
    adapter = _build_haos_adapter()
    assert Capability.STT not in adapter.capabilities


async def test_client_property_raises_before_startup():
    adapter = HaosAdapter(
        supervisor_url="http://supervisor",
        supervisor_token="t",
        data_dir="/tmp/x",
    )
    with pytest.raises(RuntimeError, match="on_startup"):
        _ = adapter._client


# ─── HaIngressAuthProvider — header trust, no bearer fallback ───────────────


async def test_ingress_auth_no_header_returns_none():
    adapter = _build_haos_adapter()
    user = await adapter.auth.authenticate(_FakeRequest())
    assert user is None


async def test_ingress_auth_with_bearer_only_returns_none():
    """Critical security invariant: a bearer token without
    X-Remote-User-Name must NOT authenticate. Ingress is the only entry point."""
    adapter = _build_haos_adapter()
    user = await adapter.auth.authenticate(
        _FakeRequest(headers={"Authorization": "Bearer leaked-token"}),
    )
    assert user is None


async def test_ingress_auth_with_header_resolves_via_auth_list():
    """X-Remote-User-Name → ``config/auth/list`` lookup. The auth
    username, not the person entity slug, is what HA ingress forwards
    in the header (#297)."""
    client = _FakeHaClient(
        auth_users=[
            {"id": "abc", "username": "alice", "name": "Alice", "is_owner": True},
        ],
    )
    adapter = _build_haos_adapter(ha_client=client)
    user = await adapter.auth.authenticate(
        _FakeRequest(
            headers={
                "X-Hass-Source": "core.ingress",
                "X-Remote-User-Name": "alice",
            }
        ),
    )
    assert user is not None
    assert user.username == "alice"
    assert user.display_name == "Alice"


async def test_ingress_auth_username_without_hass_source_rejected():
    """A request carrying ``X-Remote-User-Name`` but missing the
    ``X-Hass-Source: core.ingress`` marker did not come through Core's
    ingress proxy — the HAOS adapter must refuse to authenticate it
    regardless of whether the username exists locally."""
    client = _FakeHaClient(
        auth_users=[
            {"id": "abc", "username": "alice", "name": "Alice"},
        ],
    )
    adapter = _build_haos_adapter(ha_client=client)
    user = await adapter.auth.authenticate(
        _FakeRequest(headers={"X-Remote-User-Name": "alice"}),
    )
    assert user is None


# ─── Instance config + location override ────────────────────────────────────


async def test_get_instance_config_falls_back_when_ha_returns_none():
    adapter = _build_haos_adapter()
    cfg = await adapter.get_instance_config()
    assert cfg.location_name == "Home"
    assert cfg.latitude == 0.0
    assert cfg.longitude == 0.0


async def test_get_instance_config_maps_ha_response():
    adapter = _build_haos_adapter(
        ha_client=_FakeHaClient(
            config_response={
                "location_name": "Cottage",
                "latitude": 52.37,
                "longitude": 4.89,
                "time_zone": "Europe/Amsterdam",
                "currency": "EUR",
            }
        ),
    )
    cfg = await adapter.get_instance_config()
    assert cfg.location_name == "Cottage"
    assert cfg.latitude == 52.37
    assert cfg.time_zone == "Europe/Amsterdam"
    assert cfg.currency == "EUR"


async def test_update_location_truncates_coords_keeps_tz():
    adapter = _build_haos_adapter(
        ha_client=_FakeHaClient(
            config_response={
                "location_name": "Home",
                "latitude": 0,
                "longitude": 0,
                "time_zone": "Europe/Amsterdam",
                "currency": "EUR",
            }
        ),
    )
    cfg = await adapter.update_location(51.123456, 4.987654, "Cottage")
    assert cfg.location_name == "Cottage"
    assert cfg.latitude == 51.1235
    assert cfg.longitude == 4.9877
    assert cfg.time_zone == "Europe/Amsterdam"


# ─── Federation base lookup (cached in instance_config kv table) ───────────


async def test_get_federation_base_returns_none_before_repo_wired():
    adapter = _build_haos_adapter()
    assert await adapter.get_federation_base() is None


class _FakeInstanceConfig:
    def __init__(self, value: str | None):
        self._value = value

    async def get(self, key):
        return self._value


async def test_get_federation_base_returns_none_when_unset():
    adapter = _build_haos_adapter()
    adapter._instance_config = _FakeInstanceConfig(None)
    assert await adapter.get_federation_base() is None


async def test_get_federation_base_appends_inbox_path():
    """The HA integration pushes the bare external URL (Nabu Casa or
    admin-set ``external_url``) and registers an HA Core HTTP view at
    ``/api/socialhome/inbox/{inbox_id}`` that forwards into the
    addon. Peers POST to ``{pushed_url}/api/socialhome/inbox/...`` so
    the adapter has to splice that path onto the pushed base — the
    pairing coordinator's ``{base}/{secret_id}`` concat assumes the
    full inbox base already."""
    adapter = _build_haos_adapter()
    adapter._instance_config = _FakeInstanceConfig("https://abc.ui.nabu.casa")
    assert (
        await adapter.get_federation_base()
        == "https://abc.ui.nabu.casa/api/socialhome/inbox"
    )


async def test_get_federation_base_strips_trailing_slash_before_append():
    adapter = _build_haos_adapter()
    adapter._instance_config = _FakeInstanceConfig("https://x.example/")
    assert (
        await adapter.get_federation_base() == "https://x.example/api/socialhome/inbox"
    )


async def test_get_federation_base_idempotent_if_already_appended():
    """No double-append if the integration ever pushes the full path."""
    adapter = _build_haos_adapter()
    adapter._instance_config = _FakeInstanceConfig(
        "https://x.example/api/socialhome/inbox"
    )
    assert (
        await adapter.get_federation_base() == "https://x.example/api/socialhome/inbox"
    )


async def test_get_federation_base_returns_none_for_blank_value():
    adapter = _build_haos_adapter()
    adapter._instance_config = _FakeInstanceConfig("   ")
    assert await adapter.get_federation_base() is None


# ─── Picture fetch + extra services ─────────────────────────────────────────


async def test_fetch_entity_picture_bytes_returns_none_for_unknown_user():
    """No matching auth user → no person to look up → ``None``."""
    adapter = _build_haos_adapter()
    assert await adapter.fetch_entity_picture_bytes("ghost") is None


async def test_fetch_entity_picture_bytes_returns_none_without_attribute():
    """User exists in ``config/auth/list`` but the matched person.* has
    no ``entity_picture`` attribute — degrade silently."""
    client = _FakeHaClient(
        auth_users=[
            {"id": "uid-alice", "username": "alice", "name": "Alice"},
        ],
        states=[
            {
                "entity_id": "person.alice_v2",
                "attributes": {"user_id": "uid-alice"},
            },
        ],
    )
    adapter = _build_haos_adapter(ha_client=client)
    assert await adapter.fetch_entity_picture_bytes("alice") is None


async def test_fetch_entity_picture_bytes_walks_via_user_id():
    """Auth username → user_id → person.* via ``attributes.user_id``.
    Resilient to display-name renames that shift the entity slug
    (#297). The picture URL is fetched through the integration token."""
    client = _FakeHaClient(
        auth_users=[
            {"id": "uid-alice", "username": "alice", "name": "Alice"},
        ],
        states=[
            # The matching person entity uses a slug derived from the
            # display name, NOT the auth username — exactly the
            # mismatch that broke the previous lookup.
            {
                "entity_id": "person.alice_v2",
                "attributes": {
                    "user_id": "uid-alice",
                    "entity_picture": "/api/image/serve/alice-pic",
                },
            },
        ],
        path_bytes={"/api/image/serve/alice-pic": b"png-bytes"},
    )
    adapter = _build_haos_adapter(ha_client=client)
    assert await adapter.fetch_entity_picture_bytes("alice") == b"png-bytes"


async def test_get_extra_services_empty_before_startup():
    adapter = _build_haos_adapter()
    assert adapter.get_extra_services() == {}


# ─── ICE-prime gate ─────────────────────────────────────────────────────────


def test_provides_ice_servers_is_true():
    """haos pulls HA Core's ``web_rtc/ice_servers`` after startup, so the
    federation transport should hold its first handshake briefly rather
    than building a STUN-only peer that can never relay."""
    assert _build_haos_adapter().provides_ice_servers is True


def test_fixed_dashboard_port_is_the_container_port():
    """HA Supervisor owns the host port mapping in the Home Assistant App,
    so the dashboard always listens on the default container port."""
    assert _build_haos_adapter().fixed_dashboard_port == DEFAULT_DASHBOARD_PORT


# ─── dashboard_host_port (Supervisor ``network`` map) ───────────────────────


async def test_dashboard_host_port_reads_the_mapped_port():
    sv = _FakeSupervisor(_addon_info(network={"8100/tcp": 18100}))
    adapter = _build_haos_adapter(supervisor_client=sv)
    assert await adapter.dashboard_host_port() == DashboardHostPort(18100)
    # Cached: a new mapping only applies after Supervisor restarts the App.
    assert await adapter.dashboard_host_port() == DashboardHostPort(18100)
    assert sv.calls == 1


async def test_dashboard_host_port_declared_but_not_mapped():
    """The App declares ``8100/tcp`` but the user hasn't set a host port:
    the admin can fix that under the App's Network settings."""
    adapter = _build_haos_adapter(
        supervisor_client=_FakeSupervisor(_addon_info(network={"8100/tcp": None}))
    )
    result = await adapter.dashboard_host_port()
    assert result == DashboardHostPort(None, declared=True)
    assert result.mapped is False


@pytest.mark.parametrize(
    "data",
    [{"network": {"8099/tcp": 8099}}, {"network": None}, {}],
    ids=["missing-key", "network-null", "no-network"],
)
async def test_dashboard_host_port_undeclared_logs_once_at_info(data, caplog):
    """An App release whose manifest doesn't declare ``8100/tcp`` — an
    expected state, nothing the admin can change: INFO, once."""
    sv = _FakeSupervisor(_addon_info(**data))
    adapter = _build_haos_adapter(supervisor_client=sv)
    with caplog.at_level("INFO"):
        first = await adapter.dashboard_host_port()
        second = await adapter.dashboard_host_port()
    assert first == second == DashboardHostPort(None, declared=False)
    assert sv.calls == 1
    records = [r for r in caplog.records if "does not declare" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelname == "INFO"


async def test_dashboard_host_port_supervisor_failure_degrades(caplog):
    """``get_self_info`` returns ``None`` on an HTTP error (it logs that
    itself); the adapter warns, answers "unknown" and asks again later."""
    sv = _FakeSupervisor(None, _addon_info(network={"8100/tcp": 8100}))
    adapter = _build_haos_adapter(supervisor_client=sv)
    with caplog.at_level("WARNING"):
        assert await adapter.dashboard_host_port() is None
    assert "falls back to the container port" in caplog.text
    assert await adapter.dashboard_host_port() == DashboardHostPort(8100)
    assert sv.calls == 2


async def test_dashboard_host_port_unexpected_error_degrades(caplog):
    adapter = _build_haos_adapter(
        supervisor_client=_FakeSupervisor(RuntimeError("boom"))
    )
    with caplog.at_level("WARNING"):
        assert await adapter.dashboard_host_port() is None
    assert "boom" in caplog.text


async def test_dashboard_host_port_before_startup_is_unknown(caplog):
    with caplog.at_level("WARNING"):
        assert await _build_haos_adapter().dashboard_host_port() is None
    assert "before on_startup" in caplog.text


# ─── dashboard_lan_host (Supervisor ``/network/info``) ──────────────────────


async def test_dashboard_lan_host_reads_the_primary_ipv4():
    sv = _FakeNetworkSupervisor(_network("192.168.1.10"), _network("192.168.1.11"))
    adapter = _build_haos_adapter(supervisor_client=sv)
    assert await adapter.dashboard_lan_host() == "192.168.1.10"
    # Not cached: a DHCP lease can move the host, and only the admin page asks.
    assert await adapter.dashboard_lan_host() == "192.168.1.11"
    assert sv.calls == 2


async def test_dashboard_lan_host_supervisor_failure_is_unknown(caplog):
    """``get_network_info`` returns ``None`` on an HTTP error and has
    logged it itself — the adapter adds no misleading "no private LAN
    IPv4" warning on top."""
    adapter = _build_haos_adapter(supervisor_client=_FakeNetworkSupervisor(None))
    with caplog.at_level("WARNING"):
        assert await adapter.dashboard_lan_host() is None
    assert "no private LAN IPv4" not in caplog.text


async def test_dashboard_lan_host_no_private_address_warns(caplog):
    adapter = _build_haos_adapter(
        supervisor_client=_FakeNetworkSupervisor(_network("8.8.8.8"))
    )
    with caplog.at_level("WARNING"):
        assert await adapter.dashboard_lan_host() is None
    assert any(
        r.levelname == "WARNING" and "LAN" in r.getMessage() for r in caplog.records
    )


async def test_dashboard_lan_host_unexpected_error_degrades(caplog):
    adapter = _build_haos_adapter(
        supervisor_client=_FakeNetworkSupervisor(RuntimeError("boom"))
    )
    with caplog.at_level("WARNING"):
        assert await adapter.dashboard_lan_host() is None
    assert "boom" in caplog.text


async def test_dashboard_lan_host_before_startup_is_unknown(caplog):
    with caplog.at_level("WARNING"):
        assert await _build_haos_adapter().dashboard_lan_host() is None
    assert "before on_startup" in caplog.text


def test_does_not_honour_a_manual_federation_base():
    """The Home Assistant App sits behind ingress: peers reach it only
    through the integration's inbox forwarder, so the integration owns the
    federation base and an admin-typed one is never used."""
    assert _build_haos_adapter().honours_manual_federation_base is False


class _IceHaClient(_FakeHaClient):
    """Adds the ``web_rtc/ice_servers`` WS reply the sync needs."""

    def __init__(self, result: list | None = None) -> None:
        super().__init__(config_response={})
        self._ice_result = [] if result is None else result

    async def ws_command(self, type_: str, **fields):
        return {"result": self._ice_result}


class _PrimingFederation:
    def __init__(self) -> None:
        self.applied: list[list[dict]] = []
        self.primed = 0

    def set_ice_servers(self, servers: list[dict]) -> None:
        self.applied.append(servers)

    def mark_ice_primed(self) -> None:
        self.primed += 1


async def test_on_startup_releases_the_ice_gate_after_the_first_attempt(
    tmp_path,
    monkeypatch,
):
    """HA returning nothing usable is an ordinary deployment (no cloud
    subscription). The gate must still open, or the first outbound
    federation send stalls for the full ICE-prime timeout."""
    import asyncio

    import aiohttp
    from aiohttp import web

    from socialhome.app_keys import (
        db_key,
        event_bus_key,
        federation_service_key,
        http_session_key,
        instance_config_repo_key,
    )
    from socialhome.db.database import AsyncDatabase
    from socialhome.infrastructure.event_bus import EventBus
    from socialhome.platform.haos import adapter as haos_adapter_mod

    class _NoopBootstrap:
        def __init__(self, **kwargs):
            pass

        async def run(self):
            return None

    monkeypatch.setattr(haos_adapter_mod, "HaBootstrap", _NoopBootstrap)

    db = AsyncDatabase(str(tmp_path / "t.db"))
    await db.startup()
    async with aiohttp.ClientSession() as session:
        app = web.Application()
        app[db_key] = db
        app[instance_config_repo_key] = SqliteInstanceConfigRepo(db)
        app[event_bus_key] = EventBus()
        app[http_session_key] = session
        fed = _PrimingFederation()
        app[federation_service_key] = fed
        adapter = _build_haos_adapter(ha_client=_IceHaClient(result=[]))
        await adapter.on_startup(app)
        try:
            for _ in range(100):
                await asyncio.sleep(0.005)
                if fed.primed:
                    break
            assert fed.primed == 1, "mark_ice_primed never reached the fed service"
            assert fed.applied == []
        finally:
            await adapter.on_cleanup(app)
    await db.shutdown()


async def test_display_directory_wired_with_its_capability():
    """Same HA panel directory as ha mode — and the one way the App finds
    panels, since its mDNS can't reach the LAN."""
    adapter = _build_haos_adapter()
    assert isinstance(adapter.displays, HaDisplayDirectory)
    assert Capability.HA_DISPLAY_DISCOVERY in adapter.capabilities


# ─── Automation events + Companion push tap URL ────────────────────────────


def test_capabilities_include_automation_events():
    """D14: the SPA offers the HA automation opt-in only with this."""
    assert Capability.AUTOMATION_EVENTS in _build_haos_adapter().capabilities


async def test_companion_click_url_is_the_app_panel():
    """A relative Companion URL opens in HA's frontend; HA registers the
    App's panel at ``/<slug>`` (``addon_panel._register_panel``)."""
    sv = _FakeSupervisor(_addon_info(slug="a0d7b954_socialhome", ingress_panel=True))
    adapter = _build_haos_adapter(supervisor_client=sv)
    assert await adapter.companion_click_url("/dms/c1") == "/a0d7b954_socialhome"
    # Cached: one Supervisor read per TTL, not per push.
    assert await adapter.companion_click_url("/post/p1") == "/a0d7b954_socialhome"
    assert sv.calls == 1


async def test_companion_click_url_none_without_the_sidebar_panel():
    """No panel registered in HA → no URL (the tap opens the Companion app)
    rather than one HA answers with "not found"."""
    sv = _FakeSupervisor(_addon_info(slug="local_socialhome", ingress_panel=False))
    adapter = _build_haos_adapter(supervisor_client=sv)
    assert await adapter.companion_click_url("/dms/c1") is None


async def test_companion_click_url_rereads_after_the_ttl(monkeypatch):
    """An admin can turn the sidebar panel on without restarting the App."""
    sv = _FakeSupervisor(
        _addon_info(slug="local_socialhome", ingress_panel=False),
        _addon_info(slug="local_socialhome", ingress_panel=True),
    )
    adapter = _build_haos_adapter(supervisor_client=sv)
    clock = [1000.0]
    monkeypatch.setattr(haos_adapter_module.time, "monotonic", lambda: clock[0])
    assert await adapter.companion_click_url("/") is None
    clock[0] += 301
    assert await adapter.companion_click_url("/") == "/local_socialhome"


async def test_companion_click_url_supervisor_failure_is_not_cached(caplog):
    sv = _FakeSupervisor(
        RuntimeError("down"),
        None,
        _addon_info(slug="local_socialhome", ingress_panel=True),
    )
    adapter = _build_haos_adapter(supervisor_client=sv)
    with caplog.at_level("WARNING"):
        assert await adapter.companion_click_url("/") is None
    assert any("push tap URL" in r.getMessage() for r in caplog.records)
    assert await adapter.companion_click_url("/") is None
    assert await adapter.companion_click_url("/") == "/local_socialhome"


async def test_companion_click_url_before_startup_is_none():
    assert await _build_haos_adapter().companion_click_url("/") is None
