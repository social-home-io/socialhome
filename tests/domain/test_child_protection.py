"""Tests for the protected-account domain vocabulary (§CP.R)."""

from __future__ import annotations

from socialhome.domain.child_protection import (
    NAME_ONLY_WITHHELD_KEYS,
    PROTECTED_ACCOUNT_RESTRICTIONS,
    AccountProtectedError,
    ProtectedCapability,
    name_only,
)
from socialhome.security import SENSITIVE_FIELDS


def test_every_capability_is_restricted_for_a_protected_account():
    assert set(PROTECTED_ACCOUNT_RESTRICTIONS) == set(ProtectedCapability)
    assert len(PROTECTED_ACCOUNT_RESTRICTIONS) == len(
        set(PROTECTED_ACCOUNT_RESTRICTIONS)
    )


def test_capability_wire_values_are_stable():
    # The SPA keys its copy on these; the docs list them in this order.
    assert [c.value for c in PROTECTED_ACCOUNT_RESTRICTIONS] == [
        "bazaar",
        "public_spaces",
        "public_moments",
        "public_links",
        "api_tokens",
        "calendar_feeds",
    ]


def test_capability_names_never_collide_with_sensitive_fields():
    assert not ({c.value for c in ProtectedCapability} & SENSITIVE_FIELDS)


def test_account_protected_error_carries_the_capability():
    err = AccountProtectedError(ProtectedCapability.BAZAAR)
    assert err.capability is ProtectedCapability.BAZAAR
    assert isinstance(err, PermissionError)
    # The message names the surface, never the minor flag or an age.
    assert "bazaar" in str(err)
    assert "minor" not in str(err).lower()
    assert "declared_age" not in str(err)


def test_name_only_blanks_picture_and_bio_but_keeps_the_name():
    row = {
        "user_id": "u-kid",
        "display_name": "Kid",
        "picture_hash": "abc",
        "picture_url": "api/users/u-kid/picture?v=abc",
        "bio": "I like trains",
    }
    out = name_only(row)
    assert out == {
        "user_id": "u-kid",
        "display_name": "Kid",
        "picture_hash": None,
        "picture_url": None,
        "bio": None,
    }
    # The input row is untouched.
    assert row["bio"] == "I like trains"


def test_name_only_never_adds_keys_the_row_did_not_carry():
    assert name_only({"user_id": "u", "picture_url": "x"}) == {
        "user_id": "u",
        "picture_url": None,
    }
    assert set(NAME_ONLY_WITHHELD_KEYS) == {"picture_hash", "picture_url", "bio"}


def test_name_only_reads_session_presence_as_offline():
    row = {"user_id": "u", "is_online": True, "is_idle": True, "last_seen_at": "x"}
    assert name_only(row) == {
        "user_id": "u",
        "is_online": False,
        "is_idle": False,
        "last_seen_at": None,
    }
