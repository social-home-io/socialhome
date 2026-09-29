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
* ``spaces`` — the ``min_age`` column (§CP.F1).
* ``guardian_audit_log`` — append-only audit trail.
* ``remote_instances`` — read for the §CP.F3 DM gate.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

import orjson

from ..db import AsyncDatabase


#: Every user a guardian block separates from a viewer, in either direction
#: (§CP.F2): the people blocked for the viewer, and the protected accounts
#: that have the viewer blocked. A block only counts while its account is
#: protected. Bind the viewer's ``user_id`` twice. Shared by the repos whose
#: read queries hide a blocked author (highlights, moments).
def guardian_block_counterparts_sql(viewer: str = "?") -> str:
    """The counterparts sub-select for the SQL expression *viewer* — a bind
    placeholder by default, or a column of the outer query (e.g. the unread
    count's ``u.user_id``)."""
    return f"""
    SELECT b.blocked_user_id FROM cp_minor_blocks b
      JOIN users m ON m.user_id = b.minor_user_id
     WHERE b.minor_user_id = {viewer} AND m.child_protection_enabled = 1
    UNION
    SELECT b.minor_user_id FROM cp_minor_blocks b
      JOIN users m ON m.user_id = b.minor_user_id
     WHERE b.blocked_user_id = {viewer} AND m.child_protection_enabled = 1
"""


GUARDIAN_BLOCK_COUNTERPARTS_SQL = guardian_block_counterparts_sql()

#: Guardian blocks (while protected) from a local account onto people homed
#: on one household — ``remote_users`` places them.
_BLOCKS_HOMED_ON_SQL = """
    SELECT 1 FROM cp_minor_blocks b
      JOIN users m ON m.user_id = b.minor_user_id
      JOIN remote_users r ON r.user_id = b.blocked_user_id
     WHERE m.child_protection_enabled = 1 AND r.instance_id = ?
"""


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
    async def is_blocked_pair(self, user_a: str, user_b: str) -> bool: ...
    async def list_block_counterparts(self, user_id: str) -> frozenset[str]: ...
    async def blocks_someone_homed_on(
        self, instance_id: str, *, minor_user_id: str | None = None
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
    ) -> None: ...
    async def get_space_age_gate(self, space_id: str) -> dict: ...
    async def get_user_protection(self, user_id: str) -> dict | None: ...
    async def list_protection_status(self) -> list[dict]: ...

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

    async def is_blocked_pair(self, user_a: str, user_b: str) -> bool:
        """Whether a guardian block stands between *user_a* and *user_b*
        (either one the protected account). Only while protection is on."""
        row = await self._db.fetchone(
            "SELECT 1 FROM cp_minor_blocks b"
            " JOIN users m ON m.user_id = b.minor_user_id"
            " WHERE m.child_protection_enabled = 1 AND ("
            " (b.minor_user_id = ? AND b.blocked_user_id = ?)"
            " OR (b.minor_user_id = ? AND b.blocked_user_id = ?)) LIMIT 1",
            (user_a, user_b, user_b, user_a),
        )
        return row is not None

    async def blocks_someone_homed_on(
        self, instance_id: str, *, minor_user_id: str | None = None
    ) -> bool:
        """Whether a guardian block (of *minor_user_id*, or of any protected
        account here) names someone homed on *instance_id*."""
        if minor_user_id is None:
            row = await self._db.fetchone(
                _BLOCKS_HOMED_ON_SQL + " LIMIT 1", (instance_id,)
            )
        else:
            row = await self._db.fetchone(
                _BLOCKS_HOMED_ON_SQL + " AND b.minor_user_id = ? LIMIT 1",
                (instance_id, minor_user_id),
            )
        return row is not None

    async def list_block_counterparts(self, user_id: str) -> frozenset[str]:
        """Everyone a guardian block separates from *user_id* (see
        :data:`GUARDIAN_BLOCK_COUNTERPARTS_SQL`)."""
        rows = await self._db.fetchall(
            GUARDIAN_BLOCK_COUNTERPARTS_SQL,
            (user_id, user_id),
        )
        return frozenset(str(r[0]) for r in rows)

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
                orjson.dumps(detail or {}).decode(),
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

    async def update_space_age_gate(self, *, space_id: str, min_age: int) -> None:
        await self._db.enqueue(
            "UPDATE spaces SET min_age=? WHERE id=?", (min_age, space_id)
        )

    async def get_space_age_gate(self, space_id: str) -> dict:
        row = await self._db.fetchone(
            "SELECT min_age FROM spaces WHERE id=?",
            (space_id,),
        )
        if row is None:
            return {"min_age": 0}
        return {"min_age": int(row["min_age"] or 0)}

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

    async def list_protection_status(self) -> list[dict]:
        """Protection status for every user — keyed by ``user_id``.

        Feeds the admin Child-Protection panel's "Protected" column.
        ``is_minor``/``declared_age`` are in ``SENSITIVE_FIELDS`` (stripped
        from ``/api/users``), so this is the *only* place they surface —
        behind the admin gate in :class:`ChildProtectionService`.
        ``is_minor`` tracks ``child_protection_enabled`` (the gate's source
        of truth, set/cleared together with ``is_minor`` by
        :meth:`enable_protection` / :meth:`disable_protection`).
        """
        rows = await self._db.fetchall(
            "SELECT user_id, username, child_protection_enabled, declared_age"
            " FROM users",
        )
        return [
            {
                "user_id": str(r["user_id"]),
                "username": str(r["username"]),
                "is_minor": bool(int(r["child_protection_enabled"] or 0)),
                "declared_age": int(r["declared_age"] or 0),
            }
            for r in rows
        ]

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
