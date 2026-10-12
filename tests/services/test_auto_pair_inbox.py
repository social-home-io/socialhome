"""Tests for :class:`AutoPairInbox` — the target's queue of trust-relay
introductions awaiting its admin (§11 simple pairing)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.auto_pair_inbox import (
    AUTO_PAIR_REQUEST_TTL_SECONDS,
    AutoPairInbox,
    auto_pair_request_expired,
)


def _past(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _enqueue(inbox: AutoPairInbox, a_id: str = "a1"):
    return inbox.enqueue(
        from_a_id=a_id,
        from_a_pk="aa",
        from_a_inbox_url="https://a.example/federation/inbox/x",
        from_a_dh_pk="dd",
        via_b_id="b1",
        vouch_sig="vv",
        ts=datetime.now(timezone.utc).isoformat(),
        nonce="n",
        token="tk",
    )


def test_enqueue_get_pop_round_trip():
    inbox = AutoPairInbox(bus=EventBus())
    req = _enqueue(inbox)
    assert inbox.get(req.request_id) is req
    assert [r.request_id for r in inbox.list_pending()] == [req.request_id]
    assert inbox.pop(req.request_id) is req
    assert inbox.list_pending() == []


def test_the_inbox_forgets_requests_past_their_ttl():
    inbox = AutoPairInbox(bus=EventBus())
    fresh = _enqueue(inbox, "a-fresh")
    stale = _enqueue(inbox, "a-stale")
    stale.received_at = _past(AUTO_PAIR_REQUEST_TTL_SECONDS + 60)

    assert [r.request_id for r in inbox.list_pending()] == [fresh.request_id]
    assert inbox.get(stale.request_id) is None
    assert inbox.pop(stale.request_id) is None


def test_request_expiry():
    assert auto_pair_request_expired(_past(60)) is False
    assert auto_pair_request_expired(_past(AUTO_PAIR_REQUEST_TTL_SECONDS + 1)) is True
    # A naive stamp is read as UTC.
    naive = (datetime.now(timezone.utc) - timedelta(seconds=60)).replace(tzinfo=None)
    assert auto_pair_request_expired(naive.isoformat()) is False
    # Fail closed on garbage.
    assert auto_pair_request_expired("not-a-date") is True
