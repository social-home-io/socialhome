"""Release-blocker protocol tests: space page tombstones, a host change
with drafts pending, and a restored host's seq floor (§4.4.4.1, §25.6).

Marked ``@pytest.mark.security``. Same four real households as
:mod:`.test_space_page_host_sequencing` (H the host, A / B members, C a
moderator), every send captured and delivered through the real gates.

* **Tombstones** — a page delete keeps a content-free tombstone. A
  household that missed the delete (offline, or the envelope lost) can no
  longer bring the page back: not by its sync chunk, not by its resume
  replay, not by a late proposal; and it learns of the delete from the
  host's ``pages_deleted`` stream or the resume replay of the delete.
* **Host change** — a member's pending draft goes to the NEW host; a
  household that becomes host commits its own drafts as host; stale
  answers of the old host move nothing.
* **Restored host** — a host restored from a backup learns the members'
  ``seq`` from their sync chunks (never their content), so its next own
  edit reaches them.
"""

from __future__ import annotations

import copy

import pytest

from socialhome.app_keys import (
    federation_service_key,
    page_proposal_forwarder_key,
    page_repo_key,
)
from socialhome.federation.federation_service import FederationService
from socialhome.federation.sync.space.exporters.pages import PagesExporter
from socialhome.federation.sync.space.exporters.pages_deleted import (
    PagesDeletedExporter,
)
from socialhome.services import page_conflict_service

from . import test_space_page_host_sequencing as host_sequencing
from .test_space_page_host_sequencing import (
    BASE,
    FET,
    SID,
    House,
    _content,
    _create,
    _deliver,
    _drop,
    _edit,
    _event,
    _fake_send_event,
    _page,
    _pending,
    _pump,
    _receive,
    _sent_to,
    _seq,
    _states,
    _take,
    _with,
)

#: The four-household fixture of the host-sequencing suite.
mesh = host_sequencing.mesh

pytestmark = pytest.mark.security


async def _deleted(house: House, pid: str) -> bool:
    row = await house.db.fetchone(
        "SELECT deleted_at FROM space_pages WHERE id=? AND space_id=?", (pid, SID)
    )
    return row is not None and row["deleted_at"] is not None


async def _bodies_left(house: House, pid: str) -> int:
    hist = await house.db.fetchall(
        "SELECT 1 FROM page_edit_history WHERE page_id=? AND space_id=?", (pid, SID)
    )
    snaps = await house.db.fetchall(
        "SELECT 1 FROM space_page_snapshots WHERE page_id=? AND space_id=?",
        (pid, SID),
    )
    return len(hist) + len(snaps)


async def _chunk(
    house: House, to: House, resource: str, records: list[dict] | None = None
) -> None:
    """``house`` streams its ``resource`` records (§25.6) to ``to``."""
    if records is None:
        repo = house.app[page_repo_key]
        exporter = (
            PagesExporter(repo) if resource == "pages" else PagesDeletedExporter(repo)
        )
        records = await exporter.list_records(SID)
    receiver = to.app[federation_service_key]._space_sync_receiver
    await receiver._dispatch(resource, SID, copy.deepcopy(records), provider=house.iid)


async def _delete(house: House, pid: str) -> None:
    r = await house.tc.delete(f"/api/spaces/{SID}/pages/{pid}", headers=house.headers)
    assert r.status == 200, await r.text()


async def _delete_missed_by(mesh, pid: str, *missing: House, by: str = "H") -> None:
    """``by`` deletes the page; ``missing`` never get the delete."""
    sender = mesh[by]
    await _delete(sender, pid)
    for house in missing:
        _take(sender, house)
    await _pump(mesh)


# ── Tombstones ───────────────────────────────────────────────────────────


async def test_a_delete_keeps_a_blank_tombstone_everywhere(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _edit(a, pid, _with(BASE, p2="anna p2"))
    await _edit(b, pid, _with(BASE, p2="bert p2"))
    await _pump(mesh)  # history + a conflict side everywhere
    await _delete_missed_by(mesh, pid)
    for house in mesh.values():
        assert await _deleted(house, pid), house.name
        assert await _content(house, pid) is None, house.name
        row = await house.db.fetchone(
            "SELECT title, content, deleted_by FROM space_pages WHERE id=?", (pid,)
        )
        assert (row["title"], row["content"]) == ("", ""), house.name
        assert row["deleted_by"] == h.user_id, house.name
        assert await _bodies_left(house, pid) == 0, house.name


async def test_a_missed_delete_cannot_come_back_by_a_member_chunk(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _delete_missed_by(mesh, pid, a)
    assert await _content(a, pid) == BASE
    # A streams its stale copy to a member that deleted it, and to the host.
    await _chunk(a, b, "pages")
    await _chunk(a, h, "pages")
    assert await _content(b, pid) is None and await _deleted(b, pid)
    assert await _content(h, pid) is None and await _deleted(h, pid)
    # The host's tombstone stream tells A.
    await _chunk(h, a, "pages_deleted")
    assert await _deleted(a, pid) and await _content(a, pid) is None
    assert await _bodies_left(a, pid) == 0


async def test_a_host_stub_keeps_a_stale_copy_out_of_a_household_that_never_held_it(
    mesh,
):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    r = await h.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Rules", "content": BASE},
        headers=h.headers,
    )
    pid = (await r.json())["id"]
    _take(h, b)  # B never got the page …
    await _pump(mesh)
    await _delete_missed_by(mesh, pid, a, b)  # … nor (with A) its delete
    assert await _content(b, pid) is None and not await _deleted(b, pid)
    await _chunk(h, b, "pages_deleted")
    assert await _deleted(b, pid)
    # A's stale copy no longer lands on B.
    await _chunk(a, b, "pages")
    assert await _content(b, pid) is None


async def test_a_member_tombstone_record_never_stubs_an_unseen_id(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    r = await h.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Rules", "content": BASE},
        headers=h.headers,
    )
    pid = (await r.json())["id"]
    _take(h, b)
    await _pump(mesh)
    # A (a member) claims a delete of a page B never held: no stub.
    await _chunk(
        a,
        b,
        "pages_deleted",
        [{"id": pid, "space_id": SID, "created_by": h.user_id}],
    )
    assert not await _deleted(b, pid)


async def test_an_unbound_id_from_the_host_gets_no_stub(mesh):
    b, h = mesh["B"], mesh["H"]
    await _chunk(
        h,
        b,
        "pages_deleted",
        [{"id": "legacy-uuid", "space_id": SID, "created_by": h.user_id}],
    )
    assert not await _deleted(b, "legacy-uuid")


async def test_a_member_delete_the_host_missed_reaches_it_by_sync(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _delete_missed_by(mesh, pid, h, a, by="B")
    assert await _content(h, pid) == BASE
    # The host's next version does not bring the page back on B.
    await _edit(h, pid, _with(BASE, p1="host edit"))
    await _pump(mesh)
    assert await _deleted(b, pid) and await _content(b, pid) is None
    # B's tombstone stream reaches the host (live delete rules) …
    await _chunk(b, h, "pages_deleted")
    assert await _deleted(h, pid)
    # … and the host's own stream then reaches A, who held it at seq 2.
    assert await _seq(a, pid) == 2
    await _chunk(h, a, "pages_deleted")
    assert await _deleted(a, pid)


async def test_a_member_tombstone_needs_the_delete_level(mesh):
    b, h = mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _delete_missed_by(mesh, pid, h, by="B")
    await h.db.enqueue("UPDATE spaces SET pages_access='admin_only' WHERE id=?", (SID,))
    await _chunk(b, h, "pages_deleted")
    assert not await _deleted(h, pid)
    assert await _content(h, pid) == BASE


async def test_a_resume_replays_the_delete_with_the_deleter(mesh, monkeypatch):
    monkeypatch.setattr(FederationService, "send_event", _fake_send_event)
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    await _delete_missed_by(mesh, pid, a)
    await _receive(
        h,
        _event(
            a,
            FET.SPACE_SYNC_RESUME,
            {"space_id": SID, "since": "1970-01-01T00:00:00+00:00"},
            h,
        ),
    )
    (replayed,) = _sent_to(h, a, FET.SPACE_PAGE_DELETED)
    assert replayed["id"] == pid
    assert replayed["actor_user_id"] == h.user_id
    assert replayed["created_by"] == h.user_id
    await _deliver(h, a)
    assert await _deleted(a, pid) and await _content(a, pid) is None


async def test_a_stale_members_resume_replay_cannot_resurrect_a_page(mesh, monkeypatch):
    monkeypatch.setattr(FederationService, "send_event", _fake_send_event)
    a, b = mesh["A"], mesh["B"]
    pid = await _create(mesh)
    await _delete_missed_by(mesh, pid, a)
    # B asks A (which missed the delete) to resume: A replays its copy.
    await _receive(
        a,
        _event(
            b,
            FET.SPACE_SYNC_RESUME,
            {"space_id": SID, "since": "1970-01-01T00:00:00+00:00"},
            a,
        ),
    )
    assert _sent_to(a, b, FET.SPACE_PAGE_CREATED)
    await _deliver(a, b)
    assert await _content(b, pid) is None and await _deleted(b, pid)


async def test_a_late_proposal_for_a_deleted_page_is_refused_gone(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    await _edit(a, pid, _with(BASE, p1="anna"))
    proposal = _take(a, h)
    await _delete_missed_by(mesh, pid, a)
    for et, payload in proposal:
        await _receive(h, _event(a, et, payload, h))
    assert await _deleted(h, pid) and await _content(h, pid) is None
    refusals = [
        p
        for p in _sent_to(h, a, FET.SPACE_PAGE_UPDATED)
        if p.get("sequenced", {}).get("reason") == "gone"
    ]
    assert len(refusals) == 1 and "title" not in refusals[0]
    await _pump(mesh)
    # A keeps its words (the SPA offers "Save as new page") and stops proposing.
    assert await _content(a, pid) == _with(BASE, p1="anna")
    assert await _pending(a, pid) is None


async def test_a_create_proposal_for_a_tombstoned_id_is_refused_gone(mesh):
    a, h = mesh["A"], mesh["H"]
    r = await a.tc.post(
        f"/api/spaces/{SID}/pages",
        json={"title": "Anna's", "content": "hello"},
        headers=a.headers,
    )
    pid = (await r.json())["id"]
    create = _take(a, h)
    await h.app[page_repo_key].tombstone(pid, space_id=SID, created_by=a.user_id)
    for et, payload in create:
        await _receive(h, _event(a, et, payload, h))
    assert await _content(h, pid) is None
    assert [
        p["sequenced"]["reason"]
        for p in _sent_to(h, a, FET.SPACE_PAGE_UPDATED)
        if "sequenced" in p
    ] == ["gone"]


async def test_a_host_version_never_resurrects_a_page_deleted_here(mesh):
    b, h = mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _delete_missed_by(mesh, pid, h, by="B")
    # Even a create-shaped host version at a higher seq stays out.
    await _receive(
        b,
        _event(
            h,
            FET.SPACE_PAGE_CREATED,
            {
                "id": pid,
                "page_id": pid,
                "space_id": SID,
                "title": "Rules",
                "content": "back again",
                "created_by": h.user_id,
                "seq": 9,
                "conflict": [],
            },
            b,
        ),
    )
    assert await _content(b, pid) is None and await _deleted(b, pid)


# ── Host change with drafts pending ──────────────────────────────────────


async def _move_host(mesh, to: House) -> None:
    for house in mesh.values():
        await house.db.enqueue(
            "UPDATE spaces SET owner_instance_id=? WHERE id=?", (to.iid, SID)
        )


async def test_a_members_pending_draft_is_proposed_to_the_new_host(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _edit(a, pid, _with(BASE, p1="anna"))
    # H sequences A's proposal, but its answer is lost in transit.
    await _deliver(a, h)
    stale = _take(h, a)
    _drop(h)
    assert await _pending(a, pid) == 1
    await _move_host(mesh, b)
    await a.app[page_proposal_forwarder_key].flush(resend=True)
    (proposal,) = _sent_to(a, b, FET.SPACE_PAGE_UPDATED)
    assert proposal["base_seq"] == 1
    assert _sent_to(a, h) == []
    await _pump(mesh)
    assert await _pending(a, pid) is None
    assert await _content(b, pid) == _with(BASE, p1="anna")
    assert await _seq(b, pid) == 2
    # The old host's late answer moves nothing: only the owner's count.
    for et, payload in stale:
        await _receive(a, _event(h, et, payload, a))
    for house in (a, b, mesh["C"]):
        assert await _content(house, pid) == _with(BASE, p1="anna"), house.name
        assert await _seq(house, pid) == 2, house.name


async def test_a_household_that_becomes_host_commits_its_own_drafts(mesh):
    a, b, c, h = mesh["A"], mesh["B"], mesh["C"], mesh["H"]
    pid = await _create(mesh)
    await _edit(h, pid, _with(BASE, p5="host v2"))
    await _pump(mesh)
    await _edit(b, pid, _with(BASE, p5="host v2", p1="bert"))
    _take(b, h)  # the proposal never reached the old host
    assert await _pending(b, pid) == 2
    await _move_host(mesh, b)
    await b.app[page_proposal_forwarder_key].flush(resend=True)
    # No proposal to the former host: it only gets B's canonical version.
    assert [p for p in _sent_to(b, h) if "base_seq" in p] == []
    assert await _pending(b, pid) is None
    # Committed as host above the highest seq it had mirrored.
    assert await _seq(b, pid) == 3
    await _pump(mesh)
    for house in (a, b, c, h):
        assert await _content(house, pid) == _with(BASE, p5="host v2", p1="bert"), (
            house.name
        )
        assert await _seq(house, pid) == 3, house.name
        assert await _pending(house, pid) is None, house.name


async def test_a_new_hosts_draft_made_under_an_older_version_is_merged(mesh):
    """The draft's base is the old host's version it was made from, the
    current version the newest one mirrored meanwhile."""
    b, h = mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _edit(b, pid, _with(BASE, p1="bert"))
    _take(b, h)
    await _edit(h, pid, _with(BASE, p5="host v2"))
    await _pump(mesh)  # B keeps its draft on top, mirrors seq 2 under it
    assert (await _seq(b, pid), await _pending(b, pid)) == (2, 1)
    await _move_host(mesh, b)
    await b.app[page_proposal_forwarder_key].flush(resend=True)
    await _pump(mesh)
    states = await _states(mesh, pid)
    assert len(set(states.values())) == 1, states
    content, sides, seq, pending = next(iter(states.values()))
    assert content == _with(BASE, p1="bert", p5="host v2")
    assert (sides, seq, pending) == ((), 3, None)


# ── A restored host learns the members' seq ─────────────────────────────


async def _restore_host(h: House, pid: str, *, seq: int, content: str) -> None:
    await h.db.enqueue(
        "UPDATE space_pages SET seq=?, content=? WHERE id=?", (seq, content, pid)
    )


async def test_a_restored_hosts_next_own_edit_reaches_members(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    for i in range(4):
        await _edit(h, pid, _with(BASE, p1=f"v{i}"))
    await _pump(mesh)
    assert await _seq(a, pid) == 5
    await _restore_host(h, pid, seq=2, content=_with(BASE, p1="v0"))
    # A member's sync chunk carries its seq: the host raises its floor,
    # and takes none of its content.
    await _chunk(a, h, "pages")
    assert await _seq(h, pid) == 5
    assert await _content(h, pid) == _with(BASE, p1="v0")
    await _edit(h, pid, _with(BASE, p1="after restore"))
    await _pump(mesh)
    for house in (a, b, h):
        assert await _content(house, pid) == _with(BASE, p1="after restore")
        assert await _seq(house, pid) == 6


async def test_a_seq_hint_is_bounded_and_never_creates_a_page(mesh, monkeypatch):
    monkeypatch.setattr(page_conflict_service, "MAX_FLOOR_STEP", 10)
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    records = await PagesExporter(a.app[page_repo_key]).list_records(SID)
    (record,) = records
    await _chunk(a, h, "pages", [{**record, "seq": 1 + 11}])
    assert await _seq(h, pid) == 1  # beyond one floor step: moves nothing
    await _chunk(a, h, "pages", [{**record, "seq": 1 + 10}])
    assert await _seq(h, pid) == 11
    await _chunk(a, h, "pages", [{**record, "seq": 3}])
    assert await _seq(h, pid) == 11  # only ever raised
    await _chunk(a, h, "pages", [{**record, "id": "nope", "seq": 5}])
    assert await _content(h, "nope") is None
    # A tombstoned page takes no hint.
    await _delete(h, pid)
    await _chunk(a, h, "pages", [{**record, "seq": 15}])
    assert await _seq(h, pid) == 11


async def test_a_member_ignores_a_seq_hint(mesh):
    a, b = mesh["A"], mesh["B"]
    pid = await _create(mesh)
    records = await PagesExporter(a.app[page_repo_key]).list_records(SID)
    await _chunk(a, b, "pages", [{**records[0], "seq": 7}])
    assert await _seq(b, pid) == 1
    assert (await _page(b, pid))["seq"] == 1
