"""RealtimeService — bridges domain events to WebSocket clients.

Subscribes to the in-process :class:`EventBus`, translates each event
into a ``{type, data}`` JSON frame, and pushes via
:class:`WebSocketManager` to the affected users.

The translation is intentionally minimal — the frontend does its own
state hydration from the REST API on receipt. WS frames are signals,
not full state replication. This keeps payloads small and avoids
encoding subtle data shapes in two places.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any

from ..domain.events import (
    BazaarBidPlaced,
    BazaarBidWithdrawn,
    BazaarListingCancelled,
    BazaarListingCreated,
    BazaarListingExpired,
    BazaarListingUpdated,
    BazaarOfferAccepted,
    BazaarOfferRejected,
    CalendarEventCreated,
    CalendarEventDeleted,
    CalendarEventUpdated,
    AutoPairRequestIncoming,
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    PairingAborted,
    PairingAcceptReceived,
    PairingConfirmed,
    PairingIntroReceived,
    SpaceMemberProfileUpdated,
    UserProfileUpdated,
    CpBlockAdded,
    CpBlockRemoved,
    CpGuardianAdded,
    CpGuardianRemoved,
    CpProtectionDisabled,
    CpProtectionEnabled,
    CpSpaceAgeGateChanged,
    DmConversationCreated,
    DmMessageCreated,
    HouseholdConfigChanged,
    NotificationCreated,
    NotificationReadChanged,
    PageConflictEmitted,
    PageEditLockAcquired,
    PageEditLockReleased,
    PostCreated,
    PostDeleted,
    PostEdited,
    PostReactionChanged,
    StickyCreated,
    StickyDeleted,
    StickyUpdated,
    PresenceUpdated,
    ShoppingItemAdded,
    ShoppingItemRemoved,
    ShoppingItemsCleared,
    ShoppingItemToggled,
    SpaceConfigChanged,
    SpaceJoinApproved,
    SpaceJoinDenied,
    SpaceJoinRequested,
    SpaceMemberJoined,
    SpaceMemberLeft,
    SpaceModerationApproved,
    SpaceModerationQueued,
    SpaceModerationRejected,
    SpacePostCreated,
    SpacePostModerated,
    SpaceZoneDeleted,
    SpaceZoneUpserted,
    PollClosed,
    PollCreated,
    PollVoted,
    SchedulePollFinalized,
    SchedulePollResponded,
    TaskAssigned,
    TaskCompleted,
    TaskCreated,
    TaskDeadlineDue,
    TaskDeleted,
    TaskListCreated,
    TaskListDeleted,
    TaskListUpdated,
    TaskUpdated,
    UserStatusChanged,
)
from ..infrastructure.event_bus import EventBus
from ..infrastructure.ws_manager import WebSocketManager
from ..media_signer import MediaUrlSigner, sign_media_urls_in
from .space_bot_service import (
    SpaceBotCreated,
    SpaceBotDeleted,
    SpaceBotTokenRotated,
    SpaceBotUpdated,
)

log = logging.getLogger(__name__)


class RealtimeService:
    """Push domain events to connected WebSocket clients.

    Parameters
    ----------
    bus:
        The shared in-process event bus.
    ws:
        The WebSocketManager that owns user → connection state.
    user_repo:
        Used to fan out post events to all active household members.
    space_repo:
        Used to enumerate space members for SpacePostCreated events.
    """

    __slots__ = ("_bus", "_ws", "_user_repo", "_space_repo", "_media_signer")

    def __init__(
        self,
        bus: EventBus,
        ws: WebSocketManager,
        *,
        user_repo,
        space_repo,
        media_signer: MediaUrlSigner | None = None,
    ) -> None:
        self._bus = bus
        self._ws = ws
        self._user_repo = user_repo
        self._space_repo = space_repo
        # Lets WS broadcast frames carry the same signed ``media_url`` /
        # ``picture_url`` / ``cover_url`` shape the REST API returns, so
        # browsers can drop the fields straight into ``<img src>``
        # without a follow-up REST hydrate. Optional only because
        # ``__init__`` runs before the signer is constructed in
        # ``_on_startup``; ``attach_media_signer`` wires it in once
        # available.
        self._media_signer = media_signer

    def attach_media_signer(self, signer: MediaUrlSigner) -> None:
        """Late binding — signer is built after RealtimeService.__init__."""
        self._media_signer = signer

    # ─── Wiring ───────────────────────────────────────────────────────────

    def wire(self) -> None:
        """Subscribe handlers on the bus.  Idempotent."""
        self._bus.subscribe(PostCreated, self._on_post_created)
        self._bus.subscribe(PostEdited, self._on_post_edited)
        self._bus.subscribe(PostDeleted, self._on_post_deleted)
        self._bus.subscribe(PostReactionChanged, self._on_post_reaction)
        self._bus.subscribe(CommentAdded, self._on_comment_added)
        self._bus.subscribe(CommentUpdated, self._on_comment_updated)
        self._bus.subscribe(CommentDeleted, self._on_comment_deleted)
        self._bus.subscribe(
            UserProfileUpdated,
            self._on_user_profile_updated,
        )
        self._bus.subscribe(
            SpaceMemberProfileUpdated,
            self._on_space_member_profile_updated,
        )
        self._bus.subscribe(
            PairingAcceptReceived,
            self._on_pairing_accept_received,
        )
        self._bus.subscribe(
            PairingConfirmed,
            self._on_pairing_confirmed,
        )
        self._bus.subscribe(
            PairingAborted,
            self._on_pairing_aborted,
        )
        self._bus.subscribe(
            PairingIntroReceived,
            self._on_pairing_intro_received,
        )
        self._bus.subscribe(
            AutoPairRequestIncoming,
            self._on_auto_pair_requested,
        )
        self._bus.subscribe(SpacePostCreated, self._on_space_post_created)
        self._bus.subscribe(SpaceMemberJoined, self._on_space_member_joined)
        # Bot persona lifecycle — frontend uses these to refresh the
        # "Bots" tab in space settings and to invalidate cached bot data
        # for feed posts when a bot is renamed/deleted.
        self._bus.subscribe(SpaceBotCreated, self._on_space_bot_created)
        self._bus.subscribe(SpaceBotUpdated, self._on_space_bot_updated)
        self._bus.subscribe(SpaceBotDeleted, self._on_space_bot_deleted)
        self._bus.subscribe(SpaceBotTokenRotated, self._on_space_bot_token_rotated)
        self._bus.subscribe(SpaceMemberLeft, self._on_space_member_left)
        self._bus.subscribe(SpaceJoinRequested, self._on_space_join_requested)
        self._bus.subscribe(SpaceJoinApproved, self._on_space_join_approved)
        self._bus.subscribe(SpaceJoinDenied, self._on_space_join_denied)
        self._bus.subscribe(SpacePostModerated, self._on_space_post_moderated)
        self._bus.subscribe(SpaceModerationQueued, self._on_space_mod_queued)
        self._bus.subscribe(SpaceModerationApproved, self._on_space_mod_approved)
        self._bus.subscribe(SpaceModerationRejected, self._on_space_mod_rejected)
        self._bus.subscribe(SpaceConfigChanged, self._on_space_config_changed)
        self._bus.subscribe(SpaceZoneUpserted, self._on_space_zone_upserted)
        self._bus.subscribe(SpaceZoneDeleted, self._on_space_zone_deleted)
        self._bus.subscribe(TaskAssigned, self._on_task_assigned)
        self._bus.subscribe(TaskCompleted, self._on_task_completed)
        self._bus.subscribe(TaskDeadlineDue, self._on_task_deadline)
        self._bus.subscribe(TaskCreated, self._on_task_created)
        self._bus.subscribe(TaskUpdated, self._on_task_updated)
        self._bus.subscribe(TaskDeleted, self._on_task_deleted)
        self._bus.subscribe(TaskListCreated, self._on_task_list_created)
        self._bus.subscribe(TaskListUpdated, self._on_task_list_updated)
        self._bus.subscribe(TaskListDeleted, self._on_task_list_deleted)
        self._bus.subscribe(
            SchedulePollResponded,
            self._on_schedule_responded,
        )
        self._bus.subscribe(
            SchedulePollFinalized,
            self._on_schedule_finalized,
        )
        self._bus.subscribe(PollCreated, self._on_poll_created)
        self._bus.subscribe(PollVoted, self._on_poll_voted)
        self._bus.subscribe(PollClosed, self._on_poll_closed)
        self._bus.subscribe(CalendarEventCreated, self._on_calendar_created)
        self._bus.subscribe(CalendarEventUpdated, self._on_calendar_updated)
        self._bus.subscribe(CalendarEventDeleted, self._on_calendar_deleted)
        self._bus.subscribe(UserStatusChanged, self._on_user_status)
        self._bus.subscribe(PresenceUpdated, self._on_presence_updated)
        self._bus.subscribe(ShoppingItemAdded, self._on_shopping_added)
        self._bus.subscribe(ShoppingItemToggled, self._on_shopping_toggled)
        self._bus.subscribe(ShoppingItemRemoved, self._on_shopping_removed)
        self._bus.subscribe(ShoppingItemsCleared, self._on_shopping_cleared)
        self._bus.subscribe(NotificationCreated, self._on_notification_new)
        self._bus.subscribe(
            NotificationReadChanged,
            self._on_notification_read_changed,
        )
        self._bus.subscribe(BazaarBidPlaced, self._on_bazaar_bid_placed)
        self._bus.subscribe(
            BazaarListingExpired,
            self._on_bazaar_listing_closed,
        )
        self._bus.subscribe(
            BazaarListingCreated,
            self._on_bazaar_listing_created,
        )
        self._bus.subscribe(
            BazaarListingUpdated,
            self._on_bazaar_listing_updated,
        )
        self._bus.subscribe(
            BazaarListingCancelled,
            self._on_bazaar_listing_cancelled,
        )
        self._bus.subscribe(
            BazaarOfferAccepted,
            self._on_bazaar_offer_accepted,
        )
        self._bus.subscribe(
            BazaarOfferRejected,
            self._on_bazaar_offer_rejected,
        )
        self._bus.subscribe(
            BazaarBidWithdrawn,
            self._on_bazaar_bid_withdrawn,
        )
        self._bus.subscribe(DmMessageCreated, self._on_dm_message_created)
        self._bus.subscribe(
            DmConversationCreated,
            self._on_dm_conversation_created,
        )
        self._bus.subscribe(
            HouseholdConfigChanged,
            self._on_household_config_changed,
        )
        self._bus.subscribe(CpProtectionEnabled, self._on_cp_protection_enabled)
        self._bus.subscribe(CpProtectionDisabled, self._on_cp_protection_disabled)
        self._bus.subscribe(CpGuardianAdded, self._on_cp_guardian_added)
        self._bus.subscribe(CpGuardianRemoved, self._on_cp_guardian_removed)
        self._bus.subscribe(CpBlockAdded, self._on_cp_block_added)
        self._bus.subscribe(CpBlockRemoved, self._on_cp_block_removed)
        self._bus.subscribe(PageEditLockAcquired, self._on_page_lock_acquired)
        self._bus.subscribe(PageEditLockReleased, self._on_page_lock_released)
        self._bus.subscribe(PageConflictEmitted, self._on_page_conflict)
        self._bus.subscribe(StickyCreated, self._on_sticky_created)
        self._bus.subscribe(StickyUpdated, self._on_sticky_updated)
        self._bus.subscribe(StickyDeleted, self._on_sticky_deleted)
        self._bus.subscribe(CpSpaceAgeGateChanged, self._on_cp_age_gate_changed)

    # ─── Household feed events ────────────────────────────────────────────

    async def _on_post_created(self, event: PostCreated) -> None:
        await self._broadcast_household(
            {
                "type": "post.created",
                "post": _safe(event.post),
            }
        )

    async def _on_post_edited(self, event: PostEdited) -> None:
        await self._broadcast_household(
            {
                "type": "post.edited",
                "post": _safe(event.post),
            }
        )

    async def _on_post_deleted(self, event: PostDeleted) -> None:
        await self._broadcast_household(
            {
                "type": "post.deleted",
                "post_id": event.post_id,
            }
        )

    async def _on_post_reaction(self, event: PostReactionChanged) -> None:
        await self._broadcast_household(
            {
                "type": "post.reaction_changed",
                "post": _safe(event.post),
            }
        )

    async def _on_comment_added(self, event: CommentAdded) -> None:
        frame = {
            "type": "comment.added",
            "post_id": event.post_id,
            "space_id": event.space_id,
            "comment": _safe(event.comment),
        }
        if event.space_id:
            await self._broadcast_space(event.space_id, frame)
        else:
            await self._broadcast_household(frame)

    async def _on_comment_updated(self, event: CommentUpdated) -> None:
        frame = {
            "type": "comment.updated",
            "post_id": event.post_id,
            "space_id": event.space_id,
            "comment": _safe(event.comment),
        }
        if event.space_id:
            await self._broadcast_space(event.space_id, frame)
        else:
            await self._broadcast_household(frame)

    async def _on_comment_deleted(self, event: CommentDeleted) -> None:
        frame = {
            "type": "comment.deleted",
            "post_id": event.post_id,
            "space_id": event.space_id,
            "comment_id": event.comment_id,
        }
        if event.space_id:
            await self._broadcast_space(event.space_id, frame)
        else:
            await self._broadcast_household(frame)

    async def _on_user_profile_updated(
        self,
        event: UserProfileUpdated,
    ) -> None:
        """Household-wide broadcast (bytes excluded — clients render
        ``picture_url`` directly so they don't have to know the
        signing scheme)."""
        picture_url = (
            f"/api/users/{event.user_id}/picture?v={event.picture_hash}"
            if event.picture_hash
            else None
        )
        # ``_broadcast_household`` signs ``picture_url`` automatically via
        # ``sign_media_urls_in()``, so the SPA can drop the value into
        # ``<img src>`` directly.
        await self._broadcast_household(
            {
                "type": "user.profile_updated",
                "user_id": event.user_id,
                "username": event.username,
                "display_name": event.display_name,
                "bio": event.bio,
                "picture_hash": event.picture_hash,
                "picture_url": picture_url,
            }
        )

    async def _on_space_member_profile_updated(
        self,
        event: SpaceMemberProfileUpdated,
    ) -> None:
        picture_url = (
            f"/api/spaces/{event.space_id}/members/{event.user_id}"
            f"/picture?v={event.picture_hash}"
            if event.picture_hash
            else None
        )
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.member.profile_updated",
                "space_id": event.space_id,
                "user_id": event.user_id,
                "space_display_name": event.space_display_name,
                "picture_hash": event.picture_hash,
                "picture_url": picture_url,
            },
        )

    async def _on_pairing_accept_received(
        self,
        event: PairingAcceptReceived,
    ) -> None:
        """Peer accepted our QR invite — forward SAS to the waiting UI."""
        await self._broadcast_household(
            {
                "type": "pairing.accept_received",
                "from_instance": event.from_instance,
                "token": event.token,
                "verification_code": event.verification_code,
            }
        )

    async def _on_pairing_confirmed(
        self,
        event: PairingConfirmed,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "pairing.confirmed",
                "instance_id": event.instance_id,
            }
        )

    async def _on_pairing_aborted(
        self,
        event: PairingAborted,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "pairing.aborted",
                "instance_id": event.instance_id,
                "reason": event.reason,
            }
        )

    async def _on_pairing_intro_received(
        self,
        event: PairingIntroReceived,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "pairing.intro_received",
                "from_instance": event.from_instance,
                "via_instance_id": event.via_instance_id,
                "message": event.message,
            }
        )

    async def _on_auto_pair_requested(
        self,
        event: AutoPairRequestIncoming,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "pairing.auto_pair_requested",
                "request_id": event.request_id,
                "from_a_id": event.from_a_id,
                "from_a_display": event.from_a_display,
                "via_b_id": event.via_b_id,
                "via_b_display": event.via_b_display,
            }
        )

    # ─── Space events ─────────────────────────────────────────────────────

    async def _on_space_post_created(self, event: SpacePostCreated) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.post.created",
                "space_id": event.space_id,
                "post": _safe(event.post),
            },
        )

    async def _on_space_post_moderated(self, event: SpacePostModerated) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.post.moderated",
                "space_id": event.space_id,
                "post": _safe(event.post),
                "moderated_by": event.moderated_by,
            },
        )

    async def _on_space_mod_queued(self, event: SpaceModerationQueued) -> None:
        item = event.item
        await self._broadcast_space(
            item.space_id,
            {
                "type": "space.moderation.queued",
                "space_id": item.space_id,
                "item": _safe(item),
            },
        )

    async def _on_space_mod_approved(self, event: SpaceModerationApproved) -> None:
        item = event.item
        await self._broadcast_space(
            item.space_id,
            {
                "type": "space.moderation.approved",
                "space_id": item.space_id,
                "item": _safe(item),
            },
        )

    async def _on_space_mod_rejected(self, event: SpaceModerationRejected) -> None:
        item = event.item
        await self._broadcast_space(
            item.space_id,
            {
                "type": "space.moderation.rejected",
                "space_id": item.space_id,
                "item": _safe(item),
            },
        )

    async def _on_space_config_changed(self, event: SpaceConfigChanged) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.config.changed",
                "space_id": event.space_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
            },
        )

    # ─── Space zones (§23.8.7) ───────────────────────────────────────────

    async def _on_space_zone_upserted(self, event: SpaceZoneUpserted) -> None:
        """Local fan-out for an admin's zone CRUD. Federation to remote
        member instances is owned by :class:`SpaceZoneOutbound`; this
        handler is just the WS frame for clients on this instance.
        """
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space_zone_changed",
                "data": {
                    "space_id": event.space_id,
                    "action": "upsert",
                    "zone_id": event.zone_id,
                    "zone": {
                        "id": event.zone_id,
                        "space_id": event.space_id,
                        "name": event.name,
                        "latitude": event.latitude,
                        "longitude": event.longitude,
                        "radius_m": event.radius_m,
                        "color": event.color,
                        "created_by": event.created_by,
                        "updated_at": event.updated_at,
                    },
                },
            },
        )

    async def _on_space_zone_deleted(self, event: SpaceZoneDeleted) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space_zone_changed",
                "data": {
                    "space_id": event.space_id,
                    "action": "delete",
                    "zone_id": event.zone_id,
                    "zone": None,
                },
            },
        )

    # ─── Space membership (§23.52) ───────────────────────────────────────

    async def _on_space_member_joined(self, event: SpaceMemberJoined) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.member.joined",
                "space_id": event.space_id,
                "user_id": event.user_id,
                "role": event.role,
            },
        )

    async def _on_space_member_left(self, event: SpaceMemberLeft) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.member.left",
                "space_id": event.space_id,
                "user_id": event.user_id,
            },
        )

    # ── Bot persona lifecycle ───────────────────────────────────────────

    async def _on_space_bot_created(self, event: SpaceBotCreated) -> None:
        # token_hash is on the SpaceBot dataclass; _safe uses the
        # security.sanitise_for_api filter so it's stripped on the wire.
        await self._broadcast_space(
            event.bot.space_id,
            {
                "type": "space.bot.created",
                "space_id": event.bot.space_id,
                "bot": _safe(event.bot),
            },
        )

    async def _on_space_bot_updated(self, event: SpaceBotUpdated) -> None:
        await self._broadcast_space(
            event.bot.space_id,
            {
                "type": "space.bot.updated",
                "space_id": event.bot.space_id,
                "bot": _safe(event.bot),
            },
        )

    async def _on_space_bot_deleted(self, event: SpaceBotDeleted) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.bot.deleted",
                "space_id": event.space_id,
                "bot_id": event.bot_id,
            },
        )

    async def _on_space_bot_token_rotated(self, event: SpaceBotTokenRotated) -> None:
        # No token in the payload — just a nudge for the HA integration to
        # re-auth (it holds the old token and will start getting 401s).
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.bot.token_rotated",
                "space_id": event.space_id,
                "bot_id": event.bot_id,
            },
        )

    async def _on_space_join_requested(
        self,
        event: SpaceJoinRequested,
    ) -> None:
        # Only notify admins + owner so the notification isn't a leak
        # about a space the requester isn't in yet.
        members = await self._space_repo.list_members(event.space_id)
        admin_ids = [m.user_id for m in members if m.role in ("owner", "admin")]
        await self._ws.broadcast_to_users(
            admin_ids,
            {
                "type": "space.join.requested",
                "space_id": event.space_id,
                "user_id": event.user_id,
                "request_id": event.request_id,
            },
        )

    async def _on_space_join_approved(
        self,
        event: SpaceJoinApproved,
    ) -> None:
        await self._broadcast_space(
            event.space_id,
            {
                "type": "space.join.approved",
                "space_id": event.space_id,
                "user_id": event.user_id,
                "request_id": event.request_id,
            },
        )

    async def _on_space_join_denied(self, event: SpaceJoinDenied) -> None:
        # Only tell the requester + the admins.
        members = await self._space_repo.list_members(event.space_id)
        admin_ids = [m.user_id for m in members if m.role in ("owner", "admin")]
        await self._ws.broadcast_to_users(
            admin_ids + [event.user_id],
            {
                "type": "space.join.denied",
                "space_id": event.space_id,
                "user_id": event.user_id,
                "request_id": event.request_id,
            },
        )

    # ─── Tasks ────────────────────────────────────────────────────────────

    async def _on_task_assigned(self, event: TaskAssigned) -> None:
        await self._ws.broadcast_to_user(
            event.assigned_to,
            {
                "type": "task.assigned",
                "task": _safe(event.task),
            },
        )

    async def _on_task_completed(self, event: TaskCompleted) -> None:
        await self._broadcast_household(
            {
                "type": "task.completed",
                "task_id": event.task.id,
                "completed_by": event.completed_by,
            }
        )

    async def _on_task_deadline(self, event: TaskDeadlineDue) -> None:
        for assignee in event.task.assignees or ():
            await self._ws.broadcast_to_user(
                assignee,
                {
                    "type": "task.deadline_due",
                    "task_id": event.task.id,
                    "due_date": event.due_date.isoformat(),
                },
            )

    async def _on_task_created(self, event: TaskCreated) -> None:
        payload = {
            "type": "task.created",
            "space_id": event.space_id,
            "task": _safe(event.task),
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_task_updated(self, event: TaskUpdated) -> None:
        payload = {
            "type": "task.updated",
            "space_id": event.space_id,
            "task": _safe(event.task),
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_task_deleted(self, event: TaskDeleted) -> None:
        payload = {
            "type": "task.deleted",
            "task_id": event.task_id,
            "list_id": event.list_id,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_task_list_created(self, event: TaskListCreated) -> None:
        payload = {
            "type": "task_list.created",
            "list_id": event.list_id,
            "name": event.name,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_task_list_updated(self, event: TaskListUpdated) -> None:
        payload = {
            "type": "task_list.updated",
            "list_id": event.list_id,
            "name": event.name,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_task_list_deleted(self, event: TaskListDeleted) -> None:
        payload = {
            "type": "task_list.deleted",
            "list_id": event.list_id,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    # ─── Schedule polls (§9 / §23.53) ────────────────────────────────────

    async def _on_schedule_responded(
        self,
        event: SchedulePollResponded,
    ) -> None:
        payload = {
            "type": "schedule_poll.responded",
            "post_id": event.post_id,
            "slot_id": event.slot_id,
            "user_id": event.user_id,
            "response": event.response,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_poll_created(self, event: PollCreated) -> None:
        payload = {
            "type": "poll.created",
            "post_id": event.post_id,
            "question": event.question,
            "allow_multiple": event.allow_multiple,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_poll_voted(self, event: PollVoted) -> None:
        payload = {
            "type": "poll.voted",
            "post_id": event.post_id,
            "voter_user_id": event.voter_user_id,
            "option_ids": list(event.option_ids),
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_poll_closed(self, event: PollClosed) -> None:
        payload = {
            "type": "poll.closed",
            "post_id": event.post_id,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_schedule_finalized(
        self,
        event: SchedulePollFinalized,
    ) -> None:
        payload = {
            "type": "schedule_poll.finalized",
            "post_id": event.post_id,
            "slot_id": event.slot_id,
            "slot_date": event.slot_date,
            "start_time": event.start_time,
            "end_time": event.end_time,
            "title": event.title,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    # ─── Calendar ─────────────────────────────────────────────────────────

    async def _on_calendar_created(self, event: CalendarEventCreated) -> None:
        await self._broadcast_household(
            {
                "type": "calendar.created",
                "event": _safe(event.event),
            }
        )

    async def _on_calendar_updated(self, event: CalendarEventUpdated) -> None:
        await self._broadcast_household(
            {
                "type": "calendar.updated",
                "event": _safe(event.event),
            }
        )

    async def _on_calendar_deleted(self, event: CalendarEventDeleted) -> None:
        await self._broadcast_household(
            {
                "type": "calendar.deleted",
                "event_id": event.event_id,
            }
        )

    # ─── User status ──────────────────────────────────────────────────────

    async def _on_user_status(self, event: UserStatusChanged) -> None:
        await self._broadcast_household(
            {
                "type": "user.status_changed",
                "user_id": event.user_id,
                "status": _safe(event.status) if event.status else None,
            }
        )

    # ─── Household feature toggles (§18 / §23.13) ────────────────────────

    async def _on_household_config_changed(
        self,
        event: HouseholdConfigChanged,
    ) -> None:
        """Broadcast toggle changes so every connected client can
        refresh its nav + post-type allowlist without a page reload."""
        await self._broadcast_household(
            {
                "type": "household.config_changed",
                "changed": dict(event.changed),
            }
        )

    # ─── Child Protection (§23.107) ──────────────────────────────────────

    async def _on_cp_protection_enabled(self, event: CpProtectionEnabled) -> None:
        await self._broadcast_household(
            {
                "type": "cp.protection_enabled",
                "minor_username": event.minor_username,
                "declared_age": event.declared_age,
            }
        )

    async def _on_cp_protection_disabled(self, event: CpProtectionDisabled) -> None:
        await self._broadcast_household(
            {
                "type": "cp.protection_disabled",
                "minor_username": event.minor_username,
            }
        )

    async def _on_cp_guardian_added(self, event: CpGuardianAdded) -> None:
        await self._broadcast_household(
            {
                "type": "cp.guardian_added",
                "minor_user_id": event.minor_user_id,
                "guardian_user_id": event.guardian_user_id,
            }
        )

    async def _on_cp_guardian_removed(self, event: CpGuardianRemoved) -> None:
        await self._broadcast_household(
            {
                "type": "cp.guardian_removed",
                "minor_user_id": event.minor_user_id,
                "guardian_user_id": event.guardian_user_id,
            }
        )

    async def _on_cp_block_added(self, event: CpBlockAdded) -> None:
        await self._broadcast_household(
            {
                "type": "cp.block_added",
                "minor_user_id": event.minor_user_id,
                "blocked_user_id": event.blocked_user_id,
            }
        )

    async def _on_cp_block_removed(self, event: CpBlockRemoved) -> None:
        await self._broadcast_household(
            {
                "type": "cp.block_removed",
                "minor_user_id": event.minor_user_id,
                "blocked_user_id": event.blocked_user_id,
            }
        )

    async def _on_cp_age_gate_changed(self, event: CpSpaceAgeGateChanged) -> None:
        await self._broadcast_household(
            {
                "type": "cp.age_gate_changed",
                "space_id": event.space_id,
                "min_age": event.min_age,
                "target_audience": event.target_audience,
            }
        )

    # ─── Pages (§23.72) ───────────────────────────────────────────────────

    async def _on_page_lock_acquired(self, event: PageEditLockAcquired) -> None:
        """Broadcast ``page.editing`` so other viewers disable Edit."""
        payload = {
            "type": "page.editing",
            "page_id": event.page_id,
            "space_id": event.space_id,
            "locked_by": event.locked_by,
            "lock_expires_at": event.lock_expires_at,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_page_lock_released(self, event: PageEditLockReleased) -> None:
        payload = {
            "type": "page.editing_done",
            "page_id": event.page_id,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_page_conflict(self, event: PageConflictEmitted) -> None:
        payload = {
            "type": "page.conflict",
            "page_id": event.page_id,
            "space_id": event.space_id,
            "theirs": event.theirs,
            "theirs_by": event.theirs_by,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    # ─── Stickies (§19) ───────────────────────────────────────────────────

    async def _on_sticky_created(self, event: StickyCreated) -> None:
        payload = {
            "type": "sticky.created",
            "id": event.sticky_id,
            "space_id": event.space_id,
            "author": event.author,
            "content": event.content,
            "color": event.color,
            "position_x": event.position_x,
            "position_y": event.position_y,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_sticky_updated(self, event: StickyUpdated) -> None:
        payload = {
            "type": "sticky.updated",
            "id": event.sticky_id,
            "space_id": event.space_id,
            "content": event.content,
            "color": event.color,
            "position_x": event.position_x,
            "position_y": event.position_y,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    async def _on_sticky_deleted(self, event: StickyDeleted) -> None:
        payload = {
            "type": "sticky.deleted",
            "id": event.sticky_id,
            "space_id": event.space_id,
        }
        if event.space_id is None:
            await self._broadcast_household(payload)
        else:
            await self._broadcast_space(event.space_id, payload)

    # ─── Fan-out helpers ──────────────────────────────────────────────────

    async def _broadcast_household(self, payload: dict) -> int:
        if self._media_signer is not None:
            sign_media_urls_in(payload, self._media_signer, extra_fields=("url",))
        users = await self._user_repo.list_active()
        return await self._ws.broadcast_to_users(
            [u.user_id for u in users],
            payload,
        )

    async def _broadcast_space(self, space_id: str, payload: dict) -> int:
        if self._media_signer is not None:
            sign_media_urls_in(payload, self._media_signer, extra_fields=("url",))
        ids = await self._space_repo.list_local_member_user_ids(space_id)
        return await self._ws.broadcast_to_users(ids, payload)

    # ─── Shopping list (§23.120 — local household only) ─────────────────

    async def _on_shopping_added(self, event: ShoppingItemAdded) -> None:
        # Shape matches the REST response so the client store can
        # append the event payload directly to ``items.value``.
        await self._broadcast_household(
            {
                "type": "shopping_list.item_added",
                "id": event.item_id,
                "text": event.text,
                "completed": False,
                "created_by": event.created_by,
                "created_at": event.created_at,
            }
        )

    async def _on_shopping_toggled(self, event: ShoppingItemToggled) -> None:
        await self._broadcast_household(
            {
                "type": "shopping_list.item_updated",
                "id": event.item_id,
                "completed": event.completed,
            }
        )

    async def _on_shopping_removed(self, event: ShoppingItemRemoved) -> None:
        await self._broadcast_household(
            {
                "type": "shopping_list.item_removed",
                "id": event.item_id,
            }
        )

    async def _on_shopping_cleared(self, event: ShoppingItemsCleared) -> None:
        await self._broadcast_household(
            {
                "type": "shopping_list.cleared",
                "count": event.count,
            }
        )

    # ─── Presence (§22) ──────────────────────────────────────────────────

    async def _on_presence_updated(self, event: PresenceUpdated) -> None:
        await self._broadcast_household(
            {
                "type": "presence.updated",
                "username": event.username,
                "state": event.state,
                "zone_name": event.zone_name,
                "latitude": event.latitude,
                "longitude": event.longitude,
            }
        )

    # ─── Notifications (§21) ─────────────────────────────────────────────

    async def _on_notification_new(self, event: NotificationCreated) -> None:
        await self._ws.broadcast_to_user(
            event.user_id,
            {
                "type": "notification.new",
                "notification_id": event.notification_id,
                "notif_type": event.type,
                "title": event.title,
            },
        )

    async def _on_notification_read_changed(
        self,
        event: NotificationReadChanged,
    ) -> None:
        await self._ws.broadcast_to_user(
            event.user_id,
            {
                "type": "notification.unread_count",
                "unread_count": event.unread_count,
            },
        )

    # ─── Bazaar (§9) ─────────────────────────────────────────────────────

    async def _on_bazaar_bid_placed(self, event: BazaarBidPlaced) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.bid_placed",
                "listing_post_id": event.listing_post_id,
                "amount": event.amount,
                "new_end_time": event.new_end_time,
            }
        )

    async def _on_bazaar_listing_closed(
        self,
        event: BazaarListingExpired,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.listing_closed",
                "listing_post_id": event.listing_post_id,
                "final_status": event.final_status,
            }
        )

    async def _on_bazaar_listing_created(
        self,
        event: BazaarListingCreated,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.listing_created",
                "listing_post_id": event.listing_post_id,
                "seller_user_id": event.seller_user_id,
                "mode": event.mode,
                "title": event.title,
            }
        )

    async def _on_bazaar_listing_updated(
        self,
        event: BazaarListingUpdated,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.listing_updated",
                "listing_post_id": event.listing_post_id,
            }
        )

    async def _on_bazaar_listing_cancelled(
        self,
        event: BazaarListingCancelled,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.listing_cancelled",
                "listing_post_id": event.listing_post_id,
            }
        )

    async def _on_bazaar_offer_accepted(
        self,
        event: BazaarOfferAccepted,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.offer_accepted",
                "listing_post_id": event.listing_post_id,
                "buyer_user_id": event.buyer_user_id,
                "price": event.price,
            }
        )

    async def _on_bazaar_offer_rejected(
        self,
        event: BazaarOfferRejected,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.offer_rejected",
                "listing_post_id": event.listing_post_id,
                "bid_id": event.bid_id,
                "bidder_user_id": event.bidder_user_id,
            }
        )

    async def _on_bazaar_bid_withdrawn(
        self,
        event: BazaarBidWithdrawn,
    ) -> None:
        await self._broadcast_household(
            {
                "type": "bazaar.bid_withdrawn",
                "listing_post_id": event.listing_post_id,
                "bid_id": event.bid_id,
                "bidder_user_id": event.bidder_user_id,
            }
        )

    # ─── DMs (§23.47) ────────────────────────────────────────────────────

    async def _on_dm_message_created(self, event: DmMessageCreated) -> None:
        """Push new DM messages to every recipient's WS sessions.

        §25.3: content is included on the in-process bus and intra-device
        WS (trusted transport). Push notifications separately apply the
        title-only redaction rule via :class:`NotificationService`.

        The frame ships a fully-formed ``message`` object (matching the
        REST :py:meth:`/api/conversations/{id}/messages` shape) so the
        client can append it directly to the thread without a follow-up
        fetch — the sender, recipient *and* every other open session land
        on the same row in the same render tick.
        """
        payload = {
            "type": "dm.message",
            "conversation_id": event.conversation_id,
            "sender_display": event.sender_display_name,
            "message": {
                "id": event.message_id,
                "sender_user_id": event.sender_user_id,
                "content": event.content,
                "type": event.message_type,
                "media_url": event.media_url,
                "reply_to_id": event.reply_to_id,
                "deleted": False,
                "created_at": event.occurred_at.isoformat(),
                "edited_at": None,
            },
        }
        # Sender's own sessions get the frame too — open thread tabs
        # show the sent message without the round-trip GET that
        # ``handleSend`` used to do.
        seen: set[str] = set()
        for user_id in (event.sender_user_id, *event.recipient_user_ids):
            if user_id in seen:
                continue
            seen.add(user_id)
            await self._ws.broadcast_to_user(user_id, payload)

    async def _on_dm_conversation_created(
        self,
        event: DmConversationCreated,
    ) -> None:
        """Tell every member's open inbox to refresh.

        The frame stays minimal — the inbox does a fresh GET so
        ordering, last-message stamps, and badge counts come from a
        single source of truth (the REST list endpoint).
        """
        payload = {
            "type": "dm.conversation.created",
            "conversation_id": event.conversation_id,
            "conversation_type": event.conversation_type,
            "name": event.name,
        }
        for user_id in event.member_user_ids:
            await self._ws.broadcast_to_user(user_id, payload)


# ─── Serialisation helper ────────────────────────────────────────────────


def _safe(value: Any) -> Any:
    """Convert a domain dataclass into a JSON-serialisable dict.

    Handles ``datetime`` / ``date`` (ISO-8601), ``frozenset`` / ``set``
    / ``tuple`` (lists), and nested dataclasses.  Anything else is
    passed through.
    """
    if value is None:
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return _safe(asdict(value))
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
