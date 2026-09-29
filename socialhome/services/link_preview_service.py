"""Build link previews on the author's household — once, server-side.

A post that contains a web link gets a :class:`LinkPreview` card. Only the
household of the member who **writes** the post ever fetches the URL: the
preview is built here when the post is created and then travels inside the
post (encrypted for a space post, like its text), so households that
receive the post render the card without contacting the site.

The service is the **source of truth** for preview fields on the author's
side. The composer's live preview (``POST /api/link-preview``) and the post
create path both go through :meth:`LinkPreviewService.preview_for_url`; a
client can only say *whether* it wants a preview (``no_link_preview``),
never *what* it says. The composer call warms the cache, so the create path
normally reuses the result without a second fetch.

Guard rails:

* every request goes through :class:`~socialhome.outbound_fetch.OutboundFetcher`
  (SSRF guard: public addresses only, pinned DNS, ≤ 3 redirects, 5 s,
  512 KiB of HTML, 2 MiB of image);
* results — including "no preview" — are cached per normalised URL for a
  short time, and concurrent requests for one URL share a single fetch;
* fresh fetches are rate-limited per member and per household, so a member
  cannot turn the household into a request cannon;
* the household admin can turn previews off (``allow_link_preview``); the
  composer then shows none and posts carry none;
* the page's image is downloaded, re-encoded to a metadata-free WebP
  (:meth:`ImageProcessor.link_preview`) and stored as ordinary local media,
  so the card never hot-links the site.

Only ``text`` posts in the household feed and in spaces get previews.
Comments and DMs do not: they are conversational, and a DM preview would
make the author's household fetch every link in a private chat.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import aiofiles
import aiofiles.os

from ..domain.link_preview import (
    LINK_PREVIEW_DESCRIPTION_MAX,
    LINK_PREVIEW_SITE_NAME_MAX,
    LINK_PREVIEW_TITLE_MAX,
    LinkPreview,
    clean_text,
    first_url,
    link_preview_from_dict,
    normalise_url,
)
from ..domain.post import PostType
from ..domain.preferences import FeatureDisabledError
from ..outbound_fetch import OutboundFetcher, OutboundFetchRefused
from ..rate_limiter import RateLimiter
from .inbound_media_store import MEDIA_REF_PREFIX, local_media_ref
from .link_preview_html import extract_page_meta

if TYPE_CHECKING:
    from ..media.image_processor import ImageProcessor
    from .preferences_service import PreferencesService

log = logging.getLogger(__name__)

#: HTML read per page — the ``<head>`` is at the top; the rest is cut.
HTML_MAX_BYTES: int = 512 * 1024
#: Largest preview image downloaded.
IMAGE_MAX_BYTES: int = 2 * 1024 * 1024

HTML_TYPES: frozenset[str] = frozenset({"text/html", "application/xhtml+xml"})
IMAGE_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)

#: Wall-clock budget for building one preview — the page AND its image
#: share it, so a post create never waits longer than this on the network.
BUILD_BUDGET_S: float = 5.0
#: Below this much budget left, the image is skipped (text card only).
IMAGE_MIN_BUDGET_S: float = 0.5

#: How long a built preview is reused.
CACHE_TTL_S: float = 15 * 60
#: How long "this URL has no preview" is remembered.
NEGATIVE_TTL_S: float = 2 * 60
#: Cached URLs kept (oldest evicted first).
CACHE_MAX_ENTRIES: int = 256

#: Fresh fetches one member may cause per window.
USER_FETCH_LIMIT: int = 20
#: Fresh fetches the whole household may cause per window.
HOUSEHOLD_FETCH_LIMIT: int = 60
FETCH_WINDOW_S: int = 5 * 60

#: Post types whose text is scanned for a link.
PREVIEW_POST_TYPES: frozenset[PostType] = frozenset({PostType.TEXT})


def wire_link_preview(raw: object) -> LinkPreview | None:
    """A preview another household sent (or a stored / queued one), checked.

    Every field is re-validated; the image survives only as a local
    ``api/media/<name>`` reference (the bytes follow as a media blob that is
    matched against it), never as a remote URL.
    """
    return link_preview_from_dict(raw, image_ref=local_media_ref)


class LinkPreviewService:
    """Fetch, cache and rate-limit link previews (see the module docstring)."""

    __slots__ = (
        "_fetcher",
        "_images",
        "_media_dir",
        "_preferences",
        "_limiter",
        "_clock",
        "_cache",
        "_inflight",
    )

    def __init__(
        self,
        *,
        fetcher: OutboundFetcher,
        image_processor: "ImageProcessor",
        media_dir: pathlib.Path,
        preferences: "PreferencesService | None" = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self._images = image_processor
        self._media_dir = media_dir
        self._preferences = preferences
        self._clock = clock
        self._limiter = RateLimiter(monotonic=clock)
        self._cache: OrderedDict[str, tuple[float, LinkPreview | None]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[LinkPreview | None]] = {}

    # ── Public API ────────────────────────────────────────────────────

    async def enabled(self) -> bool:
        """``True`` unless the household admin turned previews off."""
        if self._preferences is None:
            return True
        prefs = await self._preferences.get_household()
        return bool(prefs.allow_link_preview)

    async def preview_for_url(self, url: str, *, user_id: str) -> LinkPreview | None:
        """The preview for *url*, from the cache or one guarded fetch.

        ``None`` when the URL is not a plain web link, has nothing to show,
        could not be fetched, or the member is over the fetch budget.
        Raises :class:`FeatureDisabledError` when previews are off.
        """
        if not await self.enabled():
            raise FeatureDisabledError("link_preview")
        key = normalise_url(url)
        if key is None:
            return None
        hit = await self._cached(key)
        if hit is not _MISS:
            return hit  # type: ignore[return-value]
        task = self._inflight.get(key)
        if task is None:
            if not self._allow_fetch(user_id):
                log.info("link preview: fetch budget spent (user=%s)", user_id)
                return None
            task = asyncio.create_task(self._build_and_cache(key))
            self._inflight[key] = task
            task.add_done_callback(self._forget_inflight)
        return await asyncio.shield(task)

    def _forget_inflight(self, task: "asyncio.Task[LinkPreview | None]") -> None:
        for key, pending in list(self._inflight.items()):
            if pending is task:
                del self._inflight[key]

    async def preview_for_post(
        self,
        *,
        post_type: PostType,
        content: str | None,
        user_id: str,
        no_link_preview: bool,
    ) -> LinkPreview | None:
        """The preview a new post should carry, or ``None``. Never raises —
        a preview is decoration and never blocks the post."""
        if no_link_preview or post_type not in PREVIEW_POST_TYPES:
            return None
        url = first_url(content)
        if url is None:
            return None
        try:
            return await self.preview_for_url(url, user_id=user_id)
        except FeatureDisabledError:
            return None
        except Exception:  # pragma: no cover — defensive, never block a post
            log.warning("link preview for a new post failed", exc_info=True)
            return None

    # ── Cache ─────────────────────────────────────────────────────────

    async def _cached(self, key: str) -> LinkPreview | None | object:
        entry = self._cache.get(key)
        if entry is None:
            return _MISS
        expires, preview = entry
        if expires <= self._clock():
            self._cache.pop(key, None)
            return _MISS
        # The image file may have been removed with the last post that
        # used it — rebuild rather than hand out a dangling reference.
        if preview is not None and preview.thumbnail_url:
            name = preview.thumbnail_url.removeprefix(MEDIA_REF_PREFIX)
            if not await aiofiles.os.path.isfile(self._media_dir / name):
                self._cache.pop(key, None)
                return _MISS
        self._cache.move_to_end(key)
        return preview

    def _store(self, key: str, preview: LinkPreview | None) -> None:
        ttl = CACHE_TTL_S if preview is not None else NEGATIVE_TTL_S
        self._cache[key] = (self._clock() + ttl, preview)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    def _allow_fetch(self, user_id: str) -> bool:
        # The member's own budget first: a member who is over it must not
        # keep spending the household's (and switch previews off for all).
        if not self._limiter.is_allowed(
            f"link_preview:user:{user_id}",
            limit=USER_FETCH_LIMIT,
            window_s=FETCH_WINDOW_S,
        ):
            return False
        return self._limiter.is_allowed(
            "link_preview:household",
            limit=HOUSEHOLD_FETCH_LIMIT,
            window_s=FETCH_WINDOW_S,
        )

    async def _build_and_cache(self, key: str) -> LinkPreview | None:
        preview = await self._build(key)
        self._store(key, preview)
        if preview is not None:
            # Host only — the path / query of a member's link stay out of logs.
            log.info(
                "link preview: built (host=%s, image=%s)",
                urlsplit(key).hostname,
                "yes" if preview.thumbnail_url else "no",
            )
        return preview

    # ── Fetch + extract ───────────────────────────────────────────────

    async def _build(self, url: str) -> LinkPreview | None:
        deadline = self._clock() + BUILD_BUDGET_S
        try:
            page = await self._fetcher.fetch(
                url,
                accept=HTML_TYPES,
                max_bytes=HTML_MAX_BYTES,
                truncate=True,
                timeout_s=BUILD_BUDGET_S,
            )
        except OutboundFetchRefused as exc:
            log.info(
                "link preview: not fetched (host=%s, reason=%s)",
                urlsplit(url).hostname,
                exc.reason,
            )
            return None
        meta = await asyncio.to_thread(
            extract_page_meta, page.body, page.charset, page.url
        )
        title = clean_text(meta.title, limit=LINK_PREVIEW_TITLE_MAX)
        description = clean_text(meta.description, limit=LINK_PREVIEW_DESCRIPTION_MAX)
        if title is None and description is None:
            return None
        link = _card_url(url, page.url, meta.canonical_url)
        thumbnail = await self._store_image(
            meta.image_url, budget_s=deadline - self._clock()
        )
        return wire_link_preview(
            {
                "url": link,
                "title": title,
                "description": description,
                "site_name": clean_text(
                    meta.site_name, limit=LINK_PREVIEW_SITE_NAME_MAX
                ),
                "thumbnail_url": thumbnail,
            }
        )

    async def _store_image(
        self, image_url: str | None, *, budget_s: float
    ) -> str | None:
        """Download, re-encode and store the page's image; its local ref."""
        src = normalise_url(image_url)
        if src is None:
            return None
        if budget_s < IMAGE_MIN_BUDGET_S:
            log.info("link preview image: skipped, page used the time budget")
            return None
        try:
            res = await self._fetcher.fetch(
                src, accept=IMAGE_TYPES, max_bytes=IMAGE_MAX_BYTES, timeout_s=budget_s
            )
            webp = await self._images.link_preview(res.body)
        except OutboundFetchRefused as exc:
            log.info(
                "link preview image: not fetched (host=%s, reason=%s)",
                urlsplit(src).hostname,
                exc.reason,
            )
            return None
        except ValueError as exc:
            log.info("link preview image: refused (%s)", exc)
            return None
        name = f"{uuid.uuid4().hex}.webp"
        try:
            await aiofiles.os.makedirs(self._media_dir, exist_ok=True)
            async with aiofiles.open(self._media_dir / name, "wb") as fh:
                await fh.write(webp)
        except OSError as exc:
            log.warning("link preview image: could not store (%s)", exc)
            return None
        return f"{MEDIA_REF_PREFIX}{name}"


_MISS = object()


def _card_url(requested: str, final: str, canonical: str | None) -> str:
    """The URL the card opens: the page's canonical URL only when it stays
    on the host that actually answered (a page can't make its card claim
    another site), else the answering URL, else what the member wrote."""
    final_norm = normalise_url(final)
    final_host = urlsplit(final_norm).hostname if final_norm else None
    canon = normalise_url(canonical)
    if canon is not None and final_host and urlsplit(canon).hostname == final_host:
        return canon
    return final_norm or requested
