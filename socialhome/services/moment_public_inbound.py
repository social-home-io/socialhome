"""Recipient-side handler for incoming public moments (§Momentum-public).

Wired as a callback on the persistent SH↔GFS WebSocket. The GFS pushes
``incoming_public_moment`` and ``incoming_public_moment_delete`` frames
when an author the local user follows posts or deletes, and ``moment_key``
frames carrying the author's sealed moment content key (v_62). This
handler:

1. Takes only an ENCRYPTED envelope (exactly
   :data:`~socialhome.moment_public_seal.ENVELOPE_FIELDS`): a plaintext one
   — an older author household's — is refused, never stored, so nothing
   that crossed a GFS in the clear lands here.
2. Verifies the envelope's Ed25519 signature against the cached
   ``followed_instance_pk`` for the (viewer, author, gfs) triple in
   :class:`MomentPublicFollow`. A bad signature is dropped silently
   (with a warning) — we never persist unauthenticated content.
3. Opens it with the author's key for that epoch
   (:class:`~socialhome.services.moment_public_key_inbound
   .MomentPublicKeyInbound`). Without the key the envelope is held
   briefly (bounded) and the key is asked for; when it arrives the held
   envelopes are opened in order.
4. Refreshes the author's public profile (name, bio, picture digest)
   on every local follow of them through that GFS — the envelope is
   signed by the author's household, so a GFS cannot rename anyone.
5. Persists the moment with ``received_via='gfs'`` and
   ``received_via_gfs_id={gfs_id}`` so the inbox can render the chip
   AND so the federation outbound's relay path will skip it (no
   redistribute rule).
6. Refuses a replay (v_64): the sealed, signed ``sent_at`` must be within
   :data:`SENT_AT_MAX_AGE_S` (and not ahead of the clock); a moment
   already held is never stored or announced twice.
7. Re-publishes :class:`MomentCreated` on the bus so the existing
   realtime + notification surfaces light up exactly as for a
   household-relayed moment.
8. Applies the signed delete certificates (v_66) the GFS hands it on a
   reconnect (``moment_delete_certs``) — deletes it missed while offline.
   Each is checked against the key this household pinned for the author
   when it followed (never one riding with the certificate) and applied as
   a delete (owner-bound ids, the stored row's author, tombstone rules — a
   replay changes nothing).
9. Keeps the author household's key-wrap key (v_68, ``author_keywrap_pk``
   in a verified create body, under a known KEM suite) on the follow rows
   (``moment_public_follows.followed_keywrap_pk``), and hands a
   ``moment_reaction`` frame — a follower's sealed reaction to a local
   author — to the reaction router (:mod:`.moment_reaction_router`), which
   opens and checks it like a routed one.
10. Takes an author's encrypted reaction totals (v_69,
    ``incoming_public_moment_counts``): verified like any envelope, opened
    only with a step key already held — a follower without it drops the
    update and asks for no key (no request storm) — and kept on the moment
    held here (:meth:`~.moment_reaction_counts.MomentReactionCounts
    .apply_totals`: that author and origin, live, newer ``rev`` only).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag

from ..domain.events import MomentCreated, MomentDeleted
from ..domain.moment import (
    MOMENT_MAX_LIFETIME_DAYS,
    Moment,
    clamp_moment_expiry,
    reaction_counts_valid,
)
from ..domain.moment_public import (
    MomentPublicFollow,
    public_author_bio,
    public_author_name,
    public_picture_digest,
)
from ..federation.keywrap_seal import SUPPORTED_KEM_SUITES
from ..federation.owner_bound_id import MOMENT_KIND, owner_bound_id_refused
from ..infrastructure.event_bus import EventBus
from ..moment_origin import delete_cert_refusal
from ..moment_public_seal import (
    AUTHOR_KEYWRAP_PK_FIELD,
    AUTHOR_KEYWRAP_SUITE_FIELD,
    ENVELOPE_FIELDS,
    FRAME_MOMENT_COUNTS,
    MOMENT_OP_COUNTS,
    UnsupportedMomentSealSuite,
    is_wire_int,
    item_key,
    open_body,
    verify_wire,
)
from .moment_public_media_inbound import MediaJob

if TYPE_CHECKING:
    from ..repositories.moment_public_repo import (
        AbstractMomentPublicFollowRepo,
    )
    from ..repositories.moment_repo import AbstractMomentRepo
    from .moment_public_key_inbound import MomentPublicKeyInbound
    from .moment_public_media_inbound import MomentPublicMediaInbound
    from .moment_reaction_counts import MomentReactionCounts
    from .moment_reaction_router import MomentReactionRouter

log = logging.getLogger(__name__)

#: Envelopes held for one (author, generation) while its key is asked for.
MAX_HELD_PER_EPOCH: int = 16

#: Envelopes held in all — past it, the oldest go.
MAX_HELD: int = 256

#: How long a held envelope waits for its key.
HOLD_FOR_S: float = 600.0

#: v_64: an envelope signed longer ago than this is refused (a replay). An
#: honest one can wait out a GFS's offline queue (24 h,
#: ``envelope_relay.ENVELOPE_QUEUE_TTL_SECONDS``) and then its key here
#: (:data:`HOLD_FOR_S`); a test pins the window above both plus the skew.
SENT_AT_MAX_AGE_S: int = 25 * 3600

#: v_64: how far ahead of this household's clock a send time may be.
SENT_AT_MAX_SKEW_S: int = 300

#: v_66: certificates applied from one ``moment_delete_certs`` frame, at most
#: (a GFS sends ≤ 50 a frame, ≤ 200 a push).
MAX_DELETE_CERTS_PER_FRAME: int = 50

#: v_66: a delete certificate signed longer ago than a moment can live (plus
#: a day of slack) is refused — the GFS keeps them 30 days.
DELETE_CERT_MAX_AGE_S: int = (MOMENT_MAX_LIFETIME_DAYS + 1) * 86_400

#: The frame type an envelope arrives in → the ``op`` its sealed body names.
_OPS: dict[str, str] = {
    "incoming_public_moment": "create",
    "incoming_public_moment_delete": "delete",
}


class MomentPublicInbound:
    __slots__ = (
        "_bus",
        "_counts",
        "_follows",
        "_held",
        "_keys",
        "_media",
        "_moments",
        "_reactions",
        "_sends_time",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        moment_repo: AbstractMomentRepo,
        follow_repo: AbstractMomentPublicFollowRepo,
        key_inbound: MomentPublicKeyInbound,
    ) -> None:
        self._bus = bus
        self._moments = moment_repo
        self._follows = follow_repo
        self._keys = key_inbound
        #: Encrypted media (v_63). Unwired: moments are kept without files.
        self._media: MomentPublicMediaInbound | None = None
        #: ``(author, generation)`` → ``[(monotonic time, op, envelope,
        #: gfs_id)]`` waiting for that generation's key, oldest first.
        self._held: dict[tuple[str, int], list[tuple[float, str, dict, str]]] = {}
        #: Authors whose envelopes carried ``sent_at`` (v_64): from then on
        #: one without it can only be an older envelope replayed. Bounded by
        #: the follows — only a followed author's signature verifies.
        self._sends_time: set[str] = set()
        #: v_68 — opens a follower's sealed reaction to a local author.
        #: Unwired: ``moment_reaction`` frames are dropped.
        self._reactions: MomentReactionRouter | None = None
        #: v_69 — keeps an author's reaction totals. Unwired: dropped.
        self._counts: MomentReactionCounts | None = None

    def attach_counts(self, counts: MomentReactionCounts) -> None:
        self._counts = counts

    def attach_media(self, media: MomentPublicMediaInbound) -> None:
        self._media = media

    def attach_reactions(self, router: MomentReactionRouter) -> None:
        self._reactions = router

    async def handle(self, frame: dict[str, Any], *, gfs_id: str) -> None:
        """Dispatch a single WS frame from the GFS this handler is bound to."""
        ftype = str(frame.get("type") or "")
        payload = frame.get("payload") or {}
        if ftype in _OPS:
            await self._on_envelope(payload, op=_OPS[ftype], gfs_id=gfs_id)
        elif ftype == "moment_key":
            gen = await self._keys.import_delivery(payload, gfs_id=gfs_id)
            if gen is not None:
                await self._release(str(payload.get("author_user_id") or ""), gen)
        elif ftype == "moment_delete_certs":
            await self._on_delete_certs(frame.get("certs"), gfs_id=gfs_id)
        elif ftype == "moment_reaction":
            if self._reactions is not None:
                await self._reactions.on_gfs_reaction(frame, gfs_id=gfs_id)
        elif ftype == FRAME_MOMENT_COUNTS:
            if self._counts is not None:
                await self._on_counts(payload, gfs_id=gfs_id)
        else:
            log.debug("moment_public.inbound: ignoring frame type=%r", ftype)

    # ── Envelopes ───────────────────────────────────────────────────────

    async def _on_envelope(self, env: Any, *, op: str, gfs_id: str) -> None:
        if not isinstance(env, dict) or set(env) != ENVELOPE_FIELDS:
            # An older author household's plaintext envelope (or junk): a
            # public moment that crossed the GFS in the clear never lands.
            log.info(
                "moment_public.inbound: %s refused — not an encrypted envelope",
                op,
            )
            return
        author = str(env.get("author_user_id") or "")
        gen, step = env.get("key_gen"), env.get("key_step")
        if not is_wire_int(gen) or not is_wire_int(step):
            return
        assert isinstance(gen, int) and isinstance(step, int)
        follow = await self._follow_of(env, gfs_id=gfs_id)
        if not verify_wire(env, follow.followed_instance_pk if follow else None):
            log.warning(
                "moment_public.inbound: signature failed for a %s by author %s",
                op,
                author,
            )
            return
        key = await self._keys.key_for(author, gen, step)
        if key is None:
            self._hold(author, gen, op, env, gfs_id)
            await self._keys.request_key(author, gfs_id=gfs_id, want_gen=gen)
            return
        try:
            body = open_body(step_key=key, envelope=env)
        except (InvalidTag, ValueError, TypeError, UnsupportedMomentSealSuite) as exc:
            log.warning(
                "moment_public.inbound: a %s by author %s doesn't open: %s",
                op,
                author,
                type(exc).__name__,
            )
            return
        if body.get("op") != op or body.get("author_user_id") != author:
            log.warning(
                "moment_public.inbound: sealed %s by author %s names another "
                "operation or author — dropped",
                op,
                author,
            )
            return
        refusal = self._send_time_refusal(author, op, body)
        if refusal is not None:
            log.info(
                "moment_public.inbound: %s by author %s refused — %s",
                op,
                author,
                refusal,
            )
            return
        # The moment id is the envelope's (bound into the AEAD).
        body["moment_id"] = env["moment_id"]
        if op == "create":
            await self._on_create(
                body,
                follow,
                gfs_id=gfs_id,
                item=item_key(key, str(env["moment_id"])),
            )
        else:
            await self._on_delete(body, outer_instance_id=str(env["instance_id"]))

    async def _on_counts(self, env: Any, *, gfs_id: str) -> bool:
        """An author's encrypted reaction totals (v_69). Kept only when the
        envelope verifies against the followed author's pinned key, opens
        under a step key this household already holds, names ``op:
        "counts"`` for that author, was sent lately (``sent_at``) and the
        moment is held here — by that author and household, live, older
        totals only. Without the step key it is dropped quietly: totals are
        not worth asking the author for a key (no request storm), the next
        moment brings it. Returns whether the totals were kept."""
        assert self._counts is not None
        if not isinstance(env, dict) or set(env) != ENVELOPE_FIELDS:
            log.info("moment_public.inbound: counts refused — not an envelope")
            return False
        author = str(env.get("author_user_id") or "")
        gen, step = env.get("key_gen"), env.get("key_step")
        if not is_wire_int(gen) or not is_wire_int(step):
            return False
        assert isinstance(gen, int) and isinstance(step, int)
        follow = await self._follow_of(env, gfs_id=gfs_id)
        if not verify_wire(env, follow.followed_instance_pk if follow else None):
            log.warning(
                "moment_public.inbound: signature failed for counts by author %s",
                author,
            )
            return False
        key = await self._keys.key_for(author, gen, step)
        if key is None:
            log.debug(
                "moment_public.inbound: no key for counts by author %s — dropped",
                author,
            )
            return False
        try:
            body = open_body(step_key=key, envelope=env)
        except (InvalidTag, ValueError, TypeError, UnsupportedMomentSealSuite) as exc:
            log.warning(
                "moment_public.inbound: counts by author %s don't open: %s",
                author,
                type(exc).__name__,
            )
            return False
        if body.get("op") != MOMENT_OP_COUNTS or body.get("author_user_id") != author:
            log.warning(
                "moment_public.inbound: sealed counts by author %s name another "
                "operation or author — dropped",
                author,
            )
            return False
        refusal = self._send_time_refusal(author, MOMENT_OP_COUNTS, body)
        rev, counts, total = body.get("rev"), body.get("counts"), body.get("total")
        if refusal is None and (
            not is_wire_int(rev)
            or not reaction_counts_valid(counts, total)
            or int(rev) > (time.time() + SENT_AT_MAX_SKEW_S) * 1000  # type: ignore[arg-type]
        ):
            refusal = "malformed totals"
        if refusal is not None:
            log.info(
                "moment_public.inbound: counts by author %s refused — %s",
                author,
                refusal,
            )
            return False
        assert isinstance(rev, int) and isinstance(counts, dict)
        assert isinstance(total, int)
        return await self._counts.apply_totals(
            moment_id=str(env["moment_id"]),
            author_user_id=author,
            # The household that signed the envelope — the stored row must
            # name it as the moment's origin.
            origin_instance_id=str(env["instance_id"]),
            rev=rev,
            counts=counts,
            total=total,
        )

    async def _on_delete_certs(self, certs: Any, *, gfs_id: str) -> int:
        """Apply the delete certificates (v_66) of one ``moment_delete_certs``
        frame; returns how many changed something here.

        Each must be a genuine certificate of an author followed through
        *gfs_id*: signed by the identity key pinned in that follow
        (``followed_instance_pk`` — never one riding with it), that key
        deriving to the certificate's origin household, a known suite, and a
        delete time within :data:`DELETE_CERT_MAX_AGE_S`. It is then applied
        exactly like a delete through the GFS (:meth:`_on_delete`)."""
        if not isinstance(certs, list):
            return 0
        changed = 0
        pins: dict[str, str | None] = {}
        for cert in certs[:MAX_DELETE_CERTS_PER_FRAME]:
            if not isinstance(cert, dict):
                continue
            author = str(cert.get("author_user_id") or "")
            if author not in pins:
                follow = await self._follow_of(
                    {"author_user_id": author}, gfs_id=gfs_id
                )
                pins[author] = follow.followed_instance_pk if follow else None
            pin = pins[author]
            refusal = (
                "author not followed through this GFS"
                if not pin
                else delete_cert_refusal(cert, identity_pk_hex=pin)
            ) or _delete_cert_age_refusal(cert.get("occurred_at"))
            if refusal is not None:
                log.warning(
                    "moment_public.inbound: delete certificate for author %s "
                    "refused — %s",
                    author,
                    refusal,
                )
                continue
            if await self._on_delete(
                {
                    "moment_id": cert["moment_id"],
                    "author_user_id": author,
                    "origin_instance_id": cert["origin_instance_id"],
                },
                outer_instance_id=cert["origin_instance_id"],
            ):
                changed += 1
        if changed:
            log.info(
                "moment_public.inbound: %d delete(s) caught up from %s", changed, gfs_id
            )
        return changed

    def _send_time_refusal(self, author: str, op: str, body: dict) -> str | None:
        """Why the signed send time (v_64) refuses *body*, or ``None``.

        ``sent_at`` is sealed and signed with the moment, so nobody but the
        author's household can set or strip it. Present: an integer within
        :data:`SENT_AT_MAX_AGE_S` before now and :data:`SENT_AT_MAX_SKEW_S`
        after, and a create's ``created_at`` not after it. Absent: an older
        (v_63) author household — the older rules apply — unless this author
        already sent one, so a pre-upgrade envelope can't be replayed past
        the window.
        """
        if "sent_at" not in body:
            if author in self._sends_time:
                return "no send time from an author who sends one (a replay)"
            return None
        sent = body["sent_at"]
        if not is_wire_int(sent):
            return "malformed send time"
        assert isinstance(sent, int)
        now = time.time()
        if sent > now + SENT_AT_MAX_SKEW_S:
            return "sent in the future"
        if now - sent > SENT_AT_MAX_AGE_S:
            return "sent too long ago (a replay)"
        if op == "create":
            created = _unix_or_none(body.get("created_at"))
            if created is not None and created > sent + SENT_AT_MAX_SKEW_S:
                return "created after it was sent"
        self._sends_time.add(author)
        return None

    def _hold(self, author: str, gen: int, op: str, env: dict, gfs_id: str) -> None:
        """Keep *env* until its key arrives — bounded per generation and in
        all."""
        now = monotonic()
        for k in list(self._held):
            self._held[k] = [h for h in self._held[k] if now - h[0] < HOLD_FOR_S]
            if not self._held[k]:
                del self._held[k]
        held = self._held.setdefault((author, gen), [])
        if len(held) >= MAX_HELD_PER_EPOCH:
            log.info(
                "moment_public.inbound: %d moments of author %s already wait "
                "for their key — dropping one",
                len(held),
                author,
            )
            return
        held.append((now, op, env, gfs_id))
        total = sum(len(v) for v in self._held.values())
        while total > MAX_HELD:
            oldest = min(self._held, key=lambda k: self._held[k][0][0])
            self._held[oldest].pop(0)
            if not self._held[oldest]:
                del self._held[oldest]
            total -= 1

    async def _release(self, author: str, gen: int) -> None:
        """Open what was held for (*author*, *gen*) now its key is here."""
        now = monotonic()
        for at, op, env, gfs_id in self._held.pop((author, gen), []):
            if now - at < HOLD_FOR_S:
                await self._on_envelope(env, op=op, gfs_id=gfs_id)

    async def _on_create(
        self,
        env: dict,
        follow: MomentPublicFollow | None,
        *,
        gfs_id: str,
        item: bytes,
    ) -> None:
        if not all(
            isinstance(env.get(k), str) and env.get(k)
            for k in ("moment_id", "created_at", "expires_at")
        ):
            log.warning("moment_public.inbound: sealed create is malformed — dropped")
            return
        if not _still_live(env):
            # A replayed old envelope: never re-stored, never a profile
            # refresh (M7) — whatever the store holds.
            log.info(
                "moment_public.inbound: create for expired moment %s refused",
                env.get("moment_id"),
            )
            return
        if _moment_id_refused(env, "create"):
            return
        stored = await self._moments.get(str(env["moment_id"]), include_deleted=True)
        if not _row_binds(
            stored,
            author_user_id=str(env["author_user_id"]),
            origin_instance_id=str(env.get("origin_instance_id") or ""),
            what="create",
            moment_id=str(env["moment_id"]),
        ):
            return
        if stored is not None and stored.deleted_at is not None:
            # Deletes stick: a replayed create for a deleted moment is
            # refused for as long as its tombstone lives.
            log.info(
                "moment_public.inbound: refusing create for deleted moment=%s",
                env.get("moment_id"),
            )
            return
        if stored is not None:
            # Held already (a replay within the window, a second GFS, or the
            # household relay first): one moment is stored and announced once.
            # A paired household's copy says nothing about public; this
            # verified copy does, so a local reply to it may be public too.
            if not stored.is_public:
                await self._moments.mark_public(stored.id)
            log.debug(
                "moment_public.inbound: moment %s already held — not re-stored",
                env.get("moment_id"),
            )
            return
        parent_id = env.get("parent_moment_id")
        if parent_id and await self._moments.get(str(parent_id)) is None:
            # A public reply to a moment this household never received (it
            # was not public, or arrived before the follow): out of context,
            # and the row's parent reference could not be stored anyway.
            log.info(
                "moment_public.inbound: reply %s to a moment not held here — dropped",
                env.get("moment_id"),
            )
            return
        if (
            stored is None
            and follow is not None
            and isinstance(env.get("profile"), dict)
        ):
            # Only a live moment we have not seen refreshes the profile, so a
            # replay of an old envelope cannot roll a newer one back.
            await self._refresh_profile(env["profile"], follow, gfs_id=gfs_id)
        if stored is None and follow is not None:
            await self._keep_keywrap(env, follow, gfs_id=gfs_id)
        media = env.get("media")
        if isinstance(media, dict) and self._media is not None:
            # v_63: kept once its file is here (fetched off the GFS socket).
            async def _then(media_url: str | None, media_type: str | None) -> None:
                await self._store(env, gfs_id, media_url, media_type)

            if self._media.submit(
                MediaJob(
                    gfs_id=gfs_id,
                    author_user_id=str(env["author_user_id"]),
                    moment_id=str(env["moment_id"]),
                    item_key=item,
                    media=media,
                    then=_then,
                )
            ):
                return
        # No file to keep: an author's own ``media_url`` (an older body)
        # names a file on THEIR household, never one here.
        await self._store(env, gfs_id, None, None)

    async def _store(
        self,
        env: dict,
        gfs_id: str,
        media_url: str | None,
        media_type: str | None,
    ) -> None:
        moment = Moment(
            id=str(env["moment_id"]),
            author_user_id=str(env["author_user_id"]),
            content=str(env.get("content") or ""),
            media_url=media_url,
            media_type=media_type if media_url is not None else None,
            duration_ms=env.get("duration_ms") if media_url is not None else None,
            parent_moment_id=env.get("parent_moment_id"),
            origin_instance_id=str(env.get("origin_instance_id") or ""),
            created_at=str(env["created_at"]),
            # v_65: kept at most 30 days whatever the author sealed.
            expires_at=clamp_moment_expiry(
                str(env["created_at"]), str(env["expires_at"])
            ),
            is_public=True,
            received_via="gfs",
            received_via_gfs_id=gfs_id,
        )
        await self._moments.save(moment)
        await self._bus.publish(
            MomentCreated(
                moment_id=moment.id,
                author_user_id=moment.author_user_id,
                content=moment.content,
                media_url=moment.media_url,
                media_type=moment.media_type,
                duration_ms=moment.duration_ms,
                parent_moment_id=moment.parent_moment_id,
                # Reply parent's author isn't carried on the wire — the
                # parent moment's row (if local) holds it. NULL is fine
                # for notification routing.
                parent_author_user_id=None,
                origin_instance_id=moment.origin_instance_id,
                expires_at=moment.expires_at,
                received_via="gfs",
            )
        )

    async def _on_delete(self, env: dict, *, outer_instance_id: str) -> bool:
        """Apply a verified delete; ``True`` when it changed something here
        (a moment removed, or a tombstone recorded for one never held)."""
        # The signature (checked on the envelope or the certificate) is what
        # keeps a malicious peer from taking down a moment it doesn't own.
        moment_id = str(env.get("moment_id") or "")
        if not moment_id:
            return False
        author_user_id = str(env.get("author_user_id") or "")
        # The author household names itself on the signed envelope.
        origin_instance_id = str(env.get("origin_instance_id") or outer_instance_id)
        if _moment_id_refused(env, "delete"):
            return False  # never tombstoned: it would refuse the real moment
        stored = await self._moments.get(moment_id, include_deleted=True)
        if not _row_binds(
            stored,
            author_user_id=author_user_id,
            origin_instance_id=origin_instance_id,
            what="delete",
            moment_id=moment_id,
        ):
            return False
        existing = stored if stored is not None and stored.deleted_at is None else None
        # Tombstoned whether or not we hold it, so a create that arrives
        # later (or is replayed) is refused — held for the longest a moment
        # can live when we never saw it.
        outcome = await self._moments.tombstone(
            moment_id,
            author_user_id=author_user_id,
            origin_instance_id=origin_instance_id,
            expires_at=(
                datetime.now(UTC) + timedelta(days=MOMENT_MAX_LIFETIME_DAYS)
            ).isoformat(),
        )
        if existing is None:
            return outcome.changed
        if self._media is not None:
            await self._media.forget(existing.media_url)
        await self._bus.publish(
            MomentDeleted(
                moment_id=moment_id,
                author_user_id=author_user_id,
                origin_instance_id=existing.origin_instance_id,
            )
        )
        return True

    async def _follow_of(self, env: dict, *, gfs_id: str) -> MomentPublicFollow | None:
        author = str(env.get("author_user_id") or "")
        if not author:
            return None
        # Any local follower of this author records the author's pk;
        # find the first row that matches on (followed=author, gfs=gfs_id).
        for row in await self._follows.followers_of(author):
            if row.gfs_id == gfs_id:
                return row
        return None

    async def _keep_keywrap(
        self, body: dict, follow: MomentPublicFollow, *, gfs_id: str
    ) -> None:
        """Keep the author household's key-wrap key a verified create carried
        (v_68) — what a reaction to their public moments is sealed to. Under
        an unknown suite, or malformed, it is ignored: that author's moments
        keep their reactions here, as with an older author."""
        named = _keywrap_of(body)
        if named is None:
            if body.get(AUTHOR_KEYWRAP_PK_FIELD) is not None:
                log.info(
                    "moment_public.inbound: author %s names a key-wrap key this"
                    " build can't use — reactions to it stay here",
                    follow.followed_user_id,
                )
            return
        keywrap, suite = named
        if (keywrap, suite) == (
            follow.followed_keywrap_pk,
            follow.followed_keywrap_suite,
        ):
            return
        await self._follows.set_keywrap(
            followed_user_id=follow.followed_user_id,
            gfs_id=gfs_id,
            keywrap_pk=keywrap,
            keywrap_suite=suite,
        )

    async def _refresh_profile(
        self, profile: dict, follow: MomentPublicFollow, *, gfs_id: str
    ) -> None:
        """Take the author's name, bio and picture digest from the profile a
        verified moment carried (it rides only when it changed, or daily)."""
        author = follow.followed_user_id
        name = (
            public_author_name(profile.get("display_name"), user_id=author)
            or public_author_name(profile.get("username"), user_id=author)
            or follow.followed_display_name
        )
        bio = public_author_bio(profile.get("bio"))
        digest = public_picture_digest(profile.get("picture_digest"))
        if (name, bio, digest) == (
            follow.followed_display_name,
            follow.followed_bio,
            follow.followed_picture_digest,
        ):
            return
        await self._follows.set_profile(
            followed_user_id=author,
            gfs_id=gfs_id,
            display_name=name,
            bio=bio,
            picture_digest=digest,
        )


def _keywrap_of(body: dict) -> tuple[str, str] | None:
    """The author household's key-wrap key a sealed public-moment create body
    names (v_68, ``author_keywrap_pk``) as 64 lowercase hex chars, with the
    KEM suite it is named under (``author_keywrap_suite``) — kept together,
    so the key is never read under an assumed suite. ``None`` when it names
    none, names it under a suite this build does not implement (refused,
    never read under a default), or is malformed."""
    pk = body.get(AUTHOR_KEYWRAP_PK_FIELD)
    suite = body.get(AUTHOR_KEYWRAP_SUITE_FIELD)
    if pk is None or not isinstance(suite, str) or suite not in SUPPORTED_KEM_SUITES:
        return None
    if not isinstance(pk, str) or len(pk) != 64:
        return None
    try:
        return bytes.fromhex(pk).hex(), suite
    except ValueError:
        return None


def _unix_or_none(value: object) -> float | None:
    """Unix seconds of an ISO 8601 time (naive = UTC), or ``None``."""
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def _delete_cert_age_refusal(occurred_at: object) -> str | None:
    """Why a delete certificate's signed time refuses it, or ``None``: older
    than :data:`DELETE_CERT_MAX_AGE_S` (no moment it names can still be
    live) or more than :data:`SENT_AT_MAX_SKEW_S` ahead of the clock."""
    at = _unix_or_none(occurred_at)
    if at is None:
        return "malformed delete time"
    now = time.time()
    if at > now + SENT_AT_MAX_SKEW_S:
        return "deleted in the future"
    if now - at > DELETE_CERT_MAX_AGE_S:
        return "deleted too long ago"
    return None


def _still_live(body: dict) -> bool:
    """A create whose moment hasn't expired (its ``expires_at`` clamped to 30
    days, v_65) and isn't older than a moment can live — else it is a replay
    (or junk), refused before anything is read from it."""
    try:
        created = datetime.fromisoformat(str(body["created_at"]))
        expires = datetime.fromisoformat(
            clamp_moment_expiry(str(body["created_at"]), str(body["expires_at"]))
        )
    except KeyError, ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    return expires > now and created > now - timedelta(days=MOMENT_MAX_LIFETIME_DAYS)


def _row_binds(
    stored: Moment | None,
    *,
    author_user_id: str,
    origin_instance_id: str,
    what: str,
    moment_id: str,
) -> bool:
    """A moment id already held here — live or a delete's tombstone — keeps
    its author and origin: a create or delete naming anyone else is refused.

    The same rule as the household path
    (``FederationInboundService._moment_row_binds``): ``save`` upserts on the
    bare id, so without it the first holder of a legacy id could be
    overwritten by whoever signs a moment of their own under it.
    """
    if stored is None or (
        stored.author_user_id == author_user_id
        and stored.origin_instance_id == origin_instance_id
    ):
        return True
    log.warning(
        "moment_public.inbound: %s for moment %s belongs to another author — refusing",
        what,
        moment_id,
    )
    return False


def _moment_id_refused(envelope: dict, what: str) -> bool:
    """An owner-bound moment id (v_36) claimed for another author."""
    return owner_bound_id_refused(
        MOMENT_KIND,
        str(envelope.get("moment_id") or ""),
        space_id="",
        owner_user_id=str(envelope.get("author_user_id") or ""),
        context=f"moment_public.inbound {what}",
    )
