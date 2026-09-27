"""§24.11 — who may write space content on behalf of whom.

Unit tests for :class:`SpaceAuthorship` over in-memory stubs. The
protocol-level proof (real app, real registry, table snapshots) lives in
``tests/protocol/test_space_content_authorship.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from socialhome.domain.federation import FederationEvent, FederationEventType
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
    def __init__(self, spaces: dict[str, _Space]) -> None:
        self._spaces = spaces

    async def get(self, space_id: str):
        return self._spaces.get(space_id)


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
    assert not await a.is_moderator(_ev(ADMIN_HOUSE), SPACE)


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
