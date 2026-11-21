"""Sync pages between two real households (``docs/protocol/sync-pages.md``
Phase 2): the host seals a page over its real rows, the sentinel carries it
sealed, the member verifies and stores it, checks its own rows against it,
and its sync status reads "up to date as of page N" — and "behind" once a
newer page exists.

Built on the §25.6 coverage harness (real SQLite households, real
exporters, real receiver). Only the content-key AEAD is a stand-in (bound
to the session like ``encrypt_chunk``'s AAD).
"""

from __future__ import annotations

import base64

import pytest

from socialhome.crypto import ed25519_public_key
from socialhome.domain.space_sync_page import state_with_page
from socialhome.domain.space_sync_status import SpaceSyncState
from socialhome.federation.sync.space.page_state import PageStateHasher
from socialhome.federation.sync.space.pages import SyncPageService
from socialhome.federation.sync.space.window import SyncWindows
from socialhome.sync_page import PAGE_MIN_INTERVAL_S, parse_page_ref
from tests.federation.sync.space.test_page_state import _Hasher, _stream
from tests.federation.sync.space.test_sync_coverage import (
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


@pytest.fixture
async def pair(homes):  # noqa: F811
    host, member = homes
    for home in (host, member):
        await home.db.enqueue(
            "UPDATE spaces SET identity_public_key=? WHERE id=?",
            (ed25519_public_key(SEED).hex(), SPACE),
        )
    clock = _Clock()
    yield (
        host,
        member,
        _service(host, HOST, SEED, clock),
        _service(member, MEMBER, None, clock),
        clock,
    )


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


async def test_a_page_round_trips_and_the_member_matches_it(pair):
    host, member, host_pages, member_pages, _clock = pair
    await _post(host)
    await _stream(host, member)
    page = await host_pages.seal_if_due(SPACE)
    assert page is not None and page.rows == 1
    assert await _round(host_pages, member_pages, "s-1")
    space = await member.repos.space.get(SPACE)
    pos = await member_pages.position(space, [HOST])
    assert pos is not None
    assert (pos.seq, pos.match, pos.differing_buckets) == (page.seq, True, 0)
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
    # Not applied from the host yet (no clean stream recorded): the status
    # says a newer page exists than what we applied — unless the root check
    # matched, which it did.
    assert (
        state_with_page(SpaceSyncState.HOST_OFFLINE, pos) is SpaceSyncState.UP_TO_DATE
    )


async def test_a_newer_page_shows_the_member_behind(pair):
    host, member, host_pages, member_pages, clock = pair
    await _post(host)
    await _stream(host, member)
    await host_pages.seal_if_due(SPACE)
    await _round(host_pages, member_pages, "s-1")
    # The host changes something the member does not get, seals again
    # (15 min later) and the next sentinel brings the newer page.
    await _post(host, "post-2")
    clock.ms += PAGE_MIN_INTERVAL_S * 1000
    newer = await host_pages.seal_if_due(SPACE)
    assert newer is not None and newer.prev_root is not None
    assert await _round(host_pages, member_pages, "s-3")
    space = await member.repos.space.get(SPACE)
    pos = await member_pages.position(space, [HOST])
    assert pos is not None and pos.seq == newer.seq
    assert pos.applied is False  # we applied no stream that far
    assert state_with_page(SpaceSyncState.HOST_OFFLINE, pos) is SpaceSyncState.BEHIND
    # Compared: one post the member lacks differs → needs sync, whatever
    # the host last answered.
    assert (pos.match, pos.differing_buckets) == (False, 1)
    assert state_with_page(SpaceSyncState.UP_TO_DATE, pos) is SpaceSyncState.BEHIND
