"""Tests for socialhome.platform.haos.adapter — HaosAdapter surface."""

from __future__ import annotations

import pytest

from socialhome.platform.adapter import Capability
from socialhome.platform.haos.adapter import HaosAdapter


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
        return None

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


def _build_haos_adapter(*, ha_client=None, options=None):
    return HaosAdapter(
        supervisor_url="http://supervisor",
        supervisor_token="t",
        data_dir="/tmp/x",
        options=options,
        ha_client=ha_client or _FakeHaClient(),
    )


# ─── Capabilities + provider wiring ────────────────────────────────────────


async def test_capabilities_always_include_ingress():
    adapter = _build_haos_adapter()
    caps = adapter.capabilities
    assert Capability.INGRESS in caps
    assert Capability.HA_PERSON_DIRECTORY in caps
    assert Capability.PUSH in caps


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


async def test_get_federation_base_returns_none_before_db_wired():
    adapter = _build_haos_adapter()
    assert await adapter.get_federation_base() is None


class _FakeDb:
    def __init__(self, value: str | None):
        self._value = value

    async def fetchone(self, sql, params):
        if self._value is None:
            return None
        return {"value": self._value}


async def test_get_federation_base_returns_none_when_unset():
    adapter = _build_haos_adapter()
    adapter._db = _FakeDb(None)
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
    adapter._db = _FakeDb("https://abc.ui.nabu.casa")
    assert (
        await adapter.get_federation_base()
        == "https://abc.ui.nabu.casa/api/socialhome/inbox"
    )


async def test_get_federation_base_strips_trailing_slash_before_append():
    adapter = _build_haos_adapter()
    adapter._db = _FakeDb("https://x.example/")
    assert (
        await adapter.get_federation_base() == "https://x.example/api/socialhome/inbox"
    )


async def test_get_federation_base_idempotent_if_already_appended():
    """No double-append if the integration ever pushes the full path."""
    adapter = _build_haos_adapter()
    adapter._db = _FakeDb("https://x.example/api/socialhome/inbox")
    assert (
        await adapter.get_federation_base() == "https://x.example/api/socialhome/inbox"
    )


async def test_get_federation_base_returns_none_for_blank_value():
    adapter = _build_haos_adapter()
    adapter._db = _FakeDb("   ")
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


class _IceHaClient(_FakeHaClient):
    """Adds the ``web_rtc/ice_servers`` WS reply the sync needs."""

    def __init__(self, result: list | None = None) -> None:
        super().__init__(config_response={})
        self._ice_result = [] if result is None else result

    async def ws_command(self, type_: str, **fields):  # noqa: ARG002
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
