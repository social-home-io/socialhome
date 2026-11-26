"""SpaceWriterHouseholds — the member households of a space (writer seats),
never a follower-only household nor ourselves; with ``roles=SEAT_ROLES``
every seated household, a follower's included."""

from __future__ import annotations

from types import SimpleNamespace

from socialhome.domain.space import SpaceRole
from socialhome.services.space_writer_households import (
    SEAT_ROLES,
    SpaceWriterHouseholds,
    holds_member_seat,
)


class _Seats:
    def __init__(self, seats: dict[str, str]) -> None:
        self._seats = seats  # instance_id -> role
        self.asked: list[frozenset[str]] = []

    async def list_instances_with_roles(self, space_id, roles):
        self.asked.append(frozenset(roles))
        return [iid for iid, role in self._seats.items() if role in roles]


async def test_member_households_leave_out_followers_and_ourselves():
    seats = _Seats(
        {
            "house-a": "admin",
            "house-m": "member",
            "house-f": "subscriber",
            "me": "member",
        }
    )
    members = SpaceWriterHouseholds(seats, own_instance_id="me")
    assert await members.households("sp") == {"house-a", "house-m"}
    assert "subscriber" not in seats.asked[0]
    assert await members.may_receive("sp", "house-m")
    assert not await members.may_receive("sp", "house-f")
    assert not await members.may_receive("sp", "me")


async def test_the_seat_set_counts_every_role_a_followers_included():
    """``roles=SEAT_ROLES`` (the member directory): a follower is reached
    like a member, so its seat counts; ourselves still never."""
    seats = _Seats({"house-m": "member", "house-f": "subscriber", "me": "owner"})
    seated = SpaceWriterHouseholds(seats, own_instance_id="me", roles=SEAT_ROLES)
    assert await seated.households("sp") == {"house-m", "house-f"}
    assert seats.asked[0] == {r.value for r in SpaceRole}
    assert await seated.may_receive("sp", "house-f")
    assert not await seated.may_receive("sp", "me")


class _Space:
    def __init__(self, role: str, *, local: bool = True) -> None:
        self._role = role
        self._local = local

    async def list_local_member_user_ids(self, space_id):
        return ["u1"] if self._local else []

    async def list_members(self, space_id):
        return [SimpleNamespace(user_id="u1", role=self._role)]


async def test_our_own_seat_is_a_writer_seat_unless_any_seat_is_asked_for():
    assert await holds_member_seat(_Space("member"), "sp")
    assert not await holds_member_seat(_Space("subscriber"), "sp")
    assert await holds_member_seat(_Space("subscriber"), "sp", roles=SEAT_ROLES)
    assert not await holds_member_seat(
        _Space("subscriber", local=False), "sp", roles=SEAT_ROLES
    )
