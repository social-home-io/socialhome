"""§25.6 sync of a space's roster: ``members``, ``member_pictures`` and
``bans``.
"""

from __future__ import annotations

import logging
from typing import Any

from ....domain.events import RemoteSpaceMemberBanned, SpaceMemberProfileUpdated
from ....domain.federation_capabilities import FederationCapability
from ....domain.space import SpaceMember, SpaceRole
from ....media.profile_picture import (
    PROFILE_PICTURE_MAX_DIMENSION,
    reencode_peer_picture,
)
from ....services.space_service import apply_owner_statement_from_metadata
from ...host_equivalence import touches_owner_household
from .receiver_base import ReceiverBase, _sync_event

log = logging.getLogger(__name__)


class MembersMixin(ReceiverBase):
    """The roster resources of :class:`~.receiver.SpaceSyncReceiver`."""

    __slots__ = ()

    async def _persist_members(
        self, space_id: str, records: list[dict[str, Any]]
    ) -> None:
        for r in records:
            await self._space_repo.save_member(
                SpaceMember(
                    space_id=space_id,
                    user_id=str(r.get("user_id") or ""),
                    role=str(r.get("role") or "member"),
                    joined_at=str(r.get("joined_at") or ""),
                    history_visible_from=r.get("history_visible_from"),
                    location_share_enabled=bool(r.get("location_share_enabled", False)),
                    space_display_name=r.get("space_display_name"),
                )
            )

    async def _persist_member_pictures(
        self, space_id: str, records: list[dict[str, Any]], *, provider: str
    ) -> None:
        # F6: persist per-member avatar bytes so the SPA stops
        # rendering broken <img> for every existing member after a
        # fresh joiner's catch-up. Skip silently if the receiver
        # was assembled without a profile_picture_repo (older
        # deployments).
        if self._profile_picture_repo is None:
            log.debug(
                "received %d member_picture records — no "
                "profile_picture_repo wired, skipping",
                len(records),
            )
            return
        # Our own people's per-space pictures are authored here — a
        # provider's copy never replaces them.
        ours = set(await self._space_repo.list_local_member_user_ids(space_id))
        for r in records:
            uid = str(r.get("user_id") or "")
            pic_b64 = r.get("picture_webp_base64")
            if not uid or not pic_b64 or uid in ours:
                log.debug(
                    "member_picture record for %s/%s skipped (missing "
                    "field, or one of our people)",
                    space_id,
                    uid,
                )
                continue
            # Re-encoded exactly like the live profile events, so a
            # provider cannot plant arbitrary bytes (SVG, HTML, a
            # polyglot) in the blob table; the hash is our own digest
            # of the re-encoded bytes — never the provider's
            # ``picture_hash`` (it is untrusted).
            try:
                webp, local_hash = await reencode_peer_picture(pic_b64)
            except ValueError:
                log.warning(
                    "space sync: refused member_picture from %s for %s/%s "
                    "— not a decodable image",
                    provider,
                    space_id,
                    uid,
                )
                continue
            await self._profile_picture_repo.set_member_picture(
                space_id,
                uid,
                bytes_webp=webp,
                hash=local_hash,
                width=PROFILE_PICTURE_MAX_DIMENSION,
                height=PROFILE_PICTURE_MAX_DIMENSION,
            )
            # Point the seat at the blob we hold (the ``members``
            # resource never sets ``picture_hash`` — it only clears a
            # reset one), so the picture renders — a no-op for someone
            # not seated here.
            seat = await self._space_repo.get_member(space_id, uid)
            await self._space_repo.set_member_profile(
                space_id, uid, picture_hash=local_hash
            )
            if seat is not None and seat.picture_hash != local_hash:
                await self._publish_member_picture(
                    space_id, seat, picture_hash=local_hash, provider=provider
                )

    async def _persist_bans(self, space_id: str, records: list[dict[str, Any]]) -> None:
        for r in records:
            user_id = str(r.get("user_id") or "")
            banned_by = str(r.get("banned_by") or "")
            if not user_id or not banned_by:
                continue
            # ``ban_member`` drops a seated member's row: tell the bus,
            # exactly like the host's live ban (``_on_banned``), so the
            # seat's dependents (space chat, GFS subscriber seats)
            # follow. A ban of someone not seated here is no change.
            seated = await self._space_repo.get_member(space_id, user_id)
            await self._space_repo.ban_member(
                space_id=space_id,
                user_id=user_id,
                banned_by=banned_by,
                reason=r.get("reason"),
            )
            if seated is not None:
                await self._bus.publish(
                    RemoteSpaceMemberBanned(
                        space_id=space_id, user_id=user_id, banned_by=banned_by
                    )
                )

    async def _apply_picture_resets(
        self,
        space_id: str,
        records: list[dict[str, Any]],
        *,
        provider: str,
    ) -> None:
        """Drop the per-space picture of a member whose ``members`` record
        says they have none (``picture_hash`` present and ``null``) — a
        reset this household missed while offline, which the
        ``member_pictures`` resource cannot tell (it skips seats with no
        picture). The live ``SPACE_MEMBER_PROFILE_UPDATED`` rule: only the
        member's own household — the provider holds their seat
        (:meth:`SpaceAuthorship.acts_for`, any role) — decides; the owner's
        view of another household's member is second-hand. And only from a
        provider at
        :attr:`~FederationCapability.MIN_FOR_MEMBER_PICTURE_RESET_SYNC`
        (an older one's ``null`` may be its pre-#910 roster-upsert wipe).
        Never for our own people, whose pictures are authored here. A
        non-null provider hash is never taken (it is untrusted;
        ``member_pictures`` ships and re-hashes the bytes), and a record
        without the key (an older shape, or a seat the provider holds
        bytes but no hash for) says nothing."""
        reset = [
            uid
            for r in records
            if "picture_hash" in r
            and r["picture_hash"] is None
            and (uid := str(r.get("user_id") or ""))
        ]
        if not reset or self._authorship is None or self._peer_version is None:
            return
        if await self._peer_version(provider) < (
            FederationCapability.MIN_FOR_MEMBER_PICTURE_RESET_SYNC
        ):
            return
        ours = set(await self._space_repo.list_local_member_user_ids(space_id))
        held = (
            await self._profile_picture_repo.list_member_picture_user_ids(space_id)
            if self._profile_picture_repo is not None
            else set()
        )
        event = _sync_event(space_id, "members", provider)
        for uid in reset:
            if uid in ours:
                continue
            seat = await self._space_repo.get_member(space_id, uid)
            if seat is None or (seat.picture_hash is None and uid not in held):
                continue
            if not await self._authorship.acts_for(event, space_id, uid, any_role=True):
                continue
            if uid in held and self._profile_picture_repo is not None:
                await self._profile_picture_repo.clear_member_picture(space_id, uid)
            if seat.picture_hash is not None:
                await self._space_repo.set_member_profile(
                    space_id, uid, picture_hash=None
                )
                await self._publish_member_picture(
                    space_id, seat, picture_hash=None, provider=provider
                )
            log.info(
                "space sync: %s reset %s's per-space picture in %s — dropped "
                "the stale copy held here",
                provider,
                uid,
                space_id,
            )

    async def _publish_member_picture(
        self,
        space_id: str,
        seat: SpaceMember,
        *,
        picture_hash: str | None,
        provider: str,
    ) -> None:
        """Tell open clients a catch-up changed ``seat``'s per-space picture
        — the event the live path publishes, so the realtime frame and the
        SPA's member cache follow. ``origin_instance_id`` is the provider:
        the per-space profile federation outbound never fans a change
        applied from another household back out (no echo). Called only when
        the seat's hash actually changed, so a catch-up that re-ships every
        picture nudges once per changed member, never per record."""
        await self._bus.publish(
            SpaceMemberProfileUpdated(
                space_id=space_id,
                user_id=seat.user_id,
                space_display_name=seat.space_display_name,
                picture_hash=picture_hash,
                origin_instance_id=provider,
            )
        )

    async def _keep_owner_statement(self, space_id: str, statement: object) -> None:
        if not isinstance(statement, dict):
            return
        space = await self._space_repo.get(space_id)
        if space is None:
            return
        try:
            await apply_owner_statement_from_metadata(
                self._space_repo,
                space_id,
                meta={
                    "owner_statement": statement,
                    "identity_public_key": space.identity_public_key,
                },
                owner_instance_id=space.owner_instance_id,
                authority_key_epoch=space.authority_key_epoch,
            )
        except Exception:  # pragma: no cover — defensive
            log.exception("sync: keeping the owner statement for %s failed", space_id)

    async def _spare_owner_people(
        self,
        resource: str,
        space_id: str,
        records: list[dict[str, Any]],
        provider: str,
    ) -> list[dict[str, Any]]:
        """Roster and ban records from a provider that is NOT the owner — a
        trusted inviting admin, whose stream is otherwise taken like the
        host's (migration 0093) — never touch the owner household's people
        (the owner seat or any seat the owner's household holds) and never
        name anybody ``owner``. Only the owner decides about its own."""
        if resource not in ("members", "bans") or not records:
            return records
        space = await self._space_repo.get(space_id)
        if space is None or space.owner_instance_id == provider:
            return records
        seat_repo = self._authorship.seat_repo if self._authorship else None
        kept: list[dict[str, Any]] = []
        for r in records:
            if resource == "members" and str(r.get("role") or "") == (
                SpaceRole.OWNER.value
            ):
                continue
            if await touches_owner_household(
                self._space_repo,
                seat_repo,
                space,
                user_id=str(r.get("user_id") or ""),
            ):
                continue
            kept.append(r)
        if len(kept) != len(records):
            log.warning(
                "space sync: dropped %d %s record(s) from non-owner %s for %s — "
                "they named the owner household's people",
                len(records) - len(kept),
                resource,
                provider,
                space_id,
            )
        return kept
