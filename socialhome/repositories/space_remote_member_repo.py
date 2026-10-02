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
from ..domain.space import SpaceRole
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
    #: The space authority-key epoch in force when this row was last
    #: written (migration 0066, v_44). The rotation baseline reset only
    #: overrides seats written under an OLDER key.
    authority_epoch: int = 0


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
        role: str = SpaceRole.MEMBER.value,
    ) -> None: ...

    async def remove(
        self,
        space_id: str,
        instance_id: str,
        user_id: str,
    ) -> None: ...

    async def list_for_space(self, space_id: str) -> list[SpaceRemoteMember]: ...

    async def list_for_instance(
        self,
        space_id: str,
        instance_id: str,
        *,
        include_tombstoned: bool = True,
    ) -> list[SpaceRemoteMember]: ...

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
        verified_epoch: int | None = None,
    ) -> bool: ...

    async def reset_member_state(
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
    ) -> None: ...
    async def list_admin_instances(self, space_id: str) -> list[str]: ...

    async def list_instances_with_roles(
        self, space_id: str, roles: frozenset[str]
    ) -> list[str]: ...

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
        role: str = SpaceRole.MEMBER.value,
    ) -> None:
        """Seat a remote member, role included, in ONE write.

        ``role`` lands in the same INSERT rather than through a follow-up
        :meth:`set_role`. The two-step version was a durability hole on
        the Follower path: a redeem that crashed between the INSERT and
        the UPDATE left a household that paid for a read-only seat sitting
        as a full ``member`` — the exact seat the §24.11 space-writer gate
        reads to decide whether to refuse its writes.

        A re-seat also **clears the tombstone**. :meth:`remove` retains the
        row with ``tombstoned=1``, and the writer gate reads tombstones on
        purpose (so a kicked household does not read like one we never
        met). Without clearing it here, a household kicked and then
        legitimately re-invited stayed in the gate's ``else → refuse``
        branch for ever — silently, because that refusal answers
        ``{"status": "ok"}``, so the sender's outbox never retries.

        The ``member_version`` bump rides the same CASE: the version is a
        CRDT clock over state CHANGES (:meth:`apply_member_event`), so a
        resurrection must out-rank the tombstone :meth:`remove` wrote,
        while a plain refresh of a live row must NOT race the roster
        gossip's own counter upwards.
        """
        await self._db.enqueue(
            """
            INSERT INTO space_remote_members(
                space_id, instance_id, user_id, user_pk, display_name, role,
                authority_epoch
            ) VALUES(?, ?, ?, ?, ?, ?, (SELECT authority_key_epoch FROM spaces WHERE id=?))
            ON CONFLICT(space_id, instance_id, user_id) DO UPDATE SET
                user_pk=excluded.user_pk,
                display_name=excluded.display_name,
                role=excluded.role,
                authority_epoch=excluded.authority_epoch,
                tombstoned=0,
                member_version=space_remote_members.member_version
                    + (CASE WHEN space_remote_members.tombstoned THEN 1 ELSE 0 END)
            """,
            (space_id, instance_id, user_id, user_pk, display_name, role, space_id),
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
            SET tombstoned=1, member_version=member_version + 1,
                authority_epoch=(SELECT authority_key_epoch FROM spaces WHERE id=?)
            WHERE space_id=? AND instance_id=? AND user_id=?
            """,
            (space_id, space_id, instance_id, user_id),
        )

    async def list_for_space(self, space_id: str) -> list[SpaceRemoteMember]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_remote_members "
            "WHERE space_id=? AND tombstoned=0 ORDER BY joined_at",
            (space_id,),
        )
        return [_row(r) for r in rows_to_dicts(rows)]

    async def list_for_instance(
        self,
        space_id: str,
        instance_id: str,
        *,
        include_tombstoned: bool = True,
    ) -> list[SpaceRemoteMember]:
        """Every seat one HOUSEHOLD holds in one space — tombstones included.

        The §24.11 space-writer gate
        (:func:`~socialhome.federation.inbound_validator
        .make_check_space_writer`) asks a household-level question — "does
        the household that signed this envelope hold a seat here that may
        write?" — so it needs all of that household's rows at once, and it
        needs the removed ones: :meth:`get` and :meth:`list_for_space`
        filter tombstones, which would make a household we KICKED read
        exactly like a household we have simply never heard of. Those two
        must not be the same answer — the second is the unconverged-mirror
        case the gate is lenient about, the first is a decision we already
        made.

        ``include_tombstoned=False`` gives the live-roster subset for
        callers that want today's filtered view.
        """
        sql = "SELECT * FROM space_remote_members WHERE space_id=? AND instance_id=?"
        if not include_tombstoned:
            sql += " AND tombstoned=0"
        rows = await self._db.fetchall(
            sql + " ORDER BY joined_at", (space_id, instance_id)
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
        verified_epoch: int | None = None,
    ) -> bool:
        """Version-guarded CRDT merge of an inbound roster event.

        ``verified_epoch`` (v_44) is the space authority-key epoch whose key
        the event's signature verified against. When given, the write lands
        ONLY while the space still pins that epoch — in the same statement —
        and the row is stamped with it. A rotation that moved the pin between
        the verify and the write makes this a no-op (``False``), so an
        old-key event can never be stored as a new-key row.

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
        if verified_epoch is None:
            await self._db.enqueue(
                """
                INSERT INTO space_remote_members(
                    space_id, instance_id, user_id, user_pk, display_name,
                    role, member_version, tombstoned, authority_epoch
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?,
                         (SELECT authority_key_epoch FROM spaces WHERE id=?))
                ON CONFLICT(space_id, instance_id, user_id) DO UPDATE SET
                    user_pk=excluded.user_pk,
                    display_name=excluded.display_name,
                    role=excluded.role,
                    member_version=excluded.member_version,
                    tombstoned=excluded.tombstoned,
                    authority_epoch=excluded.authority_epoch
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
                    space_id,
                ),
            )
            return True
        changed = await self._db.enqueue_rowcount(
            """
            INSERT INTO space_remote_members(
                space_id, instance_id, user_id, user_pk, display_name,
                role, member_version, tombstoned, authority_epoch
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
            WHERE (SELECT authority_key_epoch FROM spaces WHERE id=?) = ?
            ON CONFLICT(space_id, instance_id, user_id) DO UPDATE SET
                user_pk=excluded.user_pk,
                display_name=excluded.display_name,
                role=excluded.role,
                member_version=excluded.member_version,
                tombstoned=excluded.tombstoned,
                authority_epoch=excluded.authority_epoch
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
                verified_epoch,
                space_id,
                verified_epoch,
            ),
        )
        return changed > 0

    async def reset_member_state(
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
    ) -> None:
        """Overwrite one seat with the owner's state, IGNORING the version
        guard (v_44 authority-rotation baseline reset).

        :meth:`apply_member_event` refuses anything not newer than the
        stored row, which is right for gossip — but a revoked seed holder
        could have inflated a seat's ``member_version`` (or invented a seat)
        with the old key, and nothing ordinary ever out-ranks that. The
        owner's ``SPACE_AUTHORITY_ROTATED`` bundle is the one statement that
        does; only its handler calls this, after verifying the bundle.
        """
        await self._db.enqueue(
            """
            INSERT INTO space_remote_members(
                space_id, instance_id, user_id, user_pk, display_name,
                role, member_version, tombstoned, authority_epoch
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, (SELECT authority_key_epoch FROM spaces WHERE id=?))
            ON CONFLICT(space_id, instance_id, user_id) DO UPDATE SET
                user_pk=excluded.user_pk,
                display_name=excluded.display_name,
                role=excluded.role,
                member_version=excluded.member_version,
                tombstoned=excluded.tombstoned,
                authority_epoch=excluded.authority_epoch
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
                space_id,
            ),
        )

    async def list_admin_instances(self, space_id: str) -> list[str]:
        """DISTINCT instance_ids of remote members with role ADMIN.

        Used by the delegated-admin signing-seed share (v_22): when the
        owner flips ``delegated_admin_authority`` on, the seed is
        distributed to every current remote *admin* household. A
        household with several admins appears once.
        """
        return await self.list_instances_with_roles(
            space_id, frozenset({SpaceRole.ADMIN.value})
        )

    async def list_instances_with_roles(
        self, space_id: str, roles: frozenset[str]
    ) -> list[str]:
        """DISTINCT instance_ids holding a LIVE seat with one of ``roles``.

        The federated-moderation reviewer set (v_43, ``admin`` +
        ``moderator``) and the admin-only seed share above. Tombstoned
        seats never count; an empty ``roles`` matches nothing.
        """
        wanted = sorted(roles)
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        rows = await self._db.fetchall(
            "SELECT DISTINCT instance_id FROM space_remote_members "
            f"WHERE space_id=? AND role IN ({marks}) AND tombstoned=0 "
            "ORDER BY instance_id",
            (space_id, *wanted),
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
        authority_epoch=int(row.get("authority_epoch") or 0),
    )
