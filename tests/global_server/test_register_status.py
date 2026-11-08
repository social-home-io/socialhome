"""``/gfs/register`` never lifts an operator-set status (#1326 review):
a banned household that re-registers stays banned (and is told nothing
different), only an operator unbans; pending / active follow the old rules."""

from __future__ import annotations

import asyncio
import secrets

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import derive_instance_id, ed25519_public_key
from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_admin_service_key,
    gfs_envelope_queue_repo_key,
    gfs_envelope_relay_key,
    gfs_fed_repo_key,
    gfs_token_service_key,
)
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.repositories import SqliteGfsFederationRepo
from socialhome.global_server.server import create_gfs_app
from tests.global_server.test_mailboxes import SEALED, _cfg

PK = ed25519_public_key(b"\x21" * 32).hex()
HOME = derive_instance_id(bytes.fromhex(PK))


@pytest.fixture
async def gfs(tmp_dir):
    app = create_gfs_app(_cfg(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        yield tc


async def _auto_accept(tc, on: bool) -> None:
    await tc._app[gfs_admin_repo_key].set_config(
        "auto_accept_clients", "1" if on else "0"
    )


async def _register(tc, *, display_name: str = "Home"):
    token, _ = await tc._app[gfs_token_service_key].generate(
        "t-" + secrets.token_hex(4)
    )
    resp = await tc.post(
        "/gfs/register",
        json={
            "token": token,
            "instance_id": HOME,
            "public_key": PK,
            "display_name": display_name,
        },
    )
    return resp.status, await resp.json()


async def _status(tc) -> str:
    row = await tc._app[gfs_fed_repo_key].get_instance(HOME)
    assert row is not None
    return row.status


async def _settle(app) -> None:
    relay = app[gfs_envelope_relay_key]
    for _ in range(300):
        if relay.in_flight == 0:
            return
        await asyncio.sleep(0.01)


@pytest.mark.security
async def test_a_banned_household_that_re_registers_stays_banned(gfs):
    await _auto_accept(gfs, True)
    first = await _register(gfs)
    assert first == (200, {"status": "registered", "instance_id": HOME})
    admin = gfs._app[gfs_admin_service_key]
    await admin.ban_client(HOME, reason="abuse", admin_ip="127.0.0.1")
    again = await _register(gfs, display_name="Renamed")
    # The same answer an unbanned household gets: registration is no ban oracle.
    assert again == first
    assert await _status(gfs) == "banned"
    row = await gfs._app[gfs_fed_repo_key].get_instance(HOME)
    assert row.display_name == "Home"  # a banned row is not touched at all
    await gfs.post("/gfs/envelope", json={"to_instance": HOME, "sealed": SEALED})
    await _settle(gfs._app)
    assert await gfs._app[gfs_envelope_queue_repo_key].count_for(HOME) == 0


async def test_only_an_operator_lifts_a_ban(gfs):
    await _auto_accept(gfs, True)
    await _register(gfs)
    admin = gfs._app[gfs_admin_service_key]
    await admin.ban_client(HOME, reason=None, admin_ip="127.0.0.1")
    await admin.unban_client(HOME, admin_ip="127.0.0.1")
    assert await _status(gfs) == "pending"
    # After the operator's unban, auto-accept applies again as before…
    await _register(gfs)
    assert await _status(gfs) == "active"
    # …and an operator accept works on its own.
    await admin.ban_client(HOME, reason=None, admin_ip="127.0.0.1")
    await admin.unban_client(HOME, admin_ip="127.0.0.1")
    await admin.accept_client(HOME, admin_ip="127.0.0.1")
    assert await _status(gfs) == "active"


async def test_a_pending_household_follows_auto_accept_as_before(gfs):
    await _auto_accept(gfs, False)
    assert (await _register(gfs))[1]["status"] == "pending"
    await _register(gfs)
    assert await _status(gfs) == "pending"
    await _auto_accept(gfs, True)
    await _register(gfs)
    assert await _status(gfs) == "active"


async def test_an_operator_accepted_household_is_not_demoted_by_re_registering(gfs):
    await _auto_accept(gfs, False)
    await _register(gfs)
    await gfs._app[gfs_admin_service_key].accept_client(HOME, admin_ip="127.0.0.1")
    await _register(gfs)
    assert await _status(gfs) == "active"


async def test_no_upsert_turns_a_ban_back(gfs_db):
    repo = SqliteGfsFederationRepo(gfs_db)
    row = ClientInstance(
        instance_id=HOME, display_name="x", public_key=PK, status="banned"
    )
    await repo.upsert_instance(row)
    for status in ("active", "pending"):
        await repo.upsert_instance(
            ClientInstance(
                instance_id=HOME, display_name="y", public_key=PK, status=status
            )
        )
        assert (await repo.get_instance(HOME)).status == "banned"
        assert (
            await repo.register_instance_pinned(
                ClientInstance(
                    instance_id=HOME, display_name="z", public_key=PK, status=status
                )
            )
            is False
        )
        assert (await repo.get_instance(HOME)).status == "banned"
    # The operator path still works.
    await repo.set_instance_status(HOME, "pending")
    assert (await repo.get_instance(HOME)).status == "pending"
