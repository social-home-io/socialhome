"""Household mailbox helpers: key derivation, and register + claim end to
end against the real GFS app (real SQLite, real aiohttp server)."""

from __future__ import annotations

import asyncio
import contextlib

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from socialhome.crypto import INSTANCE_ID_RE, x25519_exchange
from socialhome.domain.gfs_mailbox import derive_mailbox_id
from socialhome.domain.relay_media_frame import (
    VALID_SEALED_LENGTHS,
    build_relay_media_body,
)
from socialhome.federation.gfs_mailbox import (
    MailboxClaim,
    MailboxClaimError,
    MailboxRegisterError,
    build_register_body,
    derive_mailbox_keys,
    new_mailbox_root,
    register_mailbox,
)
from socialhome.global_server.app_keys import (
    gfs_envelope_relay_key,
    gfs_mailbox_repo_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.server import create_gfs_app

ROOT = bytes(range(32))
GFS_ID = "gfs-node-a"
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
        yield server, session, str(server.make_url("")).rstrip("/")
    await server.close()


async def _settle(app) -> None:
    relay = app[gfs_envelope_relay_key]
    for _ in range(500):
        if relay.in_flight == 0 and not relay._media_inflight:
            return
        await asyncio.sleep(0.01)


async def test_register_claim_and_receive_queued_and_live_frames(gfs):
    server, session, url = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)
    assert await register_mailbox(session, url, keys, GFS_ID) == keys.mailbox_id
    # Idempotent — a household may re-register at every start.
    assert await register_mailbox(session, url, keys, GFS_ID) == keys.mailbox_id

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
    _server, session, url = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "never-registered", 0)
    with pytest.raises(MailboxClaimError, match="4401"):
        async with MailboxClaim(session, url, keys, GFS_ID):
            pass


async def test_a_claim_signed_for_another_gfs_id_is_refused(gfs):
    """The claim is bound to the GFS id the household pinned; a server that
    does not answer to it refuses (4401) — the id the challenge names is
    never what gets signed."""
    _server, session, url = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)
    await register_mailbox(session, url, keys, GFS_ID)
    with pytest.raises(MailboxClaimError, match="4401"):
        async with MailboxClaim(session, url, keys, "some-other-gfs"):
            pass


async def test_register_refusal_raises_with_the_error_code(gfs):
    _server, session, url = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)
    with pytest.raises(MailboxRegisterError) as err:
        await register_mailbox(session, url, keys, "some-other-gfs")
    assert (err.value.status, err.value.error) == (403, "wrong_gfs")


async def test_iterating_a_claim_ends_when_the_server_closes(gfs):
    server, session, url = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "space-1", 0)
    await register_mailbox(session, url, keys, GFS_ID)
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
            keys = derive_mailbox_keys(ROOT, GFS_ID, "space-9", 0)
            await register_mailbox(session, url, keys, GFS_ID)
            async with MailboxClaim(session, url, keys, GFS_ID) as claim:
                assert claim is not None
    finally:
        await server.close()


async def test_a_claim_entered_by_hand_closes_with_aclosing(gfs):
    _server, session, url = gfs
    keys = derive_mailbox_keys(ROOT, GFS_ID, "space-a", 0)
    await register_mailbox(session, url, keys, GFS_ID)
    claim = MailboxClaim(session, url, keys, GFS_ID)
    await claim.__aenter__()
    async with contextlib.aclosing(claim):
        assert claim._ws is not None
    assert claim._ws is None
