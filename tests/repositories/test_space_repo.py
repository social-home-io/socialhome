"""Tests for SqliteSpaceRepo — spaces, members, instances, bans, invites, etc."""

from __future__ import annotations


import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceType,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo


@pytest.fixture
async def env(tmp_dir):
    """Env with a space repo and a seeded user."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("alice", "uid-alice", "Alice"),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )

    class E:
        pass

    from socialhome.infrastructure.key_manager import KeyManager

    km = KeyManager(b"\x07" * 32)
    e = E()
    e.db = db
    e.kp = kp
    e.iid = iid
    e.km = km
    e.repo = SqliteSpaceRepo(db, key_manager=km)
    yield e
    await db.shutdown()


def _space(
    space_id: str = "sp-1",
    name: str = "TestSpace",
    space_type: SpaceType = SpaceType.PRIVATE,
    archived: bool = False,
    archived_reason: str | None = None,
) -> Space:
    return Space(
        id=space_id,
        name=name,
        owner_instance_id="inst-x",
        owner_username="alice",
        identity_public_key="aabb" * 16,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=space_type,
        join_mode=JoinMode.INVITE_ONLY,
        archived=archived,
        archived_reason=archived_reason,
    )


def _member(
    space_id: str, user_id: str = "uid-alice", role: str = "member"
) -> SpaceMember:
    return SpaceMember(
        space_id=space_id,
        user_id=user_id,
        role=role,
        joined_at="2025-01-01T00:00:00",
    )


# ── Spaces ─────────────────────────────────────────────────────────────────


async def test_save_and_get_space(env):
    """save persists a space; get retrieves it."""
    space = _space("sp-1")
    await env.repo.save(space)
    fetched = await env.repo.get("sp-1")
    assert fetched is not None
    assert fetched.name == "TestSpace"


async def test_get_drops_legacy_non_post_type_exemptions(env):
    """Rows written by the pre-#733 UI carry values like ``pages`` that are
    not post types (they never matched ``space_posts.type``). Reads keep
    only real post types so a re-save can't fail on stale values."""
    await env.repo.save(_space("sp-1"))
    await env.db.enqueue(
        "UPDATE spaces SET retention_exempt_json=? WHERE id=?",
        ('["pages", "poll", "gallery"]', "sp-1"),
    )
    fetched = await env.repo.get("sp-1")
    assert fetched is not None
    assert fetched.retention_exempt_types == ("poll",)


async def test_save_round_trips_allow_subscribers(env):
    """Migration 0051's ``spaces.allow_subscribers`` persists through
    ``save`` (both INSERT and the ON CONFLICT update) and comes back on
    ``SpaceFeatures``. It defaults OFF — a space is private until its owner
    says otherwise — and the column is stored as 0/1, not a bool."""
    from dataclasses import replace

    space = _space("sp-rd")
    await env.repo.save(space)
    got = await env.repo.get("sp-rd")
    assert got is not None and got.features.allow_subscribers is False

    await env.repo.save(replace(space, features=SpaceFeatures(allow_subscribers=True)))
    got = await env.repo.get("sp-rd")
    assert got is not None and got.features.allow_subscribers is True
    row = await env.db.fetchone(
        "SELECT allow_subscribers FROM spaces WHERE id=?", ("sp-rd",)
    )
    assert row["allow_subscribers"] == 1

    # …and back off again, so the ON CONFLICT path is exercised both ways.
    await env.repo.save(replace(space, features=SpaceFeatures()))
    got = await env.repo.get("sp-rd")
    assert got is not None and got.features.allow_subscribers is False


async def test_set_and_get_space_seed_round_trips(env):
    """set_space_seed persists a non-NULL column; get_space_seed returns the
    original 32-byte seed; the stored column is KEK-wrapped (≠ plaintext)."""
    from socialhome.crypto import generate_identity_keypair

    space = _space("sp-seed")
    await env.repo.save(space)
    kp = generate_identity_keypair()
    await env.repo.set_space_seed("sp-seed", kp.private_key)

    # Round-trips to the original raw seed.
    got = await env.repo.get_space_seed("sp-seed")
    assert got == kp.private_key

    # The stored column is non-NULL and is the wrapped form, not the raw seed.
    row = await env.db.fetchone(
        "SELECT identity_private_key FROM spaces WHERE id=?", ("sp-seed",)
    )
    stored = row["identity_private_key"]
    assert stored is not None
    assert stored != kp.private_key.hex()
    assert kp.private_key.hex() not in stored


async def test_get_space_seed_none_when_column_null(env):
    """A space saved without a seed → get_space_seed returns None."""
    space = _space("sp-noseed")
    await env.repo.save(space)
    assert await env.repo.get_space_seed("sp-noseed") is None


async def test_space_seed_is_bound_to_space_id_at_rest(env):
    """The wrapped seed is AES-GCM-bound to its space_id (associated data), so a
    wrapped blob copied into another space's row can't be decrypted — defends
    against a cross-row seed swap at the storage layer."""
    from socialhome.crypto import generate_identity_keypair

    await env.repo.save(_space("sp-a"))
    await env.repo.save(_space("sp-b"))
    kp = generate_identity_keypair()
    await env.repo.set_space_seed("sp-a", kp.private_key)

    # Physically copy sp-a's wrapped blob into sp-b's row.
    row = await env.db.fetchone(
        "SELECT identity_private_key FROM spaces WHERE id=?", ("sp-a",)
    )
    await env.db.enqueue(
        "UPDATE spaces SET identity_private_key=? WHERE id=?",
        (row["identity_private_key"], "sp-b"),
    )
    # sp-a still decrypts; sp-b's stolen blob fails the AD check.
    assert await env.repo.get_space_seed("sp-a") == kp.private_key
    with pytest.raises(Exception):
        await env.repo.get_space_seed("sp-b")


async def test_set_space_pubkey_targeted_update(env):
    """set_space_pubkey replaces only identity_public_key (the mint path); a
    normal save no longer mutates the pubkey, so it can't be clobbered."""
    space = _space("sp-pk")
    await env.repo.save(space)
    await env.repo.set_space_pubkey("sp-pk", "bb" * 32)
    assert (await env.repo.get("sp-pk")).identity_public_key == "bb" * 32
    # A re-save carrying a different (e.g. empty) pubkey must NOT overwrite it.
    await env.repo.save(replace(space, identity_public_key=""))
    assert (await env.repo.get("sp-pk")).identity_public_key == "bb" * 32


async def test_save_preserves_existing_seed(env):
    """A subsequent save (ON CONFLICT update) must not clobber a stored seed."""
    from socialhome.crypto import generate_identity_keypair

    space = _space("sp-keep")
    await env.repo.save(space)
    kp = generate_identity_keypair()
    await env.repo.set_space_seed("sp-keep", kp.private_key)
    # Re-save the same space (e.g. a config update).
    await env.repo.save(replace(space, name="Renamed"))
    assert await env.repo.get_space_seed("sp-keep") == kp.private_key


async def test_allowed_post_types_round_trip_including_event_location_highlight(env):
    """Every gatable post type persists — incl. event / location /
    highlight_share, which were previously dropped from the INSERT/UPDATE
    and stuck at their DEFAULT 1 (so they could never be disabled)."""
    restricted = SpaceFeatures().with_allowed_post_types({"text", "image"})
    space = Space(
        id="sp-allow",
        name="Restricted",
        owner_instance_id="inst-x",
        owner_username="alice",
        identity_public_key="aabb" * 16,
        config_sequence=0,
        features=restricted,
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )
    await env.repo.save(space)
    fetched = await env.repo.get("sp-allow")
    assert fetched is not None
    assert set(fetched.features.allowed_post_types) == {"text", "image"}
    # The three formerly-unpersisted types are genuinely off now.
    for t in ("event", "location", "highlight_share"):
        assert not fetched.features.allows(t)

    # And re-enabling them via an UPDATE (ON CONFLICT) sticks too.
    await env.repo.save(
        replace(
            space,
            features=restricted.with_allowed_post_types(
                {"text", "image", "location", "event", "highlight_share"}
            ),
        )
    )
    again = await env.repo.get("sp-allow")
    assert again is not None
    for t in ("location", "event", "highlight_share"):
        assert again.features.allows(t)


async def test_delegated_admin_authority_round_trips_and_toggles(env):
    """The delegated-admin-authority opt-in persists, defaults OFF, and can
    be flipped on for an existing space via the ON CONFLICT update path."""
    # Fresh space defaults OFF.
    await env.repo.save(_space("sp-deleg"))
    fresh = await env.repo.get("sp-deleg")
    assert fresh is not None and fresh.features.delegated_admin_authority is False

    # Save with the flag True → reload True.
    await env.repo.save(
        replace(
            _space("sp-deleg-on"),
            features=SpaceFeatures(delegated_admin_authority=True),
        )
    )
    on = await env.repo.get("sp-deleg-on")
    assert on is not None and on.features.delegated_admin_authority is True

    # Toggle it on for an existing space via save (ON CONFLICT UPDATE).
    toggled = await env.repo.get("sp-deleg")
    assert toggled is not None
    await env.repo.save(
        replace(
            toggled,
            features=replace(toggled.features, delegated_admin_authority=True),
        )
    )
    after = await env.repo.get("sp-deleg")
    assert after is not None and after.features.delegated_admin_authority is True


async def test_feature_bazaar_round_trips(env):
    """The Bazaar tab toggle persists like the other feature flags."""
    space = replace(
        _space("sp-baz"),
        features=SpaceFeatures(bazaar=False),
    )
    await env.repo.save(space)
    fetched = await env.repo.get("sp-baz")
    assert fetched is not None
    assert fetched.features.bazaar is False
    # Default stays on for a space that never touched the flag.
    await env.repo.save(_space("sp-baz-on"))
    on = await env.repo.get("sp-baz-on")
    assert on is not None and on.features.bazaar is True


async def test_get_missing_space(env):
    """get returns None for an unknown space id."""
    assert await env.repo.get("nope") is None


async def test_archived_round_trips_and_set_archived(env):
    """``archived`` persists through save/get, and ``set_archived`` flips
    it both ways (soft, reversible — the row is never removed)."""
    await env.repo.save(_space("sp-arch", archived=True))
    assert (await env.repo.get("sp-arch")).archived is True

    await env.repo.set_archived("sp-arch", False)
    assert (await env.repo.get("sp-arch")).archived is False
    await env.repo.set_archived("sp-arch", True)
    assert (await env.repo.get("sp-arch")).archived is True
    # Still present — archive never deletes.
    assert await env.repo.get("sp-arch") is not None


async def test_set_archived_with_reason_stamps_it(env):
    """``set_archived(id, True, reason=...)`` records the remote-termination
    reason alongside the flag (NULL otherwise = normal/admin archive)."""
    await env.repo.save(_space("sp-term"))
    await env.repo.set_archived("sp-term", True, reason="dissolved")
    fetched = await env.repo.get("sp-term")
    assert fetched.archived is True
    assert fetched.archived_reason == "dissolved"


async def test_unarchive_clears_reason(env):
    """Un-archiving (``set_archived(id, False)``) clears the reason back to
    NULL — reason defaults to None when omitted."""
    await env.repo.save(_space("sp-term2"))
    await env.repo.set_archived("sp-term2", True, reason="removed")
    assert (await env.repo.get("sp-term2")).archived_reason == "removed"
    await env.repo.set_archived("sp-term2", False)
    fetched = await env.repo.get("sp-term2")
    assert fetched.archived is False
    assert fetched.archived_reason is None


async def test_save_round_trips_archived_reason(env):
    """The save upsert persists ``archived_reason`` through get."""
    await env.repo.save(_space("sp-saved", archived=True, archived_reason="removed"))
    fetched = await env.repo.get("sp-saved")
    assert fetched.archived is True
    assert fetched.archived_reason == "removed"


async def test_fresh_space_has_no_archived_reason(env):
    """A freshly-created, un-archived space defaults ``archived_reason`` to
    None."""
    await env.repo.save(_space("sp-fresh"))
    fetched = await env.repo.get("sp-fresh")
    assert fetched.archived is False
    assert fetched.archived_reason is None


async def test_category_round_trips_through_save_and_get(env):
    """A space saved with a category surfaces it on get."""
    await env.repo.save(replace(_space("sp-cat"), category="gaming"))
    fetched = await env.repo.get("sp-cat")
    assert fetched is not None
    assert fetched.category == "gaming"


async def test_category_defaults_to_none_when_unset(env):
    """A space saved without a category reads back as None."""
    await env.repo.save(_space("sp-nocat"))
    fetched = await env.repo.get("sp-nocat")
    assert fetched is not None
    assert fetched.category is None


async def test_list_by_type(env):
    """list_by_type returns non-dissolved spaces matching the given type."""
    await env.repo.save(_space("sp-priv1", space_type=SpaceType.PRIVATE))
    await env.repo.save(_space("sp-priv2", name="Other", space_type=SpaceType.PRIVATE))
    results = await env.repo.list_by_type(SpaceType.PRIVATE)
    ids = [s.id for s in results]
    assert "sp-priv1" in ids
    assert "sp-priv2" in ids


async def test_list_by_type_excludes_dissolved(env):
    """list_by_type does not return dissolved spaces."""
    await env.repo.save(_space("sp-dis"))
    await env.repo.mark_dissolved("sp-dis")
    results = await env.repo.list_by_type(SpaceType.PRIVATE)
    assert not any(s.id == "sp-dis" for s in results)


async def test_mark_dissolved(env):
    """mark_dissolved sets dissolved=True on the space."""
    await env.repo.save(_space("sp-md"))
    await env.repo.mark_dissolved("sp-md")
    fetched = await env.repo.get("sp-md")
    assert fetched.dissolved is True


async def test_increment_config_sequence_atomic(env):
    """increment_config_sequence returns a strictly increasing sequence."""
    await env.repo.save(_space("sp-seq"))
    v1 = await env.repo.increment_config_sequence("sp-seq")
    v2 = await env.repo.increment_config_sequence("sp-seq")
    assert v1 == 1
    assert v2 == 2


async def test_increment_config_sequence_concurrent(env):
    """Concurrent increments each return a unique sequence number."""
    await env.repo.save(_space("sp-conc"))
    results = await asyncio.gather(
        env.repo.increment_config_sequence("sp-conc"),
        env.repo.increment_config_sequence("sp-conc"),
        env.repo.increment_config_sequence("sp-conc"),
    )
    assert sorted(results) == [1, 2, 3]


async def test_fresh_space_has_zero_config_hlc(env):
    """A space saved without a config edit defaults to the HLC zero "0-0"."""
    await env.repo.save(_space("sp-hlc0"))
    space = await env.repo.get("sp-hlc0")
    assert space is not None
    assert space.config_hlc == "0-0"


async def test_config_hlc_round_trips_through_save_and_get(env):
    """config_hlc persists through the spaces upsert + row read."""
    sp = replace(_space("sp-hlc-rt"), config_hlc="1234567890-3")
    await env.repo.save(sp)
    loaded = await env.repo.get("sp-hlc-rt")
    assert loaded is not None
    assert loaded.config_hlc == "1234567890-3"


async def test_increment_config_sequence_advances_hlc(env):
    """increment_config_sequence advances config_hlc to a STRICTLY greater HLC
    on every call (the config-LWW later-edit-wins tie-break key)."""
    from socialhome.infrastructure.hlc import HLC

    await env.repo.save(_space("sp-hlc-adv"))
    base = HLC.parse((await env.repo.get("sp-hlc-adv")).config_hlc)
    seen = [base]
    for _ in range(3):
        await env.repo.increment_config_sequence("sp-hlc-adv")
        cur = HLC.parse((await env.repo.get("sp-hlc-adv")).config_hlc)
        assert cur > seen[-1]
        seen.append(cur)


async def test_increment_roster_sequence_atomic(env):
    """increment_roster_sequence returns a strictly increasing sequence."""
    await env.repo.save(_space("sp-rseq"))
    v1 = await env.repo.increment_roster_sequence("sp-rseq")
    v2 = await env.repo.increment_roster_sequence("sp-rseq")
    assert v1 == 1
    assert v2 == 2


async def test_increment_roster_sequence_concurrent(env):
    """Concurrent roster increments each return a unique sequence number."""
    await env.repo.save(_space("sp-rconc"))
    results = await asyncio.gather(
        env.repo.increment_roster_sequence("sp-rconc"),
        env.repo.increment_roster_sequence("sp-rconc"),
        env.repo.increment_roster_sequence("sp-rconc"),
    )
    assert sorted(results) == [1, 2, 3]


async def test_roster_sequence_independent_of_config_sequence(env):
    """Bumping config_sequence must not move roster_sequence and vice versa."""
    await env.repo.save(_space("sp-indep"))
    # Bump config twice, roster once.
    await env.repo.increment_config_sequence("sp-indep")
    await env.repo.increment_config_sequence("sp-indep")
    r1 = await env.repo.increment_roster_sequence("sp-indep")
    space = await env.repo.get("sp-indep")
    assert space is not None
    assert space.config_sequence == 2
    assert space.roster_sequence == 1
    assert r1 == 1


async def test_roster_sequence_round_trips_through_save_and_get(env):
    """roster_sequence persists through the spaces upsert + row read."""
    sp = replace(_space("sp-rrt"), roster_sequence=7)
    await env.repo.save(sp)
    loaded = await env.repo.get("sp-rrt")
    assert loaded is not None
    assert loaded.roster_sequence == 7
    # Upsert preserves an updated value too.
    await env.repo.save(replace(sp, roster_sequence=9))
    loaded2 = await env.repo.get("sp-rrt")
    assert loaded2 is not None
    assert loaded2.roster_sequence == 9


async def test_increment_roster_sequence_unknown_space_raises(env):
    with pytest.raises(KeyError):
        await env.repo.increment_roster_sequence("nope")


async def test_config_author_default_none_and_roundtrip(env):
    """The last-applied config author (v_24 LWW tie-break) defaults to NULL
    on a fresh space and round-trips through set/get_config_author."""
    await env.repo.save(_space("sp-author"))
    assert await env.repo.get_config_author("sp-author") is None
    await env.repo.set_config_author("sp-author", "peer-b")
    assert await env.repo.get_config_author("sp-author") == "peer-b"
    # Overwrite (a later applied edit) replaces it.
    await env.repo.set_config_author("sp-author", "peer-c")
    assert await env.repo.get_config_author("sp-author") == "peer-c"


async def test_get_config_author_none_for_unknown_space(env):
    assert await env.repo.get_config_author("nope") is None


# ── Members ────────────────────────────────────────────────────────────────


async def test_save_and_get_member(env):
    """save_member persists; get_member retrieves a single member row."""
    await env.repo.save(_space("sp-mem"))
    member = _member("sp-mem", "uid-alice", role="owner")
    await env.repo.save_member(member)
    fetched = await env.repo.get_member("sp-mem", "uid-alice")
    assert fetched is not None
    assert fetched.role == "owner"


async def test_list_members(env):
    """list_members returns all members of a space."""
    await env.repo.save(_space("sp-lm"))
    await env.repo.save_member(_member("sp-lm", "uid-alice", role="owner"))
    await env.repo.save_member(_member("sp-lm", "uid-bob", role="member"))
    members = await env.repo.list_members("sp-lm")
    user_ids = {m.user_id for m in members}
    assert user_ids == {"uid-alice", "uid-bob"}


async def test_delete_member(env):
    """delete_member removes the member row."""
    await env.repo.save(_space("sp-dm"))
    await env.repo.save_member(_member("sp-dm", "uid-bob"))
    await env.repo.delete_member("sp-dm", "uid-bob")
    assert await env.repo.get_member("sp-dm", "uid-bob") is None


async def test_set_role(env):
    """set_role updates a member's role."""
    await env.repo.save(_space("sp-role"))
    await env.repo.save_member(_member("sp-role", "uid-alice", role="member"))
    await env.repo.set_role("sp-role", "uid-alice", "admin")
    fetched = await env.repo.get_member("sp-role", "uid-alice")
    assert fetched.role == "admin"


async def test_set_role_admits_moderator(env):
    """``moderator`` (migration 0065) is a real seat the allow-list and the
    CHECK both admit."""
    await env.repo.save(_space("sp-mod-role"))
    await env.repo.save_member(_member("sp-mod-role", "uid-alice", role="member"))
    await env.repo.set_role("sp-mod-role", "uid-alice", "moderator")
    fetched = await env.repo.get_member("sp-mod-role", "uid-alice")
    assert fetched.role == "moderator"


async def test_set_role_invalid_raises(env):
    """set_role raises ValueError for an unknown role string."""
    await env.repo.save(_space("sp-bad-role"))
    await env.repo.save_member(_member("sp-bad-role", "uid-alice"))
    with pytest.raises(ValueError, match="invalid role"):
        await env.repo.set_role("sp-bad-role", "uid-alice", "superuser")


# ── Space instances ────────────────────────────────────────────────────────


async def test_add_and_list_space_instances(env):
    """add_space_instance adds an instance link; list_member_instances lists them."""
    await env.repo.save(_space("sp-inst"))
    await env.repo.add_space_instance("sp-inst", "inst-remote-1")
    await env.repo.add_space_instance("sp-inst", "inst-remote-2")
    instances = await env.repo.list_member_instances("sp-inst")
    assert set(instances) == {"inst-remote-1", "inst-remote-2"}


# ── Bans ───────────────────────────────────────────────────────────────────


async def test_ban_and_is_banned(env):
    """ban_member bans a user; is_banned returns True."""
    await env.repo.save(_space("sp-ban"))
    await env.repo.save_member(_member("sp-ban", "uid-bob"))
    await env.repo.ban_member("sp-ban", "uid-bob", "uid-alice")
    assert await env.repo.is_banned("sp-ban", "uid-bob") is True


async def test_unban_member(env):
    """unban_member removes the ban."""
    await env.repo.save(_space("sp-unban"))
    await env.repo.save_member(_member("sp-unban", "uid-bob"))
    await env.repo.ban_member("sp-unban", "uid-bob", "uid-alice")
    await env.repo.unban_member("sp-unban", "uid-bob")
    assert await env.repo.is_banned("sp-unban", "uid-bob") is False


async def test_list_bans(env):
    """list_bans returns the ban records for a space."""
    await env.repo.save(_space("sp-bans"))
    await env.repo.save_member(_member("sp-bans", "uid-bob"))
    await env.repo.ban_member("sp-bans", "uid-bob", "uid-alice", reason="spam")
    bans = await env.repo.list_bans("sp-bans")
    assert len(bans) == 1
    assert bans[0]["user_id"] == "uid-bob"


# ── Invite tokens ──────────────────────────────────────────────────────────


async def test_create_and_consume_invite_token(env):
    """create_invite_token produces a token that can be consumed once."""
    await env.repo.save(_space("sp-tok"))
    token = await env.repo.create_invite_token("sp-tok", "uid-alice", uses=1)
    assert token
    result = await env.repo.consume_invite_token(token)
    assert result is not None
    assert result["space_id"] == "sp-tok"


async def test_consume_exhausted_token_returns_none(env):
    """consume_invite_token returns None after all uses are consumed."""
    await env.repo.save(_space("sp-exhaust"))
    token = await env.repo.create_invite_token("sp-exhaust", "uid-alice", uses=1)
    await env.repo.consume_invite_token(token)
    result = await env.repo.consume_invite_token(token)
    assert result is None


async def test_consume_expired_tz_aware_token_returns_none(env):
    """Regression: a tz-aware ISO expiry in the past must deny.

    The guard used to compare ``expires_at`` to ``datetime('now')`` as a
    string. Python writes ``2026-09-18T15:25:42+00:00`` and SQLite emits
    ``2026-09-18 15:25:42``; ``T`` sorts above the space, so every
    expiry looked like it was in the future and no invite token ever
    expired — including the 5-minute ones minted for remote invites.
    """
    from datetime import datetime, timedelta, timezone

    await env.repo.save(_space("sp-expired"))
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    token = await env.repo.create_invite_token(
        "sp-expired",
        "uid-alice",
        uses=1,
        expires_at=past,
    )
    assert await env.repo.consume_invite_token(token) is None


async def test_consume_future_tz_aware_token_succeeds(env):
    from datetime import datetime, timedelta, timezone

    await env.repo.save(_space("sp-live"))
    soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    token = await env.repo.create_invite_token(
        "sp-live",
        "uid-alice",
        uses=1,
        expires_at=soon,
    )
    row = await env.repo.consume_invite_token(token)
    assert row is not None
    assert row["space_id"] == "sp-live"


async def test_consume_missing_token_returns_none(env):
    """consume_invite_token returns None for a non-existent token."""
    result = await env.repo.consume_invite_token("no-such-token")
    assert result is None


async def test_release_invite_token_use_hands_the_use_back(env):
    """A redeem whose ACK never reached the joiner gives its use back."""
    await env.repo.save(_space("sp-release"))
    token = await env.repo.create_invite_token("sp-release", "uid-alice", uses=1)
    assert await env.repo.consume_invite_token(token) is not None
    assert await env.repo.consume_invite_token(token) is None
    await env.repo.release_invite_token_use(token)
    row = await env.repo.consume_invite_token(token)
    assert row is not None
    assert row["uses_remaining"] == 0


async def test_concurrent_consumes_of_a_single_use_token_spend_it_once(env):
    await env.repo.save(_space("sp-race"))
    token = await env.repo.create_invite_token("sp-race", "uid-alice", uses=1)
    results = await asyncio.gather(
        *(env.repo.consume_invite_token(token) for _ in range(5)),
    )
    assert sum(r is not None for r in results) == 1


async def test_release_invite_token_use_never_exceeds_the_minted_total(env):
    """A stray release can never mint a use the admin never granted."""
    await env.repo.save(_space("sp-release-cap"))
    token = await env.repo.create_invite_token("sp-release-cap", "uid-alice", uses=2)
    await env.repo.release_invite_token_use(token)
    live = await env.repo.get_live_invite_token(token)
    assert live is not None
    assert live["uses_remaining"] == 2


async def test_release_invite_token_use_of_a_revoked_token_is_a_noop(env):
    await env.repo.save(_space("sp-release-gone"))
    token = await env.repo.create_invite_token("sp-release-gone", "uid-alice")
    await env.repo.consume_invite_token(token)
    await env.repo.delete_invite_token("sp-release-gone", token)
    await env.repo.release_invite_token_use(token)
    assert await env.repo.get_invite_token_space_id(token) is None


async def test_get_invite_token_space_id_reads_a_spent_token(env):
    """The retry-after-lost-ACK lookup: an exhausted token still names its
    space (the live predicate would not)."""
    await env.repo.save(_space("sp-spent"))
    token = await env.repo.create_invite_token("sp-spent", "uid-alice", uses=1)
    await env.repo.consume_invite_token(token)
    assert await env.repo.get_live_invite_token(token) is None
    assert await env.repo.get_invite_token_space_id(token) == "sp-spent"
    assert await env.repo.get_invite_token_space_id("no-such-token") is None


# ── Invitations ────────────────────────────────────────────────────────────


async def test_save_and_get_invitation(env):
    """save_invitation creates an invitation; get_invitation retrieves it."""
    await env.repo.save(_space("sp-inv"))
    inv_id = await env.repo.save_invitation(
        "sp-inv",
        "uid-bob",
        "uid-alice",
    )
    inv = await env.repo.get_invitation(inv_id)
    assert inv is not None
    assert inv["invited_user_id"] == "uid-bob"


async def test_update_invitation_status(env):
    """update_invitation_status changes the invitation's status field."""
    await env.repo.save(_space("sp-invst"))
    inv_id = await env.repo.save_invitation("sp-invst", "uid-bob", "uid-alice")
    await env.repo.update_invitation_status(inv_id, "accepted")
    inv = await env.repo.get_invitation(inv_id)
    assert inv["status"] == "accepted"


# ── Sidebar pins ───────────────────────────────────────────────────────────


async def test_pin_and_unpin_sidebar(env):
    """pin_sidebar adds; unpin_sidebar removes a pinned space."""
    await env.repo.save(_space("sp-pin"))
    await env.repo.pin_sidebar("uid-alice", "sp-pin", 0)
    # Verify it was inserted (no error)
    await env.repo.unpin_sidebar("uid-alice", "sp-pin")
    # Should not raise


# ── Aliases ────────────────────────────────────────────────────────────────


async def test_set_and_get_space_alias(env):
    """set_space_alias stores a personal alias; get_space_alias retrieves it."""
    await env.repo.save(_space("sp-alias"))
    await env.repo.set_space_alias("sp-alias", "alice", "Family Space")
    alias = await env.repo.get_space_alias("sp-alias", "alice")
    assert alias == "Family Space"


async def test_get_missing_alias_returns_none(env):
    """get_space_alias returns None when no alias is set."""
    await env.repo.save(_space("sp-noalias"))
    alias = await env.repo.get_space_alias("sp-noalias", "alice")
    assert alias is None


# ── Sidebar links ──────────────────────────────────────────────────────────


async def test_upsert_and_list_links(env):
    await env.repo.save(_space("sp-links"))
    await env.repo.upsert_link(
        link_id="l1",
        space_id="sp-links",
        label="Wiki",
        url="https://wiki",
        position=0,
    )
    await env.repo.upsert_link(
        link_id="l2",
        space_id="sp-links",
        label="Chat",
        url="https://chat",
        position=1,
    )
    links = await env.repo.list_links("sp-links")
    assert [link["id"] for link in links] == ["l1", "l2"]
    assert links[0]["label"] == "Wiki"
    assert links[1]["url"] == "https://chat"


async def test_upsert_link_updates_existing(env):
    await env.repo.save(_space("sp-up"))
    await env.repo.upsert_link(
        link_id="l1",
        space_id="sp-up",
        label="Wiki",
        url="https://old",
        position=0,
    )
    await env.repo.upsert_link(
        link_id="l1",
        space_id="sp-up",
        label="Wiki v2",
        url="https://new",
        position=3,
    )
    links = await env.repo.list_links("sp-up")
    assert len(links) == 1
    assert links[0]["label"] == "Wiki v2"
    assert links[0]["url"] == "https://new"
    assert links[0]["position"] == 3


async def test_delete_link(env):
    await env.repo.save(_space("sp-del"))
    await env.repo.upsert_link(
        link_id="l1",
        space_id="sp-del",
        label="Wiki",
        url="https://wiki",
        position=0,
    )
    await env.repo.delete_link("l1")
    assert await env.repo.list_links("sp-del") == []


async def test_get_link(env):
    await env.repo.save(_space("sp-get"))
    await env.repo.upsert_link(
        link_id="l1",
        space_id="sp-get",
        label="Wiki",
        url="https://wiki",
        position=0,
    )
    link = await env.repo.get_link("l1")
    assert link is not None
    assert link["space_id"] == "sp-get"
    assert await env.repo.get_link("missing") is None


# ── Join requests ───────────────────────────────────────────────────────────


async def test_list_pending_join_request_space_ids_for_user(env):
    """The by-user complement returns DISTINCT space_ids of the caller's
    own pending join-requests, excluding non-pending and other users'."""
    await env.repo.save(_space("sp-pend-a"))
    await env.repo.save(_space("sp-pend-b"))
    await env.repo.save(_space("sp-approved"))
    await env.repo.save(_space("sp-other"))

    # Two pending requests for alice (b twice → DISTINCT collapses).
    await env.repo.save_join_request("sp-pend-a", "uid-alice")
    await env.repo.save_join_request("sp-pend-b", "uid-alice")
    await env.repo.save_join_request("sp-pend-b", "uid-alice")
    # An approved request for alice → excluded.
    rid = await env.repo.save_join_request("sp-approved", "uid-alice")
    await env.repo.update_join_request_status(rid, "approved")
    # A pending request for bob → excluded (different user).
    await env.repo.save_join_request("sp-other", "uid-bob")

    ids = await env.repo.list_pending_join_request_space_ids_for_user("uid-alice")
    assert set(ids) == {"sp-pend-a", "sp-pend-b"}
    assert len(ids) == len(set(ids))  # DISTINCT — no duplicate space_ids
    assert "sp-approved" not in ids
    assert "sp-other" not in ids


async def test_list_pending_join_request_space_ids_for_user_empty(env):
    """A user with no pending requests gets an empty list."""
    assert (
        await env.repo.list_pending_join_request_space_ids_for_user("uid-alice") == []
    )


async def test_host_identity_pk_round_trips(env):
    """The mesh host's identity pubkey persists on the space stub (#648).

    A member that joined over the mesh has no ``remote_instances`` row for
    the host, so this column is the only place the §25.6 receiver can get
    a key to verify the host's per-chunk signatures from.
    """
    await env.repo.save(_space("sp-mesh"))
    assert await env.repo.get_host_identity_pk("sp-mesh") is None

    await env.repo.set_host_identity_pk("sp-mesh", "cc" * 32)
    assert await env.repo.get_host_identity_pk("sp-mesh") == "cc" * 32


async def test_host_identity_pk_survives_a_resave(env):
    """A later stub re-save (SPACE_CONFIG_CHANGED) must not clobber it.

    The stub is upserted every time the host's config changes; losing the
    key there would silently break sync again on the next config edit.
    """
    space = _space("sp-mesh")
    await env.repo.save(space)
    await env.repo.set_host_identity_pk("sp-mesh", "dd" * 32)

    await env.repo.save(replace(space, name="Renamed"))

    assert await env.repo.get_host_identity_pk("sp-mesh") == "dd" * 32


async def test_host_identity_pk_is_none_for_unknown_space(env):
    assert await env.repo.get_host_identity_pk("no-such-space") is None


# ── Invite-token expiry (shape-mismatch regression) ───────────────────────


def _expired_today_iso() -> str:
    """A tz-aware expiry at the very start of the current UTC day.

    Always in the past, and always on the *same calendar date* as
    SQLite's ``datetime('now')`` — precisely the window where the raw
    TEXT compare went wrong ("T" 0x54 > " " 0x20 only decides the
    comparison once the date digits tie). Using it makes the regression
    fire deterministically instead of only when the clock cooperates.
    """
    return f"{datetime.now(timezone.utc).date().isoformat()}T00:00:00.000001+00:00"


async def _expiry_token(env, space_id: str, expires_at: str | None) -> str:
    """Mint an invite token with an exact ``expires_at`` string."""
    await env.repo.save(_space(space_id))
    return await env.repo.create_invite_token(
        space_id, "uid-alice", uses=1, expires_at=expires_at
    )


async def test_consume_rejects_expired_tz_aware_token(env):
    """A tz-aware expiry an hour in the past must NOT be consumable.

    Regression: the guard compared the stored tz-aware ISO string
    (``2026-09-18T14:52:14.331881+00:00``) against SQLite's naive
    ``datetime('now')`` (``2026-09-18 15:52:14``) as raw TEXT. ``"T"``
    (0x54) sorts above ``" "`` (0x20), so an expired token read as still
    valid for the rest of the UTC day it expired on — short-lived invite
    tokens (5 min for remote invites) were effectively immortal.
    """
    token = await _expiry_token(env, "sp-exp-past", _expired_today_iso())
    assert await env.repo.consume_invite_token(token) is None


async def test_consume_rejects_expired_token_minted_minutes_ago(env):
    """The real ``invite_remote_user`` shape: a 5-minute TTL, 10 min old."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    token = await _expiry_token(env, "sp-exp-5min", past)
    assert await env.repo.consume_invite_token(token) is None


async def test_consume_accepts_future_tz_aware_token(env):
    """A tz-aware expiry in the future is still consumable."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    token = await _expiry_token(env, "sp-exp-future", future)
    result = await env.repo.consume_invite_token(token)
    assert result is not None
    assert result["space_id"] == "sp-exp-future"


async def test_consume_accepts_null_expiry(env):
    """``expires_at IS NULL`` means uses-limited only, never time-limited."""
    token = await _expiry_token(env, "sp-exp-null", None)
    assert await env.repo.consume_invite_token(token) is not None


async def test_consume_handles_naive_sqlite_shaped_expiry(env):
    """The naive ``YYYY-MM-DD HH:MM:SS`` shape works in both directions.

    No production writer emits this shape today, but the column's sibling
    ``created_at`` carries ``DEFAULT (datetime('now'))``, so a future
    SQL-side writer could. ``datetime()`` normalises it either way.
    """
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    future = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    expired = await _expiry_token(env, "sp-naive-past", past)
    live = await _expiry_token(env, "sp-naive-future", future)
    assert await env.repo.consume_invite_token(expired) is None
    assert await env.repo.consume_invite_token(live) is not None


async def test_consume_handles_fractional_seconds(env):
    """Fractional seconds don't break the comparison in either direction."""
    now = datetime.now(timezone.utc)
    expired = await _expiry_token(env, "sp-frac-past", _expired_today_iso())
    live = await _expiry_token(
        env, "sp-frac-future", (now + timedelta(minutes=30)).isoformat()
    )
    assert "." in _expired_today_iso()
    assert await env.repo.consume_invite_token(expired) is None
    assert await env.repo.consume_invite_token(live) is not None


async def test_consume_handles_non_utc_offset(env):
    """A non-UTC offset is converted to UTC, not ignored.

    ``2026-09-18T16:52:14+02:00`` is 14:52:14 UTC — an hour in the *past*
    at 15:52 UTC even though its wall-clock digits read as the future.
    SQLite's ``datetime()`` applies the offset, so the token is rejected.
    """
    now = datetime.now(timezone.utc)
    plus2 = timezone(timedelta(hours=2))
    minus5 = timezone(timedelta(hours=-5))
    # Past instant, but wall-clock digits an hour ahead of UTC "now".
    expired = await _expiry_token(
        env, "sp-off-past", (now - timedelta(hours=1)).astimezone(plus2).isoformat()
    )
    # Future instant, but wall-clock digits five hours behind UTC "now".
    live = await _expiry_token(
        env, "sp-off-future", (now + timedelta(hours=1)).astimezone(minus5).isoformat()
    )
    assert await env.repo.consume_invite_token(expired) is None
    assert await env.repo.consume_invite_token(live) is not None


async def test_list_expired_join_requests_sees_same_day_expiry(env):
    """``list_expired_join_requests`` normalises both sides too.

    ``space_join_requests.expires_at`` is written by Python as tz-aware
    ISO, so the same raw-TEXT mismatch hid a request that expired earlier
    today from the retention sweep.
    """
    await env.repo.save(_space("sp-jr"))
    rid = await env.repo.save_join_request(
        "sp-jr", "uid-bob", message="please", ttl_days=7
    )
    await env.db.enqueue(
        "UPDATE space_join_requests SET expires_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), rid),
    )
    rows = await env.repo.list_expired_join_requests()
    assert [r["id"] for r in rows] == [rid]


async def test_a_banned_redeemer_consumes_nothing(env):
    """§13.7 folded into the consume.

    It used to be a separate query run AFTER the UPDATE, with no refund,
    so a banned household could spend a twenty-use invite link in twenty
    requests — and the differential answer told it its own ban status.
    """
    await env.repo.save(_space("sp-banned"))
    token = await env.repo.create_invite_token("sp-banned", "uid-alice", uses=20)
    await env.repo.ban_member("sp-banned", "uid-bob", "uid-alice")

    for _ in range(3):
        assert (
            await env.repo.consume_invite_token(
                token,
                redeemer_user_id="uid-bob",
            )
            is None
        )

    # Not one use burned — the next honest redeemer still gets all twenty.
    row = await env.repo.consume_invite_token(token, redeemer_user_id="uid-carol")
    assert row is not None
    assert row["uses_remaining"] == 19


async def test_a_ban_in_another_space_does_not_block_this_token(env):
    """The ban subquery is correlated on the token's OWN space."""
    await env.repo.save(_space("sp-one"))
    await env.repo.save(_space("sp-two"))
    await env.repo.ban_member("sp-two", "uid-bob", "uid-alice")
    token = await env.repo.create_invite_token("sp-one", "uid-alice", uses=1)

    row = await env.repo.consume_invite_token(token, redeemer_user_id="uid-bob")
    assert row is not None
    assert row["space_id"] == "sp-one"


async def test_consume_without_a_redeemer_id_ignores_bans(env):
    """The local ``accept_invite_token`` path passes no redeemer and keeps
    its own ban check — the new argument must stay opt-in."""
    await env.repo.save(_space("sp-opt-in"))
    await env.repo.ban_member("sp-opt-in", "uid-bob", "uid-alice")
    token = await env.repo.create_invite_token("sp-opt-in", "uid-alice", uses=1)

    assert await env.repo.consume_invite_token(token) is not None


# ── Invite-token roles + publication (migration 0053) ──────────────────────


async def test_invite_token_defaults_to_a_member_seat(env):
    """Every token that existed before 0053 means ``member``, and so does
    one minted without saying otherwise."""
    await env.repo.save(_space("sp-role-default"))
    token = await env.repo.create_invite_token("sp-role-default", "uid-alice")
    row = await env.repo.consume_invite_token(token)
    assert row is not None
    assert row["role"] == "member"


async def test_invite_token_role_round_trips(env):
    """The seat is stored on the row and comes back out of the atomic
    consume — the redeemer never supplies it."""
    await env.repo.save(_space("sp-role"))
    for role in ("member", "subscriber", "admin"):
        token = await env.repo.create_invite_token("sp-role", "uid-alice", role=role)
        row = await env.repo.consume_invite_token(token)
        assert row is not None
        assert row["role"] == role


async def test_invite_token_rejects_an_unknown_role(env):
    """The CHECK is the on-disk authority — ``owner`` is not in it, and
    neither is anything invented."""
    import sqlite3

    await env.repo.save(_space("sp-role-bad"))
    for bad in ("owner", "superuser"):
        with pytest.raises(sqlite3.IntegrityError):
            await env.repo.create_invite_token("sp-role-bad", "uid-alice", role=bad)


async def test_invite_token_type_round_trips_and_defaults_to_gfs(env):
    """Migration 0079 — the link's type is stored on the row and comes back
    out of every read and the atomic consume."""
    await env.repo.save(_space("sp-via"))
    plain = await env.repo.create_invite_token("sp-via", "uid-alice")
    internal = await env.repo.create_invite_token("sp-via", "uid-alice", via="internal")
    listed = {
        r["token"]: r["via"] for r in await env.repo.list_live_invite_tokens("sp-via")
    }
    assert listed == {plain: "gfs", internal: "internal"}
    assert (await env.repo.get_live_invite_token(internal))["via"] == "internal"
    assert (await env.repo.consume_invite_token(internal))["via"] == "internal"
    assert (await env.repo.consume_invite_token(plain))["via"] == "gfs"
    with pytest.raises(ValueError):
        await env.repo.create_invite_token("sp-via", "uid-alice", via="relay")


async def test_delete_invite_tokens_via_drops_only_that_type(env):
    await env.repo.save(_space("sp-via-del"))
    await env.repo.save(_space("sp-via-other"))
    gfs = await env.repo.create_invite_token(
        "sp-via-del", "uid-alice", gfs_id="g1", gfs_token="gt", gfs_url="u"
    )
    internal = await env.repo.create_invite_token(
        "sp-via-del", "uid-alice", via="internal"
    )
    other = await env.repo.create_invite_token("sp-via-other", "uid-alice")
    rows = await env.repo.delete_invite_tokens_via("sp-via-del", "gfs")
    assert rows == [{"token": gfs, "gfs_id": "g1", "gfs_token": "gt", "gfs_url": "u"}]
    left = [r["token"] for r in await env.repo.list_live_invite_tokens("sp-via-del")]
    assert left == [internal]
    assert await env.repo.get_live_invite_token(other) is not None
    assert await env.repo.delete_invite_tokens_via("sp-via-del", "gfs") == []


async def test_private_gfs_round_trips_through_save(env):
    space = _space("sp-pgfs")
    await env.repo.save(
        replace(space, features=replace(space.features, private_gfs=True))
    )
    got = await env.repo.get("sp-pgfs")
    assert got.features.private_gfs is True
    await env.repo.save(replace(got, features=replace(got.features, private_gfs=False)))
    assert (await env.repo.get("sp-pgfs")).features.private_gfs is False


async def test_create_invite_token_accepts_a_caller_supplied_token(env):
    """The publish-first mint seals the token into the blob before the
    row exists, so it hands the value down."""
    await env.repo.save(_space("sp-supplied"))
    token = await env.repo.create_invite_token(
        "sp-supplied",
        "uid-alice",
        token="deadbeef" * 4,
    )
    assert token == "deadbeef" * 4
    assert await env.repo.consume_invite_token("deadbeef" * 4) is not None


async def test_list_live_invite_tokens_returns_the_gfs_triple(env):
    await env.repo.save(_space("sp-pub"))
    token = await env.repo.create_invite_token(
        "sp-pub",
        "uid-alice",
        role="subscriber",
        gfs_id="gfs-1",
        gfs_token="gt-1",
        gfs_url="https://relay.example.org/join/gt-1",
    )
    rows = await env.repo.list_live_invite_tokens("sp-pub")
    assert len(rows) == 1
    assert rows[0]["token"] == token
    assert rows[0]["role"] == "subscriber"
    assert rows[0]["gfs_id"] == "gfs-1"
    assert rows[0]["gfs_token"] == "gt-1"
    assert rows[0]["gfs_url"] == "https://relay.example.org/join/gt-1"


async def test_list_live_invite_tokens_excludes_spent_links(env):
    """An expired or exhausted token grants nothing; listing it would
    only invite an owner to 'revoke' something already dead."""
    from datetime import datetime, timedelta, timezone

    await env.repo.save(_space("sp-live-list"))
    live = await env.repo.create_invite_token("sp-live-list", "uid-alice", uses=2)
    exhausted = await env.repo.create_invite_token("sp-live-list", "uid-alice", uses=1)
    await env.repo.consume_invite_token(exhausted)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    await env.repo.create_invite_token(
        "sp-live-list",
        "uid-alice",
        expires_at=past,
    )
    rows = await env.repo.list_live_invite_tokens("sp-live-list")
    assert [r["token"] for r in rows] == [live]


async def test_list_live_invite_tokens_never_leaks_another_space(env):
    await env.repo.save(_space("sp-mine"))
    await env.repo.save(_space("sp-theirs"))
    mine = await env.repo.create_invite_token("sp-mine", "uid-alice")
    await env.repo.create_invite_token("sp-theirs", "uid-bob")
    rows = await env.repo.list_live_invite_tokens("sp-mine")
    assert [r["token"] for r in rows] == [mine]


async def test_delete_invite_token_returns_the_row_and_is_idempotent(env):
    await env.repo.save(_space("sp-del"))
    token = await env.repo.create_invite_token(
        "sp-del",
        "uid-alice",
        gfs_id="gfs-9",
        gfs_token="gt-9",
    )
    row = await env.repo.delete_invite_token("sp-del", token)
    assert row is not None
    assert row["gfs_id"] == "gfs-9"
    assert row["gfs_token"] == "gt-9"
    # Gone, and a second revoke is a no-op rather than an error.
    assert await env.repo.delete_invite_token("sp-del", token) is None
    assert await env.repo.consume_invite_token(token) is None


async def test_get_invite_token_role_reads_any_row_of_its_space(env):
    """Expired or spent rows still answer (a revoke works on them), and the
    lookup is scoped to the space like the delete."""
    await env.repo.save(_space("sp-role"))
    await env.repo.save(_space("sp-other"))
    admin = await env.repo.create_invite_token(
        "sp-role", "uid-alice", role="admin", expires_at="2000-01-01T00:00:00+00:00"
    )
    plain = await env.repo.create_invite_token("sp-role", "uid-alice")
    assert await env.repo.get_invite_token_role("sp-role", admin) == "admin"
    assert await env.repo.get_invite_token_role("sp-role", plain) == "member"
    assert await env.repo.get_invite_token_role("sp-other", admin) is None
    assert await env.repo.get_invite_token_role("sp-role", "nope") is None


async def test_delete_invite_token_is_scoped_to_its_space(env):
    """An admin of one space must not be able to revoke another space's
    link by guessing the token."""
    await env.repo.save(_space("sp-a"))
    await env.repo.save(_space("sp-b"))
    token = await env.repo.create_invite_token("sp-a", "uid-alice")
    assert await env.repo.delete_invite_token("sp-b", token) is None
    assert await env.repo.consume_invite_token(token) is not None


async def test_invite_token_remembers_the_minted_total(env):
    """``uses_remaining`` is decremented in place, so the total has to be
    stored or it is gone after the first redeem."""
    await env.repo.save(_space("sp-total"))
    token = await env.repo.create_invite_token("sp-total", "uid-alice", uses=10)
    await env.repo.consume_invite_token(token)
    rows = await env.repo.list_live_invite_tokens("sp-total")
    assert rows[0]["uses_total"] == 10
    assert rows[0]["uses_remaining"] == 9


async def test_save_join_request_records_a_requested_admin_role(env):
    """An admin/mod invite link seats the redeemer as a member and files a
    pending elevation here (migration 0055). A NULL requested_role is the
    historical "join as member" request; 'admin' is a pending promotion the
    owner approves. The column rides through list_pending_join_requests
    (SELECT *), so the routes and SPA see it with no query change.
    """
    await env.repo.save(_space("sp-elev"))
    plain = await env.repo.save_join_request("sp-elev", "uid-alice")
    elev = await env.repo.save_join_request(
        "sp-elev", "uid-bob", requested_role="admin"
    )
    assert plain != elev
    pending = {
        r["user_id"]: r for r in await env.repo.list_pending_join_requests("sp-elev")
    }
    assert pending["uid-alice"]["requested_role"] is None
    assert pending["uid-bob"]["requested_role"] == "admin"


async def test_feature_timetable_round_trips(env):
    """The Timetable tab toggle persists; a space that never set it is off."""
    await env.repo.save(
        replace(_space("sp-tt"), features=SpaceFeatures(timetable=True))
    )
    fetched = await env.repo.get("sp-tt")
    assert fetched is not None and fetched.features.timetable is True
    await env.repo.save(_space("sp-tt-off"))
    off = await env.repo.get("sp-tt-off")
    assert off is not None and off.features.timetable is False


# ── Moderation queue ───────────────────────────────────────────────────────


def _mod_item(
    item_id: str,
    *,
    space_id: str = "sp-1",
    submitted_by: str = "uid-bob",
    feature: str = "tasks",
    target_id: str = "t-1",
    submitted_at: datetime | None = None,
    status=None,
):
    from socialhome.domain.space import ModerationStatus, SpaceModerationItem

    now = submitted_at or datetime.now(timezone.utc)
    return SpaceModerationItem(
        id=item_id,
        space_id=space_id,
        feature=feature,
        action="edit",
        submitted_by=submitted_by,
        payload={"target_id": target_id, "patch": {"title": "New"}},
        current_snapshot='{"title": "Old"}',
        submitted_at=now,
        expires_at=now + timedelta(days=7),
        status=status or ModerationStatus.PENDING,
    )


async def test_moderation_insert_get_round_trips(env):
    await env.repo.save(_space())
    await env.repo.insert_moderation_item(_mod_item("m1"))
    got = await env.repo.get_moderation_item("m1")
    assert got is not None
    assert got.payload == {"target_id": "t-1", "patch": {"title": "New"}}
    assert got.current_snapshot == '{"title": "Old"}'
    assert got.status.value == "pending"


async def test_claim_moderation_item_only_once(env):
    from socialhome.domain.space import ModerationStatus

    await env.repo.save(_space())
    await env.repo.insert_moderation_item(_mod_item("m1"))
    first, second = await asyncio.gather(
        env.repo.claim_moderation_item(
            "m1", status=ModerationStatus.APPROVED, reviewed_by="uid-alice"
        ),
        env.repo.claim_moderation_item(
            "m1", status=ModerationStatus.REJECTED, reviewed_by="uid-alice"
        ),
    )
    assert sorted([first, second]) == [False, True]
    got = await env.repo.get_moderation_item("m1")
    assert got.status in (ModerationStatus.APPROVED, ModerationStatus.REJECTED)
    assert got.reviewed_by == "uid-alice"
    assert got.reviewed_at is not None


async def test_claim_records_reason_and_release_reopens(env):
    from socialhome.domain.space import ModerationStatus

    await env.repo.save(_space())
    await env.repo.insert_moderation_item(_mod_item("m1"))
    assert await env.repo.claim_moderation_item(
        "m1", status=ModerationStatus.REJECTED, reviewed_by="uid-alice", reason="no"
    )
    got = await env.repo.get_moderation_item("m1")
    assert got.rejection_reason == "no"
    # Releasing a claim of another status never touches the row.
    assert not await env.repo.release_moderation_item(
        "m1", claimed_status=ModerationStatus.APPROVED
    )
    assert (
        await env.repo.get_moderation_item("m1")
    ).status is ModerationStatus.REJECTED
    assert await env.repo.release_moderation_item(
        "m1", claimed_status=ModerationStatus.REJECTED
    )
    got = await env.repo.get_moderation_item("m1")
    assert got.status is ModerationStatus.PENDING
    assert got.reviewed_by is None
    assert got.rejection_reason is None


async def test_count_pending_space_and_submitter(env):
    from socialhome.domain.space import ModerationStatus

    await env.repo.save(_space())
    await env.repo.insert_moderation_item(_mod_item("m1"))
    await env.repo.insert_moderation_item(_mod_item("m2"))
    await env.repo.insert_moderation_item(_mod_item("m3", submitted_by="uid-alice"))
    await env.repo.insert_moderation_item(
        _mod_item("m4", status=ModerationStatus.REJECTED)
    )
    assert await env.repo.count_pending("sp-1") == 3
    assert await env.repo.count_pending("sp-1", submitted_by="uid-bob") == 2
    assert await env.repo.count_pending("sp-1", submitted_by="uid-carol") == 0


async def test_insert_moderation_item_if_absent_keeps_the_first_copy(env):
    """A federated submission keeps its id everywhere (v_43): a replay or a
    second delivery of the same id never overwrites the held row."""
    await env.repo.save(_space())
    assert await env.repo.insert_moderation_item_if_absent(_mod_item("m1"))
    second = replace(_mod_item("m1"), payload={"target_id": "t-9"})
    assert not await env.repo.insert_moderation_item_if_absent(second)
    got = await env.repo.get_moderation_item("m1")
    assert got.payload["target_id"] == "t-1"


async def test_claim_moderation_item_from_rejected_lets_an_approve_win(env):
    """Approve beats reject across households: an approval may claim a
    REJECTED row when the caller allows it; a reject never claims an
    APPROVED one."""
    from socialhome.domain.space import ModerationStatus

    await env.repo.save(_space())
    await env.repo.insert_moderation_item(_mod_item("m1"))
    assert await env.repo.claim_moderation_item(
        "m1", status=ModerationStatus.REJECTED, reviewed_by="uid-c", reason="no"
    )
    # The default (pending only) refuses.
    assert not await env.repo.claim_moderation_item(
        "m1", status=ModerationStatus.APPROVED, reviewed_by="uid-b"
    )
    assert await env.repo.claim_moderation_item(
        "m1",
        status=ModerationStatus.APPROVED,
        reviewed_by="uid-b",
        from_statuses=(ModerationStatus.PENDING, ModerationStatus.REJECTED),
    )
    got = await env.repo.get_moderation_item("m1")
    assert got.status is ModerationStatus.APPROVED
    assert got.reviewed_by == "uid-b"
    assert got.rejection_reason is None
    assert not await env.repo.claim_moderation_item(
        "m1", status=ModerationStatus.REJECTED, reviewed_by="uid-c"
    )


async def test_count_pending_from_instance_counts_that_households_submitters(env):
    """The per-sender-household cap (v_43) counts pending items whose
    submitter holds a seat on that household."""
    from socialhome.domain.space import ModerationStatus

    await env.repo.save(_space())
    for inst, uid in (("h-a", "u-a1"), ("h-a", "u-a2"), ("h-b", "u-b1")):
        await env.db.enqueue(
            "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
            " VALUES('sp-1', ?, ?, 'member')",
            (inst, uid),
        )
    await env.repo.insert_moderation_item(_mod_item("m1", submitted_by="u-a1"))
    await env.repo.insert_moderation_item(_mod_item("m2", submitted_by="u-a2"))
    await env.repo.insert_moderation_item(
        _mod_item("m3", submitted_by="u-a2", status=ModerationStatus.REJECTED)
    )
    await env.repo.insert_moderation_item(_mod_item("m4", submitted_by="u-b1"))
    assert await env.repo.count_pending_from_instance("sp-1", "h-a") == 2
    assert await env.repo.count_pending_from_instance("sp-1", "h-b") == 1
    assert await env.repo.count_pending_from_instance("sp-1", "h-x") == 0


async def test_a_tombstone_is_filled_counted_and_purged(env):
    """Early-decision tombstones (v_43): a late submission fills the
    content and keeps the decided status; they are counted per deciding
    household and purged once stale."""
    from socialhome.domain.space import ModerationStatus

    await env.repo.save(_space())
    tomb = replace(
        _mod_item("m1"),
        feature="",
        action="",
        submitted_by="",
        payload={},
        current_snapshot='{"tombstone_from": "h-c"}',
        status=ModerationStatus.REJECTED,
        reviewed_at=datetime.now(timezone.utc) - timedelta(days=20),
    )
    assert await env.repo.insert_moderation_item_if_absent(tomb)
    assert await env.repo.count_moderation_tombstones("h-c") == 1
    assert await env.repo.count_moderation_tombstones("h-x") == 0
    assert await env.repo.fill_moderation_tombstone(_mod_item("m1"))
    got = await env.repo.get_moderation_item("m1")
    assert (got.feature, got.status) == ("tasks", ModerationStatus.REJECTED)
    assert not await env.repo.fill_moderation_tombstone(_mod_item("m1"))
    assert await env.repo.count_moderation_tombstones("h-c") == 0
    stale = replace(tomb, id="m2")
    await env.repo.insert_moderation_item_if_absent(stale)
    assert await env.repo.delete_stale_tombstones(datetime.now(timezone.utc)) == 1
    assert await env.repo.get_moderation_item("m1") is not None


async def test_list_moderation_for_submitter_is_own_only(env):
    await env.repo.save(_space())
    await env.repo.insert_moderation_item(_mod_item("m1"))
    await env.repo.insert_moderation_item(_mod_item("m2", submitted_by="uid-alice"))
    mine = await env.repo.list_moderation_for_submitter("sp-1", "uid-bob")
    assert [i.id for i in mine] == ["m1"]


async def test_list_pending_for_target(env):
    from socialhome.domain.space import ModerationStatus

    await env.repo.save(_space())
    await env.repo.insert_moderation_item(_mod_item("m1", target_id="t-1"))
    await env.repo.insert_moderation_item(_mod_item("m2", target_id="t-2"))
    await env.repo.insert_moderation_item(
        _mod_item("m3", target_id="t-1", status=ModerationStatus.APPROVED)
    )
    got = await env.repo.list_pending_for_target("sp-1", "tasks", "t-1")
    assert [i.id for i in got] == ["m1"]


async def test_expire_due_marks_and_returns_only_overdue_pending(env):
    from socialhome.domain.space import ModerationStatus

    await env.repo.save(_space())
    old = datetime.now(timezone.utc) - timedelta(days=8)
    await env.repo.insert_moderation_item(_mod_item("old", submitted_at=old))
    await env.repo.insert_moderation_item(_mod_item("fresh"))
    expired = await env.repo.expire_due(datetime.now(timezone.utc))
    assert [i.id for i in expired] == ["old"]
    assert expired[0].status is ModerationStatus.EXPIRED
    assert (
        await env.repo.get_moderation_item("old")
    ).status is ModerationStatus.EXPIRED
    assert (
        await env.repo.get_moderation_item("fresh")
    ).status is ModerationStatus.PENDING
    # A second sweep finds nothing new.
    assert await env.repo.expire_due(datetime.now(timezone.utc)) == []


async def test_purge_payloads_nulls_decided_rows_only(env):
    from socialhome.domain.space import ModerationStatus

    await env.repo.save(_space())
    old = datetime.now(timezone.utc) - timedelta(days=10)
    await env.repo.insert_moderation_item(
        _mod_item("done", submitted_at=old, status=ModerationStatus.REJECTED)
    )
    await env.repo.insert_moderation_item(_mod_item("pending", submitted_at=old))
    await env.repo.insert_moderation_item(
        _mod_item("recent", status=ModerationStatus.APPROVED)
    )
    n = await env.repo.purge_payloads(datetime.now(timezone.utc) - timedelta(days=7))
    assert n == 1
    done = await env.repo.get_moderation_item("done")
    assert done.payload == {} and done.current_snapshot is None
    assert (await env.repo.get_moderation_item("pending")).payload
    assert (await env.repo.get_moderation_item("recent")).payload


# ── Space authority key rotation (v_44) ──────────────────────────────────


async def test_authority_key_epoch_defaults_to_zero(env):
    await env.repo.save(_space("sp-ep"))
    got = await env.repo.get("sp-ep")
    assert got is not None
    assert got.authority_key_epoch == 0


async def test_save_never_writes_the_pin_or_the_epoch(env):
    """``save`` is the stub upsert every inbound snapshot goes through, so
    neither the pin nor the epoch may move on it — only a verified cert
    (``adopt_authority_key``) or the owner's own rotation may."""
    await env.repo.save(_space("sp-pin"))
    assert await env.repo.adopt_authority_key("sp-pin", "cc" * 32, 3)
    await env.repo.save(
        replace(_space("sp-pin"), identity_public_key="dd" * 32, authority_key_epoch=9)
    )
    got = await env.repo.get("sp-pin")
    assert got.identity_public_key == "cc" * 32
    assert got.authority_key_epoch == 3


async def test_rotate_authority_key_is_compare_and_set(env):
    from socialhome.crypto import generate_identity_keypair

    await env.repo.save(_space("sp-rot"))
    kp = generate_identity_keypair()
    assert await env.repo.rotate_authority_key(
        "sp-rot", public_key_hex=kp.public_key.hex(), seed=kp.private_key, key_epoch=1
    )
    got = await env.repo.get("sp-rot")
    assert got.identity_public_key == kp.public_key.hex()
    assert got.authority_key_epoch == 1
    assert await env.repo.get_space_seed("sp-rot") == kp.private_key
    # A concurrent rotation that read the old epoch loses the race.
    other = generate_identity_keypair()
    assert not await env.repo.rotate_authority_key(
        "sp-rot",
        public_key_hex=other.public_key.hex(),
        seed=other.private_key,
        key_epoch=1,
    )
    assert (await env.repo.get("sp-rot")).identity_public_key == kp.public_key.hex()
    assert await env.repo.get_space_seed("sp-rot") == kp.private_key


async def test_rotate_authority_key_validates_seed(env):
    await env.repo.save(_space("sp-bad"))
    with pytest.raises(ValueError):
        await env.repo.rotate_authority_key(
            "sp-bad", public_key_hex="aa" * 32, seed=b"short", key_epoch=1
        )


async def test_adopt_authority_key_clears_the_old_seed(env):
    from socialhome.crypto import generate_identity_keypair

    await env.repo.save(_space("sp-adopt"))
    old = generate_identity_keypair()
    await env.repo.set_space_seed("sp-adopt", old.private_key)
    assert await env.repo.adopt_authority_key("sp-adopt", "ee" * 32, 2)
    got = await env.repo.get("sp-adopt")
    assert got.identity_public_key == "ee" * 32
    assert got.authority_key_epoch == 2
    assert await env.repo.get_space_seed("sp-adopt") is None


async def test_adopt_authority_key_refuses_an_older_or_equal_epoch(env):
    await env.repo.save(_space("sp-old"))
    assert await env.repo.adopt_authority_key("sp-old", "11" * 32, 3)
    assert not await env.repo.adopt_authority_key("sp-old", "22" * 32, 3)
    assert not await env.repo.adopt_authority_key("sp-old", "33" * 32, 2)
    got = await env.repo.get("sp-old")
    assert got.identity_public_key == "11" * 32
    assert got.authority_key_epoch == 3


async def test_adopt_unknown_space_is_false(env):
    assert not await env.repo.adopt_authority_key("sp-none", "11" * 32, 1)


async def test_clear_space_seed(env):
    from socialhome.crypto import generate_identity_keypair

    await env.repo.save(_space("sp-clear"))
    await env.repo.set_space_seed("sp-clear", generate_identity_keypair().private_key)
    await env.repo.clear_space_seed("sp-clear")
    assert await env.repo.get_space_seed("sp-clear") is None


async def test_save_config_baseline_applies_once_under_the_pinned_epoch(env):
    """F4: the baseline config reset saves, records its author and stamps
    ``authority_config_epoch`` in ONE transaction — only while the space
    pins ``epoch`` and no config was applied under that epoch yet."""
    await env.repo.save(_space("sp-base"))
    assert await env.repo.adopt_authority_key("sp-base", "cc" * 32, 5)
    renamed = replace(_space("sp-base"), name="Owner baseline")
    assert await env.repo.save_config_baseline(renamed, author="own-iid", epoch=5)
    got = await env.repo.get("sp-base")
    assert got.name == "Owner baseline"
    assert await env.repo.get_config_author("sp-base") == "own-iid"
    assert await env.repo.get_authority_config_epoch("sp-base") == 5
    # A config applied under epoch 5 already stands: a second baseline at
    # the same epoch moves nothing.
    again = replace(_space("sp-base"), name="Second")
    assert not await env.repo.save_config_baseline(again, author="x", epoch=5)
    assert (await env.repo.get("sp-base")).name == "Owner baseline"


async def test_save_config_baseline_older_than_keeps_a_config_from_that_key(env):
    """A missed-baseline catch-up resets only a config written under a key
    OLDER than ``older_than`` — one applied under that key itself stays."""
    await env.repo.save(_space("sp-cut"))
    assert await env.repo.adopt_authority_key("sp-cut", "cc" * 32, 5)
    await env.repo.mark_config_authority("sp-cut")  # config written under 5
    assert await env.repo.adopt_authority_key("sp-cut", "dd" * 32, 9)
    reset = replace(_space("sp-cut"), name="Owner snapshot")
    assert not await env.repo.save_config_baseline(
        reset, author="own-iid", epoch=9, older_than=5
    )
    assert (await env.repo.get("sp-cut")).name != "Owner snapshot"
    assert await env.repo.save_config_baseline(
        reset, author="own-iid", epoch=9, older_than=6
    )
    assert (await env.repo.get("sp-cut")).name == "Owner snapshot"
    assert await env.repo.get_authority_config_epoch("sp-cut") == 9


async def test_authority_baseline_tracks_the_claim(env):
    await env.repo.save(_space("sp-claim"))
    assert await env.repo.get_authority_baseline("sp-claim") == (0, 0)
    assert await env.repo.claim_authority_baseline("sp-claim", 4)
    assert await env.repo.get_authority_baseline("sp-claim") == (4, 0)
    assert await env.repo.get_authority_baseline("sp-none") == (0, 0)


async def test_adopting_past_an_unclaimed_pin_records_the_owed_baseline(env):
    """An inline adoption that moves the pin past an epoch whose bundle was
    never claimed records that epoch as owed — durably, so a later bundle
    still knows a baseline was missed although the pin no longer shows it."""
    await env.repo.save(_space("sp-owe"))
    assert await env.repo.adopt_authority_key("sp-owe", "11" * 32, 3)
    # Epoch 0 was never rotated: nothing owed for it.
    assert await env.repo.get_authority_baseline("sp-owe") == (0, 0)
    assert await env.repo.adopt_authority_key("sp-owe", "22" * 32, 5)
    assert await env.repo.get_authority_baseline("sp-owe") == (0, 3)
    assert await env.repo.adopt_authority_key("sp-owe", "33" * 32, 8)
    assert await env.repo.get_authority_baseline("sp-owe") == (0, 5)
    # Claiming a baseline settles what was owed.
    assert await env.repo.claim_authority_baseline("sp-owe", 8)
    assert await env.repo.get_authority_baseline("sp-owe") == (8, 0)
    # Moving past a CLAIMED pin owes nothing.
    assert await env.repo.adopt_authority_key("sp-owe", "44" * 32, 9)
    assert await env.repo.get_authority_baseline("sp-owe") == (8, 0)


async def test_save_config_baseline_refuses_when_the_pin_moved(env):
    await env.repo.save(_space("sp-moved"))
    assert await env.repo.adopt_authority_key("sp-moved", "cc" * 32, 7)
    stale = replace(_space("sp-moved"), name="Stale")
    assert not await env.repo.save_config_baseline(stale, author="x", epoch=6)
    assert (await env.repo.get("sp-moved")).name != "Stale"
    assert await env.repo.get_authority_config_epoch("sp-moved") == 0
    assert not await env.repo.save_config_baseline(stale, author="x", epoch=8)
    assert not await env.repo.save_config_baseline(
        replace(_space("nope"), name="Ghost"), author="x", epoch=7
    )


# ── Authority epoch echo state (v_46, migration 0068) ─────────────────────


async def test_authority_cert_is_kept_only_for_the_pinned_key(env):
    await env.repo.save(_space("sp-cert"))
    assert await env.repo.get_authority_cert("sp-cert") is None
    assert await env.repo.adopt_authority_key("sp-cert", "Ab" * 32, 3)
    cert = {"key_epoch": 3, "authority_pk": "ab" * 32}
    # Wrong epoch or wrong key: not the pin, never stored.
    assert not await env.repo.remember_authority_cert(
        "sp-cert", key_epoch=2, public_key_hex="ab" * 32, cert=cert
    )
    assert not await env.repo.remember_authority_cert(
        "sp-cert", key_epoch=3, public_key_hex="cd" * 32, cert=cert
    )
    assert await env.repo.get_authority_cert("sp-cert") is None
    assert await env.repo.remember_authority_cert(
        "sp-cert", key_epoch=3, public_key_hex="AB" * 32, cert=cert
    )
    assert await env.repo.get_authority_cert("sp-cert") == cert
    assert await env.repo.get_authority_cert("sp-none") is None


async def test_authority_echo_round_trips_and_clears(env):
    await env.repo.save(_space("sp-echo"))
    assert await env.repo.get_authority_echo("sp-echo") == {}
    await env.repo.set_authority_echo("sp-echo", {"forgotten_epoch": 7})
    assert await env.repo.get_authority_echo("sp-echo") == {"forgotten_epoch": 7}
    await env.repo.set_authority_echo("sp-echo", None)
    assert await env.repo.get_authority_echo("sp-echo") == {}
    assert await env.repo.get_authority_echo("sp-none") == {}
    await env.db.enqueue(
        "UPDATE spaces SET authority_echo_json='[1]', authority_cert_json='\"x\"'"
        " WHERE id='sp-echo'"
    )
    assert await env.repo.get_authority_echo("sp-echo") == {}
    assert await env.repo.get_authority_cert("sp-echo") is None


async def test_save_never_writes_the_echo_columns(env):
    await env.repo.save(_space("sp-keep"))
    await env.repo.set_authority_echo("sp-keep", {"forgotten_epoch": 2})
    await env.repo.save(_space("sp-keep"))
    assert await env.repo.get_authority_echo("sp-keep") == {"forgotten_epoch": 2}


async def test_owner_user_id_round_trips_and_survives_a_resave(env):
    """Migration 0070: the stub's owner seat, set only by its own setter —
    a later config re-save must not clear it."""
    space = _space("sp-stub")
    await env.repo.save(space)
    assert await env.repo.get_owner_user_id("sp-stub") is None
    await env.repo.set_owner_user_id("sp-stub", "u-owner")
    await env.repo.save(replace(space, name="Renamed"))
    assert await env.repo.get_owner_user_id("sp-stub") == "u-owner"
    assert await env.repo.get_owner_user_id("sp-missing") is None


# ─── Private-space channels (migration 0077) ─────────────────────────────


async def test_gfs_channel_round_trip_and_candidate_lookup(env):
    await env.repo.save(_space("sp-1"))
    await env.repo.save(_space("sp-2"))
    assert await env.repo.get_gfs_channel("sp-1") is None
    assert await env.repo.set_gfs_channel("sp-1", "c" * 32, "pk-1")
    assert await env.repo.get_gfs_channel("sp-1") == ("c" * 32, "pk-1")
    assert await env.repo.spaces_for_gfs_channel("c" * 32) == ["sp-1"]
    # No first-come claim: another space may name the same id too (inbound
    # frames try each candidate; the content key decides).
    assert await env.repo.set_gfs_channel("sp-2", "c" * 32, "pk-2")
    assert await env.repo.spaces_for_gfs_channel("c" * 32) == ["sp-1", "sp-2"]
    assert await env.repo.set_gfs_channel("sp-2", None, None)
    # A save of the space (config upsert) keeps the channel.
    await env.repo.save(_space("sp-1", name="Renamed"))
    assert await env.repo.get_gfs_channel("sp-1") == ("c" * 32, "pk-1")
    # Forget it.
    assert await env.repo.set_gfs_channel("sp-1", None, None)
    assert await env.repo.get_gfs_channel("sp-1") is None
    assert await env.repo.spaces_for_gfs_channel("c" * 32) == []
    assert not await env.repo.set_gfs_channel("sp-missing", "d" * 32, "pk")


async def test_gfs_channel_healed_at_round_trip(env):
    await env.repo.save(_space("sp-1"))
    assert await env.repo.get_gfs_channel_healed_at("sp-1") is None
    await env.repo.set_gfs_channel_healed_at("sp-1", "2026-10-04T12:00:00+00:00")
    assert (
        await env.repo.get_gfs_channel_healed_at("sp-1") == "2026-10-04T12:00:00+00:00"
    )
    # A save of the space (config upsert) keeps it.
    await env.repo.save(_space("sp-1", name="Renamed"))
    assert await env.repo.get_gfs_channel_healed_at("sp-1") is not None
    assert await env.repo.get_gfs_channel_healed_at("sp-missing") is None
