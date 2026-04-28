"""Home Assistant OS / Supervisor add-on platform.

Adapter for Social Home running as an HA add-on with the Supervisor
ingress proxy in front. Reuses the HA Core provider classes from
:mod:`socialhome.platform.ha.providers` but swaps in
:class:`HaIngressAuthProvider` (trust the Supervisor-injected
``X-Ingress-User`` header without a bearer fallback) and runs
:class:`HaBootstrap` on first boot to provision the HA owner.
"""

from __future__ import annotations

from .adapter import HaIngressAuthProvider, HaosAdapter
from .bootstrap import (
    BOOTSTRAP_FLAG,
    INTEGRATION_TOKEN_FILENAME,
    INTEGRATION_TOKEN_LABEL,
    HaBootstrap,
)
from .supervisor import SupervisorClient

__all__ = [
    "BOOTSTRAP_FLAG",
    "HaBootstrap",
    "HaIngressAuthProvider",
    "HaosAdapter",
    "INTEGRATION_TOKEN_FILENAME",
    "INTEGRATION_TOKEN_LABEL",
    "SupervisorClient",
]
