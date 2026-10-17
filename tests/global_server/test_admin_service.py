"""Unit tests for ``GfsAdminService`` real-time rename push.

Covers the §24.12 ``server_info_updated`` WS broadcast that
:meth:`GfsAdminService.set_branding` fires when (and only when) the
server name actually changes. The integration-level branding tests live
in ``test_admin_endpoints.py``; these drive the service directly with
stub repos + a recording registry so the changed-detection branch is
exercised deterministically.
"""

from __future__ import annotations

from typing import Any

from socialhome.global_server.admin_service import GfsAdminService

# ── Stubs ────────────────────────────────────────────────────────────────────


class _StubAdminRepo:
    """Minimal in-memory config store for the branding path."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._cfg: dict[str, str] = dict(initial or {})
        self.actions: list[str] = []

    async def get_config(self, key: str) -> str | None:
        return self._cfg.get(key)

    async def set_config(self, key: str, value: str) -> None:
        self._cfg[key] = value

    async def get_configs(self, keys: list[str]) -> dict[str, str]:
        return {k: self._cfg[k] for k in keys if k in self._cfg}

    async def log_admin_action(self, **_kw: Any) -> None:
        self.actions.append(_kw.get("action", ""))


class _RecordingRegistry:
    def __init__(self) -> None:
        self.broadcasts: list[dict] = []

    async def broadcast(self, payload: dict) -> int:
        self.broadcasts.append(payload)
        return len(self.broadcasts)


def _make_service(
    *,
    admin_repo: _StubAdminRepo,
    ws_registry: _RecordingRegistry | None,
) -> GfsAdminService:
    return GfsAdminService(
        fed_repo=object(),  # unused on the branding path
        admin_repo=admin_repo,
        federation=object(),
        ws_registry=ws_registry,
    )


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_set_branding_broadcasts_on_name_change():
    admin = _StubAdminRepo({"server_name": "Old Name"})
    reg = _RecordingRegistry()
    svc = _make_service(admin_repo=admin, ws_registry=reg)

    await svc.set_branding(server_name="New Name", admin_ip="1.2.3.4")

    assert reg.broadcasts == [
        {"type": "server_info_updated", "server_name": "New Name"}
    ]
    assert await admin.get_config("server_name") == "New Name"


async def test_set_branding_no_broadcast_when_name_unchanged():
    admin = _StubAdminRepo({"server_name": "Same"})
    reg = _RecordingRegistry()
    svc = _make_service(admin_repo=admin, ws_registry=reg)

    await svc.set_branding(server_name="Same", admin_ip="1.2.3.4")

    assert reg.broadcasts == []


async def test_set_branding_no_broadcast_when_server_name_none():
    admin = _StubAdminRepo({"server_name": "Whatever"})
    reg = _RecordingRegistry()
    svc = _make_service(admin_repo=admin, ws_registry=reg)

    await svc.set_branding(landing_markdown="hello", admin_ip="1.2.3.4")

    assert reg.broadcasts == []


async def test_set_branding_without_registry_is_safe():
    admin = _StubAdminRepo({"server_name": "Old"})
    svc = _make_service(admin_repo=admin, ws_registry=None)

    # Must not raise even though there's no registry to push to.
    await svc.set_branding(server_name="New", admin_ip="1.2.3.4")

    assert await admin.get_config("server_name") == "New"
