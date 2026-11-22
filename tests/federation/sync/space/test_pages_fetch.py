"""Sync pages Phase 3 between three real households
(``docs/protocol/sync-pages.md``, "Phase 3 as built"): the host seals a page,
goes silent, and a member that is behind catches up the host's content from
another plain member household — every row checked against the signed page,
none trusted because of who delivered it.

Built on the §25.6 coverage harness (real SQLite households, real exporters,
the real receiver with its authorship rules). Only the content-key AEAD is a
stand-in; the "wire" hands the requester's fetch to the deliverer's real
:meth:`SyncPageService.fetch_records` and its answer back, through JSON.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import orjson
import pytest

from socialhome.canonical_json import canonical_str
from socialhome.crypto import ed25519_public_key
from socialhome.domain.post import Post, PostType
from socialhome.domain.space_sync_page import StoredSyncPage, state_with_page
from socialhome.domain.space_sync_status import SpaceSyncState
from socialhome.domain.task import TaskStatus
from socialhome.federation.sync.space.pages import (
    FETCH_ANSWER_WAIT_S,
    MAX_FETCH_ASKS,
    SyncPageService,
)
from socialhome.sync_page import (
    MAX_FETCH_ROWS,
    PAGE_MIN_INTERVAL_S,
    hint_from_wire,
    parse_page_ref,
    row_key,
    sign_page,
)
from tests.federation.sync.space.test_pages_households import (
    MEMBER,
    SEED,
    _Clock,
    _round,
    _service,
)
from tests.federation.sync.space.test_sync_coverage import (
    ANNA,
    BOB,
    CARL,
    HOST,
    PAGE,
    SPACE,
    TASK,
    Home,
    _home,
    _page,
    _post,
    _sticky,
    _stream,
    _task,
    _task_done,
    homes,  # noqa: F401 — the fixture
)

PK_HEX = ed25519_public_key(SEED).hex()
T0_DT = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
REQUESTER = "requester-household"
FOLLOWER = "follower-household"
OTHER = "other-member-household"


class _Versions:
    def __init__(self) -> None:
        self.version: dict[str, int] = {}

    async def space_member_supports(self, instance_id, *, min_version):
        return self.version.get(instance_id, 95) >= min_version


class _Wire:
    """The requester's fetches, answered by the deliverers' real services."""

    def __init__(self, requester: SyncPageService) -> None:
        self.requester = requester
        self.deliverers: dict[str, SyncPageService] = {}
        self.silent = True
        self.asks: list[tuple[str, list[int], str | None]] = []
        self.asked: dict[str, dict | None] = {}
        self.ref: dict | None = None
        self.tasks: list[asyncio.Task] = []
        #: Tamper with an answer: ``(listings, rows) -> (listings, rows)``.
        self.forge: Callable[[list, list], tuple[list, list]] | None = None

    async def hosts(self, space) -> list[str]:
        return [HOST]

    def asked_page(self, sync_id: str):
        """The requester's pending-request record: the page a BEGIN asked
        about (``attach_asks``)."""
        return self.asked.get(sync_id)

    async def host_round_buckets(self) -> list[int] | None:
        """What the next incremental BEGIN to the host would list."""
        ref = await self.requester.ref_for(SPACE)
        return self.requester.buckets_for(SPACE, HOST, ref)

    async def request(self, space_id: str, deliverer: str) -> None:
        """The scheduler's ``sync_with``: one incremental BEGIN to that
        member, carrying ``buckets`` / ``rows`` / ``have`` as
        ``_page_fields_for`` builds them."""
        ref = await self.requester.ref_for(space_id)
        self.ref = ref
        buckets = self.requester.buckets_for(space_id, deliverer, ref)
        have = self.requester.have_for(space_id, deliverer, ref)
        self.asks.append((deliverer, list(buckets or []), have))
        if deliverer not in self.deliverers or not buckets or have is None:
            return  # unreachable (or nothing to ask)
        sync_id = f"F{len(self.asks)}"
        self.asked[sync_id] = ref
        self.tasks.append(
            asyncio.create_task(self._answer(sync_id, deliverer, ref, buckets, have))
        )

    async def _answer(self, sync_id, deliverer, page_ref, buckets, have) -> None:
        listings, rows = await self.deliverers[deliverer].fetch_records(
            space_id=SPACE,
            page_ref=parse_page_ref(page_ref),
            buckets=buckets,
            requester_version=95,
            have=hint_from_wire(have),
            max_record_bytes=None,
        )
        listings, rows = orjson.loads(orjson.dumps([listings, rows]))
        if self.forge is not None:
            listings, rows = self.forge(listings, rows)
        await self.requester.take_listing(
            space_id=SPACE, sync_id=sync_id, provider=deliverer, records=listings
        )
        await self.requester.take_rows(
            space_id=SPACE, sync_id=sync_id, provider=deliverer, records=rows
        )
        await self.requester.listing_done(
            space_id=SPACE, sync_id=sync_id, provider=deliverer
        )

    async def settle(self) -> None:
        for _ in range(20):
            while self.tasks:
                await self.tasks.pop(0)
            await self.requester.wait_idle()
            if not self.tasks:
                return


async def _seat(home: Home, instance_id: str, user_id: str, role: str) -> None:
    await home.db.enqueue(
        "INSERT OR IGNORE INTO space_remote_members(space_id, instance_id,"
        " user_id, role) VALUES(?,?,?,?)",
        (SPACE, instance_id, user_id, role),
    )


@pytest.fixture
async def trio(homes, tmp_dir):  # noqa: F811
    """host (owner, signer), a plain member household (deliverer) and a
    member household that missed content (requester)."""
    host, member = homes
    requester = await _home(tmp_dir / "requester.db", CARL)
    for home in (host, member, requester):
        await home.db.enqueue(
            "UPDATE spaces SET identity_public_key=? WHERE id=?",
            (PK_HEX, SPACE),
        )
    clock = _Clock()
    host_pages = _service(host, HOST, SEED, clock)
    member_pages = _service(member, MEMBER, None, clock)
    r_pages = _service(requester, REQUESTER, None, clock)
    wire = _Wire(r_pages)
    wire.deliverers[MEMBER] = member_pages
    versions = _Versions()
    # The requester's roster mirror: the host's person, the deliverer's
    # (a plain member) and a follower-only household.
    await _seat(requester, HOST, ANNA, "owner")
    await _seat(requester, MEMBER, BOB, "member")
    await _seat(requester, FOLLOWER, "u-fiona", "subscriber")
    r_pages.attach_hosts(wire.hosts)
    r_pages.attach_asks(wire.asked_page)
    r_pages.attach_member_fetch(
        seats=requester.repos.space_remote_member,
        versions=versions,
        silent=lambda space_id: wire.silent,
        request=wire.request,
        apply=requester.receiver.apply_verified,
    )
    yield host, member, requester, host_pages, member_pages, r_pages, wire, versions
    await requester.db.shutdown()


async def _missed_content(host: Home, member: Home, requester: Home) -> None:
    """The requester held the space once; then the host posted again and
    wrote a wiki page, which the plain member got and the requester did not."""
    await _post(host)
    await _stream(host, requester)
    await _post(host, "post-2", content="while you were away")
    await _page(host)
    await _stream(host, member)


async def _position(home: Home, pages: SyncPageService):
    space = await home.repos.space.get(SPACE)
    return await pages.position(space, [HOST])


async def test_the_proofs_and_answers_never_block_the_event_loop(trio, monkeypatch):
    """Review 6: the leaf hashing of an answer (the requester's row checks,
    the deliverer's proofs and sizing, Phase 2's host listings) runs in
    worker threads (CLAUDE.md "CPU-bound work"); the DB reads stay async."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    page = await host_pages.seal_if_due(SPACE)
    assert page is not None
    assert await _round(host_pages, member_pages, "s-m")
    ran: list[str] = []
    real = asyncio.to_thread

    async def recording(fn, *args, **kwargs):
        ran.append(getattr(fn, "__name__", repr(fn)))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording)
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    assert await requester.repos.space_post.get("post-2") is not None
    await host_pages.listing_records(
        space_id=SPACE,
        page_ref=parse_page_ref(page.ref()),
        buckets=[row_key("posts", "post-2")[0]],
        requester_version=95,
    )
    assert {"_verify_rows", "_fetch_answer", "_host_listings"} <= set(ran)


async def test_a_member_catches_up_the_hosts_posts_and_pages_from_a_plain_member(
    trio, caplog
):
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    page = await host_pages.seal_if_due(SPACE)
    assert page is not None
    assert await _round(host_pages, member_pages, "s-m")  # the member holds it
    # The host (no delegated admin in this space) goes offline; the
    # requester gets the page relayed and compares.
    with caplog.at_level(logging.INFO):
        assert await _round(host_pages, r_pages, "s-r")
        await wire.settle()
    assert await wire.host_round_buckets() is None  # the silent host is never asked
    assert [a[0] for a in wire.asks] == [MEMBER]  # a plain member, not the host
    got = await requester.repos.space_post.get("post-2")
    assert got is not None and got[1].content == "while you were away"
    held = await requester.repos.page.get_space_page(PAGE, space_id=SPACE)
    assert held is not None and held.content == "Be kind"
    pos = await _position(requester, r_pages)
    assert pos is not None and (pos.match, pos.missing) == (True, 0)
    assert (
        state_with_page(SpaceSyncState.HOST_OFFLINE, pos) is SpaceSyncState.UP_TO_DATE
    )
    assert "verified catch-up of space" in caplog.text
    # Our copy now hashes to the page: a fresh compare finds nothing.
    vec, _rows = await r_pages._state.vector(SPACE, cutoff=page.cutoff)
    assert vec == (await requester.repos.space_sync_page.get(SPACE)).vector


async def test_a_tampered_row_is_refused(trio, caplog):
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")

    def tamper(listings, rows):
        for r in rows:
            if r["r"] == "posts":
                r["row"]["content"] = "forged words"
        return listings, rows

    wire.forge = tamper
    with caplog.at_level(logging.WARNING):
        assert await _round(host_pages, r_pages, "s-r")
        await wire.settle()
    got = await requester.repos.space_post.get("post-2")
    assert got is None  # never applied
    assert "refused 1 row(s)" in caplog.text
    pos = await _position(requester, r_pages)
    assert pos is not None and pos.match is False and pos.missing == 1
    # The rest of the answer (the page) was proven and kept.
    assert await requester.repos.page.get_space_page(PAGE, space_id=SPACE)


async def test_a_forged_listing_refuses_the_whole_answer(trio, caplog):
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")

    def forge(listings, rows):
        for listing in listings:
            if listing.get("rows"):
                listing["rows"] = listing["rows"][:-1]  # truncated
        return listings, rows

    wire.forge = forge
    with caplog.at_level(logging.WARNING):
        assert await _round(host_pages, r_pages, "s-r")
        await wire.settle()
    assert await requester.repos.space_post.get("post-2") is None
    assert await requester.repos.page.get_space_page(PAGE, space_id=SPACE) is None
    assert "refused the answer of" in caplog.text
    pos = await _position(requester, r_pages)
    assert pos is not None and pos.match is None  # unresolved, not proven


async def test_a_deliverer_reports_not_held_for_a_bucket_changed_since_the_page(trio):
    host, member, requester, host_pages, member_pages, _r_pages, _wire, _v = trio
    await _missed_content(host, member, requester)
    page = await host_pages.seal_if_due(SPACE)
    assert page is not None
    assert await _round(host_pages, member_pages, "s-m")
    await member.repos.space_post.edit(
        "post-2", "edited after the page", space_id=SPACE, edited_at="2030-01-01"
    )
    index = row_key("posts", "post-2")[0]
    listings, rows = await member_pages.fetch_records(
        space_id=SPACE,
        page_ref=parse_page_ref(page.ref()),
        buckets=[index],
        requester_version=95,
        have=frozenset(),
        max_record_bytes=None,
    )
    assert listings == [{"i": index, "held": False}]
    assert rows == []  # never a row it cannot prove


@pytest.fixture
async def partial(trio, tmp_dir):
    """A second plain member household with the same content, which edited
    post-2 after the page (it cannot prove that bucket). The stable
    rotation offset of the requester starts on it."""
    host, member, requester, host_pages, member_pages, _r_pages, wire, _versions = trio
    await _missed_content(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")
    other = await _home(tmp_dir / "other.db", "u-olga")
    await other.db.enqueue(
        "UPDATE spaces SET identity_public_key=? WHERE id=?", (PK_HEX, SPACE)
    )
    await _stream(host, other)
    other_pages = _service(other, OTHER, None, _Clock())
    assert await _round(host_pages, other_pages, "s-o")
    await other.repos.space_post.edit(
        "post-2", "edited after the page", space_id=SPACE, edited_at="2030-01-01"
    )
    await _seat(requester, OTHER, "u-olga", "member")
    wire.deliverers[OTHER] = other_pages
    yield trio
    await other.db.shutdown()


async def test_a_partial_answer_then_the_next_member(partial):
    """The first member asked cannot prove one bucket; the round goes on to
    the next member, which can — and the page bucket is not asked twice."""
    _h, _m, requester, host_pages, _mp, r_pages, wire, _v = partial
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    assert [a[0] for a in wire.asks] == [OTHER, MEMBER]
    assert len(wire.asks[1][1]) == 1  # only the bucket still to fetch
    got = await requester.repos.space_post.get("post-2")
    assert got is not None and got[1].content == "while you were away"
    pos = await _position(requester, r_pages)
    assert pos is not None and pos.match is True


async def test_an_answer_without_listings_goes_on_to_the_next_member(partial):
    """Review 4: the member our fetch rode answered with no listing at all
    (no page held, or not ours). It is an answer all the same: the ask is
    not left in flight — the round goes on to its next member at once."""
    _h, _m, requester, host_pages, _mp, r_pages, wire, _v = partial
    answers: list[int] = []

    def first_empty(listings, rows):
        answers.append(1)
        return ([], []) if len(answers) == 1 else (listings, rows)

    wire.forge = first_empty
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    assert [a[0] for a in wire.asks] == [OTHER, MEMBER]
    got = await requester.repos.space_post.get("post-2")
    assert got is not None and got[1].content == "while you were away"


async def test_a_newer_page_during_an_ask_starts_its_own_round(trio):
    """Review 3: a compare (a newer page) replaces the ask in flight — whose
    answer would now be dropped — so it must not wait for it: its own round
    asks at once."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    del wire.deliverers[MEMBER]  # the first ask goes unanswered
    assert await _round(host_pages, member_pages, "s-m")
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    assert len(wire.asks) == 1
    host_pages._clock_ms.ms += (PAGE_MIN_INTERVAL_S + 60) * 1000
    await _post(host, "post-3", content="later")
    await _stream(host, member)
    assert await host_pages.seal_if_due(SPACE) is not None
    assert await _round(host_pages, r_pages, "s-r2")
    await wire.settle()
    assert len(wire.asks) == 2


async def test_a_host_heard_mid_round_stops_the_member_asks(partial):
    _h, _m, requester, host_pages, _mp, r_pages, wire, _v = partial

    def host_back(listings, rows):
        wire.silent = False  # the host answers while this answer lands
        return listings, rows

    wire.forge = host_back
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    assert [a[0] for a in wire.asks] == [OTHER]  # no second member asked
    assert await requester.repos.space_post.get("post-2") is None


async def test_no_member_is_asked_while_a_host_answers(trio):
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")
    wire.silent = False
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    assert wire.asks == []  # members are never asked
    assert await wire.host_round_buckets()  # the next round to the host lists them
    r_pages.host_silent(SPACE)  # a stale call: the host answers now
    await wire.settle()
    assert wire.asks == []


async def test_the_scheduler_silence_starts_a_member_fetch(trio):
    """The page arrived while the host still answered (its listing could not
    go out); the scheduler then finds the host silent → members."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")
    wire.silent = False
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    assert wire.asks == []
    wire.silent = True
    r_pages.host_silent(SPACE)
    await wire.settle()
    assert [a[0] for a in wire.asks] == [MEMBER]
    assert (await _position(requester, r_pages)).match is True


async def test_followers_and_older_members_are_never_asked(trio):
    host, member, requester, host_pages, member_pages, r_pages, wire, versions = trio
    await _missed_content(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")
    versions.version[MEMBER] = 94  # an older member household degrades
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    assert wire.asks == []  # the follower is never asked either
    assert await requester.repos.space_post.get("post-2") is None


@pytest.mark.parametrize(
    ("edited_at", "verdict"),
    [
        ("2027-01-15 08:02:00", "1 bucket(s) complete"),
        ("2030-01-01 00:00:00", "1 still to fetch"),
    ],
)
async def test_unsigned_change_times_cannot_roll_back_a_newer_local_edit(
    trio, caplog, edited_at, verdict
):
    """A listing's change times are not signed. A deliverer that claims a
    far-future one cannot make the page's (older) version of a row replace
    our edit made after the page: past the page's signed ``issued_at`` our
    row is ahead, whatever the listing says — up to the horizon (now plus the
    page skew): a far-future stamp of ours never counts as newer (review 5),
    so the page's version is handed to the receiver — whose merge rules
    still keep the newer words of a post: nothing landed, so the bucket is
    still to fetch (judged on our rows after the apply, review N1)."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _post(host)
    await _stream(host, requester)
    await host.repos.space_post.edit(
        "post-1", "the host's edit", space_id=SPACE, edited_at="2026-11-02 10:00:00"
    )
    await _stream(host, member)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")
    wire.silent = False
    assert await _round(host_pages, r_pages, "s-r")  # compare: post-1 differs
    await wire.settle()
    await requester.repos.space_post.edit(
        "post-1", "my newer edit", space_id=SPACE, edited_at=edited_at
    )

    def inflate(listings, rows):
        for listing in listings:
            for row in listing.get("rows") or []:
                row[3] = "2099-01-01T00:00:00.000000+00:00"
        return listings, rows

    wire.forge = inflate
    wire.silent = True
    with caplog.at_level(logging.INFO):
        r_pages.host_silent(SPACE)
        await wire.settle()
    assert [a[0] for a in wire.asks] == [MEMBER]
    assert "0 row(s) applied" in caplog.text
    assert verdict in caplog.text
    got = await requester.repos.space_post.get("post-1")
    assert got is not None and got[1].content == "my newer edit"


async def test_one_ask_in_flight_and_each_round_asks_the_next_member(trio):
    """An ask rides an ordinary BEGIN, so an unanswered one holds the round:
    nothing more until it is given up (FETCH_ANSWER_WAIT_S); each later
    silent round then asks the next member in the stable rotation."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    for k in range(5):  # unreachable member households
        await _seat(requester, f"gone-{k}", f"u-gone-{k}", "member")
    del wire.deliverers[MEMBER]  # nobody answers
    assert await _round(host_pages, member_pages, "s-m")
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    assert len(wire.asks) == 1
    r_pages.host_silent(SPACE)  # the ask is still in flight
    await wire.settle()
    assert len(wire.asks) == 1
    for _ in range(3):
        r_pages._fetching[SPACE] -= FETCH_ANSWER_WAIT_S + 1  # given up
        r_pages.host_silent(SPACE)
        await wire.settle()
    asked = [a[0] for a in wire.asks]
    assert len(asked) == 4 and len(set(asked)) == 4
    assert MAX_FETCH_ASKS == 3


async def test_a_members_labels_and_markers_play_no_part(trio, caplog):
    """A listing's labels (resource, row id) and change times are not signed
    (Phase 2 review B4). A member that relabels the row we lack and drops
    every change time still gets only what the leaves prove: the row comes
    in under its own identity (the leaf commits to it), nothing else."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")
    target = row_key("posts", "post-2")[0]
    alias = next(
        f"x{k}" for k in range(100_000) if row_key("posts", f"x{k}")[0] == target
    )

    def relabel(listings, rows):
        for listing in listings:
            for row in listing.get("rows") or []:
                row[3] = None
                if row[1] == "post-2":
                    row[1] = alias
        return listings, rows

    wire.forge = relabel
    with caplog.at_level(logging.WARNING):
        assert await _round(host_pages, r_pages, "s-r")
        await wire.settle()
    got = await requester.repos.space_post.get("post-2")
    assert got is not None and got[1].content == "while you were away"
    assert await requester.repos.space_post.get(alias) is None
    assert "refused" not in caplog.text
    assert (await _position(requester, r_pages)).match is True


async def test_a_members_current_listing_proves_nothing(trio):
    """A host may list a bucket it changed since the page as it is now
    (``current``, trusted like its stream). From a member delivering a
    verified fetch such a listing is unproven: the bucket counts as not
    held and none of its rows is applied."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")

    def as_current(listings, rows):
        for listing in listings:
            if listing.get("held"):
                listing["held"] = False
                listing["current"] = True
        return listings, rows

    wire.forge = as_current
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    assert await requester.repos.space_post.get("post-2") is None
    assert await requester.repos.page.get_space_page(PAGE, space_id=SPACE) is None
    assert (await _position(requester, r_pages)).match is None


async def test_a_feed_card_is_never_taken_from_a_member(trio, caplog):
    """A post linked to a calendar event (the event's feed card — each
    household mints its own, the host's comes from the host alone) is left
    out quietly: its link is not committed, so it is not proven."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")

    def as_card(listings, rows):
        for r in rows:
            if r["row"].get("id") == "post-2":
                r["row"]["linked_event_id"] = "ev-1"
        return listings, rows

    wire.forge = as_card
    with caplog.at_level(logging.WARNING):
        assert await _round(host_pages, r_pages, "s-r")
        await wire.settle()
    assert await requester.repos.space_post.get("post-2") is None
    assert "refused" not in caplog.text
    assert await requester.repos.page.get_space_page(PAGE, space_id=SPACE)


async def test_the_owner_back_from_a_long_absence_catches_up_from_members(trio):
    """The owner household was offline; its delegated admin (a member
    household holding the seed) sealed a newer page that commits a post
    whose author's household is offline now. Back, the owner takes that
    page over its own older one, compares, and fetches the post from a
    member household, verified — nothing in the requester path assumes the
    requester is not the host."""
    host, member, _requester, host_pages, member_pages, _r, _w, versions = trio
    await _post(host)
    await _stream(host, member)
    own = await host_pages.seal_if_due(SPACE)
    assert own is not None and own.signer == HOST
    assert await _round(host_pages, member_pages, "s-m")
    # While the owner is away: a third household's post reaches the admin
    # household; the admin seals a page committing it.
    await _post(member, "post-carl", author=CARL, content="written while away")
    space = await member.repos.space.get(SPACE)
    vector, rows = await member_pages._state.vector(SPACE, cutoff=own.cutoff)
    admin_page = sign_page(
        space_id=SPACE,
        epoch=int(space.authority_key_epoch or 0),
        signer=MEMBER,
        seq=own.seq + 10,
        issued_at=own.issued_at + 60_000,
        cutoff=own.cutoff,
        rows=rows,
        vector=vector,
        prev_root=None,
        space_seed=SEED,
    )
    await member.repos.space_sync_page.put(
        StoredSyncPage(
            space_id=SPACE,
            page_json=canonical_str(admin_page.to_wire()),
            vector=vector,
        )
    )
    # The owner's requester wiring (the app wires it the same everywhere).
    wire = _Wire(host_pages)
    wire.deliverers[MEMBER] = member_pages
    wire.silent = False  # no host above the owner to be silent
    await _seat(host, MEMBER, BOB, "admin")
    host_pages.attach_hosts(wire.hosts)
    host_pages.attach_asks(wire.asked_page)
    host_pages.attach_member_fetch(
        seats=host.repos.space_remote_member,
        versions=versions,
        silent=lambda space_id: False,
        request=wire.request,
        apply=host.receiver.apply_verified,
    )
    # The admin's sentinel relays its page to the owner (any provider does).
    sealed = await member_pages.sentinel_page(
        space_id=SPACE,
        sync_id="s-o",
        requester_version=95,
        requester_ref=parse_page_ref(await host_pages.ref_for(SPACE)),
    )
    assert await host_pages.receive(
        space_id=SPACE, sync_id="s-o", provider=MEMBER, sealed=sealed
    )
    await wire.settle()
    assert await wire.host_round_buckets() is None  # never asks itself as "the host"
    assert [a[0] for a in wire.asks] == [MEMBER]
    got = await host.repos.space_post.get("post-carl")
    assert got is not None and got[1].content == "written while away"
    pos = await _position(host, host_pages)
    assert pos is not None and pos.match is True and pos.signer_role == "admin"


async def _deleted_alias_in_post2s_bucket(requester: Home) -> str:
    """A post id in post-2's bucket that the requester holds as DELETED."""
    target = row_key("posts", "post-2")[0]
    alias = next(
        f"gone{k}" for k in range(100_000) if row_key("posts", f"gone{k}")[0] == target
    )
    await _post(requester, alias, content="deleted here")
    await requester.repos.space_post.soft_delete(alias, space_id=SPACE)
    return alias


@pytest.mark.parametrize("forged", [True, False])
async def test_a_withheld_row_relabelled_as_a_deleted_one_is_still_missing(
    trio, forged
):
    """Security review 1 (the PoC): the deliverer withholds the row we lack
    and relabels its leaf in the (verified) listing with the id of a row we
    hold as deleted in the same bucket. Labels are not signed: completeness
    is judged by leaves only, so the bucket stays incomplete — never a false
    "up to date". The control: an honest deliverer completes it."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    alias = await _deleted_alias_in_post2s_bucket(requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")

    def withhold_and_relabel(listings, rows):
        for listing in listings:
            for row in listing.get("rows") or []:
                if row[1] == "post-2":
                    row[1] = alias
        rows = [r for r in rows if r["row"].get("id") != "post-2"]
        return listings, rows

    if forged:
        wire.forge = withhold_and_relabel
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    pos = await _position(requester, r_pages)
    got = await requester.repos.space_post.get("post-2")
    if forged:
        assert got is None
        assert pos is not None and pos.match is not True
    else:
        assert got is not None
        assert pos is not None and pos.match is True


async def _admin_page_reaches_owner(
    host, member, host_pages, member_pages, versions, own
) -> _Wire:
    """The delegated admin (the member household holding the seed) seals a
    page from its own copy; its sentinel relays it to the returning owner,
    whose requester path is wired like the app's; the round settles."""
    space = await member.repos.space.get(SPACE)
    vector, rows = await member_pages._state.vector(SPACE, cutoff=own.cutoff)
    admin_page = sign_page(
        space_id=SPACE,
        epoch=int(space.authority_key_epoch or 0),
        signer=MEMBER,
        seq=own.seq + 10,
        issued_at=own.issued_at + 60_000,
        cutoff=own.cutoff,
        rows=rows,
        vector=vector,
        prev_root=None,
        space_seed=SEED,
    )
    await member.repos.space_sync_page.put(
        StoredSyncPage(
            space_id=SPACE,
            page_json=canonical_str(admin_page.to_wire()),
            vector=vector,
        )
    )
    wire = _Wire(host_pages)
    wire.deliverers[MEMBER] = member_pages
    await _seat(host, MEMBER, BOB, "admin")
    host_pages.attach_hosts(wire.hosts)
    host_pages.attach_asks(wire.asked_page)
    host_pages.attach_member_fetch(
        seats=host.repos.space_remote_member,
        versions=versions,
        silent=lambda space_id: False,
        request=wire.request,
        apply=host.receiver.apply_verified,
    )
    sealed = await member_pages.sentinel_page(
        space_id=SPACE,
        sync_id="s-o",
        requester_version=95,
        requester_ref=parse_page_ref(await host_pages.ref_for(SPACE)),
    )
    assert await host_pages.receive(
        space_id=SPACE, sync_id="s-o", provider=MEMBER, sealed=sealed
    )
    await wire.settle()
    return wire


async def test_the_owner_never_rolls_its_own_edits_back_to_an_admin_page(trio):
    """Security review 2: the owner edited a task and a sticky while it was
    offline; its delegated admin sealed a page WITHOUT those edits. Back, the
    owner applies only what it lacks (a row it has no version of) — never
    the page's older version of a row it holds, which it would then spread
    as the host. Tasks and stickies are taken whole, so this is the case
    the merge rules do not catch."""
    host, member, _requester, host_pages, member_pages, _r, _w, versions = trio
    await _task(host)
    await _sticky(host)
    await _stream(host, member)
    own = await host_pages.seal_if_due(SPACE)
    assert own is not None
    assert await _round(host_pages, member_pages, "s-m")
    await _task_done(host)  # the owner's own edits, before the admin's page
    assert await host.repos.sticky.update_content(
        "sticky-1", "oat milk", space_id=SPACE
    )
    await _post(member, "post-carl", author=CARL, content="written while away")
    wire = await _admin_page_reaches_owner(
        host, member, host_pages, member_pages, versions, own
    )
    assert [a[0] for a in wire.asks] == [MEMBER]
    task = await host.repos.space_task.get(TASK)
    assert task is not None and task[1].status is TaskStatus.DONE
    sticky = await host.repos.sticky.get("sticky-1")
    assert sticky is not None and sticky.content == "oat milk"
    assert await host.repos.space_post.get("post-carl") is not None  # it lacked it


async def test_an_answer_over_the_row_cap_is_cut(trio):
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")

    def pad(listings, rows):
        return listings, rows + [rows[0]] * (MAX_FETCH_ROWS + 10)

    wire.forge = pad
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    # The duplicates past the cap were dropped on receipt; the real rows
    # (first in the answer) still landed.
    assert await requester.repos.space_post.get("post-2") is not None


# ── Fable re-review N1–N3: what a deliverer cannot do with a verified row ──


async def _picture_missed(host: Home, member: Home, requester: Home) -> None:
    """Like :func:`_missed_content`, with a picture on the missed post."""
    await _post(host)
    await _stream(host, requester)
    await _post(
        host,
        "post-2",
        type=PostType.IMAGE,
        content="a picture",
        media_url="api/media/a.webp",
    )
    await _stream(host, member)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/t/{name}",
        "/api/media/{name}",
        "api/media/{name}?sig=x",
        "api/media/dm-{name}",
    ],
)
async def test_a_file_reference_rewritten_off_the_local_shape_is_refused(
    trio, caplog, url
):
    """Review N1 (the PoC): the leaf commits a file by its content hash, not
    by how the row names it. A deliverer that rewrites the unsigned URL (a
    full URL, or a name another receiving path owns) keeps the leaf — the
    receiver would then drop the picture and the bucket count as complete.
    Such a row is refused; the post stays missing."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _picture_missed(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")

    def rewrite(listings, rows):
        for r in rows:
            if r["row"].get("id") == "post-2":
                name = r["row"]["media_url"].rsplit("/", 1)[-1]
                r["row"]["media_url"] = url.format(name=name)
        return listings, rows

    wire.forge = rewrite
    with caplog.at_level(logging.WARNING):
        assert await _round(host_pages, r_pages, "s-r")
        await wire.settle()
    assert await requester.repos.space_post.get("post-2") is None
    assert "refused 1 row(s)" in caplog.text
    pos = await _position(requester, r_pages)
    assert pos is not None and pos.match is False and pos.missing == 1


async def test_an_honest_picture_post_lands_with_its_picture_and_completes(trio):
    """The control: the post lands under its content name, and the re-read
    of our rows (the bytes not here yet) still counts it."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _picture_missed(host, member, requester)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    got = await requester.repos.space_post.get("post-2")
    assert got is not None and got[1].media_url is not None
    assert got[1].media_url.startswith("api/media/sc-")
    pos = await _position(requester, r_pages)
    assert pos is not None and (pos.match, pos.missing) == (True, 0)


async def test_a_row_the_receiver_silently_refuses_is_still_missing(trio, caplog):
    """Review N1 (b): completeness is judged on our rows after the apply,
    not on the receiver's answer — a post id held here in ANOTHER space is
    refused without a held-back count, so it must stay missing."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _missed_content(host, member, requester)
    space = await requester.repos.space.get(SPACE)
    assert space is not None
    await requester.repos.space.save(replace(space, id="sp-elsewhere"))
    await requester.repos.space_post.save(
        "sp-elsewhere",
        Post(
            id="post-2",
            author=ANNA,
            type=PostType.TEXT,
            created_at=T0_DT,
            content="another space's post",
        ),
    )
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")
    with caplog.at_level(logging.INFO):
        assert await _round(host_pages, r_pages, "s-r")
        await wire.settle()
    held = await requester.repos.space_post.get("post-2")
    assert held is not None and held[0] == "sp-elsewhere"
    pos = await _position(requester, r_pages)
    assert pos is not None and pos.match is not True and pos.missing == 1
    assert "1 still to fetch" in caplog.text


async def _archive(h: Home, at: datetime) -> None:
    got = await h.repos.space_task.get(TASK)
    assert got is not None
    await h.repos.space_task.save(
        replace(got[1], archived_at=at, updated_at=at), space_id=SPACE
    )


async def test_the_owner_keeps_its_offline_unarchive_against_an_admin_page(trio):
    """Review N2 (the owner PoC): ``tasks:T`` and ``tasks_archived:T`` are one
    row. The owner un-archived and edited a task while away; the admin's
    page still has it archived. Holding the live counterpart is holding the
    row: the owner never rolls it back."""
    host, member, _requester, host_pages, member_pages, _r, _w, versions = trio
    await _task(host)
    await _archive(host, T0_DT + timedelta(hours=1))
    await _stream(host, member)
    own = await host_pages.seal_if_due(SPACE)
    assert own is not None
    assert await _round(host_pages, member_pages, "s-m")
    got = await host.repos.space_task.get(TASK)
    assert got is not None
    await host.repos.space_task.save(
        replace(
            got[1],
            archived_at=None,
            title="Dishes, again",
            updated_at=T0_DT + timedelta(hours=2),
        ),
        space_id=SPACE,
    )
    await _post(member, "post-carl", author=CARL, content="written while away")
    wire = await _admin_page_reaches_owner(
        host, member, host_pages, member_pages, versions, own
    )
    assert [a[0] for a in wire.asks] == [MEMBER]
    task = await host.repos.space_task.get(TASK)
    assert task is not None
    assert task[1].archived_at is None and task[1].title == "Dishes, again"
    assert await host.repos.space_post.get("post-carl") is not None


@pytest.mark.parametrize(
    ("edited", "archived"),
    [("2027-01-15 08:02:00", False), (None, True)],
)
async def test_a_member_keeps_its_live_task_edited_after_the_page(
    trio, edited, archived
):
    """Review N2 (the member PoC): the page has the task archived; we hold
    it live and edited it after the page — our row is newer, the archive is
    not applied over it. The control: an unedited live copy takes the
    page's archive."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _task(host)
    await _stream(host, requester)
    await _archive(host, T0_DT + timedelta(hours=1))
    await _stream(host, member)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")
    if edited is not None:
        got = await requester.repos.space_task.get(TASK)
        assert got is not None
        stamp = datetime.fromisoformat(edited).replace(tzinfo=UTC)
        await requester.repos.space_task.save(
            replace(got[1], title="Dishes, mine", updated_at=stamp),
            space_id=SPACE,
        )
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    assert [a[0] for a in wire.asks] == [MEMBER]
    task = await requester.repos.space_task.get(TASK)
    assert task is not None
    assert (task[1].archived_at is not None) is archived


async def test_a_verified_row_keeps_our_pin_moderation_and_reactions(trio):
    """Review N3: the leaf does not cover a post's pin, moderation mark or
    reactions, so the deliverer's are never applied — and ours must not be
    reset by the verified version either."""
    host, member, requester, host_pages, member_pages, r_pages, wire, _v = trio
    await _post(host)
    await _stream(host, requester)
    await _post(requester, pinned=True, moderated=True)
    await requester.repos.space_post.add_reaction(
        "post-1", "🎉", CARL, space_id=SPACE, stamp="2026-11-01 10:00:00"
    )
    await host.repos.space_post.edit(
        "post-1", "the host's edit", space_id=SPACE, edited_at="2026-11-02 10:00:00"
    )
    await _stream(host, member)
    await host_pages.seal_if_due(SPACE)
    assert await _round(host_pages, member_pages, "s-m")
    assert await _round(host_pages, r_pages, "s-r")
    await wire.settle()
    assert [a[0] for a in wire.asks] == [MEMBER]
    got = await requester.repos.space_post.get("post-1")
    assert got is not None
    post = got[1]
    assert post.content == "the host's edit"
    assert post.pinned is True and post.moderated is True
    assert post.reactions == {"🎉": frozenset({CARL})}
    pos = await _position(requester, r_pages)
    assert pos is not None and (pos.match, pos.missing) == (True, 0)
