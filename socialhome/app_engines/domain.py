"""App engines — the pure shapes every engine shares (v_78).

The one ``APP_SESSION`` verb of all engines, its fields, the route call an
engine answers and the gate's coded errors. Pure data, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.errors import CodedError

#: The one sealed ``APP_SESSION`` verb every engine's steps ride.
VERB_ENGINE: str = "engine"
#: The field naming the engine's suite on every step (``*_suite`` rule).
ENGINE_SUITE_FIELD: str = "engine_suite"


@dataclass(slots=True, frozen=True)
class EngineCall:
    """A route call to an engine: ``GET …/engines/{engine}`` (``action``
    ``None``) or ``GET/POST …/engines/{engine}/{action}`` with its JSON
    body, by a person of this household."""

    app_id: str
    session_id: str
    actor_user_id: str
    method: str
    action: str | None = None
    body: dict[str, Any] = field(default_factory=dict)


class AppEngineNotDeclaredError(CodedError):
    """The app calls an engine its manifest doesn't declare."""

    status = 403
    code = "APP_ENGINE_NOT_DECLARED"
    detail = "This app didn't ask to use that Social Home feature."

    def __init__(self, engine_id: str) -> None:
        super().__init__(params={"engine": engine_id})


class AppEngineUnsupportedError(CodedError):
    """The app declares an engine this Social Home doesn't have."""

    status = 409
    code = "APP_ENGINE_UNSUPPORTED"
    detail = "This Social Home doesn't have that feature."

    def __init__(self, engine_id: str) -> None:
        super().__init__(params={"engine": engine_id})


class AppEngineActionError(CodedError):
    """No such action (or method) on the engine's routes."""

    status = 404
    code = "APP_ENGINE_ACTION"
    detail = "No such action."


__all__ = [
    "ENGINE_SUITE_FIELD",
    "VERB_ENGINE",
    "AppEngineActionError",
    "AppEngineNotDeclaredError",
    "AppEngineUnsupportedError",
    "EngineCall",
]
