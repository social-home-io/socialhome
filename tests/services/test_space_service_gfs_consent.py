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
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.domain.events import GfsConnectionAsked
from socialhome.domain.public_space import PublicSpaceListing
from socialhome.domain.space import (
    GfsConnectionRequiredError,
    NothingToAskError,
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


# ── Review round 1 (Fable) ────────────────────────────────────────────────


class _UrlGfs(_Gfs):
    """…with ``ring_at`` — the base URLs of the ring connections."""

    def __init__(self, ring=(), ring_at=()) -> None:
        super().__init__(ring=ring)
        self.ring_at = set(ring_at)

    async def ring_connected_at(self, gfs_url: str) -> bool:
        return gfs_url.rstrip("/") in self.ring_at


def _hint(**over) -> InviteBootstrapHint:
    base = {
        "invite_token": "tok",
        "space_id": "",
        "instance_id": HOST,
        "identity_pk": "aa",
        "keywrap_pk": "bb",
        "keywrap_sig": "cc",
        "gfs_url": "https://gfs.example",
    }
    base.update(over)
    return InviteBootstrapHint(**base)


async def test_a_gfs_invite_link_needs_a_ring_connection_to_that_gfs(stack):  # noqa: F811
    """A GFS link exists only for a GFS space: whatever space id the body
    names (none here), the link's own GFS must carry the channel."""
    fan = await stack.provision("fan")
    redeemed: list = []

    async def request_redeem(token, **kw):
        redeemed.append(token)
        return {"space_id": "sp-x", "role": "member"}

    stack.svc._redeem_coordinator = SimpleNamespace(request_redeem=request_redeem)
    stack.svc.attach_gfs_connection_service(
        _UrlGfs(ring={"gfs-a"}, ring_at={"https://other.example"})
    )
    with pytest.raises(GfsConnectionRequiredError):
        await stack.svc.redeem_invite_token(
            "tok", user_id=fan.user_id, issuer_instance_id=HOST, bootstrap=_hint()
        )
    assert redeemed == []
    stack.svc.attach_gfs_connection_service(
        _UrlGfs(ring={"gfs-a"}, ring_at={"https://gfs.example"})
    )
    await stack.svc.redeem_invite_token(
        "tok", user_id=fan.user_id, issuer_instance_id=HOST, bootstrap=_hint()
    )
    assert redeemed == ["tok"]


@pytest.mark.parametrize(
    ("meta", "refused"),
    [
        ({"space_type": "global"}, True),
        ({"space_type": "private", "features": {"private_gfs": True}}, True),
        ({"space_type": "private"}, False),
        ({"space_type": "public"}, False),
    ],
)
async def test_a_by_name_invite_reads_the_tier_from_the_hosts_snapshot(
    stack,  # noqa: F811
    meta,
    refused,
):
    stack.svc.attach_gfs_connection_service(_Gfs())
    meta = {"name": "Invited", **meta}
    if refused:
        with pytest.raises(GfsConnectionRequiredError):
            await stack.svc._require_gfs_for_snapshot("sp-i", HOST, meta)
    else:
        await stack.svc._require_gfs_for_snapshot("sp-i", HOST, meta)
    stack.svc.attach_gfs_connection_service(_Gfs(ring={"gfs-a"}))
    await stack.svc._require_gfs_for_snapshot("sp-i", HOST, meta)


async def test_a_stale_public_stub_does_not_hide_a_gfs_listing(stack):  # noqa: F811
    await _stub(stack, space_type=SpaceType.PUBLIC)
    await _listed(stack, "sp-s", sources=frozenset({"gfs-b"}))
    stack.svc.attach_gfs_connection_service(_Gfs(ring={"gfs-a"}, targets=None))
    ready = await stack.svc.gfs_join_readiness("sp-s")
    assert (ready.gfs_space, ready.connected) == (True, False)


async def test_an_ask_through_the_gfs_needs_a_ring_connection_without_a_listing(
    stack,  # noqa: F811
):
    """The join-contact pin outlives the directory row: the sealed ask
    through the GFS is still a GFS space's."""
    stack.fed_repo.get_instance = AsyncMock(return_value=None)
    gfs_join = MagicMock()
    gfs_join.host_of_listing = AsyncMock(return_value=HOST)
    gfs_join.request_join = AsyncMock(return_value="rq-gfs")
    stack.svc._gfs_join = gfs_join
    stack.svc.attach_gfs_connection_service(_Gfs())
    with pytest.raises(GfsConnectionRequiredError):
        await _ask(stack, "sp-pinned")
    gfs_join.request_join.assert_not_awaited()


async def test_asking_the_admins_tells_each_admin_once(stack):  # noqa: F811
    """Owner 2026-11-28 — "Ask an admin of your household" is an action: a
    member who can't connect the household asks its admins, who get one
    notification each (``GfsConnectionAsked`` → the bell) naming the space
    and the GFS, opening the Connect flow. The space's name comes from what
    this household knows, never from the client."""
    await _listed(stack)
    stack.svc.attach_gfs_connection_service(_Gfs())
    fan = await stack.provision("fan")
    olga = await stack.provision("olga")
    await stack.users.set_admin("olga", True)
    asked: list = []

    async def _record(event):
        asked.append(event)

    stack.bus.subscribe(GfsConnectionAsked, _record)
    await stack.svc.ask_admins_to_connect(
        "sp-g",
        user_id=fan.user_id,
        action="follow",
        gfs="gfs.example",
        gfs_url="https://gfs.example",
    )
    (event,) = asked
    assert event.space_id == "sp-g" and event.space_name == "Global"
    assert event.action == "follow" and event.requested_by == fan.user_id
    assert event.gfs == "gfs.example" and event.gfs_url == "https://gfs.example"
    assert event.recipients == (olga.user_id,)  # the household's admins
    stack.fed.send_event.assert_not_awaited()  # nothing leaves the household


async def test_nothing_to_ask_when_no_connection_is_needed(stack):  # noqa: F811
    """A space that needs no GFS (or one already connected) asks nobody."""
    await _listed(stack, sources=frozenset({"gfs-a"}))
    stack.svc.attach_gfs_connection_service(_Gfs(ring={"gfs-a"}))
    fan = await stack.provision("fan")
    with pytest.raises(NothingToAskError):
        await stack.svc.ask_admins_to_connect(
            "sp-g", user_id=fan.user_id, action="join", gfs="", gfs_url=""
        )
    with pytest.raises(NothingToAskError):
        await stack.svc.ask_admins_to_connect(
            "sp-unknown", user_id=fan.user_id, action="join", gfs="", gfs_url=""
        )
