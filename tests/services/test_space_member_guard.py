"""Tests for SpaceMemberGuardMixin — shared membership/role guard primitives."""

import pytest

from socialhome.crypto import derive_user_id, generate_identity_keypair
from socialhome.domain.space import SpacePermissionError, SpaceRole
from socialhome.services.space_member_guard import (
    NOT_A_SPACE_MEMBER,
    SpaceMemberGuardMixin,
    require_seat,
    user_id_of_another_household,
    user_id_proven,
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

    async def is_banned(self, space_id, user_id):
        return (space_id, user_id) in getattr(self, "banned", set())


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


async def test_require_seat_refuses_a_banned_user_with_a_leftover_seat():
    """Defence in depth: a ban must end the read access even if a seat row
    survived it (a half-applied ban, a federation race) — with the same
    403, so it reveals nothing more."""
    spaces = _FakeSpaces({("s1", "u1"): _Member(SpaceRole.MEMBER)})
    spaces.banned = {("s1", "u1")}
    with pytest.raises(SpacePermissionError, match=NOT_A_SPACE_MEMBER):
        await require_seat(spaces, "s1", "u1")


class _Roster:
    def __init__(self, rows: set[tuple[str, str, str, bool]]) -> None:
        self.rows = rows  # (space, instance, user, tombstoned)

    async def list_live_instances_of_user(self, space_id, user_id):
        return {
            i
            for s, i, u, dead in self.rows
            if (s, u) == (space_id, user_id) and not dead
        }


class _LocalUsers:
    def __init__(self, ids: set[str]) -> None:
        self.ids = ids

    async def get_by_user_id(self, user_id):
        return object() if user_id in self.ids else None


@pytest.mark.parametrize(
    ("rows", "local", "taken"),
    [
        (set(), set(), False),  # nobody holds it
        ({("s1", "home-a", "u1", False)}, set(), False),  # its own household
        ({("s1", "home-b", "u1", False)}, set(), True),  # live under another
        # A tombstoned seat under another household no longer blocks: an
        # admin removing a legacy squat clears it — no permanent lockout.
        ({("s1", "home-b", "u1", True)}, set(), False),
        ({("s2", "home-b", "u1", False)}, set(), False),  # another space only
        (set(), {"u1"}, True),  # one of our people
    ],
)
async def test_a_receiver_trusts_a_seat_only_for_its_own_household(rows, local, taken):
    """The receivers' rule (``SpaceAuthorship.acts_for``): a seat proves
    acting for ``user_id`` unless that id is one of OUR people or holds a
    LIVE seat under another household (only a pre-proof legacy seat can)."""
    got = await user_id_of_another_household(
        seats=_Roster(rows),
        users=_LocalUsers(local),
        space_id="s1",
        instance_id="home-a",
        user_id="u1",
    )
    assert got is taken


_KEY = generate_identity_keypair().public_key
_OTHER_KEY = generate_identity_keypair().public_key


@pytest.mark.parametrize(
    ("pk", "anchor", "user_id", "proven"),
    [
        (_KEY, "alice", derive_user_id(_KEY, "alice"), True),
        # Somebody else's id: it derives from another household's key.
        (_OTHER_KEY, "alice", derive_user_id(_KEY, "alice"), False),
        (_KEY, "bob", derive_user_id(_KEY, "alice"), False),  # wrong anchor
        (_KEY, None, derive_user_id(_KEY, "alice"), False),  # missing anchor
        (_KEY, "", derive_user_id(_KEY, "alice"), False),
        (_KEY, 42, derive_user_id(_KEY, "alice"), False),  # not a string
        (None, "alice", derive_user_id(_KEY, "alice"), False),  # no pinned key
        (b"short", "alice", derive_user_id(_KEY, "alice"), False),  # bad key
        (_KEY, "alice", "", False),
    ],
    ids=[
        "own",
        "somebody-elses",
        "wrong-anchor",
        "no-anchor",
        "empty-anchor",
        "int-anchor",
        "no-key",
        "bad-key",
        "no-user-id",
    ],
)
def test_a_user_id_is_proven_only_by_its_households_key(pk, anchor, user_id, proven):
    """P5 person-ID anchor: a household proves a user id is its own by the
    anchor it was minted from — ``derive_user_id(pk, anchor) == user_id`` —
    fail-closed on any malformed input."""
    assert user_id_proven(pk, anchor, user_id) is proven
