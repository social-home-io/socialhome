"""Home Assistant Core REST API client.

A thin wrapper around ``aiohttp.ClientSession`` that encapsulates
authentication, base URL handling, and the REST call shapes used by
:class:`HomeAssistantAdapter`. The adapter stays focused on translating
between the platform protocol (`ExternalUser`, `InstanceConfig`, …) and
HA's REST shapes; the wire details live here.

Two deployment modes are supported via :func:`build_ha_client`:

* **Supervisor / add-on**: base URL ``http://supervisor/core`` with the
  Supervisor-issued bearer token. Set automatically when
  ``SUPERVISOR_TOKEN`` is present in the environment.
* **Direct**: base URL + long-lived access token configured by the
  operator (``SH_HA_URL`` / ``SH_HA_TOKEN`` or ``[homeassistant] url=``
  / ``token=`` in the TOML).

Both modes speak the same REST API — the Supervisor endpoint is a
transparent proxy for ``/api/*`` into HA Core.
"""

from __future__ import annotations

import logging
from typing import AsyncIterable, Mapping

import aiohttp

log = logging.getLogger(__name__)


class HaClient:
    """HTTP client for the Home Assistant Core REST API."""

    __slots__ = ("_session", "_base_url", "_token")

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token

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
        try:
            async with self._session.get(
                f"{self._base_url}/api/states",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            log.warning("ha_client: get_states failed: %s", exc)
            return []
        return data if isinstance(data, list) else []

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
        url = f"{self._base_url}/api/services/{domain}/{service}"
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
