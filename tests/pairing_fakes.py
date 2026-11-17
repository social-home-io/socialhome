"""Shared behaviour for the in-memory federation repos pairing tests use."""

from __future__ import annotations

from socialhome.domain.federation import RemoteInstance, is_space_seat


class SavesPendingMixin:
    """``save_pending_instance`` as the SQLite repo does it: the pending
    half becomes the household's row unless that row is a live space seat
    (then nothing is written and the caller keeps the half beside it).

    For a fake that has ``get_instance`` and ``save_instance``.
    """

    async def save_pending_instance(self, inst: RemoteInstance) -> bool:
        if is_space_seat(await self.get_instance(inst.id)):  # type: ignore[attr-defined]
            return False
        await self.save_instance(inst)  # type: ignore[attr-defined]
        return True
