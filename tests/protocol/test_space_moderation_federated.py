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

import asyncio
import base64
import copy
import dataclasses
import itertools
import json
import pathlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    config_key,
    db_key,
    federation_service_key,
    space_moderation_service_key,
    space_page_service_key,
    space_remote_member_repo_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
)
from socialhome.domain.federation_capabilities import OURS
from socialhome.domain.outbox import OutboxEntry
from socialhome.domain.space import ContentQueuedForReview
from socialhome.domain.space_media_ref import space_page_media_name
from socialhome.federation.federation_service import FederationService
from socialhome.federation.gfs_relay_transport import (
    RELAY_MAX_ENVELOPE_BYTES,
)
from socialhome.federation.inbound_validator import (
    InboundContext,
    run_post_decrypt_gates,
)
from socialhome.federation.owner_bound_id import (
    SPACE_POST_KIND,
    SPACE_STICKY_KIND,
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    mint_owner_bound_id,
)
from socialhome.services import space_moderation_federation as moderation_federation
from socialhome.services.inbound_media_store import space_direct_media_name
from socialhome.services.space_moderation_federation import (
    MAX_EXPIRY,
    MAX_PENDING_PER_HOUSEHOLD,
)
from socialhome.services.space_moderation_service import (
    MAX_PAYLOAD_BYTES,
    MAX_PENDING_PER_SPACE,
    MAX_PENDING_PER_SUBMITTER,
    MAX_TOMBSTONES_PER_HOUSEHOLD,
)

pytestmark = pytest.mark.security

#: The real targeted door, before the fixture captures it (the sealing test).
_REAL_SEND = FederationService.send_with_mesh_fallback
_REAL_PEER_SUPPORTS = FederationService.peer_supports
_REAL_MEMBER_SUPPORTS = FederationService.space_member_supports

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
    now = datetime.now(UTC).isoformat()
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
        timestamp=datetime.now(UTC).isoformat(),
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
    start = datetime.now(UTC) + timedelta(days=2)
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
    start = datetime.now(UTC) + timedelta(days=2)
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


async def test_a_reviewed_picture_keeps_one_file_per_delivering_household(mesh):
    """A space file takes, on each household, the name of the household that
    delivered it (``SpaceMediaNamer``): the reviewers hold the submitter's
    picture under the submitter's name, the host's release names it as the
    host holds it, and every household — the submitter's own included —
    still takes the release (its comparison aligns the names)."""
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    r = await a.tc.post(
        f"/api/spaces/{SID}/posts",
        json={"type": "image", "image_urls": ["api/media/pic.webp"]},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    await _pump(mesh)
    from_a = space_direct_media_name(SID, a.iid, "pic.webp")
    for reviewer in (h, c):
        row = await reviewer.db.fetchone(
            "SELECT payload_json FROM space_moderation_queue WHERE id=?", (item_id,)
        )
        assert from_a in row["payload_json"], reviewer.name
        assert "api/media/pic.webp" not in row["payload_json"], reviewer.name
    await _approve(c, item_id)
    await _pump(mesh, only={FET.SPACE_MODERATION_DECIDED}, once=True)
    released = _sends(h, FET.SPACE_POST_CREATED)
    assert {tuple(p["image_urls"]) for _to, p in released} == {(f"api/media/{from_a}",)}
    await _pump(mesh)
    from_h = space_direct_media_name(SID, h.iid, from_a)
    expected = {"H": from_a, "A": from_h, "C": from_h, "D": from_h}
    for house in (a, h, c, d):
        rows = await house.db.fetchall("SELECT image_urls_json FROM space_posts", ())
        assert [x["image_urls_json"] for x in rows] == [
            f'["api/media/{expected[house.name]}"]'
        ], house.name
    # The host ships the bytes under the name its rows hold.
    shipped = await h.db.fetchall(
        "SELECT target_instance_id FROM space_media_outbox WHERE blob_id=?", (from_a,)
    )
    assert {x["target_instance_id"] for x in shipped} >= {a.iid, d.iid}


async def _submit_event_with_cover(mesh, cover: str = "api/media/cover.webp") -> str:
    a = mesh["A"]
    start = datetime.now(UTC) + timedelta(days=2)
    r = await a.tc.post(
        f"/api/spaces/{SID}/calendar/events",
        json={
            "summary": "Picnic",
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
            "cover_url": cover,
        },
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    return (await r.json())["item_id"]


async def _covers(house: House) -> list[str | None]:
    rows = await house.db.fetchall("SELECT cover_url FROM space_calendar_events", ())
    return [x["cover_url"] for x in rows]


@pytest.mark.parametrize("approver", ["H", "C"])
async def test_a_reviewed_event_with_a_cover_from_another_household_is_released(
    mesh, approver
):
    """Regression: the host refused to release a remote member's event with
    a cover (``InvalidMediaRefError``). The queue item is the only row that
    names the submitter's file, and the approve claims it (``approved``)
    before the apply re-checks the cover — which then counted only a
    *pending* item as the submitter's. Released by the host itself or by a
    moderator elsewhere, the event lands on every household with the cover
    under the delivering household's name, and the host ships the bytes to
    every other member household."""
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    item_id = await _submit_event_with_cover(mesh)
    await _pump(mesh)
    from_a = space_direct_media_name(SID, a.iid, "cover.webp")
    row = await h.db.fetchone(
        "SELECT payload_json FROM space_moderation_queue WHERE id=?", (item_id,)
    )
    assert f"api/media/{from_a}" in row["payload_json"]
    await _approve(mesh[approver], item_id)
    await _pump(mesh, only={FET.SPACE_MODERATION_DECIDED}, once=True)
    (status,) = [r["status"] for r in await _queue_rows(h)]
    assert status == "approved"
    released = _sends(h, FET.SPACE_CALENDAR_EVENT_CREATED)
    assert sorted(to for to, _p in released) == sorted([a.iid, c.iid, d.iid])
    assert {p["cover_url"] for _to, p in released} == {f"api/media/{from_a}"}
    await _pump(mesh)
    from_h = space_direct_media_name(SID, h.iid, from_a)
    expected = {"H": from_a, "A": from_h, "C": from_h, "D": from_h}
    for house in (a, h, c, d):
        assert await _covers(house) == [f"api/media/{expected[house.name]}"], house.name
    # The host ships the cover file to every other member household.
    shipped = await h.db.fetchall(
        "SELECT target_instance_id FROM space_media_outbox WHERE blob_id=?", (from_a,)
    )
    assert {x["target_instance_id"] for x in shipped} >= {a.iid, c.iid, d.iid}


async def test_a_reviewed_cover_naming_another_spaces_file_never_reaches_it(mesh):
    """A remote member that names a file it never delivered — here a file
    only another space's post on the host shows — is never given the
    host's file: the submission takes the submitter household's own name
    for it, so the release points at that household's copy, not ours."""
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    await h.db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES('sp-other', 'Other', ?, 'hanna', ?)",
        (h.iid, "cd" * 32),
    )
    await h.db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role)"
        " VALUES('sp-other', ?, 'owner')",
        (h.user_id,),
    )
    await h.db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, media_url)"
        " VALUES('p-other', 'sp-other', ?, 'image', 'api/media/secret.webp')",
        (h.user_id,),
    )
    item_id = await _submit_event_with_cover(mesh, "api/media/secret.webp")
    await _pump(mesh)
    await _approve(c, item_id)
    await _pump(mesh, only={FET.SPACE_MODERATION_DECIDED}, once=True)
    from_a = space_direct_media_name(SID, a.iid, "secret.webp")
    assert await _covers(h) == [f"api/media/{from_a}"]
    released = _sends(h, FET.SPACE_CALENDAR_EVENT_CREATED)
    assert released
    assert all("secret.webp" not in json.dumps(p) for _to, p in released)
    shipped = await h.db.fetchall(
        "SELECT blob_id FROM space_media_outbox WHERE blob_id='secret.webp'", ()
    )
    assert shipped == []


# ── A reviewed page's pictures (v_82) ───────────────────────────────────

PAGE_FILES = {"c.webp": b"cover bytes", "p1.webp": b"one", "p2.webp": b"two"}


async def _fake_media_chunk(
    self, *, to_instance_id, event_type, payload, raw_chunk, **_kw
):
    """A media chunk, captured like any send (base64 on the JSON path)."""
    OUTBOX.setdefault(self._own_instance_id, []).append(
        (
            to_instance_id,
            event_type,
            {
                **copy.deepcopy(payload),
                "bytes_b64": base64.b64encode(raw_chunk).decode(),
            },
        )
    )
    return DeliveryResult(instance_id=to_instance_id, ok=True)


def _media_sync(house: House):
    return house.app[space_moderation_service_key]._federated._media_sync


def _media_dir(house: House) -> pathlib.Path:
    return pathlib.Path(house.app[config_key].media_path)


async def _ship_files(mesh, house: House) -> list[tuple[str, str]]:
    """``house`` ships its queued space files now; every household receives
    them. Returns what was queued: ``(file, target household)``."""
    rows = await house.db.fetchall(
        "SELECT blob_id, target_instance_id FROM space_media_outbox", ()
    )
    await _media_sync(house).flush_once(limit=200)
    await _pump(mesh)
    return sorted((r["blob_id"], r["target_instance_id"]) for r in rows)


def _kept_as(mesh, house: House, name: str) -> str:
    """Where *house* keeps the file a page shows as *name*: the host under
    the name itself, a member household page-scoped (v_82)."""
    return name if house is mesh["H"] else space_page_media_name(SID, name)


def _file(mesh, house: House, name: str) -> bytes | None:
    path = _media_dir(house) / _kept_as(mesh, house, name)
    return path.read_bytes() if path.is_file() else None


async def _pages(house: House) -> list[tuple[str | None, str]]:
    rows = await house.db.fetchall(
        "SELECT cover_image_url, content FROM space_pages WHERE space_id=?", (SID,)
    )
    return [(r["cover_image_url"], r["content"]) for r in rows]


@pytest.fixture
async def page_mesh(mesh, monkeypatch):
    """The mesh with every household's file shipping driven by the test."""
    monkeypatch.setattr(FederationService, "send_media_chunk", _fake_media_chunk)
    for house in _by_iid(mesh).values():
        await _media_sync(house).stop()
        VERSIONS[house.iid] = OURS
    a = mesh["A"]
    _media_dir(a).mkdir(parents=True, exist_ok=True)
    for name, data in PAGE_FILES.items():
        (_media_dir(a) / name).write_bytes(data)
    return mesh


async def _submit_page(mesh) -> tuple[str, str]:
    """anna's page with a cover (the service: the create route takes no
    cover) and two body pictures, queued for review."""
    a = mesh["A"]
    body = "Rules ![one](api/media/p1.webp) and ![two](/api/media/p2.webp)"
    with pytest.raises(ContentQueuedForReview) as queued:
        await a.app[space_page_service_key].create(
            SID,
            actor_user_id=a.user_id,
            title="Rules",
            content=body,
            cover_image_url="api/media/c.webp",
        )
    return queued.value.item.id, body


@pytest.mark.parametrize("approver", ["H", "C"])
async def test_a_reviewed_page_with_pictures_from_another_household_reaches_all(
    page_mesh, approver
):
    """Regression (members equal): a remote member's reviewed page with a
    cover and two body pictures kept the submitter household's names, and
    nobody shipped the files — broken on the host and every other member.
    Now the files take the submitter household's names at admission (the
    body rewritten to them), the host releases the page byte for byte
    under those names, and ships the three files to every other member
    household, which shows all three."""
    mesh = page_mesh
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    item_id, _body = await _submit_page(mesh)
    await _pump(mesh)
    # The submitter's household ships the files to the reviewers only.
    sent = await _ship_files(mesh, a)
    assert {to for _f, to in sent} == {h.iid, c.iid}
    from_a = {n: space_direct_media_name(SID, a.iid, n) for n in PAGE_FILES}
    for reviewer in (h, c):
        row = await reviewer.db.fetchone(
            "SELECT payload_json FROM space_moderation_queue WHERE id=?", (item_id,)
        )
        for name, local in from_a.items():
            assert f"api/media/{local}" in row["payload_json"], reviewer.name
            assert f"api/media/{name}" not in row["payload_json"], reviewer.name
    await _approve(mesh[approver], item_id)
    await _pump(mesh)
    shown_body = (
        f"Rules ![one](api/media/{from_a['p1.webp']}) and "
        f"![two](/api/media/{from_a['p2.webp']})"
    )
    for house in (a, h, c, d):
        assert await _pages(house) == [(f"api/media/{from_a['c.webp']}", shown_body)], (
            house.name
        )
    # The host ships the three files to every other member household.
    shipped = await _ship_files(mesh, h)
    assert shipped == sorted(
        (local, iid) for local in from_a.values() for iid in (a.iid, c.iid, d.iid)
    )
    for house in (a, h, c, d):
        for name, local in from_a.items():
            path = _media_dir(house) / _kept_as(mesh, house, local)
            assert path.read_bytes() == PAGE_FILES[name], (house.name, name)


@pytest.mark.parametrize("editor", ["A", "D"])
async def test_an_edit_of_a_reviewed_page_keeps_its_pictures_and_ships_new_ones(
    page_mesh, editor
):
    """The released page is edited — by its author (A: a page proposal to
    the host, not reviewed) or by another member (D: a reviewed edit). The
    picture the edit keeps stays under the name the page shows (never
    renamed again), the new one takes the editor household's name, the
    editor ships it to the host, and the host ships only the new one to
    every other member household."""
    mesh = page_mesh
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    item_id, _body = await _submit_page(mesh)
    await _pump(mesh)
    await _ship_files(mesh, a)
    await _approve(c, item_id)
    await _pump(mesh)
    await _ship_files(mesh, h)
    (page_id,) = [
        r["id"] for r in await a.db.fetchall("SELECT id FROM space_pages", ())
    ]
    from_a = {n: space_direct_media_name(SID, a.iid, n) for n in PAGE_FILES}
    e = mesh[editor]
    new = space_direct_media_name(SID, e.iid, "n.webp")
    (_media_dir(e) / "n.webp").write_bytes(b"new")
    edited = f"Rules ![one](api/media/{from_a['p1.webp']}) and ![new](api/media/n.webp)"
    r = await e.tc.patch(
        f"/api/spaces/{SID}/pages/{page_id}",
        json={"content": edited},
        headers=e.headers,
    )
    if editor == "D":
        assert r.status == 202, await r.text()
        edit_id = (await r.json())["item_id"]
        await _pump(mesh)
        sent = await _ship_files(mesh, e)
        assert sent == sorted([("n.webp", h.iid), ("n.webp", c.iid)])
        await _approve(c, edit_id)
    else:
        assert r.status == 200, await r.text()
        assert (await r.json())["pending"] is True
        await _pump(mesh)
        sent = await _ship_files(mesh, e)
        assert sent == [("n.webp", h.iid)]
    await _pump(mesh)
    shown_body = (
        f"Rules ![one](api/media/{from_a['p1.webp']}) and ![new](api/media/{new})"
    )
    for house in (a, h, c, d):
        assert await _pages(house) == [(f"api/media/{from_a['c.webp']}", shown_body)], (
            house.name
        )
    shipped = await _ship_files(mesh, h)
    assert {f for f, _to in shipped} == {new}
    for house in (a, h, c, d):
        assert _file(mesh, house, new) == b"new", house.name
        assert _file(mesh, house, from_a["p1.webp"]) == b"one", house.name


async def test_a_reviewed_edit_restoring_a_history_picture_keeps_its_name(page_mesh):
    """Review finding 7: a reviewed edit that brings back a picture from the
    page's history repeats the name the page showed — it is never renamed
    to the editor's household, so every household still shows it."""
    mesh = page_mesh
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    item_id, _body = await _submit_page(mesh)
    await _pump(mesh)
    await _ship_files(mesh, a)
    await _approve(c, item_id)
    await _pump(mesh)
    await _ship_files(mesh, h)
    (page_id,) = [
        r["id"] for r in await a.db.fetchall("SELECT id FROM space_pages", ())
    ]
    from_a = {n: space_direct_media_name(SID, a.iid, n) for n in PAGE_FILES}
    # The host drops the second picture; it stays in the page's history.
    r = await h.tc.patch(
        f"/api/spaces/{SID}/pages/{page_id}",
        json={"content": f"Rules ![one](api/media/{from_a['p1.webp']})"},
        headers=h.headers,
    )
    assert r.status == 200, await r.text()
    await _pump(mesh)
    # D (another member, reviewed) brings it back as the page showed it.
    restored = (
        f"Rules ![one](api/media/{from_a['p1.webp']}) "
        f"![two](api/media/{from_a['p2.webp']})"
    )
    r = await d.tc.patch(
        f"/api/spaces/{SID}/pages/{page_id}",
        json={"content": restored},
        headers=d.headers,
    )
    assert r.status == 202, await r.text()
    edit_id = (await r.json())["item_id"]
    await _pump(mesh)
    await _approve(c, edit_id)
    await _pump(mesh)
    for house in (a, h, c, d):
        assert await _pages(house) == [(f"api/media/{from_a['c.webp']}", restored)], (
            house.name
        )
        assert _file(mesh, house, from_a["p2.webp"]) == b"two", house.name


async def test_a_reviewed_page_naming_a_reserved_file_never_shows_it(page_mesh):
    """A page cover or body picture naming a DM attachment (``dm-``) is
    dropped on admission: the held item, and so the release, never names
    it."""
    mesh = page_mesh
    a, h = mesh["A"], mesh["H"]
    dm = "dm-" + "0" * 40 + ".webp"
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Rules", "content": f"![x](api/media/{dm})"},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    await _pump(mesh)
    row = await h.db.fetchone(
        "SELECT payload_json FROM space_moderation_queue WHERE id=?", (item_id,)
    )
    assert dm not in row["payload_json"]


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
    now = datetime.now(UTC)
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
        "decided_at": datetime.now(UTC).isoformat(),
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
        "decided_at": datetime.now(UTC).isoformat(),
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
        "decided_at": datetime.now(UTC).isoformat(),
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
    start = datetime.now(UTC) + timedelta(days=3)
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
        "decided_at": datetime.now(UTC).isoformat(),
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
    # Migration 0145: a ref row carries no envelope — its space comes from
    # the stored payload it references.
    ref = dataclasses.replace(entry, payload_json="", payload_id="p-1", space_id=SID)
    assert await moderation.outbox_entry_wanted(entry)
    assert await moderation.outbox_entry_wanted(ref)
    await a.db.enqueue(
        "UPDATE space_remote_members SET role='member' WHERE instance_id=?", (c.iid,)
    )
    assert not await moderation.outbox_entry_wanted(entry)
    assert not await moderation.outbox_entry_wanted(ref)


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
    late["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, late, h))
    assert await _queue_rows(h) == []


async def test_a_far_expiry_and_a_future_submit_time_are_clamped(mesh):
    a, h = mesh["A"], mesh["H"]
    sub = _submission(mesh)
    now = datetime.now(UTC)
    sub["expires_at"] = (now + timedelta(days=90)).isoformat()
    sub["submitted_at"] = (now + timedelta(days=30)).isoformat()
    await _receive(h, _event(a, FET.SPACE_MODERATION_SUBMITTED, sub, h))
    item = await h.app[space_moderation_service_key].get_item(SID, sub["item_id"])
    assert item.expires_at <= datetime.now(UTC) + MAX_EXPIRY
    assert item.submitted_at <= datetime.now(UTC) + timedelta(minutes=5)
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
        "decided_at": datetime.now(UTC).isoformat(),
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
    await moderation.purge_decided(datetime.now(UTC) + timedelta(days=15))
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
    now = datetime.now(UTC)
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


# ── Catch-up for reviewer households (the §25.6 BEGIN echo) ─────────────


def _federated(house: House):
    return house.app[space_moderation_service_key]._federated


async def _begin(mesh, house: House, *, echo: object = None) -> None:
    """``house``'s periodic §25.6 ``SPACE_SYNC_BEGIN`` reaches the host: it
    echoes the moderation items ``house`` holds, by state (``echo``
    overrides what it would send), and the host's catch-up answers it."""
    h = mesh["H"]
    payload: dict = {"sync_id": "s-1", "space_id": SID, "sync_mode": "incremental"}
    if echo is None:
        ask = getattr(_federated(house), "held_echo", None)
        echo = await ask(SID, h.iid) if ask is not None else None
    if echo is not None:
        payload["mod_held"] = echo
    answer = getattr(_federated(h), "on_sync_begin", None)
    if answer is not None:
        await answer(_event(house, FET.SPACE_SYNC_BEGIN, payload, h))


#: What a role change makes the host send (and nothing of the queue).
_ROSTER_EVENTS = {
    FET.SPACE_MEMBER_ROLE_CHANGED,
    FET.SPACE_MEMBER_JOINED,
    FET.SPACE_ROSTER_SNAPSHOT,
}


async def _promote(mesh, house: House, role: str = "moderator") -> None:
    """The host's owner gives ``house``'s person ``role`` through the real
    REST path; every household applies what the host sends for it."""
    h = mesh["H"]
    r = await h.tc.patch(
        f"/api/spaces/{SID}/remote-members/{house.iid}/{house.user_id}",
        json={"role": role},
        headers=h.headers,
    )
    assert r.status == 200, await r.text()
    await _pump(mesh, only=_ROSTER_EVENTS)
    for sender in _by_iid(mesh).values():
        sender.sent[:] = [s for s in sender.sent if s[1] not in _ROSTER_EVENTS]


def _drop_sends(house: House, event_type, to: House) -> int:
    """Lose what ``house`` sent ``to`` (it was offline past the retries)."""
    kept = [s for s in house.sent if not (s[0] == to.iid and s[1] is event_type)]
    lost = len(house.sent) - len(kept)
    house.sent[:] = kept
    return lost


async def test_a_household_promoted_later_gets_the_items_already_pending(mesh):
    """Gap 1: dora is made a moderator AFTER anna's item was submitted. D
    asks the host the moment it applies the promotion, the host re-sends
    the pending item to D alone, dora approves it, everyone gets the task."""
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    asked: list[str] = []

    async def _ask(space_id: str) -> None:
        asked.append(space_id)

    for house in (a, h, c, d):
        house.app[space_moderation_service_key].attach_host_sync(_ask)
    item_id = await _submit_task(mesh)
    await _pump(mesh)
    assert await _queue_rows(d) == []
    await _promote(mesh, d)
    assert asked == [SID]
    await _begin(mesh, d)
    resent = _sends(h, FET.SPACE_MODERATION_SUBMITTED)
    assert [to for to, _p in resent] == [d.iid]
    assert resent[0][1]["submitter_instance_id"] == a.iid
    await _pump(mesh)
    assert [(r["id"], r["status"]) for r in await _queue_rows(d)] == [
        (item_id, "pending")
    ]
    r = await d.tc.get(f"/api/spaces/{SID}/moderation", headers=d.headers)
    assert [i["id"] for i in await r.json()] == [item_id]
    await _approve(d, item_id)
    await _pump(mesh)
    for house in (a, h, c, d):
        assert await _tasks(house) == [("Buy milk", a.user_id)], house.name
    for house in (a, h, c, d):
        (row,) = await _queue_rows(house)
        assert row["status"] == "approved", house.name


async def test_a_reviewer_household_that_missed_a_submission_gets_it_on_sync(mesh):
    """Gap 2: C was offline past A's retries. Its next BEGIN to the host
    echoes what it holds (nothing); the host re-sends the item it lacks."""
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    item_id = await _submit_task(mesh)
    assert _drop_sends(a, FET.SPACE_MODERATION_SUBMITTED, c) == 1
    await _pump(mesh)
    assert await _queue_rows(c) == []
    await _begin(mesh, c)
    await _pump(mesh)
    assert [(r["id"], r["status"]) for r in await _queue_rows(c)] == [
        (item_id, "pending")
    ]
    # Up to date now: the next BEGIN echoes the item and nothing is sent.
    await _begin(mesh, c)
    assert not h.sent


@pytest.mark.parametrize("decision", ["rejected", "approved"])
async def test_a_missed_decision_is_caught_up_on_sync(mesh, decision):
    """Gap 3: C never got the host's decision (nor, for an approval, the
    released task): its copy reads pending until it expires. C's BEGIN
    echoes the stale id and the host sends that one decision again."""
    h, c = mesh["H"], mesh["C"]
    item_id = await _submit_task(mesh)
    await _pump(mesh)
    if decision == "approved":
        await _approve(h, item_id)
        _drop_sends(h, FET.SPACE_TASK_CREATED, c)
    else:
        r = await h.tc.post(
            f"/api/spaces/{SID}/moderation/{item_id}/reject",
            json={"reason": "not here"},
            headers=h.headers,
        )
        assert r.status == 200, await r.text()
    assert _drop_sends(h, FET.SPACE_MODERATION_DECIDED, c) == 1
    await _pump(mesh)
    assert [r["status"] for r in await _queue_rows(c)] == ["pending"]
    await _begin(mesh, c)
    again = _sends(h, FET.SPACE_MODERATION_DECIDED)
    assert [(to, p["item_id"], p["decision"]) for to, p in again] == [
        (c.iid, item_id, decision)
    ]
    await _pump(mesh)
    assert [r["status"] for r in await _queue_rows(c)] == [decision]


async def test_only_a_reviewer_household_is_caught_up(mesh):
    """A plain member household (or one whose seat is gone) echoing an
    empty set gets nothing — pending content never leaves the reviewers."""
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    await _submit_task(mesh)
    await _pump(mesh)
    await _begin(mesh, d, echo={})
    assert not h.sent
    # The submitter's own household holds its item already; a plain member
    # one is never sent anybody else's either.
    await _begin(mesh, a, echo={})
    assert not h.sent
    # A plain member household does not echo at all…
    assert await _federated(d).held_echo(SID, h.iid) is None
    assert await _federated(a).held_echo(SID, h.iid) is None
    # …nobody echoes to a household that is not the space's host, and the
    # host echoes to nobody.
    assert await _federated(c).held_echo(SID, a.iid) is None
    assert await _federated(h).held_echo(SID, c.iid) is None
    # A demoted moderator household: its live seat is a plain member's.
    await h.db.enqueue(
        "UPDATE space_remote_members SET role='member' WHERE instance_id=?", (c.iid,)
    )
    await _begin(mesh, c, echo={})
    assert not h.sent
    # A household that is not the host never answers an echo.
    await _federated(c).on_sync_begin(
        _event(d, FET.SPACE_SYNC_BEGIN, {"space_id": SID, "mod_held": {}}, c)
    )
    assert not c.sent


async def test_a_resent_item_is_taken_from_the_host_only(mesh):
    """``submitter_instance_id`` is believed only from the host, and the
    submitter must hold a live writer seat on the household it names."""
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    sub = {**_submission(mesh), "submitter_instance_id": a.iid}
    # D (not the host) relays anna's item: D is the origin, anna is not D's.
    await _receive(c, _event(d, FET.SPACE_MODERATION_SUBMITTED, sub, c))
    assert await _queue_rows(c) == []
    # The host names a household anna holds no seat on.
    wrong = {**sub, "submitter_instance_id": d.iid}
    await _receive(c, _event(h, FET.SPACE_MODERATION_SUBMITTED, wrong, c))
    assert await _queue_rows(c) == []
    # The host relays an item of a submitter banned here.
    await c.db.enqueue(
        "INSERT INTO space_bans(space_id, user_id, banned_by) VALUES(?, ?, ?)",
        (SID, a.user_id, h.user_id),
    )
    await _receive(c, _event(h, FET.SPACE_MODERATION_SUBMITTED, sub, c))
    assert await _queue_rows(c) == []
    await c.db.enqueue("DELETE FROM space_bans", ())
    # The host, naming anna's own household: held.
    await _receive(c, _event(h, FET.SPACE_MODERATION_SUBMITTED, sub, c))
    assert [r["submitted_by"] for r in await _queue_rows(c)] == [a.user_id]
    # A plain member household drops a re-sent item like any other.
    await _receive(d, _event(h, FET.SPACE_MODERATION_SUBMITTED, sub, d))
    assert await _queue_rows(d) == []


async def test_a_catch_up_is_rate_limited_and_bounded(mesh, monkeypatch):
    h, c = mesh["H"], mesh["C"]
    first = await _submit_task(mesh, "One")
    await _pump(mesh)
    second = await _submit_task(mesh, "Two")
    await _pump(mesh)
    # One answer carries at most CATCH_UP_MAX_BYTES of items (oldest
    # first, at least one); the rest follows on a later sync.
    monkeypatch.setattr(moderation_federation, "CATCH_UP_MAX_BYTES", 1)
    await _begin(mesh, c, echo={})
    assert [p["item_id"] for _to, p in _sends(h, FET.SPACE_MODERATION_SUBMITTED)] == [
        first
    ]
    h.sent.clear()
    # At most one answer per (household, space) in CATCH_UP_INTERVAL_S —
    # checked before the host reads anything.
    reads: list[str] = []
    real = type(_federated(h))._open_rows

    async def _counted(self, space_id):
        reads.append(space_id)
        return await real(self, space_id)

    monkeypatch.setattr(type(_federated(h)), "_open_rows", _counted)
    await _begin(mesh, c, echo={"p": [first]})
    assert not h.sent and reads == []
    _federated(h)._caught_up_at.clear()
    await _begin(mesh, c, echo={"p": [first]})
    assert reads == [SID]
    assert [p["item_id"] for _to, p in _sends(h, FET.SPACE_MODERATION_SUBMITTED)] == [
        second
    ]
    # A malformed echo, or one longer than a household can hold, is
    # ignored.
    h.sent.clear()
    third = await _submit_task(mesh, "Three")
    await _pump(mesh)
    _federated(h)._caught_up_at.clear()
    for bad in (
        [],
        {"p": ["x"] * (moderation_federation.MAX_ECHO_IDS + 1)},
        {"p": [7]},
        {"p": "abc"},
        {"zz": [first]},
        {"r": [{"id": first}]},
        {"r": ["plain"]},
    ):
        await _begin(mesh, c, echo=bad)
        assert not h.sent, bad
    await _begin(mesh, c, echo={"p": [first, second]})
    assert [p["item_id"] for _to, p in _sends(h, FET.SPACE_MODERATION_SUBMITTED)] == [
        third
    ]


async def test_a_pending_posts_media_follows_a_resent_item_to_that_household(mesh):
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    r = await a.tc.post(
        f"/api/spaces/{SID}/posts",
        json={"type": "image", "image_urls": ["api/media/pending-pic.webp"]},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    await _pump(mesh)
    await _promote(mesh, d)
    await _begin(mesh, d)
    assert [to for to, _p in _sends(h, FET.SPACE_MODERATION_SUBMITTED)] == [d.iid]
    # The host ships the picture, under the name it holds it by, to D only.
    from_a = space_direct_media_name(SID, a.iid, "pending-pic.webp")
    rows = await h.db.fetchall(
        "SELECT blob_id, target_instance_id FROM space_media_outbox", ()
    )
    assert [(row["blob_id"], row["target_instance_id"]) for row in rows] == [
        (from_a, d.iid)
    ]
    assert c.iid not in {to for to, _et, _p in h.sent}
    await _pump(mesh)
    # D holds it under the host's name — and still takes the host's release.
    from_h = space_direct_media_name(SID, h.iid, from_a)
    row = await d.db.fetchone(
        "SELECT payload_json FROM space_moderation_queue WHERE id=?", (item_id,)
    )
    assert from_h in row["payload_json"]
    await _approve(d, item_id)
    await _pump(mesh)
    for house in (a, h, c, d):
        posts = await house.db.fetchall("SELECT id FROM space_posts", ())
        assert len(posts) == 1, house.name


async def test_the_host_resends_only_what_a_reviewer_would_admit(mesh):
    """Not re-sent: an item whose author left, one whose review window
    closed, one of a feature no longer reviewed. A decision is sent again
    only when it names somebody who still holds content authority."""
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    left = await _submit_task(mesh, "Author left")
    await _pump(mesh)
    # anna's seat is gone on the host's roster.
    await h.db.enqueue(
        "UPDATE space_remote_members SET tombstoned=1 WHERE user_id=?", (a.user_id,)
    )
    await _begin(mesh, c, echo={})
    assert not h.sent
    await h.db.enqueue("UPDATE space_remote_members SET tombstoned=0", ())
    # The review window closed (the hourly sweep has not run yet).
    await h.db.enqueue(
        "UPDATE space_moderation_queue SET expires_at=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), left),
    )
    await _begin(mesh, c, echo={})
    assert not h.sent
    await h.db.enqueue(
        "UPDATE space_moderation_queue SET expires_at=? WHERE id=?",
        ((datetime.now(UTC) + timedelta(days=1)).isoformat(), left),
    )
    # Tasks are no longer reviewed on the host.
    await h.db.enqueue("UPDATE spaces SET tasks_access='open'", ())
    await _begin(mesh, c, echo={})
    assert not h.sent
    await h.db.enqueue("UPDATE spaces SET tasks_access='moderated'", ())
    # Echoed ids the host cannot speak for: one it never held, and one
    # rejected by a moderator who has since lost the seat.
    await h.db.enqueue(
        "UPDATE space_moderation_queue SET status='rejected', reviewed_by=?,"
        " reviewed_at=? WHERE id=?",
        (c.user_id, datetime.now(UTC).isoformat(), left),
    )
    await h.db.enqueue(
        "UPDATE space_remote_members SET role='member' WHERE user_id=?", (c.user_id,)
    )
    # C still reviews (another moderator lives there), so it is heard.
    await h.db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role,"
        " display_name) VALUES(?, ?, 'u-cleo', 'moderator', 'Cleo')",
        (SID, c.iid),
    )
    await _begin(mesh, c, echo={"p": [left, "never-held"]})
    assert not h.sent
    # carl is a moderator again: his rejection is announced again.
    await h.db.enqueue(
        "UPDATE space_remote_members SET role='moderator' WHERE user_id=?",
        (c.user_id,),
    )
    await _begin(mesh, c, echo={"p": [left, "never-held"]})
    assert [
        (to, p["item_id"]) for to, p in _sends(h, FET.SPACE_MODERATION_DECIDED)
    ] == [(c.iid, left)]


async def test_only_a_promotion_of_our_own_person_asks_the_host(mesh):
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    asked: list[tuple[str, str]] = []
    for house in (a, h, c, d):

        async def _ask(space_id: str, name: str = house.name) -> None:
            asked.append((name, space_id))

        house.app[space_moderation_service_key].attach_host_sync(_ask)
    # Somebody else's promotion, as D's roster mirror hears it.
    await _receive(
        d,
        _event(
            h,
            FET.SPACE_MEMBER_ROLE_CHANGED,
            {
                "space_id": SID,
                "user_id": a.user_id,
                "instance_id": a.iid,
                "role": "moderator",
            },
            d,
        ),
    )
    # A demotion of our own person.
    await _promote(mesh, c, "member")
    assert asked == []
    # Our own person made an admin.
    await _promote(mesh, d, "admin")
    assert asked == [("D", SID)]


async def test_a_failing_catch_up_never_fails_the_sync_begin(mesh, monkeypatch):
    """The catch-up shares ``SPACE_SYNC_BEGIN`` with the sync itself: an
    error in it is logged, never raised into the BEGIN's dispatch."""
    h, c = mesh["H"], mesh["C"]
    await _submit_task(mesh)
    await _pump(mesh)

    async def _boom(*args, **kwargs):
        raise RuntimeError("queue unreadable")

    monkeypatch.setattr(type(_federated(h)), "_open_rows", _boom)
    await _begin(mesh, c, echo={})
    assert not h.sent


# ── Catch-up round 2: every held state is echoed, nothing repeats ───────


async def _row(house: House, item_id: str) -> tuple:
    row = await house.db.fetchone(
        "SELECT status, payload_json FROM space_moderation_queue WHERE id=?",
        (item_id,),
    )
    return (row["status"], row["payload_json"] is not None) if row else ()


async def test_a_repromoted_household_gets_its_purged_items_back_once(mesh):
    """A demotion expires the other households' pending items held here and
    NULLs their content. Promoted again, the household echoes them as
    purged; the host re-sends them ONCE, the rows are pending with content
    again, and the next sync repeats nothing."""
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    item_id = await _submit_task(mesh)
    await _pump(mesh)
    await _promote(mesh, c, "member")
    assert await _row(c, item_id) == ("expired", False)
    await _promote(mesh, c, "moderator")
    assert (await _federated(c).held_echo(SID, h.iid)) == {"x": [item_id]}
    await _begin(mesh, c)
    assert [
        (to, p["item_id"]) for to, p in _sends(h, FET.SPACE_MODERATION_SUBMITTED)
    ] == [(c.iid, item_id)]
    await _pump(mesh)
    assert await _row(c, item_id) == ("pending", True)
    r = await c.tc.get(f"/api/spaces/{SID}/moderation", headers=c.headers)
    assert [i["id"] for i in await r.json()] == [item_id]
    # Nothing repeats — not on the next sync, not after the rate window.
    _federated(h)._caught_up_at.clear()
    await _begin(mesh, c)
    assert not h.sent
    # carl approves the revived copy; everyone gets the task.
    await _approve(c, item_id)
    await _pump(mesh)
    for house in (a, h, c, mesh["D"]):
        assert await _tasks(house) == [("Buy milk", a.user_id)], house.name


async def test_only_the_host_revives_a_purged_item(mesh):
    """A replay from the submitter's household never brings a purged (or
    otherwise expired) copy back: only the host knows it is still pending."""
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    item_id = await _submit_task(mesh)
    (sent,) = [p for to, p in _sends(a, FET.SPACE_MODERATION_SUBMITTED) if to == c.iid]
    await _pump(mesh)
    await c.db.enqueue(
        "UPDATE space_moderation_queue SET status='expired', payload_json=NULL", ()
    )
    await _receive(c, _event(a, FET.SPACE_MODERATION_SUBMITTED, sent, c))
    assert await _row(c, item_id) == ("expired", False)
    # A decided copy is never reopened, by anybody.
    await c.db.enqueue("UPDATE space_moderation_queue SET status='rejected'", ())
    resent = {**sent, "submitter_instance_id": a.iid}
    await _receive(c, _event(h, FET.SPACE_MODERATION_SUBMITTED, resent, c))
    assert (await _row(c, item_id))[0] == "rejected"


async def test_a_rejection_the_host_missed_reaches_it_with_the_echo(mesh):
    """carl rejected the item on C; the host never got that decision. The
    host does not re-send the item (C holds it): C's echo names its own
    people's rejections, and the host applies it like the decision itself
    — carl's live seat on C is checked — so every copy converges."""
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    item_id = await _submit_task(mesh)
    await _pump(mesh)
    r = await c.tc.post(
        f"/api/spaces/{SID}/moderation/{item_id}/reject",
        json={"reason": "off topic"},
        headers=c.headers,
    )
    assert r.status == 200, await r.text()
    assert _drop_sends(c, FET.SPACE_MODERATION_DECIDED, h) == 1
    await _pump(mesh)
    assert (await _row(h, item_id))[0] == "pending"
    echo = await _federated(c).held_echo(SID, h.iid)
    assert echo == {"r": [{"id": item_id, "by": c.user_id, "reason": "off topic"}]}
    await _begin(mesh, c)
    assert not _sends(h, FET.SPACE_MODERATION_SUBMITTED)
    row = await h.db.fetchone(
        "SELECT status, reviewed_by, rejection_reason FROM space_moderation_queue"
        " WHERE id=?",
        (item_id,),
    )
    assert tuple(row) == ("rejected", c.user_id, "off topic")
    # A rejection in somebody else's name is not taken from an echo.
    other = await _submit_task(mesh, "Another")
    await _pump(mesh)
    await _begin(mesh, c, echo={"r": [{"id": other, "by": h.user_id}], "p": [item_id]})
    assert (await _row(h, other))[0] == "pending"
    # …nor one from a household that does not review.
    await _begin(mesh, a, echo={"r": [{"id": other, "by": a.user_id}]})
    assert (await _row(h, other))[0] == "pending"


async def test_an_item_a_household_keeps_refusing_is_not_sent_every_sync(mesh):
    """C refuses the re-sent item (tasks are not reviewed on C's copy of
    the space): the host sends it once, not on every sync — until C's seat
    changes, which starts over."""
    h, c = mesh["H"], mesh["C"]
    item_id = await _submit_task(mesh)
    _drop_sends(mesh["A"], FET.SPACE_MODERATION_SUBMITTED, c)
    await _pump(mesh)
    await c.db.enqueue("UPDATE spaces SET tasks_access='open'", ())
    await _begin(mesh, c)
    assert len(_sends(h, FET.SPACE_MODERATION_SUBMITTED)) == 1
    await _pump(mesh)
    assert await _queue_rows(c) == []
    _federated(h)._caught_up_at.clear()
    await _begin(mesh, c)
    assert not h.sent
    # A role change of C's person on the host starts over — and is never
    # held back by the answer the host gave a moment ago.
    await c.db.enqueue("UPDATE spaces SET tasks_access='moderated'", ())
    await _promote(mesh, c, "admin")
    await _begin(mesh, c)
    assert [p["item_id"] for _to, p in _sends(h, FET.SPACE_MODERATION_SUBMITTED)] == [
        item_id
    ]


async def test_a_mesh_only_reviewer_household_gets_items_and_decisions(
    mesh, monkeypatch
):
    """C is reached only over the mesh: A and H hold no ``remote_instances``
    row for it, only the version it claimed as a space member. It still
    gets a live submission, and a decision it missed. (The real version
    checks run here — nothing is patched.)"""
    monkeypatch.setattr(FederationService, "peer_supports", _REAL_PEER_SUPPORTS)
    monkeypatch.setattr(
        FederationService, "space_member_supports", _REAL_MEMBER_SUPPORTS
    )
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    for house in (a, h):
        await house.db.enqueue("DELETE FROM remote_instances WHERE id=?", (c.iid,))
        await house.db.enqueue(
            "UPDATE space_instances SET proto_version=? WHERE instance_id=?",
            (OURS, c.iid),
        )
        await house.db.enqueue("UPDATE remote_instances SET proto_version=?", (OURS,))
    await c.db.enqueue("UPDATE remote_instances SET proto_version=?", (OURS,))
    item_id = await _submit_task(mesh)
    assert sorted(to for to, _p in _sends(a, FET.SPACE_MODERATION_SUBMITTED)) == sorted(
        [h.iid, c.iid]
    )
    await _pump(mesh)
    assert await _row(c, item_id) == ("pending", True)
    r = await h.tc.post(
        f"/api/spaces/{SID}/moderation/{item_id}/reject", json={}, headers=h.headers
    )
    assert r.status == 200, await r.text()
    # The live announcement reaches the mesh-only reviewer household…
    assert c.iid in {to for to, _p in _sends(h, FET.SPACE_MODERATION_DECIDED)}
    # …and so does the catch-up when it was lost.
    h.sent.clear()
    await _begin(mesh, c, echo={"p": [item_id]})
    assert [
        (to, p["decision"]) for to, p in _sends(h, FET.SPACE_MODERATION_DECIDED)
    ] == [(c.iid, "rejected")]


async def test_a_promotion_sends_a_real_begin_that_carries_the_echo(mesh, monkeypatch):
    """No stub between the promotion and the wire: D applies its role, its
    real scheduler queues the incremental BEGIN to the host, and that
    BEGIN's payload — echo included — is what the host answers."""
    a, h, d = mesh["A"], mesh["H"], mesh["D"]
    begins: list[tuple[str, dict]] = []

    async def _capture(self, *, to_instance_id, event_type, payload, space_id=None):
        if event_type is FET.SPACE_SYNC_BEGIN:
            begins.append((to_instance_id, copy.deepcopy(payload)))
        return DeliveryResult(instance_id=to_instance_id, ok=True)

    monkeypatch.setattr(FederationService, "send_event", _capture)
    item_id = await _submit_task(mesh)
    await _pump(mesh)
    await _promote(mesh, d)
    for _ in range(100):
        if begins:
            break
        await asyncio.sleep(0.05)
    assert [to for to, _p in begins] == [h.iid]
    payload = begins[0][1]
    assert payload["sync_mode"] == "incremental" and payload["mod_held"] == {}
    await _federated(h).on_sync_begin(_event(d, FET.SPACE_SYNC_BEGIN, payload, h))
    assert [
        (to, p["item_id"]) for to, p in _sends(h, FET.SPACE_MODERATION_SUBMITTED)
    ] == [(d.iid, item_id)]
    await _pump(mesh)
    assert await _row(d, item_id) == ("pending", True)
    assert a.user_id in {r["submitted_by"] for r in await _queue_rows(d)}


# ── Catch-up round 3 ────────────────────────────────────────────────────


async def test_a_resend_that_did_not_get_out_is_tried_again(mesh, monkeypatch):
    """The once-a-day mark is for an item (or decision) that REACHED the
    household: a send the transport refused is tried again on the next
    answer."""
    h, c = mesh["H"], mesh["C"]
    item_id = await _submit_task(mesh)
    _drop_sends(mesh["A"], FET.SPACE_MODERATION_SUBMITTED, c)
    await _pump(mesh)
    failing = {FET.SPACE_MODERATION_SUBMITTED, FET.SPACE_MODERATION_DECIDED}

    async def _refused(self, *, to_instance_id, event_type, payload, space_id=None):
        if event_type in failing and self._own_instance_id == h.iid:
            return DeliveryResult(
                instance_id=to_instance_id, ok=False, error="no_route"
            )
        return await _fake_send(
            self,
            to_instance_id=to_instance_id,
            event_type=event_type,
            payload=payload,
            space_id=space_id,
        )

    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _refused)
    await _begin(mesh, c)
    assert not h.sent
    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _fake_send)
    _federated(h)._caught_up_at.clear()
    await _begin(mesh, c)
    assert [p["item_id"] for _to, p in _sends(h, FET.SPACE_MODERATION_SUBMITTED)] == [
        item_id
    ]
    # The same for a decision.
    await _pump(mesh)
    r = await h.tc.post(
        f"/api/spaces/{SID}/moderation/{item_id}/reject", json={}, headers=h.headers
    )
    assert r.status == 200, await r.text()
    h.sent.clear()
    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _refused)
    _federated(h)._caught_up_at.clear()
    await _begin(mesh, c)
    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _fake_send)
    _federated(h)._caught_up_at.clear()
    await _begin(mesh, c)
    assert [p["item_id"] for _to, p in _sends(h, FET.SPACE_MODERATION_DECIDED)] == [
        item_id
    ]


async def test_a_moderator_who_left_and_was_seated_again_starts_over(mesh):
    """No role change happens when a moderator leaves and is seated again
    (a role-bearing invite link): the seat itself changed, and that alone
    makes the host answer in full again."""
    h, c = mesh["H"], mesh["C"]
    item_id = await _submit_task(mesh)
    _drop_sends(mesh["A"], FET.SPACE_MODERATION_SUBMITTED, c)
    await _pump(mesh)
    await _begin(mesh, c)
    assert len(_sends(h, FET.SPACE_MODERATION_SUBMITTED)) == 1
    h.sent.clear()
    # carl's seat is removed and written again on the host's roster — the
    # way a leave and a redeem do, with no role-change event.
    seats = h.app[space_remote_member_repo_key]
    await seats.remove(SID, c.iid, c.user_id)
    await _begin(mesh, c, echo={})
    assert not h.sent  # not seated: not answered
    await seats.add(
        space_id=SID,
        instance_id=c.iid,
        user_id=c.user_id,
        user_pk=None,
        display_name="Carl",
        role="moderator",
    )
    _federated(h)._caught_up_at.clear()
    await _begin(mesh, c, echo={})
    assert [p["item_id"] for _to, p in _sends(h, FET.SPACE_MODERATION_SUBMITTED)] == [
        item_id
    ]


async def test_an_echoed_rejection_is_processed_once_per_rate_window(mesh, monkeypatch):
    """An echo that only carries own rejections spends the rate window
    too: the host does not run the decision checks again on every BEGIN
    (and log a refusal each time) while the entry stays in the echo."""
    h, c = mesh["H"], mesh["C"]
    item_id = await _submit_task(mesh)
    await _pump(mesh)
    seen: list[str] = []
    real = type(_federated(h))._on_decided

    async def _counted(self, event):
        seen.append(event.payload["item_id"])
        return await real(self, event)

    monkeypatch.setattr(type(_federated(h)), "_on_decided", _counted)
    # A rejection in the name of somebody who holds no seat on C: refused.
    echo = {"r": [{"id": item_id, "by": "u-nobody"}]}
    await _begin(mesh, c, echo=echo)
    await _begin(mesh, c, echo=echo)
    assert seen == [item_id]
    assert (await _row(h, item_id))[0] == "pending"


def _relay_seat_sends(monkeypatch, seat: House) -> None:
    """Sends to *seat* are answered as a GFS relay answers: accepted
    (``via = gfs_relay``), which is not a delivery."""

    async def _send(self, *, to_instance_id, event_type, payload, space_id=None):
        result = await _fake_send(
            self,
            to_instance_id=to_instance_id,
            event_type=event_type,
            payload=payload,
            space_id=space_id,
        )
        if to_instance_id == seat.iid:
            return dataclasses.replace(result, via="gfs_relay")
        return result

    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _send)


async def test_a_large_item_reaches_a_gfs_only_moderator_and_a_paired_one(
    mesh, monkeypatch
):
    """No size change for anybody: an item near the 256 KiB cap is taken
    (202) and reaches a paired moderator household as the ordinary event.
    A moderator household behind a GFS gets the very same event — the
    transport carries what a JSON relay envelope cannot as one binary relay
    frame (v_87, ``test_federation_service_relay_large_events``) — and it is
    admitted like any other; nothing in the moderation code builds a frame."""
    a, h, c, d = mesh["A"], mesh["H"], mesh["C"], mesh["D"]
    _relay_seat_sends(monkeypatch, d)
    content = secrets.token_hex(120 * 1024)  # 240 KiB of text
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Big", "content": content},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    sent = _sends(a, FET.SPACE_MODERATION_SUBMITTED)
    assert sorted(to for to, _p in sent) == sorted([h.iid, c.iid])
    for _to, p in sent:
        assert len(json.dumps(p)) > RELAY_MAX_ENVELOPE_BYTES
        # The "before" values are rebuilt by every receiver: never sent.
        assert p["snapshot"] is None
    await _pump(mesh)
    assert await _row(c, item_id) == ("pending", True)  # paired: as ever
    # D sits behind a GFS as the host sees it, and is promoted.
    await h.db.enqueue(
        "UPDATE remote_instances SET source='space_session' WHERE id=?", (d.iid,)
    )
    await _promote(mesh, d)
    await _begin(mesh, d)
    ((to, payload),) = _sends(h, FET.SPACE_MODERATION_SUBMITTED)
    assert to == d.iid and payload["item_id"] == item_id
    assert payload["submitter_instance_id"] == a.iid
    assert payload["payload"]["content"] == content  # the whole event
    await _pump(mesh)
    assert await _row(d, item_id) == ("pending", True)
    row = await d.db.fetchone(
        "SELECT payload_json FROM space_moderation_queue WHERE id=?", (item_id,)
    )
    assert json.loads(row["payload_json"])["content"] == content
    # Up to date: nothing more is sent.
    _federated(h)._caught_up_at.clear()
    h.sent.clear()
    await _begin(mesh, d)
    assert not h.sent
    # A's own view: D is a moderator household behind a GFS now. A second
    # large item goes straight to it too, like to the host and to paired C.
    await a.db.enqueue(
        "UPDATE remote_instances SET source='space_session' WHERE id=?", (d.iid,)
    )
    a.sent.clear()
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Big two", "content": content},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    assert sorted(to for to, _p in _sends(a, FET.SPACE_MODERATION_SUBMITTED)) == sorted(
        [h.iid, c.iid, d.iid]
    )


async def test_a_relay_frame_is_sent_again_after_the_rate_window(mesh, monkeypatch):
    """The relay's answer to a frame is "accepted", not "delivered" (its
    media queue drops frames after an hour, or when full). So a large item
    — one that crossed the relay as a frame — the household still does not
    hold is sent again once the 10-minute window has passed; a small one
    (a JSON envelope, kept a day by the relay) waits its day."""
    a, h, d = mesh["A"], mesh["H"], mesh["D"]
    _relay_seat_sends(monkeypatch, d)
    now = [1000.0]
    _federated(h)._clock = lambda: now[0]
    small = await _submit_task(mesh)
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Big", "content": secrets.token_hex(120 * 1024)},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    big = (await r.json())["item_id"]
    await _pump(mesh)
    await h.db.enqueue(
        "UPDATE remote_instances SET source='space_session' WHERE id=?", (d.iid,)
    )
    await _promote(mesh, d)

    def _resent() -> list[str]:
        ids = [
            p["item_id"]
            for to, p in _sends(h, FET.SPACE_MODERATION_SUBMITTED)
            if to == d.iid
        ]
        h.sent.clear()
        return sorted(ids)

    await _begin(mesh, d, echo={})
    assert _resent() == sorted([small, big])
    # Neither arrived (both lost). Inside the window: nothing.
    now[0] += moderation_federation.CATCH_UP_INTERVAL_S - 1
    await _begin(mesh, d, echo={})
    assert _resent() == []
    # After it: the large one again — the small one waits its day.
    now[0] += 2
    await _begin(mesh, d, echo={})
    assert _resent() == [big]


# ── Catch-up round 4 ────────────────────────────────────────────────────


async def test_an_item_that_cannot_reach_the_host_is_refused_at_submit(
    mesh, monkeypatch
):
    """A's household reaches the host only through a GFS, and the host (or
    that GFS) takes no binary relay frame: an item too large for a JSON
    relay envelope could never get to the host, so nobody could ever review
    it. It is refused where it is made (413) — not stored, not shown as
    pending — and a smaller one is taken as ever. Once the host takes
    frames (v_87), the large item is taken and sent to it."""
    a, h = mesh["A"], mesh["H"]
    await a.db.enqueue(
        "UPDATE remote_instances SET source='space_session' WHERE id=?", (h.iid,)
    )
    assert not await a.app[federation_service_key].large_event_reaches(h.iid)
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Big", "content": secrets.token_hex(120 * 1024)},
        headers=a.headers,
    )
    assert r.status == 413, await r.text()
    assert (await r.json())["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert await _queue_rows(a) == [] and not a.sent
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Fits", "content": secrets.token_hex(40 * 1024)},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    assert h.iid in {to for to, _p in _sends(a, FET.SPACE_MODERATION_SUBMITTED)}
    a.sent.clear()

    async def _frames_ok(self, instance, **_kw):
        return True

    monkeypatch.setattr(FederationService, "relay_event_frames_ok", _frames_ok)
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Big", "content": secrets.token_hex(120 * 1024)},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    assert h.iid in {to for to, _p in _sends(a, FET.SPACE_MODERATION_SUBMITTED)}


@pytest.mark.parametrize(
    ("error", "gets_files"),
    [("relay_too_large", False), ("no_route", False), ("delivery_failed", True)],
)
async def test_an_items_files_go_only_where_the_item_goes(
    mesh, monkeypatch, error, gets_files
):
    """The files of a pending post follow the item: a reviewer household
    the item could not be sent to gets none of them. (A send parked in the
    outbox still arrives — its files go too.)"""
    a, h, c = mesh["A"], mesh["H"], mesh["C"]

    async def _send(self, *, to_instance_id, event_type, payload, space_id=None):
        if event_type is FET.SPACE_MODERATION_SUBMITTED and to_instance_id == c.iid:
            return DeliveryResult(instance_id=to_instance_id, ok=False, error=error)
        return await _fake_send(
            self,
            to_instance_id=to_instance_id,
            event_type=event_type,
            payload=payload,
            space_id=space_id,
        )

    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _send)
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
    wanted = [h.iid, c.iid] if gets_files else [h.iid]
    assert sorted(r["target_instance_id"] for r in rows) == sorted(wanted)


# ── A text no relay frame carries is refused where it is saved ───────────


async def test_a_page_or_description_too_large_for_a_gfs_member_is_refused(mesh):
    """A space with a household behind a GFS: a page, a calendar description
    or a space description over what one relay frame (and the sync chunk
    that re-delivers it) carries answers 413 ``SPACE_ITEM_TOO_LARGE`` and
    nothing is stored — reviewed or not. Without such a household the same
    text is taken, as on any local path."""
    a, h = mesh["A"], mesh["H"]
    big = secrets.token_hex(200 * 1024)  # 400 KiB of text
    event = {
        "summary": "Long",
        "start": "2027-04-01T10:00:00Z",
        "end": "2027-04-01T11:00:00Z",
        "description": big,
    }
    # No household behind a GFS: the owner's large page is taken.
    r = await h.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Big", "content": big},
        headers=h.headers,
    )
    assert r.status in (200, 201), await r.text()
    host_pages = await _pages(h)
    pages_before = await _pages(a)
    queue_before = await _queue_rows(a)
    for house in (a, h):
        peer = h if house is a else a
        await house.db.enqueue(
            "UPDATE remote_instances SET source='space_session' WHERE id=?",
            (peer.iid,),
        )
    for house, method, path, body in (
        (a, "post", f"/api/spaces/{SID}/pages", {"title": "Big", "content": big}),
        (h, "post", f"/api/spaces/{SID}/pages", {"title": "Big", "content": big}),
        (h, "post", f"/api/spaces/{SID}/calendar/events", event),
        (h, "patch", f"/api/spaces/{SID}", {"description": big}),
    ):
        r = await getattr(house.tc, method)(path, json=body, headers=house.headers)
        assert r.status == 413, (path, await r.text())
        error = (await r.json())["error"]
        assert error["code"] == "SPACE_ITEM_TOO_LARGE"
        assert error["params"]["max_kb"] > 300
    assert await _pages(a) == pages_before
    assert await _queue_rows(a) == queue_before
    assert await _pages(h) == host_pages
    # A page under the bound is taken as ever.
    r = await h.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Fits", "content": secrets.token_hex(120 * 1024)},
        headers=h.headers,
    )
    assert r.status in (200, 201), await r.text()
