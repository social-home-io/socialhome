"""The owner's connection-server option for PRIVATE spaces
(``SpaceFeatures.private_gfs``) and the invite link type (``via``).

Real :class:`SpaceService` over a real SQLite database; the federation
repo, the remote-member repo and the member-publish (channel) service are
small in-memory stand-ins, since only their answers matter here.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import InstanceSource
from socialhome.domain.space import (
    INVITE_VIA_GFS,
    INVITE_VIA_INTERNAL,
    PrivateGfsLinkMembersError,
    PrivateGfsOffError,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import SpaceRemoteMember
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.space_service import SpaceService
from socialhome.services.user_service import UserService


class _FedRepo:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    async def get_instance(self, instance_id):
        return self.rows.get(instance_id)


class _Remote:
    def __init__(self) -> None:
        self.seats: list[SpaceRemoteMember] = []

    async def list_for_space(self, space_id):
        return [s for s in self.seats if s.space_id == space_id]


class _MemberGfs:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def enable_channel(self, space_id):
        self.calls.append(("enable", space_id))

    async def retire_channel(self, space_id):
        self.calls.append(("retire", space_id))

    async def channel_space(self, space_id):
        return False

    async def announce_epoch(self, space_id, **_kw):
        return 0


class _Gfs:
    def __init__(self) -> None:
        self.revoked: list[tuple[str, str, str]] = []

    async def revoke_invite(self, space_id, gfs_id, gfs_token):
        self.revoked.append((space_id, gfs_id, gfs_token))


@pytest.fixture
async def stack(tmp_dir, monkeypatch):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    users = SqliteUserRepo(db)
    spaces = SqliteSpaceRepo(db, key_manager=KeyManager(b"\x09" * 32))
    svc = SpaceService(spaces, SqliteSpacePostRepo(db), users, bus, own_instance_id=iid)
    fed_repo, remote, member_gfs, gfs = _FedRepo(), _Remote(), _MemberGfs(), _Gfs()
    svc.attach_federation(None, fed_repo, remote)
    svc.attach_member_gfs(member_gfs)
    svc._gfs = gfs  # revoke only; minting is not exercised here
    rotations: list[str] = []

    async def _rotate(_self, space_id):
        rotations.append(space_id)

    monkeypatch.setattr(SpaceService, "_rotate_and_distribute_space_key", _rotate)
    user_svc = UserService(users, bus, own_instance_public_key=kp.public_key)
    await user_svc.provision(username="anna", display_name="Anna", is_admin=True)
    bob = await user_svc.provision(username="bob", display_name="Bob")
    carol = await user_svc.provision(username="carol", display_name="Carol")
    space = await svc.create_space(owner_username="anna", name="Family")
    await svc.add_member(space.id, actor_username="anna", user_id=bob.user_id)
    await svc.set_role(
        space.id, actor_username="anna", user_id=bob.user_id, role="admin"
    )
    yield SimpleNamespace(
        db=db,
        svc=svc,
        spaces=spaces,
        space=space,
        fed_repo=fed_repo,
        remote=remote,
        member_gfs=member_gfs,
        gfs=gfs,
        rotations=rotations,
        carol=carol,
    )
    await db.shutdown()


async def _set(stack, actor: str, *, on: bool):
    space = await stack.spaces.get(stack.space.id)
    return await stack.svc.update_config(
        stack.space.id,
        actor_username=actor,
        features=replace(space.features, private_gfs=on),
    )


def _seat(stack, instance_id: str, user_id: str, *, source: InstanceSource):
    stack.remote.seats.append(
        SpaceRemoteMember(
            space_id=stack.space.id,
            instance_id=instance_id,
            user_id=user_id,
            display_name=f"User {user_id}",
        )
    )
    stack.fed_repo.rows[instance_id] = SimpleNamespace(
        source=source, effective_display_name=f"Home {instance_id}"
    )


def _decode(code: str) -> dict:
    blob = code.split("#", 1)[1]
    return json.loads(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))


# ── The option ────────────────────────────────────────────────────────────


async def test_a_new_private_space_does_not_use_the_gfs(stack):
    space = await stack.spaces.get(stack.space.id)
    assert space.space_type is SpaceType.PRIVATE
    assert space.features.private_gfs is False


@pytest.mark.security
async def test_only_the_owner_may_switch_the_option(stack):
    with pytest.raises(SpacePermissionError):
        await _set(stack, "bob", on=True)
    assert (await stack.spaces.get(stack.space.id)).features.private_gfs is False
    assert stack.member_gfs.calls == []


async def test_turning_it_on_starts_the_channel(stack):
    updated = await _set(stack, "anna", on=True)
    assert updated.features.private_gfs is True
    assert stack.member_gfs.calls == [("enable", stack.space.id)]
    assert stack.rotations == []


@pytest.mark.security
async def test_turning_it_off_is_refused_while_link_joined_members_remain(stack):
    await _set(stack, "anna", on=True)
    _seat(stack, "link-hh", "u-e", source=InstanceSource.SPACE_SESSION)
    _seat(stack, "paired-hh", "u-b", source=InstanceSource.MANUAL)
    stack.member_gfs.calls.clear()
    with pytest.raises(PrivateGfsLinkMembersError) as exc:
        await _set(stack, "anna", on=False)
    # Only the link-joined household is named — the paired one keeps its
    # federation route to the host.
    assert exc.value.households == [
        {
            "instance_id": "link-hh",
            "display_name": "Home link-hh",
            "members": [{"user_id": "u-e", "display_name": "User u-e"}],
        }
    ]
    space = await stack.spaces.get(stack.space.id)
    assert space.features.private_gfs is True
    assert stack.member_gfs.calls == [] and stack.rotations == []


async def test_turning_it_off_revokes_gfs_links_retires_and_rotates(stack):
    await _set(stack, "anna", on=True)
    _seat(stack, "paired-hh", "u-b", source=InstanceSource.MANUAL)
    gfs_link = await stack.svc.create_invite_link(stack.space.id, actor_username="anna")
    internal = await stack.svc.create_invite_link(
        stack.space.id, actor_username="anna", via=INVITE_VIA_INTERNAL
    )
    # A gfs link that was parked somewhere comes down there too.
    await stack.db.enqueue(
        "UPDATE space_invite_tokens SET gfs_id='g1', gfs_token='gt' WHERE token=?",
        (gfs_link["token"],),
    )
    stack.member_gfs.calls.clear()
    updated = await _set(stack, "anna", on=False)
    assert updated.features.private_gfs is False
    live = {
        r["token"] for r in await stack.spaces.list_live_invite_tokens(stack.space.id)
    }
    assert live == {internal["token"]}
    assert stack.gfs.revoked == [(stack.space.id, "g1", "gt")]
    assert stack.member_gfs.calls == [("retire", stack.space.id)]
    assert stack.rotations == [stack.space.id]


async def test_the_option_is_ignored_on_a_public_space(stack):
    public = await stack.svc.create_space(
        owner_username="anna", name="Club", space_type=SpaceType.PUBLIC
    )
    space = await stack.spaces.get(public.id)
    await stack.svc.update_config(
        public.id,
        actor_username="anna",
        features=replace(space.features, private_gfs=True),
    )
    assert stack.member_gfs.calls == []


@pytest.mark.security
async def test_a_forwarded_admin_edit_never_moves_the_option(stack):
    """A remote admin's forwarded config edit runs AS the owner on the host,
    so the owner-only gate is no defence there: the value is pinned."""
    space = await stack.spaces.get(stack.space.id)
    await stack.svc.apply_approved_admin_action(
        stack.space.id,
        action="update_config",
        params={
            "name": "Renamed",
            "features": {**space.features.to_wire_dict(), "private_gfs": True},
        },
    )
    after = await stack.spaces.get(stack.space.id)
    assert after.name == "Renamed"
    assert after.features.private_gfs is False
    assert stack.member_gfs.calls == []


# ── Invite link type ──────────────────────────────────────────────────────


async def test_an_off_private_space_mints_internal_links_by_default(stack):
    link = await stack.svc.create_invite_link(stack.space.id, actor_username="anna")
    assert link["via"] == INVITE_VIA_INTERNAL
    decoded = _decode(link["code"])
    # No key-wrap key, no relay: nothing to seal a relayed redeem to.
    assert "issuer_keywrap_pk" not in decoded
    assert "issuer_keywrap_sig" not in decoded
    assert "via_gfs" not in decoded
    # The identity key stays (a mesh redeem verifies the routed reply).
    assert decoded["issuer_identity_pk"] is not None
    (row,) = await stack.spaces.list_live_invite_tokens(stack.space.id)
    assert row["via"] == INVITE_VIA_INTERNAL


async def test_a_gfs_link_is_refused_while_the_option_is_off(stack):
    with pytest.raises(PrivateGfsOffError):
        await stack.svc.create_invite_link(
            stack.space.id, actor_username="anna", via=INVITE_VIA_GFS
        )
    assert await stack.spaces.list_live_invite_tokens(stack.space.id) == []


async def test_an_on_private_space_defaults_to_gfs_links(stack):
    await _set(stack, "anna", on=True)
    link = await stack.svc.create_invite_link(stack.space.id, actor_username="anna")
    assert link["via"] == INVITE_VIA_GFS
    decoded = _decode(link["code"])
    assert "issuer_keywrap_pk" in decoded and "issuer_keywrap_sig" in decoded
    internal = await stack.svc.create_invite_link(
        stack.space.id, actor_username="anna", via=INVITE_VIA_INTERNAL
    )
    assert "issuer_keywrap_pk" not in _decode(internal["code"])
    listed = {
        r["token"]: r["via"]
        for r in await stack.svc.list_invite_links(
            stack.space.id, actor_username="anna"
        )
    }
    assert listed == {link["token"]: "gfs", internal["token"]: "internal"}
    # The public /code endpoint hands out the same type of code.
    code = await stack.svc.invite_code_for_token(internal["token"])
    assert "issuer_keywrap_pk" not in _decode(code)


async def test_a_public_space_is_unchanged(stack):
    public = await stack.svc.create_space(
        owner_username="anna", name="Open", space_type=SpaceType.PUBLIC
    )
    link = await stack.svc.create_invite_link(public.id, actor_username="anna")
    assert link["via"] == INVITE_VIA_GFS
    assert "issuer_keywrap_pk" in _decode(link["code"])


async def test_an_internal_link_is_never_published_and_bad_types_are_refused(stack):
    with pytest.raises(ValueError):
        await stack.svc.create_invite_link(
            stack.space.id,
            actor_username="anna",
            via=INVITE_VIA_INTERNAL,
            publish_to_gfs="g1",
        )
    with pytest.raises(ValueError):
        await stack.svc.create_invite_link(
            stack.space.id, actor_username="anna", via="relay"
        )


async def test_an_internal_link_still_seats_a_local_redeemer(stack):
    link = await stack.svc.create_invite_link(
        stack.space.id, actor_username="anna", role=SpaceRole.MEMBER.value
    )
    member = await stack.svc.accept_invite_token(
        link["token"], user_id=stack.carol.user_id
    )
    assert member.role == SpaceRole.MEMBER


async def test_a_link_member_seated_during_the_switch_is_logged(
    stack, caplog, monkeypatch
):
    """A relayed redeem that lands between the refusal check and the write
    is reported at WARNING, not silently stranded."""
    await _set(stack, "anna", on=True)
    real = SpaceService._link_joined_households
    calls = {"n": 0}

    async def racing(self, space_id):
        calls["n"] += 1
        if calls["n"] == 2:
            _seat(stack, "late-hh", "u-late", source=InstanceSource.SPACE_SESSION)
        return await real(self, space_id)

    monkeypatch.setattr(SpaceService, "_link_joined_households", racing)
    with caplog.at_level("WARNING"):
        await _set(stack, "anna", on=False)
    assert any("late-hh" in r.getMessage() for r in caplog.records)
    assert stack.rotations == [stack.space.id]
