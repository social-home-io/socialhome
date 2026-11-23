"""Tests for :class:`SpaceWriterCertService` (v_49 space writer certs)."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from socialhome.crypto import b64url_encode, derive_instance_id, ed25519_public_key
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatureAccess,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.domain.space_item import SUPPORTED_ITEM_TYPES
from socialhome.domain.writer_cert import (
    WRITER_SCOPE_COMMENT,
    WRITER_SCOPE_WRITE,
    WriterCert,
    WriterEntitlement,
)
from socialhome.domain.writer_key import WriterKeyGrant
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.space_remote_member_repo import SpaceRemoteMember
from socialhome.services.space_writer_cert_service import (
    WRITER_CERT_EPOCH_GRACE_S,
    SpaceWriterCertService,
    scope_weakened,
    writer_key_min_version,
)
from socialhome.writer_cert import (
    InvalidWriterCert,
    bind_writer_users,
    sign_writer_cert,
    verify_writer_cert,
    verify_writer_users,
)
from socialhome.writer_key import (
    derive_writer_seed,
    issue_writer_key_grant,
    verify_writer_key_cert,
    verify_writer_key_grant,
)

SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SEED)
OWN_PK = ed25519_public_key(os.urandom(32))
PEER_PK = ed25519_public_key(os.urandom(32))
FOLLOWER_PK = ed25519_public_key(os.urandom(32))


def _space(**features) -> Space:
    return Space(
        id="sp-1",
        name="S",
        owner_instance_id="own",
        owner_username="anna",
        identity_public_key=SPACE_PK.hex(),
        config_sequence=1,
        features=SpaceFeatures(**features),
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.OPEN,
    )


class _Spaces:
    def __init__(self, space: Space, seed: bytes | None, members=(), mirrored=()):
        self.space = space
        self.seed = seed
        #: Our own people, then the host's mirrored by the members sync
        #: (no ``users`` row here).
        self.members = list(members) + list(mirrored)
        self.mirrored = {m.user_id for m in mirrored}

    async def get(self, space_id):
        return self.space if space_id == self.space.id else None

    async def get_space_seed(self, space_id):
        return self.seed

    async def list_members(self, space_id):
        return self.members

    async def list_local_member_user_ids(self, space_id):
        return [m.user_id for m in self.members if m.user_id not in self.mirrored]


class _Remote:
    def __init__(self, rows):
        self.rows = rows

    async def list_for_instance(
        self, space_id, instance_id, *, include_tombstoned=True
    ):
        return [
            r
            for r in self.rows
            if r.instance_id == instance_id and (include_tombstoned or not r.tombstoned)
        ]


class _Keys:
    def __init__(self, epoch: int | None = 2, *, previous: int | None = None):
        self.epoch = epoch
        self.previous = previous
        self.arrived = datetime.now(UTC).isoformat()
        self.certs: dict[tuple[str, int], str] = {}
        self.writer_keys: dict[tuple[str, int], str] = {}
        self.epochs = {epoch} if epoch is not None else set()

    async def get_latest(self, space_id):
        if self.epoch is None:
            return None
        return SimpleNamespace(epoch=self.epoch, created_at=self.arrived)

    async def get_previous(self, space_id, epoch):
        if self.previous is None:
            return None
        return SimpleNamespace(epoch=self.previous, created_at=None)

    async def set_writer_cert(self, space_id, epoch, cert_json):
        if epoch not in self.epochs:
            return False
        self.certs[(space_id, epoch)] = cert_json
        return True

    async def get_writer_cert(self, space_id, epoch):
        return self.certs.get((space_id, epoch))

    async def set_writer_key(self, space_id, epoch, wrapped):
        if epoch not in self.epochs:
            return False
        self.writer_keys[(space_id, epoch)] = wrapped
        return True

    async def get_writer_key(self, space_id, epoch):
        return self.writer_keys.get((space_id, epoch))


class _Fed:
    def __init__(
        self,
        versions: dict[str, int],
        pks: dict[str, bytes],
        *,
        mesh: dict[str, tuple[int, bytes]] | None = None,
    ):
        self.versions = versions
        self.pks = pks
        #: Mesh-only members: no row (absent from ``versions`` / ``pks``),
        #: only a recorded mesh claim ``(version, identity_pk)``.
        self.mesh = mesh or {}

    async def peer_supports(self, instance_id, *, min_version):
        return self.versions.get(instance_id, 0) >= min_version

    async def space_member_supports(self, instance_id, *, min_version):
        if instance_id in self.versions:
            return self.versions[instance_id] >= min_version
        return self.mesh.get(instance_id, (0, b""))[0] >= min_version

    async def peer_identity_public_key(self, instance_id):
        return self.pks.get(instance_id)

    async def mesh_member_identity_pk(self, instance_id):
        if instance_id in self.pks:
            return None
        found = self.mesh.get(instance_id)
        return found[1] if found else None


def _remote(
    inst: str, role: str, user: str = "u", *, tombstoned: bool = False
) -> SpaceRemoteMember:
    return SpaceRemoteMember(
        space_id="sp-1",
        instance_id=inst,
        user_id=f"{user}@{inst}",
        role=role,
        tombstoned=tombstoned,
    )


def _svc(
    *,
    space: Space | None = None,
    seed: bytes | None = SEED,
    remote_rows=(),
    local=(),
    mirrored=(),
    keys: _Keys | None = None,
    versions=None,
    kek: KeyManager | None = None,
    mesh: dict[str, tuple[int, bytes]] | None = None,
) -> tuple[SpaceWriterCertService, _Keys]:
    keys = keys or _Keys()
    svc = SpaceWriterCertService(
        space_repo=_Spaces(space or _space(), seed, local, mirrored),
        remote_member_repo=_Remote(list(remote_rows)),
        space_key_repo=keys,
        own_instance_id="own",
        own_identity_pk=OWN_PK,
        key_manager=kek,
    )
    svc.attach_federation(
        _Fed(
            versions
            if versions is not None
            else {
                "peer": FederationCapability.MIN_FOR_MEMBER_GFS_PUBLISH,
                # A follower household needs v_96 for its reader cert.
                "fol": FederationCapability.MIN_FOR_SUBSCRIBER_SEATS,
                "old": FederationCapability.MIN_FOR_MEMBER_GFS_PUBLISH - 1,
            },
            {"peer": PEER_PK, "fol": FOLLOWER_PK, "old": PEER_PK},
            mesh=mesh,
        )
    )
    return svc, keys


def _check(cert: WriterCert, pk: bytes, scope: str, epoch: int = 2) -> None:
    verify_writer_cert(
        cert,
        space_pubkey=SPACE_PK,
        space_id="sp-1",
        epoch=epoch,
        author_pk=pk,
        required_scope=scope,
    )


# ── Scope derivation ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "role",
    [SpaceRole.ADMIN, SpaceRole.MODERATOR, SpaceRole.MEMBER, SpaceRole.OWNER],
)
async def test_writer_seats_get_write_scope(role):
    svc, _ = _svc(remote_rows=[_remote("peer", role.value)])
    cert = await svc.issue_for_instance("sp-1", "peer")
    assert cert is not None and cert.scope == WRITER_SCOPE_WRITE
    _check(cert, PEER_PK, WRITER_SCOPE_WRITE)


async def test_follower_gets_comment_scope_only_when_allowed():
    svc, _ = _svc(remote_rows=[_remote("fol", SpaceRole.SUBSCRIBER.value)])
    assert await svc.issue_for_instance("sp-1", "fol") is None
    svc, _ = _svc(
        space=_space(allow_subscriber_comment=True),
        remote_rows=[_remote("fol", SpaceRole.SUBSCRIBER.value)],
    )
    cert = await svc.issue_for_instance("sp-1", "fol")
    assert cert is not None and cert.scope == WRITER_SCOPE_COMMENT
    _check(cert, FOLLOWER_PK, WRITER_SCOPE_COMMENT)


async def test_household_with_several_seats_gets_one_strongest_cert():
    svc, _ = _svc(
        space=_space(allow_subscriber_comment=True),
        remote_rows=[
            _remote("peer", SpaceRole.SUBSCRIBER.value, "a"),
            _remote("peer", SpaceRole.MEMBER.value, "b"),
        ],
    )
    cert = await svc.issue_for_instance("sp-1", "peer")
    assert cert is not None and cert.scope == WRITER_SCOPE_WRITE


async def test_no_seat_no_cert():
    svc, _ = _svc(remote_rows=[_remote("other", SpaceRole.MEMBER.value)])
    assert await svc.issue_for_instance("sp-1", "peer") is None


async def test_own_household_scope_from_local_members():
    member = SpaceMember(
        space_id="sp-1", user_id="u1", role=SpaceRole.MEMBER.value, joined_at="t"
    )
    svc, _ = _svc(local=[member])
    cert = await svc.issue_for_instance("sp-1", "own")
    assert cert is not None
    _check(cert, OWN_PK, WRITER_SCOPE_WRITE)


async def test_own_household_never_borrows_the_hosts_mirrored_seats():
    """A seed-holding member household also holds the host's people in
    ``space_members`` (the §25.6 ``members`` sync). Their owner seat is not
    ours: it neither lifts our scope nor gets bound into our cert."""
    ours = SpaceMember(
        space_id="sp-1", user_id="u1", role=SpaceRole.MEMBER.value, joined_at="t"
    )
    host_owner = SpaceMember(
        space_id="sp-1",
        user_id="u-host-owner",
        role=SpaceRole.OWNER.value,
        joined_at="t",
    )
    svc, _ = _svc(
        space=_space(posts_access=SpaceFeatureAccess.ADMIN_ONLY),
        local=[ours],
        mirrored=[host_owner],
    )
    entitlement = await svc.entitlement_for_instance(
        _space(posts_access=SpaceFeatureAccess.ADMIN_ONLY), "own"
    )
    assert entitlement.scope == WRITER_SCOPE_COMMENT
    assert "u-host-owner" not in entitlement.user_ids


# ── Issuer gates ─────────────────────────────────────────────────────────


async def test_only_a_seed_holder_issues():
    svc, _ = _svc(seed=None, remote_rows=[_remote("peer", "member")])
    assert await svc.issue_for_instance("sp-1", "peer") is None


async def test_stale_seed_that_no_longer_matches_the_pin_issues_nothing():
    """A demoted admin's leftover seed (authority rotated) must not mint."""
    svc, _ = _svc(seed=os.urandom(32), remote_rows=[_remote("peer", "member")])
    assert await svc.issue_for_instance("sp-1", "peer") is None


async def test_no_epoch_no_cert():
    svc, _ = _svc(remote_rows=[_remote("peer", "member")], keys=_Keys(epoch=None))
    assert await svc.issue_for_instance("sp-1", "peer") is None


async def test_unknown_household_key_no_cert():
    svc, _ = _svc(remote_rows=[_remote("mesh", "member")])
    assert await svc.issue_for_instance("sp-1", "mesh") is None


async def test_explicit_epoch():
    svc, _ = _svc(remote_rows=[_remote("peer", "member")])
    cert = await svc.issue_for_instance("sp-1", "peer", epoch=9)
    assert cert is not None and cert.epoch == 9


# ── Delivery gates ───────────────────────────────────────────────────────


async def test_cert_for_peer_gated_on_v49():
    svc, _ = _svc(
        remote_rows=[_remote("peer", "member"), _remote("old", "member")],
    )
    wire = await svc.cert_for_peer("sp-1", "peer")
    assert wire is not None
    assert WriterCert.from_wire(wire).instance_pk == b64url_encode(PEER_PK)
    assert await svc.cert_for_peer("sp-1", "old") is None


async def test_cert_epoch_for_peer_signs_nothing(monkeypatch):
    """I5: the roster repair compares the writer-cert epoch per BEGIN — it
    is read without signing a cert, under the same gates as delivery."""
    svc, _ = _svc(
        remote_rows=[_remote("peer", "member"), _remote("old", "member")],
    )
    wire = await svc.cert_for_peer("sp-1", "peer")

    async def _never(*a, **kw):
        raise AssertionError("signed a cert")

    monkeypatch.setattr(SpaceWriterCertService, "issue_for_instance", _never)
    assert await svc.cert_epoch_for_peer("sp-1", "peer") == wire["epoch"]
    assert await svc.cert_epoch_for_peer("sp-1", "old") is None  # below v_49
    assert await svc.cert_epoch_for_peer("sp-1", "nobody") is None  # no seat
    assert await svc.cert_epoch_for_peer("sp-1", "own") is None


async def test_peer_payload_hook_adds_only_the_named_households_cert():
    svc, _ = _svc(
        remote_rows=[
            _remote("peer", "member"),
            _remote("fol", SpaceRole.SUBSCRIBER.value),
            _remote("old", "member"),
        ],
    )
    hook = svc.peer_payload_hook("sp-1")
    base = {"space_id": "sp-1"}
    for_peer = await hook("peer", base)
    assert base == {"space_id": "sp-1"}  # never mutated in place
    cert = WriterCert.from_wire(for_peer["writer_cert"])
    assert cert.instance_pk == b64url_encode(PEER_PK)
    # A follower without comment rights and an old peer get nothing.
    assert "writer_cert" not in await hook("fol", base)
    assert "writer_cert" not in await hook("old", base)


async def test_peer_payload_hook_can_be_limited_to_one_household():
    svc, _ = _svc(remote_rows=[_remote("peer", "member"), _remote("fol", "member")])
    hook = svc.peer_payload_hook("sp-1", only_instance="fol")
    assert "writer_cert" not in await hook("peer", {})
    assert "writer_cert" in await hook("fol", {})


class _GrantingChannels:
    """A v_51 channel service stand-in: one marker grant per household."""

    def __init__(self, *, boom: bool = False) -> None:
        self.boom = boom
        self.accepted: list[tuple[str, object]] = []

    async def grant_for_peer(self, space_id, instance_id, *, epoch=None):
        if self.boom:
            raise RuntimeError("boom")
        return {"grant_for": instance_id, "epoch": epoch}

    async def accept_grant(self, space_id, raw):
        if self.boom:
            raise RuntimeError("boom")
        self.accepted.append((space_id, raw))
        return True


async def test_peer_payload_hook_carries_each_households_channel_grant():
    """v_51: every member household's copy carries ITS channel grant — a
    reader household (no cert) too, since it subscribes."""
    svc, _ = _svc(
        remote_rows=[
            _remote("peer", "member"),
            _remote("fol", SpaceRole.SUBSCRIBER.value),
        ],
    )
    svc.attach_channels(_GrantingChannels())  # type: ignore[arg-type]
    hook = svc.peer_payload_hook("sp-1", epoch=7)
    for_peer = await hook("peer", {})
    assert for_peer["gfs_channel"]["grant_for"] == "peer"
    assert "writer_cert" in for_peer
    for_reader = await hook("fol", {})
    assert for_reader["gfs_channel"] == {"grant_for": "fol", "epoch": 7}
    assert "writer_cert" not in for_reader


async def test_channel_grant_failures_never_cost_the_payload():
    svc, _ = _svc(remote_rows=[_remote("peer", "member")])
    assert await svc.channel_grant_for_peer("sp-1", "peer") is None
    assert not await svc.accept_channel_grant("sp-1", {"x": 1})
    svc.attach_channels(_GrantingChannels(boom=True))  # type: ignore[arg-type]
    assert await svc.channel_grant_for_peer("sp-1", "peer") is None
    assert not await svc.accept_channel_grant("sp-1", {"x": 1})
    hook = svc.peer_payload_hook("sp-1")
    out = await hook("peer", {})
    assert "gfs_channel" not in out and "writer_cert" in out
    ok = _GrantingChannels()
    svc.attach_channels(ok)  # type: ignore[arg-type]
    assert await svc.accept_channel_grant("sp-1", {"x": 1})
    assert not await svc.accept_channel_grant("sp-1", None)
    assert ok.accepted == [("sp-1", {"x": 1})]


# ── Receiving + storing ──────────────────────────────────────────────────


def _issued(pk: bytes = OWN_PK, **over) -> dict:
    kw = {
        "space_seed": SEED,
        "space_id": "sp-1",
        "epoch": 2,
        "instance_pk": pk,
        "scope": WRITER_SCOPE_WRITE,
    }
    kw.update(over)
    return sign_writer_cert(**kw).to_wire()


async def test_accept_stores_a_valid_cert_for_us():
    svc, keys = _svc(seed=None)
    wire = _issued()
    assert await svc.accept("sp-1", wire) is True
    assert json.loads(keys.certs[("sp-1", 2)]) == wire
    own = await svc.own_cert("sp-1", 2)
    assert own is not None and own.to_wire() == wire


@pytest.mark.parametrize(
    "wire",
    [
        None,
        "junk",
        {"cert_suite": "ed25519"},
        _issued(pk=PEER_PK),  # names another household
        _issued(space_id="sp-2"),  # another space
        _issued(space_seed=os.urandom(32)),  # not the space authority
        {**_issued(), "cert_suite": "pq-future"},  # unknown suite
    ],
)
async def test_accept_refuses_anything_not_ours(wire):
    svc, keys = _svc(seed=None)
    assert await svc.accept("sp-1", wire) is False
    assert keys.certs == {}


async def test_accept_refuses_an_epoch_we_hold_no_key_for():
    svc, keys = _svc(seed=None)
    assert await svc.accept("sp-1", _issued(epoch=5)) is False
    assert keys.certs == {}


async def test_accept_unknown_space():
    svc, _ = _svc(seed=None)
    assert await svc.accept("sp-x", _issued()) is False


async def test_own_cert_self_issues_for_a_seed_holder():
    member = SpaceMember(
        space_id="sp-1", user_id="u1", role=SpaceRole.OWNER.value, joined_at="t"
    )
    svc, keys = _svc(local=[member])
    cert = await svc.own_cert("sp-1", 2)
    assert cert is not None
    _check(cert, OWN_PK, WRITER_SCOPE_WRITE)
    assert keys.certs == {}  # derived, never stored


async def test_own_cert_none_without_cert_or_seed():
    svc, keys = _svc(seed=None)
    assert await svc.own_cert("sp-1", 2) is None
    keys.certs[("sp-1", 2)] = "not json"
    assert await svc.own_cert("sp-1", 2) is None


async def test_current_own_cert_wire():
    svc, _keys = _svc(seed=None)
    assert await svc.current_own_cert_wire("sp-1") is None
    wire = _issued()
    await svc.accept("sp-1", wire)
    assert await svc.current_own_cert_wire("sp-1") == wire
    svc2, _ = _svc(seed=None, keys=_Keys(epoch=None))
    assert await svc2.current_own_cert_wire("sp-1") is None


def test_cert_instance_pk():
    assert SpaceWriterCertService.cert_instance_pk(_issued(pk=PEER_PK)) == PEER_PK
    assert SpaceWriterCertService.cert_instance_pk("junk") is None


# ── Verification of an item's cert (receiver side) ───────────────────────


async def test_check_item_cert_round_trip_and_failures():
    svc, _ = _svc()
    space = _space()
    wire = _issued(pk=PEER_PK)
    assert svc.check_item_cert(
        space,
        wire,
        epoch=2,
        author_pk=PEER_PK,
        required_scope=WRITER_SCOPE_WRITE,
        item_type="post",
    )
    assert not svc.check_item_cert(
        space,
        wire,
        epoch=3,
        author_pk=PEER_PK,
        required_scope=WRITER_SCOPE_WRITE,
        item_type="post",
    )
    assert not svc.check_item_cert(
        space,
        "junk",
        epoch=2,
        author_pk=PEER_PK,
        required_scope=WRITER_SCOPE_WRITE,
        item_type="post",
    )
    assert not svc.check_item_cert(
        replace(space, identity_public_key="zz"),
        wire,
        epoch=2,
        author_pk=PEER_PK,
        required_scope=WRITER_SCOPE_WRITE,
        item_type="post",
    )
    assert not svc.check_item_cert(
        space,
        {**wire, "cert_suite": "pq"},
        epoch=2,
        author_pk=PEER_PK,
        required_scope=WRITER_SCOPE_WRITE,
        item_type="post",
    )


async def test_without_federation_nothing_is_delivered():
    svc = SpaceWriterCertService(
        space_repo=_Spaces(_space(), SEED, ()),
        remote_member_repo=_Remote([_remote("peer", "member")]),
        space_key_repo=_Keys(),
        own_instance_id="own",
        own_identity_pk=OWN_PK,
    )
    assert await svc.cert_for_peer("sp-1", "peer") is None
    assert await svc.issue_for_instance("sp-1", "peer") is None


# ── Second review pass ───────────────────────────────────────────────────


async def test_tombstoned_seat_gets_no_cert():
    svc, _ = _svc(remote_rows=[_remote("peer", "member", tombstoned=True)])
    assert await svc.issue_for_instance("sp-1", "peer") is None


def test_scope_weakened():
    assert scope_weakened("write", "comment")
    assert scope_weakened("write", None)
    assert scope_weakened("comment", None)
    assert not scope_weakened("comment", "write")
    assert not scope_weakened(None, "comment")
    assert not scope_weakened("write", "write")


async def test_accept_keeps_a_newer_or_stronger_cert():
    svc, _keys = _svc(seed=None)
    assert await svc.accept("sp-1", _issued(issued_at=200))
    # Older → kept out.
    assert not await svc.accept("sp-1", _issued(issued_at=100, scope="comment"))
    # Same age, weaker → kept out; same age, write → in.
    assert not await svc.accept("sp-1", _issued(issued_at=200, scope="comment"))
    assert await svc.accept("sp-1", _issued(issued_at=200))
    # Newer → in, even weaker.
    assert await svc.accept("sp-1", _issued(issued_at=300, scope="comment"))
    assert (await svc.own_cert("sp-1", 2)).scope == "comment"  # type: ignore[union-attr]


async def test_a_stored_cert_from_a_retired_key_is_replaced_or_dropped():
    svc, _keys = _svc(seed=None)
    assert await svc.accept("sp-1", _issued(issued_at=500))
    seed2 = os.urandom(32)
    svc._spaces.space = replace(  # type: ignore[attr-defined]
        svc._spaces.space,  # type: ignore[attr-defined]
        identity_public_key=ed25519_public_key(seed2).hex(),
    )
    assert await svc.own_cert("sp-1", 2) is None
    # An older cert under the NEW key replaces the stale one.
    assert await svc.accept("sp-1", _issued(space_seed=seed2, issued_at=1))
    assert await svc.own_cert("sp-1", 2) is not None


async def test_own_cert_with_unreadable_storage_falls_back():
    svc, keys = _svc(seed=None)
    keys.certs[("sp-1", 2)] = "{not json"
    assert await svc.own_cert("sp-1", 2) is None


async def test_epoch_freshness():
    svc, keys = _svc(keys=_Keys(epoch=5, previous=3))
    assert await svc.epoch_is_fresh("sp-1", 5)
    assert await svc.epoch_is_fresh("sp-1", 3)
    assert not await svc.epoch_is_fresh("sp-1", 4)
    assert not await svc.epoch_is_fresh("sp-1", 1)
    late = datetime.now(UTC) + timedelta(seconds=WRITER_CERT_EPOCH_GRACE_S + 1)
    assert not await svc.epoch_is_fresh("sp-1", 3, now=late)
    keys.arrived = "2020-01-01 10:00:00"  # SQLite's naive UTC shape parses
    assert not await svc.epoch_is_fresh("sp-1", 3)
    keys.arrived = "garbage"
    assert not await svc.epoch_is_fresh("sp-1", 3)
    svc2, _ = _svc(keys=_Keys(epoch=None))
    assert not await svc2.epoch_is_fresh("sp-1", 0)
    svc3, _ = _svc(keys=_Keys(epoch=5))
    assert not await svc3.epoch_is_fresh("sp-1", 3)


async def test_check_item_adds_freshness():
    svc, _ = _svc(keys=_Keys(epoch=5, previous=3))
    space = _space()
    fresh = _issued(pk=PEER_PK, epoch=5)
    assert await svc.check_item(
        space,
        fresh,
        epoch=5,
        author_pk=PEER_PK,
        required_scope="write",
        item_type="post",
    )
    stale = _issued(pk=PEER_PK, epoch=4)
    assert not await svc.check_item(
        space,
        stale,
        epoch=4,
        author_pk=PEER_PK,
        required_scope="write",
        item_type="post",
    )
    assert not await svc.check_item(
        space,
        "junk",
        epoch=5,
        author_pk=PEER_PK,
        required_scope="write",
        item_type="post",
    )


async def test_peer_helpers():
    svc, _ = _svc()
    assert await svc.peer_is_cert_aware("peer")
    assert not await svc.peer_is_cert_aware("old")
    assert await svc.verified_instance_pk("peer") == PEER_PK
    assert await svc.verified_instance_pk("own") == OWN_PK
    bare = SpaceWriterCertService(
        space_repo=_Spaces(_space(), SEED, ()),
        remote_member_repo=_Remote([]),
        space_key_repo=_Keys(),
        own_instance_id="own",
        own_identity_pk=OWN_PK,
    )
    assert not await bare.peer_is_cert_aware("peer")


# ── Round 3: mesh-only households (no remote_instances row) ──────────────


async def test_a_mesh_only_household_key_is_bound_by_its_instance_id():
    """An instance id IS the fingerprint of its identity key (§4.1.2), the
    binding the v_31 routed-origin check verifies a mesh origin by. A
    claimed key that derives to the id is that household's key."""
    mesh_pk = ed25519_public_key(os.urandom(32))
    mesh_id = derive_instance_id(mesh_pk)
    svc, _ = _svc(remote_rows=[_remote(mesh_id, "member")])
    assert await svc.verified_instance_pk(mesh_id) is None
    assert await svc.verified_instance_pk(mesh_id, claimed=mesh_pk) == mesh_pk
    other = ed25519_public_key(os.urandom(32))
    assert await svc.verified_instance_pk(mesh_id, claimed=other) is None
    assert await svc.verified_instance_pk(mesh_id, claimed=b"short") is None
    # A pinned key always wins over a claim.
    assert await svc.verified_instance_pk("peer", claimed=other) == PEER_PK
    cert = await svc.issue_for_instance("sp-1", mesh_id, instance_pk_hint=mesh_pk)
    assert cert is not None
    _check(cert, mesh_pk, WRITER_SCOPE_WRITE)
    # Without a verifiable key: nothing.
    assert await svc.issue_for_instance("sp-1", mesh_id) is None


# ── v2: posts access level + the user binding ────────────────────────────


def _posts(level: SpaceFeatureAccess) -> Space:
    return _space(posts_access=level)


@pytest.mark.parametrize(
    ("level", "role", "scope"),
    [
        (SpaceFeatureAccess.ADMIN_ONLY, SpaceRole.MEMBER, WRITER_SCOPE_COMMENT),
        (SpaceFeatureAccess.ADMIN_ONLY, SpaceRole.MODERATOR, WRITER_SCOPE_COMMENT),
        (SpaceFeatureAccess.ADMIN_ONLY, SpaceRole.ADMIN, WRITER_SCOPE_WRITE),
        (SpaceFeatureAccess.MODERATED, SpaceRole.MEMBER, WRITER_SCOPE_COMMENT),
        (SpaceFeatureAccess.MODERATED, SpaceRole.MODERATOR, WRITER_SCOPE_WRITE),
        (SpaceFeatureAccess.MODERATED, SpaceRole.ADMIN, WRITER_SCOPE_WRITE),
        (SpaceFeatureAccess.OPEN, SpaceRole.MEMBER, WRITER_SCOPE_WRITE),
    ],
)
async def test_write_scope_follows_the_posts_access_level(level, role, scope):
    """P1 / P2: a plain member never gets ``write`` where it may not post
    directly — its posts keep going through the host (refusal / review)."""
    svc, _ = _svc(space=_posts(level), remote_rows=[_remote("peer", role.value)])
    cert = await svc.issue_for_instance("sp-1", "peer")
    assert cert is not None and cert.scope == scope


async def test_the_cert_binds_exactly_the_users_holding_its_scope():
    svc, _ = _svc(
        space=_posts(SpaceFeatureAccess.ADMIN_ONLY),
        remote_rows=[
            _remote("peer", SpaceRole.ADMIN.value, "boss"),
            _remote("peer", SpaceRole.MEMBER.value, "pleb"),
            _remote("peer", SpaceRole.ADMIN.value, "gone", tombstoned=True),
        ],
    )
    cert = await svc.issue_for_instance("sp-1", "peer")
    assert cert.writer_user_ids == ("boss@peer",)
    verify_writer_users(cert, space_pubkey=SPACE_PK, author_user_id="boss@peer")
    with pytest.raises(InvalidWriterCert):
        verify_writer_users(cert, space_pubkey=SPACE_PK, author_user_id="pleb@peer")
    # v1 verifiers still accept it.
    _check(cert, PEER_PK, WRITER_SCOPE_WRITE)


async def test_our_own_cert_binds_our_local_writers():
    local = [
        SpaceMember(space_id="sp-1", user_id="anna", role="owner", joined_at="x"),
        SpaceMember(space_id="sp-1", user_id="fan", role="subscriber", joined_at="x"),
    ]
    svc, _ = _svc(local=local)
    cert = await svc.issue_for_instance("sp-1", "own")
    assert cert.writer_user_ids == ("anna",)


async def test_entitlement_reports_scope_and_users():
    svc, _ = _svc(
        remote_rows=[
            _remote("peer", SpaceRole.MEMBER.value, "a"),
            _remote("peer", SpaceRole.MEMBER.value, "b"),
        ]
    )
    ent = await svc.entitlement_for_instance(_space(), "peer")
    assert ent == WriterEntitlement(WRITER_SCOPE_WRITE, frozenset({"a@peer", "b@peer"}))
    assert (await svc.entitlement_for_instance(_space(), "nobody")).scope is None


def test_scope_weakened_counts_a_shrinking_user_set():
    two = WriterEntitlement(WRITER_SCOPE_WRITE, frozenset({"a", "b"}))
    one = WriterEntitlement(WRITER_SCOPE_WRITE, frozenset({"a"}))
    assert scope_weakened(two, one)
    assert not scope_weakened(one, two)
    assert scope_weakened(
        two, WriterEntitlement(WRITER_SCOPE_COMMENT, frozenset({"a", "b"}))
    )
    assert not scope_weakened(two, two)
    # A swap (one leaves, another joins) still retires the leaver.
    assert scope_weakened(
        two, WriterEntitlement(WRITER_SCOPE_WRITE, frozenset({"a", "c"}))
    )


async def test_a_truncated_binding_warns_once_per_space_and_household(caplog):
    rows = [_remote("peer", SpaceRole.MEMBER.value, f"u{i:03d}") for i in range(70)]
    svc, _ = _svc(remote_rows=rows)
    with caplog.at_level("WARNING"):
        first = await svc.issue_for_instance("sp-1", "peer")
        await svc.issue_for_instance("sp-1", "peer")
    assert len(first.writer_user_ids) == 64
    warnings = [r for r in caplog.records if "binding only the first" in r.message]
    assert len(warnings) == 1


# ── Writer group key: every publicly readable space, v_90 ────────────────

_V90 = FederationCapability.MIN_FOR_ANON_MEMBER_PUBLISH_ONLY


def _key_svc(**kw):
    kw.setdefault("space", _space(allow_subscribers=True))
    kw.setdefault(
        "versions",
        {
            "peer": _V90,
            # A follower's comment cert (and so its key) needs v_96.
            "fol": FederationCapability.MIN_FOR_SUBSCRIBER_SEATS,
            "none": _V90,
            "old": _V90 - 1,
            "v57": FederationCapability.MIN_FOR_MULTI_GFS_PUBLISH,
        },
    )
    kw.setdefault("kek", KeyManager(os.urandom(32)))
    return _svc(**kw)


async def test_writer_key_goes_to_every_publisher_alike():
    svc, _ = _key_svc(
        space=_space(allow_subscribers=True, allow_subscriber_comment=True),
        remote_rows=[
            _remote("peer", "member"),
            _remote("fol", SpaceRole.SUBSCRIBER.value),
        ],
    )
    a = await svc.writer_key_for_peer("sp-1", "peer")
    b = await svc.writer_key_for_peer("sp-1", "fol")
    # write and comment scopes get the SAME key: the GFS can't tell them apart.
    assert a is not None and a == b
    seed = verify_writer_key_grant(
        WriterKeyGrant.from_wire(a), space_pubkey=SPACE_PK, space_id="sp-1"
    )
    assert seed == derive_writer_seed(SEED, "sp-1", 2)


async def test_writer_key_never_goes_to_a_non_publisher():
    svc, _ = _key_svc(remote_rows=[_remote("fol", SpaceRole.SUBSCRIBER.value)])
    # A follower without comment rights publishes nothing → no key.
    assert await svc.writer_key_for_peer("sp-1", "fol") is None
    assert await svc.writer_key_for_peer("sp-1", "none") is None


async def test_writer_key_never_goes_to_an_older_household():
    """v_90 — one mode, no compat path: a household below it (a v_57
    household too, which used to get the key) is handed none and publishes
    nothing over the GFS."""
    svc, _ = _key_svc(remote_rows=[_remote("old", "member"), _remote("v57", "member")])
    assert await svc.writer_key_for_peer("sp-1", "old") is None
    assert await svc.writer_key_for_peer("sp-1", "v57") is None


async def test_writer_key_not_in_a_space_nobody_may_follow():
    svc, _ = _key_svc(space=_space(), remote_rows=[_remote("peer", "member")])
    assert await svc.writer_key_for_peer("sp-1", "peer") is None
    assert await svc.writer_key_cert_wire("sp-1", 2) is None
    assert await svc.own_writer_key("sp-1", 2) is None


async def test_a_seed_holder_derives_and_pins_the_key():
    svc, _ = _key_svc()
    assert await svc.own_writer_key("sp-1", 4) == derive_writer_seed(SEED, "sp-1", 4)
    wire = await svc.writer_key_cert_wire("sp-1", 4)
    expected = issue_writer_key_grant(
        space_seed=SEED, space_id="sp-1", epoch=4
    ).writer_key_cert
    pk = verify_writer_key_cert(
        expected, space_pubkey=SPACE_PK, space_id="sp-1", epoch=4
    )
    assert wire is not None and wire["writer_pk"] == b64url_encode(pk)


@pytest.mark.parametrize(
    "space",
    [
        replace(_space(allow_subscribers=True), space_type=SpaceType.PRIVATE),
        replace(_space(allow_subscribers=True), space_type=SpaceType.HOUSEHOLD),
    ],
)
async def test_no_key_outside_the_public_tiers(space):
    svc, _ = _key_svc(space=space, remote_rows=[_remote("peer", "member")])
    assert await svc.writer_key_for_peer("sp-1", "peer") is None
    assert await svc.writer_key_cert_wire("sp-1", 2) is None
    assert await svc.own_writer_key("sp-1", 2) is None


def test_writer_key_min_version():
    assert writer_key_min_version(None) is None
    assert writer_key_min_version(_space()) is None
    assert writer_key_min_version(_space(allow_subscribers=True)) == _V90
    assert (
        writer_key_min_version(
            replace(_space(allow_subscribers=True), space_type=SpaceType.GLOBAL)
        )
        == _V90
    )


async def test_writer_key_needs_a_matching_seed():
    svc, _ = _key_svc(seed=os.urandom(32), remote_rows=[_remote("peer", "member")])
    assert await svc.writer_key_for_peer("sp-1", "peer") is None
    assert await svc.writer_key_cert_wire("sp-1", 2) is None
    svc, _ = _key_svc(seed=None, remote_rows=[_remote("peer", "member")])
    assert await svc.writer_key_for_peer("sp-1", "peer") is None


async def test_writer_key_needs_an_epoch_and_federation():
    svc, _ = _key_svc(keys=_Keys(None), remote_rows=[_remote("peer", "member")])
    assert await svc.writer_key_for_peer("sp-1", "peer") is None
    assert await svc.writer_key_for_peer("sp-1", "own") is None
    svc._federation = None
    assert await svc.writer_key_for_peer("sp-1", "peer") is None


async def test_peer_payload_hook_carries_the_writer_key_next_to_the_cert():
    svc, _ = _key_svc(remote_rows=[_remote("peer", "member"), _remote("old", "member")])
    hook = svc.peer_payload_hook("sp-1", epoch=2)
    out = await hook("peer", {})
    assert "writer_cert" in out and "writer_key" in out
    assert WriterKeyGrant.from_wire(out["writer_key"]).epoch == 2
    # An older household gets the cert (v_49) but no key.
    old = await svc.peer_payload_hook("sp-1")("old", {})
    assert "writer_key" not in old


async def test_writer_key_rotates_with_the_epoch():
    svc, _ = _key_svc(remote_rows=[_remote("peer", "member")])
    k2 = await svc.writer_key_for_peer("sp-1", "peer", epoch=2)
    k3 = await svc.writer_key_for_peer("sp-1", "peer", epoch=3)
    assert k2["writer_seed"] != k3["writer_seed"]


async def test_writer_key_cert_wire_pins_the_derived_key():
    svc, _ = _key_svc()
    wire = await svc.writer_key_cert_wire("sp-1", 4)
    from_cert = verify_writer_key_cert(
        WriterKeyGrant.from_wire(
            {
                **issue_writer_key_grant(
                    space_seed=SEED, space_id="sp-1", epoch=4
                ).to_wire()
            }
        ).writer_key_cert,
        space_pubkey=SPACE_PK,
        space_id="sp-1",
        epoch=4,
    )
    assert wire["writer_pk"] == b64url_encode(from_cert)


async def test_accept_writer_key_stores_it_wrapped_and_own_writer_key_reads_it():
    svc, keys = _key_svc(seed=None)
    grant = issue_writer_key_grant(space_seed=SEED, space_id="sp-1", epoch=2).to_wire()
    assert await svc.accept_writer_key("sp-1", grant) is True
    stored = keys.writer_keys[("sp-1", 2)]
    # KEK-wrapped at rest: the seed never sits in the clear.
    assert grant["writer_seed"] not in stored
    assert await svc.own_writer_key("sp-1", 2) == derive_writer_seed(SEED, "sp-1", 2)
    assert await svc.own_writer_key("sp-1", 3) is None


async def test_accept_writer_key_refuses_a_forged_grant():
    svc, keys = _key_svc(seed=None)
    forged = issue_writer_key_grant(
        space_seed=os.urandom(32), space_id="sp-1", epoch=2
    ).to_wire()
    assert await svc.accept_writer_key("sp-1", forged) is False
    assert await svc.accept_writer_key("sp-1", {"nope": 1}) is False
    assert await svc.accept_writer_key("sp-1", None) is False
    assert await svc.accept_writer_key("other", forged) is False
    # An epoch we hold no key for is dropped.
    later = issue_writer_key_grant(space_seed=SEED, space_id="sp-1", epoch=9).to_wire()
    assert await svc.accept_writer_key("sp-1", later) is False
    assert keys.writer_keys == {}


async def test_accept_writer_key_needs_a_kek():
    svc, _ = _key_svc(seed=None, kek=None)
    svc._kek = None
    grant = issue_writer_key_grant(space_seed=SEED, space_id="sp-1", epoch=2).to_wire()
    assert await svc.accept_writer_key("sp-1", grant) is False
    assert await svc.own_writer_key("sp-1", 2) is None


async def test_a_stored_writer_key_dies_with_an_authority_repin():
    svc, _keys = _key_svc(seed=None)
    grant = issue_writer_key_grant(space_seed=SEED, space_id="sp-1", epoch=2).to_wire()
    await svc.accept_writer_key("sp-1", grant)
    svc._spaces.space = replace(
        svc._spaces.space, identity_public_key=ed25519_public_key(os.urandom(32)).hex()
    )
    assert await svc.own_writer_key("sp-1", 2) is None


async def test_a_corrupt_stored_writer_key_is_ignored():
    svc, keys = _key_svc(seed=None)
    keys.writer_keys[("sp-1", 2)] = "garbage:garbage"
    assert await svc.own_writer_key("sp-1", 2) is None


async def test_a_seed_holder_derives_its_own_writer_key_only_when_readable():
    svc, _ = _key_svc()
    assert await svc.own_writer_key("sp-1", 5) == derive_writer_seed(SEED, "sp-1", 5)
    svc, _ = _key_svc(space=_space())
    assert await svc.own_writer_key("sp-1", 5) is None
    assert await svc.own_writer_key("other", 5) is None


# ── Mesh-only member households (no ``remote_instances`` row) ─────────────

MESH_PK = ed25519_public_key(os.urandom(32))


async def test_mesh_only_member_at_v51_gets_its_cert_under_its_own_key():
    """A member the host reaches only over the mesh holds no row, so its
    version and key come from its origin-authenticated mesh claim."""
    svc, _ = _svc(
        remote_rows=[_remote("mesh", "member")],
        mesh={"mesh": (FederationCapability.MIN_FOR_PRIVATE_CHANNELS, MESH_PK)},
    )
    wire = await svc.cert_for_peer("sp-1", "mesh")
    assert wire is not None
    cert = WriterCert.from_wire(wire)
    assert cert.instance_pk == b64url_encode(MESH_PK)
    _check(cert, MESH_PK, WRITER_SCOPE_WRITE)
    # …and the per-peer fan-out hook (rekey) decorates its copy too.
    out = await svc.peer_payload_hook("sp-1")("mesh", {"space_id": "sp-1"})
    assert WriterCert.from_wire(out["writer_cert"]).instance_pk == b64url_encode(
        MESH_PK
    )


async def test_mesh_only_member_of_unknown_version_gets_nothing():
    svc, _ = _svc(remote_rows=[_remote("mesh", "member")])
    assert await svc.cert_for_peer("sp-1", "mesh") is None
    assert "writer_cert" not in await svc.peer_payload_hook("sp-1")("mesh", {})


async def test_mesh_only_member_below_v49_gets_nothing():
    svc, _ = _svc(
        remote_rows=[_remote("mesh", "member")],
        mesh={"mesh": (FederationCapability.MIN_FOR_MEMBER_GFS_PUBLISH - 1, MESH_PK)},
    )
    assert await svc.cert_for_peer("sp-1", "mesh") is None


async def test_mesh_claim_without_a_seat_gets_nothing():
    svc, _ = _svc(
        remote_rows=[_remote("mesh", "member", tombstoned=True)],
        mesh={"mesh": (FederationCapability.MIN_FOR_PRIVATE_CHANNELS, MESH_PK)},
    )
    assert await svc.cert_for_peer("sp-1", "mesh") is None


async def test_mesh_only_member_at_v90_gets_the_writer_key():
    svc, _ = _svc(
        space=_space(allow_subscribers=True),
        remote_rows=[_remote("mesh", "member")],
        mesh={"mesh": (_V90, MESH_PK)},
    )
    out = await svc.peer_payload_hook("sp-1")("mesh", {})
    assert "writer_cert" in out
    grant = WriterKeyGrant.from_wire(out["writer_key"])
    verify_writer_key_grant(grant, space_pubkey=SPACE_PK, space_id="sp-1")
    assert grant.epoch == 2


async def test_mesh_only_member_below_v90_gets_no_writer_key():
    svc, _ = _svc(
        space=_space(allow_subscribers=True),
        remote_rows=[_remote("mesh", "member")],
        mesh={"mesh": (_V90 - 1, MESH_PK)},
    )
    out = await svc.peer_payload_hook("sp-1")("mesh", {})
    assert "writer_cert" in out and "writer_key" not in out


async def test_pinned_key_wins_over_a_mesh_claim():
    """A household we hold a row for is never re-keyed by a mesh claim."""
    svc, _ = _svc(remote_rows=[_remote("peer", "member")])
    svc._federation.mesh["peer"] = (99, MESH_PK)  # type: ignore[union-attr]
    assert await svc.verified_instance_pk("peer") == PEER_PK


# ── A cached item's cert after an authority rotation ─────────────────────


def _profile_item(cert: WriterCert, *, author: str = "u@peer") -> dict:
    return {
        "item_type": "author_profile",
        "author_user_id": author,
        "author_pk": PEER_PK.hex(),
        "origin_instance_id": "peer",
        "writer_cert": cert.to_wire(),
    }


async def test_a_cached_item_keeps_its_cert_while_it_verifies():
    svc, _ = _svc(remote_rows=[_remote("peer", "member")])
    cert = await svc.issue_for_instance("sp-1", "peer", epoch=2)
    item = _profile_item(cert)
    got = await svc.current_item_cert(
        _space(), item, epoch=2, item_type="author_profile"
    )
    assert got is item["writer_cert"]


async def test_a_cert_from_before_an_authority_rotation_is_re_stamped():
    """v_44: the old key signed the cached item's cert, so a new follower
    pinned to the new key refuses it. The seed holder re-issues one for the
    same household and epoch under the current key."""
    old_seed = os.urandom(32)
    old = bind_writer_users(
        sign_writer_cert(
            space_seed=old_seed,
            space_id="sp-1",
            epoch=2,
            instance_pk=PEER_PK,
            scope=WRITER_SCOPE_WRITE,
        ),
        space_seed=old_seed,
        user_ids=["u@peer"],
    )
    svc, _ = _svc(remote_rows=[_remote("peer", "member")])
    got = await svc.current_item_cert(
        _space(), _profile_item(old), epoch=2, item_type="author_profile"
    )
    assert got is not None and got != old.to_wire()
    cert = WriterCert.from_wire(got)
    _check(cert, PEER_PK, WRITER_SCOPE_COMMENT, epoch=2)
    verify_writer_users(cert, space_pubkey=SPACE_PK, author_user_id="u@peer")


async def test_no_current_cert_for_an_author_who_left_or_without_the_seed():
    old = sign_writer_cert(
        space_seed=os.urandom(32),
        space_id="sp-1",
        epoch=2,
        instance_pk=PEER_PK,
        scope=WRITER_SCOPE_WRITE,
    )
    svc, _ = _svc(remote_rows=[_remote("peer", "member")])
    # The household still writes, but this author is not one of its writers.
    assert (
        await svc.current_item_cert(
            _space(),
            _profile_item(old, author="gone@peer"),
            epoch=2,
            item_type="author_profile",
        )
        is None
    )
    # Not a seed holder: nothing to re-stamp with.
    no_seed, _ = _svc(seed=None, remote_rows=[_remote("peer", "member")])
    assert (
        await no_seed.current_item_cert(
            _space(), _profile_item(old), epoch=2, item_type="author_profile"
        )
        is None
    )
    bad = {**_profile_item(old), "author_pk": "zz"}
    assert (
        await svc.current_item_cert(_space(), bad, epoch=2, item_type="author_profile")
        is None
    )


# ── v_96: followers get a reader cert ────────────────────────────────────

_SUB = SpaceRole.SUBSCRIBER.value


def _follower_profile_item(cert: WriterCert) -> dict:
    return {
        "item_type": "author_profile",
        "author_user_id": "u@fol",
        "author_pk": FOLLOWER_PK.hex(),
        "origin_instance_id": "fol",
        "writer_cert": cert.to_wire(),
    }


@pytest.mark.parametrize(
    ("comment", "react", "kept"),
    [(False, False, False), (True, False, True), (False, True, True)],
)
async def test_current_item_cert_applies_the_follower_switches(comment, react, kept):
    """A follower's cached profile re-stamped for a new follower bundle: a
    reader cert travels only while a follower switch the receivers check is
    on — the sender drops what every receiver would refuse."""
    reader = bind_writer_users(
        sign_writer_cert(
            space_seed=SEED,
            space_id="sp-1",
            epoch=2,
            instance_pk=FOLLOWER_PK,
            scope=WRITER_SCOPE_COMMENT,
            reader=True,
        ),
        space_seed=SEED,
        user_ids=["u@fol"],
    )
    space = _space(allow_subscriber_comment=comment, allow_subscriber_react=react)
    svc, _ = _svc(space=space, remote_rows=[_remote("fol", _SUB)])
    got = await svc.current_item_cert(
        space, _follower_profile_item(reader), epoch=2, item_type="author_profile"
    )
    assert (got is not None) is kept
    # After an authority rotation the re-issued cert is gated the same way.
    old = bind_writer_users(
        sign_writer_cert(
            space_seed=os.urandom(32),
            space_id="sp-1",
            epoch=2,
            instance_pk=FOLLOWER_PK,
            scope=WRITER_SCOPE_COMMENT,
            reader=True,
        ),
        space_seed=SEED,
        user_ids=["u@fol"],
    )
    got = await svc.current_item_cert(
        space, _follower_profile_item(old), epoch=2, item_type="author_profile"
    )
    assert (got is not None) is kept


async def test_current_item_cert_refuses_a_reader_cert_on_a_type_it_cannot_carry():
    """A reader cert is never handed out for a post-type item, whatever the
    switches say."""
    space = _space(allow_subscriber_comment=True, allow_subscriber_react=True)
    svc, _ = _svc(space=space, remote_rows=[_remote("fol", _SUB)])
    cert = await svc.issue_for_instance("sp-1", "fol", epoch=2)
    assert cert is not None and cert.reader
    item = {**_follower_profile_item(cert), "item_type": "comment"}
    assert (
        await svc.current_item_cert(space, item, epoch=2, item_type="comment")
        is not None
    )
    assert (
        await svc.current_item_cert(space, item, epoch=2, item_type="post_delete")
        is None
    )


def test_gaining_a_follower_into_the_comment_set_is_not_weakened():
    """M1: a follower switch turning ON adds the household's follower to a
    member's plain comment set and marks it reader — users only grow, so
    nothing is revoked (the household is sent its new cert, no rekey)."""
    plain = WriterEntitlement(WRITER_SCOPE_COMMENT, frozenset({"b"}))
    grown = WriterEntitlement(WRITER_SCOPE_COMMENT, frozenset({"a", "b"}), reader=True)
    assert not scope_weakened(plain, grown)
    # Same users (a member demoted to follower) or fewer → weakened.
    assert scope_weakened(
        plain, WriterEntitlement(WRITER_SCOPE_COMMENT, frozenset({"b"}), reader=True)
    )
    assert scope_weakened(
        WriterEntitlement(WRITER_SCOPE_COMMENT, frozenset({"b", "c"})),
        WriterEntitlement(WRITER_SCOPE_COMMENT, frozenset({"b"}), reader=True),
    )


async def test_a_follower_switch_on_in_a_moderated_space_is_not_weakened():
    before = _space(posts_access=SpaceFeatureAccess.MODERATED)
    after = _space(
        posts_access=SpaceFeatureAccess.MODERATED, allow_subscriber_comment=True
    )
    svc, _ = _svc(
        space=after,
        remote_rows=[_remote("fol", _SUB, "a"), _remote("fol", "member", "b")],
    )
    was = await svc.entitlement_for_instance(before, "fol")
    now = await svc.entitlement_for_instance(after, "fol")
    assert now.reader and not was.reader
    assert not scope_weakened(was, now)


async def test_a_follower_below_v96_gets_no_writer_key():
    """M2: the writer group key follows the cert's issuance rules — a
    follower household that cannot be issued a reader cert gets no key."""
    space = _space(allow_subscribers=True, allow_subscriber_react=True)
    old = FederationCapability.MIN_FOR_SUBSCRIBER_SEATS - 1
    svc, _ = _svc(
        space=space, remote_rows=[_remote("fol", _SUB)], versions={"fol": old}
    )
    assert await svc.cert_for_peer("sp-1", "fol") is None
    assert await svc.writer_key_for_peer("sp-1", "fol") is None
    # At v_96 it gets both.
    new, _ = _svc(
        space=space,
        remote_rows=[_remote("fol", _SUB)],
        versions={"fol": FederationCapability.MIN_FOR_SUBSCRIBER_SEATS},
    )
    assert await new.writer_key_for_peer("sp-1", "fol") is not None


async def test_issue_without_followers_binds_only_seated_users():
    """I3: the approved-post re-stamp of a mixed household (a member under
    MODERATED next to a follower) is issued with ``followers=False`` — a
    plain cert binding only the member, which receivers accept on a post."""
    space = _space(
        allow_subscriber_react=True, posts_access=SpaceFeatureAccess.MODERATED
    )
    svc, _ = _svc(
        space=space,
        remote_rows=[_remote("fol", _SUB, "a"), _remote("fol", "member", "b")],
    )
    mixed = await svc.issue_for_instance("sp-1", "fol")
    assert mixed is not None and mixed.reader
    plain = await svc.issue_for_instance("sp-1", "fol", followers=False)
    assert plain is not None and not plain.reader
    assert plain.writer_user_ids == ("b@fol",)
    assert SpaceWriterCertService.check_item_cert(
        space,
        plain.to_wire(),
        epoch=2,
        author_pk=FOLLOWER_PK,
        required_scope=WRITER_SCOPE_COMMENT,
        item_type="post",
    )
    # A follower-only household has nothing without its follower seats.
    only, _ = _svc(space=space, remote_rows=[_remote("fol", _SUB)])
    assert await only.issue_for_instance("sp-1", "fol", followers=False) is None


@pytest.mark.parametrize(
    ("comment", "react"), [(True, False), (False, True), (True, True)]
)
async def test_a_follower_gets_a_reader_comment_cert_when_either_flag_is_on(
    comment, react
):
    space = _space(allow_subscriber_comment=comment, allow_subscriber_react=react)
    svc, _ = _svc(space=space, remote_rows=[_remote("fol", _SUB)])
    ent = await svc.entitlement_for_instance(space, "fol")
    assert ent == WriterEntitlement(
        WRITER_SCOPE_COMMENT, frozenset({"u@fol"}), reader=True
    )
    cert = await svc.issue_for_instance("sp-1", "fol")
    assert cert is not None
    assert cert.scope == WRITER_SCOPE_COMMENT and cert.reader is True
    assert cert.writer_user_ids == ("u@fol",)
    _check(WriterCert.from_wire(cert.to_wire()), FOLLOWER_PK, WRITER_SCOPE_COMMENT)
    wire = await svc.cert_for_peer("sp-1", "fol")
    assert wire is not None and wire["reader"] is True
    assert await svc.cert_epoch_for_peer("sp-1", "fol") == 2


async def test_a_follower_gets_nothing_when_both_flags_are_off():
    space = _space()
    svc, _ = _svc(space=space, remote_rows=[_remote("fol", _SUB)])
    assert (await svc.entitlement_for_instance(space, "fol")).scope is None
    assert await svc.issue_for_instance("sp-1", "fol") is None
    assert await svc.cert_for_peer("sp-1", "fol") is None


async def test_a_member_and_follower_household_writes_and_is_no_reader():
    space = _space(allow_subscriber_comment=True, allow_subscriber_react=True)
    svc, _ = _svc(
        space=space,
        remote_rows=[_remote("peer", _SUB, "a"), _remote("peer", "member", "b")],
        versions={"peer": FederationCapability.MIN_FOR_SUBSCRIBER_SEATS},
    )
    ent = await svc.entitlement_for_instance(space, "peer")
    assert ent == WriterEntitlement(WRITER_SCOPE_WRITE, frozenset({"b@peer"}))
    cert = await svc.issue_for_instance("sp-1", "peer")
    assert cert is not None and cert.scope == WRITER_SCOPE_WRITE
    assert cert.reader is False and "reader" not in cert.to_wire()
    # The follower is not bound into the write cert.
    assert cert.writer_user_ids == ("b@peer",)


async def test_a_comment_household_binding_a_follower_is_a_reader_as_a_whole():
    """A MODERATED space: the plain member comments, the follower too — one
    comment cert, flag-gated at the receivers as a whole."""
    space = _space(
        allow_subscriber_comment=True,
        posts_access=SpaceFeatureAccess.MODERATED,
    )
    svc, _ = _svc(
        space=space,
        remote_rows=[_remote("fol", _SUB, "a"), _remote("fol", "member", "b")],
    )
    ent = await svc.entitlement_for_instance(space, "fol")
    assert ent == WriterEntitlement(
        WRITER_SCOPE_COMMENT, frozenset({"a@fol", "b@fol"}), reader=True
    )
    # Without a follower in the binding, the same household is no reader.
    only_member, _ = _svc(space=space, remote_rows=[_remote("fol", "member", "b")])
    ent = await only_member.entitlement_for_instance(space, "fol")
    assert ent == WriterEntitlement(WRITER_SCOPE_COMMENT, frozenset({"b@fol"}))


async def test_a_household_below_v96_gets_no_reader_cert():
    space = _space(allow_subscriber_comment=True, allow_subscriber_react=True)
    old = FederationCapability.MIN_FOR_SUBSCRIBER_SEATS - 1
    svc, _ = _svc(
        space=space,
        remote_rows=[_remote("fol", _SUB)],
        versions={"fol": old},
    )
    assert await svc.issue_for_instance("sp-1", "fol") is None
    assert await svc.cert_for_peer("sp-1", "fol") is None
    assert await svc.cert_epoch_for_peer("sp-1", "fol") is None
    # A mesh-only follower household below v_96: nothing either.
    mesh, _ = _svc(
        space=space,
        remote_rows=[_remote("m", _SUB)],
        versions={},
        mesh={"m": (old, PEER_PK)},
    )
    assert await mesh.issue_for_instance("sp-1", "m") is None


async def test_an_older_mixed_household_keeps_a_non_reader_cert_for_its_members():
    """Below v_96 a follower is left out of the binding; the household's
    commenting members keep their plain comment cert."""
    space = _space(
        allow_subscriber_comment=True,
        posts_access=SpaceFeatureAccess.MODERATED,
    )
    svc, _ = _svc(
        space=space,
        remote_rows=[_remote("peer", _SUB, "a"), _remote("peer", "member", "b")],
        versions={"peer": FederationCapability.MIN_FOR_SUBSCRIBER_SEATS - 1},
    )
    cert = await svc.issue_for_instance("sp-1", "peer")
    assert cert is not None
    assert cert.scope == WRITER_SCOPE_COMMENT and cert.reader is False
    assert cert.writer_user_ids == ("b@peer",)


def test_becoming_a_reader_counts_as_weakened():
    users = frozenset({"a"})
    plain = WriterEntitlement(WRITER_SCOPE_COMMENT, users)
    reader = WriterEntitlement(WRITER_SCOPE_COMMENT, users, reader=True)
    assert scope_weakened(plain, reader)
    assert not scope_weakened(reader, plain)
    assert not scope_weakened(reader, reader)
    # Any change of reader is a change of entitlement (re-delivery).
    assert plain != reader


async def test_a_member_demoted_to_follower_weakens_the_entitlement():
    space = _space(
        allow_subscriber_comment=True,
        posts_access=SpaceFeatureAccess.MODERATED,
    )
    before, _ = _svc(space=space, remote_rows=[_remote("fol", "member")])
    after, _ = _svc(space=space, remote_rows=[_remote("fol", _SUB)])
    was = await before.entitlement_for_instance(space, "fol")
    now = await after.entitlement_for_instance(space, "fol")
    assert was.user_ids == now.user_ids and was.scope == now.scope
    assert scope_weakened(was, now)


async def test_accept_prefers_a_non_reader_cert_on_a_tie():
    svc, _ = _svc(seed=None)
    comment = WRITER_SCOPE_COMMENT
    assert await svc.accept("sp-1", _issued(issued_at=200, scope=comment))
    # Same age, reader → kept out (weaker).
    assert not await svc.accept(
        "sp-1", _issued(issued_at=200, scope=comment, reader=True)
    )
    # Newer reader → in.
    assert await svc.accept("sp-1", _issued(issued_at=300, scope=comment, reader=True))
    assert (await svc.own_cert("sp-1", 2)).reader is True  # type: ignore[union-attr]
    # Same age, non-reader → in.
    assert await svc.accept("sp-1", _issued(issued_at=300, scope=comment))
    assert (await svc.own_cert("sp-1", 2)).reader is False  # type: ignore[union-attr]


#: Which follower switch a reader cert's item type needs: "comment", "react",
#: "either" (author_profile), "always" (the own-comment delete, owner F6) or
#: None (never — posts and their edits/deletes).
_READER_NEEDS: dict[str, str | None] = {
    "post": None,
    "post_edit": None,
    "post_delete": None,
    "comment": "comment",
    "comment_edit": "comment",
    "comment_delete": "always",
    "reaction_add": "react",
    "reaction_remove": "react",
    "author_profile": "either",
}


def _reader_ok(item_type: str, comment: bool, react: bool) -> bool:
    needs = _READER_NEEDS[item_type]
    if needs == "comment":
        return comment
    if needs == "react":
        return react
    if needs == "always":
        return True
    if needs == "either":
        return comment or react
    return False


def test_the_reader_matrix_covers_every_item_type():
    assert set(_READER_NEEDS) == set(SUPPORTED_ITEM_TYPES)


@pytest.mark.parametrize("item_type", sorted(_READER_NEEDS))
@pytest.mark.parametrize(
    ("comment", "react"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_check_item_cert_applies_the_follower_switches_to_a_reader_cert(
    item_type, comment, react
):
    """Every item type × every follower-switch combination: a reader cert
    passes only where the matching switch is on; a plain comment cert of the
    same household is never gated by the switches."""
    svc, _ = _svc()
    space = _space(allow_subscriber_comment=comment, allow_subscriber_react=react)
    ok = _reader_ok(item_type, comment, react)
    kw = {
        "epoch": 2,
        "author_pk": FOLLOWER_PK,
        "required_scope": WRITER_SCOPE_COMMENT,
        "item_type": item_type,
    }
    plain = _issued(pk=FOLLOWER_PK, scope=WRITER_SCOPE_COMMENT)
    assert svc.check_item_cert(space, plain, **kw) is True
    wire = _issued(pk=FOLLOWER_PK, scope=WRITER_SCOPE_COMMENT, reader=True)
    assert svc.check_item_cert(space, wire, **kw) is ok
    assert await svc.check_item(space, wire, **kw) is ok
