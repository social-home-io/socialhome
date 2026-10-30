"""Pure rules of "Ask the seller" threads (v_74)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.bazaar_thread import (
    LISTING_THREAD_GRACE,
    LISTING_THREAD_ID_PREFIX,
    LISTING_THREAD_RATE_CAPS,
    ListingThreadClosedError,
    ListingThreadNoChangesError,
    ListingThreadRateLimitedError,
    ListingThreadTextOnlyError,
    ListingThreadUnsupportedError,
    announced_end,
    is_listing_thread_id,
    listing_ended_at,
    listing_thread_closes_at,
    listing_thread_id,
    listing_thread_writable,
)
from socialhome.domain.post import BazaarListing, BazaarMode, BazaarStatus

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

LISTING = BazaarListing(
    post_id="post-1",
    space_id="sp",
    seller_user_id="u-sam",
    mode=BazaarMode.FIXED,
    title="Bike",
    end_time=(NOW + timedelta(days=3)).isoformat(),
    currency="EUR",
    status=BazaarStatus.ACTIVE,
    created_at=NOW.isoformat(),
)


def test_thread_id_is_shared_stable_and_per_buyer():
    a = listing_thread_id("post-1", "u-ben")
    assert a == listing_thread_id("post-1", "u-ben")
    assert a != listing_thread_id("post-1", "u-bea")
    assert a != listing_thread_id("post-2", "u-ben")
    assert len(a) == 32 and a.startswith(LISTING_THREAD_ID_PREFIX)
    assert is_listing_thread_id(a)
    assert not is_listing_thread_id("ab" * 16)


def test_an_active_listing_never_closes():
    assert listing_ended_at(LISTING) is None
    assert listing_thread_closes_at(LISTING) is None
    assert listing_thread_writable(LISTING, now=NOW + timedelta(days=400))


def test_a_sale_starts_the_handover_window_at_sold_at():
    sold_at = NOW - timedelta(days=2)
    sold = replace(
        LISTING, status=BazaarStatus.SOLD, sold_at=sold_at.isoformat(timespec="seconds")
    )
    assert listing_ended_at(sold) == sold_at
    assert listing_thread_closes_at(sold) == sold_at + LISTING_THREAD_GRACE
    assert listing_thread_writable(sold, now=NOW)
    assert not listing_thread_writable(sold, now=sold_at + LISTING_THREAD_GRACE)


def test_a_naive_sqlite_stamp_reads_as_utc():
    sold = replace(LISTING, status=BazaarStatus.SOLD, sold_at="2026-10-01 08:00:00")
    assert listing_ended_at(sold) == datetime(2026, 10, 1, 8, tzinfo=UTC)


@pytest.mark.parametrize("status", [BazaarStatus.EXPIRED, BazaarStatus.CANCELLED])
def test_an_expired_or_withdrawn_listing_closes_after_its_end_time(status):
    ended = replace(
        LISTING, status=status, end_time=(NOW - timedelta(days=15)).isoformat()
    )
    assert not listing_thread_writable(ended, now=NOW)
    recent = replace(
        LISTING, status=status, end_time=(NOW - timedelta(days=1)).isoformat()
    )
    assert listing_thread_writable(recent, now=NOW)


def test_a_sale_without_sold_at_falls_back_to_end_time():
    sold = replace(
        LISTING,
        status=BazaarStatus.SOLD,
        end_time=(NOW - timedelta(days=20)).isoformat(),
    )
    assert not listing_thread_writable(sold, now=NOW)


def test_an_unreadable_end_stamp_fails_closed():
    broken = replace(LISTING, status=BazaarStatus.EXPIRED, end_time="not a time")
    assert not listing_thread_writable(broken, now=NOW)
    closes = listing_thread_closes_at(broken)
    assert closes is not None and closes < NOW


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ListingThreadClosedError, 409, "LISTING_THREAD_CLOSED"),
        (ListingThreadUnsupportedError, 409, "SELLER_HOUSEHOLD_TOO_OLD"),
        (ListingThreadTextOnlyError, 422, "LISTING_THREAD_TEXT_ONLY"),
        (ListingThreadNoChangesError, 422, "LISTING_THREAD_NO_CHANGES"),
        (ListingThreadRateLimitedError, 429, "LISTING_THREAD_RATE_LIMITED"),
    ],
)
def test_coded_refusals(error, status, code):
    exc = error()
    assert (exc.status, exc.code) == (status, code)
    assert exc.detail


def test_rate_caps_are_an_hourly_and_a_daily_window():
    windows = dict(LISTING_THREAD_RATE_CAPS)
    assert windows == {timedelta(hours=1): 30, timedelta(days=1): 100}


def test_announced_end_takes_the_senders_stamp():
    stamp = NOW - timedelta(minutes=5)
    assert announced_end(stamp.isoformat(), now=NOW) == stamp


def test_announced_end_reads_a_naive_sqlite_stamp_as_utc():
    assert announced_end("2026-10-30 11:00:00", now=NOW) == datetime(
        2026, 10, 30, 11, tzinfo=UTC
    )


@pytest.mark.parametrize("raw", [None, "", "not a time", 42])
def test_announced_end_without_a_readable_stamp_is_now(raw):
    assert announced_end(raw, now=NOW) == NOW


def test_announced_end_never_lies_in_the_future():
    # A future stamp would keep the thread open past its real end.
    later = (NOW + timedelta(days=30)).isoformat()
    assert announced_end(later, now=NOW) == NOW
