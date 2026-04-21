"""GFS connection management service (§24).

Handles pairing with Global Federation Servers, disconnecting, and
publishing / unpublishing spaces to paired GFS instances.

The pairing flow (simpler than HFS):
1. Admin scans GFS QR code -> extracts ``{gfs_url, token, public_key}``
2. Instance POSTs to ``{gfs_url}/gfs/register`` with own identity + webhook
3. GFS responds with ``gfs_instance_id``
4. Connection saved as ``status=active``
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import aiohttp

from ..domain.federation import GfsConnection
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo

log = logging.getLogger(__name__)


class GfsConnectionError(Exception):
    """Raised when a GFS operation fails."""

    __slots__ = ()


class GfsConnectionService:
    """Service for managing GFS connections and space publications."""

    __slots__ = ("_repo", "_http_client")

    def __init__(
        self,
        repo: AbstractGfsConnectionRepo,
        *,
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._repo = repo
        self._http_client = http_client

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        """Provide the shared aiohttp session after construction.

        Called from ``app._on_startup`` once the app-wide
        :class:`aiohttp.ClientSession` is available. Tests can inject a
        session at construction time via the ``http_client`` kwarg.
        """
        if self._http_client is None:
            self._http_client = session

    def _client(self) -> aiohttp.ClientSession:
        if self._http_client is None:
            raise RuntimeError(
                "GfsConnectionService used before attach_session — "
                "no aiohttp client wired",
            )
        return self._http_client

    async def pair(self, qr_payload: dict) -> GfsConnection:
        """Pair with a GFS using a scanned QR payload.

        Parameters
        ----------
        qr_payload:
            Must contain ``gfs_url``, ``token``, and ``public_key``.

        Returns
        -------
        The saved :class:`GfsConnection`.
        """
        gfs_url = str(qr_payload.get("gfs_url") or "").rstrip("/")
        token = str(qr_payload.get("token") or "")
        public_key = str(qr_payload.get("public_key") or "")

        if not gfs_url or not token or not public_key:
            raise GfsConnectionError(
                "gfs_url, token, and public_key are required",
            )

        register_url = f"{gfs_url}/gfs/register"
        client = self._client()
        try:
            async with client.post(
                register_url,
                json={"token": token},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    detail = await resp.text()
                    raise GfsConnectionError(
                        f"GFS registration failed (HTTP {resp.status}): {detail}",
                    )
                body = await resp.json()
        except aiohttp.ClientError as exc:
            raise GfsConnectionError(
                f"GFS unreachable: {exc}",
            ) from exc

        gfs_instance_id = str(body.get("gfs_instance_id") or "")
        display_name = str(body.get("display_name") or gfs_url)
        if not gfs_instance_id:
            raise GfsConnectionError(
                "GFS did not return a gfs_instance_id",
            )

        now = datetime.now(timezone.utc).isoformat()
        conn = GfsConnection(
            id=uuid.uuid4().hex,
            gfs_instance_id=gfs_instance_id,
            display_name=display_name,
            public_key=public_key,
            endpoint_url=gfs_url,
            status="active",
            paired_at=now,
        )
        await self._repo.save(conn)
        return conn

    async def disconnect(self, gfs_id: str) -> None:
        """Remove a GFS connection and all its publications."""
        conn = await self._repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")
        await self._repo.delete(gfs_id)

    async def list_connections(self) -> list[GfsConnection]:
        """Return all active GFS connections."""
        return await self._repo.list_active()

    async def publish_space(self, space_id: str, gfs_id: str) -> None:
        """Publish a space to a GFS.

        Sends a POST to the GFS endpoint and records the publication locally.
        """
        conn = await self._repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")

        client = self._client()
        publish_url = f"{conn.endpoint_url}/gfs/spaces/{space_id}/publish"
        try:
            async with client.post(
                publish_url,
                json={"space_id": space_id},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 201, 204):
                    detail = await resp.text()
                    log.warning(
                        "GFS publish failed (HTTP %d): %s",
                        resp.status,
                        detail,
                    )
        except aiohttp.ClientError as exc:
            log.warning("GFS publish request failed: %s", exc)

        await self._repo.publish_space(space_id, gfs_id)

    async def unpublish_space(self, space_id: str, gfs_id: str) -> None:
        """Unpublish a space from a GFS."""
        conn = await self._repo.get(gfs_id)
        if conn is None:
            raise GfsConnectionError(f"GFS connection {gfs_id} not found")

        client = self._client()
        unpublish_url = f"{conn.endpoint_url}/gfs/spaces/{space_id}/unpublish"
        try:
            async with client.delete(
                unpublish_url,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 204):
                    detail = await resp.text()
                    log.warning(
                        "GFS unpublish failed (HTTP %d): %s",
                        resp.status,
                        detail,
                    )
        except aiohttp.ClientError as exc:
            log.warning("GFS unpublish request failed: %s", exc)

        await self._repo.unpublish_space(space_id, gfs_id)

    async def publish_space_to_all(self, space_id: str) -> int:
        """Publish a space to every active GFS connection.

        Used by :class:`SpaceService` when a space flips to
        ``space_type=global``. Returns the number of GFS instances
        published to. Errors on individual GFS instances are logged
        and do not abort the fan-out.
        """
        conns = await self._repo.list_active()
        for conn in conns:
            try:
                await self.publish_space(space_id, conn.id)
            except Exception:
                log.exception(
                    "publish_space_to_all: failed for gfs %s",
                    conn.id,
                )
        return len(conns)

    async def unpublish_space_from_all(self, space_id: str) -> int:
        """Unpublish a space from every GFS it was published to."""
        conns = await self._repo.list_active()
        for conn in conns:
            try:
                await self.unpublish_space(space_id, conn.id)
            except Exception:
                log.exception(
                    "unpublish_space_from_all: failed for gfs %s",
                    conn.id,
                )
        return len(conns)

    # ── Fraud report outbound ─────────────────────────────────────────

    async def report_fraud(
        self,
        gfs_id: str,
        *,
        target_type: str,
        target_id: str,
        category: str,
        notes: str | None,
        reporter_instance_id: str,
        reporter_user_id: str | None,
        signing_key: bytes,
    ) -> bool:
        """Sign + POST a fraud report to a single paired GFS.

        Returns ``True`` on a 2xx response, ``False`` on any failure
        (logged, never raised). Called by :class:`ReportService` in the
        background; the local report is always the source of truth.
        """
        import json
        from datetime import datetime, timezone

        from ..crypto import b64url_encode, sign_ed25519

        conn = await self._repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return False

        body = {
            "target_type": target_type,
            "target_id": target_id,
            "category": category,
            "notes": notes,
            "reporter_instance_id": reporter_instance_id,
            "reporter_user_id": reporter_user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        body["signature"] = b64url_encode(
            sign_ed25519(signing_key, canonical),
        )

        try:
            client = self._client()
        except RuntimeError:
            # No HTTP session attached (test harness without network). Skip.
            return False
        url = f"{conn.endpoint_url}/gfs/report"
        try:
            async with client.post(
                url,
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                log.warning(
                    "GFS report_fraud returned HTTP %d: %s",
                    resp.status,
                    await resp.text(),
                )
                return False
        except aiohttp.ClientError as exc:
            log.warning("GFS report_fraud request failed: %s", exc)
            return False

    async def send_appeal(
        self,
        gfs_id: str,
        *,
        target_type: str,
        target_id: str,
        message: str,
        from_instance: str,
        signing_key: bytes,
    ) -> bool:
        """Sign + POST an appeal to a GFS that banned us.

        Returns ``True`` on a 2xx response. Logs + drops on any failure.
        """
        import json

        from ..crypto import b64url_encode, sign_ed25519

        conn = await self._repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return False

        body = {
            "target_type": target_type,
            "target_id": target_id,
            "message": message,
            "from_instance": from_instance,
        }
        canonical = json.dumps(
            body,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        body["signature"] = b64url_encode(sign_ed25519(signing_key, canonical))

        try:
            client = self._client()
        except RuntimeError:
            return False
        try:
            async with client.post(
                f"{conn.endpoint_url}/gfs/appeal",
                json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                log.warning(
                    "GFS send_appeal returned HTTP %d: %s",
                    resp.status,
                    await resp.text(),
                )
                return False
        except aiohttp.ClientError as exc:
            log.warning("GFS send_appeal request failed: %s", exc)
            return False
