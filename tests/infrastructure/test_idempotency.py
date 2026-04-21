"""Tests for IdempotencyCache."""

from __future__ import annotations

import pytest

from social_home.infrastructure.idempotency import IdempotencyCache


# ─── Construction ────────────────────────────────────────────────────────


def test_zero_ttl_rejected():
    with pytest.raises(ValueError):
        IdempotencyCache(ttl_seconds=0)


def test_negative_ttl_rejected():
    with pytest.raises(ValueError):
        IdempotencyCache(ttl_seconds=-1)


# ─── Core API ────────────────────────────────────────────────────────────


def test_unseen_key_returns_false():
    c = IdempotencyCache(ttl_seconds=60)
    assert c.seen("k1") is False


def test_mark_then_seen():
    c = IdempotencyCache(ttl_seconds=60)
    c.mark_seen("k1")
    assert c.seen("k1") is True


def test_check_and_mark_first_call_returns_true():
    c = IdempotencyCache(ttl_seconds=60)
    assert c.check_and_mark("k1") is True


def test_check_and_mark_second_call_returns_false():
    c = IdempotencyCache(ttl_seconds=60)
    assert c.check_and_mark("k1") is True
    assert c.check_and_mark("k1") is False


def test_seen_does_not_record():
    """seen() is read-only — calling it does not flip future check_and_marks."""
    c = IdempotencyCache(ttl_seconds=60)
    assert c.seen("k1") is False
    assert c.check_and_mark("k1") is True


def test_separate_keys_independent():
    c = IdempotencyCache(ttl_seconds=60)
    c.mark_seen("a")
    assert c.seen("a") is True
    assert c.seen("b") is False


# ─── TTL eviction ────────────────────────────────────────────────────────


def test_key_expires_after_ttl():
    c = IdempotencyCache(ttl_seconds=10)
    c.mark_seen("k1", now=100)
    assert c.seen("k1", now=109) is True
    assert c.seen("k1", now=111) is False


def test_check_and_mark_after_expiry_returns_true_again():
    c = IdempotencyCache(ttl_seconds=10)
    assert c.check_and_mark("k1", now=100) is True
    assert c.check_and_mark("k1", now=200) is True


# ─── Cap ─────────────────────────────────────────────────────────────────


def test_max_entries_evicts_oldest():
    c = IdempotencyCache(ttl_seconds=3600, max_entries=3)
    c.mark_seen("a", now=1)
    c.mark_seen("b", now=2)
    c.mark_seen("c", now=3)
    c.mark_seen("d", now=4)
    # 'a' should have been evicted to make room for 'd'.
    assert c.seen("a", now=5) is False
    assert c.seen("d", now=5) is True


def test_size_reflects_live_entries():
    c = IdempotencyCache(ttl_seconds=10)
    c.mark_seen("a", now=1)
    c.mark_seen("b", now=1)
    assert c.size(now=2) == 2


def test_clear_drops_everything():
    c = IdempotencyCache(ttl_seconds=10)
    c.mark_seen("a")
    c.mark_seen("b")
    c.clear()
    assert c.size() == 0


def test_mark_seen_refreshes_existing_key_expiry():
    c = IdempotencyCache(ttl_seconds=10)
    c.mark_seen("k1", now=100)
    c.mark_seen("k1", now=105)  # refresh
    assert c.seen("k1", now=114) is True
    assert c.seen("k1", now=116) is False
