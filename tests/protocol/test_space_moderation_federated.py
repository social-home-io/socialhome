"""Release-blocker protocol tests: federated moderation (§4.3, v_43).

Marked ``@pytest.mark.security``.

Four REAL applications over four real SQLite databases share one space:

* **H** — the host (owner ``hanna``);
* **A** — a member household (``anna``, a plain member, the submitter);
* **C** — a moderator household (``carl``, a moderator seat);
* **D** — a plain member household (``dora``).

Every outbound send is captured at :meth:`FederationService.send_with_mesh_fallback`
— the one door both a targeted send and ``broadcast_to_space_members`` go
through (it seals under ``SPACE_ROUTED`` when the path crosses a relay) — and
delivered to the target application through the post-decrypt gates (ban,
archived space, space writer) and the real handler registry.

What this file proves:

* **Confidentiality.** A pending item goes to H and C only — never to D,
  never through ``broadcast_to_space_members``; a submission that reaches a
  household with no content authority anyway is dropped, not stored.
* **Approve anywhere.** C approves; C applies the item itself; the content
  federates as anna's with the approval block and lands on A, H and D; the
  decision reaches H and A, never D.
* **Forgeries.** A submission naming another household's user, or a create
  id bound to somebody else; an approval from a member household, naming
  another household's moderator, from a demoted moderator; content for
  anna (A's own user) without a matching item, or with altered words.
* **Convergence.** Replays; approve beats reject; two households approving.
* **Limits.** The per-household cap; a host below v_43; an archived space;
  a feature switched to ``ADMIN_ONLY`` in between.
"""

from __future__ import annotations

import copy
import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    space_moderation_service_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
)
from socialhome.domain.outbox import OutboxEntry
from socialhome.federation.federation_service import FederationService
from socialhome.federation.inbound_validator import (
    InboundContext,
    run_post_decrypt_gates,
)
from socialhome.federation.owner_bound_id import (
    SPACE_POST_KIND,
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    SPACE_STICKY_KIND,
    mint_owner_bound_id,
)
from socialhome.services.space_moderation_service import (
    MAX_TOMBSTONES_PER_HOUSEHOLD,
    MAX_PAYLOAD_BYTES,
    MAX_PENDING_PER_SPACE,
    MAX_PENDING_PER_SUBMITTER,
)
from socialhome.services.space_moderation_federation import (
    MAX_EXPIRY,
    MAX_PENDING_PER_HOUSEHOLD,
)

pytestmark = pytest.mark.security

#: The real targeted door, before the fixture captures it (the sealing test).
_REAL_SEND = FederationService.send_with_mesh_fallback

FET = FederationEventType
SID = "sp-reviewed"
_IDS = itertools.count()

#: The households' advertised versions, by instance id (``peer_supports``).
VERSIONS: dict[str, int] = {}
#: Captured sends per SENDING instance id: (to, event_type, payload).
OUTBOX: dict[str, list[tuple[str, FederationEventType, dict]]] = {}


@dataclass
class House:
    name: str
    app: object
    db: object
    iid: str
    tc: object
    user_id: str
    headers: dict
    seen: list = field(default_factory=list)

    @property
    def sent(self) -> list[tuple[str, FederationEventType, dict]]:
        return OUTBOX.setdefault(self.iid, [])


async def _fake_send(self, *, to_instance_id, event_type, payload, space_id=None):
    OUTBOX.setdefault(self._own_instance_id, []).append(
        (to_instance_id, event_type, copy.deepcopy(payload))
    )
    return DeliveryResult(instance_id=to_instance_id, ok=True)


async def _fake_supports(self, instance_id, *, min_version):
    return VERSIONS.get(instance_id, 43) >= min_version


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


ROLES = {"H": "owner", "A": "member", "C": "moderator", "D": "member"}
USERS = {"H": "hanna", "A": "anna", "C": "carl", "D": "dora"}


@pytest.fixture
async def mesh(aiohttp_client, tmp_dir, monkeypatch):
    """H, A, C and D, each a real app, seated in one space that reviews
    every feature."""
    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _fake_send)
    monkeypatch.setattr(FederationService, "peer_supports", _fake_supports)
    # Space broadcasts pick their variant by the member's space version.
    monkeypatch.setattr(FederationService, "space_member_supports", _fake_supports)
    OUTBOX.clear()
    VERSIONS.clear()
    houses: dict[str, House] = {}
    for name in ("H", "A", "C", "D"):
        app = create_app(_config(tmp_dir, name))
        tc = await aiohttp_client(app)
        db = app[db_key]
        row = await db.fetchone("SELECT instance_id FROM instance_identity")
        uid = f"u-{USERS[name]}"
        token = f"{name}-tok"
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin)"
            " VALUES(?, ?, ?, 0)",
            (USERS[name], uid, USERS[name].title()),
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
    host = houses["H"]
    list_id = mint_owner_bound_id(
        SPACE_TASK_LIST_KIND, space_id=SID, owner_user_id=host.user_id
    )
    now = datetime.now(timezone.utc).isoformat()
    for house in houses.values():
        db = house.db
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key, feature_pages, feature_stickies,"
            " feature_calendar, feature_todo, posts_access, pages_access,"
            " tasks_access, stickies_access, calendar_access)"
            " VALUES(?, 'Club', ?, 'hanna', ?, 1, 1, 1, 1, 'moderated',"
            " 'moderated', 'moderated', 'moderated', 'moderated')",
            (SID, host.iid, "ab" * 32),
        )
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            (SID, house.user_id, ROLES[house.name]),
        )
        for other in houses.values():
            await db.enqueue(
                "INSERT INTO space_instances(space_id, instance_id) VALUES(?, ?)",
                (SID, other.iid),
            )
            if other is house:
                continue
            # The roster mirror: the owner is mirrored as a plain member.
            role = "member" if other.name == "H" else ROLES[other.name]
            await db.enqueue(
                "INSERT INTO space_remote_members(space_id, instance_id,"
                " user_id, role, display_name) VALUES(?, ?, ?, ?, ?)",
                (SID, other.iid, other.user_id, role, USERS[other.name].title()),
            )
            await db.enqueue(
                "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
                " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
                " local_inbox_id, status, source, proto_version,"
                " capabilities_seen_at)"
                " VALUES(?, ?, ?, '00', '00', ?, ?, 'confirmed', 'manual', 43, ?)",
                (
                    other.iid,
                    other.name,
                    "ab" * 32,
                    f"https://{other.name}.test/inbox/x",
                    f"{other.name}_local",
                    now,
                ),
            )
        await db.enqueue(
            "INSERT INTO space_task_lists(id, space_id, name, created_by)"
            " VALUES(?, ?, 'Chores', ?)",
            (list_id, SID, host.user_id),
        )
    houses["list_id"] = list_id  # type: ignore[assignment]
    return houses


def _by_iid(mesh) -> dict[str, House]:
    return {h.iid: h for h in mesh.values() if isinstance(h, House)}


def _event(sender: House, event_type, payload: dict, to: House) -> FederationEvent:
    return FederationEvent(
        msg_id=f"m-{next(_IDS)}",
        event_type=event_type,
        from_instance=sender.iid,
        to_instance=to.iid,
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=copy.deepcopy(payload),
        space_id=SID,
    )


async def _receive(to: House, event: FederationEvent) -> bool:
    """Run ``event`` through ``to``'s post-decrypt gates and handlers."""
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
    to.seen.append((event.event_type, event.payload))
    return True


async def _pump(
    mesh,
    *,
    only: set[FederationEventType] | None = None,
    once: bool = False,
) -> None:
    """Deliver every captured send to its target until nothing is left
    (``once``: just what is queued right now)."""
    houses = _by_iid(mesh)
    while True:
        batch: list[tuple[House, str, FederationEventType, dict]] = []
        for iid, sent in OUTBOX.items():
            keep = []
            for to, et, payload in sent:
                if only is None or et in only:
                    batch.append((houses[iid], to, et, payload))
                else:
                    keep.append((to, et, payload))
            sent[:] = keep
        if not batch:
            return
        for sender, to, et, payload in batch:
            if to in houses:
                await _receive(houses[to], _event(sender, et, payload, houses[to]))
        if once:
            return


def _sends(house: House, event_type) -> list[tuple[str, dict]]:
    return [(to, p) for to, et, p in house.sent if et is event_type]


async def _queue_rows(house: House) -> list[dict]:
    rows = await house.db.fetchall(
        "SELECT id, status, submitted_by, feature FROM space_moderation_queue", ()
    )
    return [dict(r) for r in rows]


async def _tasks(house: House) -> list[tuple[str, str]]:
    rows = await house.db.fetchall(
        "SELECT title, created_by FROM space_tasks WHERE space_id=? ORDER BY title",
        (SID,),
    )
    return [(r["title"], r["created_by"]) for r in rows]


async def _submit_task(mesh, title: str = "Buy milk") -> str:
    a = mesh["A"]
    r = await a.tc.post(
        f"/api/spaces/{SID}/tasks/lists/{mesh['list_id']}/tasks",
        json={"title": title},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    return (await r.json())["item_id"]


async def _approve(house: House, item_id: str) -> None:
    r = await house.tc.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve",
        json={},
        headers=house.headers,
    )
    assert r.status == 200, await r.text()


# ── Confidentiality + approve anywhere ──────────────────────────────────


async def test_a_member_submits_a_moderator_elsewhere_approves_everyone_sees_it(mesh):
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    item_id = await _submit_task(mesh)
    # The pending item goes to the host and the moderator household — one
    # targeted send each — and to nobody else.
    targets = sorted(to for to, _p in _sends(a, FET.SPACE_MODERATION_SUBMITTED))
    assert targets == sorted([h.iid, c.iid])
    assert d.iid not in {to for to, _et, _p in a.sent}
    assert await _tasks(a) == []
    await _pump(mesh)
    assert [r["id"] for r in await _queue_rows(h)] == [item_id]
    assert [r["id"] for r in await _queue_rows(c)] == [item_id]
    assert await _queue_rows(d) == []
    assert not [s for s in d.seen if s[0] is FET.SPACE_MODERATION_SUBMITTED]
    # The moderator's queue on their own household lists it.
    r = await c.tc.get(f"/api/spaces/{SID}/moderation", headers=c.headers)
    listed = await r.json()
    assert [i["id"] for i in listed] == [item_id]
    assert listed[0]["submitted_by_display"] == "Anna"

    r = await c.tc.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve", json={}, headers=c.headers
    )
    assert r.status == 200, await r.text()
    assert (await r.json())["status"] == "publishing"
    # C applies nothing itself: its approval goes to the host alone.
    assert await _tasks(c) == []
    assert [
        (to, p["decision"]) for to, p in _sends(c, FET.SPACE_MODERATION_DECIDED)
    ] == [(h.iid, "approved")]
    assert not _sends(c, FET.SPACE_TASK_CREATED)
    r = await c.tc.get(f"/api/spaces/{SID}/moderation?status=all", headers=c.headers)
    assert [i["publishing"] for i in await r.json()] == [True]
    await _pump(mesh, only={FET.SPACE_MODERATION_DECIDED}, once=True)
    # The host applied it from its own copy and published it as anna's.
    created = _sends(h, FET.SPACE_TASK_CREATED)
    assert sorted(to for to, _p in created) == sorted([a.iid, c.iid, d.iid])
    assert all(
        p["moderation"] == {"item_id": item_id, "approved_by": c.user_id}
        and p["created_by"] == a.user_id
        and p["actor_user_id"] == a.user_id
        for _to, p in created
    )
    decided = _sends(h, FET.SPACE_MODERATION_DECIDED)
    assert sorted(to for to, _p in decided) == sorted([a.iid, c.iid])
    await _pump(mesh)
    for house in (a, h, c, d):
        assert await _tasks(house) == [("Buy milk", a.user_id)], house.name
    for house in (a, h, c):
        (row,) = await _queue_rows(house)
        assert row["status"] == "approved", house.name
    r = await a.tc.get(f"/api/spaces/{SID}/moderation/mine", headers=a.headers)
    assert [i["status"] for i in await r.json()] == ["approved"]


@pytest.mark.parametrize("feature", ["posts", "pages", "stickies", "calendar"])
async def test_every_feature_round_trips_and_the_author_household_accepts_it(
    mesh, feature
):
    """The approval block's content check holds on the REAL wire payloads:
    anna's own household (which holds the item) accepts C's release of it."""
    a, c, d = mesh["A"], mesh["C"], mesh["D"]
    start = datetime.now(timezone.utc) + timedelta(days=2)
    path, body, table, column = {
        "posts": (
            f"/api/spaces/{SID}/posts",
            {"type": "text", "content": "Hello club"},
            "space_posts",
            "content",
        ),
        "pages": (
            f"/api/spaces/{SID}/pages",
            {"title": "Rules", "content": "Be kind"},
            "space_pages",
            "title",
        ),
        "stickies": (
            f"/api/spaces/{SID}/stickies",
            {"content": "Bring cake", "color": "#FFF9B1"},
            "stickies",
            "content",
        ),
        "calendar": (
            f"/api/spaces/{SID}/calendar/events",
            {
                "summary": "Picnic",
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "location": "Park",
            },
            "space_calendar_events",
            "summary",
        ),
    }[feature]
    r = await a.tc.post(path, json=body, headers=a.headers)
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    await _pump(mesh)
    await _approve(c, item_id)
    await _pump(mesh)
    for house in (a, mesh["H"], c, d):
        rows = await house.db.fetchall(f"SELECT {column} FROM {table}", ())
        assert [r[column] for r in rows] == [body.get(column) or body["title"]], (
            feature,
            house.name,
        )


async def test_a_reviewed_event_with_a_capacity_is_released_everywhere(mesh):
    """A reviewed create with a cap: the release carries it to v_56
    households, whose release check compares it with the item; an older
    household (v_55) is sent the release without the field (its fail-closed
    check would refuse an unclassified key) and still takes the event."""
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    for house in (a, h, c):
        VERSIONS[house.iid] = 56
    VERSIONS[d.iid] = 55
    start = datetime.now(timezone.utc) + timedelta(days=2)
    r = await a.tc.post(
        f"/api/spaces/{SID}/calendar/events",
        json={
            "summary": "Workshop",
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
            "capacity": 10,
        },
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    await _pump(mesh)
    await _approve(c, item_id)
    await _pump(mesh, only={FET.SPACE_MODERATION_DECIDED}, once=True)
    # The host applied it and published the release as anna's.
    released = _sends(h, FET.SPACE_CALENDAR_EVENT_CREATED)
    assert {to: p.get("capacity") for to, p in released} == {
        a.iid: 10,
        c.iid: 10,
        d.iid: None,
    }
    assert all("moderation" in p for _to, p in released)
    assert "capacity" not in dict(released)[d.iid]
    await _pump(mesh)
    for house in (a, h, c):
        rows = await house.db.fetchall(
            "SELECT summary, capacity FROM space_calendar_events", ()
        )
        assert [tuple(x) for x in rows] == [("Workshop", 10)], house.name
    rows = await d.db.fetchall("SELECT summary FROM space_calendar_events", ())
    assert [x["summary"] for x in rows] == ["Workshop"]


async def test_a_member_edit_of_someone_elses_sticky_is_released_everywhere(mesh):
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    r = await h.tc.post(
        f"/api/spaces/{SID}/stickies", json={"content": "Old"}, headers=h.headers
    )
    assert r.status == 201, await r.text()
    await _pump(mesh)
    sticky_id = (await r.json())["id"]
    r = await a.tc.patch(
        f"/api/spaces/{SID}/stickies/{sticky_id}",
        json={"content": "New"},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    await _pump(mesh)
    await _approve(c, item_id)
    await _pump(mesh)
    for house in (a, h, c, d):
        rows = await house.db.fetchall("SELECT content, author FROM stickies", ())
        assert [(x["content"], x["author"]) for x in rows] == [("New", h.user_id)]


# ── Receiver checks on a submission ─────────────────────────────────────


def _submission(mesh, *, by: str | None = None, target: str | None = None) -> dict:
    a = mesh["A"]
    by = by or a.user_id
    target = target or mint_owner_bound_id(
        SPACE_STICKY_KIND, space_id=SID, owner_user_id=by
    )
    now = datetime.now(timezone.utc)
    return {
        "space_id": SID,
        "item_id": f"item-{next(_IDS)}",
        "feature": "stickies",
        "action": "create",
        "target_id": target,
        "submitted_by": by,
        "payload": {"entity": "sticky", "target_id": target, "content": "hi"},
        "snapshot": None,
        "submitted_at": now.isoformat(),
        "expires_at": (now + timedelta(days=7)).isoformat(),
    }


async def test_a_misdirected_submission_is_dropped_and_not_stored(mesh):
    a, d = mesh["A"], mesh["D"]
    await _receive(d, _event(a, FET.SPACE_MODERATION_SUBMITTED, _submission(mesh), d))
    assert await _queue_rows(d) == []


async def test_a_submission_naming_another_households_user_is_refused(mesh):
    a, h, d = mesh["A"], mesh["H"], mesh["D"]
    forged = _submission(mesh, by=d.user_id)
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, forged, h))
    assert await _queue_rows(h) == []


async def test_a_create_id_bound_to_somebody_else_is_refused(mesh):
    a, h, d = mesh["A"], mesh["H"], mesh["D"]
    alien = mint_owner_bound_id(
        SPACE_STICKY_KIND, space_id=SID, owner_user_id=d.user_id
    )
    forged = _submission(mesh, target=alien)
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, forged, h))
    assert await _queue_rows(h) == []
    # A legacy (unbound) id is refused too: every v_43 submitter binds it.
    legacy = _submission(mesh, target="0123456789abcdef0123456789abcdef")
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, legacy, h))
    assert await _queue_rows(h) == []


async def test_a_submission_for_a_feature_that_is_not_reviewed_is_refused(mesh):
    a, h = mesh["A"], mesh["H"]
    await h.db.enqueue("UPDATE spaces SET stickies_access='open' WHERE id=?", (SID,))
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, _submission(mesh), h))
    assert await _queue_rows(h) == []


async def test_a_replayed_submission_and_decision_change_nothing(mesh):
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    sub = _submission(mesh)
    for _ in range(2):
        await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, sub, h))
    assert len(await _queue_rows(h)) == 1
    decision = {
        "space_id": SID,
        "item_id": sub["item_id"],
        "decision": "rejected",
        "decided_by": c.user_id,
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "reason": "dup",
    }
    for _ in range(2):
        await _receive(h, _event(c, FET.SPACE_MODERATION_DECIDED, decision, h))
    (row,) = await _queue_rows(h)
    assert row["status"] == "rejected"


async def test_the_per_household_cap_refuses_the_next_item(mesh):
    a, h = mesh["A"], mesh["H"]
    # Spread across users of A so the per-submitter cap is not what bites.
    for n in range(MAX_PENDING_PER_HOUSEHOLD):
        uid = f"u-a{n // 10}"
        if n % 10 == 0:
            await h.db.enqueue(
                "INSERT OR IGNORE INTO space_remote_members(space_id, instance_id,"
                " user_id, role) VALUES(?, ?, ?, 'member')",
                (SID, a.iid, uid),
            )
        await h.db.enqueue(
            "INSERT INTO space_moderation_queue(id, space_id, feature, action,"
            " submitted_by, payload_json, expires_at) VALUES(?, ?, 'stickies',"
            " 'create', ?, '{}', '2099-01-01T00:00:00+00:00')",
            (f"seed-{n}", SID, uid),
        )
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, _submission(mesh), h))
    assert len(await _queue_rows(h)) == MAX_PENDING_PER_HOUSEHOLD


async def test_an_archived_space_takes_no_submission_but_still_takes_decisions(mesh):
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    sub = _submission(mesh)
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, sub, h))
    await h.db.enqueue("UPDATE spaces SET archived=1 WHERE id=?", (SID,))
    late = _submission(mesh)
    assert not await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, late, h))
    decision = {
        "space_id": SID,
        "item_id": sub["item_id"],
        "decision": "rejected",
        "decided_by": c.user_id,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    assert await _receive(h, _event(c, FET.SPACE_MODERATION_DECIDED, decision, h))
    rows = await _queue_rows(h)
    assert [(r["id"], r["status"]) for r in rows] == [(sub["item_id"], "rejected")]


async def test_a_decision_from_a_household_without_content_authority_is_refused(mesh):
    a, h, d = mesh["A"], mesh["H"], mesh["D"]
    sub = _submission(mesh)
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, sub, h))
    decision = {
        "space_id": SID,
        "item_id": sub["item_id"],
        "decision": "rejected",
        "decided_by": d.user_id,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    await _receive(h, _event(d, FET.SPACE_MODERATION_DECIDED, decision, h))
    # And a moderator household naming somebody else as the decider.
    decision["decided_by"] = h.user_id
    await _receive(h, _event(mesh["C"], FET.SPACE_MODERATION_DECIDED, decision, h))
    (row,) = await _queue_rows(h)
    assert row["status"] == "pending"


# ── Forged approvals ────────────────────────────────────────────────────


def _released_task(mesh, *, item_id: str, approved_by: str, title="Buy milk") -> dict:
    a = mesh["A"]
    task_id = mint_owner_bound_id(
        SPACE_TASK_KIND, space_id=SID, owner_user_id=a.user_id
    )
    return {
        "id": task_id,
        "list_id": mesh["list_id"],
        "space_id": SID,
        "title": title,
        "status": "todo",
        "position": 0,
        "created_by": a.user_id,
        "actor_user_id": a.user_id,
        "description": None,
        "due_date": None,
        "assignees": [],
        "priority": None,
        "labels": [],
        "moderation": {"item_id": item_id, "approved_by": approved_by},
    }


async def test_an_approval_from_a_plain_member_household_is_refused(mesh):
    h, d = mesh["H"], mesh["D"]
    forged = _released_task(mesh, item_id="item-x", approved_by=d.user_id)
    await _receive(h, _event(d, FET.SPACE_TASK_CREATED, forged, h))
    assert await _tasks(h) == []


async def test_an_approval_naming_another_households_moderator_is_refused(mesh):
    h, d, c = mesh["H"], mesh["D"], mesh["C"]
    forged = _released_task(mesh, item_id="item-x", approved_by=c.user_id)
    await _receive(h, _event(d, FET.SPACE_TASK_CREATED, forged, h))
    # A moderator household naming the host's owner as the approver.
    forged = _released_task(mesh, item_id="item-x", approved_by=h.user_id)
    await _receive(h, _event(c, FET.SPACE_TASK_CREATED, forged, h))
    assert await _tasks(h) == []


async def test_an_approval_by_a_demoted_moderator_is_refused(mesh):
    h, c = mesh["H"], mesh["C"]
    await h.db.enqueue(
        "UPDATE space_remote_members SET role='member' WHERE instance_id=?", (c.iid,)
    )
    released = _released_task(mesh, item_id="item-x", approved_by=c.user_id)
    await _receive(h, _event(c, FET.SPACE_TASK_CREATED, released, h))
    assert await _tasks(h) == []


async def test_no_household_authors_as_our_user_without_our_matching_item(mesh):
    """C holds a real moderator seat, but anna is A's own user: A accepts
    content in her name only for an item A holds, with the very words."""
    a, c = mesh["A"], mesh["C"]
    forged = _released_task(mesh, item_id="item-never", approved_by=c.user_id)
    await _receive(a, _event(c, FET.SPACE_TASK_CREATED, forged, a))
    assert await _tasks(a) == []
    item_id = await _submit_task(mesh, title="Buy milk")
    await _pump(mesh, only={FET.SPACE_MODERATION_SUBMITTED})
    (held,) = await _queue_rows(a)
    row = await a.db.fetchone(
        "SELECT json_extract(payload_json, '$.target_id') AS t"
        " FROM space_moderation_queue WHERE id=?",
        (item_id,),
    )
    tampered = _released_task(
        mesh, item_id=item_id, approved_by=c.user_id, title="Free beer"
    )
    tampered["id"] = row["t"]
    await _receive(a, _event(c, FET.SPACE_TASK_CREATED, tampered, a))
    assert await _tasks(a) == []
    # The host holds the item too and refuses the altered words the same way.
    await _receive(mesh["H"], _event(c, FET.SPACE_TASK_CREATED, tampered, mesh["H"]))
    assert await _tasks(mesh["H"]) == []
    assert held["status"] == "pending"


async def test_admin_only_in_between_refuses_a_moderators_release(mesh):
    h, c = mesh["H"], mesh["C"]
    item_id = await _submit_task(mesh)
    await _pump(mesh)
    for house in (h, c):
        await house.db.enqueue(
            "UPDATE spaces SET tasks_access='admin_only' WHERE id=?", (SID,)
        )
    r = await c.tc.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve", json={}, headers=c.headers
    )
    assert r.status == 403, await r.text()
    assert (await r.json())["error"]["code"] == "ACCESS_ADMIN_ONLY"
    # Content released under the block anyway is refused where admin-only.
    target = (
        await c.db.fetchone(
            "SELECT json_extract(payload_json, '$.target_id') AS t"
            " FROM space_moderation_queue WHERE id=?",
            (item_id,),
        )
    )["t"]
    released = _released_task(mesh, item_id=item_id, approved_by=c.user_id)
    released["id"] = target
    await _receive(h, _event(c, FET.SPACE_TASK_CREATED, released, h))
    assert await _tasks(h) == []


# ── Convergence ─────────────────────────────────────────────────────────


async def test_approve_beats_reject_whatever_the_order(mesh):
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    item_id = await _submit_task(mesh)
    await _pump(mesh)
    # C's moderator rejects while the host's owner approves (an equal or
    # higher role may overturn, M4); every household sees the rejection first.
    r = await c.tc.post(
        f"/api/spaces/{SID}/moderation/{item_id}/reject",
        json={"reason": "no"},
        headers=c.headers,
    )
    assert r.status == 200, await r.text()
    await _pump(mesh, only={FET.SPACE_MODERATION_DECIDED})
    await _approve(h, item_id)
    await _pump(mesh)
    for house in (a, h, c, d):
        assert await _tasks(house) == [("Buy milk", a.user_id)], house.name
    for house in (a, h, c):
        (row,) = await _queue_rows(house)
        assert row["status"] == "approved", house.name


async def test_two_households_approving_converge_on_one_row(mesh):
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    item_id = await _submit_task(mesh)
    await _pump(mesh)
    await _approve(h, item_id)
    await _approve(c, item_id)
    await _pump(mesh)
    for house in (a, h, c, d):
        assert await _tasks(house) == [("Buy milk", a.user_id)], house.name


# ── Limits ──────────────────────────────────────────────────────────────


async def test_a_stub_whose_host_is_below_v43_cannot_submit(mesh):
    a, h = mesh["A"], mesh["H"]
    VERSIONS[h.iid] = 42
    r = await a.tc.post(
        f"/api/spaces/{SID}/tasks/lists/{mesh['list_id']}/tasks",
        json={"title": "x"},
        headers=a.headers,
    )
    assert r.status == 409, await r.text()
    assert (await r.json())["error"]["code"] == "HOST_TOO_OLD"
    assert await _queue_rows(a) == []
    assert a.sent == []


async def test_an_approver_household_below_v43_is_skipped(mesh):
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    VERSIONS[c.iid] = 42
    await _submit_task(mesh)
    assert [to for to, _p in _sends(a, FET.SPACE_MODERATION_SUBMITTED)] == [h.iid]


# ── The announce card of a reviewed member's event (#790 concern 4) ─────


async def test_an_approved_announce_card_reaches_every_household(mesh):
    """Calendar open, posts reviewed: anna's event lands everywhere at once,
    its feed card waits for review, and once C approves it the card — linked
    to the event — reaches every household (no bridge minted one)."""
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    for house in (a, h, c, d):
        await house.db.enqueue(
            "UPDATE spaces SET calendar_access='open' WHERE id=?", (SID,)
        )
    start = datetime.now(timezone.utc) + timedelta(days=3)
    r = await a.tc.post(
        f"/api/spaces/{SID}/calendar/events",
        json={
            "summary": "Picnic",
            "start": start.isoformat(),
            "end": (start + timedelta(hours=2)).isoformat(),
            "announce_in_feed": True,
        },
        headers=a.headers,
    )
    assert r.status in (200, 201), await r.text()
    event_id = (await r.json())["id"]
    await _pump(mesh)
    (card,) = await _queue_rows(c)
    assert card["feature"] == "posts"
    for house in (a, h, c, d):
        assert await house.db.fetchall("SELECT id FROM space_posts", ()) == []
    await _approve(c, card["id"])
    await _pump(mesh)
    for house in (a, h, c, d):
        rows = await house.db.fetchall(
            "SELECT author, type, linked_event_id FROM space_posts", ()
        )
        assert [tuple(r) for r in rows] == [(a.user_id, "event", event_id)], house.name


async def test_a_released_card_linking_an_event_this_space_lacks_is_refused(mesh):
    c, d = mesh["C"], mesh["D"]
    forged = {
        "id": "p-card",
        "space_id": SID,
        "author": c.user_id,
        "actor_user_id": c.user_id,
        "type": "event",
        "content": "x",
        "linked_event_id": "ev-elsewhere",
        "moderation": {"item_id": "item-x", "approved_by": c.user_id},
    }
    await _receive(d, _event(c, FET.SPACE_POST_CREATED, forged, d))
    assert await d.db.fetchall("SELECT id FROM space_posts", ()) == []


async def test_the_before_values_come_from_our_copy_never_the_senders(mesh):
    """A reviewer compares an edit against the current row: a received
    item's snapshot is taken from THIS household's copy of the target."""
    a, h = mesh["A"], mesh["H"]
    r = await h.tc.post(
        f"/api/spaces/{SID}/stickies", json={"content": "Old"}, headers=h.headers
    )
    sticky_id = (await r.json())["id"]
    sub = _submission(mesh)
    sub.update(
        action="edit",
        target_id=sticky_id,
        payload={"entity": "sticky", "target_id": sticky_id, "patch": {"content": "N"}},
        snapshot='{"content": "<img src=https://tracker.example/x.gif>"}',
    )
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, sub, h))
    row = await h.db.fetchone(
        "SELECT current_snapshot FROM space_moderation_queue WHERE id=?",
        (sub["item_id"],),
    )
    assert row["current_snapshot"] == '{"content":"Old"}'


# ── Only the host releases (I2) and a release is complete (I1) ─────────


async def test_a_made_up_release_from_a_moderator_household_is_refused(mesh):
    """I2: C holds a real moderator seat, but only the host applies items —
    C's "release" of an item that never existed, in dora's name, lands
    nowhere."""
    h, a, c, d = mesh["H"], mesh["A"], mesh["C"], mesh["D"]
    forged = _released_task(
        mesh, item_id="never-existed", approved_by=c.user_id, title="Dora says hi"
    )
    forged["id"] = mint_owner_bound_id(
        SPACE_TASK_KIND, space_id=SID, owner_user_id=d.user_id
    )
    forged["created_by"] = forged["actor_user_id"] = d.user_id
    for house in (h, a, d):
        await _receive(house, _event(c, FET.SPACE_TASK_CREATED, forged, house))
        assert await _tasks(house) == [], house.name


async def _pending_status_edit(mesh) -> tuple[str, str]:
    """anna (A) asks to mark hanna's task done; returns (task id, item id)."""
    h, a = mesh["H"], mesh["A"]
    r = await h.tc.post(
        f"/api/spaces/{SID}/tasks/lists/{mesh['list_id']}/tasks",
        json={"title": "Host task"},
        headers=h.headers,
    )
    assert r.status in (200, 201), await r.text()
    task_id = (await r.json())["id"]
    await _pump(mesh)
    r = await a.tc.patch(
        f"/api/spaces/{SID}/tasks/{task_id}", json={"status": "done"}, headers=a.headers
    )
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    await _pump(mesh, only={FET.SPACE_MODERATION_SUBMITTED})
    return task_id, item_id


def _edit_wire(mesh, task_id: str, item_id: str, approved_by: str, **fields) -> dict:
    h, a = mesh["H"], mesh["A"]
    return {
        "id": task_id,
        "list_id": mesh["list_id"],
        "space_id": SID,
        "title": "Host task",
        "status": "done",
        "position": 0,
        "created_by": h.user_id,
        "actor_user_id": a.user_id,
        "description": None,
        "due_date": None,
        "assignees": [],
        "priority": None,
        "labels": [],
        "archived_at": None,
        "moderation": {"item_id": item_id, "approved_by": approved_by},
        **fields,
    }


async def _task_row(house: House, task_id: str) -> tuple:
    row = await house.db.fetchone(
        "SELECT title, status, description FROM space_tasks WHERE id=?", (task_id,)
    )
    return tuple(row)


async def test_a_field_injected_outside_the_patch_is_refused(mesh):
    """I1: the item changes the status only. A release that also rewrites
    the title is refused by anna's household (which holds the item) even
    from the host, and refused everywhere from a moderator household."""
    h, a, c, d = mesh["H"], mesh["A"], mesh["C"], mesh["D"]
    task_id, item_id = await _pending_status_edit(mesh)
    before = {house.name: await _task_row(house, task_id) for house in (a, c, d)}
    injected = _edit_wire(
        mesh, task_id, item_id, c.user_id, title="Anna says: hanna is a thief"
    )
    await _receive(a, _event(h, FET.SPACE_TASK_UPDATED, injected, a))
    await _receive(c, _event(h, FET.SPACE_TASK_UPDATED, injected, c))
    for house in (a, h, d):
        await _receive(house, _event(c, FET.SPACE_TASK_UPDATED, injected, house))
    for house in (a, c, d):
        assert await _task_row(house, task_id) == before[house.name], house.name
    assert await _task_row(h, task_id) == ("Host task", "todo", None)
    # The honest release of the same item lands.
    honest = _edit_wire(mesh, task_id, item_id, c.user_id)
    await _receive(a, _event(h, FET.SPACE_TASK_UPDATED, honest, a))
    assert await _task_row(a, task_id) == ("Host task", "done", None)
    (row,) = [r for r in await _queue_rows(a) if r["id"] == item_id]
    assert row["status"] == "approved"  # the release moved the held row


async def test_a_create_field_the_item_never_set_is_refused(mesh):
    """I1: a release of anna's task that adds a recurrence (a field the
    item never set) is not that item's release."""
    h, a, c = mesh["H"], mesh["A"], mesh["C"]
    item_id = await _submit_task(mesh)
    await _pump(mesh, only={FET.SPACE_MODERATION_SUBMITTED})
    row = await a.db.fetchone(
        "SELECT json_extract(payload_json, '$.target_id') AS t"
        " FROM space_moderation_queue WHERE id=?",
        (item_id,),
    )
    released = _released_task(mesh, item_id=item_id, approved_by=c.user_id)
    released["id"] = row["t"]
    released["recurrence"] = {"rrule": "FREQ=DAILY", "last_spawned_at": None}
    await _receive(a, _event(h, FET.SPACE_TASK_CREATED, released, a))
    assert await _tasks(a) == []
    released["archived_at"] = "2026-06-02T00:00:00+00:00"
    released.pop("recurrence")
    await _receive(a, _event(h, FET.SPACE_TASK_CREATED, released, a))
    assert await _tasks(a) == []


async def test_an_advertised_downgrade_cannot_skip_review(mesh):
    """C1: a member household that advertised v_43 and then "downgrades" to
    v_42 stays v_43 (a high-water mark), so its direct post is refused."""
    h, a = mesh["H"], mesh["A"]
    for version in (43, 42):
        await _receive(
            h,
            _event(
                a,
                FET.INSTANCE_CAPABILITIES_UPDATED,
                {"proto_version": version},
                h,
            ),
        )
    row = await h.db.fetchone(
        "SELECT proto_version FROM remote_instances WHERE id=?", (a.iid,)
    )
    assert row["proto_version"] == 43
    post = {
        "id": mint_owner_bound_id(
            SPACE_POST_KIND, space_id=SID, owner_user_id=a.user_id
        ),
        "space_id": SID,
        "author": a.user_id,
        "actor_user_id": a.user_id,
        "type": "text",
        "content": "unreviewed!",
    }
    await _receive(h, _event(a, FET.SPACE_POST_CREATED, post, h))
    assert await h.db.fetchall("SELECT id FROM space_posts", ()) == []


async def test_even_a_genuine_v42_member_cannot_post_past_review(mesh):
    """With review on, no household's plain member posts directly — the
    level could only be set with every household on v_43 (or forced)."""
    h, a = mesh["H"], mesh["A"]
    await h.db.enqueue(
        "UPDATE remote_instances SET proto_version=42 WHERE id=?", (a.iid,)
    )
    post = {
        "id": mint_owner_bound_id(
            SPACE_POST_KIND, space_id=SID, owner_user_id=a.user_id
        ),
        "space_id": SID,
        "author": a.user_id,
        "actor_user_id": a.user_id,
        "type": "text",
        "content": "unreviewed!",
    }
    await _receive(h, _event(a, FET.SPACE_POST_CREATED, post, h))
    assert await h.db.fetchall("SELECT id FROM space_posts", ()) == []


# ── Decisions, seats and retries ────────────────────────────────────────


async def test_a_decision_before_its_submission_leaves_a_tombstone(mesh):
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    sub = _submission(mesh)
    decision = {
        "space_id": SID,
        "item_id": sub["item_id"],
        "decision": "rejected",
        "decided_by": c.user_id,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    await _receive(h, _event(c, FET.SPACE_MODERATION_DECIDED, decision, h))
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, sub, h))
    rows = await h.db.fetchall(
        "SELECT status, payload_json FROM space_moderation_queue WHERE id=?",
        (sub["item_id"],),
    )
    assert [r["status"] for r in rows] == ["rejected"]
    r = await h.tc.get(f"/api/spaces/{SID}/moderation", headers=h.headers)
    assert await r.json() == []


async def test_a_household_that_stops_reviewing_drops_others_pending_items(mesh):
    a, c = mesh["A"], mesh["C"]
    await _submit_task(mesh)
    await _pump(mesh, only={FET.SPACE_MODERATION_SUBMITTED})
    assert [r["status"] for r in await _queue_rows(c)] == ["pending"]
    # The host demotes carl: SPACE_MEMBER_ROLE_CHANGED reaches C.
    await _receive(
        c,
        _event(
            mesh["H"],
            FET.SPACE_MEMBER_ROLE_CHANGED,
            {"space_id": SID, "user_id": c.user_id, "role": "member"},
            c,
        ),
    )
    rows = await c.db.fetchall(
        "SELECT status, payload_json FROM space_moderation_queue", ()
    )
    assert [(r["status"], r["payload_json"]) for r in rows] == [("expired", None)]
    # anna's own copy on A is hers and stays.
    assert [r["status"] for r in await _queue_rows(a)] == ["pending"]


async def test_a_demoted_moderator_household_is_no_longer_a_target(mesh):
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    await a.db.enqueue(
        "UPDATE space_remote_members SET role='member' WHERE instance_id=?", (c.iid,)
    )
    await _submit_task(mesh)
    assert [to for to, _p in _sends(a, FET.SPACE_MODERATION_SUBMITTED)] == [h.iid]


async def test_a_queued_retry_to_a_former_reviewer_is_dropped(mesh):
    a, c = mesh["A"], mesh["C"]
    moderation = a.app[space_moderation_service_key]
    entry = OutboxEntry(
        id="o-1",
        instance_id=c.iid,
        event_type=FET.SPACE_MODERATION_SUBMITTED,
        payload_json=json.dumps({"space_id": SID}),
        status="pending",
        attempts=1,
        next_attempt_at="",
        created_at="",
    )
    assert await moderation.outbox_entry_wanted(entry)
    await a.db.enqueue(
        "UPDATE space_remote_members SET role='member' WHERE instance_id=?", (c.iid,)
    )
    assert not await moderation.outbox_entry_wanted(entry)


async def test_a_pending_posts_media_goes_to_the_reviewers_only(mesh):
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    r = await a.tc.post(
        f"/api/spaces/{SID}/posts",
        json={"type": "image", "image_urls": ["api/media/pending-pic.webp"]},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    rows = await a.db.fetchall(
        "SELECT target_instance_id FROM space_media_outbox WHERE blob_id=?",
        ("pending-pic.webp",),
    )
    assert sorted(r["target_instance_id"] for r in rows) == sorted([h.iid, c.iid])
    assert d.iid not in {r["target_instance_id"] for r in rows}


# ── Inbound limits ──────────────────────────────────────────────────────


async def test_the_submitter_and_space_caps_refuse_the_next_item(mesh):
    a, h = mesh["A"], mesh["H"]
    for n in range(MAX_PENDING_PER_SUBMITTER):
        await h.db.enqueue(
            "INSERT INTO space_moderation_queue(id, space_id, feature, action,"
            " submitted_by, payload_json, expires_at) VALUES(?, ?, 'stickies',"
            " 'create', ?, '{}', '2099-01-01T00:00:00+00:00')",
            (f"seed-{n}", SID, a.user_id),
        )
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, _submission(mesh), h))
    assert len(await _queue_rows(h)) == MAX_PENDING_PER_SUBMITTER
    await h.db.enqueue("DELETE FROM space_moderation_queue", ())
    for n in range(MAX_PENDING_PER_SPACE):
        await h.db.enqueue(
            "INSERT INTO space_moderation_queue(id, space_id, feature, action,"
            " submitted_by, payload_json, expires_at) VALUES(?, ?, 'stickies',"
            " 'create', ?, '{}', '2099-01-01T00:00:00+00:00')",
            (f"seed-s{n}", SID, f"u-other-{n}"),
        )
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, _submission(mesh), h))
    assert len(await _queue_rows(h)) == MAX_PENDING_PER_SPACE


async def test_an_oversized_or_expired_submission_is_refused(mesh):
    a, h = mesh["A"], mesh["H"]
    big = _submission(mesh)
    big["payload"]["content"] = "x" * (MAX_PAYLOAD_BYTES + 1)
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, big, h))
    late = _submission(mesh)
    late["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, late, h))
    assert await _queue_rows(h) == []


async def test_a_far_expiry_and_a_future_submit_time_are_clamped(mesh):
    a, h = mesh["A"], mesh["H"]
    sub = _submission(mesh)
    now = datetime.now(timezone.utc)
    sub["expires_at"] = (now + timedelta(days=90)).isoformat()
    sub["submitted_at"] = (now + timedelta(days=30)).isoformat()
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, sub, h))
    item = await h.app[space_moderation_service_key].get_item(SID, sub["item_id"])
    assert item.expires_at <= datetime.now(timezone.utc) + MAX_EXPIRY
    assert item.submitted_at <= datetime.now(timezone.utc) + timedelta(minutes=5)
    assert item.submitted_at >= item.expires_at - MAX_EXPIRY


# ── The real targeted door seals a mesh-routed submission ──────────────


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


async def test_a_submission_to_an_unpaired_reviewer_rides_space_routed(
    mesh, monkeypatch
):
    """The real ``send_with_mesh_fallback``: a reviewer household this one
    is not directly paired with gets the item only inside ``SPACE_ROUTED``,
    sealed for that target — the relay sees ciphertext only."""
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _REAL_SEND)
    fed = a.app[federation_service_key]
    routed = _Routed()
    fed.attach_mesh(route_service=_Route(), routed_handler=routed)
    for iid in (h.iid, c.iid):
        await a.db.enqueue(
            "UPDATE remote_instances SET status='pending_sent' WHERE id=?", (iid,)
        )
    await _submit_task(mesh)
    inner = [
        r
        for r in routed.sent
        if r["inner_event_type"] is FET.SPACE_MODERATION_SUBMITTED
    ]
    assert sorted(r["path"][-1] for r in inner) == sorted([h.iid, c.iid])
    assert all(r["target_eph_pk_b64"] == "eph-pk" for r in inner)
    assert all(r["inner_payload"]["submitted_by"] == a.user_id for r in inner)
    assert not _sends(a, FET.SPACE_MODERATION_SUBMITTED)  # nothing sent in the clear


# ── Tombstones (M2, M3) and who may overturn a rejection (M4) ──────────


def _decision(item_id: str, by: str, decision: str = "approved") -> dict:
    return {
        "space_id": SID,
        "item_id": item_id,
        "decision": decision,
        "decided_by": by,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }


async def test_a_late_submission_fills_its_tombstone_and_keeps_the_decision(mesh):
    """M2: C rejected before the host heard of the item. The submission
    then fills in the content but the rejection stands — and an owner may
    still overturn a moderator's rejection (M4)."""
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    item_id = await _submit_task(mesh)
    await _receive(
        h,
        _event(
            c,
            FET.SPACE_MODERATION_DECIDED,
            _decision(item_id, c.user_id, "rejected"),
            h,
        ),
    )
    await _pump(mesh, only={FET.SPACE_MODERATION_SUBMITTED})
    row = await h.db.fetchone(
        "SELECT status, feature, submitted_by FROM space_moderation_queue WHERE id=?",
        (item_id,),
    )
    assert tuple(row) == ("rejected", "tasks", a.user_id)
    r = await h.tc.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve", json={}, headers=h.headers
    )
    assert r.status == 200, await r.text()
    await _pump(mesh)
    assert await _tasks(a) == [("Buy milk", a.user_id)]


async def test_approving_a_contentless_tombstone_is_409_already_decided(mesh):
    h, c = mesh["H"], mesh["C"]
    await _receive(
        h,
        _event(
            c,
            FET.SPACE_MODERATION_DECIDED,
            _decision("ghost", c.user_id, "rejected"),
            h,
        ),
    )
    r = await h.tc.post(
        f"/api/spaces/{SID}/moderation/ghost/approve", json={}, headers=h.headers
    )
    assert r.status == 409, await r.text()
    assert (await r.json())["error"]["code"] == "ALREADY_DECIDED"


async def test_early_decision_tombstones_are_capped_per_household(mesh):
    h, c = mesh["H"], mesh["C"]
    for n in range(MAX_TOMBSTONES_PER_HOUSEHOLD + 5):
        await _receive(
            h,
            _event(
                c,
                FET.SPACE_MODERATION_DECIDED,
                _decision(f"junk{n}", c.user_id, "rejected"),
                h,
            ),
        )
    row = await h.db.fetchone(
        "SELECT COUNT(*) AS n FROM space_moderation_queue WHERE feature=''", ()
    )
    assert row["n"] == MAX_TOMBSTONES_PER_HOUSEHOLD


async def test_stale_tombstones_are_purged(mesh):
    h, c = mesh["H"], mesh["C"]
    await _receive(
        h,
        _event(
            c, FET.SPACE_MODERATION_DECIDED, _decision("old", c.user_id, "rejected"), h
        ),
    )
    moderation = h.app[space_moderation_service_key]
    await moderation.purge_decided(datetime.now(timezone.utc) + timedelta(days=15))
    assert await h.db.fetchall("SELECT id FROM space_moderation_queue", ()) == []


async def test_a_moderator_cannot_overturn_the_owners_rejection(mesh):
    """M4: the owner rejected; a moderator's later approval does not
    publish it — the rejection stands."""
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    item_id = await _submit_task(mesh)
    await _pump(mesh)
    r = await h.tc.post(
        f"/api/spaces/{SID}/moderation/{item_id}/reject",
        json={"reason": "no"},
        headers=h.headers,
    )
    assert r.status == 200, await r.text()
    await _receive(
        h, _event(c, FET.SPACE_MODERATION_DECIDED, _decision(item_id, c.user_id), h)
    )
    await _pump(mesh)
    for house in (a, h, c):
        assert await _tasks(house) == [], house.name
        (row,) = await _queue_rows(house)
        assert row["status"] == "rejected", house.name


async def test_an_admin_overturns_a_moderators_rejection(mesh):
    """M4: a moderator rejected; an admin (higher) approves — published."""
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    # Dora is an admin (on her household and in the host's mirror).
    await d.db.enqueue(
        "UPDATE space_members SET role='admin' WHERE user_id=?", (d.user_id,)
    )
    for house in (a, h, c):
        await house.db.enqueue(
            "UPDATE space_remote_members SET role='admin' WHERE user_id=?",
            (d.user_id,),
        )
    item_id = await _submit_task(mesh)
    await _pump(mesh)
    await _receive(
        h,
        _event(
            c,
            FET.SPACE_MODERATION_DECIDED,
            _decision(item_id, c.user_id, "rejected"),
            h,
        ),
    )
    await _receive(
        h, _event(d, FET.SPACE_MODERATION_DECIDED, _decision(item_id, d.user_id), h)
    )
    await _pump(mesh)
    assert await _tasks(h) == [("Buy milk", a.user_id)]
    assert await _tasks(a) == [("Buy milk", a.user_id)]


async def test_a_signed_copy_rides_next_to_the_payload_and_only_verified(mesh):
    """A queued public post's author-signed copy (for GFS followers) is kept
    only when it verifies, and only from beside the payload — one smuggled
    INSIDE the payload is dropped before the codecs see it."""
    a, h = mesh["A"], mesh["H"]
    await h.db.enqueue(
        "UPDATE spaces SET space_type='global', allow_subscribers=1 WHERE id=?",
        (SID,),
    )
    target = mint_owner_bound_id(SPACE_POST_KIND, space_id=SID, owner_user_id=a.user_id)
    now = datetime.now(timezone.utc)
    smuggled = {"post_id": target, "author_sig": "AAAA"}
    submission = {
        "space_id": SID,
        "item_id": f"item-{next(_IDS)}",
        "feature": "posts",
        "action": "create",
        "target_id": target,
        "submitted_by": a.user_id,
        "payload": {
            "entity": "post",
            "target_id": target,
            "post_id": target,
            "type": "text",
            "content": "queued words",
            "public_relay": smuggled,
        },
        # Not verifiable (the mesh's user ids derive from no key): dropped.
        "public_relay": {**smuggled, "space_id": SID, "author_user_id": a.user_id},
        "snapshot": None,
        "submitted_at": now.isoformat(),
        "expires_at": (now + timedelta(days=7)).isoformat(),
    }
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, submission, h))
    rows = await h.db.fetchall(
        "SELECT payload_json FROM space_moderation_queue WHERE submitted_by=?",
        (a.user_id,),
    )
    assert len(rows) == 1
    assert "public_relay" not in json.loads(rows[0]["payload_json"])
