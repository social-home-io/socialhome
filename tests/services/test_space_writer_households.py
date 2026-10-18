"""SpaceWriterHouseholds — the member households of a space (writer seats),
never a follower-only household nor ourselves."""

from __future__ import annotations

from socialhome.services.space_writer_households import SpaceWriterHouseholds


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
    assert await members.writer_households("sp") == {"house-a", "house-m"}
    assert "subscriber" not in seats.asked[0]
    assert await members.may_receive("sp", "house-m")
    assert not await members.may_receive("sp", "house-f")
    assert not await members.may_receive("sp", "me")
