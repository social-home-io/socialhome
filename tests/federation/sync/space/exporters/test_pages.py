"""Tests for the §25.6 pages exporter — records carry the host's ``seq``,
the version hash and the open conflict list (v_48); a draft's
``pending_base_seq`` never leaves the household."""

from __future__ import annotations

from socialhome.domain.page import Page
from socialhome.domain.page_version import PageConflictSide, version_hash
from socialhome.federation.sync.space.exporters.pages import PagesExporter


class _Repo:
    def __init__(self) -> None:
        self.page = Page(
            id="pg-1",
            title="T",
            content="v3",
            created_by="u-1",
            created_at="2026-04-18T00:00:00+00:00",
            updated_at="2026-04-18T00:00:00+00:00",
            space_id="sp-1",
            cover_image_url="/c.webp",
            seq=5,
            pending_base_seq=4,
        )
        self.side = PageConflictSide(
            hash=version_hash("T", "side"),
            title="T",
            content="side",
            by="u-2",
            at="2026-04-18T00:00:00.000001+00:00",
            base_seq=3,
        )

    async def list(self, *, space_id):
        return [self.page]

    async def list_conflict_sides(self, page_id, *, space_id):
        return [self.side]


async def test_records_carry_seq_hash_and_conflict_but_no_draft_state():
    (record,) = await PagesExporter(_Repo()).list_records("sp-1")
    assert record["id"] == "pg-1"
    assert record["seq"] == 5
    assert "pending_base_seq" not in record
    assert record["version_hash"] == version_hash("T", "v3", "/c.webp")
    (side,) = record["conflict"]
    assert side["side_id"] == version_hash("T", "side")
    assert (side["by"], side["base_seq"]) == ("u-2", 3)
