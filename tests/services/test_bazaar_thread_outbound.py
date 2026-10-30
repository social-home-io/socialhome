"""Tests for socialhome.services.bazaar_thread_outbound — a local message in
an "Ask the seller" thread goes to the other person's household only,
version-gated, never echoed."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from socialhome.domain.conversation import (
    Conversation,
    ConversationType,
    RemoteConversationMember,
)
from socialhome.domain.events import DmMessageCreated
from socialhome.domain.federation import (
    BroadcastResult,
    DeliveryResult,
    FederationEventType,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.post import BazaarListing, BazaarMode, BazaarStatus
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.bazaar_thread_outbound import BazaarThreadOutbound

FET = FederationEventType
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
SELLER_HOUSE = "house-sam"
BUYER_HOUSE = "house-ben"

LISTING = BazaarListing(
    post_id="post-bike",
    space_id="sp",
    seller_user_id="u-sam",
    mode=BazaarMode.FIXED,
    title="Bike",
    end_time="2099-01-01T00:00:00+00:00",
    currency="EUR",
    status=BazaarStatus.ACTIVE,
    created_at=NOW.isoformat(),
)


@dataclass
class _Federation:
    versions: dict[str, int] = field(default_factory=dict)
    sent: list[tuple] = field(default_factory=list)
    result: BroadcastResult | None = None
    boom: bool = False

    async def space_member_supports(self, instance_id, *, min_version):
        return self.versions.get(instance_id, 74) >= min_version

    async def broadcast_to_space_members(
        self, space_id, event_type, payload, *, only_instances=None
    ):
        if self.boom:
            raise RuntimeError("transport down")
        self.sent.append((space_id, event_type, payload, only_instances))
        return self.result or BroadcastResult(attempted=1, succeeded=1, failed=0)


class _Convos:
    def __init__(self, conv: Conversation | None, seats) -> None:
        self.conv = conv
        self.seats = seats

    async def get(self, conversation_id):
        return self.conv

    async def list_remote_members(self, conversation_id):
        return self.seats


class _Listings:
    def __init__(self, listing) -> None:
        self.listing = listing

    async def get_listing(self, post_id):
        return self.listing


def _thread(conv_id: str = "bzt-1") -> Conversation:
    return Conversation(
        id=conv_id,
        type=ConversationType.DM,
        created_at=NOW,
        bazaar_listing_id=LISTING.post_id,
    )


def _seat(instance: str, user_id: str) -> RemoteConversationMember:
    return RemoteConversationMember(
        conversation_id="bzt-1",
        instance_id=instance,
        remote_username=user_id,
        joined_at=NOW.isoformat(),
        user_id=user_id,
        display_name="X",
    )


def _event(sender: str, **over) -> DmMessageCreated:
    fields = {
        "conversation_id": "bzt-1",
        "message_id": "ab" * 16,
        "sender_user_id": sender,
        "sender_display_name": "Name",
        "recipient_user_ids": (),
        "content": "Is it still there?",
        "occurred_at": NOW,
    }
    fields.update(over)
    return DmMessageCreated(**fields)


def _outbound(fed, conv=None, seats=(), listing=LISTING) -> BazaarThreadOutbound:
    out = BazaarThreadOutbound(
        bus=EventBus(),
        federation_service=fed,
        conversation_repo=_Convos(conv, list(seats)),
        bazaar_repo=_Listings(listing),
    )
    out.wire()
    return out


async def test_a_buyers_question_goes_to_the_sellers_household_only():
    fed = _Federation()
    out = _outbound(fed, _thread(), [_seat(SELLER_HOUSE, "u-sam")])
    await out._on_created(_event("u-ben"))
    assert len(fed.sent) == 1
    space_id, event_type, payload, only = fed.sent[0]
    assert (space_id, event_type, only) == (
        "sp",
        FET.BAZAAR_THREAD_MESSAGE,
        {SELLER_HOUSE},
    )
    assert payload == {
        "space_id": "sp",
        "listing_post_id": "post-bike",
        "buyer_user_id": "u-ben",
        "author_user_id": "u-ben",
        "message_id": "ab" * 16,
        "content": "Is it still there?",
        "reply_to_id": None,
        "created_at": NOW.isoformat(),
    }
    assert "conversation_id" not in payload


async def test_the_sellers_answer_names_the_buyer():
    fed = _Federation()
    out = _outbound(fed, _thread(), [_seat(BUYER_HOUSE, "u-ben")])
    await out._on_created(_event("u-sam", content="Yes"))
    _sp, _et, payload, only = fed.sent[0]
    assert only == {BUYER_HOUSE}
    assert (payload["buyer_user_id"], payload["author_user_id"]) == ("u-ben", "u-sam")


@pytest.mark.parametrize(
    ("label", "kwargs", "event_over"),
    [
        ("an inbound message is never echoed", {}, {"origin_instance_id": "house-x"}),
        ("a system chat is not ours", {}, {"system_scope": "space"}),
        ("a plain DM", {"conv": "plain"}, {}),
        ("an unknown conversation", {"conv": None}, {}),
        ("its listing is gone", {"listing": None}, {}),
        ("no remote seat", {"seats": []}, {}),
    ],
)
async def test_nothing_is_sent(label, kwargs, event_over):
    fed = _Federation()
    conv = kwargs.get("conv", _thread())
    if conv == "plain":
        conv = Conversation(id="bzt-1", type=ConversationType.DM, created_at=NOW)
    out = _outbound(
        fed,
        conv,
        kwargs.get("seats", [_seat(SELLER_HOUSE, "u-sam")]),
        kwargs.get("listing", LISTING),
    )
    await out._on_created(_event("u-ben", **event_over))
    assert fed.sent == [], label


async def test_an_older_household_is_sent_nothing():
    fed = _Federation(
        versions={SELLER_HOUSE: FederationCapability.MIN_FOR_BAZAAR_THREAD - 1}
    )
    out = _outbound(fed, _thread(), [_seat(SELLER_HOUSE, "u-sam")])
    await out._on_created(_event("u-ben"))
    assert fed.sent == []


async def test_failures_are_logged_never_raised(caplog):
    caplog.set_level(logging.WARNING)
    seats = [_seat(SELLER_HOUSE, "u-sam")]
    # The household is no member of the space (any more): nothing attempted.
    fed = _Federation(result=BroadcastResult(attempted=0, succeeded=0, failed=0))
    await _outbound(fed, _thread(), seats)._on_created(_event("u-ben"))
    assert "no member household" in caplog.text
    # A terminal failure on the way.
    fed = _Federation(
        result=BroadcastResult(
            attempted=1,
            succeeded=0,
            failed=1,
            results=(
                DeliveryResult(instance_id=SELLER_HOUSE, ok=False, error="no_route"),
            ),
        )
    )
    await _outbound(fed, _thread(), seats)._on_created(_event("u-ben"))
    assert "not delivered (no_route)" in caplog.text
    # A transport that raises.
    fed = _Federation(boom=True)
    await _outbound(fed, _thread(), seats)._on_created(_event("u-ben"))
    assert "failed" in caplog.text
    # Two seats is no thread this code made: refused, logged.
    fed = _Federation()
    await _outbound(fed, _thread(), [*seats, _seat(BUYER_HOUSE, "u-ben")])._on_created(
        _event("u-ben")
    )
    assert fed.sent == [] and "2 remote seats" in caplog.text
