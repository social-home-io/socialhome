"""Joining a GFS space needs a GFS connection first (v_97, owner 2026-11-27:
"firm consent").

A GFS space — a GLOBAL space, or a PRIVATE one with ``private_gfs`` on —
carries its content only on its channel at the GFSs its owner listed. So a
household asks to join (or follow) one only while it holds an active
connection that proves ``channel_ring`` to one of THOSE GFSs; otherwise
``409 GFS_CONNECTION_REQUIRED`` and nothing is sent. What the space is comes
from what this household knows — its own row, the GFS directory cache —
never from the client.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from socialhome.domain.public_space import PublicSpaceListing
from socialhome.domain.space import (
    GfsConnectionRequiredError,
    SpaceFeatures,
    SpaceType,
)
from socialhome.federation.invite_bootstrap import InviteBootstrapHint
from socialhome.repositories.peer_space_directory_repo import (
    PeerSpaceDirectoryEntry,
    SqlitePeerSpaceDirectoryRepo,
)
from socialhome.repositories.public_space_repo import SqlitePublicSpaceRepo
from socialhome.services.space_service import stub_space_from_metadata

from .test_space_service_follow import (  # noqa: F401 — the fixture
    HOST,
    _hosted,
    _join_request_sends,
    stack,
)


class _Gfs:
    """The household's GFS connections: ``ring`` — the connection ids that
    prove ``channel_ring``; ``targets`` — the owner's list as send targets
    (``registered`` = one of our own connections)."""

    def __init__(self, ring=(), targets=None) -> None:
        self.ring = set(ring)
        self.targets = targets
        self.asked: list = []

    async def ring_connected(self, only=None) -> bool:
        self.asked.append(only)
        return any(only is None or g in only for g in self.ring)

    async def server_targets(self, space_id):
        return self.targets


async def _listed(stack, space_id="sp-g", *, sources=None):  # noqa: F811
    repo = SqlitePublicSpaceRepo(stack.db)
    await repo.upsert(
        PublicSpaceListing(
            space_id=space_id,
            instance_id=HOST,
            name="Global",
            allow_subscribers=True,
            join_mode="open",
        )
    )
    stack.svc.attach_public_space_directory(repo)
    stack.svc.attach_gfs_listing_sources(lambda sid: sources)


async def _ask(stack, space_id="sp-g"):  # noqa: F811
    fan = await stack.provision("fan")
    return await stack.svc.request_join_remote(
        space_id, applicant_user_id=fan.user_id, host_instance_id=HOST
    )


async def test_no_gfs_connection_refuses_and_sends_nothing(stack):  # noqa: F811
    await _listed(stack)
    stack.svc.attach_gfs_connection_service(_Gfs())
    with pytest.raises(GfsConnectionRequiredError) as exc:
        await _ask(stack)
    assert exc.value.status == 409
    assert exc.value.code == "GFS_CONNECTION_REQUIRED"
    stack.fed.send_event.assert_not_awaited()


async def test_no_gfs_service_at_all_refuses(stack):  # noqa: F811
    await _listed(stack)
    with pytest.raises(GfsConnectionRequiredError):
        await _ask(stack)


async def test_a_ring_connection_to_a_gfs_listing_it_lets_the_join_go(stack):  # noqa: F811
    await _listed(stack, sources=frozenset({"gfs-a"}))
    gfs = _Gfs(ring={"gfs-a"})
    stack.svc.attach_gfs_connection_service(gfs)
    await _ask(stack)
    assert len(_join_request_sends(stack)) == 1
    assert gfs.asked == [frozenset({"gfs-a"})]


async def test_a_ring_connection_to_another_gfs_is_not_enough(stack):  # noqa: F811
    await _listed(stack, sources=frozenset({"gfs-b"}))
    stack.svc.attach_gfs_connection_service(_Gfs(ring={"gfs-a"}))
    with pytest.raises(GfsConnectionRequiredError):
        await _ask(stack)
    stack.fed.send_event.assert_not_awaited()


async def test_before_the_first_poll_any_ring_connection_counts(stack):  # noqa: F811
    await _listed(stack, sources=None)
    gfs = _Gfs(ring={"gfs-a"})
    stack.svc.attach_gfs_connection_service(gfs)
    await _ask(stack)
    assert gfs.asked == [None]


async def test_a_follow_of_a_gfs_listing_is_refused_the_same_way(stack):  # noqa: F811
    await _listed(stack)
    stack.svc.attach_gfs_connection_service(_Gfs())
    fan = await stack.provision("fan")
    with pytest.raises(GfsConnectionRequiredError):
        await stack.svc.follow_space(fan.user_id, "sp-g")
    stack.fed.send_event.assert_not_awaited()


async def test_a_paired_households_public_space_needs_no_gfs(stack):  # noqa: F811
    peers = SqlitePeerSpaceDirectoryRepo(stack.db)
    await peers.replace_snapshot(
        HOST, [PeerSpaceDirectoryEntry(instance_id=HOST, space_id="sp-p", name="P")]
    )
    stack.svc.attach_public_space_directory(
        SqlitePublicSpaceRepo(stack.db), peer_directory=peers
    )
    stack.svc.attach_gfs_connection_service(_Gfs())
    await _ask(stack, "sp-p")
    assert len(_join_request_sends(stack)) == 1


async def _stub(stack, *, space_type, private_gfs=False):  # noqa: F811
    space = stub_space_from_metadata(
        "sp-s",
        host_instance_id=HOST,
        meta={
            "name": "Stub",
            "owner_username": "olive",
            "space_type": space_type.value,
            "features": SpaceFeatures(
                allow_subscribers=True, private_gfs=private_gfs
            ).to_wire_dict(),
        },
    )
    await stack.spaces.save(space)
    return space


@pytest.mark.parametrize(
    ("space_type", "private_gfs"),
    [(SpaceType.GLOBAL, False), (SpaceType.PRIVATE, True)],
)
async def test_a_stub_uses_the_owners_signed_list(stack, space_type, private_gfs):  # noqa: F811
    await _stub(stack, space_type=space_type, private_gfs=private_gfs)
    targets = [
        SimpleNamespace(id="gfs-a", registered=True),
        SimpleNamespace(id="lst-x", registered=False),
    ]
    stack.svc.attach_gfs_connection_service(_Gfs(ring={"gfs-b"}, targets=targets))
    ready = await stack.svc.gfs_join_readiness("sp-s")
    assert (ready.gfs_space, ready.connected) == (True, False)
    stack.svc.attach_gfs_connection_service(_Gfs(ring={"gfs-a"}, targets=targets))
    ready = await stack.svc.gfs_join_readiness("sp-s")
    assert (ready.gfs_space, ready.connected) == (True, True)


async def test_a_stub_without_a_known_list_takes_any_ring_connection(stack):  # noqa: F811
    await _stub(stack, space_type=SpaceType.GLOBAL)
    gfs = _Gfs(ring={"gfs-b"}, targets=[])
    stack.svc.attach_gfs_connection_service(gfs)
    ready = await stack.svc.gfs_join_readiness("sp-s")
    assert (ready.gfs_space, ready.connected) == (True, True)
    assert gfs.asked == [None]


async def test_a_mesh_stub_needs_no_gfs(stack):  # noqa: F811
    await _stub(stack, space_type=SpaceType.PRIVATE)
    stack.svc.attach_gfs_connection_service(_Gfs())
    ready = await stack.svc.gfs_join_readiness("sp-s")
    assert (ready.gfs_space, ready.connected) == (False, True)


async def test_a_space_hosted_here_needs_no_gfs(stack):  # noqa: F811
    space = await _hosted(stack)
    stack.svc.attach_gfs_connection_service(_Gfs())
    ready = await stack.svc.gfs_join_readiness(space.id)
    assert (ready.gfs_space, ready.connected) == (False, True)


async def test_an_unknown_space_is_no_gfs_space(stack):  # noqa: F811
    stack.svc.attach_gfs_connection_service(_Gfs())
    ready = await stack.svc.gfs_join_readiness("sp-nobody-knows")
    assert (ready.gfs_space, ready.connected) == (False, True)


async def test_an_invite_link_to_a_known_gfs_space_needs_the_gfs_too(stack):  # noqa: F811
    await _listed(stack)
    stack.svc.attach_gfs_connection_service(_Gfs())
    coordinator = SimpleNamespace(request_redeem=None)
    stack.svc._redeem_coordinator = coordinator
    fan = await stack.provision("fan")
    hint = InviteBootstrapHint(
        invite_token="tok",
        space_id="sp-g",
        instance_id=HOST,
        identity_pk="aa",
        keywrap_pk="bb",
        keywrap_sig="cc",
    )
    with pytest.raises(GfsConnectionRequiredError):
        await stack.svc.redeem_invite_token(
            "tok", user_id=fan.user_id, issuer_instance_id=HOST, bootstrap=hint
        )
