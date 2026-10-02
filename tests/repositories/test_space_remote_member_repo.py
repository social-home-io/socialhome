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


async def test_list_admin_instances_never_lists_a_moderator_household(repo):
    """The delegated signing seed goes to ``list_admin_instances`` — a
    moderator holds content authority only and must never receive it."""
    await repo.add(
        space_id="sp1",
        instance_id="i-mod",
        user_id="u1",
        user_pk=None,
        display_name=None,
        role=SpaceRole.MODERATOR.value,
    )
    assert (await repo.get("sp1", "i-mod", "u1")).role == "moderator"
    assert await repo.list_admin_instances("sp1") == []


async def test_list_instances_with_roles_picks_live_seats_of_those_roles(repo):
    """The federated-moderation reviewer set (v_43): every household with a
    live ``admin`` or ``moderator`` seat, each once — never a member-only
    household, never one whose only such seat is tombstoned."""
    for inst, uid, role in (
        ("i-adm", "u1", SpaceRole.ADMIN.value),
        ("i-adm", "u2", SpaceRole.MODERATOR.value),
        ("i-mod", "u3", SpaceRole.MODERATOR.value),
        ("i-mem", "u4", SpaceRole.MEMBER.value),
        ("i-sub", "u5", SpaceRole.SUBSCRIBER.value),
        ("i-gone", "u6", SpaceRole.MODERATOR.value),
    ):
        await repo.add(
            space_id="sp1",
            instance_id=inst,
            user_id=uid,
            user_pk=None,
            display_name=None,
            role=role,
        )
    await repo.remove("sp1", "i-gone", "u6")
    got = await repo.list_instances_with_roles(
        "sp1", frozenset({SpaceRole.ADMIN.value, SpaceRole.MODERATOR.value})
    )
    assert sorted(got) == ["i-adm", "i-mod"]
    # ``list_admin_instances`` is the admin-only form of the same query.
    assert await repo.list_admin_instances("sp1") == ["i-adm"]
    assert await repo.list_instances_with_roles("sp1", frozenset()) == []


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


async def test_re_adding_a_tombstoned_member_clears_the_tombstone(repo):
    """A kicked household that is legitimately re-invited must come back
    LIVE. ``add`` used to leave ``tombstoned=1`` standing, and the §24.11
    space-writer gate reads tombstones — so the re-seated household hit
    the gate's ``else → refuse`` branch on every write, silently, for
    ever (the refusal answers ``{"status": "ok"}``, so the sender never
    retries either)."""
    await repo.add(
        space_id="sp1",
        instance_id="i-a",
        user_id="u1",
        user_pk=None,
        display_name=None,
    )
    await repo.remove("sp1", "i-a", "u1")
    assert await repo.get("sp1", "i-a", "u1") is None
    await repo.add(
        space_id="sp1",
        instance_id="i-a",
        user_id="u1",
        user_pk=None,
        display_name="Ada",
    )
    live = await repo.get("sp1", "i-a", "u1")
    assert live is not None
    assert live.tombstoned is False
    assert live.display_name == "Ada"


async def test_re_adding_bumps_member_version_so_gossip_converges(repo):
    """``remove`` bumps the version; the re-seat has to out-rank it or the
    CRDT merge in ``apply_member_event`` would drop the resurrection as
    stale the moment a peer gossiped the older tombstone back."""
    await repo.add(
        space_id="sp1",
        instance_id="i-a",
        user_id="u1",
        user_pk=None,
        display_name=None,
    )
    await repo.remove("sp1", "i-a", "u1")
    tombstone = await repo.get_including_tombstones("sp1", "i-a", "u1")
    assert tombstone is not None
    await repo.add(
        space_id="sp1",
        instance_id="i-a",
        user_id="u1",
        user_pk=None,
        display_name=None,
    )
    reseated = await repo.get("sp1", "i-a", "u1")
    assert reseated is not None
    assert reseated.member_version > tombstone.member_version


async def test_re_adding_a_live_member_leaves_the_version_alone(repo):
    """The bump belongs to the state CHANGE, not to the write. A profile
    refresh that re-adds a live seat must not race the roster gossip's own
    counter (``roster_sequence``) upwards — a local version that outran it
    would make the next legitimate gossip read as stale."""
    await repo.add(
        space_id="sp1",
        instance_id="i-a",
        user_id="u1",
        user_pk=None,
        display_name=None,
    )
    before = await repo.get("sp1", "i-a", "u1")
    await repo.add(
        space_id="sp1",
        instance_id="i-a",
        user_id="u1",
        user_pk=None,
        display_name="Ada",
    )
    after = await repo.get("sp1", "i-a", "u1")
    assert before is not None and after is not None
    assert after.member_version == before.member_version


async def test_reset_member_state_ignores_the_version_guard(repo):
    """v_44 baseline reset: the owner's rotation bundle sets a seat's state
    even when a revoked seed holder inflated the version above it."""
    await repo.apply_member_event(
        space_id="sp1",
        user_id="u1",
        instance_id="i-a",
        display_name="Old",
        user_pk=None,
        role="admin",
        member_version=10**9,
        tombstoned=False,
    )
    await repo.reset_member_state(
        space_id="sp1",
        user_id="u1",
        instance_id="i-a",
        display_name="Owner's view",
        user_pk="pk",
        role="member",
        member_version=7,
        tombstoned=False,
    )
    got = await repo.get("sp1", "i-a", "u1")
    assert (got.role, got.member_version, got.display_name) == (
        "member",
        7,
        "Owner's view",
    )
    # And a fresh row is inserted, tombstone included.
    await repo.reset_member_state(
        space_id="sp1",
        user_id="u9",
        instance_id="i-b",
        display_name=None,
        user_pk=None,
        role="member",
        member_version=3,
        tombstoned=True,
    )
    gone = await repo.get_including_tombstones("sp1", "i-b", "u9")
    assert gone.tombstoned and gone.member_version == 3


async def test_apply_member_event_with_a_stale_verified_epoch_is_dropped(repo):
    """v_44: an event verified against the epoch-0 key cannot land once the
    space pins a newer key — in the same statement as the write."""
    kw = dict(
        space_id="sp1",
        user_id="u1",
        instance_id="i-a",
        display_name=None,
        user_pk=None,
        role="admin",
        member_version=5,
        tombstoned=False,
    )
    await repo._db.enqueue("UPDATE spaces SET authority_key_epoch=7 WHERE id='sp1'")
    assert not await repo.apply_member_event(**kw, verified_epoch=0)
    assert await repo.get_including_tombstones("sp1", "i-a", "u1") is None
    assert await repo.apply_member_event(**kw, verified_epoch=7)
    assert (await repo.get("sp1", "i-a", "u1")).authority_epoch == 7
