"""Unit tests for :class:`BazaarFaqService` — the seller's questions and
answers on a listing (v_75)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.domain.bazaar_faq import (
    BazaarFaqEntry,
    BazaarFaqRateLimitedError,
    BazaarFaqTextError,
)
from socialhome.domain.child_protection import (
    AccountProtectedError,
    ProtectedCapability,
)
from socialhome.domain.events import BazaarListingFaqChanged
from socialhome.domain.post import BazaarListing, BazaarMode, BazaarStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.bazaar_faq_service import BazaarFaqService
from socialhome.services.bazaar_service import ListingNotFoundError

LISTING = BazaarListing(
    post_id="bzr-1",
    space_id="sp-1",
    seller_user_id="u-seller",
    mode=BazaarMode.FIXED,
    title="Bike",
    end_time="2099-01-01T00:00:00+00:00",
    currency="EUR",
    status=BazaarStatus.ACTIVE,
    created_at="2026-10-30T10:00:00+00:00",
    price=4000,
)


def _entry(**kw) -> BazaarFaqEntry:
    fields = {
        "listing_post_id": "bzr-1",
        "entry_id": "ab" * 16,
        "question": "Does it fold?",
        "answer": "Yes.",
        "position": 0,
        "rev": 1,
    }
    fields.update(kw)
    return BazaarFaqEntry(**fields)


@pytest.fixture
def env():
    bazaar = MagicMock()
    bazaar.editable_listing = AsyncMock(return_value=LISTING)
    bazaar.get_visible_listing = AsyncMock(return_value=LISTING)
    repo = MagicMock()
    repo.list_faq = AsyncMock(return_value=[_entry()])
    repo.add_faq_entry = AsyncMock(return_value=_entry())
    repo.edit_faq_entry = AsyncMock(return_value=_entry(rev=2))
    repo.remove_faq_entry = AsyncMock(
        return_value=_entry(rev=3, removed=True, question="", answer="")
    )
    bus = EventBus()
    published: list[BazaarListingFaqChanged] = []

    async def _keep(event):
        published.append(event)

    bus.subscribe(BazaarListingFaqChanged, _keep)
    svc = BazaarFaqService(bazaar_service=bazaar, bazaar_repo=repo, bus=bus)
    return svc, bazaar, repo, published


async def test_anyone_who_sees_the_listing_reads_its_faq(env):
    svc, bazaar, _repo, _ = env
    assert await svc.list_entries("bzr-1", "u-viewer") == [_entry()]
    bazaar.get_visible_listing.assert_awaited_once_with("bzr-1", "u-viewer")
    bazaar.get_visible_listing.side_effect = ListingNotFoundError("bzr-1")
    with pytest.raises(ListingNotFoundError):
        await svc.list_entries("bzr-1", "u-stranger")


async def test_add_cleans_the_text_and_announces_the_entry(env):
    svc, bazaar, repo, published = env
    await svc.add(
        "bzr-1", actor_user_id="u-seller", question=" Does\nit fold? ", answer="Yes."
    )
    bazaar.editable_listing.assert_awaited_once_with("bzr-1", "u-seller")
    repo.add_faq_entry.assert_awaited_once_with(
        "bzr-1", question="Does it fold?", answer="Yes."
    )
    assert [(e.space_id, e.seller_user_id, e.entry) for e in published] == [
        ("sp-1", "u-seller", _entry())
    ]
    assert published[0].origin_instance_id is None


async def test_edit_and_remove_announce_the_change(env):
    svc, _bazaar, repo, published = env
    await svc.edit(
        "bzr-1", "ab" * 16, actor_user_id="u-seller", question="Q?", answer="A."
    )
    repo.edit_faq_entry.assert_awaited_once_with(
        "bzr-1", "ab" * 16, question="Q?", answer="A."
    )
    await svc.remove("bzr-1", "ab" * 16, actor_user_id="u-seller")
    repo.remove_faq_entry.assert_awaited_once_with("bzr-1", "ab" * 16)
    assert [e.entry.rev for e in published] == [2, 3]
    assert published[1].entry.removed


async def test_bad_text_is_refused_before_anything_is_written(env):
    svc, _bazaar, repo, published = env
    with pytest.raises(BazaarFaqTextError):
        await svc.add("bzr-1", actor_user_id="u-seller", question="", answer="A")
    with pytest.raises(BazaarFaqTextError):
        await svc.add("bzr-1", actor_user_id="u-seller", question="Q", answer=None)
    repo.add_faq_entry.assert_not_awaited()
    assert published == []


async def test_only_who_may_edit_the_listing_writes_it(env):
    svc, bazaar, repo, _ = env
    bazaar.editable_listing.side_effect = PermissionError("only the seller")
    with pytest.raises(PermissionError):
        await svc.add("bzr-1", actor_user_id="u-buyer", question="Q", answer="A")
    repo.add_faq_entry.assert_not_awaited()


async def test_a_protected_account_writes_nothing(env):
    svc, _bazaar, repo, _ = env
    cp = MagicMock()
    cp.require_unrestricted = AsyncMock(
        side_effect=AccountProtectedError(ProtectedCapability.BAZAAR)
    )
    svc.attach_child_protection(cp)
    with pytest.raises(AccountProtectedError):
        await svc.add("bzr-1", actor_user_id="u-seller", question="Q", answer="A")
    repo.add_faq_entry.assert_not_awaited()


async def test_changes_are_capped_per_listing(env):
    svc, _bazaar, repo, _ = env
    for _ in range(30):
        await svc.edit(
            "bzr-1", "ab" * 16, actor_user_id="u-seller", question="Q", answer="A"
        )
    with pytest.raises(BazaarFaqRateLimitedError):
        await svc.remove("bzr-1", "ab" * 16, actor_user_id="u-seller")
    # Another listing has its own budget.
    await svc.add("bzr-2", actor_user_id="u-seller", question="Q", answer="A")
    assert repo.edit_faq_entry.await_count == 30
