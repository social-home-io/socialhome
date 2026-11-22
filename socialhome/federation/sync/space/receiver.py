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

This module keeps the chunk path (verify, decrypt, admit, dispatch) and
the per-record authorship dispatch. The per-resource persist and
authorship rules live in one mixin per resource family, composed into
:class:`SpaceSyncReceiver`:

* :mod:`.receive_members` — ``members``, ``member_pictures``, ``bans``
* :mod:`.receive_posts` — ``posts``, ``comments``, ``polls`` + tombstones
* :mod:`.receive_tasks` — ``task_lists``, ``tasks``, ``tasks_archived``
* :mod:`.receive_pages` — ``pages`` (v_48 versions), ``pages_deleted``
* :mod:`.receive_stickies` — ``stickies`` + tombstones
* :mod:`.receive_calendar` — ``calendar``, RSVPs, ``schedules``
* :mod:`.receive_gallery` — ``gallery`` albums / items + tombstones
* :mod:`.receive_bazaar` — ``bazaar`` listings (FAQ, bids)
* :mod:`.receive_zones` — ``space_zones`` + tombstones
* :mod:`.receive_timetables` — ``timetables`` + tombstones

:mod:`.receiver_base` holds the slots they read and the cross-resource
helpers (who is the host, the live authorship rules, v_80 media names);
:mod:`.stream_health` the per-stream bookkeeping (:class:`HeldBack`,
:class:`StreamHealth`).
"""

from __future__ import annotations

import logging
import pathlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

import orjson as _orjson

from ....crypto import derive_instance_id
from ....domain.events import SpaceSyncComplete
from ....domain.federation import FederationEvent
from ....domain.space import ContentAction, parse_have_seq
from ....infrastructure.event_bus import EventBus
from ....services.space_media_names import SpaceMediaNamer
from ....services.space_service import apply_inviter_reproof
from ....services.space_writer_households import holds_member_seat
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
    owner_bound_id_refused,
)
from ...space_scope import archive_refusal
from .exporter import (
    ALLOWED_RESOURCES,
    FETCHED_ROWS_RESOURCE,
    LISTING_RESOURCE,
    REMOVAL_RESOURCES,
    ROSTER_RESOURCES,
    SENTINEL_RESOURCE,
    parse_chunk,
)
from .receive_bazaar import BazaarMixin
from .receive_calendar import CalendarMixin
from .receive_gallery import GalleryMixin
from .receive_members import MembersMixin
from .receive_pages import PagesMixin
from .receive_posts import PostsMixin
from .receive_stickies import StickiesMixin
from .receive_tasks import TasksMixin
from .receive_timetables import TimetablesMixin
from .receive_zones import ZonesMixin
from .receiver_base import VERIFIED_ROW, ReceiverBase, _GfsMediaSink, _sync_event
from .stream_health import HeldBack, HeldBackStreaks, StreamHealth

if TYPE_CHECKING:
    from ....federation.space_authorship import SpaceAuthorship
    from ....repositories.bazaar_repo import AbstractBazaarRepo
    from ....repositories.calendar_repo import AbstractSpaceCalendarRepo
    from ....repositories.federation_repo import AbstractFederationRepo
    from ....repositories.gallery_repo import AbstractGalleryRepo
    from ....repositories.media_reference_repo import AbstractMediaReferenceRepo
    from ....repositories.page_repo import AbstractPageRepo
    from ....repositories.profile_picture_repo import (
        AbstractProfilePictureRepo,
    )
    from ....repositories.space_poll_repo import AbstractSpacePollRepo
    from ....repositories.space_post_repo import AbstractSpacePostRepo
    from ....repositories.space_repo import AbstractSpaceRepo
    from ....repositories.space_sync_watermark_repo import (
        AbstractSpaceSyncWatermarkRepo,
    )
    from ....repositories.space_zone_repo import AbstractSpaceZoneRepo
    from ....repositories.sticky_repo import AbstractStickyRepo
    from ....repositories.task_repo import AbstractSpaceTaskRepo
    from ....repositories.timetable_repo import AbstractSpaceTimetableRepo
    from ....services.legacy_album_deletes import LegacyAlbumDeletes
    from ....services.page_conflict_service import PageConflictService
    from ....services.pending_decrypts_cache import PendingDecryptsCache
    from ....services.space_crypto_service import SpaceContentEncryption
    from ...encoder import FederationEncoder

log = logging.getLogger(__name__)


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


class _HostHeard(Protocol):
    def heard(self, space_id: str) -> None: ...


class _StreamWatch(Protocol):
    def stream_started(self, space_id: str, provider: str) -> None: ...


class _SyncPageSink(Protocol):
    def sentinel_seen(self, space_id: str) -> None: ...

    async def take_listing(
        self, *, space_id: str, sync_id: str, provider: str, records: object
    ) -> None: ...

    async def listing_done(
        self, *, space_id: str, sync_id: str, provider: str
    ) -> None: ...

    async def take_rows(
        self, *, space_id: str, sync_id: str, provider: str, records: object
    ) -> None: ...


class SpaceSyncReceiver(
    MembersMixin,
    PostsMixin,
    TasksMixin,
    PagesMixin,
    StickiesMixin,
    CalendarMixin,
    GalleryMixin,
    BazaarMixin,
    ZonesMixin,
    TimetablesMixin,
    ReceiverBase,
):
    """Persist inbound space-sync chunks."""

    __slots__ = (
        "_applied_seqs",
        "_authorship",
        "_bazaar_repo",
        "_bus",
        "_chat_sink",
        "_crypto",
        "_encoder",
        "_federation_repo",
        "_foreign_system_albums",
        "_gallery_repo",
        "_gfs_media",
        "_health",
        "_host_contact",
        "_legacy_album_deletes",
        "_media_dir",
        "_media_names",
        "_media_refs",
        "_page_conflicts",
        "_page_repo",
        "_peer_version",
        "_pending_decrypts",
        "_poll_repo",
        "_profile_picture_repo",
        "_space_calendar_repo",
        "_space_post_repo",
        "_space_repo",
        "_space_task_repo",
        "_sticky_repo",
        "_streaks",
        "_stream_watch",
        "_sync_pages",
        "_timetable_repo",
        "_zone_repo",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        encoder: FederationEncoder,
        crypto: SpaceContentEncryption,
        federation_repo: AbstractFederationRepo,
        space_repo: AbstractSpaceRepo,
        space_post_repo: AbstractSpacePostRepo,
        space_task_repo: AbstractSpaceTaskRepo,
        page_repo: AbstractPageRepo,
        sticky_repo: AbstractStickyRepo,
        space_calendar_repo: AbstractSpaceCalendarRepo,
        gallery_repo: AbstractGalleryRepo,
        zone_repo: AbstractSpaceZoneRepo | None = None,
        bazaar_repo: AbstractBazaarRepo | None = None,
        profile_picture_repo: AbstractProfilePictureRepo | None = None,
        poll_repo: AbstractSpacePollRepo | None = None,
        pending_decrypts: PendingDecryptsCache | None = None,
        authorship: SpaceAuthorship | None = None,
        media_dir: pathlib.Path | None = None,
        media_refs: AbstractMediaReferenceRepo | None = None,
        legacy_album_deletes: LegacyAlbumDeletes | None = None,
        timetable_repo: AbstractSpaceTimetableRepo | None = None,
        page_conflicts: PageConflictService | None = None,
        applied_seqs: AbstractSpaceSyncWatermarkRepo | None = None,
        peer_version: Callable[[str], Awaitable[int]] | None = None,
    ) -> None:
        self._bus = bus
        #: A provider household's protocol version — the federation
        #: service's ``space_member_version`` (its paired row, else its mesh
        #: claim, else ``0``). ``None``: unknown, so nothing version-gated on
        #: the provider (the v_58 picture reset) is trusted.
        self._peer_version = peer_version
        #: §25.6 echo (migration 0087): where a clean stream's provider
        #: snapshot is recorded, per (space, provider household) — sent back
        #: as ``have_seq`` in the next periodic BEGIN. ``None``: never
        #: recorded, so every periodic session streams in full.
        self._applied_seqs = applied_seqs
        self._timetable_repo = timetable_repo
        #: v_55 — where ``chat_messages`` records go: the space-chat inbound
        #: handlers, which run each through the live create rule. ``None``
        #: drops the resource (see :meth:`attach_chat_sink`).
        self._chat_sink: ChatSyncSink | None = None
        #: v_95 — takes the signed sync page a sentinel carries.
        self._sync_pages: _SyncPageSink | None = None
        #: v_92 — told when the household acting as a space's host answers
        #: (:meth:`attach_host_contact`). ``None``: nobody is told.
        self._host_contact: _HostHeard | None = None
        #: Told when a stream of a space begins arriving (the sync status:
        #: "catching up"), see :meth:`attach_stream_watch`.
        self._stream_watch: _StreamWatch | None = None
        self._health = StreamHealth()
        self._streaks = HeldBackStreaks()
        #: Ids of other households' system albums seen in a stream — never
        #: stored here (see :meth:`_persist_album`); bounded.
        self._foreign_system_albums: OrderedDict[str, None] = OrderedDict()
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
        #: v_80 — keeps the ``media_refs`` a record streamed to a link-joined
        #: member carries (``None``: ignored).
        self._gfs_media: _GfsMediaSink | None = None
        #: Received space files take this household's names (sender-bound).
        self._media_names = SpaceMediaNamer()
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

    def attach_gfs_media(self, fetcher: _GfsMediaSink) -> None:
        """Wire the v_80 GFS media fetcher: a record streamed to a member
        seated from an invite link carries ``media_refs`` (its files in a
        GFS media cache, sync line 80); the fetcher keeps them and fetches
        the pictures."""
        self._gfs_media = fetcher

    def attach_media_names(self, namer: SpaceMediaNamer) -> None:
        """Wire the shared namer (it knows this household's instance id)."""
        self._media_names = namer

    def attach_sync_pages(self, pages: _SyncPageSink) -> None:
        """Wire the v_95 sync pages: a verified sentinel's end of stream
        is decrypted, verified and kept (``SyncPageService.receive``)."""
        self._sync_pages = pages

    def attach_chat_sink(self, sink: ChatSyncSink) -> None:
        """Wire where streamed space-chat messages (v_55) are applied."""
        self._chat_sink = sink

    def attach_host_contact(self, contact: _HostHeard) -> None:
        """Wire the v_92 host contact (:mod:`.host_contact`): every signed
        chunk of a space this household verifies from the household acting
        as its host here (``_acts_as_host``) marks the space heard — the
        ``__complete__`` sentinel of an empty incremental session included,
        the only answer a quiet space sends. The scheduler falls back to a
        trusted helper while the host stays silent."""
        self._host_contact = contact

    def attach_stream_watch(self, watch: _StreamWatch) -> None:
        """Wire the sync status (``SpaceSyncStatusService``): told once per
        stream, on its first verified chunk — never per chunk, never for a
        sentinel-only (quiet) session. It must not block: it does its reads
        in a task of its own, off this chunk path."""
        self._stream_watch = watch

    def _note_stream_started(self, space_id: str, provider: str) -> None:
        if self._stream_watch is None:
            return
        try:
            self._stream_watch.stream_started(space_id, provider)
        except Exception:
            log.exception("sync: noting the stream of space %s failed", space_id)

    async def _note_host_answer(self, space_id: str, provider: str) -> None:
        if self._host_contact is not None and await self._is_host(space_id, provider):
            self._host_contact.heard(space_id)

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

    async def _unpaired_provider_key(
        self, space_id: str, from_instance: str
    ) -> bytes | None:
        """The identity key to verify an UNPAIRED provider's chunk with.

        * The household this stub names as the space's host: the stub's
          ``host_identity_pk`` (#648).
        * Any other household of the space — e.g. the delegated admin
          household that invited us over the mesh and streams our catch-up:
          the key recorded on its own ``space_instances`` row for THIS space
          (migration 0078's column), accepted only when it derives to the
          sender (§4.1.2). That key authenticates the chunk and nothing
          more; what a non-host provider may write is still decided per
          record (:meth:`_admit`).

        Without one of these, no unpaired instance that merely names the
        space id is verified at all."""
        space = await self._space_repo.get(space_id)
        if space is None:
            return None
        if space.owner_instance_id == from_instance:
            key_hex = await self._space_repo.get_host_identity_pk(space_id)
        else:
            key_hex = await self._space_repo.get_space_instance_identity_pk(
                space_id, from_instance
            )
        if not key_hex:
            return None
        try:
            key = bytes.fromhex(key_hex)
        except ValueError:
            log.warning(
                "sync chunk: stored identity key for %s in space %s is not "
                "valid hex — dropping",
                from_instance,
                space_id,
            )
            return None
        if space.owner_instance_id != from_instance and (
            len(key) != 32 or derive_instance_id(key) != from_instance
        ):
            return None
        return key

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
            ed_public_key = await self._unpaired_provider_key(space_id, from_instance)
            if ed_public_key is None:
                self._health.failed(sync_id)
                log.debug(
                    "sync chunk from unknown instance %s for space %s — "
                    "no paired-peer row and no matching host / provider key; "
                    "dropping",
                    from_instance,
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
        # v_92: a verified chunk (or sentinel) of the space's acting host is
        # its answer to our BEGIN — the host is not silent.
        await self._note_host_answer(space_id, from_instance)

        # Sentinel path — publish end-of-stream + return.
        if resource == SENTINEL_RESOURCE:
            # Migration 0093: the owner's statement rides the sentinel —
            # kept when it is the owner's, for the key and epoch we pin,
            # and newer than ours. No config edit, no sequence bump.
            await self._keep_owner_statement(space_id, envelope.get("owner_statement"))
            # …and our inviting admin's fresh seed proof for the key we pin
            # resumes its trust after a rotation (never the pin move alone).
            # This stream's chunks were applied under the member rules (the
            # trust resumed only now), so like a held-back stream it is not
            # clean: the next session re-streams under the restored trust.
            reproved = await apply_inviter_reproof(
                self._space_repo,
                space_id,
                provider=from_instance,
                proof=envelope.get("inviter_proof"),
            )
            space_now = await self._space_repo.get(space_id)
            # An archive lifted (or set) while the stream ran: the lift
            # dropped our echo (migration 0090) so the next session is full
            # and delivers what the archive refused — recording this
            # stream's snapshot now would put the echo straight back.
            archive_changed = self._health.archive_changed(
                sync_id, archived=bool(space_now is not None and space_now.archived)
            )
            clean = (
                self._streaks.clean(
                    self._health.finish(sync_id, envelope.get("chunk_count")),
                    space_id,
                    from_instance,
                )
                and not reproved
            )
            if clean and not archive_changed:
                await self._record_applied(
                    space_id, from_instance, envelope.get("snapshot_seq")
                )
            # v_95: after the stream's snapshot is recorded — a held page
            # with no verdict yet is compared with what we now hold (the page
            # itself comes as its own ``SPACE_SYNC_PAGE`` event), and the
            # listings this round asked for are classified.
            if self._sync_pages is not None:
                self._sync_pages.sentinel_seen(space_id)
                await self._sync_pages.listing_done(
                    space_id=space_id, sync_id=sync_id, provider=from_instance
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

        if not self._health.begun(sync_id):
            space_then = await self._space_repo.get(space_id)
            self._health.begin(
                sync_id,
                archived=bool(space_then is not None and space_then.archived),
            )
            self._note_stream_started(space_id, from_instance)

        # Decrypt — an unknown resource too: its ``chunk_index`` (v_56) is
        # inside the payload, and in a numbered stream only indices count.
        epoch = int(envelope.get("epoch") or 0)
        ciphertext = str(envelope.get("encrypted_payload") or "")
        try:
            plaintext = await self._crypto.decrypt_chunk(
                space_id=space_id,
                epoch=epoch,
                sync_id=sync_id,
                ciphertext=ciphertext,
            )
        except Exception as exc:  # noqa: BLE001 — every branch below handles or logs it
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
            if resource not in ALLOWED_RESOURCES:
                # A newer provider's resource we cannot even open (a scheme
                # of its own): dropped by rule, as before v_56 — counted as
                # one chunk with no index (it cannot claim another's slot).
                self._health.opaque(sync_id)
                log.debug("unknown resource %r in sync chunk (opaque)", resource)
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
            body = _orjson.loads(plaintext)
            records = body.get("records") or []
            # v_56: the chunk's position in the stream, inside the signed and
            # encrypted payload; an older provider sends none.
            index = body.get("chunk_index")
        except Exception as exc:
            if resource not in ALLOWED_RESOURCES:
                self._health.opaque(sync_id)
                log.debug(
                    "unknown resource %r in sync chunk (opaque)",
                    resource,
                    exc_info=True,
                )
                return
            self._health.failed(sync_id)
            log.warning("sync chunk plaintext parse failed: %s", exc)
            return

        if resource == LISTING_RESOURCE:
            # v_95: bucket listings our BEGIN's ``buckets`` asked for —
            # the page service checks the session and the sender.
            if self._sync_pages is not None:
                await self._sync_pages.take_listing(
                    space_id=space_id,
                    sync_id=sync_id,
                    provider=from_instance,
                    records=records,
                )
            self._health.applied(sync_id, index)
            return

        if resource == FETCHED_ROWS_RESOURCE:
            # Phase 3: rows of a verified fetch we asked for — never applied
            # here as a stream; the page service checks each against the
            # signed page at the sentinel (:meth:`apply_verified`).
            if self._sync_pages is not None:
                await self._sync_pages.take_rows(
                    space_id=space_id,
                    sync_id=sync_id,
                    provider=from_instance,
                    records=records,
                )
            self._health.applied(sync_id, index)
            return

        if resource not in ALLOWED_RESOURCES:
            # A newer provider's resource: dropped by rule, never retried.
            self._health.applied(sync_id, index)
            log.debug("unknown resource %r in sync chunk", resource)
            return

        try:
            outcome = await self._apply(
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
        if outcome.failed:
            # A persist that failed where the caller logged rather than
            # raised: unclean — never forgiven as "held", but bounded by its
            # own, higher limit (PERSIST_FAILURE_LIMIT), so a row the
            # database refuses for good cannot force a full re-stream on
            # every session forever.
            self._health.persist_failed(sync_id, index)
            return
        if not outcome.count:
            self._health.applied(sync_id, index)
            return
        # A record refused only for now (its author's seat or its parent has
        # not reached us yet): the stream is unclean, so the provider keeps
        # its watermark and the next periodic session streams it again
        # (bounded — :data:`HELD_BACK_LIMIT`).
        self._health.held(sync_id, index)
        log.info(
            "sync %s: %s chunk for space %s from %s held records back for a "
            "retry — reporting the stream unclean",
            sync_id,
            resource,
            space_id,
            from_instance,
        )

    async def apply_verified(
        self, space_id: str, provider: str, resource: str, record: dict[str, Any]
    ) -> HeldBack:
        """Sync pages Phase 3: apply ONE row a member household (*provider*)
        delivered that was checked against the space authority's signed page
        (its committed part hashes to a leaf of a listing that hashes to the
        signed bucket). The row is the signer's own, so it passes where a
        host's record passes (:data:`~.receiver_base.VERIFIED_ROW`) — the
        same admission, the same merge rules (last writer wins, tombstones,
        a deleted row stays deleted), nothing else.

        What was held back only for now (``count``) or failed (``failed``):
        besides the merge rules' own holds, a row whose parent is not held
        here at all yet — a comment / poll / schedule / listing before its
        post, a task before its list, a gallery item before its album —
        counts as held back (Phase 3 re-review S1): it lands once the parent
        does, so it is no refusal by rule. Whether it landed is judged by
        the caller from its own rows."""
        token = VERIFIED_ROW.set((space_id, provider))
        try:
            outcome = await self._apply(resource, space_id, [record], provider=provider)
        finally:
            VERIFIED_ROW.reset(token)
        if not outcome.count and await self._parent_missing(space_id, resource, record):
            outcome.count += 1
        return outcome

    async def _parent_missing(
        self, space_id: str, resource: str, record: dict[str, Any]
    ) -> bool:
        """Whether *record*'s parent row is held nowhere here yet (in no
        space, and not deleted here) — the only case it may still arrive.
        One held in another space, or deleted here, is a refusal by rule.
        (A deleted post stays held, soft-deleted.)"""
        match resource:
            case "comments" | "polls" | "schedules" | "bazaar":
                post_id = str(record.get("post_id") or "")
                return (
                    bool(post_id) and await self._space_post_repo.get(post_id) is None
                )
            case "tasks" | "tasks_archived":
                list_id = str(record.get("list_id") or "")
                return (
                    bool(list_id)
                    and await self._space_task_repo.get_list(list_id) is None
                    and not await self._space_task_repo.is_list_deleted(
                        list_id, space_id=space_id
                    )
                )
            case "gallery":
                album_id = str(record.get("album_id") or "")
                return (
                    record.get("kind") != "album"
                    and bool(album_id)
                    and self._gallery_repo is not None
                    and await self._gallery_repo.get_album(album_id) is None
                    and not await self._gallery_repo.is_album_deleted(
                        album_id, space_id=space_id
                    )
                )
        return False

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
        :class:`HeldBack`) or a persist failed, so the stream must not count
        as clean."""
        outcome = await self._apply(resource, space_id, records, provider=provider)
        return not outcome.count and not outcome.failed

    async def _apply(
        self,
        resource: str,
        space_id: str,
        records: list[dict[str, Any]],
        *,
        provider: str,
    ) -> HeldBack:
        """:meth:`_dispatch`, with what was held back or failed."""
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
        if resource == "members":
            # Before admission: a member household's roster is never taken,
            # but a picture reset of a member it seats is its own to make
            # (the live ``SPACE_MEMBER_PROFILE_UPDATED`` rule) — and the
            # §25.6 scheduler streams from every co-member household.
            await self._apply_picture_resets(space_id, shaped, provider=provider)
        held_back = HeldBack()
        records = await self._admit(
            resource, space_id, shaped, provider=provider, held_back=held_back
        )
        # v_36: whoever streams it — the host included — a record may not
        # claim an owner-bound id for anybody but the user it commits to.
        records = [r for r in records if not _claims_bound_id(resource, space_id, r)]
        records = await self._spare_owner_people(resource, space_id, records, provider)
        if records:
            await self._persist(
                resource, space_id, records, provider=provider, held_back=held_back
            )
        return held_back

    async def _persist(
        self,
        resource: str,
        space_id: str,
        records: list[dict[str, Any]],
        *,
        provider: str,
        held_back: HeldBack,
    ) -> None:
        """Persist admitted records of ``resource``. A record that cannot
        land only for now is counted in ``held_back``."""
        if resource == "members":
            await self._persist_members(space_id, records)
        elif resource == "member_pictures":
            await self._persist_member_pictures(space_id, records, provider=provider)
        elif resource == "bans":
            await self._persist_bans(space_id, records)
        elif resource == "posts":
            await self._persist_posts(space_id, records, provider=provider)
        elif resource == "posts_deleted":
            await self._persist_post_tombstones(records, space_id, provider=provider)
        elif resource == "comments_deleted":
            await self._persist_comment_tombstones(records, space_id, provider=provider)
        elif resource == "comments":
            await self._persist_comments(space_id, records, provider=provider)
        elif resource == "task_lists":
            await self._persist_task_lists(space_id, records)
        elif resource == "task_lists_deleted":
            await self._persist_task_list_tombstones(
                records, space_id, provider=provider
            )
        elif resource == "tasks_deleted":
            await self._persist_task_tombstones(records, space_id, provider=provider)
        elif resource in ("tasks", "tasks_archived"):
            await self._persist_tasks(space_id, records)
        elif resource == "pages_deleted":
            await self._persist_page_tombstones(records, space_id, provider=provider)
        elif resource == "pages":
            await self._persist_pages(space_id, records, provider=provider)
        elif resource == "stickies_deleted":
            await self._persist_sticky_tombstones(records, space_id, provider=provider)
        elif resource == "stickies":
            await self._persist_stickies(space_id, records)
        elif resource == "calendar_deleted":
            await self._persist_calendar_tombstones(
                records, space_id, provider=provider
            )
        elif resource == "calendar":
            await self._persist_calendar(space_id, records, provider=provider)
        elif resource == "gallery_albums_deleted":
            await self._persist_album_tombstones(records, space_id, provider=provider)
        elif resource == "gallery_items_deleted":
            await self._persist_item_tombstones(
                records, space_id, provider=provider, held_back=held_back
            )
        elif resource == "gallery":
            await self._persist_gallery(
                space_id, records, provider=provider, held_back=held_back
            )
        elif resource == "polls":
            await self._persist_polls(records, space_id, provider=provider)
        elif resource == "schedules":
            await self._persist_schedules(space_id, records, provider=provider)
        elif resource in (
            "space_zones",
            "space_zones_deleted",
        ) and not await holds_member_seat(self._space_repo, space_id):
            # We only follow this space: its members' places are not ours
            # to keep (owner decision 13), whichever household streams them
            # — the live SPACE_ZONE_* events are dropped the same way.
            log.info(
                "sync: %d %s records from %s for %s dropped: this household "
                "holds no member seat there",
                len(records),
                resource,
                provider,
                space_id,
            )
        elif resource == "space_zones_deleted":
            await self._persist_zone_tombstones(records, space_id, provider=provider)
        elif resource == "space_zones":
            await self._persist_zones(space_id, records, provider=provider)
        elif resource == "bazaar":
            await self._persist_bazaar(space_id, records, provider=provider)
        elif resource == "timetables_deleted":
            await self._persist_timetable_tombstones(
                records, space_id, provider=provider
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

    # ─── Who may stream what (§24.11 authorship) ─────────────────────

    async def _admit(
        self,
        resource: str,
        space_id: str,
        records: list[dict[str, Any]],
        *,
        provider: str,
        held_back: HeldBack,
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
        if await self._acts_as_host(space, provider):
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
        event = _sync_event(space_id, resource, provider)
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
        held_back: HeldBack,
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
        held_back: HeldBack,
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
                return await self._authored_post(
                    auth, space_id, rid, r, event, held_back
                )
            case "posts_deleted":
                return await self._authored_post_delete(
                    auth, space_id, rid, r, event, held_back
                )
            case "comments_deleted":
                return await self._authored_comment_delete(
                    auth, space_id, rid, r, event, held_back
                )
            case "comments":
                return await self._authored_comment(
                    auth, space_id, rid, r, event, held_back
                )
            case "task_lists":
                return await self._authored_task_list(
                    auth, space_id, rid, r, event, held_back
                )
            case "task_lists_deleted":
                return await self._authored_task_list_delete(
                    auth, space_id, rid, r, event, held_back
                )
            case "tasks_deleted":
                return await self._authored_task_delete(
                    auth, space_id, rid, r, event, held_back
                )
            case "tasks" | "tasks_archived":
                return await self._authored_task(
                    auth, space_id, rid, r, event, held_back
                )
            case "pages_deleted":
                return await self._authored_page_delete(
                    auth, space_id, rid, r, event, held_back
                )
            case "pages":
                return await self._authored_page(
                    auth, space_id, rid, r, event, held_back
                )
            case "stickies_deleted":
                return await self._authored_sticky_delete(
                    auth, space_id, rid, r, event, held_back
                )
            case "stickies":
                return await self._authored_sticky(
                    auth, space_id, rid, r, event, held_back
                )
            case "calendar_deleted":
                return await self._authored_calendar_delete(
                    auth, space_id, rid, r, event, held_back
                )
            case "calendar":
                return await self._authored_calendar(
                    auth, space_id, rid, r, event, held_back
                )
            case "gallery_albums_deleted":
                return await self._authored_album_delete(
                    auth, space_id, rid, r, event, held_back
                )
            case "gallery_items_deleted":
                return await self._authored_item_delete(
                    auth, space_id, rid, r, event, held_back
                )
            case "gallery":
                return await self._authored_gallery(
                    auth, space_id, rid, r, event, held_back
                )
            case "polls" | "timetables_deleted":
                # v_92 — the host's alone: a poll's votes, closure and
                # options are everybody's (no member household may set
                # them by stream), and a timetable tombstone does not name
                # the moderator who made it, so no live rule could judge a
                # member household's copy.
                return False
            case "schedules":
                return await self._authored_schedule(
                    auth, space_id, rid, r, event, held_back
                )
            case "space_zones_deleted":
                return await self._authored_zone_delete(
                    auth, space_id, rid, r, event, held_back
                )
            case "space_zones":
                return await self._authored_zone(
                    auth, space_id, rid, r, event, held_back
                )
            case "timetables":
                return await self._authored_timetable(
                    auth, space_id, rid, r, event, held_back
                )
            case "chat_messages" | "chat_messages_deleted":
                # Judged record by record by the chat sink with the live
                # ``SPACE_CHAT_MESSAGE_CREATED`` rule (owner-bound id, the
                # author a writer this provider speaks for, held already →
                # skipped), whoever streams it.
                return True
            case "bazaar":
                return await self._authored_bazaar(
                    auth, space_id, rid, r, event, held_back
                )
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
    "timetables_deleted": (SPACE_TIMETABLE_KIND, ("created_by",)),
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
