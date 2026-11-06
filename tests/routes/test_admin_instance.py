"""Tests for ``PATCH /api/admin/instance`` (rename the household).

Admin-only endpoint that persists ``instance_identity.display_name`` and
re-broadcasts it to confirmed peers via INSTANCE_CAPABILITIES_UPDATED.
"""

from __future__ import annotations

import asyncio

import pytest

from socialhome.app_keys import (
    capabilities_outbound_key,
    federation_repo_key,
    gfs_connection_service_key,
)
from socialhome.services import recovery_reconnect_service

from .conftest import _auth


class _RecordingOutbound:
    """Stub ``capabilities_outbound`` recording ``publish()`` calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def publish(self) -> int:
        self.calls += 1
        return 0


class _RecordingGfs:
    """Stub ``gfs_connection_service`` recording the rename push.

    ``result`` is what ``update_display_name_to_all`` returns — the real
    method is best-effort and returns the success count (0 when every
    paired GFS is unreachable), never raising.
    """

    def __init__(self, *, result: int = 0) -> None:
        self.names: list[str] = []
        self._result = result

    async def update_display_name_to_all(self, display_name: str) -> int:
        self.names.append(display_name)
        return self._result


async def test_requires_admin(client):
    """A non-admin token gets 403."""
    db = client._db
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "Casa Vizeli"},
    )
    assert resp.status == 403


async def test_renames_and_persists(client):
    """Admin PATCH returns the new name and persists it to the DB."""
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "Casa Vizeli"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"display_name": "Casa Vizeli"}
    identity = await client.app[federation_repo_key].get_local_identity()
    assert identity["display_name"] == "Casa Vizeli"


async def test_triggers_broadcast(make_client):
    """A successful rename re-broadcasts capabilities exactly once."""
    stub = _RecordingOutbound()
    client = await make_client({capabilities_outbound_key: stub})
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "Casa Vizeli"},
    )
    assert resp.status == 200
    assert stub.calls == 1


async def test_pushes_rename_to_gfs(make_client):
    """A successful rename pushes the new name to every paired GFS."""
    stub = _RecordingGfs()
    client = await make_client({gfs_connection_service_key: stub})
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "Casa Vizeli"},
    )
    assert resp.status == 200
    assert stub.names == ["Casa Vizeli"]


async def test_rename_succeeds_when_no_gfs_reachable(make_client):
    """The GFS push is best-effort — when every paired GFS is down
    (result 0), the rename still persists and returns 200."""
    client = await make_client({gfs_connection_service_key: _RecordingGfs(result=0)})
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "Casa Vizeli"},
    )
    assert resp.status == 200
    identity = await client.app[federation_repo_key].get_local_identity()
    assert identity["display_name"] == "Casa Vizeli"


async def test_rejects_blank(client):
    """A blank/whitespace name is rejected with 422."""
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "   "},
    )
    assert resp.status == 422


async def test_rejects_too_long(client):
    """A name longer than 80 chars is rejected with 422."""
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "x" * 81},
    )
    assert resp.status == 422


@pytest.fixture
def slow_first_startup_step(monkeypatch):
    """Make the startup announcer's first step outlast the app's startup,
    as on a busy CI runner — its capabilities announcement then runs after
    the test doubles were swapped in."""
    original = recovery_reconnect_service.RecoveryReconnectService.maybe_reconnect

    async def slow(self, *args, **kwargs):
        await asyncio.sleep(0.3)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(
        recovery_reconnect_service.RecoveryReconnectService, "maybe_reconnect", slow
    )


async def test_the_startup_announcement_keeps_the_outbound_it_was_wired_with(
    make_client, slow_first_startup_step
):
    """The background startup announcement publishes through the outbound
    the startup wired — never one that replaced the app key later — so a
    rename is still counted exactly once however late the announcement
    runs (it made ``test_triggers_broadcast`` read 2 on a slow runner)."""
    stub = _RecordingOutbound()
    client = await make_client({capabilities_outbound_key: stub})
    await asyncio.sleep(0.6)  # the announcer has finished
    resp = await client.patch(
        "/api/admin/instance",
        headers=_auth(client._tok),
        json={"display_name": "Casa Vizeli"},
    )
    assert resp.status == 200
    assert stub.calls == 1
