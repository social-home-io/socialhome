"""Tests for socialhome.platform.ha.adapter — fake HaClient injection."""

from __future__ import annotations

from typing import AsyncIterable

import asyncio
import json
import aiohttp
import pytest
from aiohttp import web

from socialhome.app_keys import (
    db_key,
    event_bus_key,
    federation_service_key,
    http_session_key,
)
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
        auth_users: list[dict] | None = None,
        path_bytes: dict[str, bytes] | None = None,
        notify_entities: list[dict] | None = None,
    ) -> None:
        self._verify_token_response = verify_token_response
        self._states = states if states is not None else []
        self._state_by_entity = state_by_entity or {}
        self._config_response = config_response
        self._call_service_response = call_service_response
        self._fire_event_result = fire_event_result
        self._stt_response = stt_response
        self._auth_users = auth_users if auth_users is not None else []
        self._path_bytes = path_bytes or {}
        self._notify_entities = notify_entities
        self.calls: list[tuple] = []

    async def list_notify_entities(self) -> list[dict]:
        self.calls.append(("list_notify_entities",))
        return self._notify_entities or []

    async def list_auth_users(self) -> list[dict]:
        self.calls.append(("list_auth_users",))
        return self._auth_users

    async def fetch_path_bytes(self, path: str) -> bytes | None:
        self.calls.append(("fetch_path_bytes", path))
        return self._path_bytes.get(path)

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


async def test_authenticate_ingress_user_resolves_via_auth_list():
    """X-Remote-User-Name → ``config/auth/list`` lookup, no ``person.*``
    round-trip. The auth username and the person entity slug are
    unrelated by HA's contract (#297)."""
    client = _FakeHaClient(
        auth_users=[
            {
                "id": "abc123",
                "username": "pascal",
                "name": "Pascal V",
                "is_owner": True,
            },
        ],
    )
    adapter = _build_adapter(client=client)

    user = await adapter.authenticate(
        _FakeRequest(
            headers={
                "X-Hass-Source": "core.ingress",
                "X-Remote-User-Name": "pascal",
            }
        ),
    )
    assert user is not None and user.username == "pascal"
    assert user.display_name == "Pascal V"
    assert ("list_auth_users",) in client.calls
    # No ``person.*`` lookups on the auth path — those happen only when
    # the avatar lifter explicitly walks via user_id.
    assert not any(c[0] == "get_state" for c in client.calls)


async def test_authenticate_bearer_rejected_without_local_credentials():
    """HA's REST has no token→user mapping, so a bearer that isn't in
    the local ``platform_tokens`` store must be rejected — never
    fall through to ``GET /api/`` and pretend the token belongs to a
    shared identity."""
    client = _FakeHaClient(
        verify_token_response={"message": "API running."},
    )
    adapter = _build_adapter(client=client)
    user = await adapter.authenticate(
        _FakeRequest(headers={"Authorization": "Bearer tok123"}),
    )
    assert user is None
    # ``verify_token`` is no longer called from the bearer flow — the
    # only acceptance path is the local credential store.
    assert ("verify_token", "tok123") not in client.calls


async def test_authenticate_bearer_accepted_via_local_credentials():
    class _FakeCredentials:
        async def authenticate_bearer(self, token):
            from socialhome.platform.adapter import ExternalUser

            if token == "tok":
                return ExternalUser(
                    username="alice",
                    display_name="Alice",
                    picture_url=None,
                    is_admin=True,
                )
            return None

    adapter = _build_adapter(client=_FakeHaClient())
    adapter._credentials = _FakeCredentials()
    user = await adapter.authenticate_bearer("tok")
    assert user is not None
    assert user.username == "alice"
    assert user.is_admin is True


async def test_authenticate_bearer_invalid_returns_none():
    adapter = _build_adapter(client=_FakeHaClient(verify_token_response=None))
    assert await adapter.authenticate_bearer("bad") is None


# ─── Local password auth (wizard-set owner password) ──────────────────────


async def test_capabilities_include_password_auth():
    """ha mode supports local password auth — the setup wizard sets a
    password for the picked HA owner so they can log in via /api/auth/token."""
    from socialhome.platform.adapter import Capability

    adapter = _build_adapter(client=_FakeHaClient())
    assert Capability.PASSWORD_AUTH in adapter.capabilities


async def test_local_password_login_round_trip(tmp_path):
    """Wire the credential store, set a password, and verify the bearer
    token authenticates."""
    db = AsyncDatabase(tmp_path / "t.db", batch_timeout_ms=10)
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
            ha_client=_FakeHaClient(),
        )
        await adapter.on_startup(app)

        await adapter.set_local_password(
            "pascal",
            "hunter2",
            display_name="Pascal",
            is_admin=True,
        )
        # Mirror the user row so the bearer flow can resolve a user_id.
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) "
            "VALUES('pascal', 'uid-pascal', 'Pascal', 1)",
        )
        token = await adapter.issue_bearer_token("pascal", "hunter2")
        assert token is not None
        # The HaAuthProvider should resolve the token via the local
        # credential store before ever calling HA's verify_token.
        user = await adapter.authenticate_bearer(token)
        assert user is not None and user.username == "pascal"
        assert user.is_admin is True
    await db.shutdown()


async def test_local_password_wrong_returns_none(tmp_path):
    db = AsyncDatabase(tmp_path / "t.db", batch_timeout_ms=10)
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
            ha_url="http://ha.local",
            ha_token="",
            data_dir=str(tmp_path),
            ha_client=_FakeHaClient(),
        )
        await adapter.on_startup(app)
        await adapter.set_local_password("alice", "right", is_admin=True)
        assert await adapter.issue_bearer_token("alice", "wrong") is None
    await db.shutdown()


# ─── User listing ────────────────────────────────────────────────────────


async def test_list_external_users_reads_auth_list():
    """The directory sources users from ``config/auth/list``. Filters
    rejected entries: ``system_generated: true`` rows (Supervisor's
    own service account, ``Home Assistant Content``, cloud / mobile-
    app bridges — they carry ``username: null``); ``is_active: false``
    rows (disabled HA accounts shouldn't surface in the wizard); rows
    with ``username: null`` defensively, in case a future system row
    omits the flag (#297 / #298)."""
    auth_users = [
        {
            "id": "abc",
            "username": "pascal",
            "name": "Pascal V",
            "is_owner": True,
            "is_active": True,
            "system_generated": False,
        },
        {
            "id": "def",
            "username": "maria",
            "name": "Maria",
            "is_owner": False,
            "is_active": True,
            "system_generated": False,
        },
        {
            "id": "sys-supervisor",
            "username": None,
            "name": "Supervisor",
            "is_owner": False,
            "is_active": True,
            "system_generated": True,
        },
        {
            "id": "disabled",
            "username": "ex-roommate",
            "name": "Ex Roommate",
            "is_owner": False,
            "is_active": False,
            "system_generated": False,
        },
    ]
    adapter = _build_adapter(client=_FakeHaClient(auth_users=auth_users))
    users = await adapter.list_external_users()
    assert [u.username for u in users] == ["pascal", "maria"]
    assert users[0].display_name == "Pascal V"


async def test_directory_get_owner_returns_first_owner():
    """``HaUserDirectory.get_owner`` walks the auth list and returns
    the row with ``is_owner=True``."""
    auth_users = [
        {"id": "a", "username": "alice", "name": "Alice", "is_owner": False},
        {"id": "b", "username": "bob", "name": "Bob", "is_owner": True},
    ]
    adapter = _build_adapter(client=_FakeHaClient(auth_users=auth_users))
    owner = await adapter.users.get_owner()
    assert owner is not None and owner.username == "bob"


async def test_list_external_users_empty_on_client_error():
    adapter = _build_adapter(client=_FakeHaClient(auth_users=[]))
    assert await adapter.list_external_users() == []


async def test_get_external_user_found():
    auth_users = [
        {"id": "abc", "username": "pascal", "name": "Pascal V"},
    ]
    adapter = _build_adapter(client=_FakeHaClient(auth_users=auth_users))
    user = await adapter.get_external_user("pascal")
    assert user is not None and user.username == "pascal"


async def test_get_external_user_not_found():
    adapter = _build_adapter(client=_FakeHaClient(auth_users=[]))
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


def _push_user(notify_service: str | None):
    """A user-like object carrying the per-user ``ha_notify_service``
    profile preference the push provider reads (mirrors the SH ``User``
    shape: ``username`` + ``preferences_json``)."""
    from types import SimpleNamespace

    prefs = {"ha_notify_service": notify_service} if notify_service else {}
    return SimpleNamespace(
        username="pascal",
        preferences_json=json.dumps(prefs),
    )


async def test_send_push_uses_configured_notify_service():
    """A ``notify.mobile_app_<device>`` value resolves to mobile_app's legacy
    per-device service — ``call_service("notify", "mobile_app_<device>", body)``
    — and INCLUDES the rich ``data`` payload (tap url / actions). The entity
    action ``notify.send_message`` is NOT used: HA's strict entity-service
    schema rejects ``data``, so the legacy service is the only path that
    carries the payload."""
    client = _FakeHaClient(call_service_response=[])
    adapter = _build_adapter(client=client)
    user = _push_user("notify.mobile_app_pascals_iphone")
    await adapter.send_push(user, "title", "message", data={"x": 1})
    assert any(
        c[0] == "call_service"
        and c[1] == "notify"
        and c[2] == "mobile_app_pascals_iphone"
        and c[3]
        == {
            "title": "title",
            "message": "message",
            "data": {"x": 1},
        }
        for c in client.calls
    )
    # NOT routed through the send_message entity action, and the value never
    # appears as an entity_id.
    assert not any(
        c[0] == "call_service" and c[2] == "send_message" for c in client.calls
    )


async def test_send_push_other_notify_entity_uses_send_message_without_data():
    """A non-mobile_app ``notify.<entity>`` (e.g. a notify group entity that
    has no legacy per-device service) routes to the ``notify.send_message``
    entity action with the value in ``entity_id`` — and OMITS ``data`` because
    HA's ``notify.send_message`` only accepts ``message``/``title``."""
    client = _FakeHaClient(call_service_response=[])
    adapter = _build_adapter(client=client)
    user = _push_user("notify.alerts_group")
    await adapter.send_push(user, "title", "message", data={"x": 1})
    call = next(
        c for c in client.calls if c[0] == "call_service" and c[2] == "send_message"
    )
    assert call[1] == "notify"
    assert call[3]["entity_id"] == "notify.alerts_group"
    assert "data" not in call[3]


async def test_send_push_non_notify_domain_service_rejected(caplog):
    """notify-only policy: a fully-qualified non-``notify`` ``domain.service``
    (e.g. ``telegram_bot.send_xyz``) is REJECTED — NO ``call_service`` is made,
    and a WARNING is logged. SH calls HA with the instance's shared token, so a
    user-controlled ``ha_notify_service`` must never reach an arbitrary domain;
    the Settings dropdown only ever offers ``notify.*`` targets. (Behavior
    change: legacy non-notify domain.service passthrough is gone.)"""
    client = _FakeHaClient(call_service_response=[])
    adapter = _build_adapter(client=client)
    user = _push_user("telegram_bot.send_xyz")
    with caplog.at_level("WARNING"):
        await adapter.send_push(user, "title", "message", data={"x": 1})
    assert not any(c[0] == "call_service" for c in client.calls)
    assert any("unsupported notify target" in r.message for r in caplog.records)


async def test_send_push_dangerous_domain_service_rejected(caplog):
    """The attack this hardening blocks: ``homeassistant.restart`` (or
    ``shell_command.*``, ``automation.trigger``, …) set as a user's
    ``ha_notify_service`` must NEVER be invoked with the instance token."""
    for value in ("homeassistant.restart", "shell_command.foo"):
        client = _FakeHaClient(call_service_response=[])
        adapter = _build_adapter(client=client)
        user = _push_user(value)
        with caplog.at_level("WARNING"):
            await adapter.send_push(user, "title", "message", data={"x": 1})
        assert not any(c[0] == "call_service" for c in client.calls), value
        assert any("unsupported notify target" in r.message for r in caplog.records), (
            value
        )
        caplog.clear()


async def test_send_push_entity_value_only_lands_in_entity_id():
    """Anti-injection invariant for the entity branch: the user-supplied
    ``notify.*`` value is delivered as the ``entity_id`` data field while the
    invoked domain/service stay the hardcoded literals ``notify``/
    ``send_message`` — even for an odd multi-dot value — so the value can
    never reach the ``/api/services/{domain}/{service}`` URL path."""
    client = _FakeHaClient(call_service_response=[])
    adapter = _build_adapter(client=client)
    user = _push_user("notify.weird.name")
    await adapter.send_push(user, "t", "m")
    calls = [c for c in client.calls if c[0] == "call_service"]
    assert calls == [
        (
            "call_service",
            "notify",
            "send_message",
            {
                "entity_id": "notify.weird.name",
                "title": "t",
                "message": "m",
            },
            False,
        ),
    ]


async def test_send_push_mobile_app_injection_token_falls_through_to_body():
    """A ``notify.mobile_app_*`` value whose suffix is NOT a valid HA service
    token (contains ``/``, dots, traversal…) MUST NOT reach the legacy-service
    URL path. It falls through to ``notify.send_message`` so the malicious
    value lands safely in the request BODY (``entity_id``), never the
    ``/api/services/{domain}/{service}`` URL path."""
    client = _FakeHaClient(call_service_response=[])
    adapter = _build_adapter(client=client)
    user = _push_user("notify.mobile_app_x/../../evil")
    await adapter.send_push(user, "t", "m")
    calls = [c for c in client.calls if c[0] == "call_service"]
    # The invoked service stays the hardcoded literal — the value never
    # becomes the {service} URL segment.
    assert all(c[2] != "mobile_app_x/../../evil" for c in calls)
    assert calls == [
        (
            "call_service",
            "notify",
            "send_message",
            {
                "entity_id": "notify.mobile_app_x/../../evil",
                "title": "t",
                "message": "m",
            },
            False,
        ),
    ]


async def test_send_push_empty_notify_suffix_rejected(caplog):
    """``"notify."`` (no entity suffix) is not a valid entity id and, having a
    ``.``, reaches the non-notify reject branch — REJECTED with a WARNING, no
    ``call_service`` (notify-only policy; no ``notify.`` garbage call)."""
    client = _FakeHaClient(call_service_response=[])
    adapter = _build_adapter(client=client)
    with caplog.at_level("WARNING"):
        await adapter.send_push(_push_user("notify."), "t", "m")
    assert not any(c[0] == "call_service" for c in client.calls)
    assert any("unsupported notify target" in r.message for r in caplog.records)


async def test_send_push_bare_garbage_name_rejected(caplog):
    """A bare name (no dot) that is NOT a valid HA service token — e.g.
    ``"weird name/../x"`` — is REJECTED before building a ``notify.<garbage>``
    call: no ``call_service``, a WARNING logged."""
    client = _FakeHaClient(call_service_response=[])
    adapter = _build_adapter(client=client)
    with caplog.at_level("WARNING"):
        await adapter.send_push(_push_user("weird name/../x"), "t", "m")
    assert not any(c[0] == "call_service" for c in client.calls)
    assert any("unsupported notify target" in r.message for r in caplog.records)


async def test_list_notify_targets_delegates_to_client():
    targets = [{"entity_id": "notify.mobile_app_x", "name": "Mobile app x"}]
    client = _FakeHaClient(notify_entities=targets)
    adapter = _build_adapter(client=client)
    assert await adapter.push.list_notify_targets() == targets
    assert ("list_notify_entities",) in client.calls


async def test_send_push_accepts_bare_service_name():
    client = _FakeHaClient(call_service_response=[])
    adapter = _build_adapter(client=client)
    user = _push_user("mobile_app_pascals_iphone")  # no "notify." prefix
    await adapter.send_push(user, "t", "m")
    assert any(
        c[0] == "call_service"
        and c[1] == "notify"
        and c[2] == "mobile_app_pascals_iphone"
        for c in client.calls
    )


async def test_send_push_skips_when_unconfigured():
    """No ha_notify_service preference → no service call (the old
    mobile_app_{username} guess silently 400'd for everyone)."""
    client = _FakeHaClient(call_service_response=[])
    adapter = _build_adapter(client=client)
    await adapter.send_push(_push_user(None), "t", "m")
    assert not any(c[0] == "call_service" for c in client.calls)


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


async def test_get_federation_base_appends_inbox_path(tmp_path):
    """The HA integration pushes the bare external URL (Nabu Casa or
    admin-set ``external_url``). It also registers an HA Core HTTP
    view at ``/api/socialhome/inbox/{inbox_id}`` that forwards into
    the addon. So peers POST to
    ``{pushed_url}/api/socialhome/inbox/{inbox_id}`` — the adapter
    has to splice that path on so the pairing coordinator's
    ``{base}/{secret_id}`` produces a URL peers can actually reach.

    Regression: before this, the adapter returned the bare URL,
    pairing QRs ended up with ``inbox_url = {ha_url}/{secret_id}``,
    and peers hit HA's frontend instead of the addon's inbox."""
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
        ("ha_federation_base", "https://abc.ui.nabu.casa"),
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
            == "https://abc.ui.nabu.casa/api/socialhome/inbox"
        )
    await db.shutdown()


async def test_get_federation_base_strips_trailing_slash(tmp_path):
    """Trailing slash on the pushed bare URL gets normalised — the
    appended ``/api/socialhome/inbox`` keeps a single boundary slash."""
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
        ("ha_federation_base", "https://example/"),
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
            == "https://example/api/socialhome/inbox"
        )
    await db.shutdown()


async def test_get_federation_base_idempotent_if_already_appended(tmp_path):
    """A future integration that ever pushed the full path mustn't
    cause a double-append (``…/inbox/api/socialhome/inbox``). The
    adapter is idempotent against the path it owns."""
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
        ("ha_federation_base", "https://example/api/socialhome/inbox"),
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
            == "https://example/api/socialhome/inbox"
        )
    await db.shutdown()


# ── ICE-server sync wiring (#423 pull path) ───────────────────────────


class _IceHaClient(_FakeHaClient):
    """Adds the ``web_rtc/ice_servers`` WS reply the sync needs."""

    def __init__(self, result: list | None = None) -> None:
        super().__init__()
        self._ice_result = (
            result
            if result is not None
            else [
                {"urls": "turn:t.example:3478", "username": "u", "credential": "c"},
            ]
        )

    async def ws_command(self, type_: str, **fields):  # noqa: ARG002
        return {"result": self._ice_result}


class _RecordingFederation:
    def __init__(self) -> None:
        self.applied: list[list[dict]] = []

    def set_ice_servers(self, servers: list[dict]) -> None:
        self.applied.append(servers)


async def _ha_adapter_app(tmp_path, db_key_, bus_key_, session):
    from socialhome.db.database import AsyncDatabase

    db = AsyncDatabase(str(tmp_path / "t.db"))
    await db.startup()
    app = web.Application()
    app[db_key_] = db
    app[bus_key_] = EventBus()
    app[http_session_key] = session
    return app, db


async def test_on_startup_wires_the_ice_sync_and_applies_to_federation(tmp_path):
    """``on_startup`` must start the pull and route it to the federation
    service — the ``_apply`` closure had no test at all, so nothing pinned
    that the fetched list actually lands anywhere."""
    async with aiohttp.ClientSession() as session:
        app, _db = await _ha_adapter_app(tmp_path, db_key, event_bus_key, session)
        fed = _RecordingFederation()
        app[federation_service_key] = fed
        adapter = HomeAssistantAdapter(
            ha_url="http://ha.local:8123",
            ha_token="",
            data_dir=str(tmp_path),
            ha_client=_IceHaClient(),
        )
        await adapter.on_startup(app)
        try:
            assert adapter._ice_sync is not None, "ICE sync was never created"
            # Drive one cycle directly rather than racing the loop's timer.
            assert await adapter._ice_sync.fetch_and_apply_once() is True
            assert fed.applied == [
                [{"urls": ["turn:t.example:3478"], "username": "u", "credential": "c"}]
            ]
        finally:
            await adapter.on_cleanup(app)
        assert adapter._ice_sync is None, "on_cleanup left the sync running"


async def test_on_startup_tolerates_no_federation_service(tmp_path):
    """Federation unwired (or wired later) must not break adapter startup."""
    async with aiohttp.ClientSession() as session:
        app, _db = await _ha_adapter_app(tmp_path, db_key, event_bus_key, session)
        adapter = HomeAssistantAdapter(
            ha_url="http://ha.local:8123",
            ha_token="",
            data_dir=str(tmp_path),
            ha_client=_IceHaClient(),
        )
        await adapter.on_startup(app)
        assert adapter._ice_sync is None
        await adapter.on_cleanup(app)


async def test_empty_ha_reply_leaves_federation_untouched(tmp_path):
    """The #1 finding, at the wiring level: HA with nothing to offer must
    not blank out the operator's configured ICE servers."""
    async with aiohttp.ClientSession() as session:
        app, _db = await _ha_adapter_app(tmp_path, db_key, event_bus_key, session)
        fed = _RecordingFederation()
        app[federation_service_key] = fed
        adapter = HomeAssistantAdapter(
            ha_url="http://ha.local:8123",
            ha_token="",
            data_dir=str(tmp_path),
            ha_client=_IceHaClient(result=[]),
        )
        await adapter.on_startup(app)
        try:
            assert await adapter._ice_sync.fetch_and_apply_once() is True
            assert fed.applied == [], "an empty HA list reached set_ice_servers"
        finally:
            await adapter.on_cleanup(app)


# ── ICE-prime gate ────────────────────────────────────────────────────────


def test_provides_ice_servers_is_true():
    """ha mode pulls HA Core's ``web_rtc/ice_servers`` after startup, so the
    federation transport should hold its first handshake briefly."""
    adapter = HomeAssistantAdapter(
        ha_url="http://ha.local:8123",
        ha_token="",
        data_dir="/tmp",
    )
    assert adapter.provides_ice_servers is True


class _PrimingFederation(_RecordingFederation):
    def __init__(self) -> None:
        super().__init__()
        self.primed = 0

    def mark_ice_primed(self) -> None:
        self.primed += 1


async def test_on_startup_releases_the_ice_gate_after_the_first_attempt(tmp_path):
    """Even when HA has nothing usable to offer, the first fetch attempt must
    release the transport's ICE gate — otherwise every haos/ha boot pays the
    full prime timeout on its first outbound federation send."""
    async with aiohttp.ClientSession() as session:
        app, _db = await _ha_adapter_app(tmp_path, db_key, event_bus_key, session)
        fed = _PrimingFederation()
        app[federation_service_key] = fed
        adapter = HomeAssistantAdapter(
            ha_url="http://ha.local:8123",
            ha_token="",
            data_dir=str(tmp_path),
            ha_client=_IceHaClient(result=[]),
        )
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
