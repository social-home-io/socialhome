"""Tests for socialhome.domain.federation_retention — §4.4.7 outbox TTL policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_retention import (
    LONG_LIVED,
    LONG_LIVED_TTL,
    NEVER_DROP,
    RETENTION_DAYS,
    SHORT_LIVED,
    SHORT_LIVED_TTL,
    TERMINAL_GRACE,
    retention_expires_at,
    supersede_key,
)
from socialhome.domain.moment import MOMENT_MAX_LIFETIME_DAYS


def test_never_drop_event_has_no_expiry():
    """A NEVER_DROP event retries indefinitely — no expires_at."""
    assert retention_expires_at(FederationEventType.SPACE_DISSOLVED) is None
    assert retention_expires_at(FederationEventType.SPACE_MEMBER_BANNED) is None
    assert retention_expires_at(FederationEventType.UNPAIR) is None


def test_ordinary_event_expires_in_seven_days():
    """An ordinary event gets a ~7-day TTL from now (parses, in window)."""
    now = datetime.now(UTC)
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
    base = datetime(2026, 1, 1, tzinfo=UTC)
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


# ── Ephemeral state: latest-only and short-lived (§4.4.7) ──────────────────


def test_online_status_supersedes_per_user_across_the_three_types():
    """ONLINE / IDLE / OFFLINE for one person share one key — only the
    latest of them is worth delivering — and another person has another."""
    keys = {
        supersede_key(t, space_id=None, payload={"user_id": "u1"})
        for t in (
            FederationEventType.USER_ONLINE,
            FederationEventType.USER_IDLE,
            FederationEventType.USER_OFFLINE,
        )
    }
    assert len(keys) == 1
    (key,) = keys
    assert key is not None
    other = supersede_key(
        FederationEventType.USER_ONLINE, space_id=None, payload={"user_id": "u2"}
    )
    assert other != key


def test_location_supersedes_per_user_and_space():
    a = supersede_key(
        FederationEventType.SPACE_LOCATION_UPDATED,
        space_id="s1",
        payload={"user_id": "u1", "lat": 1.0},
    )
    assert a is not None
    assert a == supersede_key(
        FederationEventType.SPACE_LOCATION_UPDATED,
        space_id="s1",
        payload={"user_id": "u1", "lat": 2.0},
    )
    assert a != supersede_key(
        FederationEventType.SPACE_LOCATION_UPDATED,
        space_id="s2",
        payload={"user_id": "u1"},
    )
    assert a != supersede_key(
        FederationEventType.SPACE_LOCATION_UPDATED,
        space_id="s1",
        payload={"user_id": "u2"},
    )


def test_location_and_online_keys_never_collide():
    assert supersede_key(
        FederationEventType.USER_ONLINE, space_id="s1", payload={"user_id": "u1"}
    ) != supersede_key(
        FederationEventType.SPACE_LOCATION_UPDATED,
        space_id="s1",
        payload={"user_id": "u1"},
    )


def test_ordinary_events_never_supersede():
    """A DM or a post is never replaced by a later one."""
    for t in (
        FederationEventType.DM_MESSAGE,
        FederationEventType.SPACE_POST_CREATED,
        FederationEventType.CALL_ICE_CANDIDATE,
    ):
        assert supersede_key(t, space_id="s1", payload={"user_id": "u1"}) is None


def test_a_keyed_type_without_its_fields_does_not_supersede():
    """No user or no space → no key: never collapse distinct rows by guess."""
    assert (
        supersede_key(FederationEventType.USER_ONLINE, space_id=None, payload={})
        is None
    )
    assert (
        supersede_key(
            FederationEventType.SPACE_LOCATION_UPDATED,
            space_id=None,
            payload={"user_id": "u1"},
        )
        is None
    )


def test_call_negotiation_lives_two_minutes():
    """A call rings for 90 s; an offer / answer / candidate queued longer
    than that is noise."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for t in SHORT_LIVED:
        iso = retention_expires_at(t, now=base)
        assert iso is not None
        assert datetime.fromisoformat(iso) == base + SHORT_LIVED_TTL
    assert SHORT_LIVED_TTL == timedelta(minutes=2)
    assert not SHORT_LIVED & NEVER_DROP


def test_every_call_signal_but_the_terminal_ones_is_short_lived():
    """HANGUP / END close a call record on the far side — one per call,
    worth delivering late; everything else is negotiation."""
    calls = {t for t in FederationEventType if t.value.startswith("call_")}
    terminal = {FederationEventType.CALL_HANGUP, FederationEventType.CALL_END}
    assert calls - terminal == set(SHORT_LIVED)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for t in terminal:
        iso = retention_expires_at(t, now=base)
        assert iso is not None
        assert datetime.fromisoformat(iso) == base + timedelta(days=RETENTION_DAYS)


def test_a_moment_delete_lives_thirty_days():
    """v_66: a moment's delete must still arrive after a long absence — as
    long as a moment can live — but not forever, and stays evictable."""
    base = datetime(2026, 10, 25, tzinfo=UTC)
    assert LONG_LIVED == {FederationEventType.MOMENT_DELETED}
    iso = retention_expires_at(FederationEventType.MOMENT_DELETED, now=base)
    assert datetime.fromisoformat(iso) == base + LONG_LIVED_TTL
    assert (
        LONG_LIVED_TTL == timedelta(days=MOMENT_MAX_LIFETIME_DAYS) == timedelta(days=30)
    )
    assert not LONG_LIVED & NEVER_DROP
    assert not LONG_LIVED & SHORT_LIVED
    # A moment's create keeps the ordinary week.
    created = retention_expires_at(FederationEventType.MOMENT_CREATED, now=base)
    assert datetime.fromisoformat(created) == base + timedelta(days=7)
