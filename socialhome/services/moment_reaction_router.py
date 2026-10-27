"""Reactions reach authors further away (v_67, owner decision 9, sign-off 2).

A moment reaches households up to three hops from its author; before v_67 a
reaction went back only to an author's household *paired* with the
reactor's. This service carries a reaction from further away back along the
path the moment came — the wire shapes and crypto are in
:mod:`socialhome.federation.moment_reaction_route`. It plays three parts:

* **Creates.** The origin adds its key-wrap key to every ``MOMENT_CREATED``
  it fans out (:meth:`origin_keywrap_fields`); each relay appends a return
  token for the peer it forwards to (:meth:`stamp_relayed_create`); a
  receiver that stores the moment keeps the complete route
  (:meth:`route_from_create`, ``moments.reaction_route``).
* **Reactor.** A reaction to a moment whose author is not on this or a paired
  household — and whose origin is not a paired household either, else the
  direct path and its hide list apply (:meth:`direct_target`) —
  (:meth:`queue_reaction`) is coalesced per (moment, person) for
  :data:`DEBOUNCE_S`, signed by this household, sealed to the origin's
  key-wrap key and sent as ``MOMENT_REACTION_ROUTED`` to the neighbours the
  moment came from — along :data:`MAX_ROUTES` of the kept routes (≤
  :data:`MAX_KEPT_ROUTES`, from distinct neighbours, picked per reaction;
  #1178 F13, #1197 review) that are complete and whose neighbour is a
  confirmed v_67 peer (``peer_supports``). Otherwise the reaction stays in this household,
  as before, and the API says so (:meth:`reach_for`).
* **Relay and origin.** :meth:`_on_routed` either opens its own return token
  and passes the reaction one hop back, or — at the origin — opens the seal
  and applies the reaction after the authority checks (see
  ``docs/protocol/momentum.md`` § Authority). Relays keep no state and learn
  only their own neighbours.

* **Through a GFS (v_68).** A reaction to a public moment that came through
  a GFS takes the same sealed reaction to the GFS it came on
  (:class:`~.moment_reaction_gfs.MomentReactionGfs`); the author's
  household gets it as a ``moment_reaction`` frame
  (:meth:`on_gfs_reaction`) and applies it after the same checks, plus: the
  household the GFS authenticated is the reactor's own, and the moment is
  public.

**Privacy residual (signed off 2026-10-25).** A relay learns that its direct
neighbour (or that neighbour's part of the mesh) reacted to moment M — not
who, not with what (the seal is padded to one size) — and no household
learns any non-adjacent part of the pairing graph. A GFS learns that a
follower household sent an author's household a reaction or a removal, and
when — not who, with what, to which moment, or which of the two.
``docs/principles.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import random
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag

from ..crypto import derive_user_id
from ..domain.events import MomentReactionChanged
from ..domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
)
from ..domain.federation_capabilities import FederationCapability
from ..domain.moment import (
    MOMENT_MAX_HOPS,
    REACTION_REACH_AUTHOR,
    REACTION_REACH_HOUSEHOLD,
    Moment,
    MomentGfsReactionTarget,
    MomentReactionRoute,
    clamp_moment_expiry,
)
from ..federation.keywrap_seal import SUPPORTED_KEM_SUITES, UnsupportedKemSuite
from ..federation.moment_reaction_route import (
    MAX_ID_CHARS,
    RETURN_TOKEN_SUITE_AESGCM_256,
    RETURN_TOKEN_SUITE_FIELD,
    RETURN_TOKENS_FIELD,
    SUPPORTED_RETURN_TOKEN_SUITES,
    ReturnTokenRefused,
    UnsupportedReactionSigSuite,
    UnsupportedReturnTokenSuite,
    build_reaction,
    mint_return_token,
    open_reaction,
    open_return_token,
    origin_keywrap_fields,
    return_token_key,
    return_tokens_of,
    seal_reaction,
    sealed_shape_ok,
    verified_origin_keywrap,
    verify_reaction,
)
from ..rate_limiter import RateLimiter
from .protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..infrastructure.event_bus import EventBus
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.instance_ban_repo import AbstractHouseholdInstanceBanRepo
    from ..repositories.moment_repo import AbstractMomentRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .moment_reaction_gfs import MomentReactionGfs

log = logging.getLogger(__name__)

FET = FederationEventType

#: A person's reactions to one moment are sent once they settle: the latest
#: state per (moment, person) this long after the first change.
DEBOUNCE_S: float = 3.0

#: A routed reaction is taken up to this long after it was made — a day of
#: outbox retries plus clock skew — and refused after.
MAX_REACTION_AGE_MS: int = 25 * 3600 * 1000
#: …and at most this far in the future (clock skew).
MAX_REACTION_SKEW_MS: int = 300 * 1000

#: Sealed reactions seen at the origin, kept for :data:`MAX_REACTION_AGE_MS`
#: so a replay inside the window is refused (and one outside is too old).
SEEN_MAX: int = 50_000

#: A moment keeps up to :data:`MAX_KEPT_ROUTES` routes — the first copies
#: of it from distinct neighbours — and each reaction goes along
#: :data:`MAX_ROUTES` of the usable ones, picked afresh at random per
#: reaction (#1178 F13, #1197 review): a neighbour that claims a shorter hop
#: than it is (``hop_count`` is outside the origin signature) can't capture
#: the route, and a relay that drops and sits on two of the kept paths still
#: misses the reactions sent around it. There is no delivery feedback, so the
#: pick is random rather than "avoid what failed". A kept route whose
#: neighbour is no longer a confirmed v_67 peer gives its slot to the next
#: copy that arrives. ≤ 2 envelopes per reaction; the origin dedupes by its
#: replay digest.
MAX_ROUTES: int = 2
MAX_KEPT_ROUTES: int = 3

#: Relay limits on what one downstream neighbour hands over (#1178 F14,
#: #1197 review). Every envelope from it counts against one budget: 180 a
#: minute, 30 a minute on one moment — 3× one household's own rate, since
#: a one-token envelope may come from a relay carrying several households'
#: reactions, so one household behind it sending junk at the full source
#: rate leaves room for the others. Inside that budget, envelopes carrying
#: **two tokens** — no relay sits beyond two, so they are the neighbour's
#: own reactions (the *source* rate) — are held to 60 a minute and 10 a
#: minute on one moment. An envelope the relay refuses is not charged.
RELAY_SOURCE_PER_MIN: int = 60
RELAY_SOURCE_PER_MOMENT_PER_MIN: int = 10
RELAY_PER_NEIGHBOUR_PER_MIN: int = 3 * RELAY_SOURCE_PER_MIN
RELAY_PER_MOMENT_PER_MIN: int = 3 * RELAY_SOURCE_PER_MOMENT_PER_MIN
#: The origin takes this many reactions a minute per reactor household —
#: charged only for a reaction that passed every check and changed
#: something (#1178 F2).
ORIGIN_PER_REACTOR_PER_MIN: int = 60
#: …and, per neighbour handing them over, opens at most this many seals a
#: minute (the crypto backstop; each costs about what the neighbour's own
#: envelope already did) and takes at most this many that fail a minute —
#: a bucket of their own, so junk behind a relay (≤ the mixed rate from any
#: one household) never uses up the room of the relay's honest reactions
#: (#1178 F14).
ORIGIN_PER_NEIGHBOUR_PER_MIN: int = 1200
ORIGIN_FAILURES_PER_NEIGHBOUR_PER_MIN: int = 600
#: A sealed reaction the origin already took — the expected second copy
#: (:data:`MAX_ROUTES`) or a relay's replay — is turned away on its digest
#: before any crypto and charged here, never to the seal backstop above, so
#: replays of one genuine seal can't use up a neighbour's room for honest
#: reactions (#1197 review). Over it the copies are still dropped, only
#: logged at WARNING.
ORIGIN_REPLAYS_PER_NEIGHBOUR_PER_MIN: int = 600
#: v_68 — ``moment_reaction`` frames opened a minute per GFS connection,
#: before any crypto (each reactor household is then held to
#: :data:`ORIGIN_PER_REACTOR_PER_MIN` after every check). A GFS already
#: holds each follower household to 30 a minute per author.
ORIGIN_PER_GFS_PER_MIN: int = 600
_WINDOW_S: int = 60

#: Where a reaction goes: back along the moment's route (v_67), or through
#: the GFS it came on (v_68).
ReactionTarget = MomentReactionRoute | MomentGfsReactionTarget

#: The origin's reason for a second copy of a reaction it already took —
#: expected since a reaction travels up to :data:`MAX_ROUTES` paths, so it is
#: neither a WARNING nor charged as a failure.
_ALREADY_TAKEN = "replayed reaction"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _epoch_s(iso: str) -> int | None:
    try:
        when = datetime.fromisoformat(iso)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return int(when.timestamp())


def _seal_digest(sealed: dict[str, str]) -> bytes:
    """The replay digest of a sealed reaction — over its ciphertext, so it
    is known before the seal is opened."""
    return hashlib.sha256(sealed["ciphertext"].encode()).digest()


def _reacted_at(ms: int) -> str:
    """``occurred_at_ms`` in the ``moment_reactions.reacted_at`` shape, to
    the millisecond (``YYYY-MM-DD HH:MM:SS.mmm`` UTC) so two changes in one
    second keep their order (#1178 F3)."""
    when = datetime.fromtimestamp(ms // 1000, UTC).replace(tzinfo=None)
    return f"{when:%Y-%m-%d %H:%M:%S}.{ms % 1000:03d}"


class MomentReactionRouter(ProtectionGateMixin):
    """Carry moment reactions back to an author two or three hops away."""

    __slots__ = (
        "_ban_repo",
        "_bus",
        "_child_protection",
        "_clock_ms",
        "_debounce_s",
        "_federation",
        "_federation_repo",
        "_gfs",
        "_keywrap_priv",
        "_keywrap_pub",
        "_keywrap_sig",
        "_limiter",
        "_moments",
        "_pending",
        "_rng",
        "_seen",
        "_tasks",
        "_token_key",
        "_users",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
        federation_repo: AbstractFederationRepo,
        moment_repo: AbstractMomentRepo,
        user_repo: AbstractUserRepo,
        ban_repo: AbstractHouseholdInstanceBanRepo,
        debounce_s: float = DEBOUNCE_S,
        clock_ms: Callable[[], int] = _now_ms,
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._moments = moment_repo
        self._users = user_repo
        self._ban_repo = ban_repo
        self._child_protection = None
        #: v_68 — the GFS transport. Unwired: GFS moments' reactions stay.
        self._gfs: MomentReactionGfs | None = None
        self._debounce_s = debounce_s
        self._clock_ms = clock_ms
        self._limiter = RateLimiter(monotonic=monotonic)
        #: Picks which kept routes a reaction takes (:data:`MAX_ROUTES`).
        self._rng = rng if rng is not None else random.SystemRandom()
        self._keywrap_priv: bytes | None = None
        self._keywrap_pub: bytes | None = None
        self._keywrap_sig = ""
        self._token_key: bytes | None = None
        #: (moment id, reactor) → (emoji or None, occurred_at_ms): the latest
        #: change waiting for its debounce to end.
        self._pending: dict[tuple[str, str], tuple[str | None, int]] = {}
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        #: sha256 of every sealed reaction taken at the origin → when (ms).
        self._seen: OrderedDict[bytes, int] = OrderedDict()

    # ── Wiring ─────────────────────────────────────────────────────────

    def attach_identity(
        self,
        *,
        keywrap_private_key: bytes,
        keywrap_public_key: bytes,
        keywrap_sig: str,
        routing_secret: str,
    ) -> None:
        """This household's static key-wrap keypair (+ binding signature)
        and its ``routing_secret`` — the existing ``instance_identity``
        material; nothing new is minted. Until attached, nothing is routed."""
        self._keywrap_priv = keywrap_private_key
        self._keywrap_pub = keywrap_public_key
        self._keywrap_sig = keywrap_sig
        self._token_key = return_token_key(routing_secret)

    def attach_gfs(self, gfs: MomentReactionGfs) -> None:
        """v_68 — carry reactions to GFS moments through their GFS."""
        self._gfs = gfs

    def attach_to(self, federation_service: FederationService) -> None:
        """Register the ``MOMENT_REACTION_ROUTED`` inbound handler."""
        federation_service._event_registry.register(
            FET.MOMENT_REACTION_ROUTED, self._on_routed
        )

    async def stop(self) -> None:
        """Send what is still waiting for its debounce, then stop."""
        for key in list(self._tasks):
            task = self._tasks.pop(key)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await self._flush(key)

    # ── Creates ────────────────────────────────────────────────────────

    def origin_keywrap_fields(self) -> dict[str, str]:
        """The ``origin_keywrap_*`` fields for a create this household
        authors (``{}`` before the identity is attached)."""
        if self._keywrap_pub is None or not self._keywrap_sig:
            return {}
        return origin_keywrap_fields(self._keywrap_pub, self._keywrap_sig)

    def stamp_relayed_create(
        self,
        payload: dict[str, Any],
        *,
        received_hop: int,
        from_instance: str,
        to_instance: str,
    ) -> dict[str, Any]:
        """The create *payload* (already at the next hop) as sent to
        *to_instance*: with this relay's return token appended — opening to
        *from_instance* — when the origin shipped its key-wrap key and every
        relay so far added one; otherwise without any tokens (a reactor
        could not use a broken chain)."""
        out = dict(payload)
        if self._token_key is None or not payload.get("origin_keywrap_pk"):
            out.pop(RETURN_TOKENS_FIELD, None)
            out.pop(RETURN_TOKEN_SUITE_FIELD, None)
            return out
        try:
            tokens = return_tokens_of(payload)
        except UnsupportedReturnTokenSuite:
            tokens = None
        expires = _epoch_s(
            clamp_moment_expiry(
                str(payload.get("occurred_at") or ""),
                str(payload.get("expires_at") or ""),
            )
        )
        moment_id = str(payload.get("moment_id") or "")
        if tokens is None or len(tokens) != received_hop - 1 or expires is None:
            out.pop(RETURN_TOKENS_FIELD, None)
            out.pop(RETURN_TOKEN_SUITE_FIELD, None)
            return out
        out[RETURN_TOKENS_FIELD] = [
            *tokens,
            mint_return_token(
                key=self._token_key,
                moment_id=moment_id,
                next_hop=to_instance,
                prev_hop=from_instance,
                expires_at_s=expires,
            ),
        ]
        out[RETURN_TOKEN_SUITE_FIELD] = RETURN_TOKEN_SUITE_AESGCM_256
        return out

    async def route_from_create(
        self,
        payload: dict[str, Any],
        *,
        from_instance: str,
        origin_instance_id: str,
        hop_count: int,
    ) -> MomentReactionRoute | None:
        """The complete reaction route a received create carries, or
        ``None``: a relayed copy (hop ≥ 2) whose origin key-wrap key is bound
        to the origin's identity key, with exactly one return token per
        relay under a known suite."""
        if hop_count < 2 or hop_count > MOMENT_MAX_HOPS:
            return None
        if from_instance == origin_instance_id:
            return None
        pinned = await self._federation.peer_identity_public_key(origin_instance_id)
        keywrap = verified_origin_keywrap(
            payload,
            origin_instance_id=origin_instance_id,
            pinned_identity_pk=pinned,
        )
        if keywrap is None:
            if payload.get("origin_keywrap_pk"):
                log.warning(
                    "MOMENT_CREATED from %s: origin key-wrap key for %s does not"
                    " verify — no reaction route kept (moment=%s)",
                    from_instance,
                    origin_instance_id,
                    payload.get("moment_id"),
                )
            return None
        try:
            tokens = return_tokens_of(payload)
        except UnsupportedReturnTokenSuite as exc:
            log.warning(
                "MOMENT_CREATED from %s: %s — no reaction route kept",
                from_instance,
                exc,
            )
            return None
        if tokens is None or len(tokens) != hop_count - 1:
            return None
        return MomentReactionRoute(
            keywrap_pk=keywrap.hex(),
            via=from_instance,
            tokens=tuple(tokens),
            token_suite=str(payload.get(RETURN_TOKEN_SUITE_FIELD)),
        )

    async def keep_route_from_create(
        self,
        moment_id: str,
        payload: dict[str, Any],
        *,
        from_instance: str,
        origin_instance_id: str,
        hop_count: int,
    ) -> bool:
        """Keep the complete route a received copy of a stored moment
        carries, beside up to :data:`MAX_KEPT_ROUTES` − 1 others from
        distinct neighbours. First come, first kept: a copy claiming fewer
        hops never displaces a usable route — ``hop_count`` is outside the
        origin signature, so a neighbour could claim one to capture the
        route and drop reactions (#1178 F13). A kept route whose neighbour
        is no longer a confirmed v_67 peer can't carry anything, so it gives
        its slot to this copy (#1197 review). Returns whether it was kept."""
        route = await self.route_from_create(
            payload,
            from_instance=from_instance,
            origin_instance_id=origin_instance_id,
            hop_count=hop_count,
        )
        if route is None:
            return False
        kept = (await self._moments.get_reaction_routes([moment_id])).get(moment_id, ())
        stale = {
            r.via
            for r in kept
            if r.via != from_instance and not await self._routable_via(r.via)
        }
        return await self._moments.add_reaction_route(
            moment_id, route, keep=MAX_KEPT_ROUTES, replace=stale
        )

    # ── Reactor side ───────────────────────────────────────────────────

    async def direct_target(
        self, author_user_id: str, *, moment_id: str | None = None
    ) -> str | None:
        """The author's household when a reaction reaches it directly — this
        household or a confirmed peer (the pre-v_67 path) — else ``None``.

        With *moment_id*, a moment whose author is not on our roster (it came
        in at hop 2, or the roster lags) still goes direct when its origin is
        a confirmed peer: the routed path is for unpaired origins only, so the
        per-pair hide list always applies to a paired origin (#1178 F1)."""
        origin: str | None = None
        if moment_id is not None:
            moment = await self._moments.get(moment_id)
            origin = moment.origin_instance_id if moment is not None else None
        return await self._direct(author_user_id, origin)

    async def _direct(self, author_user_id: str, origin: str | None) -> str | None:
        try:
            home = await self._users.get_instance_for_user(author_user_id)
        except Exception:  # pragma: no cover — defensive
            log.debug("moment-reaction-router: home lookup failed", exc_info=True)
            home = None
        if home == self._federation.own_instance_id:
            return home
        for candidate in dict.fromkeys(h for h in (home, origin) if h):
            if await self._is_confirmed(candidate):
                return candidate
        return None

    async def _is_confirmed(self, instance_id: str) -> bool:
        peer = await self._federation_repo.get_instance(instance_id)
        return peer is not None and peer.status is PairingStatus.CONFIRMED

    async def _routable_via(self, peer_id: str) -> bool:
        """A confirmed social peer that ``peer_supports`` v_67 — one that
        opens return tokens."""
        peer = await self._federation_repo.get_instance(peer_id)
        if (
            peer is None
            or peer.status is not PairingStatus.CONFIRMED
            or peer.source is InstanceSource.SPACE_SESSION
        ):
            return False
        return await self._federation.peer_supports(
            peer_id,
            min_version=FederationCapability.MIN_FOR_ROUTED_MOMENT_REACTIONS,
        )

    def _route_usable(self, moment: Moment, route: MomentReactionRoute) -> bool:
        if route.token_suite not in SUPPORTED_RETURN_TOKEN_SUITES:
            return False
        if not 1 <= len(route.tokens) <= MOMENT_MAX_HOPS - 1:
            return False
        try:
            if len(bytes.fromhex(route.keywrap_pk)) != 32:
                return False
        except ValueError:
            return False
        return self._live(moment)

    def _live(self, moment: Moment) -> bool:
        expires = _epoch_s(moment.expires_at)
        return expires is not None and expires * 1000 > self._clock_ms()

    async def reach_for(self, moments: Iterable[Moment]) -> dict[str, str]:
        """``moment id → reactions_reach`` for the API: ``author`` when a
        reaction reaches the author's household (here, a paired household,
        or v_67 along the moment's route), ``household`` when it stays here."""
        items = list(moments)
        routes = await self._moments.get_reaction_routes(
            [m.id for m in items if m.received_via == "household" and m.hop_count > 1]
        )
        direct: dict[tuple[str, str], bool] = {}
        via_ok: dict[str, bool] = {}
        gfs_ok: dict[tuple[str, str | None], bool] = {}
        out: dict[str, str] = {}
        for m in items:
            if m.received_via == "self":
                out[m.id] = REACTION_REACH_AUTHOR
                continue
            who = (m.author_user_id, m.origin_instance_id)
            if who not in direct:
                direct[who] = await self._direct(*who) is not None
            reach = REACTION_REACH_HOUSEHOLD
            kept = routes.get(m.id, ())
            if direct[who]:
                reach = REACTION_REACH_AUTHOR
            elif kept:
                for route in kept:
                    if not self._route_usable(m, route):
                        continue
                    if route.via not in via_ok:
                        via_ok[route.via] = await self._routable_via(route.via)
                    if via_ok[route.via]:
                        reach = REACTION_REACH_AUTHOR
                        break
            elif self._gfs is not None and m.received_via == "gfs" and self._live(m):
                key = (m.author_user_id, m.received_via_gfs_id)
                if key not in gfs_ok:
                    gfs_ok[key] = await self._gfs.target(m) is not None
                if gfs_ok[key]:
                    reach = REACTION_REACH_AUTHOR
            out[m.id] = reach
        return out

    async def queue_reaction(
        self,
        *,
        moment_id: str,
        reactor_user_id: str,
        emoji: str | None,
        occurred_at: datetime,
    ) -> bool:
        """Queue a local reaction change for the routed path (or, v_68, the
        GFS the moment came through). Returns whether it will be sent —
        ``False`` leaves it in this household (no complete route, the
        neighbour is older than v_67, or the GFS / author can't take it)."""
        if self._keywrap_priv is None:
            return False
        if await self._usable_target(moment_id) is None:
            log.debug(
                "moment %s: no route to its author — reaction stays here", moment_id
            )
            return False
        key = (moment_id, reactor_user_id)
        self._pending[key] = (emoji, int(occurred_at.timestamp() * 1000))
        if key not in self._tasks:
            self._tasks[key] = asyncio.create_task(self._flush_later(key))
        return True

    async def _usable_target(
        self, moment_id: str
    ) -> tuple[Moment, list[ReactionTarget]] | None:
        """The moment and where a reaction to it can go — its GFS, or every
        usable kept route through distinct v_67 neighbours (a route whose
        neighbour unpaired or is older is skipped) — or ``None`` when it
        stays here."""
        moment = await self._moments.get(moment_id)
        if moment is None or moment.received_via not in ("household", "gfs"):
            return None
        if await self._is_confirmed(moment.origin_instance_id):
            # A paired origin is reached directly, under the hide list —
            # never routed past it (#1178 F1), nor through a GFS.
            return None
        if moment.received_via == "gfs":
            if self._gfs is None or not self._live(moment):
                return None
            target = await self._gfs.target(moment)
            return None if target is None else (moment, [target])
        kept = (await self._moments.get_reaction_routes([moment_id])).get(moment_id, ())
        usable: list[ReactionTarget] = []
        vias: set[str] = set()
        for route in kept:
            if (
                route.via not in vias
                and self._route_usable(moment, route)
                and await self._routable_via(route.via)
            ):
                usable.append(route)
                vias.add(route.via)
        return (moment, usable) if usable else None

    async def _flush_later(self, key: tuple[str, str]) -> None:
        try:
            await asyncio.sleep(self._debounce_s)
        except asyncio.CancelledError:
            return
        self._tasks.pop(key, None)
        await self._flush(key)

    async def _flush(self, key: tuple[str, str]) -> None:
        state = self._pending.pop(key, None)
        if state is None:
            return
        moment_id, reactor_user_id = key
        emoji, occurred_at_ms = state
        try:
            await self._send_routed(moment_id, reactor_user_id, emoji, occurred_at_ms)
        except Exception:  # pragma: no cover — defensive, never kill the task
            log.warning(
                "moment %s: routed reaction failed to send", moment_id, exc_info=True
            )

    async def _send_routed(
        self,
        moment_id: str,
        reactor_user_id: str,
        emoji: str | None,
        occurred_at_ms: int,
    ) -> None:
        found = await self._usable_target(moment_id)
        if found is None:
            return
        moment, targets = found
        if len(targets) > MAX_ROUTES:
            # A fresh pick per reaction: a relay that drops and sits on some
            # of the kept paths misses the reactions sent around it.
            targets = self._rng.sample(targets, MAX_ROUTES)
        first = targets[0]
        if (
            isinstance(first, MomentGfsReactionTarget)
            and first.keywrap_suite not in SUPPORTED_KEM_SUITES
        ):
            # Sealed only under the suite the author named its key under,
            # and only one this build implements — never an assumed X25519.
            log.warning(
                "moment %s: the author's key names an unsupported key-wrap "
                "suite %r — reaction not sent",
                moment_id,
                first.keywrap_suite,
            )
            return
        user = await self._users.get_by_user_id(reactor_user_id)
        if user is None:
            return
        own_pk = self._federation.own_identity_pk
        anchor = user.identity_anchor or user.username
        if derive_user_id(own_pk, anchor) != reactor_user_id:
            log.warning(
                "moment %s: %s does not derive from this household's key — "
                "reaction not routed",
                moment_id,
                reactor_user_id,
            )
            return
        inner = build_reaction(
            seed=self._federation.own_identity_seed,
            identity_pk=own_pk,
            reactor_instance_id=self._federation.own_instance_id,
            reactor_user_id=reactor_user_id,
            reactor_anchor=anchor,
            moment_id=moment_id,
            author_user_id=moment.author_user_id,
            origin_instance_id=moment.origin_instance_id,
            emoji=emoji,
            occurred_at_ms=occurred_at_ms,
        )
        # One seal for every route: the origin dedupes the copies by its
        # replay digest (#1178 F13). Every kept route names the same origin
        # key — each was bound to the origin's identity key on receipt.
        sealed = seal_reaction(
            origin_keywrap_pub=bytes.fromhex(first.keywrap_pk), inner=inner
        )
        if isinstance(first, MomentGfsReactionTarget):
            assert self._gfs is not None
            await self._gfs.send(
                first,
                author_user_id=moment.author_user_id,
                sealed=sealed,
                retry_key=f"{moment_id}|{reactor_user_id}",
                occurred_at_s=occurred_at_ms / 1000,
            )
            return
        for route in targets:
            assert isinstance(route, MomentReactionRoute)
            await self._federation.send_event(
                to_instance_id=route.via,
                event_type=FET.MOMENT_REACTION_ROUTED,
                payload={
                    "moment_id": moment_id,
                    "origin_instance_id": moment.origin_instance_id,
                    RETURN_TOKENS_FIELD: list(route.tokens),
                    RETURN_TOKEN_SUITE_FIELD: route.token_suite,
                    "sealed": sealed,
                },
                # Latest state per (peer, moment, person): a newer reaction
                # replaces one still queued for an offline neighbour. Local
                # only.
                outbox_key=f"moment-reaction:{moment_id}:{reactor_user_id}",
            )

    # ── Inbound: relay or origin ───────────────────────────────────────

    async def _on_routed(self, event: FederationEvent) -> None:
        p = event.payload
        moment_id = p.get("moment_id")
        origin = p.get("origin_instance_id")
        if not (
            isinstance(moment_id, str)
            and 0 < len(moment_id) <= MAX_ID_CHARS
            and isinstance(origin, str)
            and 0 < len(origin) <= MAX_ID_CHARS
            and sealed_shape_ok(p.get("sealed"))
        ):
            self._refuse(event, "malformed routed reaction")
            return
        try:
            tokens = return_tokens_of(p)
        except UnsupportedReturnTokenSuite:
            self._refuse(event, "unsupported rt_suite")
            return
        if tokens is None:
            self._refuse(event, "malformed return tokens")
            return
        self._limiter.prune(window_s=_WINDOW_S)
        neighbour = event.from_instance
        if origin == self._federation.own_instance_id:
            if tokens:
                self._refuse(event, "return tokens left at the origin")
                return
            # Before any crypto: the neighbour's failures so far, and the
            # backstop on seals opened (#1178 F14). Its accepted reactions
            # never use up the failures' room.
            if self._was_seen(_seal_digest(p["sealed"]), self._clock_ms()):
                # Already taken: the reaction's other route (#1178 F13) or a
                # replay. Turned away before any crypto, charged to replays
                # only — never to the room honest reactions have (#1197).
                if self._limiter.is_allowed(
                    f"p:{neighbour}",
                    limit=ORIGIN_REPLAYS_PER_NEIGHBOUR_PER_MIN,
                    window_s=_WINDOW_S,
                ):
                    log.debug(
                        "%s from %s: already taken (moment=%s)",
                        event.event_type,
                        neighbour,
                        moment_id,
                    )
                else:
                    self._refuse(event, "neighbour over its replay rate")
                return
            if not self._limiter.has_room(
                f"f:{neighbour}",
                limit=ORIGIN_FAILURES_PER_NEIGHBOUR_PER_MIN,
                window_s=_WINDOW_S,
            ) or not self._limiter.is_allowed(
                f"n:{neighbour}",
                limit=ORIGIN_PER_NEIGHBOUR_PER_MIN,
                window_s=_WINDOW_S,
            ):
                self._refuse(event, "neighbour over its routed-reaction rate")
                return
            await self._accept_at_origin(event, moment_id, p["sealed"])
            return
        # One budget per neighbour; two tokens (the neighbour's own
        # reactions) also hold to the source rate inside it (#1178 F14,
        # #1197). Checked in full before anything is charged.
        buckets = [
            (f"rn:{neighbour}", RELAY_PER_NEIGHBOUR_PER_MIN, "routed-reaction"),
            (f"rm:{neighbour}:{moment_id}", RELAY_PER_MOMENT_PER_MIN, "per-moment"),
        ]
        if len(tokens) >= 2:
            buckets += [
                (f"rs:{neighbour}", RELAY_SOURCE_PER_MIN, "routed-reaction"),
                (
                    f"rsm:{neighbour}:{moment_id}",
                    RELAY_SOURCE_PER_MOMENT_PER_MIN,
                    "per-moment",
                ),
            ]
        for key, limit, what in buckets:
            if not self._limiter.has_room(key, limit=limit, window_s=_WINDOW_S):
                self._refuse(event, f"neighbour over its {what} rate")
                return
        for key, limit, _what in buckets:
            self._limiter.is_allowed(key, limit=limit, window_s=_WINDOW_S)
        await self._forward(event, moment_id, origin, tokens)

    async def _forward(
        self,
        event: FederationEvent,
        moment_id: str,
        origin: str,
        tokens: list[str],
    ) -> None:
        if self._token_key is None or not tokens:
            self._refuse(event, "no return token for this household")
            return
        try:
            prev = open_return_token(
                key=self._token_key,
                token=tokens[-1],
                moment_id=moment_id,
                from_instance=event.from_instance,
                now_s=self._clock_ms() // 1000,
            )
        except ReturnTokenRefused as exc:
            self._refuse(event, str(exc))
            return
        remaining = tokens[:-1]
        if (prev == origin) != (not remaining):
            self._refuse(event, "return tokens do not lead to the origin")
            return
        if prev == event.from_instance or not await self._routable_via(prev):
            self._refuse(event, "previous hop is not a v_67 paired household")
            return
        await self._federation.send_event(
            to_instance_id=prev,
            event_type=FET.MOMENT_REACTION_ROUTED,
            payload={
                "moment_id": moment_id,
                "origin_instance_id": origin,
                RETURN_TOKENS_FIELD: remaining,
                RETURN_TOKEN_SUITE_FIELD: RETURN_TOKEN_SUITE_AESGCM_256,
                "sealed": event.payload["sealed"],
            },
        )

    async def _accept_at_origin(
        self,
        event: FederationEvent,
        moment_id: str,
        sealed: dict[str, str],
    ) -> None:
        inner, reason = await self._open_checked(sealed, moment_id=moment_id)
        if reason == _ALREADY_TAKEN:
            # Two copies opened at once — the digest check in ``_on_routed``
            # turns later ones away before any crypto. Already applied.
            log.debug(
                "%s from %s: already taken (moment=%s)",
                event.event_type,
                event.from_instance,
                moment_id,
            )
            return
        if reason is not None:
            # Charged to the neighbour's failures, never to the room its
            # accepted reactions have (#1178 F14).
            self._limiter.is_allowed(
                f"f:{event.from_instance}",
                limit=ORIGIN_FAILURES_PER_NEIGHBOUR_PER_MIN,
                window_s=_WINDOW_S,
            )
            self._refuse(
                event, reason, reactor=str((inner or {}).get("reactor_user_id") or "")
            )
            return
        assert inner is not None
        # A routed reaction came over the mesh: its reactor's household is
        # not paired with ours (§CP.R reads this on the event).
        await self._apply(inner, received_via="household", hop_count=2)

    async def on_gfs_reaction(self, frame: dict[str, Any], *, gfs_id: str) -> None:
        """A ``moment_reaction`` frame (v_68): a follower household's sealed
        reaction to one of our public moments, relayed by the GFS *gfs_id*.

        ``from_instance`` is the household the GFS authenticated. Opened and
        checked exactly like a routed reaction at its origin, plus: the
        reactor's household is that household (a follower can't pass off
        another household's reaction), and the moment is public — only a
        public moment ever crossed a GFS. The GFS learns nothing back."""
        sender = frame.get("from_instance")
        sealed = frame.get("sealed")
        if not (
            isinstance(sender, str)
            and 0 < len(sender) <= MAX_ID_CHARS
            and sealed_shape_ok(sealed)
        ):
            self._refuse_gfs(gfs_id, sender, "malformed moment_reaction frame")
            return
        assert isinstance(sealed, dict)
        self._limiter.prune(window_s=_WINDOW_S)
        if not self._limiter.is_allowed(
            f"g:{gfs_id}", limit=ORIGIN_PER_GFS_PER_MIN, window_s=_WINDOW_S
        ):
            self._refuse_gfs(gfs_id, sender, "GFS over its reaction rate")
            return
        inner, reason = await self._open_checked(sealed, gfs_sender=sender)
        if reason is not None:
            self._refuse_gfs(
                gfs_id,
                sender,
                reason,
                reactor=str((inner or {}).get("reactor_user_id") or ""),
            )
            return
        assert inner is not None
        await self._apply(inner, received_via="gfs")

    async def _open_checked(
        self,
        sealed: dict[str, str],
        *,
        moment_id: str | None = None,
        gfs_sender: str | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Open *sealed* with our key-wrap key and run every origin check:
        ``(inner, None)`` when it may be applied, else ``(inner or None,
        why not)``. *moment_id* is the one a routed envelope names;
        *gfs_sender* the household a GFS authenticated (v_68)."""
        if self._keywrap_priv is None:
            return None, "no key-wrap key attached"
        try:
            inner = open_reaction(sealed=sealed, keywrap_priv=self._keywrap_priv)
        except UnsupportedKemSuite:
            return None, "unsupported kem_suite"
        except (InvalidTag, ValueError) as exc:
            return None, (
                f"sealed reaction does not open here ({exc or type(exc).__name__})"
            )
        try:
            reason = verify_reaction(inner)
        except UnsupportedReactionSigSuite:
            reason = "unsupported reaction_sig_suite"
        if reason is None:
            reason = await self._origin_refusal(
                inner,
                str(inner["moment_id"]) if moment_id is None else moment_id,
                sealed,
                gfs_sender=gfs_sender,
            )
        return inner, reason

    async def _origin_refusal(
        self,
        inner: dict[str, Any],
        moment_id: str,
        sealed: dict[str, str],
        *,
        gfs_sender: str | None = None,
    ) -> str | None:
        """Why the origin refuses a well-signed reaction, or ``None``."""
        own = self._federation.own_instance_id
        reactor = str(inner["reactor_user_id"])
        reactor_home = str(inner["reactor_instance_id"])
        if inner["moment_id"] != moment_id or inner["origin_instance_id"] != own:
            return "sealed reaction names another moment or origin"
        if gfs_sender is not None and reactor_home != gfs_sender:
            return "the GFS sender is not the reactor's household"
        if reactor_home == own or await self._users.get_by_user_id(reactor) is not None:
            return "reactor claims to be from this household"
        pinned = await self._federation.peer_identity_public_key(reactor_home)
        if pinned is not None and pinned.hex() != inner["reactor_identity_pk"]:
            return "reactor key differs from the key pinned for its household"
        now = self._clock_ms()
        occurred = int(inner["occurred_at_ms"])
        if (
            occurred < now - MAX_REACTION_AGE_MS
            or occurred > now + MAX_REACTION_SKEW_MS
        ):
            return "reaction too old or in the future"
        digest = _seal_digest(sealed)
        if self._was_seen(digest, now):
            return _ALREADY_TAKEN
        moment = await self._moments.get(moment_id)
        if moment is None or moment.origin_instance_id != own:
            return "moment is not live here"
        if moment.author_user_id != inner["author_user_id"]:
            return "sealed reaction names another author"
        if await self._users.get_by_user_id(moment.author_user_id) is None:
            return "moment is not authored here"
        if gfs_sender is not None and not moment.is_public:
            # Only a public moment ever went to a GFS.
            return "moment is not public"
        expires = _epoch_s(moment.expires_at)
        if expires is None or expires * 1000 <= now:
            return "moment has ended"
        if await self._ban_repo.is_banned(reactor_home):
            return "reactor household is banned"
        if await self._users.is_blocked(moment.author_user_id, reactor):
            return "author blocked the reactor"
        if await self._guardian_blocked(
            moment.author_user_id, reactor
        ) or await self._guardian_blocks_household(moment.author_user_id, reactor_home):
            return "a guardian block separates them"
        if await self._is_protected(moment.author_user_id):
            return "moment is for paired households only (no_relay)"
        # The reactor household's budget is only looked at here; it is
        # charged in ``_apply``, and only for a reaction that changed
        # something. A replay — refused above while the replay cache holds
        # it, and after a restart (the cache is in memory) a no-op against
        # the stored reaction and removal — must not starve that household's
        # real ones (#1178 F2, #1179 F3). The per-neighbour / per-GFS limits
        # ran before any crypto and stay the flood backstop.
        if not self._limiter.has_room(
            f"r:{reactor_home}",
            limit=ORIGIN_PER_REACTOR_PER_MIN,
            window_s=_WINDOW_S,
        ):
            return "reactor household over its rate"
        self._remember_seen(digest, now)
        return None

    async def _apply(
        self,
        inner: dict[str, Any],
        *,
        received_via: str,
        hop_count: int = 1,
    ) -> None:
        moment_id = str(inner["moment_id"])
        reactor = str(inner["reactor_user_id"])
        emoji = inner["emoji"]
        occurred = int(inner["occurred_at_ms"])
        # Ordered by when it was made against the stored reaction *and* the
        # stored removal (both on disk), so neither a late add nor a replay
        # after a restart undoes a newer change (#1178 F3).
        changed = await self._moments.apply_reaction_at(
            moment_id, reactor, emoji, reacted_at=_reacted_at(occurred)
        )
        if not changed:
            return
        self._limiter.is_allowed(
            f"r:{inner['reactor_instance_id']}",
            limit=ORIGIN_PER_REACTOR_PER_MIN,
            window_s=_WINDOW_S,
        )
        moment = await self._moments.get(moment_id)
        if moment is None:  # pragma: no cover — deleted in between
            return
        await self._bus.publish(
            MomentReactionChanged(
                moment_id=moment_id,
                reactor_user_id=reactor,
                author_user_id=moment.author_user_id,
                emoji=emoji,
                received_via=received_via,
                hop_count=hop_count,
            )
        )

    # ── Bounded in-memory caches ───────────────────────────────────────

    def _was_seen(self, digest: bytes, now_ms: int) -> bool:
        """Whether *digest* was taken inside the replay window (expired and
        overflowing entries are dropped first)."""
        while self._seen:
            oldest, at = next(iter(self._seen.items()))
            if at >= now_ms - MAX_REACTION_AGE_MS and len(self._seen) < SEEN_MAX:
                break
            del self._seen[oldest]
        return digest in self._seen

    def _remember_seen(self, digest: bytes, now_ms: int) -> bool:
        """Record *digest* as taken; ``False`` when it already was."""
        if self._was_seen(digest, now_ms):
            return False
        self._seen[digest] = now_ms
        return True

    @staticmethod
    def _refuse_gfs(
        gfs_id: str, sender: object, reason: str, *, reactor: str = ""
    ) -> None:
        log.warning(
            "moment_reaction via GFS %s from %s: %s — refusing%s",
            gfs_id,
            sender,
            reason,
            f" (reactor={reactor})" if reactor else "",
        )

    @staticmethod
    def _refuse(event: FederationEvent, reason: str, *, reactor: str = "") -> None:
        log.warning(
            "%s from %s: %s — refusing (moment=%s%s)",
            event.event_type,
            event.from_instance,
            reason,
            event.payload.get("moment_id"),
            f" reactor={reactor}" if reactor else "",
        )


__all__ = [
    "DEBOUNCE_S",
    "MAX_KEPT_ROUTES",
    "MAX_REACTION_AGE_MS",
    "MAX_ROUTES",
    "ORIGIN_FAILURES_PER_NEIGHBOUR_PER_MIN",
    "ORIGIN_PER_GFS_PER_MIN",
    "ORIGIN_PER_NEIGHBOUR_PER_MIN",
    "ORIGIN_PER_REACTOR_PER_MIN",
    "ORIGIN_REPLAYS_PER_NEIGHBOUR_PER_MIN",
    "RELAY_PER_MOMENT_PER_MIN",
    "RELAY_PER_NEIGHBOUR_PER_MIN",
    "RELAY_SOURCE_PER_MIN",
    "RELAY_SOURCE_PER_MOMENT_PER_MIN",
    "MomentReactionRouter",
]
