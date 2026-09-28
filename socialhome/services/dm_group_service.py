"""Group conversations that span households — the membership authority (§23.47).

A group conversation is created on one household, its **authority**. The
conversation id is an owner-bound id (``federation/owner_bound_id.py``,
kind ``group-conversation``) committing to that household's
``instance_id``, so every member household can tell from the id alone
whose word counts for the member list — nothing is stored beside it.

* **Only the authority changes the member list.** Every change (create,
  add, remove, rename, a member leaving) is applied here as a new
  snapshot with the next ``membership_version`` and shipped as
  :data:`FederationEventType.DM_GROUP_ROSTER` — the whole member list — to
  every member household, and once more to a household the change took
  out, so it drops the conversation too. The envelope is signed by the
  authority household like any other (§24.11); no new key.
* **Receivers accept a roster only from the household the id commits to**,
  only when its version is newer than the one they hold, and only then
  seat anybody. A group message never seats anyone (#734's invariant
  holds), and a roster naming a person this household knows as homed
  elsewhere does not seat them.
* **Leaving is the one change a member household starts:** it takes its
  own user out locally and tells the authority
  (:data:`FederationEventType.DM_GROUP_LEAVE`); the authority accepts it
  only for a user seated on the sending household, and answers with the
  new roster.
* A household below
  :data:`FederationCapability.MIN_FOR_CROSS_HOUSEHOLD_GROUP_DM` cannot
  parse either event, so its people are refused at add time
  (:class:`GroupMemberUnsupportedError`) and it is never sent a roster.

Messages, reactions, typing and calls are not routed here — the sending
household fans those out to every member household itself
(:class:`~socialhome.services.dm_service.DmService`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..domain.conversation import (
    Conversation,
    ConversationType,
    GroupRosterChange,
    GroupRosterMember,
    RemoteConversationMember,
)
from ..domain.events import DmConversationCreated, DmGroupRosterChanged
from ..domain.federation import FederationEventType, InstanceSource, PairingStatus
from ..domain.federation_capabilities import FederationCapability
from ..federation.dm_scope import DM_HOLD_SCOPE, DmScope, refuse
from ..federation.owner_bound_id import (
    GROUP_CONVERSATION_KIND,
    OwnerBinding,
    check_owner_bound_id,
    is_owner_bound,
    mint_owner_bound_id,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.user_repo import AbstractUserRepo

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from ..federation.federation_service import FederationService
    from ..federation.pending_seat_buffer import PendingSeatBuffer
    from ..federation.sync.dm_history import DmHistoryScheduler
    from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)

#: Most people one group conversation seats. Bounds the roster envelope and
#: the per-message fan-out; far above any household chat.
MAX_GROUP_MEMBERS: int = 32

#: Longest group name kept (the SPA caps its input at 80 too).
MAX_GROUP_NAME: int = 80

#: A roster version above this is refused — far beyond any real history,
#: and inside SQLite's signed 64-bit INTEGER.
MAX_MEMBERSHIP_VERSION: int = 2**53

#: How often the authority retries a member's leave that raced another
#: membership change (each retry re-reads the new version).
_LEAVE_RETRIES: int = 3


class GroupMemberUnsupportedError(ValueError):
    """A person can't be seated in a cross-household group.

    Their household is not a directly paired one, or its Social Home is
    too old to take part (below v_37). Mapped to HTTP 422
    ``GROUP_MEMBER_UNSUPPORTED`` with the message, so the SPA can say why.
    """


class GroupManagedElsewhereError(PermissionError):
    """The member list of this group is kept by another household.

    Only people on the authority household add, remove or rename; anyone
    can leave. Mapped to HTTP 403 with the message.
    """


class DmGroupService:
    """Membership authority for group conversations, and its receiver side."""

    __slots__ = (
        "_convos",
        "_users",
        "_bus",
        "_federation",
        "_federation_repo",
        "_own_instance_id",
        "_history",
        "_scope",
        "_pending",
    )

    def __init__(
        self,
        *,
        conversation_repo: AbstractConversationRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
        own_instance_id: str = "",
    ) -> None:
        self._convos = conversation_repo
        self._users = user_repo
        self._bus = bus
        self._federation: "FederationService | None" = None
        self._federation_repo: "AbstractFederationRepo | None" = None
        self._own_instance_id = own_instance_id
        self._history: "DmHistoryScheduler | None" = None
        self._scope = DmScope(conversation_repo=conversation_repo, user_repo=user_repo)
        self._pending: "PendingSeatBuffer | None" = None

    def attach_federation(
        self,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        own_instance_id: str,
    ) -> None:
        """Late-bind federation (built after the DM stack in ``create_app``)."""
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._own_instance_id = own_instance_id

    def attach_history(self, scheduler: "DmHistoryScheduler") -> None:
        """Wire the DM history scheduler so a newly seated household catches up."""
        self._history = scheduler

    def attach_pending(self, buffer: "PendingSeatBuffer") -> None:
        """Hold a roster naming an authority user this household hasn't synced.

        Released (and re-applied through the same rules) when that user's
        profile lands — the same buffer a DM from a not-yet-synced sender
        waits in.
        """
        self._pending = buffer

    def attach_to(self, federation_service: "FederationService") -> None:
        """Register the inbound roster / leave handlers."""
        registry = federation_service._event_registry
        registry.register(FederationEventType.DM_GROUP_ROSTER, self._on_roster)
        registry.register(FederationEventType.DM_GROUP_LEAVE, self._on_leave)

    # ── Authority ───────────────────────────────────────────────────────

    def mint_conversation_id(self) -> str:
        """A group id bound to this household as its authority."""
        if not self._own_instance_id:
            raise RuntimeError("group ids need this household's instance_id")
        return mint_owner_bound_id(
            GROUP_CONVERSATION_KIND,
            space_id="",
            owner_user_id=self._own_instance_id,
        )

    def is_authority_here(self, conversation_id: str) -> bool:
        """This household keeps the member list of ``conversation_id``.

        A legacy (pre-v_37, uuid4) group id binds nobody: such a group was
        created here, is local-only, and stays managed here.
        """
        if not is_owner_bound(conversation_id):
            return True
        return bool(self._own_instance_id) and _bound_to(
            conversation_id, self._own_instance_id
        )

    def require_authority_here(self, conversation_id: str) -> None:
        if not self.is_authority_here(conversation_id):
            raise GroupManagedElsewhereError(
                "This group is managed by the household that created it — "
                "ask someone there to add, remove or rename."
            )

    async def remote_seat_for(
        self,
        conversation_id: str,
        user_id: str,
    ) -> RemoteConversationMember:
        """The seat a remote person would get, or why they can't have one.

        The person must be known here (mirrored from a directly paired,
        social household) and that household must understand groups
        (v_37+). A legacy local-only group can't take remote people at all:
        its id binds no authority, so no other household would accept its
        roster.
        """
        if not is_owner_bound(conversation_id):
            raise GroupMemberUnsupportedError(
                "This group was created before groups could include other "
                "households — start a new group to add them."
            )
        remote = await self._users.get_remote(user_id)
        if remote is None:
            raise KeyError(f"user {user_id!r} not found")
        instance = (
            await self._federation_repo.get_instance(remote.instance_id)
            if self._federation_repo is not None
            else None
        )
        if (
            instance is None
            or self._federation is None
            or instance.status is not PairingStatus.CONFIRMED
            or instance.source is InstanceSource.SPACE_SESSION
        ):
            raise GroupMemberUnsupportedError(
                f"{remote.display_name} can only join once their household is "
                "paired with yours."
            )
        if not await self._federation.peer_supports(
            remote.instance_id,
            min_version=FederationCapability.MIN_FOR_CROSS_HOUSEHOLD_GROUP_DM,
        ):
            raise GroupMemberUnsupportedError(
                f"{remote.display_name}'s household needs a Social Home update "
                "before they can join group chats."
            )
        return RemoteConversationMember(
            conversation_id=conversation_id,
            instance_id=remote.instance_id,
            remote_username=remote.remote_username,
            joined_at=_now_iso(),
            user_id=remote.user_id,
            display_name=remote.display_name,
        )

    async def commit(
        self,
        conversation: Conversation,
        *,
        local_usernames: Sequence[str],
        remote_members: Sequence[RemoteConversationMember],
        creator_user_id: str | None = None,
    ) -> GroupRosterChange:
        """Apply the next member-list snapshot here and ship it.

        ``conversation.membership_version`` must be the next version. The
        snapshot goes to every member household, plus every household it
        took out of the group. Raises :class:`ValueError` when another
        change got there first (the version is already taken).
        """
        if len(set(local_usernames)) + len(remote_members) > MAX_GROUP_MEMBERS:
            raise ValueError(f"groups are limited to {MAX_GROUP_MEMBERS} people")
        before = await self._local_member_ids(conversation.id)
        change = await self._convos.apply_group_roster(
            conversation,
            local_usernames=local_usernames,
            remote_members=remote_members,
            at=_now_iso(),
        )
        if change is None:
            raise ValueError("the group changed meanwhile — try again")
        await self._announce(
            change,
            name=conversation.name,
            type_=conversation.type,
            creator_user_id=creator_user_id,
            before=before,
        )
        if is_owner_bound(conversation.id):
            removed = {inst for inst, _ in change.removed_remote}
            await self.publish_roster(conversation.id, also_to=removed)
        return change

    async def roster(self, conversation_id: str) -> list[GroupRosterMember]:
        """The member list as the wire names it: seated locals + remote seats."""
        members: list[GroupRosterMember] = []
        for m in await self._convos.list_members(conversation_id):
            if m.deleted_at is not None:
                continue
            user = await self._users.get(m.username)
            if user is None:
                continue
            members.append(
                GroupRosterMember(
                    user_id=user.user_id,
                    instance_id=self._own_instance_id,
                    username=user.username,
                    display_name=user.display_name,
                )
            )
        for seat in await self._convos.list_remote_members(conversation_id):
            user_id = seat.user_id
            display_name = seat.display_name
            if user_id is None:
                ru = await self._users.get_remote_by_member(
                    seat.instance_id, seat.remote_username
                )
                if ru is None:
                    continue
                user_id, display_name = ru.user_id, ru.display_name
            members.append(
                GroupRosterMember(
                    user_id=user_id,
                    instance_id=seat.instance_id,
                    username=seat.remote_username,
                    display_name=display_name or seat.remote_username,
                )
            )
        return members

    async def publish_roster(
        self,
        conversation_id: str,
        *,
        also_to: Iterable[str] = (),
    ) -> None:
        """Ship the current snapshot to every member household (+ ``also_to``).

        Members only, by construction: the targets are the remote seats'
        households (the authority is directly paired with each — it seated
        them) plus the households a change just removed. A household below
        v_37 is skipped with a WARNING; it could not parse the roster.
        """
        if self._federation is None:
            return
        conv = await self._convos.get(conversation_id)
        if conv is None or conv.type is not ConversationType.GROUP_DM:
            return
        members = await self.roster(conversation_id)
        payload = {
            "conversation_id": conversation_id,
            "version": conv.membership_version,
            "name": conv.name,
            "members": [m.to_wire() for m in members],
        }
        # A household the change took out learns only that: an empty list at
        # the new version — it drops every seat, and learns nothing about who
        # stays.
        member_households = {m.instance_id for m in members}
        removed_payload = {**payload, "members": []}
        targets = member_households | set(also_to)
        targets.discard(self._own_instance_id)
        for instance_id in sorted(targets):
            if not await self._federation.peer_supports(
                instance_id,
                min_version=FederationCapability.MIN_FOR_CROSS_HOUSEHOLD_GROUP_DM,
            ):
                log.warning(
                    "group %s: roster v%d not sent to %s — household is below "
                    "v_%d and cannot take part in group chats",
                    conversation_id,
                    conv.membership_version,
                    instance_id,
                    FederationCapability.MIN_FOR_CROSS_HOUSEHOLD_GROUP_DM,
                )
                continue
            result = await self._federation.send_event(
                to_instance_id=instance_id,
                event_type=FederationEventType.DM_GROUP_ROSTER,
                payload=payload
                if instance_id in member_households
                else removed_payload,
            )
            if not getattr(result, "ok", True):
                log.warning(
                    "group %s: roster v%d to %s not delivered (%s)",
                    conversation_id,
                    conv.membership_version,
                    instance_id,
                    getattr(result, "error", None),
                )

    # ── Member side ─────────────────────────────────────────────────────

    async def send_leave(self, conversation_id: str, user_id: str) -> None:
        """Tell the group's authority that ``user_id`` (ours) left."""
        if self._federation is None or self._federation_repo is None:
            return
        authority = await self._authority_instance(conversation_id)
        if authority is None:
            log.warning(
                "group %s: leave of %s not sent — its authority household is "
                "not a peer of ours",
                conversation_id,
                user_id,
            )
            return
        await self._federation.send_event(
            to_instance_id=authority,
            event_type=FederationEventType.DM_GROUP_LEAVE,
            payload={"conversation_id": conversation_id, "user_id": user_id},
        )

    async def _authority_instance(self, conversation_id: str) -> str | None:
        """The paired household ``conversation_id`` is bound to, if any."""
        assert self._federation_repo is not None
        for instance in await self._federation_repo.list_instances(
            status=PairingStatus.CONFIRMED.value
        ):
            if _bound_to(conversation_id, instance.id):
                return instance.id
        return None

    # ── Inbound ─────────────────────────────────────────────────────────

    async def _on_roster(self, event: "FederationEvent") -> None:
        """Apply an authority's member-list snapshot (receiver rules above)."""
        p = event.payload if isinstance(event.payload, dict) else {}
        conv_id = str(p.get("conversation_id") or "")
        sender = str(event.from_instance or "")
        version = p.get("version")
        raw_members = p.get("members")
        reason: str | None = None
        if (
            not conv_id
            or type(version) is not int
            or not 1 <= version < MAX_MEMBERSHIP_VERSION
        ):
            reason = "malformed roster"
        elif not isinstance(raw_members, list) or len(raw_members) > MAX_GROUP_MEMBERS:
            reason = "malformed member list"
        elif sender == self._own_instance_id or not _bound_to(conv_id, sender):
            reason = "sender is not the group's authority household"
        elif not await self._is_social_peer(sender):
            # A group seats our people only on the word of a household we
            # chose to pair with — never a mesh stranger that minted an id
            # bound to itself.
            reason = "the group's authority is not a paired household"
        if reason is not None:
            refuse(event, reason, conversation=conv_id)
            return
        assert isinstance(version, int) and isinstance(raw_members, list)
        existing = await self._convos.get(conv_id)
        if existing is not None and existing.type is not ConversationType.GROUP_DM:
            refuse(event, "conversation is not a group", conversation=conv_id)
            return
        seats = await self._seats_from(event, conv_id, raw_members)
        if seats is None:
            return
        local_usernames, remote_members = seats
        if existing is None and not local_usernames:
            refuse(event, "no local member in a new group", conversation=conv_id)
            return
        name = clean_group_name(p.get("name"))
        conversation = Conversation(
            id=conv_id,
            type=ConversationType.GROUP_DM,
            name=name,
            created_at=existing.created_at if existing else datetime.now(timezone.utc),
            membership_version=version,
        )
        before = await self._local_member_ids(conv_id)
        change = await self._convos.apply_group_roster(
            conversation,
            local_usernames=local_usernames,
            remote_members=remote_members,
            at=_now_iso(),
        )
        if change is None:
            log.info(
                "DM_GROUP_ROSTER from %s: v%d for %s is not newer — ignoring",
                sender,
                version,
                conv_id,
            )
            return
        await self._announce(
            change,
            name=name,
            type_=ConversationType.GROUP_DM,
            creator_user_id=None,
            before=before,
        )
        if change.added_local and self._history is not None:
            # A household newly seated catches up on the backlog from the
            # authority (the same pull a re-paired peer makes).
            await self._history.enqueue_request(
                instance_id=sender, conversation_id=conv_id
            )

    async def _seats_from(
        self,
        event: "FederationEvent",
        conv_id: str,
        raw_members: list,
    ) -> tuple[list[str], list[RemoteConversationMember]] | None:
        """Local usernames + remote seats a roster names, bound per entry.

        An entry on this household must name one of our users by
        ``user_id``. An entry elsewhere must not name a person this
        household knows as homed on another household (or here). An entry
        on the authority itself must name a user it has synced to us — the
        authority's own seats are the ones it could speak for live, so a
        ``user_id`` it merely claims is not enough; while one is missing
        the whole roster is held for that user's sync (``None``). Other bad
        entries are dropped with a WARNING; the rest of the roster stands.
        """
        local_usernames: list[str] = []
        remote: dict[tuple[str, str], RemoteConversationMember] = {}
        now = _now_iso()
        for raw in raw_members:
            entry = _parse_member(raw)
            if entry is None:
                refuse(event, "malformed roster entry", conversation=conv_id)
                continue
            if entry.instance_id == self._own_instance_id:
                user = await self._users.get_by_user_id(entry.user_id)
                if user is None:
                    refuse(
                        event,
                        "roster names an unknown local user",
                        conversation=conv_id,
                        user=entry.user_id,
                    )
                    continue
                local_usernames.append(user.username)
                continue
            home = await self._users.get_instance_for_user(entry.user_id)
            if home is not None and home != entry.instance_id:
                refuse(
                    event,
                    "roster seats a user on a household they are not homed on",
                    conversation=conv_id,
                    user=entry.user_id,
                )
                continue
            if home is None and entry.instance_id == event.from_instance:
                if self._pending is not None and self._pending.hold(
                    event, space_id=DM_HOLD_SCOPE, user_id=entry.user_id
                ):
                    log.info(
                        "DM_GROUP_ROSTER from %s: authority user %s not synced "
                        "yet — holding",
                        event.from_instance,
                        entry.user_id,
                    )
                    return None
                refuse(
                    event,
                    "roster names an authority user not synced here",
                    conversation=conv_id,
                    user=entry.user_id,
                )
                continue
            remote[(entry.instance_id, entry.username)] = RemoteConversationMember(
                conversation_id=conv_id,
                instance_id=entry.instance_id,
                remote_username=entry.username,
                joined_at=now,
                user_id=entry.user_id,
                display_name=entry.display_name,
            )
        return local_usernames, list(remote.values())

    async def _on_leave(self, event: "FederationEvent") -> None:
        """Authority side: a member household's own user left the group."""
        p = event.payload if isinstance(event.payload, dict) else {}
        conv_id = str(p.get("conversation_id") or "")
        user_id = str(p.get("user_id") or "")
        conv = await self._convos.get(conv_id) if conv_id else None
        reason: str | None = None
        seat: RemoteConversationMember | None = None
        if conv is None or conv.type is not ConversationType.GROUP_DM:
            reason = "no such group here"
        elif not is_owner_bound(conv_id) or not self.is_authority_here(conv_id):
            reason = "this household is not the group's authority"
        else:
            seat = await self._scope.seat_of(event, conv_id, user_id)
            if seat is None:
                reason = "leaver is not a seated user of the sending household"
        if reason is not None or conv is None or seat is None:
            refuse(event, reason or "refused", conversation=conv_id, user=user_id)
            return
        await self.remove_remote_seat(conv_id, seat.instance_id, seat.remote_username)

    async def remove_remote_seat(
        self,
        conversation_id: str,
        instance_id: str,
        remote_username: str,
    ) -> None:
        """Authority side: take one remote seat out, as the next snapshot.

        Retried when another membership change took the version first, so a
        member's leave (or a departed user) is never lost to a race.
        """
        for attempt in range(_LEAVE_RETRIES):
            conv = await self._convos.get(conversation_id)
            if conv is None:
                return
            seats = await self._convos.list_remote_members(conversation_id)
            remaining = [
                m
                for m in seats
                if (m.instance_id, m.remote_username) != (instance_id, remote_username)
            ]
            if len(remaining) == len(seats):
                return
            local = [
                m.username
                for m in await self._convos.list_members(conversation_id)
                if m.deleted_at is None
            ]
            try:
                await self.commit(
                    Conversation(
                        id=conv.id,
                        type=conv.type,
                        name=conv.name,
                        created_at=conv.created_at,
                        membership_version=conv.membership_version + 1,
                    ),
                    local_usernames=local,
                    remote_members=remaining,
                )
                return
            except ValueError:
                if attempt == _LEAVE_RETRIES - 1:
                    raise

    async def remove_departed_user(self, conversation_id: str, user_id: str) -> None:
        """A remote member's household deprovisioned them (``USER_REMOVED``).

        Their messages in the group are cleared here. On the authority their
        seat is also taken out, as the next roster; a member household keeps
        the seat until that roster arrives (seats are the authority's).
        """
        await self._convos.soft_delete_messages_by_sender(conversation_id, user_id)
        if not is_owner_bound(conversation_id) or not self.is_authority_here(
            conversation_id
        ):
            return
        for seat in await self._convos.list_remote_members(conversation_id):
            if seat.user_id == user_id or (
                seat.user_id is None
                and (
                    ru := await self._users.get_remote_by_member(
                        seat.instance_id, seat.remote_username
                    )
                )
                is not None
                and ru.user_id == user_id
            ):
                await self.remove_remote_seat(
                    conversation_id, seat.instance_id, seat.remote_username
                )
                return

    async def _is_social_peer(self, instance_id: str) -> bool:
        """A directly paired (CONFIRMED), social — not invite-link — household."""
        if self._federation_repo is None:
            return False
        instance = await self._federation_repo.get_instance(instance_id)
        return (
            instance is not None
            and instance.status is PairingStatus.CONFIRMED
            and instance.source is not InstanceSource.SPACE_SESSION
        )

    # ── Helpers ─────────────────────────────────────────────────────────

    async def _announce(
        self,
        change: GroupRosterChange,
        *,
        name: str | None,
        type_: ConversationType,
        creator_user_id: str | None,
        before: tuple[str, ...],
    ) -> None:
        """Local WS fan-out: new members see the group, everyone refetches."""
        added_ids: list[str] = []
        for username in change.added_local:
            user = await self._users.get(username)
            if user is not None:
                added_ids.append(user.user_id)
        after = await self._local_member_ids(change.conversation_id)
        if added_ids:
            await self._bus.publish(
                DmConversationCreated(
                    conversation_id=change.conversation_id,
                    conversation_type=type_.value,
                    name=name,
                    creator_user_id=creator_user_id or "",
                    member_user_ids=tuple(added_ids),
                )
            )
        if not change.created:
            await self._bus.publish(
                DmGroupRosterChanged(
                    conversation_id=change.conversation_id,
                    name=name,
                    notify_user_ids=tuple(dict.fromkeys((*before, *after))),
                )
            )

    async def _local_member_ids(self, conversation_id: str) -> tuple[str, ...]:
        ids: list[str] = []
        for m in await self._convos.list_members(conversation_id):
            if m.deleted_at is not None:
                continue
            user = await self._users.get(m.username)
            if user is not None:
                ids.append(user.user_id)
        return tuple(ids)


def _bound_to(conversation_id: str, instance_id: str) -> bool:
    """``conversation_id`` is a group id committing to ``instance_id``."""
    return (
        check_owner_bound_id(
            GROUP_CONVERSATION_KIND,
            conversation_id,
            space_id="",
            owner_user_id=instance_id,
        )
        is OwnerBinding.VALID
    )


def clean_group_name(raw: object) -> str | None:
    """A group name as stored: stripped, capped, ``None`` when empty."""
    if not isinstance(raw, str):
        return None
    return raw.strip()[:MAX_GROUP_NAME] or None


#: Longest id / username accepted from a roster entry.
_MAX_FIELD = 128


def _parse_member(raw: object) -> GroupRosterMember | None:
    if not isinstance(raw, dict):
        return None
    fields = [raw.get("user_id"), raw.get("instance_id"), raw.get("username")]
    if not all(isinstance(f, str) and 0 < len(f) <= _MAX_FIELD for f in fields):
        return None
    user_id, instance_id, username = (str(f) for f in fields)
    display_name = raw.get("display_name")
    return GroupRosterMember(
        user_id=user_id,
        instance_id=instance_id,
        username=username,
        display_name=(str(display_name) if display_name else username)[:80],
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
