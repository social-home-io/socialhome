"""Tests for socialhome.services.federation_inbound.bazaar_thread — every
refusal of an inbound "Ask the seller" message, and what an accepted one
stores and publishes. Real SQLite repos; the event is handed straight to the
handler (the §24.11 gates in front of it are covered in
``tests/protocol/test_bazaar_thread_federation.py``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.bazaar_thread import LISTING_THREAD_RATE_CAPS, listing_thread_id
from socialhome.domain.events import DmMessageCreated
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.bazaar_repo import SqliteBazaarRepo
from socialhome.repositories.conversation_repo import SqliteConversationRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.federation_inbound import BazaarThreadInboundHandlers

SP = "sp"
BIKE = "post-bike"  # sold by our sam
BOOK = "post-book"  # sold by u-zoe on house-z
HOUSE_B = "house-b"  # ben's
HOUSE_Z = "house-z"  # zoe's


class _Registry:
    def __init__(self) -> None:
        self.handlers: dict = {}

    def register(self, event_type, handler) -> None:
        self.handlers[event_type] = handler


class _Fed:
    def __init__(self) -> None:
        self._event_registry = _Registry()


@pytest.fixture
async def env(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    far = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    for sql, params in (
        (
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            ("sam", "u-sam", "Sam"),
        ),
        (
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            ("amy", "u-amy", "Amy"),
        ),
        (
            (
                "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
                " identity_public_key) VALUES(?, 'Street', 'h', 'h', ?)"
            ),
            (SP, "ab" * 32),
        ),
        (
            (
                "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
                " identity_public_key) VALUES('sp-2', 'Other', 'h', 'h', ?)"
            ),
            ("ab" * 32,),
        ),
        (
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
            (SP, "u-sam", "member"),
        ),
        (
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
            (SP, "u-amy", "member"),
        ),
        *[
            (
                (
                    "INSERT INTO space_remote_members(space_id, instance_id, user_id,"
                    " role, display_name) VALUES(?,?,?,?,?)"
                ),
                (SP, house, uid, role, uid[2:].title()),
            )
            for house, uid, role in (
                (HOUSE_B, "u-ben", "member"),
                (HOUSE_B, "u-fay", "subscriber"),
                (HOUSE_Z, "u-zoe", "member"),
            )
        ],
        *[
            (
                (
                    "INSERT INTO space_posts(id, space_id, author, type, content)"
                    " VALUES(?,?,?, 'bazaar', 'x')"
                ),
                (pid, space, seller),
            )
            for pid, space, seller in (
                (BIKE, SP, "u-sam"),
                (BOOK, SP, "u-zoe"),
                ("post-far", "sp-2", "u-sam"),
            )
        ],
        *[
            (
                (
                    "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id,"
                    " mode, title, end_time, currency) VALUES(?,?,?, 'fixed', 'x', ?,"
                    " 'EUR')"
                ),
                (pid, space, seller, far),
            )
            for pid, space, seller in (
                (BIKE, SP, "u-sam"),
                (BOOK, SP, "u-zoe"),
                ("post-far", "sp-2", "u-sam"),
            )
        ],
    ):
        await db.enqueue(sql, params)
    bus = EventBus()
    published: list[DmMessageCreated] = []

    async def _seen(event: DmMessageCreated) -> None:
        published.append(event)

    bus.subscribe(DmMessageCreated, _seen)
    convos = SqliteConversationRepo(db)
    handlers = BazaarThreadInboundHandlers(
        bus=bus,
        bazaar_repo=SqliteBazaarRepo(db),
        space_repo=SqliteSpaceRepo(db),
        remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
        conversation_repo=convos,
        user_repo=SqliteUserRepo(db),
    )
    fed = _Fed()
    handlers.attach_to(fed)
    assert FederationEventType.BAZAAR_THREAD_MESSAGE in fed._event_registry.handlers
    yield db, convos, handlers, published
    await db.shutdown()


_N = iter(range(1, 10_000))


def _event(sender: str = HOUSE_B, **over) -> FederationEvent:
    payload = {
        "space_id": SP,
        "listing_post_id": BIKE,
        "buyer_user_id": "u-ben",
        "author_user_id": "u-ben",
        "message_id": f"{next(_N):032x}",
        "content": "still there?",
        "created_at": datetime.now(UTC).isoformat(),
    }
    payload.update(over)
    return FederationEvent(
        msg_id="m",
        event_type=FederationEventType.BAZAAR_THREAD_MESSAGE,
        from_instance=sender,
        to_instance="us",
        timestamp=datetime.now(UTC).isoformat(),
        payload=payload,
        space_id=SP,
    )


async def test_a_question_opens_the_thread_and_rings_the_seller(env):
    _db, convos, handlers, published = env
    future = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    assert await handlers.apply(_event(created_at=future))
    conv_id = listing_thread_id(BIKE, "u-ben")
    conv = await convos.get(conv_id)
    assert conv is not None and conv.bazaar_listing_id == BIKE
    [seat] = await convos.list_remote_members(conv_id)
    assert (seat.instance_id, seat.user_id, seat.display_name) == (
        HOUSE_B,
        "u-ben",
        "Ben",
    )
    [msg] = await convos.list_messages(conv_id, limit=5)
    assert msg.created_at <= datetime.now(UTC)  # a future stamp is clamped
    [event] = published
    assert event.recipient_user_ids == ("u-sam",)
    assert event.origin_instance_id == HOUSE_B
    assert event.sender_display_name == "Ben"
    # A reply to a message of the same thread is kept; one naming any
    # other message is dropped (the message itself still lands).
    assert await handlers.apply(_event(reply_to_id=msg.id, content="and?"))
    assert await handlers.apply(_event(reply_to_id="nope", content="again"))
    replies = {
        m.content: m.reply_to_id for m in await convos.list_messages(conv_id, limit=9)
    }
    assert replies["and?"] == msg.id and replies["again"] is None


async def test_the_sellers_answer_lands_in_a_thread_we_opened(env):
    _db, convos, handlers, published = env
    # amy asked zoe (house-z) about the book: the thread exists here.
    conv_id = listing_thread_id(BOOK, "u-amy")
    await convos.open_listing_thread(
        conversation_id=conv_id,
        listing_post_id=BOOK,
        username="amy",
        remote_instance_id=HOUSE_Z,
        remote_user_id="u-zoe",
        remote_display_name="Zoe",
    )
    ev = _event(
        HOUSE_Z,
        listing_post_id=BOOK,
        buyer_user_id="u-amy",
        author_user_id="u-zoe",
        content="yes",
    )
    assert await handlers.apply(ev)
    assert published[-1].recipient_user_ids == ("u-amy",)
    # A replay of the same message id is a no-op.
    assert not await handlers.apply(ev)


@pytest.mark.parametrize(
    ("label", "sender", "over"),
    [
        ("incomplete", HOUSE_B, {"content": ""}),
        ("not a string", HOUSE_B, {"buyer_user_id": 7}),
        ("malformed id", HOUSE_B, {"message_id": "not-hex"}),
        ("too long", HOUSE_B, {"content": "x" * 1001}),
        ("space not held", HOUSE_B, {"space_id": "sp-gone"}),
        ("listing of another space", HOUSE_B, {"listing_post_id": "post-far"}),
        ("unknown listing", HOUSE_B, {"listing_post_id": "post-none"}),
        (
            "asking oneself",
            HOUSE_B,
            {"buyer_user_id": "u-sam", "author_user_id": "u-sam"},
        ),
        (
            "a follower asks",
            HOUSE_B,
            {"buyer_user_id": "u-fay", "author_user_id": "u-fay"},
        ),
        ("another household speaks for ben", HOUSE_Z, {}),
        ("a third person", HOUSE_B, {"author_user_id": "u-zoe"}),
        (
            "a seller cold-opens",
            HOUSE_Z,
            {
                "listing_post_id": BOOK,
                "buyer_user_id": "u-amy",
                "author_user_id": "u-zoe",
            },
        ),
        (
            "the seller is not on the sender",
            HOUSE_B,
            {
                "listing_post_id": BOOK,
                "buyer_user_id": "u-amy",
                "author_user_id": "u-zoe",
            },
        ),
        (
            "the buyer does not live here",
            HOUSE_Z,
            {
                "listing_post_id": BOOK,
                "buyer_user_id": "u-ben",
                "author_user_id": "u-zoe",
            },
        ),
    ],
)
async def test_refusals(env, label, sender, over):
    db, _convos, handlers, published = env
    assert not await handlers.apply(_event(sender, **over)), label
    assert published == [], label
    assert await db.fetchone("SELECT 1 FROM conversation_messages", ()) is None, label


async def test_state_refusals(env):
    db, _convos, handlers, published = env
    # The Bazaar switched off in the space.
    await db.enqueue("UPDATE spaces SET feature_bazaar=0 WHERE id=?", (SP,))
    assert not await handlers.apply(_event())
    await db.enqueue("UPDATE spaces SET feature_bazaar=1 WHERE id=?", (SP,))
    # Ben banned from the space.
    await db.enqueue(
        "INSERT INTO space_bans(space_id, user_id, banned_by) VALUES(?, 'u-ben', 'x')",
        (SP,),
    )
    assert not await handlers.apply(_event())
    await db.enqueue("DELETE FROM space_bans", ())
    # Our seller left the space.
    await db.enqueue("DELETE FROM space_members WHERE user_id='u-sam'", ())
    assert not await handlers.apply(_event())
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, 'u-sam', 'member')",
        (SP,),
    )
    # A personal block, either way.
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id)"
        " VALUES('u-sam', 'u-ben')",
        (),
    )
    assert not await handlers.apply(_event())
    await db.enqueue("DELETE FROM user_blocks", ())
    # Long closed.
    await db.enqueue(
        "UPDATE bazaar_listings SET status='sold', sold_at=? WHERE post_id=?",
        ((datetime.now(UTC) - timedelta(days=30)).isoformat(), BIKE),
    )
    assert not await handlers.apply(_event())
    assert published == []
    # An id already held in another conversation is never overwritten.
    await db.enqueue(
        "UPDATE bazaar_listings SET status='active', sold_at=NULL WHERE post_id=?",
        (BIKE,),
    )
    await db.enqueue("INSERT INTO conversations(id, type) VALUES('dm-x', 'dm')", ())
    await db.enqueue(
        "INSERT INTO conversation_messages(id, conversation_id, sender_user_id,"
        " content) VALUES(?, 'dm-x', 'u-amy', 'private')",
        ("ff" * 16,),
    )
    assert not await handlers.apply(_event(message_id="ff" * 16, content="mine"))
    row = await db.fetchone(
        "SELECT conversation_id, content FROM conversation_messages WHERE id=?",
        ("ff" * 16,),
    )
    assert (row["conversation_id"], row["content"]) == ("dm-x", "private")


async def test_an_event_naming_no_space_is_refused(env):
    _db, _convos, handlers, published = env
    ev = _event(space_id="")
    ev = FederationEvent(
        msg_id=ev.msg_id,
        event_type=ev.event_type,
        from_instance=ev.from_instance,
        to_instance=ev.to_instance,
        timestamp=ev.timestamp,
        payload=ev.payload,
        space_id=None,
    )
    assert not await handlers.apply(ev)
    assert published == []


async def test_registered_handler_runs_apply(env):
    _db, _convos, handlers, published = env
    await handlers._on_message(_event())
    assert len(published) == 1


async def test_the_receiver_caps_one_buyer_per_listing(env):
    """The hourly cap on our side too: a household that ignores it is
    refused here, whatever ``created_at`` it claims."""
    _db, _convos, handlers, published = env
    (_hour, per_hour), _ = LISTING_THREAD_RATE_CAPS
    for _ in range(per_hour):
        assert await handlers.apply(_event())
    assert not await handlers.apply(_event())
    # Backdating the messages doesn't slip past: the receiver counts what
    # arrived, not only what the stamps claim.
    long_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    assert not await handlers.apply(_event(created_at=long_ago))
    # Another buyer about the same listing has their own allowance — and
    # so does ben about another listing.
    assert await handlers.apply(
        _event(sender=HOUSE_Z, buyer_user_id="u-zoe", author_user_id="u-zoe")
    )
    assert len(published) == per_hour + 1


async def test_the_receiver_counts_what_it_holds_across_restarts(env):
    """The stored messages count too — a restart doesn't reset the cap."""
    db, convos, handlers, _published = env
    (_hour, per_hour), _ = LISTING_THREAD_RATE_CAPS
    for _ in range(per_hour):
        assert await handlers.apply(_event())
    fresh = BazaarThreadInboundHandlers(
        bus=EventBus(),
        bazaar_repo=SqliteBazaarRepo(db),
        space_repo=SqliteSpaceRepo(db),
        remote_member_repo=SqliteSpaceRemoteMemberRepo(db),
        conversation_repo=convos,
        user_repo=SqliteUserRepo(db),
    )
    assert not await fresh.apply(_event())
