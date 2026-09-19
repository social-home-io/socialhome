"""Federation domain types (§4.1 / §11 / §24).

Only pure dataclasses and enums live here — no I/O, no service logic.

The :class:`FederationEventType` enum is the wire vocabulary. Adding a new
event type is a protocol change and must be done with care.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


# ─── Event type vocabulary (§24.11) ───────────────────────────────────────


class FederationEventType(str, enum.Enum):
    """All federation event types exchanged between instances.

    Values are the literal strings used on the wire. The ``str`` mixin means
    ``FederationEventType.FOO == "foo"`` evaluates true, which simplifies
    JSON parsing.
    """

    # ── Pairing ──
    PAIRING_INTRO = "pairing_intro"
    PAIRING_INTRO_RELAY = "pairing_intro_relay"
    PAIRING_INTRO_AUTO = "pairing_intro_auto"
    PAIRING_INTRO_AUTO_ACK = "pairing_intro_auto_ack"
    #: Fan-out at startup (and after the advertised capability set
    #: changes) — carries ``{proto_version, features}`` so peers can
    #: gate optional fields per receiver. See
    #: :mod:`socialhome.domain.federation_capabilities`.
    INSTANCE_CAPABILITIES_UPDATED = "instance_capabilities_updated"
    #: §319.6 — a peer asks us to re-broadcast state for a named scope
    #: (``capabilities`` / ``space:<id>`` / ``calendar:<id>``). The handler
    #: dispatches the scope and re-sends to the requester; space / calendar
    #: scopes are membership-gated. Capability-gated on
    #: :data:`FederationCapability.MIN_FOR_INSTANCE_RESYNC` (v_19).
    INSTANCE_RESYNC_REQUEST = "instance_resync_request"
    #: ``C → B`` carrying the same ack body as ``PAIRING_INTRO_AUTO_ACK``.
    #: B then forwards as ``PAIRING_INTRO_AUTO_ACK`` to A. Required because
    #: A has no confirmed pairing with C yet, so a direct C→A envelope
    #: would fail the inbound pipeline's signature check (the
    #: PENDING_SENT row A holds for C has no identity key on it).
    PAIRING_INTRO_AUTO_ACK_VIA = "pairing_intro_auto_ack_via"
    PAIRING_ACCEPT = "pairing_accept"
    PAIRING_CONFIRM = "pairing_confirm"
    #: §11 bootstrap handshake carried on the federation inbox URL
    #: instead of a dedicated REST route. Plaintext, Ed25519-signed body
    #: (TOFU on identity_pk + SAS round-trip). The receiving inbox view
    #: dispatches these short of the §24.11 pipeline because the
    #: ``RemoteInstance`` lookup that the pipeline assumes hasn't
    #: happened yet — the pair is being created by this message.
    PAIRING_PEER_ACCEPT = "pairing_peer_accept"
    PAIRING_PEER_CONFIRM = "pairing_peer_confirm"
    PAIRING_ABORT = "pairing_abort"
    UNPAIR = "unpair"
    URL_UPDATED = "url_updated"
    #: Sent on startup when the local HA-sourced home GPS coordinates
    #: change vs. the previously-stored value, and on first boot of
    #: a fresh instance. Payload: ``{"latitude": float, "longitude":
    #: float}`` (4dp-truncated per §25). Confirmed peers update
    #: their :class:`RemoteInstance.home_lat` / ``home_lon`` columns
    #: so the SPA's federation map renders the move.
    LOCAL_HOME_LOCATION_CHANGED = "local_home_location_changed"

    # ── User sync ──
    USERS_SYNC = "users_sync"
    USER_UPDATED = "user_updated"
    USER_REMOVED = "user_removed"
    USER_STATUS_UPDATED = "user_status_updated"
    USER_MOVED = "user_moved"
    USER_IDENTITY_RESOLVE = "user_identity_resolve"

    # ── Space structural ──
    SPACE_CREATED = "space_created"
    SPACE_AGE_GATE_UPDATED = "space_age_gate_updated"
    SPACE_CONFIG_CHANGED = "space_config_changed"
    SPACE_CONFIG_CATCH_UP = "space_config_catch_up"
    SPACE_DISSOLVED = "space_dissolved"
    SPACE_INSTANCE_LEFT = "space_instance_left"
    SPACE_MEMBER_JOINED = "space_member_joined"
    SPACE_MEMBER_LEFT = "space_member_left"
    SPACE_MEMBER_BANNED = "space_member_banned"
    SPACE_MEMBER_UNBANNED = "space_member_unbanned"
    #: Host promoted / demoted a member's role (#114). Broadcasts to
    #: every member household so every household's view of the
    #: roster stays in sync. v_8+.
    SPACE_MEMBER_ROLE_CHANGED = "space_member_role_changed"
    #: Cross-household admin command — remote admin on household A
    #: requests the host to kick a member (#114 phase 2, v_9+). The
    #: host validates the actor's role in ``space_remote_members.role``
    #: before dispatching. Payload carries actor identity + target.
    SPACE_REMOTE_ADMIN_KICK = "space_remote_admin_kick"
    #: Generic cross-household admin action (v_15+). A remote admin
    #: requests the host to run an admin-level mutation (config edit,
    #: ban / unban, archive / unarchive). The host re-validates the
    #: actor's ``space_remote_members.role`` and runs the real host
    #: method as the owner; the result federates back via the normal
    #: outbounds. Payload carries actor identity + ``action`` + ``params``.
    #: Generalises ``SPACE_REMOTE_ADMIN_KICK`` (kept for back-compat).
    SPACE_REMOTE_ADMIN_ACTION = "space_remote_admin_action"
    #: Host → admin households: mirror of an open / resolved critical-action
    #: approval proposal + its tally (v_16+). Lets a remote admin's SPA
    #: render the pending dissolve / publication-tier change and vote. The
    #: payload carries the SPA-facing proposal view. Members that don't
    #: support it simply don't show the pending UI.
    SPACE_ADMIN_PROPOSAL_UPDATED = "space_admin_proposal_updated"

    # ── Space invitations / join requests ──
    SPACE_INVITE = "space_invite"
    SPACE_INVITE_VIA = "space_invite_via"
    SPACE_ACCEPT = "space_accept"
    SPACE_JOIN_REQUEST = "space_join_request"
    SPACE_JOIN_REQUEST_VIA = "space_join_request_via"
    SPACE_JOIN_REQUEST_REPLY_VIA = "space_join_request_reply_via"
    SPACE_JOIN_REQUEST_APPROVED = "space_join_request_approved"
    SPACE_JOIN_REQUEST_DENIED = "space_join_request_denied"
    SPACE_JOIN_REQUEST_EXPIRED = "space_join_request_expired"
    SPACE_JOIN_REQUEST_WITHDRAWN = "space_join_request_withdrawn"
    # ── Token-based invite redeem (receiver-initiated, no admin
    # approval — the token IS the approval). Mirrors
    # ``SPACE_JOIN_REQUEST`` but for the open-invite token-paste
    # flow surfaced by the SPA's "Join with invite code" card. ──
    SPACE_INVITE_TOKEN_REDEEM = "space_invite_token_redeem"
    SPACE_INVITE_TOKEN_REDEEM_ACK = "space_invite_token_redeem_ack"
    SPACE_INVITE_TOKEN_REDEEM_DENY = "space_invite_token_redeem_deny"
    # ── §D2b invite-link bootstrap redeem (v_29) ──
    #
    # Same intent as the three above, but between households with **no**
    # pre-existing relationship: no confirmed pair, no mesh route, no
    # address. The body is sealed to the counterpart's published
    # key-wrap key and relayed by instance id through a connection
    # server, so these never ride an encrypted federation envelope and
    # never reach the §24.11 pipeline — they are dispatched ahead of it
    # exactly like the §11 pairing bootstrap. The values below are the
    # inner ``kind`` discriminators inside the sealed blob (see
    # ``socialhome.federation.invite_bootstrap``).
    SPACE_INVITE_BOOTSTRAP_REDEEM = "space_invite_bootstrap_redeem"
    SPACE_INVITE_BOOTSTRAP_REDEEM_ACK = "space_invite_bootstrap_redeem_ack"
    SPACE_INVITE_BOOTSTRAP_REDEEM_DENY = "space_invite_bootstrap_redeem_deny"
    # ── Federation mesh routing (v_6 PR 2) ──
    #
    # Generic source-routed envelope. Wraps **any** inner
    # FederationEventType for multi-hop forwarding through a chain of
    # confirmed peers when the origin isn't directly paired with the
    # target. One envelope shape for every flow — invite redeem, space
    # posts, comments, future events — so adding mesh-aware support
    # for a new event type doesn't need a new wire definition. Payload:
    # ``{route_id, path, position, inner_event_type, inner_payload}``.
    # Each hop:
    #
    # * drops if ``route_id`` seen recently (loop prevention);
    # * drops if self appears in ``path`` past current position (cycle);
    # * if next hop in ``path`` after ``position`` is self → unwrap,
    #   dispatch the inner event tagged with ``origin_instance_id =
    #   path[0]``;
    # * else forward to ``path[position+1]`` with position bumped.
    #
    # The response (ACK / DENY for redeems, future per-handler results
    # for other flows) wraps in a new SPACE_ROUTED with the reverse
    # path so both directions use one envelope.
    SPACE_ROUTED = "space_routed"
    # Route discovery — origin probes the federation graph for a
    # path to a target that isn't a direct peer. Loop prevention:
    # drop on seen request_id, drop if self is in ``hops_traversed``.
    # Response routes back along the same chain via cached
    # ``{request_id: caller_instance_id}`` (TTL ~60 s).
    SPACE_FIND_ROUTE = "space_find_route"
    SPACE_ROUTE_FOUND = "space_route_found"
    # Route-stale nack — target signals "no cached eph priv for this
    # pub" back along the reverse path so the origin invalidates its
    # cached route and retransmits once, instead of sealing under a
    # dead key until ``ROUTE_CACHE_TTL_S`` expires.
    SPACE_ROUTE_STALE = "space_route_stale"

    # ── Space content ──
    SPACE_POST_CREATED = "space_post_created"
    SPACE_POST_UPDATED = "space_post_updated"
    SPACE_POST_DELETED = "space_post_deleted"
    #: Inline-ship the WebP / WebM / file bytes that ``SPACE_POST_CREATED``
    #: referenced by URL. Without this companion event, a remote
    #: member's SPA renders ``<img src="api/media/foo.webp">`` against
    #: their OWN base href, which has no file → broken image. The
    #: receiver persists the bytes to their local ``media_path``
    #: under the same filename so the existing relative URL resolves.
    SPACE_MEDIA_BLOB = "space_media_blob"
    SPACE_COMMENT_CREATED = "space_comment_created"
    SPACE_COMMENT_UPDATED = "space_comment_updated"
    SPACE_COMMENT_DELETED = "space_comment_deleted"
    SPACE_MEMBER_PROFILE_UPDATED = "space_member_profile_updated"
    SPACE_PAGE_CREATED = "space_page_created"
    SPACE_PAGE_UPDATED = "space_page_updated"
    SPACE_PAGE_DELETED = "space_page_deleted"
    SPACE_TASK_CREATED = "space_task_created"
    SPACE_TASK_UPDATED = "space_task_updated"
    SPACE_TASK_DELETED = "space_task_deleted"
    SPACE_POLL_CREATED = "space_poll_created"
    SPACE_POLL_VOTE_CAST = "space_poll_vote_cast"
    SPACE_POLL_CLOSED = "space_poll_closed"
    SPACE_STICKY_CREATED = "space_sticky_created"
    SPACE_STICKY_UPDATED = "space_sticky_updated"
    SPACE_STICKY_DELETED = "space_sticky_deleted"
    SPACE_CALENDAR_EVENT_CREATED = "space_calendar_event_created"
    SPACE_CALENDAR_EVENT_UPDATED = "space_calendar_event_updated"
    SPACE_CALENDAR_EVENT_DELETED = "space_calendar_event_deleted"
    # Per-(event, user, occurrence) RSVP propagation. Out-of-order
    # arrivals (RSVP before its event) buffer in pending_federated_rsvps
    # and flush on event arrival.
    SPACE_RSVP_UPDATED = "space_rsvp_updated"
    SPACE_RSVP_DELETED = "space_rsvp_deleted"
    # Personal calendar federation (§23.60). Cross-household invites
    # land on the recipient's personal calendar with origin='remote_invite'.
    # Local household members are NOT invited — coordinating with a
    # household member is done by writing directly to their calendar.
    # Only confirmed paired-instance users appear in the invite picker.
    PERSONAL_CALENDAR_EVENT_CREATED = "personal_calendar_event_created"
    PERSONAL_CALENDAR_EVENT_UPDATED = "personal_calendar_event_updated"
    PERSONAL_CALENDAR_EVENT_DELETED = "personal_calendar_event_deleted"
    PERSONAL_CALENDAR_RSVP_UPDATED = "personal_calendar_rsvp_updated"
    PERSONAL_CALENDAR_RSVP_DELETED = "personal_calendar_rsvp_deleted"
    #: Schedule-poll bootstrap. Carries slot definitions + title +
    #: deadline so a remote member can render the slot picker. Without
    #: this, only ``SPACE_SCHEDULE_RESPONSE_UPDATED`` / ``_FINALIZED``
    #: federated and remote members saw an empty schedule poll until
    #: §25.6 catch-up sync ran (and catch-up didn't cover slot defs
    #: either, F5 fixes both layers).
    SPACE_SCHEDULE_CREATED = "space_schedule_created"
    SPACE_SCHEDULE_RESPONSE_UPDATED = "space_schedule_response_updated"
    SPACE_SCHEDULE_FINALIZED = "space_schedule_finalized"
    SPACE_LOCATION_UPDATED = "space_location_updated"
    # ── Space-defined zones (§23.8.7). Per-space display catalogue,
    # sealed under the space content key. CRUD is admin-only at the
    # source; the inbound handler applies the upsert/delete to the
    # local space_zones table. Zones are a display layer — they never
    # replace coordinates on the wire, and HA zones do not propagate.
    SPACE_ZONE_UPSERTED = "space_zone_upserted"
    SPACE_ZONE_DELETED = "space_zone_deleted"
    # ── Gallery (§23.119) — per-event push complementing the chunked
    # initial sync. Carries the thumbnail-only projection per S-9; the
    # full file is fetched lazily via ``gallery_item_full``. Albums
    # ride on the chunked sync only — they're rare and structural.
    SPACE_GALLERY_ITEM_CREATED = "space_gallery_item_created"
    SPACE_GALLERY_ITEM_DELETED = "space_gallery_item_deleted"
    # ── Bazaar (§5.6) — per-space marketplace listings. The wrapper
    # ``PostType.BAZAAR`` post federates via ``SPACE_POST_CREATED``
    # with just the caption; this event carries the full
    # :class:`BazaarListing` payload (mode, price, photos, status, …)
    # so remote members see what's actually for sale, not just the
    # title. Image bytes ship through the same media outbox the
    # post + gallery paths use (correlation_id = listing.post_id).
    BAZAAR_LISTING_CREATED = "bazaar_listing_created"
    #: Status-only update on an existing listing (F8) — SOLD / EXPIRED /
    #: CANCELLED. Receivers update their local row's status.
    BAZAAR_LISTING_UPDATED = "bazaar_listing_updated"
    #: F7 — a remote bidder placed a bid (or offer). Receivers
    #: persist into ``bazaar_bids`` so the seller's host (and every
    #: other member's local view) see the same canonical bid id +
    #: amount. Required because the bid endpoint on a non-seller's
    #: instance can't reach the seller's DB directly.
    BAZAAR_BID_PLACED = "bazaar_bid_placed"
    #: F7 — seller marked an offer as accepted (and the listing as
    #: sold). Receivers flip the bid row's ``accepted=true`` and apply
    #: the matching ``mark_sold`` so winner_user_id / winning_price
    #: stay consistent.
    BAZAAR_OFFER_ACCEPTED = "bazaar_offer_accepted"

    # ── Space encryption key exchange ──
    SPACE_KEY_EXCHANGE = "space_key_exchange"
    SPACE_KEY_EXCHANGE_ACK = "space_key_exchange_ack"
    SPACE_KEY_EXCHANGE_REKEY = "space_key_exchange_rekey"
    SPACE_ADMIN_KEY_SHARE = "space_admin_key_share"
    SPACE_SESSION_CLEANUP = "space_session_cleanup"

    # ── Space sync ──
    SPACE_SYNC_BEGIN = "space_sync_begin"
    SPACE_SYNC_CHUNK = "space_sync_chunk"
    SPACE_SYNC_CHUNK_ACK = "space_sync_chunk_ack"
    SPACE_SYNC_RESUME = "space_sync_resume"
    SPACE_SYNC_COMPLETE = "space_sync_complete"
    SPACE_SYNC_OFFER = "space_sync_offer"
    SPACE_SYNC_ANSWER = "space_sync_answer"
    SPACE_SYNC_ICE = "space_sync_ice"
    SPACE_SYNC_DIRECT_READY = "space_sync_direct_ready"
    SPACE_SYNC_DIRECT_FAILED = "space_sync_direct_failed"
    SPACE_SYNC_REQUEST_MORE = "space_sync_request_more"
    SPACE_SYNC_REJECTED = "space_sync_rejected"

    # ── Resilience / partition handling ──
    INSTANCE_SYNC_STATUS = "instance_sync_status"
    SPACE_PARTITION_GAP = "space_partition_gap"
    NODE_PARTITION_CATCHUP = "node_partition_catchup"
    NODE_PARTITION_GAP = "node_partition_gap"

    # ── DM relay ──
    DM_USER_TYPING = "dm_user_typing"
    DM_RELAY = "dm_relay"
    DM_MESSAGE = "dm_message"
    #: Full-quality media bytes for a previously-sent ``DM_MESSAGE``.
    #: Gated on :data:`FederationCapability.MIN_FOR_DM_MEDIA_SYNC`
    #: (proto_version 3) — see :mod:`socialhome.federation.compat.dm_media_v3`
    #: for the sub-v_3 fallback. Payload carries ``media_blob_id``
    #: (matches the ``DM_MESSAGE`` it follows), ``message_id``,
    #: ``conversation_id``, and the encrypted bytes. The full pipeline
    #: (sender outbox + receiver inbox handler + scheduler) lands
    #: incrementally in follow-up PRs; this entry reserves the wire
    #: name so the capability bump is honest about what v_3 advertises.
    DM_MEDIA_BLOB = "dm_media_blob"
    DM_MESSAGE_DELETED = "dm_message_deleted"
    DM_MESSAGE_REACTION = "dm_message_reaction"
    DM_MEMBER_ADDED = "dm_member_added"
    DM_CONTACT_REQUEST = "dm_contact_request"
    DM_CONTACT_ACCEPTED = "dm_contact_accepted"
    DM_CONTACT_DECLINED = "dm_contact_declined"
    DM_HISTORY_REQUEST = "dm_history_request"
    DM_HISTORY_CHUNK = "dm_history_chunk"
    DM_HISTORY_CHUNK_ACK = "dm_history_chunk_ack"
    DM_HISTORY_COMPLETE = "dm_history_complete"

    # ── Public space advertisement ──
    PUBLIC_SPACE_ADVERTISE = "public_space_advertise"
    PUBLIC_SPACE_WITHDRAWN = "public_space_withdrawn"

    # ── Peer-to-peer public-space directory sync (§D1a) ──
    # One household publishes a snapshot of its ``type=public`` spaces
    # to each CONFIRMED peer so the peer's space browser can list them
    # under "From friends" without going via the GFS.
    SPACE_DIRECTORY_SYNC = "space_directory_sync"

    # ── Cross-household invites for private spaces (§D1b, zero-leak) ──
    # Plaintext envelope carries only routing fields. All space metadata
    # (space_id, display hint, inviter, invite_token) rides inside the
    # encrypted payload. See `federation/private_invite_handler.py`.
    SPACE_PRIVATE_INVITE = "space_private_invite"
    SPACE_PRIVATE_INVITE_ACCEPT = "space_private_invite_accept"
    SPACE_PRIVATE_INVITE_DECLINE = "space_private_invite_decline"
    SPACE_REMOTE_MEMBER_REMOVED = "space_remote_member_removed"

    # ── GFS subscriber content-key handoff (Phase 5b-b) ──
    # A seed-holder seals the per-space content key to a new GFS subscriber's
    # published key-wrap pubkey and relays it through the content-blind GFS
    # (authorized by the space-authority signature, like ``space_post_public``).
    # Rides the GFS relay rather than a direct peer event — so no proto-version
    # bump (the OURS gate is for direct-peer fields an older peer can't
    # fail-soft against; a non-subscriber simply never receives this frame).
    SPACE_SUBSCRIBER_KEY_HANDOFF = "space_subscriber_key_handoff"

    # ── Moderation ──
    SPACE_REPORT = "space_report"

    # ── Presence ──
    PRESENCE_UPDATED = "presence_updated"
    # Session presence (online/idle/offline) — orthogonal to physical
    # PRESENCE_UPDATED. Fans out to every confirmed peer so cross-
    # instance household members can show the green/amber dot too.
    USER_ONLINE = "user_online"
    USER_IDLE = "user_idle"
    USER_OFFLINE = "user_offline"

    # ── WebRTC / calls ──
    CALL_OFFER = "call_offer"
    CALL_ANSWER = "call_answer"
    CALL_DECLINE = "call_decline"
    CALL_BUSY = "call_busy"
    CALL_HANGUP = "call_hangup"
    CALL_END = "call_end"
    CALL_ICE = "call_ice"
    CALL_ICE_CANDIDATE = "call_ice_candidate"
    CALL_QUALITY = "call_quality"

    # ── Highlights (§Highlights) ──
    # Personal "highlights" pillar — per-author per-day frame bag, federated
    # to peers per the author's audience kind. See ``highlight_service`` for
    # outbound fan-out and ``federation/sync/highlight_inbound.py`` for the
    # inbound handlers (registered via ``_event_registry``).
    HIGHLIGHT_CREATED = "highlight_created"
    HIGHLIGHT_FRAME_APPENDED = "highlight_frame_appended"
    HIGHLIGHT_FRAME_DELETED = "highlight_frame_deleted"
    HIGHLIGHT_DELETED = "highlight_deleted"
    HIGHLIGHT_FRAME_VIEWED = "highlight_frame_viewed"
    HIGHLIGHT_FRAME_REACTED = "highlight_frame_reacted"
    HIGHLIGHT_FRAME_REACTION_REMOVED = "highlight_frame_reaction_removed"

    # ── Momentum (§Momentum) — household-broadcast posts with 3-hop relay ──
    MOMENT_CREATED = "moment_created"
    MOMENT_DELETED = "moment_deleted"
    MOMENT_REACTED = "moment_reacted"
    MOMENT_REACTION_REMOVED = "moment_reaction_removed"

    # ── Public Momentum (§Momentum-public) — GFS → author back-channel for
    # follower bookkeeping. The public moment itself rides as a WS frame
    # outside the §24.11 inbound pipeline, but the GFS surfaces follow
    # changes back to the author over the same federation envelope shape
    # so the author's instance can update follower-count UI and prefs.
    MOMENT_PUBLIC_FOLLOW = "moment_public_follow"
    MOMENT_PUBLIC_UNFOLLOW = "moment_public_unfollow"

    # ── Network discovery ──
    NETWORK_SYNC = "network_sync"

    # ── P2P federation-level WebRTC signalling (§24.12.5) ──
    # Used to bootstrap a persistent DataChannel between paired
    # Social Home instances over the existing signed HTTPS inbox. Once
    # the channel is open, routine federation envelopes are delivered
    # over it and HTTPS acts as fallback only.
    FEDERATION_RTC_OFFER = "federation_rtc_offer"
    FEDERATION_RTC_ANSWER = "federation_rtc_answer"
    FEDERATION_RTC_ICE = "federation_rtc_ice"

    # ── Social Home Apps (§Apps) — cross-household app federation bridge ──
    #: Session lifecycle for a Social Home App federation session. Controls
    #: the per-pair ``fed-app-v1`` DataChannel (v_17+ fast path) or falls
    #: back gracefully to the ``fed-v1`` / HTTPS JSON event path for older
    #: peers. Session setup / teardown always rides the event path regardless
    #: of peer version.
    APP_SESSION = "app_session"
    #: Application-layer message between Social Home App instances on paired
    #: households. On v_17+ CONFIRMED direct peers the binary ``fed-app-v1``
    #: DataChannel is the fast path; sub-v_17 peers receive this as a standard
    #: JSON federation event on ``fed-v1`` / HTTPS (same fallback shape as the
    #: v_14 media channel). Payload is fully encrypted per §25.8.21.
    APP_MESSAGE = "app_message"


# Subsets used throughout the service layer ────────────────────────────────

#: Events whose plaintext payloads are permitted to carry routing metadata
#: only. The encrypted envelope is required for anything else (§25.8.20–21).
PAIRING_EVENTS: frozenset[FederationEventType] = frozenset(
    {
        FederationEventType.PAIRING_INTRO,
        FederationEventType.PAIRING_INTRO_RELAY,
        FederationEventType.PAIRING_INTRO_AUTO,
        FederationEventType.PAIRING_INTRO_AUTO_ACK,
        FederationEventType.PAIRING_INTRO_AUTO_ACK_VIA,
        FederationEventType.PAIRING_ACCEPT,
        FederationEventType.PAIRING_CONFIRM,
        FederationEventType.PAIRING_PEER_ACCEPT,
        FederationEventType.PAIRING_PEER_CONFIRM,
        FederationEventType.PAIRING_ABORT,
        FederationEventType.UNPAIR,
        FederationEventType.LOCAL_HOME_LOCATION_CHANGED,
    }
)

#: Structural events that must survive retention pruning (§4.4.7 / §25.8.19).
STRUCTURAL_EVENTS: frozenset[FederationEventType] = frozenset(
    {
        FederationEventType.SPACE_CREATED,
        FederationEventType.SPACE_DISSOLVED,
        FederationEventType.SPACE_CONFIG_CHANGED,
        FederationEventType.SPACE_MEMBER_JOINED,
        FederationEventType.SPACE_MEMBER_LEFT,
        FederationEventType.SPACE_MEMBER_BANNED,
        FederationEventType.SPACE_MEMBER_UNBANNED,
        FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
        FederationEventType.SPACE_KEY_EXCHANGE,
        FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
        FederationEventType.SPACE_ADMIN_KEY_SHARE,
    }
)


#: What a **space-scoped** peer may send us (§D2b).
#:
#: A household seated from an invite link holds an
#: :data:`InstanceSource.SPACE_SESSION` ``remote_instances`` row: a valid,
#: unexpired, unexhausted invite token was the whole of its authorization,
#: and what it bought was *one space*, not a social relationship. Yet the
#: row is CONFIRMED and carries directional session keys, so without this
#: gate the §24.11 pipeline happily validates and dispatches **any** event
#: type from it — DMs, the user roster, presence, call signalling, moments,
#: mesh-route probes. That is the shape of the finding this list closes:
#: the pipeline had no notion of peer class at all.
#:
#: The rule is **deny by default**. The
#: ``check_peer_class`` step in :mod:`socialhome.federation.inbound_validator`
#: rejects anything not named here, so a newly-added
#: :class:`FederationEventType` is refused from a space-scoped peer until
#: somebody deliberately classifies it (pinned by a test that enumerates
#: ``set(FederationEventType)``).
#:
#: Membership in *any* shared space is NOT re-checked here — the ban step
#: only fires when the envelope carries a ``space_id``, and the handlers
#: themselves are membership-gated. This list is about the *class* of
#: traffic a link-joined household is entitled to emit at all.
SPACE_SESSION_ALLOWED_EVENT_TYPES: frozenset[FederationEventType] = frozenset(
    {
        #: Peers exchange their protocol version on every startup; without
        #: it both sides stay pinned at v_1 and every capability-gated
        #: field is suppressed forever. The only non-``SPACE_*`` entry.
        FederationEventType.INSTANCE_CAPABILITIES_UPDATED,
        # ── Structure / config of the space we actually share ──
        #: The host dissolving the space, or a household leaving it, is
        #: exactly the news a link-joined member must not miss.
        FederationEventType.SPACE_DISSOLVED,
        FederationEventType.SPACE_INSTANCE_LEFT,
        #: Config edits (name, description, features, join mode, retention)
        #: and the catch-up replay of them. A member whose config rots shows
        #: a stale space forever.
        FederationEventType.SPACE_CONFIG_CHANGED,
        FederationEventType.SPACE_CONFIG_CATCH_UP,
        #: Age gate: a §CP.F1 restriction tightened after the join has to
        #: reach the member household that enforces it locally.
        FederationEventType.SPACE_AGE_GATE_UPDATED,
        # ── Roster ──
        #: The Members tab, and the per-member keys the content-key epoch
        #: is distributed against. A roster that never updates leaves the
        #: member unable to attribute — or decrypt — anything.
        FederationEventType.SPACE_MEMBER_JOINED,
        FederationEventType.SPACE_MEMBER_LEFT,
        FederationEventType.SPACE_MEMBER_BANNED,
        FederationEventType.SPACE_MEMBER_UNBANNED,
        FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
        FederationEventType.SPACE_MEMBER_PROFILE_UPDATED,
        #: Host → the kicked household: "your member is out". Dropping it
        #: would leave a removed member believing they are still seated.
        FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
        #: Host → admin households: the mirror of a pending critical-action
        #: proposal plus its tally. Read-only on the receiver.
        FederationEventType.SPACE_ADMIN_PROPOSAL_UPDATED,
        #: A link-joined member that the host later promoted to admin is an
        #: admin: these are its only way to act. Safe to accept because the
        #: host re-validates the actor's ``space_remote_members.role``
        #: before running anything — authentication here, authorization
        #: there.
        FederationEventType.SPACE_REMOTE_ADMIN_KICK,
        FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
        #: Teardown of the space-scoped relationship itself (§D2b): the
        #: side that drops its row tells the other to drop its own.
        FederationEventType.SPACE_SESSION_CLEANUP,
        # ── Content ──
        #: The space's own posts, comments and every pillar rendered
        #: inside it. This is what "joined the space" means.
        FederationEventType.SPACE_POST_CREATED,
        FederationEventType.SPACE_POST_UPDATED,
        FederationEventType.SPACE_POST_DELETED,
        FederationEventType.SPACE_COMMENT_CREATED,
        FederationEventType.SPACE_COMMENT_UPDATED,
        FederationEventType.SPACE_COMMENT_DELETED,
        FederationEventType.SPACE_PAGE_CREATED,
        FederationEventType.SPACE_PAGE_UPDATED,
        FederationEventType.SPACE_PAGE_DELETED,
        FederationEventType.SPACE_TASK_CREATED,
        FederationEventType.SPACE_TASK_UPDATED,
        FederationEventType.SPACE_TASK_DELETED,
        FederationEventType.SPACE_POLL_CREATED,
        FederationEventType.SPACE_POLL_VOTE_CAST,
        FederationEventType.SPACE_POLL_CLOSED,
        FederationEventType.SPACE_STICKY_CREATED,
        FederationEventType.SPACE_STICKY_UPDATED,
        FederationEventType.SPACE_STICKY_DELETED,
        FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
        FederationEventType.SPACE_CALENDAR_EVENT_UPDATED,
        FederationEventType.SPACE_CALENDAR_EVENT_DELETED,
        FederationEventType.SPACE_RSVP_UPDATED,
        FederationEventType.SPACE_RSVP_DELETED,
        FederationEventType.SPACE_SCHEDULE_CREATED,
        FederationEventType.SPACE_SCHEDULE_RESPONSE_UPDATED,
        FederationEventType.SPACE_SCHEDULE_FINALIZED,
        FederationEventType.SPACE_GALLERY_ITEM_CREATED,
        FederationEventType.SPACE_GALLERY_ITEM_DELETED,
        FederationEventType.BAZAAR_LISTING_CREATED,
        FederationEventType.BAZAAR_LISTING_UPDATED,
        FederationEventType.BAZAAR_BID_PLACED,
        FederationEventType.BAZAAR_OFFER_ACCEPTED,
        #: Space-scoped location pins and the space's own zone catalogue —
        #: a display layer inside the space, distinct from the household
        #: ``PRESENCE_UPDATED`` fan-out (which stays excluded).
        FederationEventType.SPACE_LOCATION_UPDATED,
        FederationEventType.SPACE_ZONE_UPSERTED,
        FederationEventType.SPACE_ZONE_DELETED,
        #: Moderation reports raised inside the space.
        FederationEventType.SPACE_REPORT,
        # ── Media ──
        #: The bytes ``SPACE_POST_CREATED`` / the gallery referenced by
        #: URL. Without them a member renders broken images.
        FederationEventType.SPACE_MEDIA_BLOB,
        # ── Content-key epochs ──
        #: The per-space AES key handshake and its rotations. Excluded from
        #: this set, a member decrypts nothing after the first rekey.
        FederationEventType.SPACE_KEY_EXCHANGE,
        FederationEventType.SPACE_KEY_EXCHANGE_ACK,
        FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
        # ── Catch-up sync (§25.6) ──
        #: The chunked backfill that populates a fresh seat. The RTC
        #: *signalling* half of sync (``SPACE_SYNC_OFFER`` / ``_ANSWER`` /
        #: ``_ICE`` / ``_DIRECT_READY`` / ``_DIRECT_FAILED``) is
        #: deliberately absent: it exists to negotiate a DIRECT peer
        #: connection, and the whole point of §D2b is that these two
        #: households never learn each other's address.
        FederationEventType.SPACE_SYNC_BEGIN,
        FederationEventType.SPACE_SYNC_CHUNK,
        FederationEventType.SPACE_SYNC_CHUNK_ACK,
        FederationEventType.SPACE_SYNC_RESUME,
        FederationEventType.SPACE_SYNC_COMPLETE,
        FederationEventType.SPACE_SYNC_REQUEST_MORE,
        FederationEventType.SPACE_SYNC_REJECTED,
        #: Space-scoped partition repair. Its node-scoped siblings
        #: (``NODE_PARTITION_*``, ``INSTANCE_SYNC_STATUS``) are about the
        #: whole household and stay out.
        FederationEventType.SPACE_PARTITION_GAP,
        # ── Routing: NOTHING ──
        #: A link-joined household is never a mesh hop (``_mesh_capable_peers``
        #: excludes it), so none of ``SPACE_FIND_ROUTE`` / ``SPACE_ROUTE_FOUND``
        #: / ``SPACE_ROUTED`` / ``SPACE_ROUTE_STALE`` has any meaning coming
        #: from one. ``SPACE_ROUTE_FOUND`` carries ``path`` — every relay
        #: household's id — so answering a stranger's probe would hand them a
        #: map of our social graph; a route-stale nack from a peer we never
        #: route through is noise at best.
        # ── Joining a SECOND space from the same household ──
        #: Once the pair exists, ``request_redeem`` takes the direct-peer
        #: branch, so a second invite token from this household rides the
        #: §24.11 pipeline rather than the bootstrap relay. The token is
        #: still the whole authorization. ``SPACE_JOIN_REQUEST*`` stays
        #: out — that is the ask-an-admin flow, and this peer class does
        #: not get to queue itself for approval.
        FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
        FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
        FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY,
    }
)
#: Deliberately **excluded**, for the record: every DM / presence / call /
#: highlight / moment / user-sync / pairing / public-directory type (none of
#: it is space federation); ``SPACE_ADMIN_KEY_SHARE`` (handing the space
#: seed to a household that walked in off a link is never right);
#: ``SPACE_JOIN_REQUEST*`` (they are already a member);
#: ``SPACE_PRIVATE_INVITE*`` and ``SPACE_INVITE*`` (§D1b social invites);
#: ``SPACE_CREATED`` (the shared space is seated by the redeem ACK — a
#: ``SPACE_CREATED`` from this peer class would seat an unrelated one);
#: ``SPACE_SUBSCRIBER_KEY_HANDOFF`` (a GFS relay frame, never a §24.11
#: envelope); and the four mesh-routing types above.


#: Space-content **writes** — every event type that mutates content inside
#: a space (create, update or delete of a post, comment, page, task, poll,
#: sticky, calendar event, RSVP, schedule, gallery item, bazaar listing /
#: bid / offer, zone, location pin, or the media bytes a post references).
#:
#: This is the vocabulary the §24.11 ``check_space_writer`` step refuses
#: from a household that holds only **Follower** seats in the space
#: (``space_remote_members.role = 'subscriber'``, migration 0054). Such a
#: household is a full participant on the transport — it sits in
#: ``space_instances``, so ``broadcast_to_space_members`` delivers the
#: content stream and the epoch content key reaches it — which means it
#: holds everything needed to produce a perfectly well-formed, correctly
#: signed write. The seat the RECEIVER holds for the signed
#: ``from_instance`` is the only authority that matters, and it is read on
#: every receiving household, not just the host: space content fans out
#: peer-to-peer from the ORIGINATING household
#: (:meth:`FederationService.broadcast_to_space_members`), so a member
#: household is a first-class enforcement point, not a mirror of one.
#:
#: The classification is **exhaustive over the enum**: every ``SPACE_*`` /
#: ``BAZAAR_*`` type is either here or in
#: :data:`SPACE_READER_EVENT_TYPES`, pinned by a test that enumerates
#: ``set(FederationEventType)`` (the same default-deny idiom as
#: :data:`SPACE_SESSION_ALLOWED_EVENT_TYPES`). A new space event type
#: fails that test until somebody classifies it on purpose, so the default
#: for anything unclassified is "somebody must decide", never "a follower
#: may send it".
SPACE_WRITE_EVENT_TYPES: frozenset[FederationEventType] = frozenset(
    {
        # ── Posts, comments and the bytes they reference ──
        FederationEventType.SPACE_POST_CREATED,
        FederationEventType.SPACE_POST_UPDATED,
        FederationEventType.SPACE_POST_DELETED,
        #: The WebP / WebM / file bytes a post or gallery item points at.
        #: A write: it lands rows in the local media store, and shipping
        #: it is how a sender puts content bytes on our disk.
        FederationEventType.SPACE_MEDIA_BLOB,
        FederationEventType.SPACE_COMMENT_CREATED,
        FederationEventType.SPACE_COMMENT_UPDATED,
        FederationEventType.SPACE_COMMENT_DELETED,
        # ── Pages / tasks / polls / stickies ──
        FederationEventType.SPACE_PAGE_CREATED,
        FederationEventType.SPACE_PAGE_UPDATED,
        FederationEventType.SPACE_PAGE_DELETED,
        FederationEventType.SPACE_TASK_CREATED,
        FederationEventType.SPACE_TASK_UPDATED,
        FederationEventType.SPACE_TASK_DELETED,
        FederationEventType.SPACE_POLL_CREATED,
        FederationEventType.SPACE_POLL_VOTE_CAST,
        FederationEventType.SPACE_POLL_CLOSED,
        FederationEventType.SPACE_STICKY_CREATED,
        FederationEventType.SPACE_STICKY_UPDATED,
        FederationEventType.SPACE_STICKY_DELETED,
        # ── Calendar, RSVPs and scheduling polls ──
        FederationEventType.SPACE_CALENDAR_EVENT_CREATED,
        FederationEventType.SPACE_CALENDAR_EVENT_UPDATED,
        FederationEventType.SPACE_CALENDAR_EVENT_DELETED,
        FederationEventType.SPACE_RSVP_UPDATED,
        FederationEventType.SPACE_RSVP_DELETED,
        FederationEventType.SPACE_SCHEDULE_CREATED,
        FederationEventType.SPACE_SCHEDULE_RESPONSE_UPDATED,
        FederationEventType.SPACE_SCHEDULE_FINALIZED,
        # ── Gallery ──
        FederationEventType.SPACE_GALLERY_ITEM_CREATED,
        FederationEventType.SPACE_GALLERY_ITEM_DELETED,
        # ── Bazaar (listings live in a space like any other content) ──
        FederationEventType.BAZAAR_LISTING_CREATED,
        FederationEventType.BAZAAR_LISTING_UPDATED,
        FederationEventType.BAZAAR_BID_PLACED,
        FederationEventType.BAZAAR_OFFER_ACCEPTED,
        # ── Map: shared location pins and zones ──
        FederationEventType.SPACE_LOCATION_UPDATED,
        FederationEventType.SPACE_ZONE_UPSERTED,
        FederationEventType.SPACE_ZONE_DELETED,
    }
)

#: The explicit complement of :data:`SPACE_WRITE_EVENT_TYPES` over the
#: ``SPACE_*`` / ``BAZAAR_*`` families: space vocabulary a **reader**
#: household may legitimately send.
#:
#: Being listed here is not an authorization — most of these carry their
#: own: the roster / config family is authority-signed and verified
#: against ``spaces.identity_public_key``, the admin-action family is
#: re-decided by the host, ``SPACE_REPORT`` is a report *about* content
#: rather than content, and the sync / key-exchange / routing families are
#: transport machinery every seated household needs. What the list means
#: is "the Follower gate does not fire for this type", and the reason is
#: recorded here so the decision is reviewable rather than implied by an
#: omission.
SPACE_READER_EVENT_TYPES: frozenset[FederationEventType] = frozenset(
    {
        # ── Space structure / config (authority-signed, verified on apply) ──
        FederationEventType.SPACE_CREATED,
        FederationEventType.SPACE_AGE_GATE_UPDATED,
        FederationEventType.SPACE_CONFIG_CHANGED,
        FederationEventType.SPACE_CONFIG_CATCH_UP,
        FederationEventType.SPACE_DISSOLVED,
        FederationEventType.SPACE_INSTANCE_LEFT,
        # ── Roster / membership (authority-signed; a leave is a reader's
        #    own right, and a kick is re-decided by the host) ──
        FederationEventType.SPACE_MEMBER_JOINED,
        FederationEventType.SPACE_MEMBER_LEFT,
        FederationEventType.SPACE_MEMBER_BANNED,
        FederationEventType.SPACE_MEMBER_UNBANNED,
        FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
        FederationEventType.SPACE_MEMBER_PROFILE_UPDATED,
        FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
        FederationEventType.SPACE_REMOTE_ADMIN_KICK,
        FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
        FederationEventType.SPACE_ADMIN_PROPOSAL_UPDATED,
        # ── Invites / joins (how a household gets a seat at all) ──
        FederationEventType.SPACE_INVITE,
        FederationEventType.SPACE_INVITE_VIA,
        FederationEventType.SPACE_ACCEPT,
        FederationEventType.SPACE_JOIN_REQUEST,
        FederationEventType.SPACE_JOIN_REQUEST_VIA,
        FederationEventType.SPACE_JOIN_REQUEST_REPLY_VIA,
        FederationEventType.SPACE_JOIN_REQUEST_APPROVED,
        FederationEventType.SPACE_JOIN_REQUEST_DENIED,
        FederationEventType.SPACE_JOIN_REQUEST_EXPIRED,
        FederationEventType.SPACE_JOIN_REQUEST_WITHDRAWN,
        FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
        FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
        FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY,
        FederationEventType.SPACE_INVITE_BOOTSTRAP_REDEEM,
        FederationEventType.SPACE_INVITE_BOOTSTRAP_REDEEM_ACK,
        FederationEventType.SPACE_INVITE_BOOTSTRAP_REDEEM_DENY,
        FederationEventType.SPACE_PRIVATE_INVITE,
        FederationEventType.SPACE_PRIVATE_INVITE_ACCEPT,
        FederationEventType.SPACE_PRIVATE_INVITE_DECLINE,
        # ── Mesh routing. SPACE_ROUTED is a wrapper, never content: its
        #    INNER event is re-gated after the unwrap
        #    (``SpaceRoutedHandler._unwrap_and_dispatch``), which is the
        #    only reason it can sit on this side of the line. ──
        FederationEventType.SPACE_ROUTED,
        FederationEventType.SPACE_FIND_ROUTE,
        FederationEventType.SPACE_ROUTE_FOUND,
        FederationEventType.SPACE_ROUTE_STALE,
        # ── Crypto: epoch keys and session teardown ──
        FederationEventType.SPACE_KEY_EXCHANGE,
        FederationEventType.SPACE_KEY_EXCHANGE_ACK,
        FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
        FederationEventType.SPACE_ADMIN_KEY_SHARE,
        FederationEventType.SPACE_SUBSCRIBER_KEY_HANDOFF,
        FederationEventType.SPACE_SESSION_CLEANUP,
        # ── §25.6 catch-up sync. A reader syncs the same way a member
        #    does; what the chunks may CONTAIN is the exporter's call on
        #    the sending side, not this gate's. ──
        FederationEventType.SPACE_SYNC_BEGIN,
        FederationEventType.SPACE_SYNC_CHUNK,
        FederationEventType.SPACE_SYNC_CHUNK_ACK,
        FederationEventType.SPACE_SYNC_RESUME,
        FederationEventType.SPACE_SYNC_COMPLETE,
        FederationEventType.SPACE_SYNC_OFFER,
        FederationEventType.SPACE_SYNC_ANSWER,
        FederationEventType.SPACE_SYNC_ICE,
        FederationEventType.SPACE_SYNC_DIRECT_READY,
        FederationEventType.SPACE_SYNC_DIRECT_FAILED,
        FederationEventType.SPACE_SYNC_REQUEST_MORE,
        FederationEventType.SPACE_SYNC_REJECTED,
        FederationEventType.SPACE_PARTITION_GAP,
        # ── Directory + moderation ──
        FederationEventType.SPACE_DIRECTORY_SYNC,
        #: A report is *about* content, not content: the whole point is
        #: that somebody who can only read still has a way to flag what
        #: they read.
        FederationEventType.SPACE_REPORT,
    }
)


# ─── Pairing state machine (§11) ──────────────────────────────────────────


class PairingStatus(str, enum.Enum):
    PENDING_SENT = "pending_sent"
    PENDING_RECEIVED = "pending_received"
    CONFIRMED = "confirmed"
    UNPAIRING = "unpairing"


class InstanceSource(str, enum.Enum):
    """How a remote_instances row came into existence.

    ``manual`` — classic QR-based pairing (§11).
    ``space_session`` — derived at space-join time from the admin key share
    flow, without ever exchanging a full pairing handshake (§13).
    """

    MANUAL = "manual"
    SPACE_SESSION = "space_session"


# ─── RemoteInstance (§4.1 / remote_instances table) ───────────────────────


@dataclass(slots=True, frozen=True)
class RemoteInstance:
    """A peer instance we know about.

    Mirrors the ``remote_instances`` row. All keys are stored KEK-encrypted at
    rest on the DB layer; the :class:`KeyManager` decrypts before handing the
    value to this dataclass.
    """

    id: str  # 32-char instance_id (derive_instance_id)
    display_name: str
    remote_identity_pk: str  # 64 hex chars — Ed25519 public key
    key_self_to_remote: str  # AES-256-GCM session key (ciphertext)
    key_remote_to_self: str  # AES-256-GCM session key (ciphertext)
    remote_inbox_url: str
    local_inbox_id: str
    status: PairingStatus = PairingStatus.CONFIRMED
    intro_relay_enabled: bool = True
    source: InstanceSource = InstanceSource.MANUAL
    #: Monotonic protocol version the peer last advertised via
    #: :data:`FederationEventType.INSTANCE_CAPABILITIES_UPDATED`. Senders
    #: gate optional fields with
    #: ``FederationService.peer_supports(instance_id, min_version=N)`` —
    #: see :mod:`socialhome.domain.federation_capabilities` for the
    #: known thresholds. Defaults to ``1`` (oldest known wire) so a
    #: peer that hasn't sent an announcement yet is treated as the
    #: most conservative shape.
    proto_version: int = 1
    # Post-quantum identity material advertised by the peer during
    # pairing. ``remote_pq_algorithm`` is non-None when the peer supports
    # a PQ suite; ``remote_pq_identity_pk`` is the hex-encoded PQ public
    # key. Both stay ``None`` on classical peers.
    remote_pq_algorithm: str | None = None
    remote_pq_identity_pk: str | None = None
    #: Per-peer negotiated wire suite — see ``federation.crypto_suite``.
    #: Default ``"ed25519"`` matches the classical (and safest-floor)
    #: behaviour when the remote side doesn't advertise a hybrid key.
    sig_suite: str = "ed25519"
    #: Who introduced this peer. For an auto-paired peer (§11 trust
    #: relay) that is the introducer's ``instance_id``. For a
    #: :data:`InstanceSource.SPACE_SESSION` row — a household seated from
    #: an invite link, which has no address at all — it is instead the
    #: **base URL of the connection server that introduced the pair**,
    #: because that relay is the only way to reach them
    #: (:mod:`socialhome.federation.gfs_relay_transport`). Deliberately
    #: the same column: both values answer "who do I go through to reach
    #: this peer", the two sources never mix on one row, and nothing reads
    #: ``relay_via`` as an instance id without first excluding
    #: ``space_session`` (``auto_pair_coordinator`` refuses those rows as
    #: introducers outright).
    relay_via: str | None = None
    #: The peer's static X25519 **key-wrap** public key, hex — verified
    #: bound to :attr:`remote_identity_pk` at seat time via
    #: :func:`~socialhome.federation.keywrap_seal.verify_keywrap_binding`.
    #: Set only on :data:`InstanceSource.SPACE_SESSION` rows, where it is
    #: what every outbound envelope is sealed to before the connection
    #: server carries it. ``None`` on every ordinary peer (they have an
    #: address; nothing needs sealing).
    remote_keywrap_pk: str | None = None
    home_lat: float | None = None  # 4dp-truncated
    home_lon: float | None = None
    paired_at: str | None = None
    created_at: str | None = None
    last_reachable_at: str | None = None
    unreachable_since: str | None = None
    #: ISO 8601 UTC timestamp of when this peer last advertised its
    #: capabilities (``INSTANCE_CAPABILITIES_UPDATED``). ``None`` means the
    #: peer has never advertised — it's paired but mid-first-handshake, so
    #: its ``proto_version`` is still the conservative default rather than a
    #: confirmed value. Surfaced in the admin federation-compatibility panel.
    capabilities_seen_at: str | None = None
    #: Local-only alias the admin set in the UI ("Brother's house").
    #: Never federated — purely a display string for this household.
    #: When ``None`` the SPA falls back to :attr:`display_name`.
    local_alias: str | None = None
    #: Whether to share this household's home location with this peer.
    #: Never federated on its own — purely a local opt-in/opt-out toggle.
    #: Defaults to ``True`` so existing paired peers keep the pre-toggle
    #: behaviour (share coordinates when the peer supports §4.5).
    share_home: bool = True

    def is_reachable(self) -> bool:
        return self.unreachable_since is None

    @property
    def effective_display_name(self) -> str:
        """The user-facing name: local alias if set, else the peer's
        federated display name. Use this everywhere the connection
        is rendered to the household — list views, the friends
        dashboard, the DM picker."""
        alias = (self.local_alias or "").strip()
        return alias or self.display_name


# ─── Space version compatibility (#319 ¶5) ────────────────────────────────


@dataclass(slots=True, frozen=True)
class BehindMember:
    """A member household whose protocol version lags behind ours, lacking
    one or more shared-space features."""

    instance_id: str
    display_name: str
    proto_version: int
    lacking_features: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class SpaceVersionCompat:
    """Per-space protocol-version compatibility of member households.

    Powers the space-admin banner ("these space features won't work until
    member household X upgrades"). Households that have never advertised
    capabilities (mid-handshake) are EXCLUDED from every field — counting
    them would phantom-nag with the conservative default version.
    """

    ours: int
    #: Min proto_version across member households whose capabilities are
    #: known; ``None`` when there are no known remote members.
    min_member_proto_version: int | None
    #: Space features unavailable because the weakest known member lacks them.
    lagging_features: tuple[str, ...]
    behind_members: tuple[BehindMember, ...]


# ─── Wire envelope (§24.11) ───────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class FederationEnvelope:
    """Cleartext routing envelope of a federation message.

    Only routing metadata is in the clear. The ``encrypted_payload`` field
    carries the AES-256-GCM ciphertext produced from the directional
    session key (§25.8.20–21).
    """

    msg_id: str  # UUID; used by ReplayCache
    event_type: FederationEventType
    from_instance: str  # instance_id
    to_instance: str  # instance_id
    timestamp: str  # ISO-8601 UTC
    encrypted_payload: str  # b64url(nonce:ciphertext:tag)
    signature: str  # b64url Ed25519 signature
    space_id: str | None = None  # space-scoped events only
    epoch: int | None = None  # encryption epoch (§25.8.20)
    proto_version: int = 1


@dataclass(slots=True, frozen=True)
class DecryptedPayload:
    """The decrypted inner JSON of a federation message.

    Contains the ``event_type`` repeated (for matching) and the full
    event-specific payload dict.
    """

    event_type: FederationEventType
    payload: dict


# ─── Pairing in-flight state ──────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class PairingSession:
    """An in-progress pairing handshake (§11)."""

    token: str  # URL-safe random token from QR
    own_identity_pk: str  # 64 hex
    own_dh_pk: str  # 64 hex (X25519)
    own_dh_sk: str  # 64 hex (X25519) — kept until confirm
    inbox_url: str
    # Our own per-peer inbox secret, generated at initiate/accept time.
    # Baked into the URL the peer POSTs to (`inbox_url` ends in this id).
    # Moves onto ``RemoteInstance.local_inbox_id`` when the pair confirms.
    own_local_inbox_id: str
    peer_identity_pk: str | None = None
    peer_dh_pk: str | None = None
    peer_inbox_url: str | None = None
    intro_note: str | None = None
    relay_via: str | None = None
    verification_code: str | None = None  # 6-digit SAS
    issued_at: str | None = None
    expires_at: str | None = None
    status: PairingStatus = PairingStatus.PENDING_SENT


# ─── Broadcast / delivery results ─────────────────────────────────────────


#: :attr:`DeliveryResult.error` value meaning "we never put a probe on the
#: wire": mesh route discovery is inside its negative cooldown for this
#: target, so the send failed WITHOUT testing whether the route works. It is
#: deliberately distinct from the generic ``"no_route"`` (we probed, and the
#: flood found nothing): the cooldown is a transient window a caller can wait
#: out, and a caller that conflates the two burns its whole retry budget in
#: milliseconds against a route that is seconds from warming up.
DELIVERY_ERROR_ROUTE_COOLDOWN: str = "route_cooldown"

#: :attr:`DeliveryResult.error` value meaning "the connection-server relay
#: answered 429". Like :data:`DELIVERY_ERROR_ROUTE_COOLDOWN` this is a
#: *waitable* window, not a broken path: the relay is up, the envelope is
#: well-formed, and the same send succeeds once the sliding window drains
#: (:data:`~socialhome.federation.invite_bootstrap.RELAY_THROTTLE_COOLDOWN_S`
#: is how long to wait, carried on :attr:`DeliveryResult.retry_after_s`).
#: A caller that reads it as terminal abandons a space-sync catch-up over a
#: few seconds of back-pressure, which is the bug it exists to prevent.
#: The envelope is still parked in the durable outbox — a throttle costs a
#: delay, never an event.
DELIVERY_ERROR_RELAY_THROTTLED: str = "relay_throttled"

#: :attr:`DeliveryResult.error` value meaning "this frame structurally
#: cannot ride the connection-server relay". The relay body cap is a fixed
#: 320 KiB and a media chunk is megabytes, so the refusal is *deterministic*
#: — the identical envelope fails identically on every attempt. It is the
#: opposite of :data:`DELIVERY_ERROR_RELAY_THROTTLED`: never retry it, or the
#: outbox spends its whole attempt budget re-proving the same arithmetic and
#: then reports a transient-looking failure for a permanent condition.
DELIVERY_ERROR_RELAY_TOO_LARGE: str = "relay_too_large"


#: :attr:`DeliveryResult.error` value meaning "enqueued to the durable
#: ``federation_outbox`` for retry" — NOT a loss. ``send_event`` emits it
#: only AFTER the envelope is queued, so the outbox drainer redelivers once
#: the direct peer is reachable again. The literal stays ``"delivery_failed"``
#: for log consumers that already match it; the name carries the semantics.
#: It is the one ``ok=False`` reason that is *not* terminal — the mesh path
#: (``no_route`` / ``unknown_instance`` / ``not_confirmed`` /
#: ``routed_send_failed`` / :data:`DELIVERY_ERROR_ROUTE_COOLDOWN`) has no
#: outbox, so every other reason is a single-attempt, permanent loss.
DELIVERY_ERROR_QUEUED: str = "delivery_failed"


@dataclass(slots=True, frozen=True)
class DeliveryResult:
    """Result of sending a single federation message to a single peer."""

    instance_id: str
    ok: bool
    status_code: int | None = None
    error: str | None = None
    #: Which transport carried it — ``"rtc"``, ``"https"`` or
    #: ``"gfs_relay"``; ``None`` when no transport facade was attached.
    #:
    #: **``via="gfs_relay"`` with ``ok=True`` means ACCEPTED, not
    #: delivered.** The connection server answers a uniform ``202`` to
    #: every well-formed envelope (any other answer would be a
    #: presence oracle), so a household that is offline — or not a
    #: client of that server at all — produces exactly the same result
    #: as one that took the frame off its live socket. What the ``202``
    #: buys is the queue: the blob waits up to
    #: ``ENVELOPE_QUEUE_TTL_SECONDS`` and drains on the recipient's next
    #: hello. Read a ``gfs_relay`` success as "handed to the relay",
    #: never as "the other household has it", and don't present it to an
    #: operator as confirmed delivery.
    via: str | None = None
    #: Seconds the caller should wait before retrying, when the sender knows.
    #: Set on the :data:`DELIVERY_ERROR_ROUTE_COOLDOWN` path (the remaining
    #: negative-cooldown window); ``None`` everywhere else.
    retry_after_s: float | None = None


@dataclass(slots=True, frozen=True)
class BroadcastResult:
    """Aggregate result of fan-out to many peers."""

    attempted: int
    succeeded: int
    failed: int
    results: tuple[DeliveryResult, ...] = field(default_factory=tuple)

    @property
    def all_ok(self) -> bool:
        return self.failed == 0 and self.attempted > 0

    @property
    def terminal_failures(self) -> tuple[DeliveryResult, ...]:
        """The ``ok=False`` results that are genuine losses.

        Excludes :data:`DELIVERY_ERROR_QUEUED` — a direct-peer send that
        ``send_event`` already parked in the durable outbox and that
        redelivers on its own. What remains is the mesh-path subset (no
        outbox, single attempt), i.e. what a caller should warn about.
        ``failed`` keeps counting every ``ok=False`` result regardless.
        """
        return tuple(
            r for r in self.results if not r.ok and r.error != DELIVERY_ERROR_QUEUED
        )


# ─── High-level typed inbound event ───────────────────────────────────────


@dataclass(slots=True, frozen=True)
class FederationEvent:
    """A fully-validated inbound event ready for the service layer.

    Produced by the federation service after envelope parse → timestamp skew
    check → instance lookup → ban check → Ed25519 verify → replay check →
    decrypt. Anything downstream of that validation sees this type, never the
    raw envelope.
    """

    msg_id: str
    event_type: FederationEventType
    from_instance: str
    to_instance: str
    timestamp: str
    payload: dict
    space_id: str | None = None
    epoch: int | None = None
    #: Source-route the event traversed when it arrived via
    #: :data:`FederationEventType.SPACE_ROUTED`. ``None`` for any event
    #: delivered directly (the common case). When set, this is the
    #: full path from origin (``path[0]``) to target (``path[-1]``,
    #: which equals ``to_instance`` on the unwrap step) — handlers
    #: that ship a response (ACK / DENY / …) read it to ship the
    #: reply back via the reverse path so both legs use mesh routing.
    #:
    #: Wire-invisible: only ever populated by
    #: :class:`socialhome.federation.routed_envelope.SpaceRoutedHandler`
    #: when it unwraps an inbound SPACE_ROUTED envelope and
    #: synthesises a fresh :class:`FederationEvent` for the inner
    #: event_type. The inbound validator (which constructs
    #: :class:`FederationEvent` from the wire envelope) never sets
    #: it — there is no on-disk / on-wire field for ``routed_path``.
    routed_path: list[str] | None = None
    #: Forward-leg ``route_id`` nonce when this event arrived via
    #: :data:`FederationEventType.SPACE_ROUTED`. ``None`` for direct
    #: deliveries. Set together with ``routed_path`` by the unwrap
    #: step so a handler that wants to ship an encrypted reply can
    #: hand it back to
    #: :meth:`SpaceRoutedHandler.send_routed_reply` — the reply leg
    #: re-uses the forward leg's ephemeral keypairs keyed on the same
    #: ``route_id``. Wire-invisible; never persisted.
    routed_route_id: str | None = None
    #: Decrypted raw media bytes when this event arrived as a binary frame
    #: on the ``fed-media-v1`` DataChannel (§24.12). ``None`` for every
    #: event delivered as JSON (the common case) — handlers then fall back
    #: to the base64 ``bytes_b64`` field in :attr:`payload`. Set by
    #: :meth:`FederationService.handle_inbound_media_frame` after the
    #: §24.11 pipeline validates the wrapping envelope and the per-chunk
    #: ``chunk_sha256`` binding checks out, so a media handler that reads
    #: this sees bytes already authenticated + integrity-checked.
    #:
    #: Wire-invisible in the same sense as :attr:`routed_path`: it is
    #: never a field on the JSON envelope. The binary transport carries
    #: the bytes alongside (not inside) the signed envelope and the
    #: receiver attaches them here before dispatch.
    media_bytes: bytes | None = None


# ─── GFS connection types (§24 — Global Federation Server) ──────────────


@dataclass(slots=True, frozen=True)
class GfsConnection:
    """A paired Global Federation Server connection."""

    id: str
    gfs_instance_id: str
    display_name: str
    public_key: str
    inbox_url: str
    status: str  # pending | active | suspended
    paired_at: str
    created_at: str | None = None


@dataclass(slots=True, frozen=True)
class GfsSpacePublication:
    """A space published to a GFS."""

    space_id: str
    gfs_connection_id: str
    published_at: str
    status: str = "active"  # active | pending | banned (GFS-returned)
