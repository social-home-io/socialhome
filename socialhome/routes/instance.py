"""Instance metadata route — what mode are we in, what can the SPA show?

`GET /api/instance/config` is the SPA's first call on cold start. The
response carries the deployment mode, the adapter's capability set, and
the first-boot setup flag so the SPA knows whether to route the user
to `/setup` instead of `/login`.

Public path — no auth required, intentionally. The SPA needs this
*before* it has a token.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..app_keys import (
    config_key,
    federation_repo_key,
    platform_adapter_key,
    setup_service_key,
)
from .base import BaseView
from .spa import _static_dir_key as _spa_static_dir_key
from .spa import get_spa_bundle_hash

log = logging.getLogger(__name__)


class InstanceConfigView(BaseView):
    """``GET /api/instance/config`` — mode + capabilities + setup flag."""

    async def get(self) -> web.Response:
        config = self.svc(config_key)
        adapter = self.svc(platform_adapter_key)
        setup = self.svc(setup_service_key)
        capabilities = sorted(str(c) for c in adapter.capabilities)
        # SPA bundle hash — surfaced so a tab that's been open across a
        # backend deploy can poll this endpoint, notice the mismatch
        # against the hash it booted with, and prompt the user to
        # reload (see ``client/src/components/SpaUpdateBanner.tsx``).
        # ``None`` when the backend isn't serving the SPA (dev mode
        # behind ``pnpm dev``) or the bundle template is missing the
        # expected ``index-{hash}.js`` script tag.
        spa_bundle_hash: str | None = None
        static_dir = self.request.app.get(_spa_static_dir_key)
        if static_dir is not None:
            spa_bundle_hash = get_spa_bundle_hash(static_dir)
        # Surface this instance's stable id so SPA features that embed
        # the id in cross-instance payloads (e.g. space-invite codes)
        # don't have to round-trip through /api/friends to discover it.
        # Pre-setup the federation tables don't exist yet — degrade to
        # ``None`` rather than fail the cold-start config probe.
        instance_id: str | None = None
        instance_display_name: str | None = None
        fed_repo = self.request.app.get(federation_repo_key)
        if fed_repo is not None:
            try:
                identity = await fed_repo.get_local_identity()
                if identity is not None:
                    instance_id = identity.get("instance_id")
                    instance_display_name = identity.get("display_name")
            except Exception:
                log.debug(
                    "InstanceConfigView.get: fail-soft error ignored", exc_info=True
                )
                instance_id = None
        return web.json_response(
            {
                "mode": config.mode,
                "instance_name": instance_display_name or config.instance_name,
                "instance_id": instance_id,
                "capabilities": capabilities,
                "setup_required": await setup.is_required(),
                "spa_bundle_hash": spa_bundle_hash,
            }
        )
