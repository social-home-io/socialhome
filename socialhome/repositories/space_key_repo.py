"""Per-space content encryption keys (§4.3, §25.8.20–21).

Each space has one or more **epoch** keys. An epoch is incremented when
the key is rotated (member ban, admin departure, scheduled rekey). All
content for that epoch is encrypted under that key; readers select the
key by epoch number on the inbound envelope.

Keys are stored KEK-encrypted (see :class:`KeyManager`). The repository
returns the ciphertext as-is — service code calls
:meth:`KeyManager.decrypt` to obtain the raw key bytes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from .base import rows_to_dicts

# Domain dataclass lives in ``socialhome/domain/space_key.py``;
# re-exported here so existing repo-level imports keep working.
from ..domain.space_key import SpaceKey  # noqa: F401,E402


@runtime_checkable
class AbstractSpaceKeyRepo(Protocol):
    async def save(
        self, key: SpaceKey, *, verified_pin: tuple[int, str] | None = None
    ) -> bool: ...
    async def get(self, space_id: str, epoch: int) -> SpaceKey | None: ...
    async def get_latest(self, space_id: str) -> SpaceKey | None: ...
    async def list_for_space(self, space_id: str) -> list[SpaceKey]: ...
    async def next_epoch(self, space_id: str) -> int: ...
    async def reset_to(
        self, key: SpaceKey, *, authority_epoch: int, older_than: int | None = None
    ) -> int: ...
    async def set_writer_cert(
        self, space_id: str, epoch: int, cert_json: str
    ) -> bool: ...
    async def get_previous(self, space_id: str, epoch: int) -> SpaceKey | None: ...
    async def get_writer_cert(self, space_id: str, epoch: int) -> str | None: ...


class SqliteSpaceKeyRepo:
    """SQLite-backed :class:`AbstractSpaceKeyRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def save(
        self, key: SpaceKey, *, verified_pin: tuple[int, str] | None = None
    ) -> bool:
        """Upsert one epoch key, stamped with the pin epoch it was written
        under.

        ``verified_pin`` (v_44) is ``(authority_key_epoch,
        identity_public_key)`` of the space key whose signature authorized
        this key (a rekey, a subscriber handoff). When given, the write lands
        ONLY while the space still pins exactly that key — same statement —
        so a rekey verified against a key that a rotation retired in the
        meantime is dropped (``False``) instead of stored as a new-key row.
        """
        created = key.created_at or datetime.now(timezone.utc).isoformat()
        if verified_pin is not None:
            epoch, pk = verified_pin
            changed = await self._db.enqueue_rowcount(
                """
                INSERT INTO space_keys(space_id, epoch, content_key_hex,
                                       created_at, rotated_by, authority_epoch)
                SELECT ?, ?, ?, ?, ?, ?
                WHERE EXISTS (
                    SELECT 1 FROM spaces WHERE id=? AND authority_key_epoch=?
                    AND identity_public_key=?
                )
                ON CONFLICT(space_id, epoch) DO UPDATE SET
                    content_key_hex=excluded.content_key_hex,
                    rotated_by=excluded.rotated_by,
                    authority_epoch=excluded.authority_epoch
                """,
                (
                    key.space_id,
                    key.epoch,
                    key.content_key_hex,
                    created,
                    key.rotated_by,
                    epoch,
                    key.space_id,
                    epoch,
                    pk,
                ),
            )
            return changed > 0
        await self._db.enqueue(
            """
            INSERT INTO space_keys(space_id, epoch, content_key_hex, created_at,
                                   rotated_by, authority_epoch)
            VALUES(?, ?, ?, ?, ?,
                   (SELECT authority_key_epoch FROM spaces WHERE id=?))
            ON CONFLICT(space_id, epoch) DO UPDATE SET
                content_key_hex=excluded.content_key_hex,
                rotated_by=excluded.rotated_by,
                authority_epoch=excluded.authority_epoch
            """,
            (
                key.space_id,
                key.epoch,
                key.content_key_hex,
                created,
                key.rotated_by,
                key.space_id,
            ),
        )
        return True

    async def get(self, space_id: str, epoch: int) -> SpaceKey | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_keys WHERE space_id=? AND epoch=?",
            (space_id, epoch),
        )
        return _row(row) if row else None

    async def get_latest(self, space_id: str) -> SpaceKey | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_keys WHERE space_id=? ORDER BY epoch DESC LIMIT 1",
            (space_id,),
        )
        return _row(row) if row else None

    async def list_for_space(self, space_id: str) -> list[SpaceKey]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_keys WHERE space_id=? ORDER BY epoch",
            (space_id,),
        )
        return [_row(r) for r in rows_to_dicts(rows)]

    async def next_epoch(self, space_id: str) -> int:
        row = await self._db.fetchone(
            "SELECT COALESCE(MAX(epoch), -1) AS m FROM space_keys WHERE space_id=?",
            (space_id,),
        )
        return int(row["m"]) + 1 if row else 0

    async def reset_to(
        self, key: SpaceKey, *, authority_epoch: int, older_than: int | None = None
    ) -> int:
        """Make ``key`` the space's current epoch key, in ONE transaction.

        Only the v_44 authority-rotation baseline reset calls this. Every
        epoch above ``key.epoch`` that was written under an authority key
        OLDER than ``older_than`` (default ``authority_epoch``) is deleted —
        those were minted while a now-revoked household could still sign
        rekeys — and ``key`` replaces whatever sits at its epoch, stamped
        with ``authority_epoch``, the pin it is installed under. Epochs above
        it written under a newer key (newer owner rekeys that arrived first)
        are kept. Returns how many rows were deleted.
        """
        created = key.created_at or datetime.now(timezone.utc).isoformat()
        cutoff = authority_epoch if older_than is None else older_than

        def _run(conn) -> int:
            cur = conn.execute(
                "DELETE FROM space_keys WHERE space_id=? AND epoch > ?"
                " AND authority_epoch < ?",
                (key.space_id, key.epoch, cutoff),
            )
            conn.execute(
                """
                INSERT INTO space_keys(space_id, epoch, content_key_hex,
                                       created_at, rotated_by, authority_epoch)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(space_id, epoch) DO UPDATE SET
                    content_key_hex=excluded.content_key_hex,
                    rotated_by=excluded.rotated_by,
                    authority_epoch=excluded.authority_epoch,
                    writer_cert=NULL
                """,
                (
                    key.space_id,
                    key.epoch,
                    key.content_key_hex,
                    created,
                    key.rotated_by,
                    authority_epoch,
                ),
            )
            return int(cur.rowcount or 0)

        return await self._db.transact(_run)

    async def set_writer_cert(self, space_id: str, epoch: int, cert_json: str) -> bool:
        """Store the writer cert this household holds for ``(space, epoch)``
        (v_49, migration 0074). Only onto an existing key row — ``False``
        when we hold no key for that epoch (nothing is created)."""
        changed = await self._db.enqueue_rowcount(
            "UPDATE space_keys SET writer_cert=? WHERE space_id=? AND epoch=?",
            (cert_json, space_id, epoch),
        )
        return changed > 0

    async def get_previous(self, space_id: str, epoch: int) -> SpaceKey | None:
        """The newest key BELOW ``epoch`` (epochs may jump), or ``None``."""
        row = await self._db.fetchone(
            "SELECT * FROM space_keys WHERE space_id=? AND epoch<?"
            " ORDER BY epoch DESC LIMIT 1",
            (space_id, epoch),
        )
        return _row(row) if row else None

    async def get_writer_cert(self, space_id: str, epoch: int) -> str | None:
        """The stored writer cert JSON for ``(space, epoch)``, or ``None``."""
        row = await self._db.fetchone(
            "SELECT writer_cert FROM space_keys WHERE space_id=? AND epoch=?",
            (space_id, epoch),
        )
        if row is None or row["writer_cert"] is None:
            return None
        return str(row["writer_cert"])


def _row(row) -> SpaceKey:
    return SpaceKey(
        space_id=row["space_id"],
        epoch=int(row["epoch"]),
        content_key_hex=row["content_key_hex"],
        created_at=row["created_at"],
        rotated_by=row["rotated_by"] if "rotated_by" in row.keys() else None,
    )
