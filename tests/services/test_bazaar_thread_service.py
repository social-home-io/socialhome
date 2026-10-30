"""Tests for socialhome.services.bazaar_thread_service — opening, listing
and the live write gate of "Ask the seller" threads (v_74).

A real app over a real SQLite database; the seller's household (``house-s``)
is another household, known only through the space roster.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    bazaar_thread_service_key,
    conversation_repo_key,
    db_key,
    dm_service_key,
    federation_repo_key,
    federation_service_key,
)
from socialhome.config import Config
from socialhome.domain.bazaar_thread import (
    ListingThreadClosedError,
    ListingThreadUnsupportedError,
    listing_thread_id,
)
from socialhome.domain.child_protection import AccountProtectedError
from socialhome.domain.conversation import Conversation, ConversationType
from socialhome.domain.federation import FederationEventType
from socialhome.federation.federation_service import FederationService
from socialhome.services.bazaar_service import ListingNotFoundError, OwnListingError
from socialhome.services.dm_service import RecipientBlockedError

SP = "sp-street"
BIKE = "post-bike"
OWN = "post-own"
HOUSE_S = "house-s"


def _config(tmp_dir) -> Config:
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "t.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://t.example"})}
        ),
    )


@pytest.fixture
async def env(aiohttp_client, tmp_dir, monkeypatch):
    app = create_app(_config(tmp_dir))
    await aiohttp_client(app)
    db = app[db_key]
    far = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    for sql, params in (
        (
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            ("ben", "u-ben", "Ben"),
        ),
        (
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            ("fay", "u-fay", "Fay"),
        ),
        (
            (
                "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
                " identity_public_key) VALUES(?, 'Street', ?, 'hanna', ?)"
            ),
            (SP, HOUSE_S, "ab" * 32),
        ),
        (
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
            (SP, "u-ben", "member"),
        ),
        (
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,?)",
            (SP, "u-fay", "subscriber"),
        ),
        (
            "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
            (SP, HOUSE_S),
        ),
        (
            (
                "INSERT INTO space_remote_members(space_id, instance_id, user_id,"
                " role, display_name) VALUES(?,?,?,?,?)"
            ),
            (SP, HOUSE_S, "u-sam", "member", "Sam"),
        ),
        *[
            (
                (
                    "INSERT INTO space_posts(id, space_id, author, type, content)"
                    " VALUES(?,?,?, 'bazaar', 'x')"
                ),
                (pid, SP, seller),
            )
            for pid, seller in ((BIKE, "u-sam"), (OWN, "u-ben"))
        ],
        *[
            (
                (
                    "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id,"
                    " mode, title, end_time, currency) VALUES(?,?,?, 'fixed', ?, ?, 'EUR')"
                ),
                (pid, SP, seller, title, far),
            )
            for pid, seller, title in ((BIKE, "u-sam", "Bike"), (OWN, "u-ben", "Lamp"))
        ],
    ):
        await db.enqueue(sql, params)
    versions = {HOUSE_S: 74}

    async def _supports(self, instance_id, *, min_version):
        return versions.get(instance_id, 0) >= min_version

    monkeypatch.setattr(FederationService, "space_member_supports", _supports)
    assert app[federation_service_key] is not None
    return app, db, versions


async def test_ask_opens_a_thread_with_a_seller_of_another_household(env):
    app, _db, _ = env
    svc = app[bazaar_thread_service_key]
    result = await svc.ask(BIKE, username="ben")
    assert result.kind == "thread"
    assert result.conversation_id == listing_thread_id(BIKE, "u-ben")
    seats = await app[conversation_repo_key].list_remote_members(result.conversation_id)
    assert [(s.instance_id, s.user_id, s.display_name) for s in seats] == [
        (HOUSE_S, "u-sam", "Sam")
    ]
    # Listed for ben, with sam as the other person; still open.
    [(conv, info)] = await svc.list_threads(BIKE, username="ben")
    assert conv.id == result.conversation_id
    assert (info.role, info.other_user_id, info.writable) == ("buyer", "u-sam", True)
    assert info.closes_at is None
    # A thread ben hid comes back when he asks again.
    await app[dm_service_key].leave(conv.id, username="ben")
    assert await svc.list_threads(BIKE, username="ben") == []
    again = await svc.ask(BIKE, username="ben")
    assert again.conversation_id == conv.id
    assert len(await svc.list_threads(BIKE, username="ben")) == 1


async def test_ask_refusals(env):
    app, db, versions = env
    svc = app[bazaar_thread_service_key]
    with pytest.raises(ListingNotFoundError):
        await svc.ask("no-such", username="ben")
    with pytest.raises(OwnListingError):
        await svc.ask(OWN, username="ben")
    with pytest.raises(KeyError):
        await svc.ask(BIKE, username="nobody")
    # An older seller's household.
    versions[HOUSE_S] = 73
    with pytest.raises(ListingThreadUnsupportedError):
        await svc.ask(BIKE, username="ben")
    versions[HOUSE_S] = 74
    # A block either way.
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?,?)",
        ("u-sam", "u-ben"),
    )
    with pytest.raises(RecipientBlockedError):
        await svc.ask(BIKE, username="ben")
    await db.enqueue("DELETE FROM user_blocks", ())
    await db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?,?)",
        ("u-ben", "u-sam"),
    )
    with pytest.raises(RecipientBlockedError):
        await svc.ask(BIKE, username="ben")
    await db.enqueue("DELETE FROM user_blocks", ())
    # The seller left the space: nobody to ask.
    await db.enqueue("UPDATE space_remote_members SET tombstoned=1", ())
    with pytest.raises(ListingNotFoundError):
        await svc.ask(BIKE, username="ben")
    await db.enqueue("UPDATE space_remote_members SET tombstoned=0", ())
    # The space's Bazaar switched off, or the space archived.
    await db.enqueue("UPDATE spaces SET feature_bazaar=0", ())
    with pytest.raises(ListingThreadClosedError):
        await svc.ask(BIKE, username="ben")
    await db.enqueue("UPDATE spaces SET feature_bazaar=1, archived=1", ())
    with pytest.raises(ListingThreadClosedError):
        await svc.ask(BIKE, username="ben")
    await db.enqueue("UPDATE spaces SET archived=0", ())
    # Ended more than 14 days ago.
    long_ago = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    await db.enqueue(
        "UPDATE bazaar_listings SET status='expired', end_time=? WHERE post_id=?",
        (long_ago, BIKE),
    )
    with pytest.raises(ListingThreadClosedError):
        await svc.ask(BIKE, username="ben")


async def test_a_protected_account_cannot_ask(env):
    app, db, _ = env
    await db.enqueue(
        "UPDATE users SET child_protection_enabled=1 WHERE username='ben'", ()
    )
    with pytest.raises(AccountProtectedError):
        await app[bazaar_thread_service_key].ask(BIKE, username="ben")


async def test_a_paired_seller_gets_the_normal_dm(env):
    app, db, _ = env
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url, local_inbox_id,"
        " status, source) VALUES(?, 'S', ?, '00', '00', 'https://s/x', 's',"
        " 'confirmed', 'manual')",
        (HOUSE_S, "ab" * 32),
    )
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES('u-sam', ?, 'sam', 'Sam')",
        (HOUSE_S,),
    )
    result = await app[bazaar_thread_service_key].ask(BIKE, username="ben")
    assert result.kind == "dm"
    # A space-only relationship (an invite link) is no pairing.
    await db.enqueue("UPDATE remote_instances SET source='space_session'", ())
    svc = app[bazaar_thread_service_key]
    assert not await svc._paired_with_seller("u-sam")
    assert await svc._paired_with_seller("u-nobody") is False


async def test_require_write_follows_the_seats_and_the_clock(env):
    app, db, _ = env
    svc = app[bazaar_thread_service_key]
    conv_id = (await svc.ask(BIKE, username="ben")).conversation_id
    conv = await app[conversation_repo_key].get(conv_id)
    await svc.require_write(conv, "u-ben")
    # Ben demoted to follower: no more asking.
    await db.enqueue(
        "UPDATE space_members SET role='subscriber' WHERE user_id='u-ben'", ()
    )
    with pytest.raises(PermissionError):
        await svc.require_write(conv, "u-ben")
    await db.enqueue("UPDATE space_members SET role='member' WHERE user_id='u-ben'", ())
    # Sam banned from the space: nobody to talk to.
    await db.enqueue(
        "INSERT INTO space_bans(space_id, user_id, banned_by) VALUES(?, 'u-sam', 'x')",
        (SP,),
    )
    with pytest.raises(PermissionError):
        await svc.require_write(conv, "u-ben")
    await db.enqueue("DELETE FROM space_bans", ())
    # The space archived: read-only here as everywhere.
    await db.enqueue("UPDATE spaces SET archived=1", ())
    with pytest.raises(ListingThreadClosedError):
        await svc.require_write(conv, "u-ben")
    await db.enqueue("UPDATE spaces SET archived=0", ())
    # Ben left the space.
    await db.enqueue("DELETE FROM space_members WHERE user_id='u-ben'", ())
    with pytest.raises(PermissionError):
        await svc.require_write(conv, "u-ben")
    # A thread whose listing is gone, or no thread at all.
    stray = Conversation(id="x", type=ConversationType.DM, created_at=datetime.now(UTC))
    with pytest.raises(ListingThreadClosedError):
        await svc.require_write(stray, "u-ben")
    assert await svc.info(stray, "u-ben") is None


async def test_the_seller_side_needs_a_writer_buyer(env):
    """On the seller's household the roles flip: the seller writes to a
    buyer who must still hold a writer seat."""
    app, db, _ = env
    svc = app[bazaar_thread_service_key]
    convos = app[conversation_repo_key]
    # Ben sells the lamp here; sam (house-s) asked about it.
    conv = await convos.open_listing_thread(
        conversation_id=listing_thread_id(OWN, "u-sam"),
        listing_post_id=OWN,
        username="ben",
        remote_instance_id=HOUSE_S,
        remote_user_id="u-sam",
        remote_display_name="Sam",
    )
    await svc.require_write(conv, "u-ben")
    info = await svc.info(conv, "u-ben")
    assert info is not None and info.role == "seller"
    await db.enqueue("UPDATE space_remote_members SET role='subscriber'", ())
    with pytest.raises(PermissionError):
        await svc.require_write(conv, "u-ben")


async def test_without_federation_no_thread_can_open(aiohttp_client, tmp_dir):
    """Fail closed: the service never opens a thread it can't deliver."""
    app = create_app(_config(tmp_dir))
    svc = app[bazaar_thread_service_key]
    assert svc._federation is None
    assert app[federation_repo_key] is not None
    assert FederationEventType.BAZAAR_THREAD_MESSAGE.value == "bazaar_thread_message"
