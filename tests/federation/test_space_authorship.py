"""§24.11 — who may write space content on behalf of whom.

Unit tests for :class:`SpaceAuthorship` over in-memory stubs. The
protocol-level proof (real app, real registry, table snapshots) lives in
``tests/protocol/test_space_content_authorship.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.space import (
    ContentAction,
    ModerationStatus,
    SpaceFeatureAccess,
    SpaceFeatures,
    SpaceModerationItem,
)
from socialhome.domain.user import SYSTEM_AUTHOR
from socialhome.federation.space_authorship import SpaceAuthorship
from socialhome.repositories.space_remote_member_repo import SpaceRemoteMember

SPACE = "sp-1"
HOST = "inst-host"
AUTHOR_HOUSE = "inst-author"
OTHER_HOUSE = "inst-other"
ADMIN_HOUSE = "inst-admin"


@dataclass
class _Space:
    owner_instance_id: str


class _Spaces:
    def __init__(self, spaces: dict[str, _Space], banned=()) -> None:
        self._spaces = spaces
        self.banned = set(banned)

    async def get(self, space_id: str):
        return self._spaces.get(space_id)

    async def is_banned(self, space_id: str, user_id: str) -> bool:
        return (space_id, user_id) in self.banned


class _Seats:
    def __init__(self, rows: list[SpaceRemoteMember]) -> None:
        self.rows = rows

    async def get(self, space_id, instance_id, user_id):
        for r in self.rows:
            if (r.space_id, r.instance_id, r.user_id) == (
                space_id,
                instance_id,
                user_id,
            ) and not r.tombstoned:
                return r
        return None

    async def list_for_instance(
        self, space_id, instance_id, *, include_tombstoned=True
    ):
        return [
            r
            for r in self.rows
            if r.space_id == space_id
            and r.instance_id == instance_id
            and (include_tombstoned or not r.tombstoned)
        ]

    async def get_including_tombstones(self, space_id, instance_id, user_id):
        # Like the real repo: keyed on (space, user); the instance is ignored.
        for r in self.rows:
            if (r.space_id, r.user_id) == (space_id, user_id):
                return r
        return None


class _Users:
    def __init__(self, local: set[str]) -> None:
        self._local = local

    async def get_by_user_id(self, user_id: str):
        return object() if user_id in self._local else None


def _seat(instance_id, user_id, *, role="member", tombstoned=False, space=SPACE):
    return SpaceRemoteMember(
        space_id=space,
        instance_id=instance_id,
        user_id=user_id,
        role=role,
        tombstoned=tombstoned,
    )


@pytest.fixture
def authorship() -> SpaceAuthorship:
    return SpaceAuthorship(
        space_repo=_Spaces({SPACE: _Space(owner_instance_id=HOST)}),
        remote_member_repo=_Seats(
            [
                _seat(AUTHOR_HOUSE, "u-author"),
                _seat(OTHER_HOUSE, "u-other"),
                _seat(ADMIN_HOUSE, "u-admin", role="admin"),
                _seat(AUTHOR_HOUSE, "u-gone", tombstoned=True),
                _seat(HOST, "u-host"),
            ]
        ),
        user_repo=_Users({"u-local"}),
    )


def _ev(sender: str) -> FederationEvent:
    return FederationEvent(
        msg_id="m",
        event_type=FederationEventType.SPACE_POST_UPDATED,
        from_instance=sender,
        to_instance="us",
        timestamp="2026-01-01T00:00:00+00:00",
        payload={},
        space_id=SPACE,
    )


# ── acts_for: the strict "this is my own member" rule ────────────────


async def test_the_authors_own_household_acts_for_the_author(authorship) -> None:
    assert await authorship.acts_for(_ev(AUTHOR_HOUSE), SPACE, "u-author")


async def test_another_member_household_does_not_act_for_the_author(
    authorship,
) -> None:
    assert not await authorship.acts_for(_ev(OTHER_HOUSE), SPACE, "u-author")


async def test_a_removed_seat_acts_for_nobody(authorship) -> None:
    assert not await authorship.acts_for(_ev(AUTHOR_HOUSE), SPACE, "u-gone")


async def test_a_seat_in_another_space_does_not_count(authorship) -> None:
    assert not await authorship.acts_for(_ev(AUTHOR_HOUSE), "sp-other", "u-author")


async def test_nobody_acts_for_a_blank_user_or_from_a_blank_sender(
    authorship,
) -> None:
    assert not await authorship.acts_for(_ev(AUTHOR_HOUSE), SPACE, "")
    assert not await authorship.acts_for(_ev(""), SPACE, "u-author")


async def test_the_host_does_not_act_for_somebody_elses_member_strictly(
    authorship,
) -> None:
    """The strict rule (votes, RSVPs, bids…) has no host exception."""
    assert not await authorship.acts_for(_ev(HOST), SPACE, "u-author")


# ── may_author: creates ──────────────────────────────────────────────


async def test_a_create_by_the_authors_household_is_allowed(authorship) -> None:
    assert await authorship.may_author(_ev(AUTHOR_HOUSE), SPACE, "u-author")


async def test_a_create_naming_another_households_member_is_refused(
    authorship,
) -> None:
    assert not await authorship.may_author(_ev(OTHER_HOUSE), SPACE, "u-author")


async def test_an_admin_household_cannot_author_for_others(authorship) -> None:
    assert not await authorship.may_author(_ev(ADMIN_HOUSE), SPACE, "u-author")


async def test_the_host_may_relay_a_remote_members_create(authorship) -> None:
    """§25.6 resume replay re-sends every member's rows from the host."""
    assert await authorship.may_author(_ev(HOST), SPACE, "u-author")


async def test_the_host_may_not_author_as_one_of_our_local_users(
    authorship,
) -> None:
    assert not await authorship.may_author(_ev(HOST), SPACE, "u-local")


async def test_nobody_authors_as_a_local_user(authorship) -> None:
    for sender in (AUTHOR_HOUSE, OTHER_HOUSE, ADMIN_HOUSE, HOST):
        assert not await authorship.may_author(_ev(sender), SPACE, "u-local")


async def test_an_unknown_space_has_no_host(authorship) -> None:
    assert not await authorship.may_author(_ev(HOST), "sp-unknown", "u-author")


async def test_system_integration_posts_are_attributed_to_no_member(
    authorship,
) -> None:
    """Bot-bridge posts carry the shared SYSTEM_AUTHOR — no member is
    impersonated, so any writer household may create one."""
    assert await authorship.may_author(_ev(OTHER_HOUSE), SPACE, SYSTEM_AUTHOR)


# ── may_mutate: edits / deletes of an owned row ──────────────────────


async def test_the_authors_household_may_mutate(authorship) -> None:
    assert await authorship.may_mutate(_ev(AUTHOR_HOUSE), SPACE, "u-author")


async def test_another_member_household_may_not_mutate(authorship) -> None:
    assert not await authorship.may_mutate(_ev(OTHER_HOUSE), SPACE, "u-author")


async def test_a_household_with_a_live_admin_seat_may_moderate(authorship) -> None:
    assert await authorship.may_mutate(_ev(ADMIN_HOUSE), SPACE, "u-author")
    assert await authorship.may_mutate(_ev(ADMIN_HOUSE), SPACE, "u-local")


async def test_the_host_may_moderate(authorship) -> None:
    assert await authorship.may_mutate(_ev(HOST), SPACE, "u-author")
    assert await authorship.may_mutate(_ev(HOST), SPACE, "u-local")


async def test_a_remote_household_may_not_mutate_a_local_users_row(
    authorship,
) -> None:
    assert not await authorship.may_mutate(_ev(AUTHOR_HOUSE), SPACE, "u-local")
    assert not await authorship.may_mutate(_ev(OTHER_HOUSE), SPACE, "u-local")


async def test_a_system_row_is_moderator_only(authorship) -> None:
    assert not await authorship.may_mutate(_ev(OTHER_HOUSE), SPACE, SYSTEM_AUTHOR)
    assert await authorship.may_mutate(_ev(ADMIN_HOUSE), SPACE, SYSTEM_AUTHOR)


async def test_a_tombstoned_admin_seat_is_no_moderator() -> None:
    a = SpaceAuthorship(
        space_repo=_Spaces({SPACE: _Space(owner_instance_id=HOST)}),
        remote_member_repo=_Seats(
            [_seat(ADMIN_HOUSE, "u-admin", role="admin", tombstoned=True)]
        ),
        user_repo=_Users(set()),
    )
    assert not await a.is_admin_household(_ev(ADMIN_HOUSE), SPACE)


# ── logging ──────────────────────────────────────────────────────────


async def test_refusal_is_logged_at_warning(
    authorship, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        authorship.log_refusal(
            _ev(OTHER_HOUSE), space_id=SPACE, what="post", row_id="p1", user_id="u"
        )
    assert "not a member of the sending household" in caplog.text
    assert "p1" in caplog.text


# ── writes_here: collaborative families ──────────────────────────────


async def test_any_writer_household_writes_collaboratively(authorship) -> None:
    for sender in (AUTHOR_HOUSE, OTHER_HOUSE, ADMIN_HOUSE, HOST):
        assert await authorship.writes_here(_ev(sender), SPACE)


async def test_a_seatless_household_does_not_write_collaboratively(
    authorship,
) -> None:
    assert not await authorship.writes_here(_ev("inst-stranger"), SPACE)
    assert not await authorship.writes_here(_ev(""), SPACE)


async def test_a_follower_or_removed_household_does_not_write_collaboratively() -> None:
    a = SpaceAuthorship(
        space_repo=_Spaces({SPACE: _Space(owner_instance_id=HOST)}),
        remote_member_repo=_Seats(
            [
                _seat("inst-follower", "u-f", role="subscriber"),
                _seat("inst-kicked", "u-k", tombstoned=True),
            ]
        ),
        user_repo=_Users(set()),
    )
    assert not await a.writes_here(_ev("inst-follower"), SPACE)
    assert not await a.writes_here(_ev("inst-kicked"), SPACE)


# ── read-only seats, the host relay, the seat-wait hold ───────────────


def _auth(rows, *, allow_comment=False, pending=None, local=frozenset()):
    return SpaceAuthorship(
        space_repo=_Spaces(
            {
                SPACE: SimpleNamespace(
                    owner_instance_id=HOST,
                    features=SimpleNamespace(allow_subscriber_comment=allow_comment),
                )
            }
        ),
        remote_member_repo=_Seats(rows),
        user_repo=_Users(set(local)),
        pending=pending,
    )


async def test_a_read_only_seat_authors_nothing() -> None:
    a = _auth([_seat(AUTHOR_HOUSE, "u-f", role="subscriber")])
    assert not await a.acts_for(_ev(AUTHOR_HOUSE), SPACE, "u-f")
    assert not await a.may_author(_ev(AUTHOR_HOUSE), SPACE, "u-f")


async def test_a_read_only_seat_may_still_change_its_own_row() -> None:
    a = _auth([_seat(AUTHOR_HOUSE, "u-f", role="subscriber")])
    assert await a.may_mutate(_ev(AUTHOR_HOUSE), SPACE, "u-f")


async def test_a_follower_comment_needs_the_spaces_opt_in() -> None:
    rows = [_seat(AUTHOR_HOUSE, "u-f", role="subscriber")]
    off = _auth(rows)
    on = _auth(rows, allow_comment=True)
    ev = _ev(AUTHOR_HOUSE)
    assert not await off.may_author(ev, SPACE, "u-f", subscriber_comment=True)
    assert await on.may_author(ev, SPACE, "u-f", subscriber_comment=True)
    assert not await on.may_author(ev, SPACE, "u-f")
    assert not await on.may_author(
        _ev(OTHER_HOUSE), SPACE, "u-f", subscriber_comment=True
    )


async def test_the_host_relays_only_users_the_space_has_a_record_of() -> None:
    a = _auth([_seat(AUTHOR_HOUSE, "u-gone", tombstoned=True)])
    assert await a.may_author(_ev(HOST), SPACE, "u-gone")
    assert not await a.may_author(_ev(HOST), SPACE, "u-never")


async def test_an_unknown_user_is_held_not_refused() -> None:
    from socialhome.federation.pending_seat_buffer import PendingSeatBuffer

    buf = PendingSeatBuffer()
    a = _auth([_seat(AUTHOR_HOUSE, "u-a")], pending=buf)
    ev = _ev(OTHER_HOUSE)
    await a.hold_or_refuse(ev, space_id=SPACE, what="post", row_id="p", user_id="u-new")
    assert len(buf) == 1
    # A known user (seated elsewhere), a local user and the bot identity
    # are refusals, never held.
    b = _auth([_seat(AUTHOR_HOUSE, "u-a")], pending=buf, local={"u-local"})
    for user in ("u-a", "u-local", "system-integration", ""):
        await b.hold_or_refuse(
            ev, space_id=SPACE, what="post", row_id="p", user_id=user
        )
    assert len(buf) == 1


async def test_without_a_buffer_an_unknown_user_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = _auth([])
    with caplog.at_level(logging.WARNING):
        await a.hold_or_refuse(
            _ev(OTHER_HOUSE), space_id=SPACE, what="post", row_id="p", user_id="u-new"
        )
    assert "refusing the write" in caplog.text


# ── admin_as: moderator-only content (zones' rule, per user) ─────


async def test_an_admin_seated_on_the_sender_moderates_as_themself(
    authorship,
) -> None:
    assert await authorship.admin_as(_ev(ADMIN_HOUSE), SPACE, "u-admin")


async def test_a_plain_member_does_not_moderate(authorship) -> None:
    assert not await authorship.admin_as(_ev(AUTHOR_HOUSE), SPACE, "u-author")


async def test_an_admin_household_cannot_moderate_as_its_plain_member() -> None:
    """The household holds an admin seat, but the named editor is not it."""
    a = SpaceAuthorship(
        space_repo=_Spaces({SPACE: _Space(owner_instance_id=HOST)}),
        remote_member_repo=_Seats(
            [_seat(ADMIN_HOUSE, "u-admin", role="admin"), _seat(ADMIN_HOUSE, "u-kid")]
        ),
        user_repo=_Users(set()),
    )
    assert not await a.admin_as(_ev(ADMIN_HOUSE), SPACE, "u-kid")


async def test_nobody_moderates_as_somebody_elses_admin(authorship) -> None:
    for sender in (AUTHOR_HOUSE, OTHER_HOUSE):
        assert not await authorship.admin_as(_ev(sender), SPACE, "u-admin")


async def test_the_host_records_its_writers_and_relays_live_admins() -> None:
    """The host (the roster authority) records any user holding a live
    writer seat on it — the owner is mirrored as a plain member — and relays
    a live admin of another household; never a follower, a removed seat, a
    remote plain member or one of our local users."""
    a = SpaceAuthorship(
        space_repo=_Spaces({SPACE: _Space(owner_instance_id=HOST)}),
        remote_member_repo=_Seats(
            [
                _seat(HOST, "u-owner"),  # the owner, mirrored as a member
                _seat(HOST, "u-host-admin", role="admin"),
                _seat(HOST, "u-host-sub", role="subscriber"),
                _seat(HOST, "u-host-gone", tombstoned=True),
                _seat(ADMIN_HOUSE, "u-admin", role="admin"),
                _seat(AUTHOR_HOUSE, "u-author"),
                _seat(AUTHOR_HOUSE, "u-sub", role="subscriber"),
                _seat(ADMIN_HOUSE, "u-ex", role="admin", tombstoned=True),
            ]
        ),
        user_repo=_Users({"u-local"}),
    )
    for user in ("u-owner", "u-host-admin", "u-admin"):
        assert await a.admin_as(_ev(HOST), SPACE, user), user
    for user in (
        "u-host-sub",
        "u-host-gone",
        "u-author",
        "u-sub",
        "u-ex",
        "u-local",
        "u-nobody",
    ):
        assert not await a.admin_as(_ev(HOST), SPACE, user), user
    # A non-host household gets no such latitude for its plain members.
    assert not await a.admin_as(_ev(AUTHOR_HOUSE), SPACE, "u-author")


async def test_blank_system_removed_and_banned_editors_never_moderate() -> None:
    a = SpaceAuthorship(
        space_repo=_Spaces(
            {SPACE: _Space(owner_instance_id=HOST)}, banned={(SPACE, "u-bad")}
        ),
        remote_member_repo=_Seats(
            [
                _seat(ADMIN_HOUSE, "u-bad", role="admin"),
                _seat(ADMIN_HOUSE, "u-old", role="admin", tombstoned=True),
            ]
        ),
        user_repo=_Users(set()),
    )
    for user in ("", SYSTEM_AUTHOR, "u-old", "u-bad"):
        assert not await a.admin_as(_ev(ADMIN_HOUSE), SPACE, user)
    assert not await a.admin_as(_ev(""), SPACE, "u-bad")


# ── v_41 moderator seats: content authority, never settings authority ──

MOD_HOUSE = "inst-mod"


def _mod_authorship(*extra: SpaceRemoteMember) -> SpaceAuthorship:
    return SpaceAuthorship(
        space_repo=_Spaces({SPACE: _Space(owner_instance_id=HOST)}),
        remote_member_repo=_Seats(
            [
                _seat(MOD_HOUSE, "u-mod", role="moderator"),
                _seat(MOD_HOUSE, "u-kid"),
                _seat(ADMIN_HOUSE, "u-admin", role="admin"),
                _seat(AUTHOR_HOUSE, "u-author"),
                _seat(HOST, "u-host-mod", role="moderator"),
                *extra,
            ]
        ),
        user_repo=_Users({"u-local"}),
    )


async def test_a_moderator_household_has_content_authority_but_is_no_admin_household():
    a = _mod_authorship()
    assert await a.has_content_authority(_ev(MOD_HOUSE), SPACE)
    assert not await a.is_admin_household(_ev(MOD_HOUSE), SPACE)


async def test_content_authority_holders():
    a = _mod_authorship()
    assert await a.has_content_authority(_ev(HOST), SPACE)
    assert await a.has_content_authority(_ev(ADMIN_HOUSE), SPACE)
    assert not await a.has_content_authority(_ev(AUTHOR_HOUSE), SPACE)
    assert not await a.has_content_authority(_ev(""), SPACE)


async def test_a_tombstoned_moderator_seat_has_no_content_authority():
    a = SpaceAuthorship(
        space_repo=_Spaces({SPACE: _Space(owner_instance_id=HOST)}),
        remote_member_repo=_Seats(
            [_seat(MOD_HOUSE, "u-mod", role="moderator", tombstoned=True)]
        ),
        user_repo=_Users(set()),
    )
    assert not await a.has_content_authority(_ev(MOD_HOUSE), SPACE)


async def test_a_moderator_household_may_mutate_others_rows():
    """Moderation edits / deletes ride ``may_mutate`` — content authority."""
    a = _mod_authorship()
    assert await a.may_mutate(_ev(MOD_HOUSE), SPACE, "u-author")
    assert await a.may_mutate(_ev(MOD_HOUSE), SPACE, SYSTEM_AUTHOR)
    assert not await a.may_mutate(_ev(AUTHOR_HOUSE), SPACE, "u-kid")


async def test_a_moderator_writes_and_acts_for_themself():
    a = _mod_authorship()
    assert await a.acts_for(_ev(MOD_HOUSE), SPACE, "u-mod")
    assert await a.may_author(_ev(MOD_HOUSE), SPACE, "u-mod")
    assert await a.writes_here(_ev(MOD_HOUSE), SPACE)


async def test_a_moderator_is_never_admin_as():
    """Zones / timetables stay settings authority: a moderator seat, on its
    own household or relayed by the host, never passes ``admin_as``."""
    a = _mod_authorship()
    assert not await a.admin_as(_ev(MOD_HOUSE), SPACE, "u-mod")
    assert not await a.admin_as(_ev(HOST), SPACE, "u-mod")


async def test_moderates_as_admits_admin_and_moderator_seats():
    a = _mod_authorship()
    assert await a.moderates_as(_ev(MOD_HOUSE), SPACE, "u-mod")
    assert await a.moderates_as(_ev(ADMIN_HOUSE), SPACE, "u-admin")
    # The host relays a remote moderator, and records its own seats.
    assert await a.moderates_as(_ev(HOST), SPACE, "u-mod")
    assert await a.moderates_as(_ev(HOST), SPACE, "u-host-mod")
    # A moderator household's plain member, someone else's moderator, a
    # remote plain member relayed by the host: no.
    assert not await a.moderates_as(_ev(MOD_HOUSE), SPACE, "u-kid")
    assert not await a.moderates_as(_ev(AUTHOR_HOUSE), SPACE, "u-mod")
    assert not await a.moderates_as(_ev(HOST), SPACE, "u-author")
    assert not await a.moderates_as(_ev(MOD_HOUSE), SPACE, "u-local")
    assert not await a.moderates_as(_ev(MOD_HOUSE), SPACE, SYSTEM_AUTHOR)


async def test_moderates_as_refuses_a_banned_moderator():
    a = SpaceAuthorship(
        space_repo=_Spaces(
            {SPACE: _Space(owner_instance_id=HOST)}, banned={(SPACE, "u-mod")}
        ),
        remote_member_repo=_Seats([_seat(MOD_HOUSE, "u-mod", role="moderator")]),
        user_repo=_Users(set()),
    )
    assert not await a.moderates_as(_ev(MOD_HOUSE), SPACE, "u-mod")


async def test_settings_tier_may_mutate_refuses_a_moderator():
    """``may_mutate(settings=True)`` — whole-album edits: the owner's
    household or settings authority, never a moderator seat."""
    a = _mod_authorship()
    assert not await a.may_mutate(_ev(MOD_HOUSE), SPACE, "u-author", settings=True)
    assert await a.may_mutate(_ev(ADMIN_HOUSE), SPACE, "u-author", settings=True)
    assert await a.may_mutate(_ev(HOST), SPACE, "u-author", settings=True)
    assert await a.may_mutate(_ev(AUTHOR_HOUSE), SPACE, "u-author", settings=True)


# ─── access_admits: the §4.3 access levels on the receiver (v_42) ──────


@dataclass
class _FeaturedSpace:
    owner_instance_id: str
    features: object


MOD_HOUSE = "inst-mod"


class _Instances:
    """``remote_instances`` stand-in: instance id → advertised proto_version."""

    def __init__(self, versions: dict[str, int]) -> None:
        self.versions = versions

    async def get_instance(self, instance_id: str, **_kw):
        v = self.versions.get(instance_id)
        return None if v is None else SimpleNamespace(proto_version=v)


def _access_authorship(
    versions: dict[str, int] | None = None, **levels
) -> SpaceAuthorship:
    from socialhome.domain.space import SpaceFeatureAccess, SpaceFeatures

    features = SpaceFeatures(
        **{f"{k}_access": SpaceFeatureAccess(v) for k, v in levels.items()}
    )
    return SpaceAuthorship(
        space_repo=_Spaces({SPACE: _FeaturedSpace(HOST, features)}),
        remote_member_repo=_Seats(
            [
                _seat(AUTHOR_HOUSE, "u-author"),
                _seat(ADMIN_HOUSE, "u-admin", role="admin"),
                _seat(ADMIN_HOUSE, "u-plain"),
                _seat(MOD_HOUSE, "u-mod", role="moderator"),
                _seat(AUTHOR_HOUSE, "u-sub", role="subscriber"),
                _seat(HOST, "u-owner"),
            ]
        ),
        user_repo=_Users({"u-local"}),
        federation_repo=_Instances(
            versions
            if versions is not None
            else {h: 42 for h in (HOST, AUTHOR_HOUSE, ADMIN_HOUSE, MOD_HOUSE)}
        ),
    )


async def _admits(auth, sender, actor, *, feature="tasks", action=None):
    from socialhome.domain.space import ContentAction

    return await auth.access_admits(
        _ev(sender),
        SPACE,
        feature,
        action or ContentAction.EDIT,
        actor=actor,
        row_owner="u-author",
    )


async def test_open_admits_anyone_the_authorship_rules_let_through():
    auth = _access_authorship()
    assert await _admits(auth, AUTHOR_HOUSE, "u-author")
    assert await _admits(auth, AUTHOR_HOUSE, None)


async def test_admin_only_admits_only_admins_seated_on_the_sender():
    auth = _access_authorship(tasks="admin_only")
    assert await _admits(auth, ADMIN_HOUSE, "u-admin")
    # The host's own people: the owner is mirrored as a member seat.
    assert await _admits(auth, HOST, "u-owner")
    for sender, actor in (
        (AUTHOR_HOUSE, "u-author"),  # a member
        (MOD_HOUSE, "u-mod"),  # a moderator — content authority is not enough
        (AUTHOR_HOUSE, "u-sub"),  # a follower
        (ADMIN_HOUSE, "u-plain"),  # an admin household's plain member
        ("inst-stranger", "u-x"),  # no seat at all
    ):
        assert not await _admits(auth, sender, actor), (sender, actor)


async def test_a_forged_actor_naming_another_households_admin_is_refused():
    auth = _access_authorship(pages="admin_only")
    assert not await _admits(auth, AUTHOR_HOUSE, "u-admin", feature="pages")


async def test_a_missing_actor_falls_back_to_the_sending_household():
    """An older (v_41) sender names no actor: settle for its seats."""
    auth = _access_authorship(
        {h: 41 for h in (HOST, AUTHOR_HOUSE, ADMIN_HOUSE, MOD_HOUSE)},
        stickies="admin_only",
    )
    assert await _admits(auth, ADMIN_HOUSE, None, feature="stickies")
    assert await _admits(auth, HOST, "", feature="stickies")
    assert not await _admits(auth, AUTHOR_HOUSE, None, feature="stickies")
    assert not await _admits(auth, MOD_HOUSE, None, feature="stickies")


async def test_a_v42_sender_must_name_the_actor_of_an_edit_or_delete():
    """Every v_42 producer names its actor: one that omits it on an
    ADMIN_ONLY edit / delete is refused — even from an admin household,
    which could otherwise pass off its plain member's change."""
    from socialhome.domain.space import ContentAction

    auth = _access_authorship(pages="admin_only")
    for action in (ContentAction.EDIT, ContentAction.DELETE):
        assert not await _admits(
            auth, ADMIN_HOUSE, None, feature="pages", action=action
        )
        assert not await _admits(auth, HOST, None, feature="pages", action=action)
        assert await _admits(
            auth, ADMIN_HOUSE, "u-admin", feature="pages", action=action
        )
    # An unknown (never advertised) peer reads as old: the household rule.
    old = _access_authorship({}, pages="admin_only")
    assert await _admits(old, ADMIN_HOUSE, None, feature="pages")


async def test_a_create_is_judged_by_its_author_not_a_named_actor():
    """A create's actor IS its author: an admin household cannot launder
    its plain member's page by naming its admin, nor by naming nobody."""
    from socialhome.domain.space import ContentAction

    auth = _access_authorship(pages="admin_only")

    async def create(sender, *, author, actor):
        return await auth.access_admits(
            _ev(sender),
            SPACE,
            "pages",
            ContentAction.CREATE,
            actor=actor,
            row_owner=author,
        )

    assert not await create(ADMIN_HOUSE, author="u-plain", actor="u-admin")
    assert not await create(ADMIN_HOUSE, author="u-plain", actor=None)
    assert await create(ADMIN_HOUSE, author="u-admin", actor="u-admin")
    assert await create(ADMIN_HOUSE, author="u-admin", actor=None)
    # v_41 senders get the same author rule — the author is on every create.
    old = _access_authorship({h: 41 for h in (ADMIN_HOUSE,)}, pages="admin_only")
    assert not await old.access_admits(
        _ev(ADMIN_HOUSE),
        SPACE,
        "pages",
        ContentAction.CREATE,
        actor=None,
        row_owner="u-plain",
    )


async def test_only_the_host_may_release_someone_elses_post():
    """A moderation release is the one create whose actor (the approver)
    is not its author — and the queue lives on the host."""
    from socialhome.domain.space import ContentAction

    auth = _access_authorship(posts="admin_only")

    async def release(sender, actor):
        return await auth.access_admits(
            _ev(sender),
            SPACE,
            "posts",
            ContentAction.CREATE,
            actor=actor,
            row_owner="u-author",
            release_ok=True,
        )

    assert await release(HOST, "u-owner")
    assert not await release(ADMIN_HOUSE, "u-admin")


async def test_the_host_relays_a_remote_members_existing_row():
    """Resume replay: the host re-sends a remote member's row (made while
    the feature was open); like ``may_author``, that relay is admitted."""
    from socialhome.domain.space import ContentAction

    auth = _access_authorship(tasks="admin_only")
    assert await auth.access_admits(
        _ev(HOST),
        SPACE,
        "tasks",
        ContentAction.CREATE,
        actor=None,
        row_owner="u-author",
    )
    assert not await auth.access_admits(
        _ev(ADMIN_HOUSE),
        SPACE,
        "tasks",
        ContentAction.CREATE,
        actor=None,
        row_owner="u-author",
    )


async def test_the_bot_identity_counts_as_its_household():
    """A bot posts as the shared system author; bots are configured by
    admins, so an admin household's bot post passes."""
    from socialhome.domain.space import ContentAction

    auth = _access_authorship(posts="admin_only")

    async def bot(sender, owner):
        return await auth.access_admits(
            _ev(sender),
            SPACE,
            "posts",
            ContentAction.EDIT,
            actor=SYSTEM_AUTHOR,
            row_owner=owner,
        )

    assert await bot(ADMIN_HOUSE, SYSTEM_AUTHOR)
    assert not await bot(AUTHOR_HOUSE, SYSTEM_AUTHOR)
    # As the actor on anybody else's row, the bot identity is nobody.
    assert not await bot(ADMIN_HOUSE, "u-author")
    assert not await bot(HOST, "u-author")


async def test_moderated_admits_for_now_but_still_binds_the_actor():
    auth = _access_authorship(calendar="moderated")
    assert await _admits(auth, AUTHOR_HOUSE, "u-author", feature="calendar")
    assert not await _admits(auth, AUTHOR_HOUSE, "u-admin", feature="calendar")


async def test_an_unknown_space_admits_nothing_restricted():
    auth = _access_authorship(tasks="admin_only")
    from socialhome.domain.space import ContentAction

    assert not await auth.access_admits(
        _ev(ADMIN_HOUSE),
        "sp-unknown",
        "tasks",
        ContentAction.CREATE,
        actor="u-admin",
        row_owner="",
    )


async def test_a_refusal_is_a_warning(caplog):
    auth = _access_authorship(tasks="admin_only")
    with caplog.at_level(logging.WARNING):
        assert not await _admits(auth, AUTHOR_HOUSE, "u-author")
    assert any("admin_only" in r.getMessage() for r in caplog.records)


async def test_moderated_admits_a_followers_own_delete():
    """A demoted author's household may still delete their own post under
    MODERATED (like ``may_mutate(any_role=True)``); not someone else's."""
    from socialhome.domain.space import ContentAction

    auth = _access_authorship(posts="moderated")
    ev = _ev(AUTHOR_HOUSE)
    assert await auth.access_admits(
        ev, SPACE, "posts", ContentAction.DELETE, actor="u-sub", row_owner="u-sub"
    )
    assert not await auth.access_admits(
        ev, SPACE, "posts", ContentAction.DELETE, actor="u-sub", row_owner="u-author"
    )
    assert not await auth.access_admits(
        ev, SPACE, "posts", ContentAction.CREATE, actor="u-sub", row_owner="u-sub"
    )


async def test_an_actor_whose_seat_has_not_arrived_is_held():
    """The roster gossip seating the actor may trail their write: hold it
    (like ``may_author``) instead of dropping it for good."""
    from socialhome.domain.space import ContentAction, SpaceFeatureAccess, SpaceFeatures

    class _Pending:
        def __init__(self):
            self.held = []

        def hold(self, event, *, space_id, user_id):
            self.held.append(user_id)
            return True

    pending = _Pending()
    auth = SpaceAuthorship(
        space_repo=_Spaces(
            {
                SPACE: _FeaturedSpace(
                    HOST, SpaceFeatures(pages_access=SpaceFeatureAccess.ADMIN_ONLY)
                )
            }
        ),
        remote_member_repo=_Seats([_seat(ADMIN_HOUSE, "u-admin", role="admin")]),
        user_repo=_Users(set()),
        pending=pending,
    )
    assert not await auth.access_admits(
        _ev(ADMIN_HOUSE),
        SPACE,
        "pages",
        ContentAction.EDIT,
        actor="u-new",
        row_owner="",
    )
    assert pending.held == ["u-new"]


# ── may_author_approved: content released from the moderation queue (v_43) ──

MOD_HOUSE = "inst-mod"
_APPROVAL = {"item_id": "item-1", "approved_by": "u-mod"}


class _ModSpaces(_Spaces):
    """``_Spaces`` plus local seats, the queue and a features block."""

    def __init__(self, *, level="moderated", members=None, items=None, banned=()):
        super().__init__(
            {
                SPACE: SimpleNamespace(
                    owner_instance_id=HOST,
                    features=SpaceFeatures(tasks_access=SpaceFeatureAccess(level)),
                )
            },
            banned=banned,
        )
        self.members = dict(members or {})
        self.items = dict(items or {})

    async def get_member(self, space_id, user_id):
        role = self.members.get(user_id)
        return SimpleNamespace(role=role) if role else None

    async def get_moderation_item(self, item_id):
        return self.items.get(item_id)


def _queued(**over):
    base = dict(
        id="item-1",
        space_id=SPACE,
        feature="tasks",
        action="create",
        submitted_by="u-author",
        payload={
            "entity": "list",
            "target_id": "list-1",
            "name": "Groceries",
        },
        current_snapshot=None,
        submitted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2026, 1, 8, tzinfo=timezone.utc),
        status=ModerationStatus.PENDING,
    )
    base.update(over)
    return SpaceModerationItem(**base)


def _released(sender, *, author="u-author", name="Groceries", approval=_APPROVAL):
    payload = {"id": "list-1", "name": name, "created_by": author}
    if approval is not None:
        payload["moderation"] = dict(approval)
    return FederationEvent(
        msg_id="m",
        event_type=FederationEventType.SPACE_TASK_LIST_CREATED,
        from_instance=sender,
        to_instance="us",
        timestamp="2026-01-01T00:00:00+00:00",
        payload=payload,
        space_id=SPACE,
    )


def _mod_auth(*, rows=None, local=frozenset(), **spaces):
    return SpaceAuthorship(
        space_repo=_ModSpaces(**spaces),
        remote_member_repo=_Seats(
            rows
            if rows is not None
            else [
                _seat(AUTHOR_HOUSE, "u-author"),
                _seat(OTHER_HOUSE, "u-other"),
                _seat(MOD_HOUSE, "u-mod", role="moderator"),
                _seat(ADMIN_HOUSE, "u-admin", role="admin"),
                _seat(HOST, "u-host"),
            ]
        ),
        user_repo=_Users(set(local)),
    )


async def test_the_host_releases_a_remote_members_item() -> None:
    a = _mod_auth()
    assert await a.may_author_approved(_released(HOST), SPACE, "u-author")
    # ``may_author`` takes the release in place of the author's own seat.
    assert await a.may_author(_released(HOST), SPACE, "u-author")


async def test_a_release_from_a_moderator_household_is_refused() -> None:
    """I2: only the host applies a queue item — a household with a real
    moderator seat cannot publish a release, made up or not."""
    a = _mod_auth()
    assert not await a.may_author_approved(_released(MOD_HOUSE), SPACE, "u-author")
    assert not await a.may_author(_released(MOD_HOUSE), SPACE, "u-author")


async def test_the_host_naming_a_plain_member_as_approver_is_refused() -> None:
    a = _mod_auth()
    ev = _released(HOST, approval={"item_id": "item-1", "approved_by": "u-other"})
    assert not await a.may_author_approved(ev, SPACE, "u-author")


async def test_a_release_from_a_member_household_is_refused() -> None:
    a = _mod_auth()
    ev = _released(
        OTHER_HOUSE, approval={"item_id": "item-1", "approved_by": "u-other"}
    )
    assert not await a.may_author_approved(ev, SPACE, "u-author")
    assert not await a.may_author(ev, SPACE, "u-author")


async def test_a_release_naming_another_households_moderator_is_refused() -> None:
    a = _mod_auth()
    assert not await a.may_author_approved(_released(ADMIN_HOUSE), SPACE, "u-author")


async def test_a_release_naming_another_households_admin_is_refused() -> None:
    a = _mod_auth()
    ev = _released(MOD_HOUSE, approval={"item_id": "item-1", "approved_by": "u-admin"})
    assert not await a.may_author_approved(ev, SPACE, "u-author")


async def test_a_demoted_moderators_release_is_refused() -> None:
    a = _mod_auth(
        rows=[_seat(AUTHOR_HOUSE, "u-author"), _seat(MOD_HOUSE, "u-mod")],
    )
    assert not await a.may_author_approved(_released(HOST), SPACE, "u-author")


async def test_a_release_for_somebody_who_cannot_write_is_refused() -> None:
    for rows in (
        [_seat(MOD_HOUSE, "u-mod", role="moderator")],
        [
            _seat(MOD_HOUSE, "u-mod", role="moderator"),
            _seat(AUTHOR_HOUSE, "u-author", tombstoned=True),
        ],
        [
            _seat(MOD_HOUSE, "u-mod", role="moderator"),
            _seat(AUTHOR_HOUSE, "u-author", role="subscriber"),
        ],
    ):
        a = _mod_auth(rows=rows)
        assert not await a.may_author_approved(_released(HOST), SPACE, "u-author"), rows
    banned = _mod_auth(banned=[(SPACE, "u-author")])
    assert not await banned.may_author_approved(_released(HOST), SPACE, "u-author")


async def test_our_local_user_needs_our_own_matching_queue_row() -> None:
    """No household can author as one of OUR people unless we hold the very
    item it releases — same submitter, same target, same content."""
    rows = [_seat(MOD_HOUSE, "u-mod", role="moderator")]
    local = {"u-author"}
    members = {"u-author": "member"}
    without = _mod_auth(rows=rows, local=local, members=members)
    assert not await without.may_author_approved(_released(HOST), SPACE, "u-author")
    held = _mod_auth(
        rows=rows, local=local, members=members, items={"item-1": _queued()}
    )
    assert await held.may_author_approved(_released(HOST), SPACE, "u-author")
    # Different words than the item proposed.
    assert not await held.may_author_approved(
        _released(HOST, name="Free beer"), SPACE, "u-author"
    )
    for item in (
        _queued(submitted_by="u-else"),
        _queued(space_id="sp-other"),
        _queued(feature="pages"),
        _queued(status=ModerationStatus.EXPIRED),
        _queued(payload={}),
    ):
        a = _mod_auth(rows=rows, local=local, members=members, items={"item-1": item})
        assert not await a.may_author_approved(_released(HOST), SPACE, "u-author"), item
    # Approve beats reject: a row we rejected still takes the release.
    rejected = _mod_auth(
        rows=rows,
        local=local,
        members=members,
        items={"item-1": _queued(status=ModerationStatus.REJECTED)},
    )
    assert await rejected.may_author_approved(_released(HOST), SPACE, "u-author")
    # A local seat that cannot write takes nothing.
    reader = _mod_auth(
        rows=rows,
        local=local,
        members={"u-author": "subscriber"},
        items={"item-1": _queued()},
    )
    assert not await reader.may_author_approved(_released(HOST), SPACE, "u-author")


async def test_a_held_row_must_match_even_for_a_remote_author() -> None:
    a = _mod_auth(items={"item-1": _queued()})
    assert await a.may_author_approved(_released(HOST), SPACE, "u-author")
    assert not await a.may_author_approved(
        _released(HOST, name="Other"), SPACE, "u-author"
    )


async def test_admin_only_takes_a_release_from_an_admin_only() -> None:
    mod = _mod_auth(level="admin_only")
    assert not await mod.may_author_approved(_released(HOST), SPACE, "u-author")
    admin = _mod_auth(level="admin_only")
    ev = _released(HOST, approval={"item_id": "item-1", "approved_by": "u-admin"})
    assert await admin.may_author_approved(ev, SPACE, "u-author")


async def test_a_block_on_an_unreviewable_event_releases_nothing() -> None:
    a = _mod_auth()
    ev = FederationEvent(
        msg_id="m",
        event_type=FederationEventType.SPACE_COMMENT_CREATED,
        from_instance=MOD_HOUSE,
        to_instance="us",
        timestamp="2026-01-01T00:00:00+00:00",
        payload={"id": "c-1", "author": "u-author", "moderation": dict(_APPROVAL)},
        space_id=SPACE,
    )
    assert not await a.may_author_approved(ev, SPACE, "u-author")
    assert not await a.may_author_approved(
        _released(HOST, approval={"item_id": 3}), SPACE, "u-author"
    )


async def test_access_admits_takes_a_valid_release_under_moderated() -> None:
    a = _mod_auth()
    ev = _released(HOST)
    ev.payload["actor_user_id"] = "u-author"
    assert await a.access_admits(
        ev, SPACE, "tasks", ContentAction.CREATE, actor="u-author", row_owner="u-author"
    )
    plain = _released(MOD_HOUSE, approval=None)
    assert not await a.access_admits(
        plain,
        SPACE,
        "tasks",
        ContentAction.CREATE,
        actor="u-author",
        row_owner="u-author",
    )
    # An approval naming one actor while the payload names a third is refused.
    assert not await a.access_admits(
        ev, SPACE, "tasks", ContentAction.CREATE, actor="u-other", row_owner="u-author"
    )
    # An edit of somebody else's row, released: the actor is the submitter.
    edit = _released(HOST)
    assert await a.access_admits(
        edit, SPACE, "tasks", ContentAction.EDIT, actor="u-author", row_owner="u-host"
    )


# ─── item_access_admits: member-published space items (v_49) ───────────


async def _item(auth, sender, author):
    return await auth.item_access_admits(
        origin_instance_id=sender,
        space_id=SPACE,
        feature="posts",
        author_user_id=author,
    )


async def test_item_access_open_needs_a_writer_seat_on_the_origin():
    auth = _access_authorship(posts="open")
    assert await _item(auth, AUTHOR_HOUSE, "u-author")
    # Another household's user, a follower seat, nobody at all.
    assert not await _item(auth, AUTHOR_HOUSE, "u-admin")
    assert not await _item(auth, AUTHOR_HOUSE, "u-sub")
    assert not await _item(auth, AUTHOR_HOUSE, "u-unknown")


async def test_item_access_admin_only_needs_an_admin_seat():
    auth = _access_authorship(posts="admin_only")
    assert await _item(auth, ADMIN_HOUSE, "u-admin")
    assert not await _item(auth, ADMIN_HOUSE, "u-plain")
    assert not await _item(auth, MOD_HOUSE, "u-mod")


async def test_item_access_moderated_needs_content_authority():
    auth = _access_authorship(posts="moderated")
    assert await _item(auth, MOD_HOUSE, "u-mod")
    assert await _item(auth, ADMIN_HOUSE, "u-admin")
    assert not await _item(auth, AUTHOR_HOUSE, "u-author")


async def test_item_access_for_an_unknown_space_is_refused():
    auth = _access_authorship(posts="open")
    assert not await auth.item_access_admits(
        origin_instance_id=AUTHOR_HOUSE,
        space_id="nope",
        feature="posts",
        author_user_id="u-author",
    )
