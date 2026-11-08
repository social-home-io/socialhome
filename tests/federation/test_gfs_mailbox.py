"""Household mailbox helpers: key derivation, and register + claim end to
end against the real GFS app (real SQLite, real aiohttp server)."""

from __future__ import annotations

import asyncio
import contextlib

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from socialhome import privacy_pass as pp
from socialhome.capabilities_sig import verify_capabilities
from socialhome.crypto import (
    INSTANCE_ID_RE,
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
    x25519_exchange,
)
from socialhome.domain.gfs_mailbox import derive_mailbox_id
from socialhome.domain.relay_media_frame import (
    VALID_SEALED_LENGTHS,
    build_relay_media_body,
)
from socialhome.federation.gfs_mailbox import (
    MailboxClaim,
    MailboxClaimError,
    MailboxRegisterError,
    MailboxTicketCache,
    MailboxTicketError,
    TicketIssuer,
    build_register_body,
    derive_mailbox_keys,
    fetch_tickets,
    new_mailbox_root,
    register_mailbox,
)
from socialhome.global_server.app_keys import (
    gfs_db_key,
    gfs_envelope_relay_key,
    gfs_fed_repo_key,
    gfs_mailbox_repo_key,
    gfs_mailbox_tickets_key,
)
from socialhome.global_server.config import EnvelopeMailboxConfig, GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app

ROOT = bytes(range(32))
GFS_ID = "gfs-node-a"
#: The household's identity key (registered and active on the test GFS).
HOME_SEED = bytes([5]) * 32
HOME_ID = derive_instance_id(ed25519_public_key(HOME_SEED))
SEALED = {"kem_suite": "x25519", "eph_pk": "ZXBo", "ciphertext": "Y2lwaGVy"}


# ── Derivation ───────────────────────────────────────────────────────────


def test_keys_are_deterministic_per_space_and_gen():
    a = derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)
    assert a == derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)
    assert a.mailbox_id == derive_mailbox_id(a.sign_public)
    assert INSTANCE_ID_RE.fullmatch(a.mailbox_id)
    others = [
        derive_mailbox_keys(ROOT, GFS_ID, "space-2", 0),
        derive_mailbox_keys(ROOT, GFS_ID, "space-1", 1),
        derive_mailbox_keys(b"\x09" * 32, GFS_ID, "space-1", 0),
    ]
    for other in others:
        assert other.mailbox_id != a.mailbox_id
        assert other.kem_public != a.kem_public
        assert other.sign_public != a.sign_public
    assert a.kem_private != a.sign_seed


def test_a_mailbox_root_is_a_fresh_32_byte_secret():
    one, two = new_mailbox_root(), new_mailbox_root()
    assert len(one) == len(two) == 32 and one != two
    assert derive_mailbox_keys(one, GFS_ID, "s", 0) != derive_mailbox_keys(
        two, GFS_ID, "s", 0
    )


def test_kem_key_is_a_working_x25519_pair():
    a = derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)
    b = derive_mailbox_keys(ROOT, GFS_ID, "space-2", 0)
    assert x25519_exchange(a.kem_private, b.kem_public) == x25519_exchange(
        b.kem_private, a.kem_public
    )


def test_repr_never_shows_the_secrets():
    keys = derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)
    text = repr(keys)
    assert keys.sign_seed.hex() not in text and str(keys.sign_seed) not in text
    assert keys.mailbox_id not in text


@pytest.mark.parametrize(
    ("seed", "space", "gen"),
    [
        (b"short", "s", 0),
        (ROOT, "", 0),
        (ROOT, "a|b", 0),
        (ROOT, "s", -1),
        (ROOT, "s", True),
    ],
)
def test_derivation_refuses_bad_input(seed, space, gen):
    with pytest.raises(ValueError):
        derive_mailbox_keys(seed, GFS_ID, space, gen)


def test_register_body_carries_the_suite_tag():
    body = build_register_body(derive_mailbox_keys(ROOT, GFS_ID, "s", 0), GFS_ID)
    assert body["sig_suite"] == "ed25519"


# ── End to end against the GFS app ───────────────────────────────────────


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
    async with aiohttp.ClientSession() as session:
        url = str(server.make_url("")).rstrip("/")
        yield server, session, url, await _ticket_cache(server.app, session, url)
    await server.close()


async def _issuer(session, url) -> TicketIssuer:
    """The issuer from the GFS's SIGNED ``/gfs/info`` (as a household reads
    it: the capability block's signature verified first)."""
    async with session.get(url + "/gfs/info") as resp:
        info = await resp.json()
    assert verify_capabilities(
        info["public_key"],
        info["gfs_instance_id"],
        info["capabilities"],
        info["capabilities_sig"],
        info["capabilities_sig_suite"],
    )
    return TicketIssuer.from_capability(info["capabilities"]["mailbox_tickets"])


async def _ticket_cache(
    app, session, url, *, batch: int = 4, prefetch: bool = True, calls=None
) -> MailboxTicketCache:
    """A household registered and active on the GFS, with a ticket cache
    that fetches over its signed channel — filled ahead of use (*prefetch*),
    as a household does on connect. *calls* counts the fetches."""
    await app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id=HOME_ID,
            display_name="Home",
            public_key=ed25519_public_key(HOME_SEED).hex(),
            status="active",
        )
    )
    issuer = await _issuer(session, url)

    async def fetch(count: int) -> list[bytes]:
        if calls is not None:
            calls.append(count)
        return await fetch_tickets(
            session, url, issuer, count, instance_id=HOME_ID, signing_key=HOME_SEED
        )

    cache = MailboxTicketCache(fetch, batch=batch)
    if prefetch:
        await cache.refill()
    return cache


async def _settle(app) -> None:
    relay = app[gfs_envelope_relay_key]
    for _ in range(500):
        if relay.in_flight == 0 and not relay._media_inflight:
            return
        await asyncio.sleep(0.01)


async def test_register_claim_and_receive_queued_and_live_frames(gfs):
    server, session, url, tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)
    assert (
        await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
        == keys.mailbox_id
    )
    # Idempotent — a household may re-register at every start.
    assert (
        await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
        == keys.mailbox_id
    )

    size = min(VALID_SEALED_LENGTHS)
    media = build_relay_media_body(keys.mailbox_id, bytes([7]) * size)
    async with session.post(
        url + "/gfs/envelope", json={"to_instance": keys.mailbox_id, "sealed": SEALED}
    ) as resp:
        assert resp.status == 202
    async with session.post(url + "/gfs/envelope", data=media) as resp:
        assert resp.status == 202
    await _settle(server.app)

    async with MailboxClaim(session, url, keys, GFS_ID) as claim:
        assert await claim.receive(timeout=5) == {"type": "envelope", "sealed": SEALED}
        assert await claim.receive(timeout=5) == media
        live = build_relay_media_body(keys.mailbox_id, bytes([9]) * size)
        async with session.post(url + "/gfs/envelope", data=live) as resp:
            assert resp.status == 202
        assert await claim.receive(timeout=5) == live
    box = await server.app[gfs_mailbox_repo_key].get(keys.mailbox_id)
    assert box is not None and box.last_claim_at is not None


async def test_claim_of_an_unregistered_mailbox_is_refused(gfs):
    _server, session, url, _tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "never-registered", 0)
    with pytest.raises(MailboxClaimError, match="4401"):
        async with MailboxClaim(session, url, keys, GFS_ID):
            pass


async def test_a_claim_signed_for_another_gfs_id_is_refused(gfs):
    """The claim is bound to the GFS id the household pinned; a server that
    does not answer to it refuses (4401) — the id the challenge names is
    never what gets signed."""
    _server, session, url, tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)
    await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
    with pytest.raises(MailboxClaimError, match="4401"):
        async with MailboxClaim(session, url, keys, "some-other-gfs"):
            pass


async def test_register_refusal_raises_with_the_error_code(gfs):
    _server, session, url, tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)
    with pytest.raises(MailboxRegisterError) as err:
        await register_mailbox(session, url, keys, "some-other-gfs", tickets=tickets)
    assert (err.value.status, err.value.error) == (403, "wrong_gfs")


async def test_iterating_a_claim_ends_when_the_server_closes(gfs):
    server, session, url, tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)
    await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
    async with session.post(
        url + "/gfs/envelope", json={"to_instance": keys.mailbox_id, "sealed": SEALED}
    ) as resp:
        assert resp.status == 202
    await _settle(server.app)
    frames = []
    async with MailboxClaim(session, url, keys, GFS_ID) as claim:
        first = await claim.receive(timeout=5)
        frames.append(first)
        # A newer claim of the same mailbox replaces this socket.
        async with MailboxClaim(session, url, keys, GFS_ID):
            async for frame in claim:
                frames.append(frame)
    assert frames == [{"type": "envelope", "sealed": SEALED}]


async def test_a_household_pinned_to_an_alias_registers_and_claims(tmp_dir):
    """A GFS whose id changed keeps answering to the id a household pinned
    (``instance_id_aliases``): register + claim stay bound to it."""
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-new",
        instance_id_aliases=(GFS_ID,),
        cluster_enabled=False,
        cluster_node_id="gfs-new",
        cluster_peers=(),
    )
    server = TestServer(create_gfs_app(cfg))
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            url = str(server.make_url("")).rstrip("/")
            tickets = await _ticket_cache(server.app, session, url)
            keys = derive_mailbox_keys(ROOT, GFS_ID, "space-9", 0)
            await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
            async with MailboxClaim(session, url, keys, GFS_ID) as claim:
                assert claim is not None
    finally:
        await server.close()


async def test_a_claim_entered_by_hand_closes_with_aclosing(gfs):
    _server, session, url, tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "space-a", 0)
    await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
    claim = MailboxClaim(session, url, keys, GFS_ID)
    await claim.__aenter__()
    async with contextlib.aclosing(claim):
        assert claim._ws is not None
    assert claim._ws is None


# ── Registration tickets (Privacy Pass) ─────────────────────────────────


async def test_tickets_are_fetched_once_and_one_is_spent_per_new_mailbox(gfs):
    _server, session, url, tickets = gfs
    assert await tickets.refill() == 4
    for space in ("s-1", "s-2"):
        keys = derive_mailbox_keys(ROOT, GFS_ID, space, 0)
        await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
    assert len(tickets) == 2  # one each; no fetch in between
    assert "<2 tickets>" in repr(tickets)


async def test_a_new_mailbox_without_a_ticket_is_refused(gfs):
    _server, session, url, _tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "no-ticket", 0)
    with pytest.raises(MailboxRegisterError) as err:
        await register_mailbox(session, url, keys, GFS_ID)
    assert (err.value.status, err.value.error) == (403, "ticket_required")


async def test_a_spent_ticket_is_replaced_once(gfs):
    """A cached ticket that was spent already (here: by hand) is dropped and
    the register retried with the next one."""
    _server, session, url, tickets = gfs
    await tickets.refill()
    spent = tickets._tickets[-1]
    first = derive_mailbox_keys(ROOT, GFS_ID, "a", 0)
    await register_mailbox(session, url, first, GFS_ID, tickets=tickets)
    tickets._tickets.append(spent)
    second = derive_mailbox_keys(ROOT, GFS_ID, "b", 0)
    assert await register_mailbox(session, url, second, GFS_ID, tickets=tickets)
    assert len(tickets) == 2


async def test_tickets_under_a_retired_key_are_dropped_without_a_fetch(tmp_dir):
    """The GFS refuses a ticket's key: the household drops that key's
    tickets and fails — it never fetches right away (an identified fetch
    right after an anonymous register would tie the two together)."""
    calls: list[int] = []
    server = TestServer(create_gfs_app(_gfs_config(tmp_dir)))
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            url = str(server.make_url("")).rstrip("/")
            tickets = await _ticket_cache(server.app, session, url, calls=calls)
            assert calls == [4]
            svc = server.app[gfs_mailbox_tickets_key]
            # The GFS forgets every key it accepted (as after two epochs).
            svc._keys.clear()
            svc._current = None
            await server.app[gfs_db_key].enqueue("DELETE FROM gfs_ticket_issuer_keys")
            svc._miss_at = -1e9
            keys = derive_mailbox_keys(ROOT, GFS_ID, "after-rotation", 0)
            with pytest.raises(MailboxRegisterError) as err:
                await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
            assert (err.value.status, err.value.error) == (403, "stale_ticket_key")
            assert len(tickets) == 0
            assert calls == [4]  # no fetch next to the refused register
            with pytest.raises(MailboxTicketError) as empty:
                await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
            assert empty.value.error == "no_ticket"
            assert calls == [4]
            # Later, on its own schedule, the household re-reads /gfs/info.
            fresh = await _ticket_cache(server.app, session, url)
            assert await register_mailbox(session, url, keys, GFS_ID, tickets=fresh)
    finally:
        await server.close()


async def test_drop_key_keeps_tickets_under_other_keys():
    cache = MailboxTicketCache(None, batch=4)  # type: ignore[arg-type]
    old = b"\x00\x02" + bytes(64) + b"o" * 32 + bytes(256)
    new = b"\x00\x02" + bytes(64) + b"n" * 32 + bytes(256)
    cache._tickets = [old, new, old]
    cache.drop_key(b"o" * 32)
    assert cache._tickets == [new]


def _gfs_config(tmp_dir, **kw) -> GfsConfig:
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id=GFS_ID,
        cluster_enabled=False,
        cluster_node_id=GFS_ID,
        cluster_peers=(),
        **kw,
    )


async def test_the_daily_batch_is_bounded_per_household(tmp_dir):
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id=GFS_ID,
        cluster_enabled=False,
        cluster_node_id=GFS_ID,
        cluster_peers=(),
        envelope_mailbox=EnvelopeMailboxConfig(mailbox_tickets_per_day=3),
    )
    server = TestServer(create_gfs_app(cfg))
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            url = str(server.make_url("")).rstrip("/")
            tickets = await _ticket_cache(
                server.app, session, url, batch=8, prefetch=False
            )
            assert await tickets.refill() == 3  # granted what is left
            tickets.clear()
            with pytest.raises(MailboxTicketError) as err:
                await tickets.refill()
            assert (err.value.status, err.value.error) == (429, "ticket_quota")
    finally:
        await server.close()


async def test_an_unregistered_household_gets_no_tickets(gfs):
    _server, session, url, _tickets = gfs
    issuer = await _issuer(session, url)
    stranger = bytes([9]) * 32
    with pytest.raises(MailboxTicketError) as err:
        await fetch_tickets(
            session,
            url,
            issuer,
            1,
            instance_id=derive_instance_id(ed25519_public_key(stranger)),
            signing_key=stranger,
        )
    assert err.value.status == 403


def test_the_issuer_capability_is_checked():
    key = pp.generate_issuer_key().public_key()
    good = {
        "suite": pp.TICKET_SUITE_PP_BLINDRSA,
        "issuer_key_id": b64url_encode(pp.token_key_id(key)),
        "public_key": b64url_encode(pp.encode_public_key(key)),
        "per_day": 50,
    }
    assert TicketIssuer.from_capability(good).per_day == 50
    for bad in (
        None,
        {**good, "suite": "privacypass-blindrsa4096"},
        {k: v for k, v in good.items() if k != "suite"},
        {**good, "issuer_key_id": b64url_encode(bytes(32))},
        {**good, "public_key": "AAAA"},
        {**good, "per_day": "50"},
    ):
        with pytest.raises(MailboxTicketError):
            TicketIssuer.from_capability(bad)


def test_a_register_body_carries_the_ticket_and_its_suite():
    keys = derive_mailbox_keys(ROOT, GFS_ID, "s", 0)
    body = build_register_body(keys, GFS_ID, ticket=b"t" * 354)
    assert body["ticket_suite"] == pp.TICKET_SUITE_PP_BLINDRSA
    assert body["ticket"] == b64url_encode(b"t" * 354)
    assert "ticket" not in build_register_body(keys, GFS_ID)
