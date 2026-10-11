"""First-boot setup service (§platform/v2).

Owns the `setup_complete` flag in the existing ``instance_config`` kv
table. Routes consult this service to decide whether to gate themselves
behind the wizard, and the three mode-specific setup handlers
(:mod:`socialhome.routes.setup`) call :meth:`mark_complete` after their
respective provisioning step succeeds.

The service has no opinion on *which* mode is in play — that's the
factory's job. It only cares whether the operator has reached the end
of the setup flow at least once.
"""

from __future__ import annotations

import logging

from ..repositories.instance_config_repo import AbstractInstanceConfigRepo

log = logging.getLogger(__name__)

#: Key in ``instance_config`` flagging "wizard finished at least once".
#: Set to ``"1"`` by :meth:`SetupService.mark_complete`.
SETUP_COMPLETE_KEY = "setup_complete"


class SetupService:
    """Tracks first-boot setup state via the ``instance_config`` kv table.

    Idempotent: :meth:`mark_complete` is safe to call repeatedly. The
    only state transition is unset → ``"1"``; we never clear it (a
    re-setup flow would have to be a separate decision).
    """

    __slots__ = ("_instance_config",)

    def __init__(self, instance_config: AbstractInstanceConfigRepo) -> None:
        self._instance_config = instance_config

    async def is_required(self) -> bool:
        """``True`` until :meth:`mark_complete` has been called once."""
        value = await self._instance_config.get(SETUP_COMPLETE_KEY)
        return (value or "").strip() != "1"

    async def mark_complete(self) -> None:
        """Persist the "setup finished" flag. Safe to call repeatedly."""
        await self._instance_config.set(SETUP_COMPLETE_KEY, "1")
