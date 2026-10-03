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

**Authority epoch echo** (v_46). A member household's ``SPACE_SYNC_BEGIN``
to the owner carries ``authority_epoch_echo`` (:meth:`authority_epoch_echo`):
the epoch it pins, the baseline it claimed / owes, and ``forgotten_epoch`` —
a rotation epoch it held that the owner's post-restore bundle showed the
owner no longer knows (its ``prior_key_epoch`` was BELOW the member's pin) —
each with the owner-signed cert for it, kept durably on the member's row.
The owner (:meth:`_on_sync_begin`) never adopts any of it:

* an epoch PROVEN by our own cert that is above our epoch, or a forgotten
  one we have not handled → a fresh rotation past it (at most one per space
  per :data:`ECHO_ROTATION_INTERVAL_S`; a proof inside the window is kept),
  marked ``baseline: false`` with ``forgotten_key_epoch``, so every member
  that missed that rotation resets what the key it retired could have
  inflated;
* without proof, a member behind on the key or its baseline → at most the
  current bundle again, to that member only (once per household and space
  per :data:`ECHO_REACTION_INTERVAL_S`), saying what the stored rotation
  header says.

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
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from ..authority_cert import (
    InvalidAuthorityCert,
    UnsupportedAuthorityCertSuite,
    sign_authority_cert,
    verify_authority_cert,
)
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
    SpaceAuthorityEchoDue,
    SpaceRemoteSeatLive,
)
from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.space import PUBLIC_SPACE_TIERS, SpaceRole, mirrorable_remote_role
from ..infrastructure.event_bus import EventBus
from .space_authority_pin import apply_authority_cert, owner_authority_cert
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
    from .space_writer_cert_service import SpaceWriterCertService

log = logging.getLogger(__name__)

#: Authority event types the bundle's parts are signed under — the SAME
#: names the ordinary config / rekey verifiers use, so a part can never be
#: lifted from one context into another.
_CONFIG_EVENT = "space_config_changed"
_REKEY_EVENT = "space_key_exchange_rekey"

#: Upper bound on the roster entries one bundle may make us verify.
_MAX_BUNDLE_ROSTER_ENTRIES = 5000

#: The owner reacts to one household's authority epoch echo for a space at
#: most this often (v_46). Members echo on every periodic sync (30 min), so
#: a lagging member gets its bundle again about hourly until it applies it.
ECHO_REACTION_INTERVAL_S: float = 3600.0

#: At most one echo-triggered ROTATION per space this often, whichever
#: household asked — so N member households (or one malicious one) cannot
#: drive a rotation storm.
ECHO_ROTATION_INTERVAL_S: float = 6 * 3600.0

#: An echoed epoch more than this far above wall-clock seconds is not one
#: the owner can have issued (a rotation's epoch is ``max(current + 1, unix
#: seconds)``) and is ignored — otherwise one member could push the space's
#: epoch to the 2^63−1 cap and leave nothing to rotate to.
MAX_ECHO_EPOCH_SKEW_S: int = 24 * 3600

#: The echo's fields, all non-negative ints.
_ECHO_FIELDS = ("key_epoch", "baseline_epoch", "owed_epoch", "forgotten_epoch")


def _nonneg_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


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
        "_writer_certs",
        "_locks",
        "_echo_reacted",
        "_echo_rotated",
        "_echo_pending",
        "_tasks",
        "_closed",
        "_restore_pending",
        "_warned_pending",
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
        #: v_49 — re-issues each household's writer cert under the NEW
        #: authority key (owner side) and stores ours (member side).
        self._writer_certs: "SpaceWriterCertService | None" = None
        #: Per-space rotation / bundle locks. Weak values: a lock nobody holds
        #: or waits on is dropped, so the map never grows with the number of
        #: spaces ever touched.
        self._locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
            weakref.WeakValueDictionary()
        )
        #: Owner side (v_46): last reaction per ``(space_id, household)`` and
        #: last echo-triggered rotation per space, monotonic seconds. Only
        #: households holding a live seat get an entry, so both stay bounded
        #: by the hosted rosters. In memory: a restart is not something a
        #: member can trigger.
        self._echo_reacted: dict[tuple[str, str], float] = {}
        self._echo_rotated: dict[str, float] = {}
        #: Owner side: the highest cert-proven forgotten epoch per space that
        #: arrived while the rotation window was closed — rotated past when
        #: it opens (on the next echo for the space), never dropped.
        self._echo_pending: dict[str, int] = {}
        #: Echo-triggered rotations / re-sends in flight (off the dispatch
        #: path); kept so they are not garbage-collected mid-run.
        self._tasks: set[asyncio.Task] = set()
        #: Set by :meth:`stop`: no new echo task may start after cleanup.
        self._closed = False
        #: "A restore has not been rotated for yet" (see
        #: :meth:`attach_restore_gate`); ``None`` = never pending.
        self._restore_pending: Callable[[], Awaitable[bool]] | None = None
        self._warned_pending = False

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

    def attach_writer_certs(self, writer_certs: "SpaceWriterCertService") -> None:
        """Optional (v_49): the bundle carries each household its own writer
        cert signed with the new key; a member stores the one it receives."""
        self._writer_certs = writer_certs

    def attach_subscriber_keys(self, subscriber_key_outbound) -> None:
        """Optional: re-seal the rotated content key to GFS subscribers."""
        self._subscriber_keys = subscriber_key_outbound

    def attach_restore_gate(self, pending: Callable[[], Awaitable[bool]]) -> None:
        """``pending()`` → a restore happened whose post-restore rotation has
        not run yet (``RecoveryReconnectService.authority_rotation_pending``).
        While it holds, authority epoch echoes are deferred: the hosted rows
        are the restored ones, and a rotation off them would share a fresh
        seed with an admin list that may name households revoked since the
        backup."""
        self._restore_pending = pending

    async def _restore_review_pending(self) -> bool:
        if self._restore_pending is None:
            return False
        try:
            pending = await self._restore_pending()
        except Exception:
            log.warning("restore gate check failed — deferring echoes", exc_info=True)
            return True
        if pending and not self._warned_pending:
            # Once per boot: the post-restore rotation failed (it ran before
            # any transport started), so echo healing is paused until the
            # next boot retries it.
            self._warned_pending = True
            log.warning(
                "post-restore authority rotation has not completed — authority "
                "epoch echoes are deferred and new seeds are not shared until "
                "a restart retries it"
            )
        return pending

    def wire(self) -> None:
        self._bus.subscribe(SpaceAdminAuthorityRevoked, self._on_revoked)

    def attach_to(self, federation_service: "FederationService") -> None:
        federation_service._event_registry.register(  # noqa: SLF001
            FederationEventType.SPACE_AUTHORITY_ROTATED,
            self._on_rotated,
        )
        # A second, independent handler next to the sync admission one: the
        # echo is read whether or not the sync itself is admitted.
        federation_service._event_registry.register(  # noqa: SLF001
            FederationEventType.SPACE_SYNC_BEGIN,
            self._on_sync_begin,
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
        # A revocation never waits — but while a restore has not been
        # rotated for, the restored admin list may name households revoked
        # since the backup: rotate, and share the new seed with nobody.
        await self.rotate(space.id, share_seed=not await self._restore_review_pending())

    async def rotate_hosted_after_restore(self, marker: str | None = None) -> int:
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

        ``marker`` identifies the restore. Each space records the marker its
        post-restore rotation ran for, so a retry after a partial failure
        rotates only the spaces still missing — re-rotating one that already
        did would overwrite its restore window with one that holds nothing
        forgotten (v_46).
        """
        done = 0
        for space in await self._spaces.list_all():
            if space.owner_instance_id != self._own_instance_id:
                continue
            if marker is not None:
                header = await self._spaces.get_authority_echo(space.id)
                if header.get("restore_marker") == marker:
                    continue  # already rotated for this restore
            delegated = space.features.delegated_admin_authority
            if (
                space.authority_key_epoch <= 0
                and not delegated
                and await self._spaces.get_seed_shared_epoch(space.id) is None
            ):
                # No authority history. (A seed marked shared counts: a run
                # that turned delegation off and then failed before rotating
                # must still rotate on the retry.)
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
                await self.rotate(
                    space.id,
                    share_seed=False,
                    baseline=False,
                    after_restore=True,
                    restore_marker=marker,
                )
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
        self,
        space_id: str,
        *,
        share_seed: bool = True,
        baseline: bool = True,
        min_epoch: int | None = None,
        forgotten_key_epoch: int | None = None,
        after_restore: bool = False,
        restore_marker: str | None = None,
    ) -> int | None:
        """Rotate ``space_id``'s authority key. Owner host only.

        ``after_restore`` marks the post-restore rotation: its header records
        the restore window ``(restore_prior, restore_epoch)`` — the only
        epochs a member can prove we forgot (v_46). No other rotation moves
        the window.

        ``baseline=False`` (post-restore) ships the cert and a fresh content
        key only: no config or roster, and receivers reset nothing — the
        owner's own state is the stale one. ``min_epoch`` raises the new
        epoch's floor (past an epoch a member echoed, v_46);
        ``forgotten_key_epoch`` names a rotation the owner forgot, so a
        member that missed it resets what it retired.

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
            epoch = max(space.authority_key_epoch + 1, int(time.time()), min_epoch or 0)
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
            # Durable header of this rotation (v_46): a bundle re-sent later
            # (:meth:`_resend_bundle`) must say what the original said, and
            # a forgotten epoch already handled is not acted on twice.
            await self._spaces.remember_authority_cert(
                space_id,
                key_epoch=epoch,
                public_key_hex=kp.public_key.hex(),
                cert=cert,
            )
            previous = await self._spaces.get_authority_echo(space_id)
            if after_restore:
                window: dict = {"restore_prior": prior_epoch, "restore_epoch": epoch}
                if restore_marker is not None:
                    window["restore_marker"] = restore_marker
            else:
                window = {
                    k: previous[k]
                    for k in ("restore_prior", "restore_epoch")
                    if _nonneg_int(previous.get(k)) is not None
                }
                if isinstance(previous.get("restore_marker"), str):
                    window["restore_marker"] = previous["restore_marker"]
            await self._spaces.set_authority_echo(
                space_id,
                {
                    **window,
                    "epoch": epoch,
                    "baseline": baseline,
                    "prior_key_epoch": prior_epoch,
                    "forgotten_key_epoch": forgotten_key_epoch or 0,
                    "max_forgotten": max(
                        _nonneg_int(previous.get("max_forgotten")) or 0,
                        forgotten_key_epoch or 0,
                    ),
                    "max_baseline": max(
                        _nonneg_int(previous.get("max_baseline")) or 0,
                        epoch if baseline else 0,
                    ),
                },
            )
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
                forgotten_key_epoch=forgotten_key_epoch,
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
        prior_key_epoch: int | None = 0,
        forgotten_key_epoch: int | None = None,
        only_instance: str | None = None,
    ) -> None:
        """Send the bundle to every member household with a live seat — or
        to ``only_instance`` alone (a re-send, v_46). ``prior_key_epoch``
        ``None`` leaves it off a non-baseline bundle."""
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
        targets = (
            [only_instance]
            if only_instance is not None
            else await fed_repo.list_member_instance_ids(space.id)
        )
        for inst in targets:
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
                    if prior_key_epoch is not None:
                        payload["prior_key_epoch"] = prior_key_epoch
                    if forgotten_key_epoch:
                        # v_46: a rotation the owner forgot (a member echoed
                        # it). An older member ignores the field.
                        payload["forgotten_key_epoch"] = forgotten_key_epoch
                if signed_key is not None:
                    payload["space_content_key"] = signed_key
                # v_49 — this household's OWN writer cert, signed with the
                # NEW authority key for the new content epoch: every cert
                # signed with the retired key stops verifying at the re-pin.
                if self._writer_certs is not None:
                    try:
                        writer_cert = await self._writer_certs.cert_for_peer(
                            space.id,
                            inst,
                            epoch=(
                                content_key.get("epoch")
                                if content_key is not None
                                else None
                            ),
                        )
                    except Exception:
                        log.exception(
                            "authority rotation: writer cert for %s failed in %s",
                            inst,
                            space.id,
                        )
                        writer_cert = None
                    if writer_cert is not None:
                        payload["writer_cert"] = writer_cert
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

    # ── Owner: the authority epoch echo (v_46) ──────────────────────────

    async def _on_sync_begin(self, event: FederationEvent) -> None:
        """Read a member household's ``authority_epoch_echo`` off its
        ``SPACE_SYNC_BEGIN``; re-send our own signed state if it shows the
        household behind, or rotate if it PROVES we forgot a rotation.

        The echoed numbers are the member's claim. A rotation needs proof:
        the owner-signed cert for the claimed epoch, verified against our
        own household identity key (which a restore keeps). Without one the
        echo can at most earn the household its current bundle again. Never
        adopted, and bounded: only a household holding a writer seat; at
        most one re-send per household and space per
        :data:`ECHO_REACTION_INTERVAL_S`; at most one echo-triggered rotation
        per space per :data:`ECHO_ROTATION_INTERVAL_S` — a proven epoch
        arriving inside that window is kept and rotated past when it opens;
        an epoch above wall-clock seconds plus :data:`MAX_ECHO_EPOCH_SKEW_S`
        is ignored. The rotation and the re-send run as tasks, off the
        inbound dispatch path.
        """
        p = event.payload if isinstance(event.payload, dict) else {}
        echo = p.get("authority_epoch_echo")
        if echo is None:
            return  # an older member, or nothing to report
        space_id = str(event.space_id or p.get("space_id") or "")
        sender = event.from_instance
        if not space_id or not sender or sender == self._own_instance_id:
            return
        space = await self._spaces.get(space_id)
        if space is None or space.owner_instance_id != self._own_instance_id:
            return
        if await self._restore_review_pending():
            # The restored rows are still current: neither a rotation nor a
            # re-send may be built off them. The member echoes again.
            log.info(
                "authority epoch echo for %s from %s deferred: the post-restore "
                "rotation has not run yet",
                space_id,
                sender,
            )
            return
        parsed = self._parse_echo(echo)
        if parsed is None:
            log.warning(
                "authority epoch echo for %s from %s is malformed — ignored",
                space_id,
                sender,
            )
            return
        seats = await self._remote_members.list_for_instance(
            space_id, sender, include_tombstoned=False
        )
        if not any(r.role != SpaceRole.SUBSCRIBER for r in seats):
            return  # no writer seat: nothing it holds can be owed
        ceiling = int(time.time()) + MAX_ECHO_EPOCH_SKEW_S
        if max(parsed) > ceiling:
            log.warning(
                "authority epoch echo for %s from %s names an epoch above "
                "wall-clock time (%d) — ignored",
                space_id,
                sender,
                max(parsed),
            )
            return
        assert isinstance(echo, dict)
        held, baseline, owed, forgotten = parsed
        own = space.authority_key_epoch
        record = await self._spaces.get_authority_echo(space_id)
        proven: list[int] = []
        if held > own and self._cert_proves(space, echo.get("key_cert"), held):
            proven.append(held)
        if (
            forgotten
            and self._in_restore_window(record, forgotten)
            and self._cert_proves(space, echo.get("forgotten_cert"), forgotten)
        ):
            proven.append(forgotten)
        if proven:
            log.warning(
                "space %s: %s proves authority epoch %d that this household no "
                "longer knows (ours %d) — restored past a rotation?",
                space_id,
                sender,
                max(proven),
                own,
            )
            self._echo_pending[space_id] = max(
                self._echo_pending.get(space_id, 0), *proven
            )
        if self._start_pending_rotation(space_id):
            return
        if not proven and own > 0 and (held < own or baseline < own or owed > 0):
            now = time.monotonic()
            last = self._echo_reacted.get((space_id, sender))
            if last is not None and now - last < ECHO_REACTION_INTERVAL_S:
                return
            self._echo_reacted[(space_id, sender)] = now
            log.info(
                "space %s: %s is behind on the authority key (holds %d, "
                "baseline %d, owes %d; ours %d) — re-sending the bundle",
                space_id,
                sender,
                held,
                baseline,
                owed,
                own,
            )
            self._spawn(self._resend_bundle(space_id, sender), f"resend-{space_id}")

    def _cert_proves(self, space: "Space", cert: object, epoch: int) -> bool:
        """``cert`` is our own household's cert for ``epoch`` of this space."""
        fed = self._federation
        if fed is None or not isinstance(cert, dict):
            return False
        try:
            verified = verify_authority_cert(
                cert,
                space_id=space.id,
                owner_instance_id=self._own_instance_id,
                known_owner_pk_hex=fed.own_identity_pk.hex(),
            )
        except UnsupportedAuthorityCertSuite, InvalidAuthorityCert:
            return False
        return verified.key_epoch == epoch

    @staticmethod
    def _in_restore_window(record: dict, forgotten: int) -> bool:
        """Could ``forgotten`` be an epoch our last restore made us forget?

        Only an epoch STRICTLY between the one the post-restore rotation
        replaced (``restore_prior``, what the backup held) and the one it
        issued (``restore_epoch``) — every other real cert of ours names an
        epoch we issued knowingly, and replaying a superseded one must not
        buy a rotation. Above ``max_forgotten``: never acted on twice. No
        restore recorded → nothing can have been forgotten.
        """
        lo = _nonneg_int(record.get("restore_prior"))
        hi = _nonneg_int(record.get("restore_epoch"))
        if lo is None or hi is None:
            return False
        done = _nonneg_int(record.get("max_forgotten")) or 0
        return lo < forgotten < hi and forgotten > done

    def _start_pending_rotation(self, space_id: str) -> bool:
        """Rotate past the highest proven forgotten epoch kept for
        ``space_id`` — if the per-space window is open. True when one
        started (the caller then has nothing else to do)."""
        pending = self._echo_pending.get(space_id)
        if not pending:
            return False
        now = time.monotonic()
        last = self._echo_rotated.get(space_id)
        if last is not None and now - last < ECHO_ROTATION_INTERVAL_S:
            log.info(
                "space %s: an echo-triggered rotation ran recently — keeping "
                "proven epoch %d for when the window opens",
                space_id,
                pending,
            )
            return False
        del self._echo_pending[space_id]
        self._echo_rotated[space_id] = now
        self._spawn(
            self._rotate_past_forgotten(space_id, pending, opened_after=last),
            f"rotate-{space_id}",
        )
        return True

    def _spawn(self, coro, name: str) -> None:
        if self._closed:
            coro.close()  # never awaited: drop it without a warning
            log.info("authority echo %s dropped: shutting down", name)
            return
        task = asyncio.create_task(coro, name=f"authority-echo-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Wait for echo-triggered rotations / re-sends still running."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    @staticmethod
    def _parse_echo(echo: object) -> tuple[int, int, int, int] | None:
        """``(key, baseline, owed, forgotten)``; ``None`` unless every field
        present is a non-negative int (a missing one reads as 0)."""
        if not isinstance(echo, dict):
            return None
        out: list[int] = []
        for name in _ECHO_FIELDS:
            value = _nonneg_int(echo.get(name, 0))
            if value is None:
                return None
            out.append(value)
        return out[0], out[1], out[2], out[3]

    async def _rotate_past_forgotten(
        self, space_id: str, forgotten: int, *, opened_after: float | None = None
    ) -> None:
        # Not a baseline: a household that forgot a rotation holds restored
        # (stale) state. The bundle names the forgotten epoch instead, and a
        # member resets only what was written under a key older than it.
        epoch: int | None = None
        try:
            # Re-checked here, after the hop off the dispatch path: never
            # rotate (and share a seed) off restored rows.
            if not await self._restore_review_pending():
                epoch = await self.rotate(
                    space_id,
                    baseline=False,
                    min_epoch=forgotten + 1,
                    forgotten_key_epoch=forgotten,
                )
        except Exception:
            log.exception("echo-triggered rotation for %s failed", space_id)
        if epoch is None:
            # Nothing rotated: give the window back and keep the proof, so
            # the next echo retries instead of waiting out a window spent
            # on nothing.
            if opened_after is None:
                self._echo_rotated.pop(space_id, None)
            else:
                self._echo_rotated[space_id] = opened_after
            self._echo_pending[space_id] = max(
                self._echo_pending.get(space_id, 0), forgotten
            )

    async def stop(self, timeout: float = 5.0) -> None:
        """App cleanup: let echo-triggered rotations / re-sends finish (each
        is a handful of DB writes and sends); cancel what is still running
        after ``timeout`` so no task outlives the database. Nothing new
        starts once this was called."""
        self._closed = True
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(self.wait_idle(), timeout=timeout)
        except TimeoutError:
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _resend_bundle(self, space_id: str, inst: str) -> None:
        """Send ``inst`` the bundle for our CURRENT key again — our cert,
        current content key, a fresh snapshot — saying what the original
        said (the stored rotation header). A rotation with no header (made
        before v_46) is re-sent as a baseline: owner state beats state a
        revoked household may have inflated."""
        fed = self._federation
        if fed is None:
            return
        async with self._lock(space_id):
            space = await self._spaces.get(space_id)
            if space is None or space.owner_instance_id != self._own_instance_id:
                return
            seed = await self._spaces.get_space_seed(space_id)
            cert = await self._spaces.get_authority_cert(space_id)
            if cert is None or cert.get("key_epoch") != space.authority_key_epoch:
                cert = owner_authority_cert(
                    space,
                    own_instance_id=self._own_instance_id,
                    owner_seed=fed.own_identity_seed,
                    owner_pk=fed.own_identity_pk,
                )
            if seed is None or cert is None:
                return
            record = await self._spaces.get_authority_echo(space_id)
            known = record.get("epoch") == space.authority_key_epoch
            baseline = bool(record.get("baseline")) if known else True
            await self._distribute(
                space,
                cert,
                seed,
                await self._current_content_key(space_id),
                baseline=baseline,
                prior_key_epoch=_nonneg_int(record.get("prior_key_epoch"))
                if known
                else None,
                forgotten_key_epoch=_nonneg_int(record.get("max_forgotten")) or None,
                only_instance=inst,
            )

    async def _current_content_key(self, space_id: str) -> dict | None:
        crypto = self._space_crypto
        if crypto is None:
            return None
        try:
            exported = await crypto.export_current_key(space_id)
        except Exception:
            log.exception("authority bundle re-send: no content key for %s", space_id)
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

    # ── Member: the authority epoch echo (v_46) ─────────────────────────

    async def authority_epoch_echo(
        self, space_id: str, to_instance_id: str
    ) -> dict | None:
        """The ``authority_epoch_echo`` our ``SPACE_SYNC_BEGIN`` to
        ``to_instance_id`` carries, or ``None``.

        Only to the space's owner, only when it is at or above v_46 (or a
        mesh-only owner whose version is unknown — an older one ignores the
        field), and only once there is something to report. Each claimed
        epoch rides with the owner's cert for it (``key_cert``,
        ``forgotten_cert``) when we hold one — the owner acts on nothing
        else.
        """
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.owner_instance_id == self._own_instance_id
            or to_instance_id != space.owner_instance_id
        ):
            return None
        fed = self._federation
        fed_repo = self._federation_repo
        if fed is None or fed_repo is None:
            return None
        if (
            not await fed.peer_supports(
                to_instance_id,
                min_version=FederationCapability.MIN_FOR_AUTHORITY_EPOCH_ECHO,
            )
            and (await fed_repo.get_instance(to_instance_id)) is not None
        ):
            return None
        baseline, owed = await self._spaces.get_authority_baseline(space_id)
        state = await self._spaces.get_authority_echo(space_id)
        forgotten = _nonneg_int(state.get("forgotten_epoch")) or 0
        if not (space.authority_key_epoch or baseline or owed or forgotten):
            return None
        echo: dict = {
            "key_epoch": space.authority_key_epoch,
            "baseline_epoch": baseline,
            "owed_epoch": owed,
            "forgotten_epoch": forgotten,
        }
        cert = await self._spaces.get_authority_cert(space_id)
        if cert is not None and cert.get("key_epoch") == space.authority_key_epoch:
            echo["key_cert"] = cert
        if forgotten and isinstance(state.get("forgotten_cert"), dict):
            echo["forgotten_cert"] = state["forgotten_cert"]
        return echo

    async def _note_forgotten(
        self,
        space: "Space",
        p: dict,
        held_before: int,
        held_cert: dict | None,
    ) -> None:
        """Track a rotation the owner forgot, from a non-baseline bundle.

        Its ``prior_key_epoch`` is the epoch the owner's rotation replaced.
        Below the epoch we held, the owner no longer knew our epoch — it was
        restored from a backup taken before that rotation, and a member that
        missed the rotation may still hold what it retired. Kept durably with
        the owner's cert for it (``held_cert``, our proof), and the owner is
        told right away (:class:`SpaceAuthorityEchoDue`). Dropped only once
        a bundle NAMES it (``forgotten_key_epoch`` at least as high) — never
        on anything another household can provoke.
        """
        state = await self._spaces.get_authority_echo(space.id)
        noted = _nonneg_int(state.get("forgotten_epoch")) or 0
        named = _nonneg_int(p.get("forgotten_key_epoch")) or 0
        if noted and named >= noted:
            await self._spaces.set_authority_echo(space.id, None)
            noted = 0
        prior = _nonneg_int(p.get("prior_key_epoch"))
        if (
            prior is None
            or not prior < held_before < space.authority_key_epoch
            or held_before <= named
            or held_before <= noted
        ):
            return
        proof = (
            held_cert
            if held_cert is not None and held_cert.get("key_epoch") == held_before
            else None
        )
        await self._spaces.set_authority_echo(
            space.id, {"forgotten_epoch": held_before, "forgotten_cert": proof}
        )
        log.warning(
            "space %s: the owner's rotation replaced authority epoch %d, but "
            "this household held %d — the owner forgot a rotation; echoing it",
            space.id,
            prior,
            held_before,
        )
        await self._bus.publish(
            SpaceAuthorityEchoDue(
                space_id=space.id, owner_instance_id=space.owner_instance_id
            )
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
            # v_49 — our writer cert under the (now pinned) new key, from the
            # owner household only (the bundle's sender). The cert also
            # authenticates itself against the pin in ``accept``.
            if p.get("writer_cert") is not None and self._writer_certs is not None:
                space = await self._spaces.get(space_id)
                if space is not None and event.from_instance == (
                    space.owner_instance_id
                ):
                    await self._writer_certs.accept(space_id, p.get("writer_cert"))

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
        held_before = space.authority_key_epoch
        held_cert = await self._spaces.get_authority_cert(space_id)
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
        if p.get("baseline") is False:
            await self._note_forgotten(space, p, held_before, held_cert)
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
            # v_46: a rotation the owner forgot and a member echoed. Whether
            # WE saw it or not, state written under a key older than it is
            # what it retired — after its own baseline there is none left,
            # so for a household that applied it the reset is a no-op.
            forgotten = _nonneg_int(p.get("forgotten_key_epoch")) or 0
            if not 0 < forgotten < space.authority_key_epoch:
                forgotten = 0
            if missed is None and not forgotten:
                await self._import_content_key(space, p.get("space_content_key"))
                return
            cutoff = max(missed or 0, forgotten)
            log.warning(
                "SPACE_AUTHORITY_ROTATED for %s: missed the baseline for key "
                "epoch %d (last applied %d) — resetting to the owner's snapshot",
                space_id,
                cutoff,
                baseline_epoch,
            )
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
        never saw is not visible here; another member's authority epoch echo
        brings it back as the bundle's ``forgotten_key_epoch`` (v_46).
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
