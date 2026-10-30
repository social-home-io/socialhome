"""Unit tests for :class:`BazaarFaqInboundHandlers` (v_75) — every refusal
on its own, against in-memory stubs. The end-to-end proof over real
households is ``tests/protocol/test_bazaar_faq_federation.py``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from socialhome.domain.events import BazaarListingFaqChanged
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.post import BazaarListing, BazaarMode, BazaarStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.federation_inbound.bazaar_faq import (
    BazaarFaqInboundHandlers,
)

SID = "sp-1"


def _listing(**kw) -> BazaarListing:
    fields = {
        "post_id": "bzr-1",
        "space_id": SID,
        "seller_user_id": "u-seller",
        "mode": BazaarMode.FIXED,
        "title": "Bike",
        "end_time": "2099-01-01T00:00:00+00:00",
        "currency": "EUR",
        "status": BazaarStatus.ACTIVE,
        "created_at": "2026-10-30T10:00:00+00:00",
    }
    fields.update(kw)
    return BazaarListing(**fields)


def _event(**over) -> FederationEvent:
    payload = {
        "space_id": SID,
        "listing_post_id": "bzr-1",
        "entry_id": "ab" * 16,
        "rev": 1,
        "position": 0,
        "removed": False,
        "question": "Does it fold?",
        "answer": "Yes.",
    }
    payload.update(over)
    return FederationEvent(
        msg_id="m-1",
        event_type=FederationEventType.BAZAAR_LISTING_FAQ_UPDATED,
        from_instance="inst-seller",
        to_instance="inst-us",
        timestamp="2026-10-30T10:00:00+00:00",
        payload=payload,
        space_id=SID,
    )


@pytest.fixture
def env():
    bus = EventBus()
    published: list[BazaarListingFaqChanged] = []

    async def _keep(event):
        published.append(event)

    bus.subscribe(BazaarListingFaqChanged, _keep)
    listings = MagicMock()
    listings.get_listing = AsyncMock(return_value=_listing())
    listings.apply_faq_entry = AsyncMock(return_value=True)
    spaces = MagicMock()
    spaces.get = AsyncMock(return_value=SimpleNamespace(dissolved=False))
    spaces.is_banned = AsyncMock(return_value=False)
    users = MagicMock()
    users.get_by_user_id = AsyncMock(return_value=None)
    authorship = MagicMock()
    authorship.acts_for = AsyncMock(return_value=True)
    handlers = BazaarFaqInboundHandlers(
        bus=bus,
        bazaar_repo=listings,
        space_repo=spaces,
        user_repo=users,
        authorship=authorship,
    )
    return SimpleNamespace(
        handlers=handlers,
        listings=listings,
        spaces=spaces,
        users=users,
        authorship=authorship,
        published=published,
    )


async def test_the_sellers_change_is_stored_and_announced(env):
    assert await env.handlers.apply(_event())
    entry = env.listings.apply_faq_entry.await_args.args[0]
    assert (entry.entry_id, entry.question, entry.rev) == (
        "ab" * 16,
        "Does it fold?",
        1,
    )
    env.authorship.acts_for.assert_awaited_once()
    assert env.authorship.acts_for.await_args.args[1:] == (SID, "u-seller")
    [event] = env.published
    assert event.origin_instance_id == "inst-seller"
    assert event.seller_user_id == "u-seller"


async def test_registers_on_the_event_registry(env):
    fed = MagicMock()
    env.handlers.attach_to(fed)
    fed._event_registry.register.assert_called_once_with(
        FederationEventType.BAZAAR_LISTING_FAQ_UPDATED, env.handlers._on_faq_updated
    )
    await env.handlers._on_faq_updated(_event())
    assert env.published


@pytest.mark.parametrize(
    ("setup", "event"),
    [
        (None, _event(listing_post_id="")),
        (None, _event(question="x" * 201)),
        (None, _event(space_id="sp-other")),
        ("no_space", _event()),
        ("dissolved", _event()),
        ("no_listing", _event()),
        ("other_space", _event()),
        ("our_seller", _event()),
        ("not_seated", _event()),
        ("banned", _event()),
        ("ended", _event()),
        ("not_newer", _event()),
    ],
)
async def test_refusals(env, setup, event):
    match setup:
        case "no_space":
            env.spaces.get.return_value = None
        case "dissolved":
            env.spaces.get.return_value = SimpleNamespace(dissolved=True)
        case "no_listing":
            env.listings.get_listing.return_value = None
        case "other_space":
            env.listings.get_listing.return_value = _listing(space_id="sp-other")
        case "our_seller":
            env.users.get_by_user_id.return_value = object()
        case "not_seated":
            env.authorship.acts_for.return_value = False
        case "banned":
            env.spaces.is_banned.return_value = True
        case "ended":
            env.listings.get_listing.return_value = _listing(status=BazaarStatus.SOLD)
        case "not_newer":
            env.listings.apply_faq_entry.return_value = False
    assert not await env.handlers.apply(event)
    assert env.published == []
    if setup != "not_newer":
        env.listings.apply_faq_entry.assert_not_awaited()


async def test_a_non_object_payload_is_refused(env):
    event = _event()
    event = FederationEvent(
        msg_id=event.msg_id,
        event_type=event.event_type,
        from_instance=event.from_instance,
        to_instance=event.to_instance,
        timestamp=event.timestamp,
        payload=None,
        space_id=SID,
    )
    assert not await env.handlers.apply(event)


async def test_a_flood_from_one_household_is_cut_off(env):
    for n in range(60):
        assert await env.handlers.apply(_event(rev=n + 1))
    assert not await env.handlers.apply(_event(rev=61))
    assert env.listings.apply_faq_entry.await_count == 60
