"""Scenario: a member household on two GFSs of a space keeps receiving when
the GFS its member card names goes down.

Real GFS apps (two), the real mailbox service (mint, claim, failover) and
the real member directory on the member; the real directory on the host
takes the moved card and resolves the mailbox a sender addresses. Only the
federation transport between the two households is a capture (the card's
delivery to the host is handed over by hand).
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from socialhome.domain.federation import GfsConnection
from socialhome.federation.member_card import verify_card
from socialhome.global_server.app_keys import gfs_fed_repo_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services import gfs_mailbox_service as gms
from socialhome.services.gfs_envelope_sender import normalize_gfs_base
from socialhome.services.gfs_mailbox_service import GfsMailboxService
from socialhome.services.space_member_directory import SpaceMemberDirectory
from tests.services.test_gfs_mailbox_service import (
    HOME_ID,
    HOME_PK,
    HOME_SEED,
    KEK,
    _Config,
    _Gfs,
    _Repo,
)
from tests.services.test_space_member_directory import (
    SPACE,
    World,
    _card,
    _hh,
)

SEALED = {"kem_suite": "x25519", "eph_pk": "ZXBo", "ciphertext": "Y2lwaGVy"}


async def _gfs_server(tmp_path, gfs_id: str) -> TestServer:
    data = tmp_path / gfs_id
    data.mkdir()
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url=f"http://{gfs_id}.test",
        data_dir=str(data),
        instance_id=gfs_id,
        cluster_enabled=False,
        cluster_node_id=gfs_id,
        cluster_peers=(),
    )
    server = TestServer(create_gfs_app(cfg))
    await server.start_server()
    await server.app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id=HOME_ID,
            display_name="Home",
            public_key=HOME_PK.hex(),
            status="active",
        )
    )
    return server


async def _connection(session, server: TestServer, gfs_id: str) -> GfsConnection:
    url = str(server.make_url("")).rstrip("/")
    async with session.get(url + "/gfs/info") as resp:
        pk = (await resp.json())["public_key"]
    return GfsConnection(
        id=f"conn-{gfs_id}",
        gfs_instance_id=gfs_id,
        display_name=gfs_id,
        public_key=pk,
        inbox_url=url,
        status="active",
        paired_at="2026-11-08T00:00:00+00:00",
    )


async def _until(check, *, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.02)


@pytest.fixture
async def two_gfs(tmp_path):
    a = await _gfs_server(tmp_path, "gfs-node-a")
    b = await _gfs_server(tmp_path, "gfs-node-b")
    async with aiohttp.ClientSession() as session:
        yield session, a, b
    for server in (a, b):
        await server.close()


async def test_a_member_on_two_gfss_still_receives_after_its_cards_gfs_goes_down(
    two_gfs, monkeypatch
):
    monkeypatch.setattr(gms, "FAILOVER_AFTER_S", 0.3)
    monkeypatch.setattr(gms, "CLAIM_BACKOFF_MIN_S", 0.05)
    session, server_a, server_b = two_gfs
    conn_a = await _connection(session, server_a, "gfs-node-a")
    conn_b = await _connection(session, server_b, "gfs-node-b")

    # ── The member: connected to both GFSs of the space ──
    member = World(host=_hh())
    host = member.host
    mailboxes = GfsMailboxService(
        instance_config=_Config(),
        key_manager=KeyManager(KEK),
        gfs_repo=_Repo(conn_a, conn_b),
        gfs_service=_Gfs(session),
        instance_id=HOME_ID,
        identity_seed=HOME_SEED,
        release_delay=(0.0, 0.0),
    )
    received: list[dict] = []

    async def inbound(frame, keys) -> None:
        received.append({"frame": frame, "mailbox_id": keys.mailbox_id})

    mailboxes.attach_inbound(inbound)
    member.dir = SpaceMemberDirectory(
        space_repo=member.spaces,
        federation_repo=member.fed_repo,
        writer_households=member.writers,
        mailboxes=mailboxes,
        instance_id=member.me["id"],
        identity_seed=member.me["seed"],
        identity_pk=member.me["pk"],
        keywrap_pk=member.me["kw_pub"],
        keywrap_sig=member.me["kw_sig"],
        remote_member_repo=member.seats,
    )
    member.dir.attach_to(member.fed)
    # The space reaches both GFSs: a GFS-reached member at each (the
    # host's directory flags them) — so we hold a mailbox at each.
    urls = {
        normalize_gfs_base(conn_a.inbox_url): "a",
        normalize_gfs_base(conn_b.inbox_url): "b",
    }
    for url in urls:
        other = _hh()
        member.member(other)
        await member.directory(
            {
                "cards": [_card(other, gfs=url).to_wire()],
                "gfs_reached": [other["id"]],
            }
        )
    await mailboxes.start()
    for conn in (conn_a, conn_b):
        await mailboxes._connected(conn.id)
    for _ in range(4):  # one mint per call (the directory asked for some)
        if mailboxes.mailbox_gfs_urls() == set(urls):
            break
        await mailboxes.mint_missing()
    assert mailboxes.mailbox_gfs_urls() == set(urls)
    assert await mailboxes.wait_claimed(5)

    # ── The host: takes our card, resolves where a sender addresses us ──
    host_world = World()
    host_world.me = host  # the same household the member takes as host
    host_world.dir = SpaceMemberDirectory(
        space_repo=host_world.spaces,
        federation_repo=host_world.fed_repo,
        writer_households=host_world.writers,
        mailboxes=host_world.mailboxes,
        instance_id=host["id"],
        identity_seed=host["seed"],
        identity_pk=host["pk"],
        keywrap_pk=host["kw_pub"],
        keywrap_sig=host["kw_sig"],
        remote_member_repo=host_world.seats,
    )
    host_world.dir.attach_to(host_world.fed)
    host_world.spaces.owner[SPACE] = host["id"]
    host_world.spaces.rows[SPACE] = {host["id"]: None}
    host_world.member(member.me)

    async def hand_over_cards() -> None:
        """What the member's transport carried to the host: its card."""
        for sent in member.fed.cards:
            assert sent["to"] == host["id"]
            card = verify_card(
                sent["payload"]["card"], expected_instance_id=member.me["id"]
            )
            await host_world.card_from(member.me, card)
        member.fed.cards.clear()

    await member.dir.publish_cards()
    await hand_over_cards()
    first = await host_world.dir.target_for(member.me["id"], SPACE)
    assert first is not None
    down, other = (
        (server_a, server_b)
        if first.gfs_url == normalize_gfs_base(conn_a.inbox_url)
        else (server_b, server_a)
    )

    # ── The card's GFS goes down ──
    # It stops taking connections and drops every open one — our claim
    # socket and any pooled keep-alive a reconnect could ride (a full app
    # shutdown would wait out its open sockets for a minute).
    assert down.runner is not None and down.runner.server is not None
    for site in list(down.runner.sites):
        await site.stop()
    await down.runner.server.shutdown(0.1)
    await _until(lambda: bool(member.fed.cards), timeout=15)
    await hand_over_cards()
    moved = await host_world.dir.target_for(member.me["id"], SPACE)
    assert moved is not None
    assert moved.gfs_url == normalize_gfs_base(str(other.make_url("")))
    assert moved.mailbox_id != first.mailbox_id

    # ── A space post addressed the way the host addresses it now ──
    async with session.post(
        moved.gfs_url + "/gfs/envelope",
        json={"to_instance": moved.mailbox_id, "sealed": SEALED},
    ) as resp:
        assert resp.status == 202
    await _until(lambda: bool(received))
    assert received[0]["mailbox_id"] == moved.mailbox_id
    assert received[0]["frame"]["sealed"] == SEALED

    await mailboxes.stop()
