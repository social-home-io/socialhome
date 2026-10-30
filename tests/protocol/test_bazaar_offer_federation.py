"""§27.9 release blocker: bazaar offers between households (v_73).

Marked ``@pytest.mark.security``.

Five households; space ``SP`` is hosted on **a**:

* **a** — anna, the owner, sells a negotiable listing;
* **b** — bob, a member: the buyer;
* **c** — cara, a member who also makes an offer (an uninvolved third
  member household for bob's offer);
* **d** — dora, who only *follows* the space (a follower-only household);
* **e** — eve, paired with a and b but no member of the space at all.

Real crypto (per-pair session keys), real SQLite, the real §24.11 inbound
pipeline (signature, replay, the space writer gate), the real bazaar
service + outbound + inbound handlers and the real notification service on
every household. The stand-in is the network: a loopback HTTPS inbox that
hands each envelope to the addressed household's
``handle_inbound_envelope`` and records what crossed the wire.

Proves: an offer (and its message) made on b reaches the seller's
household only; the seller's accept / reject reaches the buyer's household
only, the sale reaches every member household; the right person is told on
the right side; no offer field rides outside the ciphertext; a follower, a
forged offerer, a forged seller answer and a replay are refused; an older
seller household is sent nothing.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.child_protection import AccountProtectedError
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import OURS, FederationCapability
from socialhome.domain.post import BazaarListing, BazaarMode, BazaarStatus
from socialhome.domain.space import SpacePermissionError
from socialhome.federation.federation_service import FederationService
from socialhome.federation.space_authorship import SpaceAuthorship
from socialhome.federation.transport import FederationTransport
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.bazaar_repo import OfferStateError, SqliteBazaarRepo
from socialhome.repositories.calendar_repo import SqliteSpaceCalendarRepo
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.notification_repo import SqliteNotificationRepo
from socialhome.repositories.outbox_repo import SqliteOutboxRepo
from socialhome.repositories.page_repo import SqlitePageRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.sticky_repo import SqliteStickyRepo
from socialhome.repositories.task_repo import SqliteSpaceTaskRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.bazaar_outbound import BazaarOutbound
from socialhome.services.bazaar_service import BazaarService
from socialhome.services.federation_inbound.space_content import (
    SpaceContentInboundHandlers,
)
from socialhome.services.notification_service import NotificationService
from socialhome.services.space_member_guard import require_seat
from socialhome.services.space_service import SpaceService

pytestmark = pytest.mark.security

FET = FederationEventType
SP = "sp-market"
LISTING = "post-anna-bike"

#: household → its local users and their space roles (``None``: no seat).
ROSTER: dict[str, dict[str, str | None]] = {
    "a": {"anna": "owner"},
    "b": {"bob": "member"},
    "c": {"cara": "member"},
    "d": {"dora": "subscriber"},
    "e": {"eve": None},
}
MEMBER_HOUSES = ("a", "b", "c", "d")

#: Long and unique, so finding it in a wire capture can only mean a leak.
SECRET = "can you hold the blue bike until Saturday, I pay cash at the door"


class _LoopbackInbox:
    """The HTTPS inbox leg: straight into the addressed household's §24.11
    pipeline, recording every envelope that crossed the wire."""

    def __init__(self, world: dict[str, SimpleNamespace], me: str) -> None:
        self._world = world
        self._me = me
        self.posted: list[tuple[str, dict]] = []

    async def send(self, *, instance, envelope_dict):
        peer = next(h for h in self._world.values() if h.iid == instance.id)
        me = self._world[self._me]
        self.posted.append((peer.name, envelope_dict))
        try:
            await peer.federation.handle_inbound_envelope(
                f"{peer.name}-inbox-for-{me.name}",
                json.dumps(envelope_dict).encode(),
            )
        except ValueError:
            return False, 403
        return True, 200


async def _no_signal(*_a, **_kw):
    raise RuntimeError("no RTC signalling here")


class _Spaces:
    """The read / write gates :class:`BazaarService` asks the space service
    for — the real seat lookup and the real writer rule."""

    def __init__(self, spaces: SqliteSpaceRepo) -> None:
        self._spaces = spaces

    async def require_reader(self, space_id: str, user_id: str):
        return await require_seat(self._spaces, space_id, user_id)

    assert_writable_member = staticmethod(SpaceService.assert_writable_member)


async def _household(tmp_path, name: str, world: dict) -> SimpleNamespace:
    db = AsyncDatabase(tmp_path / f"{name}.db", batch_timeout_ms=10)
    await db.startup()
    ident = generate_identity_keypair()
    iid = derive_instance_id(ident.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, ident.private_key.hex(), ident.public_key.hex(), "aa" * 32),
    )
    km = KeyManager(os.urandom(32))
    bus = EventBus()
    fed_repo = SqliteFederationRepo(db)
    federation = FederationService(
        db,
        fed_repo,
        SqliteOutboxRepo(db),
        km,
        bus,
        iid,
        ident.private_key,
        ident.public_key,
    )
    inbox = _LoopbackInbox(world, name)
    transport = FederationTransport(
        own_instance_id=iid, https_inbox=inbox, signaling_send=_no_signal
    )
    transport.mark_ice_primed()
    federation.attach_transport(transport)
    users = SqliteUserRepo(db)
    spaces = SqliteSpaceRepo(db)
    seats = SqliteSpaceRemoteMemberRepo(db)
    federation.attach_space_write_gate(spaces, seats)
    bazaar_repo = SqliteBazaarRepo(db)
    bazaar = BazaarService(bazaar_repo, bus)
    bazaar.attach_spaces(_Spaces(spaces))
    BazaarOutbound(
        bus=bus,
        federation_service=federation,
        bazaar_repo=bazaar_repo,
        federation_repo=fed_repo,
        user_repo=users,
        remote_member_repo=seats,
    )
    SpaceContentInboundHandlers(
        bus=bus,
        authorship=SpaceAuthorship(
            space_repo=spaces, remote_member_repo=seats, user_repo=users
        ),
        post_repo=SqliteSpacePostRepo(db),
        page_repo=SqlitePageRepo(db),
        sticky_repo=SqliteStickyRepo(db),
        task_repo=SqliteSpaceTaskRepo(db),
        calendar_repo=SqliteSpaceCalendarRepo(db),
        bazaar_repo=bazaar_repo,
    ).attach_to(federation)
    notifs = SqliteNotificationRepo(db, max_per_user=50)
    NotificationService(notifs, users, spaces, bus).wire()
    h = SimpleNamespace(
        name=name,
        db=db,
        iid=iid,
        ident=ident,
        km=km,
        fed_repo=fed_repo,
        federation=federation,
        transport=transport,
        inbox=inbox,
        bazaar=bazaar,
        bazaar_repo=bazaar_repo,
        notifs=notifs,
    )
    world[name] = h
    return h


async def _pair(me, peer, *, peer_version: int = OURS) -> None:
    k_out, k_in = os.urandom(32), os.urandom(32)
    for side, other, out, inn in ((me, peer, k_out, k_in), (peer, me, k_in, k_out)):
        await side.fed_repo.save_instance(
            RemoteInstance(
                id=other.iid,
                display_name=f"{other.name} household",
                remote_identity_pk=other.ident.public_key.hex(),
                key_self_to_remote=side.km.encrypt(out),
                key_remote_to_self=side.km.encrypt(inn),
                remote_inbox_url=f"https://{other.name}.example/federation/inbox/x",
                local_inbox_id=f"{side.name}-inbox-for-{other.name}",
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.MANUAL,
            ),
        )
        version = peer_version if other is peer else OURS
        await side.fed_repo.set_proto_version(other.iid, version)
        side.transport._rtc_suppressed_until[other.iid] = float("inf")


async def _seed(world: dict[str, SimpleNamespace]) -> None:
    host = world["a"]
    end = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    for h in world.values():
        for house, people in ROSTER.items():
            for username in people:
                if house == h.name:
                    await h.db.enqueue(
                        "INSERT INTO users(username, user_id, display_name)"
                        " VALUES(?,?,?)",
                        (username, f"u-{username}", username.title()),
                    )
        if h.name not in MEMBER_HOUSES:
            continue
        await h.db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?, 'Market', ?, 'anna', ?)",
            (SP, host.iid, "ab" * 32),
        )
        for other_name in MEMBER_HOUSES:
            if other_name != h.name:
                await h.db.enqueue(
                    "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
                    (SP, world[other_name].iid),
                )
        for house in MEMBER_HOUSES:
            for username, role in ROSTER[house].items():
                if house == h.name:
                    await h.db.enqueue(
                        "INSERT INTO space_members(space_id, user_id, role)"
                        " VALUES(?,?,?)",
                        (SP, f"u-{username}", role),
                    )
                else:
                    # The roster mirror: a remote seat is never ``owner``.
                    await h.db.enqueue(
                        "INSERT INTO space_remote_members(space_id, instance_id,"
                        " user_id, role, display_name) VALUES(?,?,?,?,?)",
                        (
                            SP,
                            world[house].iid,
                            f"u-{username}",
                            "member" if role == "owner" else role,
                            username.title(),
                        ),
                    )
        # anna's listing, as BAZAAR_LISTING_CREATED left it on every
        # member household.
        await h.db.enqueue(
            "INSERT INTO space_posts(id, space_id, author, type, content)"
            " VALUES(?,?,?,?,?)",
            (LISTING, SP, "u-anna", "bazaar", ""),
        )
        await h.bazaar_repo.save_listing(
            BazaarListing(
                post_id=LISTING,
                space_id=SP,
                seller_user_id="u-anna",
                mode=BazaarMode.NEGOTIABLE,
                title="Blue bike",
                end_time=end,
                currency="EUR",
                status=BazaarStatus.ACTIVE,
                created_at=None,
                price=12000,
            ),
            space_id=SP,
        )


@pytest.fixture
async def world(tmp_path):
    households: dict[str, SimpleNamespace] = {}
    for name in ROSTER:
        await _household(tmp_path, name, households)
    a, b, c, d, e = (households[n] for n in "abcde")
    for x, y in ((a, b), (a, c), (a, d), (a, e), (b, c), (b, d), (b, e), (c, d)):
        await _pair(x, y)
    await _seed(households)
    yield SimpleNamespace(**households)
    for h in households.values():
        await h.db.shutdown()


async def _offers(h) -> list[tuple]:
    return [
        (r["id"], r["offerer_user_id"], r["amount"], r["message"], r["status"])
        for r in await h.db.fetchall(
            "SELECT id, offerer_user_id, amount, message, status FROM bazaar_offers"
            " ORDER BY created_at"
        )
    ]


async def _notif_types(h, user_id: str) -> list[str]:
    return [
        r["type"]
        for r in await h.db.fetchall(
            "SELECT type FROM notifications WHERE user_id=? ORDER BY created_at",
            (user_id,),
        )
    ]


async def _status(h) -> str:
    listing = await h.bazaar_repo.get_listing(LISTING)
    assert listing is not None
    return listing.status.value


def _traffic(world, *types: str) -> list[tuple[str, str, dict]]:
    """``(from, to, envelope)`` for every envelope of ``types`` on the wire."""
    out = []
    for h in (world.a, world.b, world.c, world.d, world.e):
        for to, env in h.inbox.posted:
            if env.get("event_type") in types:
                out.append((h.name, to, env))
    return out


# ── The round trip ────────────────────────────────────────────────────────


async def test_an_offer_from_another_household_reaches_only_the_seller(world):
    a, b, c, d, e = world.a, world.b, world.c, world.d, world.e
    offer = await b.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-bob", amount=10000, message=SECRET
    )
    # The seller's household holds it under the same id, message and all.
    assert await _offers(a) == [(offer.id, "u-bob", 10000, SECRET, "pending")]
    # anna is told; nobody else anywhere is.
    assert await _notif_types(a, "u-anna") == ["bazaar_bid_placed"]
    for h in (c, d, e):
        assert await _offers(h) == []
    # It crossed the wire once, b → a, and never anywhere else.
    assert [(f, t) for f, t, _ in _traffic(world, "bazaar_offer_made")] == [("b", "a")]


async def test_the_sellers_accept_reaches_the_buyer_and_the_sale_every_member(world):
    a, b, c, d, e = world.a, world.b, world.c, world.d, world.e
    bobs = await b.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-bob", amount=10000
    )
    caras = await c.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-cara", amount=9000
    )
    assert {o[0] for o in await _offers(a)} == {bobs.id, caras.id}
    await a.bazaar.accept_fixed_offer(offer_id=bobs.id, actor_user_id="u-anna")
    # bob's household: his offer accepted, bob told.
    assert [o[4] for o in await _offers(b)] == ["accepted"]
    assert await _notif_types(b, "u-bob") == ["bazaar_offer_accepted"]
    # cara's offer is over on her household too — rejected like on the
    # seller's, and (as there) without a notice.
    assert [o[4] for o in await _offers(c)] == ["rejected"]
    assert await _notif_types(c, "u-cara") == []
    # The sale reached every member household, follower included; the
    # non-member household holds nothing at all.
    for h in (a, b, c, d):
        assert await _status(h) == "sold", h.name
    assert await e.bazaar_repo.get_listing(LISTING) is None
    # The answer went to bob's household only; the sale to members only.
    assert [(f, t) for f, t, _ in _traffic(world, "bazaar_offer_updated")] == [
        ("a", "b")
    ]
    assert {t for _f, t, _ in _traffic(world, "bazaar_listing_updated")} == {
        "b",
        "c",
        "d",
    }
    assert all(t != "e" for h in (a, b, c, d) for t, _env in h.inbox.posted)


async def test_the_sellers_reject_reaches_the_buyer(world):
    a, b = world.a, world.b
    offer = await b.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-bob", amount=5000
    )
    await a.bazaar.reject_fixed_offer(
        offer_id=offer.id, actor_user_id="u-anna", reason="too low"
    )
    assert [o[4] for o in await _offers(b)] == ["rejected"]
    assert await _notif_types(b, "u-bob") == ["bazaar_offer_rejected"]
    # The listing stays open for other offers, everywhere.
    assert await _status(a) == await _status(b) == "active"


async def test_a_withdrawn_offer_can_no_longer_be_accepted(world):
    a, b = world.a, world.b
    offer = await b.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-bob", amount=10000
    )
    await b.bazaar.withdraw_fixed_offer(offer_id=offer.id, actor_user_id="u-bob")
    assert [o[4] for o in await _offers(a)] == ["withdrawn"]
    with pytest.raises(OfferStateError):
        await a.bazaar.accept_fixed_offer(offer_id=offer.id, actor_user_id="u-anna")
    assert await _status(a) == "active"


async def test_no_offer_field_rides_outside_the_ciphertext(world):
    a, b = world.a, world.b
    offer = await b.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-bob", amount=10000, message=SECRET
    )
    await a.bazaar.accept_fixed_offer(offer_id=offer.id, actor_user_id="u-anna")
    traffic = _traffic(world, "bazaar_offer_made", "bazaar_offer_updated")
    assert len(traffic) == 2
    for _frm, _to, env in traffic:
        raw = json.dumps(env)
        for leak in (SECRET, offer.id, LISTING, "u-bob", "u-anna", "10000"):
            assert leak not in raw, leak
        assert "encrypted_payload" in env
        for field in ("message", "amount", "offerer_user_id", "offer_id", "status"):
            assert field not in env


async def test_a_blocked_buyers_offer_tells_the_seller_nothing(world):
    """A block withholds the notice, exactly as for an offer made on the
    seller's own household; the offer itself still lands (the seller may
    still look at it)."""
    a, b = world.a, world.b
    await a.db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES(?,?)",
        ("u-anna", "u-bob"),
    )
    await b.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-bob", amount=10000
    )
    assert len(await _offers(a)) == 1
    assert await _notif_types(a, "u-anna") == []


async def test_an_older_sellers_household_is_sent_nothing(world):
    a, b = world.a, world.b
    # (``set_proto_version`` is a high-water mark: lower it by hand.)
    await b.db.enqueue(
        "UPDATE remote_instances SET proto_version=? WHERE id=?",
        (FederationCapability.MIN_FOR_BAZAAR_OFFERS - 1, a.iid),
    )
    offer = await b.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-bob", amount=10000
    )
    # As before v_73: the offer stays where it was made.
    assert [o[0] for o in await _offers(b)] == [offer.id]
    assert await _offers(a) == []
    assert _traffic(world, "bazaar_offer_made") == []


async def test_a_remote_auction_bid_tells_the_seller(world):
    """The bid path had the same gap on the seller's side: the bid row
    landed but nobody was told."""
    a, b, c = world.a, world.b, world.c
    for h in (a, b, c):
        await h.db.enqueue(
            "UPDATE bazaar_listings SET mode='auction', start_price=100, step_price=10"
        )
    bid = await b.bazaar.place_bid(
        listing_post_id=LISTING, bidder_user_id="u-bob", amount=500
    )
    assert await a.bazaar_repo.get_bid(bid.id) is not None
    assert await _notif_types(a, "u-anna") == ["bazaar_bid_placed"]
    # Mirrored on the third member household, and never echoed back.
    assert await c.bazaar_repo.get_bid(bid.id) is not None
    assert [f for f, _t, _ in _traffic(world, "bazaar_bid_placed")] == ["b"] * 3


# ── Refusals over the real pipeline ───────────────────────────────────────


async def _forge(sender, target, event_type, payload: dict) -> None:
    """``sender`` signs and ships one bazaar event to ``target`` directly."""
    await sender.federation.send_event(
        to_instance_id=target.iid,
        event_type=event_type,
        payload={"space_id": SP, **payload},
        space_id=SP,
    )


def _offer_payload(offerer: str, offer_id: str = "f" * 32) -> dict:
    return {
        "offer_id": offer_id,
        "listing_post_id": LISTING,
        "offerer_user_id": offerer,
        "amount": 1,
        "message": "x",
    }


async def test_a_follower_cannot_make_an_offer(world):
    a, d = world.a, world.d
    with pytest.raises(SpacePermissionError):
        await d.bazaar.make_offer(
            listing_post_id=LISTING, offerer_user_id="u-dora", amount=10000
        )
    # Nor by sending one itself: it dies at the writer gate.
    await _forge(d, a, FET.BAZAAR_OFFER_MADE, _offer_payload("u-dora"))
    assert await _offers(a) == []
    assert await _notif_types(a, "u-anna") == []


@pytest.mark.parametrize(
    ("label", "sender", "offerer"),
    [
        ("c names bob (b's member)", "c", "u-bob"),
        ("c names anna (the seller herself)", "c", "u-anna"),
        ("b names someone unknown", "b", "u-nobody"),
    ],
)
async def test_a_forged_offerer_is_refused(world, label, sender, offerer):
    a = world.a
    await _forge(
        getattr(world, sender), a, FET.BAZAAR_OFFER_MADE, _offer_payload(offerer)
    )
    assert await _offers(a) == [], label
    assert await _notif_types(a, "u-anna") == [], label


async def test_a_non_member_household_cannot_make_an_offer(world):
    a, e = world.a, world.e
    await _forge(e, a, FET.BAZAAR_OFFER_MADE, _offer_payload("u-eve"))
    assert await _offers(a) == []


async def test_an_offer_for_a_listing_not_sold_here_is_dropped(world):
    """b receives an offer for anna's listing: b is not the seller's
    household, so it keeps nothing (an offer is never fanned out)."""
    b, c = world.b, world.c
    await _forge(c, b, FET.BAZAAR_OFFER_MADE, _offer_payload("u-cara"))
    assert await _offers(b) == []


async def test_only_the_sellers_household_answers_an_offer(world):
    a, b, c = world.a, world.b, world.c
    offer = await b.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-bob", amount=10000
    )
    for status in ("accepted", "rejected"):
        await _forge(
            c,
            b,
            FET.BAZAAR_OFFER_UPDATED,
            {"offer_id": offer.id, "listing_post_id": LISTING, "status": status},
        )
    assert [o[4] for o in await _offers(b)] == ["pending"]
    assert await _notif_types(b, "u-bob") == []
    # Nor may anyone but bob's household withdraw it on the seller's.
    await _forge(
        c,
        a,
        FET.BAZAAR_OFFER_UPDATED,
        {"offer_id": offer.id, "listing_post_id": LISTING, "status": "withdrawn"},
    )
    assert [o[4] for o in await _offers(a)] == ["pending"]


async def test_a_replayed_offer_is_refused(world):
    a, b = world.a, world.b
    await b.bazaar.make_offer(
        listing_post_id=LISTING, offerer_user_id="u-bob", amount=10000
    )
    [(_frm, _to, env)] = _traffic(world, "bazaar_offer_made")
    with pytest.raises(ValueError):
        await a.federation.handle_inbound_envelope(
            "a-inbox-for-b", json.dumps(env).encode()
        )
    assert len(await _offers(a)) == 1
    assert await _notif_types(a, "u-anna") == ["bazaar_bid_placed"]


async def test_a_protected_buyer_cannot_make_an_offer(world):
    """§CP.R is enforced where the offer is made; nothing leaves b."""
    b = world.b

    class _Protected:
        async def require_unrestricted(self, user_id, capability):
            raise AccountProtectedError(capability)

        async def is_restricted(self, user_id, capability):
            return True

    b.bazaar._child_protection = _Protected()
    with pytest.raises(AccountProtectedError):
        await b.bazaar.make_offer(
            listing_post_id=LISTING, offerer_user_id="u-bob", amount=10000
        )
    assert _traffic(world, "bazaar_offer_made") == []
