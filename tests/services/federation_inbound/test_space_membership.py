"""Tests for :class:`SpaceMembershipInboundHandlers` (§13)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from socialhome.crypto import ed25519_public_key
from socialhome.domain.events import (
    RemoteSpaceCreated,
    RemoteSpaceDissolved,
    RemoteSpaceMemberBanned,
)
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from socialhome.gfs_server_list_sig import sign_gfs_server_list
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.federation_inbound import SpaceMembershipInboundHandlers
from socialhome.services.space_service import signed_ban_event

#: The space authority seed of :func:`_host_space` (its pinned public key).
SPACE_SEED = b"\x07" * 32


class _FakeRegistry:
    def __init__(self) -> None:
        self.registered = []

    def register(self, t, h):
        self.registered.append((t, h))


class _FakeFederationService:
    def __init__(self, own_instance_id: str = "self") -> None:
        self.own_instance_id = own_instance_id
        self._event_registry = _FakeRegistry()


class _FakeUsers:
    """Our own people, by user id (the owner household's, on the owner)."""

    def __init__(self, user_ids: set[str]) -> None:
        self._ids = user_ids

    async def get_by_user_id(self, user_id):
        return object() if user_id in self._ids else None


class _FakeSpaceRepo:
    def __init__(self) -> None:
        self.saved = []
        self.dissolved = []
        self.purged = []
        self.archived = []
        self.instance_removes = []
        self.bans = []
        self.unbans = []
        self.household_bans = []
        self.household_unbans = []
        self.household_banners = []
        #: (space_id, user_id | instance_id) -> banner user id.
        self.banners: dict = {}
        self.age_gates = []
        self.spaces: dict = {}

    async def save(self, space):
        self.saved.append(space)
        self.spaces[space.id] = space
        return space

    async def mark_dissolved(self, space_id):
        self.dissolved.append(space_id)

    async def purge(self, space_id):
        self.purged.append(space_id)

    async def set_archived(self, space_id, archived, reason=None):
        self.archived.append((space_id, archived, reason))

    async def remove_space_instance(self, space_id, instance_id):
        self.instance_removes.append((space_id, instance_id))

    async def ban_member(
        self, *, space_id, user_id, banned_by, identity_pk=None, reason=None
    ):
        self.bans.append((space_id, user_id, banned_by, reason))

    async def unban_member(self, space_id, user_id):
        self.unbans.append((space_id, user_id))

    async def ban_household(
        self, space_id, instance_id, *, reason=None, banned_by=None
    ):
        self.household_bans.append((space_id, instance_id, reason))
        self.household_banners.append((space_id, instance_id, banned_by))

    async def ban_banned_by(self, space_id, *, user_id=None, instance_id=None):
        return self.banners.get((space_id, user_id or instance_id))

    async def unban_household(self, space_id, instance_id):
        self.household_unbans.append((space_id, instance_id))

    async def update_age_gate(self, space_id, *, min_age=None):
        self.age_gates.append((space_id, min_age))

    async def get(self, space_id):
        return self.spaces.get(space_id)

    async def get_owner_user_id(self, space_id):
        return "u-owner"

    #: ``(user_id, role, joined_at, local)`` seats in ``space_members`` —
    #: ``local=False`` is a host person mirrored by the members sync.
    seats: tuple[tuple[str, str, str, bool], ...] = ()

    async def list_members(self, space_id):
        return [
            SimpleNamespace(user_id=u, role=r, joined_at=j)
            for u, r, j, _local in self.seats
        ]

    async def list_local_member_user_ids(self, space_id):
        return [u for u, _r, _j, local in self.seats if local]


def _host_space(space_id="sp-1", owner_instance_id="peer-a"):
    """A locally-stored space row whose owner is ``owner_instance_id`` —
    needed so the §CP.F1 host-authority guard on _on_age_gate can confirm
    the SPACE_AGE_GATE_UPDATED sender is the owning instance."""
    return Space(
        id=space_id,
        name="S",
        owner_instance_id=owner_instance_id,
        owner_username="owner",
        identity_public_key=ed25519_public_key(SPACE_SEED).hex(),
        config_sequence=1,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )


def _ban(
    *,
    banned=True,
    space_id="sp-1",
    from_instance="peer-a",
    seed=SPACE_SEED,
    **target,
):
    """A space-authority-signed ``SPACE_MEMBER_(UN)BANNED`` (P7b)."""
    event_type, payload = signed_ban_event(space_id, banned=banned, seed=seed, **target)
    return _event(event_type, payload, space_id=space_id, from_instance=from_instance)


def _event(event_type, payload, *, from_instance="peer-a", space_id=None):
    return FederationEvent(
        msg_id="m",
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(UTC).isoformat(),
        payload=payload,
        space_id=space_id,
    )


class _AdminSeats:
    """A roster mirror where the named households hold live admin seats."""

    def __init__(self, admins):
        self._admins = set(admins)

    async def list_instances_with_roles(self, space_id, roles):
        return set(self._admins)

    async def get(self, space_id, instance_id, user_id):
        return None


def replace_features(space, **flags):
    return dataclasses.replace(
        space, features=dataclasses.replace(space.features, **flags)
    )


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def repo():
    return _FakeSpaceRepo()


@pytest.fixture
def handlers(bus, repo):
    h = SpaceMembershipInboundHandlers(bus=bus, space_repo=repo)
    h.attach_to(_FakeFederationService())
    return h


async def test_attach_registers_eight_event_types(bus, repo):
    h = SpaceMembershipInboundHandlers(bus=bus, space_repo=repo)
    fed = _FakeFederationService()
    h.attach_to(fed)
    types = {t for t, _ in fed._event_registry.registered}
    assert types == {
        FederationEventType.SPACE_CREATED,
        FederationEventType.SPACE_DISSOLVED,
        FederationEventType.SPACE_SYNC_REJECTED,
        FederationEventType.SPACE_INSTANCE_LEFT,
        FederationEventType.SPACE_MEMBER_BANNED,
        FederationEventType.SPACE_MEMBER_UNBANNED,
        FederationEventType.SPACE_AGE_GATE_UPDATED,
        FederationEventType.SPACE_CONFIG_CATCH_UP,
    }


async def test_space_created_persists_and_publishes(bus, repo, handlers):
    captured: list[RemoteSpaceCreated] = []
    bus.subscribe(RemoteSpaceCreated, captured.append)
    await handlers._on_created(
        _event(
            FederationEventType.SPACE_CREATED,
            {
                "name": "Space 1",
                "owner_username": "owner",
                "identity_public_key": "aa" * 32,
                "space_type": SpaceType.HOUSEHOLD.value,
                "join_mode": JoinMode.INVITE_ONLY.value,
                "config_sequence": 5,
            },
            space_id="sp-1",
        )
    )
    assert len(repo.saved) == 1
    assert repo.saved[0].id == "sp-1"
    assert repo.saved[0].config_sequence == 5
    assert captured[0].space_id == "sp-1"


async def test_space_created_refused_when_owned_by_another_host(repo, handlers):
    """§D1b anti-hijack — a SPACE_CREATED for an id we already hold under a
    DIFFERENT host must not clobber our row (no save)."""
    repo.spaces["sp-1"] = _host_space("sp-1", owner_instance_id="the-real-host")
    await handlers._on_created(
        _event(
            FederationEventType.SPACE_CREATED,
            {
                "name": "Spoof",
                "owner_username": "x",
                "identity_public_key": "aa" * 32,
            },
            from_instance="peer-a",  # not the owner
            space_id="sp-1",
        )
    )
    assert repo.saved == []  # existing row untouched


async def test_space_created_missing_identity_key_drops(repo, handlers):
    await handlers._on_created(
        _event(
            FederationEventType.SPACE_CREATED,
            {"name": "X"},
            space_id="sp-1",
        )
    )
    assert repo.saved == []


async def test_space_dissolved_from_owner_archives_read_only(bus, repo, handlers):
    """A remote SPACE_DISSOLVED from the space's OWNER archives the local
    copy read-only (``archived_reason='dissolved'``) — it must NOT destroy
    the member's local data — and publishes the local removal event."""
    repo.spaces["sp-1"] = _host_space("sp-1", owner_instance_id="peer-a")
    captured: list[RemoteSpaceDissolved] = []
    bus.subscribe(RemoteSpaceDissolved, captured.append)
    await handlers._on_dissolved(
        _event(
            FederationEventType.SPACE_DISSOLVED,
            {},
            from_instance="peer-a",  # the owner
            space_id="sp-1",
        )
    )
    assert repo.archived == [("sp-1", True, "dissolved")]
    assert repo.purged == []  # no hard delete — local copy survives
    assert repo.spaces.get("sp-1") is not None  # still exists
    assert captured[0].space_id == "sp-1"


async def test_space_dissolved_from_non_owner_is_rejected(bus, repo, handlers):
    """Security: a non-owner peer must NOT be able to terminate your space.
    No archive, no purge, no publish."""
    repo.spaces["sp-1"] = _host_space("sp-1", owner_instance_id="the-real-host")
    captured: list[RemoteSpaceDissolved] = []
    bus.subscribe(RemoteSpaceDissolved, captured.append)
    await handlers._on_dissolved(
        _event(
            FederationEventType.SPACE_DISSOLVED,
            {},
            from_instance="peer-a",  # not the owner
            space_id="sp-1",
        )
    )
    assert repo.archived == []
    assert repo.purged == []
    assert captured == []


async def test_sync_rejected_dissolved_from_owner_archives(bus, repo, handlers):
    """§S-1 reconnect backstop: host rejected our sync with reason
    'dissolved' → archive read-only + publish RemoteSpaceDissolved."""
    repo.spaces["sp-1"] = _host_space("sp-1", owner_instance_id="peer-a")
    captured: list[RemoteSpaceDissolved] = []
    bus.subscribe(RemoteSpaceDissolved, captured.append)
    await handlers._on_sync_rejected(
        _event(
            FederationEventType.SPACE_SYNC_REJECTED,
            {"reason": "dissolved"},
            from_instance="peer-a",  # the owner
            space_id="sp-1",
        )
    )
    assert repo.archived == [("sp-1", True, "dissolved")]
    assert captured[0].space_id == "sp-1"


async def test_sync_rejected_removed_from_owner_archives(bus, repo, handlers):
    """reason='removed' (space still exists on host, we're not a member) →
    archive with reason='removed'."""
    repo.spaces["sp-1"] = _host_space("sp-1", owner_instance_id="peer-a")
    captured: list[RemoteSpaceDissolved] = []
    bus.subscribe(RemoteSpaceDissolved, captured.append)
    await handlers._on_sync_rejected(
        _event(
            FederationEventType.SPACE_SYNC_REJECTED,
            {"reason": "removed"},
            from_instance="peer-a",
            space_id="sp-1",
        )
    )
    assert repo.archived == [("sp-1", True, "removed")]
    assert captured[0].space_id == "sp-1"


@pytest.mark.parametrize(
    ("ours_joined", "archived"),
    [
        ("2026-10-10 10:00:00", True),  # our seat predates the removal
        ("2026-10-10 12:00:00", False),  # we joined again after it
    ],
)
async def test_sync_rejected_removed_weighs_only_our_own_joins(
    bus, repo, handlers, ours_joined, archived
):
    """``space_members`` also mirrors the host's people (the §25.6
    ``members`` sync). A host person who joined after our removal is no
    re-join of ours: the copy is archived all the same."""
    repo.spaces["sp-1"] = _host_space("sp-1", owner_instance_id="peer-a")
    repo.seats = (
        ("u-ours", "member", ours_joined, True),
        ("u-host-new", "member", "2026-10-10 13:00:00", False),
    )
    await handlers._on_sync_rejected(
        _event(
            FederationEventType.SPACE_SYNC_REJECTED,
            {"reason": "removed", "removed_at": "2026-10-10 11:00:00"},
            from_instance="peer-a",
            space_id="sp-1",
        )
    )
    assert (repo.archived == [("sp-1", True, "removed")]) is archived


async def test_sync_rejected_from_non_owner_is_rejected(bus, repo, handlers):
    """Security: only the OWNER host may terminate our copy — a non-owner
    SPACE_SYNC_REJECTED is dropped (no archive, no publish)."""
    repo.spaces["sp-1"] = _host_space("sp-1", owner_instance_id="the-real-host")
    captured: list[RemoteSpaceDissolved] = []
    bus.subscribe(RemoteSpaceDissolved, captured.append)
    await handlers._on_sync_rejected(
        _event(
            FederationEventType.SPACE_SYNC_REJECTED,
            {"reason": "dissolved"},
            from_instance="peer-a",  # not the owner
            space_id="sp-1",
        )
    )
    assert repo.archived == []
    assert captured == []


async def test_sync_rejected_unknown_reason_is_dropped(bus, repo, handlers):
    """An unknown/missing reason is dropped — never archive on a
    garbage/forward-incompatible payload."""
    repo.spaces["sp-1"] = _host_space("sp-1", owner_instance_id="peer-a")
    captured: list[RemoteSpaceDissolved] = []
    bus.subscribe(RemoteSpaceDissolved, captured.append)
    await handlers._on_sync_rejected(
        _event(
            FederationEventType.SPACE_SYNC_REJECTED,
            {"reason": "later-invented-reason"},
            from_instance="peer-a",
            space_id="sp-1",
        )
    )
    assert repo.archived == []
    assert captured == []
    # missing reason entirely
    await handlers._on_sync_rejected(
        _event(
            FederationEventType.SPACE_SYNC_REJECTED,
            {},
            from_instance="peer-a",
            space_id="sp-1",
        )
    )
    assert repo.archived == []
    assert captured == []


async def test_sync_rejected_absent_space_is_noop(repo, handlers):
    """No local row for the space → nothing to archive."""
    await handlers._on_sync_rejected(
        _event(
            FederationEventType.SPACE_SYNC_REJECTED,
            {"reason": "removed"},
            from_instance="peer-a",
            space_id="sp-gone",
        )
    )
    assert repo.archived == []


async def test_sync_rejected_already_terminally_archived_is_idempotent(repo, handlers):
    """A copy already in a terminal archive (archived_reason set) is left
    untouched — set_archived NOT called again."""
    repo.spaces["sp-1"] = Space(
        id="sp-1",
        name="S",
        owner_instance_id="peer-a",
        owner_username="owner",
        identity_public_key="aa" * 32,
        config_sequence=1,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        archived=True,
        archived_reason="dissolved",
    )
    await handlers._on_sync_rejected(
        _event(
            FederationEventType.SPACE_SYNC_REJECTED,
            {"reason": "removed"},
            from_instance="peer-a",
            space_id="sp-1",
        )
    )
    assert repo.archived == []


async def test_sync_rejected_upgrades_admin_archived_space(bus, repo, handlers):
    """An admin-archived space (archived=True, archived_reason=None) is NOT
    terminal — a terminal reason must still upgrade it."""
    repo.spaces["sp-1"] = Space(
        id="sp-1",
        name="S",
        owner_instance_id="peer-a",
        owner_username="owner",
        identity_public_key="aa" * 32,
        config_sequence=1,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
        archived=True,
        archived_reason=None,
    )
    captured: list[RemoteSpaceDissolved] = []
    bus.subscribe(RemoteSpaceDissolved, captured.append)
    await handlers._on_sync_rejected(
        _event(
            FederationEventType.SPACE_SYNC_REJECTED,
            {"reason": "removed"},
            from_instance="peer-a",
            space_id="sp-1",
        )
    )
    assert repo.archived == [("sp-1", True, "removed")]
    assert captured[0].space_id == "sp-1"


async def test_instance_left_removes_row(repo, handlers):
    await handlers._on_instance_left(
        _event(
            FederationEventType.SPACE_INSTANCE_LEFT,
            {},
            space_id="sp-1",
            from_instance="peer-a",
        )
    )
    assert repo.instance_removes == [("sp-1", "peer-a")]


async def test_member_banned_persists_and_publishes(bus, repo, handlers):
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    captured: list[RemoteSpaceMemberBanned] = []
    bus.subscribe(RemoteSpaceMemberBanned, captured.append)
    await handlers._on_banned(_ban(user_id="u-1", banned_by="admin-a"))
    assert repo.bans == [("sp-1", "u-1", "admin-a", None)]
    assert captured[0].user_id == "u-1"


async def test_member_banned_falls_back_to_from_instance_when_no_banned_by(
    repo,
    handlers,
):
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_banned(_ban(user_id="u-1"))
    assert repo.bans == [("sp-1", "u-1", "peer-a", None)]


async def test_member_unbanned_removes_ban(repo, handlers):
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_unbanned(_ban(banned=False, user_id="u-1"))
    assert repo.unbans == [("sp-1", "u-1")]


async def test_household_ban_and_unban_land(repo, handlers):
    """P7b — a household ban names an instance, never a person."""
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_banned(_ban(instance_id="peer-bad"))
    assert repo.household_bans == [("sp-1", "peer-bad", None)]
    assert repo.bans == []
    await handlers._on_unbanned(_ban(banned=False, instance_id="peer-bad"))
    assert repo.household_unbans == [("sp-1", "peer-bad")]


@pytest.mark.parametrize("banned", [True, False])
async def test_a_ban_without_the_authority_signature_is_refused(repo, handlers, banned):
    """P7b — the host's own unsigned word is not enough any more: every
    ban / unban is space-authority signed (no sender ever sent it
    unsigned)."""
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    handler = handlers._on_banned if banned else handlers._on_unbanned
    event_type = (
        FederationEventType.SPACE_MEMBER_BANNED
        if banned
        else FederationEventType.SPACE_MEMBER_UNBANNED
    )
    await handler(_event(event_type, {"user_id": "u-1"}, space_id="sp-1"))
    await handler(_ban(banned=banned, user_id="u-1", seed=b"\x08" * 32))
    assert repo.bans == [] and repo.unbans == []


async def test_a_signed_ban_lifted_onto_another_target_is_refused(repo, handlers):
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    ev = _ban(user_id="u-1")
    ev.payload["user_id"] = "u-owner"
    await handlers._on_banned(ev)
    assert repo.bans == []


async def test_a_ban_naming_both_or_neither_target_is_refused(repo, handlers):
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_banned(_ban(user_id="u-1", instance_id="peer-bad"))
    await handlers._on_banned(_ban())
    assert repo.bans == [] and repo.household_bans == []


async def test_a_household_ban_of_the_owner_household_is_refused(repo, handlers):
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_banned(_ban(instance_id="peer-a"))
    assert repo.household_bans == []


async def test_a_delegated_admins_signed_ban_reaches_the_owner(bus, repo):
    """P7b leak L1: on the OWNER, a ban from a seed-holding admin household
    (live admin seat, delegation on) lands — before, the owner refused it
    and the person came back through the owner's link."""
    space = _host_space(owner_instance_id="self-owner")
    repo.spaces["sp-1"] = replace_features(space, delegated_admin_authority=True)
    h = SpaceMembershipInboundHandlers(
        bus=bus,
        space_repo=repo,
        seat_repo=_AdminSeats({"admin-house"}),
        users=_FakeUsers({"u-owner"}),
    )
    h.attach_to(_FakeFederationService("self-owner"))
    await h._on_banned(_ban(user_id="u-x", from_instance="admin-house"))
    await h._on_banned(_ban(instance_id="peer-bad", from_instance="admin-house"))
    assert repo.bans == [("sp-1", "u-x", "admin-house", None)]
    assert repo.household_bans == [("sp-1", "peer-bad", None)]
    # A household with no admin seat cannot, even with a lifted signature.
    await h._on_banned(_ban(user_id="u-y", from_instance="member-house"))
    # Nor may the admin ban one of the owner household's people.
    await h._on_banned(_ban(user_id="u-owner", from_instance="admin-house"))
    assert [b[1] for b in repo.bans] == ["u-x"]


async def test_age_gate_updates_min_age_only(repo, handlers):
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_age_gate(
        _event(
            FederationEventType.SPACE_AGE_GATE_UPDATED,
            {"min_age": 13},
            space_id="sp-1",
        )
    )
    assert repo.age_gates == [("sp-1", 13)]


async def test_age_gate_target_audience_only_is_noop(repo, handlers):
    """The age gate is ``min_age``-only now — a payload carrying only the
    legacy ``target_audience`` field (e.g. from an older peer) is ignored."""
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_age_gate(
        _event(
            FederationEventType.SPACE_AGE_GATE_UPDATED,
            {"target_audience": "family"},
            space_id="sp-1",
        )
    )
    assert repo.age_gates == []


async def test_age_gate_ignores_legacy_target_audience(repo, handlers):
    """An older peer ships both fields; we apply min_age and ignore the
    legacy target_audience (it never reaches the repo)."""
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_age_gate(
        _event(
            FederationEventType.SPACE_AGE_GATE_UPDATED,
            {"min_age": 16, "target_audience": "teen"},
            space_id="sp-1",
        )
    )
    assert repo.age_gates == [("sp-1", 16)]


async def test_age_gate_empty_payload_is_noop(repo, handlers):
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_age_gate(
        _event(
            FederationEventType.SPACE_AGE_GATE_UPDATED,
            {},
            space_id="sp-1",
        )
    )
    assert repo.age_gates == []


async def test_age_gate_from_non_host_is_dropped(repo, handlers):
    """§CP.F1 host authority — a paired peer that doesn't OWN the space
    must not be able to lower/rewrite our gate (else it could set
    min_age=0 and disable child protection on a space we host)."""
    repo.spaces["sp-1"] = _host_space(owner_instance_id="the-real-host")
    await handlers._on_age_gate(
        _event(
            FederationEventType.SPACE_AGE_GATE_UPDATED,
            {"min_age": 0},
            from_instance="peer-a",  # not the owner
            space_id="sp-1",
        )
    )
    assert repo.age_gates == []  # dropped, gate untouched


async def test_age_gate_unknown_space_is_dropped(repo, handlers):
    await handlers._on_age_gate(
        _event(
            FederationEventType.SPACE_AGE_GATE_UPDATED,
            {"min_age": 13},
            space_id="sp-unknown",
        )
    )
    assert repo.age_gates == []


async def test_age_gate_invalid_min_age_is_ignored(repo, handlers):
    """A non-conforming peer shipping min_age outside {0,13,16,18} must not
    reach the schema CHECK — the bad value is ignored (fail-soft)."""
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_age_gate(
        _event(
            FederationEventType.SPACE_AGE_GATE_UPDATED,
            {"min_age": 15},  # not in {0,13,16,18}
            space_id="sp-1",
        )
    )
    assert repo.age_gates == []  # invalid → no update


async def test_config_catch_up_logs_when_behind(repo, handlers, caplog):
    """When remote sequence > local, log that we're behind."""
    import logging

    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )

    repo.spaces["sp-1"] = Space(
        id="sp-1",
        name="S",
        owner_instance_id="peer-a",
        owner_username="owner",
        identity_public_key="aa" * 32,
        config_sequence=2,
        features=SpaceFeatures(),
        space_type=SpaceType.HOUSEHOLD,
        join_mode=JoinMode.INVITE_ONLY,
    )
    with caplog.at_level(
        logging.INFO, logger="socialhome.services.federation_inbound.space_membership"
    ):
        await handlers._on_catch_up(
            _event(
                FederationEventType.SPACE_CONFIG_CATCH_UP,
                {"sequence": 5},
                space_id="sp-1",
            )
        )
    assert any("we are behind" in rec.message for rec in caplog.records)


async def test_config_catch_up_unknown_space_is_noop(handlers):
    """No local row for the space → just drop."""
    # Should not raise.
    await handlers._on_catch_up(
        _event(
            FederationEventType.SPACE_CONFIG_CATCH_UP,
            {"sequence": 5},
            space_id="never-heard-of-it",
        )
    )


async def test_config_catch_up_missing_space_id_is_noop(handlers):
    await handlers._on_catch_up(
        _event(
            FederationEventType.SPACE_CONFIG_CATCH_UP,
            {"sequence": 5},
        )
    )


# ─── Defensive early-return paths ────────────────────────────────────────


async def test_space_created_missing_space_id_is_noop(repo, handlers):
    """No space_id → handler returns silently (no save, no event)."""
    await handlers._on_created(_event(FederationEventType.SPACE_CREATED, {}))
    assert repo.saved == []


async def test_space_created_missing_identity_pk_is_noop(repo, handlers):
    """Without identity_public_key the row can't be persisted — drop."""
    await handlers._on_created(
        _event(
            FederationEventType.SPACE_CREATED,
            {"name": "X", "space_type": "private", "join_mode": "open"},
            space_id="sp-x",
        )
    )
    assert repo.saved == []


async def test_space_created_with_bad_enums_falls_back_to_defaults(repo, handlers):
    """An unknown space_type / join_mode coerces to PRIVATE / INVITE_ONLY
    (forward-compat: a newer peer's enum value should not crash us)."""
    await handlers._on_created(
        _event(
            FederationEventType.SPACE_CREATED,
            {
                "name": "S",
                "identity_public_key": "aa" * 32,
                "space_type": "what-even",
                "join_mode": "unknown-future-mode",
            },
            space_id="sp-y",
        )
    )
    assert len(repo.saved) == 1
    saved = repo.saved[0]
    assert saved.space_type is SpaceType.PRIVATE
    assert saved.join_mode is JoinMode.INVITE_ONLY


async def test_space_dissolved_missing_id_is_noop(repo, handlers):
    await handlers._on_dissolved(
        _event(FederationEventType.SPACE_DISSOLVED, {}),
    )
    assert repo.dissolved == []


async def test_space_instance_left_missing_id_is_noop(repo, handlers):
    await handlers._on_instance_left(
        _event(FederationEventType.SPACE_INSTANCE_LEFT, {}),
    )
    assert repo.instance_removes == []


async def test_member_banned_missing_user_id_is_noop(repo, handlers):
    """A SPACE_MEMBER_BANNED naming nobody is malformed — drop."""
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    captured: list[RemoteSpaceMemberBanned] = []
    bus = handlers._bus
    bus.subscribe(RemoteSpaceMemberBanned, captured.append)
    await handlers._on_banned(_ban(banned_by="x"))
    assert repo.bans == []
    assert captured == []


async def test_member_unbanned_missing_user_id_is_noop(repo, handlers):
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_unbanned(_ban(banned=False))
    assert repo.unbans == []


async def test_age_gate_missing_space_id_is_noop(repo, handlers):
    await handlers._on_age_gate(
        _event(FederationEventType.SPACE_AGE_GATE_UPDATED, {"min_age": 13}),
    )
    assert repo.age_gates == []


# ─── _push_config_to (catch-up reply path) ───────────────────────────────


class _RecordingFederation:
    def __init__(self) -> None:
        self._event_registry = _FakeRegistry()
        self.sent: list[tuple[str, FederationEventType, dict]] = []
        self.space_ids: list[str | None] = []

    async def send_event(self, *, to_instance_id, event_type, payload, space_id=None):
        self.sent.append((to_instance_id, event_type, payload))
        self.space_ids.append(space_id)


async def test_catch_up_replays_config_when_we_are_ahead(bus, repo):
    """When the requester's seq is BEHIND ours, push a
    ``SPACE_CONFIG_CHANGED`` snapshot to them so they catch up."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )

    repo.spaces["sp-ahead"] = Space(
        id="sp-ahead",
        name="Ahead",
        owner_instance_id="me",
        owner_username="alice",
        identity_public_key="aa" * 32,
        config_sequence=7,
        features=SpaceFeatures(),
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.OPEN,
        description="hi",
        emoji="🌱",
    )
    h = SpaceMembershipInboundHandlers(bus=bus, space_repo=repo)
    fed = _RecordingFederation()
    h.attach_to(fed)
    await h._on_catch_up(
        _event(
            FederationEventType.SPACE_CONFIG_CATCH_UP,
            {"sequence": 2},
            from_instance="peer-behind",
            space_id="sp-ahead",
        )
    )
    assert len(fed.sent) == 1
    to, ev, payload = fed.sent[0]
    assert to == "peer-behind"
    assert ev is FederationEventType.SPACE_CONFIG_CHANGED
    assert payload["space_id"] == "sp-ahead"
    # v_88 — the routing field too, so a GFS-only household gets it on the
    # mailbox of THIS space (a payload-only space_id is refused there).
    assert fed.space_ids == ["sp-ahead"]
    assert payload["sequence"] == 7
    assert payload["space_type"] == "public"
    assert payload["join_mode"] == "open"


async def test_catch_up_no_push_without_federation_service(bus, repo, caplog):
    """``_push_config_to`` is a no-op when federation isn't wired — the
    test that constructs handlers with the dummy ``_FakeFederationService``
    (which has ``send_event = AttributeError`` if called) covers that
    path silently."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )

    h = SpaceMembershipInboundHandlers(bus=bus, space_repo=repo)
    # No attach_to → ``self._federation`` stays ``None``.
    space = Space(
        id="sp-z",
        name="Z",
        owner_instance_id="me",
        owner_username="me",
        identity_public_key="bb" * 32,
        config_sequence=3,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )
    # Direct call — must return silently without raising.
    await h._push_config_to("peer", space)


# ── F7: a ban is the host's word, nobody else's ───────────────────────


async def test_member_banned_from_a_non_host_is_refused(repo, handlers):
    """``ban_member`` inserts the ban AND deletes the ``space_members``
    row. A household that is not a seed holder here — a read-only Follower
    household seated over the GFS relay included — must not ban the space's
    own owner out of it, even replaying a validly signed ban."""
    repo.spaces["sp-1"] = _host_space(owner_instance_id="the-real-host")
    await handlers._on_banned(_ban(user_id="u-owner", from_instance="peer-follower"))
    assert repo.bans == []


async def test_member_unbanned_from_a_non_host_is_refused(repo, handlers):
    """The mirror image: clearing a real ban is how a banned household
    walks back in."""
    repo.spaces["sp-1"] = _host_space(owner_instance_id="the-real-host")
    await handlers._on_unbanned(
        _ban(banned=False, user_id="u-1", from_instance="peer-follower")
    )
    assert repo.unbans == []


async def test_member_banned_for_an_unknown_space_is_refused(repo, handlers):
    """No local row means no key to check against. A ban we cannot
    attribute is a ban we do not apply."""
    await handlers._on_banned(_ban(user_id="u-1", space_id="sp-unknown"))
    assert repo.bans == []


async def test_member_unbanned_for_an_unknown_space_is_refused(repo, handlers):
    await handlers._on_unbanned(
        _ban(banned=False, user_id="u-1", space_id="sp-unknown")
    )
    assert repo.unbans == []


# ─── v_49: a member household's leave, on the host ───────────────────────


class _LeaveService:
    def __init__(self, hosts: bool):
        self.hosts = hosts
        self.calls: list[tuple] = []

    async def on_remote_member_left(self, space_id, instance_id, user_id):
        self.calls.append((space_id, instance_id, user_id))
        return self.hosts


async def test_instance_left_on_the_host_hands_the_leave_to_the_service(repo, handlers):
    svc = _LeaveService(hosts=True)
    handlers.attach_space_service(svc)
    await handlers._on_instance_left(
        _event(
            FederationEventType.SPACE_INSTANCE_LEFT,
            {"user_id": "u-1"},
            space_id="sp-1",
            from_instance="peer-a",
        )
    )
    # The authenticated sender, never a household named in the payload.
    assert svc.calls == [("sp-1", "peer-a", "u-1")]
    # The service decides about space_instances (last seat only).
    assert repo.instance_removes == []


async def test_instance_left_elsewhere_keeps_the_old_semantics(repo, handlers):
    svc = _LeaveService(hosts=False)
    handlers.attach_space_service(svc)
    await handlers._on_instance_left(
        _event(
            FederationEventType.SPACE_INSTANCE_LEFT,
            {"instance_id": "someone-else"},
            space_id="sp-1",
            from_instance="peer-a",
        )
    )
    assert svc.calls == [("sp-1", "peer-a", None)]
    assert repo.instance_removes == [("sp-1", "peer-a")]


async def test_space_created_never_wipes_the_owners_gfs_list(db):
    """A re-sent ``SPACE_CREATED`` rebuilds the row from scratch; the owner's
    signed GFS list (migration 0093) must survive it — ``save`` never writes
    the column."""
    repo = SqliteSpaceRepo(db)
    handlers = SpaceMembershipInboundHandlers(bus=EventBus(), space_repo=repo)
    payload = {
        "name": "Space 1",
        "owner_username": "owner",
        "identity_public_key": "aa" * 32,
        "space_type": SpaceType.GLOBAL.value,
        "join_mode": JoinMode.INVITE_ONLY.value,
        "config_sequence": 5,
    }
    created = _event(FederationEventType.SPACE_CREATED, payload, space_id="sp-1")
    await handlers._on_created(created)
    signed = sign_gfs_server_list(
        space_id="sp-1", servers=(), issued_at=1, owner_seed=b"\x07" * 32
    )
    await repo.set_gfs_servers("sp-1", signed)
    await handlers._on_created(created)
    got = await repo.get("sp-1")
    assert got is not None and got.gfs_servers == signed


async def test_an_admin_household_cannot_lift_the_owners_ban(bus, repo):
    """P7b finding 5: an unban from a seed holder that is not the owner is
    refused for a ban the owner made; the owner's own unban lands, and an
    admin's ban is lifted by an admin."""
    space = _host_space(owner_instance_id="self-owner")
    repo.spaces["sp-1"] = replace_features(space, delegated_admin_authority=True)
    repo.banners = {("sp-1", "u-x"): "u-owner", ("sp-1", "peer-bad"): "u-admin"}
    h = SpaceMembershipInboundHandlers(
        bus=bus,
        space_repo=repo,
        seat_repo=_AdminSeats({"admin-house"}),
        users=_FakeUsers({"u-owner"}),
    )
    h.attach_to(_FakeFederationService("self-owner"))
    await h._on_unbanned(_ban(banned=False, user_id="u-x", from_instance="admin-house"))
    assert repo.unbans == []
    await h._on_unbanned(
        _ban(banned=False, instance_id="peer-bad", from_instance="admin-house")
    )
    assert repo.household_unbans == [("sp-1", "peer-bad")]
    await h._on_unbanned(_ban(banned=False, user_id="u-x", from_instance="self-owner"))
    assert repo.unbans == [("sp-1", "u-x")]


async def test_a_household_ban_stores_its_banner(repo, handlers):
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_banned(_ban(instance_id="peer-bad", banned_by="u-admin"))
    assert repo.household_banners == [("sp-1", "peer-bad", "u-admin")]
