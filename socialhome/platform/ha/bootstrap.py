"""One-time HA add-on bootstrap (§5.2).

Runs on startup when Social Home is deployed as a Home Assistant add-on
(i.e. when a :class:`~.supervisor.SupervisorClient` is available).
The bootstrap:

* looks up the Home Assistant owner via the Supervisor API and provisions
  them as the Social Home admin;
* generates a persistent API token for the HA integration to use;
* pushes a Supervisor discovery entry so the official HA integration can
  find us automatically.

The bootstrap is idempotent — the ``ha_bootstrap_done`` flag in
``instance_config`` gates the one-off provisioning steps, while the
discovery push runs on every boot (so HA recovers from a restart even
before it next re-polls its discovery cache).

HA resolves the add-on hostname itself, so we no longer fetch it from
Supervisor — the discovery payload only carries the integration token.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone

from ...crypto import derive_user_id
from ...db import AsyncDatabase
from .supervisor import SupervisorClient

log = logging.getLogger(__name__)

BOOTSTRAP_FLAG = "ha_bootstrap_done"
INTEGRATION_TOKEN_FILENAME = "integration_token.txt"
INTEGRATION_TOKEN_LABEL = "HA Integration (auto)"


class HaBootstrap:
    """Provision the HA owner, mint an integration token, push discovery.

    Parameters
    ----------
    db:
        The application database.
    supervisor_client:
        Client for the Supervisor API (``/auth/list``, ``/discovery``).
    data_dir:
        Directory where the raw integration token is persisted so the
        discovery push can read it on subsequent boots. Typically
        ``config.data_dir`` — ``/data`` in add-on mode.
    """

    __slots__ = ("_db", "_sv", "_data_dir")

    def __init__(
        self,
        db: AsyncDatabase,
        supervisor_client: SupervisorClient,
        data_dir: str,
    ) -> None:
        self._db = db
        self._sv = supervisor_client
        self._data_dir = data_dir

    # ─── Public entry point ───────────────────────────────────────────────

    async def run(self) -> None:
        """Run the full bootstrap. Idempotent."""
        if not await self._is_done():
            owner = await self._sv.get_owner_username()
            if owner:
                await self._provision_admin(owner)
                await self._generate_integration_token(owner)
                await self._mark_done()
                log.info("ha_bootstrap: admin provisioned as %r", owner)
            else:
                log.warning("ha_bootstrap: could not determine HA owner — skipping")

        await self._push_discovery()

    # ─── Provisioning ────────────────────────────────────────────────────

    async def _provision_admin(self, username: str) -> None:
        """Insert or re-enable the HA owner as a SH admin."""
        existing = await self._db.fetchone(
            "SELECT user_id FROM users WHERE username=?",
            (username,),
        )
        if existing is not None:
            # Already provisioned — ensure is_admin=1 in case it was demoted
            # and stamp source='ha' so the HA Users admin panel recognises
            # the row.
            await self._db.enqueue(
                "UPDATE users SET is_admin=1, state='active', source='ha' "
                "WHERE username=?",
                (username,),
            )
            return

        identity = await self._db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'",
        )
        if identity is None:
            raise RuntimeError(
                "ha_bootstrap: instance_identity not initialised before bootstrap"
            )
        pk_bytes = bytes.fromhex(identity["identity_public_key"])
        user_id = derive_user_id(pk_bytes, username)

        await self._db.enqueue(
            """
            INSERT INTO users(user_id, username, display_name, is_admin,
                              created_at, source)
            VALUES(?, ?, ?, 1, ?, 'ha')
            """,
            (user_id, username, username, datetime.now(timezone.utc).isoformat()),
        )

    async def _generate_integration_token(self, username: str) -> None:
        """Create (or reuse) a no-expiry API token for the HA integration.

        The SHA-256 hash is stored in ``api_tokens`` and the raw token is
        written to ``<data_dir>/integration_token.txt`` (mode ``0o600``)
        so :meth:`_push_discovery` can read it on every boot.
        """
        existing = await self._db.fetchone(
            """
            SELECT token_id FROM api_tokens
             WHERE label=? AND revoked_at IS NULL
            """,
            (INTEGRATION_TOKEN_LABEL,),
        )
        if existing is not None:
            log.debug("ha_bootstrap: integration token already exists")
            return

        user = await self._db.fetchone(
            "SELECT user_id FROM users WHERE username=?",
            (username,),
        )
        if user is None:
            log.warning(
                "ha_bootstrap: user %r not found — cannot create token",
                username,
            )
            return

        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        await self._db.enqueue(
            """
            INSERT INTO api_tokens(token_id, user_id, label, token_hash, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                user["user_id"],
                INTEGRATION_TOKEN_LABEL,
                token_hash,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        token_path = self._token_path()
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(raw_token)
            os.chmod(token_path, 0o600)
            log.info("ha_bootstrap: integration token written to %s", token_path)
        except OSError as exc:
            # Discovery push will notice the file missing and log — the DB
            # row is already in place so there is nothing to retry.
            log.warning(
                "ha_bootstrap: could not write %s: %s",
                token_path,
                exc,
            )

    # ─── Discovery ───────────────────────────────────────────────────────

    async def _push_discovery(self) -> None:
        """Advertise the integration to HA via the Supervisor.

        Runs on every boot so HA recovers its discovery cache after a
        Supervisor restart. HA resolves the add-on hostname itself, so
        the payload only carries the integration token.
        """
        token_path = self._token_path()
        if not os.path.exists(token_path):
            log.debug("ha_bootstrap: no integration token file — skipping discovery")
            return
        try:
            with open(token_path, encoding="utf-8") as f:
                raw_token = f.read().strip()
        except OSError as exc:
            log.warning("ha_bootstrap: could not read %s: %s", token_path, exc)
            return
        if not raw_token:
            log.warning(
                "ha_bootstrap: empty integration token file — skipping discovery"
            )
            return

        payload = {
            "service": "socialhome",
            "config": {"token": raw_token},
        }
        if await self._sv.push_discovery(payload):
            log.info("ha_bootstrap: discovery pushed")

    # ─── Config-flag helpers ──────────────────────────────────────────────

    async def _is_done(self) -> bool:
        row = await self._db.fetchone(
            "SELECT value FROM instance_config WHERE key=?",
            (BOOTSTRAP_FLAG,),
        )
        return row is not None and row["value"] == "1"

    async def _mark_done(self) -> None:
        await self._db.enqueue(
            """
            INSERT INTO instance_config(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (BOOTSTRAP_FLAG, "1"),
        )

    def _token_path(self) -> str:
        return os.path.join(self._data_dir, INTEGRATION_TOKEN_FILENAME)


__all__ = [
    "BOOTSTRAP_FLAG",
    "HaBootstrap",
    "INTEGRATION_TOKEN_FILENAME",
    "INTEGRATION_TOKEN_LABEL",
]
