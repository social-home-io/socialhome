"""Households that can draw names, for gift-draw tests (v_78).

Builds on :mod:`tests.app_group_network`: each household gets the real
app engine registry with the real :class:`AppDrawService` on top of its
real :class:`AppGroupService`, the app installed declaring
``engines: ["draw"]``, and a seeded random source (so a run is
reproducible). :func:`settle` delivers messages and runs each household's
own draw steps until the network is quiet.

Not a test module itself (no ``test_`` prefix).
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import random
from uuid import uuid4

from socialhome.app_engines import AppEngineRegistry
from socialhome.app_engines.draw import AppDrawService
from socialhome.domain.apps import InstalledApp
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.app_engine_state_repo import SqliteAppEngineStateRepo
from tests.app_group_network import APP, Household, Network, quiz_app


async def _supports(self, instance_id: str, *, min_version: int) -> bool:
    return self.versions.get(instance_id, 0) >= min_version


def draw_app(*, engines: tuple[str, ...] = ("draw",)) -> InstalledApp:
    """The quiz stand-in, 3–30 players, declaring *engines*."""
    app = quiz_app(players=(3, 30))
    return dataclasses.replace(
        app, manifest=dataclasses.replace(app.manifest, engines=engines)
    )


def engine_step(
    sid: str, step: str, *, gen: int = 1, round_no: int = 1, **fields
) -> dict:
    """A draw step as it rides the sealed ``engine`` verb."""
    return {
        "app_id": APP,
        "session_id": sid,
        "verb": "engine",
        "engine": "draw",
        "engine_suite": "x25519-hkdf-aesgcm256",
        "step": step,
        "gen": gen,
        "round": round_no,
        **fields,
    }


def sent_steps(net: Network, step: str) -> list[dict]:
    """Every draw *step* payload that went over the network."""
    return [
        p
        for _s, _t, p in net.sent
        if p.get("verb") == "engine" and p.get("step") == step
    ]


def add_draw(home: Household, *, seed: int | None = None) -> AppDrawService:
    """Give *home* the engine registry with a gift draw service (seeded
    randomness with *seed*); calling it again swaps in a fresh one."""
    fed = home.fed
    # The draw judges space members and paired households alike.
    type(fed).space_member_supports = _supports  # type: ignore[attr-defined]
    rand = random.Random(seed).randbytes if seed is not None else os.urandom
    kek = getattr(home, "kek", None) or KeyManager(os.urandom(32))
    home.kek = kek  # type: ignore[attr-defined]
    draw = AppDrawService(
        groups=home.groups,
        group_repo=home.group_repo,
        state_repo=SqliteAppEngineStateRepo(home.db),
        app_federation=home.app_fed,
        user_repo=home.user_repo,
        federation=fed,
        key_manager=kek,
        ws=home.ws,  # type: ignore[arg-type]
        household_label=f"Home {home.instance}",
        bus=home.bus,  # type: ignore[arg-type]
        rand=rand,
    )
    registry = AppEngineRegistry(app_repo=home.app_repo)
    registry.register(draw)
    home.groups.attach_engines(registry)
    home.draw = draw  # type: ignore[attr-defined]
    home.engines = registry  # type: ignore[attr-defined]
    return draw


async def settle(net: Network, *, rounds: int = 4000, drop=None) -> None:
    """Deliver everything queued and run every household's own steps
    until quiet. *drop* (``(sender, to, payload) -> bool``) loses a
    message on the way."""
    for _ in range(rounds):
        tasks = [
            t
            for h in net.homes.values()
            for t in (*h.groups._tasks, *getattr(h, "draw", None)._tasks)  # type: ignore[union-attr]
            if not t.done()
        ]
        if tasks:
            await asyncio.wait(tasks, timeout=1.0)
            continue
        if not net.queue:
            await asyncio.sleep(0)
            if not net.queue and not any(
                not t.done()
                for h in net.homes.values()
                for t in h.draw._tasks  # type: ignore[attr-defined]
            ):
                return
            continue
        sender, to, payload = net.queue.pop(0)
        if drop is not None and drop(sender, to, payload):
            continue
        target = net.homes.get(to)
        if target is None:
            continue
        await target.app_fed.on_inbound_event(
            FederationEvent(
                msg_id=uuid4().hex,
                event_type=net.types.get(id(payload), FederationEventType.APP_SESSION),
                from_instance=sender,
                to_instance=to,
                timestamp="2026-10-31T12:00:00+00:00",
                payload=dict(payload),
            )
        )
    raise AssertionError("the network never went quiet")


async def draw_table(
    net: Network,
    people: dict[str, list[str]],
    *,
    hub: str = "hub",
    seeds: bool = True,
) -> str:
    """Households (name → usernames) at one picked table opened by the
    first person of *hub*, everyone seated; returns the session id. Every
    household is paired with every other (identity keys known)."""
    homes: dict[str, Household] = {}
    for i, (inst, users) in enumerate(people.items()):
        home = await net.household(inst)
        for u in users:
            await home.add_user(u)
        await home.app_repo.install(draw_app())
        add_draw(home, seed=1000 + i if seeds else None)
        homes[inst] = home
    names = list(homes)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            await net.pair(homes[a], homes[b])
    h = homes[hub]
    host = people[hub][0]
    targets = [
        h.target(homes[inst], u)
        for inst, users in people.items()
        for u in users
        if (inst, u) != (hub, host)
    ]
    view = await h.groups.open_table(
        app_id=APP, actor_user_id=h.uid(host), targets=targets
    )
    sid = view["session_id"]
    await settle(net)
    for inst, users in people.items():
        for u in users:
            if (inst, u) == (hub, host):
                continue
            await homes[inst].groups.join(
                app_id=APP, session_id=sid, actor_user_id=homes[inst].uid(u)
            )
            await settle(net)
    return sid


__all__ = [
    "APP",
    "add_draw",
    "draw_app",
    "draw_table",
    "engine_step",
    "sent_steps",
    "settle",
]
