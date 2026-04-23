"""Child Protection repository — guardians, minor blocks, age-gate state.

Wraps the SQL surface used by :class:`ChildProtectionService` so the
service depends only on the abstract protocol — never on raw SQL or
the SQLite implementation.

Tables touched:

* ``users`` — the ``child_protection_enabled``, ``is_minor``,
  ``declared_age``, ``date_of_birth`` and ``is_admin`` columns.
* ``cp_guardians`` — guardian ↔ minor mapping.
* ``cp_minor_blocks`` — per-minor user block list (§CP.F2).
* ``space_members`` — read for the F2 auto-removal helper.
* ``spaces`` — ``min_age`` + ``target_audience`` columns (§CP.F1).
* ``guardian_audit_log`` — append-only audit trail.
* ``remote_instances`` — read for the §CP.F3 DM gate.
"""

from __future__ import annotations

import json
import uuid
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase


# ─── Protocol ────────────────────────────────────────────────────────────


@runtime_checkable
class AbstractCpRepo(Protocol):
    # Protection toggle
    async def enable_protection(
        self,
        *,
        minor_username: str,
        declared_age: int,
        date_of_birth: str | None,
    ) -> None: ...

    async def disable_protection(self, minor_username: str) -> None: ...

    # Guardians
    async def add_guardian(
        self,
        *,
        minor_user_id: str,
        guardian_user_id: str,
        granted_by: str,
    ) -> None: ...
    async def remove_guardian(
        self,
        *,
        minor_user_id: str,
        guardian_user_id: str,
    ) -> None: ...
    async def list_guardians(self, minor_user_id: str) -> list[str]: ...
    async def list_minors_for_guardian(
        self,
        guardian_user_id: str,
    ) -> list[str]: ...
    async def is_guardian_of(
        self,
        guardian_user_id: str,
        minor_user_id: str,
    ) -> bool: ...

    # Minor blocks (§CP.F2)
    async def block_user(
        self,
        *,
        minor_user_id: str,
        blocked_user_id: str,
        blocked_by: str,
    ) -> None: ...
    async def unblock_user(
        self,
        *,
        minor_user_id: str,
        blocked_user_id: str,
    ) -> None: ...
    async def is_blocked_for_minor(
        self,
        minor_user_id: str,
        other_user_id: str,
    ) -> bool: ...
    async def list_blocks_for_minor(
        self,
        minor_user_id: str,
    ) -> list[dict]: ...
    async def remove_minor_from_blocked_user_spaces(
        self,
        *,
        minor_user_id: str,
        blocked_user_id: str,
    ) -> None: ...

    # Audit log
    async def append_audit(
        self,
        *,
        minor_id: str,
        guardian_id: str,
        action: str,
        detail: dict | None = None,
    ) -> None: ...
    async def list_audit_log(
        self,
        minor_user_id: str,
        *,
        limit: int,
    ) -> list[dict]: ...

    # Membership audit — append-only trail of space-membership changes
    # that affected a minor. Distinct from ``guardian_audit_log``: this
    # table records *system-driven* actions (admin adds/removes, auto-
    # removal from banned-user spaces) whereas ``guardian_audit_log``
    # records *guardian-driven* actions. Both surface in the parent
    # dashboard.
    async def append_membership_audit(
        self,
        *,
        minor_user_id: str,
        space_id: str,
        action: str,  # "joined" | "removed" | "blocked"
        actor_id: str,
    ) -> None: ...
    async def list_membership_audit(
        self,
        minor_user_id: str,
        *,
        limit: int,
    ) -> list[dict]: ...
    async def is_minor(self, user_id: str) -> bool: ...

    # Age gate (§CP.F1)
    async def space_exists(self, space_id: str) -> bool: ...
    async def update_space_age_gate(
        self,
        *,
        space_id: str,
        min_age: int,
        target_audience: str,
    ) -> None: ...
    async def get_space_age_gate(self, space_id: str) -> dict: ...
    async def get_user_protection(self, user_id: str) -> dict | None: ...

    # DM gate (§CP.F3)
    async def get_remote_instance_status(
        self,
        instance_id: str,
    ) -> dict | None: ...

    # Admin check
    async def is_admin(self, user_id: str) -> bool: ...


# ─── SQLite implementation ───────────────────────────────────────────────


class SqliteCpRepo:
    """SQLite-backed :class:`AbstractCpRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── protection toggle ──────────────────────────────────────────────

    async def enable_protection(
        self,
        *,
        minor_username: str,
        declared_age: int,
        date_of_birth: str | None,
    ) -> None:
        await self._db.enqueue(
            "UPDATE users SET child_protection_enabled=1, is_minor=1,"
            " declared_age=?, date_of_birth=? WHERE username=?",
            (declared_age, date_of_birth, minor_username),
        )

    async def disable_protection(self, minor_username: str) -> None:
        await self._db.enqueue(
            "UPDATE users SET child_protection_enabled=0, is_minor=0,"
            " declared_age=NULL WHERE username=?",
            (minor_username,),
        )

    # ── guardians ──────────────────────────────────────────────────────

    async def add_guardian(
        self,
        *,
        minor_user_id: str,
        guardian_user_id: str,
        granted_by: str,
    ) -> None:
        await self._db.enqueue(
            "INSERT OR IGNORE INTO cp_guardians("
            "minor_user_id, guardian_user_id, granted_by) VALUES(?, ?, ?)",
            (minor_user_id, guardian_user_id, granted_by),
        )

    async def remove_guardian(
        self,
        *,
        minor_user_id: str,
        guardian_user_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM cp_guardians WHERE minor_user_id=? AND guardian_user_id=?",
            (minor_user_id, guardian_user_id),
        )

    async def list_guardians(self, minor_user_id: str) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT guardian_user_id FROM cp_guardians WHERE minor_user_id=?",
            (minor_user_id,),
        )
        return [r["guardian_user_id"] for r in rows]

    async def list_minors_for_guardian(
        self,
        guardian_user_id: str,
    ) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT minor_user_id FROM cp_guardians WHERE guardian_user_id=?",
            (guardian_user_id,),
        )
        return [r["minor_user_id"] for r in rows]

    async def is_guardian_of(
        self,
        guardian_user_id: str,
        minor_user_id: str,
    ) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM cp_guardians WHERE guardian_user_id=? AND minor_user_id=?",
            (guardian_user_id, minor_user_id),
        )
        return row is not None

    # ── minor blocks ───────────────────────────────────────────────────

    async def block_user(
        self,
        *,
        minor_user_id: str,
        blocked_user_id: str,
        blocked_by: str,
    ) -> None:
        await self._db.enqueue(
            "INSERT OR IGNORE INTO cp_minor_blocks("
            "minor_user_id, blocked_user_id, blocked_by) VALUES(?, ?, ?)",
            (minor_user_id, blocked_user_id, blocked_by),
        )

    async def unblock_user(
        self,
        *,
        minor_user_id: str,
        blocked_user_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM cp_minor_blocks WHERE minor_user_id=? AND blocked_user_id=?",
            (minor_user_id, blocked_user_id),
        )

    async def is_blocked_for_minor(
        self,
        minor_user_id: str,
        other_user_id: str,
    ) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM cp_minor_blocks WHERE minor_user_id=? AND blocked_user_id=?",
            (minor_user_id, other_user_id),
        )
        return row is not None

    async def list_blocks_for_minor(
        self,
        minor_user_id: str,
    ) -> list[dict]:
        """Return every ``{blocked_user_id, blocked_by, blocked_at}``
        row for *minor_user_id*. Used by the Parent Dashboard (§CP)."""
        rows = await self._db.fetchall(
            "SELECT blocked_user_id, blocked_by, blocked_at"
            " FROM cp_minor_blocks WHERE minor_user_id=?"
            " ORDER BY blocked_at DESC",
            (minor_user_id,),
        )
        return [
            {
                "blocked_user_id": r["blocked_user_id"],
                "blocked_by": r["blocked_by"],
                "blocked_at": r["blocked_at"],
            }
            for r in rows
        ]

    async def remove_minor_from_blocked_user_spaces(
        self,
        *,
        minor_user_id: str,
        blocked_user_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM space_members WHERE user_id=? AND space_id IN "
            "(SELECT space_id FROM space_members WHERE user_id=?)",
            (minor_user_id, blocked_user_id),
        )

    # ── audit log ──────────────────────────────────────────────────────

    async def append_audit(
        self,
        *,
        minor_id: str,
        guardian_id: str,
        action: str,
        detail: dict | None = None,
    ) -> None:
        await self._db.enqueue(
            "INSERT INTO guardian_audit_log(id, minor_id, guardian_id, "
            "action, detail) VALUES(?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                minor_id,
                guardian_id,
                action,
                json.dumps(detail or {}),
            ),
        )

    async def list_audit_log(
        self,
        minor_user_id: str,
        *,
        limit: int,
    ) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT id, minor_id, guardian_id, action, detail, occurred_at "
            "FROM guardian_audit_log WHERE minor_id=? "
            "ORDER BY occurred_at DESC LIMIT ?",
            (minor_user_id, int(limit)),
        )
        return [dict(r) for r in rows]

    # ── membership audit ──────────────────────────────────────────────

    async def append_membership_audit(
        self,
        *,
        minor_user_id: str,
        space_id: str,
        action: str,
        actor_id: str,
    ) -> None:
        await self._db.enqueue(
            "INSERT INTO minor_space_memberships_audit("
            "id, minor_user_id, space_id, action, actor_id)"
            " VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex, minor_user_id, space_id, action, actor_id),
        )

    async def list_membership_audit(
        self,
        minor_user_id: str,
        *,
        limit: int,
    ) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT id, minor_user_id, space_id, action, actor_id, occurred_at"
            " FROM minor_space_memberships_audit WHERE minor_user_id=?"
            " ORDER BY occurred_at DESC LIMIT ?",
            (minor_user_id, int(limit)),
        )
        return [dict(r) for r in rows]

    async def is_minor(self, user_id: str) -> bool:
        """Return True iff the user has child-protection enabled.

        Used by :class:`SpaceService` to decide whether a membership
        mutation needs an audit entry. Absent user → False (caller is
        responsible for catching any real lookup error).
        """
        row = await self._db.fetchone(
            "SELECT is_minor, child_protection_enabled FROM users WHERE user_id=?",
            (user_id,),
        )
        if row is None:
            return False
        return bool(int(row["is_minor"] or 0)) or bool(
            int(row["child_protection_enabled"] or 0)
        )

    # ── age gate ───────────────────────────────────────────────────────

    async def space_exists(self, space_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM spaces WHERE id=?",
            (space_id,),
        )
        return row is not None

    async def update_space_age_gate(
        self,
        *,
        space_id: str,
        min_age: int,
        target_audience: str,
    ) -> None:
        await self._db.enqueue(
            "UPDATE spaces SET min_age=?, target_audience=? WHERE id=?",
            (min_age, target_audience, space_id),
        )

    async def get_space_age_gate(self, space_id: str) -> dict:
        row = await self._db.fetchone(
            "SELECT min_age, target_audience FROM spaces WHERE id=?",
            (space_id,),
        )
        if row is None:
            return {"min_age": 0, "target_audience": "all"}
        return {
            "min_age": int(row["min_age"] or 0),
            "target_audience": row["target_audience"] or "all",
        }

    async def get_user_protection(self, user_id: str) -> dict | None:
        row = await self._db.fetchone(
            "SELECT child_protection_enabled, declared_age FROM users WHERE user_id=?",
            (user_id,),
        )
        if row is None:
            return None
        return {
            "child_protection_enabled": int(
                row["child_protection_enabled"] or 0,
            ),
            "declared_age": int(row["declared_age"] or 0),
        }

    # ── DM gate ────────────────────────────────────────────────────────

    async def get_remote_instance_status(
        self,
        instance_id: str,
    ) -> dict | None:
        row = await self._db.fetchone(
            "SELECT status, source FROM remote_instances WHERE id=?",
            (instance_id,),
        )
        if row is None:
            return None
        return {
            "status": row["status"],
            "source": row["source"] or "manual",
        }

    # ── admin check ────────────────────────────────────────────────────

    async def is_admin(self, user_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT is_admin FROM users WHERE user_id=?",
            (user_id,),
        )
        return row is not None and bool(int(row["is_admin"] or 0))
