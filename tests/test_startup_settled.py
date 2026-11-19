"""An app handed out by ``aiohttp_client`` has finished its startup sends.

``on_startup`` starts the peer-facing startup steps (capabilities
announcement, the re-sync request after an upgrade) in a background task
(``StartupAnnouncer``), so the port opens at once. A test that then seeds
confirmed peers and records ``FederationService.send_*`` raced that task:
on a loaded runner it listed the freshly seeded peers and its sends landed
in the test's recording (a stray ``INSTANCE_RESYNC_REQUEST`` ahead of the
event under test, a ``send_event`` where none was expected). The
``aiohttp_client`` fixture in ``tests/conftest.py`` waits for the task, so
no test has to remember to.
"""

from __future__ import annotations

import asyncio
from types import MappingProxyType

from socialhome import app as app_module
from socialhome.app import create_app
from socialhome.app_keys import db_key, startup_announcer_key
from socialhome.config import Config
from socialhome.federation.federation_service import FederationService
from socialhome.infrastructure.startup_announcer import StartupAnnouncer


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "settled.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


def _late(monkeypatch) -> None:
    """The announcer task gets to run only after a while — a loaded runner."""
    real = StartupAnnouncer._run

    async def _run(self) -> None:
        await asyncio.sleep(0.2)
        await real(self)

    monkeypatch.setattr(StartupAnnouncer, "_run", _run)


async def test_the_client_fixture_returns_after_the_startup_steps(
    aiohttp_client, tmp_dir, monkeypatch
):
    ran: list[str] = []

    async def _resync(**_kw) -> int:
        ran.append("resync")
        return 0

    monkeypatch.setattr(app_module, "request_capability_resync_if_upgraded", _resync)
    _late(monkeypatch)
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    assert ran == ["resync"]
    assert app[startup_announcer_key]._task.done()


async def test_a_late_startup_step_never_sends_to_peers_seeded_by_the_test(
    aiohttp_client, tmp_dir, monkeypatch
):
    """The shape of the flake: peers seeded right after the client is up,
    every send recorded. Startup may send nothing to them."""
    sent: list[tuple[str, str]] = []

    async def _record(_self, *, to_instance_id, event_type, **_kw):
        sent.append((to_instance_id, str(event_type)))

    monkeypatch.setattr(FederationService, "send_event", _record)
    _late(monkeypatch)
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    await app[db_key].enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version)"
        " VALUES('peer-x','peer-x',?,'k1','k2','https://peer-x/wh','wh-x',"
        " 'confirmed','manual',80)",
        ("00" * 32,),
    )
    await asyncio.sleep(0.4)
    assert sent == []
