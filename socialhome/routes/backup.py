"""Backup routes — HA-mode only.

The Home Assistant Supervisor periodically snapshots an add-on's
``/data`` directory. SQLite in WAL mode keeps freshly-committed pages
in a sidecar ``-wal`` file until a checkpoint promotes them into the
main DB. Without a checkpoint the snapshot can omit recent writes —
not a corrupted DB exactly, but data loss the user wouldn't expect.

These routes solve that two ways:

* ``POST /api/backup/pre_backup`` — Supervisor calls this immediately
  before snapshotting. The handler issues
  ``PRAGMA wal_checkpoint(TRUNCATE)`` so the snapshot captures every
  durable byte. The Supervisor's add-on backup config can hook this
  via the ``backup_pre`` script slot (see ``run.sh``).

* ``POST /api/backup/post_backup`` — symmetric no-op confirmation,
  reserved for future quiesce/resume logic.

* ``GET /api/backup/export`` / ``POST /api/backup/import`` — manual
  user-driven export/restore of all user-data tables + the media
  directory. Same admin-only gate.

In ``standalone`` mode none of these are mounted: the operator owns
the host filesystem and can use ``sqlite3`` / ``rsync`` directly.
"""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from ..auth import require_admin
from ..services.backup_service import (
    BackupError,
    BackupRestoreNotEmpty,
)
from .base import BaseView


class BackupPreView(BaseView):
    """``POST /api/backup/pre_backup`` — quiesce writes + checkpoint WAL."""

    async def post(self) -> web.Response:
        require_admin(self.request)
        db = self.svc(K.db_key)
        busy, log_frames, ckpt_frames = await db.checkpoint("TRUNCATE")
        return self._json(
            {
                "ok": True,
                "busy": busy,
                "log_frames": log_frames,
                "checkpointed_frames": ckpt_frames,
            }
        )


class BackupPostView(BaseView):
    """``POST /api/backup/post_backup`` — acknowledgement-only handshake."""

    async def post(self) -> web.Response:
        require_admin(self.request)
        return self._json({"ok": True})


class BackupExportView(BaseView):
    """``GET /api/backup/export`` — download a full backup archive."""

    async def get(self) -> web.Response:
        require_admin(self.request)
        db = self.svc(K.db_key)
        await db.checkpoint("TRUNCATE")
        svc = self.svc(K.backup_service_key)
        body = await svc.export_to_bytes()
        return web.Response(
            body=body,
            content_type="application/gzip",
            headers={
                "Content-Disposition": 'attachment; filename="socialhome-backup.tar.gz"',
            },
        )


class BackupImportView(BaseView):
    """``POST /api/backup/import`` — restore from uploaded archive."""

    async def post(self) -> web.Response:
        require_admin(self.request)
        body = await self.request.read()
        if not body:
            return web.json_response({"error": "empty body"}, status=422)
        svc = self.svc(K.backup_service_key)
        try:
            await svc.restore_from_bytes(body)
        except BackupRestoreNotEmpty as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except BackupError as exc:
            return web.json_response({"error": str(exc)}, status=422)
        return self._json({"ok": True})
