"""Requester-side chunk handler.

:class:`SpaceSyncReceiver.on_chunk` is plugged into each inbound
:class:`SyncRtcSession` when the session flips to requester mode. It
parses the wire bytes, verifies the outer signature against the
peer's ``remote_identity_pk``, decrypts the payload with the space
content key (AAD = ``space_id:epoch:sync_id``), and dispatches by
``resource`` to one of the per-resource persist paths.

Persistence uses ``INSERT OR IGNORE`` semantics via the `save`
methods each repo exposes — duplicate chunks from retries are
harmless.

On the ``__complete__`` sentinel the receiver publishes
:class:`SpaceSyncComplete` so admin UI + realtime layers see the
catch-up finished.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import orjson as _orjson

from ....domain.calendar import CalendarEvent
from ....domain.events import SpaceSyncComplete
from ....domain.page import Page
from ....domain.post import (
    Comment,
    CommentType,
    FileMeta,
    Post,
    PostType,
)
from ....domain.space import SpaceMember, SpaceZone
from ....domain.sticky import Sticky
from ....domain.task import RecurrenceRule, Task, TaskStatus
from ....infrastructure.event_bus import EventBus
from .exporter import ALLOWED_RESOURCES, SENTINEL_RESOURCE, parse_chunk

if TYPE_CHECKING:
    from ....repositories.calendar_repo import AbstractSpaceCalendarRepo
    from ....repositories.federation_repo import AbstractFederationRepo
    from ....repositories.gallery_repo import AbstractGalleryRepo
    from ....repositories.page_repo import AbstractPageRepo
    from ....repositories.space_post_repo import AbstractSpacePostRepo
    from ....repositories.space_repo import AbstractSpaceRepo
    from ....repositories.space_zone_repo import AbstractSpaceZoneRepo
    from ....repositories.sticky_repo import AbstractStickyRepo
    from ....repositories.task_repo import AbstractSpaceTaskRepo
    from ....services.space_crypto_service import SpaceContentEncryption
    from ...encoder import FederationEncoder

log = logging.getLogger(__name__)


def _parse_iso(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


class SpaceSyncReceiver:
    """Persist inbound space-sync chunks."""

    __slots__ = (
        "_bus",
        "_encoder",
        "_crypto",
        "_federation_repo",
        "_space_repo",
        "_space_post_repo",
        "_space_task_repo",
        "_page_repo",
        "_sticky_repo",
        "_space_calendar_repo",
        "_gallery_repo",
        "_zone_repo",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        encoder: "FederationEncoder",
        crypto: "SpaceContentEncryption",
        federation_repo: "AbstractFederationRepo",
        space_repo: "AbstractSpaceRepo",
        space_post_repo: "AbstractSpacePostRepo",
        space_task_repo: "AbstractSpaceTaskRepo",
        page_repo: "AbstractPageRepo",
        sticky_repo: "AbstractStickyRepo",
        space_calendar_repo: "AbstractSpaceCalendarRepo",
        gallery_repo: "AbstractGalleryRepo",
        zone_repo: "AbstractSpaceZoneRepo | None" = None,
    ) -> None:
        self._bus = bus
        self._encoder = encoder
        self._crypto = crypto
        self._federation_repo = federation_repo
        self._space_repo = space_repo
        self._space_post_repo = space_post_repo
        self._space_task_repo = space_task_repo
        self._page_repo = page_repo
        self._sticky_repo = sticky_repo
        self._space_calendar_repo = space_calendar_repo
        self._gallery_repo = gallery_repo
        self._zone_repo = zone_repo

    async def on_chunk(
        self,
        raw: bytes | str,
        *,
        from_instance: str,
    ) -> None:
        """Handle one DataChannel frame. All failure modes log + return —
        the federation service has already verified the peer is paired."""
        try:
            envelope = parse_chunk(raw)
        except ValueError as exc:
            log.warning("sync chunk parse failed from %s: %s", from_instance, exc)
            return

        resource = str(envelope.get("resource") or "")
        sync_id = str(envelope.get("sync_id") or "")
        space_id = str(envelope.get("space_id") or "")
        if not resource or not sync_id or not space_id:
            log.debug("sync chunk missing required outer fields")
            return

        # Signature verification — peer's identity key from the federation
        # repo; allows tampered sessions to be dropped here rather than
        # later on the persist path.
        peer = await self._federation_repo.get_instance(from_instance)
        if peer is None:
            log.debug("sync chunk from unknown instance %s — dropping", from_instance)
            return

        signatures = envelope.get("signatures") or {}
        sig_suite = peer.sig_suite
        envelope_for_verify = {k: v for k, v in envelope.items() if k != "signatures"}
        bytes_for_verify = _orjson.dumps(envelope_for_verify)
        pq_pk_hex = peer.remote_pq_identity_pk
        pq_pk = bytes.fromhex(pq_pk_hex) if pq_pk_hex else None
        ok = self._encoder.verify_signatures_all(
            bytes_for_verify,
            suite=sig_suite,
            signatures=signatures,
            ed_public_key=bytes.fromhex(peer.remote_identity_pk),
            pq_public_key=pq_pk,
        )
        if not ok:
            log.warning(
                "sync chunk signature mismatch (sync_id=%s resource=%s)",
                sync_id,
                resource,
            )
            return

        # Sentinel path — publish end-of-stream + return.
        if resource == SENTINEL_RESOURCE:
            await self._bus.publish(
                SpaceSyncComplete(
                    space_id=space_id,
                    from_instance=from_instance,
                )
            )
            return

        if resource not in ALLOWED_RESOURCES:
            log.debug("unknown resource %r in sync chunk", resource)
            return

        # Decrypt.
        epoch = int(envelope.get("epoch") or 0)
        ciphertext = str(envelope.get("encrypted_payload") or "")
        try:
            plaintext = await self._crypto.decrypt_chunk(
                space_id=space_id,
                epoch=epoch,
                sync_id=sync_id,
                ciphertext=ciphertext,
            )
        except Exception as exc:
            log.warning(
                "sync chunk decrypt failed (sync_id=%s resource=%s): %s",
                sync_id,
                resource,
                exc,
            )
            return

        try:
            records = _orjson.loads(plaintext).get("records") or []
        except Exception as exc:
            log.warning("sync chunk plaintext parse failed: %s", exc)
            return

        try:
            await self._dispatch(resource, space_id, records)
        except Exception:  # pragma: no cover
            log.exception(
                "sync chunk persist failed (resource=%s space=%s)",
                resource,
                space_id,
            )

    async def _dispatch(
        self,
        resource: str,
        space_id: str,
        records: list[dict[str, Any]],
    ) -> None:
        if resource == "members":
            for r in records:
                await self._space_repo.save_member(
                    SpaceMember(
                        space_id=space_id,
                        user_id=str(r.get("user_id") or ""),
                        role=str(r.get("role") or "member"),
                        joined_at=str(r.get("joined_at") or ""),
                        history_visible_from=r.get("history_visible_from"),
                        location_share_enabled=bool(
                            r.get("location_share_enabled", False)
                        ),
                        space_display_name=r.get("space_display_name"),
                    )
                )
        elif resource == "bans":
            for r in records:
                user_id = str(r.get("user_id") or "")
                banned_by = str(r.get("banned_by") or "")
                if not user_id or not banned_by:
                    continue
                await self._space_repo.ban_member(
                    space_id=space_id,
                    user_id=user_id,
                    banned_by=banned_by,
                    reason=r.get("reason"),
                )
        elif resource == "posts":
            for r in records:
                post = _post_from_record(r)
                if post is not None:
                    await self._space_post_repo.save(space_id, post)
        elif resource == "comments":
            for r in records:
                comment = _comment_from_record(r)
                if comment is not None:
                    await self._space_post_repo.add_comment(comment)
        elif resource in ("tasks", "tasks_archived"):
            for r in records:
                task = _task_from_record(r)
                if task is not None:
                    await self._space_task_repo.save(space_id, task)
        elif resource == "pages":
            for r in records:
                page = _page_from_record(r, space_id)
                if page is not None:
                    await self._page_repo.save(page)
        elif resource == "stickies":
            for r in records:
                sticky = _sticky_from_record(r, space_id)
                if sticky is not None:
                    await self._sticky_repo.save(sticky)
        elif resource == "calendar":
            for r in records:
                event = _calendar_from_record(r)
                if event is not None:
                    await self._space_calendar_repo.save_event(space_id, event)
        elif resource == "gallery":
            # Albums first, then items — preserve the exporter's order.
            for r in records:
                kind = r.get("kind")
                if kind == "album":
                    await self._persist_album(r)
                elif kind == "item":
                    await self._persist_gallery_item(r)
        elif resource == "polls":
            # v1: polls ride along with posts (Post.poll field). The
            # standalone polls stream is informational — nothing to
            # persist here yet.
            log.debug(
                "received %d poll records — skipped (see Post.poll)", len(records)
            )
        elif resource == "space_zones":
            # §23.8.7: per-space zone catalogue. Receiver may be
            # configured without a zone repo (older deployments) — in
            # that case skip rather than error.
            if self._zone_repo is None:
                log.debug(
                    "received %d zone records — no zone_repo wired, skipping",
                    len(records),
                )
                return
            for r in records:
                zone = _zone_from_record(r, space_id)
                if zone is not None:
                    await self._zone_repo.upsert(zone)

    async def _persist_album(self, record: dict[str, Any]) -> None:
        from ....domain.gallery import (
            GalleryAlbum,
        )  # local to avoid cycle at module load

        album = GalleryAlbum(
            id=str(record["id"]),
            space_id=record.get("space_id"),
            owner_user_id=str(
                record.get("owner_user_id") or record.get("owner_id") or ""
            ),
            name=str(record.get("name") or ""),
            description=record.get("description"),
            cover_item_id=record.get("cover_item_id"),
            item_count=int(record.get("item_count") or 0),
            retention_exempt=bool(record.get("retention_exempt", False)),
            created_at=record.get("created_at"),
        )
        try:
            await self._gallery_repo.create_album(album)
        except Exception:  # pragma: no cover
            # already exists → INSERT OR IGNORE-equivalent.
            pass

    async def _persist_gallery_item(self, record: dict[str, Any]) -> None:
        from ....domain.gallery import GalleryItem

        item = GalleryItem(
            id=str(record["id"]),
            album_id=str(record.get("album_id") or ""),
            uploaded_by=str(record.get("uploaded_by") or record.get("uploader") or ""),
            item_type=str(record.get("item_type") or "photo"),
            url=str(record.get("url") or ""),
            thumbnail_url=str(record.get("thumbnail_url") or ""),
            width=int(record.get("width") or 0),
            height=int(record.get("height") or 0),
            duration_s=record.get("duration_s"),
            caption=record.get("caption"),
            taken_at=record.get("taken_at") or record.get("day_taken"),
            sort_order=int(record.get("sort_order") or 0),
            created_at=record.get("created_at"),
        )
        try:
            await self._gallery_repo.create_item(item)
        except Exception:  # pragma: no cover
            pass


# ─── Record → domain helpers ────────────────────────────────────────


def _post_from_record(r: dict[str, Any]) -> Post | None:
    post_id = r.get("id")
    author = r.get("author")
    if not post_id or not author:
        return None
    try:
        post_type = PostType(str(r.get("type") or "text"))
    except ValueError:
        post_type = PostType.TEXT
    file_meta_dict = r.get("file_meta")
    file_meta = None
    if isinstance(file_meta_dict, dict):
        try:
            file_meta = FileMeta(**file_meta_dict)
        except TypeError:
            file_meta = None
    return Post(
        id=str(post_id),
        author=str(author),
        type=post_type,
        created_at=_parse_iso(r.get("created_at")),
        content=r.get("content"),
        media_url=r.get("media_url"),
        comment_count=int(r.get("comment_count") or 0),
        pinned=bool(r.get("pinned", False)),
        deleted=bool(r.get("deleted", False)),
        edited_at=_parse_iso(r.get("edited_at")) if r.get("edited_at") else None,
        moderated=bool(r.get("moderated", False)),
        file_meta=file_meta,
    )


def _comment_from_record(r: dict[str, Any]) -> Comment | None:
    if not r.get("id") or not r.get("post_id") or not r.get("author"):
        return None
    try:
        comment_type = CommentType(str(r.get("type") or "text"))
    except ValueError:
        comment_type = CommentType.TEXT
    return Comment(
        id=str(r["id"]),
        post_id=str(r["post_id"]),
        author=str(r["author"]),
        type=comment_type,
        created_at=_parse_iso(r.get("created_at")),
        parent_id=r.get("parent_id"),
        content=r.get("content"),
        media_url=r.get("media_url"),
    )


def _task_from_record(r: dict[str, Any]) -> Task | None:
    if not r.get("id") or not r.get("list_id") or not r.get("title"):
        return None
    try:
        status = TaskStatus(str(r.get("status") or "todo"))
    except ValueError:
        status = TaskStatus.TODO
    rec_dict = r.get("recurrence")
    recurrence = None
    if isinstance(rec_dict, dict) and rec_dict.get("rrule"):
        recurrence = RecurrenceRule(
            rrule=str(rec_dict["rrule"]),
            last_spawned_at=rec_dict.get("last_spawned_at"),
        )
    return Task(
        id=str(r["id"]),
        list_id=str(r["list_id"]),
        title=str(r["title"]),
        status=status,
        position=int(r.get("position") or 0),
        created_by=str(r.get("created_by") or ""),
        created_at=_parse_iso(r.get("created_at")),
        updated_at=_parse_iso(r.get("updated_at")),
        description=r.get("description"),
        assignees=tuple(str(a) for a in (r.get("assignees") or ())),
        recurrence=recurrence,
    )


def _page_from_record(r: dict[str, Any], space_id: str) -> Page | None:
    if not r.get("id") or not r.get("title"):
        return None
    return Page(
        id=str(r["id"]),
        title=str(r["title"]),
        content=str(r.get("content") or ""),
        created_by=str(r.get("created_by") or ""),
        created_at=str(r.get("created_at") or ""),
        updated_at=str(r.get("updated_at") or ""),
        space_id=space_id or r.get("space_id"),
        cover_image_url=r.get("cover_image_url"),
    )


def _sticky_from_record(r: dict[str, Any], space_id: str) -> Sticky | None:
    if not r.get("id") or not r.get("author") or not r.get("content"):
        return None
    return Sticky(
        id=str(r["id"]),
        author=str(r["author"]),
        content=str(r["content"]),
        color=str(r.get("color") or "yellow"),
        position_x=float(r.get("position_x") or 0.0),
        position_y=float(r.get("position_y") or 0.0),
        created_at=str(r.get("created_at") or ""),
        updated_at=str(r.get("updated_at") or ""),
        space_id=space_id or r.get("space_id"),
    )


def _zone_from_record(r: dict[str, Any], space_id: str) -> SpaceZone | None:
    """Reconstruct a :class:`SpaceZone` from an exporter chunk record.

    Lenient: skip the row rather than raising if a malformed record
    leaks into the chunk. The federation layer has already verified
    the envelope signature, so the worst case is a peer with a buggy
    catalogue — log and drop the offending row, keep the others.
    """
    zone_id = r.get("id")
    name = r.get("name")
    if not zone_id or not name:
        return None
    try:
        latitude = float(r["latitude"])
        longitude = float(r["longitude"])
        radius_m = int(r["radius_m"])
    except KeyError, TypeError, ValueError:
        log.debug("zone record missing coords/radius: %r", r)
        return None
    return SpaceZone(
        id=str(zone_id),
        space_id=space_id or str(r.get("space_id") or ""),
        name=str(name),
        latitude=latitude,
        longitude=longitude,
        radius_m=radius_m,
        color=r.get("color"),
        created_by=str(r.get("created_by") or ""),
        created_at=str(r.get("created_at") or ""),
        updated_at=str(r.get("updated_at") or ""),
    )


def _calendar_from_record(r: dict[str, Any]) -> CalendarEvent | None:
    if (
        not r.get("id")
        or not r.get("calendar_id")
        or not r.get("summary")
        or not r.get("created_by")
    ):
        return None
    start = _parse_iso(r.get("start"))
    end = _parse_iso(r.get("end"))
    return CalendarEvent(
        id=str(r["id"]),
        calendar_id=str(r["calendar_id"]),
        summary=str(r["summary"]),
        start=start,
        end=end,
        created_by=str(r["created_by"]),
        description=r.get("description"),
        all_day=bool(r.get("all_day", False)),
        attendees=tuple(str(a) for a in (r.get("attendees") or ())),
        mirrored_from=r.get("mirrored_from"),
    )
