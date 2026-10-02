"""One-shot post-restore peer reconnect (§ Recovery Kit).

After a Recovery Kit restore + process restart, peers still hold our OLD
inbox URL. On the FIRST boot after a restore this service fans
``URL_UPDATED`` out to every confirmed peer so they update our inbox URL and
re-establish their transport; the existing §4.4 sync then resumes over the
re-established channel.

It runs **exactly once**, guarded by the ``recovery.reconnected_at``
instance_config marker: the fan-out fires only when a restore happened
(``recovery.recovered_at`` set) but no reconnect has been recorded yet. A
missing federation base leaves the marker unset so a later boot — once the
base is configured — retries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .backup_service import BACKUP_RESTORED_AT_KEY
from .recovery_kit_service import RECOVERED_AT_KEY

if TYPE_CHECKING:
    from ..db import AsyncDatabase
    from ..platform.adapter import PlatformAdapter
    from .space_authority_rotation_service import SpaceAuthorityRotationService
    from .url_update_outbound import UrlUpdateOutbound

log = logging.getLogger(__name__)

#: instance_config key recording the wall-clock time the one-shot reconnect ran.
RECONNECTED_AT_KEY = "recovery.reconnected_at"

#: instance_config key recording WHICH restore (its timestamp marker) the
#: post-restore authority rotation already ran for (v_44).
AUTHORITY_ROTATED_FOR_KEY = "authority.rotated_after_restore"


class RecoveryReconnectService:
    """One-shot post-restore peer reconnect (see module docstring)."""

    __slots__ = ("_db", "_url_update", "_adapter")

    def __init__(
        self,
        db: "AsyncDatabase",
        url_update: "UrlUpdateOutbound",
        adapter: "PlatformAdapter",
    ) -> None:
        self._db = db
        self._url_update = url_update
        self._adapter = adapter

    async def maybe_reconnect(self) -> bool:
        """Fan ``URL_UPDATED`` out to all confirmed peers on first boot after
        a restore, then mark the reconnect done.

        Returns ``True`` if it ran the fan-out, ``False`` otherwise. Never
        raises — startup must not break.
        """
        if not await self._get(RECOVERED_AT_KEY):
            return False
        if await self._get(RECONNECTED_AT_KEY):
            return False
        try:
            base = await self._adapter.get_federation_base()
        except Exception:
            log.warning(
                "post-restore reconnect: get_federation_base failed",
                exc_info=True,
            )
            return False
        if not base:
            # No reachable base yet — leave the marker unset so a later boot
            # (once the base is configured) retries the fan-out.
            log.warning(
                "post-restore reconnect: no federation base configured; "
                "peers keep the kit's stored inbox URL until one is set"
            )
            return False
        try:
            n = await self._url_update.publish(new_inbox_base_url=base)
            log.info(
                "post-restore reconnect: notified %d confirmed peer(s) "
                "of inbox base %s",
                n,
                base,
            )
        except Exception:
            log.warning(
                "post-restore reconnect: URL_UPDATED fan-out failed",
                exc_info=True,
            )
            # URL_UPDATED is enqueued durably per peer; mark done so we don't
            # re-fan every boot. The outbox retries delivery.
        await self._set(RECONNECTED_AT_KEY, datetime.now(timezone.utc).isoformat())
        return True

    async def maybe_rotate_space_authority(
        self, rotation: "SpaceAuthorityRotationService"
    ) -> int:
        """Once per restore, rotate every hosted space's authority key (v_44).

        A backup or Recovery Kit restore brings back ``spaces`` rows as they
        were: possibly a key an admin revoked since still holds, and an epoch
        the members already moved past (they refuse a lower one, so the owner
        would be locked out of its own space). A fresh rotation — epoch at
        least wall-clock seconds — puts the owner ahead again. Keyed on the
        restore's own timestamp, so a second restore rotates again and a
        plain reboot never does. Never raises.
        """
        marker = "|".join(
            v or ""
            for v in (
                await self._get(RECOVERED_AT_KEY),
                await self._get(BACKUP_RESTORED_AT_KEY),
            )
        )
        if marker == "|" or await self._get(AUTHORITY_ROTATED_FOR_KEY) == marker:
            return 0
        try:
            n = await rotation.rotate_hosted_after_restore()
        except Exception:
            log.warning("post-restore authority rotation failed", exc_info=True)
            return 0
        log.info("post-restore: rotated the authority key of %d hosted space(s)", n)
        await self._set(AUTHORITY_ROTATED_FOR_KEY, marker)
        return n

    async def _get(self, key: str) -> str | None:
        row = await self._db.fetchone(
            "SELECT value FROM instance_config WHERE key=?", (key,)
        )
        return None if row is None else (row["value"] or None)

    async def _set(self, key: str, value: str) -> None:
        await self._db.enqueue(
            "INSERT INTO instance_config(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
