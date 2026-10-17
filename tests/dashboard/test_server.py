"""Tests for DashboardServer — the dashboard's own listener lifecycle."""

from __future__ import annotations

import socket

import aiohttp
import pytest
from aiohttp import web

from socialhome.dashboard.server import DashboardServer
from socialhome.domain.household_dashboard import (
    DashboardPortInUseError,
    DashboardSettings,
)

HOST = "127.0.0.1"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _build() -> web.Application:
    async def hello(_request: web.Request) -> web.Response:
        return web.Response(text="hi")

    app = web.Application()
    app.router.add_get("/", hello)
    return app


async def _get(port: int) -> str:
    async with (
        aiohttp.ClientSession() as session,
        session.get(f"http://{HOST}:{port}/") as resp,
    ):
        return await resp.text()


@pytest.fixture
async def server():
    srv = DashboardServer(_build, HOST)
    yield srv
    await srv.stop()


async def test_start_serves_and_stop(server):
    port = _free_port()
    assert server.running_port is None
    await server.start(port)
    assert server.running_port == port
    assert await _get(port) == "hi"
    await server.stop()
    assert server.running_port is None
    with pytest.raises(aiohttp.ClientConnectionError):
        await _get(port)


async def test_stop_is_idempotent(server):
    await server.stop()
    await server.start(_free_port())
    await server.stop()
    await server.stop()
    assert server.running_port is None


async def test_port_in_use(server):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind((HOST, 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        with pytest.raises(DashboardPortInUseError) as exc:
            await server.start(port)
    assert exc.value.status == 409
    assert exc.value.code == "DASHBOARD_PORT_IN_USE"
    assert exc.value.params == {"port": port}
    assert server.running_port is None


async def test_apply_transitions(server):
    p1, p2 = _free_port(), _free_port()
    await server.apply(DashboardSettings(enabled=False, port=p1))
    assert server.running_port is None

    await server.apply(DashboardSettings(enabled=True, port=p1))
    assert server.running_port == p1
    runner = server._runner

    # Same port → no-op (the runner is untouched).
    await server.apply(DashboardSettings(enabled=True, port=p1))
    assert server._runner is runner

    await server.apply(DashboardSettings(enabled=True, port=p2))
    assert server.running_port == p2
    assert await _get(p2) == "hi"
    with pytest.raises(aiohttp.ClientConnectionError):
        await _get(p1)

    await server.apply(DashboardSettings(enabled=False, port=p2))
    assert server.running_port is None


async def test_start_while_running_restarts(server):
    p1, p2 = _free_port(), _free_port()
    await server.start(p1)
    await server.start(p2)
    assert server.running_port == p2
    with pytest.raises(aiohttp.ClientConnectionError):
        await _get(p1)


async def test_failed_port_change_keeps_the_old_port(server):
    p1 = _free_port()
    await server.apply(DashboardSettings(enabled=True, port=p1))
    runner = server._runner
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind((HOST, 0))
        blocker.listen()
        taken = blocker.getsockname()[1]
        with pytest.raises(DashboardPortInUseError) as exc:
            await server.apply(DashboardSettings(enabled=True, port=taken))
    assert exc.value.params == {"port": taken}
    assert server.running_port == p1
    assert server._runner is runner
    assert await _get(p1) == "hi"


async def test_failed_start_while_running_keeps_the_old_port(server):
    p1 = _free_port()
    await server.start(p1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind((HOST, 0))
        blocker.listen()
        with pytest.raises(DashboardPortInUseError):
            await server.start(blocker.getsockname()[1])
    assert server.running_port == p1
    assert await _get(p1) == "hi"


async def test_start_on_the_running_port_restarts(server):
    p1 = _free_port()
    await server.start(p1)
    runner = server._runner
    await server.start(p1)
    assert server.running_port == p1
    assert server._runner is not runner
    assert await _get(p1) == "hi"


class RecordingAdvertiser:
    """Records the advertise / withdraw calls the server makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    async def advertise(self, port: int) -> None:
        self.calls.append(("advertise", port))

    async def withdraw(self) -> None:
        self.calls.append(("withdraw", None))


async def test_apply_drives_the_advertiser():
    adv = RecordingAdvertiser()
    srv = DashboardServer(_build, HOST, advertiser=adv)
    p1, p2 = _free_port(), _free_port()
    try:
        await srv.apply(DashboardSettings(enabled=True, port=p1))
        await srv.apply(DashboardSettings(enabled=True, port=p1))  # no-op
        await srv.apply(DashboardSettings(enabled=True, port=p2))  # rebind
        await srv.apply(DashboardSettings(enabled=False, port=p2))
        await srv.apply(DashboardSettings(enabled=False, port=p2))  # already off
    finally:
        await srv.stop()
    assert adv.calls == [
        ("advertise", p1),
        ("advertise", p2),
        ("withdraw", None),
    ]


async def test_failed_bind_does_not_touch_the_advertiser():
    adv = RecordingAdvertiser()
    srv = DashboardServer(_build, HOST, advertiser=adv)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind((HOST, 0))
            blocker.listen()
            with pytest.raises(DashboardPortInUseError):
                await srv.start(blocker.getsockname()[1])
    finally:
        await srv.stop()
    assert adv.calls == []


async def test_restart_on_same_port_reannounces():
    adv = RecordingAdvertiser()
    srv = DashboardServer(_build, HOST, advertiser=adv)
    p1 = _free_port()
    try:
        await srv.start(p1)
        await srv.start(p1)
    finally:
        await srv.stop()
    assert adv.calls == [
        ("advertise", p1),
        ("withdraw", None),
        ("advertise", p1),
        ("withdraw", None),
    ]
