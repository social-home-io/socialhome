"""One-time HA add-on bootstrap (§5.2).

Runs on startup when Social Home is deployed as a Home Assistant add-on.
The bootstrap:

* looks up the Home Assistant owner via the **user directory** and
  provisions them as the Social Home admin;
* generates a persistent API token for the HA integration to use;
* pushes a **Supervisor discovery** entry so the official HA
  integration can find us automatically.

Two narrow collaborators do these two jobs:

* :class:`HaUserDirectory` (HA Core, WS ``config/auth/list``) — the
  single source of HA user records.
* :class:`SupervisorClient` (Supervisor REST) — addon info +
  discovery push. The Supervisor's own ``/auth/list`` REST is
  deliberately not consulted; see #297 / #298 for the slug-vs-
  username gotchas a forked identity source introduces.

The bootstrap is idempotent — the ``ha_bootstrap_done`` flag in
``instance_config`` gates the one-off provisioning steps, while the
discovery push runs on every boot (so HA recovers from a restart even
before it next re-polls its discovery cache).
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...db import AsyncDatabase
from ...identity_bootstrap import derive_local_user_id
from .supervisor import SupervisorClient

if TYPE_CHECKING:
    from ...services.user_service import UserService
    from ..ha.providers import HaUserDirectory

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
    users:
        :class:`HaUserDirectory` from the haos adapter — the source of
        the HA owner record (auth-provider ``username`` + display
        ``name``). Backed by HA Core's WS ``config/auth/list``.
    supervisor:
        :class:`SupervisorClient` — addon info + discovery push.
        Identity data is NOT pulled from here; that's the directory's
        job.
    data_dir:
        Directory where the raw integration token is persisted so the
        discovery push can read it on subsequent boots. Typically
        ``config.data_dir`` — ``/data`` in add-on mode.
    user_service:
        :class:`UserService` used to follow a HA-side person rename onto
        the local row (matched by ``external_id``) so the rename cascades
        and federates. Optional so early-boot/test wiring can build a bare
        bootstrap; when absent the rename-follow step is skipped.
    """

    __slots__ = ("_db", "_users", "_sv", "_data_dir", "_user_service")

    def __init__(
        self,
        db: AsyncDatabase,
        users: HaUserDirectory,
        supervisor: SupervisorClient,
        data_dir: str,
        user_service: UserService | None = None,
    ) -> None:
        self._db = db
        self._users = users
        self._sv = supervisor
        self._data_dir = data_dir
        self._user_service = user_service

    # ─── Public entry point ───────────────────────────────────────────────

    async def run(self) -> None:
        """Run the full bootstrap. Idempotent.

        The owner is re-mirrored on *every* boot (not just first boot): a
        HA-side person rename drifts the HA username away from the stored
        ``users.username``, and :meth:`_mirror_owner` follows it onto the
        local row. The one-off steps (integration token, ``ha_bootstrap_done``
        flag) stay gated by :meth:`_is_done`.
        """
        owner = await self._users.get_owner()
        if owner is not None:
            await self._mirror_owner(
                username=owner.username,
                display_name=owner.display_name,
                external_id=owner.external_id,
            )
            if not await self._is_done():
                await self._generate_integration_token(owner.username)
                await self._mark_done()
                log.info(
                    "ha_bootstrap: admin provisioned as %r",
                    owner.username,
                )
        else:
            log.warning("ha_bootstrap: could not determine HA owner — skipping")

        await self._push_discovery()

    async def _mirror_owner(
        self,
        *,
        username: str,
        display_name: str,
        external_id: str | None,
    ) -> None:
        """Reconcile the local HA owner row with HA's current person record.

        Runs every boot. Matches the existing row by the stable
        ``external_id`` (the HA ``user_id``), so a HA-side rename is followed
        rather than orphaning the old row / inserting a duplicate:

        * existing row found, HA name changed → rename-follow via
          :meth:`UserService.apply_ha_username` (cascades + federates);
        * existing row found, name unchanged → no-op;
        * no row for this ``external_id`` → first provision (INSERT).
        """
        if external_id is not None and self._user_service is not None:
            existing = await self._db.fetchone(
                "SELECT username FROM users WHERE external_id=? AND source='ha'",
                (external_id,),
            )
            if existing is not None:
                # Known HA row — follow any HA-side rename onto it (no-op when
                # the name is unchanged). Keeps is_admin/source/external_id via
                # the cascade-preserving repo rename; never inserts a dupe.
                await self._user_service.apply_ha_username(external_id, username)
                await self._db.enqueue(
                    "UPDATE users SET is_admin=1, state='active', source='ha'"
                    " WHERE external_id=? AND source='ha'",
                    (external_id,),
                )
                return

        await self._provision_admin(
            username=username,
            display_name=display_name,
            external_id=external_id,
        )

    # ─── Provisioning ────────────────────────────────────────────────────

    async def _provision_admin(
        self,
        *,
        username: str,
        display_name: str,
        external_id: str | None,
    ) -> None:
        """Insert or re-enable the HA owner as a SH admin.

        Persists ``external_id`` — the HA ``user_id`` from
        ``config/auth/list[].id`` — so downstream joins (the
        picture lifter; future presence / device-tracker bridges)
        don't have to re-resolve username → id. The id is stable
        across HA display-name renames that would otherwise change
        the entity slug.

        The existing-row lookup is keyed on the stable ``external_id``
        when HA supplied one (the HA username may have drifted via a
        rename); it falls back to the username only when no
        ``external_id`` is known. :meth:`_mirror_owner` handles the
        rename-follow before this is reached, so the existing-row branch
        here just re-asserts ``is_admin`` / ``source`` without inserting
        a duplicate.
        """
        existing = None
        if external_id is not None:
            existing = await self._db.fetchone(
                "SELECT user_id FROM users WHERE external_id=? AND source='ha'",
                (external_id,),
            )
        if existing is None:
            existing = await self._db.fetchone(
                "SELECT user_id FROM users WHERE username=?",
                (username,),
            )
        if existing is not None:
            # Already provisioned — ensure is_admin=1 in case it was demoted
            # and stamp source='ha' + the latest external_id so the row
            # tracks any HA-side rotation.
            await self._db.enqueue(
                "UPDATE users SET is_admin=1, state='active', source='ha',"
                " external_id=? WHERE user_id=?",
                (external_id, existing["user_id"]),
            )
            return

        # HAOS owners are username-anchored (not uuid-anchored like
        # standalone users): the bootstrap re-mirrors HA persons on every
        # boot, so the derived user_id must stay deterministic + stable
        # across re-runs. identity_anchor == username keeps it legacy-style
        # (works on all peers) and ensures the column is never NULL. Shared
        # with the standalone / ha admin-mirror paths so there is exactly one
        # id-minting rule for username-anchored local users.
        user_id = await derive_local_user_id(self._db, username)

        await self._db.enqueue(
            """
            INSERT INTO users(user_id, username, display_name, is_admin,
                              created_at, source, external_id, identity_anchor,
                              handle)
            VALUES(?, ?, ?, 1, ?, 'ha', ?, ?, ?)
            """,
            (
                user_id,
                username,
                display_name or username,
                datetime.now(timezone.utc).isoformat(),
                external_id,
                username,
                username,
            ),
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
            # Cold path: bootstrap runs once at add-on startup before the
            # HTTP server begins serving — sync file writes don't block
            # any in-flight coroutines.
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
        Supervisor restart. The payload carries the integration
        token plus the add-on's reachable ``host`` + ``port`` (read
        from ``GET /addons/self/info``) so HA Core's ``socialhome``
        config flow can talk to us straight away — no slug-to-host
        substitution on the integration side.
        """
        token_path = self._token_path()
        # Cold path: _push_discovery is called from the boot-time run()
        # method, so reading the token file synchronously here doesn't
        # contend with any request-serving coroutines.
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

        info = await self._sv.get_self_info()
        if info is None:
            log.warning(
                "ha_bootstrap: /addons/self/info missing hostname/ingress_port"
                " — skipping discovery"
            )
            return

        payload = {
            "service": "socialhome",
            "config": {
                "host": info.hostname,
                "port": info.ingress_port,
                "token": raw_token,
            },
        }
        if await self._sv.push_discovery(payload):
            log.info(
                "ha_bootstrap: discovery pushed (host=%s port=%d)",
                info.hostname,
                info.ingress_port,
            )

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
