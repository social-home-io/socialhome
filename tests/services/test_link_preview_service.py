"""Tests for :mod:`socialhome.services.link_preview_service`."""

from __future__ import annotations

import asyncio
import io
import pathlib

import pytest
from PIL import Image

from socialhome.domain.link_preview import LinkPreview
from socialhome.domain.post import PostType
from socialhome.domain.preferences import FeatureDisabledError, HouseholdPreferences
from socialhome.media.image_processor import ImageProcessor
from socialhome.outbound_fetch import FetchResult, OutboundFetchRefused
from socialhome.services import link_preview_service as lps
from socialhome.services.link_preview_service import (
    LinkPreviewService,
    wire_link_preview,
)

PAGE = b"""<html><head><title>Plain</title>
<meta property="og:title" content="A &lt;b&gt;title">
<meta property="og:description" content="Some   description">
<meta property="og:site_name" content="Example News">
<meta property="og:image" content="/card.png">
<meta property="og:url" content="https://example.com/story">
</head></html>"""


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "red").save(buf, format="PNG")
    return buf.getvalue()


class FakeFetcher:
    def __init__(self, pages: dict[str, object]) -> None:
        self.pages = pages
        self.calls: list[str] = []
        self.budgets: list[float | None] = []
        self.on_fetch = None
        self.gate: asyncio.Event | None = None

    async def fetch(self, url, *, accept, max_bytes, truncate=False, timeout_s=None):
        self.calls.append(url)
        self.budgets.append(timeout_s)
        if self.on_fetch is not None:
            self.on_fetch()
        if self.gate is not None:
            await self.gate.wait()
        item = self.pages.get(url)
        if item is None:
            raise OutboundFetchRefused("status", "404")
        if isinstance(item, Exception):
            raise item
        body, ctype, final = item  # type: ignore[misc]
        return FetchResult(
            url=final, content_type=ctype, charset=None, body=body, truncated=False
        )


class FakePrefs:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow

    async def get_household(self) -> HouseholdPreferences:
        return HouseholdPreferences(allow_link_preview=self.allow)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _svc(tmp_path: pathlib.Path, pages, *, prefs=None, clock=None):
    fetcher = FakeFetcher(pages)
    svc = LinkPreviewService(
        fetcher=fetcher,  # type: ignore[arg-type]
        image_processor=ImageProcessor(),
        media_dir=tmp_path,
        preferences=prefs,
        clock=clock or Clock(),
    )
    return svc, fetcher


def _default_pages():
    return {
        "https://example.com/story": (PAGE, "text/html", "https://example.com/story"),
        "https://example.com/card.png": (
            _png(),
            "image/png",
            "https://example.com/card.png",
        ),
    }


async def test_builds_preview_with_local_image(tmp_path):
    svc, fetcher = _svc(tmp_path, _default_pages())
    p = await svc.preview_for_url("https://EXAMPLE.com/story#top", user_id="u1")
    assert p is not None
    assert p.url == "https://example.com/story"
    assert p.title == "A <b>title"  # plain text; the SPA escapes it
    assert p.description == "Some description"
    assert p.site_name == "Example News"
    assert p.thumbnail_url is not None
    assert p.thumbnail_url.startswith("api/media/")
    stored = tmp_path / p.thumbnail_url.removeprefix("api/media/")
    assert Image.open(stored).format == "WEBP"
    assert fetcher.calls == [
        "https://example.com/story",
        "https://example.com/card.png",
    ]


async def test_cache_reuses_result_and_expires(tmp_path):
    clock = Clock()
    svc, fetcher = _svc(tmp_path, _default_pages(), clock=clock)
    a = await svc.preview_for_url("https://example.com/story", user_id="u1")
    b = await svc.preview_for_url("https://example.com/story", user_id="u2")
    assert a == b
    assert len(fetcher.calls) == 2  # page + image, once
    clock.t += lps.CACHE_TTL_S + 1
    await svc.preview_for_url("https://example.com/story", user_id="u1")
    assert len(fetcher.calls) == 4


async def test_cache_rebuilds_when_image_file_gone(tmp_path):
    svc, fetcher = _svc(tmp_path, _default_pages())
    a = await svc.preview_for_url("https://example.com/story", user_id="u1")
    assert a is not None and a.thumbnail_url
    (tmp_path / a.thumbnail_url.removeprefix("api/media/")).unlink()
    b = await svc.preview_for_url("https://example.com/story", user_id="u1")
    assert b is not None and b.thumbnail_url != a.thumbnail_url
    assert len(fetcher.calls) == 4


async def test_negative_result_is_cached_briefly(tmp_path):
    clock = Clock()
    svc, fetcher = _svc(tmp_path, {}, clock=clock)
    assert await svc.preview_for_url("https://nothing.example/", user_id="u") is None
    assert await svc.preview_for_url("https://nothing.example/", user_id="u") is None
    assert len(fetcher.calls) == 1
    clock.t += lps.NEGATIVE_TTL_S + 1
    await svc.preview_for_url("https://nothing.example/", user_id="u")
    assert len(fetcher.calls) == 2


async def test_concurrent_requests_share_one_fetch(tmp_path):
    svc, fetcher = _svc(tmp_path, _default_pages())
    fetcher.gate = asyncio.Event()
    t1 = asyncio.create_task(
        svc.preview_for_url("https://example.com/story", user_id="u1")
    )
    t2 = asyncio.create_task(
        svc.preview_for_url("https://example.com/story", user_id="u2")
    )
    await asyncio.sleep(0.01)
    fetcher.gate.set()
    a, b = await asyncio.gather(t1, t2)
    assert a == b and a is not None
    assert fetcher.calls.count("https://example.com/story") == 1


async def test_per_user_fetch_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(lps, "USER_FETCH_LIMIT", 2)
    svc, fetcher = _svc(tmp_path, {})
    for i in range(2):
        await svc.preview_for_url(f"https://x{i}.example/", user_id="greedy")
    assert await svc.preview_for_url("https://x9.example/", user_id="greedy") is None
    assert len(fetcher.calls) == 2  # third never fetched
    # another member still has budget
    await svc.preview_for_url("https://y.example/", user_id="other")
    assert len(fetcher.calls) == 3


async def test_household_fetch_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(lps, "HOUSEHOLD_FETCH_LIMIT", 2)
    svc, fetcher = _svc(tmp_path, {})
    for i in range(3):
        await svc.preview_for_url(f"https://h{i}.example/", user_id=f"u{i}")
    assert len(fetcher.calls) == 2


async def test_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(lps, "CACHE_MAX_ENTRIES", 2)
    svc, _ = _svc(tmp_path, {})
    for i in range(4):
        await svc.preview_for_url(f"https://c{i}.example/", user_id=f"u{i}")
    assert len(svc._cache) == 2


async def test_disabled_by_admin(tmp_path):
    svc, fetcher = _svc(tmp_path, _default_pages(), prefs=FakePrefs(allow=False))
    assert await svc.enabled() is False
    with pytest.raises(FeatureDisabledError):
        await svc.preview_for_url("https://example.com/story", user_id="u")
    got = await svc.preview_for_post(
        post_type=PostType.TEXT,
        content="look https://example.com/story",
        user_id="u",
        no_link_preview=False,
    )
    assert got is None
    assert fetcher.calls == []


async def test_enabled_by_default(tmp_path):
    svc, _ = _svc(tmp_path, {}, prefs=FakePrefs(allow=True))
    assert await svc.enabled() is True


@pytest.mark.parametrize(
    ("post_type", "content", "opt_out"),
    [
        (PostType.TEXT, "look https://example.com/story", True),
        (PostType.IMAGE, "look https://example.com/story", False),
        (PostType.TEXT, "no link here", False),
        (PostType.TEXT, None, False),
    ],
)
async def test_preview_for_post_skips(tmp_path, post_type, content, opt_out):
    svc, fetcher = _svc(tmp_path, _default_pages())
    got = await svc.preview_for_post(
        post_type=post_type, content=content, user_id="u", no_link_preview=opt_out
    )
    assert got is None
    assert fetcher.calls == []


async def test_preview_for_post_uses_first_link(tmp_path):
    svc, _ = _svc(tmp_path, _default_pages())
    got = await svc.preview_for_post(
        post_type=PostType.TEXT,
        content="read https://example.com/story. and https://other.example",
        user_id="u",
        no_link_preview=False,
    )
    assert got is not None and got.title == "A <b>title"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "not a url", "ftp://x.example"])
async def test_non_web_urls_never_fetched(tmp_path, url):
    svc, fetcher = _svc(tmp_path, {})
    assert await svc.preview_for_url(url, user_id="u") is None
    assert fetcher.calls == []


async def test_page_without_title_or_description_has_no_preview(tmp_path):
    pages = {
        "https://e.example/": (b"<head></head>", "text/html", "https://e.example/")
    }
    svc, _ = _svc(tmp_path, pages)
    assert await svc.preview_for_url("https://e.example/", user_id="u") is None


async def test_image_failures_keep_text_preview(tmp_path):
    page = b"""<head><meta property="og:title" content="T">
    <meta property="og:image" content="https://img.example/x.png"></head>"""
    for image_item in (
        None,  # 404
        (b"<svg/>", "image/png", "https://img.example/x.png"),  # not an image
    ):
        pages: dict = {"https://e.example/": (page, "text/html", "https://e.example/")}
        if image_item is not None:
            pages["https://img.example/x.png"] = image_item
        svc, _ = _svc(tmp_path, pages)
        p = await svc.preview_for_url("https://e.example/", user_id="u")
        assert p is not None and p.title == "T" and p.thumbnail_url is None


async def test_image_with_bad_scheme_not_fetched(tmp_path):
    page = b"""<head><meta property="og:title" content="T">
    <meta property="og:image" content="data:image/png;base64,AAAA"></head>"""
    svc, fetcher = _svc(
        tmp_path, {"https://e.example/": (page, "text/html", "https://e.example/")}
    )
    p = await svc.preview_for_url("https://e.example/", user_id="u")
    assert p is not None and p.thumbnail_url is None
    assert fetcher.calls == ["https://e.example/"]


async def test_image_store_failure_keeps_text_preview(tmp_path):
    blocker = tmp_path / "media"
    blocker.write_text("a file where the dir should be")
    fetcher = FakeFetcher(_default_pages())
    svc = LinkPreviewService(
        fetcher=fetcher,  # type: ignore[arg-type]
        image_processor=ImageProcessor(),
        media_dir=blocker,
    )
    p = await svc.preview_for_url("https://example.com/story", user_id="u")
    assert p is not None and p.thumbnail_url is None


async def test_canonical_on_another_host_is_ignored(tmp_path):
    page = b"""<head><meta property="og:title" content="Bank login">
    <meta property="og:url" content="https://bank.example/login"></head>"""
    pages = {"https://phish.example/": (page, "text/html", "https://phish.example/x")}
    svc, _ = _svc(tmp_path, pages)
    p = await svc.preview_for_url("https://phish.example/", user_id="u")
    assert p is not None
    assert p.url == "https://phish.example/x"


def test_card_url_fallbacks() -> None:
    assert (
        lps._card_url("https://a.example/", "not a url", None) == "https://a.example/"
    )


def test_wire_link_preview_filters_image_ref() -> None:
    p = wire_link_preview(
        {
            "url": "https://example.com/",
            "title": "T",
            "thumbnail_url": "https://evil.example/tracker.png",
        }
    )
    assert p == LinkPreview(url="https://example.com/", title="T")
    q = wire_link_preview(
        {
            "url": "https://example.com/",
            "title": "T",
            "thumbnail_url": "/api/media/a.webp",
        }
    )
    assert q is not None and q.thumbnail_url == "api/media/a.webp"
    assert (
        wire_link_preview(
            {
                "url": "https://example.com/",
                "title": "T",
                "thumbnail_url": "api/media/../x",
            }
        ).thumbnail_url
        is None
    )  # type: ignore[union-attr]


async def test_member_over_budget_does_not_spend_the_household_budget(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(lps, "USER_FETCH_LIMIT", 1)
    monkeypatch.setattr(lps, "HOUSEHOLD_FETCH_LIMIT", 3)
    svc, fetcher = _svc(tmp_path, {})
    for i in range(10):
        await svc.preview_for_url(f"https://g{i}.example/", user_id="greedy")
    assert len(fetcher.calls) == 1
    # The household still has room for two other members.
    await svc.preview_for_url("https://o1.example/", user_id="o1")
    await svc.preview_for_url("https://o2.example/", user_id="o2")
    assert len(fetcher.calls) == 3


async def test_page_and_image_share_one_time_budget(tmp_path):
    clock = Clock()
    svc, fetcher = _svc(tmp_path, _default_pages(), clock=clock)
    fetcher.on_fetch = lambda: setattr(clock, "t", clock.t + 1.5)
    p = await svc.preview_for_url("https://example.com/story", user_id="u")
    assert p is not None and p.thumbnail_url
    assert fetcher.budgets[0] == lps.BUILD_BUDGET_S
    assert fetcher.budgets[1] == pytest.approx(lps.BUILD_BUDGET_S - 1.5)


async def test_image_skipped_when_page_spent_the_budget(tmp_path):
    clock = Clock()
    svc, fetcher = _svc(tmp_path, _default_pages(), clock=clock)
    fetcher.on_fetch = lambda: setattr(clock, "t", clock.t + lps.BUILD_BUDGET_S)
    p = await svc.preview_for_url("https://example.com/story", user_id="u")
    assert p is not None and p.title and p.thumbnail_url is None
    assert fetcher.calls == ["https://example.com/story"]
