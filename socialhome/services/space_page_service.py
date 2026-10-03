"""Space pages — the wiki tab of a space (§4.4.4, §24.11).

Every read and write takes the caller's (path) ``space_id`` and only ever
reaches that space's pages: a page id of another space or of the household
is :class:`KeyError` (→ 404, never 403, so the answer doesn't confirm the id
exists elsewhere). Membership of the path space and the per-space ``pages``
feature toggle are checked by the route first; this service owns the rest:

* :meth:`SpacePageService.require_writer` — a write needs a non-archived
  space and a writable seat (subscribers read, never write);
* every write passes the space's ``pages`` access level for the acting
  user (§4.3, :class:`ContentAccessMixin`) — under ``ADMIN_ONLY`` only the
  owner / admins create, edit, resolve conflicts on or delete pages; under
  ``MODERATED`` a member's new page, and their edit / delete / conflict
  resolution of somebody else's, waits in the space moderation queue
  (:class:`PageModerationHandler` replays it on approval);
* pages are **host-sequenced** (v_48,
  :class:`~.page_conflict_service.PageConflictService`): on the space's
  host an edit is committed as the next canonical version and broadcast;
  on a member household of a v_48 host it is an optimistic draft the
  :class:`~.page_proposal_forwarder.PageProposalForwarder` proposes to the
  host; under an older host, last write wins as before. A conflict never
  blocks an edit; resolving one retires every open side;
* each write publishes :class:`PageCreated` / :class:`PageUpdated` /
  :class:`PageDeleted` with the actor, which ``PageFederationOutbound``
  federates (a member's draft is a ``proposal`` — never broadcast).

Bodies are stored canonical: a signed ``/api/media/…?exp=&sig=`` URL the
editor echoed back is stripped on save, and re-signed on each read by the
route.
"""

from __future__ import annotations

import builtins
import contextlib
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..domain.events import PageCreated, PageDeleted, PageUpdated
from ..domain.page import MAX_PAGE_TITLE_LENGTH, Page, PageVersion
from ..domain.page_version import PageConflictSide, is_version_hash, version_hash
from ..domain.space import (
    AccessDecision,
    ContentAction,
    ModerationStaleError,
    ModerationTargetGoneError,
    Space,
    SpaceModerationItem,
    SpacePermissionError,
)
from ..media_signer import strip_signature_query, strip_signed_media_in_markdown
from ..repositories.page_repo import mint_page_id, new_page
from .bus_publisher import BusPublisherMixin
from .content_access import ContentAccessMixin
from .page_conflict_service import (
    RESOLUTIONS,
    NoActiveConflictError,
    PageConflictStaleError,
    PageMode,
)
from .space_moderation_service import ApplyResult, item_payload_snapshot
from .space_service import SpaceService

if TYPE_CHECKING:
    from .space_moderation_service import ModerationSubmitter
    from ..infrastructure.event_bus import EventBus
    from ..repositories.page_repo import AbstractPageRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .page_conflict_service import PageConflictService


class _Unset:
    """Sentinel: a field the caller did not send (distinct from ``None``)."""


UNSET = _Unset()


class PageStaleError(Exception):
    """An update based on an ``updated_at`` that is no longer current —
    somebody else saved first. Carries the page as it now is, for the
    side-by-side conflict view (§23.72)."""

    def __init__(self, current: Page) -> None:
        super().__init__("page changed since it was loaded")
        self.current = current


async def snapshot_page_version(
    pages: "AbstractPageRepo", *, previous: Page, editor_user_id: str
) -> None:
    """Persist ``previous`` as a row in ``page_edit_history``.

    Called right before an edit or revert so the history always carries a
    copy of the pre-change state — the versions list then shows "what the
    page looked like before the current live body".
    """
    await pages.save_version(
        PageVersion(
            id=uuid.uuid4().hex,
            page_id=previous.id,
            version=await pages.next_version_number(previous.id),
            title=previous.title,
            content=previous.content,
            edited_by=editor_user_id,
            edited_at=datetime.now(timezone.utc).isoformat(),
            space_id=previous.space_id,
            cover_image_url=previous.cover_image_url,
        )
    )


def _title(value: object) -> str:
    title = str(value or "").strip()
    if not title:
        raise ValueError("page title must not be empty")
    if len(title) > MAX_PAGE_TITLE_LENGTH:
        raise ValueError(
            f"page title must be at most {MAX_PAGE_TITLE_LENGTH} characters"
        )
    return title


class SpacePageService(BusPublisherMixin, ContentAccessMixin):
    """Scoped page operations for one space's wiki."""

    __slots__ = ("_pages", "_spaces", "_bus", "_conflicts", "_moderation")

    def __init__(
        self,
        page_repo: "AbstractPageRepo",
        *,
        space_repo: "AbstractSpaceRepo",
        bus: "EventBus | None" = None,
        conflict_service: "PageConflictService | None" = None,
    ) -> None:
        self._pages = page_repo
        self._spaces = space_repo
        self._bus = bus
        self._conflicts = conflict_service
        self._moderation: "ModerationSubmitter | None" = None

    # ── Writer gate ──────────────────────────────────────────────────────

    async def require_writer(self, space_id: str, user_id: str) -> None:
        """Raise unless ``user_id`` may write this space's pages.

        * unknown / dissolved space → :class:`KeyError` (404);
        * archived space (read-only) → :class:`SpacePermissionError`;
        * not a member, or a read-only subscriber →
          :class:`SpacePermissionError` (403).
        """
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        if space.archived:
            raise SpacePermissionError(
                "space is archived (read-only) — unarchive it to make changes",
            )
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        SpaceService.assert_writable_member(member, action="edit pages", space=space)

    # ── Reads ────────────────────────────────────────────────────────────

    async def list(self, space_id: str) -> builtins.list[Page]:
        return await self._pages.list(space_id=space_id)

    async def get(self, space_id: str, page_id: str) -> Page:
        page = await self._pages.get_space_page(page_id, space_id=space_id)
        if page is None:
            raise KeyError(f"page {page_id!r} not found in this space")
        return page

    async def versions(self, space_id: str, page_id: str) -> builtins.list[PageVersion]:
        """The edit history recorded under this space (read-only)."""
        await self.get(space_id, page_id)
        return await self._pages.list_versions(page_id, space_id=space_id)

    # ── Writes ───────────────────────────────────────────────────────────

    async def create(
        self,
        space_id: str,
        *,
        actor_user_id: str,
        title: object,
        content: str | None = "",
        cover_image_url: str | None = None,
        page_id: str | None = None,
        approved_by: str | None = None,
    ) -> Page:
        """Create a page. ``approved_by`` (the moderation queue's replay)
        gates the write as that approver; ``page_id`` is the id the queued
        item minted. The page stays the actor's."""
        clean_title = _title(title)
        body = strip_signed_media_in_markdown(content or "") or ""
        cover = strip_signature_query(cover_image_url) if cover_image_url else None
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "pages",
            ContentAction.CREATE,
            approved_by is None,
        )
        if decision is AccessDecision.QUEUE:
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "pages",
                ContentAction.CREATE,
                payload={
                    "entity": "page",
                    "target_id": mint_page_id(
                        space_id=space_id, created_by=actor_user_id
                    ),
                    "title": clean_title,
                    "content": body,
                    "cover_image_url": cover,
                },
            )
        page = new_page(
            title=clean_title,
            content=body,
            created_by=actor_user_id,
            space_id=space_id,
            cover_image_url=cover,
            page_id=page_id,
        )
        mode = await self._mode(space_id)
        if mode is PageMode.HOST:
            assert self._conflicts is not None
            # The host's own create is its first canonical version.
            return await self._conflicts.host_create(page, actor_user_id=actor_user_id)
        if mode is PageMode.MEMBER:
            assert self._conflicts is not None
            # A draft until the host sequences it: proposed, never broadcast.
            page = await self._conflicts.member_create(page)
        else:
            await self._pages.save(page, space_id=space_id)
        await self._emit(
            PageCreated(
                page_id=page.id,
                space_id=space_id,
                title=page.title,
                content=page.content,
                actor_user_id=actor_user_id,
                proposal=mode is PageMode.MEMBER,
                base={"base_seq": 0} if mode is PageMode.LEGACY else None,
            )
        )
        return page

    async def _mode(self, space_id: str) -> PageMode:
        """How this household writes the space's pages (v_48): sequences
        them (host), proposes them (member of a v_48 host), or last write
        wins (an older host / no conflict service)."""
        if self._conflicts is None:
            return PageMode.LEGACY
        mode, _host = await self._conflicts.mode(space_id)
        return mode

    async def update(
        self,
        space_id: str,
        page_id: str,
        *,
        actor_user_id: str,
        title: object = UNSET,
        content: object = UNSET,
        cover_image_url: object = UNSET,
        base_updated_at: str | None = None,
        approved_by: str | None = None,
    ) -> Page:
        """Apply the sent fields. ``base_updated_at`` — the ``updated_at``
        the editor loaded — makes a concurrent save a
        :class:`PageStaleError` instead of a silent overwrite."""
        page = await self.get(space_id, page_id)
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "pages",
            ContentAction.EDIT,
            approved_by is None and page.created_by == actor_user_id,
        )
        if base_updated_at and base_updated_at != page.updated_at:
            raise PageStaleError(page)
        patch = _page_patch(
            title=title, content=content, cover_image_url=cover_image_url
        )
        if decision is AccessDecision.QUEUE:
            before = _page_dict(page)
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "pages",
                ContentAction.EDIT,
                payload={
                    "entity": "page",
                    "target_id": page.id,
                    "patch": patch,
                    "base_updated_at": page.updated_at,
                    # v_48: the host judges staleness by its own sequence.
                    "base_seq": page.seq,
                },
                snapshot={k: before[k] for k in patch},
            )
        return await self._write(
            space_id,
            page_id,
            actor_user_id=actor_user_id,
            patch=patch,
            base_updated_at=base_updated_at,
        )

    async def _write(
        self,
        space_id: str,
        page_id: str,
        *,
        actor_user_id: str,
        patch: dict[str, Any],
        base_updated_at: str | None = None,
        resolves: builtins.list[str] | None = None,
        sides_seen: builtins.list[str] | None = None,
    ) -> Page:
        """Persist an edit the gates admitted, by mode (v_48): sequenced
        here on the host, an optimistic draft proposed to a v_48 host, or
        last write wins under an older one."""

        async def _check(current: Page) -> None:
            # Re-checked under the page lock: an inbound version (or another
            # local save) may have landed since the gate.
            if base_updated_at and base_updated_at != current.updated_at:
                raise PageStaleError(current)
            if sides_seen is not None:
                hashes = [s.hash for s in await self.conflict_sides(space_id, page_id)]
                if set(sides_seen) != set(hashes):
                    raise PageConflictStaleError(hashes)

        mode = await self._mode(space_id)
        if mode is PageMode.HOST:
            assert self._conflicts is not None
            # Sequenced and broadcast by the conflict service (it writes
            # the replaced version to history).
            return await self._conflicts.commit_local(
                space_id=space_id,
                page_id=page_id,
                actor_user_id=actor_user_id,
                patch=patch,
                resolves=resolves or (),
                precheck=_check,
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        base: dict | None = None
        async with self._page_lock(space_id, page_id):
            page = await self.get(space_id, page_id)
            await _check(page)
            updated = replace(
                page,
                updated_at=now_iso,
                last_editor_user_id=actor_user_id,
                last_edited_at=now_iso,
                **patch,
            )
            if mode is PageMode.MEMBER:
                assert self._conflicts is not None
                updated = await self._conflicts.member_draft(
                    page,
                    updated,
                    space_id=space_id,
                    actor_user_id=actor_user_id,
                    resolves=resolves or (),
                )
            else:
                base = {
                    "base_seq": page.seq,
                    "base_hash": version_hash(
                        page.title, page.content, page.cover_image_url
                    ),
                }
                await self._pages.save(updated, space_id=space_id)
                # The stored row is the answer: the upsert stamps its own
                # ``updated_at``, and the editor sends exactly that back as
                # the next save's ``base_updated_at`` — echoing ``now_iso``
                # instead made every second save a false 409.
                updated = (
                    await self._pages.get_space_page(page.id, space_id=space_id)
                    or updated
                )
                await snapshot_page_version(
                    self._pages, previous=page, editor_user_id=actor_user_id
                )
                if resolves:
                    await self._pages.clear_conflict_flag(page.id, space_id=space_id)
        await self._emit(
            PageUpdated(
                page_id=updated.id,
                space_id=space_id,
                title=updated.title,
                content=updated.content,
                actor_user_id=actor_user_id,
                proposal=mode is PageMode.MEMBER,
                base=base if mode is PageMode.LEGACY else None,
            )
        )
        return updated

    def _page_lock(
        self, space_id: str, page_id: str
    ) -> contextlib.AbstractAsyncContextManager[Any]:
        """The per-page lock shared with inbound versions (v_48)."""
        if self._conflicts is None:
            return contextlib.nullcontext()
        return self._conflicts.lock_for(space_id, page_id)

    async def conflict_sides(
        self, space_id: str, page_id: str
    ) -> builtins.list[PageConflictSide]:
        """The open conflict's versions (empty: none)."""
        if self._conflicts is None:
            return []
        return await self._conflicts.sides(space_id, page_id)

    async def pages_in_conflict(self, space_id: str) -> set[str]:
        """Ids of this space's pages with an open conflict."""
        if self._conflicts is None:
            return set()
        return await self._pages.space_pages_in_conflict(space_id)

    async def delete(
        self,
        space_id: str,
        page_id: str,
        *,
        actor_user_id: str,
        approved_by: str | None = None,
    ) -> None:
        page = await self.get(space_id, page_id)
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "pages",
            ContentAction.DELETE,
            approved_by is None and page.created_by == actor_user_id,
        )
        if decision is AccessDecision.QUEUE:
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "pages",
                ContentAction.DELETE,
                payload={"entity": "page", "target_id": page.id},
                snapshot=_page_dict(page),
            )
        await self._pages.delete(page.id, space_id=space_id)
        # ``space_id`` is what routes the delete to the space's member
        # households (``PageFederationOutbound`` drops a scope-less one).
        await self._emit(
            PageDeleted(page_id=page.id, space_id=space_id, actor_user_id=actor_user_id)
        )

    async def resolve_conflict(
        self,
        space_id: str,
        page_id: str,
        *,
        actor_user_id: str,
        resolution: str,
        merged_content: str | None = None,
        side: str | None = None,
        sides: builtins.list[str] | None = None,
        approved_by: str | None = None,
    ) -> Page:
        """Settle an open edit conflict (§4.4.4.1) — an EDIT of the page,
        gated like one, that also retires every open side (``resolves``).
        ``side`` names the kept version (``resolution "side"``); ``sides``
        the versions the user saw (409 ``STALE`` when they changed). On a
        member household it is a proposal like any edit. Returns the page
        as it now is."""
        if self._conflicts is None:
            raise RuntimeError("SpacePageService: conflict service not attached")
        if resolution not in RESOLUTIONS:
            raise ValueError(f"Unknown resolution: {resolution!r}")
        page = await self.get(space_id, page_id)
        decision = await self._gate(
            space_id,
            approved_by or actor_user_id,
            "pages",
            ContentAction.EDIT,
            approved_by is None and page.created_by == actor_user_id,
        )
        merged = (
            strip_signed_media_in_markdown(merged_content)
            if merged_content is not None
            else None
        )
        open_sides = await self._conflicts.sides(space_id, page_id)
        if not open_sides:
            raise NoActiveConflictError(f"page {page_id!r} has no unresolved conflict")
        hashes = [s.hash for s in open_sides]
        if sides is not None and set(sides) != set(hashes):
            raise PageConflictStaleError(hashes)
        title, content, cover = await self._conflicts.resolution_body(
            page, open_sides, resolution=resolution, side=side, merged_content=merged
        )
        if decision is AccessDecision.QUEUE:
            chosen = next((s for s in open_sides if s.hash == side), None)
            await self._submit_for_review(
                space_id,
                actor_user_id,
                "pages",
                ContentAction.EDIT,
                payload={
                    "entity": "page",
                    "target_id": page.id,
                    "op": "resolve_conflict",
                    "resolution": resolution,
                    "merged_content": merged,
                    "side": side if resolution == "side" else None,
                    # For the reviewer's preview only — the release is
                    # bound to the side's hash.
                    "side_content": chosen.content if chosen is not None else None,
                    "sides": hashes,
                    "base_updated_at": page.updated_at,
                    "base_seq": page.seq,
                },
                snapshot={"content": page.content},
            )
        return await self._write(
            space_id,
            page_id,
            actor_user_id=actor_user_id,
            patch={"title": title, "content": content, "cover_image_url": cover},
            resolves=hashes,
            sides_seen=hashes,
        )


def _page_patch(*, title: object, content: object, cover_image_url: object) -> dict:
    """The sent fields, normalised the way a direct save stores them."""
    patch: dict[str, Any] = {}
    if not isinstance(title, _Unset):
        patch["title"] = _title(title)
    if not isinstance(content, _Unset):
        patch["content"] = strip_signed_media_in_markdown(
            content if isinstance(content, str) else None
        )
    if not isinstance(cover_image_url, _Unset):
        patch["cover_image_url"] = strip_signature_query(
            cover_image_url if isinstance(cover_image_url, str) else None
        )
    return patch


def _page_dict(page: Page) -> dict[str, Any]:
    return {
        "title": page.title,
        "content": page.content,
        "cover_image_url": page.cover_image_url,
        "created_by": page.created_by,
        "updated_at": page.updated_at,
    }


def _seq(value: object) -> int | None:
    """A queue payload's ``base_seq``: a non-negative int, or ``None``."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _hashes(value: object) -> builtins.list[str] | None:
    """A queue payload's ``sides``: a list of version hashes, or ``None``."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(is_version_hash(v) for v in value):
        raise ValueError("sides must be a list of version hashes")
    return [str(v) for v in value]


class PageModerationHandler:
    """Queue items of a space's wiki (``pages`` create / edit / delete, a
    conflict resolution riding ``edit``). Applied on the space's host only.
    An edit whose page moved on since it was submitted (its ``seq``) is
    :class:`ModerationStaleError` (409 ``STALE``) until the approver forces
    it — then its patch fast-forwards. A resolution whose sides changed is
    STALE even when forced."""

    __slots__ = ("_svc",)

    def __init__(self, service: SpacePageService) -> None:
        self._svc = service

    def validate(self, space: Space, payload: dict) -> dict:
        if payload.get("entity") != "page" or not isinstance(
            payload.get("target_id"), str
        ):
            raise ValueError("not a page submission")
        out: dict[str, Any] = {"entity": "page", "target_id": payload["target_id"]}
        if payload.get("op") == "resolve_conflict":
            resolution = payload.get("resolution")
            if resolution not in RESOLUTIONS:
                raise ValueError("unknown conflict resolution")
            side = payload.get("side")
            if resolution == "side" and not is_version_hash(side):
                raise ValueError("a side resolution names the kept version")
            side_content = payload.get("side_content")
            out.update(
                op="resolve_conflict",
                resolution=resolution,
                merged_content=payload.get("merged_content"),
                side=side if resolution == "side" else None,
                side_content=side_content if isinstance(side_content, str) else None,
                sides=_hashes(payload.get("sides")),
                base_updated_at=payload.get("base_updated_at"),
                base_seq=_seq(payload.get("base_seq")),
            )
        elif "patch" in payload:
            raw = payload["patch"]
            if not isinstance(raw, dict) or not raw:
                raise ValueError("an edit needs at least one field")
            out["patch"] = _page_patch(
                title=raw["title"] if "title" in raw else UNSET,
                content=raw["content"] if "content" in raw else UNSET,
                cover_image_url=(
                    raw["cover_image_url"] if "cover_image_url" in raw else UNSET
                ),
            )
            out["base_updated_at"] = payload.get("base_updated_at")
            out["base_seq"] = _seq(payload.get("base_seq"))
        elif "title" in payload:
            out["title"] = _title(payload["title"])
            out["content"] = strip_signed_media_in_markdown(
                payload.get("content") or ""
            )
            cover = payload.get("cover_image_url")
            out["cover_image_url"] = (
                strip_signature_query(cover) if isinstance(cover, str) else None
            )
        return out

    async def snapshot(self, space_id: str, target_id: str) -> dict | None:
        try:
            page = await self._svc.get(space_id, target_id)
        except KeyError:
            return None
        return _page_dict(page)

    async def apply(
        self, item: SpaceModerationItem, *, approved_by: str, force: bool
    ) -> ApplyResult:
        p = item.payload
        target = str(p["target_id"])
        live = await self.snapshot(item.space_id, target)
        match item.action:
            case ContentAction.CREATE.value:
                if live is None:
                    await self._svc.create(
                        item.space_id,
                        actor_user_id=item.submitted_by,
                        title=p["title"],
                        content=p.get("content", ""),
                        cover_image_url=p.get("cover_image_url"),
                        page_id=target,
                        approved_by=approved_by,
                    )
            case ContentAction.EDIT.value:
                if live is None:
                    raise ModerationTargetGoneError(target)
                if p.get("op") != "resolve_conflict" and not force:
                    await self._refuse_stale_edit(item, live)
                if p.get("op") == "resolve_conflict":
                    try:
                        await self._svc.resolve_conflict(
                            item.space_id,
                            target,
                            actor_user_id=item.submitted_by,
                            resolution=p["resolution"],
                            merged_content=p.get("merged_content"),
                            side=p.get("side"),
                            sides=p.get("sides"),
                            approved_by=approved_by,
                        )
                    except NoActiveConflictError as exc:
                        raise ModerationTargetGoneError(target) from exc
                    except PageConflictStaleError as exc:
                        raise ModerationStaleError(
                            current={"sides": exc.sides},
                            proposed={"sides": p.get("sides")},
                            base=item_payload_snapshot(item) or {},
                        ) from exc
                else:
                    await self._svc.update(
                        item.space_id,
                        target,
                        actor_user_id=item.submitted_by,
                        approved_by=approved_by,
                        **p["patch"],
                    )
            case ContentAction.DELETE.value:
                if live is not None:
                    await self._svc.delete(
                        item.space_id,
                        target,
                        actor_user_id=item.submitted_by,
                        approved_by=approved_by,
                    )
        return ApplyResult(target_id=target)

    async def _refuse_stale_edit(self, item: SpaceModerationItem, live: dict) -> None:
        """An edit whose page moved on since it was submitted is
        :class:`ModerationStaleError` until the approver forces it — then
        the item's patch fast-forwards over the current version (never a
        merge: the approval is bound to exactly that patch). The v_48 check
        is the host's ``seq``; an older item falls back to ``updated_at``.
        A resolution is judged by the ``sides`` it settles instead."""
        p = item.payload
        base_seq = p.get("base_seq")
        if isinstance(base_seq, int):
            page = await self._svc.get(item.space_id, str(p["target_id"]))
            moved = page.seq != base_seq
        else:
            base = p.get("base_updated_at")
            moved = bool(base) and live["updated_at"] != base
        if moved:
            proposed = dict(p.get("patch") or {})
            raise ModerationStaleError(
                current={k: live.get(k) for k in (proposed or live)},
                proposed=proposed,
                base=item_payload_snapshot(item) or {},
            )

    def preview(self, item: SpaceModerationItem) -> dict:
        p = item.payload
        if item.action == ContentAction.DELETE.value:
            return item_payload_snapshot(item) or {}
        if item.action == ContentAction.EDIT.value:
            if p.get("op") == "resolve_conflict":
                return {
                    "resolution": p.get("resolution"),
                    "content": p.get("merged_content")
                    if p.get("resolution") == "merged_content"
                    else p.get("side_content"),
                }
            return dict(p.get("patch") or {})
        return {
            "title": p.get("title"),
            "content": p.get("content"),
            "cover_image_url": p.get("cover_image_url"),
        }
