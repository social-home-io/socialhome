"""Home Assistant Core REST + WebSocket API client.

A thin wrapper around ``aiohttp.ClientSession`` that encapsulates
authentication, base URL handling, and the call shapes used by
:class:`HomeAssistantAdapter`. The adapter stays focused on translating
between the platform protocol (``ExternalUser``, ``InstanceConfig``, …)
and HA's wire shapes; the protocol details live here.

Two deployment modes are supported via :func:`build_ha_client`:

* **Supervisor / add-on**: base URL ``http://supervisor/core`` with the
  Supervisor-issued bearer token. Set automatically when
  ``SUPERVISOR_TOKEN`` is present in the environment.
* **Direct**: base URL + long-lived access token configured by the
  operator (``SH_HA_URL`` / ``SH_HA_TOKEN`` or ``[homeassistant] url=``
  / ``token=`` in the TOML).

Both modes speak the same API — the Supervisor endpoint is a
transparent proxy for ``/api/*`` (and ``/api/websocket``) into HA Core.
"""

from __future__ import annotations

import logging
import time
from typing import AsyncIterable, Mapping
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)


#: TTL for the cached ``config/auth/list`` reply. HA user accounts
#: don't churn — adds happen via the HA UI by a human, the
#: auth-provider username column is fixed at user creation — so a
#: 30-second freshness window is plenty even on a busy ingress hot
#: path. The cache lives on the :class:`HaClient` instance, which is
#: a per-adapter singleton.
_AUTH_LIST_TTL_SECONDS = 30


class HaClient:
    """HTTP + WS client for the Home Assistant Core API."""

    __slots__ = (
        "_session",
        "_base_url",
        "_token",
        "_auth_list_cache",
        "_auth_list_cached_at",
        "_notify_cache",
        "_notify_cached_at",
    )

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._auth_list_cache: list[dict] | None = None
        self._auth_list_cached_at: float = 0.0
        self._notify_cache: list[dict] | None = None
        self._notify_cached_at: float = 0.0

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if extra:
            headers.update(extra)
        return headers

    # ── Core API surface ─────────────────────────────────────────────────

    async def verify_token(self, token: str) -> dict | None:
        """``GET /api/`` with an arbitrary bearer token.

        Used to validate an API token supplied by a client — the call
        returns ``{"message": "API running."}`` for a valid token. On any
        non-200 or transport error returns ``None``.
        """
        try:
            async with self._session.get(
                f"{self._base_url}/api/",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            log.warning("ha_client: verify_token failed: %s", exc)
            return None

    async def get_states(self) -> list[dict]:
        """``GET /api/states`` — list every entity state. ``[]`` on error."""
        return await self.fetch_states() or []

    async def fetch_states(self) -> list[dict] | None:
        """``GET /api/states`` — every entity state, or ``None`` when the
        call failed (transport error, non-2xx, a body that isn't a JSON
        list). Callers that must tell "HA has none" from "HA didn't
        answer" (the household-zone sync) use this; the WARNING is
        logged here.

        Contract (developers.home-assistant.io/docs/api/rest,
        ``GET /api/states``): a JSON array of state objects
        ``{entity_id, state, attributes, last_changed, last_updated}``.
        """
        try:
            async with self._session.get(
                f"{self._base_url}/api/states",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.warning("ha_client: get_states failed: %s", exc)
            return None
        if not isinstance(data, list):
            log.warning("ha_client: get_states answered a non-list body")
            return None
        return data

    async def get_state(self, entity_id: str) -> dict | None:
        """``GET /api/states/{entity_id}`` — ``None`` on 404 or error."""
        try:
            async with self._session.get(
                f"{self._base_url}/api/states/{entity_id}",
                headers=self._headers(),
            ) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            log.warning("ha_client: get_state(%r) failed: %s", entity_id, exc)
            return None

    async def fetch_path_bytes(self, path: str) -> bytes | None:
        """Fetch ``{base_url}{path}`` bytes using the integration token.

        Handy for pulling the ``entity_picture`` blob a ``person.*``
        entity references — HA serves it behind the authenticated
        ``/api/image/serve/…`` endpoint, so we reuse the client's
        bearer token. Returns ``None`` on 404 / transport error.
        """
        if not path.startswith("/"):
            path = "/" + path
        try:
            async with self._session.get(
                f"{self._base_url}{path}",
                headers=self._headers(),
            ) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                return await resp.read()
        except aiohttp.ClientError as exc:
            log.warning("ha_client: fetch_path_bytes(%r) failed: %s", path, exc)
            return None

    async def ws_command(self, type_: str, **fields: object) -> dict | None:
        """Open a one-shot WebSocket session, auth, send one command, return result.

        HA Core exposes a handful of inspection APIs only over the WS
        bus — ``config/auth/list`` (the authoritative list of HA users,
        with their auth-provider usernames + display names + ids) is the
        relevant one for #297. The REST surface has no equivalent, so
        the wizard's "drop person.* lookup" needs WS access.

        The handshake is short:

        1. Server greets with ``{type: "auth_required"}``.
        2. We reply ``{type: "auth", access_token: <bearer>}``.
        3. Server confirms ``{type: "auth_ok"}`` (or ``auth_invalid`` →
           we return ``None`` and log).
        4. We send ``{id: 1, type, **fields}``.
        5. Server replies ``{id: 1, type: "result", success: bool,
           result?: ...}``.

        The connection is closed after one command so the helper stays
        stateless — no long-lived WS, no event subscriptions, no
        re-auth on token rotation. Callers that need bulk traffic
        should build a dedicated client.

        Returns the ``result`` payload, or ``None`` on any failure
        (handshake reject, transport error, non-success result).
        """
        url = f"{self._base_url}/api/websocket"
        # ``http(s)://...`` → ``ws(s)://...`` so aiohttp's ws_connect
        # picks the right scheme. The Supervisor proxy at
        # ``http://supervisor/core/api/websocket`` works transparently
        # because Supervisor upgrades the connection on its side.
        try:
            async with self._session.ws_connect(url) as ws:
                hello = await ws.receive_json(timeout=5)
                if hello.get("type") != "auth_required":
                    log.warning(
                        "ha_client: ws_command(%r) unexpected greeting %r",
                        type_,
                        hello.get("type"),
                    )
                    return None
                await ws.send_json({"type": "auth", "access_token": self._token})
                auth_ack = await ws.receive_json(timeout=5)
                if auth_ack.get("type") != "auth_ok":
                    log.warning(
                        "ha_client: ws_command(%r) auth rejected (%r)",
                        type_,
                        auth_ack.get("type"),
                    )
                    return None
                await ws.send_json({"id": 1, "type": type_, **fields})
                reply = await ws.receive_json(timeout=10)
                if not reply.get("success", False):
                    err = (reply.get("error") or {}).get("message")
                    log.warning(
                        "ha_client: ws_command(%r) failed: %s",
                        type_,
                        err or reply,
                    )
                    return None
                # ``result`` may legitimately be a list, dict, or scalar
                # — wrap into a dict for the typed signature, callers
                # that expect a list reach in via ``["result"]``.
                return {"result": reply.get("result")}
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("ha_client: ws_command(%r) failed: %s", type_, exc)
            return None

    async def list_auth_users(
        self,
        *,
        force_refresh: bool = False,
    ) -> list[dict]:
        """``config/auth/list`` over WS — the canonical HA users list.

        Each row carries::

            {
              "id": "<32-hex>",
              "username": "socialhome" | null,
              "name": "Social Home Test",
              "is_owner": false,
              "is_active": true,
              "local_only": false,
              "system_generated": false,
              "group_ids": ["system-admin"],
              "credentials": [{"type": "homeassistant"}]
            }

        System accounts (Supervisor, Home Assistant Content, …) have
        ``username: null`` and ``system_generated: true``; the auth /
        wizard / provisioning code paths skip those.

        The reply is cached for :data:`_AUTH_LIST_TTL_SECONDS` so the
        ingress hot path doesn't pay a fresh WS handshake on every
        request. Pass ``force_refresh=True`` after a user-management
        change (token issuance, password rotation) to bust the cache;
        the wizard / admin routes do.

        Empty list on transport / auth error so callers degrade
        gracefully.
        """
        if not force_refresh and self._auth_list_cache is not None:
            age = time.monotonic() - self._auth_list_cached_at
            if age < _AUTH_LIST_TTL_SECONDS:
                return self._auth_list_cache
        reply = await self.ws_command("config/auth/list")
        if reply is None:
            return []
        result = reply.get("result")
        users = result if isinstance(result, list) else []
        self._auth_list_cache = users
        self._auth_list_cached_at = time.monotonic()
        return users

    def invalidate_auth_list_cache(self) -> None:
        """Drop the cached :meth:`list_auth_users` reply.

        Called after an action that may have changed HA's user table
        (admin enable/disable, password rotation, …) so the next read
        reflects the new state without waiting out the TTL.
        """
        self._auth_list_cache = None
        self._auth_list_cached_at = 0.0

    async def list_notify_entities(
        self,
        *,
        force_refresh: bool = False,
    ) -> list[dict]:
        """``notify.*`` entities over WS — id + display name for the dropdown.

        Runs the ``get_states`` WS command and keeps the entities whose
        ``entity_id`` starts with ``notify.``, mapping each to
        ``{"entity_id": ..., "name": ...}`` where ``name`` is the
        ``friendly_name`` attribute (falling back to the entity id when
        absent or empty). The list is sorted by ``name`` case-insensitively
        for a stable dropdown order.

        Cached for :data:`_AUTH_LIST_TTL_SECONDS` — notify entities don't
        churn — and bypassed with ``force_refresh=True``. Empty list on
        transport / auth error so callers degrade gracefully.
        """
        if not force_refresh and self._notify_cache is not None:
            age = time.monotonic() - self._notify_cached_at
            if age < _AUTH_LIST_TTL_SECONDS:
                return self._notify_cache
        reply = await self.ws_command("get_states")
        if reply is None:
            return []
        result = reply.get("result")
        states = result if isinstance(result, list) else []
        entities = [
            {
                "entity_id": s["entity_id"],
                "name": (s.get("attributes") or {}).get("friendly_name")
                or s["entity_id"],
            }
            for s in states
            if isinstance(s, dict) and str(s.get("entity_id", "")).startswith("notify.")
        ]
        entities.sort(key=lambda e: e["name"].casefold())
        self._notify_cache = entities
        self._notify_cached_at = time.monotonic()
        return entities

    async def get_config(self) -> dict | None:
        """``GET /api/config`` — instance location / time zone / currency."""
        try:
            async with self._session.get(
                f"{self._base_url}/api/config",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            log.warning("ha_client: get_config failed: %s", exc)
            return None

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict | None = None,
        *,
        return_response: bool = False,
    ) -> dict | None:
        """``POST /api/services/{domain}/{service}`` — returns the parsed body.

        When ``return_response`` is ``True`` the call sets HA's
        ``?return_response`` query flag so the service response (e.g. the
        ``ai_task.generate_data`` output) is included in the reply.
        Returns ``None`` on non-2xx or transport error.
        """
        # Percent-encode the path segments so no caller can traverse/inject
        # via the domain/service (defense-in-depth alongside the provider's
        # service-token validation).
        url = (
            f"{self._base_url}/api/services/"
            f"{quote(domain, safe='')}/{quote(service, safe='')}"
        )
        if return_response:
            url = f"{url}?return_response"
        try:
            async with self._session.post(
                url,
                headers=self._headers(),
                json=data or {},
            ) as resp:
                if resp.status not in (200, 201):
                    log.debug(
                        "ha_client: call_service %s.%s -> HTTP %d",
                        domain,
                        service,
                        resp.status,
                    )
                    return None
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            log.debug(
                "ha_client: call_service %s.%s failed: %s",
                domain,
                service,
                exc,
            )
            return None

    async def fire_event(self, event_type: str, data: dict | None = None) -> bool:
        """``POST /api/events/{event_type}`` — ``True`` on 2xx, ``False`` otherwise."""
        url = f"{self._base_url}/api/events/{event_type}"
        try:
            async with self._session.post(
                url,
                headers=self._headers(),
                json=data or {},
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                log.debug(
                    "ha_client: fire_event %s -> HTTP %d",
                    event_type,
                    resp.status,
                )
                return False
        except aiohttp.ClientError as exc:
            log.debug("ha_client: fire_event %s failed: %s", event_type, exc)
            return False

    async def stream_stt(
        self,
        entity_id: str,
        audio: AsyncIterable[bytes],
        *,
        language: str,
        sample_rate: int,
        channels: int,
    ) -> dict | None:
        """``POST /api/stt/{entity_id}`` with a chunked PCM16 body.

        aiohttp sends the async iterable with ``Transfer-Encoding: chunked``
        so HA starts consuming bytes before we finish reading from the
        browser. Returns the parsed response body or ``None`` on error.
        """
        url = f"{self._base_url}/api/stt/{entity_id}"
        headers = self._headers(
            {
                "X-Speech-Content": (
                    f"format=wav; codec=pcm; sample_rate={sample_rate}; "
                    f"bit_rate=16; channel={channels}; language={language}"
                ),
            }
        )
        try:
            async with self._session.post(
                url,
                headers=headers,
                data=audio,
            ) as resp:
                if resp.status not in (200, 201):
                    log.debug(
                        "ha_client: stt %s -> HTTP %d",
                        entity_id,
                        resp.status,
                    )
                    return None
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            log.debug("ha_client: stt %s failed: %s", entity_id, exc)
            return None


def build_ha_client(
    session: aiohttp.ClientSession,
    *,
    supervisor_token: str,
    ha_url: str,
    ha_token: str,
) -> HaClient:
    """Build the right :class:`HaClient` for the runtime context.

    When ``supervisor_token`` is non-empty the app is running as a HA
    add-on; Core is reached through the Supervisor proxy at
    ``http://supervisor/core``. Otherwise the caller-supplied
    ``ha_url`` + ``ha_token`` are used directly.
    """
    if supervisor_token:
        return HaClient(session, "http://supervisor/core", supervisor_token)
    return HaClient(session, ha_url, ha_token)


__all__ = ["HaClient", "build_ha_client"]
