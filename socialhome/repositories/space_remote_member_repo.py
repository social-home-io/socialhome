"""Remote-member repository for cross-household private-space joins (§D1b).

When a household accepts a private-space invite from another household,
the inviting household records the accepter in ``space_remote_members``
so future space-message fan-outs include that instance + user in the
recipient list. Stored fields are the minimum needed to encrypt + route
subsequent space content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..db.database import AsyncDatabase
from .base import rows_to_dicts


@dataclass(slots=True, frozen=True)
class SpaceRemoteMember:
    """A single remote-member row in a federated private space."""

    space_id: str
    instance_id: str
    user_id: str
    user_pk: str | None = None
    display_name: str | None = None
    joined_at: str | None = None
    #: Per-space role. The on-disk authority is the
    #: ``space_remote_members.role`` CHECK constraint
    #: (member|admin|subscriber); in-code authority is
    #: :class:`SpaceRole`.MEMBER / .ADMIN / .SUBSCRIBER. ``subscriber``
    #: is a household that redeemed a Follower invite link: it receives
    #: the space's content stream like any member and is refused every
    #: write host-side (``make_check_space_writer``, §24.11). Owner is
    #: intentionally not allowed here — see the migration 0009 docstring
    #: for the rationale, and 0054 for why ``subscriber`` joined.
    role: str = "member"
    #: Monotonic per-(space_id, user_id) version, bumped on every
    #: authoritative mutation (add/role-change/remove). Drives the
    #: CRDT-style convergence merge in :meth:`apply_member_event`
    #: (migration 0031). Pre-0031 rows default to 0.
    member_version: int = 0
    #: ``True`` once the member is removed. The row is RETAINED rather than
    #: hard-deleted so a replayed older JOIN can't resurrect them; live
    #: roster reads filter these out (migration 0031).
    tombstoned: bool = False


@runtime_checkable
class AbstractSpaceRemoteMemberRepo(Protocol):
    async def add(
        self,
        *,
        space_id: str,
        instance_id: str,
        user_id: str,
        user_pk: str | None,
        display_name: str | None,
    ) -> None: ...

    async def remove(
        self,
        space_id: str,
        instance_id: str,
        user_id: str,
    ) -> None: ...

    async def list_for_space(self, space_id: str) -> list[SpaceRemoteMember]: ...

    async def list_for_space_including_tombstones(
        self, space_id: str
    ) -> list[SpaceRemoteMember]: ...

    async def apply_member_event(
        self,
        *,
        space_id: str,
        user_id: str,
        instance_id: str,
        display_name: str | None,
        user_pk: str | None,
        role: str,
        member_version: int,
        tombstoned: bool,
    ) -> bool: ...

    async def list_admin_instances(self, space_id: str) -> list[str]: ...

    async def list_for_user(
        self,
        instance_id: str,
        user_id: str,
    ) -> list[SpaceRemoteMember]: ...

    async def set_role(
        self,
        space_id: str,
        instance_id: str,
        user_id: str,
        role: str,
    ) -> None: ...

    async def get(
        self,
        space_id: str,
        instance_id: str,
        user_id: str,
    ) -> SpaceRemoteMember | None: ...

    async def get_including_tombstones(
        self,
        space_id: str,
        instance_id: str,
        user_id: str,
    ) -> SpaceRemoteMember | None: ...


class SqliteSpaceRemoteMemberRepo:
    """SQLite-backed :class:`AbstractSpaceRemoteMemberRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def add(
        self,
        *,
        space_id: str,
        instance_id: str,
        user_id: str,
        user_pk: str | None,
        display_name: str | None,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_remote_members(
                space_id, instance_id, user_id, user_pk, display_name
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(space_id, instance_id, user_id) DO UPDATE SET
                user_pk=excluded.user_pk,
                display_name=excluded.display_name
            """,
            (space_id, instance_id, user_id, user_pk, display_name),
        )

    async def remove(
        self,
        space_id: str,
        instance_id: str,
        user_id: str,
    ) -> None:
        """Tombstone the member (durable removal), bumping its version.

        We RETAIN the row with ``tombstoned=1`` rather than hard-DELETE so a
        replayed older JOIN can't resurrect a removed member — the convergence
        guarantee the gossip path (next phase) relies on. Live-roster reads
        (:meth:`list_for_space`, :meth:`get`, :meth:`list_for_user`,
        :meth:`list_admin_instances`) filter tombstones out, so every caller
        still observes a removed member as gone.
        """
        await self._db.enqueue(
            """
            UPDATE space_remote_members
            SET tombstoned=1, member_version=member_version + 1
            WHERE space_id=? AND instance_id=? AND user_id=?
            """,
            (space_id, instance_id, user_id),
        )

    async def list_for_space(self, space_id: str) -> list[SpaceRemoteMember]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_remote_members "
            "WHERE space_id=? AND tombstoned=0 ORDER BY joined_at",
            (space_id,),
        )
        return [_row(r) for r in rows_to_dicts(rows)]

    async def list_for_space_including_tombstones(
        self, space_id: str
    ) -> list[SpaceRemoteMember]:
        """All rows for the space INCLUDING tombstones — for the convergence
        path only. Live-roster callers want :meth:`list_for_space`."""
        rows = await self._db.fetchall(
            "SELECT * FROM space_remote_members WHERE space_id=? ORDER BY joined_at",
            (space_id,),
        )
        return [_row(r) for r in rows_to_dicts(rows)]

    async def apply_member_event(
        self,
        *,
        space_id: str,
        user_id: str,
        instance_id: str,
        display_name: str | None,
        user_pk: str | None,
        role: str,
        member_version: int,
        tombstoned: bool,
    ) -> bool:
        """Version-guarded CRDT merge of an inbound roster event.

        Applies (upserts) the event ONLY if it is newer than the stored row:
        strictly greater ``member_version``, OR an equal version that is a
        tombstone (removal-wins-tie). Anything else is a stale duplicate and is
        ignored. Returns ``True`` if applied, ``False`` if dropped as stale.

        This is the convergence primitive the gossip handler will call so
        concurrent admin join/leave decisions converge deterministically across
        households regardless of delivery order.
        """
        # The version read keys on (space_id, user_id) (get_including_tombstones)
        # while the upsert below keys on (space_id, instance_id, user_id). This is
        # safe because user_id = derive_user_id(home_pk, username) is bound to the
        # home instance — changing home instance changes user_id — so the same
        # user_id can never appear under two instance_ids, and the two keys
        # resolve to the same single row.
        current = await self.get_including_tombstones(space_id, instance_id, user_id)
        if current is not None:
            if member_version < current.member_version:
                return False
            if member_version == current.member_version and not (
                tombstoned and not current.tombstoned
            ):
                # Equal version only wins when it flips a live row to a
                # tombstone (removal-wins-tie); otherwise it's a duplicate.
                return False
        await self._db.enqueue(
            """
            INSERT INTO space_remote_members(
                space_id, instance_id, user_id, user_pk, display_name,
                role, member_version, tombstoned
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(space_id, instance_id, user_id) DO UPDATE SET
                user_pk=excluded.user_pk,
                display_name=excluded.display_name,
                role=excluded.role,
                member_version=excluded.member_version,
                tombstoned=excluded.tombstoned
            """,
            (
                space_id,
                instance_id,
                user_id,
                user_pk,
                display_name,
                role,
                member_version,
                1 if tombstoned else 0,
            ),
        )
        return True

    async def list_admin_instances(self, space_id: str) -> list[str]:
        """DISTINCT instance_ids of remote members with role ADMIN.

        Used by the delegated-admin signing-seed share (v_22): when the
        owner flips ``delegated_admin_authority`` on, the seed is
        distributed to every current remote *admin* household. A
        household with several admins appears once.
        """
        rows = await self._db.fetchall(
            "SELECT DISTINCT instance_id FROM space_remote_members "
            "WHERE space_id=? AND role='admin' AND tombstoned=0",
            (space_id,),
        )
        return [r["instance_id"] for r in rows_to_dicts(rows)]

    async def list_for_user(
        self,
        instance_id: str,
        user_id: str,
    ) -> list[SpaceRemoteMember]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_remote_members "
            "WHERE instance_id=? AND user_id=? AND tombstoned=0",
            (instance_id, user_id),
        )
        return [_row(r) for r in rows_to_dicts(rows)]

    async def set_role(
        self,
        space_id: str,
        instance_id: str,
        user_id: str,
        role: str,
    ) -> None:
        await self._db.enqueue(
            """
            UPDATE space_remote_members SET role=?
            WHERE space_id=? AND instance_id=? AND user_id=?
            """,
            (role, space_id, instance_id, user_id),
        )

    async def get(
        self,
        space_id: str,
        instance_id: str,
        user_id: str,
    ) -> SpaceRemoteMember | None:
        """Live member lookup — a tombstoned row reads as ``None`` (gone).

        Callers use the returned row as an authorization signal (is this
        actor a current member/admin?), so a removed member MUST NOT be
        observable here. The convergence path uses
        :meth:`get_including_tombstones`.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM space_remote_members "
            "WHERE space_id=? AND instance_id=? AND user_id=? AND tombstoned=0 "
            "LIMIT 1",
            (space_id, instance_id, user_id),
        )
        dicts = rows_to_dicts(rows)
        return _row(dicts[0]) if dicts else None

    async def get_including_tombstones(
        self,
        space_id: str,
        instance_id: str,
        user_id: str,
    ) -> SpaceRemoteMember | None:
        """Lookup that INCLUDES tombstones — convergence path only.

        Keyed on (space_id, user_id): a user belongs to exactly one
        instance, so the pair uniquely identifies the roster row, and the
        merge in :meth:`apply_member_event` must see the tombstone of a
        removed user even if an event re-asserts a different instance_id.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM space_remote_members WHERE space_id=? AND user_id=? LIMIT 1",
            (space_id, user_id),
        )
        dicts = rows_to_dicts(rows)
        return _row(dicts[0]) if dicts else None


def _row(row: dict) -> SpaceRemoteMember:
    return SpaceRemoteMember(
        space_id=row["space_id"],
        instance_id=row["instance_id"],
        user_id=row["user_id"],
        user_pk=row.get("user_pk"),
        display_name=row.get("display_name"),
        joined_at=row.get("joined_at"),
        role=row.get("role") or "member",
        member_version=int(row.get("member_version") or 0),
        tombstoned=bool(row.get("tombstoned")),
    )
