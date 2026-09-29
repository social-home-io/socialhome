"""Profile-sync to GFS (§Momentum-public).

Bus subscriber on :class:`UserProfileUpdated`. When a household-side
profile changes (display name, bio, avatar), re-register the user on
every active GFS so the public Momentum directory stays current.

Fire-and-forget: the local DB write has already happened by the time
the event lands here. A failure to push to a GFS doesn't surface back
to the route handler — it logs and drops. Operators reconcile on the
next save (or on the user's next moment publish, whichever comes
first).

The actual HTTP round-trip lives in
:meth:`MomentPublicService.push_profile_to_gfs` so any future change
to the register envelope only has to land in one place.
"""

from __future__ import annotations

import logging

from ..domain.child_protection import AccountProtectedError
from ..domain.events import UserProfileUpdated
from ..infrastructure.event_bus import EventBus
from ..repositories.moment_public_repo import (
    AbstractMomentPublicRegistrationRepo,
)
from .moment_public_service import MomentPublicError, MomentPublicService

log = logging.getLogger(__name__)


class ProfileSyncService:
    """Re-pushes display_name / bio / avatar to every active GFS."""

    __slots__ = ("_bus", "_regs", "_public")

    def __init__(
        self,
        *,
        bus: EventBus,
        registration_repo: AbstractMomentPublicRegistrationRepo,
        public_service: MomentPublicService,
    ) -> None:
        self._bus = bus
        self._regs = registration_repo
        self._public = public_service

    def wire(self) -> None:
        self._bus.subscribe(UserProfileUpdated, self._on_profile_updated)

    async def _on_profile_updated(self, event: UserProfileUpdated) -> None:
        regs = await self._regs.list_for_user(event.user_id)
        if not regs:
            return
        for reg in regs:
            try:
                await self._public.push_profile_to_gfs(
                    user_id=event.user_id, gfs_id=reg.gfs_id
                )
            except AccountProtectedError:
                # §CP.R: a registration from before protection no longer
                # refreshes the public directory entry.
                log.info(
                    "profile_sync: user %s is protected — not pushed to gfs %s",
                    event.user_id,
                    reg.gfs_id,
                )
                return
            except MomentPublicError as exc:
                log.warning(
                    "profile_sync: push to gfs %s failed for user %s: %s",
                    reg.gfs_id,
                    event.user_id,
                    exc,
                )
