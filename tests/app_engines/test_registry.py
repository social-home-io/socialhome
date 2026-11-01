"""The app engine registry (v_78): the one gate in front of every engine.

An app may call only an engine its manifest declares and this Social Home
has; an inbound ``engine`` step is taken only for a known engine, one of
its suites and steps, and a table whose app — as installed HERE — declares
the engine (never the sender's word)."""

from __future__ import annotations

import dataclasses
import logging

import pytest

from socialhome.app_engines import (
    AppEngine,
    AppEngineNotDeclaredError,
    AppEngineRegistry,
    AppEngineUnsupportedError,
    EngineCall,
)
from socialhome.domain.app_group import GroupSession
from socialhome.domain.federation import FederationEvent, FederationEventType
from tests.app_group_network import quiz_app


class _Repo:
    def __init__(self, engines: tuple[str, ...]) -> None:
        app = quiz_app()
        self.app = dataclasses.replace(
            app, manifest=dataclasses.replace(app.manifest, engines=engines)
        )

    async def get(self, app_id: str):
        return self.app if app_id == self.app.app_id else None


class _Engine:
    engine_id = "draw"
    suites = frozenset({"s1"})
    steps = frozenset({"start"})

    def __init__(self) -> None:
        self.inbound: list = []
        self.changed: list = []
        self.left: list = []
        self.stopped = False

    async def on_inbound(self, event, table) -> None:
        self.inbound.append(event)

    async def call(self, call: EngineCall) -> dict:
        return {"ok": call.action}

    def table_changed(self, table) -> None:
        self.changed.append(table)

    def person_left(self, table, username) -> None:
        self.left.append(username)

    async def stop(self) -> None:
        self.stopped = True


def _table() -> GroupSession:
    return GroupSession(
        session_id="t1",
        app_id="quiz",
        host_instance="hub",
        host_user="anna",
        hub_instance="hub",
        state="playing",
        min_players=3,
        max_players=30,
        roster=(),
        roster_rev=1,
        created_at="2026-11-01T00:00:00+00:00",
        expires_at="2026-11-04T00:00:00+00:00",
        updated_at="2026-11-01T00:00:00+00:00",
    )


def _event(**payload) -> FederationEvent:
    return FederationEvent(
        msg_id="m",
        event_type=FederationEventType.APP_SESSION,
        from_instance="hub",
        to_instance="b",
        timestamp="2026-11-01T00:00:00+00:00",
        payload={"verb": "engine", **payload},
    )


async def test_an_app_calls_only_engines_it_declares_and_this_home_has():
    reg = AppEngineRegistry(app_repo=_Repo(("draw", "deal")))  # type: ignore[arg-type]
    engine = _Engine()
    reg.register(engine)
    assert isinstance(engine, AppEngine)
    assert reg.ids == ("draw",)
    assert await reg.require("quiz", "draw") is engine
    with pytest.raises(AppEngineUnsupportedError):
        await reg.require("quiz", "deal")  # declared, not here
    with pytest.raises(AppEngineNotDeclaredError):
        await reg.require("quiz", "poker")
    with pytest.raises(AppEngineNotDeclaredError):
        await reg.require("other-app", "draw")
    with pytest.raises(ValueError):
        reg.register(_Engine())


async def test_inbound_steps_are_gated_on_engine_suite_step_and_declaration(caplog):
    caplog.set_level(logging.INFO)
    engine = _Engine()
    reg = AppEngineRegistry(app_repo=_Repo(("draw",)))  # type: ignore[arg-type]
    reg.register(engine)
    for payload in (
        {"engine": "deal", "engine_suite": "s1", "step": "start"},
        {"engine": 5, "engine_suite": "s1", "step": "start"},
        {"engine": "draw", "engine_suite": "s2", "step": "start"},
        {"engine": "draw", "step": "start"},
        {"engine": "draw", "engine_suite": "s1", "step": "nope"},
    ):
        await reg.on_inbound(_event(**payload), _table())
    assert engine.inbound == []
    await reg.on_inbound(
        _event(engine="draw", engine_suite="s1", step="start"), _table()
    )
    assert len(engine.inbound) == 1
    # The same step for an app that, installed here, doesn't declare it.
    undeclared = AppEngineRegistry(app_repo=_Repo(()))  # type: ignore[arg-type]
    other = _Engine()
    undeclared.register(other)
    await undeclared.on_inbound(
        _event(engine="draw", engine_suite="s1", step="start"), _table()
    )
    assert other.inbound == []
    assert "doesn't declare it" in caplog.text


async def test_table_changes_and_stop_reach_every_engine():
    engine = _Engine()
    reg = AppEngineRegistry(app_repo=_Repo(("draw",)))  # type: ignore[arg-type]
    reg.register(engine)
    reg.table_changed(_table())
    reg.person_left(_table(), "ben")
    await reg.stop()
    assert len(engine.changed) == 1 and engine.left == ["ben"] and engine.stopped
    assert reg.get("draw") is engine and reg.get("x") is None
    assert await reg.declared("nope") == ()
