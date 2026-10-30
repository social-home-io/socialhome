"""§27.9 release blocker: "Ask the seller" threads between households (v_74).

Marked ``@pytest.mark.security``.

Six REAL applications over six real SQLite databases. Space ``SID`` is
hosted on **H**; the others:

* **S** — sam, a member, sells the bike;
* **B** — ben, a member, wants it (his household never paired with S: no
  ``remote_users`` row for sam);
* **C** — cara, who only *follows* the space;
* **D** — dora, a member, sells a lamp from a household at v_73;
* **R** — rob's household: paired with everyone, but no member of the space.

Every outbound send is captured at :meth:`FederationService.send_with_mesh_fallback`
— the one door ``broadcast_to_space_members`` goes through (it seals under
``SPACE_ROUTED`` when the path crosses a relay) — and delivered to the
target through the real post-decrypt gates (ban, archive, space writer) and
the real handler registry. The people act through the real HTTP routes.

Proves: a question and its answer each reach the other person's household
and nobody else (not the host, not the follower, not a non-member); a
follower, a protected account and an older seller's household are refused
before anything is sent; forged authors, a host relaying, a non-member, a
seller cold-opening a thread, a block and a closed thread are refused on
receipt; an unpaired seller is reached only inside ``SPACE_ROUTED``.
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.domain.bazaar_thread import LISTING_THREAD_RATE_CAPS, listing_thread_id
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
)
from socialhome.federation.federation_service import FederationService
from socialhome.federation.inbound_validator import (
    InboundContext,
    run_post_decrypt_gates,
)

pytestmark = pytest.mark.security

FET = FederationEventType
THREAD = FET.BAZAAR_THREAD_MESSAGE
SID = "sp-street"
BIKE = "post-bike"
LAMP = "post-lamp"
_IDS = itertools.count()
_REAL_SEND = FederationService.send_with_mesh_fallback

#: Long and unique, so finding it anywhere but S and B can only mean a leak.
SECRET = "can I pick it up Thursday at the back gate, code 4711?"

VERSIONS: dict[str, int] = {}
OUTBOX: dict[str, list[tuple[str, FederationEventType, dict]]] = {}

ROLES = {"H": "owner", "S": "member", "B": "member", "C": "subscriber", "D": "member"}
USERS = {"H": "hanna", "S": "sam", "B": "ben", "C": "cara", "D": "dora", "R": "rob"}
MEMBERS = tuple(ROLES)


@dataclass
class House:
    name: str
    app: object
    db: object
    iid: str
    tc: object
    user_id: str
    headers: dict

    @property
    def sent(self) -> list[tuple[str, FederationEventType, dict]]:
        return OUTBOX.setdefault(self.iid, [])


async def _fake_send(self, *, to_instance_id, event_type, payload, space_id=None):
    OUTBOX.setdefault(self._own_instance_id, []).append(
        (to_instance_id, event_type, copy.deepcopy(payload))
    )
    return DeliveryResult(instance_id=to_instance_id, ok=True)


async def _fake_supports(self, instance_id, *, min_version):
    return VERSIONS.get(instance_id, 74) >= min_version


def _config(tmp_dir, name: str) -> Config:
    base = tmp_dir / name
    base.mkdir()
    return Config(
        data_dir=str(base),
        db_path=str(base / "db.sqlite"),
        media_path=str(base / "media"),
        apps_path=str(base / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": f"https://{name}.test"})}
        ),
    )


def _far() -> str:
    return (datetime.now(UTC) + timedelta(days=7)).isoformat()


@pytest.fixture
async def mesh(aiohttp_client, tmp_dir, monkeypatch):
    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _fake_send)
    monkeypatch.setattr(FederationService, "peer_supports", _fake_supports)
    monkeypatch.setattr(FederationService, "space_member_supports", _fake_supports)
    OUTBOX.clear()
    VERSIONS.clear()
    houses: dict[str, House] = {}
    for name, username in USERS.items():
        app = create_app(_config(tmp_dir, name))
        tc = await aiohttp_client(app)
        db = app[db_key]
        row = await db.fetchone("SELECT instance_id FROM instance_identity")
        uid = f"u-{username}"
        token = f"{name}-tok"
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin)"
            " VALUES(?, ?, ?, 0)",
            (username, uid, username.title()),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
            " VALUES(?, ?, 't', ?)",
            (f"t-{name}", uid, sha256_token_hash(token)),
        )
        houses[name] = House(
            name=name,
            app=app,
            db=db,
            iid=row["instance_id"],
            tc=tc,
            user_id=uid,
            headers={"Authorization": f"Bearer {token}"},
        )
    VERSIONS[houses["D"].iid] = 73
    host = houses["H"]
    now = datetime.now(UTC).isoformat()
    for house in houses.values():
        db = house.db
        # Every household pairs with every other (R included): the space's
        # transport, not a pairing, decides who hears a thread.
        for other in houses.values():
            if other is house:
                continue
            await db.enqueue(
                "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
                " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
                " local_inbox_id, status, source, proto_version,"
                " capabilities_seen_at)"
                " VALUES(?, ?, ?, '00', '00', ?, ?, 'confirmed', 'manual', ?, ?)",
                (
                    other.iid,
                    other.name,
                    "ab" * 32,
                    f"https://{other.name}.test/inbox/x",
                    f"{other.name}_local",
                    VERSIONS.get(other.iid, 74),
                    now,
                ),
            )
        if house.name == "R":
            continue  # no member of the space: holds none of it
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?, 'Street', ?, 'hanna', ?)",
            (SID, host.iid, "ab" * 32),
        )
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            (SID, house.user_id, ROLES[house.name]),
        )
        for other_name in MEMBERS:
            other = houses[other_name]
            await db.enqueue(
                "INSERT INTO space_instances(space_id, instance_id) VALUES(?, ?)",
                (SID, other.iid),
            )
            if other is house:
                continue
            role = "member" if other.name == "H" else ROLES[other.name]
            await db.enqueue(
                "INSERT INTO space_remote_members(space_id, instance_id,"
                " user_id, role, display_name) VALUES(?, ?, ?, ?, ?)",
                (SID, other.iid, other.user_id, role, USERS[other.name].title()),
            )
        for post_id, seller, title in (
            (BIKE, "u-sam", "Bike"),
            (LAMP, "u-dora", "Lamp"),
        ):
            await db.enqueue(
                "INSERT INTO space_posts(id, space_id, author, type, content)"
                " VALUES(?, ?, ?, 'bazaar', ?)",
                (post_id, SID, seller, f"🛍 {title}"),
            )
            await db.enqueue(
                "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id,"
                " mode, title, end_time, currency, price)"
                " VALUES(?, ?, ?, 'fixed', ?, ?, 'EUR', 4000)",
                (post_id, SID, seller, title, _far()),
            )
    return houses


def _by_iid(mesh) -> dict[str, House]:
    return {h.iid: h for h in mesh.values()}


def _event(sender: House, payload: dict, to: House) -> FederationEvent:
    return FederationEvent(
        msg_id=f"m-{next(_IDS)}",
        event_type=THREAD,
        from_instance=sender.iid,
        to_instance=to.iid,
        timestamp=datetime.now(UTC).isoformat(),
        payload=copy.deepcopy(payload),
        space_id=SID,
    )


async def _receive(to: House, event: FederationEvent) -> bool:
    """``event`` through ``to``'s post-decrypt gates and handlers; ``False``
    when a gate dropped it."""
    fed = to.app[federation_service_key]
    ctx = InboundContext(
        envelope={"space_id": SID, "from_instance": event.from_instance},
        event=event,
    )
    if not await run_post_decrypt_gates(
        ctx, steps=fed.post_decrypt_gate_steps(include_ban_check=True)
    ):
        return False
    for handler in fed._event_registry.handlers_for(event.event_type):
        await handler(event)
    return True


async def _pump(mesh) -> None:
    houses = _by_iid(mesh)
    while True:
        batch = []
        for iid, sent in OUTBOX.items():
            batch.extend((houses[iid], to, et, p) for to, et, p in sent)
            sent.clear()
        if not batch:
            return
        for sender, to, et, payload in batch:
            if to in houses and et is THREAD:
                await _receive(houses[to], _event(sender, payload, houses[to]))


def _thread_sends() -> list[tuple[str, str, dict]]:
    return [
        (frm, to, p)
        for frm, sent in OUTBOX.items()
        for to, et, p in sent
        if et is THREAD
    ]


async def _messages(house: House) -> list[tuple[str, str]]:
    rows = await house.db.fetchall(
        "SELECT m.sender_user_id, m.content FROM conversation_messages m"
        " JOIN conversations c ON c.id = m.conversation_id"
        " WHERE c.bazaar_listing_id IS NOT NULL ORDER BY m.created_at",
        (),
    )
    return [(r["sender_user_id"], r["content"]) for r in rows]


async def _ask(house: House, post_id: str = BIKE):
    return await house.tc.post(f"/api/bazaar/{post_id}/ask", headers=house.headers)


async def _say(house: House, conv_id: str, text: str):
    return await house.tc.post(
        f"/api/conversations/{conv_id}/messages",
        json={"content": text},
        headers=house.headers,
    )


async def _open_thread(mesh) -> str:
    r = await _ask(mesh["B"])
    assert r.status == 201, await r.text()
    body = await r.json()
    assert body["kind"] == "thread"
    return body["conversation_id"]


def _thread_payload(**over) -> dict:
    payload = {
        "space_id": SID,
        "listing_post_id": BIKE,
        "buyer_user_id": "u-ben",
        "author_user_id": "u-ben",
        "message_id": next(_HEX),
        "content": "forged?",
        "created_at": datetime.now(UTC).isoformat(),
    }
    payload.update(over)
    return payload


_HEX = (f"{n:032x}" for n in itertools.count(0xABC000))


# ── The round trip ────────────────────────────────────────────────────────


async def test_a_question_and_its_answer_reach_only_the_other_household(mesh):
    s, b = mesh["S"], mesh["B"]
    conv_id = await _open_thread(mesh)
    assert conv_id == listing_thread_id(BIKE, "u-ben")
    r = await _say(b, conv_id, SECRET)
    assert r.status == 201, await r.text()
    sends = _thread_sends()
    # Exactly one envelope, to sam's household — not the host, not the
    # follower, not the older member, not the non-member.
    assert [(frm, to) for frm, to, _p in sends] == [(b.iid, s.iid)]
    payload = sends[0][2]
    assert payload["space_id"] == SID
    assert "conversation_id" not in payload  # each side derives it
    await _pump(mesh)
    assert await _messages(s) == [("u-ben", SECRET)]
    # Sam sees the thread under his listing, with ben's name and an unread.
    r = await s.tc.get(f"/api/bazaar/{BIKE}/threads", headers=s.headers)
    threads = await r.json()
    assert [
        (t["conversation_id"], t["other_display_name"], t["unread"]) for t in threads
    ] == [(conv_id, "Ben", 1)]
    # And a "Ben asked about Bike" row — one per thread (D7).
    row = await s.db.fetchone(
        "SELECT title, link_url FROM notifications WHERE type='bazaar_question'", ()
    )
    assert row is not None and "Ben" in row["title"] and "Bike" in row["title"]
    assert row["link_url"] == f"/dms/{conv_id}"
    # Sam answers; only ben's household hears it.
    r = await _say(s, conv_id, "Sure, Thursday works.")
    assert r.status == 201, await r.text()
    assert [(frm, to) for frm, to, _p in _thread_sends()] == [(s.iid, b.iid)]
    await _pump(mesh)
    assert await _messages(b) == [("u-ben", SECRET), ("u-sam", "Sure, Thursday works.")]
    # The thread row carries the listing card for the SPA.
    r = await b.tc.get(f"/api/conversations/{conv_id}", headers=b.headers)
    card = (await r.json())["bazaar_listing"]
    assert card["post_id"] == BIKE and card["role"] == "buyer" and card["writable"]
    # Nobody else stored a word of it.
    for name in ("H", "C", "D", "R"):
        assert await _messages(mesh[name]) == [], name
        row = await mesh[name].db.fetchone(
            "SELECT 1 FROM conversation_messages WHERE content=?", (SECRET,)
        )
        assert row is None, name


async def test_asking_again_reopens_the_same_thread(mesh):
    first = await _open_thread(mesh)
    assert await _open_thread(mesh) == first


async def test_a_paired_buyer_gets_the_normal_dm(mesh):
    b, s = mesh["B"], mesh["S"]
    await b.db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES('u-sam', ?, 'sam', 'Sam')",
        (s.iid,),
    )
    r = await _ask(b)
    assert r.status == 201, await r.text()
    body = await r.json()
    assert body["kind"] == "dm"
    conv = await b.db.fetchone(
        "SELECT bazaar_listing_id FROM conversations WHERE id=?",
        (body["conversation_id"],),
    )
    assert conv["bazaar_listing_id"] is None
    assert _thread_sends() == []


# ── Refused before anything is sent ───────────────────────────────────────


async def test_a_follower_cannot_ask(mesh):
    r = await _ask(mesh["C"])
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "SUBSCRIBER_READ_ONLY"
    assert _thread_sends() == []


async def test_a_protected_account_cannot_ask(mesh):
    b = mesh["B"]
    await b.db.enqueue(
        "UPDATE users SET child_protection_enabled=1 WHERE user_id='u-ben'", ()
    )
    r = await _ask(b)
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "ACCOUNT_PROTECTED"
    assert _thread_sends() == []


async def test_an_older_sellers_household_is_sent_nothing(mesh):
    r = await _ask(mesh["B"], LAMP)
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "SELLER_HOUSEHOLD_TOO_OLD"
    assert _thread_sends() == []


async def test_the_seller_cannot_ask_themselves(mesh):
    r = await _ask(mesh["S"])
    assert r.status == 422 or r.status == 409, await r.text()
    assert (await r.json())["error"]["code"] == "OWN_LISTING"


async def test_a_non_member_cannot_even_see_the_listing(mesh):
    r = await _ask(mesh["R"])
    assert r.status == 404


async def test_media_edits_and_reactions_stay_out(mesh):
    b = mesh["B"]
    conv_id = await _open_thread(mesh)
    r = await b.tc.post(
        f"/api/conversations/{conv_id}/messages",
        json={"type": "image", "media_url": "api/media/x.webp", "content": ""},
        headers=b.headers,
    )
    assert (
        r.status == 422
        and (await r.json())["error"]["code"] == "LISTING_THREAD_TEXT_ONLY"
    )
    r = await _say(b, conv_id, "hello")
    mid = (await r.json())["id"]
    r = await b.tc.patch(
        f"/api/conversations/{conv_id}/messages/{mid}",
        json={"content": "edited"},
        headers=b.headers,
    )
    assert (
        r.status == 422
        and (await r.json())["error"]["code"] == "LISTING_THREAD_NO_CHANGES"
    )
    assert len(_thread_sends()) == 1  # the one text message


async def test_a_closed_thread_is_read_only_on_both_sides(mesh):
    s, b = mesh["S"], mesh["B"]
    conv_id = await _open_thread(mesh)
    await _say(b, conv_id, "still there?")
    await _pump(mesh)
    long_ago = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    for house in (s, b):
        await house.db.enqueue(
            "UPDATE bazaar_listings SET status='sold', sold_at=? WHERE post_id=?",
            (long_ago, BIKE),
        )
    r = await _say(b, conv_id, "hello?")
    assert (
        r.status == 409 and (await r.json())["error"]["code"] == "LISTING_THREAD_CLOSED"
    )
    # A household that sends anyway is refused on receipt.
    await _receive(s, _event(b, _thread_payload(content="late"), s))
    assert ("u-ben", "late") not in await _messages(s)


# ── Refused on receipt ────────────────────────────────────────────────────


async def test_a_followers_household_dies_at_the_writer_gate(mesh):
    c, s = mesh["C"], mesh["S"]
    ok = await _receive(
        s,
        _event(c, _thread_payload(buyer_user_id="u-cara", author_user_id="u-cara"), s),
    )
    assert ok is False
    assert await _messages(s) == []


async def test_a_non_member_household_is_refused(mesh):
    rr, s = mesh["R"], mesh["S"]
    await _receive(
        s, _event(rr, _thread_payload(buyer_user_id="u-rob", author_user_id="u-rob"), s)
    )
    assert await _messages(s) == []


@pytest.mark.parametrize(
    ("label", "sender", "over"),
    [
        ("the host speaks for ben", "H", {}),
        ("the follower's household speaks for ben", "C", {}),
        ("ben's household speaks as the seller", "B", {"author_user_id": "u-sam"}),
        (
            "a third person's words",
            "B",
            {"author_user_id": "u-dora"},
        ),
        ("our own seller's words from elsewhere", "D", {"author_user_id": "u-sam"}),
    ],
)
async def test_forged_authors_are_refused(mesh, label, sender, over):
    s = mesh["S"]
    await _receive(s, _event(mesh[sender], _thread_payload(**over), s))
    assert await _messages(s) == [], label


async def test_a_seller_never_cold_opens_a_thread(mesh):
    s, b = mesh["S"], mesh["B"]
    await _receive(
        b, _event(s, _thread_payload(author_user_id="u-sam", content="buy it!"), b)
    )
    assert await _messages(b) == []


async def test_a_block_stops_the_thread_both_ways(mesh):
    s, b = mesh["S"], mesh["B"]
    conv_id = await _open_thread(mesh)
    await _say(b, conv_id, "first")
    await _pump(mesh)
    r = await s.tc.post("/api/blocks", json={"user_id": "u-ben"}, headers=s.headers)
    assert r.status == 201
    # Sam can't write to ben any more …
    r = await _say(s, conv_id, "hi")
    assert r.status == 403
    # … and ben's household's next message is dropped on arrival.
    await _receive(s, _event(b, _thread_payload(content="second"), s))
    assert await _messages(s) == [("u-ben", "first")]


async def test_a_protected_seller_is_never_asked(mesh):
    s, b = mesh["S"], mesh["B"]
    await s.db.enqueue(
        "UPDATE users SET child_protection_enabled=1 WHERE user_id='u-sam'", ()
    )
    await _receive(s, _event(b, _thread_payload(content="hi"), s))
    assert await _messages(s) == []


async def test_a_replayed_or_squatted_message_id_changes_nothing(mesh):
    s, b = mesh["S"], mesh["B"]
    payload = _thread_payload(content="one")
    await _receive(s, _event(b, payload, s))
    await _receive(s, _event(b, dict(payload, content="rewritten"), s))
    assert await _messages(s) == [("u-ben", "one")]


async def test_a_dm_cannot_squat_on_a_threads_id(mesh):
    """A paired household can't pre-open a DM under a listing thread's
    derived id (the DM inbound refuses the id shape)."""
    s, b = mesh["S"], mesh["B"]
    conv_id = listing_thread_id(BIKE, "u-ben")
    await s.db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name) VALUES('u-ben', ?, 'ben', 'Ben')",
        (b.iid,),
    )
    fed = s.app[federation_service_key]
    for handler in fed._event_registry.handlers_for(FET.DM_MESSAGE):
        await handler(
            FederationEvent(
                msg_id="m-squat",
                event_type=FET.DM_MESSAGE,
                from_instance=b.iid,
                to_instance=s.iid,
                timestamp=datetime.now(UTC).isoformat(),
                payload={
                    "conversation_id": conv_id,
                    "message_id": "cd" * 16,
                    "sender_user_id": "u-ben",
                    "sender_display_name": "Ben",
                    "type": "text",
                    "content": "squat",
                    "recipient_user_ids": ["u-sam"],
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
            )
        )
    assert (
        await s.db.fetchone("SELECT 1 FROM conversations WHERE id=?", (conv_id,))
        is None
    )


# ── An unpaired seller: only ever inside SPACE_ROUTED ────────────────────


class _Route:
    def cooldown_remaining(self, instance_id):
        return 0.0

    async def discover_route(self, instance_id):
        return (["relay", instance_id], "eph-pk")

    def cached_target_identity_pk(self, instance_id):
        return "pinned-pk"

    async def invalidate(self, instance_id):
        return None

    def add_route_learned_listener(self, listener):
        return None


class _Routed:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_routed(self, **kwargs) -> None:
        self.sent.append(kwargs)

    def attach_route_service(self, route_service) -> None:
        return None


async def test_an_unpaired_sellers_household_gets_it_sealed_in_space_routed(
    mesh, monkeypatch
):
    """The real door: ben's household isn't paired with sam's, so the
    question rides ``SPACE_ROUTED`` sealed to S's ephemeral key — the relay
    in between sees ciphertext only — and nothing is sent in the clear."""
    b, s = mesh["B"], mesh["S"]
    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _REAL_SEND)
    fed = b.app[federation_service_key]
    routed = _Routed()
    fed.attach_mesh(route_service=_Route(), routed_handler=routed)
    await b.db.enqueue("DELETE FROM remote_instances WHERE id=?", (s.iid,))
    sent_in_clear: list = []

    async def _no_direct(self, **kwargs):
        sent_in_clear.append(kwargs)
        return DeliveryResult(instance_id=kwargs["to_instance_id"], ok=True)

    monkeypatch.setattr(FederationService, "send_event", _no_direct)
    conv_id = await _open_thread(mesh)
    r = await _say(b, conv_id, SECRET)
    assert r.status == 201, await r.text()
    inner = [x for x in routed.sent if x["inner_event_type"] is THREAD]
    assert len(inner) == 1
    assert inner[0]["path"][-1] == s.iid
    assert inner[0]["target_eph_pk_b64"] == "eph-pk"
    assert inner[0]["inner_payload"]["content"] == SECRET
    assert [k for k in sent_in_clear if k.get("event_type") is THREAD] == []


# ── Thread lifetime and the rate cap ──────────────────────────────────────


async def _deliver(mesh, event_type: FederationEventType) -> list[str]:
    """Deliver every queued ``event_type`` envelope through the real gates;
    the names of the households it reached."""
    houses = _by_iid(mesh)
    reached: list[str] = []
    for iid, sent in OUTBOX.items():
        for to, et, payload in list(sent):
            if et is event_type and to in houses:
                event = replace(
                    _event(houses[iid], payload, houses[to]), event_type=event_type
                )
                if await _receive(houses[to], event):
                    reached.append(houses[to].name)
        sent[:] = [s for s in sent if s[1] is not event_type]
    return reached


async def _card(house: House, conv_id: str) -> dict:
    r = await house.tc.get(f"/api/conversations/{conv_id}", headers=house.headers)
    assert r.status == 200, await r.text()
    return (await r.json())["bazaar_listing"]


async def test_a_withdrawn_listings_threads_close_14_days_after_the_withdrawal(mesh):
    """Not 14 days after the scheduled end: the seller's household records
    the withdrawal and every member household closes the threads from the
    same moment."""
    s, b = mesh["S"], mesh["B"]
    conv_id = await _open_thread(mesh)
    await _say(b, conv_id, "still there?")
    await _pump(mesh)
    before = datetime.now(UTC)
    r = await s.tc.delete(f"/api/bazaar/{BIKE}", headers=s.headers)
    assert r.status in (200, 204), await r.text()
    reached = await _deliver(mesh, FET.BAZAAR_LISTING_UPDATED)
    assert "B" in reached
    seller_side, buyer_side = await _card(s, conv_id), await _card(b, conv_id)
    assert seller_side["status"] == buyer_side["status"] == "cancelled"
    # Both close at the same moment: the withdrawal + 14 days.
    assert seller_side["closes_at"] == buyer_side["closes_at"]
    closes = datetime.fromisoformat(seller_side["closes_at"])
    assert before + timedelta(days=14) <= closes
    assert closes <= datetime.now(UTC) + timedelta(days=14)
    assert seller_side["writable"] and buyer_side["writable"]
    # 14 days on (the withdrawal moved back), both sides read only.
    long_ago = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    for house in (s, b):
        await house.db.enqueue(
            "UPDATE bazaar_listings SET end_time=? WHERE post_id=?", (long_ago, BIKE)
        )
    r = await _say(b, conv_id, "hello?")
    assert (await r.json())["error"]["code"] == "LISTING_THREAD_CLOSED"
    assert await _receive(s, _event(b, _thread_payload(content="late"), s))
    assert ("u-ben", "late") not in await _messages(s)


async def test_one_buyer_is_capped_on_both_households(mesh):
    """The sender stops at the cap with a coded 429 and sends nothing more;
    a household that ignores it is refused on receipt, however it stamps
    ``created_at``."""
    s, b = mesh["S"], mesh["B"]
    conv_id = await _open_thread(mesh)
    (_hour, per_hour), _ = LISTING_THREAD_RATE_CAPS
    for i in range(per_hour):
        r = await _say(b, conv_id, f"question {i}")
        assert r.status == 201, await r.text()
    await _pump(mesh)
    assert len(await _messages(s)) == per_hour
    r = await _say(b, conv_id, "one more")
    assert r.status == 429
    assert (await r.json())["error"]["code"] == "LISTING_THREAD_RATE_LIMITED"
    assert _thread_sends() == []
    # A household sending anyway — fresh or backdated — is refused.
    long_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    for created_at in (datetime.now(UTC).isoformat(), long_ago):
        await _receive(
            s, _event(b, _thread_payload(content="flood", created_at=created_at), s)
        )
    assert ("u-ben", "flood") not in await _messages(s)
    # The seller still answers: the cap is per person.
    r = await _say(s, conv_id, "Slow down, please.")
    assert r.status == 201, await r.text()
