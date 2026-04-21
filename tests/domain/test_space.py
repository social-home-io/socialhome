"""Tests for social_home.domain.space."""

from __future__ import annotations

import pytest

from social_home.domain.space import (
    HouseholdFeatures,
    SpaceConfigGapError,
    SpaceFeatureAccess,
    SpaceFeatures,
    SpacePermissionError,
)


def test_space_features_roundtrip():
    """SpaceFeatures survives a to_columns / from_row round-trip."""
    f = SpaceFeatures(calendar=True, tasks_access=SpaceFeatureAccess.MODERATED)
    f2 = SpaceFeatures.from_row(f.to_columns())
    assert f == f2


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
