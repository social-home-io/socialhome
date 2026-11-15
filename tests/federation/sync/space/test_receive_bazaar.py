"""``receive_bazaar``: bazaar listings, their FAQ and bids by §25.6 sync."""

from __future__ import annotations

import logging
import sqlite3

import pytest

from socialhome.crypto import generate_identity_keypair
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.sync.space.receive_bazaar import _apply_bazaar_faq
from socialhome.federation.sync.space.receiver import SpaceSyncReceiver
from tests.federation.sync.space.receiver_fakes import (
    _CalendarRepoStub,
    _FakeCrypto,
    _FakeFedRepo,
    _FakeRepos,
    _FakeSpacePostRepo,
    _FakeSpaceRepo,
    _GalleryRepoStub,
    _here,
    _PageRepoStub,
    _PostRepoStub,
    _send,
    _SpaceRepoStub,
    _StickyRepoStub,
    _Stub,
    _TaskRepoStub,
)


async def test_bazaar_catchup_save_failure_is_a_warning_naming_the_listing(
    bus, peer_setup, caplog
):
    """A listing the joiner cannot store is logged at WARNING, not DEBUG.

    Regression: the FK failure on an anchor-less listing was swallowed at
    DEBUG for every unannounced listing on every joiner, so the only
    trace was the writer's "1/N statements failed" count.
    """

    peer, _ = peer_setup
    kp_self = generate_identity_keypair()

    class _RefusingBazaarRepo:
        async def save_listing(self, listing, *, space_id):
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

        async def get_listing(self, post_id):
            return None

    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(kp_self.private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer),
        space_repo=_FakeSpaceRepo(),
        space_post_repo=_FakeSpacePostRepo(),
        space_task_repo=_Stub(),
        page_repo=_Stub(),
        sticky_repo=_Stub(),
        space_calendar_repo=_Stub(),
        gallery_repo=_Stub(),
        bazaar_repo=_RefusingBazaarRepo(),
    )
    record = {
        "post_id": "p-listing",
        "seller_user_id": "u-1",
        "mode": "fixed",
        "title": "Lamp",
        "status": "active",
    }
    with caplog.at_level(
        logging.WARNING, logger="socialhome.federation.sync.space.receive_bazaar"
    ):
        await r._dispatch("bazaar", "sp-1", [record], provider="peer-a")
    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "bazaar catch-up" in rec.getMessage()
    ]
    assert warnings, caplog.text
    assert "p-listing" in warnings[0]
    assert "FOREIGN KEY" in warnings[0]


class _FaqRepo:
    def __init__(self) -> None:
        self.applied: list = []

    async def apply_faq_entry(self, entry) -> bool:
        self.applied.append(entry)
        return True


def _faq_item(n: int, **over) -> dict:
    item = {
        "entry_id": f"{n:032x}",
        "rev": n,
        "position": n,
        "removed": False,
        "question": f"Q{n}?",
        "answer": "A.",
    }
    item.update(over)
    return item


async def test_a_bazaar_records_faq_is_applied_removals_first():
    repo = _FaqRepo()
    await _apply_bazaar_faq(
        repo,
        "bzr-1",
        [
            _faq_item(5),
            {"entry_id": f"{9:032x}", "rev": 9, "position": 1, "removed": True},
            _faq_item(2),
        ],
    )
    assert [(e.rev, e.removed) for e in repo.applied] == [
        (9, True),
        (2, False),
        (5, False),
    ]
    assert all(e.listing_post_id == "bzr-1" for e in repo.applied)


async def test_a_malformed_faq_entry_is_skipped_and_the_rest_applied(caplog):
    repo = _FaqRepo()
    with caplog.at_level(logging.WARNING):
        await _apply_bazaar_faq(
            repo, "bzr-1", [_faq_item(1, question="x" * 201), _faq_item(2), "junk"]
        )
    assert [e.rev for e in repo.applied] == [2]
    assert "malformed faq entry" in caplog.text


@pytest.mark.parametrize(
    "items",
    [None, "not a list", [_faq_item(n + 1) for n in range(31)]],
)
async def test_an_absent_or_oversized_faq_applies_nothing(items):
    repo = _FaqRepo()
    await _apply_bazaar_faq(repo, "bzr-1", items)
    assert repo.applied == []


async def test_a_faq_record_with_every_marker_of_a_listing_is_taken():
    # A listing ever holds at most 30 entries and keeps every removal
    # marker, so a full record (30 markers) is legitimate and applied whole.
    repo = _FaqRepo()
    items = [
        {"entry_id": f"{n:032x}", "rev": n + 1, "position": n, "removed": True}
        for n in range(30)
    ]
    await _apply_bazaar_faq(repo, "bzr-1", items)
    assert len(repo.applied) == 30 and all(e.removed for e in repo.applied)


async def test_a_synced_listing_never_names_a_reserved_media_file(setup):
    """A streamed bazaar listing naming a DM / moment / GFS file (``dm-`` /
    ``gm-`` / ``sg-``) would show a file another path owns: dropped, like
    every other received space item (``space_media_refs``)."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "bazaar",
        [
            {
                "post_id": "bzr-r",
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "fixed",
                "title": "Chair",
                "image_urls": [
                    "api/media/dm-chat.webp",
                    "api/media/gm-moment.webp",
                    "api/media/sg-cache.webp",
                    "api/media/chair.webp",
                ],
                "end_time": "2026-06-01T00:00:00+00:00",
                "currency": "USD",
                "status": "active",
                "price": 1,
            },
        ],
    )
    assert c.bazaar_listings[0].image_urls == (_here("chair.webp"),)


async def test_bazaar_listings(setup):
    """F4: bazaar listings ride the chunked sync so a new joiner sees
    the full listing card (mode / price / photos / status) — not just
    the wrapper post's caption."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "bazaar",
        [
            {
                "post_id": "bzr-1",
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "fixed",
                "title": "Vintage chair",
                "description": "A nice chair",
                "image_urls": ["api/media/chair.webp"],
                "end_time": "2026-06-01T00:00:00+00:00",
                "currency": "USD",
                "status": "active",
                "price": 4500,
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        ],
    )
    assert len(c.bazaar_listings) == 1
    listing = c.bazaar_listings[0]
    assert listing.post_id == "bzr-1"
    assert listing.mode.value == "fixed"
    assert listing.status.value == "active"
    assert listing.price == 4500
    assert listing.image_urls == (_here("chair.webp"),)


async def test_bazaar_listings_malformed_record_dropped(setup):
    """Lenient receiver: a record missing required fields is dropped,
    others in the same chunk still apply."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "bazaar",
        [
            {"post_id": "bzr-bad"},  # missing seller, mode, title
            {
                "post_id": "bzr-ok",
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "fixed",
                "title": "OK",
                "end_time": "2026-06-01T00:00:00+00:00",
                "currency": "USD",
                "status": "active",
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        ],
    )
    assert [lst.post_id for lst in c.bazaar_listings] == ["bzr-ok"]


async def test_bazaar_listings_unknown_mode_dropped(setup):
    """Forward-compat: a record with a future ``mode`` value is dropped."""
    r, c, kp = setup
    await _send(
        r,
        kp,
        "bazaar",
        [
            {
                "post_id": "bzr-1",
                "space_id": "sp-1",
                "seller_user_id": "u-seller",
                "mode": "future_mode",  # unknown
                "title": "x",
                "end_time": "2026-06-01T00:00:00+00:00",
                "currency": "USD",
                "status": "active",
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        ],
    )
    assert c.bazaar_listings == []


async def test_bazaar_listings_skipped_when_repo_not_wired(bus, peer):
    """An older deployment without a bazaar repo wired silently skips
    inbound bazaar chunks rather than erroring."""
    peer_inst, peer_kp = peer
    collector = _FakeRepos()
    self_kp = generate_identity_keypair()
    r = SpaceSyncReceiver(
        bus=bus,
        encoder=FederationEncoder(self_kp.private_key),
        crypto=_FakeCrypto(),
        federation_repo=_FakeFedRepo(peer_inst),
        space_repo=_SpaceRepoStub(collector),
        space_post_repo=_PostRepoStub(collector),
        space_task_repo=_TaskRepoStub(collector),
        page_repo=_PageRepoStub(collector),
        sticky_repo=_StickyRepoStub(collector),
        space_calendar_repo=_CalendarRepoStub(collector),
        gallery_repo=_GalleryRepoStub(collector),
        # bazaar_repo deliberately omitted
    )
    await _send(
        r,
        peer_kp,
        "bazaar",
        [
            {
                "post_id": "bzr-1",
                "space_id": "sp-1",
                "seller_user_id": "u",
                "mode": "fixed",
                "title": "x",
                "end_time": "2026-06-01T00:00:00+00:00",
                "currency": "USD",
                "status": "active",
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        ],
    )
    assert collector.bazaar_listings == []
