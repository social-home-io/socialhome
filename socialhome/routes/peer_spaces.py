"""Peer-public-space directory routes (§D1a) — /api/peer_spaces.

Serves the aggregated directory of ``type=public`` spaces hosted by all
CONFIRMED peer households. Populated by the inbound
:class:`PeerDirectoryHandler` from ``SPACE_DIRECTORY_SYNC`` envelopes.
"""

from __future__ import annotations

from aiohttp import web

from .. import app_keys as K
from ..domain.federation import PairingStatus
from .base import BaseView


class PeerSpaceCollectionView(BaseView):
    """``GET /api/peer_spaces`` — list visible peer-hosted public spaces."""

    async def get(self) -> web.Response:
        ctx = self.user
        if ctx is None or ctx.user_id is None:
            return web.json_response({"error": "unauthenticated"}, status=401)

        # §CP.F1 discovery filter — mirror the public_spaces route.
        cp_svc = self.svc(K.child_protection_service_key)
        max_min_age: int | None = None
        protection = await cp_svc._repo.get_user_protection(  # noqa: SLF001
            ctx.user_id,
        )
        if protection and protection.get("child_protection_enabled"):
            max_min_age = int(protection.get("declared_age") or 0)

        repo = self.svc(K.peer_space_directory_repo_key)
        entries = await repo.list_all(max_min_age=max_min_age)
        federation_repo = self.svc(K.federation_repo_key)
        unique_instances = {e.instance_id for e in entries}
        host_info: dict[str, tuple[str, bool]] = {}
        for iid in unique_instances:
            inst = await federation_repo.get_instance(iid)
            if inst is None:
                host_info[iid] = (iid, False)
            else:
                host_info[iid] = (
                    inst.display_name or iid,
                    inst.status is PairingStatus.CONFIRMED,
                )
        return web.json_response(
            [
                {
                    "space_id": e.space_id,
                    "host_instance_id": e.instance_id,
                    "host_display_name": host_info.get(
                        e.instance_id, (e.instance_id, False)
                    )[0],
                    "host_is_paired": host_info.get(
                        e.instance_id, (e.instance_id, False)
                    )[1],
                    "name": e.name,
                    "description": e.description,
                    "emoji": e.emoji,
                    "member_count": e.member_count,
                    "join_mode": e.join_mode,
                    "min_age": e.min_age,
                    "target_audience": e.target_audience,
                }
                for e in entries
                # A peer must be CONFIRMED — don't show orphan cache rows
                # for instances we've since unpaired.
                if host_info.get(e.instance_id, ("", False))[1]
            ]
        )
