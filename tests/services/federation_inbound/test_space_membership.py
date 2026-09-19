"""Tests for :class:`SpaceMembershipInboundHandlers` (§13)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

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
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.federation_inbound import SpaceMembershipInboundHandlers


class _FakeRegistry:
    def __init__(self) -> None:
        self.registered = []

    def register(self, t, h):
        self.registered.append((t, h))


class _FakeFederationService:
    def __init__(self) -> None:
        self._event_registry = _FakeRegistry()


class _FakeSpaceRepo:
    def __init__(self) -> None:
        self.saved = []
        self.dissolved = []
        self.purged = []
        self.archived = []
        self.instance_removes = []
        self.bans = []
        self.unbans = []
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

    async def update_age_gate(self, space_id, *, min_age=None):
        self.age_gates.append((space_id, min_age))

    async def get(self, space_id):
        return self.spaces.get(space_id)


def _host_space(space_id="sp-1", owner_instance_id="peer-a"):
    """A locally-stored space row whose owner is ``owner_instance_id`` —
    needed so the §CP.F1 host-authority guard on _on_age_gate can confirm
    the SPACE_AGE_GATE_UPDATED sender is the owning instance."""
    return Space(
        id=space_id,
        name="S",
        owner_instance_id=owner_instance_id,
        owner_username="owner",
        identity_public_key="aa" * 32,
        config_sequence=1,
        features=SpaceFeatures(),
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )


def _event(event_type, payload, *, from_instance="peer-a", space_id=None):
    return FederationEvent(
        msg_id="m",
        event_type=event_type,
        from_instance=from_instance,
        to_instance="self",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
        space_id=space_id,
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
    await handlers._on_banned(
        _event(
            FederationEventType.SPACE_MEMBER_BANNED,
            {"user_id": "u-1", "banned_by": "admin-a", "reason": "spam"},
            space_id="sp-1",
        )
    )
    assert repo.bans == [("sp-1", "u-1", "admin-a", "spam")]
    assert captured[0].user_id == "u-1"


async def test_member_banned_falls_back_to_from_instance_when_no_banned_by(
    repo,
    handlers,
):
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_banned(
        _event(
            FederationEventType.SPACE_MEMBER_BANNED,
            {"user_id": "u-1"},
            space_id="sp-1",
        )
    )
    assert repo.bans == [("sp-1", "u-1", "peer-a", None)]


async def test_member_unbanned_removes_ban(repo, handlers):
    repo.spaces["sp-1"] = _host_space(owner_instance_id="peer-a")
    await handlers._on_unbanned(
        _event(
            FederationEventType.SPACE_MEMBER_UNBANNED,
            {"user_id": "u-1"},
            space_id="sp-1",
        )
    )
    assert repo.unbans == [("sp-1", "u-1")]


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
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    import logging

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
    """A SPACE_MEMBER_BANNED without ``user_id`` is malformed — drop."""
    captured: list[RemoteSpaceMemberBanned] = []
    bus = handlers._bus
    bus.subscribe(RemoteSpaceMemberBanned, captured.append)
    await handlers._on_banned(
        _event(
            FederationEventType.SPACE_MEMBER_BANNED,
            {"reason": "x"},
            space_id="sp-1",
        )
    )
    assert repo.bans == []
    assert captured == []


async def test_member_unbanned_missing_user_id_is_noop(repo, handlers):
    await handlers._on_unbanned(
        _event(
            FederationEventType.SPACE_MEMBER_UNBANNED,
            {},
            space_id="sp-1",
        )
    )
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

    async def send_event(self, *, to_instance_id, event_type, payload):
        self.sent.append((to_instance_id, event_type, payload))


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
    row. With no authority check, any peer that could reach us — a
    read-only Follower household seated over the GFS relay included —
    could ban the space's own owner out of it."""
    repo.spaces["sp-1"] = _host_space(owner_instance_id="the-real-host")
    await handlers._on_banned(
        _event(
            FederationEventType.SPACE_MEMBER_BANNED,
            {"user_id": "u-owner"},
            space_id="sp-1",
            from_instance="peer-follower",
        )
    )
    assert repo.bans == []


async def test_member_unbanned_from_a_non_host_is_refused(repo, handlers):
    """The mirror image: clearing a real ban is how a banned household
    walks back in."""
    repo.spaces["sp-1"] = _host_space(owner_instance_id="the-real-host")
    await handlers._on_unbanned(
        _event(
            FederationEventType.SPACE_MEMBER_UNBANNED,
            {"user_id": "u-1"},
            space_id="sp-1",
            from_instance="peer-follower",
        )
    )
    assert repo.unbans == []


async def test_member_banned_for_an_unknown_space_is_refused(repo, handlers):
    """No local row means no owner to check against. A ban we cannot
    attribute is a ban we do not apply — unlike the §24.11 writer gate's
    roster-convergence leniency, there is no legitimate sender here that
    arrives before the space does."""
    await handlers._on_banned(
        _event(
            FederationEventType.SPACE_MEMBER_BANNED,
            {"user_id": "u-1"},
            space_id="sp-unknown",
        )
    )
    assert repo.bans == []


async def test_member_unbanned_for_an_unknown_space_is_refused(repo, handlers):
    await handlers._on_unbanned(
        _event(
            FederationEventType.SPACE_MEMBER_UNBANNED,
            {"user_id": "u-1"},
            space_id="sp-unknown",
        )
    )
    assert repo.unbans == []
