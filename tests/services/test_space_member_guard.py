"""Tests for SpaceMemberGuardMixin — shared membership/role guard primitives."""

import pytest

from socialhome.domain.space import SpacePermissionError, SpaceRole
from socialhome.services.space_member_guard import (
    NOT_A_SPACE_MEMBER,
    SpaceMemberGuardMixin,
    require_seat,
)


class _Member:
    def __init__(self, role):
        self.role = role


class _Actor:
    def __init__(self, user_id):
        self.user_id = user_id


class _FakeSpaces:
    def __init__(self, members):
        # members: dict[(space_id, user_id)] -> _Member
        self._members = members

    async def get_member(self, space_id, user_id):
        return self._members.get((space_id, user_id))


class _FakeUsers:
    def __init__(self, users):
        self._users = users  # dict[username] -> _Actor

    async def get(self, username):
        return self._users.get(username)


class _Svc(SpaceMemberGuardMixin):
    __slots__ = ("_spaces", "_users")

    def __init__(self, spaces, users):
        self._spaces = spaces
        self._users = users


def _svc(members=None, users=None):
    return _Svc(_FakeSpaces(members or {}), _FakeUsers(users or {}))


async def test_member_or_raise_returns_member():
    svc = _svc(members={("s1", "u1"): _Member(SpaceRole.MEMBER)})
    m = await svc._member_or_raise("s1", "u1")
    assert m.role == SpaceRole.MEMBER


async def test_member_or_raise_raises_for_non_member():
    svc = _svc()
    with pytest.raises(SpacePermissionError, match="not a member"):
        await svc._member_or_raise("s1", "nobody")


async def test_role_or_raise_allows_listed_role():
    svc = _svc(members={("s1", "u1"): _Member(SpaceRole.ADMIN)})
    m = await svc._role_or_raise(
        "s1",
        "u1",
        (SpaceRole.OWNER, SpaceRole.ADMIN),
        message="nope",
    )
    assert m.role == SpaceRole.ADMIN


async def test_role_or_raise_rejects_unlisted_role():
    svc = _svc(members={("s1", "u1"): _Member(SpaceRole.MEMBER)})
    with pytest.raises(SpacePermissionError, match="admin or owner required"):
        await svc._role_or_raise(
            "s1",
            "u1",
            (SpaceRole.OWNER, SpaceRole.ADMIN),
            message="admin or owner required",
        )


async def test_role_or_raise_rejects_non_member():
    svc = _svc()
    with pytest.raises(SpacePermissionError):
        await svc._role_or_raise("s1", "ghost", (SpaceRole.OWNER,), message="x")


async def test_actor_or_raise_resolves_user():
    svc = _svc(users={"alice": _Actor("u-alice")})
    actor = await svc._actor_or_raise("alice")
    assert actor.user_id == "u-alice"


async def test_actor_or_raise_keyerror_with_default_label():
    svc = _svc()
    with pytest.raises(KeyError, match="actor 'ghost' not found"):
        await svc._actor_or_raise("ghost")


async def test_actor_or_raise_keyerror_honours_label():
    svc = _svc()
    with pytest.raises(KeyError, match="user 'ghost' not found"):
        await svc._actor_or_raise("ghost", label="user")


def test_mixin_owns_no_slots():
    assert SpaceMemberGuardMixin.__slots__ == ()


@pytest.mark.parametrize("role", list(SpaceRole))
async def test_require_seat_admits_every_role_followers_included(role):
    spaces = _FakeSpaces({("s1", "u1"): _Member(role)})
    assert (await require_seat(spaces, "s1", "u1")).role == role


async def test_require_seat_refuses_no_seat_with_the_route_gates_text():
    with pytest.raises(SpacePermissionError) as exc:
        await require_seat(_FakeSpaces({}), "s1", "nobody")
    assert str(exc.value) == NOT_A_SPACE_MEMBER == "Not a space member."
