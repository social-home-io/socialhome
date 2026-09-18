"""Cross-instance redeem of a space invite token (§D2).

When a household pastes an invite code minted on a different household,
the SPA POSTs ``/api/spaces/join`` with ``{token, issuer_instance_id}``.
The receiving instance can't validate the token locally — the token row
lives in the *issuer's* ``space_invite_tokens`` table. This coordinator
runs the cross-instance handshake:

1. **Receiver → issuer:** :data:`SPACE_INVITE_TOKEN_REDEEM`
   carrying the token + the redeemer's identity (``user_id``,
   ``public_key``, ``display_name``) + a ``redeem_nonce`` that keys the
   in-flight Future on the receiver side.
2. **Issuer:** atomically consumes the token, seats the redeemer as a
   :class:`SpaceRemoteMember`, registers the receiver's
   ``space_instance``, and ships :data:`SPACE_INVITE_TOKEN_REDEEM_ACK`
   back with ``{redeem_nonce, space_id, role}``.
3. **Receiver:** resolves the Future on the ACK — the route handler
   wakes from its ``await``, persists ``add_space_instance(space_id,
   issuer_instance_id)`` locally, and returns ``{space_id, role}`` to
   the SPA.

On any issuer-side failure (token unknown / expired / exhausted, ban,
crypto error), the issuer sends
:data:`SPACE_INVITE_TOKEN_REDEEM_DENY` with a human-readable
``reason``; the receiver's Future raises
:class:`SpacePermissionError` carrying that reason. On no response
within ``REDEEM_TIMEOUT_SECONDS``, the receiver's Future raises
``TimeoutError`` and the route maps that to HTTP 504.

PR 2 layers federation-mesh routing on top: when the issuer is *not*
a direct CONFIRMED peer, the coordinator runs route discovery (via
:class:`RouteDiscoveryService`) and ships the REDEEM wrapped in
:data:`FederationEventType.SPACE_ROUTED` along the discovered chain.
The issuer's ACK / DENY follows the reverse path automatically —
``_on_redeem`` reads ``event.routed_path`` and ships the response
through the same :class:`SpaceRoutedHandler` when it was set.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import orjson

from ..domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from ..domain.federation_capabilities import OURS, FederationCapability
from ..domain.space import SpaceMember, SpacePermissionError, SpaceRole
from ..services.space_service import (
    _coerce_min_age,
    apply_space_content_key_from_metadata,
    apply_space_cover_from_metadata,
    apply_space_icon_from_metadata,
    build_space_snapshot_for_federation,
    can_seat_remote_stub,
    stub_space_from_metadata,
)
from .gfs_relay_transport import is_relay_envelope_body
from .inbound_validator import TRANSPORT_GFS_RELAY
from .invite_bootstrap import (
    KIND_REDEEM,
    KIND_REDEEM_ACK,
    KIND_REDEEM_DENY,
    InviteBootstrapHint,
    derive_space_session_keys,
    seal_bootstrap_envelope,
    unseal_envelope_body,
    validate_bootstrap_body,
    verify_peer_keywrap,
)

if TYPE_CHECKING:
    from ..infrastructure.key_manager import KeyManager
    from ..domain.federation import FederationEvent
    from ..infrastructure.event_bus import EventBus
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.space_cover_repo import AbstractSpaceCoverRepo
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..rate_limiter import RateLimiter
    from ..repositories.user_repo import AbstractUserRepo
    from .federation_service import FederationService
    from .invite_bootstrap import RelayEnvelopeSender
    from .route_discovery import RouteDiscoveryService
    from .routed_envelope import SpaceRoutedHandler

log = logging.getLogger(__name__)


#: How long the receiver waits for an ACK / DENY before giving up.
#: Calibrated for a single hop over a healthy WebRTC DataChannel.
REDEEM_TIMEOUT_SECONDS: float = 10.0

#: The ONE reason string every issuer-side denial ships.
#:
#: A redeem can fail for a dozen reasons — unknown token, expired,
#: exhausted, banned, a storage fault at one of three stages — and the
#: differences are all facts about the ISSUER's private state. Reported
#: separately they let anybody holding a public invite link enumerate
#: them: whether a given user is banned, whether a link is spent, which
#: internal step broke. The issuer keeps the detail in its own log
#: (``log.exception``) and the wire carries this, always.
REDEEM_DENY_REASON: str = "invite redeem denied"

#: What the REDEEMER shows its own user, regardless of what the issuer
#: put in ``reason``. The issuer is a stranger on this leg — a household
#: we met through a public link — so rendering its text verbatim in the
#: SPA hands an unknown party a string in our UI.
BOOTSTRAP_DENY_CLIENT_MESSAGE: str = (
    "that invite link didn't work — ask whoever shared it for a fresh one"
)

#: §D2b inbound throttles. This surface is unauthenticated by
#: construction — anybody who can reach the relay can address a blob at
#: us — so it is both a DoS and a token-guessing surface. Tokens are
#: ``uuid4().hex`` (122 bits), so guessing is not the threat; flooding
#: is. Two buckets on the same :class:`~socialhome.rate_limiter.
#: RateLimiter` the HTTP surface uses:
#:
#: * a **process-wide** bucket checked BEFORE the unseal, so a flood
#:   costs a list append rather than an AES-GCM open;
#: * a **per-family** bucket once the blob is open, because two unrelated
#:   traffic classes ride this one socket (see below);
#: * a **per-sender** bucket checked once the body's identity has been
#:   verified, so one household can't burn the global budget alone.
BOOTSTRAP_INBOUND_LIMIT: int = 600
BOOTSTRAP_INBOUND_WINDOW_S: int = 60
BOOTSTRAP_PER_SENDER_LIMIT: int = 30
BOOTSTRAP_PER_SENDER_WINDOW_S: int = 60

#: Per-family allowances, split out of the single pre-unseal bucket.
#:
#: The relay socket carries two things with wildly different cadences:
#: invite-bootstrap bodies (a handful a day — a redeem is two frames) and
#: full §24.11 envelopes for every household seated from an invite link
#: (every post, comment, roster change, rekey and sync chunk of every such
#: space). Sharing one allowance meant an active space could starve every
#: redeem attempt, and a redeem flood could stall a space's federation.
#: They now draw on separate buckets, both still under the process-wide
#: pre-unseal shed above.
BOOTSTRAP_BODY_INBOUND_LIMIT: int = 60
RELAY_ENVELOPE_INBOUND_LIMIT: int = 600


class SpaceInviteTokenRedeemCoordinator:
    """Coordinator for the cross-instance ``SPACE_INVITE_TOKEN_REDEEM``
    round-trip.

    The same instance plays both roles in a deployment — receiver when a
    local user pastes a peer's token, issuer when a peer redeems one of
    our tokens. The coordinator hosts both inbound handlers and the
    outbound ``request_redeem`` driver.
    """

    __slots__ = (
        "_bus",
        "_federation",
        "_spaces",
        "_remote_members",
        "_users",
        "_federation_repo",
        "_cover_repo",
        "_icon_repo",
        "_space_crypto",
        "_child_protection",
        "_pending",
        "_timeout",
        "_route_service",
        "_routed_handler",
        "_relay_sender",
        "_keywrap_private_key",
        "_keywrap_public_key",
        "_keywrap_sig",
        "_key_manager",
        "_bootstrap_hints",
        "_rate_limiter",
    )

    def __init__(
        self,
        *,
        bus: "EventBus",
        federation_service: "FederationService",
        space_repo: "AbstractSpaceRepo",
        space_remote_member_repo: "AbstractSpaceRemoteMemberRepo",
        user_repo: "AbstractUserRepo",
        federation_repo: "AbstractFederationRepo",
        timeout: float = REDEEM_TIMEOUT_SECONDS,
        route_service: "RouteDiscoveryService | None" = None,
        routed_handler: "SpaceRoutedHandler | None" = None,
        cover_repo: "AbstractSpaceCoverRepo | None" = None,
        icon_repo=None,
        space_crypto_service=None,
        child_protection_service=None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._spaces = space_repo
        self._remote_members = space_remote_member_repo
        self._users = user_repo
        self._federation_repo = federation_repo
        #: Optional — when wired, the issuer ships the host's WebP
        #: cover bytes alongside ``cover_hash`` in the ACK so the
        #: receiver's local stub doesn't fall back to the gradient
        #: placeholder (§D1b #116).
        self._cover_repo = cover_repo
        self._icon_repo = icon_repo
        #: Optional — when wired, the issuer ships the current
        #: epoch's space content key in the ACK and the receiver
        #: imports it into its local space_keys (#117).
        self._space_crypto = space_crypto_service
        #: Optional — when wired, §CP.F1 age-gates the redeemer before
        #: seating them locally, so a protected minor can't redeem an
        #: invite link into a remote-hosted age-restricted space. The
        #: host's ``min_age`` rides the ACK's ``space_meta``.
        self._child_protection = child_protection_service
        #: ``redeem_nonce`` → in-flight Future awaiting the ACK / DENY.
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._timeout = timeout
        #: Mesh-routing pair. Optional so legacy tests that exercise
        #: only the direct-pair path can construct the coordinator
        #: without the routing layer. When both are present, REDEEM
        #: requests against unpaired issuers run discovery + wrap
        #: rather than fail-fast with "pair first".
        self._route_service = route_service
        self._routed_handler = routed_handler
        #: §D2b bootstrap-redeem material, wired by
        #: :meth:`attach_bootstrap`. Absent → the bootstrap path is not
        #: offered and an unreachable issuer still fails with "pair
        #: first" / "no route to issuer".
        self._relay_sender: "RelayEnvelopeSender | None" = None
        self._keywrap_private_key: bytes = b""
        self._keywrap_public_key: bytes = b""
        self._keywrap_sig: str = ""
        self._key_manager: "KeyManager | None" = None
        #: ``redeem_nonce`` → the hint we sealed the request under, so
        #: the reply leg can be pinned to the issuer identity the invite
        #: blob advertised rather than trusting whatever comes back.
        self._bootstrap_hints: dict[str, InviteBootstrapHint] = {}
        self._rate_limiter: "RateLimiter | None" = None

    def attach_bootstrap(
        self,
        *,
        relay_sender: "RelayEnvelopeSender",
        keywrap_private_key: bytes,
        keywrap_public_key: bytes,
        keywrap_sig: str,
        key_manager: "KeyManager",
        rate_limiter: "RateLimiter | None" = None,
    ) -> None:
        """Enable the §D2b bootstrap path.

        ``relay_sender`` is the seam to whatever carries an opaque blob
        to a household we have no address for (production: the GFS
        socket). The key-wrap triple is this household's own published
        static X25519 key, its self-signature, and the private half —
        the redeemer ships the first two inside the sealed request so
        the issuer can seal the reply back, and both sides derive their
        space-scoped session keys from the pair.

        ``key_manager`` encrypts those session keys at rest, exactly as
        the pairing coordinator does for a QR-paired peer.
        """
        self._relay_sender = relay_sender
        self._keywrap_private_key = keywrap_private_key
        self._keywrap_public_key = keywrap_public_key
        self._keywrap_sig = keywrap_sig
        self._key_manager = key_manager
        self._rate_limiter = rate_limiter

    def _bootstrap_ready(self) -> bool:
        """True when every piece the bootstrap path needs is wired."""
        return (
            self._relay_sender is not None
            and self._key_manager is not None
            and bool(self._keywrap_private_key)
            and bool(self._keywrap_public_key)
            and bool(self._keywrap_sig)
        )

    def attach_to(self, federation_service: "FederationService") -> None:
        """Wire the three inbound event-type handlers into the registry."""
        registry = federation_service._event_registry  # noqa: SLF001
        registry.register(
            FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
            self._on_redeem,
        )
        registry.register(
            FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
            self._on_redeem_ack,
        )
        registry.register(
            FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY,
            self._on_redeem_deny,
        )

    # ── Sender side ────────────────────────────────────────────────────

    async def request_redeem(
        self,
        token: str,
        *,
        viewer_user_id: str,
        issuer_instance_id: str,
        bootstrap: InviteBootstrapHint | None = None,
    ) -> dict:
        """Drive the receiver-side handshake.

        If the issuer is a CONFIRMED direct peer, ships REDEEM via
        the regular ``send_event`` path. Otherwise tries
        :class:`RouteDiscoveryService` to find a chain of confirmed
        peers leading to the issuer, then wraps REDEEM in
        :data:`FederationEventType.SPACE_ROUTED` and ships along
        that path. The ACK / DENY arrives via the reverse path and
        resolves the same nonce-keyed Future.

        When neither holds and the caller supplies a ``bootstrap`` hint
        (the public invite blob the redeemer opened), falls through to
        the §D2b **bootstrap** path: a sealed, self-authenticating
        redeem envelope relayed to the issuer by instance id, with no
        pre-existing relationship and no network address on either side.
        Both existing paths are tried first and unchanged — they are
        strictly better (the direct one needs no new trust, the mesh one
        leaks nothing to a third party).

        Returns ``{space_id, role}`` on ACK. Raises
        :class:`SpacePermissionError` on DENY (or "no route to issuer"
        when discovery fails), ``TimeoutError`` on no response within
        ``self._timeout``.
        """
        instance = await self._federation_repo.get_instance(issuer_instance_id)
        direct_peer = (
            instance is not None and instance.status is PairingStatus.CONFIRMED
        )

        # Look up the local user so we can ship their identity to the
        # issuer; the issuer needs a public_key + display_name to seat
        # us as a SpaceRemoteMember on its side.
        user = await self._users.get_by_user_id(viewer_user_id)
        if user is None:
            # Surfaces as 403 — a missing local user record on a
            # supposedly-authenticated request is a permission-shape
            # error, not a client validation problem.
            raise SpacePermissionError("local user not found")

        # The mesh-routing path is opt-in (the bootstrap injects it);
        # without it, fall back to direct-only semantics.
        mesh_available = (
            self._route_service is not None and self._routed_handler is not None
        )
        # §D2b — the bootstrap path needs the blob's hint (it carries
        # the issuer's identity + key-wrap material, which is the only
        # thing we can address and seal to) and it must describe the
        # issuer we were actually asked to redeem against.
        bootstrap_available = (
            bootstrap is not None
            and bootstrap.instance_id == issuer_instance_id
            and self._bootstrap_ready()
        )

        if not direct_peer and not mesh_available and not bootstrap_available:
            raise SpacePermissionError(
                "issuer instance is not a confirmed peer — pair first",
            )

        # Direct-peer fast-path: we already have an envelope route to
        # the issuer and can gate on their announced proto_version.
        # Pre-v_6 issuers don't know SPACE_INVITE_TOKEN_REDEEM. Don't
        # ship the envelope into a 10 s timeout — fail fast with a
        # message naming the right next step.
        if direct_peer:
            if not await self._federation.peer_supports(
                issuer_instance_id,
                min_version=FederationCapability.MIN_FOR_SPACE_INVITE_REDEEM,
            ):
                raise SpacePermissionError(
                    "issuer instance is on an older protocol version — "
                    "ask them to upgrade before redeeming this code",
                )

        route_path: list[str] | None = None
        target_eph_pk: str | None = None
        if not direct_peer and mesh_available:
            # Run mesh discovery. The discovery layer already gates
            # candidate hops on v_6 so we don't need to re-check
            # ``peer_supports`` here. The discovery returns the
            # target's ephemeral X25519 pub alongside the path — that
            # pub is what ``send_routed`` seals the inner payload
            # against, so relays never see the plaintext.
            assert self._route_service is not None
            discovery_result = await self._route_service.discover_route(
                issuer_instance_id,
            )
            if discovery_result is not None and len(discovery_result[0]) >= 2:
                route_path, target_eph_pk = discovery_result
            elif not bootstrap_available:
                raise SpacePermissionError(
                    "no route to issuer — pair with them, or with one of"
                    " their household's peers",
                )

        # §D2b bootstrap — no direct pair and no mesh chain, but we do
        # hold a valid invite blob. The token IS the authorization, so
        # ship a sealed redeem envelope through the relay.
        use_bootstrap = not direct_peer and route_path is None
        if use_bootstrap:
            assert bootstrap is not None
            result = await self._request_redeem_bootstrap(
                token,
                viewer_user_id=viewer_user_id,
                user=user,
                hint=bootstrap,
            )
            return await self._seat_local_after_ack(
                result,
                viewer_user_id=viewer_user_id,
                issuer_instance_id=issuer_instance_id,
                bootstrap=bootstrap,
            )

        nonce = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[nonce] = fut
        payload = {
            "redeem_nonce": nonce,
            "invite_token": token,
            "redeemer_user_id": viewer_user_id,
            "redeemer_display_name": (user.display_name or user.username),
            "redeemer_public_key": getattr(user, "public_key", None),
        }
        try:
            if direct_peer:
                await self._federation.send_event(
                    to_instance_id=issuer_instance_id,
                    event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM,
                    payload=payload,
                )
            else:
                assert self._routed_handler is not None
                assert self._route_service is not None
                assert route_path is not None
                assert target_eph_pk is not None
                # Pin the issuer identity pk discovery verified, so the
                # origin holds a SPACE_ROUTE_STALE nack for this send
                # against the key WE checked (same shape as
                # ``FederationService.send_with_mesh_fallback``).
                pinned_pk = (
                    self._route_service.cached_target_identity_pk(issuer_instance_id)
                    or ""
                )
                await self._routed_handler.send_routed(
                    path=route_path,
                    target_eph_pk_b64=target_eph_pk,
                    inner_event_type=(FederationEventType.SPACE_INVITE_TOKEN_REDEEM),
                    inner_payload=payload,
                    target_identity_pk=pinned_pk,
                )
            try:
                result = await asyncio.wait_for(fut, timeout=self._timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    "issuer did not respond to invite-token redeem",
                ) from exc
        finally:
            self._pending.pop(nonce, None)

        return await self._seat_local_after_ack(
            result,
            viewer_user_id=viewer_user_id,
            issuer_instance_id=issuer_instance_id,
        )

    async def _seat_local_after_ack(
        self,
        result: dict,
        *,
        viewer_user_id: str,
        issuer_instance_id: str,
        bootstrap: InviteBootstrapHint | None = None,
    ) -> dict:
        """Receiver-side persistence for an accepted redeem.

        Shared by all three outbound paths (direct peer, mesh-routed,
        §D2b bootstrap) so the §CP.F1 age gate, the §D1b anti-hijack
        check and the stub / membership / roster seating can never drift
        between them. ``bootstrap`` is set only on the §D2b path, where
        it also seats the space-scoped
        :class:`~socialhome.domain.federation.RemoteInstance` row for
        the issuer — every gate above it has already passed at that
        point, so a refused redeem creates no instance record.
        """

        # Receiver-side post-condition: register the (space_id, issuer)
        # mapping locally so subsequent space-scoped fan-outs include
        # the issuer's household.
        space_id = str(result.get("space_id") or "")
        role_str = str(result.get("role") or SpaceRole.MEMBER.value)
        if space_id:
            meta = result.get("space_meta")
            meta_dict = meta if isinstance(meta, dict) else None
            # §CP.F1 — refuse to seat an under-age protected minor in a
            # remote-hosted age-restricted space. Checked BEFORE any local
            # persistence (no space_instance mapping, no stub, no content
            # key, no membership) so a blocked minor gains nothing — not
            # even the ability to decrypt fan-out. The host's min_age rides
            # the ACK's space_meta; an older issuer that ships none → 0 →
            # allowed (the historical behaviour). The host may have seated
            # us as a remote member on the valid token; without our
            # space_instance + content key that residue is inert (it can
            # only ever receive undecryptable ciphertext).
            # Coerce defensively: a malicious issuer could ship a
            # non-conforming min_age; _coerce_min_age clamps to {0,13,16,18}
            # (out-of-set / garbage → 0) so int() can't raise here.
            min_age = _coerce_min_age(meta_dict.get("min_age")) if meta_dict else 0
            if self._child_protection is not None and not (
                await self._child_protection.is_age_allowed(viewer_user_id, min_age)
            ):
                log.warning(
                    "CP.F1: refusing invite-link redeem seat for under-age "
                    "user=%s in space=%s (min_age=%d)",
                    viewer_user_id,
                    space_id,
                    min_age,
                )
                raise SpacePermissionError(
                    f"This space is restricted to users aged {min_age}+."
                )
            # §D1b anti-hijack — refuse if a local space with this id is
            # already owned by a DIFFERENT host. Otherwise a malicious
            # issuer could ship an ACK whose space_id collides with a space
            # we hold under another host and clobber its config + content
            # key. Compared against issuer_instance_id (the authenticated
            # sender), never meta["owner_instance_id"] (issuer-controlled).
            # Checked before any persistence so a conflict seats nothing.
            if not await can_seat_remote_stub(
                self._spaces, space_id, issuer_instance_id
            ):
                log.warning(
                    "§D1b: refusing redeem seat — space=%s already owned "
                    "locally by another host, issuer=%s",
                    space_id,
                    issuer_instance_id,
                )
                raise SpacePermissionError(
                    "This invite points at a space that conflicts with one "
                    "you already belong to under a different host."
                )
            # §D2b — the issuer is not (and must not silently become)
            # a social peer, but the space still needs a keyed instance
            # row to federate against. Seated only after every gate
            # above has passed.
            if bootstrap is not None:
                await self._seat_space_session_instance(
                    instance_id=issuer_instance_id,
                    display_name=(bootstrap.display_hint or issuer_instance_id[:8]),
                    identity_pk=bootstrap.identity_pk,
                    keywrap_pk=bootstrap.keywrap_pk,
                    keywrap_sig=bootstrap.keywrap_sig,
                    proto_version=bootstrap.proto_version,
                    is_redeemer=True,
                    # The server that served the invite blob — the issuer
                    # answered on it, so it reaches them.
                    gfs_url=bootstrap.gfs_url,
                )
            await self._spaces.add_space_instance(
                space_id,
                issuer_instance_id,
            )
            # §D1b — seat a local stub + membership so the redeemer's
            # /api/spaces actually shows the joined space. The ACK
            # carries the host's metadata snapshot; older issuers that
            # don't ship it fall through to today's mapping-only
            # behaviour. SpaceMember is keyed on (space_id, user_id)
            # so a repeat redeem is a no-op via INSERT OR REPLACE.
            if meta_dict is not None:
                meta = meta_dict
                stub = stub_space_from_metadata(
                    space_id,
                    host_instance_id=issuer_instance_id,
                    meta=meta,
                )
                await self._spaces.save(stub)
                # §D1b cover bytes (#116) — persist host's WebP
                # when shipped inline so the stub renders properly.
                await apply_space_cover_from_metadata(
                    space_id,
                    meta=meta,
                    cover_repo=self._cover_repo,
                )
                await apply_space_icon_from_metadata(
                    space_id,
                    meta=meta,
                    icon_repo=self._icon_repo,
                )
                # §D1b space content key (#117) — persist the
                # receiver's local epoch key from the ACK so
                # subsequent SPACE_POST_CREATED decrypts succeed.
                await apply_space_content_key_from_metadata(
                    space_id,
                    meta=meta,
                    space_crypto_service=self._space_crypto,
                )
                await self._spaces.save_member(
                    SpaceMember(
                        space_id=space_id,
                        user_id=viewer_user_id,
                        role=role_str,
                        joined_at=datetime.now(timezone.utc).isoformat(),
                    )
                )
                # §D1b member-list mirror (#115) — seat every other
                # member of this space as a ``SpaceRemoteMember`` so
                # the Members tab on the receiver's stub shows the
                # full roster (the host, federated peers, …) rather
                # than just the redeemer. Skipping ``viewer_user_id``
                # because that lives in the local ``space_members``
                # row we just inserted.
                roster = meta.get("roster")
                if isinstance(roster, list):
                    for entry in roster:
                        if not isinstance(entry, dict):
                            continue
                        user_id = str(entry.get("user_id") or "")
                        inst_id = str(entry.get("instance_id") or "")
                        if not user_id or not inst_id or user_id == viewer_user_id:
                            continue
                        await self._remote_members.add(
                            space_id=space_id,
                            instance_id=inst_id,
                            user_id=user_id,
                            user_pk=(
                                str(entry["user_pk"]) if entry.get("user_pk") else None
                            ),
                            display_name=(
                                str(entry["display_name"])
                                if entry.get("display_name")
                                else None
                            ),
                        )
        return {
            "space_id": space_id,
            "role": role_str,
        }

    # ── Receiver side (the issuer in this exchange) ────────────────────

    async def _on_redeem(self, event: "FederationEvent") -> None:
        """Issuer-side: validate the token, seat the remote redeemer,
        send ACK. On any failure ship a DENY with a ``reason``.
        """
        p = event.payload
        nonce = str(p.get("redeem_nonce") or "")
        token = str(p.get("invite_token") or "")
        if not nonce or not token:
            log.debug(
                "SPACE_INVITE_TOKEN_REDEEM from %s missing nonce/token",
                event.from_instance,
            )
            return  # cannot DENY without a nonce to key the reply

        redeemer_user_id = str(p.get("redeemer_user_id") or "")
        redeemer_pk_raw = p.get("redeemer_public_key")
        redeemer_pk = str(redeemer_pk_raw) if redeemer_pk_raw else None
        redeemer_display_raw = p.get("redeemer_display_name")
        redeemer_display = str(redeemer_display_raw) if redeemer_display_raw else None

        # Routed redeem: when the inbound came via SPACE_ROUTED, the
        # synthesised event carries the forward-leg ``route_id``. The
        # ACK / DENY ships back via :meth:`send_routed_reply`, which
        # reuses the cached ephemeral keypair so the reply is sealed
        # under the target→origin key and travels the reverse of the
        # original path — a direct ship-back would fail (receiver
        # isn't a confirmed peer) and just leak a 10 s timeout to the
        # SPA.
        routed_route_id: str | None = None
        if (
            event.routed_path is not None
            and event.routed_route_id is not None
            and self._routed_handler is not None
        ):
            routed_route_id = event.routed_route_id

        if not redeemer_user_id:
            await self._send_deny(
                event.from_instance,
                nonce,
                REDEEM_DENY_REASON,
                routed_route_id=routed_route_id,
            )
            return

        ack_payload, deny_reason = await self._consume_seat_and_build_ack(
            token=token,
            redeemer_instance_id=event.from_instance,
            redeemer_user_id=redeemer_user_id,
            redeemer_pk=redeemer_pk,
            redeemer_display=redeemer_display,
        )
        if ack_payload is None:
            await self._send_deny(
                event.from_instance,
                nonce,
                deny_reason or REDEEM_DENY_REASON,
                routed_route_id=routed_route_id,
            )
            return
        ack_payload = {"redeem_nonce": nonce, **ack_payload}
        if routed_route_id is not None and self._routed_handler is not None:
            await self._routed_handler.send_routed_reply(
                route_id=routed_route_id,
                inner_event_type=(FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK),
                inner_payload=ack_payload,
            )
        else:
            await self._federation.send_event(
                to_instance_id=event.from_instance,
                event_type=FederationEventType.SPACE_INVITE_TOKEN_REDEEM_ACK,
                payload=ack_payload,
            )

    async def _consume_seat_and_build_ack(
        self,
        *,
        token: str,
        redeemer_instance_id: str,
        redeemer_user_id: str,
        redeemer_pk: str | None,
        redeemer_display: str | None,
    ) -> tuple[dict | None, str | None]:
        """Issuer-side authorization + seating for one redeem.

        Returns ``(ack_body, None)`` on success or ``(None, reason)`` on
        every denial. Shared verbatim by the §D2 event path
        (:meth:`_on_redeem`) and the §D2b bootstrap path so the atomic
        token consume, the §13.7 ban check and the remote-member seating
        can never diverge between them.

        ``consume_invite_token`` is a single atomic UPDATE guarded on
        ``uses_remaining > 0``, the expiry **and** the §13.7 ban, so an
        unknown, expired, exhausted or banned redeem returns ``None`` here
        without ever moving the counter.

        Every denial returns the same opaque
        :data:`REDEEM_DENY_REASON`. The three former "issuer storage error
        during …" strings named the stage that failed, and the ban check's
        own string told a stranger whether they were banned — an oracle on
        a surface anybody holding a link can reach. The detail lives in
        ``log.exception`` on the issuer, where it belongs.
        """
        try:
            row = await self._spaces.consume_invite_token(
                token,
                # Folds §13.7 into the same statement: a banned redeemer
                # never decrements the counter, so twenty requests can no
                # longer exhaust a twenty-use link (and the uniform reason
                # below means they learn nothing from trying).
                redeemer_user_id=redeemer_user_id,
            )
        except Exception:
            log.exception(
                "invite redeem: consume_invite_token raised for token from %s",
                redeemer_instance_id,
            )
            return None, REDEEM_DENY_REASON

        if row is None:
            return None, REDEEM_DENY_REASON

        space_id = str(row.get("space_id") or "")
        if not space_id:
            log.warning("invite redeem: consumed token row carried no space_id")
            return None, REDEEM_DENY_REASON

        # Seat the remote redeemer + register their instance so the
        # issuer's outbound fan-outs reach them.
        try:
            await self._remote_members.add(
                space_id=space_id,
                instance_id=redeemer_instance_id,
                user_id=redeemer_user_id,
                user_pk=redeemer_pk,
                display_name=redeemer_display,
            )
            await self._spaces.add_space_instance(
                space_id,
                redeemer_instance_id,
            )
        except Exception:
            log.exception(
                "invite redeem: seating remote member failed for"
                " space_id=%s instance=%s user_id=%s",
                space_id,
                redeemer_instance_id,
                redeemer_user_id,
            )
            return None, REDEEM_DENY_REASON

        # Pull the full space row so we can ship metadata + the member
        # roster back to the receiver. Without the meta the receiver's
        # stub card is blank; without the roster the receiver's Members
        # tab shows only herself (#115).
        space = await self._spaces.get(space_id)
        ack_body: dict = {
            "space_id": space_id,
            "role": SpaceRole.MEMBER.value,
        }
        if space is not None:
            ack_body["space_meta"] = await build_space_snapshot_for_federation(
                space,
                space_repo=self._spaces,
                remote_member_repo=self._remote_members,
                user_repo=self._users,
                own_instance_id=self._federation.own_instance_id,
                cover_repo=self._cover_repo,
                icon_repo=self._icon_repo,
                space_crypto_service=self._space_crypto,
            )
        return ack_body, None

    async def _on_redeem_ack(self, event: "FederationEvent") -> None:
        """Receiver-side: resolve the in-flight Future with the issuer's
        payload. No-op if the nonce isn't ours (late ACK after timeout).
        """
        p = event.payload
        nonce = str(p.get("redeem_nonce") or "")
        if not nonce:
            return
        fut = self._pending.get(nonce)
        if fut is None or fut.done():
            return
        fut.set_result(
            {
                "space_id": str(p.get("space_id") or ""),
                "role": str(p.get("role") or SpaceRole.MEMBER.value),
                # Forward the host's snapshot so request_redeem can seat the
                # local stub + membership + roster. Dropping it here was why
                # a cross-household invite-link redeem never surfaced the
                # space on the redeemer's side (the seating block keyed on
                # result["space_meta"] which was always absent).
                "space_meta": p.get("space_meta"),
            }
        )

    async def _on_redeem_deny(self, event: "FederationEvent") -> None:
        """Receiver-side: resolve the in-flight Future with a
        :class:`SpacePermissionError` carrying the issuer's reason.
        """
        p = event.payload
        nonce = str(p.get("redeem_nonce") or "")
        if not nonce:
            return
        fut = self._pending.get(nonce)
        if fut is None or fut.done():
            return
        reason = str(p.get("reason") or "invite redeem denied by issuer")
        fut.set_exception(SpacePermissionError(reason))

    # ── §D2b bootstrap redeem (no pre-existing relationship) ───────────

    async def _request_redeem_bootstrap(
        self,
        token: str,
        *,
        viewer_user_id: str,
        user: Any,
        hint: InviteBootstrapHint,
    ) -> dict:
        """Seal a redeem request to the issuer and await the sealed reply.

        The issuer is a stranger: no pairing, no mesh route, no address.
        What we do have is the invite blob, and **possession of a valid
        token is the authorization** — so the request authenticates
        itself (Ed25519 over canonical JSON with our own identity key,
        TOFU-verified by the issuer) and travels as an opaque blob the
        relay can only route, never read.

        Raises :class:`SpacePermissionError` on a refusal we can name
        locally (issuer too old, unsealed material, relay down, DENY
        from the issuer) and ``TimeoutError`` when no reply lands.
        """
        assert self._relay_sender is not None
        # Version gate. There is no peer row to run ``peer_supports``
        # against — which is exactly why the invite blob carries the
        # issuer's advertised proto_version. An older issuer has no
        # handler for this envelope and would simply drop it, so fail
        # with the unchanged "pair first" wording instead of burning a
        # timeout on it.
        if hint.proto_version < FederationCapability.MIN_FOR_INVITE_BOOTSTRAP_REDEEM:
            raise SpacePermissionError(
                "no route to issuer — pair with them, or with one of"
                " their household's peers",
            )

        local = await self._federation_repo.get_local_identity()
        own_display_name = str((local or {}).get("display_name") or "")
        nonce = uuid.uuid4().hex
        body = {
            "kind": KIND_REDEEM,
            "invite_token": token,
            "space_id": hint.space_id,
            "redeem_nonce": nonce,
            "ts": datetime.now(timezone.utc).isoformat(),
            "instance_id": self._federation.own_instance_id,
            "identity_pk": self._federation.own_identity_pk.hex(),
            "keywrap_pk": self._keywrap_public_key.hex(),
            "keywrap_sig": self._keywrap_sig,
            "display_name": own_display_name,
            "proto_version": OURS,
            "redeemer_user_id": viewer_user_id,
            "redeemer_display_name": (user.display_name or user.username),
            "redeemer_public_key": getattr(user, "public_key", None),
        }
        try:
            envelope = seal_bootstrap_envelope(
                body=body,
                identity_seed=self._federation.own_identity_seed,
                recipient_instance_id=hint.instance_id,
                recipient_identity_pk=hint.identity_pk,
                recipient_keywrap_pk=hint.keywrap_pk,
                recipient_keywrap_sig=hint.keywrap_sig,
            )
        except ValueError as exc:
            log.warning("invite bootstrap: refusing to seal redeem: %s", exc)
            raise SpacePermissionError(
                "this invite link's keys don't check out — ask for a fresh one",
            ) from exc

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[nonce] = fut
        self._bootstrap_hints[nonce] = hint
        try:
            delivered = await self._relay_sender.send_sealed_envelope(
                to_instance_id=hint.instance_id,
                envelope=envelope,
                # The blob was minted on (and served by) this connection
                # server, so it is the one relay we know reaches the
                # issuer — never fan the request out to the others.
                gfs_url=hint.gfs_url,
            )
            if not delivered:
                raise SpacePermissionError(
                    "couldn't reach the issuing household through the "
                    "connection server — try again later",
                )
            try:
                return await asyncio.wait_for(fut, timeout=self._timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    "issuer did not respond to invite-token redeem",
                ) from exc
        finally:
            self._pending.pop(nonce, None)
            self._bootstrap_hints.pop(nonce, None)

    async def handle_relayed_envelope(
        self,
        envelope: dict,
        *,
        gfs_url: str = "",
    ) -> dict:
        """Inbound entry point for one relayed §D2b envelope.

        ``gfs_url`` is the connection server the blob arrived on; the
        reply leg goes back out the same way, so a household paired with
        several servers answers on the one the requester can hear.

        The relay hands us an opaque blob with no idea which leg it is
        (that is the point — it is identity-free), so this one entry
        point opens it and dispatches on the inner ``kind``.

        Two families ride this socket:

        * **Bootstrap bodies** (``space_invite_bootstrap_redeem`` and its
          ack / deny). The §24.11 pipeline is deliberately **not** used
          for these: it resolves signing keys from a CONFIRMED
          ``remote_instances`` row, which by definition does not exist
          for a stranger. §11 pairing has the identical problem and
          solves it the same way — a self-signed, TOFU-verified body
          dispatched ahead of the pipeline (``docs/protocol/pairing.md``).
        * **Ordinary §24.11 envelopes** for a household seated from an
          invite link, sealed by
          :class:`~socialhome.federation.gfs_relay_transport
          .GfsRelayTransport` because the pair holds no address for each
          other. Those go straight into the **unmodified** pipeline
          (:meth:`FederationService.handle_inbound_rtc` — the
          lookup-by-instance-id variant, since a relayed envelope carries
          no inbox id) with every step intact: the row lookup, the
          peer-class gate, the timestamp window, the Ed25519 verify under
          the pair key, replay, decrypt, idempotency and the ban check
          all apply exactly as they do over RTC or the HTTPS inbox.
          Riding the relay buys no exemption — the one concession is the
          timestamp step's wider window for this transport, which the
          relay's own 24 h queue TTL requires and the replay cache's
          longer retention pays for (see
          :data:`~socialhome.federation.inbound_validator
          .RELAY_TIMESTAMP_SKEW_SECONDS`).

        The pipeline itself is untouched for every ordinary event.

        Raises :class:`ValueError` on any validation failure; the caller
        drops the blob.
        """
        if not self._bootstrap_ready():
            raise ValueError("invite bootstrap is not configured on this host")
        # Process-wide throttle BEFORE the unseal — the cheapest place
        # to shed a flood, and the only one available before we know
        # which family the blob belongs to.
        if self._rate_limiter is not None and not self._rate_limiter.is_allowed(
            "invite-bootstrap:inbound",
            limit=BOOTSTRAP_INBOUND_LIMIT,
            window_s=BOOTSTRAP_INBOUND_WINDOW_S,
        ):
            raise ValueError("invite bootstrap inbound rate limit exceeded")
        body = unseal_envelope_body(
            envelope=envelope,
            keywrap_private_key=self._keywrap_private_key,
        )
        if is_relay_envelope_body(body):
            # Its OWN bucket. The two families share this socket but not
            # their budgets: an active space's relayed traffic (posts,
            # roster, sync chunks) is orders of magnitude more frequent
            # than invite redeems, so one shared allowance meant ordinary
            # space federation could starve every redeem — or a redeem
            # flood could stall a space.
            self._check_family_limit(
                "envelopes",
                limit=RELAY_ENVELOPE_INBOUND_LIMIT,
            )
            # A §24.11 envelope for a link-joined peer. No bootstrap
            # validation applies (there is no ``redeem_nonce`` and the
            # inner envelope carries its own signature): hand it to the
            # pipeline, which is the authority on every check. No
            # per-sender throttle either — the only id available before
            # the pipeline runs is the envelope's UNVERIFIED
            # ``from_instance``, and keying a budget on that would let
            # anyone starve a household's traffic by claiming its id.
            # The process-wide limiter above is the shed.
            return await self._dispatch_relayed_federation_envelope(body)
        self._check_family_limit("bodies", limit=BOOTSTRAP_BODY_INBOUND_LIMIT)
        validate_bootstrap_body(
            body,
            expected_kinds=frozenset({KIND_REDEEM, KIND_REDEEM_ACK, KIND_REDEEM_DENY}),
        )
        # Per-sender throttle, once the signature has proven who the
        # sender is (an unverified id would let anyone burn somebody
        # else's budget).
        sender = str(body.get("instance_id") or "")
        if self._rate_limiter is not None and not self._rate_limiter.is_allowed(
            f"invite-bootstrap:sender:{sender}",
            limit=BOOTSTRAP_PER_SENDER_LIMIT,
            window_s=BOOTSTRAP_PER_SENDER_WINDOW_S,
        ):
            raise ValueError("invite bootstrap per-sender rate limit exceeded")

        # Replay guard on the nonce, on the same cache + durable table
        # the §24.11 pipeline uses for ``msg_id``. Namespaced so a
        # bootstrap nonce can never collide with an envelope msg_id.
        nonce = str(body.get("redeem_nonce") or "")
        if not nonce:
            raise ValueError("bootstrap body missing redeem_nonce")
        if await self._federation.note_replay_id(f"invite-bootstrap:{nonce}"):
            raise ValueError(f"Replay detected: bootstrap nonce={nonce!r}")

        if body["kind"] == KIND_REDEEM:
            return await self._handle_bootstrap_redeem(body, gfs_url=gfs_url)
        return await self._handle_bootstrap_reply(body)

    def _check_family_limit(self, family: str, *, limit: int) -> None:
        """Throttle one traffic family on the shared relay socket.

        ``family`` is ``"bodies"`` (invite-bootstrap) or ``"envelopes"``
        (relayed §24.11). Separate keys mean separate budgets — see
        :data:`BOOTSTRAP_BODY_INBOUND_LIMIT`.
        """
        if self._rate_limiter is None:
            return
        if not self._rate_limiter.is_allowed(
            f"invite-bootstrap:{family}",
            limit=limit,
            window_s=BOOTSTRAP_INBOUND_WINDOW_S,
        ):
            raise ValueError(f"invite bootstrap {family} rate limit exceeded")

    async def _dispatch_relayed_federation_envelope(self, body: dict) -> dict:
        """Run one relayed §24.11 envelope through the normal pipeline.

        The sender is taken from the envelope's own ``from_instance``
        and used only to LOOK UP the row; the pipeline's signature step
        then requires that row's identity key to have signed these exact
        bytes and rejects anything else, so a claimed id buys nothing.
        A household that is not this peer — or this peer with a bad
        signature — is dropped there, not here.
        """
        inner = body.get("envelope")
        if not isinstance(inner, dict):
            raise ValueError("relayed federation envelope missing envelope body")
        from_instance = inner.get("from_instance")
        if not isinstance(from_instance, str) or not from_instance:
            raise ValueError("relayed federation envelope missing from_instance")
        log.debug(
            "gfs_relay: inbound %r envelope from %s",
            inner.get("event_type"),
            from_instance,
        )
        try:
            return await self._federation.handle_inbound_rtc(
                from_instance,
                orjson.dumps(inner),
                # These bytes may have sat in the relay's queue for up to
                # its TTL before the socket came back, so the timestamp
                # step judges them against the wider relay window rather
                # than the ±300 s live-wire one.
                transport=TRANSPORT_GFS_RELAY,
            )
        except ValueError as exc:
            # A relay envelope that fails validation is dropped, and until
            # now it was dropped with nothing an operator could act on:
            # the raise travelled up to the WS client's generic
            # "handler raised" line, which names neither the event type
            # nor the sender. INFO because a rejection here is a normal,
            # expected outcome (a stale queued frame, an unknown peer);
            # the reason and the routing fields only — never the payload.
            log.info(
                "gfs_relay: rejected %r envelope from %s: %s",
                inner.get("event_type"),
                from_instance,
                exc,
            )
            raise

    async def _handle_bootstrap_redeem(self, body: dict, *, gfs_url: str = "") -> dict:
        """Issuer-side: authorize a stranger's sealed redeem, reply sealed.

        Every gate above this point (shape caps, ``derive_instance_id``
        anti-tamper, signature, timestamp, replay) already ran in
        :func:`~socialhome.federation.invite_bootstrap
        .open_bootstrap_envelope` / :meth:`handle_relayed_envelope`.
        What is left is the authorization itself: an atomic token
        consume, the ban check, and the seating — all shared with the
        §D2 path via :meth:`_consume_seat_and_build_ack`.
        """
        redeemer_instance_id = str(body["instance_id"])
        nonce = str(body["redeem_nonce"])
        # We can only answer a redeemer whose key-wrap key is genuinely
        # bound to the identity that signed the request. An unbound one
        # means either a corrupt blob or an attempt to have us seal the
        # space snapshot (content key included!) to somebody else's key.
        keywrap_pub = verify_peer_keywrap(
            instance_id=redeemer_instance_id,
            identity_pk=str(body.get("identity_pk") or ""),
            keywrap_pk=str(body.get("keywrap_pk") or ""),
            keywrap_sig=str(body.get("keywrap_sig") or ""),
        )
        if keywrap_pub is None:
            raise ValueError(
                "bootstrap redeem key-wrap key is not bound to its identity",
            )

        redeemer_user_id = str(body.get("redeemer_user_id") or "")
        if not redeemer_user_id:
            await self._send_bootstrap_deny(
                body,
                nonce,
                REDEEM_DENY_REASON,
            )
            return {"ok": True, "denied": True}

        ack_body, deny_reason = await self._consume_seat_and_build_ack(
            token=str(body.get("invite_token") or ""),
            redeemer_instance_id=redeemer_instance_id,
            redeemer_user_id=redeemer_user_id,
            redeemer_pk=(
                str(body["redeemer_public_key"])
                if body.get("redeemer_public_key")
                else None
            ),
            redeemer_display=(
                str(body["redeemer_display_name"])
                if body.get("redeemer_display_name")
                else None
            ),
        )
        if ack_body is None:
            await self._send_bootstrap_deny(
                body,
                nonce,
                deny_reason or REDEEM_DENY_REASON,
            )
            return {"ok": True, "denied": True}

        # Space-scoped instance row — NOT a social peer. Seated only
        # once the token has actually been consumed.
        await self._seat_space_session_instance(
            instance_id=redeemer_instance_id,
            display_name=str(body.get("display_name") or redeemer_instance_id[:8]),
            identity_pk=str(body["identity_pk"]),
            keywrap_pk=str(body["keywrap_pk"]),
            keywrap_sig=str(body["keywrap_sig"]),
            proto_version=int(body.get("proto_version") or 1),
            is_redeemer=False,
            # The server the request arrived on: the one relay we know
            # reaches this redeemer, and the one they are listening on.
            gfs_url=gfs_url,
        )
        await self._send_bootstrap_reply(
            body,
            {"kind": KIND_REDEEM_ACK, "redeem_nonce": nonce, **ack_body},
            gfs_url=gfs_url,
        )
        return {"ok": True, "space_id": ack_body["space_id"]}

    async def _handle_bootstrap_reply(self, body: dict) -> dict:
        """Redeemer-side: resolve the in-flight redeem on a sealed reply.

        The reply is pinned to the issuer identity the invite blob
        advertised — a body that opens, verifies and is in-window but
        comes from a *different* household than the one we addressed is
        dropped rather than resolving the Future.
        """
        nonce = str(body["redeem_nonce"])
        hint = self._bootstrap_hints.get(nonce)
        fut = self._pending.get(nonce)
        if hint is None or fut is None or fut.done():
            # Late reply after our timeout, or a nonce that was never
            # ours. Nothing to resolve.
            return {"ok": True, "stale": True}
        if str(body.get("instance_id") or "") != hint.instance_id:
            raise ValueError(
                "bootstrap reply came from a household we did not address",
            )
        if body["kind"] == KIND_REDEEM_DENY:
            # The issuer's own ``reason`` is logged, never surfaced. On this
            # leg the issuer is a household we met through a public link —
            # letting its string through would render attacker-authored text
            # in our SPA, and the wire reason is opaque by design anyway
            # (:data:`REDEEM_DENY_REASON`), so there is nothing to lose.
            log.info(
                "invite bootstrap: %s denied our redeem (%r)",
                hint.instance_id,
                str(body.get("reason") or "")[:200],
            )
            fut.set_exception(SpacePermissionError(BOOTSTRAP_DENY_CLIENT_MESSAGE))
            return {"ok": True, "denied": True}
        fut.set_result(
            {
                "space_id": str(body.get("space_id") or ""),
                "role": str(body.get("role") or SpaceRole.MEMBER.value),
                "space_meta": body.get("space_meta"),
            }
        )
        return {"ok": True}

    async def _send_bootstrap_deny(
        self,
        request_body: dict,
        nonce: str,
        reason: str,
        *,
        gfs_url: str = "",
    ) -> None:
        """Best-effort sealed DENY back to the redeemer.

        Same contract as :meth:`_send_deny` on the §D2 path — logged,
        never raised: a failed DENY just leaves the redeemer waiting out
        its own timeout, which is what a dropped frame looks like too.
        """
        try:
            await self._send_bootstrap_reply(
                request_body,
                {
                    "kind": KIND_REDEEM_DENY,
                    "redeem_nonce": nonce,
                    "reason": reason,
                },
                gfs_url=gfs_url,
            )
        except Exception:
            log.exception("invite bootstrap: DENY ship-back failed")

    async def _send_bootstrap_reply(
        self,
        request_body: dict,
        reply: dict,
        *,
        gfs_url: str = "",
    ) -> None:
        """Seal ``reply`` to the requester's key-wrap key and relay it."""
        assert self._relay_sender is not None
        local = await self._federation_repo.get_local_identity()
        envelope = seal_bootstrap_envelope(
            body={
                **reply,
                "ts": datetime.now(timezone.utc).isoformat(),
                "instance_id": self._federation.own_instance_id,
                "identity_pk": self._federation.own_identity_pk.hex(),
                "keywrap_pk": self._keywrap_public_key.hex(),
                "keywrap_sig": self._keywrap_sig,
                "display_name": str((local or {}).get("display_name") or ""),
                "proto_version": OURS,
            },
            identity_seed=self._federation.own_identity_seed,
            recipient_instance_id=str(request_body["instance_id"]),
            recipient_identity_pk=str(request_body["identity_pk"]),
            recipient_keywrap_pk=str(request_body["keywrap_pk"]),
            recipient_keywrap_sig=str(request_body["keywrap_sig"]),
        )
        await self._relay_sender.send_sealed_envelope(
            to_instance_id=str(request_body["instance_id"]),
            envelope=envelope,
            gfs_url=gfs_url,
        )

    async def _seat_space_session_instance(
        self,
        *,
        instance_id: str,
        display_name: str,
        identity_pk: str,
        keywrap_pk: str,
        keywrap_sig: str,
        proto_version: int,
        is_redeemer: bool,
        gfs_url: str = "",
    ) -> None:
        """Persist the counterpart as a **space-scoped** instance row.

        :data:`~socialhome.domain.federation.InstanceSource.SPACE_SESSION`
        is what makes this row space-scoped rather than a full social
        peer: DMs, the user roster, presence, the friends constellation,
        app / calendar peer pickers and the auto-pair vouching relay all
        read the *social* peer list, which excludes this source. Space
        federation — the space's own events, roster, content key, mesh
        relaying — reads the space's own instance list and works
        normally.

        Session keys come from a static-static X25519 exchange over the
        two households' published key-wrap keys (each already verified
        as bound to its identity), so the pair holds directional AES
        keys without a further round-trip. No ``PairingConfirmed`` is
        published — that event kicks off the user roster sync, DM
        history backfill and public-space snapshot, none of which a
        space-scoped relationship is entitled to.
        """
        assert self._key_manager is not None
        peer_keywrap_pub = verify_peer_keywrap(
            instance_id=instance_id,
            identity_pk=identity_pk,
            keywrap_pk=keywrap_pk,
            keywrap_sig=keywrap_sig,
        )
        if peer_keywrap_pub is None:
            raise ValueError(
                "refusing to seat a space-session instance whose key-wrap "
                "key is not bound to its identity",
            )
        key_self_to_remote, key_remote_to_self = derive_space_session_keys(
            own_keywrap_priv=self._keywrap_private_key,
            peer_keywrap_pub=peer_keywrap_pub,
            is_redeemer=is_redeemer,
        )
        existing = await self._federation_repo.get_instance(instance_id)
        if existing is not None:
            # Never downgrade or re-key an existing relationship (a real
            # pairing, or a second space joined with the same household)
            # off the back of an invite link.
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._federation_repo.save_instance(
            RemoteInstance(
                id=instance_id,
                display_name=display_name,
                remote_identity_pk=identity_pk,
                key_self_to_remote=self._key_manager.encrypt(key_self_to_remote),
                key_remote_to_self=self._key_manager.encrypt(key_remote_to_self),
                # No address is exchanged on this path by design — the
                # invite blob is public, so neither household publishes
                # an inbox URL to the other. See the "reaching a
                # bootstrap member" note in docs/protocol/invites.md.
                remote_inbox_url="",
                # The two halves of "how do I reach this household": the
                # connection server that introduced the pair, and the
                # static key-wrap key every envelope for them is sealed
                # to before that server carries it. Both are read back by
                # :class:`~socialhome.federation.gfs_relay_transport
                # .GfsRelayTransport` on every send, so both have to
                # survive a restart — the key-wrap key cannot be
                # re-derived from anything else on the row.
                relay_via=gfs_url or None,
                remote_keywrap_pk=peer_keywrap_pub.hex(),
                local_inbox_id=secrets.token_urlsafe(24),
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.SPACE_SESSION,
                # ``RemoteInstance.share_home`` defaults True — right for a
                # household you scanned a QR code with in your kitchen,
                # wrong for one that walked in off a public link. Our home
                # GPS is a social disclosure and this is not a social
                # relationship, so the seat starts closed; the owner can
                # open it per-peer from the SPA like any other.
                share_home=False,
                proto_version=proto_version,
                paired_at=now,
            )
        )

    # ── Internal helpers ───────────────────────────────────────────────

    async def _send_deny(
        self,
        to_instance_id: str,
        nonce: str,
        reason: str,
        *,
        routed_route_id: str | None = None,
    ) -> None:
        """Best-effort DENY ship-back. Logged but never raised — a
        failed DENY just leaves the receiver hanging until its timeout,
        which is the same outcome as the network dropping the frame.

        ``routed_route_id``, when set, ships the DENY back via
        :meth:`SpaceRoutedHandler.send_routed_reply` — required when
        the inbound came via SPACE_ROUTED because the receiver isn't
        a direct peer. The reply leg reuses the forward leg's
        ephemeral keypair so relays still see only ciphertext.
        """
        deny_payload = {
            "redeem_nonce": nonce,
            "reason": reason,
        }
        try:
            if routed_route_id is not None and self._routed_handler is not None:
                await self._routed_handler.send_routed_reply(
                    route_id=routed_route_id,
                    inner_event_type=(
                        FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY
                    ),
                    inner_payload=deny_payload,
                )
            else:
                await self._federation.send_event(
                    to_instance_id=to_instance_id,
                    event_type=(FederationEventType.SPACE_INVITE_TOKEN_REDEEM_DENY),
                    payload=deny_payload,
                )
        except Exception:
            log.exception(
                "SPACE_INVITE_TOKEN_REDEEM_DENY ship-back to %s failed",
                to_instance_id,
            )
