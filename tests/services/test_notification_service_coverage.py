"""Coverage fill for NotificationService — bazaar events, remote invite
accepted/declined, space join denied."""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import (
    BazaarListingExpired,
    BazaarOfferAccepted,
    BazaarOfferRejected,
    RemoteSpaceInviteAccepted,
    RemoteSpaceInviteDeclined,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.notification_service import NotificationService
from socialhome.services.user_service import UserService


@pytest.fixture
async def stack(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    bus = EventBus()
    user_repo = SqliteUserRepo(db)
    space_repo = SqliteSpaceRepo(db)
    notif_repo = SqliteNotificationRepo(db, max_per_user=50)
    user_svc = UserService(user_repo, bus, own_instance_public_key=kp.public_key)
    notif_svc = NotificationService(notif_repo, user_repo, space_repo, bus)
    notif_svc.wire()

    class S:
        pass

    s = S()
    s.db = db
    s.bus = bus
    s.notif_repo = notif_repo
    s.space_repo = space_repo
    s.user_repo = user_repo
    s.user_svc = user_svc
    s.notif_svc = notif_svc
    yield s
    await db.shutdown()


async def _user(s, username):
    return await s.user_svc.provision(username=username, display_name=username)


# ── Bazaar events ───────────────────────────────────────────────────


async def test_bazaar_offer_accepted_notifies_buyer(stack):
    buyer = await _user(stack, "buyer")
    await stack.notif_svc.on_bazaar_offer_accepted(
        BazaarOfferAccepted(
            listing_post_id="p1",
            seller_user_id="seller-id",
            buyer_user_id=buyer.user_id,
            price=100,
        )
    )
    notifs = await stack.notif_repo.list(buyer.user_id, limit=10)
    assert any(n.type == "bazaar_offer_accepted" for n in notifs)


async def test_bazaar_offer_rejected_notifies_bidder(stack):
    bidder = await _user(stack, "bidder")
    await stack.notif_svc.on_bazaar_offer_rejected(
        BazaarOfferRejected(
            listing_post_id="p1",
            seller_user_id="seller-id",
            bidder_user_id=bidder.user_id,
            bid_id="b1",
            reason="too low",
        )
    )
    notifs = await stack.notif_repo.list(bidder.user_id, limit=10)
    assert any(n.type == "bazaar_offer_rejected" for n in notifs)


async def test_bazaar_listing_expired_sold(stack):
    seller = await _user(stack, "seller")
    await stack.notif_svc.on_bazaar_listing_expired(
        BazaarListingExpired(
            listing_post_id="p1",
            seller_user_id=seller.user_id,
            final_status="sold",
        )
    )
    notifs = await stack.notif_repo.list(seller.user_id, limit=10)
    assert any(n.type == "bazaar_listing_sold" for n in notifs)


async def test_bazaar_listing_expired_no_buyer(stack):
    seller = await _user(stack, "seller")
    await stack.notif_svc.on_bazaar_listing_expired(
        BazaarListingExpired(
            listing_post_id="p1",
            seller_user_id=seller.user_id,
            final_status="expired",
        )
    )
    notifs = await stack.notif_repo.list(seller.user_id, limit=10)
    assert any(n.type == "bazaar_listing_expired" for n in notifs)


# ── Remote invite accepted/declined ────────────────────────────────


async def _seed_space_with_admin(stack, *, sid="sp1"):
    admin = await _user(stack, "alicehost")
    await stack.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?, 'S', 'inst', 'alicehost', ?)",
        (sid, "ab" * 32),
    )
    await stack.db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'admin')",
        (sid, admin.user_id),
    )
    return sid, admin


async def test_remote_invite_accepted_notifies_admins(stack):
    sid, admin = await _seed_space_with_admin(stack)
    await stack.notif_svc.on_remote_invite_accepted(
        RemoteSpaceInviteAccepted(
            space_id=sid,
            instance_id="peer",
            invitee_user_id="u-remote",
        )
    )
    notifs = await stack.notif_repo.list(admin.user_id, limit=10)
    assert any(n.type == "space_remote_invite_accepted" for n in notifs)


async def test_remote_invite_accepted_unknown_space_noop(stack):
    await stack.notif_svc.on_remote_invite_accepted(
        RemoteSpaceInviteAccepted(
            space_id="ghost-space",
            instance_id="peer",
            invitee_user_id="u",
        )
    )
    # No crash; no notifications anywhere.
    assert (await stack.notif_repo.list("anyone", limit=10)) == []


async def test_remote_invite_declined_notifies_admins(stack):
    sid, admin = await _seed_space_with_admin(stack, sid="sp2")
    await stack.notif_svc.on_remote_invite_declined(
        RemoteSpaceInviteDeclined(
            space_id=sid,
            instance_id="peer",
            invitee_user_id="u-remote",
        )
    )
    notifs = await stack.notif_repo.list(admin.user_id, limit=10)
    assert any(n.type == "space_remote_invite_declined" for n in notifs)


async def test_remote_invite_declined_unknown_space_noop(stack):
    await stack.notif_svc.on_remote_invite_declined(
        RemoteSpaceInviteDeclined(
            space_id="ghost",
            instance_id="peer",
            invitee_user_id="u",
        )
    )


async def test_remote_invite_skips_non_admin_members(stack):
    sid, admin = await _seed_space_with_admin(stack, sid="sp3")
    # Add a non-admin member whom we should skip.
    member = await _user(stack, "member")
    await stack.db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
        (sid, member.user_id),
    )
    await stack.notif_svc.on_remote_invite_accepted(
        RemoteSpaceInviteAccepted(
            space_id=sid,
            instance_id="peer",
            invitee_user_id="u-remote",
        )
    )
    admin_notifs = await stack.notif_repo.list(admin.user_id, limit=10)
    member_notifs = await stack.notif_repo.list(member.user_id, limit=10)
    assert admin_notifs  # admin got it
    # member did not
    assert not any(n.type == "space_remote_invite_accepted" for n in member_notifs)
