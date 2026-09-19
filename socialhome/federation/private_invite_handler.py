"""Zero-leak cross-household invites for private spaces (§D1b).

Three inbound event types + one outbound:

* ``SPACE_PRIVATE_INVITE`` — host → invitee's household. The plaintext
  envelope carries ONLY routing fields; space metadata (space_id,
  display hint, inviter, invite_token) lives entirely in the encrypted
  payload. §25.8.21 compliant.
* ``SPACE_PRIVATE_INVITE_ACCEPT`` — invitee → host.
* ``SPACE_PRIVATE_INVITE_DECLINE`` — invitee → host.
* ``SPACE_REMOTE_MEMBER_REMOVED`` — host → former invitee's household
  when a remote member is removed from the space.

The handler persists the invitation on receive so the UI can surface
accept / decline buttons, and on accept wires the invitee as a
:class:`SpaceRemoteMember` so the host's subsequent
``SPACE_POST_CREATED`` fan-outs include them in the recipient list.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    RemoteSpaceInviteAccepted,
    RemoteSpaceInviteDeclined,
    RemoteSpaceInviteReceived,
    RemoteSpaceMemberRemoved,
)
from ..crypto import derive_instance_id
from ..domain.federation import FederationEventType
from ..domain.space import RemoteAdminOutcome, SpaceRole
from ..infrastructure.event_bus import EventBus
from ..repositories.space_remote_location_repo import SpaceRemoteLocation
from ..services.space_crypto_service import (
    UnsupportedAuthoritySuite,
    strip_authority_sig_fields,
    verify_authority_event,
)
from ..services.space_service import (
    SUPPORTED_SEED_SUITES,
    apply_space_content_key_from_metadata,
    apply_space_cover_from_metadata,
    apply_space_icon_from_metadata,
    can_seat_remote_stub,
    stub_space_from_metadata,
)

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from ..repositories.space_cover_repo import AbstractSpaceCoverRepo
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from .federation_service import FederationService

log = logging.getLogger(__name__)

#: Roles a roster mirror may store for a remote member — the
#: ``space_remote_members.role`` CHECK, in code. ``subscriber`` is a
#: household that redeemed a Follower invite link (migration 0054, v_30);
#: ``owner`` is absent because ownership is a local-only privilege and the
#: host's own ``space_members.role`` can legitimately carry it on the wire.
MIRRORABLE_REMOTE_ROLES: frozenset[str] = frozenset(
    {
        SpaceRole.MEMBER.value,
        SpaceRole.ADMIN.value,
        SpaceRole.SUBSCRIBER.value,
    }
)


class PrivateSpaceInviteHandler:
    """Inbound dispatcher for the :data:`SPACE_PRIVATE_INVITE*` family."""

    __slots__ = (
        "_bus",
        "_space_repo",
        "_remote_members",
        "_cover_repo",
        "_icon_repo",
        "_space_crypto",
        "_space_service",
        "_approval_service",
        "_remote_locations",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: "AbstractSpaceRepo",
        remote_member_repo: "AbstractSpaceRemoteMemberRepo",
        cover_repo: "AbstractSpaceCoverRepo | None" = None,
        icon_repo=None,
        space_crypto_service=None,
        space_service=None,
        remote_location_repo=None,
    ) -> None:
        self._bus = bus
        self._space_repo = space_repo
        self._remote_members = remote_member_repo
        #: Optional — when wired, the joiner persists the host's
        #: cover bytes from ``space_meta.cover_webp_base64`` (§D1b
        #: #116) so the stub's card renders the real image.
        self._cover_repo = cover_repo
        #: Optional — same for the space icon (avatar) bytes from
        #: ``space_meta.icon_webp_base64`` so the stub shows the real icon
        #: rather than falling back to the emoji.
        self._icon_repo = icon_repo
        #: Optional — when wired, the joiner imports the host's
        #: space content key from ``space_meta.space_content_key``
        #: so subsequent SPACE_POST_CREATED decrypts succeed
        #: (§D1b #117).
        self._space_crypto = space_crypto_service
        #: Optional — when wired, used to dispatch validated
        #: ``SPACE_REMOTE_ADMIN_KICK`` events through the full kick
        #: path (rotation, role-check, broadcast). Tests that don't
        #: exercise admin actions can omit it.
        self._space_service = space_service
        #: Optional — multi-admin approval workflow (v_16). Dispatches the
        #: ``propose`` / ``vote`` verbs and the ``SPACE_ADMIN_PROPOSAL_UPDATED``
        #: mirror. Wired post-construction like ``_space_service``.
        self._approval_service = None
        #: Optional — when wired, inbound ``SPACE_LOCATION_UPDATED``
        #: events from member households are persisted here so the
        #: space map endpoint surfaces remote members' pins alongside
        #: local presence.
        self._remote_locations = remote_location_repo

    def attach_space_service(self, space_service) -> None:
        """Wire :class:`SpaceService` post-construction (#114 phase 2).

        :class:`SpaceService` is built downstream of this handler, so
        the kick dispatch path needs an after-the-fact handle. Without
        it the inbound ``SPACE_REMOTE_ADMIN_KICK`` is logged + dropped
        — degrading to a no-op rather than crashing the receiver.
        """
        self._space_service = space_service

    def attach_approval_service(self, approval_service) -> None:
        """Wire :class:`SpaceApprovalService` post-construction (v_16) so the
        host can dispatch validated ``propose`` / ``vote`` verbs and mirror
        ``SPACE_ADMIN_PROPOSAL_UPDATED`` onto member households."""
        self._approval_service = approval_service

    def attach_to(self, federation_service: "FederationService") -> None:
        registry = federation_service._event_registry  # noqa: SLF001
        registry.register(
            FederationEventType.SPACE_PRIVATE_INVITE,
            self._on_invite,
        )
        registry.register(
            FederationEventType.SPACE_PRIVATE_INVITE_ACCEPT,
            self._on_accept,
        )
        registry.register(
            FederationEventType.SPACE_PRIVATE_INVITE_DECLINE,
            self._on_decline,
        )
        registry.register(
            FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
            self._on_member_removed,
        )
        registry.register(
            FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
            self._on_key_exchange_rekey,
        )
        registry.register(
            FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
            self._on_role_changed,
        )
        registry.register(
            FederationEventType.SPACE_REMOTE_ADMIN_KICK,
            self._on_remote_admin_kick,
        )
        registry.register(
            FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
            self._on_remote_admin_action,
        )
        registry.register(
            FederationEventType.SPACE_ADMIN_PROPOSAL_UPDATED,
            self._on_proposal_mirror,
        )
        registry.register(
            FederationEventType.SPACE_LOCATION_UPDATED,
            self._on_space_location_updated,
        )
        registry.register(
            FederationEventType.SPACE_ADMIN_KEY_SHARE,
            self._on_admin_key_share,
        )
        registry.register(
            FederationEventType.SPACE_MEMBER_JOINED,
            self._on_space_member_joined,
        )
        registry.register(
            FederationEventType.SPACE_MEMBER_LEFT,
            self._on_space_member_left,
        )
        registry.register(
            FederationEventType.SPACE_SESSION_CLEANUP,
            self._on_space_session_cleanup,
        )

    # ── Receive ─────────────────────────────────────────────────────────

    async def _on_space_session_cleanup(self, event: "FederationEvent") -> None:
        """§D2b — the peer dropped its space-scoped seat for us; drop ours.

        Sent when the sender's last shared membership with us ended (kick,
        ban, leave, dissolve). We re-derive the same answer from our own
        rows rather than taking the sender's word: a household that shares
        two spaces with us and leaves one must not be able to tear down
        the seat the other still needs.
        """
        if self._space_service is None:
            log.debug(
                "SPACE_SESSION_CLEANUP from %s: space service not attached",
                event.from_instance,
            )
            return
        dropped = await self._space_service.apply_space_session_cleanup(
            event.from_instance,
        )
        if not dropped:
            log.info(
                "SPACE_SESSION_CLEANUP from %s ignored — we still share a "
                "space with them, or they were never a space-scoped seat",
                event.from_instance,
            )

    async def _record_host_identity_pk(
        self,
        *,
        space_id: str,
        host_instance_id: str,
        claimed_pk_hex: object,
    ) -> None:
        """Store the host household's identity pubkey on the space stub.

        Why this exists (#648): a member that joined over the mesh is not a
        paired peer of the host, so it has no ``remote_instances`` row — and
        the §25.6 sync receiver verifies each chunk's signature against
        exactly that row. Without a key the receiver dropped every
        mesh-routed ``SPACE_SYNC_CHUNK`` at "sync chunk from unknown
        instance" (at DEBUG, hence silently), leaving the joiner with the
        stub, the content key and the media bytes but no post metadata.

        The claim is only as trustworthy as the check applied to it, so it
        is stored **only** when ``derive_instance_id(pk)`` equals the
        envelope's authenticated sender — the same binding v_21 uses to
        authenticate ``target_eph_pk`` in ``SPACE_ROUTE_FOUND``. An
        instance id IS the hash of its identity key, so a sender cannot
        assert any key other than its own real one. Anything malformed or
        mismatched is dropped with a warning and the stub keeps a NULL
        column (unchanged behaviour: fine for a directly-paired host).
        """
        if not claimed_pk_hex:
            # Older sender — no key shipped. Directly-paired hosts are
            # unaffected; a mesh host will still fail to sync until the
            # host upgrades, which is the pre-existing behaviour.
            return
        if not isinstance(claimed_pk_hex, str):
            log.warning(
                "§D1b: ignoring non-string host_identity_pk from %s",
                host_instance_id,
            )
            return
        try:
            pk = bytes.fromhex(claimed_pk_hex)
        except ValueError:
            log.warning(
                "§D1b: ignoring malformed host_identity_pk hex from %s",
                host_instance_id,
            )
            return
        if len(pk) != 32:
            log.warning(
                "§D1b: ignoring host_identity_pk of %d bytes from %s (want 32)",
                len(pk),
                host_instance_id,
            )
            return
        if derive_instance_id(pk) != host_instance_id:
            # Either a bug or an attempt to bind someone else's identity to
            # this space. Loud: a legitimate sender can never hit this.
            log.warning(
                "§D1b: host_identity_pk from %s does not derive to its "
                "instance id — refusing to store it for space %s",
                host_instance_id,
                space_id,
            )
            return
        await self._space_repo.set_host_identity_pk(space_id, claimed_pk_hex)

    async def _on_invite(self, event: "FederationEvent") -> None:
        """A peer invited one of our users to their private space."""
        p = event.payload
        # All fields are in the encrypted payload — envelope plaintext
        # is strictly routing metadata. §25.8.21.
        space_id = str(p.get("space_id") or "")
        invite_token = str(p.get("invite_token") or "")
        invitee_user_id = str(p.get("invitee_user_id") or "")
        if not space_id or not invite_token or not invitee_user_id:
            log.debug(
                "SPACE_PRIVATE_INVITE from %s missing required fields",
                event.from_instance,
            )
            return
        inviter_user_id = str(p.get("inviter_user_id") or "")
        display_hint = p.get("space_display_hint")
        await self._space_repo.save_remote_invitation(
            space_id=space_id,
            invited_by=inviter_user_id,
            remote_instance_id=event.from_instance,
            remote_user_id=invitee_user_id,
            invite_token=invite_token,
            space_display_hint=(str(display_hint) if display_hint else None),
        )
        # §D1b — seat a *local stub* of the host's space so accept can
        # immediately insert a ``space_members`` row pointing at it.
        # The stub is invisible to the user's /api/spaces list (which
        # joins on ``space_members``) until they accept; declining
        # leaves it dust-but-harmless. ``space.save`` is upsert so a
        # repeat invite, or a refresh after SPACE_CONFIG_CHANGED, is
        # idempotent. Older senders that don't ship ``space_meta`` get
        # the legacy behaviour — no stub, joiner sees the invite banner
        # but can't see the space until upstream upgrades.
        meta = p.get("space_meta")
        # §D1b anti-hijack — drop the snapshot (fall back to no-stub legacy
        # behaviour) if a local space with this id is already owned by a
        # different host, so a malicious inviter can't clobber its config +
        # content key. Compared against the authenticated sender, never the
        # issuer-controlled meta["owner_instance_id"].
        if isinstance(meta, dict) and not await can_seat_remote_stub(
            self._space_repo, space_id, event.from_instance
        ):
            log.warning(
                "§D1b: refusing invite stub — space=%s already owned "
                "locally by another host, inviter=%s",
                space_id,
                event.from_instance,
            )
            meta = None
        if isinstance(meta, dict):
            stub = stub_space_from_metadata(
                space_id,
                host_instance_id=event.from_instance,
                meta=meta,
            )
            await self._space_repo.save(stub)
            await self._record_host_identity_pk(
                space_id=space_id,
                host_instance_id=event.from_instance,
                claimed_pk_hex=p.get("host_identity_pk"),
            )
            # §D1b cover bytes (#116) — when shipped inline, persist
            # so the stub renders the real cover rather than the
            # gradient placeholder.
            await apply_space_cover_from_metadata(
                space_id,
                meta=meta,
                cover_repo=self._cover_repo,
            )
            # §D1b icon bytes — same, for the space avatar.
            await apply_space_icon_from_metadata(
                space_id,
                meta=meta,
                icon_repo=self._icon_repo,
            )
            # §D1b space content key (#117) — persist receiver's
            # local epoch key so this invitee can actually decrypt
            # space events once she accepts. Without this the stub
            # is just metadata; SPACE_POST_CREATED inbound would
            # raise on decrypt and the user would see nothing.
            await apply_space_content_key_from_metadata(
                space_id,
                meta=meta,
                space_crypto_service=self._space_crypto,
            )
            # §D1b member-list mirror (#115) — the meta now carries a
            # ``roster`` of everyone in the space. Seat each entry as
            # a ``SpaceRemoteMember`` row so the joiner's local
            # ``GET /api/spaces/{id}/members`` (which merges
            # ``space_remote_members`` per PR #424) shows the full
            # household-spanning member list rather than just her
            # own row. We skip the invitee's *own* user_id — that
            # comes in via ``space_members`` when she accepts.
            roster = meta.get("roster")
            if isinstance(roster, list):
                for entry in roster:
                    if not isinstance(entry, dict):
                        continue
                    user_id = str(entry.get("user_id") or "")
                    inst_id = str(entry.get("instance_id") or "")
                    if not user_id or not inst_id or user_id == invitee_user_id:
                        continue
                    await self._remote_members.add(
                        space_id=space_id,
                        instance_id=inst_id,
                        user_id=user_id,
                        user_pk=(
                            str(entry["user_pk"]) if entry.get("user_pk") else None
                        ),
                        display_name=(
                            str(entry["display_name"])
                            if entry.get("display_name")
                            else None
                        ),
                    )
        await self._bus.publish(
            RemoteSpaceInviteReceived(
                space_id=space_id,
                inviter_user_id=inviter_user_id,
                invitee_user_id=invitee_user_id,
            )
        )

    async def _on_accept(self, event: "FederationEvent") -> None:
        """Our peer accepted the invite we sent — seat them as a
        :class:`SpaceRemoteMember`."""
        p = event.payload
        token = str(p.get("invite_token") or "")
        if not token:
            return
        invite = await self._space_repo.get_invitation_by_token(token)
        if invite is None:
            log.debug(
                "SPACE_PRIVATE_INVITE_ACCEPT: unknown token from %s",
                event.from_instance,
            )
            return
        invitee_user_id = str(p.get("invitee_user_id") or "")
        invitee_pk = p.get("invitee_public_key")
        invitee_display = p.get("invitee_display_name")
        await self._remote_members.add(
            space_id=invite["space_id"],
            instance_id=event.from_instance,
            user_id=invitee_user_id,
            user_pk=str(invitee_pk) if invitee_pk else None,
            display_name=str(invitee_display) if invitee_display else None,
        )
        # Register the accepting peer as a space *instance* member too —
        # ``broadcast_to_space_members`` queries ``space_instances``, so
        # without this row the peer's household never receives the
        # space's federation events (calendar events, posts, etc.).
        # The accepting host is the source-of-truth for membership, so
        # add the row on their side; the inviter learns about new peer
        # households via the existing ``SpaceMemberJoined`` channel.
        await self._space_repo.add_space_instance(
            invite["space_id"],
            event.from_instance,
        )
        await self._space_repo.update_invitation_status(
            invite["id"],
            "accepted",
        )
        # v_23 — peer-replicate the new seat to every member household so
        # their rosters converge (not just the host's). Delegated to
        # SpaceService (the seed-holder) when wired; skipped gracefully on a
        # stack that didn't attach it.
        if self._space_service is not None:
            await self._space_service.broadcast_remote_member_joined(
                invite["space_id"],
                instance_id=event.from_instance,
                user_id=invitee_user_id,
                user_pk=str(invitee_pk) if invitee_pk else None,
                display_name=str(invitee_display) if invitee_display else None,
            )
        await self._bus.publish(
            RemoteSpaceInviteAccepted(
                space_id=invite["space_id"],
                instance_id=event.from_instance,
                invitee_user_id=invitee_user_id,
            )
        )

    async def _on_decline(self, event: "FederationEvent") -> None:
        p = event.payload
        token = str(p.get("invite_token") or "")
        if not token:
            return
        invite = await self._space_repo.get_invitation_by_token(token)
        if invite is None:
            return
        await self._space_repo.update_invitation_status(
            invite["id"],
            "declined",
        )
        await self._bus.publish(
            RemoteSpaceInviteDeclined(
                space_id=invite["space_id"],
                instance_id=event.from_instance,
                invitee_user_id=str(p.get("invitee_user_id") or ""),
            )
        )

    async def _on_member_removed(self, event: "FederationEvent") -> None:
        """The host removed us (or one of our users) from a private
        space. Cleans up both possible local representations: the
        host-side ``space_remote_members`` row (no-op on the kicked
        user's own instance) AND the local stub ``spaces`` row + the
        kicked user's ``space_members`` row (no-op on the host's
        instance). One handler does both because the same event lands
        on both sides; each side recognises only its own data."""
        p = event.payload
        space_id = str(p.get("space_id") or "")
        user_id = str(p.get("user_id") or "")
        if not space_id or not user_id:
            return
        await self._remote_members.remove(
            space_id,
            event.from_instance,
            user_id,
        )
        # Drop any stored location pin too — without this, the kicked
        # member's last pin stays on the map until next reload OR a
        # future SPACE_LOCATION_UPDATED arrives (which the kick should
        # have already prevented at the sender side).
        if self._remote_locations is not None:
            await self._remote_locations.delete_for_member(
                space_id,
                event.from_instance,
                user_id,
            )
        # §D1b — clean up the joiner-side stub. If we have a local
        # ``space_members`` row for the kicked user, drop it. If the
        # stub's only member was that user, mark the stub dissolved
        # so the space stops appearing in surfaces that join on
        # ``space_members`` AND filter on ``dissolved=0`` (which is
        # every list-for-user query in the repo). Mark-dissolved
        # mirrors what happens to locally-owned spaces when their
        # last member leaves — the row stays as audit trail; the UI
        # treats it as gone.
        await self._space_repo.delete_member(space_id, user_id)
        remaining = await self._space_repo.list_members(space_id)
        if not remaining:
            local_space = await self._space_repo.get(space_id)
            # Only dissolve a stub — never our own locally-owned space.
            if local_space is not None and (
                local_space.owner_instance_id == event.from_instance
            ):
                await self._space_repo.mark_dissolved(space_id)
                # Stop treating the host as a member household of a space
                # we are no longer in — otherwise every later fan-out still
                # targets them, and (§D2b) the space-scoped seat we hold
                # for them never looks orphaned, so it is never revoked.
                await self._space_repo.remove_space_instance(
                    space_id,
                    event.from_instance,
                )
                if self._space_service is not None:
                    # ``notify=False``: the host initiated this, and on the
                    # §D2b path it has already dropped its own row — a
                    # CLEANUP back would land on a household that no longer
                    # has a row for us and be refused at the lookup step.
                    await self._space_service.revoke_space_session_if_orphaned(
                        event.from_instance,
                        notify=False,
                    )
        await self._bus.publish(
            RemoteSpaceMemberRemoved(
                space_id=space_id,
                instance_id=event.from_instance,
                user_id=user_id,
            )
        )

    async def _on_role_changed(self, event: "FederationEvent") -> None:
        """Host promoted / demoted a member (#114).

        Three sides see this event:

        * The promoted user's own household — updates the local
          ``space_members.role`` row so the SPA gates admin controls
          on the new role.
        * Other member households (witnesses) — updates the local
          ``space_remote_members.role`` so the rendered member list
          shows the new badge.
        * The host's own broadcast loops back to the host's
          ``broadcast_to_space_members`` set, but the host's own
          instance isn't included by construction (see
          :meth:`AbstractFederationRepo.list_member_instance_ids`).

        Idempotent — the repo set ops upsert. Cross-household admin
        commands (kick from a non-host instance) are not implemented
        yet; this event only propagates the role assignment so the
        UI can surface controls. Actual remote admin operations
        ride a separate event family in a future PR.
        """
        p = event.payload
        space_id = str(p.get("space_id") or "") or (event.space_id or "")
        user_id = str(p.get("user_id") or "")
        member_instance = str(p.get("instance_id") or "")
        role = str(p.get("role") or "")
        if not space_id or not user_id or not member_instance or not role:
            log.debug(
                "SPACE_MEMBER_ROLE_CHANGED from %s missing required fields",
                event.from_instance,
            )
            return
        if role not in ("admin", "member"):
            log.debug(
                "SPACE_MEMBER_ROLE_CHANGED unknown role %r — skipping",
                role,
            )
            return
        # We may be the affected member's own household OR a witness.
        # The local stub for this space uses ``space_members`` for our
        # own users and ``space_remote_members`` for everyone else;
        # both paths are upserts so we can update without first knowing
        # which side we're on.
        local = await self._space_repo.get_member(space_id, user_id)
        if local is not None:
            await self._space_repo.set_role(space_id, user_id, role)
        await self._remote_members.set_role(
            space_id,
            member_instance,
            user_id,
            role,
        )

    async def _on_space_location_updated(
        self,
        event: "FederationEvent",
    ) -> None:
        """Remote member's pin for the space map.

        The remote household's :class:`SpaceLocationOutbound` already
        applied the privacy-tier (gps vs zone_only) on the sender
        side; we just persist what we're told. The
        :class:`SpacePresenceView` route merges this with local
        presence so the map renders both. Without this handler the
        envelope arrived, the dispatch registry had no handler, and
        the pin silently dropped — Pascal's symptom of "space map
        only says 1 user, that's me".

        ``space_id`` must reference an existing space row (FK on
        :data:`space_remote_member_locations`); we skip silently if
        the space is unknown locally (a remote household racing the
        invite cleanup).
        """
        if self._remote_locations is None:
            log.debug(
                "SPACE_LOCATION_UPDATED: no remote_location_repo wired",
            )
            return
        p = event.payload
        space_id = str(p.get("space_id") or "") or getattr(
            event,
            "space_id",
            "",
        )
        user_id = str(p.get("user_id") or "")
        mode = str(p.get("mode") or "")
        if not space_id or not user_id or mode not in ("gps", "zone_only"):
            log.debug(
                "SPACE_LOCATION_UPDATED from %s missing required fields",
                event.from_instance,
            )
            return
        # Only persist if the sender is in fact a remote member of
        # this space — drop spoofed events from non-members.
        match = await self._remote_members.get(
            space_id,
            event.from_instance,
            user_id,
        )
        if match is None:
            log.debug(
                "SPACE_LOCATION_UPDATED: %s@%s is not a remote member of %s",
                user_id,
                event.from_instance,
                space_id,
            )
            return
        try:
            await self._remote_locations.upsert(
                SpaceRemoteLocation(
                    space_id=space_id,
                    instance_id=event.from_instance,
                    user_id=user_id,
                    mode=mode,
                    latitude=p.get("lat"),
                    longitude=p.get("lon"),
                    accuracy_m=p.get("accuracy_m"),
                    zone_id=p.get("zone_id"),
                    zone_name=p.get("zone_name"),
                    updated_at=p.get("updated_at"),
                )
            )
        except Exception:
            log.exception(
                "SPACE_LOCATION_UPDATED: upsert failed for %s@%s in %s",
                user_id,
                event.from_instance,
                space_id,
            )

    async def _on_remote_admin_kick(self, event: "FederationEvent") -> None:
        """Cross-household admin kick command (#114 phase 2).

        Receiver is the host of the space; sender is the household
        of a remote admin who wants someone removed. The handler
        delegates to :meth:`SpaceService.apply_remote_admin_kick`
        which validates the actor's role from
        ``space_remote_members.role`` before dispatching the actual
        kick. The §24.11 pipeline has already verified the
        envelope's signature; the role-check is the second gate.
        """
        if self._space_service is None:
            log.warning(
                "SPACE_REMOTE_ADMIN_KICK: no space_service wired — dropping",
            )
            return
        p = event.payload
        space_id = str(p.get("space_id") or "") or (event.space_id or "")
        actor_user_id = str(p.get("actor_user_id") or "")
        # SECURITY: bind the actor's household to the *signed* envelope —
        # never trust a payload-supplied actor_instance_id. ``from_instance``
        # is cryptographically bound to the signer (inbound_validator); a
        # payload claim is authored by that signer, so honouring it would
        # let a confirmed peer forge an action attributed to another
        # household's admin (the role lookup would match the impersonated
        # admin's row). An admin's action MUST originate from the admin's
        # own household.
        actor_instance_id = event.from_instance
        target_user_id = str(p.get("target_user_id") or "")
        if not space_id or not actor_user_id or not target_user_id:
            log.debug(
                "SPACE_REMOTE_ADMIN_KICK from %s missing required fields",
                event.from_instance,
            )
            return
        await self._space_service.apply_remote_admin_kick(
            space_id,
            actor_instance_id=actor_instance_id,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
        )

    async def _on_remote_admin_action(self, event: "FederationEvent") -> None:
        """Generic cross-household admin action (v_15+).

        Receiver is the host of the space; sender is the household of a
        remote admin who wants to run an admin-level mutation (config
        edit, ban / unban, archive / unarchive) — or, for the ``propose`` /
        ``vote`` verbs (v_16), a quorum-gated critical action. The §24.11
        pipeline has already verified the envelope signature; the role
        re-check happens inside the target service. Generalises
        :meth:`_on_remote_admin_kick`.
        """
        p = event.payload
        space_id = str(p.get("space_id") or "") or (event.space_id or "")
        actor_user_id = str(p.get("actor_user_id") or "")
        # SECURITY: bind the actor's household to the *signed* envelope —
        # never trust a payload-supplied actor_instance_id (see
        # ``_on_remote_admin_kick``). Honouring it would let a confirmed
        # peer impersonate another household's admin.
        actor_instance_id = event.from_instance
        action = str(p.get("action") or "")
        raw_params = p.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        if not space_id or not actor_user_id or not action:
            log.debug(
                "SPACE_REMOTE_ADMIN_ACTION from %s missing required fields",
                event.from_instance,
            )
            return
        # Quorum-approval verbs route to the approval service; everything
        # else is a direct admin mutation on SpaceService.
        if action in ("propose", "vote"):
            if self._approval_service is None:
                log.warning(
                    "SPACE_REMOTE_ADMIN_ACTION %s: no approval_service — dropping",
                    action,
                )
                return
            if action == "propose":
                await self._approval_service.apply_remote_propose(
                    space_id,
                    proposer_instance=actor_instance_id,
                    proposer_user=actor_user_id,
                    action=str(params.get("action") or ""),
                    params=params.get("params")
                    if isinstance(params.get("params"), dict)
                    else {},
                )
            else:
                await self._approval_service.apply_remote_vote(
                    space_id,
                    voter_instance=actor_instance_id,
                    voter_user=actor_user_id,
                    proposal_id=str(params.get("proposal_id") or ""),
                    approve=str(params.get("vote") or "") == "approve",
                )
            return
        if self._space_service is None:
            log.warning(
                "SPACE_REMOTE_ADMIN_ACTION: no space_service wired — dropping",
            )
            return
        outcome = await self._space_service.apply_remote_admin_action(
            space_id,
            actor_instance_id=actor_instance_id,
            actor_user_id=actor_user_id,
            action=action,
            params=params,
        )
        if outcome is RemoteAdminOutcome.NEEDS_OWNER_APPROVAL:
            # delegation OFF: the host doesn't auto-execute — record a pending
            # owner-approval (the owner approves via the normal proposal/vote
            # flow, then the host executes it as owner). actor_instance_id is
            # the signed envelope's from_instance (already bound to the
            # verified signer above), so the enqueue inherits that authority.
            if self._approval_service is None:
                log.warning(
                    "SPACE_REMOTE_ADMIN_ACTION needs owner approval but no "
                    "approval_service wired — dropping",
                )
                return
            await self._approval_service.enqueue_owner_approval(
                space_id,
                actor_instance=actor_instance_id,
                actor_user=actor_user_id,
                fwd_action=action,
                fwd_params=params,
            )

    async def _on_proposal_mirror(self, event: "FederationEvent") -> None:
        """Member-household side of ``SPACE_ADMIN_PROPOSAL_UPDATED`` (v_16).

        The host broadcasts the current proposal view; we mirror it so
        local admins can render the pending dissolve / publication-tier
        change and vote. Host-authoritative: we only accept a mirror for a
        space whose owner is the signer.
        """
        if self._approval_service is None:
            return
        space_id = str(event.payload.get("space_id") or "") or (event.space_id or "")
        view = event.payload.get("view")
        if not space_id or not isinstance(view, dict):
            return
        space = await self._space_repo.get(space_id)
        if space is None or space.owner_instance_id != event.from_instance:
            return  # only the host may mirror proposal state
        await self._approval_service.apply_mirror_update(space_id, view)

    async def _on_key_exchange_rekey(self, event: "FederationEvent") -> None:
        """Forward-secrecy rekey from the host or a delegated admin (#121).

        Triggered every time a member is removed (local kick, ban, or §D1b
        cross-household kick) — the rotator mints a fresh epoch + ships the
        new AES-256 content key to every remaining member household. We
        persist via :func:`apply_space_content_key_from_metadata`, which
        re-wraps the bytes under our own KEK so the new ``space_keys`` row
        matches the at-rest invariant.

        SECURITY — fail-closed (mirrors the Phase-4a ``SPACE_CONFIG_CHANGED``
        authority gate). A rekey PINS the current epoch onto whatever key it
        carries (Phase-4b smallest-``rotated_by`` wins). Importing one from
        ANY confirmed peer would let a routing relay or a removed-but-meshed
        ex-member force every member household onto an attacker-chosen key
        (a persistent content-key hijack + DoS). So before importing we
        AUTHENTICATE the rotator two ways — exactly like config:

          (a) Owner back-compat — the §24.11-authenticated ``from_instance``
              is the space's ``owner_instance_id``. The host pushing its own
              rekey; unsigned accepted (pre-authority owners).
          (b) Authority-signed — the inner ``space_content_key`` meta carries
              an ``authority_sig`` produced with the space's Ed25519 seed
              (held by the owner AND delegated admins). The trust root is the
              SIGNATURE verified against ``spaces.identity_public_key``, NOT
              ``from_instance``, so a delegated admin can rotate while the
              owner is offline and any member (incl. the offline owner on
              reconnect) accepts it regardless of relay.

        A present-but-invalid / unknown-suite / wrong-key signature DROPS
        (never falls through to the owner gate). A non-owner rekey with NO
        signature DROPS. A non-owner authority-signed rekey with a blank
        ``rotated_by`` DROPS (a legit rotator always stamps its real id; this
        keeps the smallest-wins tiebreak comparing only authenticated
        non-empty ids — defence-in-depth against a ``rotated_by=""`` pin).

        Idempotent. A repeated REKEY for the same epoch upserts.

        NOTE: this gate is scoped to the REKEY path only. The §D1b INITIAL
        key handoff (``SPACE_PRIVATE_INVITE`` snapshot →
        :func:`apply_space_content_key_from_metadata`) is authenticated by
        the invite envelope/flow and is unaffected — the gate lives here at
        the rekey call site, not inside the shared import helper.
        """
        space_id = str(event.payload.get("space_id") or "") or (event.space_id or "")
        if not space_id:
            return
        meta = event.payload.get("space_content_key")
        if not isinstance(meta, dict):
            return
        space = await self._space_repo.get(space_id)
        if space is None:
            # No local row → no public key to verify against and no owner to
            # back-compat against. Drop (the import would be a no-op anyway).
            log.warning(
                "SPACE_KEY_EXCHANGE_REKEY for unknown space %s from %s — dropping",
                space_id,
                event.from_instance,
            )
            return
        # ── Authorization (mirrors Phase-4a config gate) ─────────────────────
        is_owner = space.owner_instance_id == event.from_instance
        authority_sig = str(meta.get("authority_sig") or "")
        authority_sig_suite = str(meta.get("authority_sig_suite") or "")
        authority_verified = False
        if authority_sig or authority_sig_suite:
            if not (authority_sig and authority_sig_suite):
                log.warning(
                    "SPACE_KEY_EXCHANGE_REKEY for %s from %s: partial "
                    "authority signature — dropping",
                    space_id,
                    event.from_instance,
                )
                return
            try:
                authority_verified = verify_authority_event(
                    event_type="space_key_exchange_rekey",
                    space_id=space_id,
                    payload=strip_authority_sig_fields(meta),
                    authority_sig=authority_sig,
                    authority_sig_suite=authority_sig_suite,
                    space_public_key=bytes.fromhex(space.identity_public_key),
                )
            except UnsupportedAuthoritySuite:
                log.warning(
                    "SPACE_KEY_EXCHANGE_REKEY for %s: unknown "
                    "authority_sig_suite %r — dropping",
                    space_id,
                    authority_sig_suite,
                )
                return
            except Exception:
                log.warning(
                    "SPACE_KEY_EXCHANGE_REKEY for %s: malformed space public "
                    "key / signature — dropping",
                    space_id,
                )
                return
            if not authority_verified:
                log.warning(
                    "SPACE_KEY_EXCHANGE_REKEY for %s from %s: authority "
                    "signature did not verify against the space key — dropping",
                    space_id,
                    event.from_instance,
                )
                return
        if not (is_owner or authority_verified):
            # Non-owner, no valid signature → not authorised.
            log.warning(
                "SPACE_KEY_EXCHANGE_REKEY for %s from %s: non-owner rekey "
                "with no authority signature — dropping",
                space_id,
                event.from_instance,
            )
            return
        # Defence-in-depth on the smallest-wins tiebreak: an authority-signed
        # (non-owner) rekey MUST stamp a non-empty minter id. A blank
        # ``rotated_by`` on the authenticated path is a degenerate pin attempt
        # — drop it. NULL/absent is tolerated ONLY on the owner back-compat
        # path (older owners ship no minter).
        rotated_by = meta.get("rotated_by")
        if authority_verified and (
            not isinstance(rotated_by, str) or not rotated_by.strip()
        ):
            log.warning(
                "SPACE_KEY_EXCHANGE_REKEY for %s from %s: authority-signed "
                "rekey with blank rotated_by — dropping",
                space_id,
                event.from_instance,
            )
            return
        await apply_space_content_key_from_metadata(
            space_id,
            meta=event.payload,
            space_crypto_service=self._space_crypto,
        )

    async def _on_admin_key_share(self, event: "FederationEvent") -> None:
        """Delegated-admin signing-seed share from the space owner (v_22).

        Receiver is a remote admin household; sender is the space's owner.
        The seed is the space's PRIVATE Ed25519 signing key, so this handler
        is SECURITY-SENSITIVE and fails closed — it stores the seed ONLY
        when every check passes, and drops (log + return) otherwise:

        * The local copy of the space must exist AND the §24.11-verified
          ``from_instance`` must be the space's ``owner_instance_id`` — only
          the authentic owner may hand out the signing key. A confirmed peer
          that isn't the owner is rejected even though its envelope signature
          is valid.
        * The local copy must have ``features.delegated_admin_authority``
          enabled — the seed is only accepted into a space whose owner-policy
          (as we know it) authorises delegation. This second gate means a
          stale/forged ON flag on the *wire* can't force acceptance: we
          consult our own persisted space row, not the payload.
        * ``seed_suite`` must be a recognised suite (crypto-suite rule — no
          default fallback for an unknown scheme).
        * The seed must b64url-decode to exactly 32 bytes; the decode is
          wrapped so a malformed string drops rather than raises.

        On success the seed is persisted via
        :meth:`AbstractSpaceRepo.set_space_seed` and the receipt logged at
        INFO — this is the key-blast-radius audit event.
        """
        p = event.payload
        space_id = str(p.get("space_id") or "") or (event.space_id or "")
        if not space_id:
            log.warning(
                "SPACE_ADMIN_KEY_SHARE from %s missing space_id — dropping",
                event.from_instance,
            )
            return
        space = await self._space_repo.get(space_id)
        if space is None:
            log.warning(
                "SPACE_ADMIN_KEY_SHARE: no local space %s — dropping share from %s",
                space_id,
                event.from_instance,
            )
            return
        # SECURITY: only the authentic owner may distribute the signing seed.
        if space.owner_instance_id != event.from_instance:
            log.warning(
                "SPACE_ADMIN_KEY_SHARE: %s is not the owner of %s (owner=%s) "
                "— refusing signing-seed share",
                event.from_instance,
                space_id,
                space.owner_instance_id,
            )
            return
        # SECURITY: only accept when our own copy authorises delegation.
        if not space.features.delegated_admin_authority:
            log.warning(
                "SPACE_ADMIN_KEY_SHARE: delegated_admin_authority is OFF "
                "locally for %s — refusing signing-seed share from %s",
                space_id,
                event.from_instance,
            )
            return
        suite = str(p.get("seed_suite") or "")
        if suite not in SUPPORTED_SEED_SUITES:
            log.warning(
                "SPACE_ADMIN_KEY_SHARE: unknown seed_suite %r for %s — dropping",
                suite,
                space_id,
            )
            return
        seed_b64 = str(p.get("space_seed") or "")
        try:
            seed = base64.urlsafe_b64decode(seed_b64)
        except Exception:
            log.warning(
                "SPACE_ADMIN_KEY_SHARE: malformed seed for %s — dropping",
                space_id,
            )
            return
        if len(seed) != 32:
            log.warning(
                "SPACE_ADMIN_KEY_SHARE: seed for %s is %d bytes (want 32) — dropping",
                space_id,
                len(seed),
            )
            return
        await self._space_repo.set_space_seed(space_id, seed)
        log.info(
            "SPACE_ADMIN_KEY_SHARE: stored delegated-admin signing seed for "
            "%s (from owner %s) — this household can now sign space-authority "
            "events offline",
            space_id,
            event.from_instance,
        )

    # ── Space roster gossip (v_23) ──────────────────────────────────────

    async def _verify_roster_gossip(
        self,
        event: "FederationEvent",
    ) -> tuple[str, dict] | None:
        """Authenticate an inbound roster-gossip event. Returns
        ``(space_id, payload)`` on success, or ``None`` (logged at WARNING)
        when the event must be dropped.

        SECURITY: trust is in the SIGNATURE, not the sender — any seed-holder
        may emit (the owner today, delegated admins in later phases), so we do
        NOT gate on ``from_instance == owner`` here. The space's PUBLIC key
        (``spaces.identity_public_key``) is the trust root. We drop when:

        * the space is unknown locally (no public key to verify against);
        * ``authority_sig`` is absent / forged / signed by a key other than
          the space seed;
        * ``authority_sig_suite`` is unknown (crypto-suite rule — no default
          fallback).

        CRITICAL: the signature is verified over the payload with the two
        signature fields stripped, using the SAME
        :func:`strip_authority_sig_fields` helper the signer used, so the
        canonical bytes match.
        """
        p = event.payload
        space_id = str(p.get("space_id") or "") or (event.space_id or "")
        if not space_id:
            log.warning(
                "roster-gossip %s from %s missing space_id — dropping",
                event.event_type,
                event.from_instance,
            )
            return None
        space = await self._space_repo.get(space_id)
        if space is None:
            log.warning(
                "roster-gossip %s for unknown space %s from %s — dropping",
                event.event_type,
                space_id,
                event.from_instance,
            )
            return None
        authority_sig = str(p.get("authority_sig") or "")
        authority_sig_suite = str(p.get("authority_sig_suite") or "")
        if not authority_sig or not authority_sig_suite:
            log.warning(
                "roster-gossip %s for %s from %s missing authority signature "
                "— dropping",
                event.event_type,
                space_id,
                event.from_instance,
            )
            return None
        try:
            verified = verify_authority_event(
                event_type=(
                    event.event_type.value
                    if hasattr(event.event_type, "value")
                    else str(event.event_type)
                ),
                space_id=space_id,
                payload=strip_authority_sig_fields(p),
                authority_sig=authority_sig,
                authority_sig_suite=authority_sig_suite,
                space_public_key=bytes.fromhex(space.identity_public_key),
            )
        except UnsupportedAuthoritySuite:
            log.warning(
                "roster-gossip %s for %s: unknown authority_sig_suite %r — dropping",
                event.event_type,
                space_id,
                authority_sig_suite,
            )
            return None
        except Exception:
            log.warning(
                "roster-gossip %s for %s: malformed space public key — dropping",
                event.event_type,
                space_id,
            )
            return None
        if not verified:
            log.warning(
                "roster-gossip %s for %s from %s: authority signature did not "
                "verify against the space key — dropping",
                event.event_type,
                space_id,
                event.from_instance,
            )
            return None
        return space_id, p

    async def _apply_roster_gossip(
        self,
        event: "FederationEvent",
        *,
        tombstoned: bool,
    ) -> None:
        """Verify then version-guard-merge an inbound roster-gossip event.

        On a verified event, hand off to
        :meth:`AbstractSpaceRemoteMemberRepo.apply_member_event` — the CRDT
        merge that converges concurrent/out-of-order join/leave decisions
        (strictly-greater version wins; removal wins an equal-version tie; a
        stale event is dropped). Idempotent + order-insensitive.
        """
        result = await self._verify_roster_gossip(event)
        if result is None:
            return
        space_id, p = result
        user_id = str(p.get("user_id") or "")
        instance_id = str(p.get("instance_id") or "")
        if not user_id or not instance_id:
            log.warning(
                "roster-gossip %s for %s missing user_id/instance_id — dropping",
                event.event_type,
                space_id,
            )
            return
        try:
            member_version = int(p.get("member_version") or 0)
        except TypeError, ValueError:
            log.warning(
                "roster-gossip %s for %s: non-integer member_version — dropping",
                event.event_type,
                space_id,
            )
            return
        role = str(p.get("role") or SpaceRole.MEMBER.value)
        if role not in MIRRORABLE_REMOTE_ROLES:
            # The role is advisory here; the mutation is not. A value this
            # household's ``space_remote_members.role`` CHECK rejects — an
            # ``owner`` (the host ships the raw ``space_members.role``, and
            # ownership has no remote shape), or a role from a future
            # version — used to raise IntegrityError out of
            # ``apply_member_event`` and take the WHOLE event down with it,
            # tombstone included. The version guard then refused the retry
            # at the same ``member_version``, so a removal lost this way was
            # unhealable. Coerce to the least-privileged real seat and keep
            # the mutation: losing "which seat" is recoverable from the next
            # snapshot, losing "they left" is not.
            log.info(
                "roster-gossip %s for %s: unknown role %r — mirroring as %r",
                event.event_type,
                space_id,
                role,
                SpaceRole.MEMBER.value,
            )
            role = SpaceRole.MEMBER.value
        display_name = p.get("display_name")
        user_pk = p.get("user_pk")
        await self._remote_members.apply_member_event(
            space_id=space_id,
            user_id=user_id,
            instance_id=instance_id,
            display_name=str(display_name) if display_name else None,
            user_pk=str(user_pk) if user_pk else None,
            role=role,
            member_version=member_version,
            tombstoned=tombstoned,
        )
        # Register the member's household as a broadcast target. Without this a
        # member learned ONLY via roster gossip (not a direct invite/accept) is
        # absent from ``space_instances`` — so this household's
        # ``broadcast_to_space_members`` (config edits, posts, rekeys) would
        # never reach them. This is exactly the offline-of-owner case: a
        # delegated admin must be able to fan a change out to every member it
        # knows, not just the host it accepted from. We do NOT remove on a LEFT
        # tombstone here — a household may host other live members of the space,
        # and content access is already gated by epoch rotation, so a lingering
        # target is harmless (and removal would need an any-other-live-member
        # check). ``add_space_instance`` is an idempotent upsert and
        # ``broadcast_to_space_members`` already excludes our own household, so
        # a gossip row naming our instance is harmless.
        if not tombstoned:
            await self._space_repo.add_space_instance(space_id, instance_id)

    async def _on_space_member_joined(self, event: "FederationEvent") -> None:
        """Authority-signed roster JOINED (v_23) — apply (upsert) the member."""
        await self._apply_roster_gossip(event, tombstoned=False)

    async def _on_space_member_left(self, event: "FederationEvent") -> None:
        """Authority-signed roster LEFT (v_23) — tombstone the member."""
        await self._apply_roster_gossip(event, tombstoned=True)
