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


async def test_consume_missing_token_returns_none(env):
    """consume_invite_token returns None for a non-existent token."""
    result = await env.repo.consume_invite_token("no-such-token")
    assert result is None


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
