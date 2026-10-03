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


async def _confirmed(house: House, pid: str) -> bool:
    """A tombstone the space's host confirmed (final)."""
    row = await house.db.fetchone(
        "SELECT delete_confirmed FROM space_pages WHERE id=? AND space_id=?",
        (pid, SID),
    )
    return row is not None and bool(row["delete_confirmed"])


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
    assert await _content(h, pid) == BASE and await _content(a, pid) == BASE
    # B's tombstone stream reaches the host (live delete rules); the host
    # decides, tombstones and re-broadcasts its delete to every member.
    await _chunk(b, h, "pages_deleted")
    assert await _deleted(h, pid)
    await _pump(mesh)
    for house in (a, b, mesh["C"]):
        assert await _deleted(house, pid), house.name
        assert await _confirmed(house, pid), house.name


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


def _host_version(pid: str, h: House, *, seq: int, content: str) -> dict:
    return {
        "id": pid,
        "page_id": pid,
        "space_id": SID,
        "title": "Rules",
        "content": content,
        "created_by": h.user_id,
        "seq": seq,
        "conflict": [],
    }


async def test_a_host_confirmed_delete_is_never_revived(mesh):
    """A tombstone the host confirmed (its own delete, or its re-broadcast
    of a member's) is final: no host version brings it back."""
    b, h = mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _delete(b, pid)
    await _pump(mesh)  # H accepts B's delete and re-broadcasts it
    assert await _confirmed(b, pid)
    await _receive(
        b,
        _event(
            h,
            FET.SPACE_PAGE_CREATED,
            _host_version(pid, h, seq=9, content="back again"),
            b,
        ),
    )
    assert await _content(b, pid) is None and await _deleted(b, pid)


async def test_a_member_tombstone_the_host_refused_yields_to_the_host(mesh):
    """Deletes are not sequenced: households judge a member's delete by
    their own view of seats and levels, and may disagree. Only the host's
    view is final — a member tombstone the host refused yields to the
    host's next version (live) or its sync record."""
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    # An admin-only level has reached H but not yet A or B.
    await h.db.enqueue("UPDATE spaces SET pages_access='admin_only' WHERE id=?", (SID,))
    await _delete(a, pid)
    await _pump(mesh)
    assert await _content(h, pid) == BASE  # the host refused it
    assert await _deleted(a, pid) and await _deleted(b, pid)
    assert not await _confirmed(b, pid)
    await _edit(h, pid, _with(BASE, p1="hanna keeps editing"))
    await _pump(mesh)
    for house in (a, b, mesh["C"]):
        assert await _content(house, pid) == _with(BASE, p1="hanna keeps editing"), (
            house.name
        )
        assert await _seq(house, pid) == 2, house.name


async def test_a_refused_member_tombstone_yields_to_a_host_sync_record(mesh):
    b, h = mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await h.db.enqueue("UPDATE spaces SET pages_access='admin_only' WHERE id=?", (SID,))
    await _delete(b, pid)
    await _pump(mesh)
    assert await _deleted(b, pid) and await _content(h, pid) == BASE
    # The host still holds the page at the very seq B deleted: it wins.
    await _chunk(h, b, "pages")
    assert await _content(b, pid) == BASE and await _seq(b, pid) == 1
    assert (await _page(b, pid))["title"] == "Rules"


async def test_a_member_tombstone_never_yields_to_an_older_host_version(mesh):
    a, b, h = mesh["A"], mesh["B"], mesh["H"]
    pid = await _create(mesh)
    await _edit(h, pid, _with(BASE, p1="v2"))
    await _pump(mesh)
    await h.db.enqueue("UPDATE spaces SET pages_access='admin_only' WHERE id=?", (SID,))
    await _delete(b, pid)
    await _pump(mesh)
    await _receive(
        b,
        _event(
            h, FET.SPACE_PAGE_UPDATED, _host_version(pid, h, seq=1, content=BASE), b
        ),
    )
    assert await _deleted(b, pid) and await _content(b, pid) is None
    # … nor to another member's copy.
    await _chunk(a, b, "pages")
    assert await _deleted(b, pid)


async def test_an_accepted_member_delete_is_rebroadcast_by_the_host(mesh):
    a, b, c, h = mesh["A"], mesh["B"], mesh["C"], mesh["H"]
    pid = await _create(mesh)
    await _delete(b, pid)
    _take(b, c)  # C never hears B's own delete …
    await _pump(mesh)
    # … but the host's re-broadcast reaches it, confirmed everywhere.
    for house in (a, b, c):
        assert await _deleted(house, pid), house.name
        assert await _confirmed(house, pid), house.name
    assert await _deleted(h, pid)


async def test_a_host_create_never_ghosts_a_tombstoned_id(mesh):
    from dataclasses import replace

    from socialhome.app_keys import page_conflict_service_key
    from socialhome.repositories.page_repo import PageNotFoundError, new_page

    h = mesh["H"]
    page = new_page(title="T", content="x", created_by=h.user_id, space_id=SID)
    await h.app[page_repo_key].tombstone(page.id, space_id=SID, created_by=h.user_id)
    _drop(*mesh.values())
    with pytest.raises(PageNotFoundError):
        await h.app[page_conflict_service_key].host_create(
            replace(page), actor_user_id=h.user_id
        )
    assert [e for _t, e, _p in h.sent if e is FET.SPACE_PAGE_CREATED] == []
    assert await _content(h, page.id) is None


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
    a, b, c, h = mesh["A"], mesh["B"], mesh["C"], mesh["H"]
    pid = await _create(mesh)
    records = await PagesExporter(a.app[page_repo_key]).list_records(SID)
    (record,) = records
    await _chunk(a, h, "pages", [{**record, "seq": 1 + 11}])
    assert await _seq(h, pid) == 1  # beyond one floor step: moves nothing
    await _chunk(b, h, "pages", [{**record, "seq": 3}, {**record, "seq": 1 + 10}])
    assert await _seq(h, pid) == 11  # the chunk's highest seq, once
    await _chunk(c, h, "pages", [{**record, "seq": 3}])
    assert await _seq(h, pid) == 11  # only ever raised
    await _chunk(c, h, "pages", [{**record, "id": "nope", "seq": 5}])
    assert await _content(h, "nope") is None
    # A tombstoned page takes no hint.
    await _delete(h, pid)
    await _chunk(c, h, "pages", [{**record, "seq": 15}])
    assert await _seq(h, pid) == 11


async def test_one_chunk_cannot_ratchet_the_seq(mesh):
    """Every record of a chunk used to step the floor: one 2000-record
    chunk took the seq from 1 to ~2**31. Now the chunk's highest seq per
    page counts, once per page and provider per sync session."""
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    step = page_conflict_service.MAX_FLOOR_STEP
    recs = [
        {
            "id": pid,
            "title": "Rules",
            "content": "x",
            "created_by": h.user_id,
            "seq": 1 + step * i,
        }
        for i in range(1, 2001)
    ]
    await _chunk(a, h, "pages", recs)
    assert await _seq(h, pid) == 1  # the highest claim is beyond one step
    await _chunk(a, h, "pages", [{**recs[0], "seq": 1 + step}])
    assert await _seq(h, pid) == 1  # once per page per session
    await _chunk(mesh["B"], h, "pages", [{**recs[0], "seq": 1 + step}])
    assert await _seq(h, pid) == 1 + step
    # The day's budget for the page is spent: no further provider moves it.
    await _chunk(mesh["C"], h, "pages", [{**recs[0], "seq": 1 + 2 * step}])
    assert await _seq(h, pid) == 1 + step


async def test_a_seq_hint_needs_a_writer_household(mesh):
    a, h = mesh["A"], mesh["H"]
    pid = await _create(mesh)
    records = await PagesExporter(a.app[page_repo_key]).list_records(SID)
    await h.db.enqueue(
        "UPDATE space_remote_members SET role='subscriber' WHERE instance_id=?",
        (a.iid,),
    )
    await _chunk(a, h, "pages", [{**records[0], "seq": 5}])
    assert await _seq(h, pid) == 1


async def test_seq_hints_are_rate_limited_per_provider(mesh, monkeypatch):
    monkeypatch.setattr(page_conflict_service, "FLOOR_RAISES_PER_PROVIDER_PER_HOUR", 2)
    a, h = mesh["A"], mesh["H"]
    pids = [await _create(mesh) for _ in range(3)]
    records = await PagesExporter(a.app[page_repo_key]).list_records(SID)
    await _chunk(a, h, "pages", [{**r, "seq": 4} for r in records])
    assert sorted([await _seq(h, p) for p in pids]) == [1, 4, 4]


async def test_a_member_ignores_a_seq_hint(mesh):
    a, b = mesh["A"], mesh["B"]
    pid = await _create(mesh)
    records = await PagesExporter(a.app[page_repo_key]).list_records(SID)
    await _chunk(a, b, "pages", [{**records[0], "seq": 7}])
    assert await _seq(b, pid) == 1
    assert (await _page(b, pid))["seq"] == 1
