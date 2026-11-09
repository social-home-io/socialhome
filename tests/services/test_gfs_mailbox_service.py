"""This household's ONE mailbox per GFS (GFS redesign PR 3, v_89), end to
end against the real GFS app: root, ensure (mint + register + claim),
restart, release, sweep mint, flood rotation, card rev, privacy."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import aiohttp
import orjson
import pytest
from aiohttp.test_utils import TestServer

from socialhome.crypto import derive_instance_id, ed25519_public_key
from socialhome.domain.federation import GfsConnection
from socialhome.domain.federation_capabilities import OURS
from socialhome.global_server.app_keys import gfs_fed_repo_key, gfs_mailbox_repo_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services import gfs_mailbox_service as gms
from socialhome.services.gfs_envelope_sender import normalize_gfs_base
from socialhome.services.gfs_mailbox_service import (
    MAILBOX_ROOT_CONFIG_KEY,
    MAILBOXES_CONFIG_KEY,
    PENDING_TTL_S,
    GfsMailboxService,
)

GFS_ID = "gfs-node-a"
HOME_SEED = bytes([5]) * 32
HOME_PK = ed25519_public_key(HOME_SEED)
HOME_ID = derive_instance_id(HOME_PK)
KEK = os.urandom(32)
WALL = 1_800_000_000.0


class _Config:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value):
        self.data[key] = value

    async def set_many(self, items):
        self.data.update(items)

    async def delete(self, key):
        self.data.pop(key, None)


class _GfsRepo:
    def __init__(self, conn: GfsConnection) -> None:
        self.conn = conn

    async def get(self, conn_id):
        return self.conn if conn_id == self.conn.id else None

    async def list_active(self):
        return [self.conn]


class _Gfs:
    """The connection service's surface the mailbox service uses."""

    def __init__(self, session) -> None:
        self.session = session

    async def envelope_mailbox_supported(self, conn) -> bool:
        return True

    @contextlib.asynccontextmanager
    async def anon_session(self, conn):
        yield self.session

    def client(self):
        return self.session


@pytest.fixture
async def gfs(tmp_dir):
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id=GFS_ID,
        cluster_enabled=False,
        cluster_node_id=GFS_ID,
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
    async with aiohttp.ClientSession() as session:
        url = str(server.make_url("")).rstrip("/")
        async with session.get(url + "/gfs/info") as resp:
            pk = (await resp.json())["public_key"]
        conn = GfsConnection(
            id="conn-1",
            gfs_instance_id=GFS_ID,
            display_name="Test GFS",
            public_key=pk,
            inbox_url=url,
            status="active",
            paired_at="2026-11-08T00:00:00+00:00",
        )
        yield server, session, url, conn
    await server.close()


def _service(session, conn, *, config=None, now=None):
    clock = (lambda: now[0]) if now is not None else (lambda: 1000.0)
    return GfsMailboxService(
        instance_config=config or _Config(),
        key_manager=KeyManager(KEK),
        gfs_repo=_GfsRepo(conn),
        gfs_service=_Gfs(session),
        instance_id=HOME_ID,
        identity_seed=HOME_SEED,
        clock=clock,
        wall_clock=lambda: WALL,
        release_delay=(0.0, 0.0),
        claim_jitter=(0.0, 0.0),
        card_delay=(0.0, 0.0),
    )


async def _ready(svc, conn):
    await svc.start()
    await svc._connected(conn.id, delay=0)


def _counter():
    calls: list[int] = []

    async def hook() -> None:
        calls.append(1)

    return calls, hook


async def _mailbox_id(svc, url) -> str:
    found = svc.mailbox_at(url)
    assert found is not None
    return found[0].mailbox_id


async def test_the_root_is_generated_once_and_kept_under_the_kek(gfs):
    _server, session, _url, conn = gfs
    config = _Config()
    a = _service(session, conn, config=config)
    await a.start()
    stored = config.data[MAILBOX_ROOT_CONFIG_KEY]
    root = a._root
    assert root is not None and len(root) == 32
    assert root.hex() not in stored  # wrapped, not plain
    assert KeyManager(KEK).decrypt(stored) == root
    b = _service(session, conn, config=config)
    await b.start()
    assert b._root == root
    assert config.data[MAILBOX_ROOT_CONFIG_KEY] == stored  # not regenerated
    await a.stop()
    await b.stop()


async def test_ensure_mints_one_mailbox_per_gfs_and_reuses_it(gfs):
    server, session, url, conn = gfs
    config = _Config()
    svc = _service(session, conn, config=config)
    calls, hook = _counter()
    svc.attach_on_change(hook)
    await _ready(svc, conn)
    tickets = len(svc._tickets[conn.id].cache)
    assert tickets > 0
    rev0 = svc.rev

    own = await svc.ensure(url)
    assert own is not None and own.gfs_instance_id == GFS_ID
    assert own.gfs_url == normalize_gfs_base(url)
    mailbox_id = await _mailbox_id(svc, url)
    assert await server.app[gfs_mailbox_repo_key].get(mailbox_id) is not None
    assert await svc.wait_claimed(5)
    doc = orjson.loads(config.data[MAILBOXES_CONFIG_KEY])
    assert doc["boxes"] == {GFS_ID: {"gen": own.gen, "gfs": own.gfs_url}}
    assert doc["proto"] == OURS and doc["rev"] == svc.rev

    again = await svc.ensure(url + "/")  # same GFS, other spelling
    assert again == own
    assert len(svc._tickets[conn.id].cache) == tickets - 1  # one ticket spent
    assert svc.rev == rev0 + 1  # moved once
    assert calls == [1]  # hook once
    assert svc.open_claims() == 1
    await svc.stop()


async def test_a_restart_reclaims_the_stored_mailbox_without_registering(gfs):
    server, session, url, conn = gfs
    config = _Config()
    first = _service(session, conn, config=config)
    await _ready(first, conn)
    await first.ensure(url)
    mailbox_id = await _mailbox_id(first, url)
    await first.stop()

    again = _service(session, conn, config=config)
    await again.start()
    assert await _mailbox_id(again, url) == mailbox_id
    assert again.open_claims() == 1
    assert await again.wait_claimed(5)
    assert again._tickets == {}  # no register (that would need tickets)
    assert await server.app[gfs_mailbox_repo_key].get(mailbox_id) is not None
    await again.stop()


async def test_an_unneeded_mailbox_is_released_once_no_longer_pending(gfs):
    server, session, url, conn = gfs
    now = [1000.0]
    config = _Config()
    svc = _service(session, conn, config=config, now=now)
    calls, hook = _counter()
    svc.attach_on_change(hook)

    async def needed() -> set[str]:
        return set()

    svc.attach_needed(needed)
    await _ready(svc, conn)
    await svc.ensure(url)
    mailbox_id = await _mailbox_id(svc, url)
    assert await svc.wait_claimed(5)
    assert await svc.release_unused() == 0  # fresh mint: still pending
    assert svc.mailbox_at(url) is not None

    rev = svc.rev
    now[0] += PENDING_TTL_S + 1
    assert await svc.release_unused() == 1
    await svc.settle_releases()
    assert svc.mailbox_at(url) is None
    assert svc.open_claims() == 0
    assert orjson.loads(config.data[MAILBOXES_CONFIG_KEY])["boxes"] == {}
    assert svc.rev == rev + 1
    assert calls == [1, 1]  # mint + release
    assert await server.app[gfs_mailbox_repo_key].get(mailbox_id) is None
    await svc.stop()


async def test_the_sweep_mints_a_needed_mailbox_from_cached_tickets_only(gfs):
    server, session, url, conn = gfs
    svc = _service(session, conn)

    async def needed() -> set[str]:
        return {url}

    svc.attach_needed(needed)
    await svc.start()
    assert await svc.mint_missing() == 0  # no cached ticket: none fetched
    assert svc.mailbox_at(url) is None
    assert len(svc._tickets[conn.id].cache) == 0

    await svc._connected(conn.id, delay=0)  # the refill on connect
    tickets = len(svc._tickets[conn.id].cache)
    assert tickets > 0
    assert await svc.mint_missing() == 1
    mailbox_id = await _mailbox_id(svc, url)
    assert await server.app[gfs_mailbox_repo_key].get(mailbox_id) is not None
    assert len(svc._tickets[conn.id].cache) == tickets - 1
    assert await svc.mint_missing() == 0  # nothing missing any more
    await svc.stop()


async def test_a_sweep_mint_publishes_the_card_after_its_own_wait(gfs):
    """Only the jittered sweep mints (never a directory frame: that would
    line the anonymous register up with the host's traffic). The new card
    then goes to the hosts after its own wait, not at the next sweep and
    never in the register's tick."""
    _server, session, url, conn = gfs
    svc = _service(session, conn)
    hooks: list[int] = []

    async def needed() -> set[str]:
        return {url}

    async def after() -> None:
        hooks.append(1)

    svc.attach_needed(needed)
    svc.attach_after_sweep(after)
    await _ready(svc, conn)
    await svc.sweep_once()
    assert svc.mailbox_at(url) is not None
    await asyncio.gather(*[t for t in svc._tasks if t.get_name() == "gfs-mailbox-card"])
    assert hooks == [1, 1]  # the sweep's own, then the new card's
    await svc.sweep_once()  # nothing missing: no extra publish
    assert hooks == [1, 1, 1]
    await svc.stop()


async def test_the_card_after_a_mint_is_fail_soft_and_stops_with_the_service(
    gfs, monkeypatch, caplog
):
    _server, session, _url, conn = gfs
    svc = _service(session, conn)
    hooks: list[int] = []

    async def after() -> None:
        hooks.append(1)
        raise RuntimeError("hook")

    async def one(self) -> int:
        return 1

    monkeypatch.setattr(gms.GfsMailboxService, "mint_missing", one)
    svc.attach_after_sweep(after)
    await svc.sweep_once()
    await asyncio.gather(*list(svc._tasks))
    assert hooks == [1, 1] and "publishing the new card failed" in caplog.text

    svc._card_delay = (30.0, 30.0)
    await svc.sweep_once()
    await asyncio.sleep(0)
    await svc.stop()  # the wait ends on stop: no publish
    assert hooks == [1, 1, 1]


async def test_a_flood_rotates_the_mailbox_once_a_day(gfs, monkeypatch):
    server, session, url, conn = gfs
    monkeypatch.setattr(gms, "FLOOD_ROTATE_DROPS", 3)
    svc = _service(session, conn)
    calls, hook = _counter()
    svc.attach_on_change(hook)
    await _ready(svc, conn)
    old = await svc.ensure(url)
    old_keys, _ = svc.mailbox_at(url)
    assert await svc.wait_claimed(5)

    for _ in range(2):
        svc.note_flood(old_keys)
    assert svc.mailbox_at(url)[1] == old  # below the bound: kept
    svc.note_flood(old_keys)
    async with asyncio.timeout(5):
        while svc.mailbox_at(url)[1] == old:
            await asyncio.sleep(0.01)
    new_keys, new = svc.mailbox_at(url)
    assert new.gen != old.gen and new_keys.mailbox_id != old_keys.mailbox_id
    assert calls == [1, 1]  # mint + rotation
    await svc.settle_releases()
    repo = server.app[gfs_mailbox_repo_key]
    assert await repo.get(old_keys.mailbox_id) is None  # old one unregistered
    assert await repo.get(new_keys.mailbox_id) is not None
    assert await svc.wait_claimed(5)

    # A second flood the same day does not rotate again.
    for _ in range(5):
        svc.note_flood(new_keys)
    await asyncio.sleep(0.05)
    assert svc.mailbox_at(url)[1] == new
    assert await svc.rotate(GFS_ID) is False  # ROTATIONS_PER_DAY spent
    assert calls == [1, 1]
    await svc.stop()


async def test_a_plain_rotation_keeps_the_old_mailbox_delivering_until_released(
    gfs, monkeypatch
):
    """Review I8: members learn a rotated card only at the next sweep, so a
    rotation that is not a flood keeps delivering on the old address until
    its own release delay is over; a flood drops at once."""
    _server, session, url, conn = gfs
    svc = _service(session, conn)
    await _ready(svc, conn)
    await svc.ensure(url)
    old_claim = svc._claims[GFS_ID]
    released: list = []
    monkeypatch.setattr(
        gms.GfsMailboxService,
        "_schedule_release",
        lambda _self, claim: released.append(claim),
    )
    assert await svc.rotate(GFS_ID) is True
    assert released == [old_claim] and old_claim.released is False
    await svc.stop()


async def test_the_card_rev_moves_when_the_stored_proto_is_older(gfs):
    _server, session, _url, conn = gfs
    config = _Config()
    config.data[MAILBOXES_CONFIG_KEY] = orjson.dumps(
        {"rev": int(WALL * 1000) + 7, "proto": OURS - 1, "boxes": {}}
    ).decode()
    svc = _service(session, conn, config=config)
    await svc.start()
    assert svc.rev == int(WALL * 1000) + 8
    doc = orjson.loads(config.data[MAILBOXES_CONFIG_KEY])
    assert doc == {"rev": svc.rev, "proto": OURS, "boxes": {}}
    await svc.stop()

    # Same proto: a restart keeps the rev.
    again = _service(session, conn, config=config)
    await again.start()
    assert again.rev == svc.rev
    await again.stop()


async def test_no_info_log_names_a_mailbox_id(gfs, caplog, monkeypatch):
    _server, session, url, conn = gfs
    monkeypatch.setattr(gms, "FLOOD_ROTATE_DROPS", 1)
    now = [1000.0]
    svc = _service(session, conn, now=now)

    async def needed() -> set[str]:
        return set()

    svc.attach_needed(needed)
    ids: set[str] = set()
    with caplog.at_level(logging.INFO):
        await _ready(svc, conn)
        await svc.ensure(url)
        keys, _ = svc.mailbox_at(url)
        ids.add(keys.mailbox_id)
        assert await svc.wait_claimed(5)
        svc.note_flood(keys)
        async with asyncio.timeout(5):
            while svc.mailbox_at(url)[0].mailbox_id == keys.mailbox_id:
                await asyncio.sleep(0.01)
        ids.add(svc.mailbox_at(url)[0].mailbox_id)
        now[0] += PENDING_TTL_S + 1
        assert await svc.release_unused() == 1
        await svc.settle_releases()
        await svc.stop()
    assert caplog.records  # the flow did log
    for record in caplog.records:
        text = record.getMessage() + (record.exc_text or "")
        for mailbox_id in ids:
            assert mailbox_id not in text
