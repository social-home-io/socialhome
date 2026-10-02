"""Rotate a space's authority key when an admin household is revoked (v_44).

With ``delegated_admin_authority`` on, the owner shares the space's Ed25519
seed — the space authority key every config / roster / rekey / GFS-relay
signature verifies against — with each admin household. Before v_44 nothing
took it back: a household demoted from admin stayed a co-authority forever.

This service closes that, on both ends:

**Owner host** (:meth:`SpaceAuthorityRotationService.rotate`). Triggered by
:class:`~socialhome.domain.events.SpaceAdminAuthorityRevoked` when a
household loses its LAST admin seat while delegation is on (demotion,
removal, ban, or the host learning from roster gossip that an admin left or
was removed while it was offline), and unconditionally when delegation goes
on → off. Under a per-space lock it

1. mints a fresh keypair at ``authority_key_epoch + 1`` (one compare-and-set
   write: pubkey, KEK-wrapped seed and epoch move together);
2. signs an ``authority_cert`` with the OWNER HOUSEHOLD's identity key —
   never the old space key, which the demoted household holds and could use
   to sign a competing rotation;
3. rotates the content key, the rekey signed with the NEW key;
4. sends each member household ``SPACE_AUTHORITY_ROTATED`` (cert + the
   owner's baseline config, roster and content key, all signed with the new
   key), encrypted per household; a household below v_44 gets the content
   key over the unsigned owner path instead;
5. re-shares the new seed (with the cert) to the remaining admin households
   when delegation is still on;
6. re-publishes a public/global space so each GFS re-pins from the cert,
   then re-seals the content key to GFS subscribers.

**Member household** (:meth:`_on_rotated`). Requires the bundle to come from
the space's owner household, applies the cert (shared rules in
:mod:`.space_authority_pin`), then resets to the owner's baseline — config
past last-writer-wins, roster past the version guard, content key past the
``rotated_by`` tiebreak — because the revoked household could have inflated
all three with the old key.

Residual windows, stated plainly: an offline owner rotates nothing until it
is back; before a receiver applies the cert it still accepts old-key
signatures; history the revoked household already read stays read.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import weakref
from dataclasses import replace
from typing import TYPE_CHECKING

from ..authority_cert import sign_authority_cert
from ..authority_sig import (
    UnsupportedAuthoritySuite,
    sign_authority_event,
    strip_authority_sig_fields,
    verify_authority_event,
)
from ..crypto import generate_identity_keypair
from ..domain.events import (
    SpaceAdminAuthorityRevoked,
    SpaceAdminSeedsRetiredAfterRestore,
    SpaceRemoteSeatLive,
)
from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.space import PUBLIC_SPACE_TIERS, SpaceRole, mirrorable_remote_role
from ..infrastructure.event_bus import EventBus
from .space_authority_pin import apply_authority_cert
from .space_crypto_service import KEY_SUITE_AESGCM_256, SUPPORTED_KEY_SUITES
from .space_service import (
    space_metadata_for_federation,
    keep_local_space_state,
    stub_space_from_metadata,
)

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from .space_crypto_service import SpaceContentEncryption
    from .space_service import SpaceService

log = logging.getLogger(__name__)

#: Authority event types the bundle's parts are signed under — the SAME
#: names the ordinary config / rekey verifiers use, so a part can never be
#: lifted from one context into another.
_CONFIG_EVENT = "space_config_changed"
_REKEY_EVENT = "space_key_exchange_rekey"

#: Upper bound on the roster entries one bundle may make us verify.
_MAX_BUNDLE_ROSTER_ENTRIES = 5000


class SpaceAuthorityRotationService:
    """Owner-side rotation + member-side bundle application (v_44)."""

    __slots__ = (
        "_spaces",
        "_remote_members",
        "_bus",
        "_own_instance_id",
        "_federation",
        "_federation_repo",
        "_space_crypto",
        "_space_service",
        "_gfs",
        "_subscriber_keys",
        "_locks",
    )

    def __init__(
        self,
        *,
        space_repo: "AbstractSpaceRepo",
        remote_member_repo: "AbstractSpaceRemoteMemberRepo",
        bus: EventBus,
        own_instance_id: str,
    ) -> None:
        self._spaces = space_repo
        self._remote_members = remote_member_repo
        self._bus = bus
        self._own_instance_id = own_instance_id
        self._federation: "FederationService | None" = None
        self._federation_repo: "AbstractFederationRepo | None" = None
        self._space_crypto: "SpaceContentEncryption | None" = None
        self._space_service: "SpaceService | None" = None
        self._gfs = None
        self._subscriber_keys = None
        #: Per-space rotation / bundle locks. Weak values: a lock nobody holds
        #: or waits on is dropped, so the map never grows with the number of
        #: spaces ever touched.
        self._locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
            weakref.WeakValueDictionary()
        )

    # ── Wiring ──────────────────────────────────────────────────────────

    def attach_federation(
        self,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
    ) -> None:
        self._federation = federation_service
        self._federation_repo = federation_repo

    def attach_space_crypto(self, space_crypto: "SpaceContentEncryption") -> None:
        self._space_crypto = space_crypto

    def attach_space_service(self, space_service: "SpaceService") -> None:
        """The roster snapshot builder and the seed share live there."""
        self._space_service = space_service

    def attach_gfs(self, gfs_service) -> None:
        """Optional: re-publish public/global spaces so each GFS re-pins."""
        self._gfs = gfs_service

    def attach_subscriber_keys(self, subscriber_key_outbound) -> None:
        """Optional: re-seal the rotated content key to GFS subscribers."""
        self._subscriber_keys = subscriber_key_outbound

    def wire(self) -> None:
        self._bus.subscribe(SpaceAdminAuthorityRevoked, self._on_revoked)

    def attach_to(self, federation_service: "FederationService") -> None:
        federation_service._event_registry.register(  # noqa: SLF001
            FederationEventType.SPACE_AUTHORITY_ROTATED,
            self._on_rotated,
        )

    # ── Owner: triggers ─────────────────────────────────────────────────

    async def _on_revoked(self, event: SpaceAdminAuthorityRevoked) -> None:
        """Rotate when a household that may hold the CURRENT seed lost its
        last admin seat.

        "May hold the current seed": delegation is on (the owner shares it
        with every admin household), or a seed was shared at the current key
        epoch and delegation was turned off without that share being retired
        (``authority_seed_shared_epoch``). A household that keeps any live
        admin seat keeps the key.
        """
        space = await self._spaces.get(event.space_id)
        if space is None or space.owner_instance_id != self._own_instance_id:
            return
        if event.instance_id is not None:
            if not space.features.delegated_admin_authority:
                shared = await self._spaces.get_seed_shared_epoch(space.id)
                if shared is None or shared != space.authority_key_epoch:
                    return  # no seed for the current key is out there
            seats = await self._remote_members.list_for_instance(
                space.id, event.instance_id, include_tombstoned=False
            )
            if any(r.role == SpaceRole.ADMIN for r in seats):
                return  # the household keeps another admin seat
        await self.rotate(space.id)

    async def rotate_hosted_after_restore(self) -> int:
        """Rotate every space we host that has an authority history — a
        rotated key (epoch > 0) or delegation on — after a backup or
        Recovery Kit restore.

        The restored row may carry a key that an admin revoked since the
        backup still holds, and an epoch the members already moved past.
        A fresh rotation (whose epoch is at least wall-clock seconds, see
        :meth:`rotate`) puts the owner ahead again.

        The restored roster and config are STALE — a household kicked
        after the backup is listed live again, a later config edit is
        missing — so this rotation is NOT a baseline: the bundle carries
        the cert and a fresh content key only, and members keep the roster
        and config they hold. The restored roster may also name admins
        revoked since the backup, so delegated admin is turned OFF (the
        new seed goes to nobody, and no later revocation shares one) until
        the owner reviews the admins and turns it back on. Returns how many
        spaces rotated.
        """
        done = 0
        for space in await self._spaces.list_all():
            if space.owner_instance_id != self._own_instance_id:
                continue
            delegated = space.features.delegated_admin_authority
            if space.authority_key_epoch <= 0 and not delegated:
                continue
            if delegated:
                # Local only: a config broadcast would push the restored
                # (stale) config onto every member as the newest edit.
                await self._spaces.save(
                    replace(
                        space,
                        features=replace(
                            space.features, delegated_admin_authority=False
                        ),
                    )
                )
            if (
                await self.rotate(space.id, share_seed=False, baseline=False)
                is not None
            ):
                done += 1
            if delegated:
                await self._bus.publish(
                    SpaceAdminSeedsRetiredAfterRestore(space_id=space.id)
                )
        return done

    def _lock(self, space_id: str) -> asyncio.Lock:
        lock = self._locks.get(space_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[space_id] = lock
        return lock

    # ── Owner: rotation ─────────────────────────────────────────────────

    async def rotate(
        self, space_id: str, *, share_seed: bool = True, baseline: bool = True
    ) -> int | None:
        """Rotate ``space_id``'s authority key. Owner host only.

        ``baseline=False`` (post-restore) ships the cert and a fresh content
        key only: no config or roster, and receivers reset nothing — the
        owner's own state is the stale one.

        Returns the new ``key_epoch``, or ``None`` when nothing was rotated
        (not our space, federation not wired, or a concurrent rotation won
        the compare-and-set). Delivery failures are logged, never raised —
        the local rotation stands and the cert reaches a household that
        missed the bundle on the next config edit or roster heal.
        """
        fed = self._federation
        if fed is None:
            log.warning("authority rotation for %s: federation not wired", space_id)
            return None
        async with self._lock(space_id):
            space = await self._spaces.get(space_id)
            if space is None or space.owner_instance_id != self._own_instance_id:
                return None
            prior_epoch = space.authority_key_epoch
            kp = generate_identity_keypair()
            # At least wall-clock seconds: an owner restored from a backup
            # taken before a rotation must still issue a HIGHER epoch than
            # the one its members already hold (they refuse anything else).
            epoch = max(space.authority_key_epoch + 1, int(time.time()))
            if not await self._spaces.rotate_authority_key(
                space_id,
                public_key_hex=kp.public_key.hex(),
                seed=kp.private_key,
                key_epoch=epoch,
            ):
                log.warning(
                    "authority rotation for %s lost a race at epoch %d", space_id, epoch
                )
                return None
            space = await self._spaces.get(space_id) or space
            cert = sign_authority_cert(
                space_id=space_id,
                owner_instance_id=self._own_instance_id,
                owner_seed=fed.own_identity_seed,
                owner_pk_hex=fed.own_identity_pk.hex(),
                authority_pk_hex=kp.public_key.hex(),
                key_epoch=epoch,
            )
            log.info("space %s: rotated the authority key to epoch %d", space_id, epoch)
            # After a restore the members may hold content epochs the backup
            # never saw: start above any of them (wall-clock seconds, like
            # the key epoch) so the new key is everyone's current one.
            content_key = await self._rotate_content_key(
                space_id, min_epoch=None if baseline else int(time.time())
            )
            # The remaining admins' new seed goes out FIRST: their bundle
            # clears the old seed, and a share that trailed it (or was lost)
            # would leave a legitimate admin without one.
            if (
                share_seed
                and space.features.delegated_admin_authority
                and self._space_service is not None
            ):
                for inst in await self._remote_members.list_admin_instances(space_id):
                    await self._space_service.share_admin_signing_seed(
                        space, instance_id=inst
                    )
            await self._distribute(
                space,
                cert,
                kp.private_key,
                content_key,
                baseline=baseline,
                prior_key_epoch=prior_epoch,
            )
            await self._refresh_gfs(space)
            return epoch

    async def _rotate_content_key(
        self, space_id: str, *, min_epoch: int | None = None
    ) -> dict | None:
        """Mint a new content epoch (at least ``min_epoch``); return its bare
        (unsigned) key meta."""
        crypto = self._space_crypto
        if crypto is None:
            return None
        try:
            await crypto.rotate_epoch(space_id, min_epoch=min_epoch)
            exported = await crypto.export_current_key(space_id)
        except Exception:
            log.exception(
                "authority rotation: content-key rotation failed for %s", space_id
            )
            return None
        if exported is None:
            return None
        epoch, raw = exported
        return {
            "epoch": epoch,
            "key_suite": KEY_SUITE_AESGCM_256,
            "key_base64": base64.b64encode(raw).decode("ascii"),
            "rotated_by": self._own_instance_id,
        }

    async def _distribute(
        self,
        space: "Space",
        cert: dict,
        seed: bytes,
        content_key: dict | None,
        *,
        baseline: bool = True,
        prior_key_epoch: int = 0,
    ) -> None:
        fed = self._federation
        fed_repo = self._federation_repo
        if fed is None or fed_repo is None:
            return
        signed_key: dict | None = None
        if content_key is not None:
            signed_key = {
                **content_key,
                **sign_authority_event(
                    event_type=_REKEY_EVENT,
                    space_id=space.id,
                    payload=strip_authority_sig_fields(content_key),
                    space_seed=seed,
                ),
            }
        meta = space_metadata_for_federation(space, authority_cert=cert)
        meta["config_author_instance"] = self._own_instance_id
        meta.update(
            sign_authority_event(
                event_type=_CONFIG_EVENT,
                space_id=space.id,
                payload=strip_authority_sig_fields(meta),
                space_seed=seed,
            )
        )
        for inst in await fed_repo.list_member_instance_ids(space.id):
            if inst == self._own_instance_id:
                continue
            # Only households that still hold a live seat. A household whose
            # last seat was just tombstoned (by gossip, a self-removal, …) may
            # linger in ``space_instances``; it must get neither the new
            # content key nor the config or roster.
            if not await self._remote_members.list_for_instance(
                space.id, inst, include_tombstoned=False
            ):
                continue
            rotation_aware = await fed.peer_supports(
                inst,
                min_version=FederationCapability.MIN_FOR_SPACE_AUTHORITY_ROTATION,
            )
            # A mesh-only member has no ``remote_instances`` row, so its
            # version is unknown: send it the bundle (an older one drops the
            # unknown type; the mesh has no outbox to clog) AND the legacy
            # rekey below.
            unknown = (await fed_repo.get_instance(inst)) is None
            if rotation_aware or unknown:
                payload: dict = {
                    "space_id": space.id,
                    "authority_cert": cert,
                    "space_meta": meta,
                    "roster_entries": await self._roster_for(space, seed, inst),
                    "roster_version": space.roster_sequence,
                }
                if not baseline:
                    # Post-restore: the snapshot above is the STALE restored
                    # one. A member resets to it only when it missed the
                    # baseline of an earlier rotation (see
                    # :meth:`_missed_baseline_cutoff`); ``prior_key_epoch`` —
                    # the epoch this rotation replaces — lets a member that
                    # never even saw that rotation's cert notice.
                    payload["baseline"] = False
                    payload["prior_key_epoch"] = prior_key_epoch
                if signed_key is not None:
                    payload["space_content_key"] = signed_key
                await self._send(
                    inst, FederationEventType.SPACE_AUTHORITY_ROTATED, payload, space.id
                )
            if not rotation_aware and content_key is not None:
                # Below v_44 the household still pins the old key: a
                # new-key signature would be refused, so the owner ships the
                # content key unsigned over the owner-from-instance path.
                await self._send(
                    inst,
                    FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
                    {"space_id": space.id, "space_content_key": dict(content_key)},
                    space.id,
                )

    async def _roster_for(self, space: "Space", seed: bytes, inst: str) -> list[dict]:
        if self._space_service is None:
            return []
        try:
            return await self._space_service.roster_snapshot_entries(
                space, seed=seed, to_instance_id=inst
            )
        except Exception:
            log.exception("authority rotation: roster build failed for %s", space.id)
            return []

    async def _send(
        self,
        to: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str,
    ) -> None:
        assert self._federation is not None
        try:
            await self._federation.send_with_mesh_fallback(
                to_instance_id=to,
                event_type=event_type,
                payload=payload,
                space_id=space_id,
            )
        except Exception:
            log.exception(
                "authority rotation: %s to %s failed for %s",
                event_type.value,
                to,
                space_id,
            )

    async def _refresh_gfs(self, space: "Space") -> None:
        """Re-publish a public/global space to every GFS that already lists
        it (each re-pins from the cert), THEN re-seal the content key to
        subscribers — the handoff is signed with the new key, which a GFS
        only accepts once it re-pinned."""
        if space.space_type not in PUBLIC_SPACE_TIERS:
            return
        if self._gfs is not None:
            try:
                await self._gfs.republish_space(space.id)
            except Exception:
                log.exception(
                    "authority rotation: GFS re-publish failed for %s", space.id
                )
        if self._subscriber_keys is not None:
            try:
                await self._subscriber_keys.reconcile_space_everywhere(space.id)
            except Exception:
                log.exception(
                    "authority rotation: subscriber re-seal failed for %s", space.id
                )

    # ── Member: applying the bundle ─────────────────────────────────────

    async def _on_rotated(self, event: FederationEvent) -> None:
        p = event.payload if isinstance(event.payload, dict) else {}
        space_id = str(p.get("space_id") or "") or (event.space_id or "")
        if not space_id:
            return
        # One bundle per space at a time: two copies (a redelivery racing
        # the original) must not interleave their resets.
        async with self._lock(space_id):
            await self._apply_bundle(space_id, p, event.from_instance)

    async def _apply_bundle(self, space_id: str, p: dict, sender: str) -> None:
        space = await self._spaces.get(space_id)
        if space is None:
            log.warning(
                "SPACE_AUTHORITY_ROTATED for unknown space %s from %s — dropped",
                space_id,
                sender,
            )
            return
        if space.owner_instance_id == self._own_instance_id:
            return  # our own space: our pin is the authority
        if sender != space.owner_instance_id:
            # The cert is owner-signed, but the baseline reset below goes past
            # every ordinary ordering guard — only the owner itself may ask
            # for that, never a household relaying its cert.
            log.warning(
                "SPACE_AUTHORITY_ROTATED for %s from %s, not its owner %s — dropped",
                space_id,
                sender,
                space.owner_instance_id,
            )
            return
        outcome = await apply_authority_cert(
            self._spaces,
            space,
            p.get("authority_cert"),
            own_instance_id=self._own_instance_id,
        )
        if not outcome.consistent:
            log.warning(
                "SPACE_AUTHORITY_ROTATED for %s: cert %s — dropped",
                space_id,
                outcome.value,
            )
            return
        space = await self._spaces.get(space_id) or space
        # Read before the claim overwrites it. ``owed`` is durable: it was
        # recorded when the pin moved past a rotated key whose bundle we
        # never claimed — by the cert above, or by an inline cert any
        # household relayed earlier — so who delivered the newer cert first
        # cannot hide a missed baseline.
        baseline_epoch, owed_epoch = await self._spaces.get_authority_baseline(space_id)
        # At most ONE baseline reset per key epoch (a redelivered or replayed
        # bundle is a no-op), claimed durably before any write.
        if not await self._spaces.claim_authority_baseline(
            space_id, space.authority_key_epoch
        ):
            log.info(
                "SPACE_AUTHORITY_ROTATED for %s: baseline for epoch %d already "
                "applied — nothing to reset",
                space_id,
                space.authority_key_epoch,
            )
            return
        cutoff = space.authority_key_epoch
        if p.get("baseline") is False:
            # A post-restore rotation: the owner's roster and config are the
            # stale ones. Adopt the key, import the content key as an
            # ordinary owner rekey, and reset nothing — UNLESS this household
            # missed the baseline of an earlier rotation, in which case state
            # the key it retired could have inflated is still here.
            missed = self._missed_baseline_cutoff(
                owed_epoch=owed_epoch,
                baseline_epoch=baseline_epoch,
                prior_key_epoch=p.get("prior_key_epoch"),
                new_epoch=space.authority_key_epoch,
            )
            if missed is None:
                await self._import_content_key(space, p.get("space_content_key"))
                return
            log.warning(
                "SPACE_AUTHORITY_ROTATED for %s: missed the baseline for key "
                "epoch %d (last applied %d) — resetting to the owner's snapshot",
                space_id,
                missed,
                baseline_epoch,
            )
            cutoff = missed
        # Reset to the owner's baseline. Each part is checked against the new
        # pin, and each only overrides state written under a key older than
        # ``cutoff`` — what the revoked household may have inflated. State
        # this household already accepted under the new key (it adopted the
        # cert inline, then newer owner traffic arrived before this bundle)
        # stays.
        await self._reset_config(space, p.get("space_meta"), sender, cutoff)
        await self._reset_roster(
            space,
            p.get("roster_entries"),
            p.get("roster_version"),
            cutoff,
            # The catch-up snapshot is the STALE restored roster: it may
            # still list a household this one removed after the backup.
            # A removal never grants anything, so it stands.
            keep_newer_removals=p.get("baseline") is False,
        )
        await self._reset_content_key(space, p.get("space_content_key"), cutoff)

    @staticmethod
    def _missed_baseline_cutoff(
        *,
        owed_epoch: int,
        baseline_epoch: int,
        prior_key_epoch: object,
        new_epoch: int,
    ) -> int | None:
        """The key epoch whose baseline this household missed, or ``None``.

        Two ways to have missed one, both below the new epoch:

        * we pinned a rotated key and moved past it without claiming its
          bundle — ``owed_epoch``, recorded durably by
          :meth:`AbstractSpaceRepo.adopt_authority_key`;
        * the owner rotated to ``prior_key_epoch`` and we never saw that cert
          at all (``baseline_epoch < prior_key_epoch``).

        State written under a key older than the returned epoch is what a
        household revoked by that rotation may have inflated; the reset
        overrides exactly that. A rotation the restored owner forgot AND we
        never saw is not detectable here (see docs/protocol/spaces.md).
        """
        candidates = [owed_epoch]
        if isinstance(prior_key_epoch, int) and not isinstance(prior_key_epoch, bool):
            candidates.append(prior_key_epoch)
        missed = [e for e in candidates if baseline_epoch < e < new_epoch]
        return max(missed) if missed else None

    def _authority_ok(self, space: "Space", event_type: str, part: object) -> bool:
        if not isinstance(part, dict):
            return False
        sig = str(part.get("authority_sig") or "")
        suite = str(part.get("authority_sig_suite") or "")
        if not sig or not suite:
            return False
        try:
            return verify_authority_event(
                event_type=event_type,
                space_id=space.id,
                payload=strip_authority_sig_fields(part),
                authority_sig=sig,
                authority_sig_suite=suite,
                space_public_key=bytes.fromhex(space.identity_public_key),
            )
        except UnsupportedAuthoritySuite, ValueError:
            return False

    async def _reset_config(
        self, space: "Space", meta: object, owner: str, cutoff: int
    ) -> None:
        if not self._authority_ok(space, _CONFIG_EVENT, meta):
            log.warning(
                "SPACE_AUTHORITY_ROTATED for %s: config not signed by the new key",
                space.id,
            )
            return
        assert isinstance(meta, dict)
        if space.archived_reason:
            return  # remote-terminated: a config snapshot never revives it
        refreshed = stub_space_from_metadata(
            space.id, host_instance_id=owner, meta=meta
        )
        refreshed = keep_local_space_state(
            refreshed, existing=space, meta=meta, we_host=False
        )
        # Conditional in the SAME transaction as the save: a config applied
        # under the new key in the meantime (an inline-cert edit racing this
        # bundle) already stands and is never rolled back.
        if not await self._spaces.save_config_baseline(
            refreshed,
            author=owner,
            epoch=space.authority_key_epoch,
            older_than=cutoff,
        ):
            return
        if space.features.delegated_admin_authority and not (
            refreshed.features.delegated_admin_authority
        ):
            await self._spaces.clear_space_seed(space.id)

    async def _reset_roster(
        self,
        space: "Space",
        entries: object,
        roster_version: object,
        cutoff: int,
        *,
        keep_newer_removals: bool = False,
    ) -> None:
        if not isinstance(entries, list):
            return
        # A list longer than we are willing to verify is applied up to the cap
        # but then never used to TOMBSTONE anything: seats past the cut were
        # simply not looked at, and must not read as "missing".
        truncated = len(entries) > _MAX_BUNDLE_ROSTER_ENTRIES
        candidates: list[tuple[dict, bool]] = []
        for entry in entries[:_MAX_BUNDLE_ROSTER_ENTRIES]:
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload")
            raw_type = str(entry.get("event_type") or "")
            if not isinstance(payload, dict) or payload.get("space_id") != space.id:
                continue
            if raw_type == FederationEventType.SPACE_MEMBER_JOINED.value:
                candidates.append((payload, False))
            elif raw_type == FederationEventType.SPACE_MEMBER_LEFT.value:
                candidates.append((payload, True))
        verified = await asyncio.to_thread(self._verify_entries_sync, space, candidates)
        seen: set[tuple[str, str]] = set()
        for payload, tombstoned in verified:
            user_id = str(payload.get("user_id") or "")
            instance_id = str(payload.get("instance_id") or "")
            if not user_id or not instance_id or instance_id == self._own_instance_id:
                continue
            try:
                version = int(payload.get("member_version") or 0)
            except TypeError, ValueError:
                continue
            seen.add((instance_id, user_id))
            current = await self._remote_members.get_including_tombstones(
                space.id, instance_id, user_id
            )
            if current is not None and current.authority_epoch >= cutoff:
                continue  # written under a key not being retired — leave it
            if (
                keep_newer_removals
                and not tombstoned
                and current is not None
                and current.tombstoned
                and current.member_version >= version
            ):
                continue  # removed after the (restored) snapshot — keep it
            await self._remote_members.reset_member_state(
                space_id=space.id,
                user_id=user_id,
                instance_id=instance_id,
                display_name=(
                    str(payload["display_name"])
                    if payload.get("display_name")
                    else None
                ),
                user_pk=str(payload["user_pk"]) if payload.get("user_pk") else None,
                role=mirrorable_remote_role(payload.get("role")),
                member_version=version,
                tombstoned=tombstoned,
            )
            if not tombstoned:
                await self._spaces.add_space_instance(space.id, instance_id)
                await self._bus.publish(
                    SpaceRemoteSeatLive(
                        space_id=space.id, instance_id=instance_id, user_id=user_id
                    )
                )
        if not verified or truncated:
            # Never tombstone the whole mirror on an empty / unverified list,
            # nor seats past a truncated scan.
            return
        tomb_version = (
            roster_version
            if isinstance(roster_version, int) and not isinstance(roster_version, bool)
            else 0
        )
        # Seats of OTHER households the owner does not list were invented (or
        # kept alive) with the revoked key: tombstone them at the owner's
        # roster version, so any later owner gossip out-ranks the tombstone.
        for row in await self._remote_members.list_for_space_including_tombstones(
            space.id
        ):
            if row.instance_id == self._own_instance_id:
                continue
            if (row.instance_id, row.user_id) in seen or row.tombstoned:
                continue
            if row.authority_epoch >= cutoff:
                continue  # seated under a key the revoked household never held
            await self._remote_members.reset_member_state(
                space_id=space.id,
                user_id=row.user_id,
                instance_id=row.instance_id,
                display_name=row.display_name,
                user_pk=row.user_pk,
                role=row.role,
                member_version=tomb_version,
                tombstoned=True,
            )

    def _verify_entries_sync(
        self, space: "Space", candidates: list[tuple[dict, bool]]
    ) -> list[tuple[dict, bool]]:
        """Worker thread: keep only entries signed with the new key."""
        out: list[tuple[dict, bool]] = []
        for payload, tombstoned in candidates:
            event_type = (
                FederationEventType.SPACE_MEMBER_LEFT
                if tombstoned
                else FederationEventType.SPACE_MEMBER_JOINED
            ).value
            if self._authority_ok(space, event_type, payload):
                out.append((payload, tombstoned))
        return out

    def _content_key_parts(
        self, space: "Space", meta: object
    ) -> tuple[int, bytes, str] | None:
        """The bundle's content key as ``(epoch, raw, rotated_by)`` — only
        when it is signed with the new key and well-formed."""
        if meta is None or self._space_crypto is None:
            return None
        if not self._authority_ok(space, _REKEY_EVENT, meta):
            log.warning(
                "SPACE_AUTHORITY_ROTATED for %s: content key not signed by the new key",
                space.id,
            )
            return None
        assert isinstance(meta, dict)
        if meta.get("key_suite") not in SUPPORTED_KEY_SUITES:
            log.warning(
                "SPACE_AUTHORITY_ROTATED for %s: unknown key_suite %r — key ignored",
                space.id,
                meta.get("key_suite"),
            )
            return None
        rotated_by = meta.get("rotated_by")
        epoch = meta.get("epoch")
        if (
            not isinstance(rotated_by, str)
            or not rotated_by.strip()
            or isinstance(epoch, bool)
            or not isinstance(epoch, int)
        ):
            return None
        try:
            raw = base64.b64decode(str(meta.get("key_base64") or ""), validate=True)
        except ValueError:
            log.warning(
                "SPACE_AUTHORITY_ROTATED for %s: malformed content key", space.id
            )
            return None
        return epoch, raw, rotated_by

    async def _reset_content_key(
        self, space: "Space", meta: object, cutoff: int
    ) -> None:
        parts = self._content_key_parts(space, meta)
        if parts is None:
            return
        assert self._space_crypto is not None
        epoch, raw, rotated_by = parts
        try:
            await self._space_crypto.reset_to_key(
                space.id,
                epoch,
                raw,
                rotated_by=rotated_by,
                authority_epoch=space.authority_key_epoch,
                older_than=cutoff,
            )
        except ValueError:
            log.warning(
                "SPACE_AUTHORITY_ROTATED for %s: malformed content key", space.id
            )

    async def _import_content_key(self, space: "Space", meta: object) -> None:
        """A non-baseline bundle's content key: an ordinary owner rekey,
        stored only while we still pin the key that signed it."""
        parts = self._content_key_parts(space, meta)
        if parts is None:
            return
        assert self._space_crypto is not None
        epoch, raw, rotated_by = parts
        try:
            await self._space_crypto.import_key(
                space.id,
                epoch,
                raw,
                rotated_by=rotated_by,
                verified_pin=(space.authority_key_epoch, space.identity_public_key),
            )
        except ValueError:
            log.warning(
                "SPACE_AUTHORITY_ROTATED for %s: malformed content key", space.id
            )
