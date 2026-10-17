"""Outbound federation for user profile updates (§4 / §23 profile).

Subscribes to :class:`UserProfileUpdated` and fans the event out as a
``USER_UPDATED`` payload to every paired instance that has a live
mirror of this user (a row in ``remote_users`` on the peer).

The payload always carries display name + bio + picture hash. When the
picture bytes changed in this publication, they travel as a base64
``picture_webp_base64`` field so the peer can re-validate + store
locally. When the bytes are unchanged, the field is omitted (hash alone
is enough to cache-bust URLs built from the prior bytes).

Household-scope only: paired peers receive updates for users whose
``user_id`` appears in their local ``remote_users`` — the inbound
handler treats any unknown ``user_id`` as an upsert-anyway which is
the documented §24 behaviour.

Guardian blocks (§CP.F2, via :class:`ProtectionGateMixin`): a household
where someone a protected account's guardian blocked is homed gets the
account's **name only** — ``bio`` and ``picture_hash`` go out ``null`` and
no picture bytes travel. Every receiver already reads an explicit ``null``
as "none" (the upsert overwrites both columns, and a current receiver also
drops the cached picture bytes), so whatever it held before the block is
cleared. The block never leaves this household, so the sender is the only
place it can hold. On block-add (and protection turned back on over kept
blocks) that household gets one name-only update at once — preceded by
one plain grey placeholder picture, so a receiver that keeps cached bytes
after a ``null`` hash (an older build) overwrites the child's; when the block
no longer covers it (block removed, protection off) it gets the full
profile again, picture bytes included.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..domain.events import UserProfileUpdated
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus
from .peer_outbound import ConfirmedPeerBroadcaster, SingleTargetSender
from .protection_gate import ProtectionGateMixin
from .user_identity_binding import user_identity_binding_fields
from .visibility import VisibilityMixin

if TYPE_CHECKING:
    from ..domain.user import User
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.peer_user_visibility_repo import (
        AbstractPeerUserVisibilityRepo,
    )
    from ..repositories.profile_picture_repo import AbstractProfilePictureRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

#: Queued envelopes a guardian block supersedes on a household (§CP.F2).
_SUPERSEDED: frozenset[FederationEventType] = frozenset(
    {
        FederationEventType.USER_UPDATED,
        FederationEventType.USERS_SYNC,
        FederationEventType.USER_STATUS_UPDATED,
    }
)

#: A plain grey 8x8 WebP. On block-add it goes out once, just before the
#: name-only update, so a receiver that keeps cached picture bytes after a
#: ``picture_hash: null`` (any build before this rule) overwrites the
#: child's picture with it instead of keeping it (§CP.F2).
_BLANK_PICTURE_WEBP = base64.b64decode(
    "UklGRh4AAABXRUJQVlA4TBEAAAAvB8ABAAdQ5jKXuf+BiOh/AAA="
)


class ProfileFederationOutbound(
    VisibilityMixin,
    ProtectionGateMixin,
    ConfirmedPeerBroadcaster,
    SingleTargetSender,
):
    """Publish :class:`UserProfileUpdated` events as ``USER_UPDATED``."""

    # Narrow the mixin's ``FederationService | None`` slot — this service
    # always wires a live federation service in ``__init__``.
    _federation: FederationService

    __slots__ = (
        "_bus",
        "_child_protection",
        "_federation",
        "_federation_repo",
        "_picture_repo",
        "_snapshot_sender",
        "_user_repo",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
        federation_repo: AbstractFederationRepo,
        visibility_repo: AbstractPeerUserVisibilityRepo | None = None,
        user_repo: AbstractUserRepo | None = None,
        profile_picture_repo: AbstractProfilePictureRepo | None = None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        # Optional so existing test wiring (which constructs this
        # service without the visibility filter) keeps working — when
        # unset, every user fans to every confirmed peer just like
        # before this PR.
        self._visibility_repo = visibility_repo
        # Optional — supplies the per-user Ed25519 identity keypair for the
        # proto-v_25 identity binding. When unset the binding is simply
        # omitted (legacy shape) even for a v_25 peer.
        self._user_repo = user_repo
        # Optional — the picture bytes a lifted guardian block sends again.
        self._picture_repo = profile_picture_repo
        self._child_protection = None
        self._snapshot_sender: Callable[[str], Awaitable[None]] | None = None

    def attach_snapshot_sender(self, send: Callable[[str], Awaitable[None]]) -> None:
        """Wire ``UsersSyncOutbound.send_snapshot`` — on a guardian block it
        replaces the profile / status envelopes dropped from the outbox."""
        self._snapshot_sender = send

    def wire(self) -> None:
        self._bus.subscribe(UserProfileUpdated, self._on_updated)

    async def _on_updated(self, event: UserProfileUpdated) -> None:
        # The public @handle rides USER_UPDATED as unsigned display metadata,
        # exactly like ``display_name``. Most ``UserProfileUpdated`` publishers
        # (display_name/bio/picture edits, renames) leave ``event.handle`` None
        # even when the user has a handle, so back-fill it from the user row —
        # otherwise a non-handle edit would clobber the peer's cached handle.
        handle = event.handle
        if handle is None and self._user_repo is not None:
            user = await self._user_repo.get_by_user_id(event.user_id)
            if user is not None:
                handle = user.handle
        for instance_id in await self.list_confirmed_peer_ids():
            # Per-pair user-visibility filter (peer_user_visibility).
            # ``hidden_for_peer`` is fail-soft — repo errors default to
            # visible so we never silently lose profile updates on a
            # transient infra blip.
            hidden = await self.hidden_for_peer(instance_id)
            if event.user_id in hidden:
                continue
            await self._send(
                instance_id,
                user_id=event.user_id,
                username=event.username,
                display_name=event.display_name,
                bio=event.bio,
                picture_hash=event.picture_hash,
                handle=handle,
                picture_webp=event.picture_webp,
            )

    async def send_profile(
        self,
        instance_id: str,
        user: User,
        *,
        with_picture: bool = False,
    ) -> None:
        """Send *user*'s current profile to one household as ``USER_UPDATED``
        — under the same §CP.F2 rule as every other publication.

        ``with_picture`` attaches the stored picture bytes (for a receiver
        that may not hold them). The caller owns the per-pair visibility
        check; this applies only the guardian-block one.
        """
        picture_webp: bytes | None = None
        if with_picture and user.picture_hash and self._picture_repo is not None:
            got = await self._picture_repo.get_user_picture(user.user_id)
            if got is not None:
                picture_webp = got[0]
        await self._send(
            instance_id,
            user_id=user.user_id,
            username=user.username,
            display_name=user.display_name,
            bio=user.bio,
            picture_hash=user.picture_hash,
            handle=user.handle,
            picture_webp=picture_webp,
        )

    async def _send(
        self,
        instance_id: str,
        *,
        user_id: str,
        username: str,
        display_name: str,
        bio: str | None,
        picture_hash: str | None,
        handle: str | None,
        picture_webp: bytes | None,
        blank: bool = False,
    ) -> None:
        # §CP.F2: a guardian blocked someone homed there — the name only.
        # ``null`` bio / picture_hash (and no bytes) read as "none" on every
        # receiver, so nothing set before the block stays there either.
        # ``blank`` is the one overwrite of cached bytes (never the child's).
        if not blank and await self._guardian_blocks_household(user_id, instance_id):
            bio, picture_hash, picture_webp = None, None, None
        payload: dict = {
            "user_id": user_id,
            "username": username,
            "display_name": display_name,
            "bio": bio,
            "picture_hash": picture_hash,
            "handle": handle,
        }
        if picture_webp is not None:
            payload["picture_webp_base64"] = base64.b64encode(
                picture_webp,
            ).decode("ascii")
        # Per-user identity binding (proto v_25). Computed per-peer because
        # the v_25 gate is per-peer; a v_24 peer keeps the legacy payload
        # untouched. (The picture hash is not among the signed fields.)
        binding = await user_identity_binding_fields(
            federation_service=self._federation,
            user_repo=self._user_repo,
            peer_instance_id=instance_id,
            user_id=user_id,
            username=username,
            display_name=display_name,
            picture_hash=picture_hash,
        )
        await self.send_to_instance(
            instance_id,
            FederationEventType.USER_UPDATED,
            {**payload, **binding} if binding else payload,
        )

    # ─── §CP.F2 hooks (ChildProtectionService) ────────────────────────────

    async def on_guardian_block(self, minor_user_id: str, blocked_user_id: str) -> None:
        """The households the block now covers get the name only, at once —
        so a picture or bio they hold from before the block goes."""
        await self._name_only_on_blocked_households(minor_user_id)

    async def on_account_protected(self, user_id: str) -> None:
        """Blocks kept from an earlier protection count again."""
        await self._name_only_on_blocked_households(user_id)

    async def on_guardian_block_lifted(
        self, minor_user_id: str, instance_ids: frozenset[str]
    ) -> None:
        """No block covers these households any more: the full profile,
        picture bytes included (a current receiver dropped its copy)."""
        user = await self._local_user(minor_user_id)
        if user is None:
            return
        confirmed = set(await self.list_confirmed_peer_ids())
        for instance_id in sorted(instance_ids & confirmed):
            if minor_user_id in await self.hidden_for_peer(instance_id):
                continue
            if await self._guardian_blocks_household(minor_user_id, instance_id):
                continue
            await self.send_profile(instance_id, user, with_picture=True)

    async def _name_only_on_blocked_households(self, user_id: str) -> None:
        user = await self._local_user(user_id)
        if user is None:
            return
        for instance_id in await self.list_confirmed_peer_ids():
            if user_id in await self.hidden_for_peer(instance_id):
                continue
            if await self._guardian_blocks_household(user_id, instance_id):
                await self._supersede_queued(instance_id)
                # Overwrite the cached bytes first (an older receiver keeps
                # them on a null hash), then the name only.
                await self._send(
                    instance_id,
                    user_id=user.user_id,
                    username=user.username,
                    display_name=user.display_name,
                    bio=None,
                    picture_hash=None,
                    handle=user.handle,
                    picture_webp=_BLANK_PICTURE_WEBP,
                    blank=True,
                )
                await self.send_profile(instance_id, user)

    async def _supersede_queued(self, instance_id: str) -> None:
        """Envelopes still queued for the household (it was unreachable)
        were sealed before the block — delivered late, they would put the
        picture, bio or status back. Drop them, and send one snapshot of
        the current state in their place (it also carries what they said
        about everyone else). Runs before the placeholder and the name-only
        update so those are never the ones dropped. A status clear
        :class:`UserStatusOutbound` queued for the same block may be dropped
        here too (gate order) — the snapshot carries ``status: null`` for
        the account, which says the same."""
        dropped = await self._federation.drop_queued(instance_id, _SUPERSEDED)
        if not dropped:
            return
        log.info(
            "§CP.F2: dropped %d queued profile/status envelope(s) for %s",
            dropped,
            instance_id,
        )
        if self._snapshot_sender is not None:
            await self._snapshot_sender(instance_id)

    async def _local_user(self, user_id: str) -> User | None:
        if self._user_repo is None:
            return None
        return await self._user_repo.get_by_user_id(user_id)
