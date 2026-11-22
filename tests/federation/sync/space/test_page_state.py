"""Sync pages — the host and a member hash the same rows the same
(``docs/protocol/sync-pages.md`` §2, "Required tests").

Built on the §25.6 coverage harness (two real SQLite households, the real
exporters, the real :class:`SpaceSyncReceiver`): for every item type, the
host seeds and changes a row, streams it, and both households compute the
bucket vector of the space. They must match — the host's export and the
member's re-export of the same row give the same leaf.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta

import orjson
import pytest

from socialhome.domain.calendar import CalendarEvent
from socialhome.domain.file_hashes import FileHash
from socialhome.domain.link_preview import LinkPreview
from socialhome.domain.post import (
    BazaarListing,
    BazaarMode,
    BazaarStatus,
    Comment,
    CommentType,
    FileMeta,
    PostType,
)
from socialhome.federation.sync.space.exporter import (
    RESOURCE_ORDER,
    collect_batches,
    record_batches,
)
from socialhome.federation.sync.space.page_state import PageStateHasher
from socialhome.federation.sync.space.window import SyncWindows
from socialhome.services.inbound_media_store import (
    media_basename,
    space_direct_media_name,
)
from socialhome.sync_page import (
    HINT_PREFIX_BYTES,
    VectorBuilder,
    differing_buckets,
    fetched_record,
    record_leaf,
    row_key,
)
from tests.federation.sync.space.test_sync_coverage import (
    ANNA,
    CARL,
    CASES,
    HOST,
    SPACE,
    T0,
    TASK,
    Case,
    Home,
    _post,
    _task,
    homes,  # noqa: F401 — the fixture
)


async def _stream(host: Home, member: Home) -> None:
    """The host's full stream into the member, every chunk's records through
    JSON as on the wire (a tuple arrives as a list)."""
    for resource in RESOURCE_ORDER:
        exporter = host.exporters.get(resource)
        if exporter is None:
            continue
        records = await collect_batches(record_batches(exporter, SPACE))
        if records:
            wire = orjson.loads(orjson.dumps(records))
            await member.receiver._apply(resource, SPACE, wire, provider=HOST)


#: Files the cases reference, as the host stores them.
_FILES = ("a.webp", "a_t.webp")


class _Hasher:
    """The bytes of every case file, as the host and the member each hold
    them: the host under its upload name, the member under the name the
    receiver rebinds a streamed reference to (``sd-``)."""

    def __init__(self) -> None:
        self.table: dict[str, FileHash] = {}
        for name in _FILES:
            h = FileHash(hashlib.sha256(name.encode()).hexdigest())
            self.table[name] = h
            self.table[space_direct_media_name(SPACE, HOST, name)] = h

    async def hash_name(self, name: str) -> FileHash | None:
        return self.table.get(name)


def _state(home: Home) -> PageStateHasher:
    return PageStateHasher(
        exporters=home.exporters,
        windows=SyncWindows(home.repos.space),
        files=_Hasher(),
    )


def _committed(case: Case) -> bool:
    """Cases about host-only resources (the roster, member pictures)."""
    return not any(
        w in case.name for w in ("member role", "member banned", "member picture")
    )


@pytest.mark.parametrize(
    "case", [c for c in CASES if _committed(c)], ids=lambda c: c.name
)
async def test_host_and_member_hash_the_same_state(homes, case: Case):  # noqa: F811
    host, member = homes
    await case.seed(host)
    await _stream(host, member)
    await case.change(host)
    await _stream(host, member)
    h_vec, h_rows = await _state(host).vector(SPACE)
    m_vec, m_rows = await _state(member).vector(SPACE)
    assert h_rows > 0, "the case commits nothing"
    assert differing_buckets(h_vec, m_vec) == []
    assert (h_rows, h_vec) == (m_rows, m_vec)


async def test_a_change_the_member_missed_shows_as_one_differing_bucket(
    homes,  # noqa: F811
):
    host, member = homes
    await _post(host)
    await _stream(host, member)
    await host.repos.space_post.edit(
        "post-1", "changed", space_id=SPACE, edited_at="2026-11-03 10:00:00"
    )
    h_vec, _ = await _state(host).vector(SPACE)
    m_vec, _ = await _state(member).vector(SPACE)
    assert differing_buckets(h_vec, m_vec) == [row_key("posts", "post-1")[0]]


async def test_host_only_resources_never_enter_the_vector(homes):  # noqa: F811
    host, _member = homes
    vec, rows = await _state(host).vector(SPACE)
    # The harness seats three members and no content: nothing committed.
    assert rows == 0
    assert vec == VectorBuilder().finish()


async def test_a_file_without_a_hash_keeps_its_row_out(homes):  # noqa: F811
    host, _ = homes
    await _post(host, image_urls=["api/media/unknown.webp"])
    _, rows = await _state(host).vector(SPACE)
    assert rows == 0
    await _post(host, "post-2", image_urls=["api/media/a.webp"])
    _, rows = await _state(host).vector(SPACE)
    assert rows == 1


async def test_the_pages_cutoff_pins_the_retention_window(homes):  # noqa: F811
    """Every household evaluates the commitment with the page's cutoff,
    never its own clock: a post older than the cutoff counts only when the
    cutoff includes it."""
    host, _ = homes
    space = await host.repos.space.get(SPACE)
    assert space is not None
    await host.repos.space.save(replace(space, retention_days=7))
    await _post(host)  # created 2026-11-01
    _, rows_now = await _state(host).vector(SPACE)
    _, rows_pinned = await _state(host).vector(SPACE, cutoff="2026-10-01 00:00:00")
    assert rows_pinned == rows_now + 1
    cutoff = await _state(host).cutoff(SPACE)
    assert cutoff is not None


# ── Rows with files and normalised fields, through the real receiver ─────


async def _post_with_files(h: Home) -> None:
    await _post(
        h,
        type=PostType.FILE,
        image_urls=("api/media/a.webp",),
        file_meta=FileMeta(
            url="api/media/a.webp",
            mime_type="image/webp",
            original_name="a.webp",
            size_bytes=3,
        ),
        link_preview=LinkPreview(
            url="https://example.org/x",
            title="X",
            thumbnail_url="api/media/a_t.webp",
        ),
        edited_at=T0 + timedelta(days=1),
    )


async def _comment_with_file(h: Home) -> None:
    await _post(h)
    await h.repos.space_post.add_comment(
        Comment(
            id="c-1",
            post_id="post-1",
            author=CARL,
            type=CommentType.IMAGE,
            created_at=T0,
            content="look",
            media_url="api/media/a.webp",
        ),
        space_id=SPACE,
    )


async def _event_with_cover(h: Home) -> None:
    await h.repos.space_cal.save_event(
        CalendarEvent(
            id="ev-1",
            calendar_id=SPACE,
            summary="Picnic",
            start=T0 + timedelta(days=3),
            end=T0 + timedelta(days=3, hours=2),
            created_by=ANNA,
            cover_url="api/media/a.webp",
        ),
        space_id=SPACE,
    )


async def _listing_with_images_sold(h: Home) -> None:
    await _post(h, type=PostType.BAZAAR, content="Bike")
    await h.repos.bazaar.save_listing(
        BazaarListing(
            post_id="post-1",
            space_id=SPACE,
            seller_user_id=ANNA,
            mode=BazaarMode.FIXED,
            title="Bike",
            end_time="2027-01-01T00:00:00+00:00",
            currency="EUR",
            status=BazaarStatus.SOLD,
            created_at=T0.isoformat(),
            price=50,
            image_urls=("api/media/a.webp", "api/media/a_t.webp"),
            winner_user_id=CARL,
            sold_at="2026-11-05T10:00:00.654321+00:00",
        ),
        space_id=SPACE,
    )


FILE_CASES = {
    "post with images, a file and a link card": _post_with_files,
    "comment with a picture": _comment_with_file,
    "calendar event with a cover": _event_with_cover,
    "sold bazaar listing with images": _listing_with_images_sold,
}


@pytest.mark.parametrize("seed", FILE_CASES.values(), ids=FILE_CASES.keys())
async def test_rows_with_files_hash_the_same_on_both_sides(homes, seed):  # noqa: F811
    host, member = homes
    await seed(host)
    await _stream(host, member)
    h_vec, h_rows = await _state(host).vector(SPACE)
    m_vec, m_rows = await _state(member).vector(SPACE)
    assert h_rows > 0
    assert differing_buckets(h_vec, m_vec) == []
    assert h_rows == m_rows


# ── Phase 3: a deliverer's records ───────────────────────────────────────


async def test_a_deliverer_keeps_the_records_of_the_asked_buckets_with_hashes(
    homes,  # noqa: F811
):
    """A member re-exports a host's post (its files under the member's own
    names): the record it would deliver carries the files' content hashes,
    and the committed part of it hashes to the very leaf it lists — so a
    third household can verify it without holding the files."""
    host, member = homes
    await _post_with_files(host)
    await _stream(host, member)
    index = row_key("posts", "post-1")[0]
    scan = await _state(member).rows_in(SPACE, [index], cutoff=None, keep_records=True)
    ((listed,),) = [scan.rows[index]]
    assert scan.records is not None
    ((resource, record),) = scan.records[index]
    assert resource == "posts" and record["files_suite"] == "sha256"
    hashes = {name: entry["h"] for name, entry in record["files"].items()}
    got = record_leaf(
        "posts",
        fetched_record("posts", record),
        lambda url: hashes.get(media_basename(url)),
    )
    assert got == (index, listed.leaf)


async def test_a_deliverer_skips_rows_the_requester_holds(homes):  # noqa: F811
    host, _member = homes
    await _post(host)
    await _post(host, "post-2")
    i1, i2 = row_key("posts", "post-1")[0], row_key("posts", "post-2")[0]
    state = _state(host)
    plain = await state.rows_in(SPACE, [i1, i2], cutoff=None)
    assert plain.records is None  # Phase 2 listings keep no records
    held = plain.rows[i1][0].leaf[:HINT_PREFIX_BYTES]
    scan = await state.rows_in(
        SPACE, [i1, i2], cutoff=None, keep_records=True, skip=frozenset({held})
    )
    assert scan.records is not None
    kept = [r["id"] for rows in scan.records.values() for _res, r in rows]
    assert kept == ["post-2"]
    assert len(scan.rows[i1]) == 1  # still listed in full


async def test_a_requester_keeps_its_own_records_and_counterparts(homes):  # noqa: F811
    """Phase 3 re-review N2 / N3: our own records of the delivered
    identities, from any bucket (their uncommitted columns carry over), and
    every held task under its live or archived identity with its change
    time."""
    host, _member = homes
    await _post(host)
    await _post(host, "post-2")
    i2 = row_key("posts", "post-2")[0]
    scan = await _state(host).rows_in(
        SPACE,
        [i2],
        cutoff=None,
        keep_ids=frozenset({("posts", "post-1"), ("tasks", "nope")}),
    )
    assert set(scan.own) == {("posts", "post-1")}
    assert scan.own[("posts", "post-1")]["id"] == "post-1"
    assert scan.held_any == {}  # no task held
    await _task(host)
    scan = await _state(host).rows_in(SPACE, [i2], cutoff=None)
    assert list(scan.held_any) == [("tasks", TASK)]
    assert scan.held_any[("tasks", TASK)] is not None


async def test_known_content_names_hash_without_their_bytes(homes):  # noqa: F811
    """Phase 3 re-review N1: a verified row just applied names its file by
    content; the bytes may still be on the way, so the re-read takes the
    hash from the applied row (``known``)."""
    host, _member = homes
    await _post(host, image_urls=["api/media/sc-new.webp"])
    index = row_key("posts", "post-1")[0]
    state = _state(host)
    without = await state.rows_in(SPACE, [index], cutoff=None)
    assert without.rows[index] == []  # unhashable: host-only
    h = hashlib.sha256(b"x").hexdigest()
    known = await state.rows_in(SPACE, [index], cutoff=None, known={"sc-new.webp": h})
    ((row,),) = [known.rows[index]]
    assert row.row_id == "post-1"
