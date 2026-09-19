"""Tests for socialhome.repositories.space_remote_member_repo."""

from __future__ import annotations

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.space import SpaceRole
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "srm.db", batch_timeout_ms=10)
    await db.startup()
    # ``space_remote_members.space_id`` FKs ``spaces.id`` — seat a parent
    # row so the inserts below satisfy the constraint.
    spaces = SqliteSpaceRepo(db)
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )

    await spaces.save(
        Space(
            id="sp1",
            name="S",
            owner_instance_id="host",
            owner_username="anna",
            identity_public_key="00" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    r = SqliteSpaceRemoteMemberRepo(db)
    yield r
    await db.shutdown()


async def test_list_admin_instances_distinct_and_admin_only(repo):
    """Returns the DISTINCT instance_ids of remote members whose role is
    ADMIN — never MEMBER, and a household with two admins appears once."""
    await repo.add(
        space_id="sp1", instance_id="i-a", user_id="u1", user_pk=None, display_name=None
    )
    await repo.add(
        space_id="sp1", instance_id="i-a", user_id="u2", user_pk=None, display_name=None
    )
    await repo.add(
        space_id="sp1", instance_id="i-b", user_id="u3", user_pk=None, display_name=None
    )
    await repo.add(
        space_id="sp1", instance_id="i-c", user_id="u4", user_pk=None, display_name=None
    )
    # Two admins on i-a, one admin on i-b, i-c stays a plain member.
    await repo.set_role("sp1", "i-a", "u1", SpaceRole.ADMIN)
    await repo.set_role("sp1", "i-a", "u2", SpaceRole.ADMIN)
    await repo.set_role("sp1", "i-b", "u3", SpaceRole.ADMIN)

    admins = await repo.list_admin_instances("sp1")
    assert sorted(admins) == ["i-a", "i-b"]


async def test_list_admin_instances_empty_when_no_admins(repo):
    await repo.add(
        space_id="sp1", instance_id="i-a", user_id="u1", user_pk=None, display_name=None
    )
    assert await repo.list_admin_instances("sp1") == []


# ─── member_version + tombstone convergence ───────────────────────────────


def _evt(**over):
    base = dict(
        space_id="sp1",
        user_id="u1",
        instance_id="i-a",
        display_name="Anna",
        user_pk="pk1",
        role="member",
        member_version=1,
        tombstoned=False,
    )
    base.update(over)
    return base


async def test_add_defaults_to_version_zero_live(repo):
    await repo.add(
        space_id="sp1", instance_id="i-a", user_id="u1", user_pk=None, display_name=None
    )
    row = await repo.get("sp1", "i-a", "u1")
    assert row is not None
    assert row.member_version == 0
    assert row.tombstoned is False


async def test_apply_member_event_applies_higher_version(repo):
    assert await repo.apply_member_event(**_evt(member_version=1)) is True
    assert await repo.apply_member_event(**_evt(member_version=2, role="admin")) is True
    row = await repo.get("sp1", "i-a", "u1")
    assert row.member_version == 2
    assert row.role == "admin"


async def test_apply_member_event_ignores_lower_version(repo):
    assert await repo.apply_member_event(**_evt(member_version=5, role="admin")) is True
    # Stale lower-version event must be ignored.
    assert (
        await repo.apply_member_event(**_evt(member_version=3, role="member")) is False
    )
    row = await repo.get("sp1", "i-a", "u1")
    assert row.member_version == 5
    assert row.role == "admin"


async def test_apply_member_event_ignores_equal_version_non_tombstone(repo):
    assert (
        await repo.apply_member_event(**_evt(member_version=2, role="member")) is True
    )
    assert (
        await repo.apply_member_event(**_evt(member_version=2, role="admin")) is False
    )
    row = await repo.get("sp1", "i-a", "u1")
    assert row.role == "member"


async def test_apply_member_event_equal_version_tombstone_wins(repo):
    assert await repo.apply_member_event(**_evt(member_version=2)) is True
    # Removal-wins-tie: equal version + tombstone beats a live row.
    assert (
        await repo.apply_member_event(**_evt(member_version=2, tombstoned=True)) is True
    )
    # Tombstoned → hidden from live roster, but persists in the table.
    assert await repo.get("sp1", "i-a", "u1") is None
    assert await repo.list_for_space("sp1") == []


async def test_apply_member_event_no_resurrection(repo):
    # Removed at version 3 (tombstone).
    assert (
        await repo.apply_member_event(**_evt(member_version=3, tombstoned=True)) is True
    )
    # A replayed older JOINED for the same user must NOT resurrect them.
    assert (
        await repo.apply_member_event(**_evt(member_version=2, tombstoned=False))
        is False
    )
    assert await repo.get("sp1", "i-a", "u1") is None
    # A higher-version JOIN legitimately re-adds them.
    assert (
        await repo.apply_member_event(**_evt(member_version=4, tombstoned=False))
        is True
    )
    row = await repo.get("sp1", "i-a", "u1")
    assert row is not None and row.member_version == 4


async def test_list_for_space_hides_tombstones(repo):
    await repo.add(
        space_id="sp1", instance_id="i-a", user_id="u1", user_pk=None, display_name=None
    )
    await repo.add(
        space_id="sp1", instance_id="i-b", user_id="u2", user_pk=None, display_name=None
    )
    await repo.remove("sp1", "i-a", "u1")
    live = await repo.list_for_space("sp1")
    assert [m.user_id for m in live] == ["u2"]
    # The tombstoned row is retained and visible to the convergence path.
    everything = await repo.list_for_space_including_tombstones("sp1")
    assert sorted(m.user_id for m in everything) == ["u1", "u2"]


async def test_remove_tombstones_rather_than_deletes(repo):
    await repo.add(
        space_id="sp1", instance_id="i-a", user_id="u1", user_pk=None, display_name=None
    )
    await repo.remove("sp1", "i-a", "u1")
    # Live reads treat it as gone.
    assert await repo.get("sp1", "i-a", "u1") is None
    # But the row persists as a version-bumped tombstone.
    rows = await repo.list_for_space_including_tombstones("sp1")
    assert len(rows) == 1
    assert rows[0].tombstoned is True
    assert rows[0].member_version >= 1


# ─── list_for_instance / add(role=) — the §24.11 Follower gate's reads ───


async def test_list_for_instance_returns_every_seat_of_one_household(repo):
    """The §24.11 space-writer gate asks a HOUSEHOLD-level question, so it
    needs every seat the household holds in the space — a household with
    one Follower and one full member may write."""
    await repo.add(
        space_id="sp1",
        instance_id="i-a",
        user_id="u1",
        user_pk=None,
        display_name=None,
        role=SpaceRole.SUBSCRIBER.value,
    )
    await repo.add(
        space_id="sp1",
        instance_id="i-a",
        user_id="u2",
        user_pk=None,
        display_name=None,
    )
    await repo.add(
        space_id="sp1",
        instance_id="i-b",
        user_id="u3",
        user_pk=None,
        display_name=None,
    )
    seats = await repo.list_for_instance("sp1", "i-a")
    assert sorted((s.user_id, s.role) for s in seats) == [
        ("u1", SpaceRole.SUBSCRIBER.value),
        ("u2", SpaceRole.MEMBER.value),
    ]


async def test_list_for_instance_includes_tombstones_by_default(repo):
    """A kicked household must not read as "no row" — that is the
    leniency reserved for a roster that has not converged, and handing it
    to a household we deliberately removed turns a kick into a bypass."""
    await repo.add(
        space_id="sp1",
        instance_id="i-a",
        user_id="u1",
        user_pk=None,
        display_name=None,
    )
    await repo.remove("sp1", "i-a", "u1")
    seats = await repo.list_for_instance("sp1", "i-a")
    assert [(s.user_id, s.tombstoned) for s in seats] == [("u1", True)]
    live_only = await repo.list_for_instance("sp1", "i-a", include_tombstoned=False)
    assert live_only == []


async def test_list_for_instance_is_empty_for_a_household_we_never_seated(repo):
    assert await repo.list_for_instance("sp1", "i-nobody") == []


async def test_add_lands_the_role_in_the_same_insert(repo):
    """One write, not add-then-set_role: a crash between the two used to
    leave a Follower seated as a full member."""
    await repo.add(
        space_id="sp1",
        instance_id="i-a",
        user_id="u1",
        user_pk=None,
        display_name=None,
        role=SpaceRole.SUBSCRIBER.value,
    )
    row = await repo.get("sp1", "i-a", "u1")
    assert row is not None and row.role == SpaceRole.SUBSCRIBER.value


async def test_add_keeps_defaulting_to_member(repo):
    await repo.add(
        space_id="sp1", instance_id="i-a", user_id="u1", user_pk=None, display_name=None
    )
    row = await repo.get("sp1", "i-a", "u1")
    assert row is not None and row.role == SpaceRole.MEMBER.value


async def test_add_updates_the_role_of_an_existing_seat(repo):
    """The upsert is how a re-redeem lands, so the role has to move with
    it — otherwise a re-seated Follower keeps a stale member role."""
    await repo.add(
        space_id="sp1", instance_id="i-a", user_id="u1", user_pk=None, display_name=None
    )
    await repo.add(
        space_id="sp1",
        instance_id="i-a",
        user_id="u1",
        user_pk=None,
        display_name=None,
        role=SpaceRole.SUBSCRIBER.value,
    )
    row = await repo.get("sp1", "i-a", "u1")
    assert row is not None and row.role == SpaceRole.SUBSCRIBER.value
