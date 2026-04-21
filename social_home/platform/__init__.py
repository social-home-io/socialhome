"""Platform abstraction layer.

Exports the adapter Protocol, shared value types, and the
:func:`build_platform_adapter` factory that picks between the HA and
standalone concrete implementations. The factory lives in the package
``__init__`` so it can import both concrete adapter modules at the top
level without creating a circular dependency through ``adapter.py``.
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
    _PLATFORM_SECTION,
)
from .ha import HomeAssistantAdapter
from .standalone import StandaloneAdapter


def build_platform_adapter(
    mode: str,
    db: AsyncDatabase,
    config: Config,
) -> AbstractPlatformAdapter:
    """Instantiate the platform adapter selected by ``mode``.

    :param mode: ``"ha"`` → :class:`HomeAssistantAdapter`;
                 ``"standalone"`` → :class:`StandaloneAdapter`.
    :raises ValueError: if ``mode`` is not recognised.

    The adapter receives its own ``[<mode>]`` TOML section via
    :attr:`Config.platform_options`. HA credentials come from
    :attr:`Config.ha_url` / :attr:`Config.ha_token`
    (``SH_HA_URL`` / ``SH_HA_TOKEN`` or ``[homeassistant] url=`` / ``token=``);
    when the Supervisor is in the picture (``SUPERVISOR_TOKEN`` set in the
    environment by the add-on) the adapter ignores those and routes HA
    calls through ``http://supervisor/core/api`` instead.
    """
    options: Mapping[str, Any] = config.platform_options.get(
        _PLATFORM_SECTION[mode] if mode in _PLATFORM_SECTION else mode,
        MappingProxyType({}),
    )
    match mode:
        case "ha":
            return HomeAssistantAdapter(
                ha_url=config.ha_url,
                ha_token=config.ha_token,
                supervisor_url=os.environ.get(
                    "SUPERVISOR_URL",
                    "http://supervisor",
                ),
                supervisor_token=os.environ.get("SUPERVISOR_TOKEN", ""),
                data_dir=config.data_dir,
                options=options,
            )
        case "standalone":
            return StandaloneAdapter(db=db, config=config, options=options)
        case _:
            raise ValueError(
                f"Unknown platform mode {mode!r} — expected 'ha' or 'standalone'",
            )


__all__ = [
    "AbstractPlatformAdapter",
    "ExternalUser",
    "HomeAssistantAdapter",
    "InstanceConfig",
    "StandaloneAdapter",
    "build_platform_adapter",
]
