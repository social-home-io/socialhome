"""``receive_pages``: pages (v_48 versions) by §25.6 sync."""

from __future__ import annotations

import logging

from socialhome.services.page_conflict_service import PageMode
from tests.federation.sync.space.receiver_fakes import _SYNC_REFS, _GfsMediaSink, _send


async def test_pages(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "pages",
        [
            {
                "id": "pg-1",
                "title": "Welcome",
                "content": "Hi",
                "created_by": "u-1",
                "created_at": "2026-04-18T00:00:00+00:00",
                "updated_at": "2026-04-18T00:00:00+00:00",
            },
        ],
    )
    assert len(c.pages) == 1
    assert c.pages[0].id == "pg-1"


class _FakeConflicts:
    """The v_48 engine: mode + recorded mirrors."""

    def __init__(self, mode: str = "member", host: str = "peer-a") -> None:

        self.mode_ = PageMode(mode)
        self.host = host
        self.mirrored: list[dict] = []

    async def mode(self, space_id):
        return self.mode_, self.host

    async def mirror(self, **kwargs):
        self.mirrored.append(kwargs)


_PAGE_RECORD = {
    "id": "pg-1",
    "title": "Welcome",
    "content": "Hi again",
    "created_by": "u-1",
    "last_editor_user_id": "u-2",
    "created_at": "2026-04-18T00:00:00+00:00",
    "updated_at": "2026-04-19T00:00:00+00:00",
}


async def test_a_host_record_with_seq_is_mirrored_by_seq(setup):
    """v_48: the host's chunk is its version — mirrored by ``seq`` (never
    an overwrite, never a revert)."""
    r, c, kp = setup
    conflicts = _FakeConflicts()
    r._page_conflicts = conflicts
    await _send(r, kp, "pages", [{**_PAGE_RECORD, "seq": 7, "conflict": []}])
    assert c.pages == []
    (call,) = conflicts.mirrored
    assert call["version"].seq == 7 and call["version"].content == "Hi again"


async def test_a_malformed_host_record_is_skipped(setup):
    r, c, kp = setup
    conflicts = _FakeConflicts()
    r._page_conflicts = conflicts
    await _send(r, kp, "pages", [{**_PAGE_RECORD, "seq": -3}])
    assert conflicts.mirrored == [] and c.pages == []


async def test_a_record_from_a_non_host_never_updates_a_held_page(setup):
    r, c, kp = setup
    conflicts = _FakeConflicts(host="someone-else")
    r._page_conflicts = conflicts
    r._page_repo.held[("pg-1", "sp-1")] = object()
    await _send(r, kp, "pages", [{**_PAGE_RECORD, "seq": 99}])
    assert conflicts.mirrored == [] and c.pages == []


async def test_a_new_page_from_a_non_host_lands_unsequenced(setup):
    r, c, kp = setup
    r._page_conflicts = _FakeConflicts(host="someone-else")
    await _send(r, kp, "pages", [{**_PAGE_RECORD, "seq": 99}])
    (page,) = c.pages
    assert (page.id, page.seq) == ("pg-1", 0)


async def test_under_a_legacy_host_records_are_taken_whole(setup):
    r, c, kp = setup
    conflicts = _FakeConflicts("legacy")
    r._page_conflicts = conflicts
    await _send(r, kp, "pages", [_PAGE_RECORD])
    assert [p.content for p in c.pages] == ["Hi again"]
    # … but a record from the host WITH ``seq`` is its version, mirrored
    # even before we saw its v_48 capabilities.
    await _send(r, kp, "pages", [{**_PAGE_RECORD, "seq": 3}])
    assert [m["version"].seq for m in conflicts.mirrored] == [3]


async def test_the_host_takes_no_page_record(setup):
    r, c, kp = setup
    r._page_conflicts = _FakeConflicts("host", host="self")
    await _send(r, kp, "pages", [{**_PAGE_RECORD, "seq": 3}, _PAGE_RECORD])
    assert c.pages == []


async def test_an_over_limit_page_record_from_a_member_is_skipped(
    setup, small_limits, caplog
):
    """Under a pre-v_48 host (or a new page from a member) a page record is
    stored as sent — through the same rule."""
    r, c, kp = setup
    caplog.set_level(logging.WARNING)
    await _send(
        r,
        kp,
        "pages",
        [
            {**_PAGE_RECORD, "id": "pg-big", "content": "x" * 3000},
            _PAGE_RECORD,
        ],
    )
    assert [p.id for p in c.pages] == ["pg-1"]
    assert "pg-big" in caplog.text


async def test_the_hosts_page_record_keeps_its_files_under_the_pages_names(setup):
    """v_82 (sync line 82): a page record from the host is its version
    byte for byte — the descriptor is kept under the names the page shows."""
    r, _c, kp = setup
    conflicts = _FakeConflicts()
    r._page_conflicts = conflicts
    sink = _GfsMediaSink()
    r.attach_gfs_media(sink)
    body = "![a](api/media/a.webp)"
    await _send(
        r,
        kp,
        "pages",
        [
            {
                **_PAGE_RECORD,
                "content": body,
                "cover_image_url": "api/media/c.webp",
                "seq": 7,
                "conflict": [],
                "media_refs": _SYNC_REFS,
            }
        ],
    )
    assert sink.as_shown == [
        ("sp-1", _SYNC_REFS, ["api/media/c.webp", "api/media/a.webp"], "pg-1", "peer-a")
    ]
    assert sink.kept == [{}]
    assert conflicts.mirrored[0]["version"].content == body


async def test_the_hosts_page_record_keeps_its_sides_files_too(setup):
    """Review finding 2 (sync): a record's open sides' files are kept too."""
    r, _c, kp = setup
    r._page_conflicts = _FakeConflicts()
    sink = _GfsMediaSink()
    r.attach_gfs_media(sink)
    await _send(
        r,
        kp,
        "pages",
        [
            {
                **_PAGE_RECORD,
                "content": "no pictures",
                "seq": 7,
                "conflict": [
                    {
                        "title": "T",
                        "content": "![s](api/media/s.webp)",
                        "cover": "api/media/sc.webp",
                        "by": "u-x",
                        "at": "2026-01-01T00:00:00+00:00",
                    }
                ],
                "media_refs": _SYNC_REFS,
            }
        ],
    )
    assert sink.as_shown[0][2] == ["api/media/sc.webp", "api/media/s.webp"]


async def test_another_households_page_record_keeps_no_descriptor(setup):
    r, _c, kp = setup
    r._page_conflicts = _FakeConflicts(host="someone-else")
    sink = _GfsMediaSink()
    r.attach_gfs_media(sink)
    await _send(
        r,
        kp,
        "pages",
        [
            {
                **_PAGE_RECORD,
                "content": "![a](api/media/a.webp)",
                "media_refs": _SYNC_REFS,
            }
        ],
    )
    assert sink.as_shown == [] and sink.kept == []
