"""Protected-account guard mixin (§CP.R).

Every service that owns a surface a protected account may not use
(bazaar, public spaces, public moments, public highlight links, API
tokens, calendar feeds) runs the same check before acting::

    await self._require_unrestricted(user_id, ProtectedCapability.BAZAAR)

The rule itself lives in
:meth:`ChildProtectionService.require_unrestricted`; this mixin owns the
shared wiring (``attach_child_protection``) and the call, so each
consumer is one line at the write path instead of a copy of the lookup.

Behaviour-only (``__slots__ = ()``): the consumer declares
``_child_protection`` in its own ``__slots__`` and sets it to ``None`` in
``__init__``, so the mixin composes with other slotted mixins
(``SpaceMemberGuardMixin``, ``BusPublisherMixin``). Production always
wires the service in ``app.py``; the ``None`` branch only serves unit
tests that construct a service in isolation, matching the long-standing
``SpaceService`` age-gate hook.

The same wiring carries the guardian-block reads (§CP.F2) —
:meth:`_guardian_blocked` / :meth:`_guardian_block_counterparts` — and two
hooks :class:`ChildProtectionService` calls on every attached service, so
what was set up *before* a change reacts too:

* :meth:`on_account_protected` — the account just became protected: revoke
  or close what it shared earlier (tokens, public links, listings…).
* :meth:`on_guardian_block` — a guardian just blocked someone for the
  account: step out of what the two still share.

Both default to no-ops; a service overrides the ones it owns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.child_protection import ProtectedCapability

if TYPE_CHECKING:
    from .child_protection_service import ChildProtectionService


class ProtectionGateMixin:
    """Mixin: refuse restricted surfaces for protected accounts."""

    __slots__ = ()

    _child_protection: "ChildProtectionService | None"

    def attach_child_protection(
        self, child_protection_service: "ChildProtectionService"
    ) -> None:
        """Wire :class:`ChildProtectionService` for the §CP hooks."""
        # The slot lives on the consumer (see module docstring); mypy only
        # sees this class's empty ``__slots__``.
        self._child_protection = child_protection_service  # type: ignore[misc]
        if child_protection_service is not None:
            child_protection_service.register_gate(self)

    async def on_account_protected(self, user_id: str) -> None:
        """Hook: *user_id* just became a protected account (no-op here)."""

    async def on_guardian_block(self, minor_user_id: str, blocked_user_id: str) -> None:
        """Hook: a guardian blocked *blocked_user_id* for *minor_user_id*."""

    async def _is_protected(self, user_id: str) -> bool:
        """Whether *user_id* is a protected account."""
        if self._child_protection is None:
            return False
        return await self._child_protection.is_protected(user_id)

    async def _guardian_blocked(self, user_a: str, user_b: str) -> bool:
        """Whether a guardian block separates the two users (§CP.F2)."""
        if self._child_protection is None:
            return False
        return await self._child_protection.is_guardian_blocked(user_a, user_b)

    async def _guardian_blocks_household(
        self, user_id: str | None, instance_id: str
    ) -> bool:
        """Whether a guardian block of *user_id* (``None``: of any protected
        account here) names someone homed on *instance_id*."""
        if self._child_protection is None:
            return False
        return await self._child_protection.guardian_blocks_household(
            user_id, instance_id
        )

    async def _guardian_block_counterparts(self, user_id: str) -> frozenset[str]:
        """Everyone a guardian block separates from *user_id*."""
        if self._child_protection is None:
            return frozenset()
        return await self._child_protection.guardian_block_counterparts(user_id)

    async def _is_restricted(
        self,
        user_id: str,
        capability: ProtectedCapability,
    ) -> bool:
        """Non-raising variant for background paths (fan-out, feed
        serving) that skip a protected account instead of erroring."""
        if self._child_protection is None:
            return False
        return await self._child_protection.is_restricted(user_id, capability)

    async def _require_unrestricted(
        self,
        user_id: str,
        capability: ProtectedCapability,
    ) -> None:
        """Raise :class:`AccountProtectedError` when *user_id* is a
        protected account and *capability* is restricted for it."""
        if self._child_protection is not None:
            await self._child_protection.require_unrestricted(user_id, capability)
