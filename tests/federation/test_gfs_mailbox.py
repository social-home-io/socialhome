"""Household mailbox helpers: key derivation, and register + claim end to
end against the real GFS app (real SQLite, real aiohttp server)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import aiohttp
import orjson
import pytest
from aiohttp.client_reqrep import ConnectionKey
from aiohttp.test_utils import TestServer

from socialhome import privacy_pass as pp
from socialhome.crypto import (
    INSTANCE_ID_RE,
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
    verify_ed25519,
    x25519_exchange,
)
from socialhome.domain.gfs_mailbox import (
    MAILBOX_CLOSE_AUTH_FAILED,
    MAILBOX_TICKET_CHALLENGE,
    TicketKeyPin,
    derive_mailbox_id,
    unregister_signed_bytes,
)
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
    TicketKeyGuard,
    build_register_body,
    build_unregister_body,
    derive_mailbox_keys,
    fetch_tickets,
    new_mailbox_root,
    read_ticket_issuer,
    register_mailbox,
    unregister_mailbox,
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
from socialhome.global_server.routes.ws import WS_CLOSE_AUTH_FAILED
from socialhome.global_server.server import create_gfs_app

ROOT = bytes(range(32))
GFS_ID = "gfs-node-a"
#: The household's identity key (registered and active on the test GFS).
HOME_SEED = bytes([5]) * 32
HOME_ID = derive_instance_id(ed25519_public_key(HOME_SEED))
SEALED = {"kem_suite": "x25519", "eph_pk": "ZXBo", "ciphertext": "Y2lwaGVy"}


# ── Derivation ───────────────────────────────────────────────────────────


def test_keys_are_deterministic_per_gfs_and_gen():
    a = derive_mailbox_keys(ROOT, GFS_ID, 853190)
    assert a == derive_mailbox_keys(ROOT, GFS_ID, 853190)
    assert a.mailbox_id == derive_mailbox_id(a.sign_public)
    assert INSTANCE_ID_RE.fullmatch(a.mailbox_id)
    others = [
        derive_mailbox_keys(ROOT, GFS_ID, 176610),
        derive_mailbox_keys(ROOT, GFS_ID, 853191),
        derive_mailbox_keys(b"\x09" * 32, GFS_ID, 853190),
        # The same generation at another GFS: unrelated, so two GFSs can't
        # match ids to link a household.
        derive_mailbox_keys(ROOT, "gfs-node-b", 853190),
    ]
    for other in others:
        assert other.mailbox_id != a.mailbox_id
        assert other.kem_public != a.kem_public
        assert other.sign_public != a.sign_public
    assert a.kem_private != a.sign_seed


def test_keys_name_no_space_or_peer():
    """v_89: ONE mailbox per (GFS, gen) serves every space the household
    reaches there — the keys depend on the GFS and the generation only."""
    keys = derive_mailbox_keys(ROOT, GFS_ID, 7)
    assert not hasattr(keys, "space_id")
    with pytest.raises(TypeError):
        derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)  # type: ignore[call-arg]


def test_a_mailbox_root_is_a_fresh_32_byte_secret():
    one, two = new_mailbox_root(), new_mailbox_root()
    assert len(one) == len(two) == 32 and one != two
    assert derive_mailbox_keys(one, GFS_ID, 553390) != derive_mailbox_keys(
        two, GFS_ID, 553390
    )


def test_kem_key_is_a_working_x25519_pair():
    a = derive_mailbox_keys(ROOT, GFS_ID, 853190)
    b = derive_mailbox_keys(ROOT, GFS_ID, 176610)
    assert x25519_exchange(a.kem_private, b.kem_public) == x25519_exchange(
        b.kem_private, a.kem_public
    )


def test_repr_never_shows_the_secrets():
    keys = derive_mailbox_keys(ROOT, GFS_ID, 853190)
    text = repr(keys)
    assert keys.sign_seed.hex() not in text and str(keys.sign_seed) not in text
    assert keys.mailbox_id not in text


@pytest.mark.parametrize(
    ("seed", "gfs", "gen"),
    [
        (b"short", GFS_ID, 0),
        (ROOT, "", 0),
        (ROOT, "a|b", 0),
        (ROOT, GFS_ID, -1),
        (ROOT, GFS_ID, True),
    ],
)
def test_derivation_refuses_bad_input(seed, gfs, gen):
    with pytest.raises(ValueError):
        derive_mailbox_keys(seed, gfs, gen)


def test_register_body_carries_the_suite_tag():
    body = build_register_body(derive_mailbox_keys(ROOT, GFS_ID, 553390), GFS_ID)
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


async def _gfs_pk(session, url) -> str:
    """The GFS public key, as the household pinned it when it registered."""
    async with session.get(url + "/gfs/info") as resp:
        return (await resp.json())["public_key"]


async def _issuer(session, url) -> TicketIssuer:
    """The issuer from the GFS's SIGNED ``/gfs/info`` (as a household reads
    it: the capability block's signature verified against the pinned key)."""
    return await read_ticket_issuer(
        session, url, gfs_public_key=await _gfs_pk(session, url)
    )


def _guard(session, url, pk: str, **kw) -> TicketKeyGuard:
    """The household's key guard: the anonymous view is a second, separate
    read of ``/gfs/info`` (here over the same test session)."""

    async def anon_view() -> TicketIssuer:
        return await read_ticket_issuer(session, url, gfs_public_key=pk)

    return TicketKeyGuard(anon_view, **kw)


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
    guard = _guard(session, url, await _gfs_pk(session, url))
    await guard.refresh()  # the anonymous read, on its own schedule

    async def fetch(count: int) -> list[bytes]:
        if calls is not None:
            calls.append(count)
        return await fetch_tickets(
            session,
            url,
            issuer,
            count,
            instance_id=HOME_ID,
            signing_key=HOME_SEED,
            guard=guard,
        )

    cache = MailboxTicketCache(fetch, batch=batch, guard=guard)
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
    keys = derive_mailbox_keys(ROOT, GFS_ID, 853190)
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
    keys = derive_mailbox_keys(ROOT, GFS_ID, 621780)
    with pytest.raises(MailboxClaimError, match="4401") as info:
        async with MailboxClaim(session, url, keys, GFS_ID):
            pass
    # v_88 recovery reads this: the GFS does not know the mailbox (any more).
    assert info.value.mailbox_unknown
    assert info.value.close_code == WS_CLOSE_AUTH_FAILED == MAILBOX_CLOSE_AUTH_FAILED


async def test_a_claim_signed_for_another_gfs_id_is_refused(gfs):
    """The claim is bound to the GFS id the household pinned; a server that
    does not answer to it refuses (4401) — the id the challenge names is
    never what gets signed."""
    _server, session, url, tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, 853190)
    await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
    with pytest.raises(MailboxClaimError, match="4401"):
        async with MailboxClaim(session, url, keys, "some-other-gfs"):
            pass


async def test_register_refusal_raises_with_the_error_code(gfs):
    _server, session, url, tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, 853190)
    with pytest.raises(MailboxRegisterError) as err:
        await register_mailbox(session, url, keys, "some-other-gfs", tickets=tickets)
    assert (err.value.status, err.value.error) == (403, "wrong_gfs")


async def test_iterating_a_claim_ends_when_the_server_closes(gfs):
    server, session, url, tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, 853190)
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
            keys = derive_mailbox_keys(ROOT, GFS_ID, 549970)
            await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
            async with MailboxClaim(session, url, keys, GFS_ID) as claim:
                assert claim is not None
    finally:
        await server.close()


async def test_a_claim_entered_by_hand_closes_with_aclosing(gfs):
    _server, session, url, tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, 559870)
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
    for gen in (1, 2):
        keys = derive_mailbox_keys(ROOT, GFS_ID, gen)
        await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
    assert len(tickets) == 2  # one each; no fetch in between
    assert "<2 tickets>" in repr(tickets)


async def test_a_new_mailbox_without_a_ticket_is_refused(gfs):
    _server, session, url, _tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, 190590)
    with pytest.raises(MailboxRegisterError) as err:
        await register_mailbox(session, url, keys, GFS_ID)
    assert (err.value.status, err.value.error) == (403, "ticket_required")


async def test_a_spent_ticket_is_replaced_once(gfs):
    """A cached ticket that was spent already (here: by hand) is dropped and
    the register retried with the next one."""
    _server, session, url, tickets = gfs
    await tickets.refill()
    spent = tickets._tickets[-1]
    first = derive_mailbox_keys(ROOT, GFS_ID, 559070)
    await register_mailbox(session, url, first, GFS_ID, tickets=tickets)
    tickets._tickets.append(spent)
    second = derive_mailbox_keys(ROOT, GFS_ID, 386810)
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
            # Two epochs on for the household too (its guard takes a new key
            # only in a later epoch than the one it pinned).
            tickets._guard._clock = lambda: time.time() + 61 * 86_400
            keys = derive_mailbox_keys(ROOT, GFS_ID, 657950)
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
    cache = MailboxTicketCache(None, batch=4, guard=None)  # type: ignore[arg-type]
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
    guard = _guard(session, url, await _gfs_pk(session, url))
    await guard.refresh()
    with pytest.raises(MailboxTicketError) as err:
        await fetch_tickets(
            session,
            url,
            issuer,
            1,
            instance_id=derive_instance_id(ed25519_public_key(stranger)),
            signing_key=stranger,
            guard=guard,
        )
    assert err.value.status == 403


def test_the_issuer_capability_is_checked():
    key = pp.generate_issuer_key().public_key()
    good = {
        "suite": pp.TICKET_SUITE_PP_BLINDRSA,
        "issuer_key_id": b64url_encode(pp.token_key_id(key)),
        "public_key": b64url_encode(pp.encode_public_key(key)),
        "per_day": 50,
        "epoch_days": 30,
    }
    assert TicketIssuer.from_capability(good).per_day == 50
    for bad in (
        None,
        {**good, "suite": "privacypass-blindrsa4096"},
        {k: v for k, v in good.items() if k != "suite"},
        {**good, "issuer_key_id": b64url_encode(bytes(32))},
        {**good, "public_key": "AAAA"},
        {**good, "per_day": "50"},
        {k: v for k, v in good.items() if k != "epoch_days"},
        {**good, "epoch_days": True},
    ):
        with pytest.raises(MailboxTicketError):
            TicketIssuer.from_capability(bad)


def test_a_register_body_carries_the_ticket_and_its_suite():
    keys = derive_mailbox_keys(ROOT, GFS_ID, 553390)
    body = build_register_body(keys, GFS_ID, ticket=b"t" * 354)
    assert body["ticket_suite"] == pp.TICKET_SUITE_PP_BLINDRSA
    assert body["ticket"] == b64url_encode(b"t" * 354)
    assert "ticket" not in build_register_body(keys, GFS_ID)


# ── Key consistency, refill timing, ticket waste ────────────────────────

DAY = 86_400


def _local_issuer(epoch_days: int = 30) -> tuple[TicketIssuer, object]:
    """A local issuer key: the parsed capability and the private key."""
    key = pp.generate_issuer_key()
    cap = {
        "suite": pp.TICKET_SUITE_PP_BLINDRSA,
        "issuer_key_id": b64url_encode(pp.token_key_id(key.public_key())),
        "public_key": b64url_encode(pp.encode_public_key(key.public_key())),
        "per_day": 50,
        "epoch_days": epoch_days,
    }
    return TicketIssuer.from_capability(cap), key


def _local_ticket(issuer: TicketIssuer, key) -> bytes:
    request, pending = pp.token_request(issuer.public_key, MAILBOX_TICKET_CHALLENGE)
    _t, blinded = pp.parse_token_request(request)
    return pp.finalize_token(
        issuer.public_key, pending, pp.blind_sign(key, blinded)
    ).encode()


def test_a_capability_with_a_short_epoch_is_refused():
    """A short epoch lets a GFS rotate keys to tag households, and shrinks
    the set of households a ticket hides among."""
    issuer, _key = _local_issuer()
    cap = {
        "suite": pp.TICKET_SUITE_PP_BLINDRSA,
        "issuer_key_id": b64url_encode(issuer.key_id),
        "public_key": b64url_encode(pp.encode_public_key(issuer.public_key)),
        "per_day": 50,
        "epoch_days": 1,
    }
    with pytest.raises(MailboxTicketError) as err:
        TicketIssuer.from_capability(cap)
    assert err.value.error == "ticket_epoch_too_short"


async def test_the_key_pin_refuses_an_early_rotation():
    """At most one new issuer key per epoch: a GFS can't hand this household
    a fresh key whenever it likes (and so tag its tickets)."""
    a, _ = _local_issuer()
    b, _ = _local_issuer()
    c, _ = _local_issuer()
    now = [float(30 * DAY * 1000 + DAY)]  # one day into an epoch
    views = [a]

    async def anon_view() -> TicketIssuer:
        return views[0]

    guard = TicketKeyGuard(anon_view, clock=lambda: now[0])
    await guard.refresh()
    await guard.confirm_issuer(a)
    assert guard.pin == TicketKeyPin(a.key_id, int(now[0]), 30, None)
    now[0] += 5 * DAY  # same epoch: a new key is refused
    views[0] = b
    with pytest.raises(MailboxTicketError) as err:
        await guard.refresh()
    assert err.value.error == "ticket_key_inconsistent"
    with pytest.raises(MailboxTicketError):  # the cached view is still a
        await guard.confirm_issuer(b)
    now[0] += 25 * DAY  # the next epoch: the rotation is taken
    await guard.refresh()
    await guard.confirm_issuer(b)
    assert guard.pin is not None and guard.pin.previous_key_id == a.key_id
    views[0] = c  # …but only one per epoch
    with pytest.raises(MailboxTicketError):
        await guard.refresh()
    views[0] = a  # the previous key (a node behind at the boundary) is fine
    await guard.refresh()
    assert guard.pin.key_id == b.key_id


async def test_a_restored_pin_is_kept(tmp_dir):
    a, _ = _local_issuer()
    b, _ = _local_issuer()
    now = 30 * DAY * 1000 + DAY

    async def anon_view() -> TicketIssuer:
        return b

    guard = TicketKeyGuard(
        anon_view, pin=TicketKeyPin(a.key_id, now, 30, None), clock=lambda: now
    )
    with pytest.raises(MailboxTicketError):
        await guard.refresh()


async def test_per_household_keys_are_refused(gfs, caplog):
    """The GFS shows this household (identified view) a key nobody else is
    shown (the anonymous view): no ticket is blinded under it, and the
    WARNING names no key and no household."""
    server, session, url, _tickets = gfs
    mine = await _issuer(session, url)
    everyone, _ = _local_issuer()

    async def anon_view() -> TicketIssuer:
        return everyone

    caplog.set_level(logging.DEBUG)
    guard = TicketKeyGuard(anon_view)
    await guard.refresh()
    with pytest.raises(MailboxTicketError) as err:
        await fetch_tickets(
            session,
            url,
            mine,
            2,
            instance_id=HOME_ID,
            signing_key=HOME_SEED,
            guard=guard,
        )
    assert err.value.error == "ticket_key_inconsistent"
    issued = await server.app[gfs_db_key].fetchall(
        "SELECT issued FROM gfs_ticket_issuance"
    )
    assert [r["issued"] for r in issued] == [4]  # only the fixture's batch
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    for rec in warnings:
        text = rec.getMessage()
        for secret in (mine.key_id, everyone.key_id):
            assert b64url_encode(secret) not in text and secret.hex() not in text
        assert HOME_ID not in text


async def test_a_ticket_is_not_redeemed_when_the_anonymous_view_moved(gfs):
    """Before redeeming, the anonymous view must still show a key this
    household admitted: a GFS that now shows everyone another key (inside
    the epoch) gets no register, and the ticket is kept."""
    server, session, url, tickets = gfs
    guard = tickets._guard
    other, _ = _local_issuer()

    async def anon_view() -> TicketIssuer:
        return other

    guard._anon_view = anon_view
    keys = derive_mailbox_keys(ROOT, GFS_ID, 916050)
    with pytest.raises(MailboxTicketError) as err:
        await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
    assert err.value.error == "ticket_key_inconsistent"
    assert len(tickets) == 4  # the ticket was never shown: it is kept
    assert await server.app[gfs_mailbox_repo_key].get(keys.mailbox_id) is None


async def test_reregistering_an_existing_mailbox_spends_no_ticket(gfs):
    _server, session, url, tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, 255960)
    await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
    assert len(tickets) == 3
    for _ in range(2):
        await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
    assert len(tickets) == 3  # no ticket shown for a mailbox that exists


class _Resp:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return orjson.dumps(self._body)


class _FlakySession:
    """Answers ``ticket_required`` to the ticketless try, then can't
    connect: the ticketed request never reaches the GFS."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []

    @contextlib.asynccontextmanager
    async def post(self, url, *, json, timeout):
        self.bodies.append(json)
        if "ticket" not in json:
            yield _Resp(403, {"error": "ticket_required"})
            return
        raise aiohttp.ClientConnectorError(
            ConnectionKey("gfs.test", 443, True, True, None, None, None),
            OSError(111, "refused"),
        )
        yield  # pragma: no cover


async def test_a_ticket_that_never_reached_the_gfs_is_put_back():
    issuer, key = _local_issuer()

    async def anon_view() -> TicketIssuer:
        return issuer

    guard = TicketKeyGuard(anon_view)
    await guard.refresh()
    cache = MailboxTicketCache(None, batch=4, guard=guard)  # type: ignore[arg-type]
    cache._tickets = [_local_ticket(issuer, key)]
    session = _FlakySession()
    keys = derive_mailbox_keys(ROOT, GFS_ID, 381300)
    with pytest.raises(aiohttp.ClientConnectorError):
        await register_mailbox(
            session,  # type: ignore[arg-type]
            "https://gfs.test",
            keys,
            GFS_ID,
            tickets=cache,
        )
    assert len(cache) == 1  # put back: the GFS never saw it
    assert ["ticket" in b for b in session.bodies] == [False, True]


async def test_refills_run_at_jittered_times_and_never_next_to_a_register():
    """The cache refills on its own jittered schedule, and skips a slot
    that falls right after a register (a ticket taken)."""
    issuer, key = _local_issuer()
    calls: list[int] = []

    async def fetch(count: int) -> list[bytes]:
        calls.append(count)
        return [_local_ticket(issuer, key)] * count

    class _Rng:
        def __init__(self) -> None:
            self.asked: list[tuple[float, float]] = []

        def uniform(self, lo: float, hi: float) -> float:
            self.asked.append((lo, hi))
            return lo

    rng = _Rng()
    cache = MailboxTicketCache(
        fetch, batch=2, guard=None, period=0.05, quiet=3600.0, rng=rng
    )  # type: ignore[arg-type]
    cache._tickets = [b"x"]
    cache.take()  # a register just happened
    await cache.start()
    await asyncio.sleep(0.2)
    await cache.stop()
    assert calls == []  # every slot was too close to the register
    assert rng.asked and all(lo < hi for lo, hi in rng.asked)
    quiet = MailboxTicketCache(
        fetch, batch=2, guard=None, period=0.05, quiet=0.0, rng=rng
    )  # type: ignore[arg-type]
    await quiet.start()
    await asyncio.sleep(0.2)
    await quiet.stop()
    assert calls == [2] and len(quiet) == 2


async def test_the_issuer_is_read_only_under_the_pinned_gfs_key(gfs):
    _server, session, url, _tickets = gfs
    other = b64url_encode(bytes(32)).rstrip("=")
    with pytest.raises(MailboxTicketError) as err:
        await read_ticket_issuer(session, url, gfs_public_key=other)
    assert err.value.error == "invalid_ticket_capability"
    with pytest.raises(MailboxTicketError) as err:
        await read_ticket_issuer(session, "http://127.0.0.1:1", gfs_public_key=other)
    assert err.value.error == "gfs_info_unreachable"


async def test_a_ticket_under_an_unadmitted_key_is_refused():
    issuer, key = _local_issuer()
    stranger, skey = _local_issuer()

    async def anon_view() -> TicketIssuer:
        return issuer

    guard = TicketKeyGuard(anon_view)
    with pytest.raises(MailboxTicketError) as err:
        await guard.confirm_ticket(_local_ticket(stranger, skey))
    assert err.value.error == "ticket_key_not_admitted"
    await guard.confirm_ticket(_local_ticket(issuer, key))


async def test_the_identified_fetch_never_reads_the_anonymous_view(gfs):
    """The anonymous ``/gfs/info`` read runs on its own schedule and is
    cached: the identified ticket fetch compares against the cache and does
    not read anonymously right before it (same address, ms apart)."""
    _server, session, url, _tickets = gfs
    issuer = await _issuer(session, url)
    reads: list[int] = []

    async def anon_view() -> TicketIssuer:
        reads.append(1)
        return issuer

    guard = TicketKeyGuard(anon_view)
    with pytest.raises(MailboxTicketError) as err:  # nothing read yet
        await guard.confirm_issuer(issuer)
    assert err.value.error == "ticket_key_unconfirmed"
    assert reads == []
    await guard.refresh()
    assert reads == [1]
    got = await fetch_tickets(
        session, url, issuer, 1, instance_id=HOME_ID, signing_key=HOME_SEED, guard=guard
    )
    assert len(got) == 1 and reads == [1]  # no anonymous read next to the fetch


async def test_the_anonymous_view_refreshes_on_a_jittered_schedule():
    issuer, _key = _local_issuer()
    reads: list[int] = []

    async def anon_view() -> TicketIssuer:
        reads.append(1)
        if len(reads) == 1:
            raise aiohttp.ClientConnectionError("down")
        return issuer

    class _Rng:
        def uniform(self, lo: float, hi: float) -> float:
            assert lo < hi
            return lo

    guard = TicketKeyGuard(anon_view, period=0.05, rng=_Rng())
    await guard.start()
    await asyncio.sleep(0.25)
    await guard.stop()
    assert len(reads) >= 2  # an outage does not end the loop
    await guard.confirm_issuer(issuer)  # the later read was cached


async def test_a_network_error_while_fetching_is_a_ticket_error():
    issuer, _key = _local_issuer()

    async def anon_view() -> TicketIssuer:
        return issuer

    guard = TicketKeyGuard(anon_view)
    await guard.refresh()
    async with aiohttp.ClientSession() as session:
        with pytest.raises(MailboxTicketError) as err:
            await fetch_tickets(
                session,
                "http://127.0.0.1:1",
                issuer,
                1,
                instance_id=HOME_ID,
                signing_key=HOME_SEED,
                guard=guard,
            )
    assert err.value.error == "gfs_unreachable"


async def test_the_refill_loop_survives_an_outage():
    """One GFS outage must not end the refill task for good."""
    issuer, key = _local_issuer()
    calls: list[int] = []

    async def fetch(count: int) -> list[bytes]:
        calls.append(count)
        if len(calls) == 1:
            raise aiohttp.ClientConnectionError("down")
        if len(calls) == 2:
            raise TimeoutError
        return [_local_ticket(issuer, key)] * count

    class _Rng:
        def uniform(self, lo: float, hi: float) -> float:
            return lo

    cache = MailboxTicketCache(
        fetch, batch=2, guard=None, period=0.05, quiet=0.0, rng=_Rng()
    )  # type: ignore[arg-type]
    await cache.start()
    await asyncio.sleep(0.3)
    await cache.stop()
    assert len(calls) >= 3 and len(cache) == 2


async def test_a_ticket_under_a_retired_key_is_dropped_not_put_back(gfs):
    """A ticket whose key the household no longer admits is dropped (with
    every ticket under that key) — put back, it would come out of the cache
    again next time and block every register."""
    _server, session, url, tickets = gfs
    stranger, skey = _local_issuer()
    tickets._tickets.append(_local_ticket(stranger, skey))  # taken first
    keys = derive_mailbox_keys(ROOT, GFS_ID, 392240)
    with pytest.raises(MailboxTicketError) as err:
        await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
    assert err.value.error == "ticket_key_not_admitted"
    assert len(tickets) == 4  # only the dead one went
    assert await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
    assert len(tickets) == 3


# ── Unregister (redesign PR 2) ───────────────────────────────────────────


def test_unregister_body_signs_under_the_mailbox_key():
    keys = derive_mailbox_keys(ROOT, GFS_ID, 553390)
    body = build_unregister_body(keys, GFS_ID, now=1_700_000_000, nonce="n" * 22)
    assert set(body) == {
        "sig_suite",
        "mailbox_id",
        "gfs_instance_id",
        "ts",
        "nonce",
        "sig",
    }
    assert body["sig_suite"] == "ed25519"
    assert body["mailbox_id"] == keys.mailbox_id
    assert (body["gfs_instance_id"], body["ts"], body["nonce"]) == (
        GFS_ID,
        1_700_000_000,
        "n" * 22,
    )
    signed = unregister_signed_bytes(GFS_ID, keys.mailbox_id, 1_700_000_000, "n" * 22)
    assert verify_ed25519(keys.sign_public, signed, b64url_decode(body["sig"]))
    # A fresh nonce each time by default.
    assert (
        build_unregister_body(keys, GFS_ID)["nonce"]
        != build_unregister_body(keys, GFS_ID)["nonce"]
    )


async def test_unregister_mailbox_drops_it_at_the_gfs(gfs):
    server, session, url, tickets = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, 615420)
    await register_mailbox(session, url, keys, GFS_ID, tickets=tickets)
    assert await server.app[gfs_mailbox_repo_key].get(keys.mailbox_id) is not None
    assert await unregister_mailbox(session, url, keys, GFS_ID) is True
    assert await server.app[gfs_mailbox_repo_key].get(keys.mailbox_id) is None


async def test_unregister_mailbox_is_false_on_a_gfs_without_the_route(tmp_dir):
    """An older GFS (or one with mailboxes off) answers 404: False."""
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id=GFS_ID,
        cluster_enabled=False,
        cluster_node_id=GFS_ID,
        cluster_peers=(),
        envelope_mailbox=EnvelopeMailboxConfig(enabled=False),
    )
    server = TestServer(create_gfs_app(cfg))
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            url = str(server.make_url("")).rstrip("/")
            keys = derive_mailbox_keys(ROOT, GFS_ID, 553390)
            assert await unregister_mailbox(session, url, keys, GFS_ID) is False
    finally:
        await server.close()


class _DeadSession:
    """Can't connect at all."""

    @contextlib.asynccontextmanager
    async def post(self, url, *, json, timeout):
        raise aiohttp.ClientConnectorError(
            ConnectionKey("gfs.test", 443, True, True, None, None, None),
            OSError(111, "refused"),
        )
        yield  # pragma: no cover


class _SlowSession:
    @contextlib.asynccontextmanager
    async def post(self, url, *, json, timeout):
        raise TimeoutError
        yield  # pragma: no cover


@pytest.mark.parametrize("session", [_DeadSession(), _SlowSession()])
async def test_unregister_mailbox_is_false_on_a_network_fault(session):
    keys = derive_mailbox_keys(ROOT, GFS_ID, 553390)
    assert (
        await unregister_mailbox(
            session,  # type: ignore[arg-type]
            "https://gfs.test",
            keys,
            GFS_ID,
        )
        is False
    )
