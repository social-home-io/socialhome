"""Home Assistant Core platform — adapter + REST client.

For the Supervisor add-on path (HAOS) see
:mod:`socialhome.platform.haos`. The two modes share provider classes
in :mod:`.providers` but live in separate adapter modules so neither
has to runtime-branch on ``SUPERVISOR_TOKEN``.

``HaBootstrap``, ``SupervisorClient`` and the related constants moved
to :mod:`socialhome.platform.haos` in the platform-adapter-v2 split;
import them from there. The aliases re-exported below keep
backwards-compat for older callers.
"""

from __future__ import annotations

from ..haos.bootstrap import (
    BOOTSTRAP_FLAG,
    INTEGRATION_TOKEN_FILENAME,
    INTEGRATION_TOKEN_LABEL,
    HaBootstrap,
)
from ..haos.supervisor import SupervisorClient
from .adapter import HaAdapter, HomeAssistantAdapter
from .client import HaClient, build_ha_client

__all__ = [
    "BOOTSTRAP_FLAG",
    "HaAdapter",
    "HaBootstrap",
    "HaClient",
    "HomeAssistantAdapter",
    "INTEGRATION_TOKEN_FILENAME",
    "INTEGRATION_TOKEN_LABEL",
    "SupervisorClient",
    "build_ha_client",
]
