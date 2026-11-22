"""Tests for :mod:`socialhome.federation.sync.space.trusted_helpers`."""

from __future__ import annotations

from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.space import SpaceRole
from socialhome.federation.sync.space.trusted_helpers import (
    DELIVERER_ROLES,
    HELPER_ROLES,
    helper_offset,
    member_deliverers,
    trusted_helpers,
)

V92 = FederationCapability.MIN_FOR_SPACE_MEDIA_WANTED
V95 = FederationCapability.MIN_FOR_SYNC_PAGES


class _Seats:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids
        self.asked: list[frozenset[str]] = []

    async def list_instances_with_roles(self, space_id, roles):
        self.asked.append(frozenset(roles))
        return list(self.ids)


class _Fed:
    def __init__(self, versions: dict[str, int]) -> None:
        self.versions = versions

    async def space_member_supports(self, instance_id, *, min_version):
        return self.versions.get(instance_id, 0) >= min_version


def test_helpers_are_admin_and_moderator_households_not_the_owner() -> None:
    """The owner is the host, asked first anyway."""
    assert HELPER_ROLES == frozenset({SpaceRole.ADMIN.value, SpaceRole.MODERATOR.value})


async def test_sorted_without_self_the_hosts_and_older_households() -> None:
    seats = _Seats(["mod-c", "self", "host", "admin-a", "old-b", "", "mod-c"])
    fed = _Fed({"mod-c": V92, "admin-a": V92 + 1, "old-b": V92 - 1, "host": V92})
    got = await trusted_helpers(seats, fed, "sp", exclude={"self", "host"}, offset=0)
    assert got == ["admin-a", "mod-c"]
    assert seats.asked == [HELPER_ROLES]


async def test_the_list_starts_at_the_offset_and_wraps() -> None:
    seats = _Seats(["a", "b", "c"])
    fed = _Fed({"a": V92, "b": V92, "c": V92})
    assert await trusted_helpers(seats, fed, "sp", exclude=(), offset=1) == [
        "b",
        "c",
        "a",
    ]
    assert await trusted_helpers(seats, fed, "sp", exclude=(), offset=5) == [
        "c",
        "a",
        "b",
    ]


async def test_no_helper_at_v92_is_an_empty_list() -> None:
    seats = _Seats(["a"])
    assert await trusted_helpers(seats, _Fed({"a": V92 - 1}), "sp", exclude=()) == []


def test_the_offset_is_stable_and_differs_per_household() -> None:
    assert helper_offset("member-a", "sp") == helper_offset("member-a", "sp")
    assert helper_offset("member-a", "sp") != helper_offset("member-b", "sp")


def test_deliverers_hold_any_member_seat_never_a_followers() -> None:
    assert DELIVERER_ROLES == frozenset(
        {
            SpaceRole.OWNER.value,
            SpaceRole.ADMIN.value,
            SpaceRole.MODERATOR.value,
            SpaceRole.MEMBER.value,
        }
    )
    assert SpaceRole.SUBSCRIBER.value not in DELIVERER_ROLES


async def test_deliverers_are_v95_member_households_rotated() -> None:
    """Sync pages Phase 3: a plain member qualifies (the rows are verified,
    not trusted); a household before v_95 cannot answer a fetch."""
    seats = _Seats(["m-b", "self", "host", "m-a", "old"])
    fed = _Fed({"m-a": V95, "m-b": V95 + 1, "old": V95 - 1, "host": V95})
    got = await member_deliverers(seats, fed, "sp", exclude={"self", "host"})
    assert got == ["m-a", "m-b"]
    assert seats.asked == [DELIVERER_ROLES]
    assert await member_deliverers(
        seats, fed, "sp", exclude={"self", "host"}, offset=1
    ) == ["m-b", "m-a"]
