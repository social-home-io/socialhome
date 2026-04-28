"""Platform abstraction layer.

Exports the adapter ABC, shared value types, and the
:func:`build_platform_adapter` factory that picks between the three
concrete implementations: standalone (local SQLite),
:class:`HaAdapter` (HA Core REST), :class:`HaosAdapter` (HA Supervisor
add-on with Ingress).
"""

from __future__ import annotations

import os
from types import MappingProxyType
from typing import Any, Mapping

from ..config import Config
from ..db import AsyncDatabase
from .adapter import (
    AbstractPlatformAdapter,
    ExternalUser,
    InstanceConfig,
    PlatformAdapter,
    _PLATFORM_SECTION,
)
from .ha import HaAdapter, HomeAssistantAdapter
from .haos import HaosAdapter
from .standalone import StandaloneAdapter


def build_platform_adapter(
    mode: str,
    db: AsyncDatabase,
    config: Config,
) -> PlatformAdapter:
    """Instantiate the platform adapter selected by ``mode``.

    * ``"standalone"`` → :class:`StandaloneAdapter` (local users, no HA).
    * ``"ha"`` → :class:`HaAdapter` (talks to HA Core via REST).
    * ``"haos"`` → :class:`HaosAdapter` (Supervisor proxy + Ingress).

    Each adapter receives its own ``[<section>]`` TOML section via
    :attr:`Config.platform_options`. HA credentials (``ha_url`` /
    ``ha_token``) only apply to ``ha`` mode; ``haos`` mode reads
    ``SUPERVISOR_TOKEN`` from the environment instead (injected by the
    Supervisor at runtime — there is no TOML mirror).
    """
    options: Mapping[str, Any] = config.platform_options.get(
        _PLATFORM_SECTION.get(mode, mode),
        MappingProxyType({}),
    )
    match mode:
        case "standalone":
            return StandaloneAdapter(db=db, config=config, options=options)
        case "ha":
            return HaAdapter(
                ha_url=config.ha_url,
                ha_token=config.ha_token,
                data_dir=config.data_dir,
                options=options,
            )
        case "haos":
            return HaosAdapter(
                supervisor_url=os.environ.get(
                    "SUPERVISOR_URL",
                    "http://supervisor",
                ),
                supervisor_token=os.environ["SUPERVISOR_TOKEN"],
                data_dir=config.data_dir,
                options=options,
            )
        case _:
            raise ValueError(
                f"Unknown platform mode {mode!r} — expected one of "
                "'standalone', 'ha', 'haos'",
            )


__all__ = [
    "AbstractPlatformAdapter",
    "ExternalUser",
    "HaAdapter",
    "HaosAdapter",
    "HomeAssistantAdapter",
    "InstanceConfig",
    "PlatformAdapter",
    "StandaloneAdapter",
    "build_platform_adapter",
]
