"""Seed-holder producer for the Phase-5b subscriber content-key handoff.

When a household subscribes to a PUBLIC/GLOBAL space at the GFS and a
seed-holder (the owner or a delegated admin) is online, the GFS notifies
the owner with a ``new_subscriber`` frame carrying the subscriber's
published identity + key-wrap material. This service answers it: it seals
the per-space content key to the subscriber's static X25519 key-wrap pubkey
and relays the sealed envelope back through the content-blind GFS as a
space-authority-signed ``space_subscriber_key_handoff`` event.

The point is to let a GFS subscriber DECRYPT the Phase-5a public-content
relay (which it receives but currently drops, having no key) — delivered
GFS-blind so the GFS (and any non-target subscriber the GFS fans it out to)
never sees the content key.

Pipeline (fail-closed at every step):

1. **Gate on seed + tier + readability.** Act only if this household holds the
   space seed (``get_space_seed`` non-None → owner or delegated admin), the
   space is PUBLIC/GLOBAL, and its ``features.allow_subscribers`` is ON. A
   non-seed-holder can't authority-sign; a private/household space is never
   GFS-discoverable, so its key must never leave via the GFS; and a
   public/global space with followers OFF is *listed* for discovery but is
   **not publicly readable** — only members get content, so its key is never
   sealed to a mere subscriber. The readability flag is NOT ``join_mode`` (an
   ``invite_only`` space with followers on is a broadcast space), and it is
   the OWNER's alone — a seed-holding delegated admin can sign a config edit
   but cannot flip this one.
2. **Verify the key-wrap binding (anti-substitution).** The key-wrap pubkey
   is learned *from the GFS*; a malicious GFS could substitute one it
   controls and read the sealed key. ``verify_keywrap_binding`` binds the
   key-wrap key to the subscriber identity end-to-end — if it fails, DROP and
   never seal. A subscriber that shipped no key-wrap key (older HFS) is
   skipped (it can't be sealed-to yet — graceful, no relay).
3. **Export the current content key** and build the same
   ``{space_content_key: {key_suite, epoch, key_base64, rotated_by}}`` meta
   that :func:`apply_space_content_key_from_metadata` consumes (mirrors the
   §D1b / rekey distribution shape).
4. **Seal** the meta to the verified key-wrap pubkey
   (:func:`seal_to_keywrap`) → ``{kem_suite, eph_pk, ciphertext}``.
5. **Wrap + authority-sign** ``{space_id, sealed}`` with the space seed under
   ``space_subscriber_key_handoff`` and relay via the existing GFS publish
   path. The GFS authorizes it against the TOFU-pinned space pubkey (same
   authority-relay path as ``space_post_public``). The envelope is
   **identity-free**: it does NOT name the target household, so neither the
   GFS nor the other subscribers it is fanned out to learn who is being
   onboarded — the **seal is the gate**, and a household that can't
   ``open_keywrap`` it drops it quietly. (Receivers still accept the legacy
   ``target_instance_id``-bearing shape from an older seed-holder.)

**Targeted (v_61).** When the GFS that announced the subscriber proved
``targeted_publish``, the handoff goes to that subscriber only
(``POST /gfs/publish`` with ``to`` — the instance id the GFS itself named),
not to every subscriber of the space; an older GFS still gets the fan-out.
For a NEW seat (``new_subscriber`` with ``reason: "subscribe"``) of a
follower that listed ``author_profiles_bundle`` in its subscribe's
``accepts`` (:mod:`socialhome.domain.subscriber_accepts`), the host then
sends that follower ONE sealed bundle of the space's author profiles
(:meth:`_send_profiles_bundle`,
:mod:`socialhome.domain.space_author_profile_bundle`) — the authors' own
signed ``author_profile`` items, the follower verifying each. A
``reason: "reconnect"`` notify (the GFS sends one per subscribed space on
every reconnect of the follower's socket) re-seals the key and nothing
else, so neither a GFS nor a host restart makes thousands of followers
re-download their bundles; a frame with no / an unknown ``reason`` (an
older GFS) or a follower that lists no ``accepts`` gets no bundle either —
fail-safe, the follower learns names on each author's next post. Since a
GFS can still name any household as a new subscriber, bundles are capped
per space (an hourly byte budget, :data:`BUNDLE_BYTES_PER_SPACE_PER_HOUR`;
names first, pictures only within :data:`PICTURE_BYTES_PER_SPACE_PER_HOUR`),
and a follower that
churns its seat (unsubscribe → subscribe) is bundled at most once an hour
(:data:`BUNDLE_PER_FOLLOWER_EVERY_S`).

Phase 5b-c adds the owner-offline RECONCILE (:meth:`reconcile`): on each GFS-WS
(re)connect, a seed-holder pulls the GFS subscriber list for every local
PUBLIC/GLOBAL space it holds the seed of + has published to that GFS, and runs
the SAME verified-seal+relay for each subscriber. This catches handoffs missed
while no seed-holder was online (e.g. the owner was offline at subscribe time
and only a delegated admin is up now) and is idempotent — the subscriber's
key import is per-epoch idempotent, so re-sealing is harmless.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

import aiohttp

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE,
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
    sign_authority_event,
    strip_authority_sig_fields,
)
from ..domain.space import PUBLIC_SPACE_TIERS, SpaceRole
from ..domain.space_author_profile_bundle import (
    MAX_BUNDLE_AUTHORS,
    build_bundle_parts,
)
from ..domain.subscriber_accepts import ACCEPTS_AUTHOR_PROFILES_BUNDLE
from ..federation.keywrap_seal import seal_to_keywrap, verify_keywrap_binding
from ..services.space_crypto_service import KEY_SUITE_AESGCM_256

if TYPE_CHECKING:
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.space_author_profile_repo import (
        AbstractSpaceAuthorProfileRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)

#: The most bundle bytes (padded plaintext parts) one space's host uploads
#: an hour, whoever asks. A GFS can name any registered household in a
#: ``reason: "subscribe"`` ``new_subscriber`` and claim that it ``accepts`` a
#: bundle, and anyone can register and subscribe, so this bounds what a flood
#: of fresh followers can drain: at most 24 × this a day per space.
BUNDLE_BYTES_PER_SPACE_PER_HOUR: int = 16 * 1024 * 1024

#: Of that budget, what picture parts may use. Names (part 0, ≤ 128 KiB) can
#: always use the rest, so under a flood a newcomer still gets every name
#: (at least ~32 names-only bundles an hour) and only the pictures wait.
PICTURE_BYTES_PER_SPACE_PER_HOUR: int = 12 * 1024 * 1024

#: The most spaces the budget tracks; past it, the least recently bundled
#: space is forgotten (O(1)).
_MAX_TRACKED_SPACES: int = 1024

#: One bundle per ``(space, follower)`` at most this often — a follower that
#: unsubscribes and subscribes again gets a NEW seat (``reason:
#: "subscribe"``) each time; this keeps one follower's loop from eating the
#: space's backstop and starving real newcomers. Reconnects never bundle,
#: so this holds only seats actually bundled.
BUNDLE_PER_FOLLOWER_EVERY_S: float = 3600.0

#: The most ``(space, follower)`` pairs remembered for
#: :data:`BUNDLE_PER_FOLLOWER_EVERY_S`; past it the oldest is forgotten.
_MAX_TRACKED_FOLLOWERS: int = 10_000

#: The most ``(space, follower, key-wrap key)`` seals remembered for the
#: reconcile skip (#5a); past it the oldest is forgotten (and only costs one
#: more seal on the next reconcile).
_MAX_TRACKED_SEALS: int = 50_000

#: The ``new_subscriber.reason`` of a NEW seat — the only one that earns a
#: bundle. ``"reconnect"``, a missing or an unknown reason: key handoff only.
_REASON_SUBSCRIBE: str = "subscribe"


def _clock() -> float:
    return time.monotonic()


def _opens_bundle(sub: dict[str, Any]) -> bool:
    """The follower listed ``author_profiles_bundle`` in its subscribe."""
    accepts = sub.get("accepts")
    return isinstance(accepts, list) and ACCEPTS_AUTHOR_PROFILES_BUNDLE in accepts


class _Relayed(NamedTuple):
    """A handoff the GFS accepted: the key-wrap key and epoch it was sealed
    to, and whether it went to that follower alone (v_61 targeted)."""

    keywrap_pub: bytes
    epoch: int
    targeted: bool


class SpaceSubscriberKeyOutbound:
    """``new_subscriber`` GFS frame → sealed content-key handoff producer."""

    __slots__ = (
        "_bundle_bytes",
        "_bundle_spent",
        "_bundled",
        "_crypto",
        "_gfs",
        "_gfs_conn_repo",
        "_http_session",
        "_own_instance_id",
        "_profiles",
        "_sealed",
        "_spaces",
    )

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        space_crypto: SpaceContentEncryption,
        gfs_service: GfsConnectionService,
    ) -> None:
        self._spaces = space_repo
        self._crypto = space_crypto
        self._gfs = gfs_service
        self._own_instance_id: str = ""
        # Reconcile-only deps, wired lazily (the GFS-connection repo + the
        # shared HTTP session aren't available at the same step as the rest).
        # Until attached, :meth:`reconcile` no-ops.
        self._gfs_conn_repo: AbstractGfsConnectionRepo | None = None
        self._http_session: aiohttp.ClientSession | None = None
        #: The cached author profiles a new follower's bundle is built from
        #: (v_61). Until attached, no bundle is sent.
        self._profiles: AbstractSpaceAuthorProfileRepo | None = None
        #: ``space_id`` → monotonic times of its bundles in the last minute
        #: (the per-space rate backstop; bounded by the limit per space).
        self._bundle_bytes: OrderedDict[str, deque[tuple[float, int]]] = OrderedDict()
        #: ``space_id`` → the bytes in its window (a running sum of
        #: ``_bundle_bytes``, so the budget check is O(expired)).
        self._bundle_spent: dict[str, int] = {}
        #: ``(space_id, follower)`` → monotonic time of its last bundle, in
        #: bundle order (oldest first) — the per-follower churn guard.
        self._bundled: OrderedDict[tuple[str, str], float] = OrderedDict()
        #: ``(space_id, follower, key-wrap pub hex)`` → the content-key epoch
        #: this process last relayed a seal of to that follower (#5a). A
        #: reconcile skips a follower sealed at the current epoch; in memory
        #: only, so a restart reconciles everyone once.
        self._sealed: OrderedDict[tuple[str, str, str], int] = OrderedDict()

    def attach_author_profiles(self, repo: AbstractSpaceAuthorProfileRepo) -> None:
        """Wire the author-profile cache the v_61 new-follower bundle is
        built from."""
        self._profiles = repo

    def attach_identity(self, *, own_instance_id: str) -> None:
        self._own_instance_id = own_instance_id

    def attach_reconcile_context(
        self,
        *,
        gfs_conn_repo: AbstractGfsConnectionRepo,
        http_session: aiohttp.ClientSession,
    ) -> None:
        """Wire the Phase-5b-c reconcile dependencies (the GFS-connection repo
        for enumerating published spaces + the shared HTTP session for pulling
        the subscriber list). Without this, :meth:`reconcile` no-ops."""
        self._gfs_conn_repo = gfs_conn_repo
        self._http_session = http_session

    async def handle(self, frame: dict[str, Any], *, gfs_id: str | None = None) -> None:
        """Handle one GFS ``new_subscriber`` frame. Never raises.

        *gfs_id* is the local connection the frame arrived on (bound per WS
        client): the subscriber lives there, so a targeted handoff and the
        profiles bundle go there."""
        if frame.get("type") != "new_subscriber":
            return
        space_id = str(frame.get("space_id") or "")
        sub = frame.get("subscriber")
        if not space_id or not isinstance(sub, dict):
            return

        space = await self._spaces.get(space_id)
        if space is None or space.space_type not in PUBLIC_SPACE_TIERS:
            return
        # A public/global space with ``allow_subscribers`` OFF is LISTED in
        # the GFS directory (that's how people discover it and get invited)
        # but is NOT publicly readable: only members get content. Never seal
        # the content key to a mere subscriber — no key ⇒ the relayed
        # ciphertext (which is itself suppressed, see space_public_outbound)
        # is unopenable.
        if not space.features.allow_subscribers:
            log.debug(
                "space_subscriber_key.outbound: space %s does not allow "
                "subscribers — not publicly readable, skipping handoff",
                space_id,
            )
            return
        # Only a seed-holder (owner or delegated admin) can authority-sign.
        seed = await self._spaces.get_space_seed(space_id)
        if seed is None:
            return
        if not self._own_instance_id:
            log.warning(
                "space_subscriber_key.outbound: identity not attached — "
                "dropping handoff for space %s",
                space_id,
            )
            return

        target = str(sub.get("instance_id") or "")
        sealed_to = await self._seal_and_relay(
            space_id=space_id,
            seed=seed,
            target_instance_id=target,
            identity_pub_hex=str(sub.get("identity_public_key") or ""),
            keywrap_pub_hex=str(sub.get("keywrap_public_key") or ""),
            keywrap_sig=str(sub.get("keywrap_sig") or ""),
            gfs_id=gfs_id,
        )
        # The key reached that follower alone, so its server routes by
        # target: now the authors' names, to the same follower only — once,
        # for a NEW seat (never on a reconnect) of a follower that opens it.
        if (
            sealed_to is not None
            and sealed_to.targeted
            and gfs_id is not None
            and frame.get("reason") == _REASON_SUBSCRIBE
            and _opens_bundle(sub)
        ):
            try:
                await self._send_profiles_bundle(
                    gfs_id=gfs_id,
                    space_id=space_id,
                    seed=seed,
                    target_instance_id=target,
                    keywrap_pub=sealed_to.keywrap_pub,
                )
            except Exception:
                log.exception(
                    "space_subscriber_key.outbound: profiles bundle failed for "
                    "space %s",
                    space_id,
                )

    async def _send_profiles_bundle(
        self,
        *,
        gfs_id: str,
        space_id: str,
        seed: bytes,
        target_instance_id: str,
        keywrap_pub: bytes,
    ) -> int:
        """Seal the space's cached, author-signed profile items to ONE new
        follower and relay them to it alone (v_61). Returns the parts sent.

        Names first (part 0); then pictures. Every part is padded and sealed
        separately. Each item keeps the epoch it was published at; the
        follower checks its writer cert there. Only authors whose household
        is still a member household are bundled. A follower is bundled at most
        once per :data:`BUNDLE_PER_FOLLOWER_EVERY_S`. The space's hourly byte
        budget (:data:`BUNDLE_BYTES_PER_SPACE_PER_HOUR`, of which pictures
        get :data:`PICTURE_BYTES_PER_SPACE_PER_HOUR`) bounds the rest."""
        if self._profiles is None:
            return 0
        now = _clock()
        follower = (space_id, target_instance_id)
        last = self._bundled.get(follower)
        if last is not None and now - last < BUNDLE_PER_FOLLOWER_EVERY_S:
            log.info(
                "space_subscriber_key.outbound: a follower of space %s was "
                "bundled within the hour — key only this time",
                space_id,
            )
            return 0
        # Only the space's current writers (its roster, followers aside):
        # a removed or departed author is never forwarded, even when their
        # household still has members. Over-fetched so departed rows don't
        # shrink the bundle below the cap.
        writers = {
            m.user_id
            for m in await self._spaces.list_members(space_id)
            if m.role != SpaceRole.SUBSCRIBER.value
        }
        entries = [
            e
            for e in await self._profiles.list_signed_items(
                space_id, limit=MAX_BUNDLE_AUTHORS * 4
            )
            if str(e.item.get("author_user_id")) in writers
        ][:MAX_BUNDLE_AUTHORS]
        parts = build_bundle_parts(space_id, entries)
        if not parts:
            return 0
        used = self._budget_used(space_id, now)
        sent = 0
        for index, plaintext in enumerate(parts):
            cap = (
                BUNDLE_BYTES_PER_SPACE_PER_HOUR
                if index == 0
                else PICTURE_BYTES_PER_SPACE_PER_HOUR
            )
            if used + len(plaintext) > cap:
                log.warning(
                    "space_subscriber_key.outbound: space %s used %d of its "
                    "hourly bundle bytes — this follower gets %d of %d part(s) "
                    "(%s)",
                    space_id,
                    used,
                    index,
                    len(parts),
                    "no names" if index == 0 else "names, not every picture",
                )
                break
            sealed = seal_to_keywrap(
                recipient_keywrap_pub=keywrap_pub, plaintext=plaintext
            )
            # ``issued_at`` is inside the authority signature: a follower
            # refuses a bundle older than BUNDLE_MAX_AGE_S, so a server
            # cannot replay an old one to it forever.
            envelope: dict = {
                "space_id": space_id,
                "sealed": sealed,
                "issued_at": datetime.now(UTC).isoformat(),
            }
            envelope.update(
                sign_authority_event(
                    event_type=AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE,
                    space_id=space_id,
                    payload=strip_authority_sig_fields(envelope),
                    space_seed=seed,
                )
            )
            self._spend(space_id, now, len(plaintext))
            used += len(plaintext)
            if await self._gfs.publish_space_event_to(
                gfs_id,
                space_id=space_id,
                event_type=AUTHORITY_EVENT_SPACE_AUTHOR_PROFILES_BUNDLE,
                payload=envelope,
                to_instance_id=target_instance_id,
            ):
                sent += 1
        if sent:
            self._remember_follower(space_id, target_instance_id, now)
        log.info(
            "space_subscriber_key.outbound: sent a new follower of space %s "
            "%d author profile(s) in %d of %d targeted part(s)",
            space_id,
            len(entries),
            sent,
            len(parts),
        )
        return sent

    def _budget_used(self, space_id: str, now: float) -> int:
        """Bundle bytes *space_id* sent in the last hour (expired ones
        dropped)."""
        spent = self._bundle_bytes.get(space_id)
        if spent is None:
            return 0
        total = self._bundle_spent.get(space_id, 0)
        while spent and now - spent[0][0] >= 3600.0:
            total -= spent.popleft()[1]
        self._bundle_spent[space_id] = total
        return total

    def _spend(self, space_id: str, now: float, size: int) -> None:
        """Count *size* bytes against *space_id*'s hourly budget. LRU-bounded
        with O(1) eviction."""
        spent = self._bundle_bytes.get(space_id)
        if spent is None:
            spent = deque()
            self._bundle_bytes[space_id] = spent
            if len(self._bundle_bytes) > _MAX_TRACKED_SPACES:
                gone, _ = self._bundle_bytes.popitem(last=False)
                self._bundle_spent.pop(gone, None)
        else:
            self._bundle_bytes.move_to_end(space_id)
        spent.append((now, size))
        self._bundle_spent[space_id] = self._bundle_spent.get(space_id, 0) + size

    def _remember_follower(self, space_id: str, follower: str, now: float) -> None:
        """Remember *follower* for :data:`BUNDLE_PER_FOLLOWER_EVERY_S`
        (LRU-bounded, O(1) eviction)."""
        key = (space_id, follower)
        self._bundled.pop(key, None)
        self._bundled[key] = now
        while self._bundled:
            oldest_key, at = next(iter(self._bundled.items()))
            if (
                len(self._bundled) > _MAX_TRACKED_FOLLOWERS
                or now - at >= BUNDLE_PER_FOLLOWER_EVERY_S
            ):
                del self._bundled[oldest_key]
                continue
            break

    async def _seal_and_relay(
        self,
        *,
        space_id: str,
        seed: bytes,
        target_instance_id: str,
        identity_pub_hex: str,
        keywrap_pub_hex: str,
        keywrap_sig: str,
        gfs_id: str | None = None,
    ) -> _Relayed | None:
        """Verify the subscriber's key-wrap binding, seal the current content
        key to it, authority-sign, and relay one ``space_subscriber_key_handoff``.

        With *gfs_id* (the connection the subscriber was named on) the
        handoff goes to that subscriber alone when that server proved
        ``targeted_publish`` (``targeted``, for the profiles bundle).
        Otherwise — no *gfs_id*, an older server — it is fanned out as
        before. Returns what was relayed (and remembers it for the
        reconcile skip, #5a), or ``None`` when nothing was.

        Shared by the notify-driven :meth:`handle` and the Phase-5b-c
        :meth:`reconcile` so both run the identical anti-substitution gate +
        seal + sign + relay. Caller guarantees the space is a seed-held
        PUBLIC/GLOBAL space and ``self._own_instance_id`` is set. Never raises —
        a per-subscriber failure is logged and swallowed."""
        if not target_instance_id or not identity_pub_hex:
            return None
        # A subscriber that published no key-wrap key (older HFS) can't be
        # sealed-to — skip gracefully (no relay).
        if not keywrap_pub_hex or not keywrap_sig:
            log.info(
                "space_subscriber_key.outbound: subscriber %s shipped no "
                "key-wrap key — skipping handoff for space %s",
                target_instance_id,
                space_id,
            )
            return None
        try:
            identity_pub = bytes.fromhex(identity_pub_hex)
            keywrap_pub = bytes.fromhex(keywrap_pub_hex)
        except ValueError:
            log.warning(
                "space_subscriber_key.outbound: malformed subscriber keys for "
                "%s on space %s — dropped",
                target_instance_id,
                space_id,
            )
            return None
        # ANTI-SUBSTITUTION GATE: verify the GFS-served key-wrap pubkey is
        # genuinely bound to the subscriber identity (derive_instance_id +
        # self-sig + 32-byte) BEFORE sealing. A swapped key (e.g. a malicious
        # GFS slipping in a key it controls) fails here → DROP, never seal.
        if not verify_keywrap_binding(
            instance_id=target_instance_id,
            identity_pub=identity_pub,
            keywrap_pub=keywrap_pub,
            keywrap_sig=keywrap_sig,
        ):
            log.warning(
                "space_subscriber_key.outbound: key-wrap binding FAILED for "
                "subscriber %s on space %s — refusing to seal (possible GFS "
                "substitution)",
                target_instance_id,
                space_id,
            )
            return None

        exported = await self._crypto.export_current_key(space_id)
        if exported is None:
            log.warning(
                "space_subscriber_key.outbound: no content key for space %s — "
                "cannot hand off to %s",
                space_id,
                target_instance_id,
            )
            return None
        epoch, raw_key = exported
        # Mirror the meta apply_space_content_key_from_metadata consumes (and
        # the §D1b / rekey distribution shape).
        meta = {
            "space_content_key": {
                "epoch": epoch,
                "key_suite": KEY_SUITE_AESGCM_256,
                "key_base64": base64.b64encode(raw_key).decode("ascii"),
                "rotated_by": self._own_instance_id,
            }
        }
        sealed = seal_to_keywrap(
            recipient_keywrap_pub=keywrap_pub,
            plaintext=json.dumps(meta).encode("utf-8"),
        )
        # IDENTITY-FREE WIRE SHAPE: the envelope names nobody. The target is
        # used locally (to pick + verify the key-wrap key we seal to, and for
        # our own logging) but never travels: the SEAL is the gate, so only
        # the household holding the matching key-wrap private key can open it.
        # Shipping ``target_instance_id`` would tell the GFS — and every other
        # subscriber the GFS fans this out to — which household is being
        # onboarded to which space.
        envelope: dict = {
            "space_id": space_id,
            "sealed": sealed,
        }
        sig = sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
            space_id=space_id,
            payload=strip_authority_sig_fields(envelope),
            space_seed=seed,
        )
        envelope.update(sig)
        try:
            if gfs_id is not None:
                targeted = await self._gfs.publish_space_event_to(
                    gfs_id,
                    space_id=space_id,
                    event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
                    payload=envelope,
                    to_instance_id=target_instance_id,
                )
                if targeted is not None:
                    if not targeted:
                        return None
                    return self._remember_seal(
                        space_id,
                        target_instance_id,
                        keywrap_pub_hex,
                        _Relayed(keywrap_pub, epoch, True),
                    )
            # No target-capable server: every subscriber gets it (the seal
            # is the gate), as before v_61.
            await self._gfs.publish_space_event(
                space_id=space_id,
                event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
                payload=envelope,
            )
            return self._remember_seal(
                space_id,
                target_instance_id,
                keywrap_pub_hex,
                _Relayed(keywrap_pub, epoch, False),
            )
        except Exception:
            log.exception(
                "space_subscriber_key.outbound: relay failed for space=%s target=%s",
                space_id,
                target_instance_id,
            )
        return None

    def _remember_seal(
        self, space_id: str, follower: str, keywrap_pub_hex: str, relayed: _Relayed
    ) -> _Relayed:
        key = (space_id, follower, keywrap_pub_hex.lower())
        self._sealed[key] = relayed.epoch
        self._sealed.move_to_end(key)
        while len(self._sealed) > _MAX_TRACKED_SEALS:
            self._sealed.popitem(last=False)
        return relayed

    def _sealed_at(self, space_id: str, sub: dict[str, Any]) -> int | None:
        return self._sealed.get(
            (
                space_id,
                str(sub.get("instance_id") or ""),
                str(sub.get("keywrap_public_key") or "").lower(),
            )
        )

    async def reconcile(self, gfs_id: str) -> None:
        """Owner-offline RECONCILE (Phase-5b-c). Never raises.

        Triggered on each GFS-WS (re)connect. For every PUBLIC/GLOBAL space
        this household (a) holds the seed for and (b) has published to *gfs_id*,
        pull the GFS subscriber list under a space-authority-signed query and
        re-seal the content key to each subscriber via :meth:`_seal_and_relay`.
        This delivers the key to subscribers whose owner was offline at
        subscribe time (any delegated admin can do it) and catches notifies
        missed while no seed-holder was online. Idempotent — the subscriber's
        import is per-epoch idempotent, so re-sealing is harmless.

        Fail-soft at every level: a missing reconcile context, an unknown /
        inactive GFS, a per-space transport error, or a per-subscriber seal
        failure is logged and skipped — never aborts the whole pass.
        """
        if (
            self._gfs_conn_repo is None
            or self._http_session is None
            or not self._own_instance_id
        ):
            return
        # Fail-soft on the lookups too, so the "never raises" contract holds on
        # the unwrapped on_connected invocation paths (a repo hiccup here must
        # not propagate into the reconnect loop).
        try:
            conn = await self._gfs_conn_repo.get(gfs_id)
            if conn is None or conn.status != "active":
                return
            publications = await self._gfs_conn_repo.list_publications(gfs_id)
        except Exception:
            log.exception(
                "space_subscriber_key.reconcile: lookup failed for gfs %s", gfs_id
            )
            return
        for pub in publications:
            try:
                await self._reconcile_space(conn.inbox_url, pub.space_id, gfs_id)
            except Exception:
                log.exception(
                    "space_subscriber_key.reconcile: space %s on gfs %s failed",
                    pub.space_id,
                    gfs_id,
                )

    async def reconcile_space(self, gfs_id: str, space_id: str) -> None:
        """Reconcile ONE space on ONE GFS. Never raises.

        Narrow entry point for the forward-secrecy rekey path: after a member
        removal rotates the space content key, the new epoch must reach the
        GFS subscribers too (they are not member households, so the
        ``space_instances`` fan-out skips them). Applies exactly the guards
        :meth:`reconcile` applies — reconcile context wired, GFS known and
        active, space actually published to it — and then defers to the shared
        :meth:`_reconcile_space`, which enforces the PUBLIC/GLOBAL tier +
        seed-holder gates.
        """
        if (
            self._gfs_conn_repo is None
            or self._http_session is None
            or not self._own_instance_id
        ):
            return
        try:
            conn = await self._gfs_conn_repo.get(gfs_id)
            if conn is None or conn.status != "active":
                return
            publications = await self._gfs_conn_repo.list_publications(gfs_id)
            if not any(pub.space_id == space_id for pub in publications):
                return
            await self._reconcile_space(conn.inbox_url, space_id, gfs_id)
        except Exception:
            log.exception(
                "space_subscriber_key.reconcile: space %s on gfs %s failed",
                space_id,
                gfs_id,
            )

    async def reconcile_space_everywhere(self, space_id: str) -> None:
        """Re-seal one space's current content key to its subscribers on every
        GFS it is published to. Never raises.

        Called after a content-key rotation so a subscriber isn't dark until
        the next GFS reconnect.
        """
        if self._gfs_conn_repo is None:
            return
        try:
            publications = await self._gfs_conn_repo.list_publications_for_space(
                space_id
            )
        except Exception:
            log.exception(
                "space_subscriber_key.reconcile: publication lookup failed for "
                "space %s",
                space_id,
            )
            return
        for pub in publications:
            await self.reconcile_space(pub.gfs_connection_id, space_id)

    async def _reconcile_space(
        self, gfs_base_url: str, space_id: str, gfs_id: str | None = None
    ) -> None:
        """Pull + re-seal for one published space. Skips a space this household
        doesn't hold the seed for, or a non-PUBLIC/GLOBAL space (its key must
        never leave via the GFS). Only reached after :meth:`reconcile` has
        confirmed the HTTP session is wired."""
        if self._http_session is None:
            return
        space = await self._spaces.get(space_id)
        if space is None or space.space_type not in PUBLIC_SPACE_TIERS:
            return
        # Subscribers off ⇒ listed for discovery but not publicly readable.
        # Gated here (before the subscriber-list round-trip) so we never even
        # ask the GFS who subscribed to a space whose content nobody may read.
        if not space.features.allow_subscribers:
            log.debug(
                "space_subscriber_key.reconcile: space %s does not allow "
                "subscribers — not publicly readable, skipping",
                space_id,
            )
            return
        seed = await self._spaces.get_space_seed(space_id)
        if seed is None:
            return

        ts = datetime.now(UTC).isoformat()
        sig = sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
            space_id=space_id,
            payload={"space_id": space_id, "ts": ts},
            space_seed=seed,
        )
        params = {
            "ts": ts,
            "authority_sig": sig["authority_sig"],
            "authority_sig_suite": sig["authority_sig_suite"],
        }
        url = f"{gfs_base_url}/gfs/spaces/{space_id}/subscribers"
        async with self._http_session.get(
            url,
            allow_redirects=False,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                log.warning(
                    "space_subscriber_key.reconcile: GFS returned HTTP %d for "
                    "space %s subscriber list",
                    resp.status,
                    space_id,
                )
                return
            data = await resp.json()
        subscribers = data.get("subscribers") if isinstance(data, dict) else None
        if not isinstance(subscribers, list):
            return
        # #5a: a follower this process already sealed the current epoch to
        # is skipped — this pass runs on every reconnect of OUR socket, and
        # re-sealing to every follower each time was ~140k handoffs a day.
        # A follower whose socket was down when its seal went out is
        # repaired by its own reconnect notify (:meth:`handle`), which always
        # re-seals; a rotation (new epoch) reaches everyone.
        exported = await self._crypto.export_current_key(space_id)
        current_epoch = exported[0] if exported is not None else None
        for sub in subscribers:
            if not isinstance(sub, dict):
                continue
            if current_epoch is not None and (
                self._sealed_at(space_id, sub) == current_epoch
            ):
                continue
            await self._seal_and_relay(
                space_id=space_id,
                seed=seed,
                target_instance_id=str(sub.get("instance_id") or ""),
                identity_pub_hex=str(sub.get("identity_public_key") or ""),
                keywrap_pub_hex=str(sub.get("keywrap_public_key") or ""),
                keywrap_sig=str(sub.get("keywrap_sig") or ""),
                gfs_id=gfs_id,
            )
