"""Tests for the protected-account domain vocabulary (§CP.R)."""

from __future__ import annotations

from socialhome.domain.child_protection import (
    PROTECTED_ACCOUNT_RESTRICTIONS,
    AccountProtectedError,
    ProtectedCapability,
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
