"""Tests for ``socialhome.federation.sync.space.exporters.bazaar``."""

from __future__ import annotations

from types import SimpleNamespace

from socialhome.domain.bazaar_faq import BazaarFaqEntry, entry_from_wire
from socialhome.domain.post import BazaarListing, BazaarMode, BazaarStatus
from socialhome.federation.sync.space.exporters import BazaarExporter

LISTING = BazaarListing(
    post_id="bzr-1",
    space_id="sp-1",
    seller_user_id="u-sam",
    mode=BazaarMode.FIXED,
    title="Bike",
    end_time="2099-01-01T00:00:00+00:00",
    currency="EUR",
    status=BazaarStatus.ACTIVE,
    created_at="2026-10-30T10:00:00+00:00",
    price=4000,
)
LIVE = BazaarFaqEntry(
    listing_post_id="bzr-1",
    entry_id="ab" * 16,
    question="Does it fold?",
    answer="Yes.",
    position=0,
    rev=2,
)
GONE = BazaarFaqEntry(
    listing_post_id="bzr-1",
    entry_id="cd" * 16,
    question="",
    answer="",
    position=1,
    rev=3,
    removed=True,
)


class _Windows:
    async def for_space(self, space_id):
        return SimpleNamespace(cutoff=None, exempt_types=())


class _Repo:
    def __init__(self, listings, faq):
        self._listings = listings
        self._faq = faq
        self.faq_asked: list[list[str]] = []

    async def list_sync_page(self, space_id, **_kw):
        return list(self._listings), None

    async def list_faq_for_listings(self, post_ids):
        self.faq_asked.append(list(post_ids))
        return {pid: self._faq.get(pid, []) for pid in post_ids if pid in self._faq}


async def test_a_record_carries_its_listings_faq_and_removal_markers():
    repo = _Repo([LISTING], {"bzr-1": [LIVE, GONE]})
    exporter = BazaarExporter(repo, _Windows())
    [record] = await exporter.list_records("sp-1")
    assert record["post_id"] == "bzr-1"
    assert repo.faq_asked == [["bzr-1"]]  # one read per page, not per listing
    assert record["faq"] == [
        {
            "entry_id": "ab" * 16,
            "rev": 2,
            "position": 0,
            "removed": False,
            "question": "Does it fold?",
            "answer": "Yes.",
        },
        {"entry_id": "cd" * 16, "rev": 3, "position": 1, "removed": True},
    ]
    # What the receiver parses is what the seller's household holds.
    assert [entry_from_wire("bzr-1", item) for item in record["faq"]] == [LIVE, GONE]


async def test_a_listing_without_questions_carries_an_empty_faq():
    exporter = BazaarExporter(_Repo([LISTING], {}), _Windows())
    [record] = await exporter.list_records("sp-1")
    assert record["faq"] == []
