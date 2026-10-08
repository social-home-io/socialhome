"""DM (direct message) service — 1:1 and group conversations (§23.47).

Orchestrates :class:`AbstractConversationRepo` for conversation lifecycle,
message CRUD, reactions, and read tracking.

**Privacy rules (§25.3):**

* Message content never appears in push notification bodies.
* ``sanitise_for_api`` is applied at the route layer, not here — the
  service returns full domain objects.

**Permissions:**

* Only conversation members can send messages, react, or mark read.
* Any member of a group DM can add a new participant.
* Only the creator of a group DM can rename it.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.conversation import (
    DmSelfError,
    DmTooLongError,
    GroupTooSmallError,
    Conversation,
    ConversationMember,
    ConversationMessage,
    CONVERSATION_NOTIF_LEVELS,
    ConversationType,
    MESSAGE_TYPES,
    RemoteConversationMember,
    mute_until_for,
)
from ..domain.errors import CodedError, InvalidMediaRefError
from ..domain.dm_location import normalise_location_content
from ..domain.events import (
    DmConversationCreated,
    DmMessageCreated,
    DmMessageReactionChanged,
    DmMessageUpdated,
)
from ..domain.federation import FederationEventType
from ..federation import compat
from ..infrastructure.event_bus import EventBus
from ..media.cleanup import unlink_unreferenced
from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.user_repo import AbstractUserRepo
from .dm_group_service import DmGroupService, clean_group_name
from .dm_mentions import MENTIONABLE_TYPES, DmMentionResolver
from .inbound_media_store import local_media_ref
from .protection_gate import ProtectionGateMixin
from .visibility import VisibilityMixin

if TYPE_CHECKING:
    from .audio_transcription_service import AudioTranscriptionService  # noqa: F401
    from .dm_media_sync_service import DmMediaSyncService  # noqa: F401
    from .system_chat_policy import SystemChatPolicy
    from ..federation.federation_service import FederationService
    from ..repositories.dm_routing_repo import AbstractDmRoutingRepo
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.peer_user_visibility_repo import (
        AbstractPeerUserVisibilityRepo,
    )


log = logging.getLogger(__name__)

#: Message types whose ``content`` the sender may edit: text, media
#: captions and a location pin. A voice note's ``content`` is its machine
#: transcript and a call entry is system text — neither is the sender's.
EDITABLE_TYPES: frozenset[str] = MENTIONABLE_TYPES | {"location"}


#: What a personal block answers the blocked sender with. A guardian block
#: (§CP.F2) answers the person it blocks with these very words, so nothing
#: tells them a guardian — or a protected account — is involved.
RECIPIENT_BLOCKED_DETAIL = "Recipient has you blocked."
#: What the protected account itself hears.
GUARDIAN_BLOCK_DETAIL = "You can't message this person."
#: A group a guardian block rules out (whoever creates or changes it).
GUARDIAN_BLOCK_GROUP_DETAIL = "One of these people can't be added to this group."

#: Per-message length cap (§23.47). Households don't need novella-length
#: chat messages; the cap also bounds the search-index row size and the
#: notification fan-out cost.
MAX_DM_LENGTH: int = 1000


#: What a sender who blocked the recipient hears.
YOU_BLOCKED_DETAIL = (
    "You have blocked this user — unblock them in Settings to "
    "continue this conversation."
)

#: The stable API code for each refusal, keyed by its words. A guardian
#: block reaches the person it blocks as ``RECIPIENT_BLOCKED_DETAIL`` — so
#: it carries the very same code, and nothing in the answer tells a
#: guardian (or a protected account) is involved.
_BLOCK_CODES: dict[str, str] = {
    RECIPIENT_BLOCKED_DETAIL: "DM_BLOCKED",
    YOU_BLOCKED_DETAIL: "DM_YOU_BLOCKED",
    GUARDIAN_BLOCK_DETAIL: "DM_NOT_ALLOWED",
    GUARDIAN_BLOCK_GROUP_DETAIL: "DM_GROUP_NOT_ALLOWED",
}


class RecipientBlockedError(CodedError, PermissionError):
    """Raised when a DM cannot be sent because of a personal block.

    Symmetric: rejects both "I blocked them, why am I DMing them?" and
    "they blocked me, they don't want this." :class:`BaseView._iter`
    answers 403 with the code matching the detail (``_BLOCK_CODES``).
    """

    status = 403

    def __init__(self, detail: str) -> None:
        super().__init__(detail, code=_BLOCK_CODES.get(detail, "FORBIDDEN"))


class MediaRequiresDirectPairingError(ValueError):
    """Raised when a media attachment can't go through the relay path.

    Operator decision (issue #319 paragraph 5, "federated-only" media):
    image / video / file attachments are only shared with households we
    have a direct confirmed pairing with. A DM that requires the
    multi-hop ``DM_RELAY`` path (bazaar-style inquiries, transitive-
    only peers) rejects media so a low-trust acquaintance can't pull
    bytes through a third-party relay.

    Mapped to HTTP 422 by :class:`BaseView._iter` with the
    ``MEDIA_REQUIRES_DIRECT_PAIRING`` error code so the SPA can render
    a clear "this peer needs to be in your trusted households to
    share pictures" message.
    """


class DmService(VisibilityMixin, ProtectionGateMixin):
    """Conversation + message CRUD for household DMs.

    Guardian blocks (§CP.F2, via :class:`ProtectionGateMixin`) apply like a
    personal block, in both directions: the pair can't open or continue a
    1:1, share a group, or see each other's messages. In a group the
    protected account can't post while a blocked person is seated; a blocked
    person's posts still reach the others but never the protected account.
    """

    __slots__ = (
        "_child_protection",
        "_convos",
        "_users",
        "_bus",
        "_federation",
        "_federation_repo",
        "_dm_routing_repo",
        "_media_sync",
        "_audio_transcription",
        "_media_dir",
        "_media_refs",
        "_pending_transcribe_tasks",
        "_own_instance_id",
        "_groups",
        "_system_chats",
    )

    def __init__(
        self,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
        *,
        federation_service: "FederationService | None" = None,
        federation_repo: "AbstractFederationRepo | None" = None,
        dm_routing_repo: "AbstractDmRoutingRepo | None" = None,
        media_sync: "DmMediaSyncService | None" = None,
        audio_transcription: "AudioTranscriptionService | None" = None,
        media_dir: pathlib.Path | None = None,
        media_refs: AbstractMediaReferenceRepo | None = None,
        own_instance_id: str = "",
        visibility_repo: "AbstractPeerUserVisibilityRepo | None" = None,
    ) -> None:
        self._convos = conversation_repo
        self._users = user_repo
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._dm_routing_repo = dm_routing_repo
        # Cross-household media sync (preview building + DM_MEDIA_BLOB
        # outbox). Optional — when ``None``, media DMs work for same-
        # household only; cross-household sends silently skip the
        # full-bytes follow-up. The unit-test stack omits this to keep
        # the in-memory fixture small.
        self._media_sync = media_sync
        # Sender-side voice-note transcript. Optional — when ``None``
        # (no ``adapter.stt`` configured, or running in a test stack)
        # audio messages still send and play back; the recipient may
        # fill the transcript via :class:`AudioTranscriptScheduler`'s
        # local-STT fallback. Pairs with ``media_dir`` because the
        # transcription task reads the just-uploaded blob from disk.
        self._audio_transcription = audio_transcription
        self._media_dir = media_dir
        self._media_refs = media_refs
        # Hold strong references to the fire-and-forget transcription
        # tasks so the event loop doesn't GC them mid-flight (the
        # Python-3.11+ behaviour with ``ensure_future`` is that an
        # untracked Task can be collected before completion).
        self._pending_transcribe_tasks: set[asyncio.Task[None]] = set()
        self._own_instance_id = own_instance_id
        self._visibility_repo = visibility_repo
        self._child_protection = None
        #: Live access rules for system chats (household / space); without
        #: it every system chat is refused (fail closed).
        self._system_chats: "SystemChatPolicy | None" = None
        #: Group-conversation membership authority (v_37). Always present —
        #: a local-only group needs it too; federation is attached later.
        self._groups = DmGroupService(
            conversation_repo=conversation_repo,
            user_repo=user_repo,
            bus=bus,
            own_instance_id=own_instance_id,
        )
        if federation_service is not None and federation_repo is not None:
            self._groups.attach_federation(
                federation_service, federation_repo, own_instance_id
            )

    def attach_audio_transcription(
        self,
        service: "AudioTranscriptionService",
    ) -> None:
        """Wire voice-note STT after construction.

        ``AudioTranscriptionService`` depends on :class:`PlatformAdapter`,
        which is built later in ``create_app`` than ``DmService``. This
        setter is the same shape as :meth:`attach_federation` — late
        binding so the wiring order in ``app.py`` stays linear.
        """
        self._audio_transcription = service

    def attach_system_chats(self, policy: "SystemChatPolicy") -> None:
        """Wire the access rules of system chats (household / space chat).

        Every membership check on a conversation with ``system_scope`` set
        defers to ``policy`` instead of the seat rows.
        """
        self._system_chats = policy

    def attach_federation(
        self,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        own_instance_id: str,
    ) -> None:
        """Wire federation after construction (breaks the DM ↔ federation cycle)."""
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._own_instance_id = own_instance_id
        self._groups.attach_federation(
            federation_service, federation_repo, own_instance_id
        )

    # ── Conversations ──────────────────────────────────────────────────

    async def create_dm(
        self,
        *,
        creator_username: str,
        other_username: str | None = None,
        other_user_id: str | None = None,
    ) -> Conversation:
        """Start a 1:1 DM. Idempotent if one already exists between these
        two participants — returns the existing conversation.

        ``other_username`` resolves a *local* user. ``other_user_id`` is
        the cross-household path: the DM is opened with a remote user
        already known via a paired peer's directory snapshot
        (``remote_users`` row). Exactly one must be supplied.
        """
        if (other_username is None) == (other_user_id is None):
            raise ValueError(
                "create_dm: pass exactly one of other_username / other_user_id",
            )
        creator = await self._require_user(creator_username)

        # Local-target path — both participants are users on this instance.
        if other_username is not None:
            other = await self._require_user(other_username)
            if creator.username == other.username:
                raise DmSelfError()
            await self._guard_block_pair(creator.user_id, other.user_id)

            existing = await self._convos.list_for_user(creator_username)
            for conv in existing:
                if conv.type is not ConversationType.DM:
                    continue
                members = await self._convos.list_members(conv.id)
                usernames = {m.username for m in members}
                if usernames == {creator.username, other.username}:
                    return conv

            conv = Conversation(
                id=uuid.uuid4().hex,
                type=ConversationType.DM,
                created_at=datetime.now(timezone.utc),
            )
            await self._convos.create(conv)
            now = datetime.now(timezone.utc).isoformat()
            await self._convos.add_member(
                ConversationMember(
                    conversation_id=conv.id,
                    username=creator.username,
                    joined_at=now,
                )
            )
            await self._convos.add_member(
                ConversationMember(
                    conversation_id=conv.id,
                    username=other.username,
                    joined_at=now,
                )
            )
            await self._publish_conversation_created(
                conv,
                creator_user_id=creator.user_id,
                member_usernames=(creator.username, other.username),
            )
            return conv

        # Cross-household path — ``other_user_id`` references a remote user
        # already mirrored locally by the peer-directory snapshot. We
        # seat the local creator as a :class:`ConversationMember` and
        # the remote target as a :class:`RemoteConversationMember`; from
        # there the DM_MESSAGE federation envelopes route via the remote
        # member's instance_id.
        get_remote = getattr(self._users, "get_remote", None)
        if get_remote is None:
            raise ValueError(
                "create_dm: cross-household DM requires user repo with get_remote",
            )
        remote = await get_remote(str(other_user_id))
        if remote is None:
            raise KeyError(f"remote user {other_user_id!r} not found")
        if remote.user_id == creator.user_id:
            raise DmSelfError()
        await self._guard_block_pair(creator.user_id, remote.user_id)

        existing = await self._convos.list_for_user(creator_username)
        for conv in existing:
            if conv.type is not ConversationType.DM:
                continue
            local_members = await self._convos.list_members(conv.id)
            remote_members = await self._convos.list_remote_members(conv.id)
            local_set = {m.username for m in local_members}
            remote_set = {(m.instance_id, m.remote_username) for m in remote_members}
            if local_set == {creator.username} and remote_set == {
                (remote.instance_id, remote.remote_username),
            }:
                return conv

        conv = Conversation(
            id=uuid.uuid4().hex,
            type=ConversationType.DM,
            created_at=datetime.now(timezone.utc),
        )
        await self._convos.create(conv)
        now = datetime.now(timezone.utc).isoformat()
        await self._convos.add_member(
            ConversationMember(
                conversation_id=conv.id,
                username=creator.username,
                joined_at=now,
            )
        )
        await self._convos.add_remote_member(
            RemoteConversationMember(
                conversation_id=conv.id,
                instance_id=remote.instance_id,
                remote_username=remote.remote_username,
                joined_at=now,
            )
        )
        await self._publish_conversation_created(
            conv,
            creator_user_id=creator.user_id,
            member_usernames=(creator.username,),
        )
        return conv

    async def create_group_dm(
        self,
        *,
        creator_username: str,
        member_usernames: list[str] | None = None,
        member_user_ids: list[str] | None = None,
        name: str | None = None,
    ) -> Conversation:
        """Start a group DM (3+ participants), possibly spanning households.

        ``member_usernames`` names local users; ``member_user_ids`` names
        anyone by ``user_id`` — a local user, or a person mirrored from a
        directly paired household that takes part in group chats (v_37+,
        see :meth:`DmGroupService.remote_seat_for`). This household becomes
        the group's authority: the id is bound to it, and it ships the
        member list to every other member household.
        """
        creator = await self._require_user(creator_username)
        requested = {creator.username, *(member_usernames or [])}
        if len(requested) + len(set(member_user_ids or [])) < 3:
            raise GroupTooSmallError(3)
        conv_id = self._groups.mint_conversation_id()
        local_names, remote_seats = await self._resolve_new_members(
            conv_id,
            usernames=member_usernames or [],
            user_ids=member_user_ids or [],
        )
        local_names = list(dict.fromkeys([creator.username, *local_names]))
        if len(local_names) + len(remote_seats) < 3:
            raise GroupTooSmallError(3)
        await self._guard_group_roster(local_names, remote_seats)
        conv = Conversation(
            id=conv_id,
            type=ConversationType.GROUP_DM,
            name=clean_group_name(name),
            created_at=datetime.now(timezone.utc),
            membership_version=1,
        )
        await self._groups.commit(
            conv,
            local_usernames=sorted(local_names),
            remote_members=remote_seats,
            creator_user_id=creator.user_id,
        )
        return conv

    async def _resolve_new_members(
        self,
        conversation_id: str,
        *,
        usernames: list[str],
        user_ids: list[str],
    ) -> tuple[list[str], list[RemoteConversationMember]]:
        """Split requested members into local usernames and remote seats."""
        local_names: list[str] = []
        for uname in usernames:
            local_names.append((await self._require_user(uname)).username)
        remote_seats: dict[str, RemoteConversationMember] = {}
        for uid in user_ids:
            local = await self._users.get_by_user_id(uid)
            if local is not None:
                local_names.append(local.username)
                continue
            seat = await self._groups.remote_seat_for(conversation_id, uid)
            remote_seats[uid] = seat
        return list(dict.fromkeys(local_names)), list(remote_seats.values())

    async def _group_for_change(
        self,
        conversation_id: str,
        actor_username: str,
    ) -> Conversation:
        """A group this household manages, changed by one of its members."""
        conv = await self._require_dm_conversation(conversation_id)
        if conv.type is not ConversationType.GROUP_DM:
            raise ValueError("cannot change the members of a 1:1 DM")
        await self._require_membership(conversation_id, actor_username)
        self._groups.require_authority_here(conversation_id)
        return conv

    async def _commit_group(
        self,
        conv: Conversation,
        *,
        local_usernames: list[str],
        remote_members: list[RemoteConversationMember],
        name: str | None,
    ) -> None:
        await self._groups.commit(
            Conversation(
                id=conv.id,
                type=conv.type,
                name=name,
                created_at=conv.created_at,
                membership_version=conv.membership_version + 1,
            ),
            local_usernames=local_usernames,
            remote_members=remote_members,
        )

    async def _active_local_usernames(self, conversation_id: str) -> list[str]:
        return [
            m.username
            for m in await self._convos.list_members(conversation_id)
            if m.deleted_at is None
        ]

    async def add_group_members(
        self,
        conversation_id: str,
        *,
        actor_username: str,
        usernames: list[str] | None = None,
        user_ids: list[str] | None = None,
    ) -> None:
        """A member of the authority household adds people to the group."""
        conv = await self._group_for_change(conversation_id, actor_username)
        new_local, new_remote = await self._resolve_new_members(
            conversation_id,
            usernames=usernames or [],
            user_ids=user_ids or [],
        )
        if not new_local and not new_remote:
            raise ValueError("nobody to add")
        seats = {
            (s.instance_id, s.remote_username): s
            for s in await self._convos.list_remote_members(conversation_id)
        }
        for s in new_remote:
            seats[(s.instance_id, s.remote_username)] = s
        local = await self._active_local_usernames(conversation_id)
        roster = list(dict.fromkeys([*local, *new_local]))
        await self._guard_group_roster(roster, list(seats.values()))
        await self._commit_group(
            conv,
            local_usernames=roster,
            remote_members=list(seats.values()),
            name=conv.name,
        )

    async def remove_group_member(
        self,
        conversation_id: str,
        *,
        actor_username: str,
        user_id: str,
    ) -> None:
        """A member of the authority household removes someone from the group."""
        conv = await self._group_for_change(conversation_id, actor_username)
        local = await self._active_local_usernames(conversation_id)
        target_local = await self._users.get_by_user_id(user_id)
        seats = await self._convos.list_remote_members(conversation_id)
        if target_local is not None and target_local.username in local:
            local = [u for u in local if u != target_local.username]
        else:
            kept = [s for s in seats if not await self._seat_is_user(s, user_id)]
            if len(kept) == len(seats):
                raise KeyError(f"user {user_id!r} is not in this group")
            seats = kept
        await self._commit_group(
            conv, local_usernames=local, remote_members=seats, name=conv.name
        )

    async def rename_group(
        self,
        conversation_id: str,
        *,
        actor_username: str,
        name: str | None,
    ) -> None:
        """A member of the authority household renames the group."""
        conv = await self._group_for_change(conversation_id, actor_username)
        await self._commit_group(
            conv,
            local_usernames=await self._active_local_usernames(conversation_id),
            remote_members=await self._convos.list_remote_members(conversation_id),
            name=clean_group_name(name),
        )

    async def _seat_is_user(self, seat: RemoteConversationMember, user_id: str) -> bool:
        if seat.user_id is not None:
            return seat.user_id == user_id
        ru = await self._users.get_remote_by_member(
            seat.instance_id, seat.remote_username
        )
        return ru is not None and ru.user_id == user_id

    @property
    def groups(self) -> DmGroupService:
        """The group-membership authority (wired into federation at startup)."""
        return self._groups

    async def _publish_conversation_created(
        self,
        conv: Conversation,
        *,
        creator_user_id: str,
        member_usernames: tuple[str, ...],
    ) -> None:
        """Emit ``DmConversationCreated`` for the WS fan-out path.

        Resolves usernames → user_ids inline (the realtime service keys
        WS sessions on user_id, not username). Failures during lookup
        skip the missing member rather than blocking conversation
        creation — the recipient will still see the new DM on their
        next page load.
        """
        member_ids: list[str] = []
        for uname in member_usernames:
            try:
                u = await self._users.get(uname)
            except Exception:  # pragma: no cover - defensive
                u = None
            if u is not None:
                member_ids.append(u.user_id)
        await self._bus.publish(
            DmConversationCreated(
                conversation_id=conv.id,
                conversation_type=conv.type.value,
                name=conv.name,
                creator_user_id=creator_user_id,
                member_user_ids=tuple(member_ids),
            )
        )

    async def add_group_member(
        self,
        conversation_id: str,
        *,
        actor_username: str,
        new_username: str,
    ) -> None:
        """Add one local user — see :meth:`add_group_members`."""
        await self.add_group_members(
            conversation_id,
            actor_username=actor_username,
            usernames=[new_username],
        )

    async def list_conversations(self, username: str) -> list[Conversation]:
        convs = await self._convos.list_for_user(username)
        user = await self._users.get(username)
        blocked = (
            await self._guardian_block_counterparts(user.user_id)
            if user is not None
            else frozenset()
        )
        if not blocked:
            return convs
        # §CP.F2: a 1:1 with someone a guardian block separates from the
        # viewer is gone from their list (either side), like a personal block.
        kept: list[Conversation] = []
        for conv in convs:
            if conv.type is ConversationType.DM and blocked & set(
                await self._seat_user_ids(conv.id)
            ):
                continue
            kept.append(conv)
        return kept

    async def get_conversation(self, conversation_id: str) -> Conversation:
        return await self._require_conversation(conversation_id)

    # ── Messages ───────────────────────────────────────────────────────

    async def send_message(
        self,
        conversation_id: str,
        *,
        sender_username: str,
        content: str,
        type: str = "text",
        media_url: str | None = None,
        file_name: str | None = None,
        mime_type: str | None = None,
        file_size_bytes: int | None = None,
        reply_to_id: str | None = None,
        reply_to_highlight_frame_id: str | None = None,
        reply_to_highlight_frame_snapshot: str | None = None,
    ) -> ConversationMessage:
        """Send a message. ``sender_username`` must be a member.

        Content is stored verbatim — sanitisation is the route layer's
        responsibility.
        """
        conv = await self._require_conversation(conversation_id)
        await self._require_membership(conversation_id, sender_username, write=True)
        sender = await self._require_user(sender_username)
        # §CP.F2 guardian blocks — every seat, local or remote, 1:1 or group.
        # ``withheld`` are the local members this message must not reach.
        withheld = await self._guard_guardian_blocks(conv, sender.user_id)
        # 1:1 DM block gate (§Privacy). Group DMs ignore personal blocks
        # in v1 — see ``conversation_repo.list_for_user`` for the
        # matching read-side note.
        if conv.type is ConversationType.DM:
            members = await self._convos.list_members(conversation_id)
            for m in members:
                if m.username == sender_username:
                    continue
                peer = await self._users.get(m.username)
                if peer is None:
                    continue
                await self._guard_block_pair(sender.user_id, peer.user_id)
        if type not in MESSAGE_TYPES:
            raise ValueError(f"invalid message type {type!r}")
        is_media = type in ("image", "video", "file", "audio")
        # ``text`` requires content; media types carry the bytes via
        # ``media_url`` instead so an empty caption (or, for audio, an
        # empty transcript pending STT) is fine.
        if not content and not is_media and type == "text":
            raise ValueError("message content must not be empty")
        if len(content) > MAX_DM_LENGTH:
            raise DmTooLongError(MAX_DM_LENGTH)
        if is_media and not media_url:
            raise ValueError(f"{type!r} messages require ``media_url``")
        if media_url:
            # Only a local upload is ever stored: the SPA renders this as
            # a link / "Open in new tab" target for every member, so a
            # ``javascript:`` or remote URL would be stored XSS / an IP
            # leak. Normalised to ``api/media/<name>``.
            ref = local_media_ref(media_url)
            if ref is None:
                raise InvalidMediaRefError()
            media_url = ref
        if type == "location":
            # Structured pin — validated and rounded (4-dp coords, coarse
            # accuracy bucket) here, before it is stored, published on the
            # bus or federated, so raw device precision never leaves this
            # call (CLAUDE.md GPS rule).
            content = normalise_location_content(content)
        # Federated-only rule (operator decision, issue #319 paragraph 5):
        # media may only be shared with peers we have a direct confirmed
        # pairing with — never via the multi-hop DM_RELAY path used for
        # bazaar-style inquiries to unrelated households. ``_fan_to_remote``
        # already skips unconfirmed peers downstream, but the rule there is
        # "drop silently"; for media we want the sender to know immediately
        # so they don't think the picture went through.
        if is_media:
            await self._reject_media_on_relay_only_conversation(conversation_id)

        msg = ConversationMessage(
            id=uuid.uuid4().hex,
            conversation_id=conversation_id,
            sender_user_id=sender.user_id,
            content=content,
            type=type,
            media_url=media_url,
            file_name=file_name,
            mime_type=mime_type,
            file_size_bytes=file_size_bytes,
            reply_to_id=reply_to_id,
            reply_to_highlight_frame_id=reply_to_highlight_frame_id,
            reply_to_highlight_frame_snapshot=reply_to_highlight_frame_snapshot,
            created_at=datetime.now(timezone.utc),
        )
        await self._convos.save_message(msg)

        # Fan-out: every member except the sender is a push recipient.
        # ConversationMember stores usernames, not user_ids — resolve
        # to user_ids for the PushService which keys on user_id.
        # Cross-household DMs also include the user_ids of remote
        # members so :meth:`FederationInboundService._on_dm_message`
        # on the recipient side can ensure the conversation row +
        # local membership exists for the right user.
        recipients: list[str] = []
        for m in await self._convos.list_members(conversation_id):
            if m.username == sender_username:
                continue
            if m.deleted_at is not None and conv.is_system:
                continue
            u = await self._users.get(m.username)
            if u is None or u.user_id in withheld:
                continue
            if conv.is_system and u.state != "active":
                # A seat the reconciler has not taken out yet never rings
                # a deactivated account.
                continue
            recipients.append(u.user_id)
        # Resolve each remote member's ``user_id`` via the
        # ``remote_users`` mirror so the federation envelope carries
        # the full recipient set. ``RemoteConversationMember`` only
        # stores ``(instance_id, remote_username)``; the user_id has
        # to be looked up by-instance so the receiver-side inbound
        # handler can ensure conversation membership for the right
        # local user.
        for rm in await self._convos.list_remote_members(conversation_id):
            if rm.user_id is not None:
                # A group seat names its user (they may be on a household
                # we never paired with, so ``remote_users`` can't).
                if rm.user_id not in recipients:
                    recipients.append(rm.user_id)
                continue
            list_remote = getattr(self._users, "list_remote_for_instance", None)
            if list_remote is None:
                continue
            for ru in await list_remote(rm.instance_id):
                if ru.remote_username == rm.remote_username:
                    if ru.user_id not in recipients:
                        recipients.append(ru.user_id)
                    break
        await self._bus.publish(
            DmMessageCreated(
                conversation_id=conversation_id,
                message_id=msg.id,
                sender_user_id=sender.user_id,
                sender_display_name=sender.display_name,
                recipient_user_ids=tuple(recipients),
                content=content,
                message_type=type,
                media_url=media_url,
                file_name=file_name,
                mime_type=mime_type,
                file_size_bytes=file_size_bytes,
                reply_to_id=reply_to_id,
                occurred_at=msg.created_at,
                mentions=(
                    await self._mentions().resolve(conversation_id, content)
                    if type in MENTIONABLE_TYPES
                    else ()
                ),
            )
        )
        # Stamp a monotonic sender_seq on the envelope when the
        # routing repo is wired, so recipients can run §12.5 gap
        # detection. Absent repo → legacy behaviour (no seq field).
        seq: int | None = None
        if self._dm_routing_repo is not None:
            seq = await self._dm_routing_repo.next_sender_seq(
                conversation_id=conversation_id,
                sender_user_id=sender.user_id,
            )
        payload: dict = {
            "conversation_id": conversation_id,
            "message_id": msg.id,
            "sender_user_id": sender.user_id,
            "sender_display_name": sender.display_name,
            "type": type,
            "content": content,
            "media_url": media_url,
            "reply_to_id": reply_to_id,
            "occurred_at": msg.created_at.isoformat(),
            "recipient_user_ids": recipients,
        }
        # v_3 fields. ``compat.transform_for_peer`` strips them for
        # sub-v_3 receivers via ``dm_media_v3`` — see that module's
        # docstring for the §319 paragraph-5 ``fallback`` policy.
        # Included unconditionally on outbound so the canonical wire
        # shape is what v_3+ peers see; the per-peer rewrite happens
        # downstream inside ``_fan_to_remote``.
        if is_media:
            payload["file_name"] = file_name
            payload["mime_type"] = mime_type
            payload["file_size_bytes"] = file_size_bytes
            # ``media_blob_id`` = message id. Same identifier on the
            # ``DM_MESSAGE`` envelope and the follow-up
            # ``DM_MEDIA_BLOB`` event so the receiver can correlate
            # the preview with the eventual full bytes.
            payload["media_blob_id"] = msg.id
            # Build the inline preview if the media-sync service is
            # wired (images get a base64 WebP thumbnail; video / file
            # return ``None`` and the receiver renders a placeholder
            # until ``DM_MEDIA_BLOB`` arrives).
            if self._media_sync is not None and media_url is not None:
                preview_b64 = await self._media_sync.build_preview(
                    media_url=media_url,
                    kind=type,
                    mime_type=mime_type,
                )
                if preview_b64 is not None:
                    payload["preview_bytes_b64"] = preview_b64
        if seq is not None:
            payload["sender_seq"] = seq
        await self._fan_to_remote(
            conversation_id=conversation_id,
            event_type=FederationEventType.DM_MESSAGE,
            payload=payload,
            sender_user_id=sender.user_id,
        )
        # Enqueue the dm_media_outbox rows AFTER the DM_MESSAGE
        # envelope has been dispatched — the receiver needs to see
        # the preview / metadata first so the bubble renders before
        # the full bytes land. ``enqueue_for_message`` deduplicates
        # by ``(blob_id, target_instance_id)`` so re-sends from the
        # edit path don't double-up.
        if is_media and self._media_sync is not None and media_url is not None:
            remote_instance_ids: list[str] = []
            try:
                remote_members = await self._convos.list_remote_members(
                    conversation_id,
                )
            except Exception:  # pragma: no cover
                remote_members = []
            seen: set[str] = set()
            for rm in remote_members:
                inst = getattr(rm, "instance_id", None)
                if not inst or inst == self._own_instance_id or inst in seen:
                    continue
                seen.add(inst)
                if not await self._peer_is_confirmed(inst):
                    continue
                remote_instance_ids.append(inst)
            if remote_instance_ids:
                await self._media_sync.enqueue_for_message(
                    message_id=msg.id,
                    media_url=media_url,
                    target_instance_ids=remote_instance_ids,
                )
        # Voice notes: run sender-side STT in the background. The audio
        # bubble is already persisted + delivered with empty ``content``;
        # when the transcript lands we patch the row and fire
        # ``DmMessageUpdated`` so every open thread tab swaps the
        # "Transcribing…" placeholder for the text. Skips silently if
        # no STT capability is configured or the media bytes can't be
        # read — the receiver's own ``AudioTranscriptScheduler`` may
        # still fill the gap locally.
        if (
            type == "audio"
            and media_url
            and self._audio_transcription is not None
            and self._media_dir is not None
        ):
            self._spawn_transcribe_task(
                message_id=msg.id,
                conversation_id=conversation_id,
                sender_user_id=sender.user_id,
                sender_display_name=sender.display_name,
                media_url=media_url,
                recipient_user_ids=tuple(recipients),
            )
        return msg

    # ── Voice-note transcript ──────────────────────────────────────────

    def _spawn_transcribe_task(
        self,
        *,
        message_id: str,
        conversation_id: str,
        sender_user_id: str,
        sender_display_name: str,
        media_url: str,
        recipient_user_ids: tuple[str, ...],
    ) -> None:
        """Schedule the fire-and-forget sender-side STT pass.

        Keeps a strong ref to the spawned task on ``self`` so the loop
        doesn't GC it mid-flight, and removes the ref when it
        completes.
        """
        task = asyncio.create_task(
            self._transcribe_and_patch(
                message_id=message_id,
                conversation_id=conversation_id,
                sender_user_id=sender_user_id,
                sender_display_name=sender_display_name,
                media_url=media_url,
                recipient_user_ids=recipient_user_ids,
            ),
        )
        self._pending_transcribe_tasks.add(task)
        task.add_done_callback(self._pending_transcribe_tasks.discard)

    async def _transcribe_and_patch(
        self,
        *,
        message_id: str,
        conversation_id: str,
        sender_user_id: str,
        sender_display_name: str,
        media_url: str,
        recipient_user_ids: tuple[str, ...],
    ) -> None:
        """Run STT against the local blob and, on success, patch the row."""
        if self._audio_transcription is None or self._media_dir is None:
            return
        # ``media_url`` is the canonical ``api/media/<filename>`` shape;
        # the basename is the on-disk filename.
        try:
            filename = media_url.rsplit("/", 1)[-1]
            audio_bytes = await asyncio.get_running_loop().run_in_executor(
                None, (self._media_dir / filename).read_bytes
            )
        except Exception as exc:
            log.warning(
                "dm audio: cannot read blob %s for transcription: %s",
                media_url,
                exc,
            )
            return

        transcript = await self._audio_transcription.transcribe(audio_bytes)
        if transcript is None:
            return

        try:
            await self._convos.edit_message(message_id, transcript)
        except Exception as exc:
            log.warning("dm audio: failed to persist transcript: %s", exc)
            return

        edited_at = datetime.now(timezone.utc)
        await self._bus.publish(
            DmMessageUpdated(
                conversation_id=conversation_id,
                message_id=message_id,
                sender_user_id=sender_user_id,
                recipient_user_ids=recipient_user_ids,
                content=transcript,
                edited_at=edited_at,
            )
        )
        # Re-fan DM_MESSAGE so remote peers' rows pick up the
        # transcript via the existing upsert path. The receiver's
        # inbound handler detects "row already existed" and publishes
        # ``DmMessageUpdated`` instead of ``DmMessageCreated``, so
        # remote SPA tabs render the same in-place patch.
        await self._fan_to_remote(
            conversation_id=conversation_id,
            event_type=FederationEventType.DM_MESSAGE,
            payload={
                "conversation_id": conversation_id,
                "message_id": message_id,
                "sender_user_id": sender_user_id,
                "sender_display_name": sender_display_name,
                "type": "audio",
                "content": transcript,
                "media_url": media_url,
                "occurred_at": edited_at.isoformat(),
                "edited_at": edited_at.isoformat(),
            },
            sender_user_id=sender_user_id,
        )

    async def edit_message(
        self,
        message_id: str,
        *,
        editor_username: str,
        new_content: str,
        conversation_id: str | None = None,
    ) -> datetime:
        """Edit the sender's own message; returns the new ``edited_at``.

        ``conversation_id`` (the route's path) must match the message's
        conversation — ``KeyError`` (404) otherwise. A deleted message, a
        voice note / transcript / call entry, or empty content is a
        ``ValueError`` (422); anyone but the sender is refused (403).
        """
        msg = await self._require_message(message_id, conversation_id)
        await self._require_system_write(msg.conversation_id, editor_username)
        editor = await self._require_user(editor_username)
        if msg.sender_user_id != editor.user_id:
            raise PermissionError("only the sender can edit a message")
        if msg.deleted:
            raise ValueError("a deleted message can't be edited")
        if msg.type not in EDITABLE_TYPES:
            raise ValueError(f"{msg.type!r} messages can't be edited")
        if not new_content:
            raise ValueError("content must not be empty")
        if len(new_content) > MAX_DM_LENGTH:
            raise DmTooLongError(MAX_DM_LENGTH)
        if msg.type == "location":
            new_content = normalise_location_content(new_content)
        await self._convos.edit_message(message_id, new_content)
        edited_at = datetime.now(timezone.utc)
        # Every open thread tab swaps the bubble in place; the members an
        # edit newly @-mentions (never those already mentioned) get a
        # mention bell (NotificationService).
        recipients: list[str] = []
        for m in await self._convos.list_members(msg.conversation_id):
            if m.username == editor_username:
                continue
            u = await self._users.get(m.username)
            if u is not None:
                recipients.append(u.user_id)
        await self._bus.publish(
            DmMessageUpdated(
                conversation_id=msg.conversation_id,
                message_id=msg.id,
                sender_user_id=msg.sender_user_id,
                recipient_user_ids=tuple(recipients),
                content=new_content,
                edited_at=edited_at,
                new_mentions=(
                    await self._mentions().added(
                        msg.conversation_id, msg.content, new_content
                    )
                    if msg.type in MENTIONABLE_TYPES
                    else ()
                ),
                sender_display_name=editor.display_name,
            )
        )
        # Receiver upserts on message_id (save_message ON CONFLICT UPDATE),
        # so a re-send of DM_MESSAGE with updated content + edited_at is
        # all the peer needs to reflect the edit.
        await self._fan_to_remote(
            conversation_id=msg.conversation_id,
            event_type=FederationEventType.DM_MESSAGE,
            payload={
                "conversation_id": msg.conversation_id,
                "message_id": msg.id,
                "sender_user_id": msg.sender_user_id,
                "sender_display_name": editor.display_name,
                "type": msg.type,
                "content": new_content,
                "media_url": msg.media_url,
                "reply_to_id": msg.reply_to_id,
                "occurred_at": msg.created_at.isoformat(),
                "edited_at": edited_at.isoformat(),
            },
            sender_user_id=msg.sender_user_id,
        )
        return edited_at

    async def delete_message(
        self,
        message_id: str,
        *,
        actor_username: str,
        conversation_id: str | None = None,
    ) -> None:
        msg = await self._require_message(message_id, conversation_id)
        await self._require_system_write(msg.conversation_id, actor_username)
        actor = await self._require_user(actor_username)
        if msg.sender_user_id != actor.user_id:
            raise PermissionError("only the sender can delete a message")
        await self._convos.soft_delete_message(message_id)
        # The row's media is cleared; drop the backing file too unless
        # another row still references it. Best effort: a missing file
        # never blocks the delete.
        await unlink_unreferenced(self._media_dir, self._media_refs, [msg.media_url])
        await self._fan_to_remote(
            conversation_id=msg.conversation_id,
            event_type=FederationEventType.DM_MESSAGE_DELETED,
            payload={
                "conversation_id": msg.conversation_id,
                "message_id": msg.id,
            },
            sender_user_id=msg.sender_user_id,
        )

    async def list_messages(
        self,
        conversation_id: str,
        *,
        reader_username: str,
        before: str | None = None,
        limit: int = 50,
    ) -> list[ConversationMessage]:
        await self._require_membership(conversation_id, reader_username)
        limit = max(1, min(int(limit), 100))
        messages = await self._convos.list_messages(
            conversation_id,
            before=before,
            limit=limit,
        )
        reader = await self._users.get(reader_username)
        blocked = (
            await self._guardian_block_counterparts(reader.user_id)
            if reader is not None
            else frozenset()
        )
        if not blocked:
            return messages
        # §CP.F2: a group can still carry a blocked person's messages for the
        # others — never for the reader a guardian block separates them from.
        return [m for m in messages if m.sender_user_id not in blocked]

    # ── Read tracking ──────────────────────────────────────────────────

    async def mark_read(
        self,
        conversation_id: str,
        *,
        username: str,
    ) -> int:
        """Mark every message in the conversation read for ``username``.

        Updates the watermark (`set_last_read`) for unread counts AND
        bulk-upserts ``conversation_delivery_state`` rows so other
        participants see read-receipt ticks. Returns the number of
        messages that flipped to ``read``.
        """
        await self._require_membership(conversation_id, username)
        # Pass Python-format ISO timestamp so it compares correctly with
        # message created_at (also Python ISO). SQLite's datetime('now')
        # omits the 'T' and timezone suffix, causing string-comparison
        # mismatches against Python isoformat() values.
        now = datetime.now(timezone.utc).isoformat()
        await self._convos.set_last_read(conversation_id, username, at=now)
        user = await self._require_user(username)
        return await self._convos.mark_conversation_read(
            conversation_id=conversation_id,
            user_id=user.user_id,
            up_to_at=now,
        )

    async def mark_delivered(
        self,
        conversation_id: str,
        *,
        message_id: str,
        username: str,
    ) -> None:
        """Mark ``message_id`` as delivered to ``username``.

        Called when the client receives a message via WebSocket or the
        GET /messages endpoint. ``read`` already supersedes
        ``delivered`` (handled in the repo), so calling after mark_read
        is a no-op.
        """
        await self._require_membership(conversation_id, username)
        user = await self._require_user(username)
        await self._convos.upsert_delivery_state(
            conversation_id=conversation_id,
            message_id=message_id,
            user_id=user.user_id,
            state="delivered",
        )

    async def list_delivery_states(
        self,
        conversation_id: str,
        *,
        username: str,
        message_ids: list[str] | None = None,
    ) -> list[dict]:
        await self._require_membership(conversation_id, username)
        return await self._convos.list_delivery_states(
            conversation_id,
            message_ids=message_ids,
        )

    async def list_open_gaps(
        self,
        conversation_id: str,
        *,
        username: str,
    ) -> list[dict]:
        """§12.5 — sequence holes detected for this conversation.

        Members-only. Returns ``[]`` when the routing repo wasn't wired
        or nothing is flagged.
        """
        await self._require_membership(conversation_id, username)
        if self._dm_routing_repo is None:
            return []
        return await self._dm_routing_repo.list_open_gaps(conversation_id)

    async def count_unread(
        self,
        conversation_id: str,
        *,
        username: str,
    ) -> int:
        await self._require_membership(conversation_id, username)
        return await self._convos.count_unread(conversation_id, username)

    # ── Mute ───────────────────────────────────────────────────────────

    async def mute(
        self,
        conversation_id: str,
        *,
        username: str,
        duration: str,
        now: datetime | None = None,
    ) -> str:
        """Mute the conversation for ``username`` only; returns ``muted_until``.

        ``duration`` is a :data:`MUTE_DURATIONS` key (``ValueError`` → 422
        otherwise). Members only. A muted conversation still delivers and
        still counts unread — :class:`NotificationService` just skips the
        bell row and the push. Local only: nothing is federated.
        """
        await self._require_membership(conversation_id, username)
        until = mute_until_for(duration, now=now or datetime.now(timezone.utc))
        await self._convos.set_muted_until(conversation_id, username, until)
        return until

    async def set_notif_level(
        self,
        conversation_id: str,
        *,
        username: str,
        level: str,
    ) -> str:
        """Set ``username``'s own notification level for a group chat.

        ``level`` is ``"all"`` (every message rings) or ``"mentions"``
        (only messages that @-mention them). Group conversations only — a
        1:1 has one other person, so every message is for the member.
        ``ValueError`` (→ 422) for another level or a 1:1; members only.
        An active mute still wins. Local only: never federated.
        """
        if level not in CONVERSATION_NOTIF_LEVELS:
            raise ValueError(
                f"level must be one of {', '.join(sorted(CONVERSATION_NOTIF_LEVELS))}"
            )
        conv = await self._require_conversation(conversation_id)
        await self._require_membership(conversation_id, username)
        if conv.type is not ConversationType.GROUP_DM:
            raise ValueError("notification levels apply to group conversations")
        await self._convos.set_notif_level(conversation_id, username, level)
        return level

    def _mentions(self) -> DmMentionResolver:
        """Mention resolver over this household's seats of a conversation."""
        return DmMentionResolver(self._convos, self._users)

    async def mention_tokens(self, conversation_id: str) -> dict[str, str | None]:
        """user_id → the @-token (without ``@``) a composer inserts to
        mention that member (``GET .../members`` ``mention``)."""
        return await self._mentions().tokens(conversation_id)

    async def unmute(self, conversation_id: str, *, username: str) -> None:
        """Clear ``username``'s own mute. Members only; a no-op when unmuted."""
        await self._require_membership(conversation_id, username)
        await self._convos.set_muted_until(conversation_id, username, None)

    # ── Reactions ──────────────────────────────────────────────────────

    async def add_reaction(
        self,
        message_id: str,
        *,
        username: str,
        emoji: str,
    ) -> None:
        msg = await self._require_message(message_id)
        await self._require_membership(msg.conversation_id, username, write=True)
        actor = await self._require_user(username)
        # §CP.F2: a reaction is a message too — same rule as a send, and
        # never onto a message the reader isn't shown.
        await self._guard_guardian_blocks(
            await self._require_conversation(msg.conversation_id), actor.user_id
        )
        if msg.sender_user_id in await self._guardian_block_counterparts(actor.user_id):
            raise RecipientBlockedError(
                await self._guardian_block_detail(actor.user_id)
            )
        clean = emoji.strip()
        if not clean:
            raise ValueError("emoji must not be empty")
        if len(clean) > 32:
            raise ValueError("emoji glyph too long")
        await self._convos.add_reaction(message_id, actor.user_id, clean)
        await self._publish_reaction(msg, actor.user_id, clean, "add")
        await self._fan_to_remote(
            conversation_id=msg.conversation_id,
            event_type=FederationEventType.DM_MESSAGE_REACTION,
            payload={
                "conversation_id": msg.conversation_id,
                "message_id": msg.id,
                "user_id": actor.user_id,
                "emoji": clean,
                "action": "add",
            },
            sender_user_id=actor.user_id,
        )

    async def remove_reaction(
        self,
        message_id: str,
        *,
        username: str,
        emoji: str,
    ) -> None:
        msg = await self._require_message(message_id)
        await self._require_membership(msg.conversation_id, username, write=True)
        actor = await self._require_user(username)
        clean = emoji.strip()
        if not clean:
            raise ValueError("emoji must not be empty")
        await self._convos.remove_reaction(message_id, actor.user_id, clean)
        await self._publish_reaction(msg, actor.user_id, clean, "remove")
        await self._fan_to_remote(
            conversation_id=msg.conversation_id,
            event_type=FederationEventType.DM_MESSAGE_REACTION,
            payload={
                "conversation_id": msg.conversation_id,
                "message_id": msg.id,
                "user_id": actor.user_id,
                "emoji": clean,
                "action": "remove",
            },
            sender_user_id=actor.user_id,
        )

    async def list_reactions(
        self,
        message_id: str,
        *,
        username: str,
    ) -> list:
        msg = await self._require_message(message_id)
        await self._require_membership(msg.conversation_id, username)
        return await self._convos.list_reactions(message_id)

    async def _publish_reaction(
        self,
        msg: ConversationMessage,
        actor_user_id: str,
        emoji: str,
        action: str,
    ) -> None:
        """Fan a ``DmMessageReactionChanged`` event to every local
        member's open WS sessions. The reacting user IS included in
        the recipient list so their other open tabs (mobile + desktop)
        update the reaction strip in lockstep.
        """
        members = await self._convos.list_members(msg.conversation_id)
        # §CP.F2: never to someone a guardian block separates from the actor.
        withheld = await self._guardian_block_counterparts(actor_user_id)
        recipient_ids: list[str] = []
        for m in members:
            u = await self._users.get(m.username)
            if u is not None and u.user_id not in withheld:
                recipient_ids.append(u.user_id)
        await self._bus.publish(
            DmMessageReactionChanged(
                conversation_id=msg.conversation_id,
                message_id=msg.id,
                user_id=actor_user_id,
                emoji=emoji,
                action=action,
                recipient_user_ids=tuple(recipient_ids),
            )
        )

    # ── Leave ──────────────────────────────────────────────────────────

    async def leave(
        self,
        conversation_id: str,
        *,
        username: str,
    ) -> None:
        """Soft-leave: sets ``deleted_at`` on the member row.

        Applies to both 1:1 DMs and group DMs (§23.47c). The user can be
        re-invited; their messages stay visible to remaining members. A
        background sweeper hard-deletes the conversation once every member
        has left.
        """
        conv = await self._require_dm_conversation(conversation_id)
        await self._require_membership(conversation_id, username)
        if conv.type is not ConversationType.GROUP_DM:
            await self._convos.soft_leave(conversation_id, username)
            return
        # Group: the member list is the authority's. Here, leaving is a new
        # snapshot without the user; elsewhere the user steps out locally
        # and the authority is told, answering with the new roster.
        if self._groups.is_authority_here(conversation_id):
            local = [
                u
                for u in await self._active_local_usernames(conversation_id)
                if u != username
            ]
            await self._commit_group(
                conv,
                local_usernames=local,
                remote_members=await self._convos.list_remote_members(conversation_id),
                name=conv.name,
            )
            return
        user = await self._require_user(username)
        # Remember the version we held: a roster the authority built before
        # it saw this leave can't seat the user again (only a later re-add).
        await self._convos.soft_leave(
            conversation_id, username, left_version=conv.membership_version
        )
        await self._groups.send_leave(conversation_id, user.user_id)

    # ── Internal helpers ───────────────────────────────────────────────

    async def _require_user(self, username: str):
        user = await self._users.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        return user

    async def _guard_block_pair(self, sender_id: str, recipient_id: str) -> None:
        """Reject a 1:1 DM action when either side has blocked the other.

        Symmetric — the sender can't reach a user who blocked them, and
        the sender can't message a user they themselves blocked. Raise
        :class:`RecipientBlockedError` so :class:`BaseView._iter` maps
        it to 403.
        """
        if await self._guardian_blocked(sender_id, recipient_id):
            raise RecipientBlockedError(await self._guardian_block_detail(sender_id))
        if await self._users.is_blocked(recipient_id, sender_id):
            raise RecipientBlockedError(RECIPIENT_BLOCKED_DETAIL)
        if await self._users.is_blocked(sender_id, recipient_id):
            raise RecipientBlockedError(YOU_BLOCKED_DETAIL)

    async def _seat_user_ids(self, conversation_id: str) -> list[str]:
        """``user_id`` of every active seat — local members and remote seats."""
        ids: list[str] = []
        for m in await self._convos.list_members(conversation_id):
            if m.deleted_at is not None:
                continue
            u = await self._users.get(m.username)
            if u is not None:
                ids.append(u.user_id)
        for seat in await self._convos.list_remote_members(conversation_id):
            if seat.user_id is not None:
                ids.append(seat.user_id)
                continue
            ru = await self._users.get_remote_by_member(
                seat.instance_id, seat.remote_username
            )
            if ru is not None:
                ids.append(ru.user_id)
        return ids

    async def _guard_guardian_blocks(
        self, conv: Conversation, sender_id: str
    ) -> frozenset[str]:
        """§CP.F2 on a send: raise when the sender may not post here, else
        return the seats this message is withheld from.

        A 1:1 with a counterpart is refused both ways. In a group the
        protected account is refused while a counterpart is seated; anyone
        else posts, and the protected accounts that blocked them don't get it.
        """
        blocked = await self._guardian_block_counterparts(sender_id)
        if not blocked:
            return frozenset()
        seated = blocked & set(await self._seat_user_ids(conv.id))
        if not seated:
            return frozenset()
        if conv.is_system:
            # A system chat (household / space) is not a group anyone chose
            # to share: being in the household or the space is the consent,
            # so nobody is refused for who else is in it. The protected
            # account is just never shown — or rung by — the blocked
            # person's messages (``withheld`` here, ``list_messages`` on
            # read).
            return frozenset(seated)
        if conv.type is ConversationType.DM:
            raise RecipientBlockedError(await self._guardian_block_detail(sender_id))
        if await self._is_protected(sender_id):
            raise RecipientBlockedError(GUARDIAN_BLOCK_DETAIL)
        return frozenset(seated)

    async def _guardian_block_detail(self, sender_id: str) -> str:
        """The refusal *sender_id* sees: the protected account learns it
        can't message the person; the blocked person sees a personal block."""
        if await self._is_protected(sender_id):
            return GUARDIAN_BLOCK_DETAIL
        return RECIPIENT_BLOCKED_DETAIL

    async def _guard_group_roster(
        self,
        local_usernames: list[str],
        remote_seats: list[RemoteConversationMember],
    ) -> None:
        """§CP.F2: nobody a guardian block separates may share a group."""
        ids: list[str] = []
        for uname in local_usernames:
            u = await self._users.get(uname)
            if u is not None:
                ids.append(u.user_id)
        ids.extend(s.user_id for s in remote_seats if s.user_id)
        roster = set(ids)
        for uid in ids:
            if await self._guardian_block_counterparts(uid) & roster:
                raise RecipientBlockedError(GUARDIAN_BLOCK_GROUP_DETAIL)

    async def on_guardian_block(self, minor_user_id: str, blocked_user_id: str) -> None:
        """§CP.F2: the protected account steps out of every group it shares
        with the person just blocked (the 1:1 closes through the block)."""
        minor = await self._users.get_by_user_id(minor_user_id)
        if minor is None or not await self._is_protected(minor_user_id):
            return
        failed: list[str] = []
        for conv in await self._convos.list_for_user(minor.username):
            if conv.type is not ConversationType.GROUP_DM:
                continue
            if blocked_user_id not in await self._seat_user_ids(conv.id):
                continue
            try:
                await self.leave(conv.id, username=minor.username)
            except Exception as exc:
                # Keep going; the account still can't post there and never
                # sees the blocked person's messages (read-side filter).
                log.warning("§CP.F2 leave of group %s failed: %s", conv.id, exc)
                failed.append(conv.id)
        if failed:
            raise RuntimeError(f"could not leave {len(failed)} group(s)")

    async def _require_conversation(
        self,
        conversation_id: str,
    ) -> Conversation:
        conv = await self._convos.get(conversation_id)
        if conv is None:
            raise KeyError(f"conversation {conversation_id!r} not found")
        return conv

    async def _require_dm_conversation(self, conversation_id: str) -> Conversation:
        """A person-made DM or group — a system chat reads as not found
        (404): it has no members to manage and nobody can leave it."""
        conv = await self._require_conversation(conversation_id)
        if conv.is_system:
            raise KeyError(f"conversation {conversation_id!r} not found")
        return conv

    async def _require_membership(
        self,
        conversation_id: str,
        username: str,
        *,
        write: bool = False,
    ) -> ConversationMember:
        """The caller's active seat, or :class:`PermissionError`.

        A system chat asks :class:`SystemChatPolicy` instead of the seat
        rows (``write`` = posting, reacting, editing, deleting); a caller it
        allows who has no seat yet is seated on the spot, so per-user state
        (read watermark, mute, level) always has a row to live in.
        """
        conv = await self._convos.get(conversation_id)
        if conv is not None and conv.is_system:
            return await self._require_system_seat(conv, username, write=write)
        members = await self._convos.list_members(conversation_id)
        for m in members:
            if m.username == username and m.deleted_at is None:
                return m
        raise PermissionError(f"user {username!r} is not a member of this conversation")

    async def require_member(self, conversation_id: str, username: str) -> None:
        """Public read check for routes that read a conversation's seats
        directly (roster, notification level)."""
        await self._require_membership(conversation_id, username)

    async def _require_system_seat(
        self,
        conv: Conversation,
        username: str,
        *,
        write: bool,
    ) -> ConversationMember:
        if self._system_chats is None:
            raise PermissionError("system chats are not available")
        user = await self._users.get(username)
        if user is None:
            raise PermissionError(f"user {username!r} is not a member of this chat")
        await self._system_chats.require(conv, user.user_id, write=write)
        for _ in range(2):
            for m in await self._convos.list_members(conv.id):
                if m.username == username and m.deleted_at is None:
                    return m
            await self._convos.upsert_seat(
                conv.id,
                username,
                notif_level=self._system_chats.default_notif_level(conv),
            )
        raise PermissionError(  # pragma: no cover - the upsert seats them
            f"user {username!r} could not be seated"
        )

    async def _require_system_write(self, conversation_id: str, username: str) -> None:
        """Edits and deletes check only the sender for DMs; in a system chat
        the author must still be allowed to post there (chat on, still a
        member)."""
        conv = await self._convos.get(conversation_id)
        if conv is not None and conv.is_system:
            await self._require_system_seat(conv, username, write=True)

    async def _require_message(
        self,
        message_id: str,
        conversation_id: str | None = None,
    ) -> ConversationMessage:
        msg = await self._convos.get_message(message_id)
        if msg is None or (
            conversation_id is not None and msg.conversation_id != conversation_id
        ):
            raise KeyError(f"message {message_id!r} not found")
        return msg

    # ── Federation fan-out ─────────────────────────────────────────────

    async def _fan_to_remote(
        self,
        *,
        conversation_id: str,
        event_type: FederationEventType,
        payload: dict,
        sender_user_id: str | None = None,
    ) -> None:
        """Send ``event_type`` to every remote instance with a member in
        ``conversation_id``.

        Direct paired peers receive the event via
        :meth:`FederationService.send_event`. Peers we only know
        transitively (no :class:`RemoteInstance` row) are skipped — the
        browser/client drives multi-hop relay (§12.5) where the content
        is E2E-encrypted before leaving the device, and the DM history
        sync picks up anything the peer missed when they reconnect.

        When ``sender_user_id`` is given and ``self._visibility_repo`` is
        wired, peers where the sender is hidden are silently skipped —
        forward-only semantics: the local conversation row is still
        created, only the outbound envelope is suppressed.
        """
        if self._federation is None:
            return
        conv = await self._convos.get(conversation_id)
        if conv is not None and conv.is_system:
            # System chats never travel as DM events: the household chat
            # stays on this household, a space chat has its own events.
            log.debug("%s for system chat %s not federated", event_type.value, conv.id)
            return
        try:
            remote_members = await self._convos.list_remote_members(
                conversation_id,
            )
        except Exception:  # pragma: no cover
            return
        is_group = conv is not None and conv.type is ConversationType.GROUP_DM
        seen: set[str] = set()
        for rm in remote_members:
            inst = getattr(rm, "instance_id", None)
            if not inst or inst == self._own_instance_id or inst in seen:
                continue
            seen.add(inst)
            # A group member household we never paired with is still a
            # member: reach it over the mesh, E2E-sealed to it and
            # origin-signed (``SPACE_ROUTED``) so the relays in between —
            # never members — see only ciphertext. A 1:1 keeps the
            # direct-pairing-only rule.
            via_mesh = False
            if not await self._peer_is_confirmed(inst):
                if not is_group:
                    log.debug(
                        "dm fan-out skipped: peer %s not confirmed",
                        inst,
                    )
                    continue
                if await self._mesh_refused(inst):
                    log.info(
                        "group %s: %s not sent to member household %s — we are "
                        "unpairing from it, or only share a space with it",
                        conversation_id,
                        event_type.value,
                        inst,
                    )
                    continue
                via_mesh = True
            # Per-pair user-visibility filter (peer_user_visibility).
            # When the sender is hidden from this peer, drop the envelope
            # silently — the sender's local conversation row still records
            # the message (forward-only semantics).
            if sender_user_id is not None:
                hidden = await self.hidden_for_peer(inst)
                if sender_user_id in hidden:
                    log.debug(
                        "dm fan-out suppressed: sender %s hidden from %s",
                        sender_user_id,
                        inst,
                    )
                    continue
            # Per-peer compat shim. The canonical ``payload`` above is
            # the newest (``OURS``) wire shape; for each peer we ask the
            # registered transforms whether they need to rewrite (e.g.
            # strip v_3 media fields for a v_2 peer and substitute a
            # text fallback). Returning ``None`` drops the send — no
            # current shim does, but the protocol supports the
            # force-upgrade policy.
            peer_payload = await self._compat_transform_payload(
                event_type=event_type,
                payload=payload,
                peer_instance_id=inst,
            )
            if peer_payload is None:
                continue
            try:
                if via_mesh:
                    result = await self._federation.send_with_mesh_fallback(
                        to_instance_id=inst,
                        event_type=event_type,
                        payload=peer_payload,
                    )
                    if not result.ok:
                        log.warning(
                            "group %s: %s to member household %s not "
                            "delivered over the mesh (%s)",
                            conversation_id,
                            event_type.value,
                            inst,
                            result.error,
                        )
                    continue
                await self._federation.send_event(
                    to_instance_id=inst,
                    event_type=event_type,
                    payload=peer_payload,
                )
            except Exception as exc:  # pragma: no cover
                log.debug(
                    "dm fan-out failed to %s (%s): %s",
                    inst,
                    event_type.value,
                    exc,
                )

    async def _compat_transform_payload(
        self,
        *,
        event_type: FederationEventType,
        payload: dict,
        peer_instance_id: str,
    ) -> dict | None:
        """Run the per-peer compat transforms (see ``federation/compat/``).

        Reads the peer's advertised ``proto_version`` off
        ``remote_instances`` and hands the payload to
        :func:`socialhome.federation.compat.transform_for_peer`. Falls
        open (returns the payload unchanged) if the proto_version
        lookup fails — a missing peer_version reads as "we don't know,
        assume current" which mirrors what would happen on first
        contact before the capabilities exchange.
        """
        peer_version = 1
        if self._federation_repo is not None:
            try:
                instance = await self._federation_repo.get_instance(
                    peer_instance_id,
                )
            except Exception:  # pragma: no cover
                instance = None
            if instance is not None:
                peer_version = int(
                    getattr(instance, "proto_version", None) or 1,
                )
        return compat.transform_for_peer(
            event_type=event_type,
            payload=payload,
            peer_version=peer_version,
        )

    async def _reject_media_on_relay_only_conversation(
        self,
        conversation_id: str,
    ) -> None:
        """Refuse media when any remote participant requires DM_RELAY.

        A conversation may carry remote members from one or more
        peers. Each peer is either:

        * **Directly paired (CONFIRMED)** — media flows through the
          regular ``DM_MESSAGE`` envelope (v_3) plus the follow-up
          ``DM_MEDIA_BLOB`` for full bytes.
        * **Transitive only** — reachable only via a relay path
          through one or more confirmed intermediaries (the bazaar
          contact-flow that lets households reach each other via a
          mutual friend without scanning a QR code). Media is *not*
          allowed here: relays are explicitly the lower-trust path
          and shouldn't shuttle picture/video/file bytes through
          third-party households.

        If any remote member's instance fails the confirmed-peer
        check, the whole send is rejected. The SPA disables the
        attach button when the conversation is relay-only — this is
        the server-side belt-and-braces guard that catches a
        bypass attempt.
        """
        # No remote members → same-household conversation; nothing
        # to gate.
        remote_members = await self._convos.list_remote_members(conversation_id)
        if not remote_members:
            return
        seen: set[str] = set()
        for rm in remote_members:
            inst = rm.instance_id
            if inst in seen:
                continue
            seen.add(inst)
            if not await self._peer_is_confirmed(inst):
                raise MediaRequiresDirectPairingError(
                    "Media attachments are only allowed with directly-paired "
                    "households. This conversation includes participants "
                    "reachable only via a relay.",
                )

    async def _mesh_refused(self, instance_id: str) -> bool:
        """A group member household we must not reach over the mesh either.

        One we are unpairing from (the admin asked to stop talking to it),
        or one we share only a space with (an invite-link row: no social
        relationship, and its "direct" channel is the connection-server
        relay).
        """
        if self._federation_repo is None:
            return False
        instance = await self._federation_repo.get_instance(instance_id)
        if instance is None:
            return False
        status = getattr(instance, "status", None)
        source = getattr(instance, "source", None)
        return (
            getattr(status, "value", status) == "unpairing"
            or getattr(source, "value", source) == "space_session"
        )

    async def _peer_is_confirmed(self, instance_id: str) -> bool:
        """Is ``instance_id`` a directly-paired CONFIRMED peer?

        Unconfirmed / transitive-only peers take the multi-hop relay
        path, which is browser-driven in v1 (§12.5).
        """
        if self._federation_repo is None:
            # Without a federation_repo we can't tell — be permissive so
            # tests that only wire the federation service keep working.
            return True
        try:
            instance = await self._federation_repo.get_instance(instance_id)
        except Exception:  # pragma: no cover
            return False
        if instance is None:
            return False
        status = getattr(instance, "status", None)
        if status is None:
            return False
        # §D2b: a ``space_session`` row is a household we share a space
        # with via an invite link — CONFIRMED, but never a DM peer. DMs
        # are a social relationship nobody consented to by joining a
        # space, so it is not "directly paired" for this purpose.
        source = getattr(instance, "source", None)
        if getattr(source, "value", str(source)) == "space_session":
            return False
        return getattr(status, "value", str(status)) == "confirmed"
