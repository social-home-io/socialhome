"""Child Protection service (§CP).

Coordinates all minor-account features. Security invariants from the spec:

* **§CP.A1** — Only household admins can enable/disable protection or
  assign guardians.
* **§CP.A2** — Guardians can act only on their own assigned minors.
* **§CP.F1** — Minors cannot join spaces whose ``min_age`` exceeds the
  user's ``declared_age``.
* **§CP.F2** — Guardian-set user blocks auto-remove the minor from any
  spaces shared with the blocked user.
* **§CP.F3** — Protected minors may only DM users on directly-paired
  instances (no DM_RELAY hops).

V1 implements the public-facing surface — age gate updates, the
:meth:`check_space_age_gate` enforcement hook called by SpaceService.join,
and the :meth:`is_dm_allowed` enforcement hook called by DmService.
The full guardian flows (kick, block-on-behalf, etc.) sit on the same
shape and can be enabled when the matching UX lands.
"""

from __future__ import annotations

import logging
from datetime import date as _date
from typing import Protocol, runtime_checkable

from ..domain.events import (
    CpBlockAdded,
    CpBlockRemoved,
    CpGuardianAdded,
    CpGuardianRemoved,
    CpProtectionDisabled,
    CpProtectionEnabled,
    CpSpaceAgeGateChanged,
    SpaceMemberLeft,
)
from ..domain.space import SpacePermissionError
from ..infrastructure.event_bus import EventBus
from ..repositories.cp_repo import AbstractCpRepo
from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


_VALID_MIN_AGES: frozenset[int] = frozenset({0, 13, 16, 18})
_VALID_AUDIENCES: frozenset[str] = frozenset({"all", "family", "teen", "adult"})


# ─── Errors ──────────────────────────────────────────────────────────────


class ChildProtectionError(Exception):
    """Base error class."""


class GuardianRequiredError(ChildProtectionError):
    """Caller is not a guardian of the referenced minor."""


# ─── Service ─────────────────────────────────────────────────────────────


@runtime_checkable
class _PublishesEvents(Protocol):
    async def publish(self, event) -> None: ...


class ChildProtectionService:
    """V1 child-protection coordinator."""

    __slots__ = ("_repo", "_users", "_bus", "_space_repo", "_conv_repo")

    def __init__(
        self,
        repo: AbstractCpRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
    ) -> None:
        self._repo = repo
        self._users = user_repo
        self._bus = bus
        self._space_repo = None
        self._conv_repo = None

    def attach_space_repo(self, space_repo) -> None:
        """Wire :class:`AbstractSpaceRepo` so ``kick_from_space`` can
        remove a minor directly (bypassing the usual admin-or-self check
        while still emitting ``SpaceMemberLeft`` via the bus)."""
        self._space_repo = space_repo

    def attach_conversation_repo(self, conv_repo) -> None:
        """Wire :class:`AbstractConversationRepo` so the Parent Dashboard
        can enumerate a minor's active DM conversations + their peers.
        """
        self._conv_repo = conv_repo

    # ─── Admin ops: enable/disable protection ────────────────────────────

    async def enable_protection(
        self,
        *,
        minor_username: str,
        declared_age: int,
        actor_user_id: str,
        date_of_birth: str | None = None,
    ) -> None:
        """Mark *minor_username* as a protected minor.

        ``declared_age`` must be 0–17. When ``date_of_birth`` is supplied
        it must agree with ``declared_age`` within ±1 year.
        """
        await self._require_admin(actor_user_id)
        if not (0 <= declared_age <= 17):
            raise ValueError("declared_age must be 0–17 for a minor account")
        if date_of_birth:
            try:
                dob = _date.fromisoformat(date_of_birth)
            except ValueError as exc:
                raise ValueError(f"Invalid date_of_birth format: {exc}") from exc
            age_from_dob = (_date.today() - dob).days // 365
            if abs(age_from_dob - declared_age) > 1:
                raise ValueError(
                    f"declared_age ({declared_age}) inconsistent with "
                    f"date_of_birth (computed {age_from_dob})"
                )

        await self._repo.enable_protection(
            minor_username=minor_username,
            declared_age=declared_age,
            date_of_birth=date_of_birth,
        )
        await self._bus.publish(
            CpProtectionEnabled(
                minor_username=minor_username,
                declared_age=declared_age,
            )
        )

    async def disable_protection(
        self,
        *,
        minor_username: str,
        actor_user_id: str,
    ) -> None:
        await self._require_admin(actor_user_id)
        await self._repo.disable_protection(minor_username)
        await self._bus.publish(
            CpProtectionDisabled(
                minor_username=minor_username,
            )
        )

    # ─── Guardians ───────────────────────────────────────────────────────

    async def add_guardian(
        self,
        *,
        minor_user_id: str,
        guardian_user_id: str,
        actor_user_id: str,
    ) -> None:
        await self._require_admin(actor_user_id)
        if minor_user_id == guardian_user_id:
            raise ValueError("A user cannot be their own guardian")
        await self._repo.add_guardian(
            minor_user_id=minor_user_id,
            guardian_user_id=guardian_user_id,
            granted_by=actor_user_id,
        )
        await self._bus.publish(
            CpGuardianAdded(
                minor_user_id=minor_user_id,
                guardian_user_id=guardian_user_id,
            )
        )

    async def remove_guardian(
        self,
        *,
        minor_user_id: str,
        guardian_user_id: str,
        actor_user_id: str,
    ) -> None:
        await self._require_admin(actor_user_id)
        await self._repo.remove_guardian(
            minor_user_id=minor_user_id,
            guardian_user_id=guardian_user_id,
        )
        await self._bus.publish(
            CpGuardianRemoved(
                minor_user_id=minor_user_id,
                guardian_user_id=guardian_user_id,
            )
        )

    async def list_guardians(self, minor_user_id: str) -> list[str]:
        return await self._repo.list_guardians(minor_user_id)

    async def list_minors_for_guardian(self, guardian_user_id: str) -> list[str]:
        return await self._repo.list_minors_for_guardian(guardian_user_id)

    async def is_guardian_of(
        self,
        guardian_user_id: str,
        minor_user_id: str,
    ) -> bool:
        return await self._repo.is_guardian_of(guardian_user_id, minor_user_id)

    # ─── Per-minor user blocks (§CP.F2) ──────────────────────────────────

    async def block_user_for_minor(
        self,
        *,
        minor_user_id: str,
        blocked_user_id: str,
        guardian_user_id: str,
    ) -> None:
        await self._require_guardian(guardian_user_id, minor_user_id)
        await self._repo.block_user(
            minor_user_id=minor_user_id,
            blocked_user_id=blocked_user_id,
            blocked_by=guardian_user_id,
        )
        await self.record_action(
            minor_id=minor_user_id,
            guardian_id=guardian_user_id,
            action="block_user",
            detail={"blocked_user_id": blocked_user_id},
        )
        await self._bus.publish(
            CpBlockAdded(
                minor_user_id=minor_user_id,
                blocked_user_id=blocked_user_id,
            )
        )
        # §CP.F2: drop the minor from any space that also has the
        # blocked user as a member. Failure here is logged but never
        # blocks the block itself.
        try:
            await self._repo.remove_minor_from_blocked_user_spaces(
                minor_user_id=minor_user_id,
                blocked_user_id=blocked_user_id,
            )
        except Exception as exc:  # defensive — schema may vary
            log.debug("CP.F2 space auto-removal skipped: %s", exc)

    async def list_blocks_for_minor(
        self,
        *,
        minor_user_id: str,
        actor_user_id: str,
    ) -> list[dict]:
        """Return the block list for *minor_user_id*.

        Callable by the assigned guardian or a household admin (§CP.A2).
        """
        if not await self._repo.is_guardian_of(actor_user_id, minor_user_id):
            if not await self._repo.is_admin(actor_user_id):
                raise GuardianRequiredError(
                    "caller must be a guardian of this minor",
                )
        return await self._repo.list_blocks_for_minor(minor_user_id)

    async def list_spaces_for_minor(
        self,
        *,
        minor_user_id: str,
        actor_user_id: str,
    ) -> list[dict]:
        """Return every space *minor_user_id* is currently a member of.

        Guardian-or-admin only. Powers the Parent Dashboard's per-minor
        "Joined spaces" list + the per-space kick action.
        """
        if not await self._repo.is_guardian_of(actor_user_id, minor_user_id):
            if not await self._repo.is_admin(actor_user_id):
                raise GuardianRequiredError(
                    "caller must be a guardian of this minor",
                )
        if self._space_repo is None:
            return []
        spaces = await self._space_repo.list_for_user(minor_user_id)
        return [
            {
                "id": s.id,
                "name": s.name,
                "emoji": s.emoji,
                "space_type": s.space_type.value,
            }
            for s in spaces
        ]

    async def list_conversations_for_minor(
        self,
        *,
        minor_user_id: str,
        actor_user_id: str,
    ) -> list[dict]:
        """Return every DM conversation the minor actively participates in.

        Guardian-or-admin only. Feeds the Parent Dashboard's "Active
        conversations" panel so a guardian can spot inappropriate threads
        at a glance. Returns conversation rows (not messages) — opening
        a thread is still an explicit guardian action.
        """
        await self._require_guardian_or_admin(actor_user_id, minor_user_id)
        user = await self._users.get_by_user_id(minor_user_id)
        if user is None or self._conv_repo is None:
            return []
        convs = await self._conv_repo.list_for_user(user.username)
        return [
            {
                "id": c.id,
                "type": c.type.value,
                "name": c.name,
                "last_message_at": c.last_message_at,
            }
            for c in convs
        ]

    async def list_dm_contacts_for_minor(
        self,
        *,
        minor_user_id: str,
        actor_user_id: str,
    ) -> list[dict]:
        """Return every distinct peer the minor is in a DM with.

        Guardian-or-admin only. Pulls the member list of each active
        conversation, deduplicates the minor out, and returns one row per
        peer username. Used by the Parent Dashboard's "Chats with" pane.
        """
        await self._require_guardian_or_admin(actor_user_id, minor_user_id)
        user = await self._users.get_by_user_id(minor_user_id)
        if user is None or self._conv_repo is None:
            return []
        convs = await self._conv_repo.list_for_user(user.username)
        seen: set[str] = set()
        contacts: list[dict] = []
        for c in convs:
            members = await self._conv_repo.list_members(c.id)
            for m in members:
                if m.username == user.username or m.username in seen:
                    continue
                seen.add(m.username)
                contacts.append(
                    {
                        "username": m.username,
                        "conversation_id": c.id,
                    }
                )
        return contacts

    async def _require_guardian_or_admin(
        self,
        actor_user_id: str,
        minor_user_id: str,
    ) -> None:
        if await self._repo.is_guardian_of(actor_user_id, minor_user_id):
            return
        if await self._repo.is_admin(actor_user_id):
            return
        raise GuardianRequiredError(
            "caller must be a guardian of this minor",
        )

    async def unblock_user_for_minor(
        self,
        *,
        minor_user_id: str,
        blocked_user_id: str,
        guardian_user_id: str,
    ) -> None:
        await self._require_guardian(guardian_user_id, minor_user_id)
        await self._repo.unblock_user(
            minor_user_id=minor_user_id,
            blocked_user_id=blocked_user_id,
        )
        await self.record_action(
            minor_id=minor_user_id,
            guardian_id=guardian_user_id,
            action="unblock_user",
            detail={"blocked_user_id": blocked_user_id},
        )
        await self._bus.publish(
            CpBlockRemoved(
                minor_user_id=minor_user_id,
                blocked_user_id=blocked_user_id,
            )
        )

    # ─── Guardian-scoped space control (spec §CP) ────────────────────────

    async def kick_from_space(
        self,
        *,
        minor_user_id: str,
        space_id: str,
        guardian_user_id: str,
    ) -> bool:
        """Remove a minor from a specific space as the guardian.

        Returns ``True`` when a row was removed, ``False`` when the
        minor wasn't a member. Raises :class:`GuardianRequiredError`
        if the caller isn't a guardian. Emits a :class:`SpaceMemberLeft`
        event so realtime / notification services fan out the change.
        """
        await self._require_guardian(guardian_user_id, minor_user_id)
        if self._space_repo is None:
            raise RuntimeError("space_repo not attached")
        member = await self._space_repo.get_member(space_id, minor_user_id)
        if member is None:
            return False
        await self._space_repo.delete_member(space_id, minor_user_id)
        await self.record_action(
            minor_id=minor_user_id,
            guardian_id=guardian_user_id,
            action="kick_from_space",
            detail={"space_id": space_id},
        )
        await self._bus.publish(
            SpaceMemberLeft(
                space_id=space_id,
                user_id=minor_user_id,
            )
        )
        return True

    # ─── Guardian audit log (§CP, §25.8 business logic) ──────────────────

    async def record_action(
        self,
        *,
        minor_id: str,
        guardian_id: str,
        action: str,
        detail: dict | None = None,
    ) -> None:
        """Persist a guardian action to ``guardian_audit_log``.

        Used internally by guardian-privileged operations so a minor's
        admins can audit who did what.
        """
        await self._repo.append_audit(
            minor_id=minor_id,
            guardian_id=guardian_id,
            action=action,
            detail=detail,
        )

    async def list_audit_log(
        self,
        minor_user_id: str,
        *,
        limit: int = 50,
    ) -> list[dict]:
        """Recent guardian actions for a minor, newest first."""
        return await self._repo.list_audit_log(minor_user_id, limit=limit)

    async def get_audit_log(
        self,
        minor_user_id: str,
        requester_user_id: str,
        *,
        limit: int = 50,
    ) -> list[dict]:
        """Audit-log accessor with ACL — guardians or admins only."""
        is_guardian = await self.is_guardian_of(
            requester_user_id,
            minor_user_id,
        )
        if not is_guardian:
            # Admin bypass — reuse the existing admin check.
            try:
                await self._require_admin(requester_user_id)
            except SpacePermissionError as exc:
                raise GuardianRequiredError(
                    "Only a guardian or household admin may view the audit log",
                ) from exc
        return await self.list_audit_log(minor_user_id, limit=limit)

    async def is_blocked_for_minor(
        self,
        minor_user_id: str,
        other_user_id: str,
    ) -> bool:
        return await self._repo.is_blocked_for_minor(
            minor_user_id,
            other_user_id,
        )

    # ─── Space age gate ──────────────────────────────────────────────────

    async def update_space_age_gate(
        self,
        space_id: str,
        *,
        min_age: int,
        target_audience: str,
        actor_user_id: str,
    ) -> None:
        """Set a space's ``min_age`` + ``target_audience``. Admin-only."""
        await self._require_admin(actor_user_id)
        if min_age not in _VALID_MIN_AGES:
            raise ValueError(f"min_age must be one of {sorted(_VALID_MIN_AGES)}")
        if target_audience not in _VALID_AUDIENCES:
            raise ValueError(
                f"target_audience must be one of {sorted(_VALID_AUDIENCES)}"
            )
        # Confirm the space exists so we don't silently swallow typos.
        if not await self._repo.space_exists(space_id):
            raise KeyError(f"space {space_id!r} not found")
        await self._repo.update_space_age_gate(
            space_id=space_id,
            min_age=min_age,
            target_audience=target_audience,
        )
        await self._bus.publish(
            CpSpaceAgeGateChanged(
                space_id=space_id,
                min_age=min_age,
                target_audience=target_audience,
            )
        )

    async def get_space_age_gate(self, space_id: str) -> dict:
        return await self._repo.get_space_age_gate(space_id)

    async def check_space_age_gate(
        self,
        space_id: str,
        user_id: str,
    ) -> None:
        """§CP.F1 enforcement — call before adding a member to a space.

        Raises :class:`SpacePermissionError` when a protected minor's
        ``declared_age`` is below the space's ``min_age``. No-op for
        non-protected users.
        """
        protection = await self._repo.get_user_protection(user_id)
        if protection is None or not protection["child_protection_enabled"]:
            return
        gate = await self.get_space_age_gate(space_id)
        min_age = int(gate["min_age"] or 0)
        if min_age == 0:
            return
        declared = int(protection["declared_age"] or 0)
        if declared < min_age:
            raise SpacePermissionError(
                f"This space is restricted to users aged {min_age}+."
            )

    # ─── DM enforcement (§CP.F3) ─────────────────────────────────────────

    async def is_dm_allowed(
        self,
        *,
        sender_user_id: str,
        target_instance_id: str | None,
    ) -> bool:
        """True iff a protected minor may DM the given target.

        Local DMs (``target_instance_id is None``) are always allowed.
        Cross-instance DMs require the target's instance to be a
        directly-paired peer — no relay hops permitted.
        """
        protection = await self._repo.get_user_protection(sender_user_id)
        if protection is None or not protection["child_protection_enabled"]:
            return True
        if not target_instance_id:
            return True
        # Direct pair = a remote_instance row that exists and is
        # confirmed (not via relay introduction).
        info = await self._repo.get_remote_instance_status(target_instance_id)
        if info is None:
            return False
        return info["status"] == "confirmed" and info["source"] == "manual"

    # ─── Internals ────────────────────────────────────────────────────────

    async def _require_admin(self, user_id: str) -> None:
        if not await self._repo.is_admin(user_id):
            raise SpacePermissionError("Only household admins may perform this action")

    async def _require_guardian(
        self,
        guardian_user_id: str,
        minor_user_id: str,
    ) -> None:
        if not await self.is_guardian_of(guardian_user_id, minor_user_id):
            raise GuardianRequiredError(
                f"User {guardian_user_id!r} is not a guardian of {minor_user_id!r}"
            )
