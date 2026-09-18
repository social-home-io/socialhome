"""Tests for socialhome.domain.space."""

from __future__ import annotations

import pytest

from socialhome.domain.space import (
    HouseholdFeatures,
    JoinMode,
    RemoteAdminOutcome,
    SpaceConfigGapError,
    SpaceFeatureAccess,
    SpaceFeatures,
    SpacePermissionError,
    normalize_join_mode,
    normalize_min_age,
)


def test_remote_admin_outcome_values():
    """The host's gate signals three outcomes by stable string value."""
    assert RemoteAdminOutcome.EXECUTED == "executed"
    assert RemoteAdminOutcome.NEEDS_OWNER_APPROVAL == "needs_owner_approval"
    assert RemoteAdminOutcome.DROPPED == "dropped"


def test_space_features_roundtrip():
    """SpaceFeatures survives a to_columns / from_row round-trip."""
    f = SpaceFeatures(calendar=True, tasks_access=SpaceFeatureAccess.MODERATED)
    f2 = SpaceFeatures.from_row(f.to_columns())
    assert f == f2


def test_space_features_wire_roundtrip():
    """SpaceFeatures survives a to_wire_dict / from_wire_dict round-trip —
    the path the cross-household admin config edit uses."""
    f = SpaceFeatures(
        calendar=False,
        bazaar=False,
        location=True,
        location_mode="zone_only",
        tasks_access=SpaceFeatureAccess.MODERATED,
        allow_subscriber_comment=True,
        allowed_post_types=("image", "text"),
    )
    assert SpaceFeatures.from_wire_dict(f.to_wire_dict()) == f


def test_space_features_wire_roundtrip_every_field_non_default():
    """CI guard: every SpaceFeatures field flipped to a NON-default value
    survives ``from_wire_dict(to_wire_dict())``.

    This fails the moment a field is dropped from either ``to_wire_dict``
    or ``from_wire_dict`` — the convergence point that previously let
    ``delegated_admin_authority`` (and the access / subscriber fields) be
    silently lost on the wire. Construct with EVERY field non-default so a
    missing field round-trips to its default and the equality breaks.
    """
    f = SpaceFeatures(
        calendar=False,  # default True
        todo=False,  # default True
        location=True,  # default False
        location_mode="zone_only",  # default "gps"
        stickies=False,  # default True
        pages=False,  # default True
        gallery=False,  # default True
        bazaar=False,  # default True
        posts_access=SpaceFeatureAccess.MODERATED,  # default OPEN
        pages_access=SpaceFeatureAccess.ADMIN_ONLY,  # default OPEN
        stickies_access=SpaceFeatureAccess.MODERATED,  # default OPEN
        calendar_access=SpaceFeatureAccess.ADMIN_ONLY,  # default OPEN
        tasks_access=SpaceFeatureAccess.MODERATED,  # default OPEN
        allow_subscriber_comment=True,  # default False
        allow_subscriber_react=True,  # default False
        delegated_admin_authority=True,  # default False
        allowed_post_types=("image", "text"),  # default _ALL_POST_TYPES
    )
    assert SpaceFeatures.from_wire_dict(f.to_wire_dict()) == f


def test_space_features_from_wire_dict_defaults_on_partial():
    """A partial / older-shaped wire dict falls back to class defaults
    rather than raising, and an unknown access level degrades to OPEN."""
    f = SpaceFeatures.from_wire_dict({"name_only": 1, "posts_access": "bogus"})
    assert f == SpaceFeatures()  # every field defaulted


def test_space_features_gallery_roundtrip():
    """gallery flag survives to_columns / from_row + appears on the wire."""
    on = SpaceFeatures(gallery=True)
    off = SpaceFeatures(gallery=False)
    assert SpaceFeatures.from_row(on.to_columns()).gallery is True
    assert SpaceFeatures.from_row(off.to_columns()).gallery is False
    assert on.to_wire_dict()["gallery"] is True
    assert off.to_wire_dict()["gallery"] is False


def test_space_features_gallery_default_on_missing_column():
    """A row missing ``feature_gallery`` (pre-0008 spaces from a peer
    that hasn't migrated yet) defaults to gallery=True so the tab
    stays visible — matches the dataclass default."""
    f = SpaceFeatures.from_row({})  # no feature_gallery column at all
    assert f.gallery is True


def test_space_features_delegated_admin_authority_default_false():
    """The delegated-admin-authority opt-in defaults OFF (least-privilege)."""
    assert SpaceFeatures().delegated_admin_authority is False


def test_space_features_delegated_admin_authority_roundtrip():
    """delegated_admin_authority survives to_columns/from_row + the wire."""
    on = SpaceFeatures(delegated_admin_authority=True)
    off = SpaceFeatures(delegated_admin_authority=False)
    assert SpaceFeatures.from_row(on.to_columns()).delegated_admin_authority is True
    assert SpaceFeatures.from_row(off.to_columns()).delegated_admin_authority is False
    assert on.to_wire_dict()["delegated_admin_authority"] is True
    assert off.to_wire_dict()["delegated_admin_authority"] is False
    assert (
        SpaceFeatures.from_wire_dict(on.to_wire_dict()).delegated_admin_authority
        is True
    )


def test_space_features_delegated_admin_authority_back_compat():
    """A row / wire dict missing the flag (older peer) defaults to False."""
    assert SpaceFeatures.from_row({}).delegated_admin_authority is False
    assert SpaceFeatures.from_wire_dict({}).delegated_admin_authority is False


def test_space_features_access_decision():
    """access_decision returns proceed/queue/deny based on access level and admin status."""
    f = SpaceFeatures(posts_access=SpaceFeatureAccess.MODERATED)
    assert f.access_decision("posts", is_admin=True) == "proceed"
    assert f.access_decision("posts", is_admin=False) == "queue"
    f2 = SpaceFeatures(posts_access=SpaceFeatureAccess.ADMIN_ONLY)
    assert f2.access_decision("posts", is_admin=False) == "deny"


def test_space_features_with_allowed_post_types():
    """with_allowed_post_types normalises and stores the set; empty set raises ValueError."""
    f = SpaceFeatures()
    f2 = f.with_allowed_post_types({"text", "image"})
    assert f2.allowed_post_types == ("image", "text")
    with pytest.raises(ValueError):
        f.with_allowed_post_types(set())


def test_household_features_roundtrip():
    """HouseholdFeatures survives a to_columns / from_row round-trip."""
    h = HouseholdFeatures(bazaar=False, household_name="Casa")
    h2 = HouseholdFeatures.from_row(h.to_columns())
    assert h == h2


def test_permission_error_banned():
    """SpacePermissionError with banned=True exposes the flag and a useful message."""
    e = SpacePermissionError("banned", banned=True)
    assert e.banned and "banned" in str(e)


def test_config_gap_error():
    """SpaceConfigGapError includes space_id, have, and need in its string form."""
    e = SpaceConfigGapError(space_id="s1", have=3, need=7)
    assert "s1" in str(e) and "3" in str(e)


# ─── normalize_min_age ───────────────────────────────────────────────────


@pytest.mark.parametrize("value", [0, 13, 16, 18, "13", "18"])
def test_normalize_min_age_passes_allowed_values(value):
    """Every value in the allowed set (and its string form) round-trips."""
    assert normalize_min_age(value) == int(value)


@pytest.mark.parametrize(
    "value",
    [15, -1, 99, None, "nope", 1.5, object(), [13]],
)
def test_normalize_min_age_clamps_everything_else(value):
    """Anything outside {0,13,16,18} falls back to 0 (fail-soft default)."""
    assert normalize_min_age(value) == 0


def test_allow_subscribers_defaults_off_and_is_independent_of_join_mode():
    """Readability is its own switch, not a property of ``JoinMode``.

    ``allow_subscribers`` defaults OFF, exactly like the two sibling
    subscriber flags it sits beside — a space is private until its owner says
    otherwise. And because it is a separate dial, BOTH cross-combinations are
    expressible: ``invite_only`` + subscribers-on (a broadcast space: invited
    people post, anyone may follow) and ``open`` + subscribers-off (joinable,
    but not publicly readable).
    """
    assert SpaceFeatures().allow_subscribers is False
    assert SpaceFeatures().allow_subscriber_comment is False
    assert SpaceFeatures().allow_subscriber_react is False
    # No join mode implies anything about readability any more — the enum
    # carries exactly three membership gates and nothing else.
    assert set(JoinMode) == {
        JoinMode.INVITE_ONLY,
        JoinMode.OPEN,
        JoinMode.REQUEST,
    }
    broadcast = SpaceFeatures(allow_subscribers=True)
    assert broadcast.allow_subscribers is True
    assert broadcast.to_wire_dict()["allow_subscribers"] is True


def test_allow_subscribers_round_trips_through_every_boundary():
    """Row → dataclass → columns → wire → dataclass, all four directions."""
    on = SpaceFeatures(allow_subscribers=True)
    assert on.to_columns()["allow_subscribers"] == 1
    assert SpaceFeatures().to_columns()["allow_subscribers"] == 0
    assert SpaceFeatures.from_row({"allow_subscribers": 1}).allow_subscribers is True
    assert SpaceFeatures.from_row({"allow_subscribers": 0}).allow_subscribers is False
    # A row written before migration 0051 has no such column — fail closed.
    assert SpaceFeatures.from_row({}).allow_subscribers is False
    assert SpaceFeatures.from_wire_dict(on.to_wire_dict()).allow_subscribers is True
    # An older peer's SPACE_SYNC_BEGIN omits the key ⇒ not readable.
    assert SpaceFeatures.from_wire_dict({}).allow_subscribers is False


# ─── normalize_join_mode ─────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["invite_only", "open", "request"])
def test_normalize_join_mode_passes_known_values(value):
    """Every known join mode round-trips as a plain ``str``."""
    got = normalize_join_mode(value)
    assert got == value
    assert type(got) is str


@pytest.mark.parametrize(
    "value",
    [None, "", "Open", "public", 7, 1.5, ["open"], {"open": 1}, object()],
)
def test_normalize_join_mode_fails_closed(value):
    """Anything unknown — missing, misspelled, hostile, non-string — becomes
    ``invite_only``, the mode that grants no public readership."""
    assert normalize_join_mode(value) == "invite_only"


def test_normalize_join_mode_accepts_the_enum_member():
    """A :class:`JoinMode` member (a ``StrEnum``) normalises to its value, so
    callers can pass the domain object straight through."""
    assert normalize_join_mode(JoinMode.OPEN) == "open"
