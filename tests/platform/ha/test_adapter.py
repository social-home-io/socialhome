"""Tests for socialhome.platform.ha.adapter — fake HaClient injection."""

from __future__ import annotations

from typing import AsyncIterable

import aiohttp
import pytest
from aiohttp import web

from socialhome.app_keys import db_key, event_bus_key, http_session_key
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.infrastructure.event_bus import EventBus
from socialhome.platform.ha.adapter import HomeAssistantAdapter


# ─── Fake HaClient ───────────────────────────────────────────────────────


class _FakeHaClient:
    """Minimal :class:`HaClient` stand-in for adapter unit tests.

    Records every call and returns canned responses. Keep the surface
    narrow — the real ``HaClient`` is covered by ``test_ha_client``.
    """

    def __init__(
        self,
        *,
        verify_token_response: dict | None = None,
        states: list[dict] | None = None,
        state_by_entity: dict[str, dict] | None = None,
        config_response: dict | None = None,
        call_service_response: dict | None = None,
        fire_event_result: bool = True,
        stt_response: dict | None = None,
    ) -> None:
        self._verify_token_response = verify_token_response
        self._states = states if states is not None else []
        self._state_by_entity = state_by_entity or {}
        self._config_response = config_response
        self._call_service_response = call_service_response
        self._fire_event_result = fire_event_result
        self._stt_response = stt_response
        self.calls: list[tuple] = []

    async def verify_token(self, token: str) -> dict | None:
        self.calls.append(("verify_token", token))
        return self._verify_token_response

    async def get_states(self) -> list[dict]:
        self.calls.append(("get_states",))
        return self._states

    async def get_state(self, entity_id: str) -> dict | None:
        self.calls.append(("get_state", entity_id))
        return self._state_by_entity.get(entity_id)

    async def get_config(self) -> dict | None:
        self.calls.append(("get_config",))
        return self._config_response

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict | None = None,
        *,
        return_response: bool = False,
    ) -> dict | None:
        self.calls.append(
            ("call_service", domain, service, data, return_response),
        )
        return self._call_service_response

    async def fire_event(self, event_type: str, data: dict | None = None) -> bool:
        self.calls.append(("fire_event", event_type, data))
        return self._fire_event_result

    async def stream_stt(
        self,
        entity_id: str,
        audio: AsyncIterable[bytes],
        *,
        language: str,
        sample_rate: int,
        channels: int,
    ) -> dict | None:
        chunks = [chunk async for chunk in audio]
        self.calls.append(
            ("stream_stt", entity_id, chunks, language, sample_rate, channels),
        )
        return self._stt_response


# ─── Adapter construction helper ─────────────────────────────────────────


def _build_adapter(
    *,
    client: _FakeHaClient,
    options: dict | None = None,
) -> HomeAssistantAdapter:
    return HomeAssistantAdapter(
        ha_url="http://ha-test:8123",
        ha_token="test-token",
        data_dir="/tmp/irrelevant",
        options=options,
        ha_client=client,
    )


class _FakeRequest:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query = query or {}


# ─── Authentication ──────────────────────────────────────────────────────


async def test_authenticate_no_headers():
    adapter = _build_adapter(client=_FakeHaClient())
    assert await adapter.authenticate(_FakeRequest()) is None


async def test_authenticate_ingress_user_looks_up_person_entity():
    state = {
        "entity_id": "person.pascal",
        "attributes": {"friendly_name": "Pascal"},
        "state": "home",
    }
    client = _FakeHaClient(state_by_entity={"person.pascal": state})
    adapter = _build_adapter(client=client)

    user = await adapter.authenticate(
        _FakeRequest(headers={"X-Ingress-User": "pascal"}),
    )
    assert user is not None and user.username == "pascal"
    assert ("get_state", "person.pascal") in client.calls


async def test_authenticate_bearer_falls_back_when_no_ingress_header():
    client = _FakeHaClient(
        verify_token_response={"message": "OK", "username": "api"},
    )
    adapter = _build_adapter(client=client)
    user = await adapter.authenticate(
        _FakeRequest(headers={"Authorization": "Bearer tok123"}),
    )
    assert user is not None
    assert ("verify_token", "tok123") in client.calls


async def test_authenticate_bearer_valid_returns_user():
    client = _FakeHaClient(
        verify_token_response={"message": "OK", "username": "admin"},
    )
    adapter = _build_adapter(client=client)
    user = await adapter.authenticate_bearer("tok")
    assert user is not None
    assert user.username == "admin"
    assert user.is_admin is True


async def test_authenticate_bearer_invalid_returns_none():
    adapter = _build_adapter(client=_FakeHaClient(verify_token_response=None))
    assert await adapter.authenticate_bearer("bad") is None


# ─── User listing ────────────────────────────────────────────────────────


async def test_list_external_users_filters_person_entities():
    states = [
        {"entity_id": "person.pascal", "attributes": {"friendly_name": "Pascal"}},
        {"entity_id": "light.kitchen", "attributes": {}},
        {"entity_id": "person.alex", "attributes": {}},
    ]
    adapter = _build_adapter(client=_FakeHaClient(states=states))
    users = await adapter.list_external_users()
    assert [u.username for u in users] == ["pascal", "alex"]


async def test_list_external_users_empty_on_client_error():
    adapter = _build_adapter(client=_FakeHaClient(states=[]))
    assert await adapter.list_external_users() == []


async def test_get_external_user_found():
    state = {"entity_id": "person.pascal", "attributes": {}}
    adapter = _build_adapter(
        client=_FakeHaClient(state_by_entity={"person.pascal": state}),
    )
    user = await adapter.get_external_user("pascal")
    assert user is not None and user.username == "pascal"


async def test_get_external_user_not_found():
    adapter = _build_adapter(client=_FakeHaClient())
    assert await adapter.get_external_user("nobody") is None


# ─── Instance config ─────────────────────────────────────────────────────


async def test_get_instance_config_maps_ha_response():
    cfg = {
        "location_name": "Home",
        "latitude": 52.37,
        "longitude": 4.89,
        "time_zone": "Europe/Amsterdam",
        "currency": "EUR",
    }
    adapter = _build_adapter(client=_FakeHaClient(config_response=cfg))
    config = await adapter.get_instance_config()
    assert config.location_name == "Home"
    assert config.currency == "EUR"


async def test_get_instance_config_fallback_on_client_error():
    adapter = _build_adapter(client=_FakeHaClient(config_response=None))
    config = await adapter.get_instance_config()
    assert config.location_name == "Home"
    assert config.currency == "USD"


# ─── Push + events ───────────────────────────────────────────────────────


async def test_send_push_targets_mobile_app_service():
    client = _FakeHaClient()
    adapter = _build_adapter(client=client)
    from socialhome.platform.adapter import ExternalUser

    user = ExternalUser(
        username="pascal",
        display_name="P",
        picture_url=None,
        is_admin=False,
    )
    await adapter.send_push(user, "title", "message", data={"x": 1})
    assert any(
        c[0] == "call_service"
        and c[1] == "notify"
        and c[2] == "mobile_app_pascal"
        and c[3] == {"title": "title", "message": "message", "data": {"x": 1}}
        for c in client.calls
    )


async def test_fire_event_delegates():
    client = _FakeHaClient(fire_event_result=True)
    adapter = _build_adapter(client=client)
    ok = await adapter.fire_event("socialhome.post_created", {"id": "p1"})
    assert ok is True
    assert ("fire_event", "socialhome.post_created", {"id": "p1"}) in client.calls


# ─── STT ─────────────────────────────────────────────────────────────────


async def test_supports_stt_requires_entity_id():
    assert _build_adapter(client=_FakeHaClient()).supports_stt is False
    enabled = _build_adapter(
        client=_FakeHaClient(),
        options={"stt_entity_id": "stt.whisper"},
    )
    assert enabled.supports_stt is True


async def test_transcribe_audio_raises_without_entity_id():
    adapter = _build_adapter(client=_FakeHaClient())
    with pytest.raises(NotImplementedError):
        await adapter.transcribe_audio(b"audio")


async def test_stream_transcribe_audio_success():
    client = _FakeHaClient(
        stt_response={"result": "success", "text": "hello world"},
    )
    adapter = _build_adapter(
        client=client,
        options={"stt_entity_id": "stt.whisper"},
    )

    async def _audio():
        yield b"frame1"
        yield b"frame2"

    text = await adapter.stream_transcribe_audio(_audio(), language="en")
    assert text == "hello world"
    # Client called with collected chunks + metadata.
    stt_calls = [c for c in client.calls if c[0] == "stream_stt"]
    assert len(stt_calls) == 1
    assert stt_calls[0][1] == "stt.whisper"
    assert stt_calls[0][2] == [b"frame1", b"frame2"]
    assert stt_calls[0][3] == "en"


async def test_stream_transcribe_audio_empty_on_error_payload():
    client = _FakeHaClient(stt_response={"result": "error"})
    adapter = _build_adapter(
        client=client,
        options={"stt_entity_id": "stt.whisper"},
    )

    async def _audio():
        yield b"x"

    text = await adapter.stream_transcribe_audio(_audio())
    assert text == ""


async def test_transcribe_audio_delegates_to_stream():
    client = _FakeHaClient(
        stt_response={"result": "success", "text": "hi"},
    )
    adapter = _build_adapter(
        client=client,
        options={"stt_entity_id": "stt.whisper"},
    )
    assert await adapter.transcribe_audio(b"buffered-bytes", language="de") == "hi"


# ─── AI task ─────────────────────────────────────────────────────────────


async def test_generate_ai_data_unwraps_service_response():
    client = _FakeHaClient(
        call_service_response={
            "changed_states": [],
            "service_response": {"data": "BEGIN:VCALENDAR..."},
        },
    )
    adapter = _build_adapter(client=client)
    result = await adapter.generate_ai_data(task_name="t", instructions="go")
    assert result == "BEGIN:VCALENDAR..."


async def test_generate_ai_data_sends_entity_id_when_configured():
    client = _FakeHaClient(
        call_service_response={"service_response": {"data": ""}},
    )
    adapter = _build_adapter(
        client=client,
        options={"ai_task_entity_id": "ai_task.openai"},
    )
    await adapter.generate_ai_data(task_name="t", instructions="go")
    (_, domain, service, body, return_response) = next(
        c for c in client.calls if c[0] == "call_service"
    )
    assert domain == "ai_task"
    assert service == "generate_data"
    assert return_response is True
    assert body == {
        "task_name": "t",
        "instructions": "go",
        "entity_id": "ai_task.openai",
    }


async def test_generate_ai_data_returns_empty_on_client_error():
    client = _FakeHaClient(call_service_response=None)
    adapter = _build_adapter(client=client)
    assert await adapter.generate_ai_data(task_name="t", instructions="go") == ""


# ─── update_location ─────────────────────────────────────────────────────


async def test_update_location_truncates_coords():
    cfg = {
        "location_name": "Home",
        "latitude": 52.37,
        "longitude": 4.89,
        "time_zone": "Europe/Amsterdam",
        "currency": "EUR",
    }
    adapter = _build_adapter(client=_FakeHaClient(config_response=cfg))
    updated = await adapter.update_location(51.123456, 4.987654, "Cottage")
    assert updated.location_name == "Cottage"
    assert updated.latitude == 51.1235
    assert updated.longitude == 4.9877
    # HA-supplied fields carry through.
    assert updated.time_zone == "Europe/Amsterdam"


# ─── Uninitialised adapter guard ─────────────────────────────────────────


async def test_adapter_raises_before_on_startup_when_client_not_injected():
    adapter = HomeAssistantAdapter(
        ha_url="http://ha",
        ha_token="t",
        data_dir="/tmp/unused",
    )
    with pytest.raises(RuntimeError, match="on_startup"):
        await adapter.list_external_users()


async def test_on_startup_does_not_provision_users(tmp_path):
    """HaAdapter (Core mode) never bootstraps users on startup —
    that's haos territory."""
    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    app = web.Application()
    app[db_key] = db
    app[event_bus_key] = EventBus()
    async with aiohttp.ClientSession() as session:
        app[http_session_key] = session
        adapter = HomeAssistantAdapter(
            ha_url="http://ha.local:8123",
            ha_token="",
            data_dir=str(tmp_path),
        )
        await adapter.on_startup(app)
        assert await db.fetchval("SELECT COUNT(*) FROM users") == 0
    await db.shutdown()


# ─── get_federation_base (§11) ────────────────────────────────────────────


async def test_get_federation_base_returns_none_before_integration_push(tmp_path):
    """Pre-startup (no db wired) returns None."""
    adapter = _build_adapter(client=_FakeHaClient())
    assert await adapter.get_federation_base() is None


async def test_get_federation_base_reads_instance_config(tmp_path):
    """Post-startup, returns whatever the HA integration wrote."""
    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO instance_config(key, value) VALUES(?, ?)",
        (
            "ha_federation_base",
            "https://abc.ui.nabu.casa/api/social_home/inbox",
        ),
    )

    app = web.Application()
    app[db_key] = db
    app[event_bus_key] = EventBus()
    async with aiohttp.ClientSession() as session:
        app[http_session_key] = session
        adapter = HomeAssistantAdapter(
            ha_url="http://ha.local:8123",
            ha_token="",
            data_dir=str(tmp_path),
        )
        await adapter.on_startup(app)
        assert (
            await adapter.get_federation_base()
            == "https://abc.ui.nabu.casa/api/social_home/inbox"
        )
    await db.shutdown()


async def test_get_federation_base_strips_trailing_slash(tmp_path):
    db = AsyncDatabase(tmp_path / "test.db", batch_timeout_ms=10)
    await db.startup()
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO instance_config(key, value) VALUES(?, ?)",
        ("ha_federation_base", "https://example/api/social_home/inbox/"),
    )

    app = web.Application()
    app[db_key] = db
    app[event_bus_key] = EventBus()
    async with aiohttp.ClientSession() as session:
        app[http_session_key] = session
        adapter = HomeAssistantAdapter(
            ha_url="http://ha.local:8123",
            ha_token="",
            data_dir=str(tmp_path),
        )
        await adapter.on_startup(app)
        assert (
            await adapter.get_federation_base()
            == "https://example/api/social_home/inbox"
        )
    await db.shutdown()
