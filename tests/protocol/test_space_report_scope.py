"""Release-blocker protocol tests: space-scoped reports (``SPACE_REPORT``).

Marked ``@pytest.mark.security``.

Four REAL applications over four real SQLite databases share one space:

* **H** — the host (owner ``hanna``);
* **A** — a member household (``anna``, a plain member, the reporter);
* **C** — a moderator household (``carl``, a moderator seat);
* **D** — a plain member household (``dora``).

Every outbound send is captured at
:meth:`FederationService.send_with_mesh_fallback` / ``send_event`` and
delivered through the post-decrypt gates and the real handler registry.

What this file proves:

* **Confidentiality.** A report anna files on A about a post in the space
  goes to H and C only — never to D (a plain member household); it is
  carried with the routing ``space_id`` and every content field inside the
  sealed payload; the household admin queue on no household lists it.
* **Triage.** H's owner and C's moderator see it under the space's reports
  and resolve it; D's member gets 403.
* **One decision everywhere.** C's moderator resolves; ``SPACE_REPORT_DECIDED``
  goes to H only (never D), H's copy is resolved, a replay changes nothing,
  and a decision from D — or naming D's user — is refused.
* **v_45 gate.** A moderator household below v_45 is sent neither event.
* **Forgeries.** A report naming a reporter seated on another household, a
  banned reporter, a cross-space target, and one delivered to a household
  with no content authority are all dropped, not stored.
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import db_key, federation_service_key
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
SID = "sp-reports"
_IDS = itertools.count()

#: Captured sends per SENDING instance id: (to, event_type, payload, space).
OUTBOX: dict[str, list[tuple[str, FederationEventType, dict, str | None]]] = {}


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
        (to_instance_id, event_type, copy.deepcopy(payload), space_id)
    )
    return DeliveryResult(instance_id=to_instance_id, ok=True)


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
    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _fake_send)
    monkeypatch.setattr(FederationService, "send_event", _fake_send)
    OUTBOX.clear()
    houses: dict[str, House] = {}
    for name in ("H", "A", "C", "D"):
        app = create_app(_config(tmp_dir, name))
        tc = await aiohttp_client(app)
        db = app[db_key]
        row = await db.fetchone("SELECT instance_id FROM instance_identity")
        uid = f"u-{USERS[name]}"
        token = f"{name}-tok"
        # Every household's single user is ALSO its household admin — so
        # "household admin sees nothing of a space report" is tested on
        # the very people who would see it under the old rule.
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin)"
            " VALUES(?, ?, ?, 1)",
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
    now = datetime.now(timezone.utc).isoformat()
    for house in houses.values():
        db = house.db
        for sid in (SID, "sp-other"):
            await db.enqueue(
                "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
                " identity_public_key) VALUES(?, 'Club', ?, 'hanna', ?)",
                (sid, host.iid, "ab" * 32),
            )
        await db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, ?)",
            (SID, house.user_id, ROLES[house.name]),
        )
        for pid, sid in (("p-1", SID), ("p-other", "sp-other")):
            await db.enqueue(
                "INSERT INTO space_posts(id, space_id, author, type, content)"
                " VALUES(?, ?, ?, 'text', 'hello')",
                (pid, sid, houses["D"].user_id),
            )
        for other in houses.values():
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
            await db.enqueue(
                "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
                " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
                " local_inbox_id, status, source, proto_version,"
                " capabilities_seen_at)"
                " VALUES(?, ?, ?, '00', '00', ?, ?, 'confirmed', 'manual', 45, ?)",
                (
                    other.iid,
                    other.name,
                    "ab" * 32,
                    f"https://{other.name}.test/inbox/x",
                    f"{other.name}_local",
                    now,
                ),
            )
    return houses


def _event(
    sender: House, payload: dict, to: House, et: FederationEventType = FET.SPACE_REPORT
) -> FederationEvent:
    return FederationEvent(
        msg_id=f"m-{next(_IDS)}",
        event_type=et,
        from_instance=sender.iid,
        to_instance=to.iid,
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=copy.deepcopy(payload),
        space_id=payload.get("space_id"),
    )


async def _receive(to: House, event: FederationEvent) -> None:
    fed = to.app[federation_service_key]
    ctx = InboundContext(
        envelope={"space_id": event.space_id, "from_instance": event.from_instance},
        event=event,
    )
    if not await run_post_decrypt_gates(
        ctx, steps=fed.post_decrypt_gate_steps(include_ban_check=True)
    ):
        return
    for handler in fed._event_registry.handlers_for(event.event_type):
        await handler(event)


async def _pump(mesh) -> None:
    by_iid = {h.iid: h for h in mesh.values()}
    for iid, sent in list(OUTBOX.items()):
        batch, sent[:] = list(sent), []
        for to, et, payload, _space in batch:
            if et in (FET.SPACE_REPORT, FET.SPACE_REPORT_DECIDED) and to in by_iid:
                await _receive(by_iid[to], _event(by_iid[iid], payload, by_iid[to], et))


async def _rows(house: House) -> list[dict]:
    rows = await house.db.fetchall(
        "SELECT id, space_id, reporter_user_id, reporter_instance_id, status"
        " FROM content_reports",
        (),
    )
    return [dict(r) for r in rows]


async def _file(house: House, **body) -> dict:
    r = await house.tc.post(
        "/api/reports",
        json={"category": "harassment", "notes": "mean", **body},
        headers=house.headers,
    )
    assert r.status == 201, await r.text()
    return await r.json()


def _payload(mesh, **over) -> dict:
    p = {
        "target_type": "post",
        "target_id": "p-1",
        "category": "spam",
        "notes": None,
        "reporter_user_id": mesh["A"].user_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "space_id": SID,
    }
    p.update(over)
    return p


async def test_a_report_reaches_the_host_and_moderators_never_plain_members(mesh):
    body = await _file(mesh["A"], target_type="post", target_id="p-1")
    assert body["space_id"] == SID and body["federated"] is True
    sent = OUTBOX[mesh["A"].iid]
    assert {to for to, *_ in sent} == {mesh["H"].iid, mesh["C"].iid}
    for _to, et, payload, space in sent:
        assert et is FET.SPACE_REPORT
        assert space == SID  # the routing field; the rest is sealed
        assert payload["space_id"] == SID
    await _pump(mesh)
    for name in ("H", "C"):
        rows = await _rows(mesh[name])
        assert [(r["space_id"], r["reporter_instance_id"]) for r in rows] == [
            (SID, mesh["A"].iid)
        ]
    assert await _rows(mesh["D"]) == []
    # No household admin queue lists it — every user here is one.
    for house in mesh.values():
        r = await house.tc.get("/api/admin/reports", headers=house.headers)
        assert await r.json() == []


async def test_moderators_on_any_reviewing_household_triage_members_cannot(mesh):
    await _file(mesh["A"], target_type="post", target_id="p-1")
    await _pump(mesh)
    for name in ("H", "C"):
        h = mesh[name]
        r = await h.tc.get(f"/api/spaces/{SID}/reports", headers=h.headers)
        assert r.status == 200
        rows = await r.json()
        assert rows[0]["reporter_name"] == "Anna"
        r = await h.tc.post(
            f"/api/spaces/{SID}/reports/{rows[0]['id']}/resolve",
            json={},
            headers=h.headers,
        )
        assert r.status == 200
    for name in ("A", "D"):
        h = mesh[name]
        r = await h.tc.get(f"/api/spaces/{SID}/reports", headers=h.headers)
        assert r.status == 403


async def test_a_member_report_names_the_space_and_targets_reviewers(mesh):
    await _file(
        mesh["A"], target_type="user", target_id=mesh["D"].user_id, space_id=SID
    )
    assert {to for to, *_ in OUTBOX[mesh["A"].iid]} == {mesh["H"].iid, mesh["C"].iid}
    await _pump(mesh)
    assert [r["space_id"] for r in await _rows(mesh["C"])] == [SID]


async def test_a_reporter_seated_on_another_household_is_refused(mesh):
    # D claims anna (A's member) reported something.
    await _receive(mesh["H"], _event(mesh["D"], _payload(mesh), mesh["H"]))
    assert await _rows(mesh["H"]) == []


async def test_a_banned_reporter_is_refused(mesh):
    await mesh["H"].db.enqueue(
        "INSERT INTO space_bans(space_id, user_id, banned_by) VALUES(?, ?, ?)",
        (SID, mesh["A"].user_id, mesh["H"].user_id),
    )
    await _receive(mesh["H"], _event(mesh["A"], _payload(mesh), mesh["H"]))
    assert await _rows(mesh["H"]) == []


async def test_a_cross_space_target_is_refused(mesh):
    await _receive(
        mesh["H"],
        _event(mesh["A"], _payload(mesh, target_id="p-other"), mesh["H"]),
    )
    assert await _rows(mesh["H"]) == []


async def test_a_plain_member_household_does_not_store_a_report(mesh):
    # A pre-change sender fanned reports to every member household.
    await _receive(mesh["D"], _event(mesh["A"], _payload(mesh), mesh["D"]))
    legacy = _payload(mesh)
    del legacy["space_id"]
    await _receive(mesh["D"], _event(mesh["A"], legacy, mesh["D"]))
    assert await _rows(mesh["D"]) == []
    # ...while the host derives the space from the target and keeps it.
    await _receive(mesh["H"], _event(mesh["A"], legacy, mesh["H"]))
    assert [r["space_id"] for r in await _rows(mesh["H"])] == [SID]


async def _status(house: House) -> list[str]:
    return [r["status"] for r in await _rows(house)]


async def test_a_resolve_on_one_reviewer_household_clears_it_on_the_others(mesh):
    await _file(mesh["A"], target_type="post", target_id="p-1")
    await _pump(mesh)
    c = mesh["C"]
    rows = await (
        await c.tc.get(f"/api/spaces/{SID}/reports", headers=c.headers)
    ).json()
    r = await c.tc.post(
        f"/api/spaces/{SID}/reports/{rows[0]['id']}/resolve",
        json={"dismissed": True},
        headers=c.headers,
    )
    assert r.status == 200
    sent = [(to, et) for to, et, *_ in OUTBOX[c.iid]]
    assert sent == [(mesh["H"].iid, FET.SPACE_REPORT_DECIDED)]
    decided = OUTBOX[c.iid][0][2]
    await _pump(mesh)
    assert await _status(mesh["H"]) == ["dismissed"]
    assert await _status(mesh["D"]) == []
    # A replay — and a later contrary verdict — change nothing.
    await _receive(
        mesh["H"],
        _event(
            c, {**decided, "decision": "resolved"}, mesh["H"], FET.SPACE_REPORT_DECIDED
        ),
    )
    assert await _status(mesh["H"]) == ["dismissed"]
    h = mesh["H"]
    r = await h.tc.get(f"/api/spaces/{SID}/reports", headers=h.headers)
    assert await r.json() == []


async def test_a_decision_from_a_plain_member_household_is_refused(mesh):
    await _file(mesh["A"], target_type="post", target_id="p-1")
    await _pump(mesh)
    forged = {
        "space_id": SID,
        "target_type": "post",
        "target_id": "p-1",
        "reporter_user_id": mesh["A"].user_id,
        "decision": "dismissed",
        "decided_by": mesh["D"].user_id,
    }
    await _receive(
        mesh["H"], _event(mesh["D"], forged, mesh["H"], FET.SPACE_REPORT_DECIDED)
    )
    # C names D's user as the decider: D holds no content-authority seat.
    await _receive(
        mesh["H"], _event(mesh["C"], forged, mesh["H"], FET.SPACE_REPORT_DECIDED)
    )
    assert await _status(mesh["H"]) == ["pending"]


async def test_a_reviewer_household_below_v45_is_sent_nothing(mesh):
    await mesh["A"].db.enqueue(
        "UPDATE remote_instances SET proto_version=44 WHERE id=?", (mesh["C"].iid,)
    )
    await _file(mesh["A"], target_type="post", target_id="p-1")
    assert {to for to, *_ in OUTBOX[mesh["A"].iid]} == {mesh["H"].iid}


async def test_a_host_relayed_report_is_capped_under_the_reporters_household(mesh):
    """The host re-delivers anna's report to C: C keys it (and its cap) on
    A, the household anna belongs to — not on the host. A household other
    than the host naming an origin is ignored."""
    relayed = {**_payload(mesh), "reporter_instance_id": mesh["A"].iid}
    await _receive(mesh["C"], _event(mesh["H"], relayed, mesh["C"]))
    rows = await _rows(mesh["C"])
    assert [r["reporter_instance_id"] for r in rows] == [mesh["A"].iid]
    # D claims to relay anna's report: D is not the host, so the origin is
    # D — where anna holds no seat. Refused.
    forged = {**_payload(mesh, target_id="p-1"), "reporter_instance_id": mesh["A"].iid}
    await _receive(mesh["H"], _event(mesh["D"], forged, mesh["H"]))
    assert await _rows(mesh["H"]) == []


async def test_the_first_fan_out_carries_the_reporters_household(mesh):
    await _file(mesh["A"], target_type="post", target_id="p-1")
    for _to, _et, payload, _space in OUTBOX[mesh["A"].iid]:
        assert payload["reporter_instance_id"] == mesh["A"].iid
