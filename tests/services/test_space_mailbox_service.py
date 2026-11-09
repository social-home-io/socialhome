"""The household's per-space GFS mailboxes (GFS redesign PR 2, v_88),
end to end against the real GFS app: root, tickets on connect, mint +
register + claim, delivery, targets, release + unregister, privacy."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import aiohttp
import orjson
import pytest
from aiohttp.test_utils import TestServer

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.federation import GfsConnection
from socialhome.domain.federation_capabilities import OURS
from socialhome.federation.space_mailbox_entry import verify_entry
from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
    gfs_mailbox_repo_key,
    gfs_mailbox_ws_registry_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.space_mailbox_service import (
    MAILBOX_ROOT_CONFIG_KEY,
    PENDING_TTL_S,
    TICKET_PIN_CONFIG_PREFIX,
    SpaceMailboxService,
    pin_from_json,
    pin_to_json,
)

GFS_ID = "gfs-node-a"
HOME_SEED = bytes([5]) * 32
HOME_PK = ed25519_public_key(HOME_SEED)
HOME_ID = derive_instance_id(HOME_PK)
KW_PUB = generate_x25519_keypair().public_key
KW_SIG = b64url_encode(sign_ed25519(HOME_SEED, KW_PUB))
KEK = os.urandom(32)


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


class _Spaces:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str | None] = {}
        #: Spaces nobody of ours sits in any more (left / dissolved).
        self.left: set[str] = set()

    async def list_local_member_user_ids(self, space_id):
        return [] if space_id in self.left else ["someone"]

    async def list_member_instances(self, space_id):
        return [i for (s, i) in self.rows if s == space_id]

    async def set_space_instance_mailbox(self, space_id, instance_id, value):
        if (space_id, instance_id) in self.rows:
            self.rows[(space_id, instance_id)] = value

    async def list_space_instance_mailboxes(self):
        return [(s, i, v) for (s, i), v in self.rows.items() if v is not None]


class _GfsRepo:
    def __init__(self, conn: GfsConnection) -> None:
        self.conn = conn

    async def get(self, conn_id):
        return self.conn if conn_id == self.conn.id else None

    async def list_active(self):
        return [self.conn]


class _Gfs:
    """The connection service's surface the mailbox service uses."""

    def __init__(self, session, *, mailbox: bool = True) -> None:
        self.session = session
        self.mailbox = mailbox
        self.anon_sessions = 0

    async def envelope_mailbox_supported(self, conn) -> bool:
        return self.mailbox

    @contextlib.asynccontextmanager
    async def anon_session(self, conn):
        self.anon_sessions += 1
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


def _service(session, conn, *, config=None, spaces=None, kek=None, mailbox=True):
    svc = SpaceMailboxService(
        space_repo=spaces or _Spaces(),
        instance_config=config or _Config(),
        key_manager=KeyManager(kek or KEK),
        gfs_repo=_GfsRepo(conn),
        gfs_service=_Gfs(session, mailbox=mailbox),
        instance_id=HOME_ID,
        identity_seed=HOME_SEED,
        keywrap_public_key=KW_PUB,
        keywrap_sig=KW_SIG,
        release_delay=(0.0, 0.0),
        claim_jitter=(0.0, 0.0),
    )
    return svc


async def _ready(svc, conn):
    await svc.start()
    await svc._connected(conn.id, delay=0)


async def test_the_root_is_generated_once_and_kept_under_the_kek(gfs):
    _server, session, _url, conn = gfs
    config, kek = _Config(), os.urandom(32)
    a = _service(session, conn, config=config, kek=kek)
    await a.start()
    stored = config.data[MAILBOX_ROOT_CONFIG_KEY]
    root = a._root
    assert root is not None and root.hex() not in stored  # wrapped, not plain
    b = _service(session, conn, config=config, kek=kek)
    await b.start()
    assert b._root == root
    await a.stop()
    await b.stop()


async def test_a_fresh_household_gets_tickets_on_connect_and_the_pin_persists(gfs):
    _server, session, _url, conn = gfs
    config = _Config()
    svc = _service(session, conn, config=config)
    await _ready(svc, conn)
    assert len(svc._tickets[conn.id].cache) > 0
    pin = pin_from_json(config.data[TICKET_PIN_CONFIG_PREFIX + GFS_ID])
    assert pin is not None and pin == svc._tickets[conn.id].guard.pin
    # A restart restores the pin (no fresh first-sight).
    again = _service(session, conn, config=config)
    await _ready(again, conn)
    assert again._tickets[conn.id].guard.pin.key_id == pin.key_id
    assert again._tickets[conn.id].guard.pin.first_seen == pin.first_seen
    await svc.stop()
    await again.stop()


class _FedRepo:
    """The counterpart's ``remote_instances`` row (a space_session seat)."""

    def __init__(self, url: str, *, proto: int = OURS) -> None:
        from types import SimpleNamespace

        from socialhome.domain.federation import InstanceSource

        self.row = SimpleNamespace(
            source=InstanceSource.SPACE_SESSION,
            relay_via=url,
            proto_version=proto,
            remote_identity_pk="00",
        )

    async def get_instance(self, instance_id):
        return self.row if instance_id == "peer-iid" else None


async def test_the_sweep_mints_a_missing_own_mailbox_and_announces_it(gfs):
    """Round 5: a seat that got no mailbox of ours (no ticket, a refused or
    unreachable register, a mint past its bound) is retried by the sweep —
    from cached tickets only — and the counterpart is told with
    SPACE_MAILBOX_UPDATED: the instance-addressed fallback is transient."""
    server, session, url, conn = gfs
    spaces = _Spaces()
    spaces.rows[("space-1", "peer-iid")] = orjson.dumps(
        {"peer": None, "own": None}
    ).decode()
    svc = _service(session, conn, spaces=spaces)
    svc._federation_repo = _FedRepo(url)  # type: ignore[assignment]
    announced: list = []

    async def _announce(space_id, instance_id, entry):
        announced.append((space_id, instance_id, entry))

    svc.attach_announcer(_announce)
    await _ready(svc, conn)
    assert await svc.retry_missing() == 1
    row = orjson.loads(spaces.rows[("space-1", "peer-iid")])
    assert row["own"] is not None
    [(space, peer, entry)] = announced
    assert (space, peer) == ("space-1", "peer-iid") and entry is not None
    assert await server.app[gfs_mailbox_repo_key].get(entry.mailbox_id)
    # Done: nothing more to retry.
    assert await svc.retry_missing() == 0
    await svc.stop()


async def test_the_retry_spends_cached_tickets_only_and_skips_old_peers(gfs):
    _server, session, url, conn = gfs
    spaces = _Spaces()
    spaces.rows[("space-1", "peer-iid")] = orjson.dumps(
        {"peer": None, "own": None}
    ).decode()
    svc = _service(session, conn, spaces=spaces)
    svc._federation_repo = _FedRepo(url, proto=87)  # type: ignore[assignment]
    await _ready(svc, conn)
    assert await svc.retry_missing() == 0  # a peer below v_88: never
    svc._federation_repo = _FedRepo(url)  # type: ignore[assignment]
    svc._tickets[conn.id].cache.clear()
    assert await svc.retry_missing() == 0  # no cached ticket: none fetched
    assert len(svc._tickets[conn.id].cache) == 0
    await svc.stop()


async def test_leaving_a_gfs_stops_its_ticket_loops(gfs):
    _server, session, _url, conn = gfs
    svc = _service(session, conn)
    await _ready(svc, conn)
    state = svc._tickets[conn.id]
    assert len(state.cache)
    await svc.forget_connection(conn)
    assert conn.id not in svc._tickets
    assert len(state.cache) == 0
    await svc.forget_connection(conn)  # idempotent
    await svc.stop()


def test_pin_round_trips_and_garbage_is_ignored():
    from socialhome.domain.gfs_mailbox import TicketKeyPin

    pin = TicketKeyPin(b"k" * 32, 123, 30, b"p" * 32)
    assert pin_from_json(pin_to_json(pin)) == pin
    assert (
        pin_from_json(pin_to_json(TicketKeyPin(b"k" * 32, 1, 7, None))).previous_key_id
        is None
    )
    assert pin_from_json("{not json") is None
    assert pin_from_json(None) is None


async def test_mint_registers_claims_and_delivers_frames(gfs):
    server, session, url, conn = gfs
    svc = _service(session, conn)
    got: list = []

    async def _inbound(frame, keys):
        got.append((frame, keys.space_id))

    svc.attach_inbound(_inbound)
    await _ready(svc, conn)
    minted = await svc.mint("space-1", url, peer="peer-iid")
    assert minted is not None
    entry, own = minted
    verify_entry(
        entry.to_wire(),
        identity_pk_hex=HOME_PK.hex(),
        expected_instance_id=HOME_ID,
        expected_space_id="space-1",
    )
    assert entry.proto_version == OURS and own.gfs_instance_id == GFS_ID
    assert await server.app[gfs_mailbox_repo_key].get(entry.mailbox_id) is not None
    assert await svc.wait_claimed(5)
    async with session.post(
        url + "/gfs/envelope",
        json={
            "to_instance": entry.mailbox_id,
            "sealed": {"kem_suite": "x25519", "eph_pk": "ZXBo", "ciphertext": "Y2lw"},
        },
    ) as resp:
        assert resp.status == 202
    for _ in range(200):
        if got:
            break
        await asyncio.sleep(0.02)
    assert got and got[0][1] == "space-1"
    await svc.stop()


async def test_one_mailbox_per_member_pair_unlinkable_across_pairs_and_spaces(gfs):
    """Owner decision (#1333): a host seats each GFS-only member with its OWN
    mailbox — two members of one space get unrelated mailboxes (and two
    sockets); a retry for the same member reuses its one."""
    _server, session, url, conn = gfs
    svc = _service(session, conn)
    await _ready(svc, conn)
    a1, own1 = await svc.mint("space-1", url, peer="member-a")
    a2, own2 = await svc.mint("space-1", url, peer="member-a")  # a retry
    b, _ = await svc.mint("space-1", url, peer="member-b")  # another member
    c, _ = await svc.mint("space-2", url, peer="member-a")  # another space
    assert a1.mailbox_id == a2.mailbox_id and own1 == own2
    assert len({a1.mailbox_id, b.mailbox_id, c.mailbox_id}) == 3
    assert len({a1.kem_pk, b.kem_pk, c.kem_pk}) == 3
    assert svc.open_claims() == 3  # one socket per mailbox
    for e in (a1, b, c):
        assert e.mailbox_id != HOME_ID and HOME_ID not in e.mailbox_id
    await svc.stop()


async def test_a_gfs_without_mailboxes_mints_nothing(gfs):
    _server, session, url, conn = gfs
    svc = _service(session, conn, mailbox=False)
    await _ready(svc, conn)
    assert await svc.mint("space-1", url, peer="peer-iid") is None
    assert svc.open_claims() == 0
    await svc.stop()


async def test_remember_and_targets_per_space_and_space_less(gfs):
    _server, session, url, conn = gfs
    spaces = _Spaces()
    spaces.rows[("space-1", "peer-iid")] = None
    host = _service(session, conn)
    await _ready(host, conn)
    peer_entry, _ = await host.mint(
        "space-1", url, peer="peer-iid"
    )  # stands in for the peer's
    svc = _service(session, conn, spaces=spaces)
    await _ready(svc, conn)
    _mine, own = await svc.mint("space-1", url, peer="peer-iid")
    await svc.remember("space-1", "peer-iid", peer=peer_entry, own=own)
    stored = orjson.loads(spaces.rows[("space-1", "peer-iid")])
    assert stored["own"] == {"gen": own.gen, "gfs_id": GFS_ID, "gfs": url}
    target = await svc.target_for("peer-iid", "space-1")
    assert target is not None and target.mailbox_id == peer_entry.mailbox_id
    assert target.kem_pk == peer_entry.kem_pk
    assert await svc.target_for("peer-iid", "space-2") is None
    assert (await svc.target_for("peer-iid", None)).mailbox_id == peer_entry.mailbox_id
    assert await svc.target_for("someone-else", None) is None
    # A restart rebuilds the targets and reopens our claim from the row.
    again = _service(session, conn, spaces=spaces, config=svc._config)
    await again.start()
    assert (await again.target_for("peer-iid", "space-1")) is not None
    assert again.open_claims() == 1
    for s in (host, svc, again):
        await s.stop()


async def _until(predicate, *, timeout: float = 15.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def test_a_mailbox_the_gfs_forgot_is_announced_lost_then_recovered(gfs):
    """The GFS dropped our mailbox (its 30-day idle sweep while we were
    away, or data loss): the counterpart is told at once to fall back to the
    instance-addressed relay (``None``), the SAME mailbox is registered
    again from the cached tickets (no fetch next to the register), claimed,
    and the counterpart is told it is back."""
    server, session, url, conn = gfs
    spaces = _Spaces()
    spaces.rows[("space-1", "peer-iid")] = None
    svc = _service(session, conn, spaces=spaces)
    announced: list = []

    async def _announce(space_id, instance_id, entry):
        announced.append((space_id, instance_id, entry))

    svc.attach_announcer(_announce)
    await _ready(svc, conn)
    entry, own = await svc.mint("space-1", url, peer="peer-iid")
    await svc.remember("space-1", "peer-iid", peer=None, own=own)
    assert await svc.wait_claimed(5)
    tickets = len(svc._tickets[conn.id].cache)
    # The GFS forgets it, and the socket drops.
    assert await server.app[gfs_mailbox_repo_key].unregister(entry.mailbox_id)
    await server.app[gfs_mailbox_ws_registry_key].close_one(
        entry.mailbox_id, code=1012, message=b"restart"
    )
    await _until(lambda: len(announced) >= 2)
    assert announced[0] == ("space-1", "peer-iid", None)  # fall back now
    space, peer, back = announced[1]
    assert (space, peer) == ("space-1", "peer-iid")
    assert back is not None and back.mailbox_id == entry.mailbox_id  # same one
    assert await server.app[gfs_mailbox_repo_key].get(entry.mailbox_id)
    assert len(svc._tickets[conn.id].cache) == tickets - 1  # from the cache
    await svc.stop()


async def test_a_forgotten_mailbox_with_no_ticket_waits_for_the_refill(gfs):
    """No cached ticket: nothing is fetched next to the register; the
    counterpart stays on the instance-addressed relay until a ticket
    arrives on the schedule, then the mailbox comes back."""
    server, session, url, conn = gfs
    spaces = _Spaces()
    spaces.rows[("space-1", "peer-iid")] = None
    svc = _service(session, conn, spaces=spaces)
    announced: list = []

    async def _announce(space_id, instance_id, entry):
        announced.append(entry)

    svc.attach_announcer(_announce)
    await _ready(svc, conn)
    entry, own = await svc.mint("space-1", url, peer="peer-iid")
    await svc.remember("space-1", "peer-iid", peer=None, own=own)
    assert await svc.wait_claimed(5)
    cache = svc._tickets[conn.id].cache
    cache.clear()
    assert await server.app[gfs_mailbox_repo_key].unregister(entry.mailbox_id)
    await server.app[gfs_mailbox_ws_registry_key].close_one(
        entry.mailbox_id, code=1012, message=b"restart"
    )
    await _until(lambda: announced == [None])
    await asyncio.sleep(0.5)
    assert announced == [None]  # still lost: no ticket, none fetched
    assert len(cache) == 0
    await cache.refill()  # the scheduled refill
    await _until(lambda: len(announced) == 2, timeout=30)
    assert announced[1] is not None and announced[1].mailbox_id == entry.mailbox_id
    await svc.stop()


async def test_leaving_releases_and_unregisters_the_mailbox(gfs):
    server, session, url, conn = gfs
    spaces = _Spaces()
    spaces.rows[("space-1", "peer-iid")] = None
    svc = _service(session, conn, spaces=spaces)
    await _ready(svc, conn)
    entry, own = await svc.mint("space-1", url, peer="peer-iid")
    await svc.remember("space-1", "peer-iid", peer=None, own=own)
    assert await svc.release_unused() == 0  # still used
    del spaces.rows[("space-1", "peer-iid")]  # we left / were removed
    assert await svc.release_unused() == 1
    await svc.settle_releases()
    assert svc.open_claims() == 0
    assert await server.app[gfs_mailbox_repo_key].get(entry.mailbox_id) is None
    await svc.stop()


async def test_a_pending_mint_is_reused_by_a_retry_then_expires(gfs):
    """A redeem in flight (or failed, to be retried) keeps its mailbox: a
    retry — or a second household's concurrent redeem for the same space —
    gets the SAME one (no second ticket), and nothing releases it until the
    pending time is up."""
    server, session, url, conn = gfs
    now = [1000.0]
    svc = SpaceMailboxService(
        space_repo=_Spaces(),
        instance_config=_Config(),
        key_manager=KeyManager(KEK),
        gfs_repo=_GfsRepo(conn),
        gfs_service=_Gfs(session),
        instance_id=HOME_ID,
        identity_seed=HOME_SEED,
        keywrap_public_key=KW_PUB,
        keywrap_sig=KW_SIG,
        clock=lambda: now[0],
        release_delay=(0.0, 0.0),
        claim_jitter=(0.0, 0.0),
    )
    await _ready(svc, conn)
    tickets_before = len(svc._tickets[conn.id].cache)
    entry, _own = await svc.mint("space-1", url, peer="peer-iid")
    assert len(svc._tickets[conn.id].cache) == tickets_before - 1
    assert await svc.release_unused() == 0  # in flight
    again, _ = await svc.mint("space-1", url, peer="peer-iid")  # the retry
    assert again.mailbox_id == entry.mailbox_id
    assert len(svc._tickets[conn.id].cache) == tickets_before - 1
    now[0] += PENDING_TTL_S + 1
    assert await svc.release_unused() == 1
    await svc.settle_releases()
    assert svc.open_claims() == 0
    assert await server.app[gfs_mailbox_repo_key].get(entry.mailbox_id) is None
    await svc.stop()


async def test_a_left_space_releases_its_mailbox_and_a_rejoin_gets_a_new_one(gfs):
    """Leaving (or a dissolve) may leave the row behind: the mailbox goes
    anyway, the row forgets it, and a later join mints an unrelated one."""
    server, session, url, conn = gfs
    now = [1000.0]
    spaces = _Spaces()
    spaces.rows[("space-1", "peer-iid")] = None
    svc = SpaceMailboxService(
        space_repo=spaces,
        instance_config=_Config(),
        key_manager=KeyManager(KEK),
        gfs_repo=_GfsRepo(conn),
        gfs_service=_Gfs(session),
        instance_id=HOME_ID,
        identity_seed=HOME_SEED,
        keywrap_public_key=KW_PUB,
        keywrap_sig=KW_SIG,
        clock=lambda: now[0],
        release_delay=(0.0, 0.0),
        claim_jitter=(0.0, 0.0),
    )
    await _ready(svc, conn)
    entry, own = await svc.mint("space-1", url, peer="peer-iid")
    await svc.remember("space-1", "peer-iid", peer=entry, own=own)
    now[0] += PENDING_TTL_S + 1
    assert await svc.release_unused() == 0  # still sitting in the space
    spaces.left.add("space-1")  # the last person of ours left
    assert await svc.release_unused() == 1
    await svc.settle_releases()
    assert spaces.rows[("space-1", "peer-iid")] is None
    assert await svc.target_for("peer-iid", "space-1") is None
    assert await server.app[gfs_mailbox_repo_key].get(entry.mailbox_id) is None
    spaces.left.discard("space-1")  # joined again later
    rejoined, _ = await svc.mint("space-1", url, peer="peer-iid")
    assert rejoined.mailbox_id != entry.mailbox_id
    await svc.stop()


async def test_a_row_of_a_left_space_is_not_reopened_on_start(gfs):
    _server, session, url, conn = gfs
    spaces = _Spaces()
    spaces.rows[("space-1", "peer-iid")] = None
    svc = _service(session, conn, spaces=spaces)
    await _ready(svc, conn)
    _entry, own = await svc.mint("space-1", url, peer="peer-iid")
    await svc.remember("space-1", "peer-iid", peer=None, own=own)
    await svc.stop()
    spaces.left.add("space-1")
    again = _service(session, conn, spaces=spaces, config=svc._config)
    await again.start()
    assert again.open_claims() == 0
    await again.stop()


async def test_a_seat_right_after_connecting_still_gets_a_mailbox(gfs):
    """Regression (demo): a household that redeems a link seconds after it
    connected has no ticket yet — the mint fetches one rather than leaving
    the seat instance-addressed."""
    server, session, url, conn = gfs
    svc = _service(session, conn)
    await svc.start()  # no connect refill has run
    minted = await svc.mint("space-1", url, peer="peer-iid")
    assert minted is not None
    assert await server.app[gfs_mailbox_repo_key].get(minted[0].mailbox_id)
    await svc.stop()


async def test_a_mint_that_outlives_its_callers_wait_still_finishes(gfs):
    """A bounded wait never cancels the register: the mailbox (and the ticket
    the GFS saw) is kept pending and a retry gets the same one."""
    server, session, url, conn = gfs
    svc = _service(session, conn)
    await _ready(svc, conn)
    assert await svc.mint_within("space-1", url, peer="peer-iid", timeout=0) is None
    for _ in range(200):
        if svc.open_claims():
            break
        await asyncio.sleep(0.02)
    assert svc.open_claims() == 1
    entry, _ = await svc.mint_within("space-1", url, peer="peer-iid", timeout=5)
    assert await server.app[gfs_mailbox_repo_key].get(entry.mailbox_id)
    assert svc.open_claims() == 1
    await svc.stop()


async def test_on_connected_never_blocks_and_fetches_later(gfs):
    """The connect hook returns at once; the anonymous read happens first
    and the identified fetch only after the jittered delay."""
    _server, session, _url, conn = gfs
    svc = _service(session, conn)
    await svc.start()
    await svc.on_connected(conn.id)  # returns at once
    for _ in range(200):
        state = svc._tickets.get(conn.id)
        if state is not None and state.guard.has_view:
            break
        await asyncio.sleep(0.02)
    state = svc._tickets[conn.id]
    assert state.guard.has_view  # anonymous view read
    assert len(state.cache) == 0  # identified fetch not yet
    await svc.stop()


async def test_no_mailbox_id_in_logs_at_info_or_above(gfs, caplog):
    _server, session, url, conn = gfs
    spaces = _Spaces()
    spaces.rows[("space-1", "peer-iid")] = None
    svc = _service(session, conn, spaces=spaces)
    with caplog.at_level(logging.INFO):
        await _ready(svc, conn)
        entry, own = await svc.mint("space-1", url, peer="peer-iid")
        await svc.remember("space-1", "peer-iid", peer=None, own=own)
        del spaces.rows[("space-1", "peer-iid")]
        await svc.release_unused()
        await svc.settle_releases()
        await svc.stop()
    for rec in caplog.records:
        if rec.levelno >= logging.INFO:
            assert entry.mailbox_id not in rec.getMessage()


def _recording_session(paths: list[str]) -> aiohttp.ClientSession:
    trace = aiohttp.TraceConfig()

    async def _on_start(_session, _ctx, params):
        paths.append(params.url.path)

    trace.on_request_start.append(_on_start)
    return aiohttp.ClientSession(trace_configs=[trace])


async def test_the_identified_session_never_carries_mailbox_traffic(gfs):
    """The household's identified session (its cookies, its signed calls)
    only fetches tickets; every register, claim and unregister rides the
    anonymous one — so neither the GFS nor a sticky cookie can tie a mailbox
    to the household's identified socket."""
    _server, _session, url, conn = gfs
    identified_paths: list[str] = []
    anon_paths: list[str] = []
    identified = _recording_session(identified_paths)
    anon = _recording_session(anon_paths)
    spaces = _Spaces()
    spaces.rows[("space-1", "peer-iid")] = None
    gfs_double = _Gfs(anon)
    gfs_double.client = lambda: identified  # type: ignore[method-assign]
    svc = SpaceMailboxService(
        space_repo=spaces,
        instance_config=_Config(),
        key_manager=KeyManager(KEK),
        gfs_repo=_GfsRepo(conn),
        gfs_service=gfs_double,
        instance_id=HOME_ID,
        identity_seed=HOME_SEED,
        keywrap_public_key=KW_PUB,
        keywrap_sig=KW_SIG,
        release_delay=(0.0, 0.0),
        claim_jitter=(0.0, 0.0),
    )
    try:
        await _ready(svc, conn)
        _entry, own = await svc.mint("space-1", url, peer="peer-iid")
        assert await svc.wait_claimed(5)
        await svc.remember("space-1", "peer-iid", peer=None, own=own)
        del spaces.rows[("space-1", "peer-iid")]
        await svc.release_unused()
        await svc.settle_releases()
    finally:
        await svc.stop()
        await identified.close()
        await anon.close()
    assert set(identified_paths) <= {"/gfs/info", "/gfs/mailbox/tickets"}
    assert "/gfs/mailbox/tickets" in identified_paths
    for path in ("/gfs/mailbox/register", "/gfs/mailbox/ws", "/gfs/mailbox/unregister"):
        assert path in anon_paths
    assert "/gfs/mailbox/tickets" not in anon_paths
    assert "/gfs/ws" not in anon_paths + identified_paths
