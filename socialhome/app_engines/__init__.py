"""App engines (v_78): Social Home features apps call at their tables.

See :mod:`socialhome.app_engines.registry` and
``docs/protocol/app-engines.md``. Engines: :mod:`.draw` (the gift draw).
"""

from .domain import (
    ENGINE_SUITE_FIELD,
    VERB_ENGINE,
    AppEngineActionError,
    AppEngineNotDeclaredError,
    AppEngineUnsupportedError,
    EngineCall,
)
from .registry import AppEngine, AppEngineRegistry

__all__ = [
    "ENGINE_SUITE_FIELD",
    "VERB_ENGINE",
    "AppEngine",
    "AppEngineActionError",
    "AppEngineNotDeclaredError",
    "AppEngineRegistry",
    "AppEngineUnsupportedError",
    "EngineCall",
]
