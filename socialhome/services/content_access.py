"""Per-feature access levels on the local write paths (§4.3).

A space sets one access level per collaborative feature — ``posts``,
``pages``, ``tasks`` (task lists included), ``stickies``, ``calendar`` —
to ``OPEN``, ``MODERATED`` or ``ADMIN_ONLY``
(:class:`~socialhome.domain.space.SpaceFeatureAccess`). The decision itself
is the pure :meth:`SpaceFeatures.access_decision`; this mixin is where every
write path asks it, so the services that own a feature's writes
(``SpaceService`` posts, ``SpaceTaskService``, ``StickyService``,
``SpaceCalendarService``, ``SpacePageService``) answer the same way.

Every household enforces its own copy of the space's features — the host
and every member household's stub alike (the features federate with the
space metadata and every config change). The receivers enforce them a
second time on inbound federation (``SpaceAuthorship.access_admits``), so a
household that skips this gate still cannot write into anybody else's copy.

Why a behaviour-only mixin (``__slots__ = ()``): the consumers already
compose :class:`~socialhome.services.bus_publisher.BusPublisherMixin` (and
the calendar :class:`ProtectionGateMixin`), and Python allows only one
slotted base. The mixin reads the consumer's space repo through
:meth:`ContentAccessMixin._access_space_repo` — ``self._spaces`` by
default; a consumer keeping it under another name overrides the method.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.space import (
    AccessAdminOnlyError,
    AccessDecision,
    ContentAction,
    Space,
    SpaceFeatureAccess,
    SpacePermissionError,
)

if TYPE_CHECKING:
    from ..repositories.space_repo import AbstractSpaceRepo


class ContentAccessMixin:
    """Mixin: gate a write on the space's per-feature access level."""

    __slots__ = ()

    def _access_space_repo(self) -> "AbstractSpaceRepo":
        """The space repo the gate reads. The consumer declares the slot."""
        repo: "AbstractSpaceRepo" = getattr(self, "_spaces")
        return repo

    async def _gate(
        self,
        space_or_id: Space | str,
        actor_user_id: str,
        feature: str,
        action: ContentAction,
        owns_target: bool,
    ) -> AccessDecision:
        """Decide ``actor_user_id``'s ``action`` on ``feature`` in a space.

        The role is the actor's local ``space_members`` seat. Raises
        :class:`AccessAdminOnlyError` (403 ``ACCESS_ADMIN_ONLY``) when an
        ``ADMIN_ONLY`` feature refuses the actor, a plain
        :class:`SpacePermissionError` for any other refusal, and
        :class:`KeyError` for an unknown space. Returns PROCEED or, for a
        ``posts`` create under ``MODERATED``, QUEUE — the post path owns
        the queue.
        """
        repo = self._access_space_repo()
        if isinstance(space_or_id, Space):
            space: Space | None = space_or_id
        else:
            space = await repo.get(space_or_id)
        if space is None:
            raise KeyError(f"space {space_or_id!r} not found")
        level = space.features.access_level(feature)
        if level is SpaceFeatureAccess.OPEN:
            return AccessDecision.PROCEED
        member = await repo.get_member(space.id, actor_user_id)
        decision = space.features.access_decision(
            feature,
            role=member.role if member is not None else None,
            action=action,
            owns_target=owns_target,
        )
        if decision is AccessDecision.DENY:
            if level is SpaceFeatureAccess.ADMIN_ONLY:
                raise AccessAdminOnlyError(feature)
            raise SpacePermissionError(f"not allowed to change {feature} here")
        if decision is AccessDecision.QUEUE and feature != "posts":
            # TODO(PR3): queue MODERATED pages / tasks / stickies / calendar
            # writes for review like posts. Until the moderation queue knows
            # those types they behave as OPEN.
            return AccessDecision.PROCEED
        return decision
