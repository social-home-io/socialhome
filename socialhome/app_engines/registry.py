"""App engines — Social Home features an app at a table calls (v_78).

An *app engine* runs on every household at a group table for an app:
the app only asks and hears the state, Social Home does the work (and,
where it matters, the cryptography) and shows anything private in its own
screens. The first is the gift draw (:mod:`socialhome.app_engines.draw`);
the design already names a later "deal" engine (each person secretly gets
one of N cards or roles).

* :class:`AppEngine` — what an engine is to core: its id, its suites, the
  steps it sends between households, how it attaches to the group service
  and the bus, how it takes an inbound step and how it answers its routes.
* :class:`AppEngineRegistry` — the engines this Social Home has. It is the
  one gate: an app may call only an engine it **declares** in its manifest
  (``engines: ["draw"]``) and this Social Home **has**
  (:class:`AppEngineNotDeclaredError` / :class:`AppEngineUnsupportedError`);
  an inbound step is taken only for a known engine, a suite of that
  engine, and a table whose app declares it.

On the wire every engine shares ONE sealed ``APP_SESSION`` verb,
``engine``, carrying ``{engine, engine_suite, step, …}`` between a table's
hub and each player's household (see ``docs/protocol/app-engines.md``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .domain import (
    ENGINE_SUITE_FIELD,
    AppEngineNotDeclaredError,
    AppEngineUnsupportedError,
    EngineCall,
)

if TYPE_CHECKING:
    from ..domain.app_group import GroupSession
    from ..domain.federation import FederationEvent
    from ..repositories.app_repo import AbstractAppRepo

log = logging.getLogger(__name__)


@runtime_checkable
class AppEngine(Protocol):
    """An app engine (see the module docstring)."""

    #: The engine's id — what a manifest declares and the wire names.
    engine_id: str
    #: The suites its wire shapes may carry (``engine_suite``); a step
    #: naming another is refused.
    suites: frozenset[str]
    #: The steps it sends between households (``step``).
    steps: frozenset[str]

    async def on_inbound(self, event: FederationEvent, table: GroupSession) -> None:
        """An inbound step for *table* — engine and suite already checked."""
        ...

    async def call(self, call: EngineCall) -> dict:
        """A route call from Social Home's own screens or the app bridge
        (``GET/POST …/engines/{engine}[/{action}]``)."""
        ...

    def table_changed(self, table: GroupSession) -> None:
        """The table's roster or state changed on this household."""
        ...

    def person_left(self, table: GroupSession, username: str) -> None:
        """A person of this household left the table (or was removed)."""
        ...

    async def stop(self) -> None:
        """Let the engine's own tasks finish."""
        ...


class AppEngineRegistry:
    """The engines this Social Home has, and the gate in front of them."""

    __slots__ = ("_app_repo", "_engines")

    def __init__(self, *, app_repo: AbstractAppRepo) -> None:
        self._app_repo = app_repo
        self._engines: dict[str, AppEngine] = {}

    def register(self, engine: AppEngine) -> None:
        if engine.engine_id in self._engines:
            raise ValueError(f"engine {engine.engine_id!r} registered twice")
        self._engines[engine.engine_id] = engine

    @property
    def ids(self) -> tuple[str, ...]:
        """The engines this Social Home has, sorted."""
        return tuple(sorted(self._engines))

    def get(self, engine_id: str) -> AppEngine | None:
        return self._engines.get(engine_id)

    def all(self) -> list[AppEngine]:
        return [self._engines[i] for i in self.ids]

    async def declared(self, app_id: str) -> tuple[str, ...]:
        """The engines the installed app *app_id* declares (none when it
        isn't installed)."""
        app = await self._app_repo.get(app_id)
        return app.manifest.engines if app is not None else ()

    async def require(self, app_id: str, engine_id: str) -> AppEngine:
        """The engine an app's call names — only one it declares
        (:class:`AppEngineNotDeclaredError`) and this Social Home has
        (:class:`AppEngineUnsupportedError`)."""
        if engine_id not in await self.declared(app_id):
            raise AppEngineNotDeclaredError(engine_id)
        engine = self._engines.get(engine_id)
        if engine is None:
            raise AppEngineUnsupportedError(engine_id)
        return engine

    async def on_inbound(self, event: FederationEvent, table: GroupSession) -> None:
        """An inbound ``engine`` verb for *table*: dropped (with a log
        line) unless it names an engine this Social Home has, one of that
        engine's suites, and the table's app declares the engine here."""
        payload: dict[str, Any] = event.payload
        engine_id = payload.get("engine")
        engine = self._engines.get(engine_id) if isinstance(engine_id, str) else None
        if engine is None:
            log.info("app_engines: unknown engine from %s dropped", event.from_instance)
            return
        if payload.get(ENGINE_SUITE_FIELD) not in engine.suites:
            log.info(
                "app_engines: %s step from %s with %s %r dropped",
                engine.engine_id,
                event.from_instance,
                ENGINE_SUITE_FIELD,
                payload.get(ENGINE_SUITE_FIELD),
            )
            return
        if payload.get("step") not in engine.steps:
            log.info("app_engines: unknown %s step dropped", engine.engine_id)
            return
        if engine.engine_id not in await self.declared(table.app_id):
            log.info(
                "app_engines: %s step for an app that doesn't declare it dropped",
                engine.engine_id,
            )
            return
        await engine.on_inbound(event, table)

    def table_changed(self, table: GroupSession) -> None:
        for engine in self.all():
            engine.table_changed(table)

    def person_left(self, table: GroupSession, username: str) -> None:
        for engine in self.all():
            engine.person_left(table, username)

    async def stop(self) -> None:
        for engine in self.all():
            await engine.stop()


__all__ = ["AppEngine", "AppEngineRegistry"]
