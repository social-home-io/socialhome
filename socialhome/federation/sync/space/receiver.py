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

import base64
import logging
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import orjson as _orjson

from ....domain.calendar import CalendarEvent
from ....domain.events import SpaceSyncComplete
from ....domain.page import Page
from ....domain.post import (
    BazaarListing,
    BazaarMode,
    BazaarStatus,
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
    from ....repositories.poll_repo import AbstractPollRepo
    from ....repositories.profile_picture_repo import (
        AbstractProfilePictureRepo,
    )
    from ....repositories.space_post_repo import AbstractSpacePostRepo
    from ....repositories.space_repo import AbstractSpaceRepo
    from ....repositories.bazaar_repo import AbstractBazaarRepo
    from ....repositories.space_zone_repo import AbstractSpaceZoneRepo
    from ....repositories.sticky_repo import AbstractStickyRepo
    from ....repositories.task_repo import AbstractSpaceTaskRepo
    from ....services.pending_decrypts_cache import PendingDecryptsCache
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
        "_bazaar_repo",
        "_profile_picture_repo",
        "_poll_repo",
        "_pending_decrypts",
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
        bazaar_repo: "AbstractBazaarRepo | None" = None,
        profile_picture_repo: "AbstractProfilePictureRepo | None" = None,
        poll_repo: "AbstractPollRepo | None" = None,
        pending_decrypts: "PendingDecryptsCache | None" = None,
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
        self._bazaar_repo = bazaar_repo
        self._profile_picture_repo = profile_picture_repo
        self._poll_repo = poll_repo
        #: Optional — when wired, ``decrypt_chunk`` failures that
        #: look like "missing epoch key" stash the chunk for replay
        #: once :class:`SpaceContentKeyImported` fires on the same
        #: ``(space_id, epoch)`` (#122). Without it, missing-key
        #: chunks log + drop (legacy behaviour).
        self._pending_decrypts = pending_decrypts

    async def on_chunk(
        self,
        raw: bytes | str,
        *,
        from_instance: str,
        expected_space_id: str | None = None,
    ) -> None:
        """Handle one chunk (DataChannel frame or routed federation event).

        All failure modes log + return. Note the sender is NOT necessarily
        a paired peer: a mesh-joined member receives its catch-up stream
        from a host it has no pairing with, so this method authenticates
        the chunk itself rather than assuming the transport already did
        (see the signature-verification block below, and #648).

        ``expected_space_id`` PINS the chunk to the session that carried
        it. A sync session is opened for ONE space, but nothing here
        looked at which space a chunk claimed — so a provider streaming
        an agreed session for space A could write members (role
        included), bans and every content type of space B. The caller
        knows the session; it passes its ``space_id``. ``None`` means the
        caller has no session context (kept for the replay path, which
        re-enters with the chunk it already accepted)."""
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
        if expected_space_id is not None and space_id != expected_space_id:
            log.warning(
                "sync chunk from %s claims space %s but session %s is for %s "
                "— dropping",
                from_instance,
                space_id,
                sync_id,
                expected_space_id,
            )
            return

        # Signature verification — the sender's Ed25519 identity key, so a
        # tampered chunk is dropped here rather than on the persist path.
        #
        # Two sources, in order:
        #
        # 1. The ``remote_instances`` row, for a host we are paired with.
        # 2. The space stub's ``host_identity_pk``, for a host we are NOT
        #    paired with. A member that joined over the MESH has no
        #    ``remote_instances`` row at all, so source 1 returns None and
        #    this used to ``return`` — silently, at DEBUG — discarding every
        #    chunk of the catch-up stream and leaving the joiner with the
        #    space, the content key and the media bytes but no post or
        #    gallery metadata (#648). The key is space-scoped and was stored
        #    only after ``derive_instance_id(pk) == sender`` passed on the
        #    sealed invite, so it authorises exactly one thing: signatures
        #    on content for this space.
        #
        # The old docstring claimed "the federation service has already
        # verified the peer is paired". That holds for a direct chunk and is
        # false for a mesh-routed one — which is why this is checked here.
        peer = await self._federation_repo.get_instance(from_instance)
        ed_public_key: bytes | None = None
        sig_suite = "ed25519"
        pq_pk: bytes | None = None
        if peer is not None:
            sig_suite = peer.sig_suite
            ed_public_key = bytes.fromhex(peer.remote_identity_pk)
            pq_pk_hex = peer.remote_pq_identity_pk
            pq_pk = bytes.fromhex(pq_pk_hex) if pq_pk_hex else None
        else:
            host_pk_hex = await self._space_repo.get_host_identity_pk(space_id)
            space = await self._space_repo.get(space_id)
            # Only the household this stub says hosts the space may sign its
            # content. Without this, any unpaired instance whose chunks
            # mentioned the space id would be verified against the host's key
            # — it would fail the signature check, but the identity binding
            # belongs here, explicitly, not as a side effect.
            if (
                not host_pk_hex
                or space is None
                or space.owner_instance_id != from_instance
            ):
                log.debug(
                    "sync chunk from unknown instance %s for space %s — "
                    "no paired-peer row and no matching host key; dropping",
                    from_instance,
                    space_id,
                )
                return
            try:
                ed_public_key = bytes.fromhex(host_pk_hex)
            except ValueError:
                log.warning(
                    "sync chunk: stored host_identity_pk for space %s is not "
                    "valid hex — dropping",
                    space_id,
                )
                return

        signatures = envelope.get("signatures") or {}
        envelope_for_verify = {k: v for k, v in envelope.items() if k != "signatures"}
        bytes_for_verify = _orjson.dumps(envelope_for_verify)
        ok = self._encoder.verify_signatures_all(
            bytes_for_verify,
            suite=sig_suite,
            signatures=signatures,
            ed_public_key=ed_public_key,
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
            # ``decrypt_chunk`` raises ``RuntimeError`` with
            # ``"missing epoch"`` in the message when the receiver
            # hasn't imported this epoch's key yet (#122). When a
            # pending-decrypts cache is wired, stash the chunk so it
            # replays the moment :class:`SpaceContentKeyImported`
            # fires for the matching ``(space_id, epoch)`` — covers
            # the §D1b race where a sync chunk lands on the wire
            # before its ``apply_space_content_key_from_metadata``
            # finishes on this end. Any other decrypt failure (wrong
            # AAD, tampered ciphertext, malformed wire) drops as
            # before — those are not race-recoverable.
            if self._pending_decrypts is not None and "missing epoch" in str(exc):
                log.info(
                    "sync chunk decrypt missing key for space=%s epoch=%d "
                    "— stashing for replay (sync_id=%s resource=%s)",
                    space_id,
                    epoch,
                    sync_id,
                    resource,
                )

                async def _redeliver() -> None:
                    await self.on_chunk(
                        raw,
                        from_instance=from_instance,
                        expected_space_id=space_id,
                    )

                self._pending_decrypts.stash(space_id, epoch, _redeliver)
                return
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
        elif resource == "member_pictures":
            # F6: persist per-member avatar bytes so the SPA stops
            # rendering broken <img> for every existing member after a
            # fresh joiner's catch-up. Skip silently if the receiver
            # was assembled without a profile_picture_repo (older
            # deployments).
            if self._profile_picture_repo is None:
                log.debug(
                    "received %d member_picture records — no "
                    "profile_picture_repo wired, skipping",
                    len(records),
                )
                return
            for r in records:
                uid = str(r.get("user_id") or "")
                pic_b64 = r.get("picture_webp_base64")
                pic_hash = str(r.get("picture_hash") or "")
                if not uid or not pic_b64 or not pic_hash:
                    log.debug("member_picture record missing field: %r", r)
                    continue
                try:
                    pic_bytes = base64.b64decode(pic_b64)
                except Exception:  # pragma: no cover
                    log.debug(
                        "member_picture base64 decode failed for %s/%s",
                        space_id,
                        uid,
                    )
                    continue
                try:
                    await self._profile_picture_repo.set_member_picture(
                        space_id,
                        uid,
                        bytes_webp=pic_bytes,
                        hash=pic_hash,
                        width=int(r.get("width") or 0),
                        height=int(r.get("height") or 0),
                    )
                except Exception as exc:  # pragma: no cover
                    log.debug(
                        "member_picture set_member_picture failed for %s/%s: %s",
                        space_id,
                        uid,
                        exc,
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
                if post is not None and (
                    await self._space_post_repo.save(space_id, post) is None
                ):
                    log.warning(
                        "space sync: post %s already exists in another space "
                        "— refusing the write for %s",
                        post.id,
                        space_id,
                    )
        elif resource == "comments":
            for r in records:
                comment = _comment_from_record(r)
                if comment is not None and not await self._space_post_repo.add_comment(
                    comment, space_id=space_id
                ):
                    log.warning(
                        "space sync: comment %s targets post %s outside space "
                        "%s — refusing the write",
                        comment.id,
                        comment.post_id,
                        space_id,
                    )
        elif resource in ("tasks", "tasks_archived"):
            for r in records:
                task = _task_from_record(r)
                if task is not None:
                    await self._space_task_repo.save(task, space_id=space_id)
        elif resource == "pages":
            for r in records:
                page = _page_from_record(r, space_id)
                if page is not None:
                    await self._page_repo.save(page, space_id=space_id)
        elif resource == "stickies":
            for r in records:
                sticky = _sticky_from_record(r, space_id)
                if sticky is not None:
                    await self._sticky_repo.save(sticky, space_id=space_id)
        elif resource == "calendar":
            for r in records:
                event = _calendar_from_record(r)
                if event is not None:
                    await self._space_calendar_repo.save_event(event, space_id=space_id)
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
        elif resource == "schedules":
            # F5: schedule-poll meta + slot defs catch up so a remote
            # member's slot picker isn't empty after a §25.6 sync. The
            # wrapper post (PostType.SCHEDULE) was already persisted
            # above by the ``posts`` exporter, so the FK to
            # ``space_posts(id)`` is satisfied.
            if self._poll_repo is None:
                log.debug(
                    "received %d schedule records — no poll_repo wired, skipping",
                    len(records),
                )
                return
            for r in records:
                post_id = str(r.get("post_id") or "")
                title = str(r.get("title") or "")
                slots = r.get("slots") or []
                if not post_id or not title or not slots:
                    log.debug("schedule record missing required field: %r", r)
                    continue
                try:
                    await self._poll_repo.create_schedule_poll(
                        post_id=post_id,
                        title=title,
                        deadline=r.get("deadline"),
                        slots=list(slots),
                    )
                except Exception as exc:  # pragma: no cover
                    log.debug(
                        "schedule catch-up create failed for post=%s: %s",
                        post_id,
                        exc,
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
        elif resource == "bazaar":
            # F4: catch-up bazaar listings so a new joiner sees the
            # full listing card (mode / price / photos / status) — not
            # just the wrapper post's caption. The wrapper post was
            # already persisted by the ``posts`` resource above, so
            # the BazaarListing FK to space_posts(id) is satisfied.
            if self._bazaar_repo is None:
                log.debug(
                    "received %d bazaar records — no bazaar_repo wired, skipping",
                    len(records),
                )
                return
            for r in records:
                listing = _bazaar_listing_from_record(r, space_id)
                if listing is not None:
                    try:
                        await self._bazaar_repo.save_listing(listing)
                    except Exception as exc:  # pragma: no cover
                        # FK / CHECK violation (post not yet persisted,
                        # unknown mode) — log + drop; matches the
                        # tolerance of the other resources.
                        log.debug(
                            "bazaar catch-up save_listing failed for post_id=%s: %s",
                            listing.post_id,
                            exc,
                        )

    async def _persist_album(self, record: dict[str, Any]) -> None:
        from ....domain.gallery import (
            GalleryAlbum,
        )  # local to avoid cycle at module load

        album = GalleryAlbum(
            id=str(record["id"]),
            space_id=record.get("space_id"),
            owner_user_id=(
                # System albums have no human owner; carry NULL.
                None
                if record.get("is_system")
                else (
                    str(record.get("owner_user_id") or record.get("owner_id") or "")
                    or None
                )
            ),
            name=str(record.get("name") or ""),
            description=record.get("description"),
            cover_item_id=record.get("cover_item_id"),
            item_count=int(record.get("item_count") or 0),
            retention_exempt=bool(record.get("retention_exempt", False)),
            is_system=bool(record.get("is_system", False)),
            created_at=record.get("created_at"),
        )
        try:
            await self._gallery_repo.create_album(album)
        except Exception:
            # NOT a duplicate: ``create_album`` is already
            # ``ON CONFLICT DO NOTHING``, so a redelivered album is silent
            # at the SQL layer and never reaches here. The previous comment
            # claimed otherwise and the blanket ``pass`` hid the real
            # failure for as long as gallery sync has existed — a remotely
            # owned album violated ``owner_user_id REFERENCES users``, so a
            # member household got the image bytes and no rows (#650).
            # Anything landing here is worth seeing.
            log.warning(
                "sync: persisting gallery album %s (space=%s) failed",
                album.id,
                album.space_id,
                exc_info=True,
            )

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
        except Exception:
            # Same reasoning as ``_persist_album``: redelivery is handled in
            # SQL, so a failure here is real (a missing parent album, a bad
            # record) and must not be silent.
            log.warning(
                "sync: persisting gallery item %s (album=%s) failed",
                item.id,
                item.album_id,
                exc_info=True,
            )


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
        # IANA wall-clock anchor — additive field; older peers omit it,
        # in which case the dataclass default ``"UTC"`` kicks in.
        tz=str(r.get("tz") or "UTC"),
    )


def _bazaar_listing_from_record(
    r: dict[str, Any],
    space_id: str,
) -> "BazaarListing | None":
    """Reconstruct a :class:`BazaarListing` from an exporter chunk record.

    Lenient: missing required fields or unknown mode/status → log + skip
    the row. The wrapper post must already be persisted (FK to
    space_posts.id); if it isn't, ``save_listing`` will raise IntegrityError
    and the caller catches it.
    """
    post_id = r.get("post_id")
    seller = r.get("seller_user_id")
    mode_raw = r.get("mode")
    title = r.get("title")
    if not post_id or not seller or not mode_raw or title is None:
        log.debug("bazaar record missing required field: %r", r)
        return None
    try:
        mode = BazaarMode(str(mode_raw))
        status = BazaarStatus(str(r.get("status") or "active"))
    except ValueError:
        log.debug(
            "bazaar record unknown mode/status: mode=%r status=%r",
            mode_raw,
            r.get("status"),
        )
        return None
    return BazaarListing(
        post_id=str(post_id),
        space_id=str(r.get("space_id") or space_id),
        seller_user_id=str(seller),
        mode=mode,
        title=str(title),
        end_time=str(r.get("end_time") or ""),
        currency=str(r.get("currency") or "USD"),
        status=status,
        created_at=str(r.get("created_at") or ""),
        description=r.get("description"),
        image_urls=tuple(r.get("image_urls") or ()),
        price=r.get("price"),
        start_price=r.get("start_price"),
        step_price=r.get("step_price"),
        winner_user_id=r.get("winner_user_id"),
        winning_price=r.get("winning_price"),
        sold_at=r.get("sold_at"),
    )
