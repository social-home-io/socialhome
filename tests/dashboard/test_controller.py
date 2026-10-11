"""Tests for DashboardController — settings changes the listener follows."""

from __future__ import annotations

import asyncio
import socket

import pytest

from socialhome.dashboard.controller import (
    DashboardController,
    address_hosts,
    local_hostnames,
)
from socialhome.domain.household_dashboard import (
    DashboardNotRunningError,
    DashboardPortInUseError,
    DashboardSettings,
)

from socialhome.services.dashboard_settings_service import DashboardSettingsService

from .conftest import PASSWORD, FakeConfigRepo


class FakeServer:
    """Records ``apply`` calls; can fail or block on demand."""

    def __init__(self) -> None:
        self.running_port: int | None = None
        self.applied: list[DashboardSettings] = []
        self.fail: BaseException | None = None
        self.gate: asyncio.Event | None = None
        self.stopped = False

    async def apply(self, settings: DashboardSettings) -> None:
        self.applied.append(settings)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        self.running_port = settings.port if settings.enabled else None

    async def stop(self) -> None:
        self.stopped = True
        self.running_port = None


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
async def controller(settings_service, server) -> DashboardController:
    await settings_service.set_password(actor_is_admin=True, password=PASSWORD)
    return DashboardController(
        settings_service, server, local_hostnames=frozenset({"box"})
    )


# ── helpers ─────────────────────────────────────────────────────────────


def test_local_hostnames_include_the_short_name(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "Box.Example.LAN.")
    assert local_hostnames() == frozenset({"box.example.lan", "box"})
    monkeypatch.setattr(socket, "gethostname", lambda: "box")
    assert local_hostnames() == frozenset({"box"})
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    assert local_hostnames() == frozenset()


def test_address_hosts():
    assert address_hosts(DashboardSettings()) == frozenset()
    assert address_hosts(
        DashboardSettings(address="https://dash.example.com:8443")
    ) == frozenset({"dash.example.com"})


def test_controller_exposes_settings_and_the_running_port(settings_service, server):
    ctl = DashboardController(settings_service, server, local_hostnames=frozenset())
    assert ctl.settings is settings_service
    assert ctl.running_port is None
    server.running_port = 8100
    assert ctl.running_port == 8100


# ── update ──────────────────────────────────────────────────────────────


async def test_listener_edit_is_applied(controller, server):
    updated = await controller.update(
        actor_is_admin=True, patch={"enabled": True, "port": 9100}
    )
    assert server.applied == [updated]
    assert server.running_port == 9100


async def test_other_edits_do_not_touch_the_listener(controller, server):
    updated = await controller.update(actor_is_admin=True, patch={"locale": "de"})
    assert updated.locale == "de"
    assert server.applied == []


async def test_other_edits_save_while_the_port_is_taken(
    controller, server, settings_service
):
    """An enabled dashboard whose port was taken at startup (not running)
    still accepts non-listener edits."""
    await settings_service.update(
        actor_is_admin=True, patch={"enabled": True, "port": 9100}
    )
    server.fail = DashboardPortInUseError(9100)
    updated = await controller.update(actor_is_admin=True, patch={"locale": "de"})
    assert updated.locale == "de"
    assert (await settings_service.get()).locale == "de"
    assert server.running_port is None


@pytest.mark.parametrize(
    "error", [DashboardPortInUseError(9100), RuntimeError("boom"), OSError("x")]
)
async def test_failed_apply_restores_and_reraises(
    controller, server, settings_service, error
):
    before = await settings_service.get()
    server.fail = error
    with pytest.raises(type(error)):
        await controller.update(
            actor_is_admin=True,
            patch={"enabled": True, "port": 9100, "locale": "fr"},
        )
    assert await settings_service.get() == before


async def test_restore_keeps_a_fixed_port_out_of_storage(server):
    repo = FakeConfigRepo()
    svc = DashboardSettingsService(repo, fixed_port=8200, main_port=8080)
    await svc.set_password(actor_is_admin=True, password=PASSWORD)
    controller = DashboardController(svc, server, local_hostnames=frozenset())
    server.fail = DashboardPortInUseError(8200)
    with pytest.raises(DashboardPortInUseError):
        await controller.update(actor_is_admin=True, patch={"enabled": True})
    assert "8200" not in repo.rows.get("dashboard.settings", "")
    assert (await svc.get()).enabled is False


async def test_non_admin_is_refused_without_applying(controller, server):
    with pytest.raises(PermissionError):
        await controller.update(actor_is_admin=False, patch={"enabled": True})
    assert server.applied == []


async def test_concurrent_updates_do_not_interleave(
    controller, server, settings_service
):
    server.gate = asyncio.Event()
    first = asyncio.create_task(
        controller.update(actor_is_admin=True, patch={"enabled": True, "port": 9100})
    )
    while not server.applied:
        await asyncio.sleep(0)
    second = asyncio.create_task(
        controller.update(actor_is_admin=True, patch={"locale": "de"})
    )
    for _ in range(20):
        await asyncio.sleep(0)
    # The second edit waits for the first one's apply (and any rollback).
    assert (await settings_service.get()).locale == "en"
    server.fail = DashboardPortInUseError(9100)
    server.gate.set()
    with pytest.raises(DashboardPortInUseError):
        await first
    await second
    stored = await settings_service.get()
    # The rollback of the first edit did not undo the second one.
    assert stored.locale == "de"
    assert stored.enabled is False


# ── startup / shutdown ──────────────────────────────────────────────────


async def test_start_applies_an_enabled_dashboard(controller, server, settings_service):
    await settings_service.update(
        actor_is_admin=True, patch={"enabled": True, "port": 9100}
    )
    await controller.start()
    assert server.running_port == 9100


async def test_start_leaves_a_disabled_dashboard_off(controller, server):
    await controller.start()
    assert server.applied == []


async def test_start_survives_a_taken_port(
    controller, server, settings_service, caplog
):
    await settings_service.update(
        actor_is_admin=True, patch={"enabled": True, "port": 9100}
    )
    server.fail = DashboardPortInUseError(9100)
    await controller.start()
    assert server.running_port is None
    assert "9100" in caplog.text


async def test_stop(controller, server):
    await controller.stop()
    assert server.stopped


# ── setup token ─────────────────────────────────────────────────────────


async def test_setup_token_needs_a_running_dashboard(controller):
    with pytest.raises(DashboardNotRunningError):
        await controller.issue_setup_token(actor_is_admin=True)


async def test_setup_token_admin_check_comes_first(controller):
    with pytest.raises(PermissionError):
        await controller.issue_setup_token(actor_is_admin=False)


async def test_setup_token_while_running(controller, settings_service):
    await controller.update(actor_is_admin=True, patch={"enabled": True, "port": 9100})
    token = await controller.issue_setup_token(actor_is_admin=True, now=1000.0)
    assert await settings_service.consume_setup_token(token, now=1001.0)


# ── URL host check ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("http://homeassistant.local:8100", True),
        ("http://192.168.1.5:8100", True),
        ("http://[fd00::1]:8100", True),
        ("http://box:8100", True),
        ("http://BOX:8100", True),
        ("http://homeassistant:8100", True),
        ("http://otherbox:8100", False),
        ("https://dash.example.com", False),
    ],
)
async def test_url_host_allowed(controller, url, allowed):
    assert await controller.url_host_allowed(url) is allowed


async def test_url_host_allowed_includes_the_address(controller):
    await controller.update(
        actor_is_admin=True, patch={"address": "https://dash.example.com"}
    )
    assert await controller.url_host_allowed("https://dash.example.com") is True
