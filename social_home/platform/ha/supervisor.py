"""Home Assistant Supervisor API client.

A thin wrapper for the Supervisor-only endpoints used by
:class:`HaBootstrap` when Social Home runs as a HA add-on:

* ``GET /auth/list``  — discover the HA owner account so we can provision
  them as the initial Social Home admin.
* ``POST /discovery`` — register the add-on with HA's discovery integration
  so the official ``social_home`` HA integration can pick us up automatically.

The Supervisor sets ``SUPERVISOR_URL`` / ``SUPERVISOR_TOKEN`` in the add-on
environment. We do **not** need (or fetch) the add-on's own ingress URL —
HA resolves the add-on hostname itself from the discovery record.
"""

from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger(__name__)


class SupervisorClient:
    """HTTP client for the Supervisor API (never talks to HA Core)."""

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

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def get_owner_username(self) -> str | None:
        """Return the non-system HA owner, or ``None``.

        Uses ``GET /auth/list``. The response envelope is
        ``{"data": {"users": [...]}}`` as of HA 2024+; the older
        ``{"users": [...]}`` shape is tolerated.
        """
        try:
            async with self._session.get(
                f"{self._base_url}/auth/list",
                headers=self._headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except aiohttp.ClientError as exc:
            log.warning("supervisor: /auth/list failed: %s", exc)
            return None

        users = data.get("data", data).get("users", [])
        owner = next(
            (
                u
                for u in users
                if u.get("is_owner") and not u.get("system_generated", False)
            ),
            None,
        )
        if not owner:
            log.warning("supervisor: no owner found in /auth/list")
            return None
        return owner.get("username") or owner.get("name")

    async def push_discovery(self, payload: dict) -> bool:
        """POST ``/discovery`` — returns ``True`` on 2xx.

        HA's discovery integration resolves the add-on hostname itself
        from the add-on slug, so the payload only needs to carry the
        integration token.
        """
        try:
            async with self._session.post(
                f"{self._base_url}/discovery",
                headers=self._headers(),
                json=payload,
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                log.warning(
                    "supervisor: discovery push returned HTTP %d",
                    resp.status,
                )
                return False
        except aiohttp.ClientError as exc:
            log.warning("supervisor: discovery push failed: %s", exc)
            return False


__all__ = ["SupervisorClient"]
