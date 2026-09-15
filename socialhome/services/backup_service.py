"""Backup / export — admin-only household snapshot.

The export is a tar.gz containing:

* ``manifest.json``         — schema version, instance id, export timestamp.
* ``tables/{name}.json``    — every row of every user-data table as JSON.
  Identity / KEK material is **excluded** so the backup can be shared
  without leaking private keys.
* ``media/...``             — every file under ``config.media_path`` is
  added with its relative path preserved.

Restore (:meth:`BackupService.restore`) is a destructive operation: it
refuses to run unless the target DB is empty (no users), to avoid an
admin nuking a populated household by mistake.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiofiles.os
import orjson

from ..db import AsyncDatabase

log = logging.getLogger(__name__)


# ─── What's safe to export ────────────────────────────────────────────────

#: Tables included in the export. The instance_identity row carries
#: KEK-encrypted private keys and is intentionally **excluded** —
#: re-pair from scratch on restore.
EXPORTABLE_TABLES: tuple[str, ...] = (
    "users",
    "remote_users",
    "user_blocks",
    "feed_posts",
    "feed_comments",
    "saved_posts",
    "spaces",
    "space_members",
    "space_aliases",
    "space_posts",
    "space_post_comments",
    "polls",
    "poll_options",
    "poll_votes",
    "schedule_poll_meta",
    "schedule_slots",
    "schedule_responses",
    "conversations",
    "conversation_members",
    "conversation_messages",
    "message_reactions",
    "task_lists",
    "tasks",
    "calendars",
    "calendar_events",
    "calendar_rsvps",
    "pages",
    "page_edit_history",
    "stickies",
    "shopping_list_items",
    "shopping_stores",
    "preferences",
    "household_theme",
    "space_themes",
    "space_links",
    "post_drafts",
    "notifications",
    "bazaar_listings",
    "bazaar_bids",
)

#: Tables whose names appear in the export but must NEVER appear on the
#: wire. The export will refuse to add them even if the caller passes
#: them through — defence-in-depth against future changes.
NEVER_EXPORT: frozenset[str] = frozenset(
    {
        "instance_identity",
        "space_keys",
        "pending_pairings",
        "remote_instances",  # contains KEK-wrapped session keys
        "api_tokens",  # token hashes — useless out of context but still secret
        "platform_tokens",
        "push_subscriptions",
    }
)


# ─── Errors ──────────────────────────────────────────────────────────────


class BackupError(Exception):
    """Base error class for backup operations."""


class BackupRestoreNotEmpty(BackupError):
    """Restore was attempted on a non-empty database."""


# ─── Service ─────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class BackupManifest:
    schema_version: int
    instance_id: str
    exported_at: str
    table_names: list[str]


class BackupService:
    """Stream a tar.gz of the household DB + media files.

    Parameters
    ----------
    db:
        Open AsyncDatabase.
    media_path:
        Directory of uploaded media to include verbatim.
    schema_version:
        Recorded in the manifest. The migration runner tracks the same
        number — restore checks they match.
    """

    __slots__ = ("_db", "_media_path", "_schema_version")

    def __init__(
        self,
        db: AsyncDatabase,
        media_path: str | Path,
        *,
        schema_version: int = 1,
    ) -> None:
        self._db = db
        self._media_path = Path(media_path)
        self._schema_version = schema_version

    # ─── Export ───────────────────────────────────────────────────────────

    async def export_to_bytes(self) -> bytes:
        """Build the export tarball entirely in memory.

        Returns the raw bytes — appropriate for small households. For
        very large exports the route handler can stream to disk via
        :meth:`export_to_path`.
        """
        manifest_payload, table_payloads = await self._collect_export_payloads()
        return await asyncio.to_thread(
            self._build_tarball_bytes, manifest_payload, table_payloads
        )

    async def export_to_path(self, target: str | Path) -> Path:
        target_path = Path(target)
        await aiofiles.os.makedirs(target_path.parent, exist_ok=True)
        manifest_payload, table_payloads = await self._collect_export_payloads()
        await asyncio.to_thread(
            self._build_tarball_to_path,
            target_path,
            manifest_payload,
            table_payloads,
        )
        return target_path

    async def _collect_export_payloads(
        self,
    ) -> tuple[bytes, list[tuple[str, bytes]]]:
        """Gather manifest + per-table JSON via async DB reads.

        The actual tar.gz writes happen in :meth:`_build_tarball_bytes`
        or :meth:`_build_tarball_to_path` — both run inside
        ``asyncio.to_thread`` because tarfile + gzip are sync CPU work.
        """
        instance_row = await self._db.fetchone(
            "SELECT instance_id FROM instance_identity WHERE id='self'",
        )
        instance_id = instance_row["instance_id"] if instance_row else "unknown"

        manifest = BackupManifest(
            schema_version=self._schema_version,
            instance_id=instance_id,
            exported_at=datetime.now(timezone.utc).isoformat(),
            table_names=list(EXPORTABLE_TABLES),
        )
        manifest_payload = orjson.dumps(
            {
                "schema_version": manifest.schema_version,
                "instance_id": manifest.instance_id,
                "exported_at": manifest.exported_at,
                "table_names": manifest.table_names,
            },
            option=orjson.OPT_INDENT_2,
        )

        table_payloads: list[tuple[str, bytes]] = []
        for table in EXPORTABLE_TABLES:
            if table in NEVER_EXPORT:
                continue
            rows = await self._dump_table(table)
            table_payloads.append(
                (table, orjson.dumps(rows)),
            )
        return manifest_payload, table_payloads

    def _build_tarball_bytes(
        self,
        manifest_payload: bytes,
        table_payloads: list[tuple[str, bytes]],
    ) -> bytes:
        buf = io.BytesIO()
        self._write_tar_sync(buf, manifest_payload, table_payloads)
        return buf.getvalue()

    def _build_tarball_to_path(
        self,
        target_path: Path,
        manifest_payload: bytes,
        table_payloads: list[tuple[str, bytes]],
    ) -> None:
        with target_path.open("wb") as fp:
            self._write_tar_sync(fp, manifest_payload, table_payloads)

    def _write_tar_sync(
        self,
        fp,
        manifest_payload: bytes,
        table_payloads: list[tuple[str, bytes]],
    ) -> None:
        with tarfile.open(fileobj=fp, mode="w:gz") as tar:
            self._tar_add_bytes(tar, "manifest.json", manifest_payload)
            for table, payload in table_payloads:
                self._tar_add_bytes(tar, f"tables/{table}.json", payload)
            self._tar_add_media(tar)

    async def _dump_table(self, table: str) -> list[dict]:
        try:
            rows = await self._db.fetchall(f"SELECT * FROM {table}")
        except Exception as exc:  # pragma: no cover
            log.warning("backup: skipping %s due to %s", table, exc)
            return []
        return [dict(r) for r in rows]

    def _tar_add_media(self, tar: tarfile.TarFile) -> None:
        if not self._media_path.exists():
            return
        for path in self._media_path.rglob("*"):
            if path.is_file():
                arcname = "media/" + str(path.relative_to(self._media_path))
                tar.add(path, arcname=arcname, recursive=False)

    @staticmethod
    def _tar_add_bytes(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(payload)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(info, io.BytesIO(payload))

    # ─── Restore ──────────────────────────────────────────────────────────

    async def restore_from_bytes(self, blob: bytes) -> None:
        """Restore from a tarball. Refuses if DB already has users."""
        await self._guard_db_is_empty()

        # Sync, CPU-bound phase: decompress the gzip stream and pull
        # every table + media member out of the tarball into memory.
        manifest_payload, table_payloads, media_files = await asyncio.to_thread(
            self._read_restore_tar, blob
        )

        manifest = orjson.loads(manifest_payload)
        if int(manifest.get("schema_version", -1)) != self._schema_version:
            raise BackupError(
                f"schema_version mismatch: backup={manifest.get('schema_version')!r} "
                f"current={self._schema_version}"
            )

        # Async phase: replay each table's rows through the DB writer
        # queue so they get the normal aiosqlite WAL coalescing.
        for table, payload in table_payloads:
            rows = orjson.loads(payload)
            await self._import_table(table, rows)

        # Sync, I/O-bound phase: write the media files onto disk.
        if media_files:
            await asyncio.to_thread(self._write_restore_media, media_files)

    def _read_restore_tar(
        self,
        blob: bytes,
    ) -> tuple[bytes, list[tuple[str, bytes]], list[tuple[str, bytes]]]:
        """Parse the restore tarball and pull every member into memory.

        Returns ``(manifest_payload, table_payloads, media_files)`` where
        media_files holds ``(relative_path, bytes)`` tuples ready to be
        written under ``self._media_path``.
        """
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            members = {m.name: m for m in tar.getmembers()}
            manifest_member = members.get("manifest.json")
            if manifest_member is None:
                raise BackupError("Backup missing manifest.json")
            data = tar.extractfile(manifest_member)
            if data is None:
                raise BackupError("Could not read manifest.json")
            manifest_payload = data.read()

            table_payloads: list[tuple[str, bytes]] = []
            for name, member in members.items():
                if not name.startswith("tables/") or not name.endswith(".json"):
                    continue
                if name == "manifest.json":
                    continue
                table = name[len("tables/") : -len(".json")]
                if table in NEVER_EXPORT:
                    log.warning("backup: skipping %s (in NEVER_EXPORT)", table)
                    continue
                if table not in EXPORTABLE_TABLES:
                    log.debug("backup: skipping unknown table %s", table)
                    continue
                fobj = tar.extractfile(member)
                if fobj is None:
                    continue
                table_payloads.append((table, fobj.read()))

            media_files: list[tuple[str, bytes]] = []
            for name, member in members.items():
                if not name.startswith("media/") or not member.isfile():
                    continue
                fobj = tar.extractfile(member)
                if fobj is None:
                    continue
                media_files.append((name[len("media/") :], fobj.read()))

        return manifest_payload, table_payloads, media_files

    def _write_restore_media(
        self,
        media_files: list[tuple[str, bytes]],
    ) -> None:
        for rel_name, payload in media_files:
            target = self._media_path / rel_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    async def _guard_db_is_empty(self) -> None:
        row = await self._db.fetchone("SELECT COUNT(*) AS n FROM users")
        if row and int(row["n"]) > 0:
            raise BackupRestoreNotEmpty(
                "refusing to restore: target database already has users; "
                "wipe the data dir first to restore",
            )

    async def _import_table(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        cols = list(rows[0].keys())
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(cols)
        sql = f"INSERT OR IGNORE INTO {table}({col_list}) VALUES({placeholders})"
        for row in rows:
            params = tuple(row.get(c) for c in cols)
            try:
                await self._db.enqueue(sql, params)
            except Exception as exc:
                log.warning("backup: row import failed for %s: %s", table, exc)
