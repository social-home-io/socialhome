"""Author side of encrypted public moments (v_62): the moment content key is
minted per author and sealed to one follower household at a time."""

from __future__ import annotations

import asyncio
import os

import pytest

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.events import UserBlocked
from socialhome.federation.keywrap_seal import open_keywrap
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.moment_public_seal import (
    DELIVERY_FIELDS,
    MAX_RATCHET_STEPS,
    RATCHET_STEP_S,
    parse_key_plaintext,
    ratchet,
    verify_wire,
)
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicKeyRepo,
    SqliteMomentPublicRegistrationRepo,
)
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services import moment_public_key_outbound
from socialhome.services.moment_public_key_outbound import (
    ROTATE_AT_MOST_EVERY_S,
    MomentPublicKeyOutbound,
)

AUTHOR_KP = generate_identity_keypair()


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _Resp(self.status)


class _Gfs:
    """The ``encrypted_moments`` proof per connection."""

    def __init__(self, proven: set[str]) -> None:
        self.proven = proven

    async def encrypted_moments_supported(self, gfs_id: str) -> bool:
        return gfs_id in self.proven


class _Protection:
    def __init__(self, protected: set[str]) -> None:
        self.protected = protected

    def register_gate(self, _gate) -> None:
        pass

    async def is_protected(self, user_id: str) -> bool:
        return user_id in self.protected

    async def is_restricted(self, user_id: str, _capability) -> bool:
        return user_id in self.protected


def _follower(**patch) -> tuple[dict, bytes]:
    """A follower household's published keys (as the GFS names them) and
    its key-wrap private key."""
    ident = generate_identity_keypair()
    wrap = generate_x25519_keypair()
    keys = {
        "instance_id": derive_instance_id(ident.public_key),
        "identity_public_key": ident.public_key.hex(),
        "keywrap_public_key": wrap.public_key.hex(),
        "kem_suite": "x25519",
        "keywrap_sig": b64url_encode(sign_ed25519(ident.private_key, wrap.public_key)),
        "moment_suites": {"kem": ["x25519"], "seal": ["aesgcm-256"]},
        **patch,
    }
    return keys, wrap.private_key


@pytest.fixture
async def env(db):
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u-alice','alice','Alice','active')"
    )
    for gfs_id in ("g1", "g2"):
        await db.enqueue(
            "INSERT INTO gfs_connections(id, gfs_instance_id, display_name,"
            " public_key, inbox_url, status, paired_at) VALUES(?, ?, 'G', 'ff',"
            " ?, 'active', datetime('now'))",
            (gfs_id, gfs_id, f"https://{gfs_id}.example"),
        )
    regs = SqliteMomentPublicRegistrationRepo(db)
    await regs.upsert(user_id="u-alice", gfs_id="g1", default_share=True)
    await regs.upsert(user_id="u-alice", gfs_id="g2", default_share=True)
    keys = SqliteMomentPublicKeyRepo(db)
    svc = MomentPublicKeyOutbound(
        key_repo=keys,
        registration_repo=regs,
        user_repo=SqliteUserRepo(db),
        gfs_repo=SqliteGfsConnectionRepo(db),
        gfs_service=_Gfs({"g1"}),
    )
    session = _Session()
    svc.attach_session(session)
    kek = KeyManager(os.urandom(32))
    svc.attach_identity(
        own_instance_id="inst-alice",
        signing_key=AUTHOR_KP.private_key,
        key_manager=kek,
    )
    return {"svc": svc, "session": session, "keys": keys, "db": db}


def _follow_changed(follower: dict, *, author: str = "u-alice") -> dict:
    return {
        "type": "follow_changed",
        "action": "add",
        "follower_user_id": "u-fan",
        "follower_instance_id": follower["instance_id"],
        "followed_user_id": author,
        "follower": follower,
    }


def _key_request(follower: dict, *, author: str = "u-alice") -> dict:
    return {
        "type": "moment_key_request",
        "followed_user_id": author,
        "want_gen": 0,
        "follower_user_ids": ["u-fan"],
        "follower": follower,
    }


async def test_the_key_is_minted_once_and_kept(env):
    gen, step, key = await env["svc"].current_key("u-alice")
    assert len(key) == 32 and gen > 1_600_000_000 and step == 0
    assert await env["svc"].current_key("u-alice") == (gen, step, key)
    row = await env["keys"].latest("u-alice", own=True)
    # Stored KEK-sealed, never the raw key.
    assert row is not None and key.hex() not in row.key_sealed


async def test_the_step_key_ratchets_daily_in_place(env, monkeypatch):
    """A day later the step moves on and the old step key is overwritten —
    the household can't recompute yesterday's."""
    gen, _step, key0 = await env["svc"].current_key("u-alice")
    clock = [gen + 2 * RATCHET_STEP_S + 5]
    monkeypatch.setattr(moment_public_key_outbound.time, "time", lambda: clock[0])
    gen2, step, key2 = await env["svc"].current_key("u-alice")
    assert (gen2, step) == (gen, 2) and key2 == ratchet(key0, 2)
    row = await env["keys"].latest("u-alice", own=True)
    assert row.key_step == 2


async def test_a_generation_too_old_to_ratchet_starts_a_new_one(env, monkeypatch):
    gen, _step, _key = await env["svc"].current_key("u-alice")
    far = gen + (MAX_RATCHET_STEPS + 2) * RATCHET_STEP_S
    monkeypatch.setattr(moment_public_key_outbound.time, "time", lambda: far)
    gen2, step, _ = await env["svc"].current_key("u-alice")
    assert gen2 > gen and step == 0


async def test_a_new_generation_never_repeats_an_old_one(env):
    first, _, _ = await env["svc"].current_key("u-alice")
    second, step, key2 = await env["svc"].rotate("u-alice")
    assert second > first and step == 0
    assert await env["svc"].current_key("u-alice") == (second, 0, key2)


async def test_a_new_follower_gets_the_key_sealed_to_it_alone(env):
    follower, wrap_priv = _follower()
    queued = await env["svc"].handle(_follow_changed(follower), gfs_id="g1")
    assert queued == 1
    assert env["session"].posts == []  # queued, not sent inline
    assert await env["svc"].flush() == 1
    [(url, body)] = env["session"].posts
    assert url == "https://g1.example/gfs/moments/keys"
    # The batch is signed by the author's household…
    assert verify_wire(body, AUTHOR_KP.public_key.hex())
    [delivery] = body["deliveries"]
    assert set(delivery) == DELIVERY_FIELDS
    assert delivery["to"] == follower["instance_id"]
    # …and so is each delivery, which the follower verifies itself.
    assert verify_wire(delivery, AUTHOR_KP.public_key.hex())
    gen, step, key = await env["svc"].current_key("u-alice")
    assert (delivery["key_gen"], delivery["key_step"]) == (gen, step)
    assert delivery["key_suite"] == "aesgcm-256"
    assert key.hex() not in str(body)
    opened = open_keywrap(sealed=delivery["sealed"], recipient_keywrap_priv=wrap_priv)
    assert (
        parse_key_plaintext(
            opened,
            author_user_id="u-alice",
            key_gen=gen,
            key_step=step,
            to=follower["instance_id"],
        )
        == key
    )


async def test_a_key_request_is_answered_the_same_way(env):
    follower, _ = _follower()
    assert await env["svc"].handle(_key_request(follower), gfs_id="g1") == 1
    await env["svc"].flush()
    assert env["session"].posts[0][1]["deliveries"][0]["to"] == follower["instance_id"]


async def test_a_substituted_keywrap_key_is_never_sealed_to(env):
    """A GFS that swaps in a key-wrap key it controls fails the binding."""
    follower, _ = _follower(
        keywrap_public_key=generate_x25519_keypair().public_key.hex()
    )
    assert await env["svc"].handle(_follow_changed(follower), gfs_id="g1") == 0
    assert env["session"].posts == []


@pytest.mark.parametrize(
    "patch",
    [
        # An older household: declared nothing it can open.
        {"moment_suites": None},
        {"moment_suites": {"kem": ["x25519"], "seal": []}},
        {"moment_suites": {"kem": [], "seal": ["aesgcm-256"]}},
        {"moment_suites": {"kem": "x25519", "seal": "aesgcm-256"}},
        {"kem_suite": "x25519+mlkem768"},
        {"keywrap_public_key": "zz"},
        {"keywrap_sig": ""},
        {"instance_id": "someone-else"},
    ],
)
async def test_unusable_follower_keys_are_skipped(env, patch):
    follower, _ = _follower(**patch)
    assert await env["svc"].handle(_follow_changed(follower), gfs_id="g1") == 0
    assert env["session"].posts == []


async def test_a_follow_notice_without_keys_seals_nothing(env):
    frame = _follow_changed(_follower()[0])
    del frame["follower"]
    assert await env["svc"].handle(frame, gfs_id="g1") == 0
    frame = {**_follow_changed(_follower()[0]), "action": "remove"}
    assert await env["svc"].handle(frame, gfs_id="g1") == 0
    assert await env["svc"].handle({"type": "other"}, gfs_id="g1") == 0
    assert env["session"].posts == []


async def test_nothing_is_sealed_for_an_author_not_registered_there(env):
    follower, _ = _follower()
    # Not a local user at all.
    assert (
        await env["svc"].handle(_follow_changed(follower, author="u-x"), gfs_id="g1")
        == 0
    )
    # Local, but not registered on this server.
    await env["db"].enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u-bob','bob','Bob','active')"
    )
    assert (
        await env["svc"].handle(_follow_changed(follower, author="u-bob"), gfs_id="g1")
        == 0
    )
    assert env["session"].posts == []


async def test_nothing_is_sealed_through_a_server_without_encrypted_moments(env):
    follower, _ = _follower()
    assert await env["svc"].handle(_follow_changed(follower), gfs_id="g2") == 0
    assert env["session"].posts == []


async def test_a_protected_author_seals_nothing(env):
    env["svc"].attach_child_protection(_Protection({"u-alice"}))
    follower, _ = _follower()
    assert await env["svc"].handle(_follow_changed(follower), gfs_id="g1") == 0
    assert env["session"].posts == []


async def test_repeated_requests_are_throttled_per_follower(env):
    """A GFS replaying requests can't make the author seal again and again:
    once per follower household per window, unless the key changed."""
    follower, _ = _follower()
    other, _ = _follower()
    assert await env["svc"].handle(_key_request(follower), gfs_id="g1") == 1
    assert await env["svc"].handle(_key_request(follower), gfs_id="g1") == 0
    assert await env["svc"].handle(_key_request(other), gfs_id="g1") == 1
    await env["svc"].rotate("u-alice")
    assert await env["svc"].handle(_key_request(follower), gfs_id="g1") == 1
    assert await env["svc"].flush() == 3


async def test_a_refused_delivery_counts_as_not_sent(env):
    env["session"].status = 422
    follower, _ = _follower()
    assert await env["svc"].handle(_follow_changed(follower), gfs_id="g1") == 1
    assert await env["svc"].flush() == 0


async def test_a_burst_of_requests_rides_a_few_batched_posts(env):
    """250 followers asking at once (after a rotation, or the author's
    household coming back) are three requests to the GFS, not 250."""
    for _ in range(250):
        assert await env["svc"].handle(_key_request(_follower()[0]), gfs_id="g1") == 1
    assert await env["svc"].flush() == 250
    assert [len(b["deliveries"]) for _, b in env["session"].posts] == [100, 100, 50]
    # Every target once.
    targets = [d["to"] for _, b in env["session"].posts for d in b["deliveries"]]
    assert len(set(targets)) == 250


async def test_the_queue_per_server_is_bounded(env, monkeypatch):
    monkeypatch.setattr(moment_public_key_outbound, "MAX_QUEUED_PER_GFS", 2)
    results = [
        await env["svc"].handle(_key_request(_follower()[0]), gfs_id="g1")
        for _ in range(3)
    ]
    assert results == [1, 1, 0]


async def test_the_background_flush_sends_what_is_queued(env, monkeypatch):
    monkeypatch.setattr(moment_public_key_outbound, "FLUSH_AFTER_S", 0.01)
    svc = env["svc"]
    await svc.start()
    await svc.start()  # idempotent
    await svc.handle(_follow_changed(_follower()[0]), gfs_id="g1")
    for _ in range(100):
        if env["session"].posts:
            break
        await asyncio.sleep(0.01)
    assert len(env["session"].posts) == 1
    # Stopping sends what is still queued.
    await svc.handle(_follow_changed(_follower()[0]), gfs_id="g1")
    await svc.stop()
    assert len(env["session"].posts) == 2


async def test_a_flush_to_a_gone_connection_sends_nothing(env):
    await env["svc"].handle(_follow_changed(_follower()[0]), gfs_id="g1")
    await env["db"].enqueue("UPDATE gfs_connections SET status='suspended'")
    assert await env["svc"].flush() == 0
    assert env["session"].posts == []


async def test_without_identity_nothing_is_sealed(db):
    svc = MomentPublicKeyOutbound(
        key_repo=SqliteMomentPublicKeyRepo(db),
        registration_repo=SqliteMomentPublicRegistrationRepo(db),
        user_repo=SqliteUserRepo(db),
        gfs_repo=SqliteGfsConnectionRepo(db),
        gfs_service=_Gfs({"g1"}),
    )
    assert await svc.handle(_follow_changed(_follower()[0]), gfs_id="g1") == 0
    with pytest.raises(RuntimeError):
        await svc.current_key("u-alice")


# ── Lazy rotation ─────────────────────────────────────────────────────────


def _removed(*, still: bool | None = None, author: str = "u-alice") -> dict:
    frame = {
        "type": "follow_changed",
        "action": "remove",
        "follower_user_id": "u-fan",
        "follower_instance_id": "inst-fan",
        "followed_user_id": author,
    }
    if still is not None:
        frame["household_still_follows"] = still
    return frame


def _clock(monkeypatch, start: float) -> list[float]:
    now = [start]
    monkeypatch.setattr(moment_public_key_outbound.time, "time", lambda: now[0])
    return now


async def test_a_household_that_still_follows_rotates_nothing(env):
    await env["svc"].current_key("u-alice")
    await env["svc"].handle(_removed(still=True), gfs_id="g1")
    assert not (await env["keys"].latest("u-alice", own=True)).rotate_due


@pytest.mark.parametrize("still", [False, None])
async def test_a_household_leaving_rotates_at_the_next_publish_at_most_every_6h(
    env, monkeypatch, still
):
    """Lazy: the leave only marks; the next publish rotates — but not
    within 6 h of the last generation, and never without a publish."""
    now = _clock(monkeypatch, 1_800_000_000.0)
    gen, _, key = await env["svc"].current_key("u-alice")
    await env["svc"].handle(_removed(still=still), gfs_id="g1")
    assert (await env["keys"].latest("u-alice", own=True)).rotate_due
    # Sealing to a newcomer never rotates.
    assert (await env["svc"].current_key("u-alice"))[0] == gen
    now[0] += 3600
    assert (await env["svc"].publish_key("u-alice"))[0] == gen  # < 6 h
    now[0] += ROTATE_AT_MOST_EVERY_S
    new_gen, step, new_key = await env["svc"].publish_key("u-alice")
    assert new_gen > gen and step == 0 and new_key != key
    latest = await env["keys"].latest("u-alice", own=True)
    assert latest.key_gen == new_gen and not latest.rotate_due
    # Several leaves before one publish → one rotation.
    now[0] += ROTATE_AT_MOST_EVERY_S
    for _ in range(3):
        await env["svc"].handle(_removed(), gfs_id="g1")
    third, _, _ = await env["svc"].publish_key("u-alice")
    assert third > new_gen
    assert (await env["svc"].publish_key("u-alice"))[0] == third


async def test_without_a_mark_a_publish_never_rotates(env, monkeypatch):
    now = _clock(monkeypatch, 1_800_000_000.0)
    gen, _, _ = await env["svc"].current_key("u-alice")
    now[0] += 3 * 86_400
    gen2, step, _ = await env["svc"].publish_key("u-alice")
    assert (gen2, step) == (gen, 3)


async def test_a_leave_on_a_server_the_author_isnt_on_marks_nothing(env):
    await env["svc"].current_key("u-alice")
    await env["db"].enqueue(
        "INSERT INTO gfs_connections(id, gfs_instance_id, display_name, public_key,"
        " inbox_url, status, paired_at) VALUES('g3','g3','G','ff',"
        " 'https://g3.example','active', datetime('now'))"
    )
    await env["svc"].handle(_removed(), gfs_id="g3")
    await env["svc"].handle(_removed(author=""), gfs_id="g1")
    assert not (await env["keys"].latest("u-alice", own=True)).rotate_due


async def test_marking_before_any_key_is_a_no_op(env):
    assert not await env["svc"].mark_rotation_due("u-alice", reason="test")


async def test_no_key_for_a_household_whose_followers_are_all_blocked(env):
    follower, _ = _follower()
    await env["db"].enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id)"
        " VALUES('u-alice', 'u-fan')"
    )
    assert await env["svc"].handle(_key_request(follower), gfs_id="g1") == 0
    assert await env["svc"].handle(_follow_changed(follower), gfs_id="g1") == 0
    # Another, unblocked user of that household still follows: sealed.
    frame = {**_key_request(follower), "follower_user_ids": ["u-fan", "u-kid"]}
    assert await env["svc"].handle(frame, gfs_id="g1") == 1


async def test_a_block_by_a_registered_author_marks_a_rotation(env):
    await env["svc"].current_key("u-alice")
    await env["svc"].on_user_blocked(
        UserBlocked(blocker_user_id="u-nobody", blocked_user_id="u-fan")
    )
    assert not (await env["keys"].latest("u-alice", own=True)).rotate_due
    await env["svc"].on_user_blocked(
        UserBlocked(blocker_user_id="u-alice", blocked_user_id="u-fan")
    )
    assert (await env["keys"].latest("u-alice", own=True)).rotate_due
