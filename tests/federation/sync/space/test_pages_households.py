"""Sync pages between two real households (``docs/protocol/sync-pages.md``
Phase 2): the host seals a page over its real rows, the sentinel carries it
sealed, the member verifies and stores it and compares its own rows with
it: a match, a difference explained by newer local rows (no wire cost), or
a bucket listing from the host that proves a row missing.

Built on the §25.6 coverage harness (real SQLite households, real
exporters, real receiver). Only the content-key AEAD is a stand-in (bound
to the session like ``encrypt_chunk``'s AAD).
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import orjson
import pytest

from socialhome.crypto import ed25519_public_key
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.space_sync_page import state_with_page
from socialhome.domain.space_sync_status import SpaceSyncState
from socialhome.federation.sync.space.page_state import PageStateHasher
from socialhome.federation.sync.space.pages import SyncPageService
from socialhome.federation.sync.space.window import SyncWindows
from socialhome.infrastructure.event_bus import EventBus
from socialhome.services.federation_inbound_service import FederationInboundService
from socialhome.sync_page import parse_page_ref, row_key
from tests.federation.sync.space.test_page_state import _Hasher, _stream
from tests.federation.sync.space.test_sync_coverage import (
    ANNA,
    HOST,
    SPACE,
    Home,
    _post,
    homes,  # noqa: F401 — the fixture
)

SEED = bytes(range(1, 33))
MEMBER = "member-household"
T0 = 1_800_000_000_000


class _Crypto:
    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        return 1, base64.b64encode(
            f"{space_id}|{sync_id}|".encode() + plaintext
        ).decode()

    async def decrypt_chunk(self, *, space_id, epoch, sync_id, ciphertext):
        blob = base64.b64decode(ciphertext)
        head = f"{space_id}|{sync_id}|".encode()
        assert epoch == 1 and blob.startswith(head)
        return blob[len(head) :]


class _Spaces:
    """The household's real space repo; the seed only on the host (the
    harness has no key manager to unwrap a stored one)."""

    def __init__(self, home: Home, seed: bytes | None) -> None:
        self._repo = home.repos.space
        self._seed = seed

    def __getattr__(self, name):
        return getattr(self._repo, name)

    async def get_space_seed(self, space_id):
        return self._seed


class _Clock:
    def __init__(self) -> None:
        self.ms = T0

    def __call__(self) -> int:
        return self.ms


async def _no_wait(_seconds: float) -> None:
    """The 15-min compare spacing, without waiting in a test."""


def _service(
    home: Home, own: str, seed: bytes | None, clock: _Clock
) -> SyncPageService:
    return SyncPageService(
        repo=home.repos.space_sync_page,
        spaces=_Spaces(home, seed),
        seqs=home.repos.space_sync_watermark,
        crypto=_Crypto(),
        state=PageStateHasher(
            exporters=home.exporters,
            windows=SyncWindows(home.repos.space),
            files=_Hasher(),
        ),
        exporters=home.exporters,
        own_instance_id=own,
        clock_ms=clock,
        sleep=_no_wait,
    )


class _Wire:
    """One sync round: the member's BEGIN names the buckets it wants listed
    (``buckets_for``), the real host service lists them at the end of its
    stream, the member takes them and classifies on the sentinel."""

    def __init__(self, host_pages: SyncPageService) -> None:
        self.host_pages = host_pages
        self.member_pages: SyncPageService | None = None
        self.asks: list[list[int]] = []
        self.rounds = 0
        #: sync_id → the ``page`` that round's BEGIN carried with its
        #: ``buckets`` (the requester's pending-request record).
        self.asked: dict[str, dict] = {}

    async def hosts(self, space) -> list[str]:
        return [HOST]

    async def round(self) -> None:
        """The next round to the host (periodic or Sync now)."""
        assert self.member_pages is not None
        await self.member_pages.wait_idle()
        buckets = self.member_pages.buckets_for(
            SPACE, HOST, await self.member_pages.ref_for(SPACE)
        )
        self.rounds += 1
        sync_id = f"R{self.rounds}"
        if buckets:
            self.asks.append(list(buckets))
            self.asked[sync_id] = await self.member_pages.ref_for(SPACE)
            records = await self.host_pages.listing_records(
                space_id=SPACE,
                page_ref=parse_page_ref(await self.member_pages.ref_for(SPACE)),
                buckets=buckets,
                requester_version=95,
            )
            await self.member_pages.take_listing(
                space_id=SPACE,
                sync_id=sync_id,
                provider=HOST,
                records=orjson.loads(orjson.dumps(records)),
            )
        await self.member_pages.listing_done(
            space_id=SPACE, sync_id=sync_id, provider=HOST
        )
        await self.member_pages.wait_idle()


@pytest.fixture
async def pair(homes):  # noqa: F811
    host, member = homes
    for home in (host, member):
        await home.db.enqueue(
            "UPDATE spaces SET identity_public_key=? WHERE id=?",
            (ed25519_public_key(SEED).hex(), SPACE),
        )
    clock = _Clock()
    host_pages = _service(host, HOST, SEED, clock)
    member_pages = _service(member, MEMBER, None, clock)
    wire = _Wire(host_pages)
    wire.member_pages = member_pages
    member_pages.attach_hosts(wire.hosts)
    member_pages.attach_asks(wire.asked.get)
    yield host, member, host_pages, member_pages, clock, wire


async def _round(host_pages, member_pages, sync_id: str) -> bool:
    """One sentinel: the member's BEGIN ``page``, the host's ``page_sealed``,
    the member taking it."""
    ref = await member_pages.ref_for(SPACE)
    sealed = await host_pages.sentinel_page(
        space_id=SPACE,
        sync_id=sync_id,
        requester_version=95,
        requester_ref=parse_page_ref(ref),
    )
    await host_pages.wait_idle()
    stored = await member_pages.receive(
        space_id=SPACE, sync_id=sync_id, provider=HOST, sealed=sealed
    )
    await member_pages.wait_idle()
    return stored


async def _position(member, member_pages):
    space = await member.repos.space.get(SPACE)
    return await member_pages.position(space)


async def test_a_page_round_trips_and_the_member_matches_it(pair):
    host, member, host_pages, member_pages, _clock, wire = pair
    await _post(host)
    await _stream(host, member)
    page = await host_pages.seal_if_due(SPACE)
    assert page is not None and page.rows == 1
    assert await _round(host_pages, member_pages, "s-1")
    pos = await _position(member, member_pages)
    assert pos is not None
    assert (pos.seq, pos.match, pos.missing, pos.differing_buckets) == (
        page.seq,
        True,
        0,
        0,
    )
    assert wire.asks == []
    # The page is not sent again while the member holds it.
    assert (
        await host_pages.sentinel_page(
            space_id=SPACE,
            sync_id="s-2",
            requester_version=95,
            requester_ref=parse_page_ref(await member_pages.ref_for(SPACE)),
        )
        is None
    )
    assert (
        state_with_page(SpaceSyncState.HOST_OFFLINE, pos, now=datetime.now(UTC))
        is SpaceSyncState.UP_TO_DATE
    )


async def test_a_missing_row_is_proven_by_the_hosts_listing(pair):
    """The host holds a post the member never got (created before the page,
    so no newer row of ours explains its bucket): the next round asks the
    host for that one bucket's listing, which proves the row missing —
    behind. A stream watermark past the page changes nothing
    (the old shortcut is gone)."""
    host, member, host_pages, member_pages, _clock, wire = pair
    await _post(host)
    await _stream(host, member)
    await _post(host, "post-2")  # the member never gets it
    page = await host_pages.seal_if_due(SPACE)
    assert page is not None
    await member.repos.space.add_space_instance(SPACE, HOST)
    await member.repos.space_sync_watermark.record_applied(SPACE, HOST, page.seq + 99)
    assert await _round(host_pages, member_pages, "s-1")
    await wire.round()
    assert wire.asks == [[row_key("posts", "post-2")[0]]]
    pos = await _position(member, member_pages)
    assert pos is not None and (pos.match, pos.missing) == (False, 1)
    assert (
        state_with_page(SpaceSyncState.UP_TO_DATE, pos, now=datetime.now(UTC))
        is SpaceSyncState.BEHIND
    )
    await wire.round()  # answered: the next round asks nothing
    assert len(wire.asks) == 1


async def test_a_member_ahead_by_a_live_event_matches_without_a_listing(pair):
    """Review finding 6: ahead through the LIVE federation inbound path (not
    a stream, not a hand-set watermark). The post arrives after the page;
    its bucket is explained locally — no listing, match."""
    host, member, host_pages, member_pages, clock, wire = pair
    clock.ms = int(datetime.now(UTC).timestamp() * 1000) - 60_000
    await _post(host)
    await _stream(host, member)
    page = await host_pages.seal_if_due(SPACE)
    assert page is not None
    await member.db.enqueue(
        "INSERT OR IGNORE INTO space_remote_members(space_id, instance_id,"
        " user_id, role) VALUES(?,?,?,?)",
        (SPACE, HOST, ANNA, "member"),
    )
    inbound = FederationInboundService(
        bus=EventBus(),
        conversation_repo=member.repos.conversation,
        space_post_repo=member.repos.space_post,
        space_repo=member.repos.space,
        user_repo=member.repos.user,
        space_remote_member_repo=member.repos.space_remote_member,
    )
    await inbound._on_space_post_created(
        FederationEvent(
            msg_id="msg-live",
            event_type=FederationEventType.SPACE_POST_CREATED,
            from_instance=HOST,
            to_instance=MEMBER,
            timestamp=datetime.now(UTC).isoformat(),
            payload={"id": "live-1", "author": ANNA, "type": "text", "content": "hi"},
            space_id=SPACE,
        )
    )
    assert await member.repos.space_post.get("live-1") is not None
    assert await _round(host_pages, member_pages, "s-1")
    await wire.round()
    pos = await _position(member, member_pages)
    assert pos is not None and pos.differing_buckets == 1
    assert (pos.match, pos.missing) == (True, 0)
    assert wire.asks == []
