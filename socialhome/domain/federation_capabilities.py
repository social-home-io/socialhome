"""Federation protocol version — sender-side gating for new fields.

Every peer carries a single integer ``proto_version`` on its
``remote_instances`` row. It is monotonically bumped each release
that adds a federation surface (a new event type, a new payload
field whose default-if-missing would be wrong, a new wire shape).
Senders gate optional fields with
``federation_service.peer_supports(instance_id, min_version=N)``
before including them, so a v1 receiver never sees a v2 field.

Adding a new federation surface:

1. Bump :data:`OURS` to the next integer.
2. Add a named constant on :class:`FederationCapability` so callers can
   reference the new version by intent (``MIN_FOR_OCCURRENCE_OVERRIDE``)
   instead of a magic number.
3. Wherever the new surface produces an outbound, gate it on
   ``peer_supports(..., min_version=FederationCapability.X)`` and pick
   a degraded fallback for older peers (or skip the send entirely if no
   safe fallback exists).
4. Update :file:`docs/protocol/capabilities.md` with what v_N adds and
   what an older peer should fall back to.

Adding a *flag set* on top of this (per-peer feature opt-in / opt-out)
is deliberately deferred — it only earns its complexity once we have
selective deployments, forks, or asymmetric send/receive support, none
of which exist in v1. A monotonic version covers every case we have
today; flags can layer on later as a backward-compatible
``ALTER TABLE remote_instances ADD COLUMN features TEXT`` if real
operational evidence forces it.

The ``proto_version`` integer is exchanged through
:data:`FederationEventType.INSTANCE_CAPABILITIES_UPDATED` at startup
(see :class:`CapabilitiesOutbound`); the receiving peer's row picks up
the new value, and future ``peer_supports`` calls return ``True`` for
versions at or below it.
"""

from __future__ import annotations


#: The protocol version this build advertises to peers. Bump on every
#: release that ships a federation surface that older peers cannot
#: parse fail-soft (or whose missing-default would silently produce a
#: wrong-but-not-crashing state).
#:
#: History:
#:
#: * **v1** — initial wire (every event type up to the calendar-tz fix).
#: * **v2** — events may carry an IANA ``tz`` field. Old (v1) peers
#:   tolerate it because the receiver defaults ``tz`` to ``"UTC"``,
#:   so the bump is informational; future v3+ features that aren't
#:   fail-soft will be the first to actually flip behaviour via
#:   :func:`FederationService.peer_supports`.
#: * **v3** — DM media (image / video / file). ``DM_MESSAGE`` payloads
#:   may now carry ``file_name``, ``mime_type``, ``file_size_bytes``,
#:   ``media_blob_id``, and (for cross-household sends) a tiny
#:   ``preview_bytes_b64`` thumbnail / poster / glyph that the
#:   receiver renders immediately while a follow-up
#:   :data:`FederationEventType.DM_MEDIA_BLOB` ships the full bytes.
#:   Issue #319 paragraph 5 policy: **fallback**. Sub-v_3 peers
#:   receive a synthesised ``type='text'`` message — see
#:   :mod:`socialhome.federation.compat.dm_media_v3` for the
#:   transform.
#: * **v4** — §11 peer-pairing bootstrap moves off the dedicated
#:   ``/api/pairing/peer-{accept,confirm}`` routes onto the federation
#:   inbox URL as :data:`FederationEventType.PAIRING_PEER_ACCEPT` /
#:   :data:`FederationEventType.PAIRING_PEER_CONFIRM`. Required because
#:   HA / HAOS deployments only proxy the federation inbox path —
#:   remote peers cannot reach any other SH path through Supervisor
#:   Ingress. **No fallback.** Sub-v_4 peers cannot pair under HAOS
#:   today (the bug this version fixes); standalone-to-standalone
#:   pairs with one upgraded side and one sub-v_4 side will fail too
#:   and need both sides upgraded.
#: * **v5** — :data:`FederationEventType.LOCAL_HOME_LOCATION_CHANGED`.
#:   Senders use it to push their HA-sourced home coordinates to every
#:   confirmed peer on every change. Sub-v_5 receivers reject the
#:   unknown event_type with 400 (the §24.11 pipeline's standard
#:   "Unknown event_type" path), which our outbox now treats as a
#:   PERMANENT failure and drops cleanly — so the safety net is the
#:   outbox-drop fix from PR #354. No user-visible regression for
#:   sub-v_5 peers; they just don't get the map update.
#: * **v6** — cross-instance space-invite redeem + federation mesh
#:   routing. Two related additions shipping under the same
#:   release:
#:
#:   * :data:`FederationEventType.SPACE_INVITE_TOKEN_REDEEM` family
#:     (``_REDEEM`` / ``_REDEEM_ACK`` / ``_REDEEM_DENY``). Receiver-
#:     initiated cross-instance redeem for ``socialhome://invite#…``
#:     codes — a paste on the receiver's instance routes to the
#:     issuer over federation and seats the receiver as a remote
#:     space member.
#:   * Generic mesh-routing envelope :data:`FederationEventType
#:     .SPACE_ROUTED` (PR 2) — wraps any inner event so it can
#:     traverse a chain of confirmed peers to reach a target the
#:     origin isn't directly paired with. Same envelope shape works
#:     for invite redemption, space posts, and any future event
#:     type without inventing new ``_ROUTED`` variants per case.
#:   * Route-discovery primitives :data:`FederationEventType
#:     .SPACE_FIND_ROUTE` + :data:`SPACE_ROUTE_FOUND` (PR 2). Per-
#:     target probe + response so the origin can pick a hop-count-
#:     minimal path through the federation network (default
#:     ``max_hops=3``, configurable per deployment).
#:
#:   **No fallback.** Sub-v_6 issuers can't process the redeem;
#:   the receiver-side coordinator gates the outbound on
#:   ``peer_supports(min_version=6)`` and 422s the SPA with a clear
#:   "issuer needs to upgrade" message rather than wasting the 10 s
#:   timeout window. Same gate applies to the mesh-routing
#:   envelopes once PR 2 lands.
#: * **v9** — :data:`FederationEventType.SPACE_REMOTE_ADMIN_KICK`
#:   shipped (#114 phase 2, PR #435). A remote admin can now actually
#:   kick a member of a space hosted elsewhere — host validates the
#:   actor's role from ``space_remote_members.role`` before
#:   dispatching the kick. **Force-upgrade.** Sub-v_9 hosts silently
#:   drop the command (no handler registered), so the kick appears
#:   to succeed on the actor's side but the host never applies it —
#:   the admin's intent is lost. Operators must upgrade hosts before
#:   members try cross-household admin actions.
#: * **v8** — :data:`FederationEventType.SPACE_MEMBER_ROLE_CHANGED`
#:   shipped (#114, PR #434). Host emits this every time an owner
#:   promotes / demotes a remote member's role; receivers update
#:   their local view of the roster (``space_members.role`` for the
#:   affected user's own household, ``space_remote_members.role``
#:   on witnesses). Sub-v_8 peers silently drop the event — their
#:   member-list SPA stays at the pre-change role until the user
#:   re-runs §25.6 sync or accepts a fresh invite. **Best effort.**
#:   Forward-compatible because the SPA never depends on a role
#:   it didn't see propagate; the worst-case is a stale badge.
#: * **v7** — :data:`FederationEventType.SPACE_KEY_EXCHANGE_REKEY`
#:   is now actually shipped (#121, PR #432). Every member-removal
#:   path on the host — local kick, ban, §D1b cross-household kick —
#:   rotates the space epoch and ships the new AES-256 content key
#:   to every remaining member household so a removed member can't
#:   keep decrypting future content with their cached at-rest key.
#:   The event_type itself was declared on the enum since v_1, but
#:   no sender emitted it and no receiver handled it. Sub-v_7
#:   receivers silently ignore the new outbound (event-type
#:   dispatch is best-effort — see
#:   :class:`EventDispatchRegistry.dispatch`); the resulting
#:   degradation is that they fail to decrypt every subsequent
#:   ``SPACE_POST_CREATED`` from this host until they upgrade and
#:   re-sync. **No fallback** — gracefully ignoring the rekey is a
#:   forward-secrecy violation, so we'd rather fail loud. Operators
#:   should expect to upgrade member households together with the
#:   host.
#: * **v10** — :data:`FederationEventType.BAZAAR_LISTING_CREATED` ships
#:   the full :class:`BazaarListing` payload (mode, price, photos,
#:   status, …) so remote household members see what's actually for
#:   sale, not just the caption on the wrapper
#:   ``PostType.BAZAAR`` post that already federated as
#:   ``SPACE_POST_CREATED``. Image bytes ride the existing space
#:   media outbox (correlation_id = listing.post_id). **Best
#:   effort.** Sub-v_10 peers silently drop the event — the wrapper
#:   post still federates, so the recipient sees the caption
#:   ("🛍 Title") but nothing else (today's behaviour). Operators
#:   who want bazaar visibility should upgrade member households
#:   together with the seller's.
#: * **v11** — :data:`FederationEventType.BAZAAR_LISTING_UPDATED` ships
#:   status-only mutations on an existing listing (SOLD / EXPIRED /
#:   CANCELLED). Without it, a remote member sees a stale "active"
#:   listing until the next §25.6 catch-up sync runs (or the seller
#:   re-publishes via ``BAZAAR_LISTING_CREATED``). **Best effort.**
#:   Sub-v_11 peers silently drop; same upgrade story as v_10.
#: * **v12** — F7: cross-household bazaar bids + offer acceptance.
#:   :data:`BAZAAR_BID_PLACED` lets a remote bidder's instance push
#:   bids to the seller's host (and every other member's view);
#:   :data:`BAZAAR_OFFER_ACCEPTED` propagates the seller's acceptance
#:   back to the bidder + every other member. **Best effort.**
#:   Sub-v_12 peers see local bids only — remote bids never reach
#:   the seller's DB and the seller can't accept them. Operators
#:   wanting cross-household bazaar transactions should upgrade
#:   member households alongside sellers.
#: * **v_13** (2026-05-25) — :data:`FederationEventType.SPACE_SYNC_CHUNK`
#:   federation transport for §25.6 chunked sync. Adds the HTTPS chunk
#:   path that fires when the WebRTC handshake never completes (Pascal
#:   saw ``SPACE_SYNC_DIRECT_FAILED`` between two paired instances after
#:   restart; root cause was the requester never emitting
#:   ``SPACE_SYNC_DIRECT_READY`` AND the relay-fallback BEGIN being
#:   silently ignored by the provider). Sub-v_13 providers don't know
#:   how to handle ``prefer_direct=False`` — the requester gates the
#:   relay retry on the peer's advertised version so older peers stick
#:   to direct-only (which still works on the local LAN and behind
#:   modest NATs; cross-NAT fail-soft for older peers becomes "no
#:   sync" rather than silently broken).
#: * **v_14** (2026-05-31) — dedicated binary media DataChannel
#:   (``fed-media-v1``). A CONFIRMED direct peer that advertises v_14+
#:   receives DM + space media as binary frames (no base64) on a second
#:   DataChannel multiplexed on the same federation PeerConnection, so
#:   bulk media stops head-of-line-blocking latency-sensitive control
#:   events on ``fed-v1`` and drops the ~37 % base64 tax. **Fallback.**
#:   Sub-v_14 peers (and any non-CONFIRMED / mesh-only space member —
#:   the binary channel is point-to-point only) keep receiving the
#:   existing JSON :data:`FederationEventType.DM_MEDIA_BLOB` /
#:   :data:`SPACE_MEDIA_BLOB` events over ``fed-v1`` / HTTPS / SPACE_ROUTED.
#:   The sender chooses per-peer via
#:   :meth:`FederationService.peer_supports` so there is no user-visible
#:   regression — only a throughput improvement when both sides are v_14+.
#: * **v_15** (2026-06-01) — generic cross-household admin actions
#:   (:data:`FederationEventType.SPACE_REMOTE_ADMIN_ACTION`). Generalises
#:   the v_9 kick: a remote admin on household A can now run *any*
#:   admin-level mutation on a space hosted on household B — config edit
#:   (name / emoji / features / join-mode / retention), ban / unban,
#:   archive / unarchive. The remote admin's :class:`SpaceService`
#:   forwards an intent envelope to the host; the host re-validates the
#:   actor's ``space_remote_members.role == ADMIN`` and runs the real
#:   host-side method as the owner, so the result federates back to every
#:   member through the normal outbounds (``SPACE_CONFIG_CHANGED`` etc.).
#:   Owner-only actions (dissolve, transfer-ownership, role assignment)
#:   stay host-local and are *not* forwardable. **Force-upgrade.** Sub-v_15
#:   hosts have no handler, so the action would be silently dropped — the
#:   sender gates on :data:`FederationCapability.MIN_FOR_REMOTE_ADMIN_ACTION`
#:   and raises :class:`SpacePermissionError` ("host doesn't support remote
#:   admin actions yet") instead of mutating the local stub, so the actor
#:   gets a clear error rather than a silent divergence. Operators must
#:   upgrade hosts together with admins on other households.
#: * **v_16** (2026-06-01) — multi-admin approval (quorum) for critical
#:   space actions. Dissolving a space and changing its publication tier
#:   (``space_type``) now require a *majority* of the space's admins to
#:   approve — the owner included. A remote admin proposes / votes via the
#:   ``SPACE_REMOTE_ADMIN_ACTION`` envelope (``propose`` / ``vote`` verbs);
#:   the host mirrors the open proposal + tally onto admin households with
#:   :data:`FederationEventType.SPACE_ADMIN_PROPOSAL_UPDATED` so their SPA
#:   can render it and vote. **Force-upgrade.** A remote admin's
#:   :class:`SpaceApprovalService` gates the forward on
#:   :data:`FederationCapability.MIN_FOR_ADMIN_PROPOSALS`; a sub-v_16 host
#:   has no handler, so the forward raises rather than silently dropping
#:   the proposal. Sub-v_16 *members* simply don't see the pending-proposal
#:   UI (best-effort mirror) but the host still collects votes from peers
#:   that do. Operators must upgrade the host before remote admins can
#:   propose / vote.
#: * **v_17** (2026-06-02) — Social Home Apps federation bridge:
#:   :data:`FederationEventType.APP_SESSION` (session lifecycle) and
#:   :data:`FederationEventType.APP_MESSAGE` (application-layer message).
#:   On v_17+ CONFIRMED direct peers a dedicated ``fed-app-v1`` binary
#:   DataChannel carries ``APP_MESSAGE`` as binary frames (same multiplexing
#:   pattern as the v_14 media channel). **Fallback.** Sub-v_17 peers
#:   transparently receive ``APP_MESSAGE`` as a standard JSON federation
#:   event over ``fed-v1`` / HTTPS — the same degraded-but-correct path
#:   the v_14 media channel uses for non-CONFIRMED / older peers. Session
#:   control (``APP_SESSION``) always rides the event path, so an app
#:   session degrades gracefully on older peers; only the binary fast-path
#:   is lost.
#: * **v_18** (2026-06-03) — per-user app session/message routing
#:   (``to_user`` / ``from_user``). ``APP_SESSION`` and ``APP_MESSAGE``
#:   events may now carry a ``to_user`` field (target user's local_id on
#:   the receiving household) and a ``from_user`` display hint. Sub-v_18
#:   peers receive the legacy household-addressed shape (no ``to_user``)
#:   and the local bridge fans the event to all local users (the prior
#:   behaviour). §FIX-I2 relaxed for shared-space co-members.
#: * **v_19** (2026-06-04) — :data:`FederationEventType.INSTANCE_RESYNC_REQUEST`.
#:   A peer can ask us to re-broadcast state for a named scope:
#:   ``"capabilities"`` (re-advertise our ``proto_version``),
#:   ``"space:<id>"`` (replay the space's content — membership-gated), or
#:   ``"calendar:<id>"`` (replay just the space's calendar — membership-gated).
#:   The handler dispatches the scope and re-sends to the requester; the
#:   space / calendar replay reuses the §4.4 ``SPACE_SYNC_RESUME`` machinery
#:   so receivers dedup by primary key. Capability-gated: the sender gates
#:   the outbound on :data:`FederationCapability.MIN_FOR_INSTANCE_RESYNC`
#:   (the operator endpoint 409s a sub-v_19 peer) so the request never
#:   reaches a peer with no handler. **No fallback** — a sub-v_19 peer has
#:   no resync handler, so there is no safe degraded send.
#: * **v_20** (2026-06-08) — :data:`FederationEventType.SPACE_SYNC_REJECTED`.
#:   When a member reconnects and sends ``SPACE_SYNC_BEGIN`` for a space it
#:   is no longer a member of, the host replies with a signed
#:   ``SPACE_SYNC_REJECTED {sync_id, space_id, reason}`` instead of silently
#:   dropping the request. ``reason`` is ``"dissolved"`` (the space no longer
#:   exists on the host) or ``"removed"`` (the space exists but the requester
#:   is no longer a member). The member verifies the event came from the
#:   space's owner instance, then archives its local copy read-only
#:   (``archived_reason`` = the reason) so an offline member who missed the
#:   original ``SPACE_DISSOLVED`` / removal event still reconciles on
#:   reconnect. **Best-effort backstop.** Sub-v_20 hosts silently drop the
#:   non-member sync request as before, so a sub-v_20 member relies on the
#:   normal ``SPACE_DISSOLVED`` broadcast / outbox (unchanged) and may keep
#:   an orphaned stub until that arrives.
#: * **v_21** (2026-06-09) — authenticated mesh route discovery.
#:   :data:`FederationEventType.SPACE_ROUTE_FOUND` now carries
#:   ``target_identity_pk`` (the target's Ed25519 identity public key,
#:   hex) + ``target_eph_sig`` (the target's signature over
#:   ``space-route-found:v1:<request_id>:<target_eph_pk>``). The origin
#:   verifies the ephemeral X25519 key it will seal space content under
#:   is signed by the target's identity (``derive_instance_id(
#:   target_identity_pk) == target``) and that the path actually ends at
#:   the target, before collecting the response. Closes a relay-MITM
#:   (🔴): a malicious confirmed peer on the SPACE_FIND_ROUTE flood could
#:   reply with its OWN ephemeral key and win the shortest-path
#:   tie-break, so the origin sealed real space content (post bodies,
#:   GPS, files — NOT independently encrypted on the mesh path) and the
#:   §D2 invite token under the attacker's key, letting the relay
#:   decrypt it. **No fallback — fail-closed.** A patched origin DROPS
#:   any unsigned / forged / wrong-key ROUTE_FOUND, so a sub-v_21 target
#:   (which ships no signature) becomes mesh-*unreachable* via discovery
#:   until it upgrades. The security trade is intentional: a forgeable
#:   key is strictly worse than a missing route. Direct CONFIRMED peers
#:   and the local short-circuit are unaffected (no relayed ROUTE_FOUND
#:   to trust). Space-scoped: a behind member household can't be reached
#:   over the mesh, so it warns in the per-space compatibility banner.
#: * **v_22** (2026-06-09) — delegated-admin signing-seed share.
#:   :data:`FederationEventType.SPACE_ADMIN_KEY_SHARE` ships the space's
#:   Ed25519 signing seed (the private half of ``identity_public_key``)
#:   from the owner household to a REMOTE admin household when the owner
#:   has opted into ``SpaceFeatures.delegated_admin_authority``. The
#:   recipient can then sign space-authority events even while the owner
#:   is offline. The seed travels ONLY over the encrypted peer-pair path
#:   (``send_with_mesh_fallback`` → directional session key), never
#:   broadcast; the payload carries a ``seed_suite`` tag
#:   (``"ed25519-seed"``) so an unknown suite is rejected, and the
#:   receiver fails closed — it stores the seed only when the event
#:   came from the authentic owner instance AND its own local copy of
#:   the space has ``delegated_admin_authority`` enabled. **No fallback,
#:   fail-closed.** The owner gates the send on
#:   :data:`FederationCapability.MIN_FOR_SPACE_ADMIN_KEY_SHARE`; a
#:   sub-v_22 admin household has no handler, so the owner skips the
#:   send and logs at WARNING (the admin simply can't act offline yet)
#:   rather than blasting a seed at a peer that would drop it. Already-
#:   shared seeds persist when the flag is later turned off — deeper
#:   revocation (seed rotation) is a later phase. Space-scoped: a behind
#:   admin household can't receive delegated authority, so it warns in
#:   the per-space compatibility banner.
#: * **v_23** (2026-06-10) — peer-replicated space roster gossip.
#:   :data:`FederationEventType.SPACE_MEMBER_JOINED` /
#:   :data:`SPACE_MEMBER_LEFT` are now emitted by the host on every roster
#:   mutation (local + remote member add / remove / role change) and
#:   broadcast to every member household via ``broadcast_to_space_members``
#:   (targets ``space_instances`` — the non-member-relay rule holds; never
#:   ``broadcast_to_all``). Each event is **space-authority-signed** — the
#:   payload carries ``authority_sig`` + ``authority_sig_suite`` produced
#:   with the space's Ed25519 seed (:func:`sign_authority_event`) — so any
#:   receiver trusts it by verifying against ``spaces.identity_public_key``
#:   regardless of which household relayed it (the trust root is the
#:   signature, not ``from_instance``). The payload carries a monotonic
#:   ``member_version`` (per ``(space_id, user_id)``) + a ``roster_version``,
#:   both sourced from the space's atomic ``config_sequence``, so the
#:   receiver's version-guarded CRDT merge
#:   (:meth:`AbstractSpaceRemoteMemberRepo.apply_member_event`) converges
#:   regardless of delivery order (removal-wins-tie; a replayed/stale event
#:   is ignored). Before this, a join / leave was host-only and other
#:   member households learned implicitly via §25.6 sync or fresh invites.
#:   **Best-effort, gated.** The host gates the broadcast on
#:   :data:`FederationCapability.MIN_FOR_SPACE_ROSTER_GOSSIP`; a sub-v_23
#:   member household is skipped silently and keeps learning the roster via
#:   the snapshot / sync path (today's behaviour). Only the owner / seed-
#:   holder can sign — a non-owner without the seed skips signing + gossip
#:   gracefully (falls back to today's behaviour). Space-scoped: a behind
#:   member household won't converge its roster, so it warns in the
#:   per-space compatibility banner.
#: * **v_24** (2026-06-10) — admin-authoritative offline config edits.
#:   :data:`FederationEventType.SPACE_CONFIG_CHANGED` payloads are now
#:   **space-authority-signed**: the emitter (owner host OR a seed-holding
#:   delegated admin) signs the config ``space_meta`` with the space's
#:   Ed25519 seed (:func:`sign_authority_event`), and the receiver accepts
#:   the change by verifying against ``spaces.identity_public_key`` rather
#:   than requiring ``from_instance == owner_instance_id``. This lets a
#:   delegated admin change a space's config (name / description / emoji /
#:   features / join-mode / retention / …) and have every member household
#:   — including the offline owner on reconnect — accept it, the
#:   foundational step for owner-offline spaces. An emitter that holds the
#:   seed for a ``delegated_admin_authority``-ON space executes the edit
#:   LOCALLY + authoritatively (bumps ``config_sequence``, broadcasts the
#:   signed event) instead of forwarding to the host; without the flag or
#:   the seed it keeps the v_15 forward-to-host behaviour. Concurrent
#:   same-sequence edits by two admins converge via a deterministic
#:   ``(config_sequence, author_instance_id)`` lexicographic last-writer-
#:   wins tiebreak recorded on the receiver. Toggling
#:   ``delegated_admin_authority`` itself stays OWNER-only (unchanged).
#:   **Best-effort, gated.** The owner / seed-holder gates the signed
#:   broadcast on :data:`FederationCapability.MIN_FOR_ADMIN_AUTHORITATIVE_OPS`;
#:   a sub-v_24 member household has no authority-verify path so it falls
#:   back to the legacy owner-only gate (a non-owner's signed edit is
#:   dropped there) and reconciles via the §25.6 sync / owner re-broadcast
#:   when the owner next comes online. The owner host still signs every
#:   edit, so an owner-originated change applies on a sub-v_24 peer through
#:   the legacy ``from_instance == owner`` gate (back-compat). Space-scoped:
#:   a behind member household won't accept a delegated admin's offline
#:   config edit, so it warns in the per-space compatibility banner.
#: * **v_25** (2026-06-15) — per-user identity binding (user pubkey +
#:   dual-signed assertion) in
#:   :data:`FederationEventType.USERS_SYNC` / :data:`USER_UPDATED`; older
#:   peers omit it, legacy ``user_id`` unaffected. The sender gates the
#:   extra field on :data:`FederationCapability.MIN_FOR_USER_IDENTITY_KEY`,
#:   so a sub-v_25 peer simply receives the legacy user shape (no per-user
#:   identity key) and keeps addressing users by ``user_id`` exactly as
#:   before. **Best-effort.** Per-user surface, not space-scoped — its lag
#:   affects only the two households exchanging the user roster, so it is
#:   deliberately kept out of the per-space compatibility banner.
#: * **v_26** (2026-06-15) — user_id derivation anchored to immutable
#:   identity_anchor. New users get a UUID identity_anchor; existing
#:   users are frozen to their username as the identity_anchor so their
#:   user_id remains stable across a rename. Senders gate the identity_anchor
#:   field on :data:`FederationCapability.MIN_FOR_IDENTITY_ANCHOR`; sub-v_26
#:   peers fall back to username-derived user_id (today's behaviour) and keep
#:   addressing users by the legacy ``user_id`` field unaffected. **Best-effort.**
#:   Per-user surface, not space-scoped — its lag affects only the two
#:   households exchanging the user roster, so it is deliberately kept out
#:   of the per-space compatibility banner.
#: * **v_27** (2026-06-16) — move-out link: :data:`FederationEventType.USER_MOVED`
#:   (old-home push of a dual-consent move-link) + :data:`USER_IDENTITY_RESOLVE`
#:   (pull backstop) redirect ``old_id@old_home`` → ``new_id@new_home``; the
#:   ``user_id`` stays household-scoped. Senders gate the push / resolve on
#:   :data:`FederationCapability.MIN_FOR_USER_MOVE`. **Best-effort.** Older peer
#:   fallback: not pushed / can't resolve → keeps the stale contact (no data
#:   loss, no auto-follow). Per-user surface, not space-scoped — its lag affects
#:   only the households tracking the moved user, so it is deliberately kept out
#:   of the per-space compatibility banner.
#: * **v_28** (2026-09-17) — mesh route-stale nack:
#:   :data:`FederationEventType.SPACE_ROUTE_STALE`. A ``SPACE_ROUTED`` target
#:   that has no cached ephemeral private half for the ``target_eph_pk`` an
#:   envelope was sealed under (it restarted; the key lived only in RAM) signs
#:   ``space-route-stale:v1:<route_id>:<stale_eph_pk>`` with its identity key
#:   and sends the nack back along the reverse path; the origin verifies it,
#:   drops the cached route, and retransmits once via fresh discovery. Hops
#:   gate the forward / emit on :data:`FederationCapability.MIN_FOR_ROUTE_STALE_NACK`.
#:   **Best-effort.** Older-peer fallback: a sub-v_28 hop drops the nack and
#:   behaviour is today's — the origin keeps sealing under the dead key until
#:   ``route_discovery.ROUTE_CACHE_TTL_S`` expires (#648 window). Space-scoped
#:   (the stuck envelopes are space content), so it appears in the per-space
#:   compatibility banner.
#: * **v_29** (2026-09-18) — invite-link bootstrap redeem:
#:   :data:`FederationEventType.SPACE_INVITE_BOOTSTRAP_REDEEM`. Somebody who
#:   opens a space invite link from a household they have never federated with
#:   seals a self-authenticating redeem envelope to the issuer's published
#:   key-wrap key and relays it by instance id through the connection server;
#:   the issuer validates it (``derive_instance_id`` anti-tamper, Ed25519
#:   signature, ±300 s timestamp, nonce replay, atomic token consume, ban, age
#:   gate), seats the redeemer behind an
#:   :data:`~socialhome.domain.federation.InstanceSource.SPACE_SESSION`
#:   instance row, and replies sealed through the same relay. The redeemer
#:   gates the attempt on the issuer's ``proto_version`` **from the invite
#:   blob** — there is no peer row to run ``peer_supports`` against, which is
#:   why the blob carries it. **Fail-fast, not best-effort.** Older-issuer
#:   fallback: the redeemer reads the sub-v_29 version in the blob and fails
#:   with today's "pair with them, or with one of their household's peers"
#:   message, so no envelope is burned on a household that can't answer.
#:   Space-scoped (it is how a household joins a space at all), so it appears
#:   in the per-space compatibility banner.
OURS: int = 29


class FederationCapability:
    """Named ``proto_version`` thresholds for sender-side gating.

    Callers reference these constants instead of magic numbers so the
    intent of each gate is searchable. Adding a feature: append a new
    constant whose value equals the ``proto_version`` that introduced
    it, and document it in ``docs/protocol/capabilities.md``.
    """

    #: Minimum proto_version where event payloads carry ``tz``. Senders
    #: include the field unconditionally (the receiver defaults to UTC
    #: at any version), so this constant is informational — kept as a
    #: worked example of how the next feature should be wired.
    MIN_FOR_CALENDAR_TZ = 2

    #: Minimum proto_version where ``DM_MESSAGE`` may carry the media-
    #: attachment fields (``file_name`` / ``mime_type`` /
    #: ``file_size_bytes`` / ``media_blob_id``) AND where the receiver
    #: knows how to handle the follow-up ``DM_MEDIA_BLOB`` event.
    #: Sub-v_3 peers fall back to a synthesised ``type='text'`` message
    #: ("📎 cat.jpg — sender needs to upgrade…") so the bubble still
    #: carries useful information instead of vanishing — see the
    #: :mod:`socialhome.federation.compat.dm_media_v3` transform.
    MIN_FOR_DM_MEDIA_SYNC = 3

    #: Minimum proto_version where the receiver knows
    #: :data:`FederationEventType.LOCAL_HOME_LOCATION_CHANGED`.
    #: Senders gate the broadcast on this; pre-v_5 peers are skipped
    #: silently (they'll learn the coords on next re-pair or via the
    #: peer-accept body if the operator unpairs / re-pairs).
    MIN_FOR_HOME_LOCATION_BROADCAST = 5

    #: Minimum proto_version where the issuer knows the
    #: ``SPACE_INVITE_TOKEN_REDEEM`` family + the multi-hop
    #: ``_ROUTED`` variants + ``SPACE_FIND_ROUTE`` route discovery.
    #: No fallback — the coordinator 422s the SPA on sub-v_6 issuers
    #: rather than waste the 10 s timeout. The receiver-side guard
    #: also doubles as protection against pasting a code minted by
    #: an instance that's been downgraded since.
    MIN_FOR_SPACE_INVITE_REDEEM = 6

    #: Minimum proto_version where the receiver registers a handler
    #: for :data:`FederationEventType.SPACE_KEY_EXCHANGE_REKEY`. Sub-
    #: v_7 peers silently drop the new event and stay on their cached
    #: epoch key, which means they fail-decrypt every subsequent post
    #: encrypted under the rotated epoch. The host emits unconditionally
    #: — gating on this constant would silently revert forward
    #: secrecy.
    MIN_FOR_SPACE_KEY_REKEY = 7

    #: Minimum proto_version where the receiver knows
    #: :data:`FederationEventType.SPACE_MEMBER_ROLE_CHANGED`. Sub-v_8
    #: peers silently drop the event and keep showing the
    #: pre-change role until a §25.6 sync refresh. Best-effort —
    #: a stale role badge is benign.
    MIN_FOR_REMOTE_MEMBER_ROLE = 8

    #: Minimum proto_version where the host knows
    #: :data:`FederationEventType.SPACE_REMOTE_ADMIN_KICK`. Sub-v_9
    #: hosts silently drop the kick command; the actor sees their UI
    #: succeed but the host never applies the change. Operators must
    #: upgrade hosts together with members.
    MIN_FOR_REMOTE_ADMIN_KICK = 9

    #: Minimum proto_version where the receiver knows
    #: :data:`FederationEventType.BAZAAR_LISTING_CREATED`. Sub-v_10
    #: peers silently drop the event and only see the wrapper post's
    #: caption — the listing details (price, photos, mode, status)
    #: never reach them. Best-effort: gating skips the send to older
    #: peers so they never see broken-looking partial data; they just
    #: see the post like today.
    MIN_FOR_BAZAAR_LISTING = 10

    #: Minimum proto_version where the receiver knows
    #: :data:`FederationEventType.BAZAAR_LISTING_UPDATED`. Sub-v_11
    #: peers silently drop status updates; their UI keeps showing
    #: "active" for a listing the seller marked sold/expired/cancelled
    #: until the next §25.6 catch-up sync repairs it. Best-effort.
    MIN_FOR_BAZAAR_STATUS = 11

    #: Minimum proto_version where the receiver knows
    #: :data:`FederationEventType.BAZAAR_BID_PLACED` /
    #: :data:`BAZAAR_OFFER_ACCEPTED`. Sub-v_12 peers don't see
    #: cross-household bids — a remote bidder's local UI shows their
    #: own bid, but the seller's host never receives it so the seller
    #: can't accept / mark sold. Best-effort gating.
    MIN_FOR_BAZAAR_BIDS = 12

    #: Minimum proto_version where the provider knows how to handle
    #: ``SPACE_SYNC_BEGIN {prefer_direct: false}`` (Part C HTTPS
    #: fallback). Sub-v_13 providers accept the BEGIN but never start
    #: streaming because their ``_handle_space_sync_begin`` only acted
    #: on ``prefer_direct=True``. The requester-side
    #: ``_handle_space_sync_direct_failed`` gates the relay retry on
    #: this so an older peer just doesn't get the fallback — direct
    #: works against them as before (still useful on same-LAN +
    #: modest-NAT pairs), cross-NAT pairs require both sides on v_13+.
    MIN_FOR_SYNC_HTTPS_FALLBACK = 13

    #: Minimum proto_version where the peer accepts media as binary
    #: frames on the ``fed-media-v1`` DataChannel. The sender gates the
    #: binary path on this AND on the peer being a CONFIRMED direct peer
    #: (the channel is point-to-point — mesh-only space members never use
    #: it). Sub-v_14 peers transparently fall back to the JSON
    #: ``DM_MEDIA_BLOB`` / ``SPACE_MEDIA_BLOB`` path inside
    #: :meth:`FederationService.send_media_chunk`, so the gate degrades
    #: throughput, never correctness.
    MIN_FOR_MEDIA_CHANNEL = 14

    #: Minimum proto_version where the host knows
    #: :data:`FederationEventType.SPACE_REMOTE_ADMIN_ACTION` — the generic
    #: cross-household admin mutation (config edit, ban / unban, archive /
    #: unarchive). The remote admin's :class:`SpaceService` gates the
    #: forward on this; a sub-v_15 host has no handler, so rather than
    #: mutate the local stub (which would silently diverge from the host),
    #: the forward raises :class:`SpacePermissionError`. Force-upgrade:
    #: operators must upgrade the host before remote admins can run these
    #: actions. (The v_9 :data:`SPACE_REMOTE_ADMIN_KICK` stays separate for
    #: back-compat — a v_9..v_14 host still honours kicks.)
    MIN_FOR_REMOTE_ADMIN_ACTION = 15

    #: Minimum proto_version where the host knows the multi-admin approval
    #: workflow — the ``propose`` / ``vote`` verbs on
    #: :data:`FederationEventType.SPACE_REMOTE_ADMIN_ACTION` and the
    #: :data:`SPACE_ADMIN_PROPOSAL_UPDATED` mirror broadcast. A remote
    #: admin's :class:`SpaceApprovalService` gates the forward on this and
    #: raises against an older host rather than dropping the proposal.
    MIN_FOR_ADMIN_PROPOSALS = 16

    #: Minimum proto_version where the peer accepts ``APP_SESSION`` /
    #: ``APP_MESSAGE`` and runs a Social Home App federation bridge. The
    #: dedicated ``fed-app-v1`` binary DataChannel is the fast path;
    #: sub-v_17 peers transparently receive the ``APP_MESSAGE`` JSON event
    #: over ``fed-v1`` / HTTPS instead (same fallback shape as the v_14
    #: media channel). Session control (``APP_SESSION``) always rides the
    #: event path, so an app session degrades gracefully to older peers.
    MIN_FOR_APP_CHANNEL = 17

    #: Minimum proto_version where the peer routes ``APP_SESSION`` /
    #: ``APP_MESSAGE`` to a *specific* user via the ``to_user`` field and
    #: accepts the ``from_user`` display hint. Sub-v_18 peers receive the
    #: legacy household-addressed shape (no ``to_user``) and the local
    #: bridge fans the event to all local users (the prior behaviour).
    MIN_FOR_APP_USER_ROUTING = 18

    #: Minimum proto_version where the peer registers a handler for
    #: :data:`FederationEventType.INSTANCE_RESYNC_REQUEST` — a peer can ask
    #: us to re-broadcast our capabilities, a space's content, or a space's
    #: calendar for a named scope. The operator-triggered sender gates on
    #: this and 409s the request against a sub-v_19 peer (which has no
    #: handler) rather than firing into the void. Instance-level, not a
    #: shared-space feature — a peer lacking it doesn't degrade any space —
    #: so it is deliberately kept out of the per-space compatibility banner.
    MIN_FOR_INSTANCE_RESYNC = 19

    #: Minimum proto_version where the peer (as host) replies
    #: :data:`FederationEventType.SPACE_SYNC_REJECTED` to a non-member
    #: ``SPACE_SYNC_BEGIN`` instead of silently dropping it, and (as member)
    #: handles that event by archiving its local copy read-only. Best-effort
    #: backstop: sub-v_20 peers silently drop the non-member sync request as
    #: before, so a sub-v_20 member relies on the normal ``SPACE_DISSOLVED``
    #: broadcast / outbox and may keep an orphaned stub until it arrives.
    MIN_FOR_SPACE_SYNC_REJECTED = 20

    #: Minimum proto_version where the target signs the ephemeral X25519
    #: key it ships in :data:`FederationEventType.SPACE_ROUTE_FOUND`
    #: (``target_identity_pk`` + ``target_eph_sig``) so the origin can
    #: bind the key to the target's identity before sealing space content
    #: under it. Fail-closed, no fallback: a patched origin drops any
    #: ROUTE_FOUND whose signature is missing / forged / signed by an
    #: identity that doesn't derive to the requested target, so a sub-v_21
    #: target is unreachable via mesh discovery until it upgrades. Closes
    #: the relay-MITM where a confirmed peer substituted its own key.
    MIN_FOR_AUTHENTICATED_ROUTE_DISCOVERY = 21

    #: Minimum proto_version where the admin household registers a handler
    #: for :data:`FederationEventType.SPACE_ADMIN_KEY_SHARE` — the
    #: delegated-admin signing-seed share. The owner gates the send on
    #: this; a sub-v_22 admin household has no handler, so the owner SKIPS
    #: the send and logs at WARNING (the admin can't sign space-authority
    #: events offline yet) rather than shipping a signing seed at a peer
    #: that would drop it. Fail-closed, no fallback — there is no safe
    #: degraded way to distribute a private key to a peer that can't
    #: validate it.
    MIN_FOR_SPACE_ADMIN_KEY_SHARE = 22

    #: Minimum proto_version where the member household registers handlers
    #: for the authority-signed roster gossip
    #: (:data:`FederationEventType.SPACE_MEMBER_JOINED` /
    #: :data:`SPACE_MEMBER_LEFT`) so its local roster converges peer-to-peer.
    #: The host gates the broadcast on this; a sub-v_23 household is skipped
    #: silently and keeps learning the roster via the §D1b snapshot / §25.6
    #: sync path (today's behaviour) — best-effort, so a lag is benign (a
    #: stale member list until the next sync) rather than a hard failure.
    MIN_FOR_SPACE_ROSTER_GOSSIP = 23

    #: Minimum proto_version where the member household verifies a
    #: **space-authority-signed** :data:`FederationEventType.SPACE_CONFIG_CHANGED`
    #: against ``spaces.identity_public_key`` and so accepts a config edit from
    #: a NON-owner seed-holder (a delegated admin) — the foundational gate for
    #: owner-offline config. The owner / seed-holder gates the signed broadcast
    #: on this; a sub-v_24 household has no authority-verify path, so it falls
    #: back to the legacy owner-only gate (a delegated admin's offline edit is
    #: dropped there until the owner re-broadcasts / §25.6 sync reconciles).
    #: Owner-originated edits still apply on a sub-v_24 peer via the legacy
    #: ``from_instance == owner`` path (back-compat). Best-effort, space-scoped.
    MIN_FOR_ADMIN_AUTHORITATIVE_OPS = 24

    #: Minimum proto_version where the peer accepts the per-user identity
    #: binding (the user's public key + dual-signed assertion) carried on
    #: :data:`FederationEventType.USERS_SYNC` / :data:`USER_UPDATED`. The
    #: sender gates the extra field on this; a sub-v_25 peer receives the
    #: legacy user shape (no per-user identity key) and keeps addressing
    #: users by ``user_id`` exactly as before — best-effort, the legacy
    #: ``user_id`` field is unaffected. Per-user surface (not space-scoped):
    #: its lag affects only the two households exchanging the roster, so it
    #: is intentionally excluded from the per-space compatibility banner.
    MIN_FOR_USER_IDENTITY_KEY = 25

    #: Minimum proto_version where the peer accepts the identity_anchor field
    #: on :data:`FederationEventType.USERS_SYNC` / :data:`USER_UPDATED` that
    #: anchors ``user_id`` derivation to an immutable identifier (UUID for new
    #: users, frozen username for existing users) instead of the current
    #: username. The sender gates the extra field on this; a sub-v_26 peer
    #: receives the legacy user shape (no identity_anchor) and derives user_id
    #: from username exactly as before — best-effort, the legacy ``user_id``
    #: field is unaffected. Per-user surface (not space-scoped): its lag affects
    #: only the two households exchanging the roster, so it is intentionally
    #: excluded from the per-space compatibility banner.
    MIN_FOR_IDENTITY_ANCHOR = 26

    #: Minimum proto_version where the peer accepts the move-out link —
    #: :data:`FederationEventType.USER_MOVED` (old-home push of a dual-consent
    #: move-link) and :data:`USER_IDENTITY_RESOLVE` (pull backstop) that redirect
    #: ``old_id@old_home`` → ``new_id@new_home`` while ``user_id`` stays
    #: household-scoped. The sender gates the push / resolve on this; a sub-v_27
    #: peer is not pushed (and can't resolve), so it keeps the stale contact (no
    #: data loss, no auto-follow) — best-effort. Per-user surface (not
    #: space-scoped): its lag affects only the households tracking the moved
    #: user, so it is intentionally excluded from the per-space compatibility
    #: banner.
    MIN_FOR_USER_MOVE = 27

    #: Minimum proto_version where a hop knows
    #: :data:`FederationEventType.SPACE_ROUTE_STALE` — the signed nack a
    #: ``SPACE_ROUTED`` target sends back along the reverse path when it
    #: has no cached ephemeral private half for the pub an envelope was
    #: sealed under (post-restart). A hop forwards / emits the nack only
    #: to a peer ≥ v_28; a sub-v_28 hop drops it and behaviour is
    #: today's — the origin keeps sealing under the stale key until
    #: ``route_discovery.ROUTE_CACHE_TTL_S`` expires, then rediscovers.
    #: Best-effort: the nack shortens the outage, its absence never
    #: loses data. Space-scoped (the stuck envelopes carry space
    #: content), so it appears in the per-space compatibility banner.
    MIN_FOR_ROUTE_STALE_NACK = 28

    #: Minimum proto_version where the issuing household understands the
    #: §D2b invite-link **bootstrap** redeem — a sealed, self-authenticating
    #: redeem envelope relayed by instance id, from a household that is
    #: neither a confirmed peer nor mesh-reachable. Gated on the version the
    #: public invite blob advertises rather than on ``peer_supports``: there
    #: is no ``remote_instances`` row for a stranger, so the blob is the only
    #: place the version can come from. A sub-v_29 issuer has no handler and
    #: would silently drop the envelope, so the redeemer refuses up front with
    #: the unchanged "pair with them, or with one of their household's peers"
    #: message instead of waiting out a timeout.
    MIN_FOR_INVITE_BOOTSTRAP_REDEEM = 29

    # v_4 (§11 pairing-via-inbox) intentionally has no named constant
    # here. Capability exchange happens *after* pairing completes, so
    # there is no point in the codepath where ``peer_supports(...,
    # min_version=4)`` could change behaviour — the sender always
    # emits the new shape, and v_3 receivers can't pair at all
    # (the legacy ``/api/pairing/peer-{accept,confirm}`` routes are
    # gone). The bump in :data:`OURS` plus the version-history entry
    # in the module docstring is the entire public surface.


#: Single source of truth mapping each ``MIN_FOR_*`` threshold to a short
#: human-readable feature label, for the admin federation-compatibility
#: panel. Built FROM the :class:`FederationCapability` constants so the
#: version numbers live in exactly one place — adding a feature means
#: appending one ``(FederationCapability.MIN_FOR_X, "Label")`` tuple here.
#: v_4 (pairing-via-inbox) has no entry for the same reason it has no
#: named constant: it's a pre-capability-exchange bump with no gated field.
CAPABILITY_FEATURES: list[tuple[int, str]] = [
    (FederationCapability.MIN_FOR_CALENDAR_TZ, "Calendar timezones"),
    (FederationCapability.MIN_FOR_DM_MEDIA_SYNC, "DM media"),
    (FederationCapability.MIN_FOR_HOME_LOCATION_BROADCAST, "Home-location sharing"),
    (FederationCapability.MIN_FOR_SPACE_INVITE_REDEEM, "Cross-household invite links"),
    (FederationCapability.MIN_FOR_SPACE_KEY_REKEY, "Space key rotation"),
    (FederationCapability.MIN_FOR_REMOTE_MEMBER_ROLE, "Remote member roles"),
    (FederationCapability.MIN_FOR_REMOTE_ADMIN_KICK, "Remote admin kick"),
    (FederationCapability.MIN_FOR_BAZAAR_LISTING, "Bazaar listings"),
    (FederationCapability.MIN_FOR_BAZAAR_STATUS, "Bazaar status"),
    (FederationCapability.MIN_FOR_BAZAAR_BIDS, "Bazaar bids"),
    (FederationCapability.MIN_FOR_SYNC_HTTPS_FALLBACK, "Sync HTTPS fallback"),
    (FederationCapability.MIN_FOR_MEDIA_CHANNEL, "Media DataChannel"),
    (FederationCapability.MIN_FOR_REMOTE_ADMIN_ACTION, "Remote admin actions"),
    (FederationCapability.MIN_FOR_ADMIN_PROPOSALS, "Multi-admin approvals"),
    (FederationCapability.MIN_FOR_APP_CHANNEL, "App federation channel"),
    (FederationCapability.MIN_FOR_APP_USER_ROUTING, "App user routing"),
    (FederationCapability.MIN_FOR_INSTANCE_RESYNC, "Instance resync request"),
    (FederationCapability.MIN_FOR_SPACE_SYNC_REJECTED, "Space sync reject reconcile"),
    (
        FederationCapability.MIN_FOR_AUTHENTICATED_ROUTE_DISCOVERY,
        "Authenticated mesh route discovery",
    ),
    (
        FederationCapability.MIN_FOR_SPACE_ADMIN_KEY_SHARE,
        "Space delegated admin authority",
    ),
    (
        FederationCapability.MIN_FOR_SPACE_ROSTER_GOSSIP,
        "Space roster gossip",
    ),
    (
        FederationCapability.MIN_FOR_ADMIN_AUTHORITATIVE_OPS,
        "Admin authoritative config offline",
    ),
    (
        FederationCapability.MIN_FOR_USER_IDENTITY_KEY,
        "Per-user identity binding",
    ),
    (
        FederationCapability.MIN_FOR_IDENTITY_ANCHOR,
        "UUID identity anchor",
    ),
    (
        FederationCapability.MIN_FOR_INVITE_BOOTSTRAP_REDEEM,
        "Invite-link bootstrap redeem",
    ),
    (
        FederationCapability.MIN_FOR_USER_MOVE,
        "User move-out link",
    ),
    (
        FederationCapability.MIN_FOR_ROUTE_STALE_NACK,
        "Mesh route-stale nack",
    ),
]


def features_missing_below(version: int) -> list[str]:
    """Return the feature labels a peer at ``version`` lacks vs :data:`OURS`.

    A feature is "missing" when its introducing ``min_version`` is strictly
    greater than the peer's advertised ``proto_version``. A peer at
    :data:`OURS` (or higher) lacks nothing; a peer at ``1`` lacks every
    labelled feature. Ordered by version so the SPA renders oldest-gap
    first.
    """
    return [label for ver, label in sorted(CAPABILITY_FEATURES) if ver > version]


#: The ``MIN_FOR_*`` thresholds whose feature is **shared-space scoped** — a
#: behind member household breaks them for the *whole* space, so a space-admin
#: banner should warn until that household upgrades. Built FROM the
#: :class:`FederationCapability` constants (never literal ints) so the version
#: numbers stay single-sourced.
#:
#: Intentionally EXCLUDED (not space features — they're per-pair / per-user
#: surfaces whose lag affects only the two parties involved, not the space):
#:
#: * ``MIN_FOR_CALENDAR_TZ`` — informational tz field, fail-soft at any version.
#: * ``MIN_FOR_DM_MEDIA_SYNC`` — direct-message media, a 1:1 surface.
#: * ``MIN_FOR_HOME_LOCATION_BROADCAST`` — per-pair home-location sharing.
#: * ``MIN_FOR_APP_CHANNEL`` / ``MIN_FOR_APP_USER_ROUTING`` — Social Home Apps
#:   ride a per-pair / per-user session, not a space content surface.
#: * ``MIN_FOR_USER_IDENTITY_KEY`` — per-user identity binding on the user
#:   roster (USERS_SYNC / USER_UPDATED); its lag affects only the two
#:   households exchanging the roster, not a shared space.
SPACE_SCOPED_MIN_VERSIONS: frozenset[int] = frozenset(
    {
        FederationCapability.MIN_FOR_SPACE_INVITE_REDEEM,
        FederationCapability.MIN_FOR_SPACE_KEY_REKEY,
        FederationCapability.MIN_FOR_REMOTE_MEMBER_ROLE,
        FederationCapability.MIN_FOR_REMOTE_ADMIN_KICK,
        FederationCapability.MIN_FOR_BAZAAR_LISTING,
        FederationCapability.MIN_FOR_BAZAAR_STATUS,
        FederationCapability.MIN_FOR_BAZAAR_BIDS,
        FederationCapability.MIN_FOR_SYNC_HTTPS_FALLBACK,
        FederationCapability.MIN_FOR_MEDIA_CHANNEL,
        FederationCapability.MIN_FOR_REMOTE_ADMIN_ACTION,
        FederationCapability.MIN_FOR_ADMIN_PROPOSALS,
        FederationCapability.MIN_FOR_AUTHENTICATED_ROUTE_DISCOVERY,
        FederationCapability.MIN_FOR_SPACE_ADMIN_KEY_SHARE,
        FederationCapability.MIN_FOR_SPACE_ROSTER_GOSSIP,
        FederationCapability.MIN_FOR_ADMIN_AUTHORITATIVE_OPS,
        FederationCapability.MIN_FOR_ROUTE_STALE_NACK,
        FederationCapability.MIN_FOR_INVITE_BOOTSTRAP_REDEEM,
    }
)


def space_features_missing_below(version: int) -> list[str]:
    """Space-scoped feature labels a member household at ``version`` lacks.

    Like :func:`features_missing_below`, but restricted to the
    :data:`SPACE_SCOPED_MIN_VERSIONS` subset — the features whose absence on
    one member household degrades the *whole* space. Powers the per-space
    version-compatibility banner (#319 ¶5). Ordered by version so the SPA
    renders the oldest gap first.
    """
    return [
        label
        for ver, label in sorted(CAPABILITY_FEATURES)
        if ver > version and ver in SPACE_SCOPED_MIN_VERSIONS
    ]
