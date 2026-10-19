"""Tests for :func:`space_attention` — the "needs you" counts on
``GET /api/spaces/{id}``, by the viewer's role (service stubs)."""

from __future__ import annotations

import logging

import pytest

from socialhome.domain.space import SpaceMember, SpacePermissionError, SpaceRole
from socialhome.services.space_attention import needs_you_count, space_attention

_SID = "sp-1"


class _Moderation:
    async def list_items(self, space_id, *, actor_user_id):
        assert (space_id, actor_user_id) == (_SID, "uid-me")
        return [object(), object()]


class _Reports:
    async def review_space(self, space_id, *, actor_user_id):
        return [object()]


class _Spaces:
    async def list_pending_join_requests(self, space_id, *, actor_username):
        assert actor_username == "me"
        return [{"id": "jr-1"}, {"id": "jr-2"}, {"id": "jr-3"}]


class _Approvals:
    async def list_for_space(self, space_id, *, viewer_user_id):
        return [
            {
                "id": "p-1",
                "action": "set_public_tier",
                "params": {"space_type": "public"},
                "approvals": 1,
                "needed": 2,
                "total_admins": 2,
                "voted_by_me": True,
            }
        ]


def _seat(role: SpaceRole) -> SpaceMember:
    return SpaceMember(
        space_id=_SID, user_id="uid-me", role=role, joined_at="2026-10-18T00:00:00"
    )


async def _read(role: SpaceRole, *, moderation: object = "default"):
    if moderation == "default":
        moderation = _Moderation()
    return await space_attention(
        _SID,
        seat=_seat(role),
        username="me",
        space_service=_Spaces(),
        approvals=_Approvals(),
        reports=_Reports(),
        moderation=moderation,
    )


@pytest.mark.parametrize("role", [SpaceRole.OWNER, SpaceRole.ADMIN])
async def test_settings_authority_sees_everything_waiting(role):
    out = await _read(role)
    assert out["pending_review"] == 2
    assert out["reports"] == 1
    assert out["join_requests"] == 3
    (p,) = out["proposals"]
    assert p["id"] == "p-1"
    assert p["required"] == 2
    assert p["approvals"] == 1
    assert p["voted_by_me"] is True
    assert p["params"] == {"space_type": "public"}


async def test_a_moderator_sees_review_and_reports_only():
    out = await _read(SpaceRole.MODERATOR)
    assert out == {
        "pending_review": 2,
        "reports": 1,
        "join_requests": 0,
        "proposals": [],
    }


@pytest.mark.parametrize("role", [SpaceRole.MEMBER, SpaceRole.SUBSCRIBER])
async def test_nothing_waits_for_a_member_or_a_follower(role):
    assert await _read(role) is None


async def test_no_moderation_service_counts_nothing_held():
    out = await _read(SpaceRole.ADMIN, moderation=None)
    assert out["pending_review"] == 0
    assert out["reports"] == 1


async def test_a_refused_read_counts_nothing_and_is_logged(caplog):
    """The counts are a hint: a read its service refuses must not fail
    the whole space detail."""

    class _Refusing:
        async def review_space(self, space_id, *, actor_user_id):
            raise SpacePermissionError("seat changed")

    caplog.set_level(logging.WARNING, logger="socialhome.services.space_attention")
    out = await space_attention(
        _SID,
        seat=_seat(SpaceRole.ADMIN),
        username="me",
        space_service=_Spaces(),
        approvals=_Approvals(),
        reports=_Refusing(),
        moderation=_Moderation(),
    )
    assert out["reports"] == 0
    assert out["pending_review"] == 2
    assert "reports refused" in caplog.text


def test_needs_you_count_is_the_more_badge_total():
    """Held + reports + join requests + proposals awaiting my vote."""
    attention = {
        "pending_review": 2,
        "reports": 1,
        "join_requests": 1,
        "proposals": [
            {"voted_by_me": False, "owner_only": False},
            {"voted_by_me": True, "owner_only": False},
            {"voted_by_me": False, "owner_only": True},
        ],
    }
    assert needs_you_count(attention, "admin") == 5
    assert needs_you_count(attention, "owner") == 6
    assert needs_you_count(None, "member") is None
