"""Tests for POST /api/spaces/{id}/sync ("Sync now") and the ``sync``
status of GET /api/spaces/{id}."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key as _db_key,
)
from socialhome.app_keys import (
    event_bus_key,
    federation_repo_key,
    space_sync_status_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.domain.events import SpaceSyncComplete
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.federation_service import FederationService
from socialhome.federation.sync.space.scheduler import SpaceSyncScheduler
from socialhome.routes.spaces import SYNC_NOW_RATE_LIMIT


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(tmp_dir):
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        db = app[_db_key]
        row = await db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'",
        )
        pk = bytes.fromhex(row["identity_public_key"])

        class _KP:
            public_key = pk

        kp = _KP()
        admin_uid = derive_user_id(kp.public_key, "pascal")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
            ("pascal", admin_uid, "Pascal"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-admin", admin_uid, "test", sha256_token_hash("admin-token")),
        )
        bob_uid = derive_user_id(kp.public_key, "bob")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
            ("bob", bob_uid, "Bob"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
            ("tid-bob", bob_uid, "test", sha256_token_hash("bob-token")),
        )
        tc._admin_token = "admin-token"
        tc._admin_uid = admin_uid
        tc._bob_token = "bob-token"
        tc._bob_uid = bob_uid
        yield tc


async def _create_space(client, name="Family"):
    r = await client.post(
        "/api/spaces",
        json={"name": name},
        headers=_auth(client._admin_token),
    )
    assert r.status == 201
    return (await r.json())["id"]


async def test_sync_trigger_enqueues_for_confirmed_peers(client):
    space_id = await _create_space(client)

    # Simulate peer membership + confirmed pairing.
    app = client.server.app
    fed_repo = app[federation_repo_key]

    from socialhome.domain.federation import (
        InstanceSource,
        PairingStatus,
        RemoteInstance,
    )

    await fed_repo.save_instance(
        RemoteInstance(
            id="peer-a",
            display_name="Peer A",
            remote_identity_pk="aa" * 32,
            key_self_to_remote="enc",
            key_remote_to_self="enc",
            remote_inbox_url="https://peer/wh",
            local_inbox_id="wh-peer-a",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        )
    )
    # Mark instance as a member of this space.
    await app[_db_key].enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
        (space_id, "peer-a"),
    )

    resp = await client.post(
        f"/api/spaces/{space_id}/sync",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 202
    body = await resp.json()
    assert "peer-a" in body["targets"]


async def test_sync_trigger_skips_unconfirmed_peers(client):
    space_id = await _create_space(client)
    app = client.server.app
    fed_repo = app[federation_repo_key]

    from socialhome.domain.federation import (
        InstanceSource,
        PairingStatus,
        RemoteInstance,
    )

    await fed_repo.save_instance(
        RemoteInstance(
            id="peer-new",
            display_name="Peer New",
            remote_identity_pk="bb" * 32,
            key_self_to_remote="enc",
            key_remote_to_self="enc",
            remote_inbox_url="https://peer/wh",
            local_inbox_id="wh-peer-new",
            status=PairingStatus.PENDING_SENT,
            source=InstanceSource.MANUAL,
        )
    )
    await app[_db_key].enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
        (space_id, "peer-new"),
    )

    resp = await client.post(
        f"/api/spaces/{space_id}/sync",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 202
    body = await resp.json()
    assert body["targets"] == []


async def test_sync_trigger_forbidden_for_a_non_member(client):
    """Bob holds no seat in the space: Sync now is any member's, never a
    stranger's."""
    space_id = await _create_space(client)
    resp = await client.post(
        f"/api/spaces/{space_id}/sync",
        headers=_auth(client._bob_token),
    )
    assert resp.status in (401, 403)


async def test_sync_trigger_unknown_space_returns_404(client):
    resp = await client.post(
        "/api/spaces/does-not-exist/sync",
        headers=_auth(client._admin_token),
    )
    assert resp.status == 404


# ── Sync status + "Sync now" for a space hosted elsewhere ──────────────────


async def _remote_space(client, *, role: str = "member", user: str = "admin") -> str:
    """A stub of a space hosted on ``host-x`` with a seat for one of ours."""
    db = client.server.app[_db_key]
    uid = client._admin_uid if user == "admin" else client._bob_uid
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type) VALUES('sp-r','Remote','host-x','anna',?,"
        " 'private')",
        ("ab" * 32,),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES('sp-r', ?, ?)",
        (uid, role),
    )
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES('sp-r','host-x')"
    )
    return "sp-r"


async def test_a_space_hosted_here_reads_up_to_date(client):
    space_id = await _create_space(client)
    r = await client.get(f"/api/spaces/{space_id}", headers=_auth(client._admin_token))
    assert r.status == 200
    assert (await r.json())["sync"] == {
        "state": "up_to_date",
        "last_ok_at": None,
        "waiting_to_send": 0,
        "may_be_missing": False,
        "unsent_posts": [],
    }


async def test_a_members_space_reads_never_then_up_to_date_after_the_host_answers(
    client,
):
    space_id = await _remote_space(client)
    r = await client.get(f"/api/spaces/{space_id}", headers=_auth(client._admin_token))
    assert (await r.json())["sync"]["state"] == "never"
    # The host's quiet sentinel (a clean, empty session).
    await client.server.app[event_bus_key].publish(
        SpaceSyncComplete(space_id=space_id, from_instance="host-x", clean=True)
    )
    # Recorded in a task of its own (never in the publisher's).
    await client.server.app[space_sync_status_key].settle()
    r = await client.get(f"/api/spaces/{space_id}", headers=_auth(client._admin_token))
    sync = (await r.json())["sync"]
    assert sync["state"] == "up_to_date"
    assert sync["last_ok_at"]
    # Persisted on the host's seat row (survives a restart).
    row = await client.server.app[_db_key].fetchone(
        "SELECT sync_result, sync_ok_at FROM space_instances"
        " WHERE space_id=? AND instance_id='host-x'",
        (space_id,),
    )
    assert row["sync_result"] == "ok"
    assert row["sync_ok_at"] == sync["last_ok_at"]


async def test_a_follower_gets_no_sync_status(client):
    space_id = await _remote_space(client, role="subscriber")
    r = await client.get(f"/api/spaces/{space_id}", headers=_auth(client._admin_token))
    assert r.status == 200
    assert (await r.json())["sync"] is None


async def test_sync_now_asks_a_mesh_only_host_for_an_incremental_stream(client):
    space_id = await _remote_space(client)
    fed = FederationService
    with patch.object(
        fed, "begin_mesh_catchup_sync", AsyncMock(return_value=True)
    ) as begin:
        resp = await client.post(
            f"/api/spaces/{space_id}/sync", headers=_auth(client._admin_token)
        )
    assert resp.status == 202
    assert (await resp.json())["targets"] == ["host-x"]
    begin.assert_awaited_once()
    assert begin.await_args.kwargs["host_instance_id"] == "host-x"
    assert begin.await_args.kwargs["sync_mode"] == "incremental"


async def test_sync_now_asks_a_paired_host_incrementally(client):
    space_id = await _remote_space(client)
    app = client.server.app
    await app[federation_repo_key].save_instance(
        RemoteInstance(
            id="host-x",
            display_name="Host",
            remote_identity_pk="cc" * 32,
            key_self_to_remote="enc",
            key_remote_to_self="enc",
            remote_inbox_url="https://host/wh",
            local_inbox_id="wh-host",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        )
    )
    scheduler = SpaceSyncScheduler
    with patch.object(scheduler, "enqueue_sync_for_space", AsyncMock()) as enqueue:
        resp = await client.post(
            f"/api/spaces/{space_id}/sync", headers=_auth(client._admin_token)
        )
    assert resp.status == 202
    assert (await resp.json())["targets"] == ["host-x"]
    enqueue.assert_awaited_once_with(
        space_id=space_id,
        peer_instance_id="host-x",
        sync_mode="incremental",
        newest_page=True,  # the host's newest sync page at once
    )


async def test_sync_now_never_asks_a_paired_co_member(client):
    """Only the households acting as the host are asked: a paired
    co-member's BEGIN budget is not spent on our Sync now."""
    space_id = await _remote_space(client)
    app = client.server.app
    await app[federation_repo_key].save_instance(
        RemoteInstance(
            id="co-member",
            display_name="Co-member",
            remote_identity_pk="dd" * 32,
            key_self_to_remote="enc",
            key_remote_to_self="enc",
            remote_inbox_url="https://co/wh",
            local_inbox_id="wh-co",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        )
    )
    await app[_db_key].enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?, 'co-member')",
        (space_id,),
    )
    with (
        patch.object(
            FederationService, "begin_mesh_catchup_sync", AsyncMock(return_value=True)
        ),
        patch.object(
            SpaceSyncScheduler, "enqueue_sync_for_space", AsyncMock()
        ) as enqueue,
    ):
        resp = await client.post(
            f"/api/spaces/{space_id}/sync", headers=_auth(client._admin_token)
        )
    assert resp.status == 202
    assert (await resp.json())["targets"] == ["host-x"]
    enqueue.assert_not_awaited()


async def test_any_member_may_ask_for_sync_now(client):
    space_id = await _remote_space(client, user="bob")
    fed = FederationService
    with patch.object(fed, "begin_mesh_catchup_sync", AsyncMock(return_value=True)):
        resp = await client.post(
            f"/api/spaces/{space_id}/sync", headers=_auth(client._bob_token)
        )
    assert resp.status == 202


async def test_a_follower_may_not_ask_for_sync_now(client):
    space_id = await _remote_space(client, role="subscriber")
    resp = await client.post(
        f"/api/spaces/{space_id}/sync", headers=_auth(client._admin_token)
    )
    assert resp.status == 403


async def test_sync_now_is_rate_limited_per_space(client):
    """The hosts allow 5 BEGINs an hour per household and space; the
    periodic rounds take 2 of them, so "Sync now" gets the other 3."""
    space_id = await _remote_space(client)
    fed = FederationService
    with patch.object(
        fed, "begin_mesh_catchup_sync", AsyncMock(return_value=True)
    ) as begin:
        for _ in range(SYNC_NOW_RATE_LIMIT):
            resp = await client.post(
                f"/api/spaces/{space_id}/sync", headers=_auth(client._admin_token)
            )
            assert resp.status == 202
        resp = await client.post(
            f"/api/spaces/{space_id}/sync", headers=_auth(client._admin_token)
        )
    assert resp.status == 429
    error = (await resp.json())["error"]
    assert error["code"] == "RATE_LIMITED"
    # When the next check is allowed — the SPA's "You can check again in
    # {m} min" (header and body say the same).
    assert 0 < error["retry_after_s"] <= 3600
    assert resp.headers["Retry-After"] == str(error["retry_after_s"])
    assert begin.await_count == SYNC_NOW_RATE_LIMIT
    # A reload knows it at once: the detail says when the next is allowed.
    r = await client.get(f"/api/spaces/{space_id}", headers=_auth(client._admin_token))
    next_at = datetime.fromisoformat((await r.json())["sync"]["next_check_at"])
    left = (next_at - datetime.now(UTC)).total_seconds()
    assert 0 < left <= 3600


async def test_no_next_check_at_while_check_now_is_allowed(client):
    space_id = await _remote_space(client)
    r = await client.get(f"/api/spaces/{space_id}", headers=_auth(client._admin_token))
    assert "next_check_at" not in (await r.json())["sync"]


async def _two_failed_rounds(client, space_id: str) -> None:
    """The host answered ``ok`` an hour ago, then two unclean rounds — while
    this household ran (it started 2 h ago)."""
    status = client.server.app[space_sync_status_key]
    status._started_at = datetime.now(UTC) - timedelta(hours=2)
    db = client.server.app[_db_key]
    await db.enqueue(
        "UPDATE space_instances SET sync_result='unclean',"
        " sync_ok_at=strftime('%Y-%m-%dT%H:%M:%S+00:00','now','-65 minutes'),"
        " sync_result_at=strftime('%Y-%m-%dT%H:%M:%S+00:00','now','-5 minutes')"
        " WHERE space_id=? AND instance_id='host-x'",
        (space_id,),
    )


async def test_two_failed_rounds_read_may_be_missing(client):
    space_id = await _remote_space(client)
    r = await client.get(f"/api/spaces/{space_id}", headers=_auth(client._admin_token))
    assert (await r.json())["sync"]["may_be_missing"] is False
    await _two_failed_rounds(client, space_id)
    r = await client.get(f"/api/spaces/{space_id}", headers=_auth(client._admin_token))
    sync = (await r.json())["sync"]
    assert sync["state"] == "behind"
    assert sync["may_be_missing"] is True


async def test_the_spaces_list_marks_a_space_whose_posts_may_be_missing(client):
    space_id = await _remote_space(client)
    here = await _create_space(client)

    async def _rows() -> dict:
        r = await client.get("/api/spaces", headers=_auth(client._admin_token))
        assert r.status == 200
        return {row["id"]: row for row in await r.json()}

    rows = await _rows()
    assert rows[space_id]["sync_may_be_missing"] is False
    assert rows[here]["sync_may_be_missing"] is False
    await _two_failed_rounds(client, space_id)
    rows = await _rows()
    assert rows[space_id]["sync_may_be_missing"] is True
    assert rows[here]["sync_may_be_missing"] is False


async def test_a_follower_row_never_carries_the_warning(client):
    space_id = await _remote_space(client, role="subscriber")
    await _two_failed_rounds(client, space_id)
    r = await client.get("/api/spaces", headers=_auth(client._admin_token))
    (row,) = [x for x in await r.json() if x["id"] == space_id]
    assert row["sync_may_be_missing"] is False
