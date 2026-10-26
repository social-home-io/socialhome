"""MomentReactionGfs — the follower side of a reaction through a GFS (v_68).

A reaction to a public moment that came through a GFS is posted to that GFS
only, signed by this household, as exactly ``{instance_id, sealed}`` — and
only when the GFS proved ``moment_reactions`` and the author's household
named its key-wrap key. A post that fails for a reason a later try can fix
is retried (latest state per moment + person); a final refusal is not.
"""

from __future__ import annotations

import time

import pytest

from socialhome.crypto import ed25519_public_key
from socialhome.domain.moment import Moment, MomentGfsReactionTarget
from socialhome.moment_public_seal import verify_wire
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.moment_public_repo import SqliteMomentPublicFollowRepo
from socialhome.services.moment_reaction_gfs import (
    MomentReactionGfs,
    classify_react_status,
)

SEED = b"\x07" * 32
KW = "ab" * 32
SEALED = {"kem_suite": "x25519", "eph_pk": "e" * 43, "ciphertext": "n:c"}


class _Resp:
    def __init__(self, status: int, retry_after: str | None = None) -> None:
        self.status = status
        self.headers = {"Retry-After": retry_after} if retry_after else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, status: int = 202, retry_after: str | None = None) -> None:
        self.status = status
        self.retry_after = retry_after
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json))
        return _Resp(self.status, self.retry_after)


class _Caps:
    def __init__(self, proven: set[str]) -> None:
        self.proven = proven

    async def moment_reactions_supported(self, gfs_id: str) -> bool:
        return gfs_id in self.proven


@pytest.fixture
async def env(db):
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state)"
        " VALUES('u1', 'u1', 'U1', 'active')"
    )
    for gfs_id in ("g1", "g2"):
        await db.enqueue(
            "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
            " public_key, inbox_url, status, paired_at) VALUES(?, ?, 'G', ?,"
            " ?, 'active', datetime('now'))",
            (gfs_id, gfs_id, "aa" * 32, f"https://{gfs_id}.example"),
        )
    follows = SqliteMomentPublicFollowRepo(db)
    await follows.upsert(
        follower_user_id="u1",
        followed_user_id="u-noor",
        gfs_id="g1",
        followed_instance_pk="bb" * 32,
        followed_username="noor",
        followed_display_name="Noor",
    )
    caps = _Caps({"g1"})
    gfs = MomentReactionGfs(
        gfs_repo=SqliteGfsConnectionRepo(db),
        follow_repo=follows,
        capabilities=caps,
    )
    session = _Session()
    gfs.attach_session(session)  # type: ignore[arg-type]
    gfs.attach_identity(own_instance_id="inst-me", signing_key=SEED)
    return gfs, follows, caps, session


def _moment(**over) -> Moment:
    fields = {
        "id": "m-1",
        "author_user_id": "u-noor",
        "content": "bees",
        "media_url": None,
        "media_type": None,
        "duration_ms": None,
        "parent_moment_id": None,
        "origin_instance_id": "inst-noor",
        "created_at": "2026-10-26T10:00:00+00:00",
        "expires_at": "2026-10-30T10:00:00+00:00",
        "is_public": True,
        "received_via": "gfs",
        "received_via_gfs_id": "g1",
    }
    fields.update(over)
    return Moment(**fields)


async def _with_key(follows, gfs_id: str = "g1") -> None:
    await follows.set_keywrap(followed_user_id="u-noor", gfs_id=gfs_id, keywrap_pk=KW)


async def test_the_target_is_the_gfs_the_moment_came_through(env):
    gfs, follows, _caps, _s = env
    assert await gfs.target(_moment()) is None  # no key from the author yet
    await _with_key(follows)
    assert await gfs.target(_moment()) == MomentGfsReactionTarget("g1", KW)


@pytest.mark.parametrize(
    "over",
    [
        {"received_via": "household", "received_via_gfs_id": None},
        {"received_via": "self", "received_via_gfs_id": None},
        {"is_public": False},
        {"received_via_gfs_id": None},
        # Another GFS: not proven, and the key was named through g1 only.
        {"received_via_gfs_id": "g2"},
        {"received_via_gfs_id": "g-gone"},
        {"author_user_id": "../evil"},
    ],
)
async def test_no_target_otherwise(env, over):
    gfs, follows, _caps, _s = env
    await _with_key(follows)
    assert await gfs.target(_moment(**over)) is None


async def test_a_gfs_without_the_capability_is_not_used(env):
    gfs, follows, caps, _s = env
    await _with_key(follows)
    caps.proven.clear()
    assert await gfs.target(_moment()) is None


async def test_without_identity_nothing_is_targeted(db):
    gfs = MomentReactionGfs(
        gfs_repo=SqliteGfsConnectionRepo(db),
        follow_repo=SqliteMomentPublicFollowRepo(db),
        capabilities=_Caps({"g1"}),
    )
    assert await gfs.target(_moment()) is None
    await gfs.send(
        MomentGfsReactionTarget("g1", KW),
        author_user_id="u-noor",
        sealed=SEALED,
        retry_key="m-1|u1",
        occurred_at_s=time.time(),
    )
    assert gfs.pending_count() == 0


async def test_the_post_is_exactly_the_household_and_the_seal(env):
    gfs, _follows, _caps, session = env
    await gfs.send(
        MomentGfsReactionTarget("g1", KW),
        author_user_id="u-noor",
        sealed=SEALED,
        retry_key="m-1|u1",
        occurred_at_s=time.time(),
    )
    [(url, body)] = session.posts
    assert url == "https://g1.example/gfs/moments/users/u-noor/react"
    assert set(body) == {"instance_id", "sealed", "signature"}
    assert body["instance_id"] == "inst-me" and body["sealed"] == SEALED
    assert verify_wire(body, ed25519_public_key(SEED).hex())
    assert gfs.pending_count() == 0


@pytest.mark.parametrize(
    ("status", "retried"),
    [(503, True), (429, True), (404, True), (403, False), (422, False)],
)
async def test_a_failed_post_is_retried_only_when_a_retry_can_help(
    env, status, retried
):
    gfs, _follows, _caps, session = env
    session.status = status
    await gfs.send(
        MomentGfsReactionTarget("g1", KW),
        author_user_id="u-noor",
        sealed=SEALED,
        retry_key="m-1|u1",
        occurred_at_s=time.time(),
    )
    assert gfs.pending_count() == (1 if retried else 0)


async def test_a_newer_change_replaces_the_pending_one(env):
    gfs, _follows, _caps, session = env
    session.status = 503
    target = MomentGfsReactionTarget("g1", KW)
    for _ in range(2):
        await gfs.send(
            target,
            author_user_id="u-noor",
            sealed=SEALED,
            retry_key="m-1|u1",
            occurred_at_s=time.time(),
        )
    assert gfs.pending_count() == 1
    session.status = 202
    await gfs.send(
        target,
        author_user_id="u-noor",
        sealed=SEALED,
        retry_key="m-1|u1",
        occurred_at_s=time.time(),
    )
    assert gfs.pending_count() == 0


async def test_a_retry_resends_the_same_bytes(env):
    gfs, _follows, _caps, session = env
    session.status = 503
    await gfs.send(
        MomentGfsReactionTarget("g1", KW),
        author_user_id="u-noor",
        sealed=SEALED,
        retry_key="m-1|u1",
        occurred_at_s=time.time(),
    )
    session.status = 202
    for item in gfs._retry._pending.values():
        item.due_at = 0
    await gfs._retry.run_due()
    assert gfs.pending_count() == 0
    assert session.posts[0] == session.posts[1]


async def test_a_transport_error_is_transient(env):
    gfs, _follows, _caps, _session = env

    class _Broken:
        def post(self, *_a, **_kw):
            raise TimeoutError

    gfs._http_client = _Broken()  # type: ignore[assignment]
    await gfs.send(
        MomentGfsReactionTarget("g1", KW),
        author_user_id="u-noor",
        sealed=SEALED,
        retry_key="m-1|u1",
        occurred_at_s=time.time(),
    )
    assert gfs.pending_count() == 1


async def test_no_session_or_a_gone_gfs_is_final(db, env):
    gfs, _follows, _caps, _session = env
    target = MomentGfsReactionTarget("g-gone", KW)
    await gfs.send(
        target,
        author_user_id="u-noor",
        sealed=SEALED,
        retry_key="m-1|u1",
        occurred_at_s=time.time(),
    )
    assert gfs.pending_count() == 0
    bare = MomentReactionGfs(
        gfs_repo=SqliteGfsConnectionRepo(db),
        follow_repo=SqliteMomentPublicFollowRepo(db),
        capabilities=_Caps({"g1"}),
    )
    bare.attach_identity(own_instance_id="inst-me", signing_key=SEED)
    await bare.send(
        MomentGfsReactionTarget("g1", KW),
        author_user_id="u-noor",
        sealed=SEALED,
        retry_key="m-1|u1",
        occurred_at_s=time.time(),
    )
    assert bare.pending_count() == 0


async def test_start_and_stop(env):
    gfs, _follows, _caps, session = env
    await gfs.start()
    session.status = 503
    await gfs.send(
        MomentGfsReactionTarget("g1", KW),
        author_user_id="u-noor",
        sealed=SEALED,
        retry_key="m-1|u1",
        occurred_at_s=time.time(),
    )
    await gfs.stop()
    assert gfs.pending_count() == 0


def test_status_classification():
    assert classify_react_status(202, None).kind == "delivered"
    assert classify_react_status(429, "30").retry_after_s == 30
    assert classify_react_status(500, None).kind == "transient"
    assert classify_react_status(301, None).kind == "permanent"
