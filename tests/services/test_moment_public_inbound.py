"""MomentPublicInbound — a verified envelope refreshes the author's public
profile (name, bio, picture digest) on the local follows (§Momentum-public).

The rest of the inbound path (signature, binding, deletes) is covered in
``test_moment_public_service.py``.
"""

from __future__ import annotations

import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from socialhome.crypto import b64url_encode, sign_ed25519
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.moment_public_repo import SqliteMomentPublicFollowRepo
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.services.moment_public_inbound import MomentPublicInbound

_SEED = b"\x22" * 32
_OTHER_SEED = b"\x33" * 32


def _pk(seed: bytes) -> str:
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    return sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _signed(payload: dict, seed: bytes = _SEED) -> dict:
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return {**payload, "signature": b64url_encode(sign_ed25519(seed, canonical))}


def _moment(moment_id: str = "m-1", **extra) -> dict:
    return {
        "moment_id": moment_id,
        "author_user_id": "u-noor",
        "author_username": "noor",
        "author_display_name": "Noor Haddad",
        "content": "bees",
        "media_url": None,
        "media_type": None,
        "duration_ms": None,
        "parent_moment_id": None,
        "origin_instance_id": "inst-noor",
        "created_at": "2026-10-20T08:00:00+00:00",
        "expires_at": "2099-10-21T08:00:00+00:00",
        **extra,
    }


async def _env(db, *, followers: tuple[str, ...] = ("u1",)):
    for uid in followers:
        await db.enqueue(
            "INSERT INTO users(user_id, username, display_name, state) "
            "VALUES(?, ?, ?, 'active')",
            (uid, uid, uid.upper()),
        )
    await db.enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, "
        "public_key, inbox_url, status, paired_at) VALUES('g1', 'gfs-1', 'G', "
        "?, 'https://g.example', 'active', datetime('now'))",
        ("aa" * 32,),
    )
    follows = SqliteMomentPublicFollowRepo(db)
    for uid in followers:
        await follows.upsert(
            follower_user_id=uid,
            followed_user_id="u-noor",
            gfs_id="g1",
            followed_instance_pk=_pk(_SEED),
            followed_username="noor",
            followed_display_name="Old Name",
            followed_bio="old bio",
            followed_picture_digest="aaaaaaaaaaaaaaaa",
        )
    inbound = MomentPublicInbound(
        bus=EventBus(), moment_repo=SqliteMomentRepo(db), follow_repo=follows
    )
    return inbound, follows


async def _create(inbound, payload: dict, seed: bytes = _SEED) -> None:
    await inbound.handle(
        {"type": "incoming_public_moment", "payload": _signed(payload, seed)},
        gfs_id="g1",
    )


async def _follow(follows, follower: str = "u1"):
    got = await follows.get(
        follower_user_id=follower, followed_user_id="u-noor", gfs_id="g1"
    )
    assert got is not None
    return got


async def test_verified_envelope_refreshes_every_local_follow(db):
    inbound, follows = await _env(db, followers=("u1", "u2"))
    await _create(
        inbound,
        _moment(author_bio="Beekeeper.", author_picture_digest="0123456789abcdef"),
    )
    for uid in ("u1", "u2"):
        f = await _follow(follows, uid)
        assert f.followed_display_name == "Noor Haddad"
        assert f.followed_bio == "Beekeeper."
        assert f.followed_picture_digest == "0123456789abcdef"


async def test_a_bad_signature_changes_nothing(db):
    inbound, follows = await _env(db)
    await _create(
        inbound,
        _moment(author_display_name="Mallory", author_bio="evil"),
        seed=_OTHER_SEED,
    )
    f = await _follow(follows)
    assert (f.followed_display_name, f.followed_bio) == ("Old Name", "old bio")


async def test_an_older_author_build_keeps_the_held_bio_and_picture(db):
    """No ``author_bio`` / ``author_picture_digest`` keys: an author
    household from before this change — the name still refreshes."""
    inbound, follows = await _env(db)
    await _create(inbound, _moment())
    f = await _follow(follows)
    assert f.followed_display_name == "Noor Haddad"
    assert f.followed_bio == "old bio"
    assert f.followed_picture_digest == "aaaaaaaaaaaaaaaa"


async def test_a_cleared_bio_and_picture_are_cleared(db):
    inbound, follows = await _env(db)
    await _create(inbound, _moment(author_bio=None, author_picture_digest=None))
    f = await _follow(follows)
    assert f.followed_bio is None and f.followed_picture_digest is None


async def test_hostile_values_are_dropped_and_an_id_is_not_a_name(db):
    inbound, follows = await _env(db)
    await _create(
        inbound,
        _moment(
            author_display_name="u-noor",
            author_username="",
            author_bio="x" * 281,
            author_picture_digest="ab&sig=evil",
        ),
    )
    f = await _follow(follows)
    assert f.followed_display_name == "Old Name"
    assert f.followed_bio is None
    assert f.followed_picture_digest is None


async def test_the_username_names_an_author_without_a_display_name(db):
    inbound, follows = await _env(db)
    await _create(inbound, _moment(author_display_name="  "))
    assert (await _follow(follows)).followed_display_name == "noor"


async def test_a_replayed_moment_does_not_roll_the_profile_back(db):
    inbound, follows = await _env(db)
    old = _moment("m-old", author_bio="first bio")
    await _create(inbound, old)
    await _create(inbound, _moment("m-new", author_bio="second bio"))
    await _create(inbound, old)  # the GFS replays the older envelope
    assert (await _follow(follows)).followed_bio == "second bio"


async def test_a_reply_to_a_moment_not_held_here_is_dropped_not_crashed(db):
    """Regression (federation demo): a public reply whose parent this
    household never received hit the ``parent_moment_id`` foreign key and
    raised out of the GFS socket handler."""
    inbound, _follows = await _env(db)
    await _create(inbound, _moment("m-reply", parent_moment_id="m-unknown"))
    assert await SqliteMomentRepo(db).get("m-reply") is None


async def test_a_reply_to_a_held_moment_lands(db):
    inbound, _follows = await _env(db)
    await _create(inbound, _moment("m-root"))
    await _create(inbound, _moment("m-reply", parent_moment_id="m-root"))
    got = await SqliteMomentRepo(db).get("m-reply")
    assert got is not None and got.parent_moment_id == "m-root"
