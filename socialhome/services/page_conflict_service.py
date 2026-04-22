"""Page three-way merge + conflict resolution (§4.4.4.1).

Pages are the only content type where last-write-wins is too lossy —
offline edits from different members can both be meaningful. When a
federation sync produces two concurrent bodies for the same page, the
service:

1. Runs a paragraph-level diff3 merge against the last common
   ancestor (LCA) snapshot.
2. If every paragraph reconciled without conflict, applies the merged
   body silently.
3. Otherwise stores both bodies in :table:`space_page_snapshots` with
   ``conflict=1``. Further edits are blocked until
   :meth:`resolve_conflict` is called with
   ``"mine" | "theirs" | "merged_content"``.

The merge intentionally operates on paragraph blocks (two or more
newlines) rather than lines — pages are Markdown, and whole-block
granularity keeps merge conflicts to the unit the UI shows anyway.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ..repositories.page_repo import (
    AbstractPageRepo,
    PageNotFoundError,
)

log = logging.getLogger(__name__)


# ─── Errors ──────────────────────────────────────────────────────────────


class PageConflictError(Exception):
    """Base class for conflict-resolution errors."""


class NoActiveConflictError(PageConflictError):
    """``resolve_conflict`` called but the page has no open conflict."""


# ─── Diff3 merge ─────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class MergeResult:
    """Outcome of a three-way merge."""

    #: The fully merged body. Contains conflict markers when
    #: ``has_conflict`` is ``True``.
    content: str

    #: Whether any paragraph failed to merge cleanly.
    has_conflict: bool


#: Marker strings used when a paragraph triple cannot auto-merge.
CONFLICT_HEAD = "<<<<<<< mine"
CONFLICT_SEP = "======="
CONFLICT_TAIL = ">>>>>>> theirs"


def _split_paragraphs(body: str) -> list[str]:
    """Split ``body`` into paragraph blocks.

    A paragraph boundary is two or more consecutive newlines. Trailing
    blank lines are dropped so two bodies that differ only in terminal
    whitespace merge cleanly.
    """
    if not body:
        return []
    # Normalise line endings, strip trailing whitespace.
    normalised = body.replace("\r\n", "\n").rstrip()
    # Split on any run of blank lines.
    parts = re.split(r"\n\s*\n", normalised)
    # Preserve internal structure — drop empty items from leading runs.
    return [p for p in parts if p.strip()]


def _join_paragraphs(parts: list[str]) -> str:
    return "\n\n".join(parts)


def diff3_merge(base: str, mine: str, theirs: str) -> MergeResult:
    """Paragraph-level diff3 merge.

    For each paragraph position across ``base``/``mine``/``theirs``:

    * Both sides match base → keep base.
    * Only one side changed → take that side.
    * Both sides made the same change → take it once.
    * Both sides changed and disagree → emit conflict markers.

    Added paragraphs (past the end of base) by one side are appended
    verbatim; added paragraphs by both sides concatenate with a blank
    line between. Deletions on one side win when the other side keeps
    the base paragraph.
    """
    b = _split_paragraphs(base)
    m = _split_paragraphs(mine)
    t = _split_paragraphs(theirs)

    merged: list[str] = []
    has_conflict = False

    # Align by index. Extend shorter sides with a sentinel so the loop
    # can treat "missing" uniformly.
    n = max(len(b), len(m), len(t))
    SENTINEL = object()

    def _at(xs: list[str], i: int):
        return xs[i] if i < len(xs) else SENTINEL

    for i in range(n):
        bp = _at(b, i)
        mp = _at(m, i)
        tp = _at(t, i)

        # If either side dropped the paragraph entirely and the other
        # kept base, honour the deletion. Parallel deletions: drop it.
        if mp is SENTINEL and tp is SENTINEL:
            continue
        if mp is SENTINEL:
            if bp is SENTINEL:
                # Past base on both ours + base — theirs appended a new
                # paragraph. Take it.
                merged.append(tp)
                continue
            if tp == bp:
                # Deleted on our side, unchanged on theirs → delete.
                continue
            # They changed it, we deleted it → conflict.
            has_conflict = True
            merged.append(_conflict_block("", tp if isinstance(tp, str) else ""))
            continue
        if tp is SENTINEL:
            if bp is SENTINEL:
                # Past base on theirs + base — we appended a new
                # paragraph. Take it.
                merged.append(mp)
                continue
            if mp == bp:
                # Deleted on their side, unchanged on ours → delete.
                continue
            has_conflict = True
            merged.append(_conflict_block(mp if isinstance(mp, str) else "", ""))
            continue

        # All three present.
        if mp == tp:
            merged.append(mp)  # identical edits / same keep
        elif mp == bp:
            merged.append(tp)  # theirs changed only
        elif tp == bp:
            merged.append(mp)  # mine changed only
        else:
            has_conflict = True
            merged.append(_conflict_block(mp, tp))

    return MergeResult(content=_join_paragraphs(merged), has_conflict=has_conflict)


def _conflict_block(mine: str, theirs: str) -> str:
    return f"{CONFLICT_HEAD}\n{mine}\n{CONFLICT_SEP}\n{theirs}\n{CONFLICT_TAIL}"


# ─── Service ─────────────────────────────────────────────────────────────


class PageConflictService:
    """Record concurrent-edit conflicts and resolve them on demand."""

    __slots__ = ("_pages",)

    def __init__(self, page_repo: AbstractPageRepo) -> None:
        self._pages = page_repo

    # ─── Snapshot + conflict bookkeeping ──────────────────────────────────

    async def record_base(
        self,
        *,
        page_id: str,
        space_id: str | None,
        body: str,
        author_user_id: str,
    ) -> None:
        """Mark the current body as the last-common-ancestor for future merges."""
        await self._pages.insert_snapshot(
            page_id=page_id,
            space_id=space_id,
            body=body,
            author_user_id=author_user_id,
            side="base",
            conflict=False,
        )

    async def has_active_conflict(self, page_id: str) -> bool:
        return await self._pages.has_active_conflict(page_id)

    # ─── Main entry point: merge remote body ──────────────────────────────

    async def merge_remote_body(
        self,
        *,
        page_id: str,
        space_id: str | None,
        remote_body: str,
        remote_author_user_id: str,
    ) -> MergeResult:
        """Attempt an automatic diff3 merge against the local current body.

        If the merge is clean, the local page is updated silently (and a
        new base snapshot is recorded). Otherwise the service stores
        both sides as conflicting snapshots and leaves the local body
        untouched — the UI will surface the conflict and ask a user to
        resolve it via :meth:`resolve_conflict`.
        """
        page = await self._pages.get(page_id)
        if page is None:
            raise PageNotFoundError(page_id)

        base_body = await self._pages.last_base_snapshot(page_id)
        mine_body = page.content

        result = diff3_merge(base_body, mine_body, remote_body)

        if not result.has_conflict:
            # Clean merge → apply silently + refresh base.
            updated = replace(
                page,
                content=result.content,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            await self._pages.save(updated)
            await self.record_base(
                page_id=page_id,
                space_id=space_id,
                body=result.content,
                author_user_id=remote_author_user_id,
            )
            return result

        # Conflict — store both sides. Subsequent edits should be blocked
        # by the route layer until resolve_conflict() is called.
        await self._pages.insert_snapshot(
            page_id=page_id,
            space_id=space_id,
            body=mine_body,
            author_user_id=page.created_by,
            side="mine",
            conflict=True,
        )
        await self._pages.insert_snapshot(
            page_id=page_id,
            space_id=space_id,
            body=remote_body,
            author_user_id=remote_author_user_id,
            side="theirs",
            conflict=True,
        )
        log.info("page %s: stored conflict (mine vs theirs)", page_id)
        return result

    # ─── Resolve ──────────────────────────────────────────────────────────

    async def resolve_conflict(
        self,
        *,
        space_id: str,
        page_id: str,
        user_id: str,
        resolution: str,  # "mine" | "theirs" | "merged_content"
        merged_content: str | None = None,
    ) -> str:
        """Apply the chosen version and clear the conflict flag.

        Returns the body that is now current. Raises
        :class:`NoActiveConflictError` if the page has no open conflict,
        and :class:`ValueError` if ``resolution`` isn't one of the
        three spec values.
        """
        if resolution not in ("mine", "theirs", "merged_content"):
            raise ValueError(f"Unknown resolution: {resolution!r}")

        if not await self.has_active_conflict(page_id):
            raise NoActiveConflictError(f"page {page_id!r} has no unresolved conflict")

        page = await self._pages.get(page_id)
        if page is None:
            raise PageNotFoundError(page_id)

        if resolution == "mine":
            new_body = page.content
        elif resolution == "theirs":
            new_body = await self._pages.last_theirs_snapshot(page_id) or page.content
        else:
            if not merged_content:
                raise ValueError("merged_content required for 'merged_content'")
            new_body = merged_content

        # Apply.
        updated = replace(
            page,
            content=new_body,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._pages.save(updated)

        # Clear the conflict flag and stamp a fresh base.
        await self._pages.clear_conflict_flag(page_id)
        await self.record_base(
            page_id=page_id,
            space_id=space_id,
            body=new_body,
            author_user_id=user_id,
        )
        return new_body
