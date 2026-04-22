"""Home Assistant platform — adapter, bootstrap, and API clients."""

from __future__ import annotations

from .adapter import HomeAssistantAdapter
from .bootstrap import (
    BOOTSTRAP_FLAG,
    INTEGRATION_TOKEN_FILENAME,
    INTEGRATION_TOKEN_LABEL,
    HaBootstrap,
)
from .client import HaClient, build_ha_client
from .supervisor import SupervisorClient

__all__ = [
    "BOOTSTRAP_FLAG",
    "HaBootstrap",
    "HaClient",
    "HomeAssistantAdapter",
    "INTEGRATION_TOKEN_FILENAME",
    "INTEGRATION_TOKEN_LABEL",
    "SupervisorClient",
    "build_ha_client",
]
