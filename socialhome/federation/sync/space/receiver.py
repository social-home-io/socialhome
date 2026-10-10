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
import pathlib
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Protocol, TYPE_CHECKING

import orjson as _orjson

from ....domain.calendar import CalendarEvent, is_occurrence_id
from ....domain.events import SpaceSyncComplete
from ....domain.federation import FederationEvent, FederationEventType
from ....domain.page import Page
from ....domain.post import (
    BAZAAR_MAX_IMAGES,
    FEED_POST_MAX_IMAGES,
    MAX_DISTINCT_REACTIONS_PER_POST,
    BazaarListing,
    BazaarMode,
    BazaarStatus,
    Comment,
    CommentType,
    FileMeta,
    Post,
    PostType,
)
from ....domain.gallery import GalleryAlbum, GalleryItem
from ....domain.space import (
    ContentAction,
    SpaceMember,
    SpaceZone,
    parse_have_seq,
    validate_zone_color,
    validate_zone_coord,
    validate_zone_name,
    validate_zone_radius,
)
from ....domain.sticky import MAX_STICKY_CONTENT_LENGTH, Sticky, coerce_peer_sticky
from ....domain.task import task_from_wire_dict, task_list_from_wire_dict
from ....domain.events import (
    CalendarEventDeleted,
    CommentDeleted,
    GalleryAlbumDeleted,
    GalleryItemDeleted,
    PageDeleted,
    PostDeleted,
    TaskDeleted,
    TaskListDeleted,
    TimetableSaved,
)
from ....domain.timetable import (
    Timetable,
    from_wire_dict,
    remote_version_refusal,
    validate,
)
from ....infrastructure.event_bus import EventBus
from ....utils.timezones import coerce_tz
from ....media.cleanup import unlink_unreferenced
from ...owner_bound_id import (
    GALLERY_ALBUM_KIND,
    GALLERY_ITEM_KIND,
    SPACE_CALENDAR_EVENT_KIND,
    SPACE_CHAT_MESSAGE_KIND,
    SPACE_COMMENT_KIND,
    SPACE_PAGE_KIND,
    SPACE_POST_KIND,
    SPACE_STICKY_KIND,
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    SPACE_TIMETABLE_KIND,
    OwnerBinding,
    check_owner_bound_id,
    is_owner_bound,
    owner_bound_id_refused,
)
from ....services.inbound_media_store import (
    local_media_ref,
    local_media_refs,
    verbatim_local_media_ref,
)
from ....services.link_preview_service import wire_link_preview
from ....services.page_conflict_service import PageMode, canonical_from_wire
from ...space_scope import archive_refusal
from .exporter import (
    ALLOWED_RESOURCES,
    REMOVAL_RESOURCES,
    ROSTER_RESOURCES,
    SENTINEL_RESOURCE,
    parse_chunk,
)

if TYPE_CHECKING:
    from ....federation.space_authorship import SpaceAuthorship
    from ....repositories.calendar_repo import AbstractSpaceCalendarRepo
    from ....repositories.federation_repo import AbstractFederationRepo
    from ....repositories.gallery_repo import AbstractGalleryRepo
    from ....repositories.media_reference_repo import AbstractMediaReferenceRepo
    from ....services.legacy_album_deletes import LegacyAlbumDeletes
    from ....repositories.page_repo import AbstractPageRepo
    from ....repositories.space_poll_repo import AbstractSpacePollRepo
    from ....repositories.profile_picture_repo import (
        AbstractProfilePictureRepo,
    )
    from ....repositories.space_post_repo import AbstractSpacePostRepo
    from ....repositories.space_repo import AbstractSpaceRepo
    from ....repositories.space_sync_watermark_repo import (
        AbstractSpaceSyncWatermarkRepo,
    )
    from ....repositories.bazaar_repo import AbstractBazaarRepo
    from ....repositories.space_zone_repo import AbstractSpaceZoneRepo
    from ....repositories.sticky_repo import AbstractStickyRepo
    from ....repositories.task_repo import AbstractSpaceTaskRepo
    from ....repositories.timetable_repo import AbstractSpaceTimetableRepo
    from ....services.page_conflict_service import PageConflictService
    from ....services.pending_decrypts_cache import PendingDecryptsCache
    from ....services.space_crypto_service import SpaceContentEncryption
    from ...encoder import FederationEncoder

log = logging.getLogger(__name__)


#: Streams whose health is tracked at once (bounded: a stream whose
#: sentinel never comes is evicted oldest-first).
MAX_TRACKED_STREAMS: int = 256


class HeldBack:
    """Records of one sync chunk refused only **for now** — they would land
    on a later stream, so the chunk does not count as applied and the
    stream is reported unclean (:class:`StreamHealth`); the provider keeps
    its watermark and the next periodic session streams them again,
    instead of the daily full pass.

    Only a refusal that a later stream can turn into a write counts: a
    record naming a user this space has no record of at all yet (the
    roster gossip seating them trails the stream — the same race the live
    path holds a write for), a tombstone / item whose parent is not held
    here at all yet, or a persist that failed where the caller logs rather
    than raises (the gallery album / item writes). A refusal by rule (held already, deleted here, a user
    known but not seated on the provider, another space's id) would be
    refused again and is not counted — it must not keep every stream
    unclean."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def add(self, n: int = 1) -> None:
        self.count += n


class StreamHealth:
    """Whether every chunk of a §25.6 stream was applied here.

    The requester tells the provider with ``SPACE_SYNC_COMPLETE {clean}``;
    only a clean stream advances the provider's incremental-sync watermark.
    A stream is clean when its sentinel names how many chunks the provider
    sent (``chunk_count``), exactly that many were **stored** here — counted
    once a chunk's records are persisted, never on arrival —, none failed
    to decrypt / parse / persist or held a record back for a retry, and
    none is still waiting for its epoch key. Anything else — a chunk lost on
    the way, a persist that raised or is still running, a tampered chunk, a
    record refused only for now, an older provider that sends no count — is
    unclean, so the next periodic session re-streams it rather than the
    provider skipping rows this household never stored.

    Counting on arrival made the verdict depend on chunks being persisted
    one after another: relayed chunks are separate inbound events, so the
    sentinel can be handled while an earlier chunk still persists — and
    that persist may yet fail.
    """

    __slots__ = ("_streams", "_finished")

    def __init__(self) -> None:
        #: sync_id -> [applied, failed, pending]
        self._streams: OrderedDict[str, list[int]] = OrderedDict()
        #: Streams whose verdict was given: a chunk finishing after its
        #: sentinel changes nothing, and must not open a new entry.
        self._finished: OrderedDict[str, None] = OrderedDict()

    def _entry(self, sync_id: str) -> list[int] | None:
        if sync_id in self._finished:
            return None
        entry = self._streams.get(sync_id)
        if entry is None:
            entry = self._streams[sync_id] = [0, 0, 0]
            while len(self._streams) > MAX_TRACKED_STREAMS:
                self._streams.popitem(last=False)
        return entry

    def applied(self, sync_id: str) -> None:
        """A chunk's records are stored (or refused for good)."""
        entry = self._entry(sync_id)
        if entry is not None:
            entry[0] += 1

    def failed(self, sync_id: str) -> None:
        entry = self._entry(sync_id)
        if entry is not None:
            entry[1] = 1

    def stashed(self, sync_id: str) -> None:
        entry = self._entry(sync_id)
        if entry is not None:
            entry[2] += 1

    def resolved(self, sync_id: str) -> None:
        entry = self._streams.get(sync_id)
        if entry is not None and entry[2] > 0:
            entry[2] -= 1

    def finish(self, sync_id: str, chunk_count: object) -> bool:
        """End the stream; ``True`` iff it is clean."""
        applied, failed, pending = self._streams.pop(sync_id, [0, 0, 0])
        self._finished[sync_id] = None
        while len(self._finished) > MAX_TRACKED_STREAMS:
            self._finished.popitem(last=False)
        if not isinstance(chunk_count, int) or isinstance(chunk_count, bool):
            return False
        return not failed and not pending and applied == chunk_count


class ChatSyncSink(Protocol):
    """Where streamed space-chat messages (v_55) go — the space-chat inbound
    handlers, which apply each record with the live create rule."""

    async def apply_sync_records(
        self, space_id: str, records: list[dict[str, Any]], *, provider: str
    ) -> int:
        """Apply the records; return how many were held for a seat that
        has not reached this household yet (:class:`HeldBack`)."""
        ...

    async def apply_sync_tombstones(
        self, space_id: str, records: list[dict[str, Any]], *, provider: str
    ) -> None: ...


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
        "_authorship",
        "_media_dir",
        "_media_refs",
        "_legacy_album_deletes",
        "_timetable_repo",
        "_page_conflicts",
        "_chat_sink",
        "_health",
        "_applied_seqs",
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
        poll_repo: "AbstractSpacePollRepo | None" = None,
        pending_decrypts: "PendingDecryptsCache | None" = None,
        authorship: "SpaceAuthorship | None" = None,
        media_dir: pathlib.Path | None = None,
        media_refs: "AbstractMediaReferenceRepo | None" = None,
        legacy_album_deletes: "LegacyAlbumDeletes | None" = None,
        timetable_repo: "AbstractSpaceTimetableRepo | None" = None,
        page_conflicts: "PageConflictService | None" = None,
        applied_seqs: "AbstractSpaceSyncWatermarkRepo | None" = None,
    ) -> None:
        self._bus = bus
        #: §25.6 echo (migration 0087): where a clean stream's provider
        #: snapshot is recorded, per (space, provider household) — sent back
        #: as ``have_seq`` in the next periodic BEGIN. ``None``: never
        #: recorded, so every periodic session streams in full.
        self._applied_seqs = applied_seqs
        self._timetable_repo = timetable_repo
        #: v_55 — where ``chat_messages`` records go: the space-chat inbound
        #: handlers, which run each through the live create rule. ``None``
        #: drops the resource (see :meth:`attach_chat_sink`).
        self._chat_sink: "ChatSyncSink | None" = None
        self._health = StreamHealth()
        #: v_48 — a ``pages`` record for a page held here (only the host's
        #: chunks get that far) is another version of it: fast-forward,
        #: stale, merge or conflict. ``None``: upserted (last write wins).
        self._page_conflicts = page_conflicts
        #: A streamed gallery delete removes the files it leaves unused —
        #: the rule of the live delete (``unlink_unreferenced``). ``None``
        #: keeps them for the orphan sweep.
        self._media_dir = media_dir
        self._media_refs = media_refs
        #: Overtaking album deletes no tombstone row proves (a legacy id, an
        #: owner-less v_33 delete): a streamed album must not undo them.
        self._legacy_album_deletes = legacy_album_deletes
        #: §24.11 authorship for a chunk streamed by a household that is NOT
        #: the space's host (see :meth:`_admit`). ``None`` refuses such
        #: chunks outright rather than trusting them.
        self._authorship = authorship
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

    def attach_chat_sink(self, sink: "ChatSyncSink") -> None:
        """Wire where streamed space-chat messages (v_55) are applied."""
        self._chat_sink = sink

    async def _record_applied(
        self, space_id: str, provider: str, snapshot_seq: object
    ) -> None:
        """Remember the provider's signed snapshot of a stream that applied
        cleanly here (§25.6 echo, migration 0087). Written after every row of
        the stream, in this database — so a restore from an older file
        snapshot rolls it back with the rows, and the next periodic BEGIN
        asks for the gap. A sentinel without a valid snapshot (an older
        provider) records nothing. Fail-soft: an unrecorded echo only means
        the next session streams more."""
        seq = parse_have_seq(snapshot_seq)
        if self._applied_seqs is None or seq is None:
            return
        try:
            await self._applied_seqs.record_applied(space_id, provider, seq)
        except Exception:
            log.exception(
                "sync: recording the applied snapshot of %s for space %s failed",
                provider,
                space_id,
            )

    async def on_chunk(
        self,
        raw: bytes | str,
        *,
        from_instance: str,
        expected_space_id: str | None = None,
    ) -> None:
        """Handle one chunk (DataChannel frame or routed federation event).

        A chunk counts towards its stream's verdict (:class:`StreamHealth`)
        only once its records are stored — a chunk stashed for its epoch key
        counts when the replay stores it.

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
            self._health.failed(sync_id)
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
                self._health.failed(sync_id)
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
                self._health.failed(sync_id)
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
            self._health.failed(sync_id)
            log.warning(
                "sync chunk signature mismatch (sync_id=%s resource=%s)",
                sync_id,
                resource,
            )
            return

        # Sentinel path — publish end-of-stream + return.
        if resource == SENTINEL_RESOURCE:
            clean = self._health.finish(sync_id, envelope.get("chunk_count"))
            if clean:
                await self._record_applied(
                    space_id, from_instance, envelope.get("snapshot_seq")
                )
            await self._bus.publish(
                SpaceSyncComplete(
                    space_id=space_id,
                    from_instance=from_instance,
                    sync_id=sync_id,
                    clean=clean,
                )
            )
            return

        if resource not in ALLOWED_RESOURCES:
            # A newer provider's resource: dropped by rule, never retried.
            self._health.applied(sync_id)
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
                    try:
                        await self.on_chunk(
                            raw,
                            from_instance=from_instance,
                            expected_space_id=space_id,
                        )
                    finally:
                        self._health.resolved(sync_id)

                self._health.stashed(sync_id)
                self._pending_decrypts.stash(space_id, epoch, _redeliver)
                return
            self._health.failed(sync_id)
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
            self._health.failed(sync_id)
            log.warning("sync chunk plaintext parse failed: %s", exc)
            return

        try:
            settled = await self._dispatch(
                resource, space_id, records, provider=from_instance
            )
        except Exception:
            self._health.failed(sync_id)
            log.exception(
                "sync chunk persist failed (resource=%s space=%s)",
                resource,
                space_id,
            )
            return
        if settled:
            self._health.applied(sync_id)
            return
        # A record refused only for now (its author's seat or its parent has
        # not reached us yet): the stream is unclean, so the provider keeps
        # its watermark and the next periodic session streams it again.
        self._health.failed(sync_id)
        log.info(
            "sync %s: %s chunk for space %s from %s held records back for a "
            "retry — reporting the stream unclean",
            sync_id,
            resource,
            space_id,
            from_instance,
        )

    async def _dispatch(
        self,
        resource: str,
        space_id: str,
        records: list[dict[str, Any]],
        *,
        provider: str,
    ) -> bool:
        """Admit and persist one chunk's records. ``False`` when a record
        was held back for a retry (refused only for now — see
        :class:`HeldBack`), so the stream must not count as clean."""
        if not isinstance(records, list):
            records = []
        shaped = [r for r in records if isinstance(r, dict)]
        if len(shaped) != len(records):
            log.warning(
                "space sync: dropped %d non-object %s record(s) from %s for %s",
                len(records) - len(shaped),
                resource,
                provider,
                space_id,
            )
        if resource == "pages":
            # Read before the admission drops a member's records for held
            # pages: the host's seq floor learns from them (never content).
            await self._page_seq_hints(shaped, space_id, provider=provider)
        held_back = HeldBack()
        records = await self._admit(
            resource, space_id, shaped, provider=provider, held_back=held_back
        )
        # v_36: whoever streams it — the host included — a record may not
        # claim an owner-bound id for anybody but the user it commits to.
        records = [r for r in records if not _claims_bound_id(resource, space_id, r)]
        if records:
            await self._persist(
                resource, space_id, records, provider=provider, held_back=held_back
            )
        return not held_back.count

    async def _persist(
        self,
        resource: str,
        space_id: str,
        records: list[dict[str, Any]],
        *,
        provider: str,
        held_back: "HeldBack",
    ) -> None:
        """Persist admitted records of ``resource``. A record that cannot
        land only for now is counted in ``held_back``."""
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
                if post is None:
                    continue
                # A post deleted here stays deleted: the provider may have
                # missed the delete, or the delete overtook the create (a
                # v_49 member-published delete leaves a soft-deleted row
                # under the id). ``save`` would upsert it live again.
                held_post = await self._space_post_repo.get(post.id)
                if held_post is not None and held_post[1].deleted:
                    log.debug("sync: post %s was deleted here — skipped", post.id)
                    continue
                if held_post is not None:
                    # Keep what this household holds of the row's shared
                    # state: its reactions (ordered by the stamps written
                    # with them — a provider's snapshot would wipe relayed
                    # ones and the stamps would then refuse the copy that
                    # could restore them) and its comment count.
                    post = replace(
                        post,
                        reactions=held_post[1].reactions,
                        comment_count=held_post[1].comment_count,
                    )
                if await self._space_post_repo.save(space_id, post) is None:
                    log.warning(
                        "space sync: post %s already exists in another space "
                        "— refusing the write for %s",
                        post.id,
                        space_id,
                    )
        elif resource == "posts_deleted":
            await self._persist_post_tombstones(records, space_id, provider=provider)
        elif resource == "comments_deleted":
            await self._persist_comment_tombstones(records, space_id, provider=provider)
        elif resource == "comments":
            # An older provider streamed its deleted comments as comment
            # records (content cleared, ``deleted: true``); stored, one read
            # as a live empty comment. From the host such a record is the
            # delete it stands for — applied as a ``comments_deleted``
            # tombstone; from anyone else it is dropped (a member's deletes
            # travel as tombstones, judged by the live rule).
            gone = [r for r in records if r.get("deleted")]
            if gone and await self._is_host(space_id, provider):
                await self._persist_comment_tombstones(
                    gone, space_id, provider=provider
                )
            for r in records:
                if r.get("deleted"):
                    continue
                comment = _comment_from_record(r)
                if comment is None or await self._space_post_repo.add_comment(
                    comment, space_id=space_id
                ):
                    continue
                # Not inserted: an id held here already (a member-relayed
                # copy, or a delete's tombstone) stays as it is …
                if await self._space_post_repo.get_comment(comment.id) is not None:
                    log.debug("sync: comment %s is held here — skipped", comment.id)
                else:  # … anything else targets a post outside this space.
                    log.warning(
                        "space sync: comment %s targets post %s outside space "
                        "%s — refusing the write",
                        comment.id,
                        comment.post_id,
                        space_id,
                    )
        elif resource == "task_lists":
            for r in records:
                lst = task_list_from_wire_dict(r)
                if lst is None or not lst.created_by:
                    continue
                if await self._space_task_repo.is_list_deleted(
                    lst.id, space_id=space_id
                ):
                    # Deleted here — a provider that missed the delete
                    # still streams it; the tombstone wins (its id is never
                    # reused), and its own tombstone stream will tell it.
                    log.debug("sync: task list %s was deleted here — skipped", lst.id)
                    continue
                if not await self._space_task_repo.save_list(lst, space_id=space_id):
                    _log_sync_refusal("task list", lst.id, space_id)
        elif resource == "task_lists_deleted":
            await self._persist_task_list_tombstones(
                records, space_id, provider=provider
            )
        elif resource == "tasks_deleted":
            await self._persist_task_tombstones(records, space_id, provider=provider)
        elif resource in ("tasks", "tasks_archived"):
            for r in records:
                # A member household may only add ids not held here (see
                # ``_admit``), but the host's chunks are taken whole and
                # upsert over rows we hold — so merge onto the held row of
                # THIS space, or a v39 host's chunk (no ``priority``, the
                # fields its inbound lost sent as null) would wipe the
                # priority / labels / due date / archive every tick.
                rid = str(r.get("id") or r.get("task_id") or "")
                if rid and await self._space_task_repo.is_task_deleted(
                    rid, space_id=space_id
                ):
                    # Deleted here — a provider that missed the delete still
                    # streams it; the tombstone wins (its id is never
                    # reused), and its own tombstone stream will tell it.
                    log.debug("sync: task %s was deleted here — skipped", rid)
                    continue
                held = await self._space_task_repo.get(rid) if rid else None
                existing = held[1] if held is not None and held[0] == space_id else None
                task = task_from_wire_dict(r, existing=existing)
                if task is not None and await self._space_task_repo.is_list_deleted(
                    task.list_id, space_id=space_id
                ):
                    log.debug(
                        "sync: task %s is filed under list %s, deleted here — skipped",
                        task.id,
                        task.list_id,
                    )
                    continue
                if task is not None and not await self._space_task_repo.save(
                    task, space_id=space_id
                ):
                    _log_sync_refusal("task", task.id, space_id)
        elif resource == "pages_deleted":
            await self._persist_page_tombstones(records, space_id, provider=provider)
        elif resource == "pages":
            for r in records:
                page = _page_from_record(r, space_id)
                if page is None:
                    continue
                if await self._page_repo.is_page_deleted(
                    page.id, space_id=space_id
                ) and not await self._is_host_version(space_id, provider, r):
                    # Deleted here — a provider that missed the delete still
                    # streams it; the tombstone wins (its id is never
                    # reused), and its own tombstone stream will tell it.
                    # Only the host's version may revive a tombstone, and
                    # only one the host never confirmed (the engine's call).
                    log.debug("sync: page %s was deleted here — skipped", page.id)
                    continue
                if await self._apply_page_version(page, r, space_id, provider):
                    continue
                if not await self._page_repo.save(page, space_id=space_id):
                    _log_sync_refusal("page", page.id, space_id)
        elif resource == "stickies_deleted":
            await self._persist_sticky_tombstones(records, space_id, provider=provider)
        elif resource == "stickies":
            for r in records:
                sticky = _sticky_from_record(r, space_id)
                if sticky is None:
                    continue
                if await self._sticky_repo.is_deleted(sticky.id, space_id=space_id):
                    # Deleted here — a provider that missed the delete still
                    # streams it; the tombstone wins (its id is never
                    # reused), and its own tombstone stream will tell it.
                    log.debug("sync: sticky %s was deleted here — skipped", sticky.id)
                    continue
                if not await self._sticky_repo.save(sticky, space_id=space_id):
                    _log_sync_refusal("sticky", sticky.id, space_id)
        elif resource == "calendar_deleted":
            await self._persist_calendar_tombstones(
                records, space_id, provider=provider
            )
        elif resource == "calendar":
            for r in records:
                event = _calendar_from_record(r, provider=provider)
                if event is None:
                    continue
                if await self._space_calendar_repo.is_event_deleted(
                    event.id, space_id=space_id
                ):
                    log.debug("sync: event %s was deleted here — skipped", event.id)
                    continue
                if not await self._space_calendar_repo.save_event(
                    event, space_id=space_id
                ):
                    _log_sync_refusal("calendar event", event.id, space_id)
        elif resource == "gallery_albums_deleted":
            await self._persist_album_tombstones(records, space_id, provider=provider)
        elif resource == "gallery_items_deleted":
            await self._persist_item_tombstones(
                records, space_id, provider=provider, held_back=held_back
            )
        elif resource == "gallery":
            # Albums first, then items — preserve the exporter's order.
            from_host = await self._is_host(space_id, provider)
            recount: set[str] = set()
            for r in records:
                kind = r.get("kind")
                if kind == "album":
                    if await self._persist_album(
                        r, space_id, from_host=from_host, held_back=held_back
                    ):
                        recount.add(str(r["id"]))
                elif kind == "item":
                    if await self._persist_gallery_item(r, space_id, held_back):
                        recount.add(str(r.get("album_id") or ""))
            # The album's count is what this household holds — the provider's
            # figure counts the provider's rows, and a sync item lands without
            # a bump — so it is recounted, whatever order the album record,
            # its items and their tombstones were applied in.
            for album_id in sorted(recount):
                await self._gallery_repo.recount_items(album_id)
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
                    if not await self._poll_repo.create_schedule_poll_in_space(
                        space_id=space_id,
                        post_id=post_id,
                        title=title,
                        deadline=r.get("deadline"),
                        slots=list(slots),
                    ):
                        log.warning(
                            "space sync: schedule poll %s is not a post of "
                            "space %s — refusing the write",
                            post_id,
                            space_id,
                        )
                except Exception as exc:  # pragma: no cover
                    log.debug(
                        "schedule catch-up create failed for post=%s: %s",
                        post_id,
                        exc,
                    )
        elif resource == "space_zones_deleted":
            await self._persist_zone_tombstones(records, space_id, provider=provider)
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
                zone = _zone_from_record(r, space_id, provider=provider)
                if zone is None:
                    continue
                if await self._zone_repo.is_deleted(zone.id, space_id=space_id):
                    log.debug("sync: zone %s was deleted here — skipped", zone.id)
                    continue
                if not await self._zone_repo.upsert(zone, space_id=space_id):
                    log.warning(
                        "space sync: zone %s already exists in another space "
                        "— refusing the write for %s",
                        zone.id,
                        space_id,
                    )
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
                        if not await self._bazaar_repo.save_listing(
                            listing, space_id=space_id
                        ):
                            log.warning(
                                "space sync: bazaar listing %s is not a post of "
                                "space %s — refusing the write",
                                listing.post_id,
                                space_id,
                            )
                    except Exception as exc:
                        # FK / CHECK violation (anchor post missing,
                        # unknown mode). A listing the joiner cannot store
                        # is a listing the joiner never sees, so this is a
                        # WARNING, not DEBUG — until the posts exporter
                        # shipped anchor posts it fired for every
                        # unannounced listing, on every joiner, unseen.
                        log.warning(
                            "bazaar catch-up save_listing failed for post_id=%s: %s",
                            listing.post_id,
                            exc,
                        )

        elif resource == "timetables":
            await self._persist_timetables(records, space_id, provider=provider)
        elif resource == "chat_messages_deleted":
            if self._chat_sink is not None:
                await self._chat_sink.apply_sync_tombstones(
                    space_id, records, provider=provider
                )
        elif resource == "chat_messages":
            if self._chat_sink is None:
                log.debug(
                    "received %d chat records — no chat sink wired, skipping",
                    len(records),
                )
                return
            held_back.add(
                await self._chat_sink.apply_sync_records(
                    space_id, records, provider=provider
                )
                or 0
            )

    async def _persist_timetables(
        self, records: list[dict[str, Any]], space_id: str, *, provider: str
    ) -> None:
        """Space timetables (v_39): each record is a domain wire dict,
        parsed and validated like the live event; applied last-writer-wins
        (a stale copy, a deleted id or another space's id is a no-op)."""
        if self._timetable_repo is None:
            log.debug(
                "received %d timetable records — no timetable_repo wired, skipping",
                len(records),
            )
            return
        for r in records:
            tt = _timetable_from_record(r, space_id)
            if tt is None:
                continue
            held = await self._timetable_repo.get(tt.id)
            refusal = remote_version_refusal(
                tt.version,
                held[1].version if held is not None and held[0] == space_id else None,
            )
            if refusal is not None:
                log.warning(
                    "space sync: timetable %s for %s — %s; skipped",
                    tt.id,
                    space_id,
                    refusal,
                )
                continue
            if await self._timetable_repo.apply_remote(tt, space_id=space_id):
                await self._bus.publish(
                    TimetableSaved(
                        timetable=tt, space_id=space_id, origin_instance_id=provider
                    )
                )

    async def _persist_post_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed post deletes (``posts_deleted``).

        A post held live here in this space is soft-deleted — content
        cleared, the row kept as its tombstone — and ``PostDeleted`` is
        published as for a live ``SPACE_POST_DELETED`` (its origin the
        provider, so nothing is re-broadcast). A member household's records
        were admitted only under the live rule (:meth:`_authored_record`).

        From the **host**, an id we never held gets the soft-deleted row a
        delete that overtook its create leaves (v_49), so a stale copy
        another household streams later cannot create it — but only when
        the id is owner-bound to its ``author`` in THIS space (post ids are
        global: a stub for another space's id would block that space's real
        post here). A legacy (unbound) or mismatched id is skipped.
        Refusals are summarised once per chunk.
        """
        space = await self._space_repo.get(space_id)
        from_host = space is not None and space.owner_instance_id == provider
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            post_id = str(r.get("id") or r.get("post_id") or "")
            if not post_id:
                continue
            # A moderator removal names its moderator (migration 0084); the
            # author's own delete names nobody.
            moderator = (
                str(r.get("actor_user_id") or "") or None
                if r.get("moderated")
                else None
            )
            held = await self._space_post_repo.get(post_id)
            if held is not None:
                if held[0] != space_id:
                    cross_space.append(post_id)
                elif not held[1].deleted and await self._space_post_repo.soft_delete(
                    post_id, space_id=space_id, moderated_by=moderator
                ):
                    await self._bus.publish(
                        PostDeleted(
                            post_id=post_id,
                            space_id=space_id,
                            origin_instance_id=provider,
                            author_user_id=held[1].author,
                        )
                    )
                continue
            if not from_host:
                continue
            author = str(r.get("author") or "")
            if (
                check_owner_bound_id(
                    SPACE_POST_KIND, post_id, space_id=space_id, owner_user_id=author
                )
                is not OwnerBinding.VALID
            ):
                unbound += 1
                continue
            try:
                # The post's own type: a stub of a retention-exempt type
                # stays exempt here.
                post_type = PostType(str(r.get("type") or "text"))
            except ValueError:
                post_type = PostType.TEXT
            saved = await self._space_post_repo.save(
                space_id,
                Post(
                    id=post_id,
                    author=author,
                    type=post_type,
                    created_at=_parse_iso(r.get("created_at")),
                    deleted=True,
                    moderated=bool(r.get("moderated")),
                ),
            )
            if saved is not None and moderator is not None:
                await self._space_post_repo.soft_delete(
                    post_id, space_id=space_id, moderated_by=moderator
                )
        _log_tombstone_refusals("post", provider, space_id, cross_space, unbound)

    async def _persist_comment_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed comment deletes (``comments_deleted``).

        A comment held live here on a post of this space is soft-deleted,
        its post's comment count lowered and ``CommentDeleted`` published —
        what a live ``SPACE_COMMENT_DELETED`` does. A member household's
        records were admitted only under the live rule
        (:meth:`_authored_record`).

        From the **host**, an id we never held gets a soft-deleted row on
        its post — only when the id is owner-bound to its ``author`` in THIS
        space and the post is held here in this space (the insert checks
        it), so a stale copy streamed later cannot create the comment.
        """
        space = await self._space_repo.get(space_id)
        from_host = space is not None and space.owner_instance_id == provider
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            comment_id = str(r.get("id") or r.get("comment_id") or "")
            if not comment_id:
                continue
            held = await self._space_post_repo.get_comment(comment_id)
            if held is not None:
                parent = await self._space_post_repo.get(held.post_id)
                if parent is None or parent[0] != space_id:
                    cross_space.append(comment_id)
                elif (
                    not held.deleted
                    and await self._space_post_repo.soft_delete_comment(
                        comment_id, space_id=space_id
                    )
                ):
                    await self._space_post_repo.decrement_comment_count(
                        held.post_id, space_id=space_id
                    )
                    await self._bus.publish(
                        CommentDeleted(
                            post_id=held.post_id,
                            comment_id=comment_id,
                            space_id=space_id,
                            origin_instance_id=provider,
                            author_user_id=held.author,
                        )
                    )
                continue
            if not from_host:
                continue
            author = str(r.get("author") or "")
            post_id = str(r.get("post_id") or "")
            if (
                not post_id
                or check_owner_bound_id(
                    SPACE_COMMENT_KIND,
                    comment_id,
                    space_id=space_id,
                    owner_user_id=author,
                )
                is not OwnerBinding.VALID
            ):
                unbound += 1
                continue
            if not await self._space_post_repo.add_comment(
                Comment(
                    id=comment_id,
                    post_id=post_id,
                    author=author,
                    type=CommentType.TEXT,
                    created_at=_parse_iso(r.get("created_at")),
                    deleted=True,
                ),
                space_id=space_id,
            ):
                unbound += 1  # its post is not held here in this space
        _log_tombstone_refusals("comment", provider, space_id, cross_space, unbound)

    async def _persist_task_list_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed list deletes (``task_lists_deleted``).

        A list held live here in this space is tombstoned (``deleted_by``
        from the record's ``actor_user_id``); its tasks are tombstoned with
        it (the list-tombstone trigger, 0069 / 0071), and ``TaskListDeleted`` is published as
        for a live ``SPACE_TASK_LIST_DELETED``. A member household's records
        were admitted only for such lists (:meth:`_authored_record`).

        From the **host**, an id we never held gets a stub tombstone, so a
        stale copy another household streams later cannot create it — but
        only when the id is owner-bound to its ``created_by`` in THIS
        space. List ids are global: a stub for another space's id would
        block that space's real list here forever. A legacy (unbound) or
        mismatched id is skipped; one already a tombstone is not rewritten.
        Refusals are summarised once per chunk.
        """
        space = await self._space_repo.get(space_id)
        from_host = space is not None and space.owner_instance_id == provider
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            list_id = str(r.get("id") or r.get("list_id") or "")
            if not list_id:
                continue
            deleted_by = str(r.get("actor_user_id") or "")
            held = await self._space_task_repo.get_list(list_id)
            if held is not None:
                if held[0] != space_id:
                    cross_space.append(list_id)
                elif await self._space_task_repo.delete_list(
                    list_id, space_id=space_id, deleted_by=deleted_by
                ):
                    await self._bus.publish(
                        TaskListDeleted(
                            list_id=list_id,
                            space_id=space_id,
                            origin_instance_id=provider,
                        )
                    )
                continue
            if not from_host or await self._space_task_repo.is_list_deleted(
                list_id, space_id=space_id
            ):
                continue
            created_by = str(r.get("created_by") or "")
            if (
                check_owner_bound_id(
                    SPACE_TASK_LIST_KIND,
                    list_id,
                    space_id=space_id,
                    owner_user_id=created_by,
                )
                is not OwnerBinding.VALID
            ):
                unbound += 1
                continue
            await self._space_task_repo.tombstone_list(
                list_id, space_id=space_id, created_by=created_by, deleted_by=deleted_by
            )
        if cross_space:
            log.warning(
                "space sync: %d task list tombstone(s) from %s for %s name a "
                "list held in another space — refused: %s",
                len(cross_space),
                provider,
                space_id,
                ", ".join(cross_space[:5]),
            )
        if unbound:
            log.info(
                "space sync: %d task list tombstone(s) from %s for %s name a "
                "list never held here whose id is not bound to this space — "
                "no stub recorded",
                unbound,
                provider,
                space_id,
            )

    async def _persist_task_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed single-task deletes (``tasks_deleted``).

        A task held live here in this space is tombstoned (``deleted_by``
        from the record's ``actor_user_id``) and ``TaskDeleted`` is
        published as for a live ``SPACE_TASK_DELETED``. A member
        household's records were admitted only for such tasks
        (:meth:`_authored_record`).

        From the **host**, an id we never held gets a stub tombstone, so a
        stale copy another household streams later cannot create it — but
        only when the id is owner-bound to its ``created_by`` in THIS space
        (task ids are global: a stub for another space's id would block
        that space's real task here forever), and only under a list live
        here in this space (``space_tasks.list_id`` is a FK). A task under
        a list tombstoned here needs nothing: the list's delete already
        tombstoned every task of it (0071 trigger) and keeps new ones out. Refusals are summarised
        once per chunk.
        """
        space = await self._space_repo.get(space_id)
        from_host = space is not None and space.owner_instance_id == provider
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            task_id = str(r.get("id") or r.get("task_id") or "")
            if not task_id:
                continue
            deleted_by = str(r.get("actor_user_id") or "")
            held = await self._space_task_repo.get(task_id)
            if held is not None:
                if held[0] != space_id:
                    cross_space.append(task_id)
                elif await self._space_task_repo.delete(
                    task_id, space_id=space_id, deleted_by=deleted_by
                ):
                    await self._bus.publish(
                        TaskDeleted(
                            task_id=task_id,
                            list_id=held[1].list_id,
                            space_id=space_id,
                            origin_instance_id=provider,
                        )
                    )
                continue
            if not from_host or await self._space_task_repo.is_task_deleted(
                task_id, space_id=space_id
            ):
                continue
            list_id = str(r.get("list_id") or "")
            if not list_id or await self._space_task_repo.is_list_deleted(
                list_id, space_id=space_id
            ):
                continue  # gone with its list; the list tombstone wins
            created_by = str(r.get("created_by") or "")
            if (
                check_owner_bound_id(
                    SPACE_TASK_KIND,
                    task_id,
                    space_id=space_id,
                    owner_user_id=created_by,
                )
                is not OwnerBinding.VALID
            ):
                unbound += 1
                continue
            if not await self._space_task_repo.tombstone(
                task_id,
                space_id=space_id,
                list_id=list_id,
                created_by=created_by,
                deleted_by=deleted_by,
            ):
                unbound += 1  # its list is not held live in this space
        if cross_space:
            log.warning(
                "space sync: %d task tombstone(s) from %s for %s name a task "
                "held in another space — refused: %s",
                len(cross_space),
                provider,
                space_id,
                ", ".join(cross_space[:5]),
            )
        if unbound:
            log.info(
                "space sync: %d task tombstone(s) from %s for %s name a task "
                "never held here whose id is not bound to this space, or "
                "whose list is not held here — no stub recorded",
                unbound,
                provider,
                space_id,
            )

    async def _persist_page_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed page deletes (``pages_deleted``).

        A page held live here in this space is tombstoned (``deleted_by``
        from the record's ``actor_user_id``) exactly as a live
        ``SPACE_PAGE_DELETED`` does — confirmed (final) when the host
        streams it, or when we are the host (which then re-broadcasts the
        delete); unconfirmed from another member. The host's record also
        confirms a tombstone of our own. A member household's records were
        admitted only for pages held live here (:meth:`_authored_record`).

        From the **host**, an id we never held gets a stub tombstone, so a
        stale copy another household streams or replays later cannot
        create it — but only when the id is owner-bound to its
        ``created_by`` in THIS space (page ids are global: a stub for
        another space's id would block that space's real page here).
        Refusals are summarised once per chunk.
        """
        space = await self._space_repo.get(space_id)
        from_host = space is not None and space.owner_instance_id == provider
        is_host = (
            space is not None
            and self._page_conflicts is not None
            and space.owner_instance_id == self._page_conflicts.own_instance_id
        )
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            page_id = str(r.get("id") or r.get("page_id") or "")
            if not page_id:
                continue
            deleted_by = str(r.get("actor_user_id") or "")
            held = await self._page_repo.get(page_id)
            if held is not None:
                if held.space_id != space_id:
                    cross_space.append(page_id)
                elif self._page_conflicts is not None:
                    deleted = await self._page_conflicts.delete_page(
                        space_id, page_id, deleted_by=deleted_by, from_instance=provider
                    )
                    if deleted and space is not None and is_host:
                        # The host accepted a member's delete it had missed:
                        # it decides, so it tells every member household.
                        await self._bus.publish(
                            PageDeleted(
                                page_id=page_id,
                                space_id=space_id,
                                actor_user_id=deleted_by,
                            )
                        )
                else:
                    await self._page_repo.delete(
                        page_id,
                        space_id=space_id,
                        deleted_by=deleted_by,
                        confirmed=from_host,
                    )
                continue
            if await self._page_repo.is_page_deleted(page_id, space_id=space_id):
                if from_host:
                    # The host stands behind our own (unconfirmed) delete.
                    await self._page_repo.confirm_delete(page_id, space_id=space_id)
                continue
            if not from_host:
                continue
            created_by = str(r.get("created_by") or "")
            if (
                check_owner_bound_id(
                    SPACE_PAGE_KIND,
                    page_id,
                    space_id=space_id,
                    owner_user_id=created_by,
                )
                is not OwnerBinding.VALID
            ):
                unbound += 1
                continue
            await self._page_repo.tombstone(
                page_id, space_id=space_id, created_by=created_by, deleted_by=deleted_by
            )
        if cross_space:
            log.warning(
                "space sync: %d page tombstone(s) from %s for %s name a page "
                "held in another space — refused: %s",
                len(cross_space),
                provider,
                space_id,
                ", ".join(cross_space[:5]),
            )
        if unbound:
            log.info(
                "space sync: %d page tombstone(s) from %s for %s name a page "
                "never held here whose id is not bound to this space — no "
                "stub recorded",
                unbound,
                provider,
                space_id,
            )

    async def _persist_sticky_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed sticky deletes (``stickies_deleted``, migration
        0085).

        A sticky held live here in this space is tombstoned (``deleted_by``
        from the record's ``actor_user_id``), as a live
        ``SPACE_STICKY_DELETED`` does. A member household's records were
        admitted only under the live rule (:meth:`_authored_record`). From
        the **host**, an id never held here gets a stub tombstone — only
        when the id is owner-bound to its ``author`` in THIS space (sticky
        ids are global: a stub for another space's id would block that
        space's real sticky here). Refusals are summarised once per chunk.
        """
        from_host = await self._is_host(space_id, provider)
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            sticky_id = str(r.get("id") or r.get("sticky_id") or "")
            if not sticky_id:
                continue
            deleted_by = str(r.get("actor_user_id") or "")
            held = await self._sticky_repo.get(sticky_id)
            if held is not None:
                if held.space_id != space_id:
                    cross_space.append(sticky_id)
                else:
                    await self._sticky_repo.delete(
                        sticky_id, space_id=space_id, deleted_by=deleted_by
                    )
                continue
            if not from_host or await self._sticky_repo.is_deleted(
                sticky_id, space_id=space_id
            ):
                continue
            author = str(r.get("author") or "")
            if not _bound_here(SPACE_STICKY_KIND, sticky_id, space_id, author):
                unbound += 1
                continue
            await self._sticky_repo.tombstone(
                sticky_id,
                space_id=space_id,
                author=author,
                created_at=str(r.get("created_at") or ""),
                deleted_by=deleted_by,
            )
        _log_tombstone_refusals("sticky", provider, space_id, cross_space, unbound)

    async def _persist_calendar_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed calendar-event deletes (``calendar_deleted``,
        migration 0085).

        An event held live here in this space is tombstoned — its RSVPs and
        reminders go with it — and ``CalendarEventDeleted`` is published as
        for a live ``SPACE_CALENDAR_EVENT_DELETED`` (the feed bridge removes
        the event's announcement post). A member household's records were
        admitted only under the live rule. From the **host**, an id never
        held here gets a stub tombstone when the id is owner-bound to its
        ``created_by`` in THIS space.
        """
        from_host = await self._is_host(space_id, provider)
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            event_id = str(r.get("id") or r.get("event_id") or "")
            if not event_id:
                continue
            deleted_by = str(r.get("actor_user_id") or "")
            held = await self._space_calendar_repo.get_event(event_id)
            if held is not None:
                if held[0] != space_id:
                    cross_space.append(event_id)
                elif await self._space_calendar_repo.delete_event(
                    event_id, space_id=space_id, deleted_by=deleted_by
                ):
                    await self._bus.publish(
                        CalendarEventDeleted(event_id=event_id, space_id=space_id)
                    )
                continue
            if not from_host or await self._space_calendar_repo.is_event_deleted(
                event_id, space_id=space_id
            ):
                continue
            created_by = str(r.get("created_by") or "")
            if not _bound_here(
                SPACE_CALENDAR_EVENT_KIND, event_id, space_id, created_by
            ):
                unbound += 1
                continue
            await self._space_calendar_repo.tombstone_event(
                event_id,
                space_id=space_id,
                created_by=created_by,
                created_at=str(r.get("created_at") or ""),
                deleted_by=deleted_by,
            )
        _log_tombstone_refusals(
            "calendar event", provider, space_id, cross_space, unbound
        )

    async def _persist_album_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed gallery-album deletes (``gallery_albums_deleted``,
        migration 0085).

        A user album held live here in this space is tombstoned with its
        items (the 0085 trigger), their files unlinked unless another row
        still names them, and ``GalleryAlbumDeleted`` published — what a
        live ``SPACE_GALLERY_ALBUM_DELETED`` does (its origin the provider,
        so nothing is re-broadcast). A member household's records were
        admitted only under the live rule. From the **host**, an id never
        held here gets a stub tombstone when the id is owner-bound to its
        ``owner_user_id`` in THIS space.
        """
        from_host = await self._is_host(space_id, provider)
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            album_id = str(r.get("id") or r.get("album_id") or "")
            if not album_id:
                continue
            deleted_by = await self._vetted_actor(r, space_id, provider, from_host)
            held = await self._gallery_repo.get_album(album_id)
            if held is not None:
                if held.space_id != space_id or held.is_system:
                    cross_space.append(album_id)
                    continue
                media = await self._gallery_repo.list_album_media(album_id)
                if await self._gallery_repo.delete_album_in_space(
                    album_id, space_id=space_id, deleted_by=deleted_by
                ):
                    await unlink_unreferenced(self._media_dir, self._media_refs, media)
                    await self._bus.publish(
                        GalleryAlbumDeleted(
                            album_id=album_id,
                            space_id=space_id,
                            owner_id=held.owner_user_id,
                            origin_instance_id=provider,
                        )
                    )
                continue
            if not from_host or await self._gallery_repo.is_album_deleted(
                album_id, space_id=space_id
            ):
                continue
            owner = str(r.get("owner_user_id") or "")
            if not _bound_here(GALLERY_ALBUM_KIND, album_id, space_id, owner):
                unbound += 1
                continue
            await self._gallery_repo.tombstone_album(
                album_id,
                space_id=space_id,
                owner_user_id=owner,
                created_at=str(r.get("created_at") or ""),
                deleted_by=deleted_by,
            )
        _log_tombstone_refusals(
            "gallery album", provider, space_id, cross_space, unbound
        )

    async def _persist_item_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
        held_back: HeldBack,
    ) -> None:
        """Apply streamed gallery-item deletes (``gallery_items_deleted``,
        migration 0085).

        An item held live here in an album of this space is tombstoned, its
        album's count lowered, its files unlinked unless another row still
        names them, and ``GalleryItemDeleted`` published — what a live
        ``SPACE_GALLERY_ITEM_DELETED`` does. A member household's records
        were admitted only under the live rule. From the **host**, an id
        never held here gets a stub tombstone when the id is owner-bound to
        its ``uploaded_by`` in THIS space and its album is held live here in
        this space (``album_id`` is a FK); under an album tombstoned here it
        needs nothing — the album's tombstone covers it. An album not held
        here at all yet (the chunks applied out of order — ``gallery`` after
        this one) holds the stub back for the next stream
        (:class:`HeldBack`) instead of dropping the delete.
        """
        from_host = await self._is_host(space_id, provider)
        cross_space: list[str] = []
        unbound = 0
        for r in records:
            item_id = str(r.get("id") or r.get("item_id") or "")
            if not item_id:
                continue
            deleted_by = await self._vetted_actor(r, space_id, provider, from_host)
            held = await self._gallery_repo.get_item(item_id)
            if held is not None:
                if await self._gallery_repo.delete_item_in_space(
                    item_id, space_id=space_id, deleted_by=deleted_by
                ):
                    await unlink_unreferenced(
                        self._media_dir,
                        self._media_refs,
                        [held.url, held.thumbnail_url],
                    )
                    await self._bus.publish(
                        GalleryItemDeleted(
                            item_id=item_id,
                            album_id=held.album_id,
                            space_id=space_id,
                            origin_instance_id=provider,
                        )
                    )
                else:
                    cross_space.append(item_id)
                continue
            if not from_host or await self._gallery_repo.is_item_deleted(
                item_id, space_id=space_id
            ):
                continue
            uploader = str(r.get("uploaded_by") or "")
            album_id = str(r.get("album_id") or "")
            if not album_id or await self._gallery_repo.is_album_deleted(
                album_id, space_id=space_id
            ):
                continue  # gone with its album; the album tombstone wins
            if not _bound_here(GALLERY_ITEM_KIND, item_id, space_id, uploader):
                unbound += 1
                continue
            if await self._gallery_repo.tombstone_item(
                item_id,
                space_id=space_id,
                album_id=album_id,
                uploaded_by=uploader,
                created_at=str(r.get("created_at") or ""),
                deleted_by=deleted_by,
            ):
                continue
            if await self._gallery_repo.get_album(album_id) is None:
                held_back.add()  # its album has not reached us yet
            else:
                unbound += 1  # its album is another space's
        _log_tombstone_refusals(
            "gallery item", provider, space_id, cross_space, unbound
        )

    async def _persist_zone_tombstones(
        self,
        records: list[dict[str, Any]],
        space_id: str,
        *,
        provider: str,
    ) -> None:
        """Apply streamed zone deletes (``space_zones_deleted``, migration
        0085): a zone held live here in this space is tombstoned, as a live
        ``SPACE_ZONE_DELETED`` does. A member household's records were
        admitted only from an admin household (the live rule).

        No stub for a zone never held here: zone ids are not owner-bound
        (``z_<random>``), so nothing proves an id is this space's — a stub
        could squat another space's zone. A stale copy a household streams
        later lands, and the host's next tombstone stream removes it.
        """
        if self._zone_repo is None:
            return
        from_host = await self._is_host(space_id, provider)
        cross_space: list[str] = []
        for r in records:
            zone_id = str(r.get("id") or r.get("zone_id") or "")
            if not zone_id:
                continue
            held = await self._zone_repo.get(zone_id)
            if held is None:
                continue
            if held.space_id != space_id:
                cross_space.append(zone_id)
                continue
            await self._zone_repo.delete(
                zone_id,
                space_id=space_id,
                deleted_by=await self._vetted_actor(r, space_id, provider, from_host),
            )
        _log_tombstone_refusals("zone", provider, space_id, cross_space, 0)

    async def _vetted_actor(
        self, r: dict[str, Any], space_id: str, provider: str, from_host: bool
    ) -> str:
        """The ``actor_user_id`` a gallery / zone tombstone may record as its
        ``deleted_by``. Those live delete rules judge the household, not an
        actor, so a record naming a stranger is not refused — but from a
        member household the name is kept only when the provider speaks for
        that user (seated on it, as ``_writer_delete_admits`` requires for
        stickies / events); otherwise the delete is recorded as nobody's.
        The host's stream is taken whole, like every host record."""
        actor = str(r.get("actor_user_id") or "")
        if not actor or from_host:
            return actor
        if self._authorship is None:
            return ""
        event = FederationEvent(
            msg_id=f"sync:{space_id}:actor",
            event_type=FederationEventType.SPACE_SYNC_CHUNK,
            from_instance=provider,
            to_instance="",
            timestamp="",
            payload={},
            space_id=space_id,
        )
        if await self._authorship.acts_for(event, space_id, actor, any_role=True):
            return actor
        return ""

    async def _page_seq_hints(
        self, records: list[dict[str, Any]], space_id: str, *, provider: str
    ) -> None:
        """On the **host**: a member household's ``pages`` records carry
        the ``seq`` it mirrors. A host restored from a backup raises its seq
        floor to it, so its next own edit lands above every version members
        hold — they take it. Only the number is read (the highest per page
        in the chunk), only for live pages held here, and only from a
        **writer** household seated in the space; the content is never
        taken this way (and the admission drops the records right after).
        :meth:`PageConflictService.raise_floor` bounds each raise and rate
        limits them per provider, page and day."""
        engine = self._page_conflicts
        if engine is None or not records or self._authorship is None:
            return
        mode, host = await engine.mode(space_id)
        if mode is not PageMode.HOST or not provider or provider == host:
            return
        highest: dict[str, int] = {}
        for r in records:
            page_id = str(r.get("id") or r.get("page_id") or "")
            seq = r.get("seq")
            if (
                not page_id
                or not isinstance(seq, int)
                or isinstance(seq, bool)
                or seq <= 0
            ):
                continue
            highest[page_id] = max(seq, highest.get(page_id, 0))
        if not highest:
            return
        event = FederationEvent(
            msg_id=f"sync:{space_id}:pages",
            event_type=FederationEventType.SPACE_SYNC_CHUNK,
            from_instance=provider,
            to_instance="",
            timestamp="",
            payload={},
            space_id=space_id,
        )
        if not await self._authorship.writes_here(event, space_id):
            return
        for page_id, seq in highest.items():
            await engine.raise_floor(space_id, page_id, seq, provider=provider)

    async def _is_host(self, space_id: str, provider: str) -> bool:
        """Is ``provider`` the household hosting ``space_id``?"""
        space = await self._space_repo.get(space_id)
        return (
            bool(provider)
            and space is not None
            and (space.owner_instance_id == provider)
        )

    async def _is_host_version(
        self, space_id: str, provider: str, r: dict[str, Any]
    ) -> bool:
        """Is ``r`` a host version (``seq``) streamed by the space's host?"""
        if "seq" not in r or not provider:
            return False
        space = await self._space_repo.get(space_id)
        return space is not None and space.owner_instance_id == provider

    async def _apply_page_version(
        self, page: Page, r: dict[str, Any], space_id: str, provider: str
    ) -> bool:
        """v_48 host-sequenced pages. ``True`` when handled here:

        * on the **host** every page record is ignored — it is the pages'
          sequencer; a member's new page reaches it as a create proposal,
          never as an unsequenced row it would hold but never broadcast;
        * a record streamed by the space's host carrying ``seq`` is the
          host's version: mirrored by ``seq`` (newer applies, older never
          reverts) — even when we have not seen the host's v_48
          capabilities yet;
        * any other record never updates a page held here and lands
          unsequenced (``seq`` 0 — a member cannot forge the host's order);
        * under a pre-v_48 host a record without ``seq`` is taken as before.
        """
        engine = self._page_conflicts
        if engine is None:
            return False
        mode, host = await engine.mode(space_id)
        if mode is PageMode.HOST:
            return True
        if host and provider == host and "seq" in r:
            version = canonical_from_wire(r)
            if version is None:
                log.warning(
                    "space sync: page %s in %s — malformed host version; skipped",
                    page.id,
                    space_id,
                )
                return True
            await engine.mirror(space_id=space_id, page_id=page.id, version=version)
            return True
        if mode is PageMode.LEGACY:
            return False
        held = await self._page_repo.get_space_page(page.id, space_id=space_id)
        return held is not None

    # ─── Who may stream what (§24.11 authorship) ─────────────────────

    async def _admit(
        self,
        resource: str,
        space_id: str,
        records: list[dict[str, Any]],
        *,
        provider: str,
        held_back: "HeldBack",
    ) -> list[dict[str, Any]]:
        """The records of this chunk the ``provider`` may write here.

        A chunk from the space's **host** is taken whole: the host is the
        roster and moderation authority, and its snapshot is the state the
        space converges on.

        Any other provider is a member household — the §25.6 scheduler
        syncs with every confirmed co-member on a timer, unasked for by any
        user — so its records are held to the same rules as a live event
        (``federation/space_authorship.py``): it may only **add** rows
        (never overwrite one we hold, whose author, content and moderation
        state stand), each attributed to a member seated on it; the roster
        and bans are the host's alone, and zones a moderator's. A record of
        an access-levelled feature (posts, pages, tasks, stickies, calendar)
        must also pass the space's level for its creator — an ``ADMIN_ONLY``
        feature takes only an admin's rows from a member household (§4.3).

        Content into a space that is **archived** here is refused first,
        from any provider :func:`~socialhome.federation.space_scope
        .archive_refusal` refuses — the same decision the live §24.11 gate
        takes, so a peer cannot write into the read-only snapshot by
        streaming it instead of sending it. The roster
        (:data:`ROSTER_RESOURCES`) still converges, and so do removals
        (:data:`REMOVAL_RESOURCES`), as the live gate lets deletes through.
        """
        space = await self._space_repo.get(space_id)
        if resource not in ROSTER_RESOURCES and resource not in REMOVAL_RESOURCES:
            reason = archive_refusal(space, provider)
            if reason is not None:
                log.info(
                    "space sync: refused %d %s record(s) from %s — space %s "
                    "is %s here (read-only)",
                    len(records),
                    resource,
                    provider,
                    space_id,
                    reason,
                )
                return []
        if space is not None and provider and space.owner_instance_id == provider:
            return records
        if self._authorship is None:
            log.warning(
                "space sync: %d %s record(s) for %s from non-host %s — no "
                "authorship binder wired; refusing them",
                len(records),
                resource,
                space_id,
                provider,
            )
            return []
        event = FederationEvent(
            msg_id=f"sync:{space_id}:{resource}",
            event_type=FederationEventType.SPACE_SYNC_CHUNK,
            from_instance=provider,
            to_instance="",
            timestamp="",
            payload={},
            space_id=space_id,
        )
        admitted: list[dict[str, Any]] = []
        refused = 0
        for r in records:
            if await self._admit_record(resource, space_id, r, event, held_back):
                admitted.append(r)
            else:
                refused += 1
        if refused:
            log.info(
                "space sync: kept %d of %d %s record(s) from non-host %s for %s "
                "(the rest exist already, or name no member of that household)",
                len(admitted),
                len(records),
                resource,
                provider,
                space_id,
            )
        return admitted

    async def _admit_record(
        self,
        resource: str,
        space_id: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: "HeldBack",
    ) -> bool:
        """The live-event rules for one record: its authorship, then — for
        the access-levelled features — the space's level for its creator
        (§4.3, v_42; e.g. a member household cannot stream a page into an
        ``ADMIN_ONLY`` wiki that it could not have sent live)."""
        if not await self._authored_record(resource, space_id, r, event, held_back):
            return False
        access = _ACCESS_FEATURE_OF.get(resource)
        if access is None:
            return True
        feature, creator_keys = access
        creator = next((str(r[k]) for k in creator_keys if r.get(k)), None)
        assert self._authorship is not None
        return await self._authorship.access_admits(
            event,
            space_id,
            feature,
            ContentAction.CREATE,
            actor=creator,
            row_owner=creator or "",
        )

    async def _authored_record(
        self,
        resource: str,
        space_id: str,
        r: dict[str, Any],
        event: FederationEvent,
        held_back: "HeldBack",
    ) -> bool:
        auth = self._authorship
        assert auth is not None
        rid = str(r.get("id") or "")
        match resource:
            case "members" | "bans":
                return False
            case "member_pictures":
                return await auth.acts_for(
                    event, space_id, str(r.get("user_id") or ""), any_role=True
                )
            case "posts":
                if not rid or await self._space_post_repo.get(rid) is not None:
                    return False
                return await self._may_author(
                    held_back, event, space_id, str(r.get("author") or "")
                )
            case "posts_deleted":
                # The live SPACE_POST_DELETED rule
                # (``FederationInboundService._owned_post_mutation_allowed``):
                # a post held live here IN THIS SPACE, removed by its
                # author's household or one with content authority (host /
                # admin / moderator seat — ``may_mutate``), then the space's
                # ``posts`` level for the delete, for the user who made it:
                # a moderator removal names its moderator
                # (``actor_user_id``, migration 0084) — who must be seated
                # on the provider and pass ``moderates_as`` /
                # ``admin_as`` there, as the live event's actor does; the
                # author's own delete names nobody and counts as the
                # author's when the provider speaks for the author. A
                # removal recorded before 0084 names nobody: from a
                # moderator household it passes only an ``OPEN`` level (the
                # host's own stream carries it regardless).
                rid = rid or str(r.get("post_id") or "")
                held_post = await self._space_post_repo.get(rid) if rid else None
                if (
                    held_post is None
                    or held_post[0] != space_id
                    or held_post[1].deleted
                ):
                    return False
                author = held_post[1].author
                if not await auth.may_mutate(event, space_id, author):
                    return False
                actor = (
                    str(r.get("actor_user_id") or "") or None
                    if r.get("moderated")
                    else None
                )
                if actor is None and await auth.acts_for(
                    event, space_id, author, any_role=True
                ):
                    actor = author
                return await auth.access_admits(
                    event,
                    space_id,
                    "posts",
                    ContentAction.DELETE,
                    actor=actor,
                    row_owner=author,
                    quiet=True,
                )
            case "comments_deleted":
                # The live SPACE_COMMENT_DELETED rule
                # (``_owned_comment_mutation_allowed``): a comment held live
                # here on a post of THIS SPACE, removed by its author's
                # household or one with content authority.
                rid = rid or str(r.get("comment_id") or "")
                held_comment = (
                    await self._space_post_repo.get_comment(rid) if rid else None
                )
                if held_comment is None or held_comment.deleted:
                    return False
                parent = await self._space_post_repo.get(held_comment.post_id)
                if parent is None or parent[0] != space_id:
                    return False
                return await auth.may_mutate(event, space_id, held_comment.author)
            case "comments":
                if not rid or await self._space_post_repo.get_comment(rid) is not None:
                    return False
                return await self._may_author(
                    held_back,
                    event,
                    space_id,
                    str(r.get("author") or ""),
                    subscriber_comment=True,
                )
            case "task_lists":
                # A member household only adds owner-bound lists (minted by
                # every v_40 sender); a legacy (pre-v_40) list id is taken
                # from the host alone, whose chunks never reach here — so a
                # member can't squat a list id another space holds.
                if not rid or not is_owner_bound(rid):
                    return False
                if await self._space_task_repo.get_list(rid) is not None:
                    return False
                if await self._space_task_repo.is_list_deleted(rid, space_id=space_id):
                    return False  # deleted here; the tombstone wins
                return await self._may_author(
                    held_back, event, space_id, str(r.get("created_by") or "")
                )
            case "task_lists_deleted":
                # The live SPACE_TASK_LIST_DELETED rule
                # (:meth:`_writer_delete_admits`) for a list held live here
                # IN THIS SPACE.
                rid = rid or str(r.get("list_id") or "")
                held = await self._space_task_repo.get_list(rid) if rid else None
                if held is None or held[0] != space_id:
                    return False
                return await self._writer_delete_admits(
                    event, space_id, "tasks", r, row_owner=held[1].created_by
                )
            case "tasks_deleted":
                # The live SPACE_TASK_DELETED rule for a task held live here
                # IN THIS SPACE.
                rid = rid or str(r.get("task_id") or "")
                held_task = await self._space_task_repo.get(rid) if rid else None
                if held_task is None or held_task[0] != space_id:
                    return False
                return await self._writer_delete_admits(
                    event, space_id, "tasks", r, row_owner=held_task[1].created_by
                )
            case "tasks" | "tasks_archived":
                if not rid or await self._space_task_repo.get(rid) is not None:
                    return False
                if await self._space_task_repo.is_task_deleted(rid, space_id=space_id):
                    return False  # deleted here; the tombstone wins
                return await self._may_author(
                    held_back, event, space_id, str(r.get("created_by") or "")
                )
            case "pages_deleted":
                # The live SPACE_PAGE_DELETED rule: a writer household
                # removes a page held live here IN THIS SPACE, if the
                # space's ``pages`` level admits the delete for the user who
                # made it (``actor_user_id``), who must be seated on the
                # provider. Refusals are counted in :meth:`_admit`.
                rid = rid or str(r.get("page_id") or "")
                held_page = await self._page_repo.get(rid) if rid else None
                if held_page is None or held_page.space_id != space_id:
                    return False
                return await self._writer_delete_admits(
                    event, space_id, "pages", r, row_owner=held_page.created_by
                )
            case "pages":
                if not rid or await self._page_repo.get(rid) is not None:
                    return False
                if await self._page_repo.is_page_deleted(rid, space_id=space_id):
                    return False  # deleted here; the tombstone wins
                creator = str(r.get("created_by") or "")
                if creator:
                    return await self._may_author(held_back, event, space_id, creator)
                return await auth.writes_here(event, space_id)
            case "stickies_deleted":
                # The live SPACE_STICKY_DELETED rule
                # (``_collaborative_write_allowed``, feature ``stickies``)
                # for a sticky held live here IN THIS SPACE.
                held_sticky = await self._sticky_repo.get(rid) if rid else None
                if held_sticky is None or held_sticky.space_id != space_id:
                    return False
                return await self._writer_delete_admits(
                    event, space_id, "stickies", r, row_owner=held_sticky.author
                )
            case "stickies":
                if not rid or await self._sticky_repo.get(rid) is not None:
                    return False
                if await self._sticky_repo.is_deleted(rid, space_id=space_id):
                    return False  # deleted here; the tombstone wins
                return await self._may_author(
                    held_back,
                    event,
                    space_id,
                    str(r.get("author") or r.get("created_by") or ""),
                )
            case "calendar_deleted":
                # The live SPACE_CALENDAR_EVENT_DELETED rule (feature
                # ``calendar``) for an event held live here IN THIS SPACE.
                held_event = (
                    await self._space_calendar_repo.get_event(rid) if rid else None
                )
                if held_event is None or held_event[0] != space_id:
                    return False
                return await self._writer_delete_admits(
                    event,
                    space_id,
                    "calendar",
                    r,
                    row_owner=held_event[1].created_by,
                )
            case "calendar":
                if (
                    not rid
                    or await self._space_calendar_repo.get_event(rid) is not None
                ):
                    return False
                if await self._space_calendar_repo.is_event_deleted(
                    rid, space_id=space_id
                ):
                    return False  # deleted here; the tombstone wins
                return await self._may_author(
                    held_back, event, space_id, str(r.get("created_by") or "")
                )
            case "gallery_albums_deleted":
                # The live SPACE_GALLERY_ALBUM_DELETED rule
                # (``_gallery_album_mutable``): a user album held live here
                # IN THIS SPACE, deleted by its owner's household or one
                # with settings authority (host / admin — not a moderator).
                held_album = await self._gallery_repo.get_album(rid) if rid else None
                if (
                    held_album is None
                    or held_album.space_id != space_id
                    or held_album.is_system
                ):
                    return False
                return await auth.may_mutate(
                    event, space_id, held_album.owner_user_id or "", settings=True
                )
            case "gallery_items_deleted":
                # The live SPACE_GALLERY_ITEM_DELETED rule: an item held live
                # here in an album of THIS SPACE, deleted by its uploader's
                # household or one with content authority.
                held_item = await self._gallery_repo.get_item(rid) if rid else None
                if held_item is None:
                    return False
                item_album = await self._gallery_repo.get_album(held_item.album_id)
                if item_album is None or item_album.space_id != space_id:
                    return False
                return await auth.may_mutate(event, space_id, held_item.uploaded_by)
            case "gallery":
                if r.get("kind") == "album":
                    if r.get("is_system"):
                        return False
                    owner = str(r.get("owner_user_id") or r.get("owner_id") or "")
                    if await auth.acts_for(event, space_id, owner):
                        return True
                    if await auth.trails_seat(space_id, owner):
                        held_back.add()
                    return False
                if not rid or await self._gallery_repo.get_item(rid) is not None:
                    return False
                if await self._gallery_repo.is_item_deleted(rid, space_id=space_id):
                    return False  # deleted here; the tombstone wins
                return await self._may_author(
                    held_back,
                    event,
                    space_id,
                    str(r.get("uploaded_by") or r.get("uploader") or ""),
                )
            case "schedules":
                post_id = str(r.get("post_id") or "")
                if self._poll_repo is None or not post_id:
                    return False
                if await self._poll_repo.get_schedule_meta(post_id) is not None:
                    return False
                return await self._anchor_author_ok(event, space_id, post_id)
            case "space_zones_deleted":
                # The live SPACE_ZONE_DELETED rule: an admin household
                # (zones are admin-only) removes a zone held live here IN
                # THIS SPACE.
                held_zone = (
                    await self._zone_repo.get(rid)
                    if rid and self._zone_repo is not None
                    else None
                )
                if held_zone is None or held_zone.space_id != space_id:
                    return False
                return await auth.is_admin_household(event, space_id)
            case "space_zones":
                if (
                    rid
                    and self._zone_repo is not None
                    and await self._zone_repo.is_deleted(rid, space_id=space_id)
                ):
                    return False  # deleted here; the tombstone wins
                return await auth.is_admin_household(event, space_id)
            case "timetables":
                # Moderator-only, per user, like the live event: the
                # household moderates and the recorded editor is its admin;
                # a timetable new here also names a creator it speaks for.
                if self._timetable_repo is None or not rid:
                    return False
                if not await auth.is_admin_household(event, space_id):
                    return False
                if not await auth.admin_as(
                    event, space_id, str(r.get("updated_by") or "")
                ):
                    return False
                if await self._timetable_repo.get(rid) is not None:
                    return True
                return await self._may_author(
                    held_back, event, space_id, str(r.get("created_by") or "")
                )
            case "chat_messages" | "chat_messages_deleted":
                # Judged record by record by the chat sink with the live
                # ``SPACE_CHAT_MESSAGE_CREATED`` rule (owner-bound id, the
                # author a writer this provider speaks for, held already →
                # skipped), whoever streams it.
                return True
            case "bazaar":
                post_id = str(r.get("post_id") or "")
                if self._bazaar_repo is None or not post_id:
                    return False
                if await self._bazaar_repo.get_listing(post_id) is not None:
                    return False
                got = await self._space_post_repo.get(post_id)
                seller = str(r.get("seller_user_id") or "")
                if got is None or got[1].author != seller:
                    return False
                return await self._may_author(held_back, event, space_id, seller)
        return False

    async def _may_author(
        self,
        held_back: "HeldBack",
        event: FederationEvent,
        space_id: str,
        user_id: str,
        **kwargs: Any,
    ) -> bool:
        """:meth:`SpaceAuthorship.may_author` for a record a member household
        adds — and, when it is refused because this space has no record of
        ``user_id`` at all yet (the roster gossip seating them trails the
        stream), the record is held back: the stream must not count as
        clean, or the provider's watermark would skip it until the daily
        full pass."""
        assert self._authorship is not None
        if await self._authorship.may_author(event, space_id, user_id, **kwargs):
            return True
        if await self._authorship.trails_seat(space_id, user_id):
            held_back.add()
        return False

    async def _writer_delete_admits(
        self,
        event: FederationEvent,
        space_id: str,
        feature: str,
        r: dict[str, Any],
        *,
        row_owner: str,
    ) -> bool:
        """The live delete rule of the collaborative features (tasks, task
        lists, pages, stickies, calendar events —
        ``_collaborative_write_allowed`` for ``ContentAction.DELETE``) for a
        streamed tombstone of a row held live here: a **writer** household
        removes it, if the space's ``feature`` level admits the delete for
        the user who made it (``actor_user_id``, the tombstone's
        ``deleted_by``), who must be seated on the provider. Refusals are
        counted in :meth:`_admit`'s one summary line (``quiet``), not logged
        per record on every scheduler tick."""
        auth = self._authorship
        assert auth is not None
        if not await auth.writes_here(event, space_id):
            return False
        actor = str(r.get("actor_user_id") or "") or None
        if actor is not None and not await auth.acts_for(
            event, space_id, actor, any_role=True
        ):
            return False
        return await auth.access_admits(
            event,
            space_id,
            feature,
            ContentAction.DELETE,
            actor=actor,
            row_owner=row_owner,
            quiet=True,
        )

    async def _anchor_author_ok(
        self, event: FederationEvent, space_id: str, post_id: str
    ) -> bool:
        assert self._authorship is not None
        got = await self._space_post_repo.get(post_id)
        if got is None or got[0] != space_id:
            return False
        return await self._authorship.may_author(event, space_id, got[1].author)

    def _legacy_album_deleted(self, space_id: str, album_id: str) -> bool:
        return self._legacy_album_deletes is not None and (
            self._legacy_album_deletes.is_deleted(space_id, album_id)
        )

    async def _persist_album(
        self,
        record: dict[str, Any],
        space_id: str,
        *,
        from_host: bool,
        held_back: HeldBack,
    ) -> bool:
        """Create a streamed album — or, held here already in this space for
        the same owner, take the **host's** edits of it (name, description,
        cover; the host's copy is the one the space converges on, as for
        every host record). ``True`` when the album is held here in this
        space afterwards, so its item count is recounted. A member
        household's record only ever adds an album (:meth:`_admit`)."""
        if self._legacy_album_deleted(
            space_id, str(record["id"])
        ) or await self._gallery_repo.is_album_deleted(
            str(record["id"]), space_id=space_id
        ):
            log.debug("sync: gallery album %s was deleted here — skipped", record["id"])
            return False
        # The album lands in the space this sync stream was gated for —
        # never the record's own ``space_id`` (another space, or NULL =
        # the household gallery), which is just untrusted payload.
        album = _album_from_record(record, space_id)
        if (
            check_owner_bound_id(
                GALLERY_ALBUM_KIND,
                album.id,
                space_id=space_id,
                owner_user_id=album.owner_user_id,
            )
            is OwnerBinding.MISMATCH
        ):
            # An owner-bound id commits to its creator; a record naming
            # anyone else is a claim on another household's album.
            log.warning(
                "sync: gallery album id %s is not bound to %r in space %s — skipped",
                album.id,
                album.owner_user_id,
                space_id,
            )
            return False
        held = await self._gallery_repo.get_album(album.id)
        if held is not None:
            if held.space_id != space_id or held.owner_user_id != album.owner_user_id:
                log.warning(
                    "sync: gallery album %s is held for another space or owner "
                    "— refusing the write for %s",
                    album.id,
                    space_id,
                )
                return False
            if from_host and not held.is_system:
                # An idempotent re-apply changes no synced column, so it
                # stamps nothing (migration 0086) — no echo.
                await self._gallery_repo.update_album_in_space(
                    album.id,
                    {
                        "name": album.name,
                        "description": album.description,
                        "cover_item_id": album.cover_item_id,
                    },
                    space_id=space_id,
                )
            return True
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
            held_back.add()  # a failed persist: the stream is not clean
            return False
        return True

    async def _persist_gallery_item(
        self,
        record: dict[str, Any],
        space_id: str,
        held_back: HeldBack,
    ) -> bool:
        """Store a streamed item. ``True`` when it landed in an album of this
        space (its count is then recounted)."""
        album_id = str(record.get("album_id") or "")
        if self._legacy_album_deleted(
            space_id, album_id
        ) or await self._gallery_repo.is_album_deleted(album_id, space_id=space_id):
            return False  # its album was deleted here; so was the item
        if await self._gallery_repo.is_item_deleted(
            str(record["id"]), space_id=space_id
        ):
            log.debug("sync: gallery item %s was deleted here — skipped", record["id"])
            return False
        item = GalleryItem(
            id=str(record["id"]),
            album_id=album_id,
            uploaded_by=str(record.get("uploaded_by") or record.get("uploader") or ""),
            item_type=str(record.get("item_type") or "photo"),
            # Only the canonical local ``api/media/<name>`` shape is stored.
            url=local_media_ref(record.get("url")) or "",
            thumbnail_url=local_media_ref(record.get("thumbnail_url")) or "",
            width=int(record.get("width") or 0),
            height=int(record.get("height") or 0),
            duration_s=record.get("duration_s"),
            caption=record.get("caption"),
            taken_at=record.get("taken_at") or record.get("day_taken"),
            sort_order=int(record.get("sort_order") or 0),
            created_at=record.get("created_at"),
        )
        try:
            # ``bump_count=False``: the caller recounts the album once the
            # chunk is in. The album must belong to this sync's space.
            if await self._gallery_repo.create_item_in_space(
                item, space_id=space_id, bump_count=False
            ):
                return True
            if await self._gallery_repo.get_album(album_id) is None:
                # Its album is not here yet (a chunk applied out of order):
                # a later stream lands it — not a refusal.
                held_back.add()
                return False
            log.warning(
                "sync: gallery item %s names album %s outside space %s "
                "— refusing the write",
                item.id,
                item.album_id,
                space_id,
            )
        except Exception:
            # Same reasoning as ``_persist_album``: redelivery is handled in
            # SQL, so a failure here is real (a missing parent album, a bad
            # record) and must not be silent — nor count as stored.
            log.warning(
                "sync: persisting gallery item %s (album=%s) failed",
                item.id,
                item.album_id,
                exc_info=True,
            )
            held_back.add()
        return False


#: ``resource → (id kind, owner fields in precedence order)`` for the sync
#: resources whose rows carry an owner-bound id (v_36). Gallery albums are
#: checked in :meth:`SpaceSyncReceiver._persist_album` (v_34).
_BOUND_RESOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "posts": (SPACE_POST_KIND, ("author",)),
    "posts_deleted": (SPACE_POST_KIND, ("author",)),
    "comments": (SPACE_COMMENT_KIND, ("author",)),
    "comments_deleted": (SPACE_COMMENT_KIND, ("author",)),
    "gallery": (GALLERY_ITEM_KIND, ("uploaded_by", "uploader")),
    "calendar": (SPACE_CALENDAR_EVENT_KIND, ("created_by",)),
    "task_lists": (SPACE_TASK_LIST_KIND, ("created_by",)),
    "tasks": (SPACE_TASK_KIND, ("created_by",)),
    "tasks_archived": (SPACE_TASK_KIND, ("created_by",)),
    "pages": (SPACE_PAGE_KIND, ("created_by",)),
    "stickies": (SPACE_STICKY_KIND, ("author", "created_by")),
    "stickies_deleted": (SPACE_STICKY_KIND, ("author",)),
    "calendar_deleted": (SPACE_CALENDAR_EVENT_KIND, ("created_by",)),
    "gallery_albums_deleted": (GALLERY_ALBUM_KIND, ("owner_user_id",)),
    "gallery_items_deleted": (GALLERY_ITEM_KIND, ("uploaded_by",)),
    "timetables": (SPACE_TIMETABLE_KIND, ("created_by",)),
    "chat_messages": (SPACE_CHAT_MESSAGE_KIND, ("author_user_id",)),
    "chat_messages_deleted": (SPACE_CHAT_MESSAGE_KIND, ("author_user_id",)),
}


#: Sync resources carrying an access-levelled feature (§4.3): resource →
#: (feature, the record keys naming its creator, in order). A member
#: household only ever ADDS rows by sync, so each is a ``CREATE``.
_ACCESS_FEATURE_OF: dict[str, tuple[str, tuple[str, ...]]] = {
    "posts": ("posts", ("author",)),
    "task_lists": ("tasks", ("created_by",)),
    "tasks": ("tasks", ("created_by",)),
    "tasks_archived": ("tasks", ("created_by",)),
    "pages": ("pages", ("created_by",)),
    "stickies": ("stickies", ("author", "created_by")),
    "calendar": ("calendar", ("created_by",)),
}


def _claims_bound_id(resource: str, space_id: str, r: dict[str, Any]) -> bool:
    """Is ``r`` a claim on an owner-bound id for somebody else? (Logged.)"""
    bound = _BOUND_RESOURCES.get(resource)
    if bound is None or (resource == "gallery" and r.get("kind") != "item"):
        return False
    kind, owner_fields = bound
    owner = next((str(r[f]) for f in owner_fields if r.get(f)), "")
    return owner_bound_id_refused(
        kind,
        str(r.get("id") or ""),
        space_id=space_id,
        owner_user_id=owner,
        context=f"space sync ({resource})",
    )


def _bound_here(kind: str, row_id: str, space_id: str, owner: str) -> bool:
    """Is ``row_id`` owner-bound to ``owner`` in ``space_id``? The condition
    for a host stub of an id never held here: ids are global, so a stub for
    another space's id (or a legacy, unbound one) would block a real row."""
    return (
        check_owner_bound_id(kind, row_id, space_id=space_id, owner_user_id=owner)
        is OwnerBinding.VALID
    )


def _log_tombstone_refusals(
    what: str, provider: str, space_id: str, cross_space: list[str], unbound: int
) -> None:
    """One summary per chunk of the tombstone records that were refused."""
    if cross_space:
        log.warning(
            "space sync: %d %s tombstone(s) from %s for %s name a %s held in "
            "another space — refused: %s",
            len(cross_space),
            what,
            provider,
            space_id,
            what,
            ", ".join(cross_space[:5]),
        )
    if unbound:
        log.info(
            "space sync: %d %s tombstone(s) from %s for %s name a %s never "
            "held here whose id is not bound to this space (or, for a "
            "comment / gallery item, whose post / album is not held here) — "
            "no stub recorded",
            unbound,
            what,
            provider,
            space_id,
            what,
        )


def _log_sync_refusal(what: str, row_id: str, space_id: str) -> None:
    """WARNING for a sync record the scoped repo refused: the id belongs
    to another space (or, for a task, its list is not in this space)."""
    log.warning(
        "space sync: %s %s is not writable in space %s — refusing the write",
        what,
        row_id,
        space_id,
    )


# ─── Record → domain helpers ────────────────────────────────────────


def _album_from_record(record: dict[str, Any], space_id: str) -> GalleryAlbum:
    """The album a sync record describes, filed under ``space_id``."""
    return GalleryAlbum(
        id=str(record["id"]),
        space_id=space_id,
        owner_user_id=(
            # System albums have no human owner; carry NULL.
            None
            if record.get("is_system")
            else (
                str(record.get("owner_user_id") or record.get("owner_id") or "") or None
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
        # Media references only in the local-upload shape.
        media_url=local_media_ref(r.get("media_url")),
        comment_count=int(r.get("comment_count") or 0),
        pinned=bool(r.get("pinned", False)),
        deleted=bool(r.get("deleted", False)),
        edited_at=_parse_iso(r.get("edited_at")) if r.get("edited_at") else None,
        moderated=bool(r.get("moderated", False)),
        file_meta=file_meta,
        # An unannounced listing / event anchor must stay out of the
        # joiner's feed exactly as it is out of the provider's.
        hidden_from_feed=bool(r.get("hidden_from_feed", False)),
        # The post's image gallery — the media bytes that follow are
        # matched against these names. Local-upload references, feed-capped.
        image_urls=local_media_refs(r.get("image_urls"), limit=FEED_POST_MAX_IMAGES),
        # The author-built link card (never re-fetched here), re-validated.
        link_preview=wire_link_preview(r.get("link_preview")),
        # A joiner sees the reactions the provider holds (a held post keeps
        # its own — see the ``posts`` branch).
        reactions=_reactions_from_record(r.get("reactions")),
    )


def _reactions_from_record(raw: object) -> dict[str, frozenset[str]]:
    """The exporter's ``{emoji: [user_id, …]}``, malformed entries dropped,
    capped at the per-post distinct-emoji maximum."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, frozenset[str]] = {}
    for emoji, users in raw.items():
        if len(out) >= MAX_DISTINCT_REACTIONS_PER_POST:
            break
        if not isinstance(emoji, str) or not emoji or not isinstance(users, list):
            continue
        ids = frozenset(u for u in users if isinstance(u, str) and u)
        if ids:
            out[emoji] = ids
    return out


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
        media_url=local_media_ref(r.get("media_url")),
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
    if not r.get("id") or not r.get("author"):
        return None
    # Shared sticky field rules: a non-hex colour (rendered as CSS) is
    # replaced, coordinates clamped, content sanitised + capped.
    fields = coerce_peer_sticky(
        content=r.get("content"),
        color=r.get("color"),
        position_x=r.get("position_x"),
        position_y=r.get("position_y"),
    )
    if not fields.content:
        return None
    if fields.truncated:
        log.warning(
            "space sync: sticky %s in space %s — content over %d characters, truncated",
            r["id"],
            space_id,
            MAX_STICKY_CONTENT_LENGTH,
        )
    return Sticky(
        id=str(r["id"]),
        author=str(r["author"]),
        content=fields.content,
        color=fields.color,
        position_x=fields.position_x,
        position_y=fields.position_y,
        created_at=str(r.get("created_at") or ""),
        updated_at=str(r.get("updated_at") or ""),
        space_id=space_id or r.get("space_id"),
    )


def _zone_from_record(
    r: dict[str, Any], space_id: str, *, provider: str
) -> SpaceZone | None:
    """Reconstruct a :class:`SpaceZone` from an exporter chunk record.

    Lenient: skip the row rather than raising if a malformed record
    leaks into the chunk. The federation layer has already verified
    the envelope signature, so the worst case is a peer with a buggy
    catalogue — log and drop the offending row, keep the others.

    Name and colour pass the same validation as the local API
    (§23.8.7) — a refused row is a WARNING naming the space and the
    provider, never the name.
    """
    zone_id = r.get("id")
    if not zone_id or not r.get("name"):
        return None
    try:
        name = validate_zone_name(r.get("name"))
        color = validate_zone_color(r.get("color"))
        # Same geometry rules as the local API: finite, in range, 4 dp
        # (CLAUDE.md GPS rule), radius 25 m – 50 km.
        latitude = validate_zone_coord(r.get("latitude"), name="latitude", limit=90)
        longitude = validate_zone_coord(r.get("longitude"), name="longitude", limit=180)
        radius_m = validate_zone_radius(r.get("radius_m"))
    except ValueError as exc:
        log.warning(
            "space sync from %s: zone %s in space %s refused — %s",
            provider,
            str(zone_id)[:64],
            space_id,
            exc,
        )
        return None
    return SpaceZone(
        id=str(zone_id),
        space_id=space_id or str(r.get("space_id") or ""),
        name=name,
        latitude=latitude,
        longitude=longitude,
        radius_m=radius_m,
        color=color,
        created_by=str(r.get("created_by") or ""),
        created_at=str(r.get("created_at") or ""),
        updated_at=str(r.get("updated_at") or ""),
    )


def _calendar_from_record(r: dict[str, Any], *, provider: str) -> CalendarEvent | None:
    """A ``calendar`` record as the event row it streams — read like the
    live ``SPACE_CALENDAR_EVENT_*`` payload, rule and all: a record that
    dropped ``rrule`` / ``cover_url`` / ``location`` / ``announce_in_feed``
    upserted a member's copy of a series into a one-off event and wiped the
    rest, on every stream.

    An id of the form ``<event id>@<its own start>`` is not a row: it is
    an occurrence an older provider expanded a series into (one record per
    occurrence, ten years either side of its clock). The series streams as
    its own row; storing its occurrences would duplicate it as one-off
    events, so they are skipped."""
    rid = str(r.get("id") or "")
    if (
        not rid
        or is_occurrence_id(rid, str(r.get("start") or ""))
        or not r.get("calendar_id")
        or not r.get("summary")
        or not r.get("created_by")
    ):
        return None
    start = _parse_iso(r.get("start"))
    end = _parse_iso(r.get("end"))
    rrule = r.get("rrule")
    location = r.get("location")
    return CalendarEvent(
        id=rid,
        calendar_id=str(r["calendar_id"]),
        summary=str(r["summary"]),
        start=start,
        end=end,
        created_by=str(r["created_by"]),
        description=r.get("description"),
        all_day=bool(r.get("all_day", False)),
        attendees=tuple(str(a) for a in (r.get("attendees") or ())),
        mirrored_from=r.get("mirrored_from"),
        rrule=rrule if isinstance(rrule, str) and rrule else None,
        # Rendered as ``<img src>`` for every member: only a local media
        # reference — a third-party URL would leak their IPs.
        cover_url=verbatim_local_media_ref(r.get("cover_url")),
        location=location if isinstance(location, str) and location else None,
        # IANA wall-clock anchor — validated as on the live event; an older
        # peer omits it and gets ``"UTC"``.
        tz=coerce_tz(r.get("tz"), context=f"space sync calendar from {provider}"),
        # §23.15 opt-in feed mirror; absent on an older provider → True, the
        # live event's default.
        announce_in_feed=bool(r.get("announce_in_feed", True)),
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
        image_urls=local_media_refs(r.get("image_urls"), limit=BAZAAR_MAX_IMAGES),
        price=r.get("price"),
        start_price=r.get("start_price"),
        step_price=r.get("step_price"),
        winner_user_id=r.get("winner_user_id"),
        winning_price=r.get("winning_price"),
        sold_at=r.get("sold_at"),
    )


def _timetable_from_record(r: dict[str, Any], space_id: str) -> Timetable | None:
    """A synced timetable record, or ``None`` (logged at WARNING) when it is
    malformed, out of bounds, carries assignees, or its id is not bound to
    its creator in this space — space timetables are owner-bound from their
    first release, so a legacy-shaped id is refused too."""
    try:
        tt = from_wire_dict(r)
        validate(tt)
    except Exception as exc:  # hostile input: never raise out of the stream
        log.warning(
            "space sync: unusable timetable record for %s — skipped: %.200s",
            space_id,
            exc,
        )
        return None
    if tt.assignees:
        log.warning(
            "space sync: timetable %s for %s names assignees — skipped",
            tt.id,
            space_id,
        )
        return None
    if (
        check_owner_bound_id(
            SPACE_TIMETABLE_KIND, tt.id, space_id=space_id, owner_user_id=tt.created_by
        )
        is not OwnerBinding.VALID
    ):
        log.warning(
            "space sync: timetable id %s is not bound to %r in space %s — skipped",
            tt.id,
            tt.created_by,
            space_id,
        )
        return None
    return tt
