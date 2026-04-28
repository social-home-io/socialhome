"""Tests for socialhome.platform.haos.adapter — HaosAdapter surface."""

from __future__ import annotations

from typing import AsyncIterable

import pytest

from socialhome.platform.adapter import Capability
from socialhome.platform.haos.adapter import HaIngressAuthProvider, HaosAdapter


# ─── Fake HaClient (mirrors the one in tests/platform/ha/test_adapter.py) ──


class _FakeHaClient:
    def __init__(
        self,
        *,
        states: list[dict] | None = None,
        state_by_entity: dict[str, dict] | None = None,
        config_response: dict | None = None,
    ) -> None:
        self._states = states or []
        self._state_by_entity = state_by_entity or {}
        self._config_response = config_response
        self.calls: list[tuple] = []

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

    async def fetch_path_bytes(self, *a, **kw):
        return None


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
    X-Ingress-User must NOT authenticate. Ingress is the only entry point."""
    adapter = _build_haos_adapter()
    user = await adapter.auth.authenticate(
        _FakeRequest(headers={"Authorization": "Bearer leaked-token"}),
    )
    assert user is None


async def test_ingress_auth_with_header_resolves_person():
    state = {"entity_id": "person.alice", "attributes": {"friendly_name": "Alice"}}
    client = _FakeHaClient(state_by_entity={"person.alice": state})
    adapter = _build_haos_adapter(ha_client=client)
    user = await adapter.auth.authenticate(
        _FakeRequest(headers={"X-Ingress-User": "alice"}),
    )
    assert user is not None
    assert user.username == "alice"


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


async def test_get_federation_base_strips_trailing_slash():
    adapter = _build_haos_adapter()
    adapter._db = _FakeDb("https://x.example/inbox/")
    assert await adapter.get_federation_base() == "https://x.example/inbox"


async def test_get_federation_base_returns_none_for_blank_value():
    adapter = _build_haos_adapter()
    adapter._db = _FakeDb("   ")
    assert await adapter.get_federation_base() is None


# ─── Picture fetch + extra services ─────────────────────────────────────────


async def test_fetch_entity_picture_bytes_returns_none_without_state():
    adapter = _build_haos_adapter()
    assert await adapter.fetch_entity_picture_bytes("ghost") is None


async def test_fetch_entity_picture_bytes_returns_none_without_attribute():
    state = {"entity_id": "person.alice", "attributes": {}}
    adapter = _build_haos_adapter(
        ha_client=_FakeHaClient(state_by_entity={"person.alice": state}),
    )
    assert await adapter.fetch_entity_picture_bytes("alice") is None


async def test_get_extra_services_empty_before_startup():
    adapter = _build_haos_adapter()
    assert adapter.get_extra_services() == {}
