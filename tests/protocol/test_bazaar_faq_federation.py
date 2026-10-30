"""§27.9 release blocker: a listing's questions and answers across
households (v_75).

Marked ``@pytest.mark.security``.

Six REAL applications over six real SQLite databases. Space ``SID`` is
hosted on **H**; the others:

* **S** — sam, a member, sells the bike and writes its FAQ;
* **B** — ben, a member (his household never paired with S);
* **C** — cara, who only *follows* the space;
* **D** — dora, a member, from a household at v_74;
* **R** — rob's household: paired with everyone, but no member of the space.

Every outbound send is captured at :meth:`FederationService.send_with_mesh_fallback`
— the one door ``broadcast_to_space_members`` goes through (it seals under
``SPACE_ROUTED`` when the path crosses a relay) — and delivered through the
real post-decrypt gates (ban, archive, space writer) and the real handler
registry. The people act through the real HTTP routes.

Proves: an FAQ change reaches every member household at v_75 (the
follower's too — it reads the listing) and nobody else (not the older
household, not the non-member); only the seller's household, for the
seller, can add / edit / remove — a forged seller, another member's
household, the host relaying, a non-member and a follower-only household
are refused on receipt; bounds and plain text are enforced on receipt; a
late edit never resurrects a removed entry; a late joiner gets the FAQ with
the listing over the §25.6 sync; an unpaired member household gets it only
inside ``SPACE_ROUTED``.
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    space_sync_receiver_key,
    space_sync_service_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
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
FAQ = FET.BAZAAR_LISTING_FAQ_UPDATED
SID = "sp-street"
BIKE = "post-bike"
_IDS = itertools.count()
_HEX = (f"{n:032x}" for n in itertools.count(0xFA0000))
_REAL_SEND = FederationService.send_with_mesh_fallback

#: Long and unique, so finding it anywhere it should not be means a leak.
QUESTION = "Is the frame size 58 cm and does it come with the blue lock?"
ANSWER = "Yes — 58 cm, and the lock is included."

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


async def _fake_send(self, *, to_instance_id, event_type, payload, space_id=None):
    OUTBOX.setdefault(self._own_instance_id, []).append(
        (to_instance_id, event_type, copy.deepcopy(payload))
    )
    return DeliveryResult(instance_id=to_instance_id, ok=True)


async def _fake_supports(self, instance_id, *, min_version):
    return VERSIONS.get(instance_id, 75) >= min_version


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


async def _seed_listing(db) -> None:
    await db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES(?, ?, 'u-sam', 'bazaar', '🛍 Bike')",
        (BIKE, SID),
    )
    await db.enqueue(
        "INSERT INTO bazaar_listings(post_id, space_id, seller_user_id,"
        " mode, title, end_time, currency, price)"
        " VALUES(?, ?, 'u-sam', 'fixed', 'Bike', ?, 'EUR', 4000)",
        (BIKE, SID, _far()),
    )


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
    VERSIONS[houses["D"].iid] = 74
    host = houses["H"]
    now = datetime.now(UTC).isoformat()
    for house in houses.values():
        db = house.db
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
                    VERSIONS.get(other.iid, 75),
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
        await _seed_listing(db)
    return houses


def _by_iid(mesh) -> dict[str, House]:
    return {h.iid: h for h in mesh.values()}


def _event(sender: House, payload: dict, to: House) -> FederationEvent:
    return FederationEvent(
        msg_id=f"m-{next(_IDS)}",
        event_type=FAQ,
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
            if to in houses and et is FAQ:
                await _receive(houses[to], _event(sender, payload, houses[to]))


def _faq_sends() -> list[tuple[str, str, dict]]:
    return [
        (frm, to, p) for frm, sent in OUTBOX.items() for to, et, p in sent if et is FAQ
    ]


async def _held(house: House) -> list[tuple[str, str]]:
    rows = await house.db.fetchall(
        "SELECT question, answer FROM bazaar_listing_faq"
        " WHERE listing_post_id=? AND removed=0 ORDER BY position",
        (BIKE,),
    )
    return [(r["question"], r["answer"]) for r in rows]


async def _add(house: House, question: str = QUESTION, answer: str = ANSWER):
    return await house.tc.post(
        f"/api/bazaar/{BIKE}/faq",
        json={"question": question, "answer": answer},
        headers=house.headers,
    )


def _payload(**over) -> dict:
    payload = {
        "space_id": SID,
        "listing_post_id": BIKE,
        "entry_id": next(_HEX),
        "rev": 1,
        "position": 0,
        "removed": False,
        "question": "Forged?",
        "answer": "Forged.",
    }
    payload.update(over)
    return payload


# ── The round trip ────────────────────────────────────────────────────────


async def test_an_faq_entry_reaches_every_member_household_and_nobody_else(mesh):
    s = mesh["S"]
    r = await _add(s)
    assert r.status == 201, await r.text()
    sends = _faq_sends()
    targets = {to for frm, to, _p in sends}
    assert {frm for frm, _to, _p in sends} == {s.iid}
    # H, B and the follower's household C read the listing; D (v_74) and R
    # (no member) are sent nothing.
    assert targets == {mesh[n].iid for n in ("H", "B", "C")}
    payload = sends[0][2]
    assert payload["space_id"] == SID
    assert payload["question"] == QUESTION and payload["answer"] == ANSWER
    # The entry never names who asked (nor the seller: the listing does).
    assert set(payload) == {
        "space_id",
        "listing_post_id",
        "entry_id",
        "rev",
        "position",
        "removed",
        "question",
        "answer",
    }
    await _pump(mesh)
    for name in ("S", "H", "B", "C"):
        assert await _held(mesh[name]) == [(QUESTION, ANSWER)], name
    for name in ("D", "R"):
        assert await _held(mesh[name]) == [], name
        row = await mesh[name].db.fetchone(
            "SELECT 1 FROM bazaar_listing_faq WHERE question=?", (QUESTION,)
        )
        assert row is None, name
    # Every viewer reads it with the listing — the follower included.
    c = mesh["C"]
    detail = await (await c.tc.get(f"/api/bazaar/{BIKE}", headers=c.headers)).json()
    assert [(e["question"], e["answer"]) for e in detail["faq"]] == [(QUESTION, ANSWER)]


async def test_edits_and_removals_reach_every_member_household(mesh):
    s = mesh["S"]
    entry = await (await _add(s)).json()
    await _pump(mesh)
    r = await s.tc.patch(
        f"/api/bazaar/{BIKE}/faq/{entry['entry_id']}",
        json={"question": "Frame size?", "answer": "58 cm."},
        headers=s.headers,
    )
    assert r.status == 200, await r.text()
    await _pump(mesh)
    for name in ("H", "B", "C"):
        assert await _held(mesh[name]) == [("Frame size?", "58 cm.")], name
    r = await s.tc.delete(
        f"/api/bazaar/{BIKE}/faq/{entry['entry_id']}", headers=s.headers
    )
    assert r.status == 204
    removal = _faq_sends()[-1][2]
    assert removal["removed"] is True
    assert "question" not in removal and "answer" not in removal
    await _pump(mesh)
    for name in ("H", "B", "C"):
        assert await _held(mesh[name]) == [], name


async def test_a_late_edit_never_brings_a_removed_entry_back(mesh):
    s, b = mesh["S"], mesh["B"]
    entry = await (await _add(s)).json()
    first = next(p for _f, to, p in _faq_sends() if to == b.iid)
    for sent in OUTBOX.values():
        sent.clear()
    await s.tc.delete(f"/api/bazaar/{BIKE}/faq/{entry['entry_id']}", headers=s.headers)
    await _pump(mesh)  # the removal arrives first …
    assert await _receive(b, _event(s, first, b))  # … then the late add
    assert await _held(b) == []


async def test_the_faq_ends_with_the_listing(mesh):
    s, b = mesh["S"], mesh["B"]
    await _add(s)
    await _pump(mesh)
    await b.db.enqueue("DELETE FROM space_posts WHERE id=?", (BIKE,))
    row = await b.db.fetchone("SELECT COUNT(*) AS n FROM bazaar_listing_faq", ())
    assert row["n"] == 0


# ── Refused before anything is sent ───────────────────────────────────────


async def test_only_the_seller_writes_and_nothing_is_sent_otherwise(mesh):
    b, d = mesh["B"], mesh["D"]
    assert (await _add(b)).status == 403
    assert (await _add(mesh["R"])).status == 404
    # An older member's household: no new event to a household below v_75.
    assert d.iid not in {to for _f, to, _p in _faq_sends()}
    assert _faq_sends() == []


async def test_a_protected_seller_sends_nothing(mesh):
    s = mesh["S"]
    await s.db.enqueue(
        "UPDATE users SET child_protection_enabled=1 WHERE user_id='u-sam'", ()
    )
    r = await _add(s)
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "ACCOUNT_PROTECTED"
    assert _faq_sends() == []


# ── Refused on receipt ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sender", "why"),
    [
        ("B", "another member's household speaks for the seller"),
        ("H", "the host relays for the seller"),
        ("R", "a household that is no member"),
    ],
)
async def test_a_forged_seller_is_refused(mesh, sender, why):
    b = mesh["B"]
    target = mesh["C"] if sender == "B" else b
    await _receive(target, _event(mesh[sender], _payload(), target))
    assert await _held(target) == [], why


async def test_a_followers_household_dies_at_the_writer_gate(mesh):
    c, b = mesh["C"], mesh["B"]
    ok = await _receive(b, _event(c, _payload(), b))
    assert ok is False
    assert await _held(b) == []


async def test_our_own_seller_is_never_written_by_a_peer(mesh):
    s, b = mesh["S"], mesh["B"]
    # B claims sam's seat (a hostile roster row): S still refuses — only S
    # writes its own seller's FAQ.
    await s.db.enqueue(
        "INSERT OR REPLACE INTO space_remote_members(space_id, instance_id,"
        " user_id, role, display_name) VALUES(?, ?, 'u-sam', 'member', 'Sam')",
        (SID, b.iid),
    )
    await _receive(s, _event(b, _payload(), s))
    assert await _held(s) == []


@pytest.mark.parametrize(
    "over",
    [
        {"question": "x" * 201},
        {"answer": "y" * 501},
        {"question": ""},
        {"answer": "nul\x00"},
        {"question": "two\nlines"},
        {"question": "  untrimmed "},
        {"entry_id": "../etc"},
        {"rev": 0},
        {"rev": "7"},
        {"removed": "no"},
        {"question": None},
    ],
)
async def test_bounds_and_plain_text_are_enforced_on_receipt(mesh, over):
    s, b = mesh["S"], mesh["B"]
    await _receive(b, _event(s, _payload(**over), b))
    assert await _held(b) == []


async def test_an_eleventh_entry_is_refused_on_receipt(mesh):
    s, b = mesh["S"], mesh["B"]
    for n in range(10):
        assert await _receive(
            b, _event(s, _payload(rev=n + 1, position=n, question=f"Q{n}?"), b)
        )
    await _receive(b, _event(s, _payload(rev=11, position=10, question="Q10?"), b))
    assert len(await _held(b)) == 10


async def test_no_faq_change_to_a_listing_that_ended(mesh):
    s, b = mesh["S"], mesh["B"]
    await b.db.enqueue(
        "UPDATE bazaar_listings SET status='cancelled' WHERE post_id=?", (BIKE,)
    )
    await _receive(b, _event(s, _payload(), b))
    assert await _held(b) == []


async def test_a_listing_of_another_space_is_refused(mesh):
    s, b = mesh["S"], mesh["B"]
    event = _event(s, _payload(space_id="sp-other"), b)
    event = FederationEvent(
        msg_id=event.msg_id,
        event_type=FAQ,
        from_instance=s.iid,
        to_instance=b.iid,
        timestamp=event.timestamp,
        payload=event.payload,
        space_id=SID,
    )
    await _receive(b, event)
    assert await _held(b) == []


# ── A late joiner gets the FAQ with the listing (§25.6) ──────────────────


async def test_a_late_joiner_gets_the_faq_over_sync(mesh):
    s, b = mesh["S"], mesh["B"]
    keep = await (await _add(s, "Kept?", "Yes.")).json()
    gone = await (await _add(s, "Gone?", "Yes.")).json()
    await s.tc.delete(f"/api/bazaar/{BIKE}/faq/{gone['entry_id']}", headers=s.headers)
    # B joined late: it holds neither the listing nor its FAQ.
    await b.db.enqueue("DELETE FROM space_posts WHERE id=?", (BIKE,))
    exporters = s.app[space_sync_service_key]._exporters
    receiver = b.app[space_sync_receiver_key]
    for resource in ("posts", "bazaar"):
        records = await exporters[resource].list_records(SID)
        await receiver._dispatch(resource, SID, records, provider=s.iid)
    rows = await b.db.fetchall(
        "SELECT entry_id, question, removed FROM bazaar_listing_faq"
        " WHERE listing_post_id=? ORDER BY rev",
        (BIKE,),
    )
    assert [(r["entry_id"], r["question"], r["removed"]) for r in rows] == [
        (keep["entry_id"], "Kept?", 0),
        (gone["entry_id"], "", 1),
    ]
    # The record carries the removal marker, never its words.
    record = (await exporters["bazaar"].list_records(SID))[0]
    assert all("Gone?" not in str(item) for item in record["faq"])


# ── An unpaired member household: only ever inside SPACE_ROUTED ──────────


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


async def test_an_unpaired_member_household_gets_it_sealed_in_space_routed(
    mesh, monkeypatch
):
    """The real door: sam's household isn't paired with ben's, so the
    entry rides ``SPACE_ROUTED`` sealed to B's ephemeral key — the relay in
    between sees ciphertext only — and nothing reaches B in the clear."""
    s, b = mesh["S"], mesh["B"]
    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _REAL_SEND)
    fed = s.app[federation_service_key]
    routed = _Routed()
    fed.attach_mesh(route_service=_Route(), routed_handler=routed)
    await s.db.enqueue("DELETE FROM remote_instances WHERE id=?", (b.iid,))
    direct: list = []

    async def _direct(self, **kwargs):
        direct.append(kwargs)
        return DeliveryResult(instance_id=kwargs["to_instance_id"], ok=True)

    monkeypatch.setattr(FederationService, "send_event", _direct)
    r = await _add(s)
    assert r.status == 201, await r.text()
    inner = [x for x in routed.sent if x["inner_event_type"] is FAQ]
    assert [x["path"][-1] for x in inner] == [b.iid]
    assert inner[0]["target_eph_pk_b64"] == "eph-pk"
    assert inner[0]["inner_payload"]["question"] == QUESTION
    assert b.iid not in {
        k["to_instance_id"] for k in direct if k.get("event_type") is FAQ
    }
