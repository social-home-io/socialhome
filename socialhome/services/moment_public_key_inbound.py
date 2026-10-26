"""Follower side of encrypted public moments (§Momentum-public, v_62).

A followed author's household seals its current moment step key to this
household's static key-wrap key and sends it through the GFS
(``moment_key`` frame — pushed at once, or from the GFS's mailbox when this
household reconnects). This service takes it in, keeps it KEK-sealed in
``moment_public_keys`` (``is_own = 0``), ratchets it forward as moments of
later days arrive (overwriting the older step — forward secrecy at rest),
and hands :class:`~socialhome.services.moment_public_inbound
.MomentPublicInbound` the step key a moment names.

A delivery is taken only when ALL hold — anything else is dropped:

* exactly the delivery shape (:data:`DELIVERY_FIELDS`), a known
  ``key_suite``, addressed to us;
* a local user follows that author **through this GFS**, and the delivery
  is signed by the author household key that follow pinned and names the
  household that key derives from — so neither the GFS nor anyone else can
  plant a key of its own;
* it opens with our key-wrap private key (unknown KEM suites refused) and
  the sealed plaintext names the same author, key id and recipient as the
  signed outer delivery;
* the author id is not a local user's (a delivered key never shadows a
  local author's own), and it moves us forward: a new generation, or a
  later step of the one we hold (never back, never a swap).

When a moment arrives under a generation this household has no key for,
the inbound asks (:meth:`request_key`) — at most once a minute per author;
the GFS answers from its mailbox or forwards the ask to the author, only
for a household that follows.

The follow request declares what this household can open
(:data:`OWN_MOMENT_SUITES`, signed): an author seals keys only to a
household that declared them.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from cryptography.exceptions import InvalidTag

from ..crypto import derive_instance_id
from ..domain.moment_public import MomentPublicKey, is_path_safe_id
from ..federation.keywrap_seal import SUPPORTED_KEM_SUITES, open_keywrap
from ..moment_public_seal import (
    DELIVERY_FIELDS,
    MAX_RATCHET_STEPS,
    MOMENT_OP_COUNTS,
    SUPPORTED_MOMENT_SEAL_SUITES,
    UnsupportedMomentSealSuite,
    is_wire_int,
    key_at_rest_context,
    parse_key_plaintext,
    ratchet,
    require_known_key_suite,
    sign_wire,
    verify_wire,
)

if TYPE_CHECKING:
    from ..infrastructure.key_manager import KeyManager
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.moment_public_repo import (
        AbstractMomentPublicFollowRepo,
        AbstractMomentPublicKeyRepo,
    )
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

#: What this household can open — declared (signed) in every follow request.
OWN_MOMENT_SUITES: dict[str, list[str]] = {
    "kem": sorted(SUPPORTED_KEM_SUITES),
    "seal": sorted(SUPPORTED_MOMENT_SEAL_SUITES),
}

#: v_69 — the extra frames this household takes, declared (as ``ops``) only
#: to a GFS that proved ``moment_counts``: reaction totals of the moments it
#: holds, pushed while it is connected.
OWN_MOMENT_OPS: tuple[str, ...] = (MOMENT_OP_COUNTS,)

#: A follower asks one author for a key at most this often.
REQUEST_EVERY_S: float = 60.0

#: Delivered generations kept per followed author: the current one and the
#: one before it, for moments sealed just before a rotation.
KEEP_DELIVERED_GENERATIONS: int = 2

#: Past this many remembered requests, the stale ones are forgotten.
_REQUESTED_PRUNE_ABOVE: int = 10_000


class MomentPublicKeyInbound:
    """Import followed authors' sealed moment keys; ask for missing ones."""

    __slots__ = (
        "_follows",
        "_gfs_repo",
        "_http_client",
        "_kek",
        "_keys",
        "_keywrap_private_key",
        "_own_instance_id",
        "_requested_at",
        "_signing_key",
        "_users",
    )

    def __init__(
        self,
        *,
        key_repo: AbstractMomentPublicKeyRepo,
        follow_repo: AbstractMomentPublicFollowRepo,
        user_repo: AbstractUserRepo,
        gfs_repo: AbstractGfsConnectionRepo,
        http_client: aiohttp.ClientSession | None = None,
    ) -> None:
        self._keys = key_repo
        self._follows = follow_repo
        self._users = user_repo
        self._gfs_repo = gfs_repo
        self._http_client = http_client
        self._kek: KeyManager | None = None
        self._signing_key: bytes | None = None
        self._keywrap_private_key: bytes | None = None
        self._own_instance_id: str = ""
        #: author → monotonic time of the last key request.
        self._requested_at: dict[str, float] = {}

    def attach_session(self, session: aiohttp.ClientSession) -> None:
        if self._http_client is None:
            self._http_client = session

    def attach_identity(
        self,
        *,
        own_instance_id: str,
        signing_key: bytes,
        key_manager: KeyManager,
        keywrap_private_key: bytes,
    ) -> None:
        self._own_instance_id = own_instance_id
        self._signing_key = signing_key
        self._kek = key_manager
        self._keywrap_private_key = keywrap_private_key

    # ── Keys held ───────────────────────────────────────────────────────

    async def key_for(
        self, author_user_id: str, key_gen: int, key_step: int
    ) -> bytes | None:
        """The step key for (*key_gen*, *key_step*) of a followed author —
        ratcheted forward from the step held, which then moves forward too
        (so an older step can't be recomputed here). ``None`` for an unknown
        generation, a step older than the one held, or one too far ahead."""
        if self._kek is None:
            return None
        row = await self._keys.get(author_user_id, key_gen, own=False)
        if row is None or key_step < row.key_step:
            return None
        if key_step - row.key_step > MAX_RATCHET_STEPS:
            return None
        ctx = key_at_rest_context(author_user_id, key_gen, own=False)
        key = self._kek.decrypt(row.key_sealed, associated_data=ctx)
        if key_step == row.key_step:
            return key
        key = ratchet(key, key_step - row.key_step)
        await self._keys.advance(
            MomentPublicKey(
                author_user_id=author_user_id,
                key_gen=key_gen,
                key_step=key_step,
                is_own=False,
                key_sealed=self._kek.encrypt(key, associated_data=ctx),
            )
        )
        return key

    async def forget_if_unfollowed(self, author_user_id: str) -> None:
        """Drop a followed author's keys once no local user follows them."""
        if not await self._follows.followers_of(author_user_id):
            await self._keys.delete_all(author_user_id, own=False)

    # ── Deliveries ──────────────────────────────────────────────────────

    async def import_delivery(self, delivery: Any, *, gfs_id: str) -> int | None:
        """Take one sealed key from the GFS. Returns the generation it moved
        forward, or ``None`` when the delivery was refused (logged) or
        brought nothing new."""
        if self._kek is None or self._keywrap_private_key is None:
            return None
        if not isinstance(delivery, dict) or set(delivery) != DELIVERY_FIELDS:
            log.info("moment_public.keys: malformed key delivery dropped")
            return None
        author = str(delivery.get("author_user_id") or "")
        gen, step = delivery.get("key_gen"), delivery.get("key_step")
        if (
            not author
            or not is_wire_int(gen)
            or not is_wire_int(step)
            or not isinstance(delivery.get("sealed"), dict)
            or delivery.get("to") != self._own_instance_id
        ):
            log.info("moment_public.keys: key delivery not for us — dropped")
            return None
        assert isinstance(gen, int) and isinstance(step, int)
        try:
            require_known_key_suite(delivery.get("key_suite"))
        except UnsupportedMomentSealSuite:
            log.warning("moment_public.keys: key delivery with an unknown suite")
            return None
        pk = await self._followed_pk(author, gfs_id=gfs_id)
        if pk is None or not verify_wire(delivery, pk):
            log.warning(
                "moment_public.keys: key delivery for author %s on %s is not "
                "signed by the household we follow them at — dropped",
                author,
                gfs_id,
            )
            return None
        if derive_instance_id(bytes.fromhex(pk)) != delivery.get("instance_id"):
            log.warning(
                "moment_public.keys: key delivery for author %s names another "
                "household — dropped",
                author,
            )
            return None
        if await self._users.get_by_user_id(author) is not None:
            log.warning(
                "moment_public.keys: key delivery names local user %s — dropped",
                author,
            )
            return None
        try:
            opened = open_keywrap(
                sealed=delivery["sealed"],
                recipient_keywrap_priv=self._keywrap_private_key,
            )
            key = parse_key_plaintext(
                opened,
                author_user_id=author,
                key_gen=gen,
                key_step=step,
                to=self._own_instance_id,
            )
        except (InvalidTag, ValueError, TypeError, KeyError) as exc:
            log.warning(
                "moment_public.keys: key delivery for author %s doesn't open: %s",
                author,
                type(exc).__name__,
            )
            return None
        row = MomentPublicKey(
            author_user_id=author,
            key_gen=gen,
            key_step=step,
            is_own=False,
            key_sealed=self._kek.encrypt(
                key, associated_data=key_at_rest_context(author, gen, own=False)
            ),
        )
        held = await self._keys.get(author, gen, own=False)
        if held is None:
            await self._keys.save(row)
            await self._keys.prune(author, own=False, keep=KEEP_DELIVERED_GENERATIONS)
        elif not await self._keys.advance(row):
            return None
        log.info(
            "moment_public.keys: moment key %d/%d of author %s stored",
            gen,
            step,
            author,
        )
        return gen

    async def _followed_pk(self, author: str, *, gfs_id: str) -> str | None:
        for row in await self._follows.followers_of(author):
            if row.gfs_id == gfs_id and row.followed_instance_pk:
                return row.followed_instance_pk
        return None

    # ── Requests ────────────────────────────────────────────────────────

    async def request_key(
        self, author_user_id: str, *, gfs_id: str, want_gen: int
    ) -> bool:
        """Ask for *author_user_id*'s key of generation *want_gen* through
        *gfs_id*. At most once per :data:`REQUEST_EVERY_S` per author;
        ``True`` when a request went out."""
        if self._signing_key is None or self._http_client is None:
            return False
        # The id goes into a URL path: one path-safe segment only.
        if not is_path_safe_id(author_user_id):
            return False
        now = time.monotonic()
        last = self._requested_at.get(author_user_id)
        if last is not None and now - last < REQUEST_EVERY_S:
            return False
        conn = await self._gfs_repo.get(gfs_id)
        if conn is None or conn.status != "active":
            return False
        if len(self._requested_at) > _REQUESTED_PRUNE_ABOVE:
            self._requested_at = {
                a: t for a, t in self._requested_at.items() if now - t < REQUEST_EVERY_S
            }
        self._requested_at[author_user_id] = now
        body = sign_wire(
            self._signing_key,
            {"instance_id": self._own_instance_id, "want_gen": want_gen},
        )
        try:
            async with self._http_client.post(
                f"{conn.inbox_url}/gfs/moments/users/{author_user_id}/key_request",
                allow_redirects=False,
                json=body,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    log.warning(
                        "moment_public.keys: GFS %s refused a key request — HTTP %d",
                        gfs_id,
                        resp.status,
                    )
                    return False
        except aiohttp.ClientError as exc:
            log.warning("moment_public.keys: key request to %s failed: %s", gfs_id, exc)
            return False
        log.info(
            "moment_public.keys: asked for author %s's moment key through %s",
            author_user_id,
            gfs_id,
        )
        return True
