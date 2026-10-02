"""Tests for socialhome.domain.federation_retention — §4.4.7 outbox TTL policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_retention import (
    NEVER_DROP,
    RETENTION_DAYS,
    TERMINAL_GRACE,
    retention_expires_at,
)


def test_never_drop_event_has_no_expiry():
    """A NEVER_DROP event retries indefinitely — no expires_at."""
    assert retention_expires_at(FederationEventType.SPACE_DISSOLVED) is None
    assert retention_expires_at(FederationEventType.SPACE_MEMBER_BANNED) is None
    assert retention_expires_at(FederationEventType.UNPAIR) is None


def test_ordinary_event_expires_in_seven_days():
    """An ordinary event gets a ~7-day TTL from now (parses, in window)."""
    now = datetime.now(timezone.utc)
    iso = retention_expires_at(FederationEventType.SPACE_POST_CREATED)
    assert iso is not None
    parsed = datetime.fromisoformat(iso)
    assert parsed > now + timedelta(days=6)
    assert parsed < now + timedelta(days=8)


def test_retention_days_constant():
    """RETENTION_DAYS is 7 (§4.4.7)."""
    assert RETENTION_DAYS == 7


def test_terminal_grace_is_positive_timedelta():
    """TERMINAL_GRACE is a positive timedelta of a sane minimum (≥1h)."""
    assert isinstance(TERMINAL_GRACE, timedelta)
    assert TERMINAL_GRACE > timedelta(0)
    assert TERMINAL_GRACE >= timedelta(hours=1)


def test_retention_expires_at_honours_injected_now():
    """The ``now`` kwarg anchors the expiry deterministically."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    iso = retention_expires_at(FederationEventType.DM_MESSAGE, now=base)
    assert iso is not None
    assert datetime.fromisoformat(iso) == base + timedelta(days=RETENTION_DAYS)


def test_never_drop_membership():
    """NEVER_DROP carries the expected security / structural members."""
    expected = {
        FederationEventType.SPACE_DISSOLVED,
        FederationEventType.SPACE_MEMBER_BANNED,
        FederationEventType.SPACE_MEMBER_UNBANNED,
        FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
        FederationEventType.SPACE_REMOTE_ADMIN_KICK,
        FederationEventType.SPACE_KEY_EXCHANGE,
        FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
        FederationEventType.SPACE_ADMIN_KEY_SHARE,
        # v_44 — a lost rotation bundle leaves a member pinned to a key the
        # revoked household still holds.
        FederationEventType.SPACE_AUTHORITY_ROTATED,
        FederationEventType.UNPAIR,
    }
    # Exact equality — catches an accidentally dropped OR added member, so the
    # set stays the deliberate §4.4.7 security/structural list.
    assert expected == NEVER_DROP
