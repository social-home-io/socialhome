"""Release-blocker protocol tests: host-sequenced space pages (§4.4.4.1, v_48).

Marked ``@pytest.mark.security``.

Four REAL applications over four real SQLite databases share one space:

* **H** — the host (owner ``hanna``): the only sequencer of its pages;
* **A** — a member household (``anna``);
* **B** — a member household (``bert``);
* **C** — a moderator household (``carl``).

Every outbound send is captured at :meth:`FederationService.send_with_mesh_fallback`
and delivered to its target through the post-decrypt gates and the real
handler registry, in an order each test chooses.

Each adversarial probe of the decentralised design (probe14) is a test here
and must hold: concurrent appends converge to one state and ``seq`` in every
delivery order (P1); a concurrent undo is kept (P2); a household offline
for many edits fast-forwards with no conflict, live or by sync (P3); a cover
change converges (P4); conflicts hold one side per user, the host's own
edit still merges, and one resolution converges everyone (P5); every
household holds the identical conflict (P6). Plus: an unreachable host
(drafts wait, one proposal per page, convergence on return), a member that
forges the host's ``seq`` (live, chunk, resume), every refusal, moderated
stale / force, mixed fleets, deletes, the same-household stale save.
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    page_repo_key,
)
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.domain.federation import (
    DeliveryResult,
    FederationEvent,
    FederationEventType,
)
from socialhome.domain.page_version import version_hash
from socialhome.federation.federation_service import FederationService
from socialhome.federation.inbound_validator import (
    InboundContext,
    run_post_decrypt_gates,
)
from socialhome.federation.sync.space.exporters.pages import PagesExporter
from socialhome.services import page_conflict_service

pytestmark = pytest.mark.security

FET = FederationEventType
SID = "sp-wiki"
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
    return VERSIONS.get(instance_id, 48) >= min_version


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


ROLES = {"H": "owner", "A": "member", "B": "member", "C": "moderator"}
USERS = {"H": "hanna", "A": "anna", "B": "bert", "C": "carl"}


@pytest.fixture
async def mesh(request, aiohttp_client, tmp_dir, monkeypatch):
    """H, A, B and C, each a real app, seated in one space whose wiki has
    the access level ``request.param`` (default ``open``)."""
    access = getattr(request, "param", "open")
    monkeypatch.setattr(FederationService, "send_with_mesh_fallback", _fake_send)
    monkeypatch.setattr(FederationService, "peer_supports", _fake_supports)
    OUTBOX.clear()
    VERSIONS.clear()
    houses: dict[str, House] = {}
    for name in ("H", "A", "B", "C"):
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
    now = datetime.now(timezone.utc).isoformat()
    for house in houses.values():
        db = house.db
        await db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key, feature_pages, pages_access)"
            " VALUES(?, 'Wiki club', ?, 'hanna', ?, 1, ?)",
            (SID, host.iid, "ab" * 32, access),
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
                " VALUES(?, ?, ?, '00', '00', ?, ?, 'confirmed', 'manual', 48, ?)",
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


def _by_iid(mesh) -> dict[str, House]:
    return {h.iid: h for h in mesh.values()}


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


async def _pump(mesh, *, only: set[FederationEventType] | None = None) -> None:
    """Deliver every captured send to its target until nothing is left."""
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


def _take(sender: House, to: House) -> list[tuple[FederationEventType, dict]]:
    """Remove (undelivered) and return ``sender``'s queued sends to ``to``."""
    sent = sender.sent
    mine = [(et, p) for t, et, p in sent if t == to.iid]
    sent[:] = [(t, et, p) for t, et, p in sent if t != to.iid]
    return mine


async def _deliver(sender: House, to: House) -> None:
    for et, payload in _take(sender, to):
        await _receive(to, _event(sender, et, payload, to))


def _drop(*houses: House) -> None:
    """Forget whatever these households queued (an edit that never left)."""
    for house in houses:
        house.sent.clear()


def _body(*paras: str) -> str:
    return "\n\n".join(paras)


BASE = _body("p1", "p2", "p3", "p4", "p5")


async def _create(mesh, content: str = BASE, *, by: str = "H") -> str:
    house = mesh[by]
    r = await house.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Rules", "content": content},
        headers=house.headers,
    )
    assert r.status == 201, await r.text()
    await _pump(mesh)
    return (await r.json())["id"]


async def _edit(house: House, pid: str, content: str, *, expect: int = 200):
    r = await house.tc.patch(
        f"/api/spaces/{SID}/pages/{pid}",
        json={"content": content},
        headers=house.headers,
    )
    assert r.status == expect, await r.text()
    return await r.json()


async def _page(house: House, pid: str) -> dict:
    r = await house.tc.get(f"/api/spaces/{SID}/pages/{pid}", headers=house.headers)
    assert r.status == 200, await r.text()
    return await r.json()


async def _content(house: House, pid: str) -> str | None:
    """The live content (``None``: not held, or a tombstone)."""
    row = await house.db.fetchone(
        "SELECT content FROM space_pages"
        " WHERE id=? AND space_id=? AND deleted_at IS NULL",
        (pid, SID),
    )
    return None if row is None else row["content"]


async def _contents(mesh, pid: str) -> dict[str, str | None]:
    return {name: await _content(h, pid) for name, h in mesh.items()}


async def _sides(house: House, pid: str) -> list[str]:
    conflict = (await _page(house, pid))["conflict"]
    return [] if conflict is None else sorted(s["hash"] for s in conflict["sides"])


def _with(base: str, **replace: str) -> str:
    """``base`` with paragraph ``pN`` replaced by ``replace["pN"]``."""
    return _body(*(replace.get(p, p) for p in base.split("\n\n")))


async def _host_chunk(mesh, pid: str) -> list[dict]:
    records = await PagesExporter(mesh["H"].app[page_repo_key]).list_records(SID)
    return [r for r in records if r["id"] == pid]


async def _sync_from_host(mesh, to: House, records: list[dict]) -> None:
    receiver = to.app[federation_service_key]._space_sync_receiver
    await receiver._dispatch(
        "pages", SID, copy.deepcopy(records), provider=mesh["H"].iid
    )


async def _seq(house: House, pid: str) -> int | None:
    row = await house.db.fetchone(
        "SELECT seq FROM space_pages WHERE id=? AND space_id=?", (pid, SID)
    )
    return None if row is None else int(row["seq"])


async def _pending(house: House, pid: str) -> int | None:
    row = await house.db.fetchone(
        "SELECT pending_base_seq FROM space_pages WHERE id=? AND space_id=?",
        (pid, SID),
    )
    return None if row is None else row["pending_base_seq"]


async def _state(house: House, pid: str) -> tuple:
    return (
        await _content(house, pid),
        tuple(await _sides(house, pid)),
        await _seq(house, pid),
        await _pending(house, pid),
    )


async def _states(mesh, pid: str) -> dict[str, tuple]:
    return {n: await _state(h, pid) for n, h in mesh.items()}


def _sent_to(sender: House, to: House, et=None) -> list[dict]:
    return [p for t, e, p in sender.sent if t == to.iid and (et is None or e is et)]


def _converged(states: dict[str, tuple]) -> tuple:
    assert len(set(states.values())) == 1, states
    return next(iter(states.values()))


# ── P1: three appends at one point — one state, one seq ────────────────


@pytest.mark.parametrize("order", ["pump", "A-first", "B-first"])
async def test_p1_three_concurrent_appends_converge_in_every_order(mesh, order):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _edit(a, pid, BASE + "\n\nanna 0")
    await _edit(b, pid, BASE + "\n\nbert 0")
    await _edit(h, pid, BASE + "\n\nhanna 0")
    # Members propose to the host alone — never to each other.
    assert _sent_to(a, b) == [] and _sent_to(b, a) == []
    if order == "A-first":
        await _deliver(a, h)
    elif order == "B-first":
        await _deliver(b, h)
    await _pump(mesh)
    content, sides, seq, pending = _converged(await _states(mesh, pid))
    assert sides == () and pending is None
    assert seq == 4  # create, hanna, then each proposal
    for words in ("anna 0", "bert 0", "hanna 0"):
        assert words in content
    # The host's own (first-sequenced) append comes first.
    assert content.index("hanna 0") < content.index("anna 0")
    assert content.index("hanna 0") < content.index("bert 0")


# ── P2: a concurrent undo is kept ────────────────────────────────────────


async def test_p2_a_concurrent_undo_is_kept(mesh):
    a, b = mesh["A"], mesh["B"]
    pid = await _create(mesh)
    x = _with(BASE, p1="oops")
    await _edit(b, pid, x)
    await _pump(mesh)
    await _edit(a, pid, _with(x, p3="anna p3"))  # concurrent with the undo
    await _edit(b, pid, BASE)  # bert undoes his p1 change
    await _pump(mesh)
    content, sides, _seq_, _p = _converged(await _states(mesh, pid))
    assert content == _with(BASE, p3="anna p3")
    assert sides == ()


# ── P3: offline for many edits — fast-forward, never a conflict ──────────


async def test_p3_offline_for_many_edits_fast_forwards(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    for i in range(25):
        await _edit(h, pid, _with(BASE, p2=f"v{i}"))
    to_a = _take(h, a)
    _drop(*mesh.values())
    # A only gets the newest version (the rest were lost).
    et, payload = to_a[-1]
    await _receive(a, _event(h, et, payload, a))
    assert await _content(a, pid) == _with(BASE, p2="v24")
    assert await _sides(a, pid) == []
    assert await _seq(a, pid) == await _seq(h, pid) == 26
    # The lost older ones arriving late change nothing.
    for et, payload in to_a[:-1]:
        await _receive(a, _event(h, et, payload, a))
    assert await _content(a, pid) == _with(BASE, p2="v24")


async def test_p3b_offline_for_many_edits_fast_forwards_by_sync(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    for i in range(25):
        await _edit(h, pid, _with(BASE, p2=f"v{i}"))
    _drop(*mesh.values())
    await _sync_from_host(mesh, a, await _host_chunk(mesh, pid))
    assert await _content(a, pid) == _with(BASE, p2="v24")
    assert await _sides(a, pid) == []
    assert await _seq(a, pid) == 26


# ── P4: a cover change converges ─────────────────────────────────────────


async def _cover(house: House, pid: str) -> str | None:
    row = await house.db.fetchone(
        "SELECT cover_image_url FROM space_pages WHERE id=?", (pid,)
    )
    return row["cover_image_url"]


async def test_p4_a_cover_change_converges_live_and_by_sync(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    r = await h.tc.patch(
        f"/api/spaces/{SID}/pages/{pid}",
        json={"cover_image_url": "/api/media/new.webp"},
        headers=h.headers,
    )
    assert r.status == 200, await r.text()
    _take(h, a)  # A misses it live
    await _pump(mesh)
    assert await _cover(b, pid) == "/api/media/new.webp"
    await _sync_from_host(mesh, a, await _host_chunk(mesh, pid))
    assert await _cover(a, pid) == "/api/media/new.webp"
    # A member's cover change goes through the host too.
    r = await a.tc.patch(
        f"/api/spaces/{SID}/pages/{pid}",
        json={"cover_image_url": "/api/media/anna.webp"},
        headers=a.headers,
    )
    assert r.status == 200, await r.text()
    await _pump(mesh)
    for house in mesh.values():
        assert await _cover(house, pid) == "/api/media/anna.webp", house.name


# ── P5: one side per user; the host's edit merges; one resolution ────────


async def test_p5_one_side_per_user_and_one_resolution_converges(mesh):
    a, b, c, h = mesh["A"], mesh["B"], mesh["C"], mesh["H"]
    pid = await _create(mesh)
    await _edit(a, pid, _with(BASE, p2="anna p2"))
    await _edit(b, pid, _with(BASE, p2="bert p2"))
    await _edit(b, pid, _with(BASE, p2="bert p2 again"))
    await _edit(c, pid, _with(BASE, p2="carl p2"))
    # H edits a DIFFERENT paragraph before hearing of any of them.
    await _edit(h, pid, _with(BASE, p5="hanna p5"))
    await _deliver(a, h)
    await _deliver(b, h)
    await _deliver(c, h)
    await _pump(mesh)
    content, sides, _s, _p = _converged(await _states(mesh, pid))
    # Anna's merged with hanna's; bert's and carl's overlap → one side each.
    assert content == _with(BASE, p2="anna p2", p5="hanna p5")
    assert len(sides) == 2
    by = {s["by"] for s in (await _page(h, pid))["conflict"]["sides"]}
    assert by == {b.user_id, c.user_id}
    # Bert edited again before the host answered: his newer draft goes out
    # once the first is answered, and replaces his side — still ONE side.
    conflict = (await _page(h, pid))["conflict"]
    assert sorted(s["by"] for s in conflict["sides"]) == sorted([b.user_id, c.user_id])
    bert = next(s for s in conflict["sides"] if s["by"] == b.user_id)
    assert bert["content"] == _with(BASE, p2="bert p2 again")
    # The conflict never blocks an edit.
    await _edit(a, pid, _with(content, p4="anna p4"))
    await _pump(mesh)
    # Anyone who may edit resolves — anna keeps the current body.
    sides = await _sides(a, pid)
    current = (await _page(a, pid))["conflict"]["current_hash"]
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages/{pid}/resolve-conflict",
        json={"resolution": "side", "side": current, "sides": sides},
        headers=a.headers,
    )
    assert r.status == 200, await r.text()
    await _pump(mesh)
    content, sides, _s, pending = _converged(await _states(mesh, pid))
    assert sides == () and pending is None
    assert content == _with(BASE, p2="anna p2", p4="anna p4", p5="hanna p5")
    # The resolved sides live on in the host's history.
    r = await h.tc.get(f"/api/spaces/{SID}/pages/{pid}/versions", headers=h.headers)
    kept = [v["content"] for v in await r.json()]
    assert _with(BASE, p2="carl p2") in kept
    assert _with(BASE, p2="bert p2") in kept  # bert's older side


async def test_p5b_a_resolution_keeping_a_side_converges(mesh):
    a, b = mesh["A"], mesh["B"]
    pid = await _create(mesh)
    await _edit(a, pid, _with(BASE, p2="anna p2"))
    await _edit(b, pid, _with(BASE, p2="bert p2"))
    await _pump(mesh)
    conflict = (await _page(b, pid))["conflict"]
    (side,) = conflict["sides"]
    r = await b.tc.post(
        f"/api/spaces/{SID}/pages/{pid}/resolve-conflict",
        json={"resolution": "side", "side": side["hash"], "sides": [side["hash"]]},
        headers=b.headers,
    )
    assert r.status == 200, await r.text()
    await _pump(mesh)
    content, sides, _s, _p = _converged(await _states(mesh, pid))
    assert content == side["content"] and sides == ()


# ── P6: every household holds the identical conflict ─────────────────────


async def test_p6_three_branches_leave_one_identical_conflict(mesh):
    a, b, c, h = mesh["A"], mesh["B"], mesh["C"], mesh["H"]
    pid = await _create(mesh)
    await _edit(a, pid, _with(BASE, p2="anna p2"))
    await _edit(b, pid, _with(BASE, p2="bert p2"))
    await _edit(c, pid, _with(BASE, p5="carl p5"))
    await _deliver(c, h)
    await _deliver(b, h)
    await _pump(mesh)
    content, sides, seq, _p = _converged(await _states(mesh, pid))
    assert content == _with(BASE, p2="bert p2", p5="carl p5")
    assert len(sides) == 1
    conflicts = {
        name: (await _page(house, pid))["conflict"] for name, house in mesh.items()
    }
    assert len({repr(c_) for c_ in conflicts.values()}) == 1, conflicts


# ── The host unreachable: drafts wait, one proposal per page ─────────────


async def test_an_unreachable_host_gets_one_proposal_when_it_returns(mesh):
    from socialhome.domain.events import ConnectionReachable
    from socialhome.app_keys import event_bus_key

    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    await a.db.enqueue(
        "UPDATE remote_instances SET unreachable_since=datetime('now') WHERE id=?",
        (h.iid,),
    )
    for i in range(3):
        await _edit(a, pid, _with(BASE, p1=f"offline {i}"))
    assert _sent_to(a, h) == []
    assert await _pending(a, pid) == 1
    # The editor keeps working on the optimistic draft.
    page = await _page(a, pid)
    assert page["pending"] is True and page["base_seq"] == 1
    await a.db.enqueue(
        "UPDATE remote_instances SET unreachable_since=NULL WHERE id=?", (h.iid,)
    )
    await a.app[event_bus_key].publish(ConnectionReachable(instance_id=h.iid))
    proposals = _sent_to(a, h, FET.SPACE_PAGE_UPDATED)
    assert len(proposals) == 1
    assert proposals[0]["content"] == _with(BASE, p1="offline 2")
    assert proposals[0]["base_seq"] == 1
    await _pump(mesh)
    content, sides, seq, pending = _converged(await _states(mesh, pid))
    assert content == _with(BASE, p1="offline 2") and pending is None and seq == 2


async def test_stop_and_wait_one_outstanding_proposal_per_page(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    await _edit(a, pid, _with(BASE, p1="one"))
    await _edit(a, pid, _with(BASE, p1="two"))
    await _edit(a, pid, _with(BASE, p1="three"))
    assert len(_sent_to(a, h)) == 1  # the next waits for the host's answer
    await _pump(mesh)
    # The answer released the newest draft, which converged too.
    content, _sides_, _s, pending = _converged(await _states(mesh, pid))
    assert content == _with(BASE, p1="three") and pending is None


# ── A member cannot forge the host's order ───────────────────────────────


def _forged_version(pid: str, content: str, seq: int) -> dict:
    return {
        "id": pid,
        "page_id": pid,
        "space_id": SID,
        "title": "Rules",
        "content": content,
        "seq": seq,
        "conflict": [],
        "version_hash": version_hash("Rules", content),
    }


async def test_a_forged_seq_from_a_member_moves_nothing_live_chunk_or_resume(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    # Live, as an update and as a resume-style create.
    for et in (FET.SPACE_PAGE_UPDATED, FET.SPACE_PAGE_CREATED):
        forged = _forged_version(pid, "forged", 99)
        await _receive(a, _event(b, et, forged, a))
    assert (await _content(a, pid), await _seq(a, pid)) == (BASE, 1)
    # A chunk from a member for a held page …
    receiver = a.app[federation_service_key]._space_sync_receiver
    await receiver._dispatch(
        "pages",
        SID,
        [{**_forged_version(pid, "forged", 99), "created_by": b.user_id}],
        provider=b.iid,
    )
    assert (await _content(a, pid), await _seq(a, pid)) == (BASE, 1)
    # … and the host's next version still lands.
    await _edit(h, pid, _with(BASE, p1="real"))
    await _pump(mesh)
    assert (await _content(a, pid), await _seq(a, pid)) == (_with(BASE, p1="real"), 2)


async def test_a_member_chunk_adds_a_new_page_unsequenced(mesh):
    from socialhome.federation.owner_bound_id import (
        SPACE_PAGE_KIND,
        mint_owner_bound_id,
    )

    a, b = mesh["A"], mesh["B"]
    pid = mint_owner_bound_id(SPACE_PAGE_KIND, space_id=SID, owner_user_id=b.user_id)
    receiver = a.app[federation_service_key]._space_sync_receiver
    await receiver._dispatch(
        "pages",
        SID,
        [
            {
                **_forged_version(pid, "bert's page", 99),
                "created_by": b.user_id,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ],
        provider=b.iid,
    )
    assert (await _content(a, pid), await _seq(a, pid)) == ("bert's page", 0)


# ── Refusals ─────────────────────────────────────────────────────────────


async def test_an_access_refusal_restores_the_hosts_version(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    await _edit(a, pid, _with(BASE, p1="anna"))
    # The host's level changed before the proposal arrived.
    await h.db.enqueue("UPDATE spaces SET pages_access='admin_only' WHERE id=?", (SID,))
    await _pump(mesh)
    assert (await _content(a, pid), await _pending(a, pid)) == (BASE, None)
    assert await _content(h, pid) == BASE
    assert any(
        et is FET.SPACE_PAGE_UPDATED
        and p.get("sequenced", {}).get("reason") == "access"
        for et, p in a.seen
    )


async def test_a_gone_refusal_keeps_the_words_but_stops_proposing(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    r = await h.tc.delete(f"/api/spaces/{SID}/pages/{pid}", headers=h.headers)
    assert r.status == 200
    _drop(h)  # A never heard of the delete
    await _edit(a, pid, _with(BASE, p1="anna"))
    await _pump(mesh)
    assert await _content(a, pid) == _with(BASE, p1="anna")
    assert await _pending(a, pid) is None
    assert await _content(h, pid) is None


async def test_a_rate_limited_proposal_keeps_the_draft(mesh, monkeypatch):
    monkeypatch.setattr(page_conflict_service, "PROPOSALS_PER_MINUTE", 1)
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    await _edit(a, pid, _with(BASE, p1="one"))
    await _pump(mesh)
    await _edit(a, pid, _with(BASE, p1="two"))
    await _pump(mesh)
    assert await _content(h, pid) == _with(BASE, p1="one")
    assert await _content(a, pid) == _with(BASE, p1="two")
    assert await _pending(a, pid) == 2


async def test_an_archived_host_takes_no_proposal(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    await h.db.enqueue("UPDATE spaces SET archived=1 WHERE id=?", (SID,))
    await _edit(a, pid, _with(BASE, p1="late"))
    await _pump(mesh)
    assert await _content(h, pid) == BASE
    assert await _seq(h, pid) == 1


async def test_a_base_ahead_of_the_host_raises_its_floor_never_overwrites(mesh):
    """A base ahead of the host (a restored host, or a lie) never refuses
    the edit and never fast-forwards over the host's copy: the host raises
    its seq floor above it and keeps the proposal as a side."""
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    forged = {
        "id": pid,
        "page_id": pid,
        "space_id": SID,
        "title": "Rules",
        "content": "ahead of the host",
        "actor_user_id": a.user_id,
        "base_seq": 50,
    }
    await _receive(h, _event(a, FET.SPACE_PAGE_UPDATED, forged, h))
    assert await _content(h, pid) == BASE
    assert await _seq(h, pid) == 51
    sides = (await _page(h, pid))["conflict"]["sides"]
    assert [s["content"] for s in sides] == ["ahead of the host"]


# ── MODERATED: stale, and force fast-forwards ────────────────────────────


@pytest.mark.parametrize("mesh", ["moderated"], indirect=True)
async def test_moderated_edit_stale_then_force_fast_forwards(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    r = await a.tc.patch(
        f"/api/spaces/{SID}/pages/{pid}",
        json={"content": _with(BASE, p1="anna")},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    await _pump(mesh)
    await _edit(h, pid, _with(BASE, p5="hanna"))
    await _pump(mesh)
    r = await h.tc.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve", json={}, headers=h.headers
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "STALE"
    r = await h.tc.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve",
        json={"force": True},
        headers=h.headers,
    )
    assert r.status == 200, await r.text()
    await _pump(mesh)
    # The item's patch, exactly — never merged with hanna's newer edit.
    content, sides, _s, _p = _converged(await _states(mesh, pid))
    assert content == _with(BASE, p1="anna") and sides == ()


@pytest.mark.parametrize("mesh", ["moderated"], indirect=True)
async def test_moderated_queued_resolution_is_released_everywhere(mesh):
    a, h, c = mesh["A"], mesh["H"], mesh["C"]
    pid = await _create(mesh)
    await _edit(h, pid, _with(BASE, p2="hanna's p2"))
    await _edit(c, pid, _with(BASE, p2="carl's p2"))
    await _pump(mesh)
    sides = await _sides(a, pid)
    assert len(sides) == 1
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages/{pid}/resolve-conflict",
        json={"resolution": "side", "side": sides[0], "sides": sides},
        headers=a.headers,
    )
    assert r.status == 202, await r.text()
    item_id = (await r.json())["item_id"]
    await _pump(mesh)
    r = await h.tc.post(
        f"/api/spaces/{SID}/moderation/{item_id}/approve", json={}, headers=h.headers
    )
    assert r.status == 200, await r.text()
    await _pump(mesh)
    content, s, _seq_, _p = _converged(await _states(mesh, pid))
    assert content == _with(BASE, p2="carl's p2") and s == ()


# ── Mixed fleets ─────────────────────────────────────────────────────────


async def test_a_v47_member_gets_plain_versions_and_its_update_is_sequenced(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    VERSIONS[b.iid] = 47
    pid = await _create(mesh)
    await _edit(h, pid, _with(BASE, p1="hanna"))
    (to_b,) = _sent_to(h, b, FET.SPACE_PAGE_UPDATED)
    assert not {"seq", "conflict", "sequenced", "version_hash"} & set(to_b)
    (to_a,) = _sent_to(h, a, FET.SPACE_PAGE_UPDATED)
    assert to_a["seq"] == 2
    await _pump(mesh)
    # A v47 household's update, broadcast to everyone, no base: the host
    # takes it as last write wins and sequences it; A ignores the direct copy.
    legacy = {
        "id": pid,
        "page_id": pid,
        "space_id": SID,
        "title": "Rules",
        "content": _with(BASE, p1="bert"),
        "actor_user_id": b.user_id,
    }
    await _receive(a, _event(b, FET.SPACE_PAGE_UPDATED, legacy, a))
    assert await _content(a, pid) == _with(BASE, p1="hanna")
    await _receive(h, _event(b, FET.SPACE_PAGE_UPDATED, legacy, h))
    await _pump(mesh)
    assert await _content(a, pid) == await _content(h, pid) == _with(BASE, p1="bert")
    assert await _seq(a, pid) == 3


async def test_under_a_v47_host_pages_stay_last_write_wins(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    VERSIONS[h.iid] = 47
    pid = await _create(mesh)
    await _edit(a, pid, _with(BASE, p1="anna"))
    # Broadcast to everyone, the old way, no sequencing fields.
    to_b = _sent_to(a, b, FET.SPACE_PAGE_UPDATED)
    # The old way — but naming its base, so a v_48 host would merge it.
    assert to_b and to_b[0]["base_seq"] == 1 and "seq" not in to_b[0]
    await _pump(mesh)
    for house in (a, b):
        assert await _content(house, pid) == _with(BASE, p1="anna"), house.name


# ── Deletes ──────────────────────────────────────────────────────────────


async def test_delete_while_pending_or_conflicted(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _edit(a, pid, _with(BASE, p2="anna p2"))
    await _edit(b, pid, _with(BASE, p2="bert p2"))
    await _pump(mesh)
    assert await _sides(h, pid)
    await _edit(a, pid, _with(BASE, p3="pending"))  # A's draft, undelivered
    _take(a, h)
    r = await h.tc.delete(f"/api/spaces/{SID}/pages/{pid}", headers=h.headers)
    assert r.status == 200
    await _pump(mesh)
    for house in mesh.values():
        assert await _content(house, pid) is None, house.name
        rows = await house.db.fetchall(
            "SELECT 1 FROM space_page_snapshots WHERE page_id=?", (pid,)
        )
        assert rows == [], house.name


# ── The same-household stale save is unchanged ───────────────────────────


async def test_a_same_household_stale_save_is_still_409(mesh):
    a = mesh["A"]
    pid = await _create(mesh)
    loaded = (await _page(a, pid))["updated_at"]
    await _edit(a, pid, _with(BASE, p1="first tab"))
    r = await a.tc.patch(
        f"/api/spaces/{SID}/pages/{pid}",
        json={"content": "second tab", "base_updated_at": loaded},
        headers=a.headers,
    )
    assert r.status == 409
    assert (await r.json())["error"] == "stale_update"


# ── Wire shape ───────────────────────────────────────────────────────────


async def test_a_proposal_goes_to_the_host_alone_with_its_base(mesh):
    a, b, c, h = mesh["A"], mesh["B"], mesh["C"], mesh["H"]
    pid = await _create(mesh)
    await _edit(a, pid, _with(BASE, p1="anna"))
    assert _sent_to(a, b) == [] and _sent_to(a, c) == []
    (proposal,) = _sent_to(a, h)
    assert proposal["base_seq"] == 1
    assert proposal["base_hash"] == version_hash("Rules", BASE)
    assert "seq" not in proposal and "sequenced" not in proposal
    await _pump(mesh)
    canon = [p for to, et, p in h.sent]  # all delivered already
    assert canon == []


async def test_a_members_new_page_is_sequenced_by_the_host(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Anna's", "content": "hello"},
        headers=a.headers,
    )
    assert r.status == 201, await r.text()
    page = await r.json()
    assert (page["seq"], page["pending"]) == (0, True)
    pid = page["id"]
    # Proposed to the host alone, as a create.
    (proposal,) = _sent_to(a, h, FET.SPACE_PAGE_CREATED)
    assert proposal["base_seq"] == 0 and proposal["created_by"] == a.user_id
    assert _sent_to(a, b) == []
    await _pump(mesh)
    content, sides, seq, pending = _converged(await _states(mesh, pid))
    assert (content, sides, seq, pending) == ("hello", (), 1, None)


# ── Review round 2 (repros of the adversarial review) ────────────────────


async def _fake_send_event(
    self, *, to_instance_id, event_type, payload, space_id=None, **kw
):
    OUTBOX.setdefault(self._own_instance_id, []).append(
        (to_instance_id, event_type, copy.deepcopy(payload))
    )
    return DeliveryResult(instance_id=to_instance_id, ok=True)


async def test_i1_a_members_resume_replay_never_rolls_the_host_back(mesh, monkeypatch):
    monkeypatch.setattr(FederationService, "send_event", _fake_send_event)
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    await _edit(h, pid, _with(BASE, p1="v2"))
    await _pump(mesh)
    await _edit(h, pid, _with(BASE, p1="v3 newest"))
    _drop(*mesh.values())  # A misses v3
    assert (await _seq(h, pid), await _seq(a, pid)) == (3, 2)
    # The host asks A to resume (a long offline / operator resync).
    await _receive(
        a,
        _event(
            h,
            FET.SPACE_SYNC_RESUME,
            {"space_id": SID, "since": "1970-01-01T00:00:00+00:00"},
            a,
        ),
    )
    # A replays no page to the host: the host is the pages' authority.
    assert not [p for t, e, p in a.sent if t == h.iid and e is FET.SPACE_PAGE_CREATED]
    await _pump(mesh)
    assert await _content(h, pid) == _with(BASE, p1="v3 newest")
    assert await _seq(h, pid) == 3


async def test_i1_a_seq_carrying_or_baseless_replay_from_a_v48_member_is_ignored(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    await _edit(h, pid, _with(BASE, p1="v2"))
    _drop(*mesh.values())
    stale = {
        "id": pid,
        "page_id": pid,
        "space_id": SID,
        "title": "Rules",
        "content": BASE,
        "created_by": h.user_id,
        "actor_user_id": a.user_id,
    }
    for payload, et in (
        ({**stale, "seq": 1}, FET.SPACE_PAGE_CREATED),
        (stale, FET.SPACE_PAGE_CREATED),
        ({**stale, "seq": 1}, FET.SPACE_PAGE_UPDATED),
    ):
        await _receive(h, _event(a, et, payload, h))
    assert await _content(h, pid) == _with(BASE, p1="v2")
    assert await _seq(h, pid) == 2


async def test_i2_a_proposal_naming_another_households_user_is_refused(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _edit(h, pid, _with(BASE, p2="hanna p2"))
    await _edit(b, pid, _with(BASE, p2="bert p2"))
    await _pump(mesh)
    for content in (
        _with(BASE, p2="anna pretending to be bert"),
        _with(BASE, p1="vandalism"),
    ):
        forged = {
            "id": pid,
            "page_id": pid,
            "space_id": SID,
            "title": "Rules",
            "content": content,
            "actor_user_id": b.user_id,
            "base_seq": 1,
            "base_hash": version_hash("Rules", BASE),
        }
        await _receive(h, _event(a, FET.SPACE_PAGE_UPDATED, forged, h))
    page = await _page(h, pid)
    assert not any("pretending" in s["content"] for s in page["conflict"]["sides"])
    assert [s["by"] for s in page["conflict"]["sides"]] == [b.user_id]
    assert "vandalism" not in page["content"]
    assert page["last_editor_user_id"] != b.user_id or "bert" in page["content"]
    refusals = [
        p for p in _sent_to(h, a) if p.get("sequenced", {}).get("outcome") == "refused"
    ]
    assert refusals and refusals[0]["sequenced"]["reason"] == "access"
    # At most one refusal per (sender, page) per window (M1).
    assert len(refusals) == 1


async def test_i3_a_member_unaware_of_the_hosts_v48_still_mirrors_it(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    VERSIONS[h.iid] = 1  # A has not seen H's capabilities yet
    await _edit(h, pid, _with(BASE, p1="hanna"))
    await _pump(mesh)
    assert await _content(a, pid) == await _content(h, pid) == _with(BASE, p1="hanna")
    assert await _seq(a, pid) == 2


async def test_i4_a_member_chunk_never_creates_a_page_on_the_host(mesh):
    a, h = mesh["A"], mesh["H"]
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Anna's", "content": "hello"},
        headers=a.headers,
    )
    pid = (await r.json())["id"]
    held = _take(a, h)  # the create proposal is in flight
    records = [
        r
        for r in await PagesExporter(a.app[page_repo_key]).list_records(SID)
        if r["id"] == pid
    ]
    # The unacked draft is never exported (M3).
    assert records == []
    receiver = h.app[federation_service_key]._space_sync_receiver
    forged = {
        "id": pid,
        "title": "Anna's",
        "content": "hello",
        "created_by": a.user_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    await receiver._dispatch("pages", SID, [forged], provider=a.iid)
    assert await _content(h, pid) is None
    a.sent.extend((h.iid, et, p) for et, p in held)
    await _pump(mesh)
    content, sides, seq, pending = _converged(await _states(mesh, pid))
    assert (content, seq, pending) == ("hello", 1, None)


async def test_i4_a_duplicate_on_an_unsequenced_page_is_sequenced_and_broadcast(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    for house in mesh.values():  # a pre-v_48 page everywhere: seq 0
        await house.db.enqueue("UPDATE space_pages SET seq=0 WHERE id=?", (pid,))
    proposal = {
        "id": pid,
        "page_id": pid,
        "space_id": SID,
        "title": "Rules",
        "content": BASE,
        "actor_user_id": a.user_id,
        "base_seq": 0,
        "base_hash": version_hash("Rules", BASE),
    }
    await _receive(h, _event(a, FET.SPACE_PAGE_UPDATED, proposal, h))
    assert await _seq(h, pid) == 1
    assert _sent_to(h, b, FET.SPACE_PAGE_UPDATED) or _sent_to(
        h, b, FET.SPACE_PAGE_CREATED
    )
    await _pump(mesh)
    assert await _seq(b, pid) == 1


async def test_i5_an_archived_host_refuses_and_the_member_stops_waiting(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    await h.db.enqueue("UPDATE spaces SET archived=1 WHERE id=?", (SID,))
    await _edit(a, pid, _with(BASE, p1="late"))
    await _pump(mesh)
    assert await _pending(a, pid) is None
    assert await _content(a, pid) == BASE
    assert await _content(h, pid) == BASE


async def test_i5_a_malformed_proposal_is_answered_bad_base(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    bad = {
        "id": pid,
        "page_id": pid,
        "space_id": SID,
        "title": "Rules",
        "content": "x",
        "actor_user_id": a.user_id,
        "base_seq": "three",
    }
    await _receive(h, _event(a, FET.SPACE_PAGE_UPDATED, bad, h))
    await _receive(h, _event(a, FET.SPACE_PAGE_UPDATED, bad, h))
    refusals = _sent_to(h, a)
    assert len(refusals) == 1
    assert refusals[0]["sequenced"]["reason"] == "bad_base"


async def test_m5_an_overlong_title_is_refused(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    long = {
        "id": pid,
        "page_id": pid,
        "space_id": SID,
        "title": "x" * 201,
        "content": BASE,
        "actor_user_id": a.user_id,
        "base_seq": 1,
        "base_hash": version_hash("Rules", BASE),
    }
    await _receive(h, _event(a, FET.SPACE_PAGE_UPDATED, long, h))
    assert (await _page(h, pid))["title"] == "Rules"


# ── Review round 3 ───────────────────────────────────────────────────────

STRANGER = "x" * 52


async def _archived_proposal(h, pid, *, sender: str, actor: str):
    now = datetime.now(timezone.utc).isoformat()
    return FederationEvent(
        msg_id=f"m-{next(_IDS)}",
        event_type=FET.SPACE_PAGE_UPDATED,
        from_instance=sender,
        to_instance=h.iid,
        timestamp=now,
        payload={
            "id": pid,
            "page_id": pid,
            "space_id": SID,
            "title": "x",
            "content": "y",
            "actor_user_id": actor,
            "base_seq": 1,
        },
        space_id=SID,
    )


async def test_c1_an_archived_refusal_reaches_no_non_member_and_carries_no_page(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh, "secret wiki text")
    now = datetime.now(timezone.utc).isoformat()
    await h.db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version, capabilities_seen_at)"
        " VALUES(?, 'X', ?, '00', '00', 'https://x.test/inbox/x', 'x_local',"
        " 'confirmed', 'manual', 48, ?)",
        (STRANGER, "cd" * 32, now),
    )
    await h.db.enqueue("UPDATE spaces SET archived=1 WHERE id=?", (SID,))
    await _receive(h, await _archived_proposal(h, pid, sender=STRANGER, actor="u-x"))
    assert [p for t, _e, p in h.sent if t == STRANGER] == []  # silence, no oracle
    await _receive(h, await _archived_proposal(h, pid, sender=a.iid, actor=a.user_id))
    (refusal,) = _sent_to(h, a)
    assert refusal["sequenced"]["reason"] == "archived"
    assert "content" not in refusal and "title" not in refusal
    assert "conflict" not in refusal or refusal["conflict"] == []


async def test_c1_a_member_restores_its_own_base_on_an_archived_refusal(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    await h.db.enqueue("UPDATE spaces SET archived=1 WHERE id=?", (SID,))
    await _edit(a, pid, _with(BASE, p1="late"))
    await _pump(mesh)
    assert (await _content(a, pid), await _pending(a, pid)) == (BASE, None)


async def test_a_stale_capability_resume_never_resurrects_a_deleted_page(
    mesh, monkeypatch
):
    monkeypatch.setattr(FederationService, "send_event", _fake_send_event)
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    r = await h.tc.delete(f"/api/spaces/{SID}/pages/{pid}", headers=h.headers)
    assert r.status == 200
    h.sent[:] = [(t, e, p) for t, e, p in h.sent if t != a.iid]  # A misses it
    await _pump(mesh)
    assert await _content(b, pid) is None and await _content(a, pid) is not None
    VERSIONS[h.iid] = 1  # A has not seen H's v48 yet: it replays its pages
    await _receive(
        a,
        _event(
            h,
            FET.SPACE_SYNC_RESUME,
            {"space_id": SID, "since": "1970-01-01T00:00:00+00:00"},
            a,
        ),
    )
    VERSIONS.pop(h.iid)
    await _pump(mesh)
    assert await _content(h, pid) is None and await _content(b, pid) is None


async def test_i6_a_host_restored_from_backup_converges_again(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    for i in range(4):
        await _edit(h, pid, _with(BASE, p1=f"v{i}"))
    await _pump(mesh)
    assert await _seq(a, pid) == 5
    # The host restores a backup taken at seq 2.
    await h.db.enqueue(
        "UPDATE space_pages SET seq=2, content=? WHERE id=?",
        (_with(BASE, p1="v0"), pid),
    )
    await _edit(h, pid, _with(BASE, p1="after restore"))
    await _pump(mesh)
    await _edit(a, pid, _with(BASE, p1="after restore", p5="anna"))
    await _pump(mesh)
    assert await _content(a, pid) == await _content(h, pid)
    assert await _seq(a, pid) == await _seq(h, pid) > 5
    assert await _pending(a, pid) is None
    # Anna's edit was not lost: it is current or kept as her side.
    kept = [
        await _content(h, pid),
        *[
            s["content"]
            for s in ((await _page(h, pid))["conflict"] or {"sides": []})["sides"]
        ],
    ]
    assert _with(BASE, p1="after restore", p5="anna") in kept


async def test_m7_a_demoted_author_still_edits_their_own_page(mesh):
    a, h = mesh["A"], mesh["H"]
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Anna's", "content": "hello"},
        headers=a.headers,
    )
    pid = (await r.json())["id"]
    await _pump(mesh)
    # Anna is demoted to a read-only seat; her household keeps a writer.
    await h.db.enqueue(
        "UPDATE space_remote_members SET role='subscriber' WHERE user_id=?",
        (a.user_id,),
    )
    await h.db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role,"
        " display_name) VALUES(?, ?, 'u-anton', 'member', 'Anton')",
        (SID, a.iid),
    )
    proposal = {
        "id": pid,
        "page_id": pid,
        "space_id": SID,
        "title": "Anna's",
        "content": "edited",
        "actor_user_id": a.user_id,
        "base_seq": 1,
        "base_hash": version_hash("Anna's", "hello"),
    }
    await _receive(h, _event(a, FET.SPACE_PAGE_UPDATED, proposal, h))
    assert await _content(h, pid) == "edited"
    # … but not somebody else's page.
    other = await _create(mesh)
    theirs = {
        "id": other,
        "page_id": other,
        "space_id": SID,
        "title": "Rules",
        "content": "nope",
        "actor_user_id": a.user_id,
        "base_seq": 1,
        "base_hash": version_hash("Rules", BASE),
    }
    await _receive(h, _event(a, FET.SPACE_PAGE_UPDATED, theirs, h))
    assert await _content(h, other) == BASE


async def test_m8_refusals_per_sender_are_capped_across_pages(mesh, monkeypatch):
    monkeypatch.setattr(page_conflict_service, "REFUSALS_PER_SENDER_PER_MINUTE", 3)
    a, h = mesh["A"], mesh["H"]
    for i in range(10):
        gone = {
            "id": f"pg-gone-{i}",
            "page_id": f"pg-gone-{i}",
            "space_id": SID,
            "title": "T",
            "content": "x",
            "actor_user_id": a.user_id,
            "base_seq": 4,
        }
        await _receive(h, _event(a, FET.SPACE_PAGE_UPDATED, gone, h))
    assert len(_sent_to(h, a)) == 3


async def test_a_legacy_mode_broadcast_carries_its_base_and_is_merged(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    await _edit(h, pid, _with(BASE, p5="hanna"))  # A misses this one
    _take(h, a)
    await _pump(mesh)
    VERSIONS[h.iid] = 47  # A sees the host as pre-v_48: last write wins mode
    await _edit(a, pid, _with(BASE, p1="anna"))
    (legacy,) = _sent_to(a, h, FET.SPACE_PAGE_UPDATED)
    assert legacy["base_seq"] == 1 and legacy["base_hash"] == version_hash(
        "Rules", BASE
    )
    VERSIONS.pop(h.iid)
    await _pump(mesh)
    # Merged at the host, never an overwrite of hanna's newer edit.
    assert await _content(h, pid) == _with(BASE, p1="anna", p5="hanna")


# ── Review round 4 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("claim", [2**53, 2**53 - 1, 2**20 + 5])
async def test_r3_1_a_huge_base_seq_cannot_wedge_the_page(mesh, claim):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    evil = {
        "id": pid,
        "page_id": pid,
        "space_id": SID,
        "title": "Rules",
        "content": _with(BASE, p5="bert"),
        "actor_user_id": b.user_id,
        "base_seq": claim,
        "base_hash": version_hash("Rules", BASE),
    }
    await _receive(h, _event(b, FET.SPACE_PAGE_UPDATED, evil, h))
    await _pump(mesh)
    # The claim does not move seq: kept as bert's side one step up.
    assert await _seq(h, pid) == 2
    assert await _seq(a, pid) == 2
    await _edit(a, pid, _with(BASE, p1="anna"))
    await _pump(mesh)
    assert await _pending(a, pid) is None
    assert "anna" in await _content(h, pid)
    assert await _content(a, pid) == await _content(h, pid)


async def test_r3_1_a_host_version_above_max_seq_is_malformed(mesh):
    from socialhome.services.page_conflict_service import MAX_SEQ

    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    forged = {
        "id": pid,
        "page_id": pid,
        "space_id": SID,
        "title": "Rules",
        "content": "x",
        "seq": MAX_SEQ + 1,
        "conflict": [],
    }
    await _receive(a, _event(h, FET.SPACE_PAGE_UPDATED, forged, a))
    assert (await _content(a, pid), await _seq(a, pid)) == (BASE, 1)


async def test_r3_2_a_stale_view_members_live_create_reaches_everyone(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    VERSIONS[h.iid] = 1  # A has not seen H's v_48 yet; H knows A is v_48
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Anna's", "content": "hello"},
        headers=a.headers,
    )
    assert r.status == 201
    pid = (await r.json())["id"]
    (to_h,) = _sent_to(a, h, FET.SPACE_PAGE_CREATED)
    assert to_h["base_seq"] == 0
    await _pump(mesh)
    VERSIONS.pop(h.iid)
    assert await _content(h, pid) == "hello"
    assert await _seq(h, pid) == 1
    await _pump(mesh)
    assert await _content(b, pid) == "hello"


async def test_r3_2_a_resume_replay_is_marked_as_one(mesh, monkeypatch):
    monkeypatch.setattr(FederationService, "send_event", _fake_send_event)
    a, h = mesh["A"], mesh["H"]
    await _create(mesh)
    VERSIONS[h.iid] = 1
    await _receive(
        a,
        _event(
            h,
            FET.SPACE_SYNC_RESUME,
            {"space_id": SID, "since": "1970-01-01T00:00:00+00:00"},
            a,
        ),
    )
    replays = [p for t, e, p in a.sent if t == h.iid and e is FET.SPACE_PAGE_CREATED]
    assert replays and all(p.get("replay") is True for p in replays)


# ── The proposer sees versions out of order (demo `all` finding) ────────


async def _deliver_reversed(sender: House, to: House) -> None:
    for et, payload in reversed(_take(sender, to)):
        await _receive(to, _event(sender, et, payload, to))


async def test_a_draft_settles_when_n_plus_1_arrives_before_its_ack(mesh):
    """Carol's proposal fast-forwards (seq 2); Alice's becomes a side
    (seq 3). Carol hears seq 3 before seq 2: her draft is already the body
    of seq 3, so it settles — nothing stays pending."""
    a, c, h = mesh["A"], mesh["C"], mesh["H"]
    pid = await _create(mesh)
    await _edit(c, pid, _with(BASE, p2="Two by Carol."))
    await _edit(a, pid, _with(BASE, p2="Two by Alice."))
    await _deliver(c, h)
    await _deliver(a, h)
    await _deliver_reversed(h, c)
    assert await _pending(c, pid) is None
    await _pump(mesh)
    content, sides, seq, pending = _converged(await _states(mesh, pid))
    assert (content, seq, pending) == (_with(BASE, p2="Two by Carol."), 3, None)
    assert len(sides) == 1


async def test_a_draft_settles_when_its_ack_is_lost(mesh):
    a, c, h = mesh["A"], mesh["C"], mesh["H"]
    pid = await _create(mesh)
    await _edit(c, pid, _with(BASE, p2="Two by Carol."))
    await _edit(a, pid, _with(BASE, p2="Two by Alice."))
    await _deliver(c, h)
    await _deliver(a, h)
    to_c = _take(h, c)
    assert len(to_c) == 2
    et, payload = to_c[-1]  # seq 3 only; the seq-2 ack is lost
    await _receive(c, _event(h, et, payload, c))
    assert await _pending(c, pid) is None
    assert await _seq(c, pid) == 3


async def test_a_merged_draft_settles_on_its_late_ack(mesh):
    """Carol's draft is MERGED into a different body (seq 3), then the host
    edits again (seq 4). Carol hears seq 4, then her late ack: the draft
    settles and seq 4 — the newest — is what she shows."""
    b, c, h = mesh["B"], mesh["C"], mesh["H"]
    pid = await _create(mesh)
    await _edit(b, pid, _with(BASE, p1="bert"))
    await _edit(c, pid, _with(BASE, p3="carol"))
    await _deliver(b, h)  # seq 2
    await _deliver(c, h)  # seq 3: merged
    await _edit(h, pid, _with(BASE, p1="bert", p3="carol", p5="hanna"))  # seq 4
    await _deliver_reversed(h, c)
    assert await _pending(c, pid) is None
    assert await _content(c, pid) == _with(BASE, p1="bert", p3="carol", p5="hanna")
    assert await _seq(c, pid) == 4
    await _pump(mesh)
    _converged(await _states(mesh, pid))


# ── Review round 5: only a SENT, resolve-free draft settles by content ──


async def _host_offline_for(a: House, h: House, offline: bool) -> None:
    await a.db.enqueue(
        "UPDATE remote_instances SET unreachable_since="
        + ("datetime('now')" if offline else "NULL")
        + " WHERE id=?",
        (h.iid,),
    )


async def _reconnect(a: House, h: House, mesh) -> None:
    from socialhome.app_keys import event_bus_key
    from socialhome.domain.events import ConnectionReachable

    await _host_offline_for(a, h, False)
    await a.app[event_bus_key].publish(ConnectionReachable(instance_id=h.iid))
    await _pump(mesh)


async def _with_bert_side(mesh) -> tuple[str, list[str]]:
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _edit(h, pid, _with(BASE, p2="hanna p2"))
    await _edit(b, pid, _with(BASE, p2="bert p2"))
    await _pump(mesh)
    sides = await _sides(a, pid)
    assert len(sides) == 1
    return pid, sides


async def test_r4_1_an_unsent_side_resolution_survives_a_newer_host_version(mesh):
    a, h = mesh["A"], mesh["H"]
    pid, sides = await _with_bert_side(mesh)
    await _host_offline_for(a, h, True)
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages/{pid}/resolve-conflict",
        json={"resolution": "side", "side": sides[0], "sides": sides},
        headers=a.headers,
    )
    assert r.status == 200, await r.text()
    assert _sent_to(a, h) == []
    # The host edits elsewhere; bert's side stays open in that version.
    await _edit(h, pid, _with(BASE, p2="hanna p2", p5="hanna p5"))
    await _pump(mesh)
    assert await _pending(a, pid) is not None  # not settled by content
    await _reconnect(a, h, mesh)
    assert "bert p2" in await _content(h, pid)
    assert await _sides(h, pid) == []
    _converged(await _states(mesh, pid))


async def test_r4_1_an_unsent_mine_resolution_survives_a_newer_host_version(mesh):
    a, h = mesh["A"], mesh["H"]
    pid, sides = await _with_bert_side(mesh)
    await _host_offline_for(a, h, True)
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages/{pid}/resolve-conflict",
        json={"resolution": "mine", "sides": sides},
        headers=a.headers,
    )
    assert r.status == 200, await r.text()
    # A newer host version with the same body: carl's conflicting edit
    # only adds a side.
    seq_before = await _seq(h, pid)
    c = mesh["C"]
    late = {
        "id": pid,
        "page_id": pid,
        "space_id": SID,
        "title": "Rules",
        "content": _with(BASE, p2="carl p2"),
        "actor_user_id": c.user_id,
        "base_seq": 1,
        "base_hash": version_hash("Rules", BASE),
    }
    await _receive(h, _event(c, FET.SPACE_PAGE_UPDATED, late, h))
    await _pump(mesh)
    assert await _seq(a, pid) > seq_before  # the newer version reached A
    assert await _pending(a, pid) is not None
    await _reconnect(a, h, mesh)
    # Anna's resolution retired the side she saw (bert's); carl's, which
    # she never saw, stays open.
    page = await _page(h, pid)
    assert await _content(h, pid) == _with(BASE, p2="hanna p2")
    assert [s["by"] for s in page["conflict"]["sides"]] == [mesh["C"].user_id]
    _converged(await _states(mesh, pid))


async def test_r4_1_an_unsent_edit_equal_to_another_users_side_is_still_proposed(mesh):
    a, h = mesh["A"], mesh["H"]
    pid, _sides_ = await _with_bert_side(mesh)
    await _host_offline_for(a, h, True)
    # Anna independently writes exactly bert's version (not via resolve).
    await _edit(a, pid, _with(BASE, p2="bert p2"))
    await _edit(h, pid, _with(BASE, p2="hanna p2", p5="hanna p5"))
    await _pump(mesh)
    assert await _pending(a, pid) is not None  # bert's side is not ours
    await _reconnect(a, h, mesh)
    assert await _pending(a, pid) is None
    _converged(await _states(mesh, pid))
    # The host had it: anna's proposal reached it and was sequenced.
    assert any(
        p.get("sequenced", {}).get("proposer_instance") == a.iid for _et, p in a.seen
    )
