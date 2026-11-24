"""Tests for socialhome.global_server.fan_out_workers — the background
workers that push accepted channel items (moved out of the retired space-
keyed member publish, v_96)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from socialhome.global_server import fan_out_workers as fw
from socialhome.global_server.fan_out_workers import GfsFanOutWorkers

AUDIENCE = ("channel", "c" * 32)


class _Relay:
    """Records every fan-out; *gate* (when set) holds each call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.gate: asyncio.Event | None = None
        self.fail = False

    async def fan_out_relay(self, targets, *, queue_ok, frame, audience, skip):
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(
            {
                "targets": list(targets),
                "queue_ok": set(queue_ok),
                "frame": frame,
                "audience": audience,
                "skip": skip,
            }
        )
        return len(targets)


def _resolve(targets: list[str], queue_ok: set[str] | None = None):
    async def _r() -> tuple[list[str], set[str]]:
        return list(targets), set(queue_ok or ())

    return _r


@pytest.fixture
async def workers():
    relay = _Relay()
    svc = GfsFanOutWorkers(relay=relay)  # type: ignore[arg-type]
    await svc.start()
    yield svc, relay
    await svc.stop()


async def test_an_item_reaches_every_target_but_its_publisher(workers):
    svc, relay = workers
    assert svc.submit(
        "ch:1",
        publisher_id="pub",
        frame={"payload": "x"},
        resolve=_resolve(["a", "pub", "b"], {"a"}),
        audience=AUDIENCE,
    )
    await svc.wait_idle()
    assert relay.calls == [
        {
            "targets": ["a", "b"],
            "queue_ok": {"a"},
            "frame": {"payload": "x"},
            "audience": AUDIENCE,
            "skip": "pub",
        }
    ]


async def test_nobody_to_reach_makes_no_relay_call(workers):
    svc, relay = workers
    assert svc.submit(
        "ch:1",
        publisher_id="pub",
        frame={},
        resolve=_resolve(["pub"]),
        audience=AUDIENCE,
    )
    await svc.wait_idle()
    assert relay.calls == []


async def test_a_key_s_items_go_out_in_submit_order(workers):
    svc, relay = workers
    for i in range(6):
        assert svc.submit(
            "ch:1",
            publisher_id="",
            frame={"payload": f"ct-{i}"},
            resolve=_resolve(["a"]),
            audience=AUDIENCE,
        )
    await svc.wait_idle()
    assert [c["frame"]["payload"] for c in relay.calls] == [f"ct-{i}" for i in range(6)]


def test_one_key_is_pinned_to_one_worker():
    assert GfsFanOutWorkers._worker_index("ch:x") == GfsFanOutWorkers._worker_index(
        "ch:x"
    )
    assert 0 <= GfsFanOutWorkers._worker_index("ch:y") < fw.FAN_OUT_WORKERS


async def test_nothing_is_taken_before_start_or_after_stop():
    relay = _Relay()
    svc = GfsFanOutWorkers(relay=relay)  # type: ignore[arg-type]
    args = {
        "publisher_id": "",
        "frame": {},
        "resolve": _resolve(["a"]),
        "audience": AUDIENCE,
    }
    assert svc.submit("ch:1", **args) is False
    await svc.start()
    await svc.start()  # idempotent
    assert svc.submit("ch:1", **args) is True
    await svc.stop()
    assert svc.submit("ch:1", **args) is False


async def test_one_busy_key_cannot_refuse_another(workers):
    svc, relay = workers
    relay.gate = asyncio.Event()
    args = {
        "publisher_id": "",
        "frame": {},
        "resolve": _resolve(["a"]),
        "audience": AUDIENCE,
    }
    accepted = [
        svc.submit("ch:busy", **args) for _ in range(fw.FAN_OUT_BACKLOG_PER_KEY + 3)
    ]
    assert accepted.count(True) == fw.FAN_OUT_BACKLOG_PER_KEY
    assert accepted[-1] is False
    assert svc.submit("ch:other", **args) is True
    relay.gate.set()
    await svc.wait_idle()


async def test_a_full_worker_queue_refuses(monkeypatch):
    monkeypatch.setattr(fw, "FAN_OUT_BACKLOG", 1)
    relay = _Relay()
    relay.gate = asyncio.Event()
    svc = GfsFanOutWorkers(relay=relay)  # type: ignore[arg-type]
    await svc.start()
    args = {
        "publisher_id": "",
        "frame": {},
        "resolve": _resolve(["a"]),
        "audience": AUDIENCE,
    }
    results = [svc.submit("ch:1", **args) for _ in range(4)]
    assert False in results
    relay.gate.set()
    await svc.stop()


async def test_a_failing_fan_out_is_logged_and_the_worker_goes_on(workers, caplog):
    svc, relay = workers
    relay.fail = True
    args = {
        "publisher_id": "",
        "frame": {},
        "resolve": _resolve(["a"]),
        "audience": AUDIENCE,
    }
    with caplog.at_level(logging.ERROR, logger="socialhome.global_server"):
        assert svc.submit("ch:1", **args)
        await svc.wait_idle()
    assert "fan-out failed" in caplog.text
    relay.fail = False
    assert svc.submit("ch:1", **args)
    await svc.wait_idle()
    assert len(relay.calls) == 1


async def test_stop_drains_accepted_items_before_returning():
    relay = _Relay()
    svc = GfsFanOutWorkers(relay=relay)  # type: ignore[arg-type]
    await svc.start()
    for i in range(4):
        svc.submit(
            "ch:1",
            publisher_id="",
            frame={"i": i},
            resolve=_resolve(["a"]),
            audience=AUDIENCE,
        )
    await svc.stop()
    assert len(relay.calls) == 4


async def test_stop_warns_with_the_count_it_could_not_drain(monkeypatch, caplog):
    monkeypatch.setattr(fw, "FAN_OUT_STOP_DRAIN_S", 0.05)
    relay = _Relay()
    relay.gate = asyncio.Event()
    svc = GfsFanOutWorkers(relay=relay)  # type: ignore[arg-type]
    await svc.start()
    for i in range(3):
        svc.submit(
            "ch:1",
            publisher_id="",
            frame={"i": i},
            resolve=_resolve(["a"]),
            audience=AUDIENCE,
        )
    with caplog.at_level(logging.WARNING, logger="socialhome.global_server"):
        stopping = asyncio.create_task(svc.stop())
        await asyncio.sleep(0.2)
        relay.gate.set()
        await stopping
    assert "not fanned out before shutdown" in caplog.text
