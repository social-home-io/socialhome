"""Outbound roster sync on pair-confirm and on every fresh connect (§4 / §11 / §23).

Subscribes to :class:`PairingConfirmed` and pushes a single
``USERS_SYNC`` envelope to the freshly-paired peer carrying every
local household member's profile.

It sends the same snapshot again each time a paired household connects
fresh — :class:`PeerCapabilitiesAdvertised`, which a household's
``INSTANCE_CAPABILITIES_UPDATED`` raises here and which it sends on every
startup — so a member it missed while it was away (a child account added
while it was offline, an envelope that aged out of the outbox) arrives
then. A reconnect that changes nothing sends nothing: we keep, per
household, a digest of the last snapshot it accepted from us
(:attr:`UsersSyncOutbound._sent`, in memory, like the peer directory's), so
a household that restarts over and over gets the snapshot once until a
member actually changes. After our own restart the first reconnect sends
once again.

The snapshot carries ``complete: true``: it names every member the
household may see, so the receiver takes a member it no longer names as
removed (``FederationInboundService._on_users_sync``) — which also heals a
``USER_REMOVED`` it missed. Without this, the peer's
``remote_users`` mirror sits empty until each member *individually*
edits their profile — which is rarely the first thing people do
after pairing, so households would see only the admin (whoever
happened to touch their profile) in :file:`/friends`.

Payload mirrors what the existing ``USER_UPDATED`` outbound
(:mod:`socialhome.services.profile_federation_outbound`) ships, just
batched: ``{users: [{user_id, username, display_name, bio,
picture_hash, handle, status, picture_webp_base64?}, ...]}``.
``status`` is the user's current, non-expired ``{emoji, text,
expires_at}`` (what ``USER_STATUS_UPDATED`` ships on a change) or
``null`` when they have none, so a peer shows it right after pairing
instead of only after the next status change. The inbound handler
(:meth:`FederationInboundService._on_users_sync`) iterates the list
and upserts each row via the same ``_upsert_remote_user`` path that
``USER_UPDATED`` uses, then lands ``status`` (when present) on the
accepted entry.

Per-pair user-visibility filter applies — admins who have hidden a
user from a peer via the existing peer-user-visibility surface keep
that user out of the snapshot.

Guardian blocks (§CP.F2): a protected account goes to a household where
someone its guardian blocked is homed with its name only — ``bio``,
``picture_hash`` and ``status`` ``null``, no picture bytes — like
``USER_UPDATED`` and ``USER_STATUS_UPDATED`` do.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..canonical_json import canonical_bytes
from ..domain.events import (
    PairingConfirmed,
    PeerCapabilitiesAdvertised,
    PeerUnpaired,
)
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from .peer_outbound import ConfirmedPeerBroadcaster
from .protection_gate import ProtectionGateMixin
from .user_identity_binding import user_identity_binding_fields
from .visibility import VisibilityMixin

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.peer_user_visibility_repo import (
        AbstractPeerUserVisibilityRepo,
    )
    from ..repositories.profile_picture_repo import AbstractProfilePictureRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

#: Outbox key of the snapshot — latest-only per household.
_OUTBOX_KEY = "users_sync"

#: Snapshot entry fields left out of the per-household digest: the instance
#: assertion is re-signed with a fresh ``issued_at`` on every build, and the
#: picture bytes are covered by ``picture_hash``.
_UNSTABLE_FIELDS = frozenset(
    {"user_assertion_signature", "user_assertion_issued_at", "picture_webp_base64"}
)


class UsersSyncOutbound(VisibilityMixin, ProtectionGateMixin, ConfirmedPeerBroadcaster):
    """Send ``USERS_SYNC`` to a freshly-confirmed or freshly-connected peer."""

    # Narrow the mixin's ``FederationService | None`` slot — always wired.
    _federation: FederationService

    __slots__ = (
        "_bus",
        "_child_protection",
        "_federation",
        "_federation_repo",
        "_picture_repo",
        "_sent",
        "_user_repo",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
        user_repo: AbstractUserRepo,
        profile_picture_repo: AbstractProfilePictureRepo | None = None,
        visibility_repo: AbstractPeerUserVisibilityRepo | None = None,
        federation_repo: AbstractFederationRepo | None = None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._user_repo = user_repo
        # The paired (social) households: a fresh connect is answered only
        # for one of them — never a household met through an invite link.
        self._federation_repo = federation_repo
        #: household → digest of the last snapshot it accepted from us.
        #: Bounded by the paired households; dropped on unpair.
        self._sent: dict[str, str] = {}
        # Optional so test wiring without picture bytes still works —
        # ``picture_webp_base64`` is just omitted in that case and
        # receivers fall back to the ``picture_hash`` URL.
        self._picture_repo = profile_picture_repo
        # Same optionality contract the existing profile_federation_outbound
        # uses: when unset, every local user fans to the new peer.
        self._visibility_repo = visibility_repo
        self._child_protection = None

    def wire(self) -> None:
        self._bus.subscribe(PairingConfirmed, self._on_pair_confirmed)
        self._bus.subscribe(PeerCapabilitiesAdvertised, self._on_peer_connected)
        self._bus.subscribe(PeerUnpaired, self._on_unpaired)

    async def _on_pair_confirmed(self, event: PairingConfirmed) -> None:
        await self.send_snapshot(event.instance_id)

    async def _on_peer_connected(self, event: PeerCapabilitiesAdvertised) -> None:
        """A paired household connected fresh: the snapshot, unless it
        already has this very one from us."""
        if event.instance_id not in await self.list_confirmed_peer_ids():
            return
        await self.send_snapshot(event.instance_id, only_if_changed=True)

    async def _on_unpaired(self, event: PeerUnpaired) -> None:
        self._sent.pop(event.instance_id, None)

    async def send_snapshot(
        self, peer_id: str, *, only_if_changed: bool = False
    ) -> None:
        """Send ``peer_id`` one ``USERS_SYNC`` of every (visible) local user.

        ``only_if_changed`` skips the send when the household already
        accepted this same snapshot from us (a reconnect that changes
        nothing).
        """
        own = getattr(self._federation, "_own_instance_id", "")
        if not peer_id or peer_id == own:
            return
        try:
            users = await self._user_repo.list_active()
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("users-sync: list_active failed: %s", exc, exc_info=True)
            return

        # Fail closed, unlike the per-event ``hidden_for_peer``: a snapshot
        # that cannot read the hide list would carry members hidden from
        # this household back to it.
        try:
            hidden = (
                await self._visibility_repo.hidden_user_ids_for_peer(peer_id)
                if self._visibility_repo is not None
                else frozenset()
            )
        except Exception as exc:
            log.warning(
                "users-sync: hide list for %s unreadable, no snapshot: %s",
                peer_id,
                exc,
                exc_info=True,
            )
            return
        now = datetime.now(UTC)
        payload_users: list[dict] = []
        for u in users:
            if u.user_id in hidden:
                continue
            # §CP.F2: a guardian of this account blocked someone homed on
            # the peer — the name only. ``None`` bio / picture (and no
            # bytes) read as "none", as on ``USER_UPDATED``; no status.
            blocked = await self._guardian_blocks_household(u.user_id, peer_id)
            picture_hash = None if blocked else u.picture_hash

            entry: dict = {
                "user_id": u.user_id,
                "username": u.username,
                "display_name": u.display_name,
                "bio": None if blocked else u.bio,
                "picture_hash": picture_hash,
                # Unsigned public @handle — same shape USER_UPDATED ships, so
                # the bootstrap sync at pairing lands the handle on the peer's
                # remote_users row without waiting for a later profile edit.
                "handle": u.handle,
                # Current status, or ``None`` (explicit clear) when unset or
                # past its deadline. Same per-pair visibility filter as
                # ``USER_STATUS_UPDATED`` (the ``hidden`` skip above).
                # §CP.F2: never a protected account's status to a household
                # where someone its guardian blocked is homed (as
                # ``USER_STATUS_UPDATED``) — ``None`` reads as no status.
                "status": None if blocked else u.status.visible_dict(now),
            }
            # Per-user identity binding (proto v_25) — present only for a
            # peer that can validate it; older peers keep the legacy shape.
            entry.update(
                await user_identity_binding_fields(
                    federation_service=self._federation,
                    user_repo=self._user_repo,
                    peer_instance_id=peer_id,
                    user_id=u.user_id,
                    username=u.username,
                    display_name=u.display_name,
                    picture_hash=picture_hash,
                ),
            )
            # Attach the WebP bytes so the peer can render the avatar
            # immediately. Skipped silently when the picture lookup
            # fails or no bytes exist — the receiver will still get
            # the hash and can lazy-load the avatar via the per-user
            # picture endpoint on first render.
            if picture_hash and self._picture_repo is not None:
                try:
                    pic = await self._picture_repo.get_user_picture(u.user_id)
                except Exception as exc:  # pragma: no cover — defensive
                    log.debug(
                        "users-sync: picture lookup for %s failed: %s",
                        u.user_id,
                        exc,
                        exc_info=True,
                    )
                    pic = None
                if pic is not None:
                    raw_bytes, _hash = pic
                    entry["picture_webp_base64"] = base64.b64encode(
                        raw_bytes,
                    ).decode("ascii")
            payload_users.append(entry)

        if not payload_users:
            return

        digest = snapshot_digest(payload_users)
        if only_if_changed and self._sent.get(peer_id) == digest:
            return
        try:
            result = await self._federation.send_event(
                to_instance_id=peer_id,
                event_type=FederationEventType.USERS_SYNC,
                # ``complete``: every member this household may see — the
                # receiver takes one it no longer names as removed, unless
                # it heard of them after ``as_of`` (a snapshot that waited
                # in a queue must not undo a newer USER_UPDATED).
                payload={
                    "users": payload_users,
                    "complete": True,
                    "as_of": datetime.now(UTC).isoformat(),
                },
                # Latest-only: at most one snapshot waits for a household
                # that is away, and it is the newest.
                outbox_key=_OUTBOX_KEY,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("users-sync: send to %s failed: %s", peer_id, exc, exc_info=True)
            return
        # Only a snapshot the household took counts. One left in the outbox,
        # or merely accepted by a GFS relay (queued there, may expire), goes
        # again on its next connect.
        if getattr(result, "ok", False) and getattr(result, "via", None) != "gfs_relay":
            self._sent[peer_id] = digest
        else:
            self._sent.pop(peer_id, None)


def snapshot_digest(payload_users: list[dict]) -> str:
    """What the household holds after this snapshot, ignoring re-signing."""
    stable = [
        {k: v for k, v in entry.items() if k not in _UNSTABLE_FIELDS}
        for entry in payload_users
    ]
    return hashlib.sha256(canonical_bytes(stable)).hexdigest()
