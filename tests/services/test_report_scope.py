"""Tests for :class:`ReportScope` — the space a report target lives in."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from socialhome.domain.report import ReportTargetType as T
from socialhome.services.report_scope import ReportScope, TargetScope


class _Posts:
    async def get(self, pid):
        if pid == "p-del":
            return ("sp-post", SimpleNamespace(deleted=True, content="x"))
        return ("sp-post", SimpleNamespace()) if pid == "p1" else None

    async def get_comment(self, cid):
        if cid == "c1":
            return SimpleNamespace(post_id="p1")
        if cid == "c-del":
            return SimpleNamespace(post_id="p1", deleted=True)
        if cid == "orphan":
            return SimpleNamespace(post_id="gone")
        return None


class _ById:
    def __init__(self, hits):
        self._hits = hits

    async def get(self, i):
        return self._hits.get(i)

    async def get_event(self, i):
        return self._hits.get(i)


class _Gallery:
    async def get_item(self, i):
        return {
            "g1": SimpleNamespace(album_id="a-space"),
            "g2": SimpleNamespace(album_id="a-house"),
            "g3": SimpleNamespace(album_id="a-gone"),
        }.get(i)

    async def get_album(self, a):
        return {
            "a-space": SimpleNamespace(space_id="sp-gal"),
            "a-house": SimpleNamespace(space_id=None),
        }.get(a)


@pytest.fixture
def scope():
    return ReportScope(
        space_post_repo=_Posts(),  # type: ignore[arg-type]
        page_repo=_ById(  # type: ignore[arg-type]
            {
                "pg1": SimpleNamespace(space_id="sp-page"),
                "pg2": SimpleNamespace(space_id=None),
            }
        ),
        sticky_repo=_ById({"s1": SimpleNamespace(space_id="sp-st")}),  # type: ignore[arg-type]
        space_task_repo=_ById({"t1": ("sp-task", SimpleNamespace())}),  # type: ignore[arg-type]
        space_calendar_repo=_ById({"e1": ("sp-cal", SimpleNamespace())}),  # type: ignore[arg-type]
        gallery_repo=_Gallery(),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("tt", "tid", "expected"),
    [
        (T.POST, "p1", TargetScope(True, "sp-post")),
        (T.POST, "nope", TargetScope(False)),
        (T.POST, "p-del", TargetScope(True, "sp-post", gone=True)),
        (T.COMMENT, "c-del", TargetScope(True, "sp-post", gone=True)),
        (T.COMMENT, "c1", TargetScope(True, "sp-post")),
        (T.COMMENT, "orphan", TargetScope(False)),
        (T.COMMENT, "nope", TargetScope(False)),
        (T.PAGE, "pg1", TargetScope(True, "sp-page")),
        (T.PAGE, "pg2", TargetScope(True, None)),
        (T.PAGE, "nope", TargetScope(False)),
        (T.STICKY, "s1", TargetScope(True, "sp-st")),
        (T.STICKY, "nope", TargetScope(False)),
        (T.TASK, "t1", TargetScope(True, "sp-task")),
        (T.TASK, "nope", TargetScope(False)),
        (T.CALENDAR_EVENT, "e1", TargetScope(True, "sp-cal")),
        (T.CALENDAR_EVENT, "nope", TargetScope(False)),
        (T.GALLERY_ITEM, "g1", TargetScope(True, "sp-gal")),
        (T.GALLERY_ITEM, "g2", TargetScope(True, None)),
        (T.GALLERY_ITEM, "g3", TargetScope(False)),
        (T.GALLERY_ITEM, "nope", TargetScope(False)),
        (T.USER, "u1", TargetScope(False)),
        (T.POST, "", TargetScope(False)),
    ],
)
async def test_scope_of(scope, tt, tid, expected):
    assert await scope.of(tt, tid) == expected


@pytest.mark.parametrize(
    "tt",
    [T.POST, T.COMMENT, T.PAGE, T.STICKY, T.TASK, T.CALENDAR_EVENT, T.GALLERY_ITEM],
)
async def test_unwired_repos_find_nothing(tt):
    assert await ReportScope().of(tt, "x") == TargetScope(False)


async def test_preview_is_flattened_and_clipped():
    class _P:
        async def get(self, pid):
            return ("sp", SimpleNamespace(content="a\n\n  b " + "x" * 400))

    got = await ReportScope(space_post_repo=_P()).of(T.POST, "p")  # type: ignore[arg-type]
    assert got.preview is not None
    assert got.preview.startswith("a b x")
    assert len(got.preview) == 160 and got.preview.endswith("…")

    class _Blank:
        async def get(self, pid):
            return ("sp", SimpleNamespace(content="   "))

    blank = await ReportScope(space_post_repo=_Blank()).of(T.POST, "p")  # type: ignore[arg-type]
    assert blank.preview is None
