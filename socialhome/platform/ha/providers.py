"""Shared HA provider classes.

The provider Protocols defined in :mod:`socialhome.platform.adapter`
have one HA-shaped implementation each that both :class:`HaAdapter`
(Core) and :class:`~socialhome.platform.haos.HaosAdapter` (Supervisor
add-on) compose. The auth provider is the only piece that varies —
HAOS uses :class:`~socialhome.platform.haos.adapter.HaIngressAuthProvider`
which trusts the Supervisor-injected ``X-Ingress-User`` header before
falling back to the bearer flow.

Each provider holds a back-reference to its adapter so it can lazily
access ``adapter._client`` (only available after the adapter's
``on_startup`` has run) and ``adapter._options``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterable

from ..adapter import ExternalUser, _extract_bearer

if TYPE_CHECKING:
    from aiohttp import web

    from .adapter import HaAdapter


def _state_to_user(state: dict) -> ExternalUser:
    """Convert an HA ``person.*`` state dict to :class:`ExternalUser`."""
    entity_id: str = state.get("entity_id", "")
    username = entity_id.removeprefix("person.")
    attrs: dict = state.get("attributes", {})
    return ExternalUser(
        username=username,
        display_name=attrs.get("friendly_name") or username,
        picture_url=attrs.get("entity_picture"),
        is_admin=False,
        email=None,
    )


class HaAuthProvider:
    """Resolve a request via ``X-Ingress-User`` header or HA bearer token.

    Default for :class:`HaAdapter` (HA Core / non-supervisor mode).
    HAOS swaps in :class:`~socialhome.platform.haos.adapter.HaIngressAuthProvider`,
    which trusts the Supervisor-injected header without bearer fallback.
    """

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HaAdapter") -> None:
        self._adapter = adapter

    async def authenticate(
        self, request: "web.Request",
    ) -> ExternalUser | None:
        ingress_user = request.headers.get("X-Ingress-User")
        if ingress_user:
            return await self._adapter.users.get(ingress_user)
        token = _extract_bearer(request)
        if token:
            return await self._authenticate_bearer(token)
        return None

    async def _authenticate_bearer(self, token: str) -> ExternalUser | None:
        data = await self._adapter._client.verify_token(token)
        if data is None:
            return None
        username = data.get("username") or "ha_api_user"
        return ExternalUser(
            username=username,
            display_name=username,
            picture_url=None,
            is_admin=True,
        )


class HaUserDirectory:
    """List / get principals from the HA ``person.*`` registry.

    HA mode treats persons as read-only data — provisioning happens
    via the steady-state ``/api/admin/ha-users`` routes which mirror
    enabled persons into the local ``users`` table. ``enable`` /
    ``disable`` raise :class:`NotImplementedError` so misuse is loud.
    """

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HaAdapter") -> None:
        self._adapter = adapter

    async def list_users(self) -> list[ExternalUser]:
        states = await self._adapter._client.get_states()
        out: list[ExternalUser] = []
        for state in states:
            entity_id: str = state.get("entity_id", "")
            if not entity_id.startswith("person."):
                continue
            out.append(_state_to_user(state))
        return out

    async def get(self, username: str) -> ExternalUser | None:
        state = await self._adapter._client.get_state(f"person.{username}")
        return _state_to_user(state) if state else None

    async def is_enabled(self, username: str) -> bool:
        return (await self.get(username)) is not None

    async def enable(
        self,
        username: str,
        *,
        password: str | None = None,
    ) -> ExternalUser:
        raise NotImplementedError(
            "HA-mode enable goes through ha_users routes; the directory "
            "is read-only as far as HA persons are concerned",
        )

    async def disable(self, username: str) -> None:
        raise NotImplementedError(
            "HA-mode disable goes through ha_users routes",
        )


class HaPushProvider:
    """Deliver via ``notify.mobile_app_{username}`` HA service call."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HaAdapter") -> None:
        self._adapter = adapter

    async def send(
        self,
        user: ExternalUser,
        title: str,
        message: str,
        data: dict | None = None,
    ) -> None:
        body: dict = {"title": title, "message": message}
        if data:
            body["data"] = data
        await self._adapter._client.call_service(
            "notify",
            f"mobile_app_{user.username}",
            body,
        )


class HaSTTProvider:
    """Stream PCM16 audio to ``/api/stt/{stt_entity_id}``."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HaAdapter") -> None:
        self._adapter = adapter

    async def transcribe(self, audio: bytes, language: str = "en") -> str:
        async def _single_chunk() -> AsyncIterable[bytes]:
            if audio:
                yield audio

        return await self.stream_transcribe(_single_chunk(), language=language)

    async def stream_transcribe(
        self,
        stream: AsyncIterable[bytes],
        *,
        language: str = "en",
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> str:
        entity_id = self._adapter._options.get("stt_entity_id")
        if not entity_id:
            raise NotImplementedError(
                "HaAdapter: no [homeassistant].stt_entity_id configured — "
                "set it to an HA STT entity id (e.g. "
                "'stt.home_assistant_cloud') to enable transcription."
            )
        payload = await self._adapter._client.stream_stt(
            entity_id,
            stream,
            language=language,
            sample_rate=sample_rate,
            channels=channels,
        )
        if not isinstance(payload, dict) or payload.get("result") != "success":
            return ""
        text = payload.get("text") or ""
        return text if isinstance(text, str) else str(text)


class HaAIProvider:
    """Run HA's ``ai_task.generate_data`` action."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HaAdapter") -> None:
        self._adapter = adapter

    async def generate_data(
        self,
        *,
        task_name: str,
        instructions: str,
    ) -> str:
        body: dict = {"task_name": task_name, "instructions": instructions}
        entity_id = self._adapter._options.get("ai_task_entity_id")
        if entity_id:
            body["entity_id"] = entity_id
        payload = await self._adapter._client.call_service(
            "ai_task",
            "generate_data",
            body,
            return_response=True,
        )
        if payload is None:
            return ""
        service_response = (payload or {}).get("service_response") or {}
        data = service_response.get("data", "")
        if isinstance(data, str):
            return data
        return str(data) if data else ""


class HaEventSink:
    """``POST /api/events/{event_type}`` so HA automations can subscribe."""

    __slots__ = ("_adapter",)

    def __init__(self, adapter: "HaAdapter") -> None:
        self._adapter = adapter

    async def fire(self, event_type: str, data: dict) -> bool:
        return await self._adapter._client.fire_event(event_type, data)
