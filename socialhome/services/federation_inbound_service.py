"""Federation inbound service — land remote DM/space/user events locally (§24).

The §24.11 validation pipeline (``federation/inbound_validator.py``) has
already verified signature, replay cache, ban list, and decrypted the
payload by the time an event reaches the event registry. Handlers
attached by this service persist the effect locally and publish the
matching :class:`DomainEvent` on the bus so
:class:`~socialhome.services.realtime_service.RealtimeService` can
fan out to WebSocket clients.

Events without a concrete subscriber fall through to a debug log — the
event dispatch registry never raises, so silent drops are observable.
"""

from __future__ import annotations

import base64
import binascii
import logging
import pathlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os

from ..domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationMessage,
    ConversationType,
    MESSAGE_TYPES,
    RemoteConversationMember,
)
from ..domain.events import (
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    DmMessageCreated,
    DmMessageReactionChanged,
    DmMessageUpdated,
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
    PostDeleted,
    PostEdited,
    SpaceConfigChanged,
    SpaceMemberProfileUpdated,
    SpacePostCreated,
    HighlightFrameAdded,
    HighlightFrameReactionChanged,
    HighlightFrameRemoved,
    HighlightFrameViewed,
    HighlightRemoved,
    UserStatusChanged,
)
from ..domain.post import (
    FEED_POST_MAX_IMAGES,
    Comment,
    CommentType,
    LocationData,
    Post,
    PostType,
)
from ..domain.moment import MOMENT_RETENTION_DAYS, Moment
from ..domain.presence import truncate_coord
from ..domain.space import Space, SpaceConfigEventType, SpaceRole
from ..domain.highlight import (
    Highlight,
    HighlightAudience,
    HighlightFrame,
    HighlightFrameType,
)
from ..crypto import (
    UnsupportedUserSigSuite,
    derive_user_id,
    verify_user_identity_assertion,
)
from ..domain.user import (
    RemoteUser,
    UserIdentityAssertion,
    UserStatus,
    clean_status_emoji,
    clean_status_text,
)
from ..domain.federation_capabilities import FederationCapability
from ..federation.dm_scope import DM_HOLD_SCOPE, DmScope, refuse
from ..federation.moment_origin import check_relayed_moment_origin
from ..federation.owner_bound_id import (
    MOMENT_KIND,
    SPACE_COMMENT_KIND,
    SPACE_POST_KIND,
    is_owner_bound,
    owner_bound_id_refused,
)
from ..federation.space_authorship import SpaceAuthorship
from ..federation.space_scope import (
    log_cross_space_refusal,
    log_not_applied,
    resolve_space_id,
)
from ..infrastructure.event_bus import EventBus
from ..infrastructure.hlc import HLC, HLC_MAX_DRIFT_MS
from ..media.image_processor import ImageProcessor
from ..repositories.profile_picture_repo import compute_picture_hash
from ..services.user_service import PROFILE_PICTURE_MAX_DIMENSION
from .inbound_media_store import (
    is_safe_media_name,
    local_media_ref,
    local_media_refs,
    media_basename,
    note_existing,
    parse_chunk_meta,
    partial_key,
    publish_once,
    remove_quietly,
)
from .space_mentions import SpaceMentionResolver
from .space_crypto_service import (
    UnsupportedAuthoritySuite,
    strip_authority_sig_fields,
    verify_authority_event,
)
from .space_service import (
    apply_space_images_from_config_change,
    stub_space_from_metadata,
)
from ..utils.datetime import parse_iso8601_lenient

if TYPE_CHECKING:
    from .dm_group_service import DmGroupService
    from ..federation.pending_seat_buffer import PendingSeatBuffer
    from ..domain.federation import FederationEvent
    from ..repositories.bazaar_repo import AbstractBazaarRepo
    from ..repositories.conversation_repo import AbstractConversationRepo
    from ..repositories.gallery_repo import AbstractGalleryRepo
    from ..repositories.moment_repo import AbstractMomentRepo
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_cover_repo import AbstractSpaceCoverRepo
    from ..repositories.space_icon_repo import AbstractSpaceIconRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.highlight_repo import AbstractHighlightRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .moment_federation_outbound import MomentFederationOutbound
    from .relay_policy import RelayPolicy

log = logging.getLogger(__name__)

#: Hold-buffer scope for DMs waiting on their sender's user sync.
_DM_HOLD_SCOPE = DM_HOLD_SCOPE


def _moment_expired(expires_at: object) -> bool:
    """True iff ``expires_at`` parses and is already in the past.

    Naive timestamps are UTC. An absent or unparseable value is left to
    the existing handling (the row is stored and never shown).
    """
    if not isinstance(expires_at, str) or not expires_at:
        return False
    try:
        when = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_sequence(meta: dict, payload: dict) -> int | None:
    """Pull ``config_sequence`` out of a SPACE_CONFIG_CHANGED payload.

    Senders ship the integer at two layers — the flat legacy
    ``payload["sequence"]`` (catch-up reply path + SpaceConfigOutbound)
    and inside the modern ``space_meta`` blob as
    ``space_meta["config_sequence"]``. Prefer the legacy field because
    it's the one every sender populates today; fall through to the
    space_meta value if a future sender drops the legacy shape.

    Returns ``None`` when neither layer carries an integer — the caller
    treats that as "no guard available, fall through to upsert" so a
    malformed/older sender doesn't accidentally lock us out of legitimate
    updates.
    """
    raw = payload.get("sequence")
    if raw is None:
        raw = meta.get("config_sequence")
    if raw is None:
        return None
    try:
        return int(raw)
    except TypeError, ValueError:
        return None


def _keep_local_space_state(
    refreshed: Space,
    *,
    existing: Space,
    meta: dict,
    we_host: bool,
) -> Space:
    """Carry the row's local state across a SPACE_CONFIG_CHANGED snapshot.

    ``stub_space_from_metadata`` rebuilds a whole :class:`Space` from the
    wire, so every field that never federates comes back at its dataclass
    default, and ``save`` writes it over the stored row. On the HOST — which
    receives a seed-holding delegated admin's authority-signed snapshot —
    that used to wipe its join code, geo-gate, bot toggle and retention
    policy. Keep them from ``existing``:

    * Host-local state (join code, geo-gate, bot toggle, dissolve bookkeeping)
      never federates; the stored value always wins.
    * Retention is mirrored (it rides ``space_meta``) but enforced by the host
      alone, so the host pins its own values — a co-admin's mirror may be
      stale, and a co-admin's retention edit reaches the host as a forwarded
      ``update_config`` instead. A member household takes the host's values
      when the snapshot carries them, and keeps what it has when an older
      sender omits them ("absent" is "unknown", not "Forever").
    """
    keep_retention = we_host or "retention_days" not in meta
    keep_exempt = we_host or "retention_exempt_types" not in meta
    return replace(
        refreshed,
        join_code=existing.join_code,
        lat=existing.lat,
        lon=existing.lon,
        radius_km=existing.radius_km,
        bot_enabled=existing.bot_enabled,
        dissolved=existing.dissolved,
        archived_reason=existing.archived_reason,
        retention_days=(
            existing.retention_days if keep_retention else refreshed.retention_days
        ),
        retention_exempt_types=(
            existing.retention_exempt_types
            if keep_exempt
            else refreshed.retention_exempt_types
        ),
    )


#: Mapping from canonical media MIME types (produced by
#: :class:`ImageProcessor` / :class:`VideoProcessor`) to file
#: extensions for the receiver-side ``DM_MEDIA_BLOB`` write. Falls
#: back to ``.bin`` for unknown types — the receiver's bubble will
#: still render the file pill (clicking opens the system handler
#: based on response MIME, not extension). Kept narrow on purpose —
#: only MIME types we actually produce or accept upstream.
_MEDIA_MIME_EXT: dict[str, str] = {
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "video/webm": ".webm",
    "video/mp4": ".mp4",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "application/pdf": ".pdf",
}


def _mime_to_ext(mime_type: str) -> str:
    return _MEDIA_MIME_EXT.get(mime_type.lower(), ".bin")


def _declared_size(payload: dict, raw: bytes, chunk_count: int) -> int:
    """Whole-file size of a media blob: the chunk itself, or the sender's
    ``file_size_bytes`` for a multi-chunk transfer (``-1`` if absent)."""
    if chunk_count == 1:
        return len(raw)
    try:
        return int(payload.get("file_size_bytes") or -1)
    except TypeError, ValueError:
        return -1


#: Magic-byte signatures for the MIME types the upload pipeline
#: actually produces — :class:`ImageProcessor` always outputs
#: ``image/webp``, :class:`VideoProcessor` always outputs
#: ``video/webm``, and :class:`AudioProcessor` always outputs
#: ``audio/ogg`` (voice notes). Any other ``mime_type`` declared on
#: an inbound ``image`` / ``video`` / ``audio`` blob means either
#: the sender's instance is compromised or skipped the upload
#: pipeline; ``_bytes_match_mime`` returns ``False`` in that case so
#: :meth:`_on_dm_media_blob` flips the row to
#: ``media_sync_status='failed'``. ``type='file'`` blobs (PDFs,
#: ZIPs, arbitrary payloads) don't have a fixed signature and skip
#: the check.
_BLOB_MAGIC: dict[str, tuple[int, bytes]] = {
    "image/webp": (8, b"WEBP"),
    "video/webm": (0, b"\x1a\x45\xdf\xa3"),
    "audio/ogg": (0, b"OggS"),
    "audio/webm": (0, b"\x1a\x45\xdf\xa3"),
    # Safari's MediaRecorder emits MP4/AAC. ``ftyp`` lives at offset 4
    # in every well-formed ISO base-media file.
    "audio/mp4": (4, b"ftyp"),
}


def _bytes_match_mime(data: bytes, mime_type: str) -> bool | None:
    """Sniff ``data``'s leading bytes against the claimed MIME.

    Returns ``True`` when the bytes match a known signature,
    ``False`` when the claim is image / video / audio but the header
    doesn't match webp / webm / ogg (the upload pipeline never
    produces other image/video/audio formats — anything else is
    suspicious), or ``None`` when there's no signature to check
    (``type='file'`` and the like — direct-trust between paired
    households is the safety net there).
    """
    lower = mime_type.lower()
    sig = _BLOB_MAGIC.get(lower)
    if sig is None:
        # No registered signature. For ``image/*`` / ``video/*`` /
        # ``audio/*`` without a match in :data:`_BLOB_MAGIC` — i.e.
        # the sender claims a format we'd never produce on upload —
        # flag as suspicious. Everything else (``application/*``,
        # ``text/*``, the ``file`` passthrough) just trusts the
        # direct-pairing safety net.
        if (
            lower.startswith("image/")
            or lower.startswith("video/")
            or lower.startswith("audio/")
        ):
            return False
        return None
    offset, pattern = sig
    return data[offset : offset + len(pattern)] == pattern


class FederationInboundService:
    """Apply decrypted inbound federation events to local state.

    Registers handlers for the event families backed by a concrete repo:
    DM messages, space posts/comments, space membership, user status.
    Handlers call the injected repos to persist the row and publish a
    local :class:`DomainEvent` so the realtime layer picks it up.
    """

    __slots__ = (
        "_bus",
        "_conversation_repo",
        "_space_post_repo",
        "_space_repo",
        "_user_repo",
        "_highlight_repo",
        "_moment_repo",
        "_moment_outbound",
        "_profile_picture_repo",
        "_report_service",
        "_dm_routing_repo",
        "_relay_policy",
        "_media_dir",
        "_realtime",
        "_space_remote_member_repo",
        "_gallery_repo",
        "_bazaar_repo",
        "_authorship",
        "_dm_scope",
        "_pending",
        "_federation_service",
        "_space_cover_repo",
        "_space_icon_repo",
        "_groups",
        "_mentions",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        conversation_repo: "AbstractConversationRepo",
        space_post_repo: "AbstractSpacePostRepo",
        space_repo: "AbstractSpaceRepo",
        user_repo: "AbstractUserRepo",
        highlight_repo: "AbstractHighlightRepo | None" = None,
        moment_repo: "AbstractMomentRepo | None" = None,
        moment_outbound: "MomentFederationOutbound | None" = None,
        profile_picture_repo=None,
        report_service=None,
        dm_routing_repo=None,
        relay_policy: "RelayPolicy | None" = None,
        media_dir: "pathlib.Path | None" = None,
        realtime: "object | None" = None,
        space_remote_member_repo=None,
        gallery_repo: "AbstractGalleryRepo | None" = None,
        bazaar_repo: "AbstractBazaarRepo | None" = None,
        pending_seat_buffer: "PendingSeatBuffer | None" = None,
        space_cover_repo: "AbstractSpaceCoverRepo | None" = None,
        space_icon_repo: "AbstractSpaceIconRepo | None" = None,
    ) -> None:
        self._bus = bus
        self._conversation_repo = conversation_repo
        self._space_post_repo = space_post_repo
        self._space_repo = space_repo
        self._user_repo = user_repo
        self._highlight_repo = highlight_repo
        self._moment_repo = moment_repo
        self._moment_outbound = moment_outbound
        # v_3 DM media: receiver writes the embedded preview here on
        # ``DM_MESSAGE``, then the full bytes from ``DM_MEDIA_BLOB``.
        # ``realtime`` is the WS publisher — we push
        # ``dm.media_ready`` to the conversation's local members when
        # the full bytes land. Both optional so unit-test stacks can
        # omit them.
        self._media_dir = media_dir
        self._realtime = realtime
        self._profile_picture_repo = profile_picture_repo
        self._report_service = report_service
        self._dm_routing_repo = dm_routing_repo
        self._relay_policy = relay_policy
        # #117 — needed to route SPACE_MEMBER_JOINED writes to the
        # right table. Optional so legacy test stacks that don't
        # exercise the cross-household path can omit it.
        self._space_remote_member_repo = space_remote_member_repo
        # Resolve which space a SPACE_MEDIA_BLOB's gallery item / bazaar
        # listing lives in. Optional so unit-test stacks can omit them.
        self._gallery_repo = gallery_repo
        self._bazaar_repo = bazaar_repo
        #: §24.11 authorship — binds the author a post / comment names to
        #: the household that signed it. Built from the roster mirror, so
        #: without one there is nothing to bind against and every space
        #: post / comment write is refused (fail closed, see
        #: :meth:`_space_authorship`).
        self._authorship: SpaceAuthorship | None = (
            SpaceAuthorship(
                space_repo=space_repo,
                remote_member_repo=space_remote_member_repo,
                user_repo=user_repo,
                pending=pending_seat_buffer,
            )
            if space_remote_member_repo is not None
            else None
        )
        #: @-mentions in inbound space posts / comments resolve against
        #: THIS household's view of the space's members (local + remote
        #: seats) on the decrypted content — nothing mention-related is on
        #: the wire.
        self._mentions = SpaceMentionResolver(
            space_repo, user_repo, space_remote_member_repo
        )
        #: §24.11 DM binding — ties the conversation, message and person a
        #: DM payload names to the household that signed it.
        self._dm_scope = DmScope(
            conversation_repo=conversation_repo,
            user_repo=user_repo,
        )
        #: Holds a DM whose sender this household has no user row for yet
        #: (the sender's profile sync trails their first message); released
        #: when that user syncs. ``None`` refuses instead.
        self._pending = pending_seat_buffer
        self._federation_service = None
        #: Where a member stores the cover / icon a host's
        #: ``SPACE_CONFIG_CHANGED`` ships. Optional so unit-test stacks can
        #: omit them (the image is then simply not stored).
        self._space_cover_repo = space_cover_repo
        self._space_icon_repo = space_icon_repo
        #: Group-conversation authority (v_37) — a deprovisioned member of a
        #: cross-household group leaves it instead of taking it down.
        self._groups: "DmGroupService | None" = None

    def attach_groups(self, groups: "DmGroupService") -> None:
        """Wire the group-membership authority (built with the DM stack)."""
        self._groups = groups

    def attach_realtime(self, realtime: "object") -> None:
        """Wire the realtime broadcaster after construction.

        :class:`RealtimeService` is constructed downstream of this
        service in :func:`create_app`, so the realtime handle gets
        set after the fact. Used by ``_on_dm_media_blob`` to fan a
        ``dm.media_ready`` WS frame to local participants once the
        full bytes for a cross-household media DM have landed.
        """
        self._realtime = realtime

    def attach_to(self, federation_service) -> None:
        """Register inbound handlers on the federation event registry."""
        from ..domain.federation import FederationEventType as FET

        # Stash so handlers (role-change routing, config pinning) can
        # read own_instance_id without threading it through every call.
        self._federation_service = federation_service
        registry = federation_service._event_registry
        registry.register(FET.DM_MESSAGE, self._on_dm_message)
        registry.register(FET.DM_MEDIA_BLOB, self._on_dm_media_blob)
        registry.register(FET.DM_MESSAGE_DELETED, self._on_dm_deleted)
        registry.register(FET.DM_MESSAGE_REACTION, self._on_dm_reaction)

        registry.register(FET.SPACE_POST_CREATED, self._on_space_post_created)
        registry.register(FET.SPACE_POST_UPDATED, self._on_space_post_updated)
        registry.register(FET.SPACE_POST_DELETED, self._on_space_post_deleted)
        registry.register(FET.SPACE_MEDIA_BLOB, self._on_space_media_blob)
        registry.register(FET.SPACE_COMMENT_CREATED, self._on_space_comment_added)
        registry.register(FET.SPACE_COMMENT_UPDATED, self._on_space_comment_updated)
        registry.register(FET.SPACE_COMMENT_DELETED, self._on_space_comment_deleted)

        # v_23: SPACE_MEMBER_JOINED / SPACE_MEMBER_LEFT are owned SOLELY by the
        # authority-verifying gossip handler in PrivateSpaceInviteHandler — it
        # rejects any event whose payload isn't signed by the space seed. There
        # is intentionally NO legacy handler here: an unsigned roster mutation
        # must never be applied (a confirmed peer could otherwise forge/evict a
        # roster entry). Role changes stay host-authoritative below.
        registry.register(
            FET.SPACE_MEMBER_ROLE_CHANGED, self._on_space_member_role_changed
        )
        registry.register(
            FET.SPACE_MEMBER_PROFILE_UPDATED,
            self._on_space_member_profile_updated,
        )
        # §D1b — apply config changes (rename, emoji, feature toggles)
        # from the host to our local stub of a remote space so the
        # card the joiner sees in /spaces stays in sync.
        registry.register(FET.SPACE_CONFIG_CHANGED, self._on_space_config_changed)

        registry.register(FET.USERS_SYNC, self._on_users_sync)
        registry.register(FET.USER_UPDATED, self._on_user_updated)
        registry.register(FET.USER_REMOVED, self._on_user_removed)
        registry.register(FET.USER_STATUS_UPDATED, self._on_user_status_updated)

        registry.register(FET.SPACE_REPORT, self._on_space_report)

        # Highlights — only registered when a highlight repo is wired in. Tests
        # that instantiate :class:`FederationInboundService` for non-
        # highlight coverage don't need to plumb a highlight repo through.
        if self._highlight_repo is not None:
            registry.register(FET.HIGHLIGHT_CREATED, self._on_highlight_created)
            registry.register(
                FET.HIGHLIGHT_FRAME_APPENDED, self._on_highlight_frame_appended
            )
            registry.register(
                FET.HIGHLIGHT_FRAME_DELETED, self._on_highlight_frame_deleted
            )
            registry.register(FET.HIGHLIGHT_DELETED, self._on_highlight_deleted)
            registry.register(
                FET.HIGHLIGHT_FRAME_VIEWED, self._on_highlight_frame_viewed
            )
            registry.register(
                FET.HIGHLIGHT_FRAME_REACTED, self._on_highlight_frame_reacted
            )
            registry.register(
                FET.HIGHLIGHT_FRAME_REACTION_REMOVED,
                self._on_highlight_frame_reaction_removed,
            )

        # Moments — only registered when the moment repo is wired.
        if self._moment_repo is not None:
            registry.register(FET.MOMENT_CREATED, self._on_moment_created)
            registry.register(FET.MOMENT_DELETED, self._on_moment_deleted)
            registry.register(FET.MOMENT_REACTED, self._on_moment_reacted)
            registry.register(
                FET.MOMENT_REACTION_REMOVED,
                self._on_moment_reaction_removed,
            )

    # ── DM handlers ────────────────────────────────────────────────────

    async def _on_dm_message(self, event: "FederationEvent") -> None:
        p = event.payload
        conv_id = str(p.get("conversation_id") or "")
        message_id = str(p.get("message_id") or "")
        sender_user_id = str(p.get("sender_user_id") or "")
        content = str(p.get("content") or "")
        msg_type = str(p.get("type") or "text")
        if not conv_id or not message_id or not sender_user_id:
            log.debug("DM_MESSAGE missing required field: %s", p)
            return
        if msg_type not in MESSAGE_TYPES:
            msg_type = "text"
        recipients = tuple(p.get("recipient_user_ids") or ())
        if not await self._dm_message_in_scope(
            event, conv_id, message_id, sender_user_id, recipients
        ):
            return

        # Cross-household DMs arrive without the conversation ever
        # being created on this instance — the *sender's* household
        # built it locally via :meth:`DmService.create_dm` and we just
        # got the first DM_MESSAGE for it. Auto-create the conversation
        # row + seat the local recipient(s) FIRST so the §12.5
        # gap-detection block below (which writes
        # ``conversation_sender_sequences`` with an FK → ``conversations``)
        # doesn't trip a FOREIGN KEY constraint failure on the receiver
        # the very first time a sender ships a DM. Idempotent —
        # ``conversation_repo`` upserts.
        await self._ensure_remote_dm_conversation(
            conv_id=conv_id,
            sender_instance_id=event.from_instance,
            sender_user_id=sender_user_id,
            sender_display_name=str(p.get("sender_display_name") or ""),
            recipient_user_ids=recipients,
            occurred_at=p.get("occurred_at"),
        )

        # §12.5 gap detection — when the sender stamps a monotonic
        # ``sender_seq`` on the envelope, compare against our last-seen
        # value and persist one ``conversation_message_gaps`` row per
        # missing sequence. Skipped when the routing repo isn't wired
        # or the payload doesn't carry a seq (backwards-compat path).
        sender_seq = p.get("sender_seq")
        if self._dm_routing_repo is not None and sender_seq is not None:
            try:
                incoming = int(sender_seq)
            except TypeError, ValueError:
                incoming = 0
            if incoming > 0:
                last = await self._dm_routing_repo.peek_sender_seq(
                    conversation_id=conv_id,
                    sender_user_id=sender_user_id,
                )
                if last == 0:
                    # First message ever from this sender on this conv.
                    # We have no baseline — claiming "everything below
                    # ``incoming`` is missing" is meaningless. Seed the
                    # watermark to ``incoming`` and sweep any open-gap
                    # rows that the pre-fix detector inserted while the
                    # watermark stayed pinned at 0 (those rows are
                    # bogus by construction — you can't legitimately
                    # track missing seqs before you've received
                    # anything).
                    await self._dm_routing_repo.clear_all_gaps_for_sender(
                        conversation_id=conv_id,
                        sender_user_id=sender_user_id,
                    )
                    await self._dm_routing_repo.record_received_seq(
                        conversation_id=conv_id,
                        sender_user_id=sender_user_id,
                        seq=incoming,
                    )
                elif incoming > last + 1:
                    missing = list(range(last + 1, incoming))
                    log.warning(
                        "DM gap detected conv=%s sender=%s missing=%d..%d",
                        conv_id,
                        sender_user_id,
                        missing[0],
                        missing[-1],
                    )
                    await self._dm_routing_repo.insert_gaps(
                        conversation_id=conv_id,
                        sender_user_id=sender_user_id,
                        expected_seqs=missing,
                    )
                    await self._dm_routing_repo.record_received_seq(
                        conversation_id=conv_id,
                        sender_user_id=sender_user_id,
                        seq=incoming,
                    )
                elif incoming <= last:
                    # Out-of-order delivery resolving a previously-
                    # recorded gap; clear it so the UI banner disappears.
                    await self._dm_routing_repo.resolve_gap(
                        conversation_id=conv_id,
                        sender_user_id=sender_user_id,
                        expected_seq=incoming,
                    )
                else:
                    # Normal forward case (incoming == last + 1). Just
                    # advance the high-watermark.
                    await self._dm_routing_repo.record_received_seq(
                        conversation_id=conv_id,
                        sender_user_id=sender_user_id,
                        seq=incoming,
                    )

        # ── v_3: cross-household media preview ───────────────────────
        # When the sender embedded a ``preview_bytes_b64`` we save it
        # to local media storage now so the bubble can render
        # immediately. ``media_sync_status='pending'`` tells the SPA
        # to keep the bubble's brightness-pulse overlay until the
        # follow-up ``DM_MEDIA_BLOB`` arrives and we swap to the
        # full-quality bytes. Senders at v_2 don't carry these
        # fields; ``media_url`` from the payload (if any) goes
        # through unchanged.
        media_url, media_sync_status = await self._receive_media_preview(
            payload=p,
            message_id=message_id,
            msg_type=msg_type,
        )

        msg = ConversationMessage(
            id=message_id,
            conversation_id=conv_id,
            sender_user_id=sender_user_id,
            content=content,
            created_at=parse_iso8601_lenient(p.get("occurred_at")),
            type=msg_type,
            media_url=media_url,
            file_name=p.get("file_name"),
            mime_type=p.get("mime_type"),
            file_size_bytes=(
                int(p["file_size_bytes"])
                if isinstance(p.get("file_size_bytes"), (int, str))
                and str(p.get("file_size_bytes")).lstrip("-").isdigit()
                else None
            ),
            media_blob_id=p.get("media_blob_id"),
            media_sync_status=media_sync_status,
        )
        # ``save_message_returning_created`` is race-safe: when the
        # same envelope arrives via two transports (perfect-negotiation
        # WebRTC + HTTPS-inbox failover) only the first call returns
        # ``created=True``. The previous peek-then-save pattern raced
        # — both transports could see ``existing=None`` before either
        # wrote — and the user got two bell rows + two pushes for one
        # message.
        _, created = await self._conversation_repo.save_message_returning_created(msg)
        # Who hears about it here: the conversation's own local members —
        # never whoever the sender listed in ``recipient_user_ids`` (a
        # sender could name any local user). A 1:1 the local member hid
        # still reaches them (as before); a group member who left doesn't.
        local_recipients = await self._local_member_ids(conv_id)

        if not created:
            # Two cases land here:
            #   (a) sender re-fanned the envelope with updated ``content``
            #       — a voice-note transcript or an explicit edit, signalled
            #       by ``edited_at`` in the payload.
            #   (b) the same envelope arrived a second time via a different
            #       transport (perfect-negotiation WebRTC + HTTPS-inbox
            #       failover) with no real edit.
            # Only (a) is worth firing ``DmMessageUpdated`` for. (b) is a
            # silent no-op — the row is already in the DB and the SPA has
            # already painted the bubble from the first delivery; emitting
            # a redundant ``dm.message_updated`` frame would falsely trip
            # any future "(edited)" indicator the SPA picks up.
            edited_at_iso = p.get("edited_at")
            if edited_at_iso is None:
                return
            await self._bus.publish(
                DmMessageUpdated(
                    conversation_id=conv_id,
                    message_id=message_id,
                    sender_user_id=sender_user_id,
                    recipient_user_ids=local_recipients,
                    content=content,
                    edited_at=parse_iso8601_lenient(edited_at_iso),
                )
            )
            return

        await self._bus.publish(
            DmMessageCreated(
                conversation_id=conv_id,
                message_id=message_id,
                sender_user_id=sender_user_id,
                sender_display_name=str(p.get("sender_display_name") or sender_user_id),
                recipient_user_ids=local_recipients,
                content=content,
                message_type=msg_type,
                media_url=media_url,
                reply_to_id=p.get("reply_to_id"),
                occurred_at=msg.created_at,
            )
        )

    async def _ensure_remote_dm_conversation(
        self,
        *,
        conv_id: str,
        sender_instance_id: str,
        sender_user_id: str,
        sender_display_name: str,
        recipient_user_ids: tuple,
        occurred_at: object,
    ) -> None:
        """Ensure the local conversation row + the recipient's local
        membership exist for an inbound cross-household DM.

        The sender's household creates the conversation locally on
        send; the receiver's household has no row until the first
        DM_MESSAGE arrives. Without this ensure-step the recipient
        sees nothing in ``GET /api/conversations`` and
        ``GET /api/conversations/{id}/messages`` returns 403
        ("not a member").

        Runs only for a conversation that is not here yet: the seats of an
        existing conversation are never rewritten by an inbound message
        (:meth:`_dm_message_in_scope` already required the sender to hold
        one). Idempotent: ``add_member`` / ``add_remote_member`` upsert on
        the natural keys.
        """
        if await self._conversation_repo.get(conv_id) is not None:
            return
        now_iso = (
            occurred_at
            if isinstance(occurred_at, str) and occurred_at
            else datetime.now(timezone.utc).isoformat()
        )
        try:
            await self._conversation_repo.create(
                Conversation(
                    id=conv_id,
                    type=ConversationType.DM,
                    created_at=parse_iso8601_lenient(now_iso),
                ),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.debug(
                "_ensure_remote_dm_conversation: create skipped (%s)",
                exc,
            )

        joined_at = datetime.now(timezone.utc).isoformat()

        # Seat each local recipient as a ConversationMember so
        # list_for_user(username) finds the conv.
        get_by_user_id = getattr(self._user_repo, "get_by_user_id", None)
        for rid in recipient_user_ids:
            if get_by_user_id is None:
                break
            local = await get_by_user_id(str(rid))
            if local is None:
                continue
            try:
                await self._conversation_repo.add_member(
                    ConversationMember(
                        conversation_id=conv_id,
                        username=local.username,
                        joined_at=joined_at,
                    ),
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.debug(
                    "_ensure_remote_dm_conversation: add_member %s skipped (%s)",
                    local.username,
                    exc,
                )

        # Seat the sender as a remote member so the conversation
        # roster reflects who the recipient is talking to.
        get_remote = getattr(self._user_repo, "get_remote", None)
        if get_remote is not None:
            remote = await get_remote(sender_user_id)
            if remote is not None:
                try:
                    await self._conversation_repo.add_remote_member(
                        RemoteConversationMember(
                            conversation_id=conv_id,
                            instance_id=sender_instance_id,
                            remote_username=remote.remote_username,
                            joined_at=joined_at,
                        ),
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    log.debug(
                        "_ensure_remote_dm_conversation: add_remote_member "
                        "%s/%s skipped (%s)",
                        sender_instance_id,
                        remote.remote_username,
                        exc,
                    )

    async def _dm_message_in_scope(
        self,
        event: "FederationEvent",
        conv_id: str,
        message_id: str,
        sender_user_id: str,
        recipients: tuple,
    ) -> bool:
        """Whether ``from_instance`` may write this ``DM_MESSAGE``.

        * the sender is a user of the sending household (never a local
          member, never a third household's user);
        * an existing message id is only ever re-sent (edit, transcript) by
          its own sender, in its own conversation;
        * an existing conversation takes messages only from a sender seated
          in it;
        * a conversation not here yet is opened only for a local recipient,
          and never for a group: a group's seats come only from its
          authority's ``DM_GROUP_ROSTER``, whose arrival also pulls the
          backlog — so a message that overtook the roster is caught up then.

        A group member on a household we never paired with has no user row
        here, only the seat the roster wrote (``seat_of``); that seat is what
        binds them to the signing household.
        """
        scope = self._dm_scope
        reason: str | None = None
        conv = await self._conversation_repo.get(conv_id)
        seat = (
            await scope.seat_of(event, conv_id, sender_user_id)
            if conv is not None
            else None
        )
        if (
            seat is None
            and await self._user_repo.get_instance_for_user(sender_user_id) is None
        ):
            # Not a refusal yet: the sender's user row can trail their first
            # DM. Hold it until that user syncs (bounded; see
            # :meth:`_release_held_dms`), refuse only when it can't be held.
            if self._pending is not None and self._pending.hold(
                event, space_id=_DM_HOLD_SCOPE, user_id=sender_user_id
            ):
                log.info(
                    "DM_MESSAGE from %s: sender %s not synced yet — holding",
                    event.from_instance,
                    sender_user_id,
                )
                return False
        existing = await self._conversation_repo.get_message(message_id)
        if seat is None and not await scope.sent_from(event, sender_user_id):
            reason = "sender is not a user of the sending household"
        elif existing is not None and (
            existing.conversation_id != conv_id
            or existing.sender_user_id != sender_user_id
        ):
            reason = "message id belongs to another message"
        elif conv is not None:
            if seat is None and not await self._heal_legacy_seat(
                event, conv, sender_user_id
            ):
                reason = "sender is not seated in the conversation"
        elif is_owner_bound(conv_id):
            reason = "group conversation not seated here (no roster yet)"
        elif not await self._has_local_recipient(recipients):
            reason = "no local recipient for a new conversation"
        if reason is None:
            return True
        refuse(
            event,
            reason,
            conversation=conv_id,
            message=message_id,
            sender=sender_user_id,
        )
        return False

    async def _heal_legacy_seat(
        self,
        event: "FederationEvent",
        conv: Conversation,
        sender_user_id: str,
    ) -> bool:
        """Seat the sender in an older 1:1 whose remote seat was never written.

        Only for a 1:1 with exactly one local member, no remote seat at all,
        and earlier messages from this very sender — proof they were the other
        party. The sender is already bound to ``from_instance`` by the caller.
        Anything else (a group, a thread with another remote party, a 1:1 the
        sender never wrote in) stays refused.
        """
        if conv.type is not ConversationType.DM:
            return False
        if await self._conversation_repo.list_remote_members(conv.id):
            return False
        if len(await self._conversation_repo.list_members(conv.id)) != 1:
            return False
        if conv.id not in await self._conversation_repo.list_conversations_with_sender(
            sender_user_id
        ):
            return False
        remote = await self._user_repo.get_remote(sender_user_id)
        if remote is None:
            return False
        await self._conversation_repo.add_remote_member(
            RemoteConversationMember(
                conversation_id=conv.id,
                instance_id=event.from_instance,
                remote_username=remote.remote_username,
                joined_at=datetime.now(timezone.utc).isoformat(),
            ),
        )
        log.info(
            "DM: restored %s's seat in older conversation %s",
            sender_user_id,
            conv.id,
        )
        return True

    async def _local_member_ids(self, conv_id: str) -> tuple[str, ...]:
        """``user_id`` of the local members of ``conv_id``.

        A group's members who left are not; a 1:1's hidden member still is.
        """
        conv = await self._conversation_repo.get(conv_id)
        group = conv is not None and conv.type is ConversationType.GROUP_DM
        ids: list[str] = []
        for m in await self._conversation_repo.list_members(conv_id):
            if group and m.deleted_at is not None:
                continue
            user = await self._user_repo.get(m.username)
            if user is not None:
                ids.append(user.user_id)
        return tuple(ids)

    async def _has_local_recipient(self, recipients: tuple) -> bool:
        for rid in recipients:
            if await self._user_repo.get_by_user_id(str(rid)) is not None:
                return True
        return False

    # ── DM media ─────────────────────────────────────────────────────

    async def _receive_media_preview(
        self,
        *,
        payload: dict,
        message_id: str,
        msg_type: str,
    ) -> tuple[str | None, str | None]:
        """Persist any embedded preview + return the local ``media_url``.

        Returns ``(media_url, media_sync_status)``:

        * For a v_3 media message with ``preview_bytes_b64``: write
          the preview WebP to ``media_dir`` and point ``media_url``
          at the local API path; ``media_sync_status='pending'``
          flags the bubble for the brightness-pulse overlay until
          the matching ``DM_MEDIA_BLOB`` lands.
        * For a v_3 media message *without* a preview (video / file
          today): no local bytes yet, ``media_url`` carries the
          sender's URL untouched and the SPA renders a placeholder
          glyph; ``media_sync_status='pending'`` keeps the row in
          the "waiting on blob" state.
        * For a text / transcript / location message, or a media
          message from a sub-v_3 sender (which fell back to text via
          the compat shim): no media to persist, the existing
          payload's ``media_url`` flows through.
        """
        is_media = msg_type in ("image", "video", "file")
        media_url_in = payload.get("media_url")
        if not is_media or payload.get("media_blob_id") is None:
            # Same-household DM where the sender's URL is reachable,
            # or a non-media message — nothing for the receiver to
            # build locally.
            return media_url_in, None
        # Reordering guard: ``DM_MEDIA_BLOB`` may have arrived
        # *before* this ``DM_MESSAGE`` (federation transport doesn't
        # guarantee ordering across event types). If a finalised
        # full file already sits under ``media_dir/<msg_id>.<ext>``,
        # adopt it directly — no preview needed, no pending state.
        # The blob handler's own ``update_media_sync_status`` call
        # no-op'd at the time (row didn't exist yet); the row we're
        # about to create here will pick up the correct URL on its
        # first save.
        if self._media_dir is not None:
            mime_in = str(payload.get("mime_type") or "")
            full_path = self._media_dir / f"{message_id}{_mime_to_ext(mime_in)}"
            if await aiofiles.os.path.isfile(full_path):
                # Drop any stale preview from a previous attempt.
                preview_old = self._media_dir / f"{message_id}.preview.webp"
                try:
                    await aiofiles.os.remove(preview_old)
                except FileNotFoundError:
                    pass
                except OSError:  # pragma: no cover
                    pass
                return f"api/media/{full_path.name}", None
        preview_b64 = payload.get("preview_bytes_b64")
        if not preview_b64 or self._media_dir is None:
            # Cross-household media without an embedded preview, or
            # the receiver was launched without a media root (test
            # stack). The SPA renders the type-glyph placeholder
            # until the full blob lands.
            return None, "pending"
        try:
            preview_bytes = base64.b64decode(preview_b64)
        except Exception as exc:  # pragma: no cover
            log.warning(
                "DM_MESSAGE: malformed preview_bytes_b64 for %s: %s",
                message_id,
                exc,
            )
            return None, "pending"
        # Preview file is short-lived — it'll be overwritten when
        # ``DM_MEDIA_BLOB`` arrives. Naming it after the message_id
        # keeps the replace operation atomic on the receiver: the
        # full bytes land at the same filename and the SPA's
        # ``media_url`` doesn't have to change at all.
        try:
            await aiofiles.os.makedirs(self._media_dir, exist_ok=True)
            dest = self._media_dir / f"{message_id}.preview.webp"
            async with aiofiles.open(dest, "wb") as f:
                await f.write(preview_bytes)
        except OSError as exc:  # pragma: no cover
            log.warning(
                "DM_MESSAGE: failed to write preview for %s: %s",
                message_id,
                exc,
            )
            return None, "pending"
        return f"api/media/{dest.name}", "pending"

    @staticmethod
    def _media_chunk_bytes(event: "FederationEvent") -> bytes | None:
        """Return the decoded chunk bytes for a media blob event.

        Prefers :attr:`FederationEvent.media_bytes` — the
        already-decrypted payload of a binary ``fed-media-v1`` frame
        (which the §24.11 pipeline + ``chunk_sha256`` binding have
        authenticated). Falls back to the base64 ``bytes_b64`` /
        ``bytes_base64`` field of the JSON path. Returns ``None`` when
        neither is present or the base64 is malformed.
        """
        if event.media_bytes is not None:
            return event.media_bytes
        payload = event.payload if isinstance(event.payload, dict) else {}
        b64 = payload.get("bytes_b64") or payload.get("bytes_base64")
        if not isinstance(b64, str):
            return None
        try:
            return base64.b64decode(b64)
        except binascii.Error, ValueError:
            return None

    async def _on_dm_media_blob(self, event: "FederationEvent") -> None:
        """Land the full bytes for a previously-arrived ``DM_MESSAGE``.

        Decodes the bytes from one chunk of the blob, writes them
        under ``media_dir`` as a part file keyed by ``chunk_index``,
        and — when the ``final`` chunk arrives (or for legacy
        single-payload sends) — concatenates the parts into the
        final file, swaps the message row's ``media_url`` to point
        at it, clears ``media_sync_status``, and pushes a
        ``dm.media_ready`` WS frame so the SPA can swap the
        bubble's preview ``<img src>`` for the full media.

        Backwards compat: payloads from older builds (and small
        files from current builds) carry no ``chunk_count``; we
        treat that as a single-chunk transfer. Chunks may arrive
        out of order; each is written as ``<msg_id>.part<idx>`` so
        a re-send from a sender restart simply overwrites the part
        and the finalisation reads them in order.
        """
        p = event.payload
        blob_id = str(p.get("media_blob_id") or "")
        message_id = str(p.get("message_id") or "")
        # Chunk bytes arrive either as the decrypted binary-frame payload
        # (``fed-media-v1``) or as a base64 ``bytes_b64`` field (JSON
        # path). ``_media_chunk_bytes`` prefers the former.
        raw = self._media_chunk_bytes(event)
        if raw is None or not blob_id or not message_id:
            log.debug("DM_MEDIA_BLOB missing required field: %s", p)
            return
        if self._media_dir is None:
            log.warning(
                "DM_MEDIA_BLOB: media_dir not wired; dropping blob %s",
                blob_id,
            )
            return
        # Chunk metadata. Default to a single-chunk transfer for
        # backwards compat with senders that don't emit these
        # fields yet.
        try:
            chunk_index = int(p.get("chunk_index", 0))
            chunk_count = int(p.get("chunk_count", 1))
        except TypeError, ValueError:
            chunk_index = 0
            chunk_count = 1
        if parse_chunk_meta(chunk_index, chunk_count) is None:
            log.warning(
                "DM_MEDIA_BLOB from %s: bad chunk metadata for %s — dropping",
                event.from_instance,
                message_id,
            )
            return
        if not is_safe_media_name(message_id):
            log.warning(
                "DM_MEDIA_BLOB: rejecting message id %r from %s (unsafe name)",
                message_id,
                event.from_instance,
            )
            return
        if not await self._dm_media_in_scope(event, message_id, blob_id):
            return
        is_final = bool(p.get("final", chunk_index == chunk_count - 1))
        mime_type = str(p.get("mime_type") or "application/octet-stream")
        await self._assemble_dm_chunk(
            raw=raw,
            message_id=message_id,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            is_final=is_final,
            mime_type=mime_type,
            conversation_id=str(p.get("conversation_id") or ""),
            from_instance=event.from_instance,
            total_size=_declared_size(p, raw, chunk_count),
        )

    async def _dm_media_in_scope(
        self,
        event: "FederationEvent",
        message_id: str,
        blob_id: str,
    ) -> bool:
        """Whether ``from_instance`` may ship the bytes for ``message_id``.

        When the message is already here, only its sender's household may:
        the blob id must be the one the message announced, and the
        sender's home instance must be ``from_instance`` — which also
        rules out a local member's message (home = this instance). A
        message not seen yet (the blob overtook its ``DM_MESSAGE``) is
        accepted: the write is write-once, and ``_receive_media_preview``
        adopts the file when the message lands.
        """
        msg = await self._conversation_repo.get_message(message_id)
        if msg is None:
            return True
        reason: str | None = None
        if msg.media_blob_id != blob_id:
            reason = "blob id does not match the message"
        elif await self._user_repo.get_instance_for_user(msg.sender_user_id) != (
            event.from_instance
        ):
            reason = "message was not sent from this household"
        if reason is None:
            return True
        log.warning(
            "DM_MEDIA_BLOB from %s: %s (message=%s) — refusing",
            event.from_instance,
            reason,
            message_id,
        )
        return False

    async def _assemble_dm_chunk(
        self,
        *,
        raw: bytes,
        message_id: str,
        chunk_index: int,
        chunk_count: int,
        is_final: bool,
        mime_type: str,
        conversation_id: str,
        from_instance: str,
        total_size: int,
    ) -> None:
        """Write one DM media chunk to disk and finalise on the last one.

        Source-agnostic core shared by the JSON ``DM_MEDIA_BLOB`` handler
        and the binary ``fed-media-v1`` path — ``raw`` is the already-
        decoded chunk bytes (base64-decoded or AEAD-decrypted upstream).
        On the final chunk it concatenates the parts, swaps the message
        row's ``media_url``, and pushes the ``dm.media_ready`` WS frame.
        ``self._media_dir`` is guaranteed non-None by the caller.

        Write-once: the final file is moved into place with
        :func:`publish_once`, so an existing ``<message_id><ext>`` is never
        replaced — a re-delivery just re-adopts the file already there.
        """
        assert self._media_dir is not None
        data = raw
        ext = _mime_to_ext(mime_type)
        dest = self._media_dir / f"{message_id}{ext}"
        tmp_dest = self._media_dir / f"{message_id}.assembled{ext}"
        try:
            await aiofiles.os.makedirs(self._media_dir, exist_ok=True)
            if await aiofiles.os.path.exists(dest):
                # Already landed (re-delivery). Don't rewrite; on the
                # final chunk fall through so the row adopts the file.
                if not is_final and chunk_count > 1:
                    return
                await note_existing(
                    dest,
                    incoming_size=total_size,
                    what="DM_MEDIA_BLOB",
                    from_instance=from_instance,
                )
            elif chunk_count == 1:
                # Fast path: no parts to assemble.
                async with aiofiles.open(tmp_dest, "wb") as out_f:
                    await out_f.write(data)
                if not await publish_once(tmp_dest, dest):
                    await note_existing(
                        dest,
                        incoming_size=total_size,
                        what="DM_MEDIA_BLOB",
                        from_instance=from_instance,
                    )
            else:
                part_path = self._media_dir / f"{message_id}.part{chunk_index:05d}"
                async with aiofiles.open(part_path, "wb") as out_f:
                    await out_f.write(data)
                if not is_final:
                    # Wait for the next chunk; the receive-side state
                    # change happens only after the final chunk lands.
                    return
                # Final chunk: concat 0..N-1 in order, write the
                # result, drop parts. If any earlier chunk failed to
                # land yet we have a hole — log + bail; the sender's
                # outbox retry will resend the missing chunk and the
                # finalisation will rerun.
                async with aiofiles.open(tmp_dest, "wb") as out_f:
                    for i in range(chunk_count):
                        part = self._media_dir / f"{message_id}.part{i:05d}"
                        if not await aiofiles.os.path.isfile(part):
                            log.warning(
                                "DM_MEDIA_BLOB: missing chunk %d for %s; "
                                "awaiting resend",
                                i,
                                message_id,
                            )
                            try:
                                await aiofiles.os.remove(tmp_dest)
                            except FileNotFoundError:
                                pass
                            return
                        async with aiofiles.open(part, "rb") as in_f:
                            await out_f.write(await in_f.read())
                if not await publish_once(tmp_dest, dest):
                    await note_existing(
                        dest,
                        incoming_size=total_size,
                        what="DM_MEDIA_BLOB",
                        from_instance=from_instance,
                    )
                # Cleanup part files.
                for i in range(chunk_count):
                    try:
                        await aiofiles.os.remove(
                            self._media_dir / f"{message_id}.part{i:05d}",
                        )
                    except FileNotFoundError:
                        pass
        except OSError as exc:  # pragma: no cover
            log.warning(
                "DM_MEDIA_BLOB: write failed for %s: %s",
                message_id,
                exc,
            )
            return
        # MIME-byte sniff. Direct-trust between paired households
        # is the primary safety net, but if a sender's instance is
        # ever compromised + lies about the payload type, we want
        # the recipient bubble to surface "this didn't match what
        # was claimed" rather than silently render whatever
        # extension the file ended up with. We still store the
        # file (the user may want to inspect it manually); the
        # status flips to ``failed`` so the SPA shows the warning
        # footnote.
        sniff_result: bool | None = None
        try:
            async with aiofiles.open(dest, "rb") as f:
                head = await f.read(16)
            sniff_result = _bytes_match_mime(head, mime_type)
        except OSError:  # pragma: no cover
            sniff_result = None
        # Drop the preview file if it's there — keeping it around
        # wastes disk space (small) but more importantly an unused
        # ``.preview.webp`` is a vector for stale-state bugs.
        preview = self._media_dir / f"{message_id}.preview.webp"
        try:
            await aiofiles.os.remove(preview)
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover
            pass
        new_url = f"api/media/{dest.name}"
        final_status: str | None = None
        if sniff_result is False:
            log.warning(
                "DM_MEDIA_BLOB: bytes for %s don't match claimed "
                "mime_type=%s — flagging as failed",
                message_id,
                mime_type,
            )
            final_status = "failed"
        await self._conversation_repo.update_media_sync_status(
            message_id=message_id,
            status=final_status,
            media_url=new_url,
        )
        # Push the WS frame so the SPA swaps the bubble's preview for
        # the full media without waiting for a refetch. The realtime
        # layer is responsible for fanning to the conversation's
        # local members. Best-effort — if the realtime service isn't
        # wired (test stack), the SPA picks up the new ``media_url``
        # on next ``GET /messages``.
        if self._realtime is not None:
            broadcaster = getattr(self._realtime, "broadcast_dm_media_ready", None)
            if broadcaster is not None:
                try:
                    await broadcaster(
                        message_id=message_id,
                        conversation_id=conversation_id,
                        media_url=new_url,
                    )
                except Exception as exc:  # pragma: no cover
                    log.debug(
                        "DM_MEDIA_BLOB: realtime broadcast failed: %s",
                        exc,
                    )

    async def _on_dm_deleted(self, event: "FederationEvent") -> None:
        """Soft-delete a message — only its own sender's household may."""
        message_id = str(event.payload.get("message_id") or "")
        conv_id = str(event.payload.get("conversation_id") or "")
        if not message_id:
            return
        msg = await self._conversation_repo.get_message(message_id)
        if msg is None:
            return
        reason: str | None = None
        if not await self._dm_scope.authored_by_sender(
            event, msg.conversation_id, msg.sender_user_id
        ):
            reason = "message was not sent from this household"
        elif conv_id and conv_id != msg.conversation_id:
            reason = "message is not in the named conversation"
        if reason is not None:
            refuse(event, reason, conversation=conv_id, message=message_id)
            return
        await self._conversation_repo.soft_delete_message(message_id)

    async def _on_dm_reaction(self, event: "FederationEvent") -> None:
        p = event.payload
        message_id = str(p.get("message_id") or "")
        user_id = str(p.get("user_id") or "")
        emoji = str(p.get("emoji") or "")
        action = str(p.get("action") or "add")
        conv_id = str(p.get("conversation_id") or "")
        if not message_id or not user_id or not emoji:
            return
        # A reaction is its reactor's own: only a user of the sending
        # household, seated in the message's conversation.
        msg = await self._conversation_repo.get_message(message_id)
        if msg is None:
            return
        reason: str | None = None
        if conv_id and conv_id != msg.conversation_id:
            reason = "message is not in the named conversation"
        elif not await self._dm_scope.speaks_for(event, msg.conversation_id, user_id):
            reason = "reactor is not a seated user of the sending household"
        if reason is not None:
            refuse(event, reason, message=message_id, user=user_id)
            return
        if action == "remove":
            await self._conversation_repo.remove_reaction(message_id, user_id, emoji)
        else:
            await self._conversation_repo.add_reaction(message_id, user_id, emoji)
        # Fan to local WS sessions so every open thread tab on this
        # household updates the reaction strip without a refetch.
        members = await self._conversation_repo.list_members(msg.conversation_id)
        recipient_ids: list[str] = []
        for m in members:
            u = await self._user_repo.get(m.username)
            if u is not None:
                recipient_ids.append(u.user_id)
        await self._bus.publish(
            DmMessageReactionChanged(
                conversation_id=msg.conversation_id,
                message_id=message_id,
                user_id=user_id,
                emoji=emoji,
                action=action,
                recipient_user_ids=tuple(recipient_ids),
            )
        )

    # ── Space content handlers ─────────────────────────────────────────

    async def _on_space_post_created(self, event: "FederationEvent") -> None:
        space_id = resolve_space_id(event)
        if not space_id:
            return
        post = self._post_from_payload(event.payload)
        if post is None:
            return
        # v_36: an owner-bound id commits to its author and space, so a
        # claim of it for anybody else is refused before it can hold the id.
        if owner_bound_id_refused(
            SPACE_POST_KIND,
            post.id,
            space_id=space_id,
            owner_user_id=post.author,
            context=f"{event.event_type} from {event.from_instance}",
        ):
            return
        authorship = self._space_authorship(event)
        if authorship is None:
            return
        # The claimed author must be the sender's own member (or a remote
        # member's row relayed by the host).
        existing = await self._space_post_repo.get(post.id)
        if existing is not None and existing[0] != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="post", row_id=post.id
            )
            return
        if existing is not None:
            # A re-send of an existing id is an upsert of that row — an
            # edit — so it is judged like one (the author's household or a
            # moderator; a bot post only a moderator), it keeps the row's
            # author, a deleted post stays deleted, and only a moderator
            # may touch a post held by moderation. Whatever it carries,
            # the state other people put on the row is kept.
            row = existing[1]
            if row.deleted:
                log_not_applied(
                    event, what="post", row_id=post.id, reason="already deleted"
                )
                return
            if (
                row.author != post.author
                or not await authorship.may_mutate(event, space_id, row.author)
                or (
                    row.moderated and not await authorship.is_moderator(event, space_id)
                )
            ):
                await authorship.hold_or_refuse(
                    event,
                    space_id=space_id,
                    what="post",
                    row_id=post.id,
                    user_id=post.author,
                )
                return
            post = replace(
                post,
                bot_id=row.bot_id,
                linked_event_id=row.linked_event_id,
                linked_highlight_id=row.linked_highlight_id,
                reactions=row.reactions,
                comment_count=row.comment_count,
                pinned=row.pinned,
                deleted=row.deleted,
                moderated=row.moderated,
                hidden_from_feed=row.hidden_from_feed,
            )
        elif not await authorship.may_author(event, space_id, post.author):
            await authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="post",
                row_id=post.id,
                user_id=post.author,
            )
            return
        raw_relay = event.payload.get("public_relay")
        public_relay = raw_relay if isinstance(raw_relay, dict) else None
        if await self._space_post_repo.save(space_id, post) is None:
            # The id already exists in another space — the sender was
            # gated for this one, so refuse rather than hijack the row.
            log_cross_space_refusal(
                event,
                space_id=space_id,
                what="post",
                row_id=post.id,
            )
            return
        if existing is not None:
            await self._bus.publish(
                PostEdited(
                    post=post,
                    space_id=space_id,
                    origin_instance_id=event.from_instance,
                )
            )
            return
        await self._bus.publish(
            SpacePostCreated(
                post=post,
                space_id=space_id,
                mentions=await self._mentions.resolve(
                    space_id, post.content, author_id=post.author
                ),
                origin_instance_id=event.from_instance,
                public_relay=public_relay,
            )
        )

    async def _on_space_media_blob(self, event: "FederationEvent") -> None:
        """Persist a chunked space-post media blob to local media path.

        Mirrors :meth:`_on_dm_media_blob`. The sender's
        :class:`SpaceMediaSyncService` ships one or more chunks (each
        carrying ``chunk_index`` / ``chunk_count`` / ``final``); the
        receiver writes each chunk to a part file under
        ``<media_dir>/.partial/<key>/`` and finalises by moving the
        assembled bytes into place when the ``final`` chunk arrives.

        Without this handler the post lands but the picture is broken
        because the URL points at the receiver's media path with no
        backing file.

        Scope (``docs/protocol/media.md``):

        * The envelope must name one space (:func:`resolve_space_id`).
        * The correlated row — ``correlation_id`` (``post_id`` on older
          senders): a post, a bazaar listing or a gallery item — must,
          when already known here, live in that space and reference
          ``filename``. Otherwise the blob is refused with a WARNING.
        * Not yet known (the blob overtook its row on the mesh / §25.6
          drain) → accepted, because the write below is write-once.
        * **Write-once:** an existing file under ``filename`` is never
          replaced (:mod:`inbound_media_store`). Media names are random,
          so this is what keeps a blob from swapping another space's, a
          DM's or the household's own file.

        Safety:

        * ``filename`` must pass :func:`is_safe_media_name` — a single
          path component, no separators / leading dot / NUL.
        * Chunk metadata is range-checked; part files are keyed by the
          sending household so two households' chunks never mix, and
          live under ``.partial`` which ``MediaServeView`` never serves.
        """
        if self._media_dir is None:
            return
        p = event.payload
        filename = str(p.get("filename") or "")
        # Bytes arrive as the decrypted binary-frame payload
        # (``fed-media-v1``) or base64 ``bytes_b64`` / ``bytes_base64``.
        raw = self._media_chunk_bytes(event)
        if not filename or raw is None:
            return
        if not is_safe_media_name(filename):
            log.warning(
                "SPACE_MEDIA_BLOB: rejecting filename %r from %s (unsafe name)",
                filename,
                event.from_instance,
            )
            return
        chunk_meta = parse_chunk_meta(
            p.get("chunk_index") or 0,
            p.get("chunk_count") or 1,
        )
        if chunk_meta is None:
            log.warning(
                "SPACE_MEDIA_BLOB from %s: bad chunk metadata for %s — dropping",
                event.from_instance,
                filename,
            )
            return
        space_id = resolve_space_id(event)
        correlation_id = str(p.get("correlation_id") or p.get("post_id") or "")
        if not space_id or not correlation_id:
            log.warning(
                "SPACE_MEDIA_BLOB from %s: %s names no space or row — refusing",
                event.from_instance,
                filename,
            )
            return
        if not await self._space_media_in_scope(
            space_id=space_id,
            correlation_id=correlation_id,
            filename=filename,
        ):
            log.warning(
                "SPACE_MEDIA_BLOB from %s: %s is not media of %s in space %s "
                "— refusing",
                event.from_instance,
                filename,
                correlation_id,
                space_id,
            )
            return
        transfer_id = str(p.get("transfer_id") or "") or filename
        chunk_index, chunk_count = chunk_meta
        await self._assemble_space_chunk(
            raw=raw,
            filename=filename,
            partial_dir=partial_key(event.from_instance, transfer_id),
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            final=bool(p.get("final")) or chunk_count == 1,
            post_id=correlation_id,
            from_instance=event.from_instance,
            total_size=_declared_size(p, raw, chunk_count),
        )

    async def _space_media_in_scope(
        self,
        *,
        space_id: str,
        correlation_id: str,
        filename: str,
    ) -> bool:
        """Whether a blob for ``correlation_id`` may land in ``space_id``.

        ``False`` only on positive evidence: the correlated row is known
        and lives in another space, or is known and does not reference
        ``filename``. An unknown row is ``True`` — the write is
        write-once, so accepting it can't replace anything.
        """
        found = await self._space_post_repo.get(correlation_id)
        if found is not None:
            post_space, post = found
            if post_space != space_id:
                return False
            names = {
                media_basename(post.media_url),
                *(media_basename(u) for u in post.image_urls or ()),
                media_basename(post.file_meta.url if post.file_meta else None),
            }
            if filename in names:
                return True
            if post.type != PostType.BAZAAR:
                return False
            # A bazaar wrapper post carries its photos on the listing row.
            listing = (
                await self._bazaar_repo.get_listing(correlation_id)
                if self._bazaar_repo is not None
                else None
            )
            if listing is None:
                return True  # listing not here yet
            return listing.space_id == space_id and filename in {
                media_basename(u) for u in listing.image_urls
            }
        if self._gallery_repo is not None:
            item = await self._gallery_repo.get_item(correlation_id)
            if item is not None:
                album = await self._gallery_repo.get_album(item.album_id)
                if album is None or album.space_id != space_id:
                    return False
                return filename in {
                    media_basename(item.url),
                    media_basename(item.thumbnail_url),
                }
        return True

    async def _assemble_space_chunk(
        self,
        *,
        raw: bytes,
        filename: str,
        partial_dir: str,
        chunk_index: int,
        chunk_count: int,
        final: bool,
        post_id: object,
        from_instance: str,
        total_size: int,
    ) -> None:
        """Write one space-media chunk to disk and finalise on the last.

        ``raw`` is the already-decoded chunk bytes; ``filename`` has been
        validated and scope-checked by the caller; ``partial_dir`` is the
        sender-bound :func:`partial_key`. ``self._media_dir`` is
        guaranteed non-None by the caller. Every path ends in
        :func:`publish_once`, so an existing ``filename`` is never
        replaced.
        """
        assert self._media_dir is not None
        target = self._media_dir / filename
        partial_root = self._media_dir / ".partial" / partial_dir
        if await aiofiles.os.path.exists(target):
            # Already have it — a re-delivery, or a name that belongs to
            # something else. Nothing to write; drop any stale parts.
            if final:
                await note_existing(
                    target,
                    incoming_size=total_size,
                    what="SPACE_MEDIA_BLOB",
                    from_instance=from_instance,
                )
                await self._drop_space_partials(partial_root, chunk_count)
            return
        try:
            await aiofiles.os.makedirs(partial_root, exist_ok=True)
            part_path = partial_root / f"{chunk_index:06d}"
            async with aiofiles.open(part_path, "wb") as fh:
                await fh.write(raw)
        except OSError as exc:
            log.warning(
                "SPACE_MEDIA_BLOB: part write failed for %s chunk %d: %s",
                filename,
                chunk_index,
                exc,
            )
            return
        if not final:
            return
        # Final chunk landed — assemble the parts in index order into a
        # temp file next to them, then move it into place write-once.
        assembled = partial_root / "assembled"
        try:
            for i in range(chunk_count):
                if not await aiofiles.os.path.isfile(partial_root / f"{i:06d}"):
                    log.warning(
                        "SPACE_MEDIA_BLOB: chunk %d/%d missing at finalise "
                        "for %s — leaving partials for next attempt",
                        i,
                        chunk_count,
                        filename,
                    )
                    return
            async with aiofiles.open(assembled, "wb") as out:
                for i in range(chunk_count):
                    async with aiofiles.open(partial_root / f"{i:06d}", "rb") as inp:
                        await out.write(await inp.read())
            await aiofiles.os.makedirs(self._media_dir, exist_ok=True)
            published = await publish_once(assembled, target)
        except OSError as exc:
            log.warning(
                "SPACE_MEDIA_BLOB: finalise failed for %s: %s",
                filename,
                exc,
            )
            await remove_quietly(assembled)
            return
        await self._drop_space_partials(partial_root, chunk_count)
        if not published:
            await note_existing(
                target,
                incoming_size=total_size,
                what="SPACE_MEDIA_BLOB",
                from_instance=from_instance,
            )
            return
        log.info(
            "SPACE_MEDIA_BLOB: stored %s from %d chunk(s) for post=%s",
            filename,
            chunk_count,
            post_id,
        )

    @staticmethod
    async def _drop_space_partials(partial_root: pathlib.Path, count: int) -> None:
        for i in range(count):
            await remove_quietly(partial_root / f"{i:06d}")
        try:
            await aiofiles.os.rmdir(partial_root)
        except OSError:
            pass

    async def _on_space_post_updated(self, event: "FederationEvent") -> None:
        p = event.payload
        post_id = str(p.get("id") or p.get("post_id") or "")
        new_content = str(p.get("content") or "")
        if not post_id:
            return
        gated_space_id = resolve_space_id(event)
        if not gated_space_id:
            return
        if not await self._owned_post_mutation_allowed(event, gated_space_id, post_id):
            return
        if not await self._space_post_repo.edit(
            post_id,
            new_content,
            space_id=gated_space_id,
        ):
            log_cross_space_refusal(
                event,
                space_id=gated_space_id,
                what="post",
                row_id=post_id,
            )
            return
        refreshed = await self._space_post_repo.get(post_id)
        if refreshed is None:
            return
        space_id, post = refreshed
        await self._bus.publish(
            PostEdited(
                post=post,
                space_id=space_id,
                origin_instance_id=event.from_instance,
            )
        )

    async def _on_space_post_deleted(self, event: "FederationEvent") -> None:
        post_id = str(event.payload.get("post_id") or event.payload.get("id") or "")
        if not post_id:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        if not await self._owned_post_mutation_allowed(event, space_id, post_id):
            return
        moderated_by = event.payload.get("moderated_by")
        if not await self._space_post_repo.soft_delete(
            post_id,
            space_id=space_id,
            moderated_by=str(moderated_by) if moderated_by else None,
        ):
            log_cross_space_refusal(
                event,
                space_id=space_id,
                what="post",
                row_id=post_id,
            )
            return
        await self._bus.publish(
            PostDeleted(
                post_id=post_id,
                space_id=space_id,
                origin_instance_id=event.from_instance,
            )
        )

    async def _on_space_comment_added(self, event: "FederationEvent") -> None:
        p = event.payload
        post_id = str(p.get("post_id") or "")
        comment_id = str(p.get("comment_id") or p.get("id") or "")
        author = str(p.get("author") or "")
        if not post_id or not comment_id or not author:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        if owner_bound_id_refused(
            SPACE_COMMENT_KIND,
            comment_id,
            space_id=space_id,
            owner_user_id=author,
            context=f"{event.event_type} from {event.from_instance}",
        ):
            return
        authorship = self._space_authorship(event)
        if authorship is None:
            return
        # A live seat of any role counts: a Follower's comment reaches
        # here only through the §24.11 ``allow_subscriber_comment`` opt-in,
        # which already bound that author to a subscriber seat.
        if not await authorship.may_author(
            event, space_id, author, subscriber_comment=True
        ):
            await authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="comment",
                row_id=comment_id,
                user_id=author,
            )
            return
        comment_type_str = str(p.get("type") or "text")
        try:
            comment_type = CommentType(comment_type_str)
        except ValueError:
            comment_type = CommentType.TEXT
        comment = Comment(
            id=comment_id,
            post_id=post_id,
            author=author,
            type=comment_type,
            created_at=parse_iso8601_lenient(p.get("occurred_at")),
            parent_id=p.get("parent_id"),
            content=p.get("content") or "",
            media_url=local_media_ref(p.get("media_url")),
        )
        if not await self._space_post_repo.add_comment(comment, space_id=space_id):
            log_cross_space_refusal(
                event,
                space_id=space_id,
                what="post",
                row_id=post_id,
            )
            return
        await self._space_post_repo.increment_comment_count(
            post_id,
            space_id=space_id,
        )
        await self._bus.publish(
            CommentAdded(
                post_id=post_id,
                comment=comment,
                space_id=space_id,
                origin_instance_id=event.from_instance,
                mentions=await self._mentions.resolve(
                    space_id, comment.content, author_id=comment.author
                ),
            ),
        )

    async def _on_space_comment_updated(self, event: "FederationEvent") -> None:
        p = event.payload
        comment_id = str(p.get("id") or p.get("comment_id") or "")
        content = p.get("content")
        if not comment_id or content is None:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        if not await self._owned_comment_mutation_allowed(event, space_id, comment_id):
            return
        # No post id on the wire for this event — the repo's EXISTS
        # sub-select resolves the comment's parent post and checks it
        # lives in the gated space in the same statement.
        if not await self._space_post_repo.edit_comment(
            comment_id,
            str(content),
            space_id=space_id,
        ):
            log_cross_space_refusal(
                event,
                space_id=space_id,
                what="comment",
                row_id=comment_id,
            )
            return
        refreshed = await self._space_post_repo.get_comment(comment_id)
        if refreshed is None:
            return
        await self._bus.publish(
            CommentUpdated(
                post_id=refreshed.post_id,
                comment=refreshed,
                space_id=space_id,
                origin_instance_id=event.from_instance,
            ),
        )

    async def _on_space_comment_deleted(self, event: "FederationEvent") -> None:
        p = event.payload
        comment_id = str(p.get("comment_id") or p.get("id") or "")
        post_id = str(p.get("post_id") or "")
        if not comment_id or not post_id:
            return
        space_id = resolve_space_id(event)
        if not space_id:
            return
        if not await self._owned_comment_mutation_allowed(event, space_id, comment_id):
            return
        if not await self._space_post_repo.soft_delete_comment(
            comment_id,
            space_id=space_id,
        ):
            log_cross_space_refusal(
                event,
                space_id=space_id,
                what="comment",
                row_id=comment_id,
            )
            return
        # Decrement the comment's REAL parent, not the payload's post id.
        deleted = await self._space_post_repo.get_comment(comment_id)
        if deleted is not None:
            post_id = deleted.post_id
        await self._space_post_repo.decrement_comment_count(
            post_id,
            space_id=space_id,
        )
        await self._bus.publish(
            CommentDeleted(
                post_id=post_id,
                comment_id=comment_id,
                space_id=space_id,
                origin_instance_id=event.from_instance,
            ),
        )

    # ── Space content authorship (§24.11) ──────────────────────────────

    def _space_authorship(self, event: "FederationEvent") -> SpaceAuthorship | None:
        """The authorship binder, or ``None`` (logged) to refuse the write.

        Fail closed: a stack wired without the roster mirror cannot tell
        whose member a payload names, so it accepts no space content.
        """
        if self._authorship is None:
            log.warning(
                "%s from %s: no space roster mirror wired — cannot bind the "
                "author to the sending household; refusing the write",
                event.event_type,
                event.from_instance,
            )
        return self._authorship

    async def _owned_post_mutation_allowed(
        self,
        event: "FederationEvent",
        space_id: str,
        post_id: str,
    ) -> bool:
        """Edit / delete of a space post: the author's household or a
        moderator (the federated "author or space admin" rule of
        ``SpaceService.edit_post`` / ``delete_post``).

        Also sorts the benign no-ops (unknown here, already deleted) to
        DEBUG from the refusals (another space, not the author's
        household) at WARNING.
        """
        authorship = self._space_authorship(event)
        if authorship is None:
            return False
        got = await self._space_post_repo.get(post_id)
        if got is None:
            log_not_applied(event, what="post", row_id=post_id, reason="unknown here")
            return False
        row_space, post = got
        if row_space != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="post", row_id=post_id
            )
            return False
        if post.deleted:
            log_not_applied(
                event, what="post", row_id=post_id, reason="already deleted"
            )
            return False
        if not await authorship.may_mutate(event, space_id, post.author):
            await authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="post",
                row_id=post_id,
                user_id=post.author,
            )
            return False
        return True

    async def _owned_comment_mutation_allowed(
        self,
        event: "FederationEvent",
        space_id: str,
        comment_id: str,
    ) -> bool:
        """Edit / delete of a comment: the author's household or a
        moderator (``SpaceService.edit_comment`` / ``delete_comment``)."""
        authorship = self._space_authorship(event)
        if authorship is None:
            return False
        comment = await self._space_post_repo.get_comment(comment_id)
        if comment is None:
            log_not_applied(
                event, what="comment", row_id=comment_id, reason="unknown here"
            )
            return False
        parent = await self._space_post_repo.get(comment.post_id)
        if parent is None or parent[0] != space_id:
            log_cross_space_refusal(
                event, space_id=space_id, what="comment", row_id=comment_id
            )
            return False
        if comment.deleted:
            log_not_applied(
                event, what="comment", row_id=comment_id, reason="already deleted"
            )
            return False
        if not await authorship.may_mutate(event, space_id, comment.author):
            await authorship.hold_or_refuse(
                event,
                space_id=space_id,
                what="comment",
                row_id=comment_id,
                user_id=comment.author,
            )
            return False
        return True

    # ── Report handler ─────────────────────────────────────────────────

    async def _on_space_report(self, event: "FederationEvent") -> None:
        """A peer's member reported content we host — persist locally."""
        if self._report_service is None:
            log.debug("SPACE_REPORT received but no ReportService attached")
            return
        p = event.payload
        await self._report_service.create_report_from_remote(
            reporter_user_id=str(p.get("reporter_user_id") or ""),
            reporter_instance_id=event.from_instance,
            target_type=str(p.get("target_type") or ""),
            target_id=str(p.get("target_id") or ""),
            category=str(p.get("category") or ""),
            notes=p.get("notes"),
        )

    # ── Space membership handlers ──────────────────────────────────────

    async def _on_space_member_role_changed(self, event: "FederationEvent") -> None:
        """Apply a host's admin promotion/demotion locally (§13, multi-admin).

        The host broadcasts ``SPACE_MEMBER_ROLE_CHANGED`` whenever it
        grants/revokes admin on a member. Without applying it, the role
        lived only on the host: a promoted cross-household admin stayed a
        plain ``member`` on their own household, and every admin guard
        (``_require_admin_or_owner`` reads the *local* role) blocked them —
        so multi-admin never worked end-to-end. Roles are host-authoritative,
        so only honour the event from the space's owner instance.
        """
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        user_id = str(event.payload.get("user_id") or "")
        role = str(event.payload.get("role") or "")
        if (
            not space_id
            or not user_id
            or role
            not in (
                SpaceRole.ADMIN,
                SpaceRole.MEMBER,
            )
        ):
            return
        space = await self._space_repo.get(space_id)
        if space is None:
            return
        # Host authority: a role change is only valid from the owning
        # instance. Reject anything else (a peer can't promote itself).
        if space.owner_instance_id != event.from_instance:
            log.debug(
                "SPACE_MEMBER_ROLE_CHANGED for %s from non-host %s — dropping",
                space_id,
                event.from_instance,
            )
            return
        member_instance = str(event.payload.get("instance_id") or "")
        own = (
            self._federation_service.own_instance_id
            if self._federation_service is not None
            else None
        )
        if member_instance and own and member_instance != own:
            # The promoted member lives on another peer household — update
            # our roster mirror so the Members tab shows the right role.
            if self._space_remote_member_repo is None:
                return
            await self._space_remote_member_repo.set_role(
                space_id,
                member_instance,
                user_id,
                role,
            )
        else:
            # One of our own users was promoted/demoted — update their
            # local space_members role so the admin guards + SPA see it.
            if await self._space_repo.get_member(space_id, user_id) is None:
                return
            await self._space_repo.set_role(space_id, user_id, role)
        # Mirror the host's local notification so connected tabs refresh
        # the roster. Safe on a receiver: space_config_outbound only
        # re-broadcasts when this instance owns the space.
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=(
                    SpaceConfigEventType.ADMIN_GRANTED
                    if role == SpaceRole.ADMIN
                    else SpaceConfigEventType.ADMIN_REVOKED
                ).value,
                payload={
                    "user_id": user_id,
                    "instance_id": member_instance,
                    "role": role,
                },
                sequence=space.config_sequence,
            )
        )

    async def _on_space_config_changed(
        self,
        event: "FederationEvent",
    ) -> None:
        """§D1b — host renamed / re-emoji'd / toggled features. Apply
        to our local stub (if we have one) so the card stays
        accurate. Idempotent — ``save`` upserts.

        We only touch a row whose ``owner_instance_id`` matches the
        envelope's ``from_instance``. That guards against a peer's
        config-change event accidentally clobbering a row we own
        ourselves (impossible under correct usage, but defence in
        depth is cheap).
        """
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        if not space_id:
            return
        # New senders (B2+) wrap the metadata under one ``space_meta``
        # key — the same shape ``stub_space_from_metadata`` consumes
        # everywhere else. Older catch-up pushes ship the metadata
        # flat at the top of the payload (``name``, ``emoji``, …);
        # accept both so renames keep landing while a deployment
        # rolls out.
        meta = event.payload.get("space_meta")
        if not isinstance(meta, dict):
            meta = {
                k: event.payload.get(k)
                for k in (
                    "name",
                    "emoji",
                    "description",
                    "owner_instance_id",
                    "owner_username",
                    "identity_public_key",
                    "config_sequence",
                    "space_type",
                    "join_mode",
                    "tz",
                    "cover_hash",
                    "about_markdown",
                )
                if event.payload.get(k) is not None
            }
            feats = event.payload.get("features")
            if isinstance(feats, dict):
                meta["features"] = feats
        if not meta:
            return
        existing = await self._space_repo.get(space_id)
        if existing is None:
            # No stub yet — config-changed alone doesn't seat us as a
            # member, so creating one here would surface a card the
            # user never joined. Bail.
            return
        if existing.archived_reason:
            # Terminal state: the host dissolved this space (or removed us). A
            # later config snapshot must not revive it — ignore further config
            # changes for a remote-terminated space.
            return
        # ── Authorization (v_24) ────────────────────────────────────────────
        # Two ways an inbound config edit is authorised to mutate our stub:
        #
        #   (a) Legacy owner path — the §24.11-authenticated ``from_instance``
        #       is the row's ``owner_instance_id``. This is the host pushing its
        #       own config (rename / catch-up snapshot). Unchanged, no signature
        #       required (back-compat with pre-v_24 senders).
        #
        #   (b) Authority-signed path — the payload carries an
        #       ``authority_sig`` produced with the space's Ed25519 seed
        #       (:func:`sign_authority_event`). A seed-holding delegated admin
        #       can change config while the owner is offline; the trust root is
        #       the SIGNATURE verified against ``spaces.identity_public_key``,
        #       not ``from_instance``, so any member household (including the
        #       offline owner on reconnect) accepts it regardless of relay.
        #
        # SECURITY — fail-closed:
        #   * If a signature is PRESENT it MUST verify. A present-but-invalid /
        #     unknown-suite / wrong-key signature DROPS the event — it never
        #     falls through to the owner gate (otherwise an attacker could
        #     attach garbage and rely on (a) for an owner-spoofed envelope).
        #   * A NON-owner with NO signature is dropped (today's behaviour) —
        #     only a valid signature relaxes the owner-only gate.
        is_owner = existing.owner_instance_id == event.from_instance
        authority_sig = str(meta.get("authority_sig") or "")
        authority_sig_suite = str(meta.get("authority_sig_suite") or "")
        authority_verified = False
        if authority_sig or authority_sig_suite:
            if not (authority_sig and authority_sig_suite):
                log.warning(
                    "SPACE_CONFIG_CHANGED for %s from %s: partial authority "
                    "signature — dropping",
                    space_id,
                    event.from_instance,
                )
                return
            try:
                authority_verified = verify_authority_event(
                    event_type="space_config_changed",
                    space_id=space_id,
                    payload=strip_authority_sig_fields(meta),
                    authority_sig=authority_sig,
                    authority_sig_suite=authority_sig_suite,
                    space_public_key=bytes.fromhex(existing.identity_public_key),
                )
            except UnsupportedAuthoritySuite:
                log.warning(
                    "SPACE_CONFIG_CHANGED for %s: unknown authority_sig_suite "
                    "%r — dropping",
                    space_id,
                    authority_sig_suite,
                )
                return
            except Exception:
                log.warning(
                    "SPACE_CONFIG_CHANGED for %s: malformed space public key / "
                    "signature — dropping",
                    space_id,
                )
                return
            if not authority_verified:
                log.warning(
                    "SPACE_CONFIG_CHANGED for %s from %s: authority signature "
                    "did not verify against the space key — dropping",
                    space_id,
                    event.from_instance,
                )
                return
        if not (is_owner or authority_verified):
            # Non-owner, no valid signature → not authorised.
            return
        # ── Last-writer-wins ordering (v_24) ────────────────────────────────
        # With #459, every config edit broadcasts a SPACE_CONFIG_CHANGED, AND
        # the §D1b catch-up path (_push_config_to) still ships a snapshot on
        # demand — out-of-order delivery is normal. With v_24, two delegated
        # admins (or owner + admin) editing while offline of each other can
        # produce two DIFFERENT edits at the SAME ``config_sequence``. A bare
        # ``incoming_seq > existing`` guard would then let whichever arrived
        # LAST win per receiver — non-deterministic divergence.
        #
        # So order on the lexicographic TRIPLE
        # ``(config_sequence, config_hlc, author)`` and apply iff it is STRICTLY
        # GREATER than what we last applied (equal → no-op). The Hybrid Logical
        # Clock (migration 0037, ``HLC`` is ``order=True``) breaks an
        # equal-sequence tie by "later edit wins" — intuitive, monotonic per
        # node and drift-bounded across nodes, so every receiver derives the
        # SAME total order. A legacy zero-HLC row / older sender ships "0-0",
        # so the HLC component TIES and the existing author tie-break decides
        # — behaviour-identical to pre-0037 for non-HLC senders. The author is
        # read out of the AUTHORITY-SIGNED meta (``config_author_instance``),
        # so it can't be forged independently of the signature; the owner
        # legacy path (no signed author) falls back to ``from_instance``.
        # ``None`` author / missing seq / unparseable HLC sort lowest, matching
        # the prior fail-soft default for older senders.
        incoming_seq = _coerce_sequence(meta, event.payload)
        # The tie-break author MUST be authenticated. On the authority-signed
        # path the ``config_author_instance`` field is INSIDE the signed bytes,
        # so a relay can't forge it; trust it. On the legacy owner path there is
        # no signed author, so fall back to the §24.11-authenticated
        # ``from_instance`` (the owner) rather than an unsigned payload field an
        # owner could otherwise stuff to mis-order the LWW.
        if authority_verified:
            incoming_author = str(
                meta.get("config_author_instance") or event.from_instance or ""
            )
        else:
            incoming_author = str(event.from_instance or "")
        if incoming_seq is not None:
            # An unrecorded author (pre-v_24 row, or a stub seated before any
            # config edit) was, by construction, last written by the host —
            # default to ``owner_instance_id`` so an owner's equal-sequence
            # re-push stays a no-op (it doesn't out-rank itself) while a
            # delegated admin's higher-id concurrent edit can still win.
            existing_author = (
                await self._space_repo.get_config_author(space_id)
                or existing.owner_instance_id
                or ""
            )
            incoming_hlc = HLC.parse(meta.get("config_hlc"))
            # A signed config_hlc can't be trusted to be near real time on its
            # own — a seed-holder could stamp a far-future HLC to win every
            # config race forever. Bound it against the event's OWN
            # §24.11-checked envelope timestamp (already within ±300s of real
            # now), NOT local `now` — the envelope ts is identical on every
            # receiver, so this drop decision stays DETERMINISTIC (a local-now
            # clamp would diverge receivers). HLC_MAX_DRIFT_MS mirrors the skew
            # bound. A legacy edit with config_hlc absent (HLC(0,0),
            # physical_ms=0) trivially passes, preserving back-compat.
            envelope_ms = int(parse_iso8601_lenient(event.timestamp).timestamp() * 1000)
            if incoming_hlc.physical_ms > envelope_ms + HLC_MAX_DRIFT_MS:
                log.warning(
                    "SPACE_CONFIG_CHANGED for %s: config_hlc physical=%d outruns "
                    "envelope ts=%d by > %dms — dropping (clock-abuse guard)",
                    space_id,
                    incoming_hlc.physical_ms,
                    envelope_ms,
                    HLC_MAX_DRIFT_MS,
                )
                return
            existing_hlc = HLC.parse(existing.config_hlc)
            if (incoming_seq, incoming_hlc, incoming_author) <= (
                existing.config_sequence,
                existing_hlc,
                existing_author,
            ):
                log.debug(
                    "SPACE_CONFIG_CHANGED for %s: (seq=%d, hlc=%s, author=%s) <= "
                    "(seq=%d, hlc=%s, author=%s) — dropping (out-of-order or "
                    "replay)",
                    space_id,
                    incoming_seq,
                    incoming_hlc,
                    incoming_author,
                    existing.config_sequence,
                    existing_hlc,
                    existing_author,
                )
                return
        refreshed = stub_space_from_metadata(
            space_id,
            host_instance_id=event.from_instance,
            meta=meta,
        )
        # SECURITY: on the household that HOSTS this space, two feature flags
        # are the OWNER's alone — ``allow_subscribers`` (whether strangers on a
        # connection server may read the space) and ``delegated_admin_authority``
        # (whether admins act on the owner's behalf). The authority-signed path
        # above accepts a whole ``features`` block from ANY seed holder, so
        # without this a delegated admin could write over the owner's own row
        # and expose the space — or grant itself delegation. Pin both to what we
        # already store. We pin ONLY where we host: a member household mirroring
        # the space is not the authority for it, and the owner's value is
        # exactly what it should be mirroring.
        own_instance_id = (
            self._federation_service.own_instance_id
            if self._federation_service is not None
            else None
        )
        if own_instance_id and existing.owner_instance_id == own_instance_id:
            pinned = replace(
                refreshed.features,
                allow_subscribers=existing.features.allow_subscribers,
                delegated_admin_authority=(existing.features.delegated_admin_authority),
            )
            if pinned != refreshed.features:
                log.info(
                    "SPACE_CONFIG_CHANGED for %s from %s tried to change an "
                    "owner-only feature flag on the space we host "
                    "(allow_subscribers / delegated_admin_authority) — pinned "
                    "to the stored values; the rest of the block applies",
                    space_id,
                    event.from_instance,
                )
            refreshed = replace(refreshed, features=pinned)
        refreshed = _keep_local_space_state(
            refreshed,
            existing=existing,
            meta=meta,
            we_host=bool(own_instance_id)
            and existing.owner_instance_id == own_instance_id,
        )
        await self._space_repo.save(refreshed)
        # A cover / icon change ships the new image in ``space_meta``; land
        # it (validated) now that the change itself passed every gate above,
        # so an out-of-order older change can never roll the picture back.
        await apply_space_images_from_config_change(
            space_id,
            meta=meta,
            previous=existing,
            cover_repo=self._space_cover_repo,
            icon_repo=self._space_icon_repo,
        )
        # Record the author of the edit we just applied so a later
        # equal-sequence edit can deterministically tie-break (v_24 LWW).
        if incoming_author:
            await self._space_repo.set_config_author(space_id, incoming_author)

    async def _on_space_member_profile_updated(
        self,
        event: "FederationEvent",
    ) -> None:
        p = event.payload
        space_id = event.space_id or str(p.get("space_id") or "")
        user_id = str(p.get("user_id") or "")
        if not space_id or not user_id:
            return
        # SECURITY: a member's per-space display name / picture may only be
        # updated by the household that member is seated on. The seat is
        # looked up in our OWN roster mirror under the §24.11-authenticated
        # ``from_instance`` — never a payload field — so it only exists when
        # ``user_id`` is a live member of this space on the SENDER's
        # household. A local member, a member of another household, a
        # removed member, or an unknown user has no such seat: drop.
        seat = None
        if self._space_remote_member_repo is not None and event.from_instance:
            seat = await self._space_remote_member_repo.get(
                space_id,
                event.from_instance,
                user_id,
            )
        if seat is None:
            log.info(
                "SPACE_MEMBER_PROFILE_UPDATED for %s in %s from %s: no member "
                "seat on the sending household — dropping",
                user_id,
                space_id,
                event.from_instance,
            )
            return
        member = await self._space_repo.get_member(space_id, user_id)
        if member is None:
            # Unknown member on this side — skip silently; a membership
            # event will catch up eventually.
            return
        picture_hash = p.get("picture_hash")
        bytes_b64 = p.get("picture_webp_base64")
        if bytes_b64 and self._profile_picture_repo is not None:
            try:
                raw = base64.b64decode(bytes_b64)
                webp = await ImageProcessor().generate_thumbnail(
                    raw,
                    size=PROFILE_PICTURE_MAX_DIMENSION,
                )
                local_hash = compute_picture_hash(webp)
                await self._profile_picture_repo.set_member_picture(
                    space_id,
                    user_id,
                    bytes_webp=webp,
                    hash=local_hash,
                    width=PROFILE_PICTURE_MAX_DIMENSION,
                    height=PROFILE_PICTURE_MAX_DIMENSION,
                )
                picture_hash = local_hash
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "SPACE_MEMBER_PROFILE_UPDATED: bad blob for %s in %s: %s",
                    user_id,
                    space_id,
                    exc,
                )
        await self._space_repo.set_member_profile(
            space_id,
            user_id,
            space_display_name=p.get("space_display_name"),
            picture_hash=picture_hash,
        )
        await self._bus.publish(
            SpaceMemberProfileUpdated(
                space_id=space_id,
                user_id=user_id,
                space_display_name=p.get("space_display_name"),
                picture_hash=picture_hash,
            )
        )

    # ── User-profile handlers ──────────────────────────────────────────

    async def _on_users_sync(self, event: "FederationEvent") -> None:
        """Import a peer's full user roster.

        Each user is guarded on its own: the batch is a snapshot, so one
        unusable entry must never drop the tail of the envelope. Failures are
        logged at WARNING rather than swallowed — a silently-eaten
        ``IntegrityError`` here is exactly what hid the repaired-user_id
        collision (migration 0049) for a full release.
        """
        users = event.payload.get("users") or []
        if not isinstance(users, list):
            return
        for u in users:
            try:
                await self._upsert_remote_user(event.from_instance, u)
            except Exception as exc:
                log.warning(
                    "USERS_SYNC: skipping user %r from %s: %s",
                    (u or {}).get("username") if isinstance(u, dict) else u,
                    event.from_instance,
                    exc,
                )

    async def _on_user_updated(self, event: "FederationEvent") -> None:
        await self._upsert_remote_user(event.from_instance, event.payload)

    async def _on_user_removed(self, event: "FederationEvent") -> None:
        """Mark a remote user as deprovisioned + cascade-purge their content.

        Two layers of effect:

        * **Deprovision**: the ``remote_users`` row stays so historical
          posts / comments keep resolving to a display name, but the
          ``deprovisioned_at`` flag filters the user out of member
          lists and autocomplete via
          ``list_remote_for_instance``. The §24.11 receiver-side
          filter also keys off this flag to drop future user-scoped
          envelopes from the same user (even ones that arrive via
          mesh relay).
        * **Cascade purge**: every moment, highlight, and DM
          conversation involving this user is hard-deleted. Matches
          the "hide = remove" semantic the SPA copy promises in
          ConnectionDetail — forward-only would leave orphan content
          from a user who just disappeared from the household view.
        """
        user_id = str(event.payload.get("user_id") or "")
        if not user_id:
            return
        # Only the user's home household may remove them — never a local
        # member, another household's user, or an id we have never seen.
        if not await self._dm_scope.sent_from(event, user_id):
            refuse(event, "user is not homed on the sending household", user=user_id)
            return
        log.info("USER_REMOVED: flagging remote user %s as deprovisioned", user_id)
        await self._user_repo.mark_remote_deprovisioned(user_id)

        if self._moment_repo is not None:
            try:
                n = await self._moment_repo.delete_by_author(user_id)
                if n:
                    log.info(
                        "USER_REMOVED cascade: purged %d moments by %s",
                        n,
                        user_id,
                    )
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "USER_REMOVED cascade: moment purge failed for %s: %s",
                    user_id,
                    exc,
                )

        if self._highlight_repo is not None:
            try:
                n = await self._highlight_repo.delete_by_author(user_id)
                if n:
                    log.info(
                        "USER_REMOVED cascade: purged %d highlights by %s",
                        n,
                        user_id,
                    )
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "USER_REMOVED cascade: highlight purge failed for %s: %s",
                    user_id,
                    exc,
                )

        try:
            conv_ids = await self._conversation_repo.list_conversations_with_sender(
                user_id,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "USER_REMOVED cascade: conversation lookup failed for %s: %s",
                user_id,
                exc,
            )
            conv_ids = []
        for conv_id in conv_ids:
            conv = await self._conversation_repo.get(conv_id)
            if (
                conv is not None
                and conv.type is ConversationType.GROUP_DM
                and self._groups is not None
            ):
                # A group is everyone's, not the departed user's: clear
                # their messages, and (on its authority) their seat.
                await self._groups.remove_departed_user(conv_id, user_id)
                continue
            try:
                await self._conversation_repo.hard_delete(conv_id)
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "USER_REMOVED cascade: hard_delete(%s) failed: %s",
                    conv_id,
                    exc,
                )
        if conv_ids:
            log.info(
                "USER_REMOVED cascade: purged %d conversations involving %s",
                len(conv_ids),
                user_id,
            )

    async def _on_user_status_updated(self, event: "FederationEvent") -> None:
        p = event.payload
        user_id = str(p.get("user_id") or "")
        if not user_id:
            return
        if not await self._dm_scope.sent_from(event, user_id):
            refuse(event, "user is not homed on the sending household", user=user_id)
            return
        status: UserStatus | None
        if p.get("status_cleared"):
            status = None
        else:
            # Same caps the local ``PATCH /api/me`` applies — a peer
            # can't push a multi-line essay into our status surfaces.
            try:
                emoji = clean_status_emoji(p.get("emoji"))
                text = clean_status_text(p.get("text"))
            except ValueError as exc:
                refuse(event, f"invalid status: {exc}", user=user_id)
                return
            if emoji is None and text is None:
                status = None
            else:
                status = UserStatus(
                    emoji=emoji,
                    text=text,
                    expires_at=str(p["expires_at"]) if p.get("expires_at") else None,
                )
        await self._bus.publish(UserStatusChanged(user_id=user_id, status=status))

    # ── Highlight handlers ─────────────────────────────────────────────────

    async def _on_highlight_created(self, event: "FederationEvent") -> None:
        """Land a remote ``HIGHLIGHT_CREATED`` envelope.

        Persists the parent ``Highlight`` row (or upserts an existing one
        with refreshed audience/expiry) plus the first frame, then
        republishes :class:`HighlightFrameAdded` so :class:`RealtimeService`
        can fan a ``highlight.frame_added`` WS frame to local viewers.

        Authority check: the envelope's signed sender (``from_instance``)
        must equal the home instance of the payload's
        ``author_user_id`` — peers can't impersonate highlights from
        someone else's instance. The upsert is by id, so an id we already
        store must belong to that same author, and the frame id must not
        belong to another highlight (see :meth:`_highlight_write_binds`).
        """
        if self._highlight_repo is None:
            return
        p = event.payload
        highlight = self._highlight_from_payload(p)
        if highlight is None:
            log.debug("HIGHLIGHT_CREATED missing required field: %s", p)
            return
        if not await self._authority_matches(
            event.from_instance, highlight.author_user_id
        ):
            log.warning(
                "HIGHLIGHT_CREATED authority mismatch: envelope from %s, "
                "author %s lives elsewhere — dropped",
                event.from_instance,
                highlight.author_user_id,
            )
            return
        frame = self._frame_from_payload(highlight.id, p)
        if not await self._highlight_write_binds(event, highlight, frame):
            return
        await self._highlight_repo.save_highlight(highlight)
        if frame is not None:
            await self._highlight_repo.save_frame(frame)
            await self._publish_frame_added(highlight, frame, is_first=True, p=p)

    async def _highlight_write_binds(
        self,
        event: "FederationEvent",
        highlight: Highlight | None,
        frame: HighlightFrame | None,
    ) -> bool:
        """The by-id upserts may only touch rows that are already the sender's.

        ``save_highlight`` / ``save_frame`` upsert on the bare id the payload
        names, so the id alone must not reach a row that belongs to someone
        else: a stored highlight with that id must have the same author, and
        a stored frame with that id must already hang off the same highlight.
        Refusals are logged at WARNING and change nothing.
        """
        if self._highlight_repo is None:  # pragma: no cover — callers gate
            return False
        reason: str | None = None
        if highlight is not None:
            stored = await self._highlight_repo.get_highlight(highlight.id)
            if stored is not None and stored.author_user_id != (
                highlight.author_user_id
            ):
                reason = "highlight id belongs to another author"
        if reason is None and frame is not None:
            stored_frame = await self._highlight_repo.get_frame(frame.id)
            if stored_frame is not None and stored_frame.highlight_id != (
                frame.highlight_id
            ):
                reason = "frame id belongs to another highlight"
        if reason is None:
            return True
        log.warning(
            "%s from %s: %s (highlight=%s frame=%s) — refusing",
            event.event_type,
            event.from_instance,
            reason,
            highlight.id if highlight is not None else None,
            frame.id if frame is not None else None,
        )
        return False

    async def _on_highlight_frame_appended(self, event: "FederationEvent") -> None:
        """Append a frame to an existing remote highlight.

        We expect the ``HIGHLIGHT_CREATED`` envelope to have arrived first —
        if the parent highlight is missing locally (out-of-order delivery
        or pruned by retention), we lazily upsert it from the same
        payload, since every frame envelope carries the routing fields
        the parent needs.
        """
        if self._highlight_repo is None:
            return
        p = event.payload
        highlight_id = str(p.get("highlight_id") or "")
        if not highlight_id:
            log.debug("HIGHLIGHT_FRAME_APPENDED missing highlight_id: %s", p)
            return
        highlight = await self._highlight_repo.get_highlight(highlight_id)
        lazy_parent = highlight is None
        if highlight is None:
            highlight = self._highlight_from_payload(p)
            if highlight is None:
                log.debug(
                    "HIGHLIGHT_FRAME_APPENDED for unknown highlight_id %s and no "
                    "fallback metadata — dropped",
                    highlight_id,
                )
                return
            if not await self._authority_matches(
                event.from_instance, highlight.author_user_id
            ):
                log.warning(
                    "HIGHLIGHT_FRAME_APPENDED authority mismatch (lazy parent): "
                    "envelope from %s, author %s lives elsewhere — dropped",
                    event.from_instance,
                    highlight.author_user_id,
                )
                return
        else:
            if not await self._authority_matches(
                event.from_instance, highlight.author_user_id
            ):
                log.warning(
                    "HIGHLIGHT_FRAME_APPENDED authority mismatch: envelope from "
                    "%s, highlight author %s lives elsewhere — dropped",
                    event.from_instance,
                    highlight.author_user_id,
                )
                return
        frame = self._frame_from_payload(highlight.id, p)
        if frame is None:
            log.debug("HIGHLIGHT_FRAME_APPENDED missing frame fields: %s", p)
            return
        # Bind before any write: a lazily-created parent must not land when
        # the frame id it carries belongs to another highlight.
        if not await self._highlight_write_binds(event, None, frame):
            return
        if lazy_parent:
            await self._highlight_repo.save_highlight(highlight)
        await self._highlight_repo.save_frame(frame)
        await self._publish_frame_added(highlight, frame, is_first=False, p=p)

    async def _on_highlight_frame_deleted(self, event: "FederationEvent") -> None:
        if self._highlight_repo is None:
            return
        p = event.payload
        frame_id = str(p.get("frame_id") or "")
        if not frame_id:
            return
        # Bind to the stored frame: its own parent highlight's author must
        # live on the sender. A frame we don't hold is nothing to delete.
        frame = await self._highlight_repo.get_frame(frame_id)
        if frame is None:
            return
        highlight = await self._highlight_repo.get_highlight(frame.highlight_id)
        if highlight is None or not await self._authority_matches(
            event.from_instance, highlight.author_user_id
        ):
            log.warning(
                "HIGHLIGHT_FRAME_DELETED from %s: frame %s is not the "
                "sender's — refusing",
                event.from_instance,
                frame_id,
            )
            return
        await self._highlight_repo.delete_frame(frame_id)
        await self._bus.publish(
            HighlightFrameRemoved(
                highlight_id=highlight.id,
                frame_id=frame_id,
                author_user_id=highlight.author_user_id,
                audience_kind=highlight.audience_kind.value,
                audience=highlight.audience,
            )
        )

    async def _on_highlight_deleted(self, event: "FederationEvent") -> None:
        if self._highlight_repo is None:
            return
        p = event.payload
        highlight_id = str(p.get("highlight_id") or "")
        if not highlight_id:
            return
        highlight = await self._highlight_repo.get_highlight(highlight_id)
        if highlight is None:
            return
        if not await self._authority_matches(
            event.from_instance, highlight.author_user_id
        ):
            log.warning("HIGHLIGHT_DELETED authority mismatch: dropped")
            return
        await self._highlight_repo.delete_highlight(highlight_id)
        await self._bus.publish(
            HighlightRemoved(
                highlight_id=highlight_id,
                author_user_id=highlight.author_user_id,
                audience_kind=highlight.audience_kind.value,
                audience=highlight.audience,
            )
        )

    # ── Highlight back-channel handlers ────────────────────────────────────

    async def _on_highlight_frame_viewed(self, event: "FederationEvent") -> None:
        """A remote viewer marked one of *our* author's frames as seen.

        Persists the row in ``highlight_frame_views`` and republishes
        :class:`HighlightFrameViewed` so the realtime layer pushes the
        view-count update to the author's WS sessions.
        """
        if self._highlight_repo is None:
            return
        p = event.payload
        highlight_id = str(p.get("highlight_id") or "")
        frame_id = str(p.get("frame_id") or "")
        viewer_user_id = str(p.get("viewer_user_id") or "")
        if not (highlight_id and frame_id and viewer_user_id):
            return
        highlight = await self._local_frame_reply_target(
            event, highlight_id, frame_id, viewer_user_id
        )
        if highlight is None:
            return
        await self._highlight_repo.mark_viewed(frame_id, viewer_user_id)
        await self._bus.publish(
            HighlightFrameViewed(
                highlight_id=highlight.id,
                frame_id=frame_id,
                viewer_user_id=viewer_user_id,
                author_user_id=highlight.author_user_id,
            )
        )

    async def _local_frame_reply_target(
        self,
        event: "FederationEvent",
        highlight_id: str,
        frame_id: str,
        actor_user_id: str,
    ) -> Highlight | None:
        """Return our author's highlight iff the sender may reply on ``frame_id``.

        Views and reactions are unicast to the author's home
        (``HighlightFederationOutbound._on_frame_viewed`` /
        ``_on_reaction_changed``), so a reply is honoured only when all of
        these hold — otherwise it is dropped with a WARNING:

        * ``actor_user_id``'s home household is the sending household. An
          unknown user, a local member and another household's user are
          all refused;
        * the frame exists here and hangs off ``highlight_id``;
        * that highlight is authored by one of *our* users.

        The caller publishes the stored author, never the payload's.
        """
        if self._highlight_repo is None:  # pragma: no cover — callers gate
            return None
        reason: str | None = None
        highlight: Highlight | None = None
        home = await self._user_repo.get_instance_for_user(actor_user_id)
        if home is None or home != event.from_instance:
            reason = "user does not belong to the sending household"
        else:
            frame = await self._highlight_repo.get_frame(frame_id)
            if frame is None or frame.highlight_id != highlight_id:
                reason = "frame is not part of that highlight here"
            else:
                highlight = await self._highlight_repo.get_highlight(highlight_id)
                if highlight is None or (
                    await self._user_repo.get_by_user_id(highlight.author_user_id)
                    is None
                ):
                    reason = "frame is not authored here"
        if reason is None:
            return highlight
        log.warning(
            "%s from %s: %s (highlight=%s frame=%s user=%s) — refusing",
            event.event_type,
            event.from_instance,
            reason,
            highlight_id,
            frame_id,
            actor_user_id,
        )
        return None

    async def _on_highlight_frame_reacted(self, event: "FederationEvent") -> None:
        await self._handle_reaction_envelope(event, removed=False)

    async def _on_highlight_frame_reaction_removed(
        self,
        event: "FederationEvent",
    ) -> None:
        await self._handle_reaction_envelope(event, removed=True)

    async def _handle_reaction_envelope(
        self,
        event: "FederationEvent",
        *,
        removed: bool,
    ) -> None:
        if self._highlight_repo is None:
            return
        p = event.payload
        highlight_id = str(p.get("highlight_id") or "")
        frame_id = str(p.get("frame_id") or "")
        reactor_user_id = str(p.get("reactor_user_id") or "")
        emoji = None if removed else (p.get("emoji") or None)
        if not (highlight_id and frame_id and reactor_user_id):
            return
        highlight = await self._local_frame_reply_target(
            event, highlight_id, frame_id, reactor_user_id
        )
        if highlight is None:
            return
        if removed or emoji is None:
            await self._highlight_repo.clear_reaction(frame_id, reactor_user_id)
            published_emoji: str | None = None
        else:
            await self._highlight_repo.set_reaction(frame_id, reactor_user_id, emoji)
            published_emoji = emoji
        await self._bus.publish(
            HighlightFrameReactionChanged(
                highlight_id=highlight.id,
                frame_id=frame_id,
                reactor_user_id=reactor_user_id,
                author_user_id=highlight.author_user_id,
                emoji=published_emoji,
            )
        )

    # ── Momentum handlers ──────────────────────────────────────────────

    async def _on_moment_created(self, event: "FederationEvent") -> None:
        """Land a remote moment and fire :class:`MomentCreated`.

        Authority check: the envelope's signed sender (``from_instance``)
        must equal the home of ``payload.author_user_id`` for a top-level
        post. The 3-hop relay re-broadcasts the *original* envelope, so
        when the relay path is in play the receiver verifies against
        ``payload.origin_instance_id`` instead — that's the only field
        that pins the original sender across hops.
        """
        if self._moment_repo is None:
            return
        p = event.payload
        moment_id = str(p.get("moment_id") or "")
        author_user_id = str(p.get("author_user_id") or "")
        origin_instance_id = str(p.get("origin_instance_id") or "")
        if not (moment_id and author_user_id and origin_instance_id):
            log.debug("MOMENT_CREATED missing required fields: %s", p)
            return
        if self._moment_id_refused(event, moment_id, author_user_id):
            return
        if not await self._moment_authority_matches(
            event.from_instance,
            origin_instance_id,
            author_user_id,
        ):
            log.warning("MOMENT_CREATED authority mismatch — dropped")
            return
        if not await self._moment_origin_verified(event, origin_instance_id):
            return
        if not await self._moment_row_binds(
            event, moment_id, author_user_id, origin_instance_id
        ):
            return
        if await self._moment_create_is_stale(event, moment_id):
            return
        # §Momentum-relay-policy: drop banned-source / open-report
        # envelopes before either persist or relay.
        if self._relay_policy is not None and not await self._relay_policy.allow_relay(
            source_instance_id=event.from_instance,
            author_user_id=author_user_id,
            target_id=moment_id,
        ):
            log.info(
                "MOMENT_CREATED dropped by relay policy: from=%s moment=%s",
                event.from_instance,
                moment_id,
            )
            return
        media_type = p.get("media_type")
        if media_type not in ("image", "video", None):
            media_type = None
        try:
            hop_count = int(p.get("hop_count") or 1)
        except TypeError, ValueError:
            hop_count = 1
        moment = Moment(
            id=moment_id,
            author_user_id=author_user_id,
            content=str(p.get("content") or ""),
            media_url=p.get("media_url"),
            media_type=media_type,
            duration_ms=(
                int(p["duration_ms"]) if p.get("duration_ms") is not None else None
            ),
            parent_moment_id=p.get("parent_moment_id"),
            origin_instance_id=origin_instance_id,
            created_at=str(p.get("occurred_at") or _now_iso()),
            expires_at=str(p.get("expires_at") or _now_iso()),
            hop_count=hop_count,
            received_via="household",
        )
        # §Momentum-relay-policy pure-pass-through: skip the local
        # persist when no local user can see the row (under their
        # max_hops + block list). The relay still runs below — this
        # instance acts as a transparent forwarder.
        wants_local = await self._moment_repo.has_visible_recipient(
            author_user_id=author_user_id,
            hop_count=hop_count,
        )
        parent_author_user_id: str | None = None
        if wants_local:
            await self._moment_repo.save(moment)
            # Look up the parent's author so the local notification
            # handler can ping them when the inbound moment is a reply.
            if moment.parent_moment_id is not None:
                parent = await self._moment_repo.get(moment.parent_moment_id)
                if parent is not None:
                    parent_author_user_id = parent.author_user_id
            # Bus republish so the realtime layer + downstream
            # listeners see the same shape they'd see on a local
            # write.
            await self._bus.publish(
                MomentCreated(
                    moment_id=moment.id,
                    author_user_id=moment.author_user_id,
                    content=moment.content,
                    media_url=moment.media_url,
                    media_type=moment.media_type,
                    duration_ms=moment.duration_ms,
                    parent_moment_id=moment.parent_moment_id,
                    parent_author_user_id=parent_author_user_id,
                    origin_instance_id=moment.origin_instance_id,
                    expires_at=moment.expires_at,
                )
            )
        # 3-hop relay: forward to the rest of *our* paired peers. The
        # outbound's bus subscriber would skip this event (author isn't
        # local), so the relay must be triggered explicitly here. Runs
        # whether or not we persisted locally — the no-redistribute
        # guard inside ``relay_inbound`` is layered on top.
        await self._maybe_relay(
            event_type=event.event_type,
            payload=p,
            from_instance=event.from_instance,
        )

    async def _on_moment_deleted(self, event: "FederationEvent") -> None:
        if self._moment_repo is None:
            return
        p = event.payload
        moment_id = str(p.get("moment_id") or "")
        author_user_id = str(p.get("author_user_id") or "")
        origin_instance_id = str(p.get("origin_instance_id") or "")
        if not (moment_id and author_user_id and origin_instance_id):
            return
        # A delete for a bound id naming anybody but its author is not
        # remembered — it would otherwise refuse the real moment's create.
        if self._moment_id_refused(event, moment_id, author_user_id):
            return
        if not await self._moment_authority_matches(
            event.from_instance,
            origin_instance_id,
            author_user_id,
        ):
            log.warning("MOMENT_DELETED authority mismatch — dropped")
            return
        if not await self._moment_origin_verified(event, origin_instance_id):
            return
        if not await self._moment_row_binds(
            event, moment_id, author_user_id, origin_instance_id
        ):
            return
        # Deletes stick: the row stays as a tombstone until the moment
        # expires, so a replayed create is refused. A moment we never
        # stored (the delete outran its create on another relay path) is
        # held the same way, for the longest a moment can live. Either way
        # the delete travels on so households that hold it apply it too.
        removed = await self._moment_repo.tombstone(
            moment_id,
            author_user_id=author_user_id,
            origin_instance_id=origin_instance_id,
            expires_at=(
                datetime.now(timezone.utc) + timedelta(days=MOMENT_RETENTION_DAYS)
            ).isoformat(),
        )
        if removed:
            await self._bus.publish(
                MomentDeleted(
                    moment_id=moment_id,
                    author_user_id=author_user_id,
                    origin_instance_id=origin_instance_id,
                )
            )
        await self._maybe_relay(
            event_type=event.event_type,
            payload=p,
            from_instance=event.from_instance,
        )

    async def _on_moment_reacted(self, event: "FederationEvent") -> None:
        await self._handle_moment_reaction(event, removed=False)

    async def _on_moment_reaction_removed(
        self,
        event: "FederationEvent",
    ) -> None:
        await self._handle_moment_reaction(event, removed=True)

    async def _handle_moment_reaction(
        self,
        event: "FederationEvent",
        *,
        removed: bool,
    ) -> None:
        if self._moment_repo is None:
            return
        p = event.payload
        moment_id = str(p.get("moment_id") or "")
        reactor_user_id = str(p.get("reactor_user_id") or "")
        if not (moment_id and reactor_user_id):
            return
        # Reactions are unicast to the author's home: the reactor must be a
        # user of the sending household, and the moment one authored here.
        # The published author is the stored one, never the payload's.
        reason: str | None = None
        moment = None
        if await self._user_repo.get_instance_for_user(reactor_user_id) != (
            event.from_instance
        ):
            reason = "reactor does not belong to the sending household"
        else:
            moment = await self._moment_repo.get(moment_id)
            if moment is None or (
                await self._user_repo.get_by_user_id(moment.author_user_id) is None
            ):
                reason = "moment is not authored here"
        if reason is not None or moment is None:
            log.warning(
                "%s from %s: %s (moment=%s user=%s) — refusing",
                event.event_type,
                event.from_instance,
                reason,
                moment_id,
                reactor_user_id,
            )
            return
        author_user_id = moment.author_user_id
        emoji = None if removed else (p.get("emoji") or None)
        if removed or emoji is None:
            await self._moment_repo.clear_reaction(moment_id, reactor_user_id)
            published_emoji: str | None = None
        else:
            await self._moment_repo.set_reaction(
                moment_id,
                reactor_user_id,
                emoji,
            )
            published_emoji = emoji
        await self._bus.publish(
            MomentReactionChanged(
                moment_id=moment_id,
                reactor_user_id=reactor_user_id,
                author_user_id=author_user_id,
                emoji=published_emoji,
            )
        )

    @staticmethod
    def _moment_id_refused(
        event: "FederationEvent", moment_id: str, author_user_id: str
    ) -> bool:
        """An owner-bound moment id (v_36) claimed for another author.

        The stored row binds a moment id to whoever named it here first
        (:meth:`_moment_row_binds`, and a delete's tombstone), so a bound id
        is checked before either: a claim for anyone but the author it
        commits to is neither stored, tombstoned nor relayed. A legacy id
        keeps the first-come rule.
        """
        return owner_bound_id_refused(
            MOMENT_KIND,
            moment_id,
            space_id="",
            owner_user_id=author_user_id,
            context=f"{event.event_type} from {event.from_instance}",
        )

    async def _moment_authority_matches(
        self,
        from_instance: str,
        origin_instance_id: str,
        author_user_id: str,
    ) -> bool:
        """Origin-vs-relay authority check.

        On a 1-hop direct delivery, ``from_instance == origin_instance_id``
        and ``origin_instance_id`` should be the author's home instance.
        On a 2/3-hop relay, ``from_instance != origin_instance_id`` —
        we trust the origin field on the payload as long as the
        author's home instance lookup matches. Unknown authors (the
        ``USER_UPDATED`` envelope hasn't landed yet) fall through and
        accept the row.

        Never accepted, whichever path: a moment whose claimed origin is
        this household, or whose author is one of our own users — those
        only ever originate here.
        """
        own = (
            self._federation_service.own_instance_id
            if self._federation_service is not None
            else ""
        )
        if (own and origin_instance_id == own) or (
            await self._user_repo.get_by_user_id(author_user_id) is not None
        ):
            return False
        if from_instance == origin_instance_id:
            # Direct delivery — also check the author lives there.
            try:
                home = await self._user_repo.get_instance_for_user(author_user_id)
            except Exception:  # pragma: no cover — defensive
                return True
            return home is None or home == origin_instance_id
        # Relay: trust the origin field; sender just relayed.
        try:
            home = await self._user_repo.get_instance_for_user(author_user_id)
        except Exception:  # pragma: no cover — defensive
            return True
        return home is None or home == origin_instance_id

    async def _moment_origin_verified(
        self,
        event: "FederationEvent",
        origin_instance_id: str,
    ) -> bool:
        """A relayed moment must carry its origin household's signature.

        A direct delivery (``from_instance == origin_instance_id``) is bound
        by the §24.11 envelope signature. A relayed one was re-sent under the
        relay's own envelope, so ``origin_instance_id`` is only the relay's
        claim until the origin's identity signature over the moment verifies
        (v_35, :mod:`socialhome.federation.moment_origin`) — against the key
        pinned for the origin, or a shipped key bound by
        ``derive_instance_id`` for a friend-of-friend origin we never paired
        with. The one exception is the legacy window: an unsigned relay from
        an origin we hold a row for that predates signing, logged at INFO.
        Refused events are neither applied nor relayed.
        """
        if event.from_instance == origin_instance_id:
            return True
        fed = self._federation_service
        if fed is None:  # no key directory: nothing can be verified
            return False
        verdict = check_relayed_moment_origin(
            event_type=event.event_type,
            payload=event.payload,
            origin_instance_id=origin_instance_id,
            pinned_pk=await fed.peer_identity_public_key(origin_instance_id),
            origin_signs=await fed.peer_supports(
                origin_instance_id,
                min_version=FederationCapability.MIN_FOR_MOMENT_ORIGIN_SIGNATURE,
            ),
        )
        if not verdict.accepted:
            log.warning(
                "%s relayed by %s for origin %s: %s — refusing (moment=%s)",
                event.event_type,
                event.from_instance,
                origin_instance_id,
                verdict.reason,
                event.payload.get("moment_id"),
            )
            return False
        if verdict.legacy:
            log.info(
                "%s relayed by %s: accepting unsigned moment from pre-v_%d"
                " origin %s (legacy window — closes when it upgrades)",
                event.event_type,
                event.from_instance,
                FederationCapability.MIN_FOR_MOMENT_ORIGIN_SIGNATURE,
                origin_instance_id,
            )
        return True

    async def _moment_create_is_stale(
        self,
        event: "FederationEvent",
        moment_id: str,
    ) -> bool:
        """A create for a deleted or expired moment is never applied.

        The origin signature proves who made a moment, not that it still
        exists, so a relay can re-send an old signed create. Refused while
        the delete's tombstone exists (until the moment's ``expires_at``);
        once the tombstone is swept the replay is past its own
        ``expires_at`` and refused on that. Neither is applied or relayed.
        """
        assert self._moment_repo is not None  # callers gate
        stored = await self._moment_repo.get(moment_id, include_deleted=True)
        reason: str | None = None
        if stored is not None and stored.deleted_at is not None:
            reason = "the moment was deleted"
        elif _moment_expired(event.payload.get("expires_at")):
            reason = "the moment has expired"
        if reason is None:
            return False
        log.info(
            "MOMENT_CREATED from %s: %s — refusing (moment=%s)",
            event.from_instance,
            reason,
            moment_id,
        )
        return True

    async def _moment_row_binds(
        self,
        event: "FederationEvent",
        moment_id: str,
        author_user_id: str,
        origin_instance_id: str,
    ) -> bool:
        """A moment id we already store must carry the same author + origin.

        ``moment_repo.save`` upserts on the bare id, and a delete names only
        the id: the stored row, not the payload, says whose moment it is.
        Refused writes are neither applied nor relayed.
        """
        if self._moment_repo is None:  # pragma: no cover — callers gate
            return False
        stored = await self._moment_repo.get(moment_id, include_deleted=True)
        if stored is None or (
            stored.author_user_id == author_user_id
            and stored.origin_instance_id == origin_instance_id
        ):
            return True
        log.warning(
            "%s from %s: moment %s belongs to another author — refusing",
            event.event_type,
            event.from_instance,
            moment_id,
        )
        return False

    async def _maybe_relay(
        self,
        *,
        event_type,
        payload: dict,
        from_instance: str,
    ) -> None:
        if self._moment_outbound is None:
            return
        try:
            await self._moment_outbound.relay_inbound(
                event_type=event_type,
                payload=payload,
                from_instance=from_instance,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("moment-relay failed: %s", exc)

    async def _publish_frame_added(
        self,
        highlight: Highlight,
        frame: HighlightFrame,
        *,
        is_first: bool,
        p: dict,
    ) -> None:
        await self._bus.publish(
            HighlightFrameAdded(
                highlight_id=highlight.id,
                frame_id=frame.id,
                author_user_id=highlight.author_user_id,
                highlight_date=highlight.highlight_date,
                sequence=frame.sequence,
                is_first_frame=is_first,
                audience_kind=highlight.audience_kind.value,
                audience=highlight.audience,
                frame_type=frame.frame_type.value,
                media_url=frame.media_url,
                caption_text=frame.caption_text,
                caption_emoji=frame.caption_emoji,
                duration_ms=frame.duration_ms,
                expires_at=highlight.expires_at or str(p.get("expires_at") or ""),
            )
        )

    @staticmethod
    def _highlight_from_payload(payload: dict) -> Highlight | None:
        highlight_id = str(payload.get("highlight_id") or "")
        author = str(payload.get("author_user_id") or "")
        highlight_date = str(payload.get("highlight_date") or "")
        if not highlight_id or not author or not highlight_date:
            return None
        try:
            kind = HighlightAudience(str(payload.get("audience_kind") or "all_paired"))
        except ValueError:
            kind = HighlightAudience.ALL_PAIRED
        audience = tuple(str(x) for x in (payload.get("audience") or ()))
        return Highlight(
            id=highlight_id,
            author_user_id=author,
            highlight_date=highlight_date,
            audience_kind=kind,
            audience=audience,
            expires_at=str(payload.get("expires_at") or "") or None,
        )

    @staticmethod
    def _frame_from_payload(highlight_id: str, payload: dict) -> HighlightFrame | None:
        frame_id = str(payload.get("frame_id") or "")
        if not frame_id:
            return None
        try:
            ftype = HighlightFrameType(str(payload.get("frame_type") or "image"))
        except ValueError:
            ftype = HighlightFrameType.IMAGE
        try:
            sequence = int(payload.get("sequence") or 1)
        except TypeError, ValueError:
            sequence = 1
        try:
            duration = (
                int(payload["duration_ms"])
                if payload.get("duration_ms") is not None
                else None
            )
        except TypeError, ValueError:
            duration = None
        media_url = str(payload.get("media_url") or "")
        if not media_url:
            return None
        return HighlightFrame(
            id=frame_id,
            highlight_id=highlight_id,
            sequence=sequence,
            frame_type=ftype,
            media_url=media_url,
            caption_text=payload.get("caption_text"),
            caption_emoji=payload.get("caption_emoji"),
            duration_ms=duration,
        )

    async def _authority_matches(
        self,
        from_instance: str,
        author_user_id: str,
    ) -> bool:
        """Reject envelopes that claim authorship for a user not on the
        sending instance. Mismatches are logged + dropped so a misbehaved
        peer can't plant content on the audience's behalf."""
        try:
            home = await self._user_repo.get_instance_for_user(author_user_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("highlight authority lookup failed: %s", exc)
            return False
        # If the author is unknown locally, accept on first sight — the
        # ``USER_UPDATED`` / ``USERS_SYNC`` envelope from the same peer
        # will arrive eventually and seed ``remote_users``.
        if home is None:
            return True
        return home == from_instance

    # ── Helpers ────────────────────────────────────────────────────────

    async def _upsert_remote_user(self, instance_id: str, payload: dict) -> None:
        user_id = str(payload.get("user_id") or "")
        username = str(payload.get("username") or payload.get("remote_username") or "")
        if not user_id or not username:
            return
        # Binding check BEFORE any write — the picture table is shared with
        # local users, so a refused entry must not touch it either. A user
        # already known here (local or remote) is only ever published by
        # their own home household.
        home = await self._user_repo.get_instance_for_user(user_id)
        if home is not None and home != instance_id:
            log.warning(
                "user sync from %s refused: user %s is homed elsewhere",
                instance_id,
                user_id,
            )
            return
        if home is None and not await self._derives_from_sender(
            instance_id, user_id, username, payload
        ):
            return
        picture_hash = payload.get("picture_hash")

        # If the peer shipped fresh picture bytes, revalidate and store
        # locally. We trust the signature on the envelope (§24.11) but
        # still re-run the image through ImageProcessor so a malicious
        # peer can't plant arbitrary bytes in the blob table.
        bytes_b64 = payload.get("picture_webp_base64")
        if bytes_b64 and self._profile_picture_repo is not None:
            try:
                raw = base64.b64decode(bytes_b64)
                webp = await ImageProcessor().generate_thumbnail(
                    raw,
                    size=PROFILE_PICTURE_MAX_DIMENSION,
                )
                local_hash = compute_picture_hash(webp)
                await self._profile_picture_repo.set_user_picture(
                    user_id,
                    bytes_webp=webp,
                    hash=local_hash,
                    width=PROFILE_PICTURE_MAX_DIMENSION,
                    height=PROFILE_PICTURE_MAX_DIMENSION,
                )
                picture_hash = local_hash
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "USER_UPDATED: rejected remote picture for %s: %s",
                    user_id,
                    exc,
                )

        # ``handle`` is unsigned public display metadata (like display_name /
        # picture) — older peers omit it. Pass None through when absent so the
        # repo's COALESCE leaves any previously-cached handle untouched rather
        # than nulling it on a non-handle profile edit.
        raw_handle = payload.get("handle")
        handle = str(raw_handle) if raw_handle else None
        remote = RemoteUser(
            user_id=user_id,
            instance_id=instance_id,
            remote_username=username,
            display_name=str(payload.get("display_name") or username),
            picture_hash=picture_hash,
            bio=payload.get("bio"),
            public_key=payload.get("public_key"),
            handle=handle,
            synced_at=_now_iso(),
        )
        await self._user_repo.upsert_remote(remote)
        await self._release_held_dms(instance_id, user_id)

        # Per-user identity binding (independent user identity, proto v_25).
        # When the entry carries the self-verifying assertion fields, verify
        # the WHOLE assertion against the sender's pinned instance key and, on
        # success, persist the user's own identity public key. This is the
        # transplant-resistant path: the binding can be cached/relayed and
        # re-verified outside the original §24.11 envelope (Phase 3/4), so we
        # check the instance signature here too — not just the user self-sig.
        await self._store_user_identity_binding(instance_id, user_id, payload)

    async def _release_held_dms(self, instance_id: str, user_id: str) -> None:
        """Replay DMs held for ``user_id`` now that their user row exists.

        Each replay runs the full DM rules again, so a DM some other
        household sent in that user's name is refused at this point.
        """
        if self._pending is None or self._federation_service is None:
            return
        for held in self._pending.release(
            space_id=_DM_HOLD_SCOPE, instance_id=instance_id, user_id=user_id
        ):
            await self._federation_service.replay_held(
                held, reason=f"user {user_id} synced"
            )

    async def _derives_from_sender(
        self,
        instance_id: str,
        user_id: str,
        username: str,
        payload: dict,
    ) -> bool:
        """A first-seen ``user_id`` must derive from the sender's pinned key.

        ``derive_user_id(sender_pk, identity_anchor or username)`` — the same
        construction every household mints its users with — so a household
        can only introduce its own users and never pre-claim somebody else's
        id before that user's home syncs it. Refused (WARNING) when the key
        can't be resolved or the id doesn't derive.
        """
        sender_pk = (
            await self._federation_service.peer_identity_public_key(instance_id)
            if self._federation_service is not None
            else None
        )
        raw_anchor = payload.get("identity_anchor")
        seed = raw_anchor if isinstance(raw_anchor, str) and raw_anchor else username
        derived = ""
        if sender_pk is not None:
            try:
                derived = derive_user_id(sender_pk, seed)
            except ValueError:
                derived = ""
        if derived and derived == user_id:
            return True
        log.warning(
            "user sync from %s refused: new user %s does not derive from the "
            "sender's identity key",
            instance_id,
            user_id,
        )
        return False

    async def _store_user_identity_binding(
        self,
        instance_id: str,
        user_id: str,
        payload: dict,
    ) -> None:
        """Verify + store a per-user identity binding, fail-soft.

        No-op when the binding fields are absent. Any verification failure
        (bad signature, unknown suite, instance_id mismatch, missing sender
        key) logs a WARNING and leaves the key unset — never raises, never
        drops the surrounding sync, never stores an unverified key.
        """
        pubkey_hex = payload.get("user_identity_public_key")
        if not pubkey_hex:
            return  # legacy / first-revision entry — nothing to bind.
        if self._federation_service is None:
            return  # not attached (test stack) — can't resolve sender key.

        sender_pk = await self._federation_service.peer_identity_public_key(instance_id)
        if sender_pk is None:
            log.warning(
                "user-identity-binding: no pinned key for sender %s; "
                "not storing binding for %s",
                instance_id,
                user_id,
            )
            return

        # The anchor (proto v_26) is the uuid the verifier derives user_id from
        # when present (else username, the v_25 path). Reconstruct it onto the
        # assertion so ``verify_user_identity_assertion`` commits to it in both
        # the user_id-derivation check and the signed-bytes check — a forged
        # anchor (one whose derivation doesn't match the asserted user_id, or
        # one not committed to by the instance signature) fails verify and is
        # rejected fail-soft below.
        raw_anchor = payload.get("identity_anchor")
        identity_anchor = str(raw_anchor) if raw_anchor is not None else None
        assertion = UserIdentityAssertion(
            user_id=user_id,
            instance_id=instance_id,
            username=str(
                payload.get("username") or payload.get("remote_username") or ""
            ),
            display_name=str(payload.get("display_name") or ""),
            issued_at=str(payload.get("user_assertion_issued_at") or ""),
            signature=str(payload.get("user_assertion_signature") or ""),
            user_identity_public_key=str(pubkey_hex),
            user_pq_public_key=payload.get("user_pq_public_key"),
            user_sig_suite=payload.get("user_sig_suite"),
            user_signature=payload.get("user_signature"),
            identity_anchor=identity_anchor,
        )
        try:
            verify_user_identity_assertion(assertion, sender_pk)
        except (ValueError, UnsupportedUserSigSuite) as exc:
            # ValueError covers the bind-sanity instance_id mismatch (the
            # assertion's instance_id must equal the sender it's attributed to),
            # bad signatures and expiry; UnsupportedUserSigSuite covers an
            # unknown suite. All fail-soft: legacy upsert already happened.
            log.warning(
                "user-identity-binding: rejected binding for %s from %s: %s",
                user_id,
                instance_id,
                exc,
            )
            return

        # KNOWN LIMITATION (Phase 1): this is an unconditional UPDATE. The
        # assertion's ±24h issued_at staleness gate (in verify) bounds replay,
        # but there is no per-user monotonic freshness check, so a captured
        # ≤24h-old valid binding could replay over a *rotated* key (only ever to
        # a prior LEGITIMATE key — attacker-key injection is blocked by the
        # sender-pinned-key verification above). Inert in Phase 1: user-key
        # rotation does not exist yet. Phase 3/4 (bindings cached + relayed
        # standalone) MUST add a stored-issued_at monotonic guard before the
        # UPDATE. See docs/protocol/capabilities.md v_25 + the design spec.
        await self._user_repo.set_remote_user_identity_key(
            user_id,
            public_key_hex=str(pubkey_hex),
            identity_anchor=identity_anchor,
        )

    def _post_from_payload(self, payload: dict) -> Post | None:
        post_id = str(payload.get("id") or payload.get("post_id") or "")
        author = str(payload.get("author") or "")
        if not post_id or not author:
            return None
        type_str = str(payload.get("type") or "text")
        try:
            post_type = PostType(type_str)
        except ValueError:
            post_type = PostType.TEXT
        # Location is carried inside the encrypted payload alongside the
        # rest of the post body. Drop it silently if the peer sent
        # malformed coords — the post itself is still readable.
        # GPS coordinates are truncated to 4 decimal places before
        # storage to match the §25 / CLAUDE.md privacy invariant — peers
        # could send full precision, so the receiver re-applies the
        # truncation rather than trusting the wire value.
        location: LocationData | None = None
        raw_loc = payload.get("location")
        if isinstance(raw_loc, dict):
            try:
                lat_t = truncate_coord(float(raw_loc["lat"]))
                lon_t = truncate_coord(float(raw_loc["lon"]))
                if lat_t is None or lon_t is None:
                    raise ValueError("nan coordinate")
                location = LocationData(
                    lat=lat_t,
                    lon=lon_t,
                    label=raw_loc.get("label"),
                )
            except KeyError, TypeError, ValueError:
                location = None
        return Post(
            id=post_id,
            author=author,
            type=post_type,
            content=payload.get("content"),
            # Media references only in the local-upload shape
            # (``api/media/<name>``); anything else is dropped.
            media_url=local_media_ref(payload.get("media_url")),
            file_meta=None,
            location=location,
            created_at=parse_iso8601_lenient(payload.get("occurred_at")),
            # Mirror the host's feed visibility. Absent on an older sender
            # → default visible (the historical behaviour).
            hidden_from_feed=bool(payload.get("hidden_from_feed", False)),
            # The multi-image gallery the sender ships (``SpacePostOutbound``).
            # Local-upload references only, capped at the feed maximum; the
            # bytes follow as ``SPACE_MEDIA_BLOB``, which is checked against
            # these names.
            image_urls=local_media_refs(
                payload.get("image_urls"), limit=FEED_POST_MAX_IMAGES
            ),
        )
