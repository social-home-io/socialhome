"""Recovery Kit service — build + restore of a household's trust layer.

A "Recovery Kit" (``.shrk`` file) lets a household reconstitute the SAME
``instance_id`` on fresh hardware: it captures the **trust layer** —
``instance_identity`` (the KEK-wrapped Ed25519 seed + routing secret),
``remote_instances`` (KEK-wrapped per-peer session keys), ``spaces`` (every
space row — for owned spaces this carries the KEK-wrapped signing seed) and
``space_keys`` (KEK-wrapped per-space content keys) — together with the
``.kek_salt`` that the runtime KEK is derived from, all sealed behind a
user passphrase by
:mod:`socialhome.services.recovery_crypto`.

``spaces`` is included for two reasons: it is the FK parent of ``space_keys``
(restoring it first lets the FK resolve naturally on a fresh DB, no PRAGMA
games), and for owned spaces it carries the signing authority you need to
remain a space owner after recovery. ``spaces`` rows also re-appear in the
shareable
data backup, but every restore inserts with ``INSERT OR IGNORE``, so a
double-restore is idempotent and each space's content key stays paired with
its row.

This module does **whole-table dump / restore with raw SQL**. That is an
explicitly-allowed exception to the no-SQL-outside-``repositories/`` rule,
exactly like :mod:`socialhome.services.backup_service` and
:mod:`socialhome.services.data_export_service`: a snapshot/restore that copies
rows verbatim has no per-row domain logic to push into a repo.

KEK-wrapped column values are dumped and restored **verbatim** (still
wrapped). The kit also carries the ``.kek_salt`` so the restored host derives
the identical KEK and can decrypt those wrapped values — a wrapped value
without its salt is useless. Restore therefore writes ``.kek_salt`` first,
runs a **KEK self-test** (decrypt the wrapped identity seed) before touching
the database, and refuses to overwrite a populated instance — fail closed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import aiofiles
import aiofiles.os
import orjson

from ..crypto import b64url_decode, b64url_encode
from ..db import AsyncDatabase
from ..infrastructure.key_manager import KeyManager, KeyManagerError
from .recovery_crypto import seal_kit, unseal_kit

log = logging.getLogger(__name__)

#: The trust-layer tables captured by the kit, in restore order. Ordered
#: parents-first so FK constraints hold during a clean insert: ``spaces`` is
#: the FK parent of ``space_keys`` (and is itself FK-free), so restoring it
#: before ``space_keys`` lets the reference resolve naturally on a fresh DB —
#: no PRAGMA toggling. ``instance_identity`` / ``remote_instances`` /
#: ``space_keys`` hold KEK-wrapped secrets, dumped/restored verbatim (wrapped).
TRUST_TABLES: tuple[str, ...] = (
    "instance_identity",
    "remote_instances",
    "spaces",  # parent of space_keys; itself FK-free
    "space_keys",
)

#: instance_config key recording the wall-clock time a restore completed.
RECOVERED_AT_KEY = "recovery.recovered_at"

_SALT_FILENAME = ".kek_salt"


class RecoveryRestoreError(RuntimeError):
    """Restore cannot proceed (instance already populated, or KEK self-test
    failed), or a kit cannot be built (no identity to back up)."""


class RecoveryKitService:
    """Build and restore passphrase-sealed trust-layer Recovery Kits."""

    __slots__ = ("_data_dir", "_db")

    def __init__(self, db: AsyncDatabase, data_dir: str | Path) -> None:
        self._db = db
        self._data_dir = Path(data_dir)

    # ── Build ──────────────────────────────────────────────────────────────

    async def build_kit(self, passphrase: str) -> bytes:
        """Dump the trust tables + ``.kek_salt``, return sealed ``.shrk`` bytes.

        The dump includes ``spaces`` (the FK parent of ``space_keys`` and the
        carrier of owned-space signing authority) alongside the wrapped-secret
        tables. ``spaces`` also re-appears in the shareable data backup;
        restore uses ``INSERT OR IGNORE`` so a double-restore is idempotent.

        Raises :class:`RecoveryRestoreError` when there is no identity to back
        up, or the ``.kek_salt`` is missing.
        """
        ident = await self._db.fetchone(
            "SELECT instance_id, created_at FROM instance_identity WHERE id='self'",
        )
        if ident is None:
            raise RecoveryRestoreError("no identity to back up")
        instance_id = ident["instance_id"]

        tables: dict[str, list[dict]] = {}
        for table in TRUST_TABLES:
            rows = await self._db.fetchall(f"SELECT * FROM {table}")
            tables[table] = [dict(r) for r in rows]

        salt = await self._read_salt()
        if salt is None:
            raise RecoveryRestoreError(
                f"{_SALT_FILENAME} missing — cannot build a recovery kit "
                "without the KEK salt",
            )

        payload = orjson.dumps(
            {"kek_salt": b64url_encode(salt), "tables": tables},
        )
        created_at_now = datetime.now(UTC).isoformat()
        return seal_kit(
            payload, passphrase, instance_id=instance_id, created_at=created_at_now
        )

    # ── Restore ────────────────────────────────────────────────────────────

    async def restore_kit(self, kit_bytes: bytes, passphrase: str) -> str:
        """Reconstitute the trust layer on a fresh instance.

        Returns the restored ``instance_id``. Raises
        :class:`RecoveryRestoreError` if an identity already exists or the KEK
        self-test fails; propagates ``RecoveryKitError`` /
        ``UnsupportedRecoverySuite`` from the codec (wrong passphrase, tamper,
        unknown suite). Fails closed: ``.kek_salt`` is written and the KEK is
        self-tested **before** any row is inserted.
        """
        # Refuse unless EVERY trust table is empty — not just instance_identity.
        # A partially-provisioned box (peers/spaces present, no identity) would
        # otherwise be left Frankensteined: INSERT OR IGNORE keeps the stale
        # rows while the salt overwrite renders their KEK-wrapped keys
        # permanently undecryptable. Restore only into a genuinely empty box.
        for table in TRUST_TABLES:
            row = await self._db.fetchone(f"SELECT 1 FROM {table} LIMIT 1")
            if row is not None:
                raise RecoveryRestoreError(
                    "instance is not empty; restore only into a fresh instance",
                )

        header, payload = unseal_kit(kit_bytes, passphrase)
        salt, tables = self._parse_payload(payload)

        # Write the salt FIRST (overwriting any startup-minted random salt) so
        # KeyManager.from_data_dir derives the kit's KEK, then self-test. On any
        # failure after this point the box is still empty, so a best-effort salt
        # cleanup returns it to a pristine "no salt" state.
        await self._write_salt(salt)
        self_row = self._find_self_row(tables)
        try:
            await asyncio.to_thread(self._kek_self_test, self_row)
            await self._insert_trust_tables(tables)
        except Exception:
            await self._remove_salt()
            raise

        await self._db.enqueue(
            "INSERT INTO instance_config(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (RECOVERED_AT_KEY, datetime.now(UTC).isoformat()),
        )

        if self_row is not None and self_row.get("instance_id"):
            return str(self_row["instance_id"])
        return str(header["instance_id"])

    # ── Reset ──────────────────────────────────────────────────────────────

    async def reset_trust_layer(self) -> None:
        """Delete the auto-minted trust rows so a fresh-box restore can proceed.

        Only safe pre-setup (throwaway identity). Child-first delete order keeps
        FK enforcement happy; ON DELETE CASCADE clears any dependents. Whole-table
        raw SQL — same documented exception as build_kit/restore_kit.
        """

        def _run(conn: sqlite3.Connection) -> None:
            for table in (
                "space_keys",
                "spaces",
                "remote_instances",
                "instance_identity",
            ):
                conn.execute(f"DELETE FROM {table}")

        await self._db.transact(_run)

    # ── Helpers ──────────────────────────────────────────────────────────--

    @staticmethod
    def _find_self_row(tables: dict[str, list[dict]]) -> dict | None:
        for row in tables.get("instance_identity", []):
            if row.get("id") == "self":
                return row
        return None

    def _kek_self_test(self, self_row: dict | None) -> None:
        """Prove the runtime KEK (from the just-written salt) decrypts the
        wrapped identity seed. Run BEFORE inserting any row so a salt/KEK
        mismatch never leaves a half-restored, undecryptable instance."""
        if self_row is None:
            raise RecoveryRestoreError(
                "recovery kit has no instance_identity self row",
            )
        km = KeyManager.from_data_dir(self._data_dir)
        wrapped = self_row.get("identity_private_key")
        try:
            seed = km.decrypt(str(wrapped))
        except KeyManagerError as exc:
            raise RecoveryRestoreError(
                "KEK self-test failed — wrong .kek_salt / KEK passphrase mode mismatch",
            ) from exc
        if len(seed) != 32:
            raise RecoveryRestoreError(
                "KEK self-test failed — wrong .kek_salt / KEK passphrase mode mismatch",
            )

    async def _insert_trust_tables(self, tables: dict[str, list[dict]]) -> None:
        """Insert all trust rows in one atomic transaction, parents-first.

        ``TRUST_TABLES`` is ordered so every FK parent precedes its child —
        ``spaces`` before ``space_keys`` — so on a fresh (empty) instance the
        FK from ``space_keys.space_id`` resolves against the ``spaces`` row
        inserted moments earlier. FK enforcement stays ON throughout; no
        PRAGMA toggling, no manual COMMIT/BEGIN. ``transact`` owns the
        transaction lifecycle, mirroring ``backup_service._import_table``.
        """

        def _run(conn: sqlite3.Connection) -> None:
            for table in TRUST_TABLES:
                for row in tables.get(table, []):
                    self._insert_row(conn, table, row)

        await self._db.transact(_run)

    @staticmethod
    def _insert_row(conn: sqlite3.Connection, table: str, row: dict) -> None:
        if not row:
            return
        cols = list(row.keys())
        col_list = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT OR IGNORE INTO {table}({col_list}) VALUES({placeholders})"
        conn.execute(sql, tuple(row[c] for c in cols))

    async def _read_salt(self) -> bytes | None:
        path = self._data_dir / _SALT_FILENAME
        if not await aiofiles.os.path.isfile(path):
            return None
        async with aiofiles.open(path, "rb") as f:
            return await f.read()

    async def _write_salt(self, salt: bytes) -> None:
        await aiofiles.os.makedirs(self._data_dir, exist_ok=True)
        path = self._data_dir / _SALT_FILENAME
        async with aiofiles.open(path, "wb") as f:
            await f.write(salt)
        # aiofiles.os has no chmod; run the sync call off the event loop.
        await asyncio.to_thread(os.chmod, path, 0o600)

    async def _remove_salt(self) -> None:
        """Best-effort removal of the just-written salt after a failed restore."""
        path = self._data_dir / _SALT_FILENAME
        if await aiofiles.os.path.isfile(path):
            await aiofiles.os.remove(path)

    @staticmethod
    def _parse_payload(payload: bytes) -> tuple[bytes, dict[str, list[dict]]]:
        """Validate the decrypted kit payload shape, returning (salt, tables).

        The kit is authenticated (a valid passphrase produced it), but
        authenticity does not prove the contents are well-formed — a crafted
        kit could still carry a malformed payload. Map every shape error to
        :class:`RecoveryRestoreError` so callers (and a future route) get a
        domain error, never a bare ``KeyError`` / ``AttributeError``.
        """
        try:
            data = orjson.loads(payload)
        except orjson.JSONDecodeError as exc:
            raise RecoveryRestoreError(
                "recovery kit payload is not valid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise RecoveryRestoreError("recovery kit payload must be a JSON object")
        raw_salt = data.get("kek_salt")
        tables = data.get("tables")
        if not isinstance(raw_salt, str) or not isinstance(tables, dict):
            raise RecoveryRestoreError(
                "recovery kit payload is missing kek_salt/tables"
            )
        try:
            salt = b64url_decode(raw_salt)
        except (ValueError, TypeError) as exc:
            raise RecoveryRestoreError(
                "recovery kit .kek_salt is not valid b64"
            ) from exc
        if len(salt) != KeyManager.KEK_BYTES:
            raise RecoveryRestoreError(
                f"recovery kit .kek_salt has unexpected length {len(salt)}",
            )
        for name, rows in tables.items():
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise RecoveryRestoreError(
                    f"recovery kit table {name!r} is not a list of rows",
                )
        return salt, tables
