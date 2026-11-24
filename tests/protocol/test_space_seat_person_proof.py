"""Release-blocker protocol tests: a host seats a person another household
names only when that household proves the id (P5 person-ID anchor).

The redeemer names its own user id. Before P5 the host took its word for
it, behind a first-come rule (refuse an id held — tombstones included —
under another household): a household that redeemed a link under somebody
else's id first locked that person out of the space for good, and the
refusal, being roster-dependent, let a multi-use link probe "is / was user X
here". Now every seating request carries the person's identity anchor
inside its sealed body and the host checks ``derive_user_id(the sender's
identity key, anchor) == user_id`` first.

Real households (SQLite, real coordinators) talking through a fake GFS over
the §D2b invite-link bootstrap: ``a`` (a squatter), ``b`` (the host), ``c``
(the person ``a`` tries to pass for).
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from socialhome.domain.federation_capabilities import OURS
from socialhome.domain.space import JoinMode, SpacePermissionError, SpaceRole, SpaceType
from socialhome.domain.user import User
from socialhome.federation.invite_bootstrap import InviteBootstrapHint
from socialhome.federation.invite_token_redeem import BOOTSTRAP_DENY_CLIENT_MESSAGE
from socialhome.repositories.user_repo import SqliteUserRepo
from tests.federation.test_invite_bootstrap_gfs_e2e import _FakeGfs, _household

try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass


pytestmark = pytest.mark.security

TOKEN = "p5-person-proof-token"


@pytest.fixture
async def gfs():
    fake = _FakeGfs()
    server = TestServer(fake.app())
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    yield fake
    await fake.drain()
    await server.close()


@pytest.fixture
async def http_session():
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
async def trio(tmp_path, gfs, http_session):
    a = await _household(tmp_path, "a", gfs, http_session)
    b = await _household(tmp_path, "b", gfs, http_session)
    c = await _household(tmp_path, "c", gfs, http_session)
    yield a, b, c
    await gfs.drain()
    for h in (a, b, c):
        await h.db.shutdown()


async def _mint(b, gfs_url: str, *, uses: int):
    """b hosts a private space (GFS on) with a ``uses``-use invite link."""
    space = await b.space_service.create_space(
        owner_username=b.username,
        name="Proof",
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )
    await b.db.enqueue("UPDATE spaces SET private_gfs=1 WHERE id=?", (space.id,))
    await b.space_repo.create_invite_token(space.id, b.user_id, uses=uses)
    await b.db.enqueue(
        "UPDATE space_invite_tokens SET token=? WHERE space_id=?", (TOKEN, space.id)
    )
    return space, InviteBootstrapHint(
        invite_token=TOKEN,
        space_id=space.id,
        instance_id=b.instance_id,
        identity_pk=b.identity_pk.hex(),
        keywrap_pk=b.keywrap_pub.hex(),
        keywrap_sig=b.keywrap_sig,
        proto_version=OURS,
        display_hint="Proof",
        gfs_url=gfs_url,
    )


async def _impersonate(a, victim_user_id: str) -> None:
    """a keeps a local user row under the victim's id — all a squatting
    household can do: the anchor it sends derives from ITS key."""
    users = SqliteUserRepo(a.db)
    users.attach_key_manager(a.key_manager)
    await users.save(
        User(user_id=victim_user_id, username="squat", display_name="Squat")
    )


async def _redeem(h, user_id: str, b, hint):
    return await h.space_service.redeem_invite_token(
        TOKEN, user_id=user_id, issuer_instance_id=b.instance_id, bootstrap=hint
    )


async def _uses_left(b) -> int:
    row = await b.db.fetchone(
        "SELECT uses_remaining FROM space_invite_tokens WHERE token=?", (TOKEN,)
    )
    assert row is not None
    return int(row["uses_remaining"])


async def _seat_rows(b, space_id: str, user_id: str) -> list[tuple[str, int]]:
    rows = await b.db.fetchall(
        "SELECT instance_id, tombstoned FROM space_remote_members"
        " WHERE space_id=? AND user_id=? ORDER BY instance_id",
        (space_id, user_id),
    )
    return [(str(r["instance_id"]), int(r["tombstoned"])) for r in rows]


async def test_a_squat_is_refused_and_the_real_person_still_joins(trio, gfs):
    a, b, c = trio
    space, hint = await _mint(b, gfs.url, uses=2)
    await _impersonate(a, c.user_id)

    with pytest.raises(SpacePermissionError) as exc:
        await _redeem(a, c.user_id, b, hint)
    assert str(exc.value) == BOOTSTRAP_DENY_CLIENT_MESSAGE
    assert await _seat_rows(b, space.id, c.user_id) == []
    assert await _uses_left(b) == 2

    result = await _redeem(c, c.user_id, b, hint)
    assert result["space_id"] == space.id
    assert await _seat_rows(b, space.id, c.user_id) == [(c.instance_id, 0)]


@pytest.mark.parametrize("tombstoned", [False, True])
async def test_a_legacy_squat_seat_does_not_lock_the_real_person_out(
    trio, gfs, tombstoned
):
    """A seat a pre-proof host gave a squatter under the person's id —
    live or already removed — no longer blocks the person's own household
    (the P4 first-come rule did, forever)."""
    a, b, c = trio
    space, hint = await _mint(b, gfs.url, uses=1)
    await b.remote_members.add(
        space_id=space.id,
        instance_id=a.instance_id,
        user_id=c.user_id,
        user_pk=None,
        display_name="Squat",
        role=SpaceRole.MEMBER.value,
    )
    if tombstoned:
        await b.remote_members.remove(space.id, a.instance_id, c.user_id)

    await _redeem(c, c.user_id, b, hint)
    assert (c.instance_id, 0) in await _seat_rows(b, space.id, c.user_id)


@pytest.mark.parametrize("state", ["unknown", "seated", "removed", "banned"])
async def test_an_unproven_redeem_reveals_nothing_about_the_roster(trio, gfs, state):
    """No probing through a multi-use link: the squatter's refusal is the
    same message with the same untouched link, whatever the host holds for
    the id it names."""
    a, b, c = trio
    space, hint = await _mint(b, gfs.url, uses=3)
    if state in ("seated", "removed"):
        await b.remote_members.add(
            space_id=space.id,
            instance_id=c.instance_id,
            user_id=c.user_id,
            user_pk=None,
            display_name="C",
            role=SpaceRole.MEMBER.value,
        )
        if state == "removed":
            await b.remote_members.remove(space.id, c.instance_id, c.user_id)
    elif state == "banned":
        await b.space_repo.ban_member(space.id, c.user_id, banned_by=b.user_id)
    before = await _seat_rows(b, space.id, c.user_id)
    await _impersonate(a, c.user_id)

    with pytest.raises(SpacePermissionError) as exc:
        await _redeem(a, c.user_id, b, hint)
    assert str(exc.value) == BOOTSTRAP_DENY_CLIENT_MESSAGE
    assert await _uses_left(b) == 3
    assert await _seat_rows(b, space.id, c.user_id) == before
