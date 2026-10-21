"""Author side of encrypted public moments (§Momentum-public, v_62).

Every public moment is encrypted under a key derived from its author's
current **step key** (:mod:`socialhome.moment_public_seal`) before it
reaches a GFS (:mod:`socialhome.services.moment_public_outbound`). This
service owns the author's keys on the author's household:

* :meth:`current_key` — the author's current ``(key_gen, key_step,
  step key)``. A *generation* root is minted on first use (32 random bytes,
  KEK-sealed in ``moment_public_keys``, ``is_own = 1``; ``key_gen`` is the
  Unix-seconds mint time, so it never repeats). The step key ratchets
  forward once a day *in place* — the household keeps no older step key.
* :meth:`rotate` — a fresh generation. Moments from then on are readable
  only by households that are sealed the new key; what a household
  already holds stays readable to it.
* **Lazy rotation.** A follower household leaving (``follow_changed``
  ``remove`` with no other user of that household still following), a
  block of someone, or a registration ending only *marks* the current
  generation (:meth:`mark_rotation_due`). The next publish
  (:meth:`publish_key`) starts a new generation — at most once per
  :data:`ROTATE_AT_MOST_EVERY_S`, and never without a publish (moments live
  a day, so a mark with no new moment costs nothing). The remaining
  followers then fetch the new key once each (pull + mailbox), however many
  left in between.
* :meth:`handle` — the GFS names a follower household that needs the key:
  ``follow_changed`` (``action: "add"``, someone just followed) or
  ``moment_key_request`` (a follower has no key for the generation a
  moment arrived under, and the GFS's mailbox had none). The current step
  key is sealed to that ONE household's static X25519 key-wrap key and
  queued for the same GFS; a background flush sends the queue in batches
  (``POST /gfs/moments/keys``, ≤ :data:`MAX_BATCH` per request). The GFS
  keeps the latest sealed key per (author, household) in a mailbox it
  can't open, so a household that is offline gets it when it reconnects.

Never per moment: a key is sealed when a household follows, when it asks,
and (rotation) when a generation changes — publishing a moment seals
nothing.

Fail closed at every step:

* **Anti-substitution.** The follower's key-wrap key comes *from the GFS*.
  :func:`verify_keywrap_binding` checks it is self-signed by the identity
  key the follower's instance id derives from; a GFS that swaps in a key of
  its own fails, and nothing is sealed.
* **Only to a household that can open it.** The follower household must
  have declared (signed, in its follow request) the KEM suite of its
  key-wrap key and the moment seal suite; an older household declared
  none and is sent nothing.
* **Only what is ours to give.** The author must be a local user with a
  registration on that GFS, not a protected account (§CP.R), and the GFS
  must have proved ``encrypted_moments``.
* **No amplification.** At most one seal per (author, follower household)
  per :data:`RESEAL_AFTER_S` for the same key id.

Owner decision 2026-10-20: the author's household may learn which
households follow it (it already got ``follow_changed`` with the follower's
instance id) and seals to each household's key-wrap key directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from ..domain.child_protection import ProtectedCapability
from ..domain.events import UserBlocked
from ..domain.moment_public import MomentPublicKey
from ..federation.keywrap_seal import (
    SUPPORTED_KEM_SUITES,
    seal_to_keywrap,
    verify_keywrap_binding,
)
from ..moment_public_seal import (
    KEY_SUITE_AESGCM_256,
    MAX_RATCHET_STEPS,
    MOMENT_KEY_BYTES,
    MOMENT_SEAL_SUITE_AESGCM256,
    key_at_rest_context,
    key_plaintext,
    ratchet,
    sign_wire,
    step_for,
)
from .protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from ..infrastructure.key_manager import KeyManager
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.moment_public_repo import (
        AbstractMomentPublicKeyRepo,
        AbstractMomentPublicRegistrationRepo,
    )
    from ..repositories.user_repo import AbstractUserRepo
    from .gfs_connection_service import GfsConnectionService

log = logging.getLogger(__name__)

#: The same follower household is sealed the same key at most this often.
RESEAL_AFTER_S: float = 30.0

#: Own generations kept per author: the current one, and the one before it
#: while moments sealed under it may still be in flight.
KEEP_OWN_GENERATIONS: int = 2

#: A marked generation is replaced at the next publish, but at most this
#: often: every rotation makes each remaining follower household fetch one
#: sealed key, so the cap bounds that cost to ≤ 4 rounds a day.
ROTATE_AT_MOST_EVERY_S: float = 6 * 3600.0

#: Past this many remembered seals, those older than :data:`RESEAL_AFTER_S`
#: are forgotten.
_SEALED_PRUNE_ABOVE: int = 10_000

#: Sealed keys per ``POST /gfs/moments/keys`` — the GFS's own cap
#: (``routes/moments_public.MAX_KEY_DELIVERIES``).
MAX_BATCH: int = 100

#: How long the flush waits after the first queued key, so a burst of
#: requests rides one batch.
FLUSH_AFTER_S: float = 0.5

#: Sealed keys waiting per GFS at most; past it a key is not queued (the
#: follower asks again a minute later).
MAX_QUEUED_PER_GFS: int = 10_000


class MomentPublicKeyOutbound(ProtectionGateMixin):
    """Mint and ratchet the author's moment keys; seal them to followers."""

    __slots__ = (
        "_child_protection",
        "_gfs",
        "_gfs_repo",
        "_http_client",
        "_kek",
        "_keys",
        "_own_instance_id",
        "_queued",
        "_regs",
        "_sealed_at",
        "_signing_key",
        "_stop",
        "_task",
        "_users",
        "_wake",
    )

    def __init__(
        self,
        *,
        key_repo: AbstractMomentPublicKeyRepo,
        registration_repo: AbstractMomentPublicRegistrationRepo,
        user_repo: AbstractUserRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        gfs_service: GfsConnectionService,
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._keys = key_repo
        self._regs = registration_repo
        self._users = user_repo
        self._gfs_repo = gfs_repo
        self._gfs = gfs_service
        self._http_client = http_client
        self._kek: KeyManager | None = None
        self._signing_key: bytes | None = None
        self._own_instance_id: str = ""
        self._child_protection = None
        #: ``(author, follower instance)`` → ``((gen, step), monotonic time)``
        #: of the last seal.
        self._sealed_at: dict[tuple[str, str], tuple[tuple[int, int], float]] = {}
        #: ``gfs_id`` → sealed deliveries waiting for the next flush.
        self._queued: dict[str, list[dict]] = {}
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    def attach_identity(
        self, *, own_instance_id: str, signing_key: bytes, key_manager: KeyManager
    ) -> None:
        self._own_instance_id = own_instance_id
        self._signing_key = signing_key
        self._kek = key_manager

    # ── Lifecycle (the batched flush) ───────────────────────────────────

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="moment-key-flush")

    async def stop(self) -> None:
        """Stop the flush loop, sending what is still queued first."""
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=15.0)
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self._wake.wait()
            # Let a burst gather into one batch (or stop early).
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=FLUSH_AFTER_S)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.flush()
            except Exception:  # pragma: no cover - defensive
                log.exception("moment_public.keys: flush failed")

    # ── The key ─────────────────────────────────────────────────────────

    async def current_key(self, author_user_id: str) -> tuple[int, int, bytes]:
        """The author's current ``(key_gen, key_step, step key)`` — a
        generation minted on first use, the step ratcheted to today."""
        kek = self._require_kek()
        row = await self._keys.latest(author_user_id, own=True)
        if row is None:
            return await self.rotate(author_user_id)
        key = kek.decrypt(
            row.key_sealed,
            associated_data=key_at_rest_context(author_user_id, row.key_gen, own=True),
        )
        step = step_for(row.key_gen, time.time())
        if step <= row.key_step:
            return row.key_gen, row.key_step, key
        if step - row.key_step > MAX_RATCHET_STEPS:
            # Older than a follower would ratchet: start a new generation.
            return await self.rotate(author_user_id)
        key = ratchet(key, step - row.key_step)
        await self._keys.advance(
            MomentPublicKey(
                author_user_id=author_user_id,
                key_gen=row.key_gen,
                key_step=step,
                is_own=True,
                key_sealed=kek.encrypt(
                    key,
                    associated_data=key_at_rest_context(
                        author_user_id, row.key_gen, own=True
                    ),
                ),
            )
        )
        return row.key_gen, step, key

    async def publish_key(self, author_user_id: str) -> tuple[int, int, bytes]:
        """The key a new moment is sealed under: :meth:`current_key`, but a
        generation marked for rotation is replaced first — once it is at
        least :data:`ROTATE_AT_MOST_EVERY_S` old."""
        row = await self._keys.latest(author_user_id, own=True)
        if (
            row is not None
            and row.rotate_due
            and time.time() - row.key_gen >= ROTATE_AT_MOST_EVERY_S
        ):
            return await self.rotate(author_user_id)
        return await self.current_key(author_user_id)

    async def mark_rotation_due(self, author_user_id: str, *, reason: str) -> bool:
        """Mark *author_user_id*'s generation for replacement at the next
        publish (lazy, see the module docstring). ``False`` when the author
        has no key yet."""
        marked = await self._keys.mark_rotate_due(author_user_id)
        if marked:
            log.info(
                "moment_public.keys: author %s's key will rotate at the next "
                "publish (%s)",
                author_user_id,
                reason,
            )
        return marked

    async def on_user_blocked(self, event: UserBlocked) -> None:
        """A local author blocked someone: a household that follows only
        through them must not read what comes next. Marked lazily; sealing
        to a household whose followers are all blocked is refused anyway."""
        if await self._regs.list_for_user(event.blocker_user_id):
            await self.mark_rotation_due(event.blocker_user_id, reason="block")

    async def rotate(self, author_user_id: str) -> tuple[int, int, bytes]:
        """Mint a fresh generation for *author_user_id* and make it current."""
        kek = self._require_kek()
        latest = await self._keys.latest(author_user_id, own=True)
        gen = max(int(time.time()), (latest.key_gen + 1) if latest else 0)
        key = os.urandom(MOMENT_KEY_BYTES)
        await self._keys.save(
            MomentPublicKey(
                author_user_id=author_user_id,
                key_gen=gen,
                key_step=0,
                is_own=True,
                key_sealed=kek.encrypt(
                    key,
                    associated_data=key_at_rest_context(author_user_id, gen, own=True),
                ),
            )
        )
        await self._keys.prune(author_user_id, own=True, keep=KEEP_OWN_GENERATIONS)
        log.info(
            "moment_public.keys: new moment key generation %d for author %s",
            gen,
            author_user_id,
        )
        return gen, 0, key

    # ── GFS notices ─────────────────────────────────────────────────────

    async def handle(self, frame: dict[str, Any], *, gfs_id: str) -> int:
        """Seal the key to the follower household a GFS notice names.

        ``follow_changed`` with ``action: "add"`` (a new follower) and
        ``moment_key_request`` (a follower without the current key). Returns
        how many sealed keys were queued for the next flush (0 or 1). Never
        raises."""
        ftype = frame.get("type")
        author = str(frame.get("followed_user_id") or "")
        if ftype == "follow_changed" and frame.get("action") == "remove":
            await self._on_removed(frame, author, gfs_id=gfs_id)
            return 0
        if ftype == "follow_changed" and frame.get("action") != "add":
            return 0
        if ftype not in ("follow_changed", "moment_key_request"):
            return 0
        follower = frame.get("follower")
        if not isinstance(follower, dict) or not author:
            return 0
        users = frame.get("follower_user_ids")
        if not isinstance(users, list):
            users = [frame.get("follower_user_id")]
        named = [str(u) for u in users if isinstance(u, str) and u]
        try:
            return await self._seal_to(author, follower, named, gfs_id=gfs_id)
        except Exception:
            log.exception("moment_public.keys: sealing to a follower failed")
            return 0

    async def _on_removed(self, frame: dict, author: str, *, gfs_id: str) -> None:
        """A follower left. Only when no other user of that household still
        follows (the GFS says so; an older GFS doesn't — then assume none)
        is the key marked for rotation: the key is per household."""
        if not author or frame.get("household_still_follows") is True:
            return
        if await self._regs.get(user_id=author, gfs_id=gfs_id) is None:
            return
        try:
            await self.mark_rotation_due(author, reason="a follower household left")
        except Exception:
            log.exception("moment_public.keys: marking a rotation failed")

    async def _seal_to(
        self, author: str, follower: dict, named: list[str], *, gfs_id: str
    ) -> int:
        if self._signing_key is None or self._kek is None:
            return 0
        if await self._users.get_by_user_id(author) is None:
            return 0
        if await self._regs.get(user_id=author, gfs_id=gfs_id) is None:
            return 0
        if await self._is_restricted(author, ProtectedCapability.PUBLIC_MOMENTS):
            return 0
        if not await self._gfs.encrypted_moments_supported(gfs_id):
            return 0
        target = str(follower.get("instance_id") or "")
        if named and all([await self._users.is_blocked(author, u) for u in named]):
            log.info(
                "moment_public.keys: every follower the GFS names at household "
                "%s is blocked by the author — not sealing",
                target,
            )
            return 0
        if not _can_open(follower):
            log.info(
                "moment_public.keys: follower household %s can't open encrypted "
                "moments (no matching suites declared) — not sealing",
                target,
            )
            return 0
        keywrap = _bound_keywrap_key(target, follower)
        if keywrap is None:
            log.warning(
                "moment_public.keys: follower %s key-wrap key is not bound to "
                "its identity — not sealing (possible GFS substitution)",
                target,
            )
            return 0
        gen, step, key = await self.current_key(author)
        if self._recently_sealed(author, target, (gen, step)):
            return 0
        queue = self._queued.setdefault(gfs_id, [])
        if len(queue) >= MAX_QUEUED_PER_GFS:
            log.warning(
                "moment_public.keys: %d sealed keys already wait for GFS %s — "
                "follower %s asks again later",
                len(queue),
                gfs_id,
                target,
            )
            return 0
        delivery = sign_wire(
            self._signing_key,
            {
                "author_user_id": author,
                "instance_id": self._own_instance_id,
                "to": target,
                "key_gen": gen,
                "key_step": step,
                "key_suite": KEY_SUITE_AESGCM_256,
                "sealed": seal_to_keywrap(
                    recipient_keywrap_pub=keywrap,
                    plaintext=key_plaintext(
                        author_user_id=author,
                        key_gen=gen,
                        key_step=step,
                        to=target,
                        key=key,
                    ),
                ),
            },
        )
        self._sealed_at[(author, target)] = ((gen, step), time.monotonic())
        queue.append(delivery)
        self._wake.set()
        return 1

    async def flush(self) -> int:
        """Send every queued sealed key, at most :data:`MAX_BATCH` per
        request. Returns how many the GFSs accepted."""
        sent = 0
        for gfs_id in list(self._queued):
            queue = self._queued.pop(gfs_id, [])
            for i in range(0, len(queue), MAX_BATCH):
                sent += await self._post_deliveries(gfs_id, queue[i : i + MAX_BATCH])
        return sent

    def _recently_sealed(
        self, author: str, target: str, key_id: tuple[int, int]
    ) -> bool:
        now = time.monotonic()
        if len(self._sealed_at) > _SEALED_PRUNE_ABOVE:
            self._sealed_at = {
                k: v for k, v in self._sealed_at.items() if now - v[1] < RESEAL_AFTER_S
            }
        last = self._sealed_at.get((author, target))
        return last is not None and last[0] == key_id and now - last[1] < RESEAL_AFTER_S

    async def _post_deliveries(self, gfs_id: str, deliveries: list[dict]) -> int:
        conn = await self._gfs_repo.get(gfs_id)
        if self._http_client is None or conn is None or conn.status != "active":
            return 0
        assert self._signing_key is not None
        body = sign_wire(
            self._signing_key,
            {"instance_id": self._own_instance_id, "deliveries": deliveries},
        )
        try:
            async with self._http_client.post(
                f"{conn.inbox_url}/gfs/moments/keys",
                allow_redirects=False,
                json=body,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    log.warning(
                        "moment_public.keys: GFS %s refused %d sealed key(s) — HTTP %d",
                        gfs_id,
                        len(deliveries),
                        resp.status,
                    )
                    return 0
        except aiohttp.ClientError as exc:
            log.warning(
                "moment_public.keys: GFS %s key delivery failed: %s", gfs_id, exc
            )
            return 0
        return len(deliveries)

    def _require_kek(self) -> KeyManager:
        if self._kek is None:
            raise RuntimeError("MomentPublicKeyOutbound used before attach_identity")
        return self._kek


def _can_open(follower: dict) -> bool:
    """Whether the follower household declared both the KEM suite of its
    key-wrap key (one we seal to) and the moment seal suite."""
    suites = follower.get("moment_suites")
    if not isinstance(suites, dict):
        return False
    kem, seal = suites.get("kem"), suites.get("seal")
    kem_suite = str(follower.get("kem_suite") or "")
    return (
        isinstance(kem, list)
        and isinstance(seal, list)
        and kem_suite in SUPPORTED_KEM_SUITES
        and kem_suite in kem
        and MOMENT_SEAL_SUITE_AESGCM256 in seal
    )


def _bound_keywrap_key(instance_id: str, follower: dict) -> bytes | None:
    """The follower's key-wrap key — only when it is bound to the follower's
    identity end to end."""
    try:
        identity = bytes.fromhex(str(follower.get("identity_public_key") or ""))
        keywrap = bytes.fromhex(str(follower.get("keywrap_public_key") or ""))
    except ValueError:
        return None
    if not verify_keywrap_binding(
        instance_id=instance_id,
        identity_pub=identity,
        keywrap_pub=keywrap,
        keywrap_sig=str(follower.get("keywrap_sig") or ""),
    ):
        return None
    return keywrap
