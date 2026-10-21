"""Follower side of encrypted public moments (v_62): a sealed moment key is
taken only from the followed author's household, only when sealed to us."""

from __future__ import annotations

import os

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.federation.keywrap_seal import seal_to_keywrap
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.moment_public_seal import (
    MAX_RATCHET_STEPS,
    key_plaintext,
    ratchet,
    sign_wire,
    verify_wire,
)
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicFollowRepo,
    SqliteMomentPublicKeyRepo,
)
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.moment_public_key_inbound import MomentPublicKeyInbound

AUTHOR = generate_identity_keypair()
AUTHOR_INSTANCE = derive_instance_id(AUTHOR.public_key)
OWN = generate_identity_keypair()
OWN_INSTANCE = derive_instance_id(OWN.public_key)
WRAP = generate_x25519_keypair()
KEY = os.urandom(32)


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _Resp(202)


@pytest.fixture
async def env(db):
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u-fan','fan','Fan','active')"
    )
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
        " inbox_url, status, paired_at) VALUES('g1', 'g1', 'G', 'ff',"
        " 'https://g1.example', 'active', datetime('now'))"
    )
    follows = SqliteMomentPublicFollowRepo(db)
    await follows.upsert(
        follower_user_id="u-fan",
        followed_user_id="u-alice",
        gfs_id="g1",
        followed_instance_pk=AUTHOR.public_key.hex(),
        followed_username="alice",
        followed_display_name="Alice",
    )
    keys = SqliteMomentPublicKeyRepo(db)
    svc = MomentPublicKeyInbound(
        key_repo=keys,
        follow_repo=follows,
        user_repo=SqliteUserRepo(db),
        gfs_repo=SqliteGfsConnectionRepo(db),
    )
    session = _Session()
    svc.attach_session(session)
    svc.attach_identity(
        own_instance_id=OWN_INSTANCE,
        signing_key=OWN.private_key,
        key_manager=KeyManager(os.urandom(32)),
        keywrap_private_key=WRAP.private_key,
    )
    return {"svc": svc, "keys": keys, "session": session, "db": db, "follows": follows}


def _delivery(
    *,
    gen: int = 1_700_000_000,
    step: int = 0,
    author: str = "u-alice",
    to: str = OWN_INSTANCE,
    sealed_to: bytes = WRAP.public_key,
    signer=AUTHOR,
    instance_id: str = AUTHOR_INSTANCE,
    plaintext_to: str | None = None,
    key: bytes = KEY,
    key_suite: str = "aesgcm-256",
) -> dict:
    return sign_wire(
        signer.private_key,
        {
            "author_user_id": author,
            "instance_id": instance_id,
            "to": to,
            "key_gen": gen,
            "key_step": step,
            "key_suite": key_suite,
            "sealed": seal_to_keywrap(
                recipient_keywrap_pub=sealed_to,
                plaintext=key_plaintext(
                    author_user_id=author,
                    key_gen=gen,
                    key_step=step,
                    to=plaintext_to or to,
                    key=key,
                ),
            ),
        },
    )


GEN = 1_700_000_000


async def test_a_delivery_from_the_author_imports_the_key(env):
    assert await env["svc"].import_delivery(_delivery(), gfs_id="g1") == GEN
    assert await env["svc"].key_for("u-alice", GEN, 0) == KEY
    assert await env["svc"].key_for("u-alice", 1, 0) is None
    row = await env["keys"].latest("u-alice", own=False)
    # KEK-sealed at rest, never the raw key.
    assert row is not None and KEY.hex() not in row.key_sealed


async def test_a_later_step_ratchets_forward_and_forgets_the_older_one(env):
    """Forward secrecy at rest: after a moment of step 3 the held key is the
    step-3 key — step 0 can't be recomputed here any more."""
    await env["svc"].import_delivery(_delivery(), gfs_id="g1")
    assert await env["svc"].key_for("u-alice", GEN, 3) == ratchet(KEY, 3)
    assert (await env["keys"].get("u-alice", GEN, own=False)).key_step == 3
    assert await env["svc"].key_for("u-alice", GEN, 0) is None
    assert await env["svc"].key_for("u-alice", GEN, 3 + MAX_RATCHET_STEPS + 1) is None


async def test_a_delivery_only_ever_moves_forward(env):
    await env["svc"].import_delivery(
        _delivery(step=2, key=ratchet(KEY, 2)), gfs_id="g1"
    )
    # The same step again, an older step, a swapped key: nothing changes.
    assert await env["svc"].import_delivery(_delivery(step=2), gfs_id="g1") is None
    assert await env["svc"].import_delivery(_delivery(step=1), gfs_id="g1") is None
    assert await env["svc"].key_for("u-alice", GEN, 2) == ratchet(KEY, 2)
    # A later step (after a long time offline) is taken.
    assert (
        await env["svc"].import_delivery(
            _delivery(step=5, key=ratchet(KEY, 5)), gfs_id="g1"
        )
        == GEN
    )


async def test_only_the_two_newest_generations_are_kept(env):
    for gen in (1, 2, 3):
        await env["svc"].import_delivery(_delivery(gen=gen), gfs_id="g1")
    assert await env["svc"].key_for("u-alice", 1, 0) is None
    assert await env["svc"].key_for("u-alice", 3, 0) == KEY


@pytest.mark.parametrize(
    "delivery",
    [
        # A GFS (or anyone) planting its own key: not the author's signature.
        _delivery(signer=generate_identity_keypair()),
        # Sealed to someone else.
        _delivery(sealed_to=generate_x25519_keypair().public_key),
        # Addressed to another household.
        _delivery(to="inst-someone-else"),
        # A blob lifted from another household's delivery.
        _delivery(plaintext_to="inst-someone-else"),
        # Signed by the author's key but claiming another household.
        _delivery(instance_id="inst-other"),
        # An author nobody here follows through this server.
        _delivery(author="u-bob"),
        # A key suite this build doesn't know.
        _delivery(key_suite="aesgcm-512"),
    ],
)
async def test_untrusted_deliveries_are_refused(env, delivery):
    assert await env["svc"].import_delivery(delivery, gfs_id="g1") is None
    assert await env["keys"].latest(delivery["author_user_id"], own=False) is None


async def test_a_delivery_through_another_server_is_refused(env):
    assert await env["svc"].import_delivery(_delivery(), gfs_id="g2") is None


@pytest.mark.parametrize(
    "patch",
    [
        {"extra": "x"},
        {"key_gen": "7"},
        {"key_step": True},
        {"sealed": "nope"},
        {
            "sealed": {
                "kem_suite": "x25519+mlkem768",
                "eph_pk": "e",
                "ciphertext": "c:t",
            }
        },
    ],
)
async def test_malformed_deliveries_are_refused(env, patch):
    delivery = {**_delivery(), **patch}
    signed = sign_wire(AUTHOR.private_key, delivery)
    assert await env["svc"].import_delivery(signed, gfs_id="g1") is None


async def test_a_key_for_a_local_author_is_never_taken(env):
    """A followed id that collides with a local user is refused, so a
    delivered key can never shadow a local author's own."""
    await env["db"].enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u-alice','alice2','Alice','active')"
    )
    assert await env["svc"].import_delivery(_delivery(), gfs_id="g1") is None


async def test_request_key_asks_the_gfs_once_a_minute(env):
    assert await env["svc"].request_key("u-alice", gfs_id="g1", want_gen=0)
    assert not await env["svc"].request_key("u-alice", gfs_id="g1", want_gen=0)
    [(url, body)] = env["session"].posts
    assert url == "https://g1.example/gfs/moments/users/u-alice/key_request"
    assert body["instance_id"] == OWN_INSTANCE and body["want_gen"] == 0
    assert verify_wire(body, OWN.public_key.hex())


async def test_request_key_for_a_path_unsafe_id_sends_nothing(env):
    assert not await env["svc"].request_key("../x", gfs_id="g1", want_gen=0)
    assert not await env["svc"].request_key("u-alice", gfs_id="nope", want_gen=0)
    assert env["session"].posts == []


async def test_forget_drops_keys_once_nobody_here_follows(env):
    await env["svc"].import_delivery(_delivery(), gfs_id="g1")
    await env["svc"].forget_if_unfollowed("u-alice")
    assert await env["svc"].key_for("u-alice", GEN, 0) == KEY
    await env["follows"].delete(
        follower_user_id="u-fan", followed_user_id="u-alice", gfs_id="g1"
    )
    await env["svc"].forget_if_unfollowed("u-alice")
    assert await env["svc"].key_for("u-alice", GEN, 0) is None


async def test_without_identity_nothing_is_imported(db):
    svc = MomentPublicKeyInbound(
        key_repo=SqliteMomentPublicKeyRepo(db),
        follow_repo=SqliteMomentPublicFollowRepo(db),
        user_repo=SqliteUserRepo(db),
        gfs_repo=SqliteGfsConnectionRepo(db),
    )
    assert await svc.import_delivery(_delivery(), gfs_id="g1") is None
    assert await svc.key_for("u-alice", 1, 0) is None
    assert not await svc.request_key("u-alice", gfs_id="g1", want_gen=0)
